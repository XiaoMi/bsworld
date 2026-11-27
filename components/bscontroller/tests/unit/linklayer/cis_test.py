# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from bscontroller import hci
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_ENCRYPTION_COUNTER_BITS,
    LL_RESPONSE_TIMEOUT_US,
    LL_SCA_50_PPM,
    LL_SCA_500_PPM,
    ControlOpcode,
)
from bscontroller.linklayer.api import CigConfig, CisConfig
from bscontroller.linklayer.cig_schedule import available_cig_offsets
from bscontroller.linklayer.cis import (
    CIS_NESN,
    CIS_NPI,
    CIS_SN,
    CisDirection,
    CisEvent,
    CisEventParameters,
)
from bscontroller.linklayer.cis_parameters import CisRequest, CisWindow, plan_cig
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.frame_space import FrameSpaceRequest
from bscontroller.linklayer.iso import IsoDataPaths
from bscontroller.linklayer.isoal import IsoSdu, IsoStatus
from bscontroller.linklayer.packet_security import PacketCipher, PacketSecurity
from bscontroller.linklayer.resource import RadioResourceManager
from bscontroller.linklayer.scheduler import RadioPriority, RadioReservation
from bscontroller.linklayer.state import Role
from bscontroller.radio.base import PHY_TIME_UNITS_PER_SECOND, Phy, Received

from .llcp.harness import LlcpHarness

HANDLE = 1
INTERVAL_US = 10_000
ANCHOR_US = 2000


def make_event(outbound, inbound, responses, *, framed=False, cig_delay_us=1000):
    parameters = CisEventParameters(
        outbound, inbound, INTERVAL_US, 0, 1, framed, 1000, cig_delay_us
    )
    listener = Mock()
    iso = IsoDataPaths(listener)
    for transmit, direction in ((True, outbound), (False, inbound)):
        if direction.bn:
            iso.add(
                HANDLE,
                transmit,
                direction.isoal(framed=framed, interval_us=INTERVAL_US),
            )
            iso.get_stream(HANDLE, transmit).configured = True
    radio = SimpleNamespace(
        now_us=0,
        earliest_start_us=Mock(return_value=1),
        transmit=AsyncMock(side_effect=[2120, 12120]),
        receive=AsyncMock(side_effect=responses),
    )
    link = SimpleNamespace(
        radio=radio,
        now_us=0,
        trace=Mock(),
        resource=RadioResourceManager(lambda: radio.now_us, lambda _owner: []),
    )
    connection = SimpleNamespace(
        role=Role.CENTRAL,
        clock_rate=PHY_TIME_UNITS_PER_SECOND,
        link=link,
        radio=radio,
        closed=False,
        last_anchor_rx_us=0,
        parameters=SimpleNamespace(channel_map=(1 << 37) - 1),
        controls=SimpleNamespace(
            power=SimpleNamespace(
                transmit_iso_power_dbm=Mock(return_value=0),
                received_iso=Mock(),
            )
        ),
        packet_security=PacketSecurity(),
    )
    event = CisEvent(
        connection, HANDLE, 0x9A328370, 0x123456, ANCHOR_US, parameters, iso=iso
    )
    return event, radio, listener


def test_central_cis_supervision_waits_for_acl_ack():
    """The six-interval limit starts at the next CIS anchor after a late ACK."""
    direction = CisDirection(Phy.LE_1M, 20, 1, 1, INTERVAL_US, 20)
    event, radio, _ = make_event(direction, direction, [])

    assert event.awaiting_indication_ack
    assert event.deadline_us is None
    radio.now_us = ANCHOR_US + 2 * INTERVAL_US + 1
    event.start_supervision()
    assert not event.awaiting_indication_ack
    assert event.deadline_us == ANCHOR_US + (3 + 6) * INTERVAL_US


