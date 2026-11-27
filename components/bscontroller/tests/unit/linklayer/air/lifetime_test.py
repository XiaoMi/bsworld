# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Role lifetimes on one shared radio (Core Vol 6, Part B, 4.4; implementation
ownership rules).

A role context can be released by the Host, or replaced by a callback, while an
RF exchange for it is still in flight. These scenarios pin the rule that a stale
context can never commit a connection, a sync or a report, and that radio
arbitration skips events without corrupting counters or timeouts.

Invariants:
    - A released, disabled, reset or replaced advertiser cannot commit a
      connection from a delayed CONNECT_IND or AUX_CONNECT_REQ.
    - A delayed scan/initiation/sync result cannot complete a newer context, and
      a completion callback cannot act on a context freed during the callback.
    - Radio arbitration yields an event (connection, periodic TX/RX) without
      refreshing timers or desynchronizing event counters.
    - Wire identities (access address, CRC init, hop, window offset) are seeded,
      bounded, unique among live owners and reproducible after reset.

Most tests drive a ``RadioLink`` over the ``air/harness.py`` ScriptedRadio with a
``Recorder`` standing in for the HCI listener, so lifetime ordering is observed as
``on_*`` callbacks rather than HCI events; a few exercise the pure backoff and
resource-manager helpers directly. The on-air role behaviour these races guard
is covered in the other ``air/`` files.
"""

import random
from dataclasses import replace

import pytest

from bscontroller import hci
from bscontroller.address import Address
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    LL_DATA_CHANNEL_MAP_ALL,
    LL_EVENT_COUNTER_MASK,
    LL_HOP_INCREMENT_MAX,
    LL_HOP_INCREMENT_MIN,
    LL_PERIODIC_INTERVAL_UNIT_US,
    LL_SCA_500_PPM,
    PDU_ADV_TXADD_MASK,
    PDU_CRC_SIZE,
    PDU_DATA_NESN_MASK,
    PDU_DATA_SN_MASK,
    AdvertisingMode,
    AdvertisingPduType,
    ControlOpcode,
    LlId,
    advertiser,
)
from bscontroller.linklayer.advertising_pdu import ExtendedPdu, SyncInfo
from bscontroller.linklayer.advertising_radio import (
    advertising_request,
    respond_to_aux_request,
)
from bscontroller.linklayer.bitstream import generate_access_address
from bscontroller.linklayer.connection import Connection
from bscontroller.linklayer.discovery import (
    Advertising,
    Initiating,
    PeriodicSync,
    ScanBackoff,
    Scanning,
)
from bscontroller.linklayer.initiating_radio import initiate_extended
from bscontroller.linklayer.link import RadioLink
from bscontroller.linklayer.resource import RadioResourceManager
from bscontroller.linklayer.results import (
    ConnectionAccepted,
    PeriodicPacketReceived,
    ScanEvent,
    SyncInfoReceived,
)
from bscontroller.linklayer.scanning_radio import receive_scan_event
from bscontroller.linklayer.scheduler import (
    RadioPriority,
    RadioReservation,
    first_radio_gap,
    higher_priority_overlap,
)
from bscontroller.linklayer.state import DataStatus, Role
from bscontroller.linklayer.subrating import SubrateSchedule
from bscontroller.radio.base import PHY_OCTET_BITS, Received
from tests.unit.helpers import PARAMETERS, check_access_address
from tests.unit.linklayer.air.harness import (
    CONNECT_PARAMETERS,
    PEER,
    Air,
    AirHarness,
    adv_ind,
    connect_ind,
)

SELF = Address('F0:F1:F2:F3:F4:01')
INFO = SyncInfo(9000, 80, LL_DATA_CHANNEL_MAP_ALL, 0x456789AB, 0x123456, 0)


class Recorder:
    """An LL listener without HCI: lifetime ordering is observed directly."""

    local_version = (14, 0, 0)

    def __init__(self):
        self.events = []

    def trace(self, message):
        pass

    def __getattr__(self, name):
        if name.startswith('on_'):
            return lambda *args, **kwargs: self.events.append((name, args))
        raise AttributeError(name)


@pytest.fixture
def link():
    harness = AirHarness()
    result = RadioLink(harness.radio, Recorder())
    result.loop = harness.loop
    yield result
    harness.close()


def run(link, coroutine):
    return link.loop.run_until_complete(coroutine)


def accepted(peer='C0:11:22:33:44:55', **changes):
    values = dict(
        parameters=PARAMETERS,
        peer=Address(peer),
        role=Role.PERIPHERAL,
        end_us=500,
        self_address=SELF,
        csa2=True,
    )
    return ConnectionAccepted(**(values | changes))


# ---------------------------------------------------------------------------
# Connection commit ownership
# ---------------------------------------------------------------------------
def test_commit_stops_advertiser_and_assigns_handles(link):
    """Committing a connection stops its advertiser and allocates a handle.

    Purpose: Verify that accepting a connection from an advertiser disables that
    advertiser, carries its advertising handle and completed-event count into the
    result, raises on_connected, allocates increasing handles, and reuses a freed
    handle (Vol 6, Part B, 4.4.2).

    Steps:
        1. Publish an enabled advertiser and accept a connection from it.
        2. Accept a second connection from another peer.
        3. Delete the first connection and allocate a handle again.

    Expected:
        - The first connection gets handle 1, the advertiser is disabled, and the
          advertising handle and event count are carried through.
        - The listener saw exactly on_connected for the first connection.
        - The second connection gets handle 2.
        - After deleting the first, the next allocated handle is 1 again.
    """
    item = Advertising(SELF, handle=4, enabled=True, events=7)
    link.roles.advertising.publish(item)
    first = link.roles.accepted(accepted(advertiser=item))
    assert first.handle == 1 and not item.enabled
    assert (first.advertising_handle, first.advertising_events) == (4, 7)
    assert link.listener.events == [('on_connected', (first,))]
    second = link.roles.accepted(accepted('C0:11:22:33:44:66'))
    assert second.handle == 2
    del link.connections[first.handle]
    assert link.roles.connections.allocate_handle() == 1


@pytest.mark.parametrize('action', ['disabled', 'removed'])
def test_released_advertiser_cannot_commit(link, action):
    """A released advertiser cannot commit a connection.

    Purpose: Verify that once an advertiser is disabled or removed, a late
    connection-accept for it commits nothing and raises no event (Vol 6, Part B,
    4.4.2).

    Steps:
        1. Publish an enabled advertiser.
        2. Disable it or remove it, per the action parameter.
        3. Accept a connection naming that advertiser.

    Expected:
        - The accept returns None.
        - No connection exists and no listener event was raised.

    Parameters range over the advertiser being disabled and removed.
    """
    item = Advertising(SELF, handle=4, enabled=True)
    link.roles.advertising.publish(item)
    if action == 'disabled':
        item.enabled = False
    else:
        link.roles.advertising.remove(4)
    assert link.roles.accepted(accepted(advertiser=item)) is None
    assert not link.connections and not link.listener.events


def test_delayed_result_cannot_complete_newer_initiation(link):
    """A delayed accept cannot complete a newer initiation context.

    Purpose: Verify that a connection-accept carrying a stale initiating context
    is rejected while a newer initiation is current, and that an accept carrying
    the current context completes it (Vol 6, Part B, 4.4.4).

    Steps:
        1. Start an initiation, then replace it with a newer one at a later start.
        2. Accept a connection carrying the original (stale) initiating context.
        3. Accept a connection carrying the current context.

    Expected:
        - The stale accept returns None and the current initiation is unchanged.
        - The current accept succeeds and clears the initiating context.
    """
    original = Initiating(SELF, PEER, 20, 10, PARAMETERS, 0)
    current = replace(original, start_us=100)
    link.roles.scanning.start_initiating(current)
    delayed = accepted(role=Role.CENTRAL, initiator=original)
    assert link.roles.accepted(delayed) is None
    assert link.roles.scanning.initiating is current
    assert link.roles.accepted(replace(delayed, initiator=current))
    assert link.roles.scanning.initiating is None


def test_old_window_packet_cannot_start_new_initiation(link):
    """A packet from an old window cannot start a replaced initiation.

    Purpose: Verify that an advertisement received during an initiation that is
    cancelled and replaced mid-receive does not create a connection for the old
    context nor transmit, leaving the new initiation current (Vol 6, Part B,
    4.4.4).

    Steps:
        1. Start an initiation.
        2. Patch the radio so the receive cancels it and starts a replacement,
           then returns a matching advertisement.
        3. Run the original initiation's receive.

    Expected:
        - The current initiation is the replacement.
        - No connection was created and nothing was transmitted.
    """
    original = Initiating(SELF, PEER, 20_000, 10_000, PARAMETERS, 0)
    current = replace(original, start_us=100)
    scanning = link.roles.scanning
    scanning.start_initiating(original)
    packet = Received(adv_ind(), -40, 100, 200, True)

    async def receive(channel, until_us, **_):
        scanning.cancel_initiating()
        scanning.start_initiating(current)
        link.radio.now_us = packet.end_us
        return packet

    link.radio.receive = receive
    run(link, scanning.run(original, 1000))
    assert scanning.initiating is current
    assert not link.connections and not link.radio.sent


def test_old_connection_cannot_release_reused_handle(link):
    """A stale connection cannot release a handle reused by a new one.

    Purpose: Verify that releasing an old connection does not free a handle that
    has since been reassigned to a new connection, so the new connection keeps
    the handle (Vol 6, Part B, 4.4).

    Steps:
        1. Accept a connection and release it.
        2. Accept a new connection that reuses the same handle.
        3. Release the old connection object again.

    Expected:
        - The old and new connections share the handle.
        - The second release of the old connection returns False.
        - The handle still maps to the new connection.
    """
    manager = link.roles.connections
    old = link.roles.accepted(accepted())
    assert manager.release(old)
    current = link.roles.accepted(accepted('C0:11:22:33:44:66'))
    assert old.handle == current.handle
    assert not manager.release(old)
    assert manager.contexts[current.handle] is current


@pytest.mark.parametrize(
    'llid,payload',
    [
        (LlId.START, b'abc'),
        (
            LlId.CONTROL,
            bytes(
                (
                    ControlOpcode.LL_TERMINATE_IND,
                    ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR,
                )
            ),
        ),
    ],
    ids=['ACL', 'LL_TERMINATE_IND'],
)
@pytest.mark.parametrize('reuse', [False, True])
@pytest.mark.parametrize('role', [Role.CENTRAL, Role.PERIPHERAL])
def test_reset_discards_in_flight_acl_receive(iut, role, llid, payload, reuse):
    """An RX started before HCI Reset cannot deliver data, disconnect or send an ACK.

    Steps:
        Exchange an empty PDU to establish the ACL. Reset during its next
        receive window, optionally reuse the handle, then finish receiving
        the old peer's data or termination.

    Expected:
        After Command Complete, the old PDU produces no HCI event, ACL data or
        radio transmission. Any new connection keeps its handle.
    """
    connections = iut.link.roles.connections
    old = iut.link.roles.accepted(accepted(role=role))
    iut.lt_tx(
        bytes((LlId.CONTINUATION, 0)), access_address=old.parameters.access_address
    )
    iut.run_until(old.anchor_us + 5000)
    assert old.established
    pdu = bytes((llid | PDU_DATA_NESN_MASK | PDU_DATA_SN_MASK, len(payload))) + payload
    iut.lt_tx(pdu, access_address=old.parameters.access_address)
    with iut.pause_radio(iut.link.advance_to_us(old.anchor_us + 5000), 'receive'):
        iut.ut(hci.HCI_Reset_Command())
        iut.ut(hci.HCI_Set_Event_Mask_Command(b'\xff' * 8))
        iut.ut(hci.HCI_LE_Set_Event_Mask_Command(b'\xff' * 8))
        if reuse:
            current = iut.link.roles.accepted(
                accepted('C0:11:22:33:44:66', end_us=iut.now_us + 20000)
            )
            assert current.handle == old.handle
        iut.events.clear()
        iut.lt_rx_all()
    assert iut.radio.delivered[-1].pdu == pdu
    iut.ut_rx_q_is_empty()
    iut.lt_rx_q_is_empty()
    assert connections.contexts == ({current.handle: current} if reuse else {})


@pytest.mark.parametrize('extended', [False, True])
def test_duplicate_connect_request_from_existing_peer_is_ignored(link, extended):
    """A connect request from an already-connected peer is ignored.

    Purpose: Verify that a CONNECT_IND (legacy or extended) from a peer that is
    already connected does not create a second connection, keeps the advertiser
    enabled, and neither raises an event nor transmits (Vol 6, Part B, 4.4.2.4,
    4.4.2.7).

    Steps:
        1. Accept a connection from the peer and clear events.
        2. Publish a connectable advertiser.
        3. Handle a CONNECT_IND from the same peer, via the extended or legacy
           path.

    Expected:
        - The request yields no result; the existing connection is unchanged.
        - The advertiser stays enabled, no event was raised and nothing was
          transmitted.

    Parameters range over the legacy and extended request paths.
    """
    existing = link.roles.accepted(accepted(str(PEER)))
    link.listener.events.clear()
    item = Advertising(
        SELF,
        handle=0,
        enabled=True,
        extended=extended,
        mode=AdvertisingMode.CONNECTABLE,
        pdu_type=AdvertisingPduType.ADV_IND,
    )
    link.roles.advertising.publish(item)
    packet = Received(connect_ind(PEER, SELF), -40, 100, 500, True)
    result = run(
        link,
        respond_to_aux_request(link.roles.advertising, item, 0, packet)
        if extended
        else advertising_request(link.roles.advertising, item, 37, packet),
    )
    assert not result and link.connections == {existing.handle: existing}
    assert item.enabled and not link.listener.events and not link.radio.sent


def test_aux_connect_without_radio_room_is_not_attempted(link):
    """An AUX_CONNECT_REQ is not sent without radio room.

    Purpose: Verify that an extended initiator does not transmit an
    AUX_CONNECT_REQ when the radio resource deadline leaves no room for the
    exchange (Vol 6, Part B, 4.4.4.2).

    Steps:
        1. Start an initiation and set the resource deadline just ahead of now.
        2. Run the extended initiate handling for a connectable AUX_ADV_IND.

    Expected:
        - Nothing was transmitted.
    """
    link.roles.scanning.start_initiating(Initiating(SELF, PEER, 20, 10, PARAMETERS, 0))
    link.resource.deadline_us = link.radio.now_us + 10
    aux = ExtendedPdu(
        mode=AdvertisingMode.CONNECTABLE, address=bytes(PEER), random_address=True
    )
    packet = Received(aux.encode(), -40, 1, 300, True)
    run(link, initiate_extended(link, link.initiating, 12, packet, PEER))
    assert not link.radio.sent


# ---------------------------------------------------------------------------
# Advertising lifetime
# ---------------------------------------------------------------------------
def test_event_limit_ends_advertising_but_not_periodic_train(link):
    """Reaching the advertising event limit ends it but keeps the periodic train.

    Purpose: Verify that when an advertiser reaches its max-events limit the
    legacy/extended advertising ends with Limit Reached while an associated
    periodic train keeps running and is still scheduled (Vol 6, Part B, 4.4.2.6,
    4.4.2.12).

    Steps:
        1. Publish an advertiser at its event limit with a periodic train
           enabled.
        2. Prepare the advertiser's radio operations.
        3. Prepare once more.

    Expected:
        - The only prepared operation is the periodic TX.
        - on_advertising_ended was raised once with LIMIT_REACHED_ERROR.
        - The second prepare raises nothing more and the periodic train stays
          enabled.
    """
    item = Advertising(
        SELF,
        handle=1,
        enabled=True,
        events=3,
        max_events=3,
        periodic_enabled=True,
        periodic_interval=80,
        periodic_next_us=10_000,
    )
    link.roles.advertising.publish(item)
    operations = link.roles.advertising.prepare()
    assert [operation.priority for operation in operations] == [
        RadioPriority.PERIODIC_TX
    ]
    assert link.listener.events == [
        (
            'on_advertising_ended',
            (item, ErrorCode.LIMIT_REACHED_ERROR),
        )
    ]
    link.roles.advertising.prepare()
    assert len(link.listener.events) == 1 and item.periodic_enabled


@pytest.mark.parametrize('action', ['reset', 'remove'])
def test_completion_callback_cannot_prepare_released_context(link, action):
    """A completion callback cannot prepare a context it released.

    Purpose: Verify that when the on_advertising_ended callback resets the link or
    removes the sibling advertiser, that sibling is not prepared and a later
    expire for it raises no further callback (Vol 6, Part B, 4.4.2).

    Steps:
        1. Publish two advertisers past their deadlines.
        2. In on_advertising_ended, reset the link or remove the second
           advertiser per the action.
        3. Prepare the advertisers.
        4. Expire the second advertiser.

    Expected:
        - Prepare returns no operations and only the first advertiser ended.
        - Expiring the second raises no additional callback.

    Parameters range over reset and remove in the callback.
    """
    manager = link.roles.advertising
    first = Advertising(SELF, handle=1, enabled=True, deadline_us=10)
    second = replace(first, handle=2, periodic_enabled=True, periodic_interval=80)
    manager.publish(first)
    manager.publish(second)
    link.radio.now_us = 10
    seen = []

    def ended(item, status):
        seen.append(item.handle)
        if action == 'reset':
            link.reset()
        else:
            manager.remove(2)

    link.listener.on_advertising_ended = ended
    assert not manager.prepare()
    assert seen == [first.handle]
    manager.expire(second, ErrorCode.ADVERTISING_TIMEOUT_ERROR)
    assert seen == [first.handle]


def test_queued_radio_work_cannot_transmit_after_advertising_handle_reuse(link):
    """Primary and periodic work keep the identity of the set that queued them."""
    manager = link.roles.advertising
    original = Advertising(
        SELF,
        handle=3,
        enabled=True,
        periodic_enabled=True,
        periodic_interval=80,
        periodic_next_us=100_000,
    )
    manager.publish(original)
    primary, periodic = manager.prepare()
    assert (primary.owner, periodic.owner) == (original, original)

    replacement = replace(original)
    manager.remove(original.handle)
    manager.publish(replacement)
    for operation in (primary, periodic):
        run(link, operation.run())

    assert not link.radio.sent
    assert manager.owns(replacement)
    assert original.events == original.periodic_counter == 0


def test_advertising_event_waits_for_a_free_radio_gap(link):
    """An advertising event defers when the radio is reserved.

    Purpose: Verify that when the radio is reserved for the whole next interval
    the advertising event transmits nothing, reschedules past now, and does not
    advance its event count (Vol 6, Part B, 4.4.2).

    Steps:
        1. Publish an advertiser.
        2. Make the resource manager report a reservation covering the window.
        3. Run the advertiser's event.

    Expected:
        - Nothing was transmitted and the next event is rescheduled past now.
        - The event count is still zero.
    """
    item = Advertising(SELF, handle=0, enabled=True, interval_us=100_000)
    link.roles.advertising.publish(item)
    link.roles.reservations = lambda exclude=None: [
        RadioReservation(link.radio.now_us + 1, 5_000, 20_000)
    ]
    link.resource = RadioResourceManager(lambda: link.now_us, link.roles.reservations)
    run(link, link.roles.advertising.run(item))
    assert not link.radio.sent and item.next_us > link.radio.now_us
    assert item.events == 0


@pytest.mark.parametrize('recurring', [True, False], ids=['no-gap', 'one-shot-only'])
def test_reservations_do_not_extend_advertising_search(link, monkeypatch, recurring):
    """Use one advertising interval even with long periodic reservations."""
    item = Advertising(
        SELF,
        handle=0,
        enabled=True,
        interval_us=100_000,
        pdu_type=AdvertisingPduType.ADV_IND,
        data=bytearray(31),
        scan_response=bytearray(31),
    )
    manager = link.roles.advertising
    manager.publish(item)
    start = link.radio.earliest_start_us(37, item.primary_phy, transmit=True)
    one_shot = RadioReservation(start, 80, priority=RadioPriority.PERIODIC_RX)
    reservations = [one_shot]
    if recurring:
        reservations.extend(
            (
                RadioReservation(start, 6_000, 7_500),
                RadioReservation(start, 80, 41_000_000_000, RadioPriority.PERIODIC_RX),
            )
        )
    link.resource = RadioResourceManager(lambda: link.now_us, lambda _: reservations)

    def bounded_gap(reserved, start_us, duration_us, latest_us):
        # Check the bound before running the search so a regression fails promptly.
        assert latest_us - start_us == item.interval_us
        return first_radio_gap(reserved, start_us, duration_us, latest_us)

    monkeypatch.setattr(advertiser, 'first_radio_gap', bounded_gap)
    run(link, manager.run(item))
    assert not link.radio.sent and item.events == 0
    if recurring:
        assert item.next_us == start + item.interval_us
    else:
        assert item.next_us == one_shot.start_us + one_shot.duration_us
        run(link, link.radio.advance_to_us(item.next_us - 1))
        run(link, manager.run(item))
        assert item.events == 1
        assert link.radio.sent[0].start_us == one_shot.start_us + one_shot.duration_us


# ---------------------------------------------------------------------------
# Scanning lifetime
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('period_us', [0, 100])
def test_scan_duration_timeout_vs_period(link, period_us):
    """A scan deadline restarts with a period but times out without one.

    Purpose: Verify that at the scan deadline a periodic scan restarts the next
    period (raising on_scan_period) while a one-shot scan ends (raising
    on_scan_timeout) (Vol 6, Part B, 4.4.3; Vol 4, Part E, 7.8.65).

    Steps:
        1. Enable a scan with the parametrized period and a deadline at 10 us.
        2. Advance the clock to the deadline and run the scan.

    Expected:
        - With a period: the scan stays active, its start and deadline advance to
          the next period, and on_scan_period was raised.
        - Without a period: the active scan is cleared and on_scan_timeout was
          raised.

    Parameters range over no period and a 100 us period.
    """
    manager = link.roles.scanning
    manager.start(Scanning(SELF, 20, 10), SELF, 10, period_us)
    scan = manager.active
    link.radio.now_us = 10
    run(link, manager.run(scan, 20))
    if period_us:
        assert manager.active is scan
        assert (scan.start_us, scan.deadline_us) == (100, 110)
        assert link.listener.events == [('on_scan_period', ())]
    else:
        assert manager.active is None
        assert link.listener.events == [('on_scan_timeout', ())]


def test_idle_scan_event_only_advances_time(link):
    """An idle scan event only advances the clock.

    Purpose: Verify that handling a scan event with nothing to receive just
    advances the simulation clock and opens no listen window (Vol 6, Part B,
    4.4.3).

    Steps:
        1. Run receive_scan_event for an idle event ending at 500 us.

    Expected:
        - The clock advanced to 500 us and no listen window opened.
    """
    run(link, receive_scan_event(link, ScanEvent(500)))
    assert link.radio.now_us == 500 and not link.radio.listens


def test_backoff_halves_after_two_successes():
    """The scan backoff upper limit halves after two consecutive successes.

    Purpose: Verify that the active-scan backoff keeps its upper limit after one
    success, halves it after two consecutive successes, and settles at the
    minimum of one with continued successes (Vol 6, Part B, 4.4.3.3).

    Steps:
        1. Build a backoff with upper limit 8.
        2. Complete one success, then a second.
        3. Complete twenty more successes.

    Expected:
        - The upper limit is still 8 after one success.
        - It is 4 with the success streak reset after the second.
        - It settles with upper limit and count both 1.
    """
    backoff = ScanBackoff(upper_limit=8)
    generator = random.Random(0)
    backoff.complete(True, generator)
    assert backoff.upper_limit == 8
    backoff.complete(True, generator)
    assert backoff.upper_limit == 4 and backoff.successes == 0
    for _ in range(20):
        backoff.complete(True, generator)
    assert backoff.upper_limit == backoff.count == 1


def test_backoff_saturates_at_256_and_streaks_reset():
    """The scan backoff saturates at 256 and resets streaks on a mixed outcome.

    Purpose: Verify that repeated failures drive the backoff upper limit to its
    maximum of 256, and that a success followed by a failure resets the streak
    counters (Vol 6, Part B, 4.4.3.3).

    Steps:
        1. Complete twenty failures.
        2. Complete one success, then one failure.

    Expected:
        - The upper limit saturates at 256 with the count within [1, 256].
        - After the success-then-failure, successes is 0 and failures is 1.
    """
    backoff = ScanBackoff()
    generator = random.Random(0)
    for _ in range(20):
        backoff.complete(False, generator)
    assert backoff.upper_limit == 256 and 1 <= backoff.count <= 256
    backoff.complete(True, generator)
    backoff.complete(False, generator)
    assert backoff.successes == 0 and backoff.failures == 1


# ---------------------------------------------------------------------------
# Synchronization lifetime
# ---------------------------------------------------------------------------
def test_syncinfo_acquires_and_first_packet_establishes(link):
    """SyncInfo from the right advertiser acquires, then a packet establishes.

    Purpose: Verify that SyncInfo from the wrong advertiser is ignored, SyncInfo
    from the target seeds the pending sync's anchor and counter, and the first
    received periodic packet establishes the sync and raises on_sync_established
    (Vol 6, Part B, 4.4.5).

    Steps:
        1. Start a pending sync for the peer.
        2. Deliver SyncInfo from another advertiser, then from the peer.
        3. Deliver a periodic packet twice.

    Expected:
        - The wrong-advertiser SyncInfo leaves the sync with no info.
        - The peer's SyncInfo makes it pending with the seeded next time and
          counter and no event yet.
        - The periodic packet establishes it (active, no longer pending) and
          raises on_sync_established.
    """
    sync = PeriodicSync(PEER, 2, 100_000, 100_000)
    manager = link.roles.synchronization
    manager.start(sync)
    info = replace(INFO, event_counter=4)
    packet = Received(b'', -40, 100, 200, True)
    manager.acquired(SyncInfoReceived(Address('C0:11:22:33:44:66'), 2, info, packet))
    assert sync.info is None
    manager.acquired(SyncInfoReceived(PEER, 2, info, packet))
    assert manager.pending is sync and (sync.next_us, sync.counter) == (9100, 4)
    assert not link.listener.events
    periodic = Received(b'', -40, 9200, 9300, True)
    result = PeriodicPacketReceived(sync, periodic, ExtendedPdu())
    assert manager.received(result) and manager.received(result)
    assert (sync.next_us, sync.deadline_us) == (9200, 109300)
    assert manager.active[sync.handle] is sync and manager.pending is None
    assert [name for name, _ in link.listener.events] == ['on_sync_established']


def test_syncinfo_for_already_synchronized_train_is_ignored(link):
    """SyncInfo for an already-synchronized train is ignored.

    Purpose: Verify that SyncInfo for an advertiser/SID already established is not
    acquired again by a pending sync (Vol 6, Part B, 4.4.5).

    Steps:
        1. Mark a sync for the peer/SID as already established.
        2. Start a pending sync for the same peer/SID via the list.
        3. Deliver SyncInfo from the peer.

    Expected:
        - The pending sync still has no info.
    """
    manager = link.roles.synchronization
    manager.active[1] = PeriodicSync(PEER, 2, 100_000, established=True, handle=1)
    sync = PeriodicSync(PEER, 2, 100_000, use_list=True)
    link.periodic_advertiser_list.add((PEER, 2))
    manager.start(sync)
    manager.acquired(SyncInfoReceived(PEER, 2, INFO, Received(b'', -40, 1, 2, True)))
    assert sync.info is None


def test_delayed_result_cannot_revive_cancelled_acquisition(link):
    """A delayed packet cannot revive a cancelled, replaced acquisition.

    Purpose: Verify that a periodic packet carrying a cancelled acquisition does
    not establish anything while a replacement acquisition is pending (Vol 6,
    Part B, 4.4.5).

    Steps:
        1. Start an acquisition, cancel it, and start a replacement.
        2. Deliver a periodic packet carrying the original acquisition.

    Expected:
        - The packet is not accepted.
        - The replacement is still pending and nothing is active.
    """
    manager = link.roles.synchronization
    original = PeriodicSync(PEER, 2, 100_000, 100_000)
    manager.start(original)
    manager.cancel()
    current = replace(original)
    manager.start(current)
    result = PeriodicPacketReceived(
        original, Received(b'', -40, 1, 2, True), ExtendedPdu()
    )
    assert not manager.received(result)
    assert manager.pending is current and not manager.active


def test_removed_sync_cannot_publish_later_fragments(link):
    """A sync removed mid-report publishes no later fragments.

    Purpose: Verify that when the on_periodic_report callback terminates the sync,
    a following report fragment for that sync is not delivered (Vol 6, Part B,
    4.4.5).

    Steps:
        1. Mark a sync as established.
        2. In on_periodic_report, terminate the sync.
        3. Report an incomplete fragment, then a complete one.

    Expected:
        - Only the first fragment ("first") was delivered.
    """
    manager = link.roles.synchronization
    sync = PeriodicSync(PEER, 2, 100_000, 100_000, established=True, handle=1)
    manager.active[1] = sync
    seen = []

    def report(item, data, rssi, tx_power, status, *, counter, subevent):
        seen.append(data)
        manager.terminate(item.handle)

    link.listener.on_periodic_report = report
    manager.report(sync, b'first', -40, None, DataStatus.INCOMPLETE)
    manager.report(sync, b'stale', -40, None, DataStatus.COMPLETE)
    assert seen == [b'first']


def test_expiry_without_radio_receive(link):
    """Expiry fails a pending sync and loses an established one.

    Purpose: Verify that expiring synchronization at its deadline fails the
    pending acquisition and loses the established sync, raising
    on_sync_established then on_sync_lost (Vol 6, Part B, 4.4.5).

    Steps:
        1. Set a pending sync and an established sync, both at the deadline.
        2. Advance the clock to the deadline and expire.

    Expected:
        - No sync remains pending or active.
        - The listener saw on_sync_established then on_sync_lost.
    """
    manager = link.roles.synchronization
    pending = PeriodicSync(PEER, 2, 100_000, 20, info=INFO, handle=1)
    manager.pending = pending
    manager.active[2] = replace(pending, established=True, handle=2)
    link.radio.now_us = 20
    manager.expire()
    assert manager.pending is None and not manager.active
    assert [name for name, _ in link.listener.events] == [
        'on_sync_established',
        'on_sync_lost',
    ]


@pytest.mark.parametrize('winner', ['connection', 'periodic_tx'])
def test_periodic_rx_yields_without_refreshing_timeout(link, winner):
    """Periodic RX yields to a higher-priority event without refreshing timeout.

    Purpose: Verify that when a connection anchor or a periodic TX collides with a
    periodic RX, the RX yields: it does not listen, advances its event counter and
    next time by (skip + 1) intervals, and does not refresh its last-reception
    time or deadline (Vol 6, Part B, 4.4.5).

    Steps:
        1. Set an established sync with skip 2 due at 10 000 us.
        2. Add the colliding winner (a connection or a periodic TX) at 10 100 us.
        3. Run periodic_receive for the sync.

    Expected:
        - The clock did not advance and no listen opened.
        - The next time advanced by three intervals and the counter wrapped to 1.
        - The last-reception time and deadline are unchanged.

    Parameters range over the collision winner being a connection and a periodic
    TX.
    """
    link.radio.now_us = 9999
    sync = PeriodicSync(
        PEER,
        1,
        100_000,
        100_000,
        skip=2,
        info=replace(INFO, offset_us=0, interval=40),
        next_us=10_000,
        counter=LL_EVENT_COUNTER_MASK - 1,
        established=True,
    )
    link.syncs[sync.handle] = sync
    if winner == 'connection':
        connection = Connection(link, PARAMETERS, PEER, Role.CENTRAL, 0)
        connection.anchor_us = 10_100
        connection.established = True
        link.connections[connection.handle] = connection
    else:
        link.roles.advertising.publish(
            Advertising(
                PEER,
                handle=0,
                periodic_enabled=True,
                periodic_next_us=10_100,
                periodic_interval=40,
            )
        )
    run(link, link.roles.synchronization.run_periodic(sync))
    assert link.radio.now_us == 9999 and not link.radio.listens
    assert sync.next_us == 10_000 + 3 * 40 * LL_PERIODIC_INTERVAL_UNIT_US
    assert sync.counter == 1
    assert sync.last_rx_us == 0 and sync.deadline_us == 100_000


def test_periodic_rx_of_non_periodic_pdu_is_ignored(link):
    """A non-periodic PDU on the sync window is ignored.

    Purpose: Verify that a connectable extended PDU received where a periodic
    event is expected raises no report and does not refresh the sync's
    last-reception time (Vol 6, Part B, 4.4.5).

    Steps:
        1. Set an established sync due at 100 us.
        2. Script a connectable extended PDU at that time.
        3. Run periodic_receive for the sync.

    Expected:
        - No listener event was raised.
        - The last-reception time is unchanged.
    """
    sync = PeriodicSync(
        PEER, 1, 100_000, 100_000, info=INFO, next_us=100, established=True, handle=1
    )
    link.syncs[1] = sync
    link.radio.script.append(
        Air(ExtendedPdu(mode=AdvertisingMode.CONNECTABLE).encode(), at=100)
    )
    run(link, link.roles.synchronization.run_periodic(sync))
    assert not link.listener.events and sync.last_rx_us == 0


def test_periodic_tx_yields_and_skips_counter(link):
    """Periodic TX yields to a connection anchor and skips its counter.

    Purpose: Verify that when a connection anchor collides with a periodic TX
    event the train yields: it transmits nothing but still advances its next time
    by one interval and wraps its event counter (Vol 6, Part B, 4.4.2.12).

    Steps:
        1. Add an established connection whose anchor collides with the train.
        2. Set the advertiser's periodic train due at 10 000 us at the wrap
           counter.
        3. Run periodic_transmit for the advertiser.

    Expected:
        - Nothing was transmitted.
        - The next time advanced by one interval and the counter wrapped to 0.
    """
    connection = Connection(link, PARAMETERS, PEER, Role.CENTRAL, 0)
    connection.anchor_us = 10_100
    connection.established = True
    link.connections[connection.handle] = connection
    link.radio.now_us = 9999
    item = Advertising(
        SELF,
        handle=0,
        periodic_enabled=True,
        periodic_next_us=10_000,
        periodic_interval=160,
        periodic_counter=LL_EVENT_COUNTER_MASK,
        periodic_data=bytes(200),
    )
    link.roles.advertising.publish(item)
    run(link, link.roles.advertising.run_periodic(item))
    assert not link.radio.sent
    assert item.periodic_next_us == 10_000 + 160 * LL_PERIODIC_INTERVAL_UNIT_US
    assert item.periodic_counter == 0


# ---------------------------------------------------------------------------
# Wire identities and seeded randomness
# ---------------------------------------------------------------------------
def test_new_connection_window_avoids_existing_acl(link):
    """A new connection's window and access address avoid an existing ACL.

    Purpose: Verify that parameters for a new connection pick a non-zero transmit
    window offset and a different access address from an existing ACL, to keep the
    two anchors and wire identities distinct (Vol 6, Part B, 4.5.1, 4.5.3).

    Steps:
        1. Add an established connection.
        2. Compute connection parameters for a new connection at that anchor.

    Expected:
        - The window offset is greater than zero.
        - The access address differs from the existing connection's.
    """
    connection = Connection(link, PARAMETERS, PEER, Role.PERIPHERAL, 0)
    connection.established = True
    link.connections[connection.handle] = connection
    parameters = link.connection_parameters(PARAMETERS, 0, connection.anchor_us)
    assert parameters.window_offset > 0
    assert parameters.access_address != PARAMETERS.access_address


def test_connection_wire_parameters_are_seeded_and_bounded(link):
    """Connection wire parameters are bounded, varied and reproducible.

    Purpose: Verify that generated connection parameters produce valid access
    addresses, in-range CRC inits and hop increments, vary across connections,
    and reproduce exactly when the seeded random state is restored (Vol 6,
    Part B, 2.3.3.1, 4.5.3).

    Steps:
        1. Save the random state and generate 100 parameter sets.
        2. Check each set's access address, CRC init and hop bounds.
        3. Restore the state and regenerate the same count.

    Expected:
        - Every access address is valid, CRC init is in range, and hop is within
          [min, max].
        - The CRC inits are not all identical.
        - The regenerated sets equal the originals.
    """
    state = link.random.getstate()
    values = [link.connection_parameters(PARAMETERS, 0, 1250) for _ in range(100)]
    for parameters in values:
        check_access_address(parameters.access_address)
        assert 0 <= parameters.crc_init < 1 << (PDU_CRC_SIZE * PHY_OCTET_BITS)
        assert LL_HOP_INCREMENT_MIN <= parameters.hop <= LL_HOP_INCREMENT_MAX
    assert len({parameters.crc_init for parameters in values}) > 1
    link.random.setstate(state)
    assert values == [link.connection_parameters(PARAMETERS, 0, 1250) for _ in values]


def test_no_connection_window_when_radio_is_full(link):
    """No connection window is produced when the radio is fully reserved.

    Purpose: Verify that connection parameters cannot be produced when a
    reservation occupies the radio for the whole schedule, so the generator
    returns None (Vol 6, Part B, 4.5.1).

    Steps:
        1. Make the resource manager report a reservation spanning the schedule.
        2. Compute connection parameters.

    Expected:
        - The generator returns None.
    """
    link.roles.reservations = lambda exclude=None: [RadioReservation(0, 10**9, 10**9)]
    link.resource = RadioResourceManager(lambda: link.now_us, link.roles.reservations)
    assert link.connection_parameters(PARAMETERS, 0, 1250) is None


def test_access_address_allocation_excludes_live_owners(link, monkeypatch):
    """Access-address allocation skips values used by live owners.

    Purpose: Verify that a new access address rejects candidates already used by
    a live connection or an enabled periodic train, and may reuse a periodic
    value once that train is disabled (Vol 6, Part B, 2.1.2).

    Steps:
        1. Add a connection and an enabled periodic advertiser with known access
           addresses.
        2. Feed the generator those two values then a free one.
        3. Disable the periodic train and feed its old value.

    Expected:
        - The generator returns the free value while both owners are live.
        - After disabling the train, its old value is accepted.
    """
    connection = Connection(link, PARAMETERS, PEER, Role.CENTRAL, 0)
    link.connections[connection.handle] = connection
    periodic = Advertising(
        PEER, handle=0, periodic_enabled=True, periodic_aa=0x456789AB
    )
    link.roles.advertising.publish(periodic)
    candidates = iter([PARAMETERS.access_address, periodic.periodic_aa, 0x12345678])
    monkeypatch.setattr(link.random, 'getrandbits', lambda width: next(candidates))
    assert link.new_access_address() == 0x12345678
    periodic.periodic_enabled = False
    candidates = iter([periodic.periodic_aa])
    assert link.new_access_address() == periodic.periodic_aa


@pytest.mark.parametrize(
    'rejected',
    [0x8E89BED7, 0x8E89BED6, 0x00000000, 0x0F0F0F0F],
    ids=['adv-aa', 'one-bit', 'run', 'pattern'],
)
def test_access_address_generator_rejects_invalid_patterns(rejected):
    """The access-address generator rejects invalid bit patterns.

    Purpose: Verify that the access-address generator discards candidates that
    match the advertising access address, differ from it by one bit, have long
    runs, or form disallowed patterns, returning the next valid candidate (Vol 6,
    Part B, 2.1.2).

    Steps:
        1. Feed the generator the rejected candidate followed by a valid one.

    Expected:
        - The generator returns the valid candidate 0x9A328370.

    Parameters range over the advertising AA, a one-bit variant, an all-zero run,
    and a repeating pattern.
    """
    values = iter([rejected, 0x9A328370])
    generator = type('G', (), {'getrandbits': lambda self, width: next(values)})()
    assert generate_access_address(generator, set()) == 0x9A328370


def test_advertising_delay_seed_is_per_controller_and_reset_repeats():
    """The advDelay seed is per-controller and reproducible after reset.

    Purpose: Verify that each controller seeds its random generator from its
    address, so resetting reproduces the same advDelay sequence while two
    different controllers produce different sequences (Vol 6, Part B, 4.4.2.2.1).

    Steps:
        1. For two controller addresses, draw a 10-value random sequence.
        2. Reset the link and draw the sequence again.
        3. Compare the two controllers' sequences.

    Expected:
        - Each controller reproduces its sequence after reset.
        - The two controllers' sequences differ.
    """
    sequences = []
    for address in ('F0:F1:F2:F3:F4:01', 'F0:F1:F2:F3:F4:02'):
        harness = AirHarness(Address(address))
        link = harness.link
        sequence = [link.random.randint(0, 10_000) for _ in range(10)]
        link.reset()
        assert [link.random.randint(0, 10_000) for _ in range(10)] == sequence
        sequences.append(sequence)
        harness.close()
    assert sequences[0] != sequences[1]


def test_radio_admission_needs_only_clock_and_windows():
    """Radio admission depends only on the clock, windows and the claiming owner.

    Purpose: Verify that the resource manager admits an event that fits a free
    window for its owner, refuses one for a different owner, refuses one starting
    too early, and that a failed claim clears the deadline (Vol 6, Part B,
    implementation resource arbitration).

    Steps:
        1. Build a resource manager with a reservation visible to all but the
           owner.
        2. Try to admit the owner, then another owner, then the owner with a
           later earliest start.
        3. Claim and raise inside the claim.

    Expected:
        - The owner is admitted, the other owner is not, and the too-early start
          is not.
        - After the failed claim the deadline is cleared.
    """
    owner = object()
    reservations = [RadioReservation(30, 10, 100, RadioPriority.CONNECTION)]
    resource = RadioResourceManager(
        lambda: 10, lambda exclude: [] if exclude is owner else reservations
    )
    assert resource.admit(owner, 30, 40, RadioPriority.PERIODIC_RX)
    assert not resource.admit(object(), 30, 40, RadioPriority.PERIODIC_RX)
    assert not resource.admit(owner, 30, 40, RadioPriority.PERIODIC_RX, earliest_us=31)
    with pytest.raises(RuntimeError), resource.claim():
        assert resource.deadline_us == 30
        raise RuntimeError('event aborted')
    assert resource.deadline_us is None


def test_one_shot_inside_protected_exchange_has_no_following_deadline():
    """A low-priority one-shot yields to the protected exchange once."""
    response = RadioReservation(10, 5, priority=RadioPriority.PERIODIC_RX)
    reservations = [response]
    resource = RadioResourceManager(lambda: 0, lambda _: reservations)
    protected = RadioReservation(8, 15, 40, RadioPriority.CONNECTION)

    assert resource.radio_limit() == 10
    assert resource.radio_limit(protected=protected) is None

    reservations.append(RadioReservation(28, 5, 40, RadioPriority.PERIODIC_RX))
    assert resource.radio_limit(protected=protected) == 28


def test_acl_reservation_detects_overlap_at_a_drifted_anchor(link):
    """Keep the slot reserved when the test clock shifts the next ACL anchor."""
    link.sleep_clock_accuracy = LL_SCA_500_PPM
    connection = Connection(
        link, replace(PARAMETERS, interval=3200), PEER, Role.CENTRAL, 0
    )
    connection.established = True
    link.connections[connection.handle] = connection
    (reservation,) = link.resource.reservations()

    connection.step_event()
    (actual,) = link.resource.reservations()
    assert actual.start_us - reservation.start_us == 4_001_000
    end = actual.start_us + actual.duration_us
    start = end - 300
    assert reservation.window_after(start) == (actual.start_us, end)
    assert higher_priority_overlap([reservation], start, end, RadioPriority.PERIODIC_TX)
    assert first_radio_gap([reservation], start, 300, end) == end


def test_acl_reservation_projects_full_intervals_with_fractional_drift(link):
    """Project from the current fractional phase and carry it across future events."""
    link.sleep_clock_accuracy = LL_SCA_500_PPM
    connection = Connection(
        link, replace(PARAMETERS, interval=6), PEER, Role.CENTRAL, 0
    )
    connection.established = True
    link.connections[connection.handle] = connection
    connection.step_event()
    (reservation,) = link.resource.reservations()

    # Each 7.5-ms interval gains 1.875 us. The first step left 0.875 us.
    for event, drift in enumerate((2, 4, 6, 8, 10, 12, 14, 15), start=1):
        connection.step_event()
        (actual,) = link.resource.reservations()
        expected = reservation.start_us + event * 7_500 + drift
        assert actual.start_us == expected
        window = (expected, expected + reservation.duration_us)
        assert reservation.window_after(expected - 1) == window
        assert reservation.window_after(expected) == window


@pytest.mark.parametrize('update_window', (False, True))
def test_subrated_peripheral_reservation_covers_late_reply(link, update_window):
    """Protect a Peripheral's widened receive window and response from periodic TX."""
    link.sleep_clock_accuracy = LL_SCA_500_PPM
    parameters = replace(
        PARAMETERS,
        interval=8,
        latency=0,
        sca=LL_SCA_500_PPM,
        timeout=3200,
        window_size=2,
    )
    connection = Connection(link, parameters, PEER, Role.PERIPHERAL, 0)
    connection.established = True
    connection.update_window = update_window
    connection.last_rx_us = connection.anchor_us
    factor = 400
    connection.controls.subrating.schedule = SubrateSchedule(factor=factor)
    link.connections[connection.handle] = connection
    interval = parameters.interval * LL_CONN_INTERVAL_UNIT_US
    anchor = connection.anchor_us + factor * interval
    connection.step_event()

    widening = connection.receive_widening_at(anchor)
    latest_rx = anchor + widening + connection.receive_window_us - 1
    (reservation,) = link.resource.reservations()
    link.radio.script.append(
        Air(
            bytes((LlId.CONTINUATION, 0)),
            at=latest_rx,
            access_address=parameters.access_address,
        )
    )
    run(link, link.advance_to_us(latest_rx))
    (reply,) = link.radio.sent
    assert reply.access_address == parameters.access_address
    assert higher_priority_overlap(
        [reservation], latest_rx, reply.end_us, RadioPriority.PERIODIC_TX
    )
    assert reservation.start_us + reservation.duration_us >= reply.end_us


