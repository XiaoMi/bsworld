# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.3: Connection parameter request and update."""

import struct
from dataclasses import replace

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_CHANNEL_MAP_SIZE,
    LL_CONN_INTERVAL_MAX,
    LL_CONN_INTERVAL_MIN,
    LL_CONN_INTERVAL_UNIT_US,
    LL_CONN_WINDOW_SIZE_MAX,
    LL_CONN_WINDOW_SIZE_MIN,
    LL_DATA_CHANNEL_COUNT,
    LL_EVENT_COUNTER_MASK,
    LL_INSTANT_MIN_LISTENING_EVENTS,
    LL_RESPONSE_TIMEOUT_US,
    LL_SCA_50_PPM,
    LL_SCA_500_PPM,
    LL_SCA_PPM,
    LL_SUPERVISION_TIMEOUT_MAX,
    LL_SUPERVISION_TIMEOUT_MIN,
    LL_SUPERVISION_TIMEOUT_UNIT_US,
    PDU_CONTROL_CONN_PARAM_OFFSET_COUNT,
    PDU_CONTROL_CONN_PARAM_OFFSET_UNSPECIFIED,
    PDU_CONTROL_FEATURE_SET_SIZE,
    PDU_CONTROL_INSTANT_SIZE,
    PDU_DATA_LLID_MASK,
    PDU_HEADER_SIZE,
    LlId,
)
from bscontroller.linklayer.timing import LL_CLOCK_JITTER_US
from bscontroller.radio.base import PHY_TIME_UNITS_PER_SECOND
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.connection import (
    HCI_OBSERVATION_STEP_US,
    PROCEDURE_COLLISION,
    ErrorCode,
    Opcode,
    _accept_remote_parameters,
    _advertiser_connection_parameters,
    _check_parameter_choice,
    _check_parameter_pdu,
    _check_rf_stopped,
    _disconnected,
    _parameter_request,
    _procedure_timeout,
    _restore_peer,
    _select_peer_interval,
    _sequence_bits,
)
from tests.qualification.link import (
    _check_parameter_update,
    _data_pdu,
    _EncryptedPeer,
    _wire_event,
    start_encryption,
)
from tests.qualification.peer import establish, peer_send
from tests.qualification.tester import (
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
)

PROCEDURES = ProcedureMap()


async def _local_parameters(tester, *, central, reject=False):
    """Initiate a maximum-interval Central update or three Peripheral requests
    through HCI."""
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    if central:
        await link.control(
            Opcode.LL_FEATURE_RSP,
            bytes([Opcode.LL_PERIPHERAL_FEATURE_REQ])
            + bytes(PDU_CONTROL_FEATURE_SET_SIZE),
        )
    for interval, timeout in (
        ((LL_CONN_INTERVAL_MIN, 300),)
        if reject
        else (
            ((LL_CONN_INTERVAL_MAX, LL_SUPERVISION_TIMEOUT_MAX),)
            if central
            else (
                (LL_CONN_INTERVAL_MIN, 300),
                (LL_CONN_INTERVAL_MAX, LL_SUPERVISION_TIMEOUT_MAX),
                (LL_CONN_INTERVAL_MIN, 300),
            )
        )
    ):
        await tester.command(
            hci.HCI_LE_Connection_Update_Command(
                link.handle, interval, interval, 0, timeout, 0, 0
            )
        )
        if central:
            update = await link.control(Opcode.LL_CONNECTION_UPDATE_IND)
            assert struct.unpack('<BHHHHH', update[1:])[2:5] == (interval, 0, timeout)
        else:
            request = await link.control(Opcode.LL_CONNECTION_PARAM_REQ)
            minimum, _, latency, supervision, *_ = _check_parameter_pdu(request)
            if reject:
                await link.exchange(
                    bytes(
                        [
                            Opcode.LL_REJECT_EXT_IND,
                            Opcode.LL_CONNECTION_PARAM_REQ,
                            ErrorCode.COMMAND_DISALLOWED_ERROR,
                        ]
                    ),
                    llid=LlId.CONTROL,
                    check_ack=True,
                )
                event = await tester.event(hci.HCI_LE_Connection_Update_Complete_Event)
                assert (event.status, event.connection_handle) == (
                    ErrorCode.COMMAND_DISALLOWED_ERROR,
                    link.handle,
                )
                await link.exchange()
                assert not any(
                    isinstance(event, hci.HCI_LE_Connection_Update_Complete_Event)
                    for event in tester.events
                ), 'rejected parameter request produced another completion'
                break
            interval, timeout = (minimum, supervision)
            update = bytes([Opcode.LL_CONNECTION_UPDATE_IND]) + struct.pack(
                '<BHHHHH',
                LL_CONN_WINDOW_SIZE_MIN,
                0,
                interval,
                latency,
                timeout,
                link.counter + LL_INSTANT_MIN_LISTENING_EVENTS & LL_EVENT_COUNTER_MASK,
            )
            await link.exchange(update, llid=LlId.CONTROL, check_ack=True)
        await _check_parameter_update(tester, link, update, interval, timeout)
    await link.disconnect(local=True)


async def _parameter_collision(tester, *, central=False, channel_map=False):
    """Exercise the role-specific parameter/channel-map collision on an active ACL."""
    import pytest

    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    interval, timeout = (LL_CONN_INTERVAL_MIN, 300)
    new_map = sum(1 << channel for channel in range(0, LL_DATA_CHANNEL_COUNT, 2))
    reason = (
        ErrorCode.DIFFERENT_TRANSACTION_COLLISION_ERROR
        if central or channel_map
        else PROCEDURE_COLLISION
    )
    if central:
        for attempt in range(20):
            candidate = new_map if not attempt else new_map & ~(1 << 2 * (attempt - 1))
            result = await tester.command(
                hci.HCI_LE_Set_Host_Channel_Classification_Command(
                    candidate.to_bytes(LL_CHANNEL_MAP_SIZE, 'little')
                ),
                status=tuple(ErrorCode),
            )
            if result.status == ErrorCode.SUCCESS:
                break
            deadline = tester.radio.now_us + 5000000
            while tester.radio.now_us < deadline:
                await link.exchange()
        else:
            pytest.skip('Inconclusive: all twenty Host channel classifications failed')
        update = await link.control(
            Opcode.LL_CHANNEL_MAP_IND,
            response=_parameter_request(link, interval, timeout),
            required=False,
        )
        if update is None:
            pytest.skip('Inconclusive: Controller chose not to send LL_CHANNEL_MAP_IND')
        response = await link.control(Opcode.LL_REJECT_EXT_IND)
        assert response == bytes(
            [Opcode.LL_REJECT_EXT_IND, Opcode.LL_CONNECTION_PARAM_REQ, reason]
        )
    else:
        await tester.command(
            hci.HCI_LE_Connection_Update_Command(
                link.handle, interval, interval, 0, timeout, 0, 0
            )
        )
        request = await link.control(Opcode.LL_CONNECTION_PARAM_REQ)
        _check_parameter_pdu(request)
        if channel_map:
            update = (
                bytes([Opcode.LL_CHANNEL_MAP_IND])
                + new_map.to_bytes(LL_CHANNEL_MAP_SIZE, 'little')
                + (
                    link.counter + LL_INSTANT_MIN_LISTENING_EVENTS
                    & LL_EVENT_COUNTER_MASK
                ).to_bytes(PDU_CONTROL_INSTANT_SIZE, 'little')
            )
            await link.exchange(update, llid=LlId.CONTROL, check_ack=True)
        else:
            packet = await link.exchange(
                _parameter_request(link, interval, timeout),
                llid=LlId.CONTROL,
                check_ack=True,
            )
            assert not (
                packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
                and packet.pdu[PDU_HEADER_SIZE] == Opcode.LL_REJECT_EXT_IND
            ), 'Peripheral rejected the Central request instead of preserving it'
        await link.exchange(
            bytes([Opcode.LL_REJECT_EXT_IND, Opcode.LL_CONNECTION_PARAM_REQ, reason]),
            llid=LlId.CONTROL,
            check_ack=True,
        )
        if channel_map:
            event = await tester.event(hci.HCI_LE_Connection_Update_Complete_Event)
            assert (event.connection_handle, event.status) == (link.handle, reason)
        else:
            failures = [
                event
                for event in tester.events
                if isinstance(event, hci.HCI_LE_Connection_Update_Complete_Event)
            ]
            assert all(
                (event.connection_handle, event.status) == (link.handle, reason)
                for event in failures
            )
            for event in failures:
                tester.events.remove(event)
            await _accept_remote_parameters(
                tester, link, interval, timeout, optional_error=reason
            )
            assert not any(
                isinstance(event, hci.HCI_LE_Remote_Connection_Parameter_Request_Event)
                for event in tester.events
            )
            await link.disconnect(local=True)
            return
    wire_map = int.from_bytes(update[1:-PDU_CONTROL_INSTANT_SIZE], 'little')
    assert wire_map.bit_count() >= 2 and (not wire_map >> LL_DATA_CHANNEL_COUNT)
    await link.apply_update(update)
    for _ in range(3):
        await link.exchange()
    assert not any(
        isinstance(
            event,
            (
                hci.HCI_LE_Remote_Connection_Parameter_Request_Event,
                hci.HCI_LE_Connection_Update_Complete_Event,
            ),
        )
        for event in tester.events
    ), 'channel-map collision leaked a Host parameter event'
    result = await tester.command(hci.HCI_LE_Read_Channel_Map_Command(link.handle))
    assert result.connection_handle == link.handle
    assert int.from_bytes(result.channel_map, 'little') == link.parameters.channel_map
    await peer_send(tester, link, b'after-channel-map-update')
    await link.disconnect(local=True)


async def _remote_parameters(
    tester, *, central, reject=False, masked=False, invalid=False
):
    """Request 6/3200/6 interval updates remotely; accept, reject or mask the Host
    event; verify RF instants."""
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    if masked:
        await tester.masks(
            le=(1 << 64) - 1
            & ~(1 << hci.HCI_LE_REMOTE_CONNECTION_PARAMETER_REQUEST_EVENT - 1)
        )
    for interval, timeout in (
        ((6, 300),)
        if reject or masked or invalid
        else ((6, 300), (3200, 3200), (6, 300))
    ):
        request = _parameter_request(link, interval if not invalid else 4, timeout)
        if invalid or masked:
            response = await link.control(Opcode.LL_REJECT_EXT_IND, request)
            assert response[1:] == bytes(
                [
                    Opcode.LL_CONNECTION_PARAM_REQ,
                    (
                        ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR
                        if invalid
                        else ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
                    ),
                ]
            )
            for _ in range(3):
                await link.exchange()
            assert not any(
                isinstance(
                    e,
                    (
                        hci.HCI_LE_Remote_Connection_Parameter_Request_Event,
                        hci.HCI_LE_Connection_Update_Complete_Event,
                    ),
                )
                for e in tester.events
            )
            break
        await link.exchange(request, llid=LlId.CONTROL)
        if reject:
            event = await tester.event(
                hci.HCI_LE_Remote_Connection_Parameter_Request_Event
            )
            assert (
                event.connection_handle,
                event.interval_min,
                event.interval_max,
                event.max_latency,
                event.timeout,
            ) == (link.handle, interval, interval, 0, timeout)
            reason = ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR
            result = await tester.command(
                hci.HCI_LE_Remote_Connection_Parameter_Request_Negative_Reply_Command(
                    link.handle, reason
                )
            )
            assert result.connection_handle == link.handle
            response = await link.control(Opcode.LL_REJECT_EXT_IND)
            assert response[1:] == bytes([Opcode.LL_CONNECTION_PARAM_REQ, reason])
            for _ in range(3):
                await link.exchange()
            assert not any(
                isinstance(e, hci.HCI_LE_Connection_Update_Complete_Event)
                for e in tester.events
            )
        else:
            await _accept_remote_parameters(tester, link, interval, timeout)
    await link.disconnect(local=True)


async def _central_parameters(tester, *, reject=False, collision=False, legacy=False):
    """Accept four rounds or exercise rejection, collision or legacy fallback."""
    link = await establish(
        tester, central=True, peer_address=LOWER_PERIPHERAL_PUBLIC_ADDRESS
    )
    await link.control(
        Opcode.LL_FEATURE_RSP,
        bytes([Opcode.LL_PERIPHERAL_FEATURE_REQ])
        + int(hci.LeFeatureMask.CONNECTION_PARAMETERS_REQUEST_PROCEDURE).to_bytes(
            PDU_CONTROL_FEATURE_SET_SIZE, 'little'
        ),
    )
    rounds = (
        ((LL_CONN_INTERVAL_MIN, 300),)
        if reject or collision or legacy
        else (
            (LL_CONN_INTERVAL_MIN, 300),
            (LL_CONN_INTERVAL_MAX, LL_SUPERVISION_TIMEOUT_MAX),
            (LL_CONN_INTERVAL_MIN, 300),
            (2400, LL_SUPERVISION_TIMEOUT_MAX),
        )
    )
    for index, (interval, timeout) in enumerate(rounds):
        latency = 0
        if index == 3:
            # Case 4: 3 s, or 2 s if already using 3 s; retain current latency.
            interval = 1600 if link.parameters.interval == 2400 else 2400
            latency = link.parameters.latency
        await tester.command(
            hci.HCI_LE_Connection_Update_Command(
                link.handle, interval, interval, latency, timeout, 0, 0
            )
        )
        request = await link.control(Opcode.LL_CONNECTION_PARAM_REQ)
        fields = _check_parameter_pdu(request)
        if collision:
            peer_request = _parameter_request(link, interval, timeout)
            rejection = await link.control(Opcode.LL_REJECT_EXT_IND, peer_request)
            assert rejection == bytes(
                [
                    Opcode.LL_REJECT_EXT_IND,
                    Opcode.LL_CONNECTION_PARAM_REQ,
                    PROCEDURE_COLLISION,
                ]
            )
        if reject or legacy:
            reason = (
                ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
                if legacy
                else ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR
            )
            response = bytes(
                [Opcode.LL_REJECT_EXT_IND, Opcode.LL_CONNECTION_PARAM_REQ, reason]
            )
        else:
            minimum, maximum, latency, timeout, periodicity, reference, *offsets = (
                fields
            )
            if index == 3:
                # Change the reference without retaining offsets tied to the old one.
                reference = (link.counter - 1) & LL_EVENT_COUNTER_MASK
                if reference == fields[5]:
                    reference = (reference - 1) & LL_EVENT_COUNTER_MASK
                offsets = [
                    PDU_CONTROL_CONN_PARAM_OFFSET_UNSPECIFIED
                ] * PDU_CONTROL_CONN_PARAM_OFFSET_COUNT
            response = bytes([Opcode.LL_CONNECTION_PARAM_RSP]) + struct.pack(
                '<HHHHBH6H',
                minimum,
                maximum,
                latency,
                timeout,
                periodicity,
                reference,
                *offsets,
            )
        update = await link.control(
            Opcode.LL_CONNECTION_UPDATE_IND,
            response,
            check_ack=True,
            required=not reject,
        )
        if reject and update is None:
            event = await tester.event(hci.HCI_LE_Connection_Update_Complete_Event)
            assert (event.status, event.connection_handle) == (reason, link.handle)
            for _ in range(3):
                packet = await link.exchange()
                assert not (
                    packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
                    and packet.pdu[PDU_HEADER_SIZE] == Opcode.LL_CONNECTION_UPDATE_IND
                ), 'update followed the rejected-procedure completion'
            assert not any(
                isinstance(event, hci.HCI_LE_Connection_Update_Complete_Event)
                for event in tester.events
            )
        else:
            assert not any(
                isinstance(event, hci.HCI_LE_Connection_Update_Complete_Event)
                for event in tester.events
            ), 'procedure completion before the update Instant'
            interval, timeout = _check_parameter_choice(
                update, None if reject or legacy else fields
            )
            await _check_parameter_update(tester, link, update, interval, timeout)
    await link.disconnect()


