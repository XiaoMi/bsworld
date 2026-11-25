# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.3: ACL data, acknowledgement, latency and termination."""

import random
import struct
from dataclasses import replace

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_MAX,
    LL_CONN_INTERVAL_MIN,
    LL_CONN_INTERVAL_UNIT_US,
    LL_EVENT_COUNTER_MASK,
    LL_IFS_US,
    LL_RESPONSE_TIMEOUT_US,
    LL_SCA_500_PPM,
    LL_SCA_PPM,
    LL_SUPERVISION_TIMEOUT_MAX,
    LL_SUPERVISION_TIMEOUT_MIN,
    LL_SUPERVISION_TIMEOUT_UNIT_US,
    PDU_CONTROL_FEATURE_SET_SIZE,
    PDU_CONTROL_PHY_REQ_SIZE,
    PDU_CONTROL_VERSION_SIZE,
    PDU_CRC_SIZE,
    PDU_DATA_LLID_MASK,
    PDU_DATA_MD_MASK,
    PDU_DATA_NESN_MASK,
    PDU_DATA_PAYLOAD_SIZE_MIN,
    PDU_DATA_PAYLOAD_TIME_CODED_MIN_US,
    PDU_DATA_PAYLOAD_TIME_MIN_US,
    PDU_HEADER_SIZE,
    LlId,
)
from bscontroller.radio.base import (
    PHY_CODED_FEC1_TIME_US,
    PHY_CODED_TERM2_SIZE_BITS,
    PHY_OCTET_BITS,
    PHY_OCTET_MASK,
    PHY_TIME_UNITS_PER_SECOND,
    Phy,
)
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.connection import (
    ARQ_PAYLOAD,
    HCI_OBSERVATION_STEP_US,
    LENGTH_FIELDS,
    MINIMUM,
    ErrorCode,
    Opcode,
    _advertiser_connection_parameters,
    _check_central_timing,
    _check_changed_events,
    _check_completion,
    _check_feature_pdu,
    _check_parameter_pdu,
    _check_parameters,
    _check_rf_stopped,
    _completion,
    _disconnected,
    _effective_lengths,
    _observe_disconnection,
    _prepare_preferences,
    _procedure_timeout,
    _quiet_events,
    _receive_central_event,
    _restore_peer,
    _sequence_bits,
    _supported_lengths,
    _version_link,
    _version_payload,
)
from tests.qualification.link import _data_pdu, _wire_event
from tests.qualification.peer import (
    PeerLink,
    establish,
    host_send,
    peer_send,
    received_acl,
)
from tests.qualification.peer import finish_phy_update as _finish_update
from tests.qualification.peer import phy_bit as _bit
from tests.qualification.peer import phy_update_pdu as _update_pdu
from tests.qualification.tester import (
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
    LinkParameters,
)

PROCEDURES = ProcedureMap()


class _HciDataStream:
    """Compare bytes and start-flag offsets, not packetization (LL.TS.p28 4.1.7)."""

    def __init__(self, tester, handle):
        self.tester, self.handle = tester, handle
        self.expected, self.received = bytearray(), bytearray()
        self.expected_starts, self.received_starts = [], []

    def sent(self, data, continuation):
        if not continuation:
            self.expected_starts.append(len(self.expected))
        self.expected.extend(data)

    def accept(self, event):
        assert event.connection_handle == self.handle
        assert event.bc_flag == 0 and event.data_total_length == len(event.data)
        assert event.pb_flag in (
            hci.HCI_ACL_PB_FIRST_FLUSHABLE,
            hci.HCI_ACL_PB_CONTINUATION,
        )
        if event.pb_flag == hci.HCI_ACL_PB_FIRST_FLUSHABLE:
            self.received_starts.append(len(self.received))
        self.received.extend(event.data)
        assert self.expected.startswith(self.received), 'HCI data bytes changed'
        assert (
            self.received_starts == self.expected_starts[: len(self.received_starts)]
        ), 'HCI start-flag locations changed'

    def poll(self):
        while event := self.tester.take(
            lambda value: isinstance(value, hci.HCI_AclDataPacket)
        ):
            self.accept(event)

    async def finish(self):
        self.poll()
        while len(self.received) < len(self.expected) or len(
            self.received_starts
        ) < len(self.expected_starts):
            self.accept(await self.tester.event(hci.HCI_AclDataPacket))
            self.poll()
        assert self.received == self.expected
        assert self.received_starts == self.expected_starts, 'missing HCI start flag'


async def _data_exchange(tester, *, central, direction):
    """Verify directional 1000-octet groups, ACL fragmentation and simultaneous data
    exchange."""
    result = await tester.command(hci.HCI_LE_Read_Buffer_Size_Command())
    buffer_size = result.le_acl_data_packet_length
    assert buffer_size >= PDU_DATA_PAYLOAD_SIZE_MIN
    assert result.total_num_le_acl_data_packets > 0
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    generator = random.Random(0)
    receiver = _HciDataStream(tester, link.handle) if direction != 'tx' else None
    if direction == 'duplex':
        for index in range(100):
            data = generator.randbytes(10) if central else bytes(10)
            peer_data = generator.randbytes(10)
            continuation = central and index != 0
            receiver.sent(peer_data, continuation)
            await host_send(
                tester,
                link,
                data,
                continuation,
                peer_data=peer_data,
                peer_continuation=continuation,
                receiver=receiver,
            )
    else:
        operation = host_send if direction == 'tx' else peer_send
        for group in range(3):
            for index in range(100):
                data = bytes(10) if direction == 'tx' else generator.randbytes(10)
                continuation = group == 1 or (group == 2 and index != 0)
                if receiver is not None:
                    receiver.sent(data, continuation)
                await operation(
                    tester,
                    link,
                    data,
                    continuation,
                    receiver=receiver,
                )
        if direction == 'tx' and buffer_size > PDU_DATA_PAYLOAD_SIZE_MIN:
            sent = 0
            while sent < 1000:
                data = bytes(
                    generator.randrange(PDU_DATA_PAYLOAD_SIZE_MIN + 1, buffer_size + 1)
                )
                assert await host_send(tester, link, data) >= 2
                sent += len(data)
        elif direction == 'rx':
            for _ in range(10):
                data = generator.randbytes(3)
                receiver.sent(data, False)
                await peer_send(tester, link, data, receiver=receiver)
    if receiver is not None:
        await receiver.finish()
    await link.disconnect(local=True)


async def _connection_termination(tester, *, central, local):
    """Verify termination acknowledgement, HCI reasons and supervision-time RF
    silence."""
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
        timeout=100 if central else LL_SUPERVISION_TIMEOUT_MIN,
    )
    reason = ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    if local:
        await tester.command(hci.HCI_Disconnect_Command(link.handle, reason))
        response = await link.control(Opcode.LL_TERMINATE_IND)
        assert response == bytes([Opcode.LL_TERMINATE_IND, reason])
        if not central:
            await link._send(link.channel, b'', LlId.CONTINUATION, link.anchor)
            link.anchor += link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
            link.counter = link.counter + 1 & LL_EVENT_COUNTER_MASK
    else:
        sn = link.sn
        channel = link.channel
        response = await link.exchange(
            bytes([Opcode.LL_TERMINATE_IND, reason]), llid=LlId.CONTROL
        )
        assert response.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
        assert response.pdu[PDU_HEADER_SIZE:] == b''
        if central:
            assert (
                await tester.receive(
                    channel,
                    duration_us=link.anchor - tester.radio.now_us - 1,
                    **link.radio_parameters,
                )
                is None
            ), 'termination acknowledgement occurred before the following event'
            response = await tester.expect_pdu(
                link.channel, duration_us=1000, **link.radio_parameters
            )
            assert response.start_us == link.anchor
            assert response.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
            assert response.pdu[PDU_HEADER_SIZE:] == b''
            sn = link.sn
            link.anchor += link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
            link.counter = link.counter + 1 & LL_EVENT_COUNTER_MASK
        assert int(bool(response.pdu[0] & PDU_DATA_NESN_MASK)) != sn
    deadline = (
        tester.radio.now_us + link.parameters.timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
    )
    while link.anchor < deadline:
        if not central:
            await link._send(link.channel, b'', LlId.CONTINUATION, link.anchor)
        until = min(
            deadline,
            link.anchor + link.parameters.interval * LL_CONN_INTERVAL_UNIT_US - 1,
        )
        packet = await tester.receive(
            link.channel,
            duration_us=until - tester.radio.now_us,
            **link.radio_parameters,
        )
        assert packet is None, 'connection traffic continued after termination ACK'
        link.anchor += link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
        link.counter = link.counter + 1 & LL_EVENT_COUNTER_MASK
    await tester.wait(max(0, deadline - tester.radio.now_us))
    event = tester.take(
        lambda event: isinstance(event, hci.HCI_Disconnection_Complete_Event)
    )
    assert event is not None, 'missing HCI disconnection during RF observation'
    assert event.status == ErrorCode.SUCCESS
    assert event.connection_handle == link.handle
    assert event.reason == (
        ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR if local else reason
    )
    assert not any(
        isinstance(event, hci.HCI_Disconnection_Complete_Event)
        for event in tester.events
    ), 'duplicate HCI disconnection event'


async def _invalid_control_invalid_llid(tester, *, central=False):
    """Acknowledge reserved LLID without interpreting its payload as an LL command."""
    declared = int.from_bytes(
        (
            await tester.command(hci.HCI_LE_Read_Local_Supported_Features_Command())
        ).le_features,
        'little',
    )
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    if central:
        await tester.command(hci.HCI_LE_Read_Remote_Features_Command(link.handle))
        await link.control(Opcode.LL_FEATURE_REQ)
    payload = bytes(
        [Opcode.LL_FEATURE_RSP if central else Opcode.LL_FEATURE_REQ]
    ) + bytes(PDU_CONTROL_FEATURE_SET_SIZE)

    def check_ignored(packet):
        assert not any(
            isinstance(
                event,
                (hci.HCI_AclDataPacket, hci.HCI_LE_Read_Remote_Features_Complete_Event),
            )
            for event in tester.events
        )
        assert not (
            packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
            and packet.pdu[1]
            and (packet.pdu[PDU_HEADER_SIZE] == Opcode.LL_FEATURE_RSP)
        )

    sent_sn = None if central else link.sn
    nesn = link.nesn

    def build(packet):
        nonlocal sent_sn, nesn
        if packet is not None:
            peer_sn, peer_nesn = _sequence_bits(packet)
            if sent_sn is None:
                sent_sn = peer_nesn
            nesn = peer_sn ^ 1
        return _data_pdu(sent_sn, nesn, payload, llid=LlId.RESERVED)

    until = 0
    for attempt in range(5):
        packet, sent = await _wire_event(link, build)
        if attempt == 0:
            until = (
                link.last_sent_end_us
                - link.tx_phy.airtime_us(len(sent))
                + 4 * PHY_TIME_UNITS_PER_SECOND
            )
        check_ignored(packet)
        if (not central or attempt) and bool(
            packet.pdu[0] & PDU_DATA_NESN_MASK
        ) != bool(sent_sn):
            break
        nesn = _sequence_bits(packet)[0] ^ 1
    else:
        raise AssertionError('reserved LLID not acknowledged within four retries')
    _restore_peer(link, packet, sent)
    while tester.radio.now_us < until:
        packet = await link.exchange()
        check_ignored(packet)
    if central:
        await link.exchange(payload, llid=LlId.CONTROL)
        event = await tester.event(hci.HCI_LE_Read_Remote_Features_Complete_Event)
        assert (event.status, event.connection_handle, event.le_features) == (
            ErrorCode.SUCCESS,
            link.handle,
            payload[1:],
        )
    else:
        response = await link.control(Opcode.LL_FEATURE_RSP, payload, check_ack=True)
        _check_feature_pdu(declared, response)
    await link.disconnect()


# LL.TS.p28, CEN Step7/PER Step8: complete packet count for each payload size.
CODED_PACKET_COUNT = 288

# LL.TS.p28, PDF827/676..677: eighteen rows of sixteen consecutive DATA packets.
CODING_PATTERN = tuple(
    int(value)
    for row in (
        '2828282828282828',
        '2828282828282828',
        '2222222288888888',
        '2222222282222222',
        '2888888882888888',
        '8888888888222222',
        '2222222222822222',
        '2222222222288888',
        '8888888888828888',
        '8888888888888222',
        '8882228882882228',
        '2822828888828282',
        '8222282882888822',
        '8882282828822882',
        '2222882882828882',
        '8222882282228222',
        '2228228228828228',
        '8882888222288888',
    )
    for value in row
)

# LL.TS.p28, CEN Step8/PER Step9: 150 us +/-2 us for every response.
RESPONSE_TOLERANCE_US = 2


