# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.3: PHY update and procedure collisions."""

import random
import struct
from collections import deque

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_CHANNEL_MAP_SIZE,
    LL_CONN_INTERVAL_MIN,
    LL_CONN_INTERVAL_UNIT_US,
    LL_CONN_WINDOW_SIZE_MIN,
    LL_DATA_CHANNEL_COUNT,
    LL_EVENT_COUNTER_MASK,
    LL_INSTANT_MIN_LISTENING_EVENTS,
    LL_RESPONSE_TIMEOUT_US,
    LL_SUPERVISION_TIMEOUT_UNIT_US,
    PDU_CONTROL_CHANNEL_MAP_SIZE,
    PDU_CONTROL_FEATURE_SET_SIZE,
    PDU_CONTROL_INSTANT_SIZE,
    PDU_CONTROL_PHY_REQ_SIZE,
    PDU_DATA_LLID_MASK,
    PDU_DATA_PAYLOAD_SIZE_MIN,
    PDU_HEADER_SIZE,
    LlId,
)
from bscontroller.radio.base import Phy
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.connection import (
    CONTROL_OBSERVATION_EVENTS,
    HCI_OBSERVATION_STEP_US,
    LENGTH_FIELDS,
    MAINTAINED_EVENTS,
    MAXIMUM,
    MINIMUM,
    PROCEDURE_COLLISION,
    ErrorCode,
    Opcode,
    _accept_remote_parameters,
    _check_changed_events,
    _check_feature_pdu,
    _check_parameter_pdu,
    _check_parameters,
    _check_rf_stopped,
    _connection_event,
    _disconnected,
    _effective_lengths,
    _LengthObserver,
    _observe_disconnection,
    _parameter_request,
    _prepare_preferences,
    _restore_peer,
    _sequence_bits,
    _supported_lengths,
)
from tests.qualification.link import _check_parameter_update, _data_pdu, _wire_event
from tests.qualification.peer import change_phy as _change_phy
from tests.qualification.peer import check_phy_choice as _check_phy_choice
from tests.qualification.peer import establish, peer_send
from tests.qualification.peer import finish_phy_update as _finish_update
from tests.qualification.peer import phy_bit as _bit
from tests.qualification.peer import phy_request as _phy_request
from tests.qualification.peer import phy_update_pdu as _update_pdu
from tests.qualification.peer import settle_autonomous_phy as _settle_autonomous_phy
from tests.qualification.tester import (
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
)

PROCEDURES = ProcedureMap()

# LL.TS.p28 Table 4.3-56, PDF pages 801-802: ALL_PHYS, TX, RX, peer RX, peer TX.
# Literal field values are specification stimuli, not implementation selections.
CENTRAL_INITIATING = (
    (0, 2, 2, 2, 2),
    (0, 1, 2, 1, 2),
    (0, 2, 1, 2, 1),
    (0, 1, 1, 1, 1),
    (0, 3, 2, 3, 2),
    (0, 3, 1, 3, 1),
    (0, 1, 3, 1, 3),
    (0, 2, 3, 2, 3),
    (0, 3, 3, 3, 3),
    (1, 0, 2, 3, 3),
    (2, 2, 0, 3, 3),
    (3, 0, 0, 3, 3),
    (0, 4, 4, 4, 4),
    (0, 1, 4, 1, 4),
    (0, 4, 1, 4, 1),
    (0, 1, 1, 1, 1),
    (0, 5, 4, 5, 4),
    (0, 5, 1, 5, 1),
    (0, 1, 5, 1, 5),
    (0, 4, 5, 4, 5),
    (0, 5, 5, 5, 5),
    (1, 0, 4, 5, 5),
    (2, 4, 0, 5, 5),
    (3, 0, 0, 5, 5),
    (0, 6, 6, 2, 2),
    (0, 6, 6, 4, 4),
    (0, 6, 6, 2, 2),
    (0, 2, 4, 2, 4),
    (0, 4, 2, 4, 2),
    (3, 0, 0, 4, 4),
    (3, 0, 0, 2, 2),
    (3, 0, 0, 1, 1),
)

# Table 4.3-41, PDF pages 641-643: ALL_PHYS, TX, RX, Central TX, Peripheral TX.
PERIPHERAL_INITIATING = (
    (0, 1, 3, 2, 0),
    (0, 2, 5, 1, 2),
    (0, 4, 2, 2, 4),
    (0, 2, 1, 0, 2),
    (0, 1, 4, 4, 0),
    (0, 6, 3, 2, 2),
    (0, 1, 1, 1, 1),
    (0, 3, 4, 0, 1),
    (0, 1, 5, 0, 0),
    (0, 2, 4, 4, 2),
    (0, 7, 3, 1, 0),
    (0, 6, 6, 2, 4),
    (0, 2, 3, 2, 2),
    (0, 6, 1, 1, 4),
    (0, 6, 5, 1, 4),
    (0, 5, 4, 4, 1),
    (0, 5, 1, 1, 0),
    (0, 3, 5, 1, 2),
    (0, 4, 6, 2, 4),
    (0, 7, 2, 0, 1),
    (0, 1, 6, 2, 0),
    (0, 2, 2, 2, 0),
    (0, 4, 3, 1, 4),
    (0, 3, 7, 4, 1),
    (0, 1, 2, 2, 1),
    (0, 3, 3, 1, 1),
    (0, 3, 2, 2, 1),
    (0, 7, 4, 0, 2),
    (0, 4, 7, 0, 0),
    (0, 1, 7, 4, 0),
    (0, 5, 6, 4, 1),
    (0, 6, 2, 0, 4),
    (0, 7, 7, 1, 1),
    (0, 6, 4, 4, 2),
    (0, 5, 2, 2, 1),
    (0, 2, 6, 4, 0),
    (0, 2, 7, 1, 0),
    (0, 6, 7, 4, 4),
    (0, 7, 6, 2, 2),
    (0, 3, 6, 0, 2),
    (0, 4, 5, 4, 4),
    (0, 5, 7, 4, 1),
    (0, 7, 1, 0, 4),
    (0, 6, 6, 2, 2),
    (0, 6, 6, 4, 4),
    (1, 0, 1, 1, 1),
    (2, 3, 0, 2, 2),
    (3, 0, 0, 1, 1),
    (2, 5, 0, 4, 4),
    (3, 0, 0, 4, 4),
    (3, 0, 0, 2, 2),
    (1, 0, 6, 4, 4),
    (1, 0, 6, 4, 2),
    (2, 7, 0, 2, 1),
    (2, 6, 0, 2, 2),
    (1, 0, 8, 2, 2),
    (1, 0, 17, 1, 0),
    (1, 0, 32, 2, 1),
    (1, 0, 65, 1, 1),
    (1, 0, 128, 1, 2),
    (1, 0, 255, 0, 1),
    (2, 8, 0, 0, 0),
    (2, 17, 0, 2, 0),
    (2, 32, 0, 0, 2),
    (2, 65, 0, 1, 1),
    (2, 128, 0, 2, 2),
    (2, 255, 0, 1, 1),
)

# Tables 4.3-59/44, PDF pages 805-807/646-647: TX, RX, ordered Central/Peripheral
# preferences.
RESPONDING = (
    (3, 1, (2,), (1,)),
    (5, 2, (1,), (2,)),
    (2, 4, (2,), (4,)),
    (1, 2, (1,), (2,)),
    (4, 1, (4,), (1,)),
    (3, 6, (2,), (2,)),
    (1, 1, (1,), (1,)),
    (4, 3, (4,), (1,)),
    (5, 1, (1,), (1,)),
    (4, 4, (4,), (4,)),
    (5, 7, (4,), (2, 4)),
    (5, 5, (4,), (1,)),
    (4, 2, (4,), (2,)),
    (3, 7, (1,), (4, 1)),
    (6, 6, (2,), (4,)),
    (3, 2, (2,), (2,)),
    (1, 6, (1,), (4,)),
    (5, 6, (1,), (4,)),
    (4, 5, (4,), (1,)),
    (1, 5, (1,), (4,)),
    (5, 3, (1,), (2,)),
    (1, 4, (1,), (4,)),
    (1, 3, (1,), (2,)),
    (3, 5, (2,), (1,)),
    (6, 4, (2,), (4,)),
    (2, 7, (2,), (1, 2)),
    (6, 1, (2,), (1,)),
    (2, 2, (2,), (2,)),
    (3, 4, (1,), (4,)),
    (7, 3, (4, 1), (1,)),
    (2, 1, (2,), (1,)),
    (3, 3, (1,), (1,)),
    (2, 3, (2,), (1,)),
    (4, 7, (4,), (2, 4)),
    (7, 4, (1, 4), (4,)),
    (7, 1, (4, 2), (1,)),
    (6, 5, (4,), (1,)),
    (2, 6, (2,), (4,)),
    (7, 7, (1, 2), (1, 4)),
    (4, 6, (4,), (2,)),
    (2, 5, (2,), (1,)),
    (6, 2, (4,), (2,)),
    (7, 2, (1, 2), (2,)),
    (7, 6, (4, 1), (4,)),
    (6, 7, (2,), (2, 1)),
    (6, 3, (2,), (2,)),
    (5, 4, (4,), (4,)),
    (7, 5, (4, 2), (1,)),
    (1, 7, (1,), (4, 2)),
)


async def _central_response(link, request):
    """Handle Step 3 collision, but forbid later unsolicited requests."""
    while True:
        packet = await link.exchange(request, llid=LlId.CONTROL)
        if _phy_request(packet) is not None:
            rejection = await link.control(Opcode.LL_REJECT_EXT_IND)
            assert rejection == bytes(
                [Opcode.LL_REJECT_EXT_IND, Opcode.LL_PHY_REQ, PROCEDURE_COLLISION]
            )
            await _settle_autonomous_phy(link)
            continue
        for _ in range(16):
            if packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL:
                response = packet.pdu[PDU_HEADER_SIZE:]
                assert response[0] == Opcode.LL_PHY_UPDATE_IND, (
                    'unexpected control after PHY request; Central must not send '
                    'PHY_RSP'
                )
                return response
            packet = await link.exchange()
        raise AssertionError('Central PHY update indication absent')


def _initiating_selection(request, preferences, current):
    tx, rx = request[1:]
    central = rx & preferences[0]
    peripheral = tx & preferences[1]
    if tx == rx and tx.bit_count() == 1:
        central = peripheral = tx
    return _no_change_bits(central, peripheral, current)


async def _past_phy_update_outcome(link, update):
    """Accept a failed past-Instant update or the source's immediate-PHY alternative."""
    tester = link.tester
    packet, sent = await _wire_event(
        link,
        lambda _: _data_pdu(link.sn, link.nesn, update, llid=LlId.CONTROL),
        required=False,
    )
    if packet is not None:
        _restore_peer(link, packet, sent)
    event = await tester.expect(
        lambda event: isinstance(
            event,
            (
                hci.HCI_LE_PHY_Update_Complete_Event,
                hci.HCI_Disconnection_Complete_Event,
            ),
        )
    )
    assert event.connection_handle == link.handle
    if isinstance(event, hci.HCI_LE_PHY_Update_Complete_Event):
        if event.status == ErrorCode.SUCCESS:
            target = Phy(update[1].bit_length())
            assert (event.tx_phy, event.rx_phy) == (target, target)
            link.tx_phy = link.rx_phy = target
            for _ in range(MAINTAINED_EVENTS):
                await link.exchange(check_ack=True)
                assert _disconnected(tester) is None
                assert not any(
                    isinstance(value, hci.HCI_LE_PHY_Update_Complete_Event)
                    for value in tester.events
                ), 'duplicate past-Instant PHY completion'
            await link.disconnect(local=True)
            return
        event = await tester.event(hci.HCI_Disconnection_Complete_Event)
    assert (event.status, event.connection_handle, event.reason) == (
        ErrorCode.SUCCESS,
        link.handle,
        ErrorCode.INSTANT_PASSED_ERROR,
    )
    for value in list(tester.events):
        if isinstance(value, hci.HCI_LE_PHY_Update_Complete_Event):
            assert value.connection_handle == link.handle
            assert value.status != ErrorCode.SUCCESS
            tester.events.remove(value)
    deadline = (
        tester.radio.now_us + link.parameters.timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
    )
    while link.anchor < deadline:
        await link._send(link.channel, b'', LlId.CONTINUATION, link.anchor)
        until = min(
            deadline,
            link.anchor + link.parameters.interval * LL_CONN_INTERVAL_UNIT_US - 1,
        )
        assert (
            await tester.receive(
                link.channel,
                duration_us=until - tester.radio.now_us,
                **link.radio_parameters,
            )
            is None
        )
        link.counter = link.counter + 1 & LL_EVENT_COUNTER_MASK
        link.anchor += link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
    await tester.wait(max(0, deadline - tester.radio.now_us))


async def _invalid_phy_update_outcome(link, update, declared):
    """Accept disconnection or verify Feature Exchange on the unchanged active PHY."""
    tester = link.tester
    pending = deque(
        [
            update,
            bytes([Opcode.LL_FEATURE_REQ]) + bytes(PDU_CONTROL_FEATURE_SET_SIZE),
        ]
    )
    deadline = tester.radio.now_us + LL_RESPONSE_TIMEOUT_US
    while link.anchor < deadline:
        event = _disconnected(tester)
        if event is not None:
            assert (event.status, event.connection_handle) == (
                ErrorCode.SUCCESS,
                link.handle,
            )
            for value in list(tester.events):
                if isinstance(value, hci.HCI_LE_PHY_Update_Complete_Event):
                    assert value.connection_handle == link.handle
                    assert value.status != ErrorCode.SUCCESS
                    tester.events.remove(value)
            await _check_rf_stopped(link)
            return True
        data = pending[0] if pending else b''
        sent_sn = link.sn
        packet, sent = await _wire_event(
            link,
            lambda _, data=data: _data_pdu(
                link.sn,
                link.nesn,
                data,
                llid=LlId.CONTROL if data else LlId.CONTINUATION,
            ),
            required=False,
        )
        if packet is None:
            continue
        if pending and _sequence_bits(packet)[1] != sent_sn:
            pending.popleft()
            if len(pending) == 1:
                deadline = (
                    link.anchor
                    + link.tx_phy.airtime_us(PDU_HEADER_SIZE + len(pending[0]))
                    + LL_RESPONSE_TIMEOUT_US
                )
        _restore_peer(link, packet, sent)
        if packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL and packet.pdu[
            PDU_HEADER_SIZE : PDU_HEADER_SIZE + 1
        ] == bytes([Opcode.LL_FEATURE_RSP]):
            assert not pending, 'Feature Response before its request was acknowledged'
            _check_feature_pdu(declared, packet.pdu[PDU_HEADER_SIZE:])
            await link.exchange(check_ack=True)
            assert _disconnected(tester) is None
            assert not any(
                isinstance(value, hci.HCI_LE_PHY_Update_Complete_Event)
                for value in tester.events
            ), 'invalid-PHY maintenance reported a PHY completion'
            return False
    raise AssertionError(
        'invalid PHY update neither disconnected nor maintained LL traffic'
    )


async def _invalid_update(tester, *, past=False, rfu=False):
    """Inject past Instants or the source's invalid-PHY table rows."""
    link = await establish(tester, peer_address=LOWER_CENTRAL_PUBLIC_ADDRESS)
    declared = 0
    if past:
        await _prepare_preferences(tester, link)
        rows = ((_bit(Phy.LE_2M), _bit(Phy.LE_2M)),)
    else:
        declared = int.from_bytes(
            (
                await tester.command(hci.HCI_LE_Read_Local_Supported_Features_Command())
            ).le_features,
            'little',
        )
        invalid = 128 if rfu else _bit(Phy.LE_1M) | _bit(Phy.LE_2M)
        rows = (
            (invalid, invalid),
            (invalid, _bit(Phy.LE_1M)),
            (_bit(Phy.LE_1M), invalid),
        )
    for central, peripheral in rows:
        mask = _bit(Phy.LE_2M) if past else 255
        response = await link.control(
            Opcode.LL_PHY_RSP, bytes([Opcode.LL_PHY_REQ, mask, mask])
        )
        assert len(response) == PDU_CONTROL_PHY_REQ_SIZE and response[1] and response[2]
        supported = _bit(Phy.LE_1M) | _bit(Phy.LE_2M) | _bit(Phy.LE_CODED)
        assert not (response[1] | response[2]) & ~supported
        if past and (not response[1] & response[2] & _bit(Phy.LE_2M)):
            pytest.skip(
                'Inconclusive: PHY response does not select a non-1M PHY in both fields'
            )
        update = _update_pdu(link, central, peripheral)
        if past:
            instant = link.counter + LL_EVENT_COUNTER_MASK // 2 & LL_EVENT_COUNTER_MASK
            update = update[:-PDU_CONTROL_INSTANT_SIZE] + instant.to_bytes(
                PDU_CONTROL_INSTANT_SIZE, 'little'
            )
            await _past_phy_update_outcome(link, update)
            return
        if await _invalid_phy_update_outcome(link, update, declared):
            link = await establish(tester, peer_address=LOWER_CENTRAL_PUBLIC_ADDRESS)
    await link.disconnect(local=True)


def _no_change_bits(central, peripheral, current):
    return tuple(
        (
            value if value != _bit(old) else 0
            for value, old in zip((central, peripheral), current)
        )
    )


async def _no_common_phy(tester, *, asymmetric=False):
    """Complete a Central PHY request without changing either radio direction."""
    link = await establish(
        tester, central=True, peer_address=LOWER_PERIPHERAL_PUBLIC_ADDRESS
    )
    one, two = (_bit(Phy.LE_1M), _bit(Phy.LE_2M))
    await tester.command(
        hci.HCI_LE_Set_PHY_Command(link.handle, 0, one if asymmetric else two, two, 0)
    )
    request = await link.control(Opcode.LL_PHY_REQ, required=False)
    if request is None or (asymmetric and request[1:] != bytes([one, two])):
        pytest.skip('Inconclusive: required LL_PHY_REQ preferences not selected')
    assert len(request) == PDU_CONTROL_PHY_REQ_SIZE and request[1] and request[2]
    if asymmetric:
        peer_tx = peer_rx = two
    else:
        peer_tx, peer_rx = tuple(
            two if mask == one else one for mask in reversed(request[1:])
        )
    await link.exchange(bytes([Opcode.LL_PHY_RSP, peer_tx, peer_rx]), llid=LlId.CONTROL)
    assert not any(
        isinstance(event, hci.HCI_LE_PHY_Update_Complete_Event)
        for event in tester.events
    ), 'PHY completion before the no-change indication was sent'
    update = await link.control(Opcode.LL_PHY_UPDATE_IND)
    assert update == bytes([Opcode.LL_PHY_UPDATE_IND, 0, 0, 0, 0])
    await _finish_update(tester, link, update, event_required=True, allowed=(0, 0))
    await link.disconnect(local=True)


async def _phy_timeout(tester, *, central=False, local=True):
    """Keep supervision alive while an acknowledged PHY request cannot complete."""
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    before = tester.radio.now_us
    mask = _bit(Phy.LE_2M)
    if local:
        await tester.command(hci.HCI_LE_Set_PHY_Command(link.handle, 0, mask, mask, 0))
        request = await link.control(Opcode.LL_PHY_REQ, required=False)
        if request is None:
            pytest.skip('Inconclusive: IUT did not initiate LL_PHY_REQ')
    else:
        request = await link.control(
            Opcode.LL_PHY_RSP, bytes([Opcode.LL_PHY_REQ, mask, mask])
        )
    after = link.last_received.start_us
    assert len(request) == PDU_CONTROL_PHY_REQ_SIZE and request[1] and request[2]
    if not central:
        await link.exchange()
    earliest = before + LL_RESPONSE_TIMEOUT_US - HCI_OBSERVATION_STEP_US
    latest = after + LL_RESPONSE_TIMEOUT_US + HCI_OBSERVATION_STEP_US
    while link.anchor < earliest:
        assert _disconnected(tester) is None
        packet = await link.exchange()
        if packet.pdu[1]:
            assert packet.pdu[PDU_HEADER_SIZE:] == request
        assert not any(
            isinstance(event, hci.HCI_LE_PHY_Update_Complete_Event)
            and event.status == ErrorCode.SUCCESS
            for event in tester.events
        )
    event = await _observe_disconnection(tester, earliest, latest)
    assert (event.status, event.connection_handle, event.reason) == (
        ErrorCode.SUCCESS,
        link.handle,
        ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR,
    )
    await _check_rf_stopped(link)
    for event in tester.events:
        if isinstance(event, hci.HCI_LE_PHY_Update_Complete_Event):
            assert (
                event.status != ErrorCode.SUCCESS
                and event.connection_handle == link.handle
            )