async def _parameter_control(link, expected, *, without_features=False):
    """Reject a forbidden procedure immediately rather than ignoring it until
    timeout."""
    for _ in range(16):
        packet = await link.exchange()
        if packet.pdu[0] & PDU_DATA_LLID_MASK != LlId.CONTROL:
            continue
        payload = packet.pdu[PDU_HEADER_SIZE:]
        if payload[0] == expected:
            return payload
        if without_features and payload[0] in (
            Opcode.LL_FEATURE_REQ,
            Opcode.LL_PERIPHERAL_FEATURE_REQ,
        ):
            if link.central:
                pytest.skip(
                    'Inconclusive: autonomous Central feature exchange before PARAM_REQ'
                )
            await link.exchange(
                bytes([Opcode.LL_UNKNOWN_RSP, payload[0]]), llid=LlId.CONTROL
            )
        else:
            pytest.fail(
                f'expected {expected.name}, received control opcode0x{payload[0]:02x}'
            )
    pytest.fail(f'no {expected.name} within sixteen connection events')


async def _parameter_encryption_collision(tester, *, central):
    """Complete encryption and parameter negotiation in the prescribed collision
    order."""
    link = _EncryptedPeer(
        await establish(
            tester,
            central=central,
            peer_address=(
                LOWER_PERIPHERAL_PUBLIC_ADDRESS
                if central
                else LOWER_CENTRAL_PUBLIC_ADDRESS
            ),
        )
    )
    interval, timeout = (LL_CONN_INTERVAL_MIN, 300)
    if central:
        await start_encryption(
            tester, link, parameter_request=_parameter_request(link, interval, timeout)
        )
        await _accept_remote_parameters(tester, link, interval, timeout)
    else:
        await tester.command(
            hci.HCI_LE_Connection_Update_Command(
                link.handle, interval, interval, 0, timeout, 0, 0
            )
        )
        request = await link.control(Opcode.LL_CONNECTION_PARAM_REQ)
        interval, _, latency, timeout, *_ = _check_parameter_pdu(request)
        requested_at = tester.radio.now_us
        await start_encryption(tester, link)
        assert tester.radio.now_us - requested_at < LL_RESPONSE_TIMEOUT_US
        update = bytes([Opcode.LL_CONNECTION_UPDATE_IND]) + struct.pack(
            '<BHHHHH',
            LL_CONN_WINDOW_SIZE_MIN,
            0,
            interval,
            latency,
            timeout,
            link.counter + LL_INSTANT_MIN_LISTENING_EVENTS & LL_EVENT_COUNTER_MASK,
        )
        await link.exchange(update, llid=LlId.CONTROL, check_ack=True)
        await _check_parameter_update(tester, link, update, interval, timeout)
    await link.exchange()
    await link.disconnect()


async def _preferred_parameters(tester, *, central, anchors_only=False, anchors=False):
    """Accept preferred anchor points and preferred interval periodicity on the air."""
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    rounds = (
        ((1,), (link.parameters.interval - 1,), (1, 2))
        if anchors_only
        else ((1,) if anchors else (0,) if central else (),)
    )
    for offsets in rounds:
        minimum = maximum = link.parameters.interval
        latency, timeout = (link.parameters.latency, link.parameters.timeout)
        periodicity = 0
        if not anchors_only:
            minimum, maximum, latency, timeout, periodicity = (13, 19, 0, 300, 18)
        reference = link.counter
        reference_anchor = link.anchor
        request = bytes([Opcode.LL_CONNECTION_PARAM_REQ]) + struct.pack(
            '<HHHHBH6H',
            minimum,
            maximum,
            latency,
            timeout,
            periodicity,
            reference,
            *offsets,
            *[PDU_CONTROL_CONN_PARAM_OFFSET_UNSPECIFIED]
            * (PDU_CONTROL_CONN_PARAM_OFFSET_COUNT - len(offsets)),
        )
        reply = None
        if anchors_only:
            reply = await link.control(
                (
                    Opcode.LL_CONNECTION_UPDATE_IND
                    if central
                    else Opcode.LL_CONNECTION_PARAM_RSP
                ),
                request,
            )
            assert not any(
                isinstance(event, hci.HCI_LE_Remote_Connection_Parameter_Request_Event)
                for event in tester.events
            )
        else:
            await link.exchange(request, llid=LlId.CONTROL)
            event = await tester.event(
                hci.HCI_LE_Remote_Connection_Parameter_Request_Event
            )
            assert (
                event.connection_handle,
                event.interval_min,
                event.interval_max,
                event.max_latency,
                event.timeout,
            ) == (link.handle, minimum, maximum, latency, timeout)
            result = await tester.command(
                hci.HCI_LE_Remote_Connection_Parameter_Request_Reply_Command(
                    link.handle, minimum, maximum, latency, timeout, 0, 0
                )
            )
            assert result.connection_handle == link.handle
        if central:
            update = (
                reply
                if reply is not None
                else await link.control(Opcode.LL_CONNECTION_UPDATE_IND)
            )
            interval, timeout = _check_parameter_choice(
                update, (minimum, maximum, latency, timeout)
            )
        else:
            response = (
                reply
                if reply is not None
                else await link.control(Opcode.LL_CONNECTION_PARAM_RSP)
            )
            fields = _check_parameter_pdu(response)
            if anchors_only:
                interval = link.parameters.interval
                assert fields[0] <= interval <= fields[1]
                assert fields[2:4] == (latency, timeout)
            else:
                interval = _select_peer_interval(fields[0], fields[1], fields[4])
                latency, timeout = fields[2:4]
            instant = (
                link.counter + LL_INSTANT_MIN_LISTENING_EVENTS & LL_EVENT_COUNTER_MASK
            )
            offset = (
                (
                    offsets[0]
                    - (instant - reference & LL_EVENT_COUNTER_MASK)
                    * link.parameters.interval
                )
                % interval
                if offsets
                else 0
            )
            update = bytes([Opcode.LL_CONNECTION_UPDATE_IND]) + struct.pack(
                '<BHHHHH',
                LL_CONN_WINDOW_SIZE_MIN,
                offset,
                interval,
                latency,
                timeout,
                instant,
            )
            await link.exchange(update, llid=LlId.CONTROL, check_ack=True)
        await _check_parameter_update(
            tester, link, update, interval, timeout, notify=not anchors_only
        )
        if central:
            phase = (link.last_received.start_us - reference_anchor) % (
                interval * LL_CONN_INTERVAL_UNIT_US
            )
            if anchors_only:
                assert phase == offsets[0] * LL_CONN_INTERVAL_UNIT_US
            tester.output.write(
                f'PARAMETERS interval={interval} preferred_periodicity={periodicity} '
                f'anchor_phase_us={phase}\n'
            )
    await link.disconnect()


async def _unsupported_parameters(tester, *, central, features):
    """Learn an unsupported peer procedure without retrying it on the same ACL."""
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    initial = (
        link.last_received.pdu[PDU_HEADER_SIZE:]
        if link.last_received.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
        else b''
    )
    if (
        not features
        and initial
        and initial[0] in (Opcode.LL_FEATURE_REQ, Opcode.LL_PERIPHERAL_FEATURE_REQ)
    ):
        if central:
            pytest.skip(
                'Inconclusive: autonomous Central feature exchange during setup'
            )
        await link.exchange(
            bytes([Opcode.LL_UNKNOWN_RSP, initial[0]]), llid=LlId.CONTROL
        )
    if features:
        peer_features = bytes(PDU_CONTROL_FEATURE_SET_SIZE)
        if central:
            autonomous = initial[:1] == bytes([Opcode.LL_FEATURE_REQ])
            if autonomous:
                request = initial
            else:
                await tester.command(
                    hci.HCI_LE_Read_Remote_Features_Command(link.handle)
                )
                request = await _parameter_control(link, Opcode.LL_FEATURE_REQ)
            assert len(request) == 1 + PDU_CONTROL_FEATURE_SET_SIZE
            await link.exchange(
                bytes([Opcode.LL_FEATURE_RSP]) + peer_features, llid=LlId.CONTROL
            )
            if autonomous:
                await link.exchange()
            else:
                event = await tester.event(
                    hci.HCI_LE_Read_Remote_Features_Complete_Event
                )
                assert (event.status, event.connection_handle, event.le_features) == (
                    ErrorCode.SUCCESS,
                    link.handle,
                    peer_features,
                )
        else:
            if initial[:1] == bytes([Opcode.LL_PERIPHERAL_FEATURE_REQ]):
                assert len(initial) == 1 + PDU_CONTROL_FEATURE_SET_SIZE
                await link.exchange(
                    bytes([Opcode.LL_FEATURE_RSP]) + peer_features, llid=LlId.CONTROL
                )
            else:
                response = await link.control(
                    Opcode.LL_FEATURE_RSP,
                    bytes([Opcode.LL_FEATURE_REQ]) + peer_features,
                )
                assert len(response) == 1 + PDU_CONTROL_FEATURE_SET_SIZE
            await link.exchange()
    commands = (
        ((LL_CONN_INTERVAL_MIN, 300), (12, 400))
        if central and (not features)
        else ((LL_CONN_INTERVAL_MIN, 300),)
    )
    for index, (interval, timeout) in enumerate(commands):
        command = hci.HCI_LE_Connection_Update_Command(
            link.handle, interval, interval, 0, timeout, 0, 0
        )
        if not central and features:
            status = await tester.command(
                command,
                status=(ErrorCode.SUCCESS, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR),
            )
        else:
            status = await tester.command(command)
        if not features and index == 0:
            request = await _parameter_control(
                link, Opcode.LL_CONNECTION_PARAM_REQ, without_features=True
            )
            _check_parameter_pdu(request)
            await link.exchange(
                bytes([Opcode.LL_UNKNOWN_RSP, Opcode.LL_CONNECTION_PARAM_REQ]),
                llid=LlId.CONTROL,
            )
            if not central:
                failure = await tester.event(
                    hci.HCI_LE_Connection_Update_Complete_Event
                )
                assert (failure.status, failure.connection_handle) == (
                    ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR,
                    link.handle,
                )
                status = await tester.command(
                    command,
                    status=(
                        ErrorCode.SUCCESS,
                        ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR,
                    ),
                )
        if central:
            update = await _parameter_control(link, Opcode.LL_CONNECTION_UPDATE_IND)
            interval, timeout = _check_parameter_choice(update)
            await _check_parameter_update(tester, link, update, interval, timeout)
        else:
            if status.status == ErrorCode.SUCCESS:
                failure = await tester.event(
                    hci.HCI_LE_Connection_Update_Complete_Event
                )
                assert (failure.status, failure.connection_handle) == (
                    ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR,
                    link.handle,
                )
            for _ in range(16):
                packet = await link.exchange()
                assert not (
                    packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
                    and packet.pdu[PDU_HEADER_SIZE] == Opcode.LL_CONNECTION_PARAM_REQ
                ), 'known unsupported PARAM_REQ was sent again'
        assert not any(
            isinstance(event, hci.HCI_LE_Connection_Update_Complete_Event)
            for event in tester.events
        ), 'unexpected or duplicate parameter completion'
    await link.disconnect()


async def _past_update_outcome(link, payload, *, allow_maintenance):
    """Accept only the source's disconnect or acknowledged-maintenance alternatives."""
    tester = link.tester
    sent_sn = link.sn
    packet, sent = await _wire_event(
        link,
        lambda _: _data_pdu(link.sn, link.nesn, payload, llid=LlId.CONTROL),
        required=False,
    )
    deadline = (
        link.last_sent_end_us + link.parameters.timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
    )
    acknowledged = packet is not None and _sequence_bits(packet)[1] != sent_sn
    pending = b'' if acknowledged else payload
    maintained = True
    if packet is not None:
        _restore_peer(link, packet, sent)
    while True:
        event = _disconnected(tester)
        if event is not None:
            assert tester.radio.now_us < deadline, 'late past-Instant disconnection'
            assert (event.status, event.connection_handle, event.reason) == (
                ErrorCode.SUCCESS,
                link.handle,
                ErrorCode.INSTANT_PASSED_ERROR,
            )
            await _check_rf_stopped(link)
            return
        if tester.radio.now_us >= deadline:
            break
        if link.anchor + HCI_OBSERVATION_STEP_US >= deadline:
            await tester.wait(deadline - tester.radio.now_us)
            continue
        sent_sn = link.sn
        packet, sent = await _wire_event(
            link,
            lambda _, data=pending: _data_pdu(
                link.sn,
                link.nesn,
                data,
                llid=LlId.CONTROL if data else LlId.CONTINUATION,
            ),
            required=False,
        )
        maintained &= packet is not None
        if packet is not None:
            if pending and _sequence_bits(packet)[1] != sent_sn:
                acknowledged = True
                pending = b''
            _restore_peer(link, packet, sent)
    assert allow_maintenance, 'past connection update did not disconnect'
    assert acknowledged, 'maintained connection did not acknowledge the channel map'
    assert maintained, 'connection was not maintained after the invalid channel map'
    await link.disconnect(local=True)