async def _coded_reception(tester, *, central):
    """Receive every prescribed coding transition at maximum and minimum payload
    sizes."""
    supported = await _supported_lengths(tester)
    link = await establish(
        tester,
        central=central,
        interval=LL_CONN_INTERVAL_MAX,
        timeout=LL_SUPERVISION_TIMEOUT_MAX,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    coded = _bit(Phy.LE_CODED)
    if central:
        await tester.command(
            hci.HCI_LE_Set_PHY_Command(link.handle, 0, coded, coded, 0)
        )
        request = await link.control(Opcode.LL_PHY_REQ)
        assert len(request) == PDU_CONTROL_PHY_REQ_SIZE and request[1] and request[2]
        update = await link.control(
            Opcode.LL_PHY_UPDATE_IND, bytes([Opcode.LL_PHY_RSP, coded, coded])
        )
        if update[1:3] != bytes([coded, coded]):
            pytest.skip('Inconclusive: Central did not select Coded in both directions')
    else:
        await _prepare_preferences(tester, link)
        response = await link.control(
            Opcode.LL_PHY_RSP, bytes([Opcode.LL_PHY_REQ, coded, coded])
        )
        assert len(response) == PDU_CONTROL_PHY_REQ_SIZE and response[1] and response[2]
        if not response[1] & response[2] & coded:
            pytest.skip('Inconclusive: Peripheral PHY response does not support Coded')
        update = _update_pdu(link, coded, coded)
        await link.exchange(update, llid=LlId.CONTROL, check_ack=True)
    await _finish_update(
        tester, link, update, event_required=True, allowed=(coded, coded)
    )
    before = (PDU_DATA_PAYLOAD_SIZE_MIN, PDU_DATA_PAYLOAD_TIME_CODED_MIN_US) * 2
    _check_changed_events(tester, link.handle, MINIMUM, before)
    effective = before
    if (
        supported[2] > PDU_DATA_PAYLOAD_SIZE_MIN
        or supported[3] > PDU_DATA_PAYLOAD_TIME_MIN_US
    ):
        result = await tester.command(
            hci.HCI_LE_Set_Data_Length_Command(link.handle, *supported[2:])
        )
        assert result.connection_handle == link.handle
        request = await link.control(Opcode.LL_LENGTH_REQ, required=False)
        peer = (*supported[2:], *supported[:2])
        if request is not None:
            dut = _check_parameters(request, Opcode.LL_LENGTH_REQ, supported)
            await link.exchange(
                bytes([Opcode.LL_LENGTH_RSP]) + LENGTH_FIELDS.pack(*peer),
                llid=LlId.CONTROL,
            )
        else:
            response = await link.control(
                Opcode.LL_LENGTH_RSP,
                bytes([Opcode.LL_LENGTH_REQ]) + LENGTH_FIELDS.pack(*peer),
            )
            dut = _check_parameters(response, Opcode.LL_LENGTH_RSP, supported)
        effective = _effective_lengths(dut, peer, link)
        _check_changed_events(
            tester,
            link.handle,
            before,
            effective,
            intermediate=_effective_lengths(dut, MINIMUM, link),
        )
    for size in (effective[2], PDU_DATA_PAYLOAD_SIZE_MIN):
        await _receive_coding_sequence(tester, link, size)
    await link.disconnect(local=True)


async def _receive_coding_sequence(tester, link, size):
    """Keep each 288-packet sequence within one maximum-interval connection event."""
    assert len(CODING_PATTERN) == CODED_PACKET_COUNT
    tester.output.write(f'CODED RX ROUND octets={size} packets={len(CODING_PATTERN)}\n')
    if link.central:
        response = await _receive_empty(link, link.anchor)
        start = response.end_us + LL_IFS_US
    else:
        start = link.anchor
    for index, coding in enumerate(CODING_PATTERN):
        data = bytes(index * size + octet & PHY_OCTET_MASK for octet in range(size))
        sn = link.sn
        pdu = _data_pdu(sn, link.nesn, data, llid=LlId.START)
        more = link.central or index + 1 < len(CODING_PATTERN)
        pdu = bytes([pdu[0] | (PDU_DATA_MD_MASK if more else 0)]) + pdu[1:]
        tester.output.write(f'CODED RX PACKET {index + 1} S={coding} octets={size}\n')
        end = await tester.transmit(
            link.channel, pdu, start_us=start, coding=coding, **link.radio_parameters
        )
        assert (
            end - start
            == PHY_CODED_FEC1_TIME_US
            + (
                (PDU_HEADER_SIZE + size + PDU_CRC_SIZE) * PHY_OCTET_BITS
                + PHY_CODED_TERM2_SIZE_BITS
            )
            * coding
        )
        response = await _receive_empty(link, end + LL_IFS_US, acknowledged_sn=sn)
        await received_acl(tester, link, data, False)
        start = response.end_us + LL_IFS_US
    if link.central:
        await link._send(link.channel, b'', LlId.CONTINUATION, start)
    assert (
        tester.radio.now_us
        < link.anchor + link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
    )
    link.anchor += link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
    link.counter = link.counter + 1 & LL_EVENT_COUNTER_MASK
    assert not any(
        isinstance(event, hci.HCI_AclDataPacket) for event in tester.events
    ), 'duplicate or unexpected HCI data after the 288-packet sequence'


async def _receive_empty(link, expected_start, *, acknowledged_sn=None):
    tester = link.tester
    packet = await tester.expect_pdu(
        link.channel,
        start_us=max(tester.radio.now_us + 1, expected_start - RESPONSE_TOLERANCE_US),
        duration_us=max(3000, expected_start - tester.radio.now_us + 3000),
        **link.radio_parameters | {'phy': link.rx_phy},
    )
    assert abs(packet.start_us - expected_start) <= RESPONSE_TOLERANCE_US
    assert packet.phy == Phy.LE_CODED
    assert packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
    assert packet.pdu[1] == 0, 'IUT response is not an empty DATA PDU'
    sn, nesn = _sequence_bits(packet)
    assert sn == link.nesn
    if acknowledged_sn is not None:
        assert nesn == acknowledged_sn ^ 1, 'IUT did not acknowledge the DATA PDU'
    link._accept(packet)
    link.last_received = packet
    return packet


ARQ_CENTRAL_TIMEOUT = 90

# LL.TS.p28 ARQ procedures repeat fifteen exchanges; Central uses 900 ms.
ARQ_ROUNDS = 15


async def _acknowledgement_scheme(tester, *, central=False):
    """Exercise fifteen normal sequence transitions with exact payload and HCI
    checks."""
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
        timeout=ARQ_CENTRAL_TIMEOUT if central else 100,
    )
    receiver = _HciDataStream(tester, link.handle) if central else None
    for _ in range(ARQ_ROUNDS):
        if central:
            data = ARQ_PAYLOAD * 4
            receiver.sent(data, False)
            await host_send(tester, link, data, peer_data=data, receiver=receiver)
        else:
            expected = link.nesn
            packet = await link.exchange(check_ack=True)
            assert _sequence_bits(packet)[0] == expected
            assert packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
            assert len(packet.pdu) == PDU_HEADER_SIZE and packet.pdu[1] == 0
    if receiver is not None:
        await receiver.finish()
    await link.disconnect()


async def _retransmissions(tester, *, central=False, mode='crc', latency=False):
    """Verify the prescribed CRC, negative-ACK, repeated-ACK or lost-NAK sequence."""
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
        timeout=(
            LL_SUPERVISION_TIMEOUT_MAX
            if latency
            else ARQ_CENTRAL_TIMEOUT
            if central
            else 100
        ),
        latency=1 if latency else 0,
    )
    if central:
        await tester.wait(0)
        tester.send(
            bytes(
                hci.HCI_AclDataPacket(
                    link.handle, hci.HCI_ACL_PB_FIRST_NON_FLUSHABLE, 0, 1, ARQ_PAYLOAD
                )
            )
        )
    peer_sn = link.sn if not central else link.sn ^ 1
    held_sn = _sequence_bits(link.last_received)[0] if not central else None
    expected_nesn = peer_sn
    previous = None
    rounds = ARQ_ROUNDS + int(
        (mode == 'duplicate' and not central) or (mode == 'lost' and central)
    )
    for index in range(rounds):
        crc_bad = mode == 'crc' or (mode == 'lost' and index == 0)
        duplicate_sn = peer_sn
        if mode == 'lost' and index:
            duplicate_sn ^= 1

        def build(packet, sn=duplicate_sn):
            nonlocal held_sn
            if packet is not None and (held_sn is None or mode == 'duplicate'):
                held_sn = _sequence_bits(packet)[0]
            nesn = held_sn ^ 1 if mode in ('crc', 'duplicate') else held_sn
            payload = ARQ_PAYLOAD if mode in ('duplicate', 'lost') else b''
            return _data_pdu(
                sn, nesn, payload, llid=LlId.START if payload else LlId.CONTINUATION
            )

        packet, sent = await _wire_event(
            link, build, bad_crc=crc_bad, silent=latency and index % 2 == 1
        )
        sn, nesn = _sequence_bits(packet)
        if central:
            assert packet.pdu[0] & PDU_DATA_LLID_MASK == (
                LlId.START if packet.pdu[1] else LlId.CONTINUATION
            )
        if not central:
            assert packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
            assert len(packet.pdu) == PDU_HEADER_SIZE and packet.pdu[1] == 0
            if previous is None:
                assert sn == held_sn ^ (mode == 'duplicate')
        if previous is not None:
            expected_sn = _sequence_bits(previous)[0] ^ (mode == 'duplicate')
            assert sn == expected_sn
            if central:
                assert nesn == expected_nesn
        if central and (mode in ('crc', 'nak', 'lost') or index == 0):
            assert packet.pdu[PDU_HEADER_SIZE:] == ARQ_PAYLOAD
        if mode in ('crc', 'nak', 'lost') and previous is not None:
            assert packet.pdu[PDU_HEADER_SIZE:] == previous.pdu[PDU_HEADER_SIZE:]
        if sent is not None and (not crc_bad):
            expected_nesn = peer_sn ^ 1 if mode != 'lost' else peer_sn
            if mode == 'nak':
                peer_sn ^= 1
        if not central:
            assert nesn == expected_nesn
        if mode == 'duplicate':
            held_sn = sn
        previous = packet
    acl = [event for event in tester.events if isinstance(event, hci.HCI_AclDataPacket)]
    if mode == 'duplicate':
        assert len(acl) == 1 and acl[0].connection_handle == link.handle
        assert (
            acl[0].data == ARQ_PAYLOAD
            and acl[0].pb_flag == hci.HCI_ACL_PB_FIRST_FLUSHABLE
        )
    else:
        assert not acl
    if central and mode != 'duplicate':
        assert not any(
            isinstance(event, hci.HCI_Number_Of_Completed_Packets_Event)
            for event in tester.events
        )

    def recover(packet):
        sn, nesn = _sequence_bits(packet or previous)
        return _data_pdu(nesn, sn ^ 1)

    packet, sent = await _wire_event(link, recover)
    if central:
        sn, nesn = _sequence_bits(packet)
        assert packet.pdu[0] & PDU_DATA_LLID_MASK == (
            LlId.START if packet.pdu[1] else LlId.CONTINUATION
        )
        assert sn == _sequence_bits(previous)[0] ^ (mode == 'duplicate')
        assert nesn == expected_nesn
        if mode != 'duplicate':
            assert packet.pdu[PDU_HEADER_SIZE:] == ARQ_PAYLOAD
    _restore_peer(link, packet, sent)
    await link.exchange()
    if central:
        completed = [
            event
            for event in tester.events
            if isinstance(event, hci.HCI_Number_Of_Completed_Packets_Event)
        ]
        assert sum(sum(event.num_completed_packets) for event in completed) == 1
        assert all(event.connection_handles == [link.handle] for event in completed)
    await link.disconnect(local=central or mode not in ('nak', 'duplicate'))


async def _supervision(tester, *, central=False):
    """Verify maximum and minimum supervision configurations without valid peer DATA."""
    for interval, timeout, resolution in (
        (LL_CONN_INTERVAL_MAX, LL_SUPERVISION_TIMEOUT_MAX, 10000),
        (LL_CONN_INTERVAL_MIN, LL_SUPERVISION_TIMEOUT_MIN, HCI_OBSERVATION_STEP_US),
    ):
        link = await establish(
            tester,
            central=central,
            interval=interval,
            timeout=timeout,
            peer_address=(
                LOWER_PERIPHERAL_PUBLIC_ADDRESS
                if central
                else LOWER_CENTRAL_PUBLIC_ADDRESS
            ),
        )
        # PHY validates the last occupied microsecond; packet end is exclusive.
        deadline = link.last_sent_end_us - 1 + timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
        event = await _observe_disconnection(
            tester,
            deadline if central else deadline - resolution,
            deadline + (HCI_OBSERVATION_STEP_US if central else resolution),
        )
        assert (event.status, event.connection_handle, event.reason) == (
            ErrorCode.SUCCESS,
            link.handle,
            ErrorCode.CONNECTION_TIMEOUT_ERROR,
        )
        if central:
            await tester.wait(max(0, link.anchor - tester.radio.now_us))
            assert (
                await tester.receive(
                    link.channel,
                    duration_us=interval * LL_CONN_INTERVAL_UNIT_US,
                    **link.radio_parameters,
                )
                is None
            )


async def _timeouts_termination(tester, *, central=False):
    """Keep peer DATA valid but withhold the termination ACK until T_Terminate
    expires."""
    parameters = _advertiser_connection_parameters() if central else LinkParameters()
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
        interval=parameters.interval,
        timeout=parameters.timeout,
    )
    reason = ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    before = tester.radio.now_us
    await tester.command(hci.HCI_Disconnect_Command(link.handle, reason))
    timeout_us = link.parameters.timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
    held_sn = None
    peer_sn = link.sn
    first_transmission = None
    repeats = 0
    earliest = before + timeout_us - HCI_OBSERVATION_STEP_US
    while link.anchor + HCI_OBSERVATION_STEP_US < earliest:

        def build(received):
            nonlocal held_sn, peer_sn
            if received is not None:
                sn, nesn = _sequence_bits(received)
                peer_sn = nesn
                if held_sn is None:
                    held_sn = sn
            nesn = (
                held_sn
                if held_sn is not None
                else _sequence_bits(link.last_received)[0] ^ 1
            )
            return _data_pdu(peer_sn, nesn)

        packet, _sent = await _wire_event(link, build)
        if first_transmission is None:
            first_transmission = packet.start_us
        sn, nesn = _sequence_bits(packet)
        if not central:
            peer_sn = nesn
            if held_sn is None:
                held_sn = sn
        assert packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
        assert packet.pdu[PDU_HEADER_SIZE:] == bytes([Opcode.LL_TERMINATE_IND, reason])
        assert sn == held_sn
        assert _disconnected(tester) is None
        repeats += 1
    assert repeats > 1
    event = await _observe_disconnection(
        tester,
        earliest,
        first_transmission + timeout_us + HCI_OBSERVATION_STEP_US,
    )
    assert event.status == ErrorCode.SUCCESS and event.connection_handle == link.handle
    assert event.reason in (
        ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR,
        ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR,
    )
    await _check_rf_stopped(link)


async def _version_timeout(tester, *, central=False):
    """Withhold peer version information while maintaining the ACL until transaction
    expiry."""
    setup_before = tester.radio.now_us
    parameters = _advertiser_connection_parameters() if central else LinkParameters()
    link = await _version_link(
        tester,
        central=central,
        interval=parameters.interval,
        timeout=parameters.timeout,
    )
    response = _version_payload(link.last_received)
    requested = response is None
    if requested:
        before = tester.radio.now_us
        await tester.command(
            hci.HCI_Read_Remote_Version_Information_Command(link.handle)
        )
        response = await link.control(Opcode.LL_VERSION_IND)
    else:
        before = setup_before
    first_transmission = link.last_received.start_us
    assert len(response) == PDU_CONTROL_VERSION_SIZE
    if not central:
        await link.exchange()
    earliest = before + LL_RESPONSE_TIMEOUT_US - HCI_OBSERVATION_STEP_US
    latest = first_transmission + LL_RESPONSE_TIMEOUT_US + HCI_OBSERVATION_STEP_US
    while link.anchor + HCI_OBSERVATION_STEP_US < earliest:
        await _quiet_events(link, 1, no_completion=True)
    while tester.radio.now_us <= latest:
        terminal = [
            event
            for event in tester.events
            if isinstance(
                event,
                (
                    hci.HCI_Read_Remote_Version_Information_Complete_Event,
                    hci.HCI_Disconnection_Complete_Event,
                ),
            )
        ]
        if terminal:
            assert tester.radio.now_us >= earliest, 'premature version timeout'
            for event in terminal:
                if isinstance(
                    event, hci.HCI_Read_Remote_Version_Information_Complete_Event
                ):
                    assert requested, (
                        'unsolicited Host completion for autonomous version'
                    )
                    _check_completion(
                        event,
                        link.handle,
                        status=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR,
                    )
            if any(
                isinstance(event, hci.HCI_Disconnection_Complete_Event)
                for event in terminal
            ):
                assert len(terminal) <= 2
                event = terminal[-1]
                assert isinstance(event, hci.HCI_Disconnection_Complete_Event), (
                    'version completion followed disconnection'
                )
                assert (event.status, event.connection_handle, event.reason) == (
                    ErrorCode.SUCCESS,
                    link.handle,
                    ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR,
                )
                for event in terminal:
                    tester.events.remove(event)
                break
        if tester.radio.now_us == latest:
            raise AssertionError(
                'Version Exchange did not expire at the transaction deadline'
            )
        target = min(tester.radio.now_us + HCI_OBSERVATION_STEP_US, latest)
        if tester.radio.now_us < earliest - 1:
            target = min(target, earliest - 1)
        await tester.wait(target - tester.radio.now_us)
    else:
        raise AssertionError('Version Exchange observation began after its deadline')
    await _check_rf_stopped(link)
    assert _completion(tester) is None