async def _phy_update(tester, *, central, local):
    """Run the complete role-specific PHY matrix in ordered and seeded random rounds."""
    result = await tester.command(hci.HCI_LE_Read_Local_Supported_Features_Command())
    features = int.from_bytes(result.le_features, 'little')
    assert features & int(
        hci.LeFeatureMask.LE_2M_PHY | hci.LeFeatureMask.LE_CODED_PHY
    ) == int(hci.LeFeatureMask.LE_2M_PHY | hci.LeFeatureMask.LE_CODED_PHY), (
        'reduced-feature PHY tables require a separate procedure'
    )
    supported = _bit(Phy.LE_1M) | _bit(Phy.LE_2M) | _bit(Phy.LE_CODED)
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    if not local:
        await _prepare_preferences(tester, link)
    table = (
        CENTRAL_INITIATING
        if central and local
        else PERIPHERAL_INITIATING
        if local
        else RESPONDING
    )
    rows = list(enumerate(table, 1))
    changes = procedures = 0
    for repetition in range(2):
        for number, row in rows:
            tester.output.write(f'PHY ROUND {repetition + 1} ROW {number} {row}\n')
            current = (
                (link.tx_phy, link.rx_phy)
                if not central
                else (link.rx_phy, link.tx_phy)
            )
            if local:
                all_phys, tx, rx, peer_a, peer_b = row
                expected = (
                    ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR
                    if (tx | rx) & ~supported
                    else ErrorCode.SUCCESS
                )
                status = await tester.command(
                    hci.HCI_LE_Set_PHY_Command(link.handle, all_phys, tx, rx, 0),
                    status=expected,
                )
                request = await link.control(Opcode.LL_PHY_REQ, required=False)
                if request is None:
                    if status.status == ErrorCode.SUCCESS:
                        event = await tester.event(hci.HCI_LE_PHY_Update_Complete_Event)
                        assert (
                            event.status,
                            event.connection_handle,
                            event.tx_phy,
                            event.rx_phy,
                        ) == (ErrorCode.SUCCESS, link.handle, link.rx_phy, link.tx_phy)
                    else:
                        assert not any(
                            isinstance(event, hci.HCI_LE_PHY_Update_Complete_Event)
                            for event in tester.events
                        )
                    continue
                assert len(request) == PDU_CONTROL_PHY_REQ_SIZE
                req_tx, req_rx = request[1:]
                assert req_tx and req_rx and (not (req_tx | req_rx) & ~supported)
                if central:
                    update = await link.control(
                        Opcode.LL_PHY_UPDATE_IND,
                        bytes([Opcode.LL_PHY_RSP, peer_b, peer_a]),
                    )
                    allowed = (req_tx & peer_a, req_rx & peer_b)
                else:
                    selected = _initiating_selection(request, (peer_a, peer_b), current)
                    update = _update_pdu(link, *selected)
                    await link.exchange(update, llid=LlId.CONTROL, check_ack=True)
                    allowed = (req_rx, req_tx)
                event_required = status.status == ErrorCode.SUCCESS
            else:
                tx, rx, preferences_central, preferences_peripheral = row
                request = bytes([Opcode.LL_PHY_REQ, tx, rx])
                if central:
                    update = await _central_response(link, request)
                    allowed = (rx, tx)
                    if tx == rx and tx.bit_count() == 1:
                        assert not any(update[1:3]) or all(
                            (selected or _bit(old)) == tx
                            for selected, old in zip(update[1:3], current)
                        ), 'single-PHY request did not select symmetric effective PHYs'
                else:
                    response = await link.control(Opcode.LL_PHY_RSP, request)
                    assert len(response) == PDU_CONTROL_PHY_REQ_SIZE
                    rsp_tx, rsp_rx = response[1:]
                    assert rsp_tx and rsp_rx and (not (rsp_tx | rsp_rx) & ~supported)
                    selected = _responding_selection(
                        request,
                        response,
                        (preferences_central, preferences_peripheral),
                        current,
                    )
                    update = _update_pdu(link, *selected)
                    await link.exchange(update, llid=LlId.CONTROL, check_ack=True)
                    allowed = (tx & rsp_rx, rx & rsp_tx)
                event_required = False
            changes += await _finish_update(
                tester,
                link,
                update,
                event_required=event_required,
                allowed=allowed,
                constrained_zero=central and local,
                allow_autonomous=central and not local,
            )
            procedures += 1
        random.Random(0).shuffle(rows)
    requires_change = central if local else not central
    if not procedures or (requires_change and not changes):
        pytest.skip('Inconclusive: no required PHY procedure/change was exercised')
    await link.disconnect(local=True)


def _responding_selection(request, response, preferences, current):
    req_tx, req_rx = request[1:]
    rsp_tx, rsp_rx = response[1:]
    central = _select(req_tx & rsp_rx, preferences[0])
    peripheral = _select(req_rx & rsp_tx, preferences[1])
    if rsp_tx == rsp_rx and rsp_tx.bit_count() == 1:
        central = peripheral = rsp_tx if rsp_tx & req_tx & req_rx else 0
    return _no_change_bits(central, peripheral, current)


def _select(mask, preferences):
    if mask.bit_count() <= 1:
        return mask
    return next(value for value in preferences if value & mask)


# Minimum interval, zero latency and intermediate 3-second supervision timeout.
PARAMETER_TIMEOUT = 300