def test_cis_nonce_uses_current_acl_cipher_and_checks_counter():
    """CIS derives its IV from the current ACL and shares its counter bound."""
    direction = CisDirection(Phy.LE_1M, 20, 1, 1, INTERVAL_US, 20)
    event, _, _ = make_event(direction, direction, [])
    security = event.connection.packet_security
    security.tx_enabled = security.rx_enabled = True
    header, payload, count = 2, b'CIS payload', 7
    access_address_mask = event.access_address.to_bytes(4, 'little') + bytes(4)
    for marker in (0x2A, 0x55):
        key, iv = bytes([marker]) * 16, bytes([marker]) * 8
        security.cipher = PacketCipher(key, iv, central=True)
        expected_iv = bytes(x ^ y for x, y in zip(iv, access_address_mask))
        aes = AESCCM(key, tag_length=4)
        central_counter = bytes([count, 0, 0, 0, 0x80])
        peripheral_counter = bytes([count, 0, 0, 0, 0])
        sent = aes.encrypt(central_counter + expected_iv, payload, bytes([header]))
        assert event._crypt(header, payload, count, transmit=True) == sent
        assert event._crypt(header, payload, count, transmit=True) == sent
        wire = aes.encrypt(peripheral_counter + expected_iv, payload, bytes([header]))
        assert event._crypt(header, wire, count, transmit=False) == payload
        assert security.cipher.tx_counter == security.cipher.rx_counter == 0

    with pytest.raises(OverflowError, match='packet counter exhausted'):
        event._crypt(header, payload, 1 << LL_ENCRYPTION_COUNTER_BITS, transmit=True)


def test_central_acl_and_cis_keep_fractional_phase_when_clock_accuracy_changes(
    connection,
):
    """A 7.5-ms interval adds 1.875 us of drift per event in loose-clock mode."""
    connection.role = Role.CENTRAL
    connection.parameters = replace(connection.parameters, interval=6)
    direction = CisDirection(Phy.LE_1M, 0, 0, 1, 7500, 0)
    parameters = CisEventParameters(direction, direction, 7500, 0, 1, False, 400, 400)
    offset = 2000
    event = CisEvent(
        connection,
        HANDLE,
        0x9A328370,
        0x123456,
        connection.anchor_us + offset,
        parameters,
        iso=connection.link.roles.iso,
    )
    for accuracy, elapsed in ((LL_SCA_500_PPM, 60_015), (LL_SCA_50_PPM, 60_000)):
        connection.link.sleep_clock_accuracy = accuracy
        start = connection.anchor_us
        reservation = event.reservation()
        for _ in range(8):
            connection.step_event()
            event._close_subevent()
            assert event.anchor_us == connection.anchor_us + offset
            assert reservation.window_after(event.next_us)[0] == event.next_us
        assert connection.anchor_us - start == elapsed


@pytest.mark.asyncio
@pytest.mark.parametrize('ft', [1, 2])
async def test_retry_stops_at_flush_point(ft):
    """A payload outlives its first event only if its negotiated FT permits it.

    Core Vol 6, Part B, 4.5.9 and 4.5.13.5. Use BN=NSE=1 so the flush point
    falls at the end of a CIS event. Drop the first acknowledgment. Verify
    the exact payload and SN sent in the next event and the quality counters.
    """
    outbound = CisDirection(Phy.LE_1M, 20, 1, ft, INTERVAL_US, 20)
    inbound = CisDirection(Phy.LE_1M, 0, 0, 1, INTERVAL_US, 0)
    ack = 0x40 | (CIS_NESN if ft == 2 else 0)
    event, radio, _ = make_event(
        outbound, inbound, [None, Received(bytes([ack, 0]), -40, 12270, 12350, True)]
    )
    stream = event.iso.get_stream(HANDLE, True)
    stream.enqueue_sdu(IsoSdu(1, b'first', 0))
    stream.enqueue_sdu(IsoSdu(2, b'next', ANCHOR_US + 1))
    await event.advance(is_current=lambda: True)
    await event.advance(is_current=lambda: True)

    expected = bytes([CIS_SN, 4]) + b'next' if ft == 1 else b'\x00\x05first'
    assert [call.args[1] for call in radio.transmit.await_args_list] == [
        b'\x00\x05first',
        expected,
    ]
    assert event.link_quality() == (
        (1, 0, 0, 0, 0, 0, 0) if ft == 1 else (0, 0, 0, 1, 0, 0, 0)
    )
    assert stream.tx_sync.sequence_number == (2 if ft == 1 else 1)