async def accepting_parameter_update(tester):
    """Accepting Parameter Update.

    Initial Condition:
        An unencrypted connected public Peripheral with common connection and
        channel parameters. The Lower Central advertises 500-ppm SCA.
        Before Step 1, update common parameters to 100-ms interval, latency 2,
        timeout 3 s with an Instant 100 events ahead, then return to common
        parameters with an Instant ten events ahead, as the procedure specifies.

    Test Steps:
        1. Send LL_CONNECTION_UPDATE_IND selecting minimum interval, offset and
           window size, no latency, intermediate timeout, and Instant one event
           after first transmission. Require acknowledgement. The Step 3 packet
           must start 0.5 us after the beginning of the widened receive window.
        2. Continue empty DATA and observe responses with unchanged old timing
           until the event counter reaches the indicated Instant.
        3. At the Instant, maintain the connection with the new parameters;
           warn if the IUT does not respond to the first updated event.
        4. Interleaved with Step 3, require Connection Update Complete with the
           correct handle, successful status and all new connection parameters.
        5. Send an update selecting maximum interval, maximum window offset and
           size, no latency, maximum timeout, and Instant 100 events ahead.
           Require the IUT to acknowledge the update.
        6. Repeat Steps 2-4 for the maximum-interval transition.
        7. Send an update selecting minimum interval, no latency, intermediate
           timeout, and Instant six events after first transmission; require ACK.
        8. Repeat Steps 2-4 for the minimum-interval transition.
        9. Send an update selecting 100-ms interval, maximum window offset/size,
           no latency, maximum timeout, and Instant six events ahead; require ACK.
        10. Repeat Steps 2-4, transmitting the Step 3 packet 0.5 us before the
            ending edge of the widened receive window.
        11. Send an update selecting 110-ms interval, maximum window offset,
            no latency, maximum timeout, and Instant six events ahead; require ACK.
        12. Repeat Steps 2-4, transmitting the Step 3 packet 0.5 us after the
            beginning edge of the widened receive window.

    Expected Outcome:
        Pass verdict:
            The IUT acknowledges updates, changes parameters at each Instant,
            responds in at least 65 of the 100 events in the short-to-long
            transition, and reports all new parameters through HCI.

    Source Correction:
        Step 12 describes the beginning edge but parenthetically says widening
        after the window end. Use widening before the window beginning, matching
        Step 1 and the receive-window definition. Step 11 leaves window size
        unspecified; select the maximum size as in the preceding edge trial.

    Review Limitation:
        The radio API represents integer microseconds. The executable trial uses
        a one-microsecond inside-edge guard, not the required 0.5-us stimulus in
        Steps 1, 10 and 12. All seven transitions and HCI/ACK/Instant assertions
        execute, but this is partial coverage, not a complete source verdict.

    Coverage Note:
        Inject no RF errors; the source threshold assumes BER <= 0.1 percent.
        Verify post-update ACL delivery after each transition and disconnect
        through HCI after the final update.

    Timing Approximation:
        Execute at 1-us timestamp resolution, retaining the limited precision
        label. Fractional guards use the documented integer-us approximation.
        Complete all executable checks without skipping for precision alone;
        only an independent source_conflict label permits a protocol-path skip.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-10-C procedure in
        Section 4.3.5, Figures 4.3-51/52; PDF 600-603, printed 4.3-127 through
        4.3-130. Common initial conditions: Section 4.1.6.
    """
    link = await establish(tester, peer_address=LOWER_CENTRAL_PUBLIC_ADDRESS)
    common = link.parameters
    rounds = (
        (80, 2, 300, 0, 1, 100, None),
        (common.interval, common.latency, common.timeout, 0, 1, 10, None),
        (LL_CONN_INTERVAL_MIN, 0, 300, 0, LL_CONN_WINDOW_SIZE_MIN, 1, 'early'),
        (
            LL_CONN_INTERVAL_MAX,
            0,
            LL_SUPERVISION_TIMEOUT_MAX,
            LL_CONN_INTERVAL_MAX,
            LL_CONN_WINDOW_SIZE_MAX,
            100,
            None,
        ),
        (LL_CONN_INTERVAL_MIN, 0, 300, 0, LL_CONN_WINDOW_SIZE_MIN, 6, None),
        (80, 0, LL_SUPERVISION_TIMEOUT_MAX, 80, LL_CONN_WINDOW_SIZE_MAX, 6, 'late'),
        (88, 0, LL_SUPERVISION_TIMEOUT_MAX, 88, LL_CONN_WINDOW_SIZE_MAX, 6, 'early'),
    )
    for interval, latency, timeout, offset, size, distance, edge in rounds:
        instant = link.counter + distance & LL_EVENT_COUNTER_MASK
        update = bytes([Opcode.LL_CONNECTION_UPDATE_IND]) + struct.pack(
            '<BHHHHH', size, offset, interval, latency, timeout, instant
        )
        await link.exchange(update, llid=LlId.CONTROL, check_ack=True)
        responses = 1
        while link.counter != instant:
            assert not any(
                isinstance(event, hci.HCI_LE_Connection_Update_Complete_Event)
                for event in tester.events
            ), 'update completed before Instant'
            response = await link.exchange(required=False)
            responses += response is not None
        if distance == 100:
            assert responses >= 65
        link.anchor += offset * LL_CONN_INTERVAL_UNIT_US
        elapsed = link.anchor - (link.last_sent_end_us - 1)
        accuracy_ppm = LL_SCA_PPM[LL_SCA_500_PPM] + LL_SCA_PPM[LL_SCA_50_PPM]
        widening = (
            LL_CLOCK_JITTER_US
            + (elapsed * accuracy_ppm + PHY_TIME_UNITS_PER_SECOND - 1)
            // PHY_TIME_UNITS_PER_SECOND
        )
        if edge == 'early':
            link.anchor -= widening - 1
        elif edge == 'late':
            link.anchor += size * LL_CONN_INTERVAL_UNIT_US + widening - 1
        link.parameters = replace(
            link.parameters,
            interval=interval,
            latency=latency,
            timeout=timeout,
            window_size=size,
            window_offset=offset,
        )
        if await link.exchange(required=False) is None:
            tester.output.write('WARNING: no response at connection-update Instant\n')
        event = await tester.event(hci.HCI_LE_Connection_Update_Complete_Event)
        assert (
            event.connection_handle == link.handle and event.status == ErrorCode.SUCCESS
        )
        assert (
            event.connection_interval,
            event.peripheral_latency,
            event.supervision_timeout,
        ) == (interval, latency, timeout)
        await peer_send(tester, link, b'updated')
    await link.disconnect(local=True)


# Verify Peripheral acknowledgement of connection parameter updates, retention of old
# parameters before the Instant, and adoption of the new parameters at the requested
# event, including receive-window edges.
PROCEDURES |= procedure_cases(
    accepting_parameter_update,
    {
        'LL/CON/PER/BV-10-C': pytest.param(
            'Accepting Parameter Update',
            {},
            marks=[
                pytest.mark.limited,
            ],
        ),
    },
)


async def initiating_connection_parameter_request_accept(tester):
    """Initiating Connection Parameter Request – Accept.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        selected timeout and SCA. Select public addresses and common channel
        parameters. The Lower Tester maintains the Central role and ACL handle.

    Test Steps:
        Case 1:
            1. Send HCI LE Connection Update selecting minimum interval, zero
               latency and intermediate supervision timeout (3 s); require status.
            2. Receive LL_CONNECTION_PARAM_REQ and validate its actual parameters.
               Accept directly with LL_CONNECTION_UPDATE_IND selecting a valid
               interval within that request; require the IUT to acknowledge it.
            3. Continue empty DATA and ACKs on old timing until the Instant.
            4. At the Instant, maintain DATA using the accepted new parameters.
            5. Require successful Connection Update Complete with exact handle,
               interval, latency and timeout from the accepted update.
        Case 2:
            1. Send HCI LE Connection Update selecting maximum interval, zero
               latency and maximum timeout; require successful Command Status.
            2. Receive and validate LL_CONNECTION_PARAM_REQ, accept directly with
               LL_CONNECTION_UPDATE_IND, and require acknowledgement.
            3. Continue empty DATA and ACKs on old timing until the Instant.
            4. At the Instant, maintain DATA using the accepted new parameters.
            5. Require successful Connection Update Complete with all accepted
               parameters and the same handle.
        Case 3:
            1. Send HCI LE Connection Update selecting minimum interval, zero
               latency and intermediate timeout (3 s); require Command Status.
            2. Receive and validate LL_CONNECTION_PARAM_REQ, accept directly with
               LL_CONNECTION_UPDATE_IND, and require acknowledgement.
            3. Continue empty DATA and ACKs on old timing until the Instant.
            4. At the Instant, maintain DATA using the accepted new parameters.
            5. Require successful Connection Update Complete with all accepted
               parameters and the same handle.

    Expected Outcome:
        Pass verdict:
            All three cases emit a parameter request, acknowledge the update,
            adopt new parameters at the assigned event, and report those
            parameters with Connection Update Complete.

    Coverage Note:
        Choose the minimum valid interval from each actual LL request; do not
        require it to equal the Host command. No extra PARAM_RSP is inserted
        before the Central's direct UPDATE_IND. Reject early successful HCI
        completion and check subsequent DATA timing, then disconnect through HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-24-C procedure in
        Section 4.3.5, Figure 4.3-66; PDF 620-623, printed 4.3-147 through 4.3-150.
    """
    await _local_parameters(tester, central=False, reject=False)


# Verify Peripheral-initiated parameter requests accepted by the Central, including
# minimum/maximum/minimum interval transitions. The actual LL request may select
# parameters different from the Host command.
PROCEDURES |= procedure_cases(
    initiating_connection_parameter_request_accept,
    {
        'LL/CON/PER/BV-24-C': (
            'Initiating Connection Parameter Request – Accept',
            {},
        ),
    },
)


async def initiating_connection_parameter_request_reject(tester):
    """Initiating Connection Parameter Request – Reject.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        selected timeout and SCA. Select public addresses and common channel
        parameters; the Lower Tester maintains the Central role and ACL handle.

    Test Steps:
        1. Send HCI LE Connection Update selecting minimum interval, zero
           latency and intermediate timeout (3 s); require Command Status.
        2. Receive LL_CONNECTION_PARAM_REQ and validate its actual parameters,
           which may differ from the Host command. Send LL_REJECT_EXT_IND naming
           the rejected opcode and selected error; require IUT acknowledgement.
        3. Require Connection Update Complete with the established handle and
           exact error code from LL_REJECT_EXT_IND.

    Expected Outcome:
        Pass verdict:
            The IUT emits the parameter request and reports the Lower Tester's
            rejection code through Connection Update Complete.

    Coverage Note:
        Select Command Disallowed (0x0C) as the rejection reason. Verify continued
        DATA on unchanged parameters, no extra update completion, and then HCI
        termination of the original ACL.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-25-C procedure in
        Section 4.3.5, Figure 4.3-67; PDF 623-624, printed 4.3-150 through 4.3-151.
    """
    await _local_parameters(tester, central=False, reject=True)


# Verify that a Peripheral reports rejection of its connection parameter request with
# the same error code supplied by the Central.
PROCEDURES |= procedure_cases(
    initiating_connection_parameter_request_reject,
    {
        'LL/CON/PER/BV-25-C': (
            'Initiating Connection Parameter Request – Reject',
            {},
        ),
    },
)


async def initiating_connection_parameter_request_same_procedure_collision(tester):
    """Initiating Connection Parameter Request – same procedure collision.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        selected timeout and SCA. Select public peers and common channel
        parameters; the Lower Tester maintains the Central role and ACL handle.

    Test Steps:
        1. Send HCI LE Connection Update selecting minimum interval, zero
           latency and intermediate timeout (3 s); require Command Status.
        2. Receive and validate LL_CONNECTION_PARAM_REQ. Send a Central
           LL_CONNECTION_PARAM_REQ, then reject the IUT request with
           LL_REJECT_EXT_IND, rejected opcode PARAM_REQ and error 0x23.
        3. Require the IUT to ask its Host to accept/reject the Central parameters,
           with correct connection handle and all requested fields.
        4. At this point or later, allow an optional Connection Update Complete
           failure only with the same handle and error 0x23.
        5. Accept the Central request through HCI Remote Connection Parameter
           Request Reply; require successful completion and the same handle.
        6. Receive LL_CONNECTION_PARAM_RSP and validate its actual parameters.
        7. Send a valid LL_CONNECTION_UPDATE_IND based on that response and
           require the IUT to acknowledge it.
        8. Continue empty DATA and ACKs under old parameters until the Instant.
        9. Maintain DATA using the new parameters starting at the Instant.
        10. Require successful Connection Update Complete with the same handle
            and exact accepted interval, latency and supervision timeout.

    Expected Outcome:
        Pass verdict:
            The local PARAM_REQ is emitted; any local-failure HCI event has error
            0x23. The Central request survives the collision, receives PARAM_RSP,
            is adopted at its Instant, and completes through HCI with new values.

    Coverage Note:
        Reject an immediate Peripheral LL_REJECT_EXT_IND for the preserved
        Central request. Optional local failure may be interleaved with the
        subsequent completion; it must not replace the successful peer update.
        Check new-interval DATA and disconnect after the normative observations.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-26-C procedure in
        Section 4.3.5, Figure 4.3-68; PDF 624-626, printed 4.3-151 through 4.3-153.
    """
    await _parameter_collision(tester, central=False, channel_map=False)


# Verify that a Peripheral preserves an overlapping Central parameter request, reports
# rejection of its local request when applicable, and completes the Central procedure
# after Host approval.
PROCEDURES |= procedure_cases(
    initiating_connection_parameter_request_same_procedure_collision,
    {
        'LL/CON/PER/BV-26-C': (
            'Initiating Connection Parameter Request – same procedure collision',
            {},
        ),
    },
)