async def peripheral_sending_data(tester):
    """Peripheral Sending Data.

    Initial Condition:
        An unencrypted connected public Peripheral, zero latency, and common
        connection interval, timeout and channel parameters. Read the LE buffer
        size preamble and retain the established handle and data_packet_length.
        The Lower Tester acts as Central and supplies correctly sequenced ACKs.

    Test Steps:
        1. Submit ten-octet zero-filled Host ACL packets with PB=0x00 until
           1000 octets are submitted; observe matching-handle Number Of Completed
           Packets events and respect the advertised HCI packet credits.
        2. Receive the corresponding DATA PDU with LLID=0x02 and exact payload.
        3. Send an empty Central DATA PDU once per connection interval using
           the common channel parameters and the DATA acknowledgement scheme.
        4. Repeat Steps 2-3 until all Step 1 data has been received and credited.
        5. Submit another 1000 zero-filled octets in ten-octet Host ACL packets,
           now all PB=0x01; observe the corresponding HCI completion credits.
        6. Receive each packet as LLID=0x01 and acknowledge with an empty Central
           DATA PDU using the same channel and sequence rules.
        7. Repeat Step 6 until every Step 5 payload has been received.
        8. Submit another 1000 octets in ten-octet packets, with PB=0x00 for the
           first packet and PB=0x01 for subsequent packets; track HCI credits.
        9. Require LLID=0x02 for the first payload and LLID=0x01 thereafter;
           acknowledge every packet with correctly sequenced empty DATA.
        10. Repeat Step 9 until all Step 8 data is received and credited.
        11. If data_packet_length <= 27, skip Steps 12-13 and proceed to Step 14.
        12. Otherwise, submit zero-filled Host packets with PB=0x00 and seeded
            random lengths from 28 through data_packet_length until at least
            1000 octets have been submitted.
        13. For each Step 12 packet, require at least two DATA fragments: first
            LLID=0x02, then LLID=0x01. Reassemble the exact Host payload and
            acknowledge fragments using the common sequence/channel parameters.
        14. Execute Peripheral Connection Terminated for the established handle
            and require successful HCI termination completion.

    Expected Outcome:
        Pass verdict:
            All submitted octets arrive unchanged, each Host packet is credited
            once, and LLID fragmentation boundaries follow Section 4.1.7.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-04-C procedure in
        Section 4.3.5, Figure 4.3-48; PDF 593-596, printed 4.3-120 through 4.3-123.
    """
    await _data_exchange(tester, central=False, direction='tx')


# Verify Peripheral Host-to-air DATA delivery, HCI flow control, and preservation of
# message boundaries across Link Layer fragmentation.
PROCEDURES |= procedure_cases(
    peripheral_sending_data,
    {
        'LL/CON/PER/BV-04-C': (
            'Peripheral Sending Data',
            {},
        ),
    },
)


async def peripheral_receiving_data(tester):
    """Peripheral Receiving Data.

    Initial Condition:
        An unencrypted connected public Peripheral with common connection,
        channel and timeout parameters. Retain the preamble connection handle
        and buffer size. The Lower Tester acts as Central with independent ACK
        state and deterministic seeded random payloads.

    Test Steps:
        1. Configure 100 DATA packets, each with LLID=0x02 and ten random octets.
        2. Send one DATA packet per connection interval using the common channel
           parameters and acknowledgement scheme; receive the IUT response.
           Repeat Step 2 until all Step 1 payloads have been reported.
        3. Require HCI ACL Data with the exact Step 1 payload and PB=0x02.
        4. Configure another 100 ten-octet random DATA packets, all LLID=0x01.
        5. Send the Step 4 packets once per interval with correct sequence/channel
           parameters, repeating until every payload has been reported.
        6. Require the exact Step 5 payloads in HCI ACL Data with PB=0x01.
        7. Configure 100 further ten-octet random packets: first LLID=0x02,
           subsequent LLID=0x01.
        8. Send these packets once per interval, following the acknowledgement
           scheme, until all Step 7 payloads have been reported.
        9. Require the exact payload sequence in HCI ACL Data, first PB=0x02
           and subsequent PB=0x01, applying the Source Correction below.
        10. Configure ten three-octet random DATA packets with LLID=0x02; repeat
            their transfer until all ten have been acknowledged by the IUT.
        11. Send each Step 10 payload once per interval with LLID=0x02 and
            correct sequence/channel parameters; receive the IUT DATA response.
        12. Require one HCI ACL Data packet for each Step 11 payload, exact bytes
            and handle, with PB=0b10.

    Expected Outcome:
        Pass verdict:
            The IUT acknowledges all transmitted data and reports every payload
            unchanged, using the HCI fragmentation flags in Section 4.1.7.

    Source Correction:
        Step 9 says continuation PB=0x00; use PB=0x01 per Core 6.3 Vol 4,
        Part E, Section 5.4.2 and source Step 6. Amber confirms PB=0x00 is not
        a Controller-to-Host LE continuation flag. Step 10's reference to packets
        in Step 9 identifies HCI reports, not transmit packets; apply it to the
        ten packets configured in Step 10 and transmitted in Step 11. Step 11's
        payload reference to itself is Step 10. These are local interpretations,
        not published SIG errata.

    Coverage Note:
        Check HCI length/broadcast flags and DATA ACKs for every transfer, then
        disconnect through HCI after the normative twelve-step procedure.
        Section 4.1.7 compares the byte stream and start-flag locations;
        it does not require HCI and Link Layer packetization to coincide.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-05-C procedure in
        Section 4.3.5, Figure 4.3-49; PDF 596-598, printed 4.3-123 through 4.3-125.
    """
    await _data_exchange(tester, central=False, direction='rx')


# Verify that Peripheral DATA reception preserves payloads and upper-layer message
# boundaries when reporting HCI ACL packets to its Host.
PROCEDURES |= procedure_cases(
    peripheral_receiving_data,
    {
        'LL/CON/PER/BV-05-C': (
            'Peripheral Receiving Data',
            {},
        ),
    },
)


async def peripheral_sending_and_receiving_data(tester):
    """Peripheral Sending and Receiving Data.

    Initial Condition:
        An unencrypted connected public Peripheral with common interval, latency,
        timeout and channel parameters. Read the buffer size preamble and retain
        the connection handle. Transfer at least 1000 octets per direction and
        more octets than the IUT data_packet_length.

    Test Steps:
        1. Submit ten-octet zero-filled Host ACL packets with PB=0x00 until
           1000 octets have been submitted for Peripheral transmission.
        2. Configure 100 Lower Tester DATA packets, each LLID=0x02 with ten
           deterministic seeded random payload octets.
        3. Send Step 2 packets once per connection interval using the common
           channel parameters and independent DATA acknowledgement scheme.
        4. Receive the IUT DATA response with LLID=0x02 and exact Step 1 data.
           Repeat Steps 3-4 until both directions' data is received/reported.
        5. Require HCI ACL Data reports with the exact Step 2 payload and
           PB=0x02, repeating until every Lower Tester payload is reported.

    Expected Outcome:
        Pass verdict:
            All transmitted data is acknowledged, every received payload is
            reported unchanged, and LLID/HCI boundaries preserve each message
            as specified in Section 4.1.7.

    Source Correction:
        Step 5 ends with all data sent in Step 1 being reported, but its HCI
        reports carry the Lower Tester data configured in Step 2. Apply the
        completion condition to Step 2; the two directions are checked separately.

    Coverage Note:
        Submit and verify one ten-octet message per direction in each of 100
        exchanges, checking HCI credits and ACKs before reusing a buffer. This
        exceeds the reported 251-octet HCI buffer size. Disconnect after all data.
        Compare received bytes and start-flag locations under Section 4.1.7,
        allowing different HCI packetization of the same message stream.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-06-C procedure in
        Section 4.3.5, Figure 4.3-50; PDF 599-600, printed 4.3-126 through 4.3-127.
    """
    await _data_exchange(tester, central=False, direction='duplex')


# Verify simultaneous Peripheral transmission and reception, preserving payloads,
# acknowledgements and HCI/LL fragmentation boundaries.
PROCEDURES |= procedure_cases(
    peripheral_sending_and_receiving_data,
    {
        'LL/CON/PER/BV-06-C': (
            'Peripheral Sending and Receiving Data',
            {},
        ),
    },
)


async def peripheral_sending_termination(tester):
    """Peripheral Sending Termination.

    Initial Condition:
        An unencrypted connected public Peripheral with common interval/channel
        parameters, zero latency, and a 100-ms supervision timeout.
        The Lower Tester acts as Central and retains the established handle.

    Test Steps:
        1. Send HCI Disconnect for that handle and require successful Command
           Status. Select Remote User Terminated Connection as the reason.
        2. Receive LL_TERMINATE_IND in response to a Central DATA transmission,
           validate the requested reason, and acknowledge with empty Central
           DATA in the following connection event.
        3. Continue empty Central DATA transmissions for one full supervision
           timeout; require no further Peripheral DATA or termination responses.
        4. Interleaved with Step 3, require successful Disconnection Complete
           for the handle, indicating termination requested by the local Host.

    Expected Outcome:
        Pass verdict:
            The IUT transmits termination until acknowledged, stops maintaining
            the ACL after acknowledgement, and reports termination through HCI.
            It must stop retransmitting termination when T_Terminate expires.

    Coverage Note:
        This procedure acknowledges termination at the first opportunity.
        The separate timeout procedure exercises the unacknowledged path.
        Check HCI status, handle, local reason, and absence of duplicate events.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-11-C procedure in
        Section 4.3.5, Figure 4.3-53; PDF 604-605, printed 4.3-131 through 4.3-132.
    """
    await _connection_termination(tester, central=False, local=True)


# Verify Peripheral-initiated connection termination, acknowledgement by the Lower
# Central, cessation of DATA, and Host termination notification.
PROCEDURES |= procedure_cases(
    peripheral_sending_termination,
    {
        'LL/CON/PER/BV-11-C': (
            'Peripheral Sending Termination',
            {},
        ),
    },
)


async def peripheral_accepting_termination(tester):
    """Peripheral Accepting Termination.

    Initial Condition:
        An unencrypted connected public Peripheral with common interval/channel
        parameters, zero latency, and a 100-ms supervision timeout.
        The Lower Tester acts as Central and retains the established handle.

    Test Steps:
        1. Send LL_TERMINATE_IND with the selected peer reason. Require an empty
           Peripheral DATA response at T_IFS with NESN acknowledging the request.
        2. Continue empty Central DATA transmissions for one full supervision
           timeout; require no further Peripheral responses.
        3. Interleaved with Step 2, require successful Disconnection Complete
           with the established handle and the reason requested by the peer.

    Expected Outcome:
        Pass verdict:
            The IUT acknowledges termination before T_Terminate expires, stops
            maintaining the connection after that acknowledgement, and reports
            termination through HCI.

    Coverage Note:
        Select Remote User Terminated Connection as the peer reason and reject
        duplicate HCI disconnection events throughout the observation window.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-12-C procedure in
        Section 4.3.5, Figure 4.3-54; PDF 605-606, printed 4.3-132 through 4.3-133.
    """
    await _connection_termination(tester, central=False, local=False)


# Verify that a Peripheral acknowledges a Central termination request, ceases ACL
# maintenance, and reports the peer-requested disconnection.
PROCEDURES |= procedure_cases(
    peripheral_accepting_termination,
    {
        'LL/CON/PER/BV-12-C': (
            'Peripheral Accepting Termination',
            {},
        ),
    },
)


async def peripheral_supervision_timer(tester):
    """Peripheral Supervision Timer.

    Initial Condition:
        An unencrypted connected public Peripheral with selected interval and
        timeout, zero latency, and common channel parameters.
        The Lower Tester acts as Central; use the common connection preamble.

    Test Steps:
        1. Establish the connection using the maximum supported interval (4 s)
           and supervision timeout (32 s); verify the reported HCI parameters.
        2. Stop Central maintenance after a known valid DATA event. Measure the
           supervision deadline from the last valid peer packet, not Host setup.
        3. Require Disconnection Complete for the same handle with successful
           status and Connection Timeout reason, after the last event before
           expiry and at the selected supervision deadline.
        4. Establish a new connection using the minimum supported interval
           (7.5 ms) and supervision timeout (100 ms).
        5. Repeat Steps 2-3 using the minimum settings.

    Expected Outcome:
        Pass verdict:
            HCI reports disconnection at the selected connSupervisionTimeout
            after the final valid event for both maximum and minimum settings.

    Coverage Note:
        Use simulation time, with 10-ms measurement resolution for maximum
        timeout and 1-ms resolution for minimum timeout as specified by the
        source notes. Reject early or late events outside that resolution.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-13-C procedure in
        Section 4.3.5, Figure 4.3-55; PDF 606-607, printed 4.3-133 through 4.3-134.
    """
    await _supervision(tester, central=False)


# Verify that the Peripheral terminates an unmaintained ACL on supervision timeout and
# reports the correct handle and timeout reason to its Host.
PROCEDURES |= procedure_cases(
    peripheral_supervision_timer,
    {
        'LL/CON/PER/BV-13-C': (
            'Peripheral Supervision Timer',
            {},
        ),
    },
)


async def peripheral_retransmission_request(tester):
    """Peripheral Retransmission Request.

    Initial Condition:
        An unencrypted connected public Peripheral with common interval, latency,
        timeout and channel parameters. The Lower Tester acts as Central and
        retains current/next SN and NESN from the established connection.

    Test Steps:
        1. Repeat Steps 1-2 fifteen times. Send one DATA PDU per interval using
           the common channel selection, inverting all CRC bits. Advance the
           peer SN/NESN only as permitted by the preceding valid IUT ACK state.
        2. Receive the Peripheral response at T_IFS in required listening events.
           Require SN matching current peer NESN and NESN matching current peer
           SN, indicating that the corrupt packet was not accepted.
        3. Execute Peripheral Connection Terminated using the established handle
           and parameters, after restoring a valid acknowledged peer exchange.

    Expected Outcome:
        Pass verdict:
            Every corrupt packet elicits a negative acknowledgement: IUT SN/NESN
            remain unchanged and invalid DATA is not delivered to the Host.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-15-C procedure in
        Section 4.3.5, Figure 4.3-57; PDF 608-609, printed 4.3-135 through 4.3-136.
    """
    await _retransmissions(tester, central=False, mode='crc', latency=False)


# Verify that a Peripheral retains the DATA acknowledgement state when receiving packets
# with invalid CRC and continues to request retransmission.
PROCEDURES |= procedure_cases(
    peripheral_retransmission_request,
    {
        'LL/CON/PER/BV-15-C': (
            'Peripheral Retransmission Request',
            {},
        ),
    },
)


