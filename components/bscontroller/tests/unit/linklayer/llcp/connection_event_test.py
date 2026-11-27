# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connection event execution and connection lifetime (Core Vol 6, Part B, 4.5).

These scenarios run the real ``Connection.advance_to_us`` event loop on the fake
radio: establishment, supervision, T_terminate, missed events, channel selection,
transmit windows and exchange budgeting. Each test states one rule.

Axes: {establishment (4.5.2, 4.5.5), supervision (4.5.2), T_terminate (5.1.6),
Peripheral event (4.5.5, 4.5.6), missed events and radio conflicts, channel
selection (CSA#1/#2) and exchange budget}. They use the Lower Tester / Upper
Tester harness (llcp/harness.py) for the Host side and fixtures, but drive the
connection event loop and radio directly rather than the step vocabulary.
"""

from dataclasses import replace
from struct import pack
from types import SimpleNamespace

import pytest

from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    LL_DATA_CHANNEL_COUNT,
    LL_SUPERVISION_TIMEOUT_UNIT_US,
    PDU_DATA_PAYLOAD_SIZE_MAX,
    PDU_DATA_PAYLOAD_TIME_UNCODED_MAX_US,
    LlId,
)
from bscontroller.linklayer.connection import Connection
from bscontroller.linklayer.data_length import DataLengths
from bscontroller.linklayer.instant import InstantUpdate
from bscontroller.linklayer.timing import window_widening
from bscontroller.radio.base import (
    PHY_CODED_S2_FACTOR,
    PHY_CODED_S8_FACTOR,
    Phy,
    Received,
)
from tests.unit.helpers import PARAMETERS
from tests.unit.linklayer.llcp.harness import (
    CENTRAL,
    PERIPHERAL,
    ROLES,
    ErrorCode,
    Opcode,
    channel_map_ind,
    connection_update_ind,
    hci,
    terminate_ind,
)

DISCONNECTED = hci.HCI_Disconnection_Complete_Event
INTERVAL_US = PARAMETERS.interval * LL_CONN_INTERVAL_UNIT_US
REMOTE = ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
OTHER_PEER = hci.Address('F0:F1:F2:F3:F4:09')


def run(dut, target_us, **kwargs):
    return dut.loop.run_until_complete(
        dut.connection.advance_to_us(target_us, **kwargs)
    )


def unestablished(dut, role=PERIPHERAL):
    """A connection created by CONNECT_IND that has not yet heard its peer."""
    # A distinct peer: the fixture's reported connection already owns PEER's record.
    connection = Connection(dut.link, PARAMETERS, OTHER_PEER, role, 0)
    dut.link.connections[connection.handle] = connection
    dut.connection = connection
    dut.controller.events.clear()
    return connection


@pytest.mark.parametrize(
    'interval_change', [False, True], ids=['channel-map', 'connection-update']
)
def test_subrated_peripheral_listens_for_instant_ack(peripheral, interval_change):
    """Listen at underlying events until the Central confirms an Instant PDU.

    Purpose: Verify the Peripheral listens outside its subrated schedule when
    returning an Instant acknowledgment (Vol 6, Part B, 5.5.1). A Connection
    Update also requires listening at the preceding event and Instant (5.1.1).

    Steps:
        1. Set factor 5 and continuation zero.
        2. Receive an Instant PDU on event zero, acknowledging it with no reply.
        3. Receive the Central's retransmission at event one.
        4. Receive a valid new packet with the incremented SN.

    Expected:
        - Event one and the event after the retransmission are scheduled.
        - The new packet restores the subrated schedule, with the Connection
          Update exception for the event preceding the Instant.
    """
    connection = peripheral.connection
    peripheral.lt_tx(pack('<B5H', Opcode.LL_SUBRATE_IND, 5, 0, 0, 0, 100))
    peripheral.lt_rx_empty()
    instant = 9
    indication = (
        connection_update_ind(32, instant)
        if interval_change
        else channel_map_ind(0x03, instant)
    )
    peripheral.lt_tx(indication, sn=0)
    peripheral.lt_rx_empty(ack=False)
    peripheral.event_done()

    anchor = connection.anchor_us
    packet = bytes([LlId.CONTROL, len(indication)]) + indication
    peripheral.radio.incoming.append(
        Received(packet, -45, anchor, anchor + Phy.LE_1M.airtime_us(len(packet)), True)
    )
    run(peripheral, anchor)
    assert not peripheral.radio.incoming, 'the Peripheral skipped event one'
    assert connection.event_index == connection.next_scheduled_event == 2

    # The next valid packet toggles the Central's SN and confirms our ACK.
    peripheral.lt_tx_raw(LlId.CONTINUATION, b'', sn=1)
    assert connection.next_scheduled_event == 5
    connection.step_event(4)
    assert connection.next_scheduled_event == (8 if interval_change else instant)


# ---------------------------------------------------------------------------
# Establishment (4.5.2, 4.5.5)
# ---------------------------------------------------------------------------
def test_establishment_failure_is_reported_as_connection_complete(peripheral):
    """A never-heard peer is reported as Connection Complete with failure status.

    Purpose: Verify that a connection created by CONNECT_IND that never hears its
    peer before the first-anchor deadline is reported to the Host as LE Enhanced
    Connection Complete with CONNECTION_FAILED_TO_BE_ESTABLISHED, not a
    disconnection (4.5.2, 4.5.5).

    Steps:
        1. Build an unestablished Peripheral connection with anchor at 200 ms.
        2. Advance the event loop to just before the anchor.

    Expected:
        - Host gets LE Enhanced Connection Complete
          (CONNECTION_FAILED_TO_BE_ESTABLISHED).
    """
    connection = unestablished(peripheral)
    connection.anchor_us = 200_000
    assert run(peripheral, 180_000) == 180_000
    peripheral.ut_rx(
        hci.HCI_LE_Enhanced_Connection_Complete_Event,
        status=ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR,
    )


def test_establishment_failure_after_creation_is_a_disconnection(peripheral):
    """After the connection was reported created, a setup failure is a disconnect.

    Purpose: Verify that once the connection has already been reported to the Host
    as created, a later establishment failure is surfaced as Disconnection
    Complete (reason CONNECTION_FAILED_TO_BE_ESTABLISHED) rather than a second
    Connection Complete (4.5.2, 4.5.5).

    Steps:
        1. Build an unestablished Peripheral connection and report it created.
        2. Set the anchor and advance the event loop to just before it.

    Expected:
        - Host gets Disconnection Complete (status SUCCESS, reason
          CONNECTION_FAILED_TO_BE_ESTABLISHED).
    """
    connection = unestablished(peripheral)
    connection.report_created()
    peripheral.controller.events.clear()
    connection.anchor_us = 200_000
    run(peripheral, 180_000)
    peripheral.ut_rx(
        DISCONNECTED,
        status=ErrorCode.SUCCESS,
        reason=ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR,
    )


def test_creation_is_reported_once(peripheral):
    """Connection creation is reported exactly once, with no extra CSA event.

    Purpose: Verify that repeated report_created calls and a later establish do
    not emit a second LE Enhanced Connection Complete, and that no LE Channel
    Selection Algorithm event is produced for this path; a connection snapshot is
    available in between (4.5.5).

    Steps:
        1. Build an unestablished Peripheral connection and report it created.
        2. Take a connection snapshot, then report created again and establish.

    Expected:
        - The snapshot's handle matches the connection.
        - Exactly one LE Enhanced Connection Complete event is present.
        - No LE Channel Selection Algorithm event is present.
    """
    connection = unestablished(peripheral)
    connection.report_created()
    snapshot = peripheral.controller.le_procedures.ll.connection_snapshot(
        connection.handle
    )
    assert snapshot.handle == connection.handle
    connection.report_created()
    connection.establish()
    events = peripheral.controller.events
    assert (
        sum(
            isinstance(e, hci.HCI_LE_Enhanced_Connection_Complete_Event) for e in events
        )
        == 1
    )
    assert not any(
        isinstance(e, hci.HCI_LE_Channel_Selection_Algorithm_Event) for e in events
    )


def test_bad_crc_reception_establishes_without_accepting_payload(peripheral):
    """A first bad-CRC reception establishes the link but accepts no payload.

    Purpose: Verify that the Peripheral's first reception, even with a failing
    CRC, establishes the connection without acknowledging or delivering the
    payload (4.5.5).

    Steps:
        1. Build an unestablished Peripheral connection and report it created.
        2. Queue a bad-CRC START PDU carrying 'abc' and run one receive window.

    Expected:
        - The connection becomes established with rx_nesn still 0.
        - No ACL data is delivered to the Host.
    """
    connection = unestablished(peripheral)
    connection.report_created()
    peripheral.radio.incoming.append(
        Received(bytes([LlId.START, 3]) + b'abc', -45, 1300, 1404, False)
    )
    peripheral.loop.run_until_complete(connection.receive(7, 2000))
    assert connection.established and connection.event.rx_nesn == 0
    assert peripheral.acl_received() == []


@pytest.mark.parametrize('pdu', [b'', b'\x01', b'\x02\x02x', b'\x02\x1c' + bytes(28)])
def test_invalid_data_pdu_does_not_establish(peripheral, pdu):
    """A malformed first data PDU neither establishes the link nor reaches the Host.

    Purpose: Verify that a structurally invalid first data PDU does not establish
    the Peripheral connection and delivers nothing to the Host (4.5.5). Runs over
    an empty PDU, a one-byte PDU, a length/payload mismatch and an over-length
    PDU.

    Steps:
        1. Build an unestablished Peripheral connection.
        2. Feed the malformed PDU into Connection.accept with a valid CRC.

    Expected:
        - The connection is still not established.
        - Nothing reaches the Host.
    """
    connection = unestablished(peripheral)
    connection.accept(pdu, True)
    assert not connection.established
    peripheral.ut_rx_q_is_empty()


def test_rssi_is_recorded_from_valid_reception(central):
    """A valid reception records RSSI, clamped to the reportable minimum.

    Purpose: Verify that a valid reception updates the connection's RSSI, and that
    an out-of-range value is clamped to the minimum reportable level (-127)
    (4.5.2).

    Steps:
        1. Queue a valid reception with an RSSI of -200.
        2. Run one receive window.

    Expected:
        - connection.rssi is -127.
    """
    central.radio.incoming.append(Received(b'\x01\x00', -200, 1300, 1380, True))
    central.loop.run_until_complete(central.connection.receive(7, 2000))
    assert central.connection.rssi == -127


# ---------------------------------------------------------------------------
# Supervision (4.5.2)
# ---------------------------------------------------------------------------
def test_supervision_timeout_uses_last_valid_rx(peripheral):
    """Supervision timeout is measured from the last valid reception.

    Purpose: Verify that the supervision timer is anchored on the last valid RX
    time, so the link closes when that interval elapses with a Connection Timeout
    (4.5.2).

    Steps:
        1. Set the last valid RX time and a later anchor.
        2. Advance the event loop to the supervision deadline.

    Expected:
        - Host gets Disconnection Complete (CONNECTION_TIMEOUT).
    """
    connection = peripheral.connection
    connection.last_rx_us = 80
    connection.anchor_us = 1_100_000
    assert run(peripheral, 1_000_080) == 1_000_080
    peripheral.ut_rx(DISCONNECTED, reason=ErrorCode.CONNECTION_TIMEOUT_ERROR)


@pytest.mark.parametrize('established', [False, True])
def test_earlier_software_timer_does_not_expire_connection(peripheral, established):
    """An earlier software-timer boundary does not prematurely close the link.

    Purpose: Verify that when the radio returns early at an unrelated software
    timer boundary, the connection (established or still being set up) is not
    closed before its real deadline; it only closes once time reaches that
    deadline (4.5.2). Runs for the established and unestablished cases.

    Steps:
        1. Pick the established connection or build an unestablished one, with the
           matching deadline.
        2. Wrap advance_to_us so it returns early at 1000 us; run to the deadline.
        3. Restore advance_to_us and run to the deadline again.

    Expected:
        - After the early return the connection is open and still registered.
        - After the real advance the connection is closed.
    """
    connection = peripheral.connection if established else unestablished(peripheral)
    deadline = 1_000_000 if established else 180_000
    connection.anchor_us = deadline + LL_CONN_INTERVAL_UNIT_US
    advance = peripheral.radio.advance_to_us

    async def timer_boundary(target):
        return await advance(min(target, 1000))

    peripheral.radio.advance_to_us = timer_boundary
    assert run(peripheral, deadline) == 1000
    assert not connection.closed and connection.handle in peripheral.link.connections
    peripheral.radio.advance_to_us = advance
    assert run(peripheral, deadline) == deadline
    assert connection.closed


def test_widened_rx_search_stops_at_supervision_deadline(peripheral):
    """The widened RX search never runs past the supervision deadline.

    Purpose: Verify that the Peripheral's window-widened receive search is bounded
    by the supervision deadline: time stops exactly at the deadline and the link
    closes (4.5.2).

    Steps:
        1. Set the anchor just before the supervision deadline.
        2. Run the event loop to that anchor.

    Expected:
        - Time advances to the supervision deadline and the connection is closed.
    """
    connection = peripheral.connection
    deadline = connection.supervision_deadline_us
    connection.anchor_us = deadline - 80
    run(peripheral, connection.anchor_us)
    assert peripheral.radio.now_us == deadline and connection.closed


# ---------------------------------------------------------------------------
# T_terminate (5.1.6)
# ---------------------------------------------------------------------------
def test_termination_timer_is_not_restarted(peripheral):
    """T_terminate is set once and never restarted by later events.

    Purpose: Verify that after terminate(), the termination deadline is fixed from
    the first call and is not moved by a second terminate() or by a later valid
    reception, so the link closes at the original deadline (5.1.6).

    Steps:
        1. At t=100 call terminate(); record the deadline.
        2. At t=200 call terminate() again and accept a valid Empty PDU.
        3. Advance to the deadline and check the timeout.

    Expected:
        - The deadline equals 100 + supervision timeout and is unchanged by the
          second terminate and the reception (last_rx_us tracks the reception).
        - The next operation is at the deadline; check_timeout fires there.
        - Host gets Disconnection Complete (CONNECTION_TERMINATED_BY_LOCAL_HOST).
    """
    connection = peripheral.connection
    peripheral.radio.now_us = 100
    connection.terminate(REMOTE)
    deadline = 100 + PARAMETERS.timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
    assert connection.termination_deadline_us == deadline
    peripheral.radio.now_us = 200
    connection.terminate(REMOTE)
    connection.accept(bytes([LlId.CONTINUATION, 0]), True)
    assert connection.last_rx_us == 200
    assert connection.termination_deadline_us == deadline
    connection.anchor_us = deadline + LL_CONN_INTERVAL_UNIT_US
    assert connection.next_operation_us == deadline
    peripheral.radio.now_us = deadline
    assert connection.check_timeout()
    peripheral.ut_rx(
        DISCONNECTED, reason=ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR
    )


def test_earlier_software_timer_does_not_expire_termination(peripheral):
    """An early software-timer return does not close a terminating link early.

    Purpose: Verify that during local termination, a radio returning early at an
    unrelated software timer boundary does not close the link before T_terminate;
    it closes only once time reaches the termination deadline (5.1.6).

    Steps:
        1. Call terminate() and record the termination deadline.
        2. Wrap advance_to_us to return early at 1000 us; run to the deadline.
        3. Restore advance_to_us and run to the deadline again.

    Expected:
        - After the early return the link is not closed.
        - After the real advance the link is closed.
    """
    connection = peripheral.connection
    connection.terminate(REMOTE)
    deadline = connection.termination_deadline_us
    connection.anchor_us = deadline + LL_CONN_INTERVAL_UNIT_US
    advance = peripheral.radio.advance_to_us

    async def timer_boundary(target):
        return await advance(min(target, 1000))

    peripheral.radio.advance_to_us = timer_boundary
    assert run(peripheral, deadline) == 1000 and not connection.closed
    peripheral.radio.advance_to_us = advance
    assert run(peripheral, deadline) == deadline and connection.closed


@pytest.mark.parametrize('role', ROLES)
def test_remote_termination_inside_event(iut, role):
    """The event loop closes the link right after acknowledging TERMINATE_IND.

    Purpose: Verify, through the real event loop, that a peer TERMINATE_IND is
    acknowledged and the link is then closed, for both Central and Peripheral
    roles (5.1.6). Runs for both roles.

    Steps:
        1. Queue a TERMINATE_IND reception at the appropriate anchor for the role.
        2. Run the event loop over the event(s) needed to acknowledge it.

    Expected:
        - The connection is closed.
        - Host gets Disconnection Complete (REMOTE_USER_TERMINATED).
    """
    dut = iut(role)
    connection = dut.connection
    packet = bytes([LlId.CONTROL, 2]) + terminate_ind(REMOTE)
    if role == PERIPHERAL:
        start = connection.anchor_us
        dut.radio.incoming.append(Received(packet, -45, start, start + 112, True))
        run(dut, connection.anchor_us)
    else:
        connection.event.queue.clear()
        dut.radio.incoming.append(Received(packet, -45, 1480, 1592, True))
        run(dut, LL_CONN_INTERVAL_UNIT_US)
        run(dut, connection.anchor_us)
    assert connection.closed
    dut.ut_rx(DISCONNECTED, reason=REMOTE)


# ---------------------------------------------------------------------------
# Peripheral event (4.5.5, 4.5.6)
# ---------------------------------------------------------------------------
def test_peripheral_listens_from_widening_and_adopts_anchor(peripheral):
    """The Peripheral adopts the received packet start as its new anchor.

    Purpose: Verify that the Peripheral listens from a window-widened start, and
    on receiving a packet adopts that packet's start as the event anchor, with the
    reply following after T_IFS (4.5.5, 4.5.6).

    Steps:
        1. Compute the widened window for the current anchor.
        2. Queue a valid reception a few microseconds after the anchor.
        3. Run the event loop over the anchor.

    Expected:
        - The next anchor is the packet start plus one connection interval.
        - The reply is sent 150 us (T_IFS) after the packet ends.
        - event_counter is 1, no update window pending, and widening is
          non-negative.
    """
    connection = peripheral.connection
    anchor = connection.anchor_us
    widening = window_widening(anchor - connection.last_rx_us, PARAMETERS.sca)
    received = anchor + 3
    peripheral.radio.incoming.append(
        Received(b'\x01\x00', -45, received, received + 80, True)
    )
    run(peripheral, anchor)
    # The packet start becomes this event's anchor; the reply follows T_IFS.
    assert connection.anchor_us == received + INTERVAL_US
    assert peripheral.radio.sent[-1][0] == received + 80 + 150
    assert connection.event_counter == 1 and not connection.update_window
    assert widening >= 0


@pytest.mark.parametrize('update_window', (False, True))
def test_peripheral_clock_correction_moves_only_its_cis(
    peripheral, monkeypatch, update_window
):
    """An observed clock offset moves the CIS; a new ACL transmit window does not."""
    connection = peripheral.connection
    connection.update_window = update_window
    same_acl = SimpleNamespace(
        connection=connection, anchor_us=20_000, established=False
    )
    other_acl = SimpleNamespace(connection=object(), anchor_us=20_000)
    monkeypatch.setattr(
        peripheral.link.roles.cig, 'events', lambda: iter((same_acl, other_acl))
    )
    scheduled = connection.anchor_us
    observed = scheduled + 3
    peripheral.radio.incoming.append(
        Received(bytes((1, 0)), -45, observed, observed + 80, True)
    )
    run(peripheral, scheduled)
    assert connection.last_anchor_rx_us == observed
    assert connection.anchor_us == observed + INTERVAL_US
    assert same_acl.anchor_us == (20_000 if update_window else 20_003)
    assert other_acl.anchor_us == 20_000


def test_peripheral_two_crc_errors_close_the_event(peripheral):
    """Two CRC errors in one event stop the Peripheral from replying again.

    Purpose: Verify that the Peripheral replies after the first CRC-failed
    reception but, on a second CRC error in the same event, closes the event
    without a further reply (4.5.6).

    Steps:
        1. Queue three bad-CRC receptions within the event.
        2. Run the event loop over the anchor.

    Expected:
        - Only one reply is sent.
        - One queued reception remains unconsumed (the event ended early).
    """
    connection = peripheral.connection
    anchor = connection.anchor_us
    for index in range(3):
        start = anchor + index * 500
        peripheral.radio.incoming.append(
            Received(b'\x01\x00', -45, start, start + 80, False)
        )
    run(peripheral, anchor)
    # One reply after the first CRC error, none after the second.
    assert len(peripheral.radio.sent) == 1
    assert len(peripheral.radio.incoming) == 1


def test_peripheral_md_continues_the_event(peripheral):
    """A peer MD bit keeps the Peripheral event open for a second exchange.

    Purpose: Verify that when a received PDU sets the More Data bit, the
    Peripheral continues the event and transmits again for the following reception
    (4.5.6).

    Steps:
        1. Queue a reception with MD set, then a second valid reception.
        2. Run the event loop over the anchor.

    Expected:
        - Two replies are sent (the event stayed open for the MD exchange).
    """
    from bscontroller.linklayer import PDU_DATA_MD_MASK

    connection = peripheral.connection
    anchor = connection.anchor_us
    peripheral.radio.incoming.append(
        Received(
            bytes([LlId.CONTINUATION | PDU_DATA_MD_MASK, 0]),
            -45,
            anchor,
            anchor + 80,
            True,
        )
    )
    peripheral.radio.incoming.append(
        Received(b'\x0d\x00', -45, anchor + 500, anchor + 580, True)
    )
    run(peripheral, anchor)
    assert len(peripheral.radio.sent) == 2


@pytest.mark.parametrize('role', ROLES)
def test_update_receive_window_survives_missed_events(iut, role, monkeypatch):
    """A pending receive-window update persists across missed events.

    Purpose: Verify that while an update_window is pending, missed events do not
    clear it or advance the last valid RX time, and each listen uses the correct
    window-widened start and bounded end derived from the anchor (4.5.5, 4.5.6).
    The IUT is forced to the Peripheral role regardless of the parametrized role.

    Steps:
        1. Build a Peripheral connection with update_window set.
        2. Patch receive to miss (recording the window) and advance; repeat twice.

    Expected:
        - After each miss the update_window stays set and last_rx_us is unchanged.
        - Each recorded start equals anchor minus widening, and each end equals
          the widened window end bound.
    """
    dut = iut(PERIPHERAL)
    connection = dut.connection
    connection.update_window = True
    original_rx = connection.last_rx_us
    windows = []

    async def miss(channel, until_us, **parameters):
        windows.append((connection.anchor_us, until_us, parameters['start_us']))
        dut.radio.now_us = until_us

    monkeypatch.setattr(connection, 'receive', miss)
    for _ in range(2):
        run(dut, connection.anchor_us)
        assert connection.update_window and connection.last_rx_us == original_rx
    for anchor, until, start in windows:
        widening = window_widening(anchor - original_rx, PARAMETERS.sca)
        assert start == anchor - widening
        assert until == (
            anchor
            + PARAMETERS.window_size * LL_CONN_INTERVAL_UNIT_US
            + widening
            + connection.rx_phy.address_us
            - 1
        )
    assert role in ROLES


# ---------------------------------------------------------------------------
# Missed events and radio conflicts
# ---------------------------------------------------------------------------
def test_skipped_events_apply_instant_and_advance_csa1(central):
    """Skipping missed events applies a pending Instant and advances CSA#1 state.

    Purpose: Verify that skip_missed_events fast-forwards the event counter,
    applies a pending channel-map InstantUpdate at its Instant, and advances the
    CSA#1 unmapped-channel state consistently, without transmitting (4.5.6).

    Steps:
        1. Accept a channel-map InstantUpdate at event 1.
        2. Move time two intervals past the anchor and skip missed events.

    Expected:
        - event_counter becomes 3 and the unmapped channel follows CSA#1 hop.
        - The channel map reflects the applied Instant.
        - No pending update remains and nothing was transmitted.
    """
    connection = central.connection
    connection.controls.accept_instant(
        InstantUpdate(1, channel_map=(1 << 2) | (1 << 8))
    )
    central.radio.now_us = connection.anchor_us + 2 * INTERVAL_US + 1
    connection.skip_missed_events()
    assert connection.event_counter == 3
    assert connection.unmapped_channel == 3 * PARAMETERS.hop % LL_DATA_CHANNEL_COUNT
    assert connection.parameters.channel_map == (1 << 2) | (1 << 8)
    assert connection.controls.pending is None and not central.radio.sent


def test_peripheral_skips_only_after_its_widened_window(peripheral):
    """The Peripheral counts an event as missed only past its widened window.

    Purpose: Verify that the Peripheral does not treat an event as missed while
    time is still within its window-widened listen; it only advances the event
    counter once time is well past the anchor (4.5.6).

    Steps:
        1. Move time just past the anchor and skip missed events.
        2. Move time half an interval past the anchor and skip again.

    Expected:
        - Just past the anchor the event_counter stays 0.
        - Half an interval later the event_counter is 1.
    """
    connection = peripheral.connection
    peripheral.radio.now_us = connection.anchor_us + 1
    connection.skip_missed_events()
    assert connection.event_counter == 0
    peripheral.radio.now_us = connection.anchor_us + INTERVAL_US // 2
    connection.skip_missed_events()
    assert connection.event_counter == 1


def test_radio_conflict_skips_without_consuming_pending_acl(central):
    """A radio conflict skips the event without losing queued ACL data.

    Purpose: Verify that when the radio is unavailable for the event window, the
    Central skips the event (advancing the event counter) without transmitting or
    consuming its queued ACL data (4.5.6).

    Steps:
        1. Queue ACL data 'keep'.
        2. Advance with the radio reserved until just past the anchor.

    Expected:
        - Nothing is transmitted and event_counter is 1.
        - The queued 'keep' data is still at the tail of the queue.
    """
    connection = central.connection
    connection.enqueue_acl(False, b'keep')
    run(central, connection.anchor_us + 100, radio_until_us=connection.anchor_us + 100)
    assert not central.radio.sent and connection.event_counter == 1
    assert connection.event.queue[-1].payload == b'keep'


def test_busy_radio_at_anchor_skips_central_event(central):
    """A radio busy past the anchor makes the Central skip the event.

    Purpose: Verify that when the earliest possible start is after the anchor, the
    Central cannot start the event and skips it, advancing the event counter
    without transmitting (4.5.6).

    Steps:
        1. Force earliest_start_us to return just after the anchor.
        2. Run the event loop over the anchor.

    Expected:
        - Nothing is transmitted and event_counter is 1.
    """
    connection = central.connection
    central.radio.earliest_start_us = lambda *a, **k: connection.anchor_us + 1
    run(central, connection.anchor_us)
    assert not central.radio.sent and connection.event_counter == 1


@pytest.mark.parametrize(
    'role,offset', [(CENTRAL, 4), (PERIPHERAL, 4), (PERIPHERAL, 1)]
)
def test_update_instant_rechecks_new_window_before_skipping(iut, role, offset):
    """A parameter-update Instant re-anchors the window before any further skip.

    Purpose: Verify that when a connection-parameter InstantUpdate (new window
    size/offset) is applied during skip_missed_events, the new window is adopted
    and re-checked before skipping continues, so the anchor moves by the offset
    and the receive window is re-armed without transmitting (4.5.6). Runs for
    Central (offset 4) and Peripheral (offsets 4 and 1).

    Steps:
        1. Accept a parameter InstantUpdate at event 1 and set event_counter to 1.
        2. Move time two intervals past the old anchor and skip missed events.

    Expected:
        - No pending update remains and event_counter stays 1.
        - The new anchor equals the old anchor plus offset intervals.
        - update_window is set and nothing was transmitted.
    """
    dut = iut(role)
    connection = dut.connection
    old_anchor = connection.anchor_us
    parameters = replace(connection.parameters, window_size=3, window_offset=offset)
    connection.controls.accept_instant(InstantUpdate(1, parameters=parameters))
    connection.event_counter = 1
    dut.radio.now_us = old_anchor + 2 * LL_CONN_INTERVAL_UNIT_US
    connection.skip_missed_events()
    assert connection.controls.pending is None and connection.event_counter == 1
    assert connection.anchor_us == old_anchor + offset * LL_CONN_INTERVAL_UNIT_US
    assert connection.update_window and not dut.radio.sent


def test_procedure_timeout_inside_skip_stops_the_loop(central):
    """A procedure timeout reached while skipping closes the connection.

    Purpose: Verify that if a procedure response timer (LLCP next deadline) has
    already expired when skip_missed_events runs, the skip detects the timeout and
    closes the connection (5.1.6, 4.5.6).

    Steps:
        1. Start a Version Exchange and move time past the LLCP deadline.
        2. Set last_rx_us to now and the anchor to 0, then skip missed events.

    Expected:
        - The connection is closed.
    """
    connection = central.connection
    central.host_start('prepare_version')
    central.radio.now_us = connection.llcp.next_deadline_us + 1
    central.connection.last_rx_us = central.radio.now_us
    connection.anchor_us = 0
    connection.skip_missed_events()
    assert connection.closed


def test_closed_during_prepare_ends_event(central):
    """A connection closed during prepare ends the event without transmitting.

    Purpose: Verify that when a procedure timeout closes the connection during the
    event's prepare phase, the event loop ends without transmitting (5.1.6,
    4.5.6).

    Steps:
        1. Start a Version Exchange and set the anchor just past the LLCP deadline.
        2. Set last_rx_us and time to the deadline, then run the event loop.

    Expected:
        - The connection is closed and nothing was transmitted.
    """
    connection = central.connection
    central.host_start('prepare_version')
    deadline = connection.llcp.next_deadline_us
    connection.anchor_us = deadline + 10
    connection.last_rx_us = deadline
    central.radio.now_us = deadline
    run(central, deadline + 10)
    assert connection.closed and not central.radio.sent


# ---------------------------------------------------------------------------
# Channel selection and exchange budget
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'channel_map,expected',
    [
        ((1 << LL_DATA_CHANNEL_COUNT) - 1, [7, 14, 21, 28, 35, 5]),
        ((1 << 2) | (1 << 8), [8, 2, 8, 2, 8, 8]),
    ],
)
def test_csa1_uses_live_channel_state(central, channel_map, expected):
    """CSA#1 selects channels from the live channel map and hop state.

    Purpose: Verify that Channel Selection Algorithm #1 produces the expected data
    channel sequence from the connection's current channel map, including remapping
    when only a subset of channels is enabled (4.5.8). Runs for a full map and a
    two-channel map.

    Steps:
        1. Set the connection's channel map.
        2. Run one event per expected channel.

    Expected:
        - The transmitted channels equal the expected CSA#1 sequence.
    """
    central.set_parameters(channel_map=channel_map)
    for _ in expected:
        run(central, central.connection.anchor_us)
    assert [packet[1] for packet in central.radio.sent] == expected


def test_csa2_uses_event_counter(central):
    """CSA#2 derives the channel from the event counter and access address.

    Purpose: Verify that with Channel Selection Algorithm #2 enabled the first
    event's channel equals channel_selection_2 for event 0, the access address and
    the channel map (4.5.8).

    Steps:
        1. Enable CSA#2 on the connection.
        2. Run one event.

    Expected:
        - The transmitted channel equals channel_selection_2(0, access_address,
          channel_map).
    """
    from bscontroller.linklayer.channel_selection import channel_selection_2

    central.connection.csa2 = True
    run(central, central.connection.anchor_us)
    assert central.radio.sent[0][1] == channel_selection_2(
        0, PARAMETERS.access_address, PARAMETERS.channel_map
    )


@pytest.mark.parametrize(
    'phy,coding,expected',
    [
        (Phy.LE_1M, PHY_CODED_S8_FACTOR, 742),
        (Phy.LE_2M, PHY_CODED_S8_FACTOR, 454),
        (Phy.LE_CODED, PHY_CODED_S8_FACTOR, 5046),
        (Phy.LE_CODED, PHY_CODED_S2_FACTOR, 3492),
    ],
)
def test_exchange_reserves_complete_phy_exchange(central, phy, coding, expected):
    """The reserved exchange duration matches the PHY and coding in use.

    Purpose: Verify that first_exchange_us returns the full time for a complete
    packet exchange on the connection's PHY and coding, so an event is only
    started when it fits (4.5.6). Runs for 1M, 2M and Coded (S8 and S2).

    Steps:
        1. Set the connection's TX/RX PHY and TX coding.
        2. Queue a 27-byte ACL fragment and read first_exchange_us.

    Expected:
        - first_exchange_us equals the expected microsecond value for that PHY and
          coding.
    """
    connection = central.connection
    connection.tx_phy = connection.rx_phy = phy
    connection.tx_coding = coding
    connection.enqueue_acl(False, bytes(27))
    assert connection.first_exchange_us() == expected


@pytest.mark.parametrize('encrypted', [False, True])
def test_initial_exchange_does_not_reserve_maximum_ack(central, encrypted):
    """An exchange with max_rx=False does not reserve the maximum acknowledgment.

    Purpose: Verify that budgeting an exchange without reserving a maximum-size RX
    (max_rx=False) stays at the minimal duration even when data-length limits are
    raised and RX encryption is toggled, while the full-RX budget is larger
    (4.5.6). Runs with RX encryption off and on.

    Steps:
        1. Record the minimal exchange with max_rx=False.
        2. Raise local/remote data lengths to the maximum and set RX encryption.
        3. Compare minimal and full-RX exchange durations.

    Expected:
        - first_exchange_us(max_rx=False) still equals the recorded minimal value.
        - first_exchange_us() (full RX) is strictly larger.
    """
    connection = central.connection
    minimal = connection.first_exchange_us(max_rx=False)
    lengths = connection.controls.data_length
    lengths.local = lengths.remote = DataLengths(
        PDU_DATA_PAYLOAD_SIZE_MAX,
        PDU_DATA_PAYLOAD_TIME_UNCODED_MAX_US,
        PDU_DATA_PAYLOAD_SIZE_MAX,
        PDU_DATA_PAYLOAD_TIME_UNCODED_MAX_US,
    )
    connection.packet_security.rx_enabled = encrypted
    assert connection.first_exchange_us(max_rx=False) == minimal
    assert connection.first_exchange_us() > minimal


def test_event_without_room_for_exchange_is_skipped(central):
    """An event with no room for a full exchange is skipped.

    Purpose: Verify that when the radio is available only for less than one full
    exchange, the Central skips the event without transmitting its queued data
    (4.5.6).

    Steps:
        1. Record the duration of an exchange of two Empty PDUs at the anchor.
        2. Queue a 27-byte ACL fragment and advance with less airtime available.

    Expected:
        - Nothing is transmitted and event_counter is 1.
    """
    connection = central.connection
    minimum_exchange_us = connection.first_exchange_us(max_rx=False)
    connection.enqueue_acl(False, bytes(27))
    run(
        central,
        connection.anchor_us,
        radio_until_us=connection.anchor_us + minimum_exchange_us - 1,
    )
    assert not central.radio.sent and connection.event_counter == 1


def test_central_md_exchange_stops_at_event_end(central):
    """A Central MD burst stops at the end of the connection event.

    Purpose: Verify that the Central keeps exchanging while both sides set More
    Data but stops before the connection interval ends, so it neither drains the
    whole queue nor runs past the event boundary (4.5.6).

    Steps:
        1. Shorten the interval and queue 20 large ACL fragments.
        2. Queue 20 MD-bearing receptions spaced through the event.
        3. Run the event loop over the anchor.

    Expected:
        - More than one but fewer than 20 PDUs are sent.
        - Time stays within the connection interval from the start anchor.
    """
    from bscontroller.linklayer import PDU_DATA_MD_MASK

    connection = central.connection
    central.set_parameters(interval=6)
    for _ in range(20):
        connection.enqueue_acl(False, bytes(27))
    start = connection.anchor_us
    for index in range(20):
        at = start + 400 + index * 1000
        central.radio.incoming.append(
            Received(
                bytes([(index & 1) << 3 | (~index & 1) << 2 | 1 | PDU_DATA_MD_MASK, 0]),
                -45,
                at,
                at + 80,
                True,
            )
        )
    run(central, start)
    assert 1 < len(central.radio.sent) < 20
    assert central.radio.now_us < start + 6 * LL_CONN_INTERVAL_UNIT_US