@pytest.mark.asyncio
async def test_cis_transmit_sync_uses_group_reference():
    """A CIS later in the group uses the same reference as the first CIS."""
    outbound = CisDirection(Phy.LE_1M, 20, 1, 1, INTERVAL_US, 15)
    inbound = CisDirection(Phy.LE_1M, 0, 0, 1, INTERVAL_US, 0)
    event, radio, _ = make_event(
        outbound, inbound, [None], framed=True, cig_delay_us=1500
    )
    stream = event.iso.get_stream(HANDLE, True)
    stream.enqueue_sdu(IsoSdu(1, b'test', 300))
    await event.advance(is_current=lambda: True)

    timestamp = ANCHOR_US - 500
    assert stream.tx_sync.timestamp_us == timestamp
    assert stream.tx_sync.offset_us == timestamp - 300
    packet = radio.transmit.await_args.args[1]
    assert int.from_bytes(packet[4:7], 'little') == timestamp - 300


@pytest.mark.asyncio
async def test_null_pdu_does_not_deliver_an_empty_sdu():
    """CIS Header bit 6 is NPI. Null PDUs carry no payload number (6.B.2.6.1)."""
    outbound = CisDirection(Phy.LE_1M, 0, 0, 1, INTERVAL_US, 0)
    inbound = CisDirection(Phy.LE_1M, 20, 1, 1, INTERVAL_US, 20)
    event, radio, listener = make_event(
        outbound, inbound, [Received(b'\x40\x00', -40, 2270, 2350, True)]
    )
    await event.advance(is_current=lambda: True)
    event.iso.flush(2350)

    assert radio.transmit.await_args.args[1] == b'\x40\x00'
    assert event.link_quality() == (0, 0, 0, 0, 0, 1, 0)
    handle, sdu = listener.on_iso_received.call_args.args
    assert (handle, sdu.status) == (HANDLE, IsoStatus.LOST)


def test_cig_offset_window_stays_clear_through_fractional_clock_cycle():
    """Either edge offered to the peer must fit for every rounding of a 7.5-ms event."""
    direction = CisDirection(Phy.LE_1M, 20, 1, 1, 7500, 20)
    parameters = CisEventParameters(direction, direction, 7500, 0, 1, False, 1000, 1000)
    reserved = RadioReservation(
        1030, 3000, 7501, interval_fraction=875_000, start_fraction=750_000
    )
    period = 7500 * (PHY_TIME_UNITS_PER_SECOND + 250)
    window = available_cig_offsets(parameters, period, [reserved], 0, 100, 7420)
    assert window is not None
    for offset in window:
        for event in range(8):
            start = offset + event * period // PHY_TIME_UNITS_PER_SECOND
            busy, free = reserved.window_after(start)
            assert free <= start or start + parameters.cig_sync_delay_us <= busy


@pytest.mark.parametrize(
    'recurring,expected',
    [(None, (2000, 9000)), (RadioReservation(65_000, 1000, 10_000), (6000, 9000))],
    ids=['one-shot-only', 'mixed-reservations'],
)
def test_cig_offsets_project_only_recurring_windows(recurring, expected):
    """A PAwR response cannot reduce the period used to find a CIG phase."""
    direction = CisDirection(Phy.LE_1M, 20, 1, 1, INTERVAL_US, 20)
    parameters = CisEventParameters(
        direction, direction, INTERVAL_US, 0, 1, False, 1000, 1000
    )
    response = RadioReservation(66_500, 80, priority=RadioPriority.PERIODIC_RX)
    reservations = [response] if recurring is None else [response, recurring]
    period = INTERVAL_US * PHY_TIME_UNITS_PER_SECOND

    assert (
        available_cig_offsets(parameters, period, reservations, 60_000, 2000, 9000)
        == expected
    )


def test_framed_cig_checks_actual_transport_latency():
    """A valid HCI latency field can still be too short for a framed SDU."""
    config = CigConfig(
        1,
        10_000,
        10_000,
        0,
        0,
        1,
        5,
        5,
        (CisConfig(1, 16, 16, 1, 1),),
    )
    with pytest.raises(ValueError, match='transport latency'):
        plan_cig(config)