async def peripheral_retransmission(tester):
    """Peripheral Retransmission.

    Initial Condition:
        An unencrypted connected public Peripheral with common interval, latency,
        timeout and channel parameters. The Lower Central retains independent
        current/next SN/NESN state; the outstanding IUT packet is empty DATA.

    Test Steps:
        1. Repeat Steps 1-2 fifteen times. Send one empty DATA PDU per interval
           on the common selected channel. Advance SN when acknowledged, but
           keep NESN equal to the IUT SN, deliberately withholding its ACK.
        2. Receive the Peripheral response at T_IFS. Require the same IUT SN
           and payload, with NESN acknowledging the newly accepted Central SN.
        3. The Lower Tester terminates the connection through LL_TERMINATE_IND
           after restoring normal peer sequence state.

    Expected Outcome:
        Pass verdict:
            The IUT retransmits identical payload and SN for each negative ACK
            while still acknowledging new valid Central packets.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-16-C procedure in
        Section 4.3.5, Figure 4.3-58; PDF 609-610, printed 4.3-136 through 4.3-137.
    """
    await _retransmissions(tester, central=False, mode='nak', latency=False)


# Verify that a Peripheral retransmits its outstanding DATA with unchanged sequence
# number and payload when the Central withholds acknowledgement.
PROCEDURES |= procedure_cases(
    peripheral_retransmission,
    {
        'LL/CON/PER/BV-16-C': (
            'Peripheral Retransmission',
            {},
        ),
    },
)


async def peripheral_acknowledgement_repetition(tester):
    """Peripheral Acknowledgement Repetition.

    Initial Condition:
        An unencrypted connected public Peripheral with common interval, latency,
        timeout and channel parameters. The Lower Central retains current/next
        SN/NESN and uses one 0xFF payload octet as shown in the source MSC.

    Test Steps:
        1. Send DATA with the current Central SN. Ignore positive ACKs for that
           payload, but correctly acknowledge received IUT DATA by advancing NESN.
        2. Require the IUT acknowledgement with NESN equal to next Central SN.
        3. Repeat Steps 3-4 fifteen times. Resend the same Central DATA with
           unchanged SN and exact payload, pretending its ACK was not received.
        4. Require repeated positive ACK: IUT NESN remains next Central SN and
           IUT SN advances because its own previous DATA was acknowledged.
        5. The Lower Tester terminates the connection through LL_TERMINATE_IND
           after restoring normal peer sequence state.

    Expected Outcome:
        Pass verdict:
            The IUT acknowledges every duplicate, advances its own acknowledged
            sequence correctly, and delivers the duplicate payload to its Host
            exactly once.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-17-C procedure in
        Section 4.3.5, Figure 4.3-59; PDF 610-611, printed 4.3-137 through 4.3-138.
    """
    await _retransmissions(tester, central=False, mode='duplicate', latency=False)


# Verify that a Peripheral repeats a positive acknowledgement when the Central
# retransmits the same DATA payload and sequence number.
PROCEDURES |= procedure_cases(
    peripheral_acknowledgement_repetition,
    {
        'LL/CON/PER/BV-17-C': (
            'Peripheral Acknowledgement Repetition',
            {},
        ),
    },
)


async def peripheral_lost_negative_acknowledgement(tester):
    """Peripheral Lost Negative Acknowledgement.

    Initial Condition:
        An unencrypted connected public Peripheral with common interval, latency,
        timeout and channel parameters. The Lower Central retains independent
        current/next SN/NESN from the established connection.

    Test Steps:
        1. Repeat Steps 1-2 fifteen times. First send the next Central DATA with
           invalid CRC. After the resulting negative ACK, incorrectly advance
           Central SN and send valid-CRC DATA. Keep Central NESN equal to the
           IUT SN, negatively acknowledging the IUT's own DATA throughout.
        2. Receive the IUT response at T_IFS on the selected data channel.
           Require repeated unchanged IUT payload and SN matching current peer
           NESN; IUT NESN remains the current peer SN, rejecting the sequence jump.
        3. Execute Peripheral Connection Terminated with the established handle
           and parameters after restoring a valid acknowledged exchange.

    Expected Outcome:
        Pass verdict:
            The IUT repeats a negative ACK when the next payload is sent after
            the lost negative ACK, and does not deliver invalid/duplicate data.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-18-C procedure in
        Section 4.3.5, Figure 4.3-60; PDF 612-613, printed 4.3-139 through 4.3-140.
    """
    await _retransmissions(tester, central=False, mode='lost', latency=False)


# Verify that a Peripheral preserves DATA sequence state when the Central mistakes a
# negative acknowledgement for a positive one.
PROCEDURES |= procedure_cases(
    peripheral_lost_negative_acknowledgement,
    {
        'LL/CON/PER/BV-18-C': (
            'Peripheral Lost Negative Acknowledgement',
            {},
        ),
    },
)


async def peripheral_acknowledgement_scheme(tester):
    """Peripheral Acknowledgement Scheme.

    Initial Condition:
        An unencrypted connected public Peripheral with common interval, latency,
        timeout and channel parameters. The Lower Central retains independent
        current/next SN and NESN from the connection preamble.

    Test Steps:
        1. Repeat Steps 1-2 fifteen times. Send one DATA PDU per interval on the
           selected data channel. Advance SN only after its positive ACK; advance
           NESN when a correctly received IUT SN matches current peer NESN.
        2. In required listening events, receive the Peripheral DATA at T_IFS.
           Require SN matching current peer NESN and NESN matching next peer SN.
        3. Execute Peripheral Connection Terminated for the established handle
           and connection parameters.

    Expected Outcome:
        Pass verdict:
            The normal acknowledgement scheme executes correctly and the IUT
            acknowledges at least ten of the fifteen Central packets.

    Coverage Note:
        Select empty DATA and inject no RF errors. Require all fifteen correctly
        timed empty replies in this ideal channel, exceeding the source minimum.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-21-C procedure in
        Section 4.3.5, Figure 4.3-63; PDF 615-616, printed 4.3-142 through 4.3-143.
    """
    await _acknowledgement_scheme(tester, central=False)


# Verify normal DATA acknowledgement and sequence-state transitions in a maintained
# Peripheral connection.
PROCEDURES |= procedure_cases(
    peripheral_acknowledgement_scheme,
    {
        'LL/CON/PER/BV-21-C': (
            'Peripheral Acknowledgement Scheme',
            {},
        ),
    },
)


async def peripheral_receiving_data_le_coded_ci_change(tester):
    """Peripheral Receiving Data, LE Coded, CI Change.

    Initial Condition:
        An unencrypted connected Peripheral with public address, maximum supported
        interval (4 s), common latency/channel parameters and selected SCA.
        Select zero latency and a valid 32-s supervision timeout; retain the
        preamble handle, HCI buffer information and maximum data-length values.

    Test Steps:
        1. Send HCI LE Set PHY with ALL_PHYS=0x03 and PHY_options=0x0000;
           require successful Command Status.
        2. Require PHY Update Complete with the handle and both PHYs=LE 1M.
        3. Send Central LL_PHY_REQ preferring LE Coded in both TX/RX fields.
        4. Receive nonzero LL_PHY_RSP fields containing LE Coded in both
           directions; otherwise report Inconclusive.
        5. Send LL_PHY_UPDATE_IND selecting LE Coded both ways and Instant more
           than six and fewer than 32767 events ahead. Complete the update and
           require HCI completion with both directional PHYs=LE Coded.
        6. If supported TX maxima exceed 27 octets or 328 us, send HCI LE Set
           Data Length with those maxima and require Command Complete;
           otherwise proceed to Step 8.
        7. Answer an IUT DLE or initiate peer DLE if absent. The Lower Tester's
           TX octets/time use IUT RX maxima; its RX values use IUT TX maxima.
           Validate the negotiated fields and eligible HCI data-length changes.
        8. Configure 288 DATA packets at the effective Lower Tester TX/IUT RX
           maximum. Increment payload octets continuously from 0x00 across all
           packet boundaries, wrapping after 0xFF. Use the exact eighteen-row,
           sixteen-entry-per-row CODING_PATTERN from source Step 8.
        9. Send DATA using the common channel selection and acknowledgement
           scheme. Require one empty, positive Peripheral ACK for every packet
           at T_IFS=150 +/-2 us, for both S=2 and S=8 coding.
        10. Require HCI ACL Data for the transmitted payloads, with the correct
            handle, boundary/broadcast flags, length and continuous byte sequence.
        11. Repeat Steps 8-10's transfer/reporting until all 288 configured
            packets have been acknowledged and reported, without duplicates.
        12. Repeat the complete Steps 8-11 exchange at the minimum 27-octet
            payload, restarting the payload sequence at 0x00.

    Expected Outcome:
        Pass verdict:
            Every DATA packet is acknowledged, every payload is reported intact
            and in order through HCI, and all responses satisfy 150 +/-2 us T_IFS.

    Source Correction:
        Step 3 names ALL_PHYS on LL_PHY_REQ; that field belongs to the HCI
        command, not this wire PDU. Use TX_PHYS/RX_PHYS for LE Coded.
        Step 8's pattern numbers 2/8 denote coding factors S=2/S=8, not literal
        two-bit CI values: Core 6.3 Vol 6, Part B, Section 2.2.3, Table 2.2 maps
        CI=01 to S=2 and CI=00 to S=8.

    Coverage Note:
        Compare all eighteen pattern rows to PDF 676-677. Exchange the full
        sequence within one maximum-interval event using MD, measure each ACK
        against its own packet end, and verify exact physical airtime independently
        of the IUT. Disconnect through HCI after both 288-packet exchanges.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-54-C procedure in
        Section 4.3.5, Figure 4.3-92; PDF 674-677,
        printed 4.3-201 through 4.3-204. Common initial conditions: Section 4.1.6.
    """
    await _coded_reception(tester, central=False)


# Verify Peripheral DATA reception while the Central alternates LE Coded S=2/S=8 rates,
# including every response's 150 +/-2 us T_IFS and exact Host delivery at the effective
# maximum and minimum payload sizes.
PROCEDURES |= procedure_cases(
    peripheral_receiving_data_le_coded_ci_change,
    {
        'LL/CON/PER/BV-54-C': (
            'Peripheral Receiving Data, LE Coded, CI Change',
            {},
        ),
    },
)


async def invalid_crc_anchor_point(tester):
    """Invalid CRC Anchor Point.

    Initial Condition:
        An unencrypted connected Peripheral with public addresses, common
        channel map, channel selection, interval and Peripheral latency, and
        selected SCA. The Lower Tester acts as Central. Select a 10-ms interval,
        zero latency and the prescribed 3000-ms supervision timeout; preserve
        current/next SN and NESN from the last valid connection exchange.

    Test Steps:
        1. Once per connection interval, send a DATA packet with an invalid
           CRC on the scheduled data channel, using the common channel
           selection and the saved sequence bits.
        2. Receive Peripheral DATA at T_IFS in events where it must listen;
           allow mismatches attributable to incorrectly received packets.
        3. Repeat Steps 1-2 for longer than the supervision timeout. Measure
           300 connection events, then continue observing through expiry.
        4. Require Disconnection Complete with the original connection handle
           and Connection Timeout (0x08).

    Expected Outcome:
        Pass verdict:
            The selected parameters are used, at least 65 of the measured
            300 connection events contain Peripheral responses, and HCI
            reports connection timeout after only invalid-CRC Central traffic.

    Coverage Note:
        The source assumes BER <=0.1%; the selected ideal RF channel has zero
        background BER. Its preamble/access-address error model follows PER
        BV-10, with the required response rate divided by latency plus one.
        Deliberately invert all CRC bits without corrupting the preamble or
        access address. Require all received replies to be CRC-valid, correctly
        timed and preserve the saved sequence acknowledgement. Invalid-CRC DATA
        must not produce Host ACL Data. HCI disconnection status must be success.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BI-01-C procedure in
        Section 4.3.5, Figure 4.3-105; PDF 699-700,
        printed 4.3-226 through 4.3-227. Steps 1-4, Test Condition, outcome
        and Notes; referenced Core Vol 6, Part B, Section 4.5.5.
    """
    link = await establish(
        tester,
        central=False,
        peer_address=LOWER_CENTRAL_PUBLIC_ADDRESS,
        interval=8,
        timeout=300,
    )
    sn, nesn = _sequence_bits(link.last_received)
    peer_sn, peer_nesn = (link.sn, link.nesn)
    responses = 0
    started = tester.radio.now_us
    for _ in range(300):
        packet, _ = await _wire_event(
            link, lambda _: _data_pdu(peer_sn, peer_nesn), bad_crc=True, required=False
        )
        if packet:
            assert _sequence_bits(packet) == (sn, nesn)
            responses += 1
    assert responses >= 65
    await tester.wait(
        max(0, started + 3 * PHY_TIME_UNITS_PER_SECOND + 10000 - tester.radio.now_us)
    )
    event = await tester.event(hci.HCI_Disconnection_Complete_Event)
    assert (
        event.status == ErrorCode.SUCCESS
        and event.connection_handle == link.handle
        and event.reason == hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR
    )
    assert not any(isinstance(event, hci.HCI_AclDataPacket) for event in tester.events)


# Verify that a Peripheral uses the start of a Central packet as the event anchor
# despite an invalid CRC, responds where required, but does not treat invalid-CRC DATA
# as valid traffic for supervision refresh.
PROCEDURES |= procedure_cases(
    invalid_crc_anchor_point,
    {
        'LL/CON/PER/BI-01-C': (
            'Invalid CRC Anchor Point',
            {},
        ),
    },
)


async def peripheral_t_terminate_timer(tester):
    """Peripheral T_Terminate Timer.

    Initial Condition:
        An unencrypted connected Peripheral with public addresses, common
        channel map, interval and Peripheral latency, selected timeout and SCA.
        The Lower Tester acts as Central. Retain the connection handle and
        sequence state; use zero latency and common supervision timing.

    Test Steps:
        1. Send HCI Disconnect with the preamble connection handle and require
           successful Command Status.
        2. Transmit Central DATA and receive LL_TERMINATE_IND. Do not
           acknowledge that indication or any subsequent packet, but continue
           Central transmissions.
        3. Send correctly formatted empty DATA without acknowledging any
           Peripheral response; repeat until T_Terminate expires.
        4. Require Disconnection Complete with status Success (0x00), the
           original handle and either Local Host Termination (0x16) or
           LL Response Timeout (0x22).

    Expected Outcome:
        Pass verdict:
            The IUT continues sending LL_TERMINATE_IND until T_Terminate
            expires and reports link termination through the specified HCI event.

    Coverage Note:
        Retain NESN equal to the termination SN so it remains unacknowledged;
        advance Central SN when appropriate so valid peer traffic keeps the
        ordinary supervision timer alive. Require the same termination payload
        and SN on retransmission. Bound expiry from the HCI command's enqueue
        window using the supervision timeout, and verify RF silence afterwards.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BI-02-C procedure in
        Section 4.3.5, Figure 4.3-106; PDF 701-702,
        printed 4.3-228 through 4.3-229. Steps 1-4 and outcome;
        referenced Core Vol 6, Part B, Section 5.1.6.
    """
    await _timeouts_termination(tester, central=False)