async def _central_collision(tester, *, procedure):
    """Reject a Peripheral PHY request while retaining the Central's procedure."""
    link = await establish(
        tester, central=True, peer_address=LOWER_PERIPHERAL_PUBLIC_ADDRESS
    )
    mask = _bit(Phy.LE_2M)
    request_opcode = {
        'phy': Opcode.LL_PHY_REQ,
        'map': Opcode.LL_CHANNEL_MAP_IND,
        'parameters': Opcode.LL_CONNECTION_PARAM_REQ,
    }[procedure]
    if procedure == 'phy':
        await tester.command(hci.HCI_LE_Set_PHY_Command(link.handle, 0, mask, mask, 0))
    elif procedure == 'map':
        even = sum(1 << channel for channel in range(0, LL_DATA_CHANNEL_COUNT, 2))
        for attempt in range(20):
            channel_map = even if not attempt else even & ~(1 << 2 * (attempt - 1))
            result = await tester.command(
                hci.HCI_LE_Set_Host_Channel_Classification_Command(
                    channel_map.to_bytes(LL_CHANNEL_MAP_SIZE, 'little')
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
    else:
        await link.control(
            Opcode.LL_FEATURE_RSP,
            bytes([Opcode.LL_PERIPHERAL_FEATURE_REQ])
            + int(hci.LeFeatureMask.CONNECTION_PARAMETERS_REQUEST_PROCEDURE).to_bytes(
                PDU_CONTROL_FEATURE_SET_SIZE, 'little'
            ),
        )
        await tester.command(
            hci.HCI_LE_Connection_Update_Command(
                link.handle,
                LL_CONN_INTERVAL_MIN,
                LL_CONN_INTERVAL_MIN,
                0,
                PARAMETER_TIMEOUT,
                0,
                0,
            )
        )
    original = None

    def collide(packet):
        nonlocal original
        if (
            packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
            and packet.pdu[PDU_HEADER_SIZE] == request_opcode
        ):
            original = packet.pdu[PDU_HEADER_SIZE:]
            return (bytes([Opcode.LL_PHY_REQ, mask, mask]), LlId.CONTROL)
        return (b'', LlId.CONTINUATION)

    for _ in range(16):
        await link.exchange(reply=collide)
        if original is not None:
            break
    if original is None:
        if procedure == 'phy':
            pytest.skip('Inconclusive: required local LL indication/request absent')
        pytest.fail(f'Central did not initiate the {procedure} procedure')
    reason = (
        PROCEDURE_COLLISION
        if procedure == 'phy'
        else ErrorCode.DIFFERENT_TRANSACTION_COLLISION_ERROR
    )
    rejection = await link.control(Opcode.LL_REJECT_EXT_IND)
    assert rejection == bytes([Opcode.LL_REJECT_EXT_IND, Opcode.LL_PHY_REQ, reason])
    if procedure == 'phy':
        assert len(original) == PDU_CONTROL_PHY_REQ_SIZE and original[1] and original[2]
        supported = sum(_bit(phy) for phy in Phy)
        assert not (original[1] | original[2]) & ~supported
        update = await link.control(
            Opcode.LL_PHY_UPDATE_IND, bytes([Opcode.LL_PHY_RSP, mask, mask])
        )
        allowed = (original[1] & mask, original[2] & mask)
        if not _check_phy_choice(
            update,
            allowed,
            (link.rx_phy, link.tx_phy),
            constrained_zero=True,
        ):
            pytest.skip('Inconclusive: Central did not select a PHY change')
        await _finish_update(
            tester,
            link,
            update,
            event_required=True,
            allowed=allowed,
            constrained_zero=True,
        )
    elif procedure == 'parameters':
        _check_parameter_pdu(original)
        response = (
            bytes([Opcode.LL_CONNECTION_PARAM_RSP])
            + _parameter_request(link, LL_CONN_INTERVAL_MIN, PARAMETER_TIMEOUT)[1:]
        )
        update = await link.control(Opcode.LL_CONNECTION_UPDATE_IND, response)
        await _check_parameter_update(
            tester, link, update, LL_CONN_INTERVAL_MIN, PARAMETER_TIMEOUT
        )
    else:
        assert len(original) == PDU_CONTROL_CHANNEL_MAP_SIZE
        assert (
            int.from_bytes(original[1:-PDU_CONTROL_INSTANT_SIZE], 'little')
            == channel_map
        )
        await link.apply_update(original)
        for _ in range(3):
            await link.exchange()
        result = await tester.command(hci.HCI_LE_Read_Channel_Map_Command(link.handle))
        assert result.connection_handle == link.handle
        assert int.from_bytes(result.channel_map, 'little') == channel_map
    assert not any(
        isinstance(event, hci.HCI_LE_PHY_Update_Complete_Event)
        for event in tester.events
    ), 'rejected remote request leaked an HCI PHY completion'
    await peer_send(tester, link, b'collision-complete')
    await link.disconnect(local=True)


async def _phy_collision_peripheral_collision(tester, *, procedure):
    """Yield a local PHY procedure to the Central without losing its new procedure."""
    link = await establish(tester, peer_address=LOWER_CENTRAL_PUBLIC_ADDRESS)
    mask = _bit(Phy.LE_2M)
    await tester.command(hci.HCI_LE_Set_PHY_Command(link.handle, 0, mask, mask, 0))
    request = await link.control(Opcode.LL_PHY_REQ, required=False)
    if request is None:
        pytest.skip('Inconclusive: Peripheral did not initiate LL_PHY_REQ')
    assert len(request) == PDU_CONTROL_PHY_REQ_SIZE and request[1] and request[2]
    instant = link.counter + LL_INSTANT_MIN_LISTENING_EVENTS + 1 & LL_EVENT_COUNTER_MASK
    channel_map = sum(1 << channel for channel in range(0, LL_DATA_CHANNEL_COUNT, 2))
    if procedure == 'phy':
        incoming = bytes([Opcode.LL_PHY_REQ, mask, mask])
    elif procedure == 'map':
        incoming = (
            bytes([Opcode.LL_CHANNEL_MAP_IND])
            + channel_map.to_bytes(LL_CHANNEL_MAP_SIZE, 'little')
            + instant.to_bytes(PDU_CONTROL_INSTANT_SIZE, 'little')
        )
    elif procedure == 'parameters':
        incoming = _parameter_request(link, LL_CONN_INTERVAL_MIN, PARAMETER_TIMEOUT)
    else:
        assert procedure == 'update'
        incoming = bytes([Opcode.LL_CONNECTION_UPDATE_IND]) + struct.pack(
            '<BHHHHH',
            LL_CONN_WINDOW_SIZE_MIN,
            0,
            LL_CONN_INTERVAL_MIN,
            0,
            PARAMETER_TIMEOUT,
            instant,
        )
    packet = await link.exchange(incoming, llid=LlId.CONTROL, check_ack=True)
    assert packet.pdu[0] & PDU_DATA_LLID_MASK != LlId.CONTROL, (
        'Peripheral rejected or answered the Central before local rejection'
    )
    reason = (
        PROCEDURE_COLLISION
        if procedure == 'phy'
        else ErrorCode.DIFFERENT_TRANSACTION_COLLISION_ERROR
    )
    packet = await link.exchange(
        bytes([Opcode.LL_REJECT_EXT_IND, Opcode.LL_PHY_REQ, reason]),
        llid=LlId.CONTROL,
        check_ack=True,
    )
    failures = [
        event
        for event in tester.events
        if isinstance(event, hci.HCI_LE_PHY_Update_Complete_Event)
    ]
    assert failures or procedure == 'phy', 'missing HCI PHY collision failure'
    for event in failures:
        assert (event.status, event.connection_handle, event.tx_phy, event.rx_phy) == (
            reason,
            link.handle,
            Phy.LE_1M,
            Phy.LE_1M,
        )
        tester.events.remove(event)
    if procedure == 'phy':
        response = packet.pdu[PDU_HEADER_SIZE:]
        if not response or response[0] != Opcode.LL_PHY_RSP:
            response = await link.control(Opcode.LL_PHY_RSP)
        assert len(response) == PDU_CONTROL_PHY_REQ_SIZE
        supported = _bit(Phy.LE_1M) | _bit(Phy.LE_2M) | _bit(Phy.LE_CODED)
        assert (
            response[1]
            and response[2]
            and (not (response[1] | response[2]) & ~supported)
        )
        assert response[1] & response[2] & mask
        update = _update_pdu(link, mask, mask)
        await link.exchange(update, llid=LlId.CONTROL, check_ack=True)
        await _finish_update(
            tester, link, update, event_required=True, allowed=(mask, mask)
        )
    elif procedure == 'parameters':
        await _accept_remote_parameters(
            tester, link, LL_CONN_INTERVAL_MIN, PARAMETER_TIMEOUT
        )
    elif procedure == 'update':
        await _check_parameter_update(
            tester, link, incoming, LL_CONN_INTERVAL_MIN, PARAMETER_TIMEOUT
        )
    else:
        await link.apply_update(incoming)
        for _ in range(3):
            await link.exchange()
        result = await tester.command(hci.HCI_LE_Read_Channel_Map_Command(link.handle))
        assert result.connection_handle == link.handle
        assert int.from_bytes(result.channel_map, 'little') == channel_map
    assert not any(
        isinstance(event, hci.HCI_LE_PHY_Update_Complete_Event)
        for event in tester.events
    ), 'duplicate or unrelated HCI PHY completion after collision'
    await peer_send(tester, link, b'collision-complete')
    await link.disconnect(local=True)


CODED_DELAY_OCTETS = 423
CODED_PACKET_OCTETS = 141
CODED_PACKET_TIME_US = 1240

# All changed-PHY traffic procedures observe ten further events after Instant.
POST_INSTANT_EVENTS = 10

# No-change procedures delay the indication until these DATA totals are received.
UNCODED_DELAY_OCTETS = 140

# LL.TS.p28 PHY packet-time procedures: 2M->1M, or 1M->Coded.
UNCODED_PACKET_OCTETS = 32
UNCODED_PACKET_TIME_US = 328


async def _complete_update(stream, update, target):
    link, tester = (stream.link, stream.tester)
    mask = _bit(target)
    supported = sum(_bit(phy) for phy in Phy)
    _check_phy_choice(
        update,
        (supported, supported),
        (link.rx_phy, link.tx_phy) if link.central else (link.tx_phy, link.rx_phy),
    )
    if update[1:3] != bytes([mask, mask]):
        pytest.skip('Inconclusive: IUT did not select the required bidirectional PHY')
    instant = int.from_bytes(update[-PDU_CONTROL_INSTANT_SIZE:], 'little')
    distance = instant - link.counter & LL_EVENT_COUNTER_MASK
    assert 0 <= distance < LL_EVENT_COUNTER_MASK // 2
    before = _effective_lengths(stream.dut, stream.peer, link)
    for _ in range(distance):
        assert not any(
            isinstance(event, hci.HCI_LE_PHY_Update_Complete_Event)
            for event in tester.events
        ), 'PHY completion before Instant'
        await stream.event()
    assert not any(
        isinstance(event, hci.HCI_LE_PHY_Update_Complete_Event)
        for event in tester.events
    ), 'PHY completion before Instant'
    link.tx_phy = link.rx_phy = target
    await stream.event()
    event = await tester.event(hci.HCI_LE_PHY_Update_Complete_Event)
    assert (event.status, event.connection_handle, event.tx_phy, event.rx_phy) == (
        ErrorCode.SUCCESS,
        link.handle,
        target,
        target,
    )
    after = _effective_lengths(stream.dut, stream.peer, link)
    if getattr(stream, 'lengths', None) is None:
        _check_changed_events(tester, link.handle, before, after)
    for _ in range(POST_INSTANT_EVENTS):
        await stream.event()
    if getattr(stream, 'lengths', None) is not None:
        stream.lengths.check(final=True)
    assert not any(
        isinstance(
            event,
            (hci.HCI_LE_PHY_Update_Complete_Event, hci.HCI_LE_Data_Length_Change_Event),
        )
        for event in tester.events
    ), 'duplicate change event'


async def _mandatory_coded_minimum(tester, *, central):
    """Keep 27-octet DATA flowing when Coded raises the mandatory time minimum."""
    link = await establish(
        tester,
        central=central,
        interval=48,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    buffer = await tester.command(hci.HCI_LE_Read_Buffer_Size_Command())
    stream = _AclStream(
        tester,
        link,
        MINIMUM,
        MINIMUM,
        PDU_DATA_PAYLOAD_SIZE_MIN,
        buffer.total_num_le_acl_data_packets,
    )
    stream.coded_dle_inconclusive = True
    stream.require_initial_full_size = False
    await tester.command(hci.HCI_LE_Set_PHY_Command(link.handle, 3, 0, 0, 0))
    request = await stream.control(Opcode.LL_PHY_REQ, queue=False, required=False)
    if request is not None:
        assert len(request) == PDU_CONTROL_PHY_REQ_SIZE and request[1] and request[2]
        if central:
            update = await stream.control(
                Opcode.LL_PHY_UPDATE_IND,
                bytes([Opcode.LL_PHY_RSP, _bit(Phy.LE_1M), _bit(Phy.LE_1M)]),
                queue=False,
            )
            assert update == _update_pdu(link, 0, 0)
        else:
            await stream.event(_update_pdu(link, 0, 0), queue=False)
    event = await tester.event(hci.HCI_LE_PHY_Update_Complete_Event)
    assert (event.status, event.connection_handle, event.tx_phy, event.rx_phy) == (
        ErrorCode.SUCCESS,
        link.handle,
        Phy.LE_1M,
        Phy.LE_1M,
    )
    request = stream.take(Opcode.LL_LENGTH_REQ)
    if request is not None:
        supported = await _supported_lengths(tester)
        dut = _check_parameters(request, Opcode.LL_LENGTH_REQ, supported)
        await stream.event(
            bytes([Opcode.LL_LENGTH_RSP]) + LENGTH_FIELDS.pack(*MINIMUM), queue=False
        )
        _check_changed_events(
            tester, link.handle, MINIMUM, _effective_lengths(dut, MINIMUM, link)
        )
    await stream.event()
    stream.trigger = Opcode.LL_PHY_UPDATE_IND if central else Opcode.LL_PHY_REQ
    for target in (Phy.LE_CODED, Phy.LE_1M):
        # Core Vol 6, Part B, 5.1.10: the peer Peripheral requests a change,
        # but only the Central sends the indication that applies it.
        update = await _start_update(stream, target, local=not central, fallback=True)
        await _complete_update(stream, update, target)
    await stream.finish()
    await link.disconnect(local=True)


async def _negotiate_traffic_lengths(tester, link, octets, time_us):
    supported = await _supported_lengths(tester)
    peer = (octets, time_us) * 2
    await tester.command(
        hci.HCI_LE_Set_Data_Length_Command(link.handle, octets, time_us)
    )
    request = await link.control(Opcode.LL_LENGTH_REQ, required=False)
    if request is not None:
        dut = _check_parameters(request, Opcode.LL_LENGTH_REQ, supported)
        await link.exchange(
            bytes([Opcode.LL_LENGTH_RSP]) + LENGTH_FIELDS.pack(*peer), llid=LlId.CONTROL
        )
    else:
        response = await link.control(
            Opcode.LL_LENGTH_RSP,
            bytes([Opcode.LL_LENGTH_REQ]) + LENGTH_FIELDS.pack(*peer),
        )
        dut = _check_parameters(response, Opcode.LL_LENGTH_RSP, supported)
    if dut[2] < octets or dut[3] < time_us:
        pytest.skip('Inconclusive: IUT did not select the required DLE limits')
    await tester.wait(0)
    current = _effective_lengths(dut, peer, link)
    _check_changed_events(
        tester, link.handle, _effective_lengths(MINIMUM, MINIMUM, link), current
    )
    return (dut, peer)


async def _packet_time_restrictions(
    tester, *, central, local, coded=False, no_change=False
):
    """Observe every DATA before the procedure, before Instant and after the change."""
    link = await establish(
        tester,
        central=central,
        interval=48,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    if local or not central or coded:
        await _prepare_preferences(tester, link)
    if not coded:
        await _change_phy(tester, link, Phy.LE_2M, local=central)
    size, time_us = (
        (CODED_PACKET_OCTETS, CODED_PACKET_TIME_US)
        if coded
        else (UNCODED_PACKET_OCTETS, UNCODED_PACKET_TIME_US)
    )
    dut, peer = await _negotiate_traffic_lengths(tester, link, size, time_us)
    buffer = await tester.command(hci.HCI_LE_Read_Buffer_Size_Command())
    stream = _AclStream(
        tester, link, dut, peer, size, buffer.total_num_le_acl_data_packets
    )
    stream.coded_dle_inconclusive = coded and (not central or local)
    if coded and central and not local:
        supported = await _supported_lengths(tester)
        stream.lengths = _LengthObserver(tester, link, dut, peer, supported)
        link.on_receive = stream.lengths.receive
    for _ in range(CONTROL_OBSERVATION_EVENTS):
        await stream.event()
        if stream.submitted > len(stream.expected):
            break
    else:
        raise AssertionError('no complete DATA observed before the PHY procedure')
    if central and (not local) and (not coded):
        await tester.command(hci.HCI_LE_Set_PHY_Command(link.handle, 3, 0, 0, 0))
        if await stream.control(Opcode.LL_PHY_REQ, required=False) is not None:
            mask = _bit(Phy.LE_2M)
            update = await stream.control(
                Opcode.LL_PHY_UPDATE_IND, bytes([Opcode.LL_PHY_RSP, mask, mask])
            )
            assert update == bytes([Opcode.LL_PHY_UPDATE_IND, 0, 0, 0, 0])
        event = await tester.event(hci.HCI_LE_PHY_Update_Complete_Event)
        assert (event.status, event.connection_handle, event.tx_phy, event.rx_phy) == (
            ErrorCode.SUCCESS,
            link.handle,
            Phy.LE_2M,
            Phy.LE_2M,
        )
    stream.trigger = (
        Opcode.LL_PHY_UPDATE_IND
        if central
        else Opcode.LL_PHY_REQ
        if local
        else Opcode.LL_PHY_RSP
    )
    stream.limit = size - 1
    target = Phy.LE_CODED if coded else Phy.LE_1M
    update = await _start_update(stream, target, local=local, no_change=no_change)
    assert not stream.initial, 'on-air restriction boundary was not observed'
    if no_change:
        assert update == bytes([Opcode.LL_PHY_UPDATE_IND, 0, 0, 0, 0])
        await tester.wait(0)
        if local:
            event = await tester.event(hci.HCI_LE_PHY_Update_Complete_Event)
            assert (
                event.status,
                event.connection_handle,
                event.tx_phy,
                event.rx_phy,
            ) == (ErrorCode.SUCCESS, link.handle, link.rx_phy, link.tx_phy)
        else:
            assert not any(
                isinstance(event, hci.HCI_LE_PHY_Update_Complete_Event)
                for event in tester.events
            )
    else:
        await _complete_update(stream, update, target)
    await stream.finish()
    await link.disconnect(local=True)


async def _start_update(stream, target, *, local, no_change=False, fallback=False):
    tester, link = (stream.tester, stream.link)
    procedure_before_us = tester.radio.now_us
    mask = _bit(target)
    if local:
        await tester.wait(0)
        stream.enqueue()
        await tester.command(hci.HCI_LE_Set_PHY_Command(link.handle, 0, mask, mask, 0))
        request = await stream.control(Opcode.LL_PHY_REQ)
        assert len(request) == PDU_CONTROL_PHY_REQ_SIZE and request[1] and request[2]
        permitted = (
            request[1] if no_change and not link.central else request[1] & request[2]
        )
        if not permitted & mask:
            if not fallback or link.central:
                pytest.skip(
                    'Inconclusive: IUT request does not permit the selected PHY'
                )
            await stream.event(_update_pdu(link, 0, 0))
            event = await tester.event(hci.HCI_LE_PHY_Update_Complete_Event)
            assert (event.status, event.tx_phy, event.rx_phy) == (
                ErrorCode.SUCCESS,
                link.rx_phy,
                link.tx_phy,
            )
            response = await stream.control(
                Opcode.LL_PHY_RSP, bytes([Opcode.LL_PHY_REQ, mask, mask])
            )
            assert (
                len(response) == PDU_CONTROL_PHY_REQ_SIZE
                and response[1]
                and response[2]
            )
            if not response[1] & response[2] & mask:
                pytest.skip(
                    'Inconclusive: remote fallback does not permit selected PHY'
                )
        if link.central:
            return await stream.control(
                Opcode.LL_PHY_UPDATE_IND, bytes([Opcode.LL_PHY_RSP, mask, mask])
            )
    else:
        preferences = mask | (_bit(link.tx_phy) if no_change else 0)
        request = bytes([Opcode.LL_PHY_REQ, preferences, preferences])
        if link.central:
            return await stream.control(Opcode.LL_PHY_UPDATE_IND, request)
        response = await stream.control(Opcode.LL_PHY_RSP, request)
        assert len(response) == PDU_CONTROL_PHY_REQ_SIZE and response[1] and response[2]
        permitted = response[1] if no_change else response[1] & response[2]
        if not permitted & mask:
            pytest.skip('Inconclusive: IUT response does not permit the selected PHY')
    if no_change:
        threshold = (
            CODED_DELAY_OCTETS if target == Phy.LE_CODED else UNCODED_DELAY_OCTETS
        )
        deadline = tester.radio.now_us + LL_RESPONSE_TIMEOUT_US
        while stream.restricted_octets < threshold:
            if tester.radio.now_us >= deadline:
                pytest.skip(
                    'Inconclusive: required DATA total absent before procedure timeout'
                )
            await stream.event(
                response_timeout_after_us=procedure_before_us
                + LL_RESPONSE_TIMEOUT_US
                - HCI_OBSERVATION_STEP_US
            )
        update = _update_pdu(link, 0, 0)
    else:
        update = _update_pdu(link, mask, mask)
    if no_change:
        # The Lower Central sends the update before DATA returned by this exchange.
        stream.restricted = False
    await stream.event(update)
    return update


class _AclStream:
    """Maintain HCI flow control and independently reassemble every observed DATA
    PDU."""

    def __init__(self, tester, link, dut, peer, size, slots):
        self.tester, self.link = (tester, link)
        self.dut, self.peer = (dut, peer)
        self.size, self.slots = (size, slots)
        self.expected = deque()
        self.partial = b''
        self.submitted = self.completed = 0
        self.controls = deque()
        self.trigger = None
        self.initial = True
        self.require_initial_full_size = True
        self.coded_dle_inconclusive = False
        self.restricted = False
        self.limit = size
        self.restricted_octets = 0
        self.lengths = None

    def enqueue(self):
        if self.submitted - self.completed >= self.slots:
            return
        data = bytes((self.submitted + index) % 256 for index in range(self.size))
        self.expected.append(data)
        self.submitted += 1
        self.tester.send(
            bytes(
                hci.HCI_AclDataPacket(
                    self.link.handle,
                    hci.HCI_ACL_PB_FIRST_NON_FLUSHABLE,
                    0,
                    len(data),
                    data,
                )
            )
        )

    async def event(self, data=b'', *, queue=True, response_timeout_after_us=None):
        await self.tester.wait(0)
        if queue:
            self.enqueue()
        for packet in await _connection_event(
            self.link,
            data,
            llid=LlId.CONTROL if data else LlId.CONTINUATION,
            response_timeout_after_us=response_timeout_after_us,
        ):
            llid = packet.pdu[0] & PDU_DATA_LLID_MASK
            payload = packet.pdu[PDU_HEADER_SIZE:]
            if llid == LlId.CONTROL:
                if (
                    payload[0] == Opcode.LL_LENGTH_REQ
                    and self.link.rx_phy == Phy.LE_CODED
                ):
                    _check_parameters(payload, Opcode.LL_LENGTH_REQ, MAXIMUM)
                    if self.coded_dle_inconclusive:
                        pytest.skip(
                            'Inconclusive: autonomous DLE after Coded PHY Instant'
                        )
                    assert self.lengths is not None, 'unhandled autonomous DLE'
                    continue
                self.controls.append(payload)
                if payload[0] == self.trigger:
                    self.restricted = True
                    self.initial = False
                continue
            if not payload:
                continue
            limits = (
                self.lengths.packet_limits.pop(packet.start_us)
                if self.lengths is not None
                else _effective_lengths(self.dut, self.peer, self.link)
            )
            assert len(payload) <= limits[0]
            assert packet.end_us - packet.start_us <= limits[1]
            if self.restricted:
                assert len(payload) <= self.limit, (
                    'DATA exceeds post-procedure packet limit'
                )
                self.restricted_octets += len(payload)
            elif (
                self.initial
                and self.require_initial_full_size
                and (len(payload) != self.size)
            ):
                pytest.skip(
                    'Inconclusive: DATA was already fragmented before PHY procedure'
                )
            assert self.expected, 'unsolicited DATA without a queued Host packet'
            assert llid == (LlId.CONTINUATION if self.partial else LlId.START)
            self.partial += payload
            assert self.expected[0].startswith(self.partial), (
                'ACL bytes/boundaries changed'
            )
            if self.partial == self.expected[0]:
                self.expected.popleft()
                self.partial = b''
        if self.lengths is not None:
            self.dut = self.lengths.dut
            self.lengths.check()
        while event := self.tester.take(
            lambda event: isinstance(event, hci.HCI_Number_Of_Completed_Packets_Event)
        ):
            assert event.connection_handles == [self.link.handle]
            self.completed += sum(event.num_completed_packets)
            assert self.completed <= self.submitted - len(self.expected)

    async def control(self, opcode, data=b'', *, queue=True, required=True):
        for index in range(CONTROL_OBSERVATION_EVENTS):
            if payload := self.take(opcode):
                return payload
            await self.event(data if index == 0 else b'', queue=queue)
        if payload := self.take(opcode):
            return payload
        if required:
            raise AssertionError(f'missing {opcode.name} during ACL stream')
        return None

    def take(self, opcode):
        for payload in self.controls:
            if payload[0] == opcode:
                self.controls.remove(payload)
                return payload
        return None

    async def finish(self):
        for _ in range(CONTROL_OBSERVATION_EVENTS):
            if self.completed == self.submitted:
                break
            await self.event(queue=False)
        assert not self.expected and (not self.partial)
        assert self.completed == self.submitted
        if self.lengths is not None:
            self.lengths.check(final=True)


async def initiating_phy_update_procedure(tester):
    """Initiating PHY Update Procedure.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common interval/channel parameters, selected timeout and SCA. Select
        public peers and initial LE 1M in both directions. The current profile
        supports LE 2M, LE Coded and asymmetric connections; read local features
        to confirm applicability of the full Table 4.3-41 matrix.

    Test Steps:
        Execute all 67 Table 4.3-41 rounds in order, then every round again in
        deterministic seeded random order, carrying the resulting PHY state.
        1. Send HCI LE Set PHY with that row's ALL_PHYS, TX_PHYS and RX_PHYS,
           and PHY_options=0x0000.
        2. Require Command Status: 0x11 if a requested bit denotes an unsupported
           PHY, otherwise success. For a symmetric-only IUT, ALL_PHYS=0 and
           different TX/RX sets also require 0x11; this profile is asymmetric.
        3. If the IUT does not initiate a PHY procedure, skip to Step 9 for an
           accepted command, or to the next row for a rejected command.
        4. Otherwise receive LL_PHY_REQ with nonzero supported TX/RX sets.
           Send LL_PHY_UPDATE_IND according to the row's Lower Tester preference:
            a. PHY_C_TO_P = row C_TO_P preference AND request RX_PHYS.
            b. PHY_P_TO_C = row P_TO_C preference AND request TX_PHYS.
            c. If request TX_PHYS/RX_PHYS contain the same single PHY, select
               that PHY in both directions or no change, overriding a/b.
            d. Replace a selected PHY equal to its current directional PHY
               with zero, indicating no change in that direction.
            e. For a nonzero change, choose Instant more than six and fewer
               than 32767 events ahead, modulo 65536; otherwise use Instant=0.
        5. Require acknowledgement of LL_PHY_UPDATE_IND from the Peripheral.
        6. Continue empty DATA with old PHYs and independent channel/ACK state
           until the event counter reaches the Instant.
        7. At the Instant, maintain DATA using each selected directional PHY;
           preserve the old PHY for any zero update field.
        8. Exchange further correctly acknowledged empty DATA using those PHYs.
        9. If the HCI command was accepted or either update PHY field was nonzero,
           require successful PHY Update Complete with the same handle and exact
           directional TX/RX values. For no change, report prior values. Otherwise
           prohibit PHY Update Complete.

    Expected Outcome:
        Pass verdict:
            Every emitted request has nonzero TX/RX fields; symmetric-only
            requests, where applicable, select one identical PHY. Updates are
            acknowledged, applied at the Instant and maintained thereafter.
            HCI notification eligibility and directional values match Step 9.
        Fail verdict:
            A command requesting an unsupported PHY bit is accepted.
        Inconclusive verdict:
            No PHY Update Procedure is initiated during the complete test.

    Coverage Note:
        The full table includes twelve RFU/unsupported-bit HCI rows. Reduced-PHY
        and symmetric-only profiles are not substituted for the selected profile.
        Validate every source table tuple, independent directional selection,
        no-change handling, no early successful HCI completion and no duplicate
        completion. Disconnect after both ordered and random passes.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-40-C procedure in
        Section 4.3.5, Table 4.3-41, Figure 4.3-76;
        PDF 639-643, printed 4.3-166 through 4.3-170.
    """
    await _phy_update(tester, central=False, local=True)


# Verify Peripheral-initiated PHY updates across all supported PHYs, including
# asymmetric directions, and continued DATA on the selected PHYs.
PROCEDURES |= procedure_cases(
    initiating_phy_update_procedure,
    {
        'LL/CON/PER/BV-40-C': (
            'Initiating PHY Update Procedure',
            {},
        ),
    },
)


async def responding_to_phy_update_procedure(tester):
    """Responding to PHY Update Procedure.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common interval/channel parameters, selected timeout and SCA. Select
        public peers, initial LE 1M in both directions, and the profile supporting
        LE 2M, LE Coded and asymmetric links; use Table 4.3-44.

    Test Steps:
        1. Send HCI LE Set PHY with ALL_PHYS=0x03, expressing no preference,
           and require successful Command Status.
        2. Require successful PHY Update Complete with the handle/current PHYs.
        3. Execute Steps 4-11 for all 49 Table 4.3-44 cases in order, then
           every case again in deterministic seeded random order (2N rounds).
        4. Send LL_PHY_REQ with the row's TX_PHYS and RX_PHYS.
        5. Receive LL_PHY_RSP with at least one supported PHY bit in each field.
        6. Send LL_PHY_UPDATE_IND using these directional selection rules:
            a. Intersect request TX_PHYS and response RX_PHYS for C_TO_P.
               Use zero/single bit directly; for several bits, choose the first
               available bit in the row's ordered C_TO_P preference list.
            b. Intersect request RX_PHYS and response TX_PHYS for P_TO_C.
               Resolve multiple bits using the ordered P_TO_C preference list.
            c. If both IUT response fields specify the same single PHY, use it
               symmetrically if permitted by the request, otherwise no change;
               this overrides a/b.
            d. Encode zero for a directional PHY equal to the current PHY.
            e. For a nonzero change, select Instant more than six and fewer
               than 32767 events ahead, modulo 65536; otherwise set Instant=0.
        7. Require acknowledgement of LL_PHY_UPDATE_IND. If both selected PHY
           fields are zero, proceed directly to Step 11.
        8. Continue correctly acknowledged empty DATA on old PHYs until Instant.
        9. At Instant, maintain the link using the selected new directional PHYs.
        10. Exchange further empty DATA and ACKs on the selected PHYs.
        11. For an actual change, require successful PHY Update Complete with
            exact handle and TX/RX PHYs, retaining old values for zero fields.
            For no change in both directions, prohibit PHY Update Complete.

    Expected Outcome:
        Pass verdict:
            Each request receives a nonzero supported response, each update is
            acknowledged and applied at its Instant, DATA continues on selected
            PHYs, and HCI events occur only for real changes with correct fields.
        Inconclusive verdict:
            No PHY change occurs during the complete test under the selection
            rules in Step 6.

    Coverage Note:
        Check all 49 source tuples, including ordered multi-bit preferences;
        reduced-feature Tables 4.3-42/43 do not apply to this profile. Complete
        any initial no-preference procedure without changing LE 1M, reject early
        or duplicate completion, and disconnect after both passes. For no change,
        perform two additional DATA exchanges solely to observe forbidden HCI
        events; these do not introduce an Instant or change either PHY.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-42-C procedure in
        Section 4.3.5, Tables 4.3-42/43/44, Figure 4.3-77;
        PDF 643-648, printed 4.3-170 through 4.3-175.
    """
    await _phy_update(tester, central=False, local=False)


# Verify Peripheral responses to Central PHY updates across supported directional PHY
# combinations, with HCI notification only when appropriate.
PROCEDURES |= procedure_cases(
    responding_to_phy_update_procedure,
    {
        'LL/CON/PER/BV-42-C': (
            'Responding to PHY Update Procedure',
            {},
        ),
    },
)


async def handling_protocol_collision_same_procedure(tester):
    """Handling Protocol Collision – Same Procedure.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common interval/channel parameters, selected timeout and SCA. Select
        public peers, initial LE 1M in both directions, and supported LE 2M
        as the non-1M target. The Lower Tester acts as Central.

    Test Steps:
        1. Send HCI LE Set PHY with ALL_PHYS=0, TX/RX preferring LE 2M and
           PHY_options=0x0000; require successful Command Status.
        2. Receive LL_PHY_REQ; if none is emitted, report Inconclusive. Send a
           Central LL_PHY_REQ preferring LE 2M in both directions, then reject
           the IUT request with LL_REJECT_EXT_IND identifying PHY_REQ/error 0x23.
        3. Allow an optional PHY Update Complete only with error 0x23 and the
           same connection handle, retaining current LE 1M directional values.
        4. Receive LL_PHY_RSP to the preserved Central request, with nonzero
           supported TX/RX fields, after the local request has been rejected.
        5. Send LL_PHY_UPDATE_IND selecting LE 2M in both directions and Instant
           more than six and fewer than 32767 events ahead; require the IUT ACK.
        6. Continue empty DATA and ACKs with old PHYs until the Instant.
        7. At the Instant, maintain the connection on the selected LE 2M PHYs.
        8. Exchange further correctly acknowledged DATA using the new PHYs.
        9. Require successful PHY Update Complete for the handle with both
           directional PHY fields equal to the LE 2M selection in Step 2.

    Expected Outcome:
        Pass verdict:
            The IUT requests the update, reports optional local rejection only
            with error 0x23, and preserves/responds to the Central request after
            LL_REJECT_EXT_IND. It acknowledges and adopts the update at Instant
            and reports the selected PHYs through HCI.
        Inconclusive verdict:
            No LL_PHY_REQ is sent by the IUT in Step 2.

    Coverage Note:
        Prohibit answering/rejecting the Central request before local rejection,
        premature successful HCI completion, and duplicate/unrelated PHY events.
        Verify post-collision ACL payload delivery and then terminate via HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-44-C procedure in
        Section 4.3.5, Figure 4.3-78; PDF 648-650, printed 4.3-175 through 4.3-177.
    """
    await _phy_collision_peripheral_collision(tester, procedure='phy')


# Verify that a Peripheral yields its pending PHY request to an overlapping Central PHY
# request and completes the Central procedure after rejection of its own procedure.
PROCEDURES |= procedure_cases(
    handling_protocol_collision_same_procedure,
    {
        'LL/CON/PER/BV-44-C': (
            'Handling Protocol Collision – Same Procedure',
            {},
        ),
    },
)


async def protocol_timeout_for_phy_update_procedure(tester):
    """Protocol Timeout for PHY Update Procedure.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common interval/channel parameters, selected timeout and SCA. Select
        public peers and initial LE 1M; the Lower Tester acts as Central and
        maintains the ACL to prevent an unrelated supervision timeout.

    Test Steps:
        1. Send HCI LE Set PHY preferring supported LE 2M instead of LE 1M
           in both directions with PHY_options=0x0000; require Command Status.
        2. Receive LL_PHY_REQ with nonzero supported TX/RX fields. If no request
           is sent, report Inconclusive.
        3. Acknowledge LL_PHY_REQ but do not send LL_PHY_UPDATE_IND. Continue
           valid empty DATA and ACKs, preserving the existing LE 1M connection.
        4. Allow optional PHY Update Complete only with a nonzero status and
           the same connection handle; prohibit premature successful completion.
        5. At the procedure response timeout, require successful Disconnection
           Complete with the handle and LL Response Timeout reason (0x22),
           followed by cessation of connection RF traffic.

    Expected Outcome:
        Pass verdict:
            The IUT disconnects with reason 0x22 when the control transaction
            timer expires and stops maintaining the connection. Any optional
            PHY Update Complete has nonzero status.
        Inconclusive verdict:
            The IUT does not initiate LL_PHY_REQ in Step 2.

    Coverage Note:
        Core 6.3 Vol 6, Part B, Section 5.2 starts the response timer on procedure
        initiation and resets it for queued LL Control PDUs, not on first RF TX.
        Bracket the unobservable request-enqueue time between HCI submission and
        the observed first LL_PHY_REQ transmission, not HCI Command Status.
        Require 40-s expiry within that bracket plus 1-ms observation resolution;
        maintain DATA throughout to isolate procedure timeout from supervision.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-45-C procedure in
        Section 4.3.5, Figure 4.3-79; PDF 650-651, printed 4.3-177 through 4.3-178.
    """
    await _phy_timeout(tester, central=False, local=True)


# Verify that a Peripheral terminates its ACL when a locally initiated PHY procedure
# receives no update before the response timer expires.
PROCEDURES |= procedure_cases(
    protocol_timeout_for_phy_update_procedure,
    {
        'LL/CON/PER/BV-45-C': (
            'Protocol Timeout for PHY Update Procedure',
            {},
        ),
    },
)


async def handling_protocol_collision_different_procedure_channel_map(tester):
    """Handling Protocol Collision – Different Procedure – Channel Map.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common interval/channel parameters, selected timeout and SCA. Select
        public peers, initial LE 1M and supported LE 2M as the requested PHY.
        The Lower Tester acts as Central and maintains the ACL handle.

    Test Steps:
        1. Send HCI LE Set PHY with ALL_PHYS=0, TX/RX preferring LE 2M and
           PHY_options=0x0000; require successful Command Status.
        2. Receive LL_PHY_REQ. If no request is sent, report Inconclusive.
        3. Send LL_CHANNEL_MAP_IND with a valid new map and future Instant,
           then LL_REJECT_EXT_IND naming PHY_REQ with error 0x2A.
           Require the IUT to acknowledge both control PDUs.
        4. Require PHY Update Complete with the same handle and status 0x2A,
           retaining the existing LE 1M values in both directions.
        5. Continue empty DATA and ACKs on the old map until Instant, then
           maintain the connection on the accepted new map without a PHY change.

    Expected Outcome:
        Pass verdict:
            The IUT sends its PHY request, reports the correct collision error
            0x2A, and correctly handles the Central's channel-map update.

    Coverage Note:
        Independently compute old/new hopping over a valid even-channel map.
        Read back the map through HCI, verify continued ACL payload transfer,
        prohibit extra PHY completion and terminate the original link via HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-46-C procedure in
        Section 4.3.5, Figure 4.3-80; PDF 651-652, printed 4.3-178 through 4.3-179.
    """
    await _phy_collision_peripheral_collision(tester, procedure='map')


# Verify that a Peripheral preserves a Central channel-map update during its local PHY
# request and reports the correct different-procedure error.
PROCEDURES |= procedure_cases(
    handling_protocol_collision_different_procedure_channel_map,
    {
        'LL/CON/PER/BV-46-C': (
            'Handling Protocol Collision – Different Procedure – Channel Map',
            {},
        ),
    },
)


async def handling_protocol_collision_different_procedure_connection_parameters(tester):
    """Handling Protocol Collision – Different Procedure – Connection Parameters.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common interval/channel parameters, selected timeout and SCA. Select
        public peers and initial LE 1M in both directions; the Lower Tester
        maintains the Central role and existing connection handle.

    Test Steps:
        1. Send HCI LE Set PHY with ALL_PHYS=0, both directions preferring
           supported LE 2M and PHY_options=0x0000; require Command Status.
        2. Receive LL_PHY_REQ. If none is sent, report Inconclusive.
        3. Send a Central LL_CONNECTION_PARAM_REQ, then LL_REJECT_EXT_IND
           identifying the IUT PHY_REQ and error 0x2A; require both ACKs.
        4. Require the IUT Host approval request for the Central's connection
           parameters, checking the handle and all requested parameter fields.
        5. Require PHY Update Complete with status 0x2A, the existing handle,
           and unchanged directional LE 1M values.
        6. Accept the Central parameter request through HCI Remote Connection
           Parameter Request Reply; require success and the same handle.
        7. Receive LL_CONNECTION_PARAM_RSP and validate its actual parameters.
        8. Send a valid LL_CONNECTION_UPDATE_IND based on that response and
           require an acknowledgement from the IUT.
        9. Continue empty DATA and ACKs with old parameters until the Instant.
        10. Starting at the Instant, maintain DATA using the new parameters.
        11. Require successful Connection Update Complete with the handle and
            exact accepted interval, latency and supervision timeout.

    Expected Outcome:
        Pass verdict:
            The IUT initiates PHY update and reports its collision failure 0x2A,
            preserves/accepts the Central parameter request, acknowledges the
            update, adopts it at Instant and reports the new HCI parameters.
            The original ACL remains usable after the update.
        Inconclusive verdict:
            No LL_PHY_REQ is sent by the IUT in Step 2.

    Coverage Note:
        Select minimum interval, zero latency and 3-s timeout in the Central
        request. Require both Host notifications before approving the request,
        reject early successful update or extra PHY events, verify post-update
        ACL payload delivery and then terminate through HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-47-C procedure in
        Section 4.3.5, Figure 4.3-81; PDF 653-655, printed 4.3-180 through 4.3-182.
    """
    await _phy_collision_peripheral_collision(tester, procedure='parameters')


# Verify that a Peripheral preserves a Central connection parameter request while its
# own pending PHY update is rejected because of the collision.
PROCEDURES |= procedure_cases(
    handling_protocol_collision_different_procedure_connection_parameters,
    {
        'LL/CON/PER/BV-47-C': (
            (
                'Handling Protocol Collision – Different Procedure – '
                'Connection Parameters'
            ),
            {},
        ),
    },
)


async def handling_protocol_collision_different_procedure_connection_update(tester):
    """Handling Protocol Collision – Different Procedure – Connection Update.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common interval/channel parameters, selected timeout and SCA. Select
        public peers and initial LE 1M in both directions; the Lower Tester
        maintains the Central role and existing connection handle.

    Test Steps:
        1. Send HCI LE Set PHY with ALL_PHYS=0, both directions preferring
           supported LE 2M and PHY_options=0x0000; require Command Status.
        2. Receive LL_PHY_REQ. If none is sent, report Inconclusive.
        3. Send a valid Central LL_CONNECTION_UPDATE_IND with a future Instant,
           then LL_REJECT_EXT_IND identifying PHY_REQ with error 0x2A.
           Require the IUT to acknowledge both Central control packets.
        4. Require PHY Update Complete with status 0x2A, the existing handle,
           and unchanged directional LE 1M values.
        5. Continue empty DATA and ACKs on old parameters until the Instant.
        6. Maintain DATA with the new parameters starting at the Instant.
        7. Require successful Connection Update Complete with exact accepted
           interval, latency, supervision timeout and the original handle.

    Expected Outcome:
        Pass verdict:
            The IUT sends its PHY request and reports collision error 0x2A,
            acknowledges/adopts the Central connection update at the assigned
            event, reports the new parameters through HCI and maintains the ACL.
        Inconclusive verdict:
            No LL_PHY_REQ is sent by the IUT in Step 2.

    Coverage Note:
        Select minimum interval, zero latency, 3-s supervision timeout and
        an Instant more than six events ahead. Prohibit loss/rejection of the
        Central update and early successful HCI completion, verify continued
        ACL data after the Instant, and terminate through HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-48-C procedure in
        Section 4.3.5, Figure 4.3-82; PDF 655-657, printed 4.3-182 through 4.3-184.
    """
    await _phy_collision_peripheral_collision(tester, procedure='update')


# Verify that a Peripheral preserves a Central connection update while its local PHY
# request is rejected because of a different-procedure collision.
PROCEDURES |= procedure_cases(
    handling_protocol_collision_different_procedure_connection_update,
    {
        'LL/CON/PER/BV-48-C': (
            'Handling Protocol Collision – Different Procedure – Connection Update',
            {},
        ),
    },
)


async def initiating_phy_update_procedure_packet_time_restrictions(tester):
    """Initiating PHY Update Procedure – Packet Time Restrictions.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common channel parameters, selected timeout and SCA. Select public peers
        and a 60-ms common interval. The Lower Tester acts as Central; initially
        both directions use LE 1M, with LE 2M and DLE supported.

    Test Steps:
        1. Send HCI LE Set PHY with ALL_PHYS=0x03; require Command Status.
        2. Require PHY Update Complete indicating LE 1M in both directions.
        3. Send a Central LL_PHY_REQ preferring LE 2M for TX/RX.
        4. Receive LL_PHY_RSP with nonzero fields including LE 2M in both
           directions; otherwise report Inconclusive.
        5. Send LL_PHY_UPDATE_IND selecting LE 2M in both directions with Instant
           more than six and fewer than 32767 events ahead. Complete the procedure
           and require HCI completion reporting LE 2M in both directions.
        6. Send HCI LE Set Data Length with TxTime=0x0148 (328 us), TxOctets=32;
           require successful Command Complete.
        7. Answer an IUT DLE with RxTime=328 us/RxOctets=32, or initiate DLE
           with those values if the IUT does not. Report Inconclusive if the IUT
           chooses TxTime<328 us or TxOctets<32.
        8. Continuously queue 32-octet Host start packets through HCI, respecting
           buffer credits, throughout the remainder of the procedure.
        9. Observe at least one unfragmented 32-octet DATA matching the Host
           payload. Fragmentation at this stage is Inconclusive.
        10. Send HCI LE Set PHY with ALL_PHYS=0 and TX/RX preferring LE 1M.
        11. Receive LL_PHY_REQ permitting LE 1M in both directions, or report
            Inconclusive. Respond with LL_PHY_UPDATE_IND selecting LE 1M and
            Instant more than six and fewer than 32767 events ahead.
        12. From transmission of Step 11 LL_PHY_REQ, require every subsequent
            received DATA payload to be <=31 octets, before and after the update.
        13. Complete the PHY procedure while continuing 32-octet Host start
            packets, correct DATA acknowledgement, and independent reassembly.
        14. At the indicated Instant, maintain the link using LE 1M.
        15. Require PHY Update Complete with the handle and both PHYs=LE 1M.
        16. Continue queueing/receiving DATA for at least ten events after Instant,
            requiring every DATA payload <=31 octets and exact reconstructed data.

    Expected Outcome:
        Pass verdict:
            DATA before LL_PHY_REQ is unfragmented and exactly 32 octets;
            DATA after it is <=31 octets. The IUT changes to LE 1M at Instant.
            Every PDU fits effective receive-octet and receive-time limits.
            For the first two DATA packets after DLE, including retransmissions,
            allow the greater pre/post-DLE limits as described by the source.
        Inconclusive verdict:
            LE 2M or LE 1M cannot be selected at its specified step, DLE values
            are too small, or DATA was fragmented before the initiating request.

    Source Correction:
        Step 3 names ALL_PHYS on LL_PHY_REQ, but that field exists only in the
        HCI command. The wire request carries TX_PHYS/RX_PHYS for LE 2M.
        Steps 8/13 describe Host data using LLID; send HCI start PB=0x00,
        which maps to air LLID=0b10, rather than treating LLID as an HCI field.

    Coverage Note:
        Both previous DLE limits are <= the new 32-octet/328-us limits, so the
        first-two-packet grace does not enlarge the active receive bound.
        Check every PDU's airtime, LLID/SN and bytes, all Host completion credits,
        old PHY until Instant and new PHY thereafter. Drain and disconnect.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-49-C procedure in
        Section 4.3.5, Figures 4.3-83/84; PDF 657-661,
        printed 4.3-184 through 4.3-188.
    """
    await _packet_time_restrictions(
        tester, central=False, local=True, coded=False, no_change=False
    )


# Verify Peripheral packet-time restrictions before, during and after a locally
# initiated transition from LE 2M to LE 1M while Host DATA continues.
PROCEDURES |= procedure_cases(
    initiating_phy_update_procedure_packet_time_restrictions,
    {
        'LL/CON/PER/BV-49-C': (
            'Initiating PHY Update Procedure – Packet Time Restrictions',
            {},
        ),
    },
)


async def responding_to_phy_update_procedure_packet_time_restrictions(tester):
    """Responding to PHY Update Procedure – Packet Time Restrictions.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common channel parameters, selected timeout and SCA. Select public peers
        and a 60-ms common interval. The Lower Tester acts as Central; initially
        both directions use LE 1M, with LE 2M and DLE supported.

    Test Steps:
        1. Send HCI LE Set PHY with ALL_PHYS=0x03; require Command Status.
        2. Require PHY Update Complete indicating LE 1M in both directions.
        3. Send a Central LL_PHY_REQ preferring LE 2M for TX/RX.
        4. Receive LL_PHY_RSP with nonzero fields including LE 2M in both
           directions; otherwise report Inconclusive.
        5. Send LL_PHY_UPDATE_IND selecting LE 2M in both directions with Instant
           more than six and fewer than 32767 events ahead. Complete the procedure
           and require HCI completion reporting LE 2M in both directions.
        6. Send HCI LE Set Data Length with TxTime=0x0148 (328 us), TxOctets=32;
           require successful Command Complete.
        7. Answer an IUT DLE with RxTime=328 us/RxOctets=32, or initiate DLE
           with those values if the IUT does not. Report Inconclusive if the IUT
           chooses TxTime<328 us or TxOctets<32.
        8. Continuously queue 32-octet Host start packets through HCI, respecting
           buffer credits, throughout the remainder of the procedure.
        9. Observe at least one unfragmented 32-octet DATA matching the Host
           payload. Fragmentation at this stage is Inconclusive.
        10. Send LL_PHY_REQ with TX_PHYS=RX_PHYS=0x01 (LE 1M).
        11. Receive LL_PHY_RSP permitting LE 1M in both directions, or report
            Inconclusive. Respond with LL_PHY_UPDATE_IND selecting LE 1M and
            Instant more than six and fewer than 32767 events ahead.
        12. From transmission of Step 11 LL_PHY_RSP, require every subsequent
            received DATA payload to be <=31 octets, before and after the update.
        13. Complete the PHY procedure while continuing 32-octet Host start
            packets, correct DATA acknowledgement, and independent reassembly.
        14. At the indicated Instant, maintain the link using LE 1M.
        15. Require PHY Update Complete with the handle and both PHYs=LE 1M.
        16. Continue queueing/receiving DATA for at least ten events after Instant,
            requiring every DATA payload <=31 octets and exact reconstructed data.

    Expected Outcome:
        Pass verdict:
            DATA before LL_PHY_RSP is unfragmented and exactly 32 octets;
            DATA after it is <=31 octets. The IUT changes to LE 1M at Instant.
            Every PDU fits effective receive-octet and receive-time limits.
            The first two DATA packets after DLE, including retransmissions,
            may use the greater pre/post-DLE limits.
        Inconclusive verdict:
            LE 2M or LE 1M cannot be selected at its specified step, DLE values
            are too small, or DATA was fragmented before the responding PHY PDU.

    Source Correction:
        Step 3 names ALL_PHYS on LL_PHY_REQ, but its wire fields are only
        TX_PHYS/RX_PHYS. Steps 8/13's Host LLID wording maps to HCI start
        PB=0x00, which produces air LLID=0b10.

    Coverage Note:
        Previous DLE limits are <= the new 32-octet/328-us limits, so the
        first-two-packet grace adds no larger bound in this trial. Check every
        PDU's airtime, LLID/SN and bytes, all Host credits, directional PHYs at
        Instant and no duplicate HCI completion; drain and disconnect.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-50-C procedure in
        Section 4.3.5, Figures 4.3-85/86; PDF 661-665,
        printed 4.3-188 through 4.3-192.
    """
    await _packet_time_restrictions(
        tester, central=False, local=False, coded=False, no_change=False
    )


# Verify Peripheral packet-time restrictions before, during and after a
# Central-initiated transition from LE 2M to LE 1M with ongoing Host DATA.
PROCEDURES |= procedure_cases(
    responding_to_phy_update_procedure_packet_time_restrictions,
    {
        'LL/CON/PER/BV-50-C': (
            'Responding to PHY Update Procedure – Packet Time Restrictions',
            {},
        ),
    },
)


async def protocol_timeout_for_phy_update_procedure_no_update_request(tester):
    """Protocol Timeout for PHY Update Procedure – No Update Request.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common interval/channel parameters, selected timeout and SCA. Select
        public peers and initial LE 1M; the Lower Tester maintains the Central
        role and valid DATA so supervision cannot cause the disconnection.

    Test Steps:
        1. Send LL_PHY_REQ with TX_PHYS and RX_PHYS preferring supported LE 2M
           rather than LE 1M.
        2. Receive LL_PHY_RSP with at least one bit set in each field and
           acknowledge it, but do not send LL_PHY_UPDATE_IND.
        3. Allow optional PHY Update Complete only with a nonzero status and
           the same established connection handle.
        4. At the procedure response timeout, require successful Disconnection
           Complete with the handle and LL Response Timeout reason (0x22),
           followed by cessation of connection RF traffic.

    Expected Outcome:
        Pass verdict:
            The IUT responds with nonzero PHY_RSP fields, reports disconnection
            with reason 0x22 when the control transaction timer expires, and
            stops maintaining the link. Any optional PHY completion has nonzero
            status, not a successful or unrelated completion.

    Coverage Note:
        Continue empty DATA/ACKs without the missing update. Bound the internal
        response-enqueue time between Lower Tester submission and observed
        LL_PHY_RSP transmission; check 40-s expiry within that bracket plus
        1-ms observation resolution, then verify RF cessation.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-51-C procedure in
        Section 4.3.5, Figure 4.3-87; PDF 665-666, printed 4.3-192 through 4.3-193.
    """
    await _phy_timeout(tester, central=False, local=False)


# Verify that a Peripheral terminates the ACL when a Central-initiated PHY procedure is
# left incomplete until the response timer expires.
PROCEDURES |= procedure_cases(
    protocol_timeout_for_phy_update_procedure_no_update_request,
    {
        'LL/CON/PER/BV-51-C': (
            'Protocol Timeout for PHY Update Procedure – No Update Request',
            {},
        ),
    },
)


async def initiating_phy_update_procedure_packet_time_restrictions_no_change(tester):
    """Initiating PHY Update Procedure – Packet Time Restrictions, No Change.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common channel parameters, selected timeout and SCA. Select public peers,
        a 60-ms common interval, initial LE 1M and support for LE 2M/DLE.
        The Lower Tester acts as Central and maintains the ACL handle.

    Test Steps:
        1. Send HCI LE Set PHY with ALL_PHYS=0x03; require Command Status.
        2. Require PHY Update Complete reporting LE 1M in both directions.
        3. Send Central LL_PHY_REQ preferring LE 2M in both TX/RX fields.
        4. Receive nonzero LL_PHY_RSP fields permitting LE 2M both ways,
           otherwise report Inconclusive.
        5. Send a bidirectional LE 2M update with Instant more than six and
           fewer than 32767 events ahead, complete it, and require HCI LE 2M.
        6. Send HCI LE Set Data Length with TxOctets=32 and TxTime=328 us.
        7. Answer an IUT DLE or initiate peer DLE if absent, with RxOctets=32
           and RxTime=328 us. Smaller IUT TX values are Inconclusive.
        8. Continuously queue 32-octet Host start packets through HCI, respecting
           buffer credits throughout the rest of the procedure.
        9. Receive at least one matching unfragmented 32-octet DATA packet;
           pre-procedure fragmentation is Inconclusive.
        10. Send HCI LE Set PHY with ALL_PHYS=0 and TX/RX preferring LE 1M.
        11. Receive LL_PHY_REQ whose TX_PHYS includes LE 1M; otherwise report
            Inconclusive. Delay the update until at least 140 DATA octets have
            been received; insufficient DATA before response timeout is
            Inconclusive. RX_PHYS need not contain LE 1M for this no-change case.
        12. Require every DATA payload after that LL_PHY_REQ and before the
            Lower Tester's update to contain <=31 octets; a larger packet fails.
        13. Send LL_PHY_UPDATE_IND with both PHY fields and Instant all zero,
            selecting no change in either direction.
        14. Require successful PHY Update Complete with the same handle and
            both directional PHYs still LE 2M.

    Expected Outcome:
        Pass verdict:
            DATA before LL_PHY_REQ is unfragmented/exactly 32 octets; DATA after
            it is <=31 octets until Step 13's update is transmitted. Every DATA
            fits effective receive-octet/time bounds. For the first two packets
            after DLE, including retransmissions, allow the greater old/new limits.
        Inconclusive verdict:
            Initial LE 2M selection is unavailable, DLE TX values are too small,
            pre-request DATA is fragmented, request TX_PHYS lacks LE 1M, or fewer
            than 140 DATA octets arrive before the response timeout.

    Source Correction:
        Step 3's ALL_PHYS is not a wire LL_PHY_REQ field; use TX_PHYS/RX_PHYS.
        Host start data in Step 8 uses HCI PB=0x00, mapped to air LLID=0b10.

    Coverage Note:
        Apply the 31-octet limit from observed PHY_REQ until transmitting the
        no-change update, not until its HCI notification. The greater DLE bounds
        equal the new 32/328 limits in this trial. Distinguish valid procedure
        timeout from early/wrong-handle/wrong-reason failures when classifying
        missing DATA. Check ACKs, fragment bytes and all Host credits; disconnect.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-52-C procedure in
        Section 4.3.5, Figures 4.3-88/89; PDF 666-669,
        printed 4.3-193 through 4.3-196.
    """
    await _packet_time_restrictions(
        tester, central=False, local=True, coded=False, no_change=True
    )


# Verify Peripheral packet-time restrictions during a locally initiated PHY procedure
# when the Central ultimately selects no PHY change.
PROCEDURES |= procedure_cases(
    initiating_phy_update_procedure_packet_time_restrictions_no_change,
    {
        'LL/CON/PER/BV-52-C': (
            'Initiating PHY Update Procedure – Packet Time Restrictions, No Change',
            {},
        ),
    },
)


async def responding_to_phy_update_procedure_packet_time_restrictions_no_change(tester):
    """Responding to PHY Update Procedure – Packet Time Restrictions, No Change.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common channel parameters, selected timeout and SCA. Select public peers,
        a 60-ms common interval, initial LE 1M and support for LE 2M/DLE.
        The Lower Tester acts as Central and maintains the ACL handle.

    Test Steps:
        1. Send HCI LE Set PHY with ALL_PHYS=0x03; require Command Status.
        2. Require PHY Update Complete reporting LE 1M in both directions.
        3. Send Central LL_PHY_REQ preferring LE 2M in both TX/RX fields.
        4. Receive nonzero LL_PHY_RSP fields permitting LE 2M both ways,
           otherwise report Inconclusive.
        5. Send a bidirectional LE 2M update with Instant more than six and
           fewer than 32767 events ahead, complete it, and require HCI LE 2M.
        6. Send HCI LE Set Data Length with TxOctets=32/TxTime=328 us;
           require successful Command Complete.
        7. Answer an IUT DLE or initiate peer DLE if absent, with RxOctets=32
           and RxTime=328 us. Smaller IUT TX values are Inconclusive.
        8. Continuously queue 32-octet Host start packets through HCI, respecting
           buffer credits throughout the rest of the procedure.
        9. Receive at least one matching unfragmented 32-octet DATA packet;
           pre-procedure fragmentation is Inconclusive.
        10. Send LL_PHY_REQ with TX_PHYS=RX_PHYS=0x03 (LE 1M and LE 2M).
        11. Receive LL_PHY_RSP with nonzero TX/RX fields and TX_PHYS containing
            LE 1M, otherwise report Inconclusive. Delay the update until at least
            140 DATA octets have arrived; insufficient DATA before response
            timeout is Inconclusive. RX_PHYS need not include LE 1M.
        12. Require every DATA payload after that LL_PHY_RSP and before the
            Lower Tester's update to contain <=31 octets; a larger packet fails.
        13. Send LL_PHY_UPDATE_IND with both PHY fields and Instant all zero,
            selecting no change in either direction.

    Expected Outcome:
        Pass verdict:
            DATA before LL_PHY_RSP is unfragmented/exactly 32 octets; DATA after
            it is <=31 octets until Step 13. Every DATA fits effective receive
            octet/time bounds. For the first two packets after DLE, including
            retransmissions, allow the greater old/new limits.
        Inconclusive verdict:
            Initial LE 2M selection is unavailable, DLE TX values are too small,
            pre-response DATA is fragmented, response TX_PHYS lacks LE 1M, or
            fewer than 140 DATA octets arrive before the response timeout.

    Source Correction:
        Step 3's ALL_PHYS is not a wire LL_PHY_REQ field; use TX_PHYS/RX_PHYS.
        Host start data in Step 8 uses HCI PB=0x00, mapped to air LLID=0b10.

    Coverage Note:
        End the 31-octet restriction before the update exchange. No actual PHY
        change occurs and no HCI PHY Update Complete is eligible for this remote
        no-change procedure. Verify reassembly/ACK/credits and then disconnect.
        Greater DLE grace bounds equal the new 32/328 limits in this trial;
        valid response-timeout Inconclusive does not mask earlier RF failures.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-53-C procedure in
        Section 4.3.5, Figures 4.3-90/91; PDF 670-673,
        printed 4.3-197 through 4.3-200.
    """
    await _packet_time_restrictions(
        tester, central=False, local=False, coded=False, no_change=True
    )


# Verify Peripheral packet-time restrictions while responding to a Central PHY procedure
# that ultimately leaves both directional PHYs unchanged.
PROCEDURES |= procedure_cases(
    responding_to_phy_update_procedure_packet_time_restrictions_no_change,
    {
        'LL/CON/PER/BV-53-C': (
            (
                'Responding to PHY Update Procedure – Packet Time '
                'Restrictions, No Change'
            ),
            {},
        ),
    },
)


async def initiating_phy_update_procedure_packet_time_restrictions_le_coded(tester):
    """Initiating PHY Update Procedure – Packet Time Restrictions, LE Coded.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common channel parameters, selected timeout and SCA. Select public peers,
        initial LE 1M and a 60-ms common interval; support LE Coded and DLE.
        The Lower Tester acts as Central and maintains the ACL handle.

    Test Steps:
        1. Send HCI LE Set PHY with ALL_PHYS=0x03 and PHY_options=0x0000;
           require successful Command Status.
        2. Require PHY Update Complete with the handle and both PHYs=LE 1M.
        3. Send HCI LE Set Data Length with TxTime=0x04D8 (1240 us) and
           TxOctets=141; require successful Command Complete.
        4. Answer an IUT DLE with RxTime=1240 us/RxOctets=141, or initiate
           peer DLE if the IUT does not. Smaller IUT TX values are Inconclusive.
        5. Continuously queue 141-octet Host start packets through HCI,
           respecting buffer credits throughout the remaining steps.
        6. Receive matching unfragmented 141-octet DATA; fragmentation before
           the initiating PHY request is Inconclusive.
        7. Send HCI LE Set PHY with ALL_PHYS=0, PHY_options=0 and TX/RX
           preferring LE Coded; require successful Command Status.
        8. Receive LL_PHY_REQ permitting Coded in both directions, or report
           Inconclusive. Respond with a Coded/Coded LL_PHY_UPDATE_IND and
           Instant more than six and fewer than 32767 events ahead.
        9. From the IUT's LL_PHY_REQ, require every subsequent DATA payload
           to be <=140 octets, both before and after the PHY update.
        10. Complete the PHY procedure while continuously queueing 141-octet
            Host start packets and receiving correctly acknowledged DATA.
        11. At the indicated Instant, maintain the connection using LE Coded.
        12. Require successful PHY Update Complete with the handle and both
            directional PHYs=LE Coded.
        13. Continue Host queueing and DATA for at least ten events after Instant,
            requiring <=140-octet payloads and exact reconstructed Host bytes.

    Expected Outcome:
        Pass verdict:
            Pre-PHY_REQ DATA is unfragmented/exactly 141 octets; subsequent DATA
            is <=140 octets. The IUT changes PHY at Instant. Every DATA packet
            fits the effective receive-octet/time limits. For the first two packets
            after DLE, including retransmissions, allow the greater old/new limits.
        Inconclusive verdict:
            DLE TX values are below 141/1240, pre-PHY_REQ DATA is fragmented,
            the request cannot select Coded both ways, or the IUT autonomously
            sends LL_LENGTH_REQ after the PHY Instant.

    Source Correction:
        Steps 5/10 describe Host start data as LLID=0b10. Supply HCI start
        PB=0x00, which maps to that air LLID; HCI does not carry an LLID field.

    Coverage Note:
        Require actual full-size DATA before issuing Set PHY. Apply the size
        restriction at observed PHY_REQ, not only at Instant; account for Coded
        minimum effective times and verify each physical packet's airtime, all
        fragments and Host credits. Initial DLE grace does not exceed the newly
        negotiated bounds. Drain the DATA stream and disconnect through HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-55-C procedure in
        Section 4.3.5, Figures 4.3-93/94; PDF 677-681,
        printed 4.3-204 through 4.3-208.
    """
    await _packet_time_restrictions(
        tester, central=False, local=True, coded=True, no_change=False
    )


# Verify Peripheral packet-time restrictions during a locally initiated LE 1M-to-Coded
# transition, including DATA queued before the Instant that could otherwise exceed the
# post-update packet limits on retransmission.
PROCEDURES |= procedure_cases(
    initiating_phy_update_procedure_packet_time_restrictions_le_coded,
    {
        'LL/CON/PER/BV-55-C': (
            'Initiating PHY Update Procedure – Packet Time Restrictions, LE Coded',
            {},
        ),
    },
)


async def responding_to_phy_update_procedure_packet_time_restrictions_le_coded(tester):
    """Responding to PHY Update Procedure – Packet Time Restrictions, LE Coded.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common channel parameters, selected timeout and SCA. Select public peers,
        initial LE 1M and a 60-ms common interval; support LE Coded and DLE.
        The Lower Tester acts as Central and maintains the ACL handle.

    Test Steps:
        1. Send HCI LE Set PHY with ALL_PHYS=0x03 and PHY_options=0x0000;
           require successful Command Status.
        2. Require PHY Update Complete with the handle and both PHYs=LE 1M.
        3. Send HCI LE Set Data Length with TxTime=0x04D8 (1240 us) and
           TxOctets=141; require successful Command Complete.
        4. Answer an IUT DLE with RxTime=1240 us/RxOctets=141, or initiate
           peer DLE if the IUT does not. Smaller IUT TX values are Inconclusive.
        5. Continuously queue 141-octet Host start packets through HCI,
           respecting buffer credits throughout the remaining steps.
        6. Receive matching unfragmented 141-octet DATA. Apply the Source
           Correction below: pre-procedure fragmentation is Inconclusive.
        7. Send LL_PHY_REQ with TX_PHYS=RX_PHYS=0x04 (LE Coded).
        8. Receive LL_PHY_RSP permitting Coded in both directions, or report
           Inconclusive. Respond with a Coded/Coded LL_PHY_UPDATE_IND and
           Instant more than six and fewer than 32767 events ahead.
        9. From the IUT's LL_PHY_RSP, require every subsequent DATA payload
           to be <=140 octets, both before and after the PHY update.
        10. Complete the PHY procedure while continuously queueing 141-octet
            Host start packets and receiving correctly acknowledged DATA.
        11. At the indicated Instant, maintain the connection using LE Coded.
        12. Require successful PHY Update Complete with the handle and both
            directional PHYs=LE Coded.
        13. Continue Host queueing and DATA for at least ten events after Instant,
            requiring <=140-octet payloads and exact reconstructed Host bytes.

    Expected Outcome:
        Pass verdict:
            Pre-PHY_RSP DATA is unfragmented/exactly 141 octets; subsequent DATA
            is <=140 octets. The IUT changes PHY at Instant. Every DATA packet
            fits effective receive-octet/time limits, with the greater old/new
            limits for the first two packets after DLE, including retransmissions.
        Inconclusive verdict:
            DLE TX values are below 141/1240, pre-PHY_RSP DATA is fragmented,
            the response cannot select Coded both ways, or autonomous LL_LENGTH_REQ
            follows the PHY Instant.

    Source Correction:
        Step 6 says initial fragmentation is Fail, but this procedure's explicit
        Inconclusive list names that same condition. Use Inconclusive: Core
        6.3 Vol 6, Part B, Section 4.5.10 constrains maximum DATA size, not a
        mandatory full-size fragment. Already-fragmented DATA cannot exercise
        the intended 141-to-140 boundary. Amber supports this local interpretation;
        no adopted SIG erratum was found. Post-PHY_RSP oversize DATA still fails.
        Steps 5/10's Host LLID wording maps HCI start PB=0x00 to air LLID=0b10.

    Coverage Note:
        Apply the restriction at observed PHY_RSP, not Instant. Verify actual
        initial full-size DATA, per-PDU airtime, independent fragment/byte/credit
        accounting, old/new PHY boundaries and ten post-Instant events. Initial
        DLE grace adds no larger bound here; drain and disconnect through HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-56-C procedure in
        Section 4.3.5, Figures 4.3-95/96; PDF 681-685,
        printed 4.3-208 through 4.3-212.
    """
    await _packet_time_restrictions(
        tester, central=False, local=False, coded=True, no_change=False
    )


# Verify Peripheral packet-time restrictions during a Central-initiated LE 1M-to-Coded
# transition, including pre-Instant queued DATA whose later transmission or
# retransmission could otherwise violate the new limits.
PROCEDURES |= procedure_cases(
    responding_to_phy_update_procedure_packet_time_restrictions_le_coded,
    {
        'LL/CON/PER/BV-56-C': (
            ('Responding to PHY Update Procedure – Packet Time Restrictions, LE Coded'),
            {},
        ),
    },
)


async def mandatory_minimum_pdu_length_le_coded(tester):
    """Mandatory Minimum PDU Length, LE Coded.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common channel parameters, selected timeout and SCA. Select public peers,
        initial LE 1M, a 60-ms interval, zero latency and minimum data lengths.
        The Lower Tester acts as Central and maintains the ACL handle.

    Test Steps:
        1. Send HCI LE Set PHY with ALL_PHYS=0x03 and PHY_options=0x0000;
           require successful Command Status.
        2. If the IUT sends LL_PHY_REQ, respond with LL_PHY_UPDATE_IND selecting
           no PHY change: PHY_C_TO_P=PHY_P_TO_C=0.
        3. Require PHY Update Complete with the handle and both PHYs=LE 1M.
        4. If the IUT autonomously initiates DLE, respond with Lower Tester
           RxOctets=27 and RxTime=328 us; retain minimum values both ways.
        5. Continuously queue 27-octet Host start packets through HCI,
           respecting buffer credits throughout the remaining procedure.
        6. Send HCI LE Set PHY with TX_PHYS=RX_PHYS=0x04, ALL_PHYS=0 and
           PHY_options=0; require successful Command Status.
        7. Receive the IUT's LL_PHY_REQ while maintaining DATA exchange.
        8. Choose the source alternative according to the request's PHY masks:
           8A. If both directions permit LE Coded:
               8A.1 Send LL_PHY_UPDATE_IND selecting Coded/Coded and an Instant
                    more than six and fewer than 32767 events ahead.
           8B. Otherwise:
               8B.1 Send LL_PHY_UPDATE_IND selecting the current PHYs.
               8B.2 Send LL_PHY_REQ with TX_PHYS=RX_PHYS=0x04.
               8B.3 Receive LL_PHY_RSP; if it cannot select LE Coded in both
                    directions, report Inconclusive.
               8B.4 Send LL_PHY_UPDATE_IND selecting Coded/Coded and an Instant
                    more than six and fewer than 32767 events ahead.
        9. Require received DATA payloads to remain at most 27 octets.
        10. Complete the PHY procedure while continuing 27-octet Host start
            packets and correctly sequenced DATA exchange.
        11. At the indicated Instant, maintain the connection using LE Coded.
        12. Require successful PHY Update Complete with both PHYs=LE Coded.
            If DLE is supported, also require Data Length Change with TX/RX
            octets=27 and TX/RX times=2704 us; either HCI event order is allowed.
        13. Continue Host queueing and reception for at least ten connection
            events after Instant, checking every DATA payload is <=27 octets.
        14. Repeat Steps 6-13 for LE 1M: change the requested PHY masks to 0x01,
            select LE 1M at Instant and require effective TX/RX times=328 us.

    Expected Outcome:
        Pass verdict:
            DATA remains <=27 octets and within effective receive-time limits.
            Both PHY changes occur at their Instants and produce the correct
            PHY completion and eligible data-length events. For the first two
            packets after DLE, including retransmissions, allow the greater
            old/new limits. Payloads, fragment boundaries and credits are intact.
        Inconclusive verdict:
            Step 8B.3 cannot select the requested PHY, or the IUT autonomously
            sends LL_LENGTH_REQ after the PHY Instant in Step 8A.1 or 8B.4.

    Coverage Note:
        Preserve optional initial DLE while collecting the PHY exchange; do not
        discard it as unrelated control traffic. The selected IUT supports DLE.
        Drain all queued Host data and disconnect after Step 14.

    Source Correction:
        The outcome references LL_PHY_RSP, which occurs only on fallback 8B;
        normal 8A uses PHY_REQ. Apply the 27-octet bound throughout both paths,
        including earlier DATA. Amber supports this local interpretation under
        Core Vol 6, Part B, Sections 4.5.10 and 5.1.10.1; it is not a
        claimed adopted SIG erratum. Step 5's Host LLID=0b10 is HCI start PB=0x00.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-57-C procedure in
        Section 4.3.5, Figures 4.3-97/98; PDF 685-689,
        printed 4.3-212 through 4.3-216. Steps 1-14 including 8A/8B;
        referenced Core Vol 6, Part B, Sections 4.5.10 and 5.1.10.1.
    """
    await _mandatory_coded_minimum(tester, central=False)


# Verify that a Peripheral continues transmitting the mandatory minimum DATA length when
# a PHY transition raises the minimum effective packet time. Maintain traffic during a
# transition to LE Coded and back to LE 1M.
PROCEDURES |= procedure_cases(
    mandatory_minimum_pdu_length_le_coded,
    {
        'LL/CON/PER/BV-57-C': (
            'Mandatory Minimum PDU Length, LE Coded',
            {},
        ),
    },
)


async def initiating_phy_update_procedure_packet_time_restrictions_no_change_le_coded(
    tester,
):
    """Initiating PHY Update Procedure – Packet Time Restrictions, No Change, LE Coded.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common channel parameters, selected timeout and SCA. Select public peers,
        initial LE 1M and a 60-ms interval; support LE Coded and DLE. The Lower
        Tester acts as Central and maintains the ACL handle.

    Test Steps:
        1. Send HCI LE Set PHY with ALL_PHYS=0x03 and PHY_options=0x0000;
           require successful Command Status.
        2. Require PHY Update Complete with the handle and both PHYs=LE 1M.
        3. Send HCI LE Set Data Length with TxTime=0x04D8 (1240 us) and
           TxOctets=141; require successful Command Complete.
        4. Answer an IUT DLE with RxTime=1240 us/RxOctets=141, or initiate
           peer DLE if the IUT does not. Smaller IUT TX values are Inconclusive.
        5. Continuously queue 141-octet Host start packets through HCI,
           respecting buffer credits throughout the remaining steps.
        6. Receive matching unfragmented 141-octet DATA; initial fragmentation
           is Inconclusive because the intended size boundary cannot be tested.
        7. Send HCI LE Set PHY with ALL_PHYS=0, PHY_options=0 and TX/RX
           preferring LE Coded; require successful Command Status.
        8. Receive LL_PHY_REQ with the Coded bit in TX_PHYS; otherwise report
           Inconclusive. Continue receiving DATA and withhold UPDATE until at
           least 423 DATA payload octets have arrived after PHY_REQ.
        9. From PHY_REQ until the Lower Tester sends UPDATE, require every
           DATA payload to be <=140 octets; oversize DATA fails. Fewer than
           423 octets before procedure response timeout is Inconclusive.
        10. Send LL_PHY_UPDATE_IND selecting no change, with all fields zero.
        11. Require successful PHY Update Complete with the handle and both
            PHYs still LE 1M.

    Expected Outcome:
        Pass verdict:
            Pre-PHY_REQ DATA is unfragmented/exactly 141 octets; pending-update
            DATA is <=140 octets. Every DATA packet fits effective receive-octet
            and receive-time limits, using the greater old/new bounds for the
            first two packets after DLE, including retransmissions. The local
            no-change procedure completes successfully without changing PHY.
        Inconclusive verdict:
            DLE TX values are below 141/1240, initial DATA is fragmented,
            TX_PHYS does not permit Coded, or fewer than 423 DATA octets arrive
            before the procedure response timeout.

    Source Correction:
        Step 5's Host LLID=0b10 means HCI start PB=0x00, which maps to that air
        LLID. HCI ACL Data does not carry an LLID field.

    Coverage Note:
        Require actual full-size DATA before Set PHY. Observe the restriction
        at PHY_REQ, not a nonexistent PHY-change Instant. Count only constrained
        DATA payload octets; release the temporary cap before receiving DATA
        responding to the all-zero UPDATE. Independently reassemble Host bytes,
        check airtime and completion credits, then drain and disconnect.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-58-C procedure in
        Section 4.3.5, Figures 4.3-99/100; PDF 689-692,
        printed 4.3-216 through 4.3-219. Steps 1-11 and Expected Outcome;
        referenced Core Vol 6, Part B, Section 5.1.10.1.
    """
    await _packet_time_restrictions(
        tester, central=False, local=True, coded=True, no_change=True
    )


# Verify Peripheral packet-time restrictions when the Host requests LE Coded but the
# Central selects no PHY change. Previously queued DATA must respect the prospective
# retransmission limit while the procedure is pending, even though the connection
# remains on LE 1M.
PROCEDURES |= procedure_cases(
    initiating_phy_update_procedure_packet_time_restrictions_no_change_le_coded,
    {
        'LL/CON/PER/BV-58-C': (
            (
                'Initiating PHY Update Procedure – Packet Time '
                'Restrictions, No Change, LE Coded'
            ),
            {},
        ),
    },
)


async def responding_to_phy_update_packet_time_limits_no_change_le_coded(
    tester,
):
    """Responding to PHY Update Procedure – Packet Time Restrictions, No Change, LE
    Coded.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common channel parameters, selected timeout and SCA. Select public peers,
        initial LE 1M and a 60-ms interval; support LE Coded and DLE. The Lower
        Tester acts as Central and maintains the ACL handle.

    Test Steps:
        1. Send HCI LE Set PHY with ALL_PHYS=0x03 and PHY_options=0x0000;
           require successful Command Status.
        2. Require PHY Update Complete with the handle and both PHYs=LE 1M.
        3. Send HCI LE Set Data Length with TxTime=0x04D8 (1240 us) and
           TxOctets=141; require successful Command Complete.
        4. Answer an IUT DLE with RxTime=1240 us/RxOctets=141, or initiate
           peer DLE if the IUT does not. Smaller IUT TX values are Inconclusive.
        5. Continuously queue 141-octet Host start packets through HCI,
           respecting buffer credits throughout the remaining steps.
        6. Receive matching unfragmented 141-octet DATA; initial fragmentation
           is Inconclusive because the intended size boundary cannot be tested.
        7. Send LL_PHY_REQ with TX_PHYS=RX_PHYS=0x05 (LE 1M and LE Coded).
        8. Receive LL_PHY_RSP with at least one bit in each PHY field and the
           Coded bit in TX_PHYS; otherwise report Inconclusive for missing Coded
           support. Continue receiving DATA and withhold UPDATE until at least
           423 DATA payload octets have arrived after PHY_RSP.
        9. From PHY_RSP until the Lower Tester sends UPDATE, require every
           DATA payload to be <=140 octets; oversize DATA fails. Fewer than
           423 octets before procedure response timeout is Inconclusive.
        10. Send LL_PHY_UPDATE_IND selecting no change, with all fields zero.

    Expected Outcome:
        Pass verdict:
            Pre-PHY_RSP DATA is unfragmented/exactly 141 octets; pending-update
            DATA is <=140 octets. Every DATA packet fits effective receive-octet
            and receive-time limits, using the greater old/new bounds for the
            first two packets after DLE, including retransmissions.
        Inconclusive verdict:
            DLE TX values are below 141/1240, initial DATA is fragmented,
            TX_PHYS does not permit Coded, or fewer than 423 DATA octets arrive
            before the procedure response timeout.

    Source Correction:
        Step 5's Host LLID=0b10 means HCI start PB=0x00, which maps to that air
        LLID. HCI ACL Data does not carry an LLID field.

    Coverage Note:
        Unlike the locally initiated procedure, no PHY Update Complete event
        is expected for this remote no-change procedure. Observe the size cap
        at PHY_RSP and count only constrained payload octets. Release the cap
        before receiving DATA responding to the all-zero UPDATE. Independently
        reassemble Host bytes, check airtime and credits, drain and disconnect.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-59-C procedure in
        Section 4.3.5, Figures 4.3-101/102; PDF 692-695,
        printed 4.3-219 through 4.3-222. Steps 1-10 and Expected Outcome;
        referenced Core Vol 6, Part B, Section 5.1.10.1.
    """
    await _packet_time_restrictions(
        tester, central=False, local=False, coded=True, no_change=True
    )


# Verify Peripheral packet-time restrictions when a Central requests LE Coded but then
# selects no PHY change. Previously queued DATA must respect the prospective
# retransmission limit while the procedure is pending, even though the connection
# remains on LE 1M.
PROCEDURES |= procedure_cases(
    responding_to_phy_update_packet_time_limits_no_change_le_coded,
    {
        'LL/CON/PER/BV-59-C': (
            (
                'Responding to PHY Update Procedure – Packet Time '
                'Restrictions, No Change, LE Coded'
            ),
            {},
        ),
    },
)


async def responding_to_phy_update_procedure_instant_in_past(tester):
    """Responding to PHY Update Procedure – Instant In Past.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common channel parameters and interval, selected timeout and SCA.
        Select public peers, zero latency and initial LE 1M. The Lower Tester
        acts as Central and selects LE 2M as the non-1M PHY for this vector.

    Test Steps:
        1. Send HCI LE Set PHY with ALL_PHYS=0x03 and require successful
           Command Status and PHY Update Complete, retaining initial LE 1M.
        2. Send LL_PHY_REQ with both TX_PHYS and RX_PHYS preferring a PHY
           other than LE 1M; select LE 2M in this implementation.
        3. Receive LL_PHY_RSP with nonzero TX/RX fields supporting the selected
           non-1M PHY in both directions. Otherwise report Inconclusive.
        4. Send LL_PHY_UPDATE_IND selecting that non-1M PHY both ways, with
           (Instant - connEventCount) modulo 65536 >=32767. Accept either:
           Alternative 1: an optional PHY Update Complete has nonzero status;
           require Disconnection Complete with Instant Passed (0x28) and
           cessation of connection maintenance.
           Alternative 2: require PHY Update Complete with both requested PHYs,
           maintain the connection on those PHYs and report no connection failure.

    Expected Outcome:
        Pass verdict:
            PHY_RSP has a nonzero selection in both fields. The nonzero PHY
            update is handled through one complete Step 4 alternative: failed
            completion if present and disconnection 0x28, or successful non-1M
            completion and a maintained connection without failure.
        Inconclusive verdict:
            Step 3 does not permit a non-1M PHY in both directions.

    Source Correction:
        The outcome's reference to an optional completion in Step 5 points to
        Step 4 Alternative 1; no Step 5 exists. Amber corroborates this local
        editorial interpretation; no adopted SIG erratum is claimed.

    Coverage Note:
        Use distance 32767, the source boundary, for the invalid Instant.
        Accept both alternatives rather than forcing the current IUT's chosen
        disconnection. Check event handles/status, reject successful completion
        on the failure path and duplicate/contradictory notifications. Probe
        the old PHY for one supervision period after disconnection. For the
        maintained path, check ten acknowledged connection events on the new
        PHY and terminate through HCI. Boundary units cover both alternatives;
        the native IUT currently exercises Alternative 1.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BI-09-C procedure in
        Section 4.3.5, Figure 4.3-111; PDF 708-710,
        printed 4.3-235 through 4.3-237. Steps 1-4, both alternatives and
        Expected Outcome; referenced Core Vol 6, Part B, Section 5.1.10.
    """
    await _invalid_update(tester, past=True, rfu=False)


# Verify that a Peripheral handles a Central PHY update with an Instant in the past,
# accepting either the specified failure/disconnection path or the source's alternative
# of switching PHY and maintaining the link.
PROCEDURES |= procedure_cases(
    responding_to_phy_update_procedure_instant_in_past,
    {
        'LL/CON/PER/BI-09-C': (
            'Responding to PHY Update Procedure – Instant In Past',
            {},
        ),
    },
)


async def phy_update_procedure_invalid_or_unsupported_phy_central_initiated(
    tester, *, rfu
):
    """PHY Update Procedure, Invalid or Unsupported PHY, Central Initiated.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common channel parameters and interval, selected timeout and SCA.
        Select public peers, zero latency and initial LE 1M; the Lower Tester
        is Central. Read the declared local feature set to validate any later
        Feature Response. The IUT supports LE 1M, LE 2M and LE Coded.

    Test Case Configuration:
        LL/CON/PER/BI-20-C: multiple PHY bits 0x03.
        LL/CON/PER/BI-21-C: RFU PHY bit 0x80.

    Test Steps:
        1. Send LL_PHY_REQ with TX_PHYS=RX_PHYS=0xFF.
        2. Receive LL_PHY_RSP with nonzero TX/RX masks containing only the
           defined, supported PHY bits.
        3. Send LL_PHY_UPDATE_IND with PHY_C_TO_P/PHY_P_TO_C from the selected
           Table 4.3-49 row, using a valid future Instant. For BI-20 use
           (0x03,0x03), (0x03,0x01), (0x01,0x03); for BI-21 use
           (0x80,0x80), (0x80,0x01), (0x01,0x80).
        4. Follow the IUT's chosen outcome:
           Alternative 4A, disconnection:
               4A.1 Require HCI Disconnection Complete for the active handle.
               4A.2 Reestablish the connection between IUT and Lower Tester.
               4A.3 Require Connection Complete for the new Peripheral ACL.
           Alternative 4B, maintenance on the active PHY:
               4B.1 Require no HCI PHY Update Complete event.
               4B.2 Send LL_FEATURE_REQ on the unchanged active PHY with the
                    Lower Tester feature set; select zero features here.
               4B.3 Receive a valid LL_FEATURE_RSP on that PHY with the
                    permitted IUT feature set.
               4B.4 If PHY_C_TO_P is an unsupported defined PHY and not RFU,
                    transmit on the unsupported PHY and require no response.

        Repeat the complete procedure for all three rows of the selected case.

    Expected Outcome:
        Pass verdict:
            Complete the selected disconnect/reestablish or maintained-link
            alternative. Maintenance produces a valid Feature Response and no
            Host PHY completion. Any applicable unsupported-PHY probe receives
            no response.

    Source Correction:
        The outcome addresses the HCI PHY completion check to the Lower Tester.
        HCI events belong to the Upper Tester, as Step 4B.1 specifies. Amber
        corroborates this local destination correction; no adopted erratum is
        claimed.

    Coverage Note:
        Step 4B.4 is inapplicable to both selected cases: 0x03 is an invalid
        multi-PHY mask, not HCI PHY enumeration 3/Coded, and 0x80 is RFU.
        The unsupported-single-PHY cases BI-18/BI-19 are not selected here.
        Amber corroborates that applicability distinction. Keep each on-air
        control payload unchanged until acknowledged before advancing to the
        Feature Request, and require its acknowledgement before accepting the
        response. Reestablish after every 4A result, including the final row;
        retain a 4B connection for the next row, then disconnect through HCI.
        Boundary units cover both paths, retries and invalid notifications;
        native trials currently exercise 4A. A failed optional PHY notification
        on 4A must have the right handle and may not claim successful change.

    Reference:
        LL.TS.p28, Section 4.3.5.3, Table 4.3-49, Figure 4.3-125;
        LL/CON/PER/BI-20-C and LL/CON/PER/BI-21-C, PDF 732-734,
        printed 4.3-259 through 4.3-261. Steps 1-4, alternatives 4A.1-3/
        4B.1-4 and Expected Outcome; referenced Core Vol 6, Part B, 5.1.10.
    """
    await _invalid_update(tester, past=False, rfu=rfu)


# Verify that a Peripheral handles a Central PHY update containing invalid multiple-PHY
# or RFU selections, either disconnecting or keeping its active PHY without an
# inappropriate Host PHY-change notification.
PROCEDURES |= procedure_cases(
    phy_update_procedure_invalid_or_unsupported_phy_central_initiated,
    {
        'LL/CON/PER/BI-20-C': (
            (
                'PHY Update Procedure, Invalid or Unsupported PHY, Central '
                'Initiated, Multiple PHYs Specified'
            ),
            {'rfu': False},
        ),
        'LL/CON/PER/BI-21-C': (
            (
                'PHY Update Procedure, Invalid or Unsupported PHY, Central '
                'Initiated, RFU Bits Specified'
            ),
            {'rfu': True},
        ),
    },
)


async def initiating_phy_update_procedure_ll_con_cen_bv_41_c(tester):
    """Initiating PHY Update Procedure.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window and connection interval, common
        Peripheral latency and supervision timeout. The connection starts on LE 1M.
        This IUT declares both LE 2M and LE Coded support.

    Test Steps:
        Execute all 32 rounds of Table 4.3-56 in order, then execute every round
        again in seeded random order. Preserve the table's RX_PHYS/TX_PHYS column
        order when constructing the Lower Tester's response.

        1. Send HCI_LE_Set_PHY with the round's ALL_PHYS, TX_PHYS and RX_PHYS,
           and PHY_options=0x0000.
        2. Receive HCI_Command_Status. Require Unsupported Feature or Parameter
           Value (0x11) if any requested PHY bit is unsupported; otherwise require
           success (0x00).
        3. If the IUT does not initiate a PHY change, go to Step 9 for an accepted
           command, or proceed to the next round for a rejected command.
        4. Receive LL_PHY_REQ with at least one supported bit in each of TX_PHYS
           and RX_PHYS. Acknowledge the request and send LL_PHY_RSP using the
           response fields from this round of Table 4.3-56.
        5. Receive LL_PHY_UPDATE_IND. Each directional field must contain zero or
           one PHY bit; nonzero choices must be common to the corresponding
           request and response directions. If both fields are zero, go to Step 8.
        6. Maintain the connection using empty DATA packets on the old PHYs until
           the event counter reaches the indicated Instant.
        7. At the Instant, use the new PHY in each nonzero direction and retain
           the previous PHY in each zero direction. Reject an early PHY change
           or an early successful HCI PHY completion.
        8. Receive and acknowledge the IUT's empty DATA packets on the selected
           directional PHYs.
        9. If Step 2 accepted the command or either UPDATE field is nonzero,
           require LE_PHY_Update_Complete with success, the connection handle
           and the effective directional PHYs. Retain the previous PHY for a
           zero field or an absent UPDATE. Otherwise require no completion.

    Expected Outcome:
        Pass verdict:
            Every selected nonzero PHY belongs to both directional preference
            masks and differs from the previous PHY. A zero directional field
            is allowed when there is no common PHY, the current PHY is common,
            or both UPDATE fields are zero. A change uses a valid future Instant.
            DATA continues on the indicated PHYs, changing only at the Instant.
            HCI completion reports the effective PHYs, including an accepted
            command that changes neither direction. A rejected command without
            a PHY change produces no completion.
        Fail verdict:
            The IUT accepts an unsupported PHY bit or violates any required
            selection, Instant, connection-maintenance or HCI-event condition.
        Inconclusive verdict:
            The IUT initiates no PHY Update procedure or selects no actual PHY
            change over the complete test.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-41-C; Figure 4.3-165, Table 4.3-56;
        PDF pages 799-802, printed pages 4.3-326..329; source reference [10] 5.1.10.
        Core v6.3, Vol 6, Part B, 2.4.2.23: unchanged PHY fields shall be zero;
        Vol 1, Part E, 2.4.1: a sender sets the unused Instant (RFU) to zero.
    """
    await _phy_update(tester, central=True, local=True)


# Verify that a Central can initiate PHY Update, select supported symmetric and
# asymmetric PHYs, and maintain the connection on the selected PHYs. The Upper Tester
# issues HCI commands; the Peripheral Lower Tester observes and answers the IUT's Link
# Layer procedure.
PROCEDURES |= procedure_cases(
    initiating_phy_update_procedure_ll_con_cen_bv_41_c,
    {
        'LL/CON/CEN/BV-41-C': (
            'Initiating PHY Update Procedure',
            {},
        ),
    },
)


async def responding_to_phy_update_procedure_ll_con_cen_bv_43_c(tester):
    """Responding to PHY Update Procedure.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window and connection interval, common
        Peripheral latency and supervision timeout. The connection starts on LE 1M.
        This IUT declares both LE 2M and LE Coded, selecting Table 4.3-59 rather
        than the reduced-feature Tables 4.3-57 or 4.3-58.

    Test Steps:
        1. Send HCI_LE_Set_PHY with ALL_PHYS=0x03 and require successful
           HCI_Command_Status. If the IUT sends LL_PHY_REQ, answer with
           LL_PHY_RSP specifying the current PHY in both directions and receive
           the IUT's no-change LL_PHY_UPDATE_IND. With or without this optional
           exchange, require HCI_LE_PHY_Update_Complete reporting LE 1M/LE 1M.
        2. Perform Steps 3-9 for every table case in order, then every case again
           in seeded random order: 2N iterations, where N=49 for Table 4.3-59.
        3. Send LL_PHY_REQ with this table case's TX_PHYS and RX_PHYS.
        4. Receive LL_PHY_UPDATE_IND, not the Peripheral-only LL_PHY_RSP. Each
           directional choice is zero or one bit permitted by the request.
           Require a valid future Instant if either field is nonzero.
        5. Maintain the connection using empty DATA packets on the old PHYs
           until the event counter reaches the Instant.
        6. At the Instant, use the PHYs selected by LL_PHY_UPDATE_IND; a zero
           field retains that direction's current PHY.
        7. Starting at the Instant, maintain the connection on the selected PHYs.
        8. Receive and acknowledge the IUT's empty DATA packets on those PHYs.
        9. If a PHY changed, require LE_PHY_Update_Complete with success, the
           connection handle and the selected effective PHYs. If neither PHY
           changed, require no completion.

    Test Condition:
        Before Step 3 or after the Instant, the IUT may initiate an autonomous
        LL_PHY_REQ. Answer with current-PHY-only LL_PHY_RSP and require an
        LL_PHY_UPDATE_IND with both directional fields zero.
        If the IUT sends LL_PHY_REQ simultaneously with Step 3, require its
        LL_REJECT_EXT_IND for the Lower Tester's LL_PHY_REQ with procedure
        collision (0x23). Answer the retained IUT request with current-PHY-only
        LL_PHY_RSP, require a no-change UPDATE, and repeat Step 3.
        An unsolicited IUT LL_PHY_REQ after Step 3 but before the Instant fails.
        No-change autonomous procedures must not produce Host PHY completions.

    Expected Outcome:
        Pass verdict:
            Each nonzero UPDATE field selects one requested PHY and encodes an
            actual directional change. If the request specifies the same single
            PHY in both fields, select that PHY in both effective directions
            or leave both directions unchanged; see Source Correction below.
            Nonzero fields have a valid Instant. DATA remains on the selected
            PHYs, with no change before the Instant. HCI completion reports
            effective PHYs only when a PHY changed.
        Fail verdict:
            The IUT sends an unsolicited LL_PHY_REQ after Step 3 and before the
            Instant, or violates the required selection, collision, timing,
            DATA-maintenance or HCI-event behavior.
        Inconclusive verdict:
            No PHY Update procedure is initiated over the complete test.
            The source does not require an actual PHY change for this verdict.

    Source Correction:
        The pass text literally requires identical nonzero UPDATE fields for a
        symmetric single-PHY request. Core v6.3, Vol 6, Part B, 2.4.2.23 requires
        zero for any unchanged direction; 5.1.10 requires symmetric effective
        PHY selection, not redundant nonzero encoding. From LE 1M/LE Coded,
        selecting LE Coded/LE Coded therefore uses UPDATE 0x04/0x00, not
        0x04/0x04. Compare effective PHYs while enforcing unchanged-field zero.
        Amber corroborated this interpretation against both Core clauses.
        This is a local source correction, not an adopted SIG erratum.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-43-C; Figure 4.3-166, Tables 4.3-57..59;
        PDF pages 803-808, printed pages 4.3-330..335; source reference [10] 5.1.10.
        Core v6.3, Vol 6, Part B, 2.4.2.23 and 5.1.10; Vol 1, Part E, 2.4.1:
        a sender sets the unused Instant (RFU) to zero.
    """
    await _phy_update(tester, central=True, local=False)


# Verify that a Central can respond to Peripheral-initiated PHY Update, use supported
# symmetric and asymmetric PHYs, and maintain the connection on the selected PHYs.
PROCEDURES |= procedure_cases(
    responding_to_phy_update_procedure_ll_con_cen_bv_43_c,
    {
        'LL/CON/CEN/BV-43-C': (
            'Responding to PHY Update Procedure',
            {},
        ),
    },
)


async def handling_protocol_collision_same_procedure_ll_con_cen_bv_45_c(tester):
    """Handling Protocol Collision – Same Procedure.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window and connection interval, common
        Peripheral latency and supervision timeout. The connection starts on LE 1M.

    Test Steps:
        1. Send HCI_LE_Set_PHY with ALL_PHYS=0, TX_PHYS=RX_PHYS preferring a PHY
           other than LE 1M, and PHY_options=0x0000. Receive HCI_Command_Status.
           This procedure chooses LE 2M.
        2. Receive LL_PHY_REQ with at least one supported PHY bit in each field.
           If the IUT sends no request, end with Inconclusive.
        3. Respond with the Lower Tester's own LL_PHY_REQ, preferring a non-1M
           PHY in both directions. Inject this request in the immediate
           acknowledgement of the IUT request, before completing its procedure.
        4. Require LL_REJECT_EXT_IND rejecting the Lower Tester's LL_PHY_REQ,
           with reason code 0x23 (LL Procedure Collision).
        5. Answer the retained IUT request with LL_PHY_RSP preferring a non-1M
           PHY in both directions. Receive LL_PHY_UPDATE_IND with zero or one
           bit per field, each nonzero choice permitted by the directional
           request and response. Require a valid Instant for a nonzero choice.
           If both fields are zero, end with Inconclusive.
        6. Maintain the connection with empty DATA on the old PHYs until the
           event counter reaches the indicated Instant.
        7. At the Instant, use the PHY selected by each nonzero UPDATE field;
           retain the previous PHY for each zero field.
        8. Start maintaining the connection on those PHYs at the Instant.
        9. Receive and acknowledge the IUT's empty DATA packets on those PHYs.
        10. Require LE_PHY_Update_Complete reporting success, the active
            connection handle and the effective selected PHYs.

    Expected Outcome:
        Pass verdict:
            The original local procedure survives the collision. Each nonzero
            UPDATE field selects a common directional PHY. A zero is permitted
            if no common PHY exists, the current PHY is common, or the response
            specifies the same single PHY in both directions and both UPDATE
            fields are zero. A change uses a valid Instant; the Host receives
            the selected PHYs in LE_PHY_Update_Complete, including a no-change
            completion where applicable. No completion belongs to the rejected
            remote request.
        Inconclusive verdict:
            No LL_PHY_REQ is initiated in Step 2, or no PHY change is selected
            in Step 5. The latter ends the procedure before Steps 6-10.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-45-C; Figure 4.3-167;
        PDF pages 808-810, printed pages 4.3-335..337; source reference [10] 5.3.
    """
    await _central_collision(tester, procedure='phy')


# Verify that a Central rejects a colliding Peripheral PHY Update request and then
# completes its own PHY Update procedure. The Upper Tester starts the IUT's local
# procedure; the Lower Tester injects a competing request immediately after receiving
# the IUT request.
PROCEDURES |= procedure_cases(
    handling_protocol_collision_same_procedure_ll_con_cen_bv_45_c,
    {
        'LL/CON/CEN/BV-45-C': (
            'Handling Protocol Collision – Same Procedure',
            {},
        ),
    },
)


async def protocol_timeout_for_phy_update_procedure_ll_con_cen_bv_46_c(tester):
    """Protocol Timeout for PHY Update Procedure.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window and connection interval, common
        Peripheral latency and supervision timeout. The connection starts on LE 1M.

    Test Steps:
        1. Send HCI_LE_Set_PHY with TX_PHYS and RX_PHYS preferring a non-1M PHY
           and PHY_options=0x0000, and receive HCI_Command_Status. This
           procedure requests LE 2M in both directions with ALL_PHYS=0.
        2. Receive the IUT's LL_PHY_REQ with at least one supported PHY bit in
           each field. If no request is sent, end with Inconclusive.
        3. Acknowledge LL_PHY_REQ but do not send LL_PHY_RSP. Continue valid
           empty DATA on the old PHY throughout the procedure-response timer
           so that supervision timeout does not replace the intended outcome.
        4. The IUT may send HCI_LE_PHY_Update_Complete with a nonzero status.
           Reject any successful PHY completion for the incomplete procedure.
        5. Require HCI_Disconnection_Complete with success, the active handle
           and reason 0x22 (LL Response Timeout) when the transaction timer
           expires. Verify that the IUT stops maintaining the connection.

    Expected Outcome:
        Pass verdict:
            The IUT disconnects with reason 0x22 at procedure-response timeout
            and ceases RF connection maintenance. Any optional Step 4 PHY
            completion has a nonzero status and the active connection handle.
        Inconclusive verdict:
            The IUT does not initiate LL_PHY_REQ in Step 2.

    Observation Boundary:
        Bracket internal procedure initiation between the triggering HCI command
        and the first transmitted LL_PHY_REQ; do not restart the timer on its
        acknowledgement. Sample HCI delivery at 1 ms resolution around that
        bracket plus the 40 s response timeout. This is the test's observation
        policy, not a numeric timing tolerance specified by Core 5.2.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-46-C; Figure 4.3-168;
        PDF pages 810-811, printed pages 4.3-337..338; source reference [10] 5.2.
    """
    await _phy_timeout(tester, central=True, local=True)


# Verify that a Central terminates the Link Layer connection when its PHY Update
# procedure remains incomplete until the response timer expires. The Peripheral Lower
# Tester maintains the connection but withholds the required procedure response.
PROCEDURES |= procedure_cases(
    protocol_timeout_for_phy_update_procedure_ll_con_cen_bv_46_c,
    {
        'LL/CON/CEN/BV-46-C': (
            'Protocol Timeout for PHY Update Procedure',
            {},
        ),
    },
)


async def handling_protocol_collision_different_procedure_channel_map_cen_bv_47(
    tester,
):
    """Handling Protocol Collision – Different Procedure – Channel Map.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window and connection interval, common
        Peripheral latency and supervision timeout. The connection starts on LE 1M.

    Test Steps:
        1. Send HCI_LE_Set_Host_Channel_Classification specifying only even
           data channels.
        2. If the command succeeds, require HCI_Command_Complete with status
           0x00. Otherwise retry with different Channel_Map values, maintaining
           at least 5 seconds between attempts. After 20 failures, end with
           Inconclusive; stop retrying as soon as a command succeeds.
        3. Receive LL_CHANNEL_MAP_IND containing the accepted map from Step 1.
           Its absence after a successful command is a failure, not Inconclusive.
        4. Respond with LL_PHY_REQ preferring a PHY other than LE 1M in both
           directions. This procedure selects LE 2M and injects the request in
           the immediate acknowledgement of LL_CHANNEL_MAP_IND.
        5. Require LL_REJECT_EXT_IND rejecting LL_PHY_REQ with reason 0x2A
           (Different Transaction Collision).
        6. Maintain the connection with empty DATA until the event counter
           reaches the Channel Map Update Instant. Continue DATA using the
           updated map at and after that Instant; confirm it with HCI Read
           Channel Map.

    Expected Outcome:
        Pass verdict:
            The IUT transmits the required LL_CHANNEL_MAP_IND, rejects the
            competing PHY request, and maintains the connection with the new
            data-channel selection parameters after the assigned event.
            The rejected PHY request must not produce a PHY completion.
        Inconclusive verdict:
            HCI_LE_Set_Host_Channel_Classification fails on all 20 attempts.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-47-C; Figure 4.3-169;
        PDF pages 812-813, printed pages 4.3-339..340; source reference [10] 5.3.
    """
    await _central_collision(tester, procedure='map')


# Verify that a Central rejects a Peripheral PHY Update request colliding with the
# Central's Channel Map Update, then completes the map update and maintains the
# connection using the new data-channel selection parameters.
PROCEDURES |= procedure_cases(
    handling_protocol_collision_different_procedure_channel_map_cen_bv_47,
    {
        'LL/CON/CEN/BV-47-C': (
            'Handling Protocol Collision – Different Procedure – Channel Map',
            {},
        ),
    },
)


async def handling_connection_parameter_collision_cen_bv_48(
    tester,
):
    """Handling Protocol Collision – Different Procedure – Connection Parameters.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window and connection interval, common
        Peripheral latency and supervision timeout. The connection starts on LE 1M.
        Declare the Lower Tester's Connection Parameters Request Procedure support
        through Feature Exchange so the IUT uses LL_CONNECTION_PARAM_REQ.

    Test Steps:
        1. Send HCI_LE_Connection_Update requesting the minimum connection
           interval (7.5 ms), zero latency and a 3 s supervision timeout.
           Receive HCI_Command_Status.
        2. Receive LL_CONNECTION_PARAM_REQ; its parameters may differ from the
           Host's requested values. Validate its fields and respond with
           LL_PHY_REQ preferring a non-1M PHY in both directions. This procedure
           chooses LE 2M and sends that request in the immediate acknowledgement
           of LL_CONNECTION_PARAM_REQ.
        3. Require LL_REJECT_EXT_IND rejecting LL_PHY_REQ with reason 0x2A
           (Different Transaction Collision).
        4. Answer the retained LL_CONNECTION_PARAM_REQ with
           LL_CONNECTION_PARAM_RSP and receive LL_CONNECTION_UPDATE_IND.
           This response proposes the minimum interval, zero latency and 3 s
           timeout; check the indication's legal parameters and transmit window.
        5. Maintain the connection with empty DATA until the event counter
           reaches the indicated connection update Instant.
        6. At that Instant, use the new parameters selected by the IUT.
        7. Start maintaining the connection with those parameters at the update,
           allowing the Central's first anchor anywhere inside the transmit window.
        8. Require HCI_LE_Connection_Update_Complete with success, the active
           handle and the new interval, latency and supervision timeout.
           Reject an early successful completion before the Instant.

    Expected Outcome:
        Pass verdict:
            The IUT sends LL_CONNECTION_PARAM_REQ, rejects the competing PHY
            request, maintains the connection with the selected new parameters
            and reports them in HCI_LE_Connection_Update_Complete.
            The rejected PHY request must not produce a PHY completion.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-48-C; Figure 4.3-170;
        PDF pages 813-815, printed pages 4.3-340..342; source reference [10] 5.3.
    """
    await _central_collision(tester, procedure='parameters')


# Verify that a Central rejects a Peripheral PHY Update request colliding with the
# Central's Connection Parameters Request, then completes its original procedure and
# reports the resulting connection parameters.
PROCEDURES |= procedure_cases(
    handling_connection_parameter_collision_cen_bv_48,
    {
        'LL/CON/CEN/BV-48-C': (
            (
                'Handling Protocol Collision – Different Procedure – '
                'Connection Parameters'
            ),
            {},
        ),
    },
)


async def initiating_phy_update_procedure_packet_time_restrictions_ll_con_cen_bv_49_c(
    tester,
):
    """Initiating PHY Update Procedure – Packet Time Restrictions.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window and connection interval, common
        Peripheral latency and supervision timeout. This procedure uses a 60 ms
        connection interval and begins on LE 1M.

    Test Steps:
        1. Send HCI_LE_Set_PHY with ALL_PHYS=0x03 and receive HCI_Command_Status.
        2. Require HCI_LE_PHY_Update_Complete reporting LE 1M in both directions.
           If LE 1M/LE 1M is not selected, end with Inconclusive.
        3. Send HCI_LE_Set_PHY with ALL_PHYS=0, TX_PHYS=RX_PHYS=0x02 and receive
           HCI_Command_Status.
        4. Receive LL_PHY_REQ and answer with LL_PHY_RSP having TX_PHYS and
           RX_PHYS both 0x02.
        5. Receive LL_PHY_UPDATE_IND selecting LE 2M in both directions.
           Otherwise end with Inconclusive. Maintain old PHYs until the Instant
           and then exchange DATA on LE 2M.
        6. Require HCI_LE_PHY_Update_Complete reporting LE 2M/LE 2M.
        7. Send HCI_LE_Set_Data_Length with TxTime=0x0148 (328 us) and
           TxOctets=32. Receive HCI_Command_Complete.
        8. If the IUT initiates Data Length Update, answer with RxTime=0x0148
           and RxOctets=32. Otherwise initiate it with those receive limits and
           receive the IUT's response. If IUT TxTime<0x0148 or TxOctets<32,
           end with Inconclusive.
        9. Begin continuously queueing 32-octet Host data packets with LLID=0b10
           (start). Keep queueing for the remainder, respecting HCI credits.
        10. Receive byte-identical, unfragmented packets matching the queued data.
            Fragmentation before the PHY restriction boundary is Inconclusive.
        11. Send HCI_LE_Set_PHY with ALL_PHYS=0 and TX_PHYS=RX_PHYS=0x01.
        12. Receive LL_PHY_REQ and answer with LL_PHY_RSP having TX_PHYS and
            RX_PHYS both 0x01.
        13. Receive LL_PHY_UPDATE_IND selecting LE 1M in both directions.
            Otherwise end with Inconclusive.
        14. Require every DATA payload received after that indication to contain
            at most 31 octets. Observe packets both before and after the PHY
            update; do not defer this restriction until the Instant.
        15. Continue the PHY Update with the Host still queueing 32-octet start
            packets and the Lower Tester receiving and reassembling DATA.
        16. At the indicated Instant, switch connection maintenance to LE 1M.
        17. Require HCI_LE_PHY_Update_Complete reporting LE 1M/LE 1M, success
            and the active connection handle, with no early completion.
        18. Continue queueing and receiving DATA of at most 31 octets for at
            least 10 connection events after the Instant.

    Expected Outcome:
        Pass verdict:
            Before Step 13's indication, DATA is 32 octets and unfragmented.
            Afterwards DATA is at most 31 octets, including before the Instant.
            The PHY changes to LE 1M at the Instant. Every received payload and
            its airtime remain within effective receive octet/time limits.
            For the first two packets after each Data Length Update, including
            retransmissions, the applicable limits are the greater of old and
            new limits. This setup increases the initial limits to 32/328, so
            the same maximum applies during and after that allowance.
            Reassembled bytes, start/continuation boundaries and HCI credits
            match the continuously submitted Host packets.
        Inconclusive verdict:
            Required LE 1M setup or LE 2M setup is not selected, the IUT's DLE
            transmit limits are too small, pre-indication DATA is fragmented,
            or the final update does not select LE 1M in both directions.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-49-C; Figures 4.3-171 and 4.3-172;
        PDF pages 815-819, printed pages 4.3-342..346; source reference [10] 5.1.10.1.
    """
    await _packet_time_restrictions(
        tester, central=True, local=True, coded=False, no_change=False
    )


# Verify a Central's packet-time restrictions before, during and after its locally
# initiated PHY change from LE 2M to LE 1M, while the Host continues queueing 32-octet
# start packets.
PROCEDURES |= procedure_cases(
    initiating_phy_update_procedure_packet_time_restrictions_ll_con_cen_bv_49_c,
    {
        'LL/CON/CEN/BV-49-C': (
            'Initiating PHY Update Procedure – Packet Time Restrictions',
            {},
        ),
    },
)


async def responding_to_phy_update_packet_time_limits_cen_bv_50(
    tester,
):
    """Responding to PHY Update Procedure – Packet Time Restrictions.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window and connection interval, common
        Peripheral latency and supervision timeout. This procedure uses a 60 ms
        connection interval and begins on LE 1M.

    Test Steps:
        1. Send HCI_LE_Set_PHY with ALL_PHYS=0, TX_PHYS=RX_PHYS=0x02 and receive
           HCI_Command_Status.
        2. Receive LL_PHY_REQ and answer with LL_PHY_RSP having TX_PHYS and
           RX_PHYS both 0x02.
        3. Receive LL_PHY_UPDATE_IND selecting LE 2M in both directions.
           Otherwise end with Inconclusive. Maintain old PHYs until the Instant
           and then exchange DATA on LE 2M.
        4. Require HCI_LE_PHY_Update_Complete reporting LE 2M/LE 2M.
        5. Send HCI_LE_Set_Data_Length with TxTime=0x0148 (328 us) and
           TxOctets=32. Receive HCI_Command_Complete.
        6. If the IUT initiates Data Length Update, answer with RxTime=0x0148
           and RxOctets=32. Otherwise initiate it with those receive limits and
           receive the IUT's response. If IUT TxTime<0x0148 or TxOctets<32,
           end with Inconclusive.
        7. Begin continuously queueing 32-octet Host data packets with LLID=0b10
           (start). Keep queueing for the remainder, respecting HCI credits.
        8. Receive byte-identical, unfragmented packets matching the queued data.
           Fragmentation before the PHY restriction boundary is Inconclusive.
        9. Send HCI_LE_Set_PHY with ALL_PHYS=0x03 and receive HCI_Command_Status.
           If the IUT initiates LL_PHY_REQ, answer with LL_PHY_RSP specifying
           the current PHY in both directions and receive its no-change UPDATE.
        10. Require HCI_LE_PHY_Update_Complete reporting LE 2M/LE 2M, success
            and the active connection handle.
        11. Send LL_PHY_REQ with TX_PHYS=RX_PHYS=0x01.
        12. Receive LL_PHY_UPDATE_IND selecting LE 1M in both directions.
            Otherwise end with Inconclusive.
        13. Require every DATA payload received after that indication to contain
            at most 31 octets. Observe packets both before and after the PHY
            update; do not defer this restriction until the Instant.
        14. Continue the PHY Update with the Host still queueing 32-octet start
            packets and the Lower Tester receiving and reassembling DATA.
        15. At the indicated Instant, switch connection maintenance to LE 1M.
        16. Require HCI_LE_PHY_Update_Complete reporting LE 1M/LE 1M, success
            and the active connection handle, with no early completion.
        17. Continue queueing and receiving DATA of at most 31 octets for at
            least 10 connection events after the Instant.

    Expected Outcome:
        Pass verdict:
            Before Step 12's indication, DATA is 32 octets and unfragmented.
            Afterwards DATA is at most 31 octets, including before the Instant.
            The PHY changes to LE 1M at the Instant. Every received payload and
            its airtime remain within effective receive octet/time limits.
            For the first two packets after each Data Length Update, including
            retransmissions, the applicable limits are the greater of old and
            new limits. This setup increases the initial limits to 32/328, so
            the same maximum applies during and after that allowance.
            Reassembled bytes, start/continuation boundaries and HCI credits
            match the continuously submitted Host packets.
        Inconclusive verdict:
            LE 2M setup is not selected, the IUT's DLE transmit limits are too
            small, pre-indication DATA is fragmented, or the final update does
            not select LE 1M in both directions.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-50-C; Figures 4.3-173 and 4.3-174;
        PDF pages 819-823, printed pages 4.3-346..350; source reference [10] 5.1.10.1.
    """
    await _packet_time_restrictions(
        tester, central=True, local=False, coded=False, no_change=False
    )


# Verify a Central's packet-time restrictions before, during and after a
# Peripheral-initiated change from LE 2M to LE 1M, while the Host continues queueing
# 32-octet start packets.
PROCEDURES |= procedure_cases(
    responding_to_phy_update_packet_time_limits_cen_bv_50,
    {
        'LL/CON/CEN/BV-50-C': (
            'Responding to PHY Update Procedure – Packet Time Restrictions',
            {},
        ),
    },
)


async def initiating_phy_update_procedure_no_common_phy(tester):
    """Initiating PHY Update Procedure – No Common PHY.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window and connection interval, common
        Peripheral latency and supervision timeout. The connection starts on LE 1M;
        the IUT also supports LE 2M.

    Test Steps:
        1. Send HCI_LE_Set_PHY with ALL_PHYS=0, TX_PHYS=RX_PHYS preferring one
           supported non-1M PHY, and PHY_options=0x0000. Receive successful
           HCI_Command_Status. This procedure selects LE 2M.
        2. Receive LL_PHY_REQ with at least one supported bit in each field.
           If the IUT sends no request, end with Inconclusive.
        3. Acknowledge LL_PHY_REQ and send LL_PHY_RSP. Select each corresponding
           directional field independently:
           a. If the request field has exactly one bit, choose a different bit
              that corresponds to a PHY supported by the IUT.
           b. If the request field has multiple bits, choose the current PHY.
           Map the IUT's TX preference to the Lower Tester's RX field and the
           IUT's RX preference to the Lower Tester's TX field.
        4. Receive LL_PHY_UPDATE_IND with PHY_C_TO_P=PHY_P_TO_C=0 and Instant=0.
           Continue DATA on the unchanged LE 1M PHYs.
        5. Require LE_PHY_Update_Complete reporting success, the active handle
           and the previous PHYs in both directions, with no duplicate completion.

    Expected Outcome:
        Pass verdict:
            Both UPDATE PHY fields and the Instant are zero. The IUT reports
            the previous PHYs in a successful no-change completion and continues
            to exchange DATA without changing the PHY.
        Inconclusive verdict:
            The IUT does not send LL_PHY_REQ in Step 2.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-51-C; Figure 4.3-175;
        PDF pages 823-824, printed pages 4.3-350..351; source reference [10] 5.1.10.
    """
    await _no_common_phy(tester, asymmetric=False)


# Verify that a Central correctly completes a locally initiated PHY Update when the
# peer's response provides no common new PHY, retaining the active PHYs and reporting a
# successful no-change completion.
PROCEDURES |= procedure_cases(
    initiating_phy_update_procedure_no_common_phy,
    {
        'LL/CON/CEN/BV-51-C': (
            'Initiating PHY Update Procedure – No Common PHY',
            {},
        ),
    },
)


async def initiating_phy_update_packet_time_limits_le_coded_cen_bv_53(
    tester,
):
    """Initiating PHY Update Procedure – Packet Time Restrictions, LE Coded.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window and connection interval, common
        Peripheral latency and supervision timeout. This procedure uses a 60 ms
        interval and starts on LE 1M.

    Test Steps:
        1. Send HCI_LE_Set_PHY with ALL_PHYS=0x03 and PHY_options=0x0000;
           receive HCI_Command_Status.
        2. Require HCI_LE_PHY_Update_Complete reporting LE 1M in both directions.
           If LE 1M/LE 1M is not selected, end with Inconclusive.
        3. Send HCI_LE_Set_Data_Length with TxTime=0x04D8 (1240 us) and
           TxOctets=141; receive HCI_Command_Complete.
        4. If the IUT initiates Data Length Update, answer with RxTime=0x04D8
           and RxOctets=141. Otherwise initiate it with those receive limits
           and receive the IUT's response. If IUT TxTime<0x04D8 or
           TxOctets<141, end with Inconclusive.
        5. Begin continuously queueing 141-octet Host packets with LLID=0b10
           (start); keep queueing for the remainder, respecting HCI credits.
        6. Receive unfragmented packets matching the queued Host data.
           Pre-indication fragmentation is Inconclusive.
        7. Send HCI_LE_Set_PHY with ALL_PHYS=0, PHY_options=0x0000 and
           TX_PHYS=RX_PHYS preferring LE Coded.
        8. Receive LL_PHY_REQ and answer with LL_PHY_RSP having TX_PHYS and
           RX_PHYS both 0x04.
        9. Receive LL_PHY_UPDATE_IND selecting LE Coded in both directions.
           Otherwise end with Inconclusive.
        10. Require every DATA payload after that indication to be at most
            140 octets. A larger payload fails, including before the Instant.
            Observe DATA both before and after PHY Update.
        11. Continue PHY Update with the Host still queueing 141-octet start
            packets and the Lower Tester receiving DATA.
        12. At the indicated Instant, switch connection maintenance to LE Coded.
        13. Require HCI_LE_PHY_Update_Complete reporting LE Coded/LE Coded,
            success and the active handle, with no early completion.
        14. Continue queueing and receiving DATA of at most 140 octets for at
            least 10 connection events after the Instant.

    Expected Outcome:
        Pass verdict:
            Before Step 9's indication, DATA is 141 octets and unfragmented;
            afterwards every DATA payload is at most 140 octets. PHYs change
            only at the Instant. Payload size and airtime obey effective
            receive-octet/time limits. For the first two packets after Data
            Length Update, including retransmissions, use the greater of old
            and new limits; the initial increase makes this the new limit.
            Reassembled Host bytes, fragment boundaries and HCI credits match.
        Inconclusive verdict:
            LE 1M setup is not selected, DLE transmit limits are too small,
            pre-indication DATA is fragmented, LE Coded is not selected in
            both directions, or the IUT autonomously sends LL_LENGTH_REQ after
            the Instant in Step 9.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-53-C; Figures 4.3-177 and 4.3-178;
        PDF pages 828-832, printed pages 4.3-355..359; source reference [10] 5.1.10.1.
    """
    await _packet_time_restrictions(
        tester, central=True, local=True, coded=True, no_change=False
    )


# Verify a Central's transmit restrictions during a locally initiated change from LE 1M
# to LE Coded. Packets queued during PHY Update must also remain valid if awaiting
# retransmission after the Instant.
PROCEDURES |= procedure_cases(
    initiating_phy_update_packet_time_limits_le_coded_cen_bv_53,
    {
        'LL/CON/CEN/BV-53-C': (
            'Initiating PHY Update Procedure – Packet Time Restrictions, LE Coded',
            {},
        ),
    },
)


async def responding_to_phy_update_packet_time_limits_le_coded_cen_bv_54(
    tester,
):
    """Responding to PHY Update Procedure – Packet Time Restrictions, LE Coded.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window and connection interval, common
        Peripheral latency and supervision timeout. This procedure uses a 60 ms
        interval and starts on LE 1M.

    Test Steps:
        1. Send HCI_LE_Set_PHY with ALL_PHYS=0x03 and PHY_options=0x0000;
           receive HCI_Command_Status.
        2. Require HCI_LE_PHY_Update_Complete reporting LE 1M in both directions.
           If LE 1M/LE 1M is not selected, end with Inconclusive.
        3. Send HCI_LE_Set_Data_Length with TxTime=0x04D8 (1240 us) and
           TxOctets=141; receive HCI_Command_Complete.
        4. If the IUT initiates Data Length Update, answer with RxTime=0x04D8
           and RxOctets=141. Otherwise initiate it with those receive limits
           and receive the IUT's response. If IUT TxTime<0x04D8 or
           TxOctets<141, end with Inconclusive.
        5. Begin continuously queueing 141-octet Host packets with LLID=0b10
           (start); keep queueing for the remainder, respecting HCI credits.
        6. Receive unfragmented packets matching the Host data. Apply the
           source's explicit pre-indication fragmentation Inconclusive verdict;
           see Source Correction.
        7. Send LL_PHY_REQ with TX_PHYS and RX_PHYS both 0x04.
        8. Receive LL_PHY_UPDATE_IND selecting LE Coded in both directions.
           Otherwise end with Inconclusive.
        9. Require every DATA payload after Step 8's indication to be at most
           140 octets. A larger payload fails, including before the Instant.
           Observe DATA both before and after PHY Update.
        10. Continue PHY Update with the Host still queueing 141-octet start
            packets and the Lower Tester receiving DATA.
        11. At the indicated Instant, switch connection maintenance to LE Coded.
        12. Require HCI_LE_PHY_Update_Complete reporting LE Coded/LE Coded,
            success and the active handle, with no early completion.
        13. Continue queueing and receiving DATA of at most 140 octets for at
            least 10 connection events after the Instant.

    Expected Outcome:
        Pass verdict:
            Before Step 8's indication, DATA is 141 octets and unfragmented;
            afterwards every DATA payload is at most 140 octets. PHYs change
            only at the Instant. Payload size and airtime obey effective
            receive-octet/time limits. For the first two packets after Data
            Length Update, including retransmissions, use the greater of old
            and new limits; the initial increase makes this the new limit.
            Reassembled Host bytes, fragment boundaries and HCI credits match.
        Inconclusive verdict:
            LE 1M setup is not selected, DLE transmit limits are too small,
            pre-indication DATA is fragmented, or LE Coded is not selected
            in both directions.

    Source Correction:
        Step 6 says fragmentation is Fail, but the explicit Inconclusive list
        names the same pre-indication fragmentation. Use that explicit verdict.
        Step 9 refers to an UPDATE in Step 13, which contains post-Instant
        DATA; the actual UPDATE is in Step 8. Apply the 140-octet restriction
        immediately after Step 8. Amber corroborated these local editorial
        interpretations from the supplied source text; neither is a SIG erratum.

    Coverage Note:
        Unlike BV-53, this source does not authorize Inconclusive for an
        autonomous LL_LENGTH_REQ after the Instant. Answer such requests with
        the unchanged Lower Tester limits and independently check effective
        length events, RF payload length/airtime, reassembly and HCI credits.
        The greater old/new limits apply to the first two DATA packets after
        DLE; the separate 140-octet post-indication restriction remains active.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-54-C; Figures 4.3-179 and 4.3-180;
        PDF pages 832-835, printed pages 4.3-359..362; source reference [10] 5.1.10.1.
    """
    await _packet_time_restrictions(
        tester, central=True, local=False, coded=True, no_change=False
    )


# Verify a Central's transmit restrictions during a Peripheral-initiated change from LE
# 1M to LE Coded. Packets queued during PHY Update must also remain valid if awaiting
# retransmission after the Instant.
PROCEDURES |= procedure_cases(
    responding_to_phy_update_packet_time_limits_le_coded_cen_bv_54,
    {
        'LL/CON/CEN/BV-54-C': (
            ('Responding to PHY Update Procedure – Packet Time Restrictions, LE Coded'),
            {},
        ),
    },
)


async def mandatory_minimum_pdu_length_le_coded_ll_con_cen_bv_55_c(tester):
    """Mandatory Minimum PDU Length, LE Coded.

    Initial Condition:
        Connected Central, public Peripheral Lower Tester, 60-ms interval,
        zero latency and minimum data-length octet/time values.

    Test Steps:
        1-3. Set PHY with no preference and verify completion on LE 1M.
             If the Central sends PHY_REQ, the Peripheral answers PHY_RSP
             retaining LE 1M; the Central sends any required UPDATE.
        4. If DLE occurs, answer with 27 octets and 328 us per direction.
        5. Queue 27-octet Host start packets continuously from this point.
        6-8. The Peripheral requests Coded in both directions. Receive the
             Central's UPDATE with a valid future Instant and acknowledge it.
        9-11. Keep DATA within the effective octet/time limits, maintain the
              connection, and apply Coded at the indicated Instant.
        12. Verify PHY completion and, when effective limits change, Data
            Length Change: 27 octets and 2704 us in both directions.
        13. Continue receiving DATA for at least ten further events.
        14. Repeat the change to LE 1M, checking 27 octets and 328 us.

    Expected Outcome:
        DATA continues through both PHY changes with valid acknowledgements,
        no oversized payloads and correct effective-length notifications.
        The first two transition packets use the greater of old/new limits.

    Source Correction:
        PDF 837 inherits Peripheral-IUT steps without consistently swapping
        roles. Follow the Central-IUT purpose and its explicit Step 6 override:
        the Peripheral Lower Tester initiates with PHY_REQ; the Central IUT
        sends UPDATE. Core Vol 6, Part B, 5.1.10 assigns these roles; the copied
        diagram's Peripheral UPDATE and Central PHY_RSP are not legal here.
        Preserve the inherited no-preference setup, minimum DLE, queued DATA,
        two PHY changes, Instants and ten-event observations. Core 4.5.10
        requires a minimum 2704-us Coded budget even when negotiated times are
        328 us. This is a local correction, not an adopted SIG erratum.

    Reference:
        LL.TS.p28, LL/CON/CEN/BV-55-C, PDF 836-837, Figures 4.3-181/182;
        inherited PER/BV-57-C, PDF 685-689, Figures 4.3-97/98, Steps 1-14.
        Core Vol 6, Part B, 4.5.10 and 5.1.10.
    """
    await _mandatory_coded_minimum(tester, central=True)


# Verify that a Central continues transmitting DATA with the mandatory minimum 27-octet
# allowance when negotiated times would otherwise imply a shorter LE Coded packet. The
# source states that the Peripheral Lower Tester initiates PHY Update.
PROCEDURES |= procedure_cases(
    mandatory_minimum_pdu_length_le_coded_ll_con_cen_bv_55_c,
    {
        'LL/CON/CEN/BV-55-C': ('Mandatory Minimum PDU Length, LE Coded', {}),
    },
)


async def phy_update_procedure_central_requests_asymmetrical_peripheral_symmetrical(
    tester,
):
    """PHY Update Procedure – Central Requests Asymmetrical, Peripheral Symmetrical.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window and connection interval, common
        Peripheral latency and supervision timeout. All communication uses LE 1M.

    Test Steps:
        1. Send HCI_LE_Set_PHY with ALL_PHYS=0x00, TX_PHYS=0x01 and
           RX_PHYS=0x02.
        2. Receive successful HCI_Command_Status.
        3. Receive the IUT's LL_PHY_REQ with TX_PHYS=0x01 and RX_PHYS=0x02.
           If no request is sent or either field differs, end with Inconclusive.
        4. Answer LL_PHY_RSP with TX_PHYS=RX_PHYS=0x02, expressing the
           Peripheral's symmetric-only LE 2M preference.
        5. Require LL_PHY_UPDATE_IND with PHY_C_TO_P=PHY_P_TO_C=0, indicating
           no change. Require the unused Instant field to be zero.
        6. Require LE_PHY_Update_Complete reporting success, the active handle
           and unchanged LE 1M in both directions. Continue old-PHY DATA and
           reject duplicate completions.

    Expected Outcome:
        Pass verdict:
            The IUT sends the all-zero no-change UPDATE and reports unchanged
            PHYs. It must not independently change only the direction whose
            masks intersect: the Peripheral's same-single-PHY response requires
            symmetric selection or no change under Core 5.1.10.
        Inconclusive verdict:
            The IUT sends no LL_PHY_REQ or does not send TX_PHYS=0x01 and
            RX_PHYS=0x02 in Step 3.

    Source Correction:
        Step 4 writes TX_PHY twice. The second field is RX_PHYS, so both
        LL_PHY_RSP fields are 0x02. Amber corroborated the field correction.
        A follow-up retrieved Core 6.3 Vol 6 B 5.1.10's specific symmetric
        Peripheral preference rule, confirming that the generic directional
        intersection alone cannot justify a one-direction change here.
        This is a local editorial correction, not a SIG erratum.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-117-C; Figure 4.3-189, Steps 1-6 and verdicts;
        PDF pages 854-855, printed pages 4.3-381..382; source reference [10] 5.1.10.
        Core v6.3, Vol 6, Part B, 5.1.10 and 2.4.2.23.
    """
    await _no_common_phy(tester, asymmetric=True)


# Verify that a Central keeps both PHYs unchanged when it requests an asymmetric
# connection but the Peripheral's single-PHY response only permits a symmetric
# connection.
PROCEDURES |= procedure_cases(
    phy_update_procedure_central_requests_asymmetrical_peripheral_symmetrical,
    {
        'LL/CON/CEN/BV-117-C': (
            (
                'PHY Update Procedure – Central Requests Asymmetrical, '
                'Peripheral Symmetrical'
            ),
            {},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_phy_update(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