def test_cig_reduces_advisory_repetitions_when_durations_overlap():
    """A CIG with two CISes can fit its streams but not every preferred retry."""
    config = CigConfig(
        1,
        20_000,
        20_000,
        0,
        0,
        0,
        40,
        40,
        (CisConfig(1, 251, 251, 1, 1, 3, 3), CisConfig(2, 251, 251, 1, 1, 3, 3)),
    )
    first, second = plan_cig(config).values()
    assert first.nse == second.nse == 1
    assert 0 < second.cis_sync_delay_us < first.cis_sync_delay_us < 20_000


def test_interleaved_cig_keeps_every_subevent_within_iso_interval():
    """Core 6.B.4.5.13.2 also bounds NSE times the interleaved Sub_Interval."""
    first = CisConfig(
        1,
        251,
        251,
        1,
        1,
        nse=2,
        max_pdu_c_to_p=251,
        max_pdu_p_to_c=251,
        bn_c_to_p=1,
        bn_p_to_c=1,
    )
    config = CigConfig(
        1,
        15_000,
        15_000,
        0,
        1,
        0,
        0,
        0,
        (first, replace(first, cis_id=2, nse=1)),
        iso_interval=12,
    )
    with pytest.raises(ValueError, match='available event time'):
        plan_cig(config)


@pytest.fixture
def pending_cis(connection):
    return start_cis_creation(connection)


def start_cis_creation(connection):
    connection.role = Role.CENTRAL
    connection.peer_features = LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_PERIPHERAL
    connection.link.features |= LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM
    manager = connection.link.roles.cig
    config = CigConfig(
        1,
        INTERVAL_US,
        INTERVAL_US,
        0,
        0,
        0,
        30,
        30,
        (CisConfig(1, 20, 20, 1, 1),),
    )
    status, (handle,) = manager.set_cig_parameters(config)
    assert status == ErrorCode.SUCCESS
    pair = ((handle, connection.handle),)
    assert manager.create_cis(pair) == ErrorCode.SUCCESS
    connection.llcp.start_deferred()
    return manager, handle


def transmit_control(connection):
    packet = connection.event.queue.popleft()
    connection.llcp.sent(
        packet.payload,
        length=len(packet.payload),
        first=True,
        procedure=packet.procedure,
    )
    return packet


def test_authenticated_payload_timeout_waits_behind_cis_creation(controller):
    """Keep LE Ping behind the Central's active CIS Creation (Core 6.B.5, 5.4)."""
    central = LlcpHarness(controller, Role.CENTRAL)
    central.enable_encryption()
    manager, _ = start_cis_creation(central.connection)
    central.lt_rx(ControlOpcode.LL_CIS_REQ)
    central.tx_ack()
    assert manager.busy(central.connection)

    encryption = central.encryption
    expires = encryption.last_authenticated_us + encryption.auth_timeout_us
    earliest_ping = expires - 2 * central.connection.listening_interval_us
    assert not central.elapse(earliest_ping - central.radio.now_us)
    central.lt_rx_q_is_empty()
    assert not central.elapse(expires - central.radio.now_us)
    central.ut_rx(
        hci.HCI_Authenticated_Payload_Timeout_Expired_Event,
        connection_handle=central.handle,
    )
    central.lt_rx_q_is_empty()
    assert manager.busy(central.connection)


def check_queued_setup_timeout(connection):
    deadline = connection.radio.now_us + LL_RESPONSE_TIMEOUT_US
    connection.radio.now_us += 2000
    transmit_control(connection)
    connection.radio.now_us = deadline - 1
    assert not connection.llcp.check_timeout()
    assert not connection.closed
    connection.radio.now_us = deadline
    assert connection.llcp.check_timeout()
    assert connection.closed


def test_cis_request_timeout_starts_in_the_acl_queue(connection, pending_cis):
    """Run T_PRT from enqueue through a delayed first LL_CIS_REQ (Core 6.B.5.2)."""
    check_queued_setup_timeout(connection)


def test_cis_response_timeout_starts_at_host_acceptance(connection, controller):
    """Accepting the request starts a fresh 40 s before LL_CIS_RSP transmission."""
    manager = connection.link.roles.cig
    connection.link.features |= LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM
    direction = CisDirection(Phy.LE_1M, 32, 1, 1, 20_000, 32)
    params = CisEventParameters(direction, direction, 20_000, 0, 1, False, 0, 0)
    connection.control(CisRequest(1, 1, params, CisWindow(2000, 2000, 6)).encode())
    (event,) = controller.events
    assert isinstance(event, hci.HCI_LE_CIS_Request_Event)
    connection.radio.now_us += 1000
    assert manager.accept_cis(event.cis_connection_handle) == ErrorCode.SUCCESS
    check_queued_setup_timeout(connection)