async def initiating_parameter_request_channel_map_collision(
    tester,
):
    """Initiating Connection Parameter Request – different procedure collision – channel
    map update.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        selected timeout and SCA. Select public peers and common channel
        parameters; the Lower Tester maintains the Central role and ACL handle.

    Test Steps:
        1. Send HCI LE Connection Update selecting minimum interval, zero
           latency and intermediate timeout (3 s); require Command Status.
        2. Receive and validate LL_CONNECTION_PARAM_REQ. Send LL_CHANNEL_MAP_IND,
           then LL_REJECT_EXT_IND identifying the local PARAM_REQ with error
           0x2A; require acknowledgements of both Central control packets.
        3. Require Connection Update Complete with the original handle and
           Different Transaction Collision status (0x2A).
        4. Continue empty DATA and ACKs on the original channel map until the
           indicated channel-map Instant; then maintain DATA on the new map.

    Expected Outcome:
        Pass verdict:
            The IUT emits the parameter request, reports collision error 0x2A,
            acknowledges the channel update, adopts it at the assigned event,
            and maintains the original connection on the new channel map.

    Coverage Note:
        Select a valid map containing even-numbered data channels. Independently
        compute old/new hopping, read the resulting map through HCI, verify
        post-update ACL delivery, and prohibit leaked Host parameter requests
        or additional update completions before HCI termination.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-27-C procedure in
        Section 4.3.5, Figure 4.3-69; PDF 626-627, printed 4.3-153 through 4.3-154.
    """
    await _parameter_collision(tester, central=False, channel_map=True)


# Verify that a Peripheral accepts a Central channel-map update during its own
# parameter-request procedure and reports the correct collision error.
PROCEDURES |= procedure_cases(
    initiating_parameter_request_channel_map_collision,
    {
        'LL/CON/PER/BV-27-C': (
            (
                'Initiating Connection Parameter Request – different '
                'procedure collision – channel map update'
            ),
            {},
        ),
    },
)


async def initiating_parameter_request_encryption_collision(
    tester,
):
    """Initiating Connection Parameter Request – different procedure collision –
    encryption.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        selected timeout and SCA. Select public addresses and common channel
        parameters; the Lower Tester maintains the Central role and ACL handle.

    Test Steps:
        1. Send HCI LE Connection Update selecting minimum interval, zero
           latency and intermediate timeout (3 s); require Command Status.
        2. Receive LL_CONNECTION_PARAM_REQ and validate the actual parameters,
           which may differ from Host values. Respond with LL_ENC_REQ.
        3. Receive LL_ENC_RSP with the Peripheral session-key-diversifier and
           initialization-vector components; derive the independent session key.
        4. Require HCI LE Long Term Key Request with the exact connection handle,
           random number and encryption diversifier from LL_ENC_REQ.
        5. Supply the matching LTK through HCI Long Term Key Request Reply and
           require successful Command Complete.
        6. Receive LL_START_ENC_REQ and acknowledge it on the ACL connection.
        7. Send encrypted LL_START_ENC_RSP and receive the matching encrypted
           LL_START_ENC_RSP from the Peripheral, validating its integrity.
        8. Require successful Encryption Change with encryption enabled and the
           same connection handle.
        9. Accept the preserved parameter request with encrypted
           LL_CONNECTION_UPDATE_IND selecting valid actual request parameters;
           require acknowledgement from the IUT.
        10. Continue empty DATA and ACKs using old parameters until the Instant.
        11. At the Instant, maintain the encrypted ACL with the new parameters.
        12. Require successful Connection Update Complete with exact handle,
            interval, latency and supervision timeout from the accepted update.

    Expected Outcome:
        Pass verdict:
            The IUT requests parameters, supplies its encryption components,
            requests the LTK, exchanges and acknowledges start-encryption PDUs,
            reports encryption enabled, and then acknowledges/adopts the parameter
            update at its Instant with correct HCI completion.

    Coverage Note:
        Use public fixed test vectors, independent AES-CCM and packet counters.
        Complete encryption before the pending parameter procedure's response
        timeout; measure that budget in simulation time. Verify encrypted DATA
        after the update and disconnect through HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-28-C procedure in
        Section 4.3.5, Figure 4.3-70; PDF 628-630, printed 4.3-155 through 4.3-157.
    """
    await _parameter_encryption_collision(tester, central=False)


# Verify that a Peripheral completes a Central encryption start procedure during its own
# pending parameter request, then completes that request.
PROCEDURES |= procedure_cases(
    initiating_parameter_request_encryption_collision,
    {
        'LL/CON/PER/BV-28-C': (
            (
                'Initiating Connection Parameter Request – different '
                'procedure collision – encryption'
            ),
            {},
        ),
    },
)


async def accepting_connection_parameter_request_no_preferred_periodicity(tester):
    """Accepting Connection Parameter Request – no Preferred_Periodicity.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        selected timeout and SCA. Select public peers and common channel
        parameters; the Lower Tester maintains the Central role and ACL handle.

    Test Steps:
        Case 1:
            1. Send LL_CONNECTION_PARAM_REQ selecting minimum interval, zero
               latency, intermediate timeout (3 s) and Preferred_Periodicity=0.
            2. Require the IUT Host parameter approval request with exact handle
               and fields; accept it through the HCI positive reply.
            3. Receive LL_CONNECTION_PARAM_RSP, validate its actual parameters,
               and send a valid LL_CONNECTION_UPDATE_IND; require the IUT ACK.
            4. Continue empty DATA and ACKs with old parameters until the Instant.
            5. At the Instant, maintain the accepted new connection parameters.
            6. Require successful Connection Update Complete with exact handle,
               interval, latency and timeout from the Lower Tester's update.
        Case 2:
            1. Send LL_CONNECTION_PARAM_REQ selecting maximum interval, zero
               latency, maximum timeout and Preferred_Periodicity=0.
            2. Require the matching HCI Host approval request and accept it.
            3. Validate LL_CONNECTION_PARAM_RSP, send a valid update selecting
               actual response parameters, and require the IUT ACK.
            4. Continue empty DATA and ACKs with old parameters until the Instant.
            5. Maintain the new connection parameters starting at the Instant.
            6. Require successful HCI completion with the exact accepted values.
        Case 3:
            1. Send LL_CONNECTION_PARAM_REQ selecting minimum interval, zero
               latency, intermediate timeout (3 s) and Preferred_Periodicity=0.
            2. Require the matching HCI Host approval request and accept it.
            3. Validate LL_CONNECTION_PARAM_RSP, send a valid update selecting
               actual response parameters, and require the IUT ACK.
            4. Continue empty DATA and ACKs with old parameters until the Instant.
            5. Maintain the new connection parameters starting at the Instant.
            6. Require successful HCI completion with the exact accepted values.

    Expected Outcome:
        Pass verdict:
            In all three cases, the IUT accepts the request, acknowledges the
            update, adopts it at the assigned event and reports the selected
            parameters through Connection Update Complete.

    Coverage Note:
        The LL response may differ from the Host's values. Choose a valid
        interval from the actual response, reject early successful completion,
        verify subsequent DATA timing and terminate through HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-29-C procedure in
        Section 4.3.5, Figure 4.3-71; PDF 630-633, printed 4.3-157 through 4.3-160.
    """
    await _remote_parameters(
        tester, central=False, reject=False, masked=False, invalid=False
    )


# Verify Peripheral acceptance of Central parameter requests without a preferred
# periodicity, across minimum/maximum/minimum interval changes.
PROCEDURES |= procedure_cases(
    accepting_connection_parameter_request_no_preferred_periodicity,
    {
        'LL/CON/PER/BV-29-C': (
            'Accepting Connection Parameter Request – no Preferred_Periodicity',
            {},
        ),
    },
)


async def accepting_connection_parameter_request_preferred_anchor_points_only(tester):
    """Accepting Connection Parameter Request – preferred anchor points only.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common interval/channel parameters, selected timeout and SCA. Select
        public peers; the Lower Tester maintains the Central role and ACL handle.

    Test Steps:
        Case 1:
            1. Send LL_CONNECTION_PARAM_REQ with Offset0=1.25 ms, Offset1-5
               invalid, and unchanged interval, latency and supervision timeout.
            2. Receive LL_CONNECTION_PARAM_RSP and send LL_CONNECTION_UPDATE_IND
               shifting anchors by 1.25 ms; require acknowledgement.
            3. Continue empty DATA and ACKs on old timing until the Instant.
            4. Maintain the new anchor phase starting at the indicated event.
        Case 2:
            1. Send LL_CONNECTION_PARAM_REQ with Offset0=interval-1.25 ms,
               Offset1-5 invalid, and unchanged interval, latency and timeout.
            2. Receive LL_CONNECTION_PARAM_RSP and send an update shifting anchors
               by interval-1.25 ms; require acknowledgement.
            3. Continue empty DATA and ACKs on old timing until the Instant.
            4. Maintain the new anchor phase starting at the indicated event.
        Case 3:
            1. Send LL_CONNECTION_PARAM_REQ with Offset0=1.25 ms, Offset1=2.5 ms,
               Offset2-5 invalid, and unchanged interval, latency and timeout.
            2. Receive LL_CONNECTION_PARAM_RSP and send an update selecting the
               1.25-ms anchor shift; require acknowledgement.
            3. Continue empty DATA and ACKs on old timing until the Instant.
            4. Maintain the new anchor phase starting at the indicated event.

    Expected Outcome:
        Pass verdict:
            The IUT responds positively, acknowledges updates and adopts the
            requested anchor shifts: 1.25 ms, interval-1.25 ms, then 1.25 ms.

    Coverage Note:
        Compute the new phase relative to ReferenceConnEventCount, including
        counter wraparound. Verify unchanged interval/latency/timeout, absence
        of Host parameter approval or update-complete events for anchor-only
        changes, and continued DATA at the shifted anchors before HCI termination.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-30-C procedure in
        Section 4.3.5, Figure 4.3-72; PDF 633-635, printed 4.3-160 through 4.3-162.
    """
    await _preferred_parameters(tester, central=False, anchors_only=True, anchors=True)


# Verify Peripheral acceptance of anchor-only parameter requests without changing the
# connection interval, latency or supervision timeout.
PROCEDURES |= procedure_cases(
    accepting_connection_parameter_request_preferred_anchor_points_only,
    {
        'LL/CON/PER/BV-30-C': (
            'Accepting Connection Parameter Request – preferred anchor points only',
            {},
        ),
    },
)


async def accepting_connection_parameter_request_preferred_periodicity(
    tester, *, anchors
):
    """Accepting Connection Parameter Request – Preferred_Periodicity.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common interval/channel parameters, selected timeout and SCA. Select
        public peers; the Lower Tester maintains the Central role and ACL handle.

    Test Case Configuration:
        LL/CON/PER/BV-31-C: Preferred_Periodicity without preferred anchor
            points.
        LL/CON/PER/BV-32-C: Preferred_Periodicity with preferred anchor points.

    Test Steps:
        1. Select alternative 1A or 1B according to Table 4.3-40:
            1A.1. For BV-31-C, send LL_CONNECTION_PARAM_REQ with a nonzero interval
                  range, zero latency, intermediate timeout (3 s), and a nonzero
                  periodicity having a multiple within the requested range.
            1B.1. For BV-32-C, send the same request plus ReferenceConnEventCount
                  and valid Offset0 corresponding to a 1.25-ms anchor shift;
                  mark Offset1-5 invalid.
        2. Require the Host parameter approval request with exact handle and
           requested fields, and accept it through the HCI positive reply.
        3. Receive LL_CONNECTION_PARAM_RSP and validate its actual parameters,
           which may differ from the Host values. Send LL_CONNECTION_UPDATE_IND
           choosing a valid interval within that response and require the IUT ACK.
        4. Continue empty DATA and ACKs using old parameters until the Instant.
        5. At the Instant, maintain DATA with the Lower Tester's accepted interval,
           latency, timeout and selected anchor phase.
        6. Require successful Connection Update Complete with exact accepted
           parameters and the original connection handle.

    Expected Outcome:
        Pass verdict:
            The IUT responds positively, acknowledges the update, adopts the
            parameters at the assigned event, and reports those values via HCI.

    Coverage Note:
        Select request interval range 13..19 and periodicity 18 in 1.25-ms units.
        Prefer a periodic multiple in the actual response range if one exists;
        otherwise select a valid response interval. PreferredPeriodicity is a
        preference, not a mandatory multiple constraint (Core 6.3 Vol 6, Part B,
        Section 5.1.7.2; Amber corroborated). Select within the actual response
        range even when it excludes that multiple. Compute Offset0's phase against the
        reference counter, reject early HCI completion, verify subsequent DATA,
        and disconnect after the normative six-step procedure.

    Reference:
        LL.TS.p28, Section 4.3.5.2, Table 4.3-40, Figure 4.3-73;
        LL/CON/PER/BV-31-C and LL/CON/PER/BV-32-C; PDF 635-637,
        printed 4.3-162 through 4.3-164.
    """
    await _preferred_parameters(
        tester, central=False, anchors_only=False, anchors=anchors
    )


# Verify Peripheral acceptance of Central parameter requests carrying a preferred
# interval periodicity, optionally with preferred anchor points.
PROCEDURES |= procedure_cases(
    accepting_connection_parameter_request_preferred_periodicity,
    {
        'LL/CON/PER/BV-31-C': (
            'Accepting Connection Parameter Request – Preferred_Periodicity',
            {'anchors': False},
        ),
        'LL/CON/PER/BV-32-C': (
            (
                'Accepting Connection Parameter Request – '
                'Preferred_Periodicity and preferred anchor points'
            ),
            {'anchors': True},
        ),
    },
)