def test_colliding_acl_anchors_share_the_radio(link):
    """Two ACLs with colliding anchors share the radio fairly.

    Purpose: Verify that two established connections whose anchors collide each
    still get connection events over time, with the service roughly balanced
    between them (Vol 6, Part B, 4.5, resource arbitration).

    Steps:
        1. Add two established connections with distinct access addresses.
        2. Advance the clock over several connection intervals.
        3. Count transmissions per connection's access address.

    Expected:
        - Each connection transmitted at least twice.
        - The counts differ by at most one.
    """
    first = Connection(link, PARAMETERS, PEER, Role.CENTRAL, 0)
    first.established = True
    link.connections[first.handle] = first
    second = Connection(
        link,
        replace(PARAMETERS, access_address=PARAMETERS.access_address ^ 1),
        Address('C0:11:22:33:44:66'),
        Role.CENTRAL,
        0,
    )
    second.established = True
    link.connections[second.handle] = second
    run(link, link.advance_to_us(first.anchor_us + 5 * PARAMETERS.interval * 1250))
    counts = [
        sum(
            packet.access_address == value.parameters.access_address
            for packet in link.radio.sent
        )
        for value in (first, second)
    ]
    assert min(counts) >= 2 and max(counts) - min(counts) <= 1


def test_txadd_identity_is_part_of_connect_peer(link):
    """The CONNECT_IND TxAdd bit is part of the peer identity.

    Purpose: Verify that the initiator's address type from the CONNECT_IND TxAdd
    bit is carried into the committed connection's peer, so a public-typed
    initiator yields a public peer, and that the request's LLData interval is
    applied (Vol 6, Part B, 2.3.3.1, 4.4.2.4).

    Steps:
        1. Publish an ADV_IND advertiser.
        2. Handle a CONNECT_IND whose TxAdd bit is cleared (public initiator).

    Expected:
        - The committed connection's peer is the public-typed address.
        - The connection interval matches the CONNECT_IND LLData.
    """
    item = Advertising(
        SELF, handle=0, enabled=True, pdu_type=AdvertisingPduType.ADV_IND
    )
    link.roles.advertising.publish(item)
    public = Address(bytes(PEER), 0)
    pdu = bytearray(connect_ind(PEER, SELF))
    pdu[0] &= ~PDU_ADV_TXADD_MASK

    run(
        link,
        advertising_request(
            link.roles.advertising, item, 37, Received(bytes(pdu), -40, 1, 400, True)
        ),
    )
    (connection,) = link.connections.values()
    assert connection.peer == public
    assert CONNECT_PARAMETERS.interval == connection.parameters.interval