@pytest.mark.asyncio
async def test_central_waits_for_first_cis_pdu_before_channel_map_update(
    connection, controller, pending_cis
):
    """An ACL ACK for LL_CIS_IND starts supervision. The first received CIS PDU
    completes CIS Creation and releases the next local procedure (Core 6.B.5.1.15).
    """
    manager, handle = pending_cis
    queue = connection.event.queue
    request = transmit_control(connection).payload
    connection.controls.request_channel_map(0x1F)
    connection.control(
        bytes([ControlOpcode.LL_CIS_RSP]) + CisRequest.decode(request).window.encode()
    )
    indication = transmit_control(connection)
    assert indication.opcode == ControlOpcode.LL_CIS_IND
    connection.llcp.acknowledged(indication.payload, indication.procedure)
    connection.llcp.start_deferred()
    assert not queue

    event = manager.streams[handle].event
    airtime_us = Phy.LE_1M.airtime_us(2)
    start_us = event.anchor_us + airtime_us + event.spacing_us
    connection.radio.incoming.append(
        Received(bytes([CIS_NPI, 0]), -40, start_us, start_us + airtime_us, True)
    )
    await manager.run(event)
    (established,) = controller.events
    assert (established.connection_handle, established.status) == (
        handle,
        ErrorCode.SUCCESS,
    )
    connection.llcp.start_deferred()
    (update,) = queue
    assert update.payload[0] == ControlOpcode.LL_CHANNEL_MAP_IND


def test_iso_test_commands_require_an_established_cis(controller, pending_cis):
    """A configured data path does not make a CIS available for ISO testing.

    Core Vol 4, Part E, 7.8.111–114. The pending CIS handle identifies
    existing ISO paths but the peer has not yet established the CIS.
    """
    manager, handle = pending_cis
    assert manager.iso.contains(handle)
    service = controller.le_procedures.ll
    unknown = ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
    for transmit in (False, True):
        assert service.iso.test(handle, transmit, 0) == unknown
        assert manager.iso.get_stream(handle, transmit).test_type is None
    for end in (False, True):
        status, counters = service.iso.counters(handle, end=end)
        assert status == unknown
        assert (counters.received, counters.missed, counters.failed) == (0, 0, 0)


@pytest.mark.parametrize('request_sent', [False, True])
def test_cancel_pending_cis(connection, controller, pending_cis, request_sent):
    """Cancel before LL_CIS_IND and retain the configured CIS ID for reuse.

    Core Vol 4, Part E, 7.1.6 and Vol 6, Part B, 5.1.15. Exercise both
    sides of the first LL_CIS_REQ transmission boundary.
    """
    manager, handle = pending_cis
    request = CisRequest.decode(connection.event.queue[0].payload)
    if request_sent:
        transmit_control(connection)

    reason = ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    assert manager.disconnect(handle, reason) == ErrorCode.SUCCESS
    manager.prepare()
    if request_sent:
        assert not controller.events
        connection.control(bytes([ControlOpcode.LL_CIS_RSP]) + request.window.encode())
        assert [item.payload for item in connection.event.queue] == [
            bytes(
                [
                    ControlOpcode.LL_REJECT_EXT_IND,
                    ControlOpcode.LL_CIS_RSP,
                    ErrorCode.OPERATION_CANCELLED_BY_HOST_ERROR,
                ]
            )
        ]
    else:
        assert not connection.event.queue

    established, disconnected = controller.events
    assert isinstance(established, hci.HCI_LE_CIS_Established_Event)
    assert (established.connection_handle, established.status) == (
        handle,
        ErrorCode.OPERATION_CANCELLED_BY_HOST_ERROR,
    )
    assert isinstance(disconnected, hci.HCI_Disconnection_Complete_Event)
    assert (disconnected.connection_handle, disconnected.reason) == (
        handle,
        ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR,
    )
    assert manager.create_cis(((handle, connection.handle),)) == ErrorCode.SUCCESS