async def accepting_connection_parameter_request_event_masked(tester):
    """Accepting Connection Parameter Request – event masked.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common interval/channel parameters, selected timeout and SCA. Select
        public peers; the Lower Tester maintains the Central role and ACL handle.

    Test Steps:
        1. Mask LE Remote Connection Parameter Request through HCI LE Set Event
           Mask and require successful Command Complete.
        2. Send LL_CONNECTION_PARAM_REQ selecting minimum interval, zero
           latency and intermediate supervision timeout (3 s).
        3. Require LL_REJECT_EXT_IND identifying LL_CONNECTION_PARAM_REQ and
           Unsupported Remote Feature error (0x1A).

    Expected Outcome:
        Pass verdict:
            The IUT rejects the parameter update with LL_REJECT_EXT_IND and
            the required error code 0x1A.

    Coverage Note:
        Continue three DATA exchanges on the original parameters, requiring no
        Host parameter request or update-complete event, then terminate via HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-33-C procedure in
        Section 4.3.5, Figure 4.3-74; PDF 637-638, printed 4.3-164 through 4.3-165.
    """
    await _remote_parameters(
        tester, central=False, reject=False, masked=True, invalid=False
    )


# Verify that a Peripheral rejects a Central parameter request requiring Host approval
# when the Host has masked the approval-request event.
PROCEDURES |= procedure_cases(
    accepting_connection_parameter_request_event_masked,
    {
        'LL/CON/PER/BV-33-C': (
            'Accepting Connection Parameter Request – event masked',
            {},
        ),
    },
)


async def accepting_connection_parameter_request_host_rejects(tester):
    """Accepting Connection Parameter Request – Host rejects.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        selected timeout and SCA. Select public peers and common channel
        parameters; the Lower Tester maintains the Central role and ACL handle.

    Test Steps:
        1. Send LL_CONNECTION_PARAM_REQ selecting minimum interval, zero
           latency and intermediate supervision timeout (3 s).
        2. Require the Host parameter approval request with exact handle and
           requested fields. Reject it through HCI Remote Connection Parameter
           Request Negative Reply using Unacceptable Connection Parameters
           (0x3B); require successful Command Complete and the same handle.
        3. Require LL_REJECT_EXT_IND naming LL_CONNECTION_PARAM_REQ and carrying
           the exact error code 0x3B provided by the Host.

    Expected Outcome:
        Pass verdict:
            The IUT responds with LL_REJECT_EXT_IND and the exact Host-selected
            rejection error code.

    Coverage Note:
        Acknowledge the LL rejection, continue three DATA exchanges on unchanged
        parameters, reject a successful/duplicate update completion, and then
        terminate the original ACL through HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-34-C procedure in
        Section 4.3.5, Figure 4.3-75; PDF 638-639, printed 4.3-165 through 4.3-166.
    """
    await _remote_parameters(
        tester, central=False, reject=True, masked=False, invalid=False
    )


# Verify Peripheral rejection of a Central parameter request when the Peripheral Host
# supplies a negative approval reply.
PROCEDURES |= procedure_cases(
    accepting_connection_parameter_request_host_rejects,
    {
        'LL/CON/PER/BV-34-C': (
            'Accepting Connection Parameter Request – Host rejects',
            {},
        ),
    },
)


async def initiating_connection_parameter_request_unsupported_without_feature_exchange(
    tester,
):
    """Initiating Connection Parameter Request – Unsupported Without Feature Exchange.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common advertising/channel parameters, selected timeout and SCA.
        Select public peers and initial LE 1M; the Lower Tester acts as Central.
        Do not establish peer parameter-request support before Step 3.

    Test Steps:
        1. If the IUT autonomously sends LL_PERIPHERAL_FEATURE_REQ before
           Step 3, answer with LL_UNKNOWN_RSP identifying that opcode.
        2. Send HCI LE Connection Update with the minimum interval (7.5 ms),
           zero latency and intermediate supervision timeout (3 s); require
           successful Command Status.
        3. Receive LL_CONNECTION_PARAM_REQ and respond with LL_UNKNOWN_RSP
           identifying opcode 0x0F. The requested parameters may differ from
           the Host command; validate them as legal connection parameters.
        4. Require Connection Update Complete with the original handle and
           Unsupported Remote Feature (0x1A).
        5. Resend the identical HCI LE Connection Update and accept either:
           a. Command Status with Unsupported Remote Feature (0x1A).
           b. Successful Command Status followed by Connection Update Complete
              with the original handle and Unsupported Remote Feature (0x1A).
           Any new LL_CONNECTION_PARAM_REQ in this step fails the test.

    Expected Outcome:
        Pass verdict:
            The first Host request produces PARAM_REQ and its UNKNOWN_RSP
            produces the specified failure completion. The repeated Host
            request is rejected with 0x1A by one of Step 5's two HCI alternatives.
        Fail verdict:
            A repeated on-air PARAM_REQ, successful parameter update, incorrect
            handle/status, or duplicate completion.

    Coverage Note:
        Handle autonomous Feature Requests both during connection setup and
        while waiting for PARAM_REQ. Observe sixteen further old-parameter
        events after the second rejection to detect forbidden on-air retries;
        this is an observation bound, not an additional normative step.
        Disconnect through HCI after the observation.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-85-C procedure in
        Section 4.3.5, Figure 4.3-103; PDF 696-698,
        printed 4.3-223 through 4.3-225. Steps 1-5 including 5a/5b and outcome;
        referenced Core Vol 6, Part B, Section 5.1.7.
    """
    await _unsupported_parameters(tester, central=False, features=False)


# Verify that a Peripheral learns that its Central does not support Connection Parameter
# Request without a completed Feature Exchange, reports the first failed request and
# rejects a repeated Host request without retransmitting the unsupported procedure.
PROCEDURES |= procedure_cases(
    initiating_connection_parameter_request_unsupported_without_feature_exchange,
    {
        'LL/CON/PER/BV-85-C': (
            (
                'Initiating Connection Parameter Request – Unsupported '
                'Without Feature Exchange'
            ),
            {},
        ),
    },
)


async def initiating_connection_parameter_request_unsupported_with_feature_exchange(
    tester,
):
    """Initiating Connection Parameter Request – Unsupported With Feature Exchange.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common advertising/channel parameters, selected timeout and SCA.
        Select public peers and initial LE 1M; the Lower Tester acts as Central.

    Test Steps:
        1. Initiate Feature Exchange unless the IUT has already done so.
           Advertise no Connection Parameter Request support in the Lower
           Tester feature set. Answer an autonomous Peripheral Feature Request
           directly; otherwise send FEATURE_REQ and require FEATURE_RSP.
        2. Send HCI LE Connection Update with the minimum interval (7.5 ms),
           zero latency and intermediate supervision timeout (3 s). Accept:
           a. Command Status with Unsupported Remote Feature (0x1A).
           b. Successful Command Status followed by Connection Update Complete
              with the original handle and Unsupported Remote Feature (0x1A).
           Any LL_CONNECTION_PARAM_REQ in this step fails the test.

    Expected Outcome:
        Pass verdict:
            The IUT rejects the Host request with Unsupported Remote Feature
            through either specified HCI alternative, without an on-air PARAM_REQ.
        Fail verdict:
            Any on-air PARAM_REQ, successful parameter update, incorrect
            handle/status, or duplicate completion.

    Coverage Note:
        Use a zero Lower Tester feature set to clear the relevant support bit.
        Observe sixteen old-parameter connection events after rejection;
        disconnect through HCI after checking that no extra completion appears.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-86-C procedure in
        Section 4.3.5, Figure 4.3-104; PDF 698-699,
        printed 4.3-225 through 4.3-226. Steps 1-2 including 2a/2b and outcome;
        referenced Core Vol 6, Part B, Section 5.1.7.
    """
    await _unsupported_parameters(tester, central=False, features=True)


# Verify that a Peripheral rejects a Host Connection Parameter Request after Feature
# Exchange has disclosed that its Central does not support the procedure, without
# sending an on-air parameter request.
PROCEDURES |= procedure_cases(
    initiating_connection_parameter_request_unsupported_with_feature_exchange,
    {
        'LL/CON/PER/BV-86-C': (
            (
                'Initiating Connection Parameter Request – Unsupported With'
                ' Feature Exchange'
            ),
            {},
        ),
    },
)


async def rejecting_connection_change(tester):
    """Rejecting Connection Change.

    Initial Condition:
        An unencrypted connected Peripheral with public addresses, common
        channel selection, interval and latency, selected timeout and SCA.
        The Lower Tester acts as Central. Use zero latency; retain the handle,
        event counter and common connection parameters for each fresh ACL.

    Test Steps:
        1. Send LL_CONNECTION_UPDATE_IND with intermediate legal connection
           parameters and an Instant at or before the current event. An
           acknowledgement from the IUT is optional.
        2. Require Disconnection Complete with the preamble handle and
           Instant Passed (0x28).
        3. Establish a fresh Peripheral connection.
        4. Send LL_CHANNEL_MAP_IND with the first selected channel map and
           an Instant at or before the current event. Accept either:
           Alternative 1: the IUT may acknowledge, then reports Disconnection
           Complete with the preamble handle and Instant Passed (0x28).
           Alternative 2: the IUT acknowledges, reports no disconnection and
           continues to maintain the connection with the Lower Tester.

    Expected Outcome:
        Pass verdict:
            The past connection update produces the specified HCI failure.
            For Step 4 Alternative 1, the IUT reports failure and stops the
            connection before one supervision timeout elapses from the invalid
            request. For Alternative 2, it maintains the acknowledged connection
            and does not report failure.

    Coverage Note:
        Use the preceding event as Instant, midpoint interval and timeout with
        zero latency for the connection request, and the selected even-channel
        map for the second trial. Anchor the observation deadline to the actual
        request's RF end, not the earlier test-command time. Keep valid Central
        traffic through one supervision period to distinguish maintenance from
        delayed failure; do not refresh the original invalid request's deadline.
        Both alternatives have verdict-boundary unit coverage. The current IUT
        takes Alternative 1 in the native PHY trial. Probe the next receive
        window after disconnection; terminate any maintained ACL through HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BI-04-C procedure in
        Section 4.3.5, Figure 4.3-107; PDF 702-704,
        printed 4.3-229 through 4.3-231. Steps 1-4, both alternatives and
        Expected Outcome; referenced Core Vol 6, Part B, Sections 5.1.1/5.1.2.
    """
    for channel_map in (False, True):
        link = await establish(tester, peer_address=LOWER_CENTRAL_PUBLIC_ADDRESS)
        instant = link.counter - 1 & LL_EVENT_COUNTER_MASK
        payload = (
            bytes([Opcode.LL_CHANNEL_MAP_IND])
            + sum(
                1 << channel for channel in range(0, LL_DATA_CHANNEL_COUNT, 2)
            ).to_bytes(LL_CHANNEL_MAP_SIZE, 'little')
            + instant.to_bytes(PDU_CONTROL_INSTANT_SIZE, 'little')
            if channel_map
            else bytes([Opcode.LL_CONNECTION_UPDATE_IND])
            + struct.pack(
                '<BHHHHH',
                LL_CONN_WINDOW_SIZE_MIN,
                0,
                (LL_CONN_INTERVAL_MIN + LL_CONN_INTERVAL_MAX) // 2,
                0,
                (LL_SUPERVISION_TIMEOUT_MIN + LL_SUPERVISION_TIMEOUT_MAX) // 2,
                instant,
            )
        )
        await _past_update_outcome(link, payload, allow_maintenance=channel_map)


# Verify that a Peripheral handles past Instants in connection and channel-map updates:
# report the required connection-update failure, then either disconnect or acknowledge
# and maintain the connection for the channel-map case, as permitted by the source
# alternatives.
PROCEDURES |= procedure_cases(
    rejecting_connection_change,
    {
        'LL/CON/PER/BI-04-C': (
            'Rejecting Connection Change',
            {},
        ),
    },
)


async def initiating_connection_parameter_request_timeout(tester):
    """Initiating Connection Parameter Request – Timeout.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common channel parameters and interval, selected timeout and SCA.
        Select public peers, zero latency and LE 1M. The Lower Tester acts as
        Central and advertises Connection Parameter Request support.

    Test Steps:
        1. Send HCI LE Connection Update with the minimum connection interval
           (7.5 ms), zero latency and intermediate supervision timeout (3 s);
           require successful Command Status.
        2. Receive and acknowledge LL_CONNECTION_PARAM_REQ without sending
           LL_CONNECTION_PARAM_RSP or an update. Its legal parameters may
           differ from the Host-requested values.
        3. Require Disconnection Complete with LL Response Timeout (0x22)
           when the connection control transaction timer expires, and require
           the IUT to stop maintaining the connection.

    Expected Outcome:
        Pass verdict:
            The IUT reports LL Response Timeout at transaction expiry and
            ceases connection RF traffic.

    Coverage Note:
        Validate every parameter field and positively acknowledge the request.
        Continue valid empty DATA on the old schedule so ordinary supervision
        cannot expire. Reject early disconnection or successful parameter
        completion. Bound the 40-s response timeout by the command enqueue
        window with 1-ms HCI observation resolution, check the HCI handle/status,
        then probe the next Peripheral receive window for RF silence.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BI-07-C procedure in
        Section 4.3.5, Figure 4.3-109; PDF 706-707,
        printed 4.3-233 through 4.3-234. Steps 1-3 and Expected Outcome;
        referenced Core Vol 6, Part B, Section 5.1.7.
    """
    await _procedure_timeout(tester, central=False, parameters=True)


# Verify that a Peripheral starts Connection Parameter Request following a Host command
# and terminates with LL Response Timeout if the Central acknowledges the request but
# never returns its procedure response.
PROCEDURES |= procedure_cases(
    initiating_connection_parameter_request_timeout,
    {
        'LL/CON/PER/BI-07-C': (
            'Initiating Connection Parameter Request – Timeout',
            {},
        ),
    },
)


async def accepting_connection_parameter_request_illegal_parameters(tester):
    """Accepting Connection Parameter Request – Illegal Parameters.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common channel parameters and interval, selected timeout and SCA.
        Select public peers, zero latency and LE 1M; the Lower Tester is Central.

    Test Steps:
        1. Send LL_CONNECTION_PARAM_REQ with both minimum and maximum interval
           fields set to the invalid encoded value 4.
        2. Receive LL_REJECT_EXT_IND with ErrorCode=Invalid LL Parameters
           (0x1E), identifying LL_CONNECTION_PARAM_REQ as the rejected opcode.

    Expected Outcome:
        Pass verdict:
            The IUT rejects the invalid interval request through the specified
            extended reject opcode and reason.

    Source Correction:
        The source labels interval value 4 as 6 ms. At the specified 1.25-ms
        interval unit it means 5 ms; retain the encoded value 4 as the invalid
        test stimulus. Core Vol 6, Part B, Section 2.4.2.16 and Amber support this
        local editorial correction; no adopted SIG erratum is claimed.

    Coverage Note:
        Keep other fields legal. Acknowledge the reject and maintain three
        further old-parameter connection events. Reject any Host remote-parameter
        request or update completion resulting from the invalid request, then
        disconnect through HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BI-08-C procedure in
        Section 4.3.5, Figure 4.3-110; PDF 707-708,
        printed 4.3-234 through 4.3-235. Steps 1-2 and Expected Outcome;
        referenced Core Vol 6, Part B, Section 5.1.7.
    """
    await _remote_parameters(
        tester, central=False, reject=False, masked=False, invalid=True
    )