# Verify that a Peripheral retransmits an unacknowledged termination indication until
# T_Terminate expires, then reports link termination. Valid Central traffic must not
# indefinitely restart that timer.
PROCEDURES |= procedure_cases(
    peripheral_t_terminate_timer,
    {
        'LL/CON/PER/BI-02-C': (
            'Peripheral T_Terminate Timer',
            {},
        ),
    },
)


async def peripheral_connection_control_timer(tester):
    """Peripheral Connection Control Timer.

    Initial Condition:
        An unencrypted connected Peripheral with public addresses, common
        channel selection, interval and latency, selected timeout and SCA.
        The Lower Tester acts as Central, retains the connection handle and
        continues valid DATA so supervision does not cause the failure.

    Test Steps:
        1. If the IUT has autonomously sent LL_VERSION_IND after connection
           setup, acknowledge it and continue Central transmissions without
           ever sending a peer VERSION_IND. Continue directly to Step 6.
        2. Otherwise, send HCI Read Remote Version Information for the saved
           handle and require successful Command Status.
        3. Receive the IUT's LL_VERSION_IND and positively acknowledge it.
        4. Continue empty Central DATA until the connection control timeout
           or the completion in Step 5; do not provide a peer version response.
        5. For the Host-requested branch, receive Read Remote Version
           Information Complete with the saved handle and LL Response Timeout
           status, unless disconnection precedes procedure completion.
        6. Require Disconnection Complete for the saved handle with LL
           Response Timeout (0x22), indicating the connection transaction timeout.

    Expected Outcome:
        Pass verdict:
            The procedure fails through its control timeout, not a supervision
            timeout. The IUT reports termination to the Host and stops responding
            to Central transmissions.

    Coverage Note:
        Preserve the autonomous path without issuing an extra Host command;
        an autonomous exchange must not produce an unsolicited Host version
        completion. Verify one acknowledged VERSION_IND, no repeats after ACK,
        and no early completion/disconnection before the 40-s response deadline.
        An optional Host failure completion must precede disconnection; no
        version completion may follow it. Probe the next RF receive window.
        Use a 1-ms HCI observation bound around the recorded timer start;
        the autonomous path brackets that start between connection setup and
        the first received RF timestamp, rather than treating TX as queue time.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BI-05-C procedure in
        Section 4.3.5, Figure 4.3-108; PDF 704-706,
        printed 4.3-231 through 4.3-233. Steps 1-6 and Expected Outcome;
        referenced Core Vol 6, Part B, Section 5.2.
    """
    await _version_timeout(tester, central=False)


# Verify recovery from a stalled Version Exchange: keep the ACL alive without returning
# peer version information, then require the connection control timeout and cessation of
# RF maintenance. Inform the Host of its pending procedure failure unless disconnection
# takes precedence.
PROCEDURES |= procedure_cases(
    peripheral_connection_control_timer,
    {
        'LL/CON/PER/BI-05-C': (
            'Peripheral Connection Control Timer',
            {},
        ),
    },
)


async def invalid_llid(tester):
    """Invalid LLID.

    Initial Condition:
        Use the common unencrypted connected Peripheral setup with public
        peers, common channel parameters, zero latency and LE 1M. The Lower
        Tester is Central. Read the declared local LE feature set to validate
        the later legitimate response. The source gives no separate initial
        condition for this independently titled procedure.

    Test Steps:
        1. Send the LL_FEATURE_REQ payload with LLID=0b00 rather than the
           control-packet LLID; retain valid sequence bits and CRC.
        2. Require the IUT to acknowledge it with no more than four retries;
           a missing acknowledgement fails the test.
        3. For four seconds after the first invalid transmission, require
           that no LL_FEATURE_RSP is returned in response to its payload.
        4. After that four-second interval, send the same LL_FEATURE_REQ
           payload with the correct LLID=0b11.
        5. Require LL_FEATURE_RSP with valid control LLID and FeatureSet.

    Expected Outcome:
        Pass verdict:
            The invalid-LLID packet is acknowledged but not processed as a
            Feature Request. The valid packet produces a correctly encoded
            Feature Response with the permitted local features.
        Fail verdict:
            The initial packet remains unacknowledged after four retries.

    Coverage Note:
        Use at most five total transmissions: the initial packet plus four
        retries. Anchor the four-second window to the first RF transmission,
        not the final retry or acknowledgement. Continue correctly sequenced
        empty DATA while checking that the invalid payload does not leak into
        Host ACL data. Validate the legitimate response's feature masks and
        acknowledgement before disconnecting through HCI.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BI-17-C procedure in
        Section 4.3.5, Figure 4.3-123; PDF 729-730,
        printed 4.3-256 through 4.3-257. Steps 1-5 and Expected Outcome;
        referenced Core Vol 6, Part B, Section 2.4.
    """
    await _invalid_control_invalid_llid(tester, central=False)


# Verify that a Peripheral acknowledges a packet with reserved LLID=0 without
# interpreting its payload as a Feature Request, then correctly responds when the same
# feature payload uses the valid control LLID.
PROCEDURES |= procedure_cases(
    invalid_llid,
    {
        'LL/CON/PER/BI-17-C': (
            'Invalid LLID',
            {},
        ),
    },
)


async def listening_for_packet_with_window_widening_acl(tester):
    """Listening for Packet With Window Widening, ACL.

    Initial Condition:
        A connected Peripheral with supported own address type, selected
        channel parameters, timeout and SCA. The Lower Tester is Central and
        advertises 500-ppm sleep clock accuracy. Use public peers, initial LE 1M
        and zero latency. Repeat all steps for both Table 4.3-50 rows:
        round 1, 3-s supervision and the early receive-window extension;
        round 2, 2-s supervision and the late receive-window extension.

    Test Steps:
        1. Send HCI LE Connection Update with zero latency and the round's
           supervision timeout; require successful Command Status.
        2. Receive LL_CONNECTION_PARAM_REQ. Its legal parameters may differ
           from those provided by the Host command.
        3. Accept the request with LL_CONNECTION_UPDATE_IND, Instant fifty
           connection events ahead and WinOffset=1.
        4. Send empty DATA within the receive window, obtaining acknowledgements
           until the connection event counter reaches Instant.
        5. Require an empty IUT DATA reply for each packet received in Step 4.
        6. At the update, send no PDU for ten connection intervals.
        7. Send empty DATA inside the outer widened edge: round 1 at
           receiveWindowStart - widening + 1 us, round 2 at
           receiveWindowEnd + widening - 1 us. Compute the source widening as
           (500 / 1000000) * (receiveWindowEnd - last Step 4 DATA anchor) + 16 us.
        8. Require the IUT to receive that Step 7 packet during the widened
           interval and respond with empty DATA.

    Expected Outcome:
        Pass verdict:
            For both rounds, the IUT receives the correctly positioned Step 7
            packet and returns empty DATA within the widened-window scenario.

    Source Correction:
        PDF 738's "after the beginning edge" is an early-edge description
        copied into both rounds. Core Vol 6, Part B, 4.2.4 gives the complete
        listening interval [receiveWindowStart - windowWidening,
        receiveWindowEnd + windowWidening]. Table 4.3-50 selects its early and
        late extensions, so place the stimulus just inside the corresponding
        outer edge. A probe beyond the trailing edge would contradict Step 8.
        The verdict's packet reference is Step 7, not Step 8. These are local
        corrections, not adopted SIG errata.

    Timing Approximation:
        Replace the source's 0.5-us inside-edge guard with 1 us at integer-us
        timestamp resolution. Retain the limited marker for this precision;
        neither round is skipped. Compute widening from the source's 500-ppm
        peer clock and 16-us allowance, without calling the DUT timing helper.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-158-C procedure in
        Section 4.3.5, Figure 4.3-127 and Table 4.3-50; PDF 736-738,
        printed 4.3-263 through 4.3-265. Steps 1-8 and Expected Outcome;
        referenced Core Vol 6, Part B, Section 4.2.4.
    """
    for timeout, late in ((300, False), (200, True)):
        parameters = LinkParameters(interval=24, timeout=timeout, hop_sca=5)
        link = await establish(
            tester,
            peer_address=LOWER_CENTRAL_PUBLIC_ADDRESS,
            connection_parameters=parameters,
        )
        await link.control(
            Opcode.LL_FEATURE_RSP,
            bytes([Opcode.LL_FEATURE_REQ])
            + int(hci.LeFeatureMask.CONNECTION_PARAMETERS_REQUEST_PROCEDURE).to_bytes(
                PDU_CONTROL_FEATURE_SET_SIZE, 'little'
            ),
        )
        interval = 32
        await tester.command(
            hci.HCI_LE_Connection_Update_Command(
                link.handle, interval, interval, 0, timeout, 0, 0
            )
        )
        _check_parameter_pdu(await link.control(Opcode.LL_CONNECTION_PARAM_REQ))
        instant = (link.counter + 50) & LL_EVENT_COUNTER_MASK
        update = bytes([Opcode.LL_CONNECTION_UPDATE_IND]) + struct.pack(
            '<BHHHHH', 1, 1, interval, 0, timeout, instant
        )
        await link.exchange(update, llid=LlId.CONTROL, check_ack=True)
        last_anchor = None
        while link.counter != instant:
            last_anchor = link.anchor
            packet = await link.exchange()
            assert packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
            assert packet.pdu[1] == 0
        assert last_anchor is not None
        link.anchor += LL_CONN_INTERVAL_UNIT_US
        link.parameters = replace(
            link.parameters,
            window_size=1,
            window_offset=1,
            interval=interval,
            latency=0,
            timeout=timeout,
        )
        # Omit the first ten events after the Instant, including the transmit
        # window. Keep event/channel counters independent of the DUT scheduler.
        link.anchor += 10 * interval * LL_CONN_INTERVAL_UNIT_US
        link.counter = (link.counter + 10) & LL_EVENT_COUNTER_MASK
        start = link.anchor
        end = start + LL_CONN_INTERVAL_UNIT_US
        widening = (
            LL_SCA_PPM[LL_SCA_500_PPM] * (end - last_anchor)
        ) // PHY_TIME_UNITS_PER_SECOND + 16
        probe = end + widening - 1 if late else start - widening + 1
        assert probe > end if late else probe < start
        tester.output.write(
            f'WINDOW late={late} nominal={start}:{end} last_sync={last_anchor} '
            f'widening={widening} probe={probe}\n'
        )
        link.anchor = probe
        packet = await link.exchange(check_ack=True)
        assert packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
        assert packet.pdu[1] == 0
        event = await tester.event(hci.HCI_LE_Connection_Update_Complete_Event)
        assert (
            event.status,
            event.connection_handle,
            event.connection_interval,
            event.peripheral_latency,
            event.supervision_timeout,
        ) == (ErrorCode.SUCCESS, link.handle, interval, 0, timeout)
        await link.disconnect(local=True)


# Verify that a Peripheral widens its receive window after missed events and receives a
# Central packet within the specified early or late widened region after a connection
# update.
PROCEDURES |= procedure_cases(
    listening_for_packet_with_window_widening_acl,
    {
        'LL/CON/PER/BV-158-C': pytest.param(
            'Listening for Packet With Window Widening, ACL',
            {},
            marks=[
                pytest.mark.limited,
            ],
        ),
    },
)


async def events_with_peripheral_latency(tester):
    """Events with Peripheral Latency.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and common channel parameters. Use latency
        five, supervision timeout 32 s and the nearest encodable connection
        interval to the source's 32 ms, as explained below. Read HCI buffer
        capacity and honor Host flow control throughout.

    Test Steps:
        Repeat Steps 1-5 for 600 connection events.
        1. Submit four-octet Host data 0xFFFFFFFF for the established handle,
           with the HCI packet-boundary start flag, when a credit is available.
        2. Receive a Central DATA packet once per connection interval on the
           channel derived from the channel-selection parameters.
        3. Send Peripheral DATA only in events required by latency five, at
           T_IFS after the received packet. Retain the initial SN and NESN.
        4. Require the Host to receive the Step 3 data with the start flag.
           Repeated Peripheral SN must not produce duplicate Host delivery.
        5. Until the next Peripheral response, require the Central to retain
           the acknowledgement state or indicate no response, with SN/NESN
           corresponding to the peer's NESN/SN. Permit flow control to change
           SN without changing NESN; a retransmission keeps its data and LLID.
        6. Execute Central Connection Terminated using the established
           interval, latency, timeout, channel map and unencrypted handle.

    Expected Outcome:
        Pass verdict:
            The connection remains established throughout the latency sequence.
            DATA acknowledgement state obeys Step 5, including flow control.
            Per-event interval and accumulated Central drift remain within
            the advertised sleep-clock-accuracy limits.

    Source Correction:
        The prescribed 32-ms interval is not encodable in 1.25-ms units.
        Use 26 units (32.5 ms), the nearest legal value, retaining latency five,
        600 repetitions and the 32-s timeout. Amber corroborates this local
        interpretation; no adopted SIG erratum is claimed.

    Coverage Note:
        Compare HCI bytes and start-flag offsets, not a fixed fragmentation.
        Count matching-handle completion credits and reject credits for
        unsubmitted packets. The source's long timeout avoids statistically
        expected supervision failures when Peripheral latency is exercised.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-02-C procedure in
        Section 4.3.6, Figure 4.3-134; PDF 748-750, printed 4.3-275 through
        4.3-277. Steps 1-6 and Expected Outcome; DATA fragmentation in 4.1.7.
    """
    link = await establish(
        tester,
        central=True,
        peer_address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
        interval=26,
        latency=5,
        timeout=LL_SUPERVISION_TIMEOUT_MAX,
    )
    capacity = await tester.command(hci.HCI_LE_Read_Buffer_Size_Command())
    credits = capacity.total_num_le_acl_data_packets
    data = b'\xff' * 4
    receiver = _HciDataStream(tester, link.handle)
    receiver.sent(data, False)
    peer_sn, peer_nesn = (link.sn ^ 1, link.nesn)
    previous = None
    submitted = completed = transmitted = 0
    first_anchor = link.anchor
    starts = []
    sent = None
    for index in range(600):
        if credits:
            await tester.wait(0)
            tester.send(
                bytes(
                    hci.HCI_AclDataPacket(
                        link.handle,
                        hci.HCI_ACL_PB_FIRST_NON_FLUSHABLE,
                        0,
                        len(data),
                        data,
                    )
                )
            )
            credits -= 1
            submitted += 1
        channel = link.channel
        packet = await _receive_central_event(link)
        starts.append(packet.start_us)
        sent = None
        if index % (link.parameters.latency + 1) == link.parameters.latency:
            sent = _data_pdu(peer_sn, peer_nesn, data, llid=LlId.START)
            await tester.transmit(
                channel,
                sent,
                start_us=packet.end_us + LL_IFS_US,
                **link.radio_parameters,
            )
            link.last_sent_end_us = tester.radio.now_us + 1
        duplicate = (
            previous is not None
            and _sequence_bits(packet)[0] == _sequence_bits(previous)[0]
        )
        if duplicate:
            assert packet.pdu[PDU_HEADER_SIZE:] == previous.pdu[PDU_HEADER_SIZE:]
            assert (
                packet.pdu[0] & PDU_DATA_LLID_MASK
                == previous.pdu[0] & PDU_DATA_LLID_MASK
            )
        if packet.pdu[0] & PDU_DATA_LLID_MASK != LlId.CONTROL and packet.pdu[1]:
            offset = transmitted % len(data)
            if not duplicate:
                assert packet.pdu[0] & PDU_DATA_LLID_MASK == (
                    LlId.CONTINUATION if offset else LlId.START
                )
                assert packet.pdu[1] <= len(data) - offset
                transmitted += packet.pdu[1]
                assert transmitted <= submitted * len(data)
            assert all(octet == data[0] for octet in packet.pdu[PDU_HEADER_SIZE:])
        if index >= 6:
            assert _sequence_bits(packet)[1] == peer_sn ^ 1
        previous = packet
        while event := tester.take(
            lambda event: isinstance(event, hci.HCI_Number_Of_Completed_Packets_Event)
        ):
            assert event.connection_handles == [link.handle]
            count = sum(event.num_completed_packets)
            completed += count
            assert completed <= submitted, 'completed unsubmitted Host packets'
            credits += count
            assert credits <= capacity.total_num_le_acl_data_packets
        receiver.poll()
    await receiver.finish()
    assert transmitted and sent is not None
    _check_central_timing(link.parameters, first_anchor, starts)
    _restore_peer(link, previous, sent)
    await link.exchange()
    assert not any(
        isinstance(event, hci.HCI_Disconnection_Complete_Event)
        for event in tester.events
    )
    await link.disconnect()