@pytest.mark.parametrize(
    'established', [True, False], ids=['phy-update', 'cis-creation']
)
def test_cis_termination_waits_for_local_procedure(
    connection, controller, pending_cis, established
):
    """Queue CIS Termination after the current local procedure (Core 6.B.5, 5.1.16).

    Steps:
        1. Create the CIS and acknowledge LL_CIS_IND. Leave CIS Creation pending,
           or complete it and start a PHY Update that waits for a response.
        2. Request termination. Complete the outstanding procedure.
        3. Observe LL_CIS_TERMINATE_IND. Request the peer's version before ACK.

    Expected:
        CIS radio activity stops when the termination PDU is queued. Version
        Exchange and Disconnection Complete wait for the Link Layer ACK.
    """
    manager, handle = pending_cis
    llcp = connection.llcp
    queue = connection.event.queue
    request = transmit_control(connection).payload
    connection.control(
        bytes([ControlOpcode.LL_CIS_RSP]) + CisRequest.decode(request).window.encode()
    )
    indication = transmit_control(connection)
    llcp.acknowledged(indication.payload, indication.procedure)
    event = manager.event_for_handle(handle)
    if established:
        event.established = True
        manager._report_established(event)
        assert (
            llcp.request_phy(hci.PhyBit.LE_2M, hci.PhyBit.LE_2M, 0) == ErrorCode.SUCCESS
        )
        assert transmit_control(connection).opcode == ControlOpcode.LL_PHY_REQ

    reason = ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    assert manager.disconnect(handle, reason) == ErrorCode.SUCCESS
    assert manager.disconnect(handle, reason) == ErrorCode.COMMAND_DISALLOWED_ERROR
    llcp.start_deferred()
    assert not queue and tuple(manager.events()) == (event,)
    if established:
        connection.control(
            bytes(
                [
                    ControlOpcode.LL_REJECT_EXT_IND,
                    ControlOpcode.LL_PHY_REQ,
                    ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR,
                ]
            )
        )
    else:
        event.established = True
        manager._report_established(event)
        (created,) = controller.events
        assert created.status == ErrorCode.SUCCESS
    controller.events.clear()

    llcp.start_deferred()
    assert tuple(manager.events()) == ()
    (termination,) = queue
    assert termination.payload == bytes(
        (ControlOpcode.LL_CIS_TERMINATE_IND, 1, 1, reason)
    )
    assert llcp.read_remote_version() == ErrorCode.SUCCESS
    llcp.start_deferred()
    assert list(queue) == [termination] and not controller.events
    queue.popleft()
    llcp.acknowledged(termination.payload, termination.procedure)
    (disconnected,) = controller.events
    assert isinstance(disconnected, hci.HCI_Disconnection_Complete_Event)
    assert disconnected.connection_handle == handle
    llcp.start_deferred()
    assert queue[0].opcode == ControlOpcode.LL_VERSION_IND


@pytest.mark.parametrize('peer_responded', [False, True])
def test_cis_creation_collides_with_frame_space(
    connection, pending_cis, peer_responded
):
    """Apply the Central collision rule before and after LL_CIS_RSP.

    Core Vol 6, Part B, 5.3. The Central rejects Frame Space and continues
    its own creation before the response. Once the peer has responded,
    an incompatible new procedure closes the ACL.
    """
    manager, handle = pending_cis
    request = CisRequest.decode(transmit_control(connection).payload)
    if peer_responded:
        connection.control(bytes([ControlOpcode.LL_CIS_RSP]) + request.window.encode())
    frame_space = (
        bytes([ControlOpcode.LL_FRAME_SPACE_REQ])
        + FrameSpaceRequest(80, 1000, 1, 1).pack()
    )
    connection.llcp.control(frame_space)
    assert connection.closed is peer_responded
    if not peer_responded:
        assert connection.event.queue[0].payload == bytes(
            [
                ControlOpcode.LL_REJECT_EXT_IND,
                ControlOpcode.LL_FRAME_SPACE_REQ,
                ErrorCode.DIFFERENT_TRANSACTION_COLLISION_ERROR,
            ]
        )
        assert manager.busy(connection)
    status, counters = manager.iso_link_quality(handle)
    assert status == ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
    assert counters == (0,) * 7