# Verify that a Peripheral rejects a Central Connection Parameter Request whose minimum
# and maximum interval fields are outside the valid range.
PROCEDURES |= procedure_cases(
    accepting_connection_parameter_request_illegal_parameters,
    {
        'LL/CON/PER/BI-08-C': (
            'Accepting Connection Parameter Request – illegal parameters',
            {},
        ),
    },
)


async def requesting_parameter_update(tester):
    """Requesting Parameter Update.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and common interval, latency, timeout and
        channel map. The optional Feature Exchange preamble declares no peer
        Connection Parameters Request support, selecting the direct update.

    Test Steps:
        1. Send HCI LE Connection Update for the maximum supported connection
           interval and require successful Command Status.
        2. Receive LL_CONNECTION_UPDATE_IND with the requested parameters.
           Acknowledge on the same channel with empty Peripheral DATA using
           the acknowledgement scheme. Continue until the following IUT
           packet is received, or up to fifteen connection events.
        3. Maintain the connection with empty DATA using the old parameters
           until the event counter reaches the update Instant.
        4. At Instant, apply the new parameters. Require the next two Central
           DATA transmissions to be separated by the new connection interval,
           allowing the applicable clock jitter.
        5. Require successful HCI LE Connection Update Complete after Instant
           with the established handle and the new interval, latency and timeout.

    Expected Outcome:
        Pass verdict:
            The Central transmits the update indication, maintains the
            connection using the new parameters, and reports those parameters
            to the Host. A successful completion before Instant fails.

    Coverage Note:
        The selected simulation uses a zero-drift clock. The independent peer
        follows the old schedule through Instant, then checks consecutive
        new-interval packets before disconnecting through HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-07-C procedure in
        Section 4.3.6, Figure 4.3-138; PDF 757-758, printed 4.3-284 through
        4.3-285. Steps 1-5 and Expected Outcome. Feature Exchange preamble
        in Section 4.1.6.7.3; Core Vol 6, Part B, Section 5.1.1.
    """
    await _local_parameters(tester, central=True, reject=False)


# Verify that a Central initiates a connection parameter update, applies the new timing
# at Instant, and reports the new parameters to its Host.
PROCEDURES |= procedure_cases(
    requesting_parameter_update,
    {
        'LL/CON/CEN/BV-07-C': (
            'Requesting Parameter Update',
            {},
        ),
    },
)


async def initiating_connection_parameter_request_accept_ll_con_cen_bv_24_c(tester):
    """Initiating Connection Parameter Request – Accept.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and common latency, timeout and channel map.
        Use the optional Feature Exchange preamble to declare peer support for
        Connection Parameters Request. Run all four cases on the maintained ACL.

    Test Steps:
        Case 1:
        1. Send HCI LE Connection Update requesting the minimum interval,
           zero latency and 3-s supervision; require successful Command Status.
        2. Receive valid LL_CONNECTION_PARAM_REQ, whose fields may differ from
           the Host request. Accept with LL_CONNECTION_PARAM_RSP and require
           its acknowledgement followed by LL_CONNECTION_UPDATE_IND.
        3. Maintain empty DATA using the old schedule until the update Instant.
        4. At Instant, adopt the IUT-selected parameters and continue DATA.
        5. Require successful HCI LE Connection Update Complete reporting
           the new interval, latency and timeout for the established handle.
        Case 2:
        1. Send HCI LE Connection Update requesting the maximum interval,
           zero latency and maximum supervision; require successful Status.
        2. Receive valid LL_CONNECTION_PARAM_REQ, accept it with PARAM_RSP,
           and require its acknowledgement followed by UPDATE_IND.
        3. Maintain empty DATA using the old schedule until Instant.
        4. At Instant, continue with the IUT-selected new parameters.
        5. Require successful HCI Update Complete with the selected parameters.
        Case 3:
        1. Send HCI LE Connection Update requesting the minimum interval,
           zero latency and 3-s supervision; require successful Status.
        2. Receive valid LL_CONNECTION_PARAM_REQ, accept it with PARAM_RSP,
           and require its acknowledgement followed by UPDATE_IND.
        3. Maintain empty DATA using the old schedule until Instant.
        4. At Instant, continue with the IUT-selected new parameters.
        5. Require successful HCI Update Complete with the selected parameters.
        Case 4:
        1. Request interval min=max=0x0960 (3 s), or 0x0640 (2 s) if already
           using 3 s, maximum supervision and unchanged latency; require
           successful HCI Command Status.
        2. Receive LL_CONNECTION_PARAM_REQ with a valid ReferenceConnEventCount.
           Its timing fields may differ from the Host request.
        3. Send LL_CONNECTION_PARAM_RSP with a different valid reference count.
        4. Receive LL_CONNECTION_UPDATE_IND.
        5. Maintain empty DATA using the old schedule until its Instant.
        6. At Instant, continue with the IUT-selected new parameters.
        7. Require successful HCI Update Complete with the selected parameters.

    Expected Outcome:
        Pass verdict:
            In Cases 1-3 the IUT initiates negotiation, adopts the accepted
            LL timing fields at Instant and reports them through HCI.
            In Case 4 the ACL is maintained through Instant and the new
            parameters are reported despite the changed reference count.

    Coverage Note:
        Check valid request ranges independently of Host arguments, allowing
        PreferredPeriodicity=0 with a range. For identical accepted REQ/RSP
        timing fields, the selected interval stays within the accepted range
        and latency/timeout match those fields. Reject early HCI success.
        Case 4 uses a known recent reference event different from the request;
        mark all response offsets unspecified rather than retaining offsets
        calculated against the old reference. Disconnect after all four cases.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-24-C procedure in
        Section 4.3.6, Figures 4.3-153/154; PDF 775-780, printed 4.3-302
        through 4.3-307. Cases 1-3 Steps 1-5 and Case 4 Steps 1-7;
        Core parameter negotiation procedure 5.1.7.
    """
    await _central_parameters(tester, reject=False, collision=False, legacy=False)


# Verify Central-initiated parameter negotiation when the Peripheral accepts, including
# a changed reference event in the response. The IUT may select different parameters
# from the Host's request.
PROCEDURES |= procedure_cases(
    initiating_connection_parameter_request_accept_ll_con_cen_bv_24_c,
    {
        'LL/CON/CEN/BV-24-C': (
            'Initiating Connection Parameter Request – Accept',
            {},
        ),
    },
)


async def initiating_connection_parameter_request_reject_ll_con_cen_bv_25_c(tester):
    """Initiating Connection Parameter Request – Reject.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and common latency, timeout and channel map.
        Declare peer Connection Parameters Request support in the optional
        Feature Exchange preamble.

    Test Steps:
        1. Send HCI LE Connection Update requesting the minimum interval,
           zero latency and 3-s supervision; require successful Command Status.
        2. Receive valid LL_CONNECTION_PARAM_REQ, whose parameters may differ
           from the Host request. Reject it with LL_REJECT_EXT_IND identifying
           that opcode and error Unacceptable Connection Parameters (0x3B).
           Require the Central to acknowledge the rejection.
        Alternative 1:
        - Require HCI LE Connection Update Complete for the established handle
          with the rejection's error code 0x3B.
        Alternative 2:
        - Receive LL_CONNECTION_UPDATE_IND; its parameters may differ from
          the Host request.
        - Maintain empty DATA using the old schedule until the update Instant.
        - At Instant, adopt the IUT-selected parameters and continue DATA.
        - Require successful HCI LE Connection Update Complete reporting
          those new parameters.

    Expected Outcome:
        Pass verdict:
            The IUT sends its parameter request and either reports error
            0x3B or completes a valid connection update with HCI notification.

    Coverage Note:
        Accept either source alternative, not only the error branch.
        Failed HCI completion fields other than status/handle are not used
        as negotiated parameters. In that branch, verify continued old RF
        timing and reject a second completion or contradictory update.
        In the update branch, validate the selected timing fields and Instant
        rather than forcing the rejected Host values. Disconnect afterwards.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-25-C procedure in
        Section 4.3.6, Figure 4.3-155; PDF 780-782, printed 4.3-307 through
        4.3-309. Steps 1-2, both alternatives and Expected Outcome;
        Core parameter negotiation procedure 5.1.7.
    """
    await _central_parameters(tester, reject=True, collision=False, legacy=False)


# Verify Central behavior when the Peripheral rejects its connection parameter request,
# accepting either permitted outcome of the procedure.
PROCEDURES |= procedure_cases(
    initiating_connection_parameter_request_reject_ll_con_cen_bv_25_c,
    {
        'LL/CON/CEN/BV-25-C': (
            'Initiating Connection Parameter Request – Reject',
            {},
        ),
    },
)


async def initiating_parameter_request_same_procedure_collision_cen_bv_26(
    tester,
):
    """Initiating Connection Parameter Request – same procedure collision.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and common latency, timeout and channel map.
        Declare peer Connection Parameters Request support before the procedure.

    Test Steps:
        1. Send HCI LE Connection Update requesting the minimum interval,
           zero latency and 3-s supervision; require successful Command Status.
        2. Receive LL_CONNECTION_PARAM_REQ, whose fields may differ from the
           Host request, and respond with a colliding LL_CONNECTION_PARAM_REQ.
        3. Require LL_REJECT_EXT_IND rejecting the Lower Tester's request
           with reason LL Procedure Collision (0x23).
        4. Accept the original IUT request with LL_CONNECTION_PARAM_RSP and
           require LL_CONNECTION_UPDATE_IND.
        5. Maintain empty DATA using the old schedule until the update Instant.
        6. Require the IUT-selected new parameters to take effect at Instant.
        7. From the update point, maintain the connection using that new timing.
        8. Require successful HCI LE Connection Update Complete reporting
           the new parameters for the established handle.

    Expected Outcome:
        Pass verdict:
            The Central issues its own request, rejects the colliding request,
            accepts the Lower Tester's response to the original request,
            maintains the updated ACL and reports the selected parameters.

    Coverage Note:
        Validate the rejection's opcode as well as its reason. Check final
        timing against the accepted LL fields, not the raw Host request.
        Require acknowledgement of the response, no HCI success before
        Instant, and consecutive new-interval DATA before disconnecting.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-26-C procedure in
        Section 4.3.6, Figure 4.3-156; PDF 782-784, printed 4.3-309 through
        4.3-311. Steps 1-8 and Expected Outcome; Core procedure 5.1.7.
    """
    await _central_parameters(tester, reject=False, collision=True, legacy=False)


# Verify that a Central rejects a colliding Peripheral parameter request while
# preserving and completing its own parameter negotiation.
PROCEDURES |= procedure_cases(
    initiating_parameter_request_same_procedure_collision_cen_bv_26,
    {
        'LL/CON/CEN/BV-26-C': (
            'Initiating Connection Parameter Request – same procedure collision',
            {},
        ),
    },
)


async def initiating_parameter_request_channel_map_collision_cen_bv_27(
    tester,
):
    """Initiating Connection Parameter Request – different procedure collision – channel
    map update.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and common connection/channel parameters.

    Test Steps:
        1. Send HCI LE Set Host Channel Classification requesting even
           data channels only.
        2. Require Command Complete with success. If the command fails,
           retry with a different channel map, at least five seconds apart,
           up to twenty attempts or until successful.
        3. If all twenty attempts fail, return an Inconclusive verdict.
        4. Receive LL_CHANNEL_MAP_IND; its new map need not equal the map
           submitted by the Host.
        5. Send LL_CONNECTION_PARAM_REQ as the same-event acknowledgement
           of that indication, requesting the minimum interval, zero latency
           and 3-s supervision timeout.
        6. Require LL_REJECT_EXT_IND identifying that request and error
           Different Transaction Collision (0x2A).
        7. Maintain empty DATA on the old channel map until the indicated
           Instant, then maintain the connection using the new map.

    Expected Outcome:
        Pass verdict:
            The Central sends Channel Map Update, rejects the colliding
            parameter request, and maintains DATA using the updated selection
            parameters after Instant.
        Inconclusive verdict:
            All twenty Host commands fail, or the Controller chooses not to
            provide a Channel Map Update indication.

    Coverage Note:
        Send the colliding request in the packet that acknowledges the map
        indication, not in a later event. Validate at least two enabled data
        channels and no reserved map bits. Check the updated map through
        HCI Read Channel Map and transfer ACL DATA before disconnecting.
        No Host parameter-request or update-completion event is expected.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-27-C procedure in
        Section 4.3.6, Figure 4.3-157; PDF 784-785, printed 4.3-311 through
        4.3-312. Steps 1-7, Pass and Inconclusive verdicts; Core procedure 5.1.7.
    """
    await _parameter_collision(tester, central=True, channel_map=False)


# Verify that a Central completes Channel Map Update while rejecting a colliding
# Peripheral Connection Parameters Request.
PROCEDURES |= procedure_cases(
    initiating_parameter_request_channel_map_collision_cen_bv_27,
    {
        'LL/CON/CEN/BV-27-C': (
            (
                'Initiating Connection Parameter Request – different '
                'procedure collision – channel map update'
            ),
            {},
        ),
    },
)