# Verify that a Central maintains its connection and acknowledgement state when a
# Peripheral uses latency, with acceptable Central clock drift.
PROCEDURES |= procedure_cases(
    events_with_peripheral_latency,
    {
        'LL/CON/CEN/BV-02-C': (
            'Events With Peripheral Latency',
            {},
        ),
    },
)


async def central_sending_data(tester):
    """Central Sending Data.

    Initial Condition:
        An unencrypted connected Central, public Lower Tester Peripheral,
        supported own address type and common interval, latency, timeout and
        channel parameters. Retain the preamble handle and HCI buffer length.

    Test Steps:
        1. Submit ten-octet zero-filled Host packets, all PB=0x00, until
           1000 octets have been submitted for the established handle.
        2. Receive matching-handle HCI Number Of Completed Packets credits.
        3. Receive the Step 1 data as LLID=0x02 and acknowledge on the same
           data channel with correctly sequenced empty Peripheral DATA.
        4. Repeat Steps 2-3 until all Step 1 data has been received.
        5. Submit another 1000 zero-filled octets in ten-octet Host packets,
           now all PB=0x01.
        6. Receive the corresponding HCI completion credits.
        7. Receive the Step 5 data as LLID=0x01 and acknowledge with empty
           Peripheral DATA using the same channel and acknowledgement scheme.
        8. Repeat Steps 6-7 until all Step 5 data has been received.
        9. Submit a further 1000 zero-filled octets in ten-octet packets,
           PB=0x00 for the first packet and PB=0x01 for subsequent packets.
        10. Receive the corresponding HCI completion credits.
        11. Receive Step 9 data, LLID=0x02 first and LLID=0x01 thereafter.
        12. Acknowledge with Peripheral DATA on the same data channel.
        13. Repeat Steps 10-12 until all Step 9 data has been received.
        14. If the advertised HCI buffer length exceeds 27 octets, submit
            zero-filled PB=0x00 packets with lengths from 28 through that
            buffer length until at least 1000 octets are submitted. Observe
            matching-handle completion credits; honor flow control throughout.
        15. For Step 14, receive and reconstruct every Host payload, using
            LLID=0x02 for each start and LLID=0x01 for continuations.
            Send empty Peripheral DATA once per connection interval with
            the common channel-selection parameters and acknowledgement scheme.
        16. Repeat Step 15 until all Step 14 data has been received.
        17. Execute Central Connection Terminated for the established
            unencrypted handle and connection parameters.
        Steps 14-16 are optional only when the HCI buffer length is at most 27.

    Expected Outcome:
        Pass verdict:
            All Host data arrives unchanged and in order. HCI completion
            credits match submitted packets. LLID start/continuation boundaries
            satisfy Section 4.1.7 without imposing a specific fragmentation.

    Source Correction:
        Step 15 cites Step 10, which reports credits, and requires reserved
        LLID=0x00. Apply it to the data submitted in Step 14, using LLID=0x02
        for starts and LLID=0x01 for continuations under Section 4.1.7 and
        Core Vol 6, Part B, Section 2.4.1. Amber corroborates these local
        corrections; no adopted SIG erratum is claimed.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-03-C procedure in
        Section 4.3.6, Figure 4.3-135; PDF 750-752, printed 4.3-277 through
        4.3-279. Steps 1-17 and Expected Outcome; DATA fragmentation in 4.1.7.
    """
    await _data_exchange(tester, central=True, direction='tx')


# Verify Central transmission of Host data to a Peripheral, including HCI completion
# credits and preservation of fragmentation boundaries.
PROCEDURES |= procedure_cases(
    central_sending_data,
    {
        'LL/CON/CEN/BV-03-C': (
            'Central Sending Data',
            {},
        ),
    },
)


async def central_receiving_data(tester):
    """Central Receiving Data.

    Initial Condition:
        An unencrypted connected Central, public Lower Tester Peripheral,
        supported own address type and common connection/channel parameters.
        Retain the preamble handle and HCI data packet length. Use reproducible
        seeded random payloads for the Lower Tester.

    Test Steps:
        1. Configure 100 ten-octet random Peripheral packets, all LLID=0x02,
           for the first 1000-octet group, applying the correction below.
        2. Receive Central DATA and respond on the same data channel using
           the acknowledgement scheme until all 1000 octets are acknowledged.
        3. Require the Step 1 payloads at the Host, totalling 1000 octets,
           with PB=0x02 at each message start.
        4. Configure another 100 ten-octet random packets, all LLID=0x01.
        5. Repeat Step 2 for the Step 4 group.
        6. Require the Step 4 payloads at the Host, totalling 1000 octets,
           with PB=0x01, applying the source-reference correction below.
        7. Configure one ten-octet random LLID=0x02 packet followed by
           99 ten-octet random LLID=0x01 packets.
        8. Repeat Step 2 for the Step 7 group.
        9. Require all 1000 Step 7 octets at the Host, PB=0x02 first and
           PB=0x01 thereafter, applying the source-reference correction below.
        10. Configure ten three-octet random Peripheral packets, all LLID=0x02.
        11. Receive Central DATA and respond on the same channel using the
            acknowledgement scheme until every Step 10 packet is acknowledged.
        12. Require the Step 11 payloads at the Host with PB=0b10 at each
            original start boundary and exactly the Lower Tester's bytes.
        13. Execute Central Connection Terminated for the established
            unencrypted handle and connection parameters.

    Expected Outcome:
        Pass verdict:
            Every Lower Tester payload is acknowledged and reported unchanged
            and in order. HCI fragmentation preserves message starts according
            to Section 4.1.7; LL and HCI packet boundaries need not coincide.

    Source Correction:
        Steps 1/4 say 1000 ten-octet packets, while Steps 2/3/6 require a
        1000-octet group: use 100 packets. Step 7 says 990 continuation packets;
        use 99 after its one start packet for the same 1000-octet total.
        Steps 6/9 compare against Step 1 but must compare their own source
        groups in Steps 4/7. Amber corroborates these local count/reference
        corrections; no adopted SIG erratum is claimed.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-04-C procedure in
        Section 4.3.6, Figure 4.3-136; PDF 753-755, printed 4.3-280 through
        4.3-282. Steps 1-13 and Expected Outcome; DATA fragmentation in 4.1.7.
    """
    await _data_exchange(tester, central=True, direction='rx')


# Verify that a Central acknowledges Peripheral DATA and reports its payload and
# fragmentation boundaries correctly to the Host.
PROCEDURES |= procedure_cases(
    central_receiving_data,
    {
        'LL/CON/CEN/BV-04-C': (
            'Central Receiving Data',
            {},
        ),
    },
)


async def central_sending_and_receiving_data(tester):
    """Central Sending and Receiving Data.

    Initial Condition:
        An unencrypted connected Central, public Lower Tester Peripheral,
        supported own address type and common connection/channel parameters.
        Retain the preamble handle and HCI buffer length. Each direction
        transfers 1000 reproducible seeded random octets in ten-octet messages.

    Test Steps:
        1. Configure 100 ten-octet random Peripheral packets, LLID=0x02
           first and LLID=0x01 thereafter.
        2. Submit 1000 random Host octets in ten-octet packets for the
           established handle, PB=0x00 first and PB=0x01 thereafter.
        3. Receive matching-handle HCI Number Of Completed Packets credits.
        4. Receive Central DATA containing Step 2 bytes, LLID=0x02 first
           and LLID=0x01 thereafter, applying the correction below.
        5. Respond on the same data channel using the acknowledgement
           scheme and the Peripheral data configured in Step 1.
        6. Repeat Steps 4-5 until all Step 2 Host data has been received.
        7. Require Step 1 Peripheral data at the Host with PB=0x02 first
           and PB=0x01 thereafter, unchanged and in its original order.

    Expected Outcome:
        Pass verdict:
            Both directions' data is acknowledged and received unchanged.
            HCI bytes and message-start offsets obey Section 4.1.7, without
            requiring LL and HCI packetization to be identical.

    Source Correction:
        Step 4 refers to data submitted in Step 1, but Step 1 configures the
        opposite direction. Central DATA must contain Host bytes from Step 2.
        Amber corroborates this local reference correction; no adopted SIG
        erratum is claimed.

    Coverage Note:
        Respect HCI buffer credits while interleaving the two data directions.
        Disconnect through HCI after all seven source steps are complete.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-05-C procedure in
        Section 4.3.6, Figure 4.3-137; PDF 755-756, printed 4.3-282 through
        4.3-283. Steps 1-7 and Expected Outcome; DATA fragmentation in 4.1.7.
    """
    await _data_exchange(tester, central=True, direction='duplex')


# Verify simultaneous Central transmission and reception with a Peripheral, including
# acknowledgement and Host DATA delivery.
PROCEDURES |= procedure_cases(
    central_sending_and_receiving_data,
    {
        'LL/CON/CEN/BV-05-C': (
            'Central Sending and Receiving Data',
            {},
        ),
    },
)


async def central_sending_termination(tester):
    """Central Sending Termination.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and common connection/channel parameters.
        Retain the preamble handle and the connection supervision timeout.

    Test Steps:
        1. Send HCI Disconnect for the established handle and require
           successful Command Status.
        2. Receive LL_TERMINATE_IND carrying the requested reason and
           acknowledge with empty Peripheral DATA in the same event.
        3. Observe no further Central transmissions for a duration equal
           to the connection supervision timeout after the acknowledgement.
        4. Interleaved with Step 3, receive successful HCI Disconnection
           Complete for the established handle, reporting local termination.

    Expected Outcome:
        Pass verdict:
            The Central sends termination until acknowledged, stops its
            connection transmissions after the acknowledgement, and reports
            successful termination through HCI.

    Coverage Note:
        Start RF observation immediately after the acknowledgement rather than
        advancing time to await HCI first. Collect HCI events during that same
        observation period, checking the handle/reason and rejecting duplicates.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-08-C procedure in
        Section 4.3.6, Figure 4.3-139; PDF 758-759, printed 4.3-285 through
        4.3-286. Steps 1-4 and Expected Outcome; Core Vol 6, Part B, 5.1.6.
    """
    await _connection_termination(tester, central=True, local=True)


# Verify that a Central sends termination, stops transmitting after the Peripheral
# acknowledges, and reports disconnection to its Host.
PROCEDURES |= procedure_cases(
    central_sending_termination,
    {
        'LL/CON/CEN/BV-08-C': (
            'Central Sending Termination',
            {},
        ),
    },
)


async def central_accepting_termination(tester):
    """Central Accepting Termination.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and common connection/channel parameters.
        Retain the preamble handle and connection supervision timeout.

    Test Steps:
        1. Receive empty Central DATA and respond with LL_TERMINATE_IND.
           Require an acknowledgement in the following connection event,
           before T_Terminate expires.
        2. Observe no further Central transmissions for a duration equal
           to the connection supervision timeout after its acknowledgement.
        3. Interleaved with Step 2, receive successful HCI Disconnection
           Complete for the established handle and the peer termination reason.

    Expected Outcome:
        Pass verdict:
            The Central acknowledges before the termination timer expires,
            stops transmitting after its acknowledgement, and reports the
            correct peer-requested termination through HCI.

    Coverage Note:
        Require an empty DATA acknowledgement with NESN acknowledging the
        termination SN, in the next event rather than the request's event.
        Observe RF and HCI concurrently over the post-acknowledgement timeout;
        missing or duplicate HCI disconnections fail.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-09-C procedure in
        Section 4.3.6, Figure 4.3-140; PDF 759-760, printed 4.3-286 through
        4.3-287. Steps 1-3 and Expected Outcome; Core Vol 6, Part B, 5.1.6.
    """
    await _connection_termination(tester, central=True, local=False)


# Verify that a Central acknowledges Peripheral termination, stops transmitting and
# reports the peer-requested disconnection to its Host.
PROCEDURES |= procedure_cases(
    central_accepting_termination,
    {
        'LL/CON/CEN/BV-09-C': (
            'Central Accepting Termination',
            {},
        ),
    },
)


async def central_supervision_timer(tester):
    """Central Supervision Timer.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type, zero latency and common channel parameters.
        Select the maximum and minimum supported interval/timeout pairs.

    Test Steps:
        1. Establish using the maximum supported supervision timeout and
           connection interval, with zero latency: 32 s and 4 s respectively.
        2. Stop Peripheral responses after a known valid connection event
           and remain silent until the supervision timeout expires.
        3. Require successful HCI Disconnection Complete for the established
           handle with Connection Timeout reason, no earlier than the selected
           timeout measured from the last responded event.
        4. Establish again with the minimum supported supervision timeout and
           connection interval, with zero latency: 100 ms and 7.5 ms.
        5. Repeat Steps 2-3 for the minimum settings.

    Expected Outcome:
        Pass verdict:
            For both parameter pairs, the Central reports connection timeout
            after the selected connSupervisionTimeout from the last response.

    Coverage Note:
        Use the last valid Peripheral packet as the timer reference, rather
        than Host setup. Its validation timestamp is the PHY's last occupied
        microsecond, one less than the exclusive packet-end boundary.
        Observe HCI at 1-ms resolution, including an explicit
        check immediately before the permitted boundary so a coarse sample
        cannot hide an already queued early completion. The Central procedure
        does not inherit the Peripheral procedure's 10-ms rounding note.
        Reject a missing/late event, wrong handle/status/reason, or resumed
        Central RF traffic after timeout.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-10-C procedure in
        Section 4.3.6, Figure 4.3-141; PDF 760-761, printed 4.3-287 through
        4.3-288. Steps 1-5 and Expected Outcome; Core Vol 6, Part B, 4.5.2.
    """
    await _supervision(tester, central=True)


# Verify that a Central stops an unmaintained connection on supervision timeout and
# reports the timeout and established handle to its Host.
PROCEDURES |= procedure_cases(
    central_supervision_timer,
    {
        'LL/CON/CEN/BV-10-C': (
            'Central Supervision Timer',
            {},
        ),
    },
)