def test_periodic_enable_without_free_phase_keeps_nominal_anchor(link):
    """Enabling a periodic train with no free phase keeps the nominal anchor.

    Purpose: With no gap in the next interval the train starts one interval later
    and yields to the arbiter event by event, keeping the nominal anchor and a
    valid seeded access address; re-enabling an already-enabled train changes
    nothing (Vol 6, Part B, 4.4.2.12).

    Steps:
        1. Publish an extended advertiser with a periodic interval.
        2. Make the resource manager report a full-schedule reservation.
        3. Enable the periodic train at now = 1000 us.
        4. Enable it again.

    Expected:
        - The train is enabled with its next anchor one interval after now and a
          valid access address.
        - Re-enabling leaves the next anchor unchanged.
    """
    item = Advertising(SELF, handle=0, extended=True, periodic_interval=80)
    link.roles.advertising.publish(item)
    link.roles.reservations = lambda exclude=None: [RadioReservation(0, 10**9, 10**9)]
    link.resource = RadioResourceManager(lambda: link.now_us, link.roles.reservations)
    link.radio.now_us = 1000
    link.roles.advertising.set_periodic_enabled(item, True)
    assert item.periodic_enabled
    assert item.periodic_next_us == 1000 + 80 * LL_PERIODIC_INTERVAL_UNIT_US
    check_access_address(item.periodic_aa)
    # Re-enabling an enabled train changes nothing.
    anchor = item.periodic_next_us
    link.roles.advertising.set_periodic_enabled(item, True)
    assert item.periodic_next_us == anchor