async def initiating_parameter_request_encryption_collision_cen_bv_28(
    tester,
):
    """Initiating Connection Parameter Request – different procedure collision –
    encryption.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and common latency, timeout and channel map.
        The Lower Tester maintains independent AES-CCM state and test key material.

    Test Steps:
        1. Send HCI LE Enable Encryption and require successful Command Status.
        2. Receive LL_ENC_REQ.
        3. As its same-event acknowledgement, send LL_CONNECTION_PARAM_REQ
           requesting the minimum interval, zero latency and 3-s supervision.
        4. Send LL_ENC_RSP with the Lower Tester's session-key and IV components.
        5. Send LL_START_ENC_REQ and require the IUT to acknowledge it.
        6. Receive LL_START_ENC_RSP and respond with LL_START_ENC_RSP.
        7. Require successful HCI Encryption Change for the established handle.
        8. At any time after Step 3, possibly interleaved with Steps 4-7,
           receive HCI LE Remote Connection Parameter Request and accept it.
        9. Require LL_CONNECTION_UPDATE_IND with the selected parameters;
           they may differ from the Host's provided values.
        10. Maintain empty DATA using the old schedule until its Instant.
        11. At Instant, maintain the connection using the selected new timing.
        12. Require successful HCI LE Connection Update Complete reporting
            those parameters for the established handle.

    Expected Outcome:
        Pass verdict:
            The IUT sends LL_ENC_REQ and LL_START_ENC_RSP, completes encryption
            with HCI notification, accepts the pending parameter negotiation,
            maintains the updated ACL and reports the selected timing through HCI.

    Coverage Note:
        Inject the parameter request in the ACK of LL_ENC_REQ, not after an
        intervening empty ACK. Derive the session key and encrypted packet
        counters independently; validate the decrypted START_ENC exchange.
        Complete encryption before the parameter procedure response timeout.
        Accept the Host parameter request after Step 7, one of the permitted
        Step 8 interleavings. Check original timing until Instant, updated DATA,
        full HCI parameters, and an encrypted exchange before disconnection.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-28-C procedure in
        Section 4.3.6, Figure 4.3-158; PDF 786-788, printed 4.3-313 through
        4.3-315. Steps 1-12, Expected Outcome and Notes; Core 5.1.7/5.1.3.1.
    """
    await _parameter_encryption_collision(tester, central=True)


# Verify that a Central completes Encryption Start despite a colliding Peripheral
# parameter request, then completes that deferred negotiation.
PROCEDURES |= procedure_cases(
    initiating_parameter_request_encryption_collision_cen_bv_28,
    {
        'LL/CON/CEN/BV-28-C': (
            (
                'Initiating Connection Parameter Request – different '
                'procedure collision – encryption'
            ),
            {},
        ),
    },
)


async def initiating_connection_parameter_request_remote_legacy_host(tester):
    """Initiating Connection Parameter Request – remote legacy Host.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and common latency, timeout and channel map.
        The peer Link Layer supports Connection Parameters Request; the Lower
        Tester models its legacy or event-masked Host by rejecting the request.

    Test Steps:
        1. Send HCI LE Connection Update requesting the minimum interval,
           zero latency and 3-s supervision; require successful Command Status.
        2. Receive valid LL_CONNECTION_PARAM_REQ, whose parameters may differ
           from the Host request. Respond with LL_REJECT_EXT_IND identifying
           that request and error Unsupported Remote Feature (0x1A).
           Require acknowledgement of the rejection.
        3. Receive LL_CONNECTION_UPDATE_IND.
        4. Maintain empty DATA using the old schedule until its Instant.
        5. At Instant, maintain the connection with the IUT-selected new timing.
        6. Require successful HCI LE Connection Update Complete reporting
           the selected parameters for the established handle.

    Expected Outcome:
        Pass verdict:
            The IUT initiates its parameter request, sends Connection Update
            after the 0x1A rejection, maintains the updated ACL and reports
            its new parameters through HCI.

    Coverage Note:
        Do not send PARAM_RSP or terminate on the rejection. Validate the
        direct update's legal timing independently of the original Host
        request, require no completion before Instant, and check consecutive
        new-interval DATA and the HCI fields before disconnecting.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-29-C procedure in
        Section 4.3.6, Figure 4.3-159; PDF 788-790, printed 4.3-315 through
        4.3-317. Steps 1-6 and Expected Outcome; Core procedure 5.1.7.
    """
    await _central_parameters(tester, reject=False, collision=False, legacy=True)


# Verify that a Central falls back to Connection Update when a remote Host does not
# support parameter requests or masks the request event.
PROCEDURES |= procedure_cases(
    initiating_connection_parameter_request_remote_legacy_host,
    {
        'LL/CON/CEN/BV-29-C': (
            'Initiating Connection Parameter Request – remote legacy host',
            {},
        ),
    },
)


async def accepting_parameter_request_no_preferred_periodicity_cen_bv_30(
    tester,
):
    """Accepting Connection Parameter Request – no Preferred_Periodicity.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and common latency, timeout and channel map.
        Maintain the same ACL through all three cases.

    Test Steps:
        Case 1:
        1. Send LL_CONNECTION_PARAM_REQ requesting the minimum interval,
           zero latency, 3-s supervision and PreferredPeriodicity=0.
        2. Require the Host parameter approval request and accept it.
        3. Receive LL_CONNECTION_UPDATE_IND and acknowledge it. The selected
           parameters may differ from those provided by the Host.
        4. Maintain empty DATA using old timing until the indicated Instant.
        5. Require the selected new parameters to take effect at Instant.
        6. From that point, maintain the connection using the new timing.
        7. Require successful HCI LE Connection Update Complete reporting
           those parameters for the established handle.
        Case 2:
        1. Send LL_CONNECTION_PARAM_REQ requesting the maximum interval,
           zero latency, maximum supervision and PreferredPeriodicity=0.
        2. Require the Host approval request and accept it.
        3. Receive and acknowledge LL_CONNECTION_UPDATE_IND carrying the
           IUT-selected parameters.
        4. Maintain empty DATA using old timing until Instant.
        5. Require the new parameters to take effect at Instant.
        6. Maintain the connection using the new timing.
        7. Require successful HCI Update Complete with the selected parameters.
        Case 3:
        1. Send LL_CONNECTION_PARAM_REQ requesting the minimum interval,
           zero latency, 3-s supervision and PreferredPeriodicity=0.
        2. Require the Host approval request and accept it.
        3. Receive and acknowledge LL_CONNECTION_UPDATE_IND carrying the
           IUT-selected parameters.
        4. Maintain empty DATA using old timing until Instant.
        5. Require the new parameters to take effect at Instant.
        6. Maintain the connection using the new timing.
        7. Require successful HCI Update Complete with the selected parameters.

    Expected Outcome:
        Pass verdict:
            All three requests are accepted, the ACL is maintained using the
            selected new parameters, and HCI reports each completed update.

    Coverage Note:
        Select supported limits 7.5 ms/4 s and 32-s maximum supervision.
        Check every Host approval field and positive-reply handle, old/new
        schedules, legal transmit-window reception and no early HCI success.
        Validate subsequent DATA and full HCI parameters before termination.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-30-C procedure in
        Section 4.3.6, Figure 4.3-160; PDF 790-793, printed 4.3-317 through
        4.3-320. Cases 1-3, each Steps 1-7, and Expected Outcome; Core 5.1.7.
    """
    await _remote_parameters(
        tester, central=True, reject=False, masked=False, invalid=False
    )


# Verify that a Central accepts Peripheral parameter requests without a preferred
# periodicity, adopts the selected timing and informs its Host.
PROCEDURES |= procedure_cases(
    accepting_parameter_request_no_preferred_periodicity_cen_bv_30,
    {
        'LL/CON/CEN/BV-30-C': (
            'Accepting Connection Parameter Request – no Preferred_Periodicity',
            {},
        ),
    },
)


async def accepting_parameter_request_preferred_anchors_cen_bv_31(
    tester,
):
    """Accepting Connection Parameter Request – preferred anchor points only.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and common interval, latency, timeout and
        channel map. Maintain the same ACL through all three cases.

    Test Steps:
        Case 1:
        1. Send LL_CONNECTION_PARAM_REQ with Offset0=1.25 ms, Offset1-5
           unspecified, and unchanged interval, latency and supervision timeout.
        2. Receive and acknowledge LL_CONNECTION_UPDATE_IND that moves
           the connection anchors by 1.25 ms.
        3. Maintain empty DATA using old timing until Instant.
        4. At Instant, maintain the connection at the new anchor phase.
        Case 2:
        1. Send LL_CONNECTION_PARAM_REQ with Offset0=interval-1.25 ms,
           Offset1-5 unspecified, and unchanged connection parameters.
        2. Receive and acknowledge LL_CONNECTION_UPDATE_IND that moves
           anchors by interval-1.25 ms.
        3. Maintain empty DATA using old timing until Instant.
        4. At Instant, maintain the connection at the new anchor phase.
        Case 3:
        1. Send LL_CONNECTION_PARAM_REQ with Offset0=1.25 ms, Offset1=2.5 ms,
           Offset2-5 unspecified, and unchanged connection parameters.
        2. Receive and acknowledge LL_CONNECTION_UPDATE_IND that selects
           the 1.25-ms anchor displacement.
        3. Maintain empty DATA using old timing until Instant.
        4. At Instant, maintain the connection at the new anchor phase.

    Expected Outcome:
        Pass verdict:
            The IUT accepts each request and maintains the ACL with actual
            anchor displacements 1.25 ms, interval-1.25 ms, and 1.25 ms.

    Coverage Note:
        These exact displacements are explicit in this procedure's verdict.
        Determine phase from received DATA, not WinOffset alone: the first
        Central packet inside the transmit window establishes the new anchor.
        Check unchanged timing parameters, old schedule through Instant and
        subsequent new-anchor DATA. An anchor-only move does not notify the
        Host of a parameter update or require Host parameter approval.
        Disconnect after all twelve numbered steps.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-31-C procedure in
        Section 4.3.6, Figure 4.3-161; PDF 793-795, printed 4.3-320 through
        4.3-322. Cases 1-3, each Steps 1-4, and Expected Outcome;
        Core 5.1.7 and anchor-point definition in 5.1.1.
    """
    await _preferred_parameters(tester, central=True, anchors_only=True, anchors=True)


# Verify Central acceptance of anchor-only Peripheral parameter requests, retaining
# interval, latency and supervision timeout while moving anchors.
PROCEDURES |= procedure_cases(
    accepting_parameter_request_preferred_anchors_cen_bv_31,
    {
        'LL/CON/CEN/BV-31-C': (
            'Accepting Connection Parameter Request – preferred anchor points only',
            {},
        ),
    },
)


async def accepting_connection_parameter_request(tester, *, anchors):
    """Accepting Connection Parameter Request.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and common connection/channel parameters.
        Configure the appropriate Table 4.3-55 row:
        BV-32-C: preferred periodicity and Offset0=0.
        BV-33-C: preferred periodicity, a valid reference event and Offset0=1.25 ms;
        Offset1-5 are unspecified.

    Test Case Configuration:
        LL/CON/CEN/BV-32-C: Preferred_Periodicity without preferred anchor
            points.
        LL/CON/CEN/BV-33-C: Preferred_Periodicity with preferred anchor points.

    Test Steps:
        1. Send LL_CONNECTION_PARAM_REQ with a nonzero interval range, zero
           latency, 3-s supervision and preferred periodicity having at least
           one multiple in the range. Apply the selected Table 4.3-55 offsets.
        2. Require the Host parameter approval request and accept it.
        3. Receive LL_CONNECTION_UPDATE_IND and acknowledge it.
        4. Maintain empty DATA using old timing until Instant.
        5. At Instant, maintain the connection using the selected new timing.
        6. Require successful HCI LE Connection Update Complete reporting
           the new parameters for the established handle.

    Expected Outcome:
        Pass verdict:
            The request is accepted, the ACL is maintained using the selected
            new parameters, and HCI reports the completed parameter update.

    Coverage Note:
        Use range 13..19 and PreferredPeriodicity=18 in 1.25-ms units, satisfying
        both the table's range wording and the requirement for a valid multiple.
        Check all Host approval fields and the positive-reply handle, accepted
        timing ranges, Instant, actual first-packet anchor and continued DATA.
        Core 5.1.7.2 says to try the periodic multiple and preferred offsets;
        these preferences are not mandatory verdict conditions in this procedure.
        Record selected interval and observed phase without rejecting a legal
        nonmultiple or a different legal phase. This differs from BV-31-C,
        whose verdict explicitly specifies the displacement. Disconnect afterwards.

    Reference:
        LL.TS.p28, Section 4.3.6.1, Table 4.3-55 and Figure 4.3-162;
        LL/CON/CEN/BV-32-C and LL/CON/CEN/BV-33-C; PDF 796-797,
        printed 4.3-323 through 4.3-324. Steps 1-6 and Expected Outcome.
    """
    await _preferred_parameters(
        tester, central=True, anchors_only=False, anchors=anchors
    )


# Verify Central acceptance of Peripheral parameter requests containing preferred
# periodicity and, optionally, preferred anchor points.
PROCEDURES |= procedure_cases(
    accepting_connection_parameter_request,
    {
        'LL/CON/CEN/BV-32-C': (
            'Accepting Connection Parameter Request – Preferred_Periodicity',
            {'anchors': False},
        ),
        'LL/CON/CEN/BV-33-C': (
            (
                'Accepting Connection Parameter Request – '
                'Preferred_Periodicity and preferred anchor points'
            ),
            {'anchors': True},
        ),
    },
)


async def accepting_connection_parameter_request_event_masked_ll_con_cen_bv_34_c(
    tester,
):
    """Accepting Connection Parameter Request – event masked.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and common latency, timeout and channel map.

    Test Steps:
        1. Mask HCI LE Remote Connection Parameter Request on the IUT.
        2. Send LL_CONNECTION_PARAM_REQ requesting the minimum interval,
           zero latency and 3-s supervision timeout.
        3. Require LL_REJECT_EXT_IND with Unsupported Remote Feature (0x1A).

    Expected Outcome:
        Pass verdict:
            The IUT rejects the parameter request with LL_REJECT_EXT_IND
            carrying error 0x1A.

    Coverage Note:
        Check the rejected opcode as well as the reason. Require no Host
        parameter approval or update-completion event while continuing
        DATA on the unchanged connection, then disconnect through HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-34-C procedure in
        Section 4.3.6, Figure 4.3-163; PDF 798, printed 4.3-325.
        Steps 1-3 and Expected Outcome; Core procedure 5.1.7.
    """
    await _remote_parameters(
        tester, central=True, reject=False, masked=True, invalid=False
    )