async def central_retransmission_request(tester):
    """Central Retransmission Request.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type, common channel-selection parameters and
        30-ms interval, zero latency and 900-ms supervision timeout.
        Retain the current/next SN and NESN variables from the connection.

    Test Steps:
        1. Submit one Host ACL data octet 0xFF for the established handle
           with the packet-boundary start flag.
        2. Receive Central DATA once per connection interval on its selected
           data channel. Respond at T_IFS with invalid-CRC DATA; repeat fifteen
           times.
        3. During Step 2, derive the next transmitted SN from the last
           correctly received NESN and acknowledge correctly received Central
           DATA using the next NESN.
        4. Require following Central packets to indicate failure to receive
           the Peripheral packet: Central SN matches the current tester NESN
           and Central NESN retains the next tester SN.
        5. Execute Central Connection Terminated for the established
           unencrypted handle and connection parameters.

    Expected Outcome:
        Pass verdict:
            The Central sends negative acknowledgements for invalid-CRC DATA.

    Coverage Note:
        Invert every CRC bit on the wire, rather than changing a receive-result
        flag. Require unchanged Central payload/sequence and no Host delivery
        from the faulty packets. Check the response after the fifteenth fault
        before recovery, and require exactly one Host completion credit after
        a valid ACK restores the independent peer's normal sequence state.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-14-C procedure in
        Section 4.3.6, Figure 4.3-143; PDF 763-764, printed 4.3-290 through
        4.3-291. Steps 1-5 and Expected Outcome; Core Vol 6, Part B, 4.5.9.
    """
    await _retransmissions(tester, central=True, mode='crc', latency=False)


# Verify that a Central maintains acknowledgement state and requests retransmission when
# the Peripheral response has an invalid CRC.
PROCEDURES |= procedure_cases(
    central_retransmission_request,
    {
        'LL/CON/CEN/BV-14-C': (
            'Central Retransmission Request',
            {},
        ),
    },
)


async def central_retransmission(tester):
    """Central Retransmission.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type, common channel-selection parameters and
        30-ms interval, zero latency and 900-ms supervision timeout.
        Retain the connection's current/next SN and NESN variables.

    Test Steps:
        1. Submit one Host ACL data octet 0xFF for the established handle
           with the packet-boundary start flag.
        Repeat Steps 2-3 fifteen times.
        2. Receive Central DATA once per interval on the selected data channel.
           Respond at T_IFS with CRC-valid DATA, retaining the current tester
           NESN to withhold acknowledgement of the Central packet. Use a new
           tester SN when the previous Central NESN acknowledges its packet.
        3. Require Central retransmission in the following received packet,
           with SN matching current tester NESN and NESN acknowledging the
           next correctly received tester SN.
        4. Execute Central Connection Terminated for the established
           unencrypted handle and connection parameters.

    Expected Outcome:
        Pass verdict:
            Every negative acknowledgement causes retransmission of the
            same Central payload.

    Coverage Note:
        Validate the final Step 3 observation as well as intermediate replies.
        No Host completion credit may be returned before the payload is
        acknowledged. Recover with a valid ACK, require exactly one matching
        completion credit, then terminate.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-15-C procedure in
        Section 4.3.6, Figure 4.3-144; PDF 764-765, printed 4.3-291 through
        4.3-292. Steps 1-4 and Expected Outcome; Core Vol 6, Part B, 4.5.9.
    """
    await _retransmissions(tester, central=True, mode='nak', latency=False)


# Verify that a Central repeats the same DATA payload when its Peripheral negatively
# acknowledges that packet.
PROCEDURES |= procedure_cases(
    central_retransmission,
    {
        'LL/CON/CEN/BV-15-C': (
            'Central Retransmission',
            {},
        ),
    },
)


async def central_acknowledgement_repetition(tester):
    """Central Acknowledgement Repetition.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type, common channel-selection parameters and
        30-ms interval, zero latency and 900-ms supervision timeout.
        Retain the connection's current/next SN and NESN variables.

    Test Steps:
        1. Submit one Host ACL data octet 0xFF for the established handle
           with the packet-boundary start flag.
        Repeat Steps 2-3 fifteen times.
        2. Receive Central DATA once per interval on the selected data channel
           and respond at T_IFS. Keep the tester SN despite the Central NESN
           acknowledging it, simulating a missed acknowledgement. Positively
           acknowledge correctly received Central packets using the next NESN.
        3. Require the following Central packet to repeat its acknowledgement:
           SN matches current tester NESN and NESN retains the next tester SN.
        4. Execute Central Connection Terminated for the established
           unencrypted handle and connection parameters.

    Expected Outcome:
        Pass verdict:
            The Central repeats its acknowledgement when the tester behaves
            as though the previous acknowledgement was not received.

    Coverage Note:
        Transmit the same one-octet Peripheral payload and SN throughout;
        require exactly one Host delivery, not duplicate deliveries. Check
        the final repeated acknowledgement before restoring normal sequencing,
        and require exactly one completion credit for the submitted Host packet.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-16-C procedure in
        Section 4.3.6, Figure 4.3-145; PDF 765-767, printed 4.3-292 through
        4.3-294. Steps 1-4 and Expected Outcome; Core Vol 6, Part B, 4.5.9.
    """
    await _retransmissions(tester, central=True, mode='duplicate', latency=False)


# Verify that a Central repeats its positive acknowledgement when the Peripheral
# retransmits an already received DATA packet.
PROCEDURES |= procedure_cases(
    central_acknowledgement_repetition,
    {
        'LL/CON/CEN/BV-16-C': (
            'Central Acknowledgement Repetition',
            {},
        ),
    },
)


async def central_lost_negative_acknowledgement(tester):
    """Central Lost Negative Acknowledgement.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type, common channel-selection parameters and
        30-ms interval, zero latency and 900-ms supervision timeout.
        Retain the connection's current/next SN and NESN variables.

    Test Steps:
        1. Submit one Host ACL data octet 0xFF for the established handle
           with the packet-boundary start flag.
        2. Receive Central DATA once per interval on the selected channel.
           Respond at T_IFS with one invalid-CRC DATA packet using the next
           tester SN identified by the previous Central NESN.
        Repeat Steps 3-4 fifteen times.
        3. Receive the negative acknowledgement and respond with valid-CRC
           DATA using the next tester SN, simulating failure to receive the NAK.
           Retain tester NESN equal to Central SN, as Figure 4.3-146 shows.
        4. Require following Central packets to continue the negative
           acknowledgement, with NESN still naming the missing tester packet.
        5. Execute Central Connection Terminated for the established
           unencrypted handle and connection parameters.

    Expected Outcome:
        Pass verdict:
            The Central repeats its negative acknowledgement when the tester
            transmits its next payload.

    Source Correction:
        Step 3's same-SN and positive-ACK wording conflicts with the purpose,
        Expected Outcome and Figure 4.3-146. Follow the latter three instead:
        after the bad-CRC packet, advance the tester SN as if its NAK were lost,
        and retain tester NESN equal to Central SN. Core Vol 6, Part B, 4.5.9
        requires the Central to retain its expected SN after bad CRC and ignore
        the out-of-sequence payload; its unchanged NESN repeats the NAK.
        The retained tester NESN also keeps the Central payload unacknowledged,
        allowing all fifteen repetitions to check its unchanged SN and bytes.
        This is a local correction, not an adopted SIG erratum.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-17-C procedure in
        Section 4.3.6, Figure 4.3-146; PDF 767-768, printed 4.3-294 through
        4.3-295. Steps 1-5 and Expected Outcome; Core Vol 6, Part B, 4.5.9.
    """
    await _retransmissions(tester, central=True, mode='lost')


# Verify that a Central preserves packet sequence numbering and repeats a negative
# acknowledgement when the Peripheral incorrectly advances to its next packet after a
# lost negative acknowledgement.
PROCEDURES |= procedure_cases(
    central_lost_negative_acknowledgement,
    {
        'LL/CON/CEN/BV-17-C': ('Central Lost Negative Acknowledgement', {}),
    },
)


async def central_latency_retransmission_request(tester):
    """Central Latency Retransmission Request.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type, common channel-selection parameters and
        30-ms interval. Select nonzero Peripheral latency one and the maximum
        supervision timeout from TSPX_conn_timeout_max, here 32 s.
        Retain the connection's current/next SN and NESN variables.

    Test Steps:
        1. Submit one Host ACL data octet 0xFF for the established handle
           with the packet-boundary start flag.
        Repeat Steps 2-3 fifteen times.
        2. Receive Central DATA once per interval on the selected data channel.
           Respond at T_IFS with invalid-CRC DATA only in events required by
           the Peripheral latency parameter. Derive the tester SN and NESN
           from the last correctly received Central acknowledgement state.
        3. Require following Central DATA to indicate either CRC failure or
           absence of a response: SN matches current tester NESN and NESN
           retains current tester SN.
        4. Execute Central Connection Terminated for the established
           unencrypted handle and connection parameters.

    Expected Outcome:
        Pass verdict:
            The Central uses retransmissions to handle Peripheral latency.

    Coverage Note:
        With latency one, omit alternate responses while still observing
        every Central event. Require unchanged retransmitted Host payload,
        sequence state and no premature Host completion credit. Check the
        final fault's response, recover with a valid ACK and require exactly
        one matching completion credit before termination.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-18-C procedure in
        Section 4.3.6, Figure 4.3-147; PDF 768-769, printed 4.3-295 through
        4.3-296. Steps 1-4 and Expected Outcome; Core Vol 6, Part B, 4.5.9.
    """
    await _retransmissions(tester, central=True, mode='crc', latency=True)


# Verify Central acknowledgement and retransmission behavior when a Peripheral combines
# latency with invalid-CRC responses.
PROCEDURES |= procedure_cases(
    central_latency_retransmission_request,
    {
        'LL/CON/CEN/BV-18-C': (
            'Central Latency Retransmission Request',
            {},
        ),
    },
)


async def connection_control_timeout(tester):
    """Connection Control Timeout.

    Initial Condition:
        The IUT is in Standby. The Lower Tester acts as a Peripheral,
        advertises with a public address on the first supported advertising
        channel, and can acknowledge DATA without answering Feature Exchange.

    Test Steps:
        1. Configure the public Lower Tester advertiser on the first supported
           advertising channel.
        2. Send HCI LE Create Connection with public Peer_Address_Type=0x00
           and the Lower Tester address; require successful Command Status.
        3. Require HCI LE Connection Complete for the established Central ACL.
        4. Send HCI LE Read Remote Features Page 0. If the IUT already sent an
           autonomous LL_FEATURE_REQ after connection, treat that packet as
           Step 5 and perform Step 4 as soon as practical; do not require
           another request or restart the existing procedure's timer.
        5. Receive LL_FEATURE_REQ from the IUT, unless already received
           through the autonomous alternative preceding Step 4.
        6. Acknowledge the request with DATA but never send LL_FEATURE_RSP.
        7. Maintain valid empty DATA to avoid supervision timeout while
           withholding LL_FEATURE_RSP until the procedure response timer expires.
        8. If HCI LE Read Remote Features Page 0 Complete is reported,
           require the established handle and status LL Response Timeout (0x22).
        9. Require successful HCI Disconnection Complete for that handle,
           with reason LL Response Timeout (0x22).

    Expected Outcome:
        Pass verdict:
            At control transaction timeout, the IUT reports disconnection
            with reason 0x22, rather than a supervision timeout.

    Source Correction:
        Step 6 describes the LL_FEATURE_REQ as received by the IUT, although
        Step 5 has the IUT transmit it. The Lower Tester acknowledges the
        request it received from the IUT. This is a local subject correction,
        not an adopted SIG erratum.

    Coverage Note:
        Core Vol 6, Part B, 5.2 starts the 40-s response timer at procedure
        initiation/LL Control PDU queueing, not at peer ACK or first RF TX.
        That internal queueing timestamp is not exposed by HCI. Bound it
        between the triggering action and the first observed request; for
        the autonomous alternative, the lower bound precedes connection
        establishment. Do not treat the Host-command timestamp as an exact
        normative timer origin. Observe with 1-ms precision, reject early
        completion, duplicate or wrong-handle/status events, and any procedure
        completion following Disconnection Complete. Require RF cessation.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-19-C procedure in
        Section 4.3.6, Figure 4.3-148; PDF 770-771, printed 4.3-297 through
        4.3-298. Steps 1-9 and the autonomous alternative before Step 4;
        Core Vol 6, Part B, 5.2.
    """
    await _procedure_timeout(tester, central=True, parameters=False)


# Verify that a Central terminates its ACL when an initiated Link Layer control
# procedure receives no response before the transaction timeout.
PROCEDURES |= procedure_cases(
    connection_control_timeout,
    {
        'LL/CON/CEN/BV-19-C': (
            'Connection Control Timeout',
            {},
        ),
    },
)


async def central_acknowledgement_scheme(tester):
    """Central Acknowledgement Scheme.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type, common channel-selection parameters,
        30-ms interval, zero latency and 900-ms supervision timeout.
        Retain the connection handle and current/next SN and NESN variables.

    Test Steps:
        Repeat Steps 1-5 fifteen times.
        1. Submit four-octet Host data 0xFFFFFFFF for the established handle
           with the HCI packet-boundary start flag.
        2. Configure one Lower Tester DATA packet with four-octet payload
           0xFFFFFFFF.
        3. Receive Central DATA once per connection interval on its selected
           channel. Require SN matching the current tester NESN and NESN
           matching the next tester SN for correctly received packets.
        4. Respond at T_IFS using the normal acknowledgement scheme: use
           next SN when the Central NESN acknowledges the current packet,
           and next NESN to acknowledge correctly received Central DATA.
        5. Require HCI ACL Data containing the Lower Tester payload.
        6. Execute Central Connection Terminated for the established
           unencrypted handle and connection parameters.

    Expected Outcome:
        Pass verdict:
            All fifteen exchanges complete with the normal acknowledgement
            scheme, preserving payloads and packet sequence numbering.

    Coverage Note:
        Check all four Host-to-air octets and completion credits. For received
        data, compare the complete HCI byte stream and message-start offsets
        under Section 4.1.7 rather than requiring a specific HCI fragmentation.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-22-C procedure in
        Section 4.3.6, Figure 4.3-151; PDF 773-774, printed 4.3-300 through
        4.3-301. Steps 1-6 and Expected Outcome; Core Vol 6, Part B, 4.5.9.
    """
    await _acknowledgement_scheme(tester, central=True)


# Verify normal Central acknowledgement and sequence-number handling while transferring
# data in both directions with a Peripheral.
PROCEDURES |= procedure_cases(
    central_acknowledgement_scheme,
    {
        'LL/CON/CEN/BV-22-C': (
            'Central Acknowledgement Scheme',
            {},
        ),
    },
)


async def central_receiving_data_le_coded_ci_change(tester):
    """Central Receiving Data, LE Coded, CI Change.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window, maximum supported connection
        interval, common Peripheral latency and supervision timeout. This
        procedure uses a 4 s interval, zero latency and a 32 s supervision timeout.
        Retain the established connection handle and negotiated data lengths.

    Test Steps:
        1. Send HCI_LE_Set_PHY with ALL_PHYS=0, TX_PHYS=RX_PHYS preferring
           LE Coded, and PHY_options=0x0000. Receive HCI_Command_Status.
        2. Receive LL_PHY_REQ and answer with LL_PHY_RSP specifying LE Coded
           in both TX_PHYS and RX_PHYS.
        3. Receive LL_PHY_UPDATE_IND selecting LE Coded for both directions.
           Otherwise end with Inconclusive. Maintain the old PHYs until the
           Instant, then maintain the connection using LE Coded.
        4. Require HCI_LE_PHY_Update_Complete reporting success, the active
           handle and LE Coded in both directions.
        5. If TSPX_TxOctets_Max>27 or TSPX_TxTime_Max>328 us, send
           HCI_LE_Set_Data_Length with those transmit maxima and receive
           HCI_Command_Complete. Otherwise go to Step 7.
        6. If the IUT initiates Data Length Update, answer with the Lower
           Tester's TxOctets=TSPX_RxOctets_Max and TxTime=TSPX_RxTime_Max.
           Otherwise initiate Data Length Update with those transmit values
           and receive the IUT's response. Validate the negotiated fields
           and effective-length HCI changes.
        7. Configure 288 DATA packets at the Lower Tester's effective maximum
           transmit size, equal to the IUT's effective receive-octet limit.
           Payload octets increment continuously across packets, starting at
           0x00 and wrapping after 0xFF. Use every CI value in the source's
           18-row, 16-value-per-row S=2/S=8 sequence, in the original order.
        8. Send the DATA packets using the acknowledgement scheme and active
           channel selection. Require an empty IUT DATA acknowledgement for
           each packet, with positive NESN transition and T_IFS=150 +/-2 us.
        9. Receive the corresponding HCI ACL data with the active handle,
           correct boundary flags and exact incrementing payload. Continue
           Steps 7-9 until all 288 packets have been acknowledged and reported.
        10. Repeat the data exchange once using 27-octet packets rather than
            the effective maximum. Repeat the entire 288-value coding sequence,
            restarting the payload at 0x00, and apply every RF/HCI check again.

    Expected Outcome:
        Pass verdict:
            Both payload-size rounds complete. Every DATA packet is acknowledged
            within 150 +/-2 us and all incrementing payload bytes reach the Host
            correctly, without duplicates. MD permits the full coding sequence
            within the connection event; acknowledgements are not deferred to
            the next 4 s event.
        Inconclusive verdict:
            The IUT does not select LE Coded in both directions in Step 3.

    Source Correction:
        Step 10 refers recursively to Steps 7-10. Repeat the data-exchange body
        (Steps 7-9) once at the minimum payload size, not the repetition
        instruction itself. The stated maximum/minimum test purpose and Amber
        corroborate this local editorial interpretation; it is not a SIG erratum.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-52-C; Figure 4.3-176 and Step 7 coding table;
        PDF pages 825-828, printed pages 4.3-352..355; source reference [10] 4.5.
    """
    await _coded_reception(tester, central=True)


# Verify that a Central receives Peripheral DATA across transitions between LE Coded S=2
# (500 kbit/s) and S=8 (125 kbit/s), acknowledges every packet within T_IFS tolerance,
# and reports the complete payload to the Host. Exercise both the maximum effective and
# minimum 27-octet payload sizes.
PROCEDURES |= procedure_cases(
    central_receiving_data_le_coded_ci_change,
    {
        'LL/CON/CEN/BV-52-C': (
            'Central Receiving Data, LE Coded, CI Change',
            {},
        ),
    },
)


async def central_t_terminate_timer(tester):
    """Central T_Terminate Timer.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window, common connection timing and
        Peripheral latency. Use the common 10 ms interval, zero latency and
        300 ms supervision timeout; retain the established connection handle.

    Test Steps:
        1. Send HCI_Disconnect for the saved handle and require successful
           HCI_Command_Status.
        2. Receive LL_TERMINATE_IND. Continue valid Peripheral transmissions,
           but set NESN so that the termination PDU is not acknowledged.
        3. Require retransmissions of the same control PDU, payload and SN.
           Respond with empty DATA without acknowledging termination, continuing
           until T_Terminate expires. Follow the IUT's NESN for fresh Peripheral
           DATA so normal supervision is refreshed independently.
        4. Require HCI_Disconnection_Complete with success, the active handle
           and reason 0x16 (Connection Terminated by Local Host) or 0x22
           (LL Response Timeout). Verify that connection RF maintenance stops.

    Expected Outcome:
        Pass verdict:
            The IUT continues sending the same TERMINATE_IND until its
            termination timer expires, then reports termination through HCI.
            Valid non-acknowledging DATA does not restart T_Terminate.

    Observation Boundary:
        Core 5.1.6 starts T_Terminate when TERMINATE_IND is queued and sets it
        to connSupervisionTimeout. Bracket that internal queueing between the
        Host trigger and first observed TERMINATE_IND transmission, not peer
        acknowledgement. Check HCI around that bracket plus 300 ms at 1 ms
        sampling resolution; this is a test observation bound, not Core tolerance.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BI-02-C; Figure 4.3-190, Steps 1-4 and verdict;
        PDF pages 855-856, printed pages 4.3-382..383; source reference [3] 5.1.6.
        Common timing: Section 4.3.2.1, PDF 476, printed 4.3-3.
        Core v6.3, Vol 6, Part B, 5.1.6 and 4.5.2.
    """
    await _timeouts_termination(tester, central=True)


# Verify that a Central retransmits unacknowledged LL_TERMINATE_IND until T_Terminate
# expires, even while valid Peripheral DATA keeps the ordinary supervision timer alive.
PROCEDURES |= procedure_cases(
    central_t_terminate_timer,
    {
        'LL/CON/CEN/BI-02-C': (
            'Central T_Terminate Timer',
            {},
        ),
    },
)


async def central_connection_control_timer(tester):
    """Central Connection Control Timer.

    Initial Condition:
        Connected Central with a public Lower Tester peer address, supported own
        address type, any scan interval/window, common connection timing and
        Peripheral latency. Use the common 10 ms interval, zero latency and
        300 ms supervision timeout; retain the established connection handle.

    Test Steps:
        1. If the IUT autonomously sent LL_VERSION_IND after connection setup,
           acknowledge that indication. Continue Peripheral DATA but never send
           the Lower Tester's VERSION_IND; proceed directly to Step 4.
        2. Otherwise send HCI_Read_Remote_Version_Information for the saved
           handle and receive successful HCI_Command_Status.
        3. Receive the IUT's LL_VERSION_IND and positively acknowledge it.
           Do not send a peer VERSION_IND.
        4. Continue empty Peripheral DATA until the connection control timeout
           or the disconnection in Step 5. Reject additional VERSION_IND after
           its acknowledgement and reject premature procedure completion.
        5. Require HCI_Disconnection_Complete for the active handle with
           success and reason 0x22 (LL Response Timeout). Verify RF maintenance
           stops. Any preceding Host-requested Version Complete must carry
           the timeout status and handle; no such event belongs to an autonomous
           exchange or may follow disconnection.

    Expected Outcome:
        Pass verdict:
            The IUT recovers from the stalled control procedure by terminating
            the connection at its 40 s procedure response timeout, not through
            supervision expiry while valid DATA is still received.

    Source Correction:
        The purpose calls the IUT a Peripheral and says its request is not
        acknowledged. The title, initial condition and Steps 1/3 instead specify
        a Central IUT and explicit acknowledgement by the Lower Tester.
        Follow those numbered steps: omit the peer's protocol response, not
        the Link Layer ACK. Core 5.1.5/5.2 and Amber corroborate this distinction.
        This is a local editorial correction, not an adopted SIG erratum.

    Observation Boundary:
        Bracket procedure initiation from the Host trigger to first VERSION_IND
        transmission. For an autonomous request, the lower bound is connection
        setup start, not its later RF timestamp. Observe HCI with a 1 ms bound,
        including a sample immediately before the earliest permitted expiry;
        never treat HCI response time or RF transmission as the exact queue time.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BI-04-C; Figure 4.3-191, Steps 1-5 and verdict;
        PDF pages 856-858, printed pages 4.3-383..385; source reference [3] 5.2.
        Common timing: Section 4.3.2.1, PDF 476, printed 4.3-3.
        Core v6.3, Vol 6, Part B, 5.1.5 and 5.2.
    """
    await _version_timeout(tester, central=True)


# Verify that a Central recovers from a stalled Version Exchange through the procedure
# response timeout. The Peripheral Lower Tester acknowledges the IUT's VERSION_IND but
# withholds its own version response while maintaining valid DATA.
PROCEDURES |= procedure_cases(
    central_connection_control_timer,
    {
        'LL/CON/CEN/BI-04-C': (
            'Central Connection Control Timer',
            {},
        ),
    },
)


async def invalid_llid_ll_con_cen_bi_14_c(tester):
    """Invalid LLID.

    Initial Condition:
        Connected Central with a Peripheral Lower Tester, public peer address,
        supported own address type, any scan interval/window and connection
        interval, common Peripheral latency and supervision timeout.
        Use a fresh ACL without prior Feature Exchange and select
        TSPX_pdu_with_response=LL_FEATURE_REQ for this IUT.

    Test Steps:
        1. Send HCI LE Read Remote Features for the active handle and require
           successful HCI_Command_Status.
        2. Receive LL_FEATURE_REQ with valid LLID and FeatureSet.
        3. Send LL_FEATURE_RSP with reserved LLID=0b00 and a valid zero
           FeatureSet.
        4. Require acknowledgement of that packet after at most four retries;
           fail if its NESN acknowledgement is not received.
        5. For the four simulated seconds after Step 3, require no HCI LE
           Read Remote Features Complete. Continue valid empty DATA and
           ensure the invalid control payload is not delivered as Host ACL.
        6. Send the same LL_FEATURE_RSP payload with valid LLID=0b11.
        7. Require HCI LE Read Remote Features Complete with success, the
           active handle and the exact valid FeatureSet from Step 6.

    Test Condition:
        If the IUT has already exchanged features and does not generate
        FEATURE_REQ in Step 2, the source permits a different supported
        request/response PDU and its corresponding Host command/completion.
        This configuration selects the fresh-ACL Feature Exchange path; it
        does not substitute another control procedure.

    Expected Outcome:
        Pass verdict:
            No feature completion occurs in Step 5; Step 7 reports success
            and the supplied FeatureSet only after the correctly typed PDU.
        Fail verdict:
            The reserved-LLID packet is not acknowledged in Step 4, or its
            payload is processed as a control response or Host data.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BI-14-C; Figure 4.3-206, Steps 1-7,
        Test Condition and verdicts; PDF pages 885-886, printed 4.3-412..413;
        source reference [8] 2.4. The source's HCI Page 0 names use the
        equivalent existing HCI_LE_Read_Remote_Features API here.
    """
    await _invalid_control_invalid_llid(tester, central=True)


# Verify that a Central acknowledges a reserved-LLID packet at the sequence level
# without interpreting its Feature Response payload, then correctly accepts the same
# response with a valid control LLID.
PROCEDURES |= procedure_cases(
    invalid_llid_ll_con_cen_bi_14_c,
    {
        'LL/CON/CEN/BI-14-C': (
            'Invalid LLID',
            {},
        ),
    },
)


class _FlushTimeoutPeer(PeerLink):
    withheld_payload = None

    def _accept(self, packet):
        saved = self.nesn
        super()._accept(packet)
        if packet.pdu[PDU_HEADER_SIZE:] == self.withheld_payload:
            self.nesn = saved


async def flush_timeout(tester, *, central):
    """Flush queued ACL data while preserving an existing transmission.

    Initial Condition:
        An unencrypted LE ACL with a 10 ms interval and 32 s supervision timeout.

    Test Case Configuration:
        LL/CON/CEN/BV-176-C: Central IUT.
        LL/CON/PER/BV-180-C: Peripheral IUT.

    Test Steps:
        1. Set the automatic flush timeout to one second. Queue a non-flushable
           L2CAP PDU, a flushable PDU, and a second non-flushable PDU.
        2. Withhold acknowledgment of the first PDU for 150 connection events.
           Then acknowledge it and observe the next transmission.
        3. Send a flushable PDU, wait for its first transmission, and withhold
           acknowledgment for another 150 events before acknowledging it.

    Expected Outcome:
        The timeout discards the queued flushable PDU. The non-flushable PDUs and
        the already-transmitted flushable PDU are delivered. The Host receives
        exactly four completed-packet credits.

    Reference:
        LL.TS.p28, 4.3.7.53, Table 4.3-137, Figure 4.3-265;
        Core Vol 6, Part B, 4.5.17.1 and Vol 4, Part E, 7.3.30.
    """
    link = await establish(
        tester,
        central=central,
        interval=8,
        timeout=LL_SUPERVISION_TIMEOUT_MAX,
        peer_factory=_FlushTimeoutPeer,
    )
    await tester.command(
        hci.HCI_Write_Automatic_Flush_Timeout_Command(link.handle, 0x640)
    )
    await tester.wait(0)
    first, dropped, third, retry = (
        struct.pack('<HHB', 1, 4, marker) for marker in range(1, 5)
    )

    def enqueue(data, *, flushable=False):
        boundary = (
            hci.HCI_ACL_PB_FIRST_FLUSHABLE
            if flushable
            else hci.HCI_ACL_PB_FIRST_NON_FLUSHABLE
        )
        tester.send(
            bytes(hci.HCI_AclDataPacket(link.handle, boundary, 0, len(data), data))
        )

    async def check_data(expected, *, attempts=1):
        for _ in range(attempts):
            packet = await link.exchange()
            if packet.pdu[1]:
                assert packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.START
                assert packet.pdu[PDU_HEADER_SIZE:] == expected
                return
            assert packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
        raise AssertionError('expected an ACL PDU')

    async def withhold_and_release(data):
        link.withheld_payload = data
        await check_data(data, attempts=4)
        sn = link.nesn
        for _ in range(149):
            await check_data(data)
            assert link.nesn == sn
        link.withheld_payload = None
        await check_data(data)
        assert link.nesn != sn

    enqueue(first)
    enqueue(dropped, flushable=True)
    enqueue(third)
    await withhold_and_release(first)
    await check_data(third, attempts=4)
    await link.exchange()

    await tester.wait(0)
    enqueue(retry, flushable=True)
    await withhold_and_release(retry)
    await link.exchange()
    await tester.wait(0)

    credits = 0
    while event := tester.take(
        lambda packet: isinstance(packet, hci.HCI_Number_Of_Completed_Packets_Event)
    ):
        assert event.connection_handles == [link.handle]
        credits += sum(event.num_completed_packets)
    assert credits == 4
    await link.disconnect()


# Verify the queue deadline and the ARQ boundary in both connection roles.
PROCEDURES |= procedure_cases(
    flush_timeout,
    {
        'LL/CON/CEN/BV-176-C': (
            'Flush Timeout for flushable and non-flushable ACL Data packets, Central',
            {'central': True},
        ),
        'LL/CON/PER/BV-180-C': (
            (
                'Flush Timeout for flushable and non-flushable ACL Data packets, '
                'Peripheral'
            ),
            {'central': False},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_acl_data(air_dut, identifier, description, procedure, parameters, marks):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