# Verify that a Central rejects a Peripheral parameter request requiring Host approval
# when the Host has masked that approval-request event.
PROCEDURES |= procedure_cases(
    accepting_connection_parameter_request_event_masked_ll_con_cen_bv_34_c,
    {
        'LL/CON/CEN/BV-34-C': (
            'Accepting Connection Parameter Request – event masked',
            {},
        ),
    },
)


async def accepting_connection_parameter_request_host_rejects_ll_con_cen_bv_35_c(
    tester,
):
    """Accepting Connection Parameter Request – Host rejects.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and common latency, timeout and channel map.

    Test Steps:
        1. Send LL_CONNECTION_PARAM_REQ requesting the minimum interval,
           zero latency and 3-s supervision timeout.
        2. Require the Host approval request and reject it with
           Unacceptable Connection Parameters (0x3B).
        3. Require LL_REJECT_EXT_IND carrying the Host-provided error 0x3B.

    Expected Outcome:
        Pass verdict:
            The IUT responds with LL_REJECT_EXT_IND using the error code
            supplied by the Host.

    Coverage Note:
        Check every Host request field, the HCI negative-reply handle and
        the rejected LL opcode. Continue acknowledged DATA on the old
        connection and require no successful parameter update before
        disconnecting through HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-35-C procedure in
        Section 4.3.6, Figure 4.3-164; PDF 798-799, printed 4.3-325 through
        4.3-326. Steps 1-3 and Expected Outcome; Core procedure 5.1.7.
    """
    await _remote_parameters(
        tester, central=True, reject=True, masked=False, invalid=False
    )


# Verify that a Central rejects a Peripheral parameter request with the error code
# supplied by its Host's negative reply.
PROCEDURES |= procedure_cases(
    accepting_connection_parameter_request_host_rejects_ll_con_cen_bv_35_c,
    {
        'LL/CON/CEN/BV-35-C': (
            'Accepting Connection Parameter Request – Host rejects',
            {},
        ),
    },
)


async def initiating_parameter_request_unsupported_without_feature_exchange_cen_bv_81(
    tester,
):
    """Initiating Connection Parameter Request – Unsupported Without Feature Exchange.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window, common Peripheral latency and
        supervision timeout. No Feature Exchange has disclosed peer support.

    Test Steps:
        1. If the IUT autonomously starts Feature Exchange before Step 3, end
           with Inconclusive, including a request received during setup.
        2. Send HCI_LE_Connection_Update requesting the minimum connection
           interval, zero latency and a 3 s supervision timeout. Receive
           successful HCI_Command_Status.
        3. Receive LL_CONNECTION_PARAM_REQ. Its legal parameters may differ
           from the Host request. Answer LL_UNKNOWN_RSP whose UnknownType
           identifies LL_CONNECTION_PARAM_REQ (0x0F).
        4. Receive LL_CONNECTION_UPDATE_IND. Validate its actual window,
           interval, latency and timeout; these may differ from the Host inputs.
        5. Maintain the connection with empty DATA on the old parameters until
           the event counter reaches the indication's Instant.
        6. At that Instant, maintain the connection using the newly selected
           parameters, allowing the first Central anchor within the transmit
           window.
        7. Require HCI_LE_Connection_Update_Complete with success, the active
           handle and the actual new interval, latency and supervision timeout.
           Reject a successful completion before the Instant.
        8. Repeat Steps 2 and 4-7 using Host parameters different from the first
           iteration. This implementation requests interval 15 ms and timeout
           4 s rather than 7.5 ms and 3 s. Any LL_CONNECTION_PARAM_REQ in this
           iteration fails; require the direct UPDATE_IND path and matching
           HCI completion.

    Expected Outcome:
        Pass verdict:
            The first update uses PARAM_REQ, UNKNOWN_RSP and UPDATE_IND, then
            reports the actual selected parameters. The second update omits
            PARAM_REQ and directly indicates its new parameters. Continued
            DATA and HCI agree with the selected parameters in each iteration.
        Inconclusive verdict:
            The IUT autonomously performs Feature Exchange before Step 3.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-81-C; Figure 4.3-187;
        PDF pages 850-852, printed pages 4.3-377..379; source reference [8] 5.1.7.
        Autonomous Feature Exchange: Section 4.1.6.7.4.
    """
    await _unsupported_parameters(tester, central=True, features=False)


# Verify that a Central learns from LL_UNKNOWN_RSP that its Peripheral does not support
# Connection Parameter Request, completes the current update through the legacy
# indication, and avoids the unsupported request on the next update of the same
# connection.
PROCEDURES |= procedure_cases(
    initiating_parameter_request_unsupported_without_feature_exchange_cen_bv_81,
    {
        'LL/CON/CEN/BV-81-C': (
            (
                'Initiating Connection Parameter Request – Unsupported '
                'Without Feature Exchange'
            ),
            {},
        ),
    },
)


async def initiating_parameter_request_unsupported_with_feature_exchange_cen_bv_82(
    tester,
):
    """Initiating Connection Parameter Request – Unsupported With Feature Exchange.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window, common Peripheral latency and
        supervision timeout.

    Test Steps:
        1. Initiate Feature Exchange through the Upper Tester unless the IUT
           has already initiated it. The Lower Tester reports no Connection
           Parameter Request support.
           If an autonomous FEATURE_REQ was received during setup, answer that
           request and finish its exchange rather than waiting for a second
           request. Otherwise issue HCI Read Remote Features, answer FEATURE_REQ
           with a zero peer feature set, and verify its matching HCI completion.
        2. Send HCI_LE_Connection_Update requesting the minimum connection
           interval, zero latency and a 3 s supervision timeout. Receive
           successful HCI_Command_Status.
        3. Receive LL_CONNECTION_UPDATE_IND. Its actual parameters may differ
           from the Host inputs; validate their legal ranges and transmit
           window. LL_CONNECTION_PARAM_REQ instead of UPDATE_IND fails.
        4. Maintain the connection with empty DATA on the old parameters until
           the event counter reaches the indication's Instant.
        5. At that Instant, use the newly selected parameters, allowing the
           first Central anchor within the transmit window.
        6. Require HCI_LE_Connection_Update_Complete with success, the active
           handle and the actual selected interval, latency and supervision
           timeout. Reject a successful completion before the Instant.

    Expected Outcome:
        Pass verdict:
            The IUT sends UPDATE_IND without PARAM_REQ and reports the selected
            new parameters in Connection Update Complete. Subsequent DATA uses
            the same interval and connection parameters; no duplicate completion
            is accepted.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-82-C; Figure 4.3-188;
        PDF pages 852-853, printed pages 4.3-379..380; source reference [8] 5.1.7.
    """
    await _unsupported_parameters(tester, central=True, features=True)


# Verify that a Central directly uses Connection Update Indication after Feature
# Exchange discloses that its Peripheral does not support Connection Parameter Request.
PROCEDURES |= procedure_cases(
    initiating_parameter_request_unsupported_with_feature_exchange_cen_bv_82,
    {
        'LL/CON/CEN/BV-82-C': (
            (
                'Initiating Connection Parameter Request – Unsupported With'
                ' Feature Exchange'
            ),
            {},
        ),
    },
)


async def initiating_connection_parameter_request_timeout_ll_con_cen_bi_05_c(tester):
    """Initiating Connection Parameter Request – Timeout.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window and connection interval, common
        Peripheral latency and supervision timeout. The Lower Tester declares
        Connection Parameter Request support through Feature Exchange so the
        IUT uses that procedure rather than a legacy direct update.

    Test Steps:
        1. Send HCI_LE_Connection_Update requesting the minimum connection
           interval (7.5 ms), zero latency and a 3 s supervision timeout.
           Receive successful HCI_Command_Status.
        2. Receive LL_CONNECTION_PARAM_REQ. Its legal parameters may differ
           from the Host request. Acknowledge the request, but send no
           LL_CONNECTION_PARAM_RSP. Continue valid empty DATA on the old
           parameters so supervision does not replace the intended timeout.
        3. Require HCI_Disconnection_Complete with success, the active handle
           and reason 0x22 (LL Response Timeout) when the control transaction
           timer expires. Verify that connection RF maintenance stops.

    Expected Outcome:
        Pass verdict:
            The connection ends through procedure-response timeout with reason
            0x22, not an early success or supervision timeout, and the IUT stops
            maintaining the link. Any optional pending procedure completion must
            report its timeout failure before disconnection, never after it.

    Observation Boundary:
        Bracket internal procedure initiation between the triggering HCI command
        and the first observed PARAM_REQ. Observe the 40 s timer around that
        bracket at 1 ms HCI resolution; this is a test-system bound, not a
        normative extra tolerance.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BI-05-C; Figure 4.3-192, Steps 1-3 and verdict;
        PDF pages 858-859, printed pages 4.3-385..386; source reference [3] 5.1.7.
        Core v6.3, Vol 6, Part B, 5.2.
    """
    await _procedure_timeout(tester, central=True, parameters=True)


# Verify that a Central terminates the connection when its Connection Parameter Request
# is acknowledged but the Peripheral never sends the corresponding protocol response.
PROCEDURES |= procedure_cases(
    initiating_connection_parameter_request_timeout_ll_con_cen_bi_05_c,
    {
        'LL/CON/CEN/BI-05-C': (
            'Initiating Connection Parameter Request – Timeout',
            {},
        ),
    },
)


async def accepting_connection_parameter_request_illegal_parameters_ll_con_cen_bi_06_c(
    tester,
):
    """Accepting Connection Parameter Request – illegal parameters.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window and connection interval, common
        Peripheral latency and supervision timeout. Leave the remote-parameter
        Host event enabled so masking cannot explain the rejection.

    Test Steps:
        1. Send LL_CONNECTION_PARAM_REQ with Interval_Min=Interval_Max=4,
           below the valid minimum value 6. This encodes 5 ms.
        2. Require LL_REJECT_EXT_IND identifying LL_CONNECTION_PARAM_REQ with
           ErrorCode=0x1E (Invalid LL Parameters).

    Expected Outcome:
        Pass verdict:
            The IUT rejects the invalid request with the correct opcode and
            reason. It does not delegate the invalid request to the Host or
            report a successful update. Continue DATA on the old parameters
            before disconnecting.

    Source Correction:
        Step 1 labels the encoded interval 4 as 6 ms. Core interval units are
        1.25 ms, so the value means 5 ms; retain the prescribed wire value 4.
        Amber corroborated the unit calculation. No SIG erratum is claimed.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BI-06-C; Figure 4.3-193, Steps 1-2 and verdict;
        PDF pages 859-860, printed pages 4.3-386..387; source reference [3] 5.1.7.
        Core v6.3, Vol 6, Part B, 2.4.2.16.
    """
    await _remote_parameters(
        tester, central=True, reject=False, masked=False, invalid=True
    )


# Verify that a Central rejects a Peripheral Connection Parameter Request whose minimum
# and maximum connection intervals are outside the legal range.
PROCEDURES |= procedure_cases(
    accepting_connection_parameter_request_illegal_parameters_ll_con_cen_bi_06_c,
    {
        'LL/CON/CEN/BI-06-C': (
            'Accepting Connection Parameter Request – illegal parameters',
            {},
        ),
    },
)


async def reject_invalid_connection_parameter_request_parameters(tester, *, central):
    """Reject Invalid Connection Parameter Request Parameters.

    Initial Condition:
        Connected in the selected role on LE 1M, with public addresses, common
        connection interval, Peripheral latency and supervision timeout, and
        a selected channel map. Any advertising interval, advertising channel
        map and SCA value are permitted.

    Test Case Configuration:
        LL/CON/CEN/BI-13-C: Central.
        LL/CON/PER/BI-16-C: Peripheral.

    Test Steps:
        1. Send LL_CONNECTION_PARAM_REQ with Interval_Max < Interval_Min.
        2. Require LL_REJECT_EXT_IND with RejectOpcode LL_CONNECTION_PARAM_REQ
           and Invalid LMP Parameters / Invalid LL Parameters (0x1E).

    Expected Outcome:
        Pass verdict:
            The IUT rejects the offending opcode through LL_REJECT_EXT_IND
            with error code 0x1E.

    Coverage Note:
        Use a minimum one unit above the legal minimum and a maximum equal to
        the legal minimum. Keep other fields valid to isolate reversed bounds,
        rather than an independently invalid interval. Acknowledge the reject;
        observe three unchanged connection events with no Host parameter
        approval or update-completion event, then disconnect through HCI.

    Reference:
        LL.TS.p28.pdf, Section 4.3.7.45; Table 4.3-127, Figure 4.3-257,
        Steps 1-2 and pass verdict; PDF 1006-1007, printed 4.3-533..534.
        Source reference [10] 2.4.2.16.
    """
    parameters = _advertiser_connection_parameters()
    link = await establish(
        tester,
        central=central,
        interval=parameters.interval,
        timeout=parameters.timeout,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    request = _parameter_request(
        link, LL_CONN_INTERVAL_MIN + 1, link.parameters.timeout
    )
    fields = list(struct.unpack('<HHHHBH6H', request[1:]))
    fields[1] = LL_CONN_INTERVAL_MIN
    request = bytes([Opcode.LL_CONNECTION_PARAM_REQ]) + struct.pack(
        '<HHHHBH6H', *fields
    )
    response = await link.control(Opcode.LL_REJECT_EXT_IND, request, check_ack=True)
    assert response == bytes(
        [
            Opcode.LL_REJECT_EXT_IND,
            Opcode.LL_CONNECTION_PARAM_REQ,
            ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR,
        ]
    )
    for _ in range(3):
        await link.exchange()
        assert not any(
            isinstance(
                event,
                (
                    hci.HCI_LE_Remote_Connection_Parameter_Request_Event,
                    hci.HCI_LE_Connection_Update_Complete_Event,
                ),
            )
            for event in tester.events
        )
    await link.disconnect()


# Verify that the IUT rejects a Connection Parameters Request whose Interval_Max is
# smaller than Interval_Min.
PROCEDURES |= procedure_cases(
    reject_invalid_connection_parameter_request_parameters,
    {
        'LL/CON/CEN/BI-13-C': (
            'Reject Invalid Connection Parameter Request Parameters',
            {'central': True},
        ),
        'LL/CON/PER/BI-16-C': (
            'Reject Invalid Connection Parameter Request Parameters',
            {'central': False},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_connection_parameters(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
