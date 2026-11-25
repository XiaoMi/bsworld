# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL role owners: lifetime transitions, event preparation and completion.

These managers own active contexts. RF procedures deliver validated outcomes;
the shared-radio dispatcher does not implement advertising or sync lifetimes.
This is an implementation boundary inside Core Vol 6, Part B's Link Layer.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from bscontroller.address import AddressType
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_ADV_DELAY_MAX_US,
    LL_ADV_PRIMARY_CHANNELS,
    LL_CONN_INTERVAL_UNIT_US,
    LL_DATA_CHANNEL_MAP_ALL,
    LL_EVENT_COUNTER_MASK,
    LL_IFS_US,
    LL_INSTANT_MIN_LISTENING_EVENTS,
    LL_MAFS_US,
    LL_PERIODIC_INTERVAL_UNIT_US,
    LL_PERIODIC_SYNC_ESTABLISHMENT_EVENTS,
    LL_SCA_50_PPM,
    PDU_ADV_ADI_DID_MASK,
    PDU_ADV_AUX_CONNECT_RSP_SIZE,
    PDU_ADV_CONNECT_SIZE,
    PDU_ADV_EXT_PAYLOAD_SIZE_MAX,
    PDU_CRC_SIZE,
    PDU_HEADER_SIZE,
)
from bscontroller.linklayer.advertising import ExtendedPdu, SyncInfo
from bscontroller.linklayer.broadcast import BroadcastManager
from bscontroller.linklayer.channel_selection import channel_selection_2
from bscontroller.linklayer.connection import Connection
from bscontroller.linklayer.discovery import (
    Advertising,
    Initiating,
    PeriodicSync,
    Scanning,
    ScanWindow,
    TransferOrigin,
    scan_window_at,
)
from bscontroller.linklayer.extended import periodic_time_us, subevent_pdu
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.iso import IsoDataPaths
from bscontroller.linklayer.pawr import (
    RESPONSE_TX_POWER_DBM,
    PawrAdvertiser,
    PawrSync,
    PendingResponse,
    ScheduledResponse,
    SlotResponse,
    Subevent,
    SubeventResponses,
    acad_timing,
    fits,
    indication_limit_us,
    response_limit_us,
    response_pdu,
)
from bscontroller.linklayer.periodic import (
    periodic_receive,
    periodic_transmit,
    response_receive,
    response_transmit,
    subevent_receive,
    subevent_transmit,
)
from bscontroller.linklayer.primary import (
    advertise_legacy,
    advertising_event_time_us,
    receive_scan_event,
)
from bscontroller.linklayer.resource import (
    PendingOperation,
)
from bscontroller.linklayer.results import ScanEvent
from bscontroller.linklayer.scheduler import (
    RadioPriority,
    RadioReservation,
    first_radio_gap,
)
from bscontroller.linklayer.state import DataStatus, Role
from bscontroller.linklayer.timing import (
    LL_DEFAULT_SCAN_INTERVAL_US,
    LL_INSTANT_PAST_THRESHOLD,
    window_widening,
)
from bscontroller.linklayer.transfer import (
    indication_valid,
    signed_counter,
    transfer_timing,
)
from bscontroller.radio.base import (
    PHY_OCTET_BITS,
    PHY_TIME_UNITS_PER_SECOND,
    RADIO_SWITCH_TIME_US,
    Phy,
)

if TYPE_CHECKING:
    from bscontroller.address import Address
    from bscontroller.linklayer.link import RadioLink
    from bscontroller.linklayer.results import (
        ConnectionAccepted,
        PeriodicPacketReceived,
        SyncInfoReceived,
    )
    from bscontroller.linklayer.transfer import PeriodicSyncIndication
    from bscontroller.radio.base import Received

# A Channel Map Update Indication goes out in at least six events before its
# Instant; one more covers a PAwR event already under way (LL.TS 4.2.3.13).
LL_PERIODIC_CHANNEL_MAP_UPDATE_EVENTS = LL_INSTANT_MIN_LISTENING_EVENTS + 1


def connection_window(connection: Connection) -> RadioReservation:
    """Protect the first packet exchange at each scheduled connection anchor."""
    subrating = connection.controls.subrating
    index = connection.next_scheduled_event
    period = connection.event_interval_fixed_us
    offset, phase = divmod(
        period * (index - connection.event_index) + connection.clock_remainder,
        PHY_TIME_UNITS_PER_SECOND,
    )
    anchor = connection.anchor_us + offset
    start = anchor
    duration = connection.first_exchange_us()
    anchor_uncertainty = 0
    if connection.role == Role.PERIPHERAL:
        widening = connection.receive_widening_at(anchor)
        start -= widening
        # The Central can start as late as the end of the widened RX window.
        anchor_uncertainty = 2 * widening + connection.receive_window_us
        duration += anchor_uncertainty
    elif not connection.established:
        duration = max(
            duration, connection.parameters.window_size * LL_CONN_INTERVAL_UNIT_US
        )
    # An outstanding Frame Space request or a long ACL packet can make the
    # unconstrained exchange estimate larger than the event it protects.
    own_interval = (period + phase) // PHY_TIME_UNITS_PER_SECOND
    duration = min(
        duration + connection.event_guard_us, own_interval + anchor_uncertainty
    )
    # During a change or a continuation burst, protect the temporary extra
    # radio slots. The periodic reservation then follows the committed phase.
    schedule = subrating.schedule
    regular = schedule.next_event(index) == index
    if regular and subrating.transition is None and connection.controls.pending is None:
        period *= schedule.factor
    interval, fraction = divmod(period, PHY_TIME_UNITS_PER_SECOND)
    return RadioReservation(
        start - RADIO_SWITCH_TIME_US,
        duration + RADIO_SWITCH_TIME_US,
        interval,
        interval_fraction=fraction,
        start_fraction=phase,
    )


def periodic_transmit_start_us(item: Advertising) -> int:
    """Radio preparation start for the next AUX_SYNC_IND train."""
    return item.periodic_next_us - RADIO_SWITCH_TIME_US


def periodic_transmit_window(item: Advertising) -> RadioReservation:
    """Next AUX_SYNC_IND train, including AUX_CHAIN_IND fragments and T_MAFS.

    The duration re-encodes the train; dispatch uses periodic_transmit_start_us.
    """
    return RadioReservation(
        periodic_transmit_start_us(item),
        periodic_time_us(item) + LL_MAFS_US + RADIO_SWITCH_TIME_US,
        item.periodic_interval * LL_PERIODIC_INTERVAL_UNIT_US,
        RadioPriority.PERIODIC_TX,
    )


def periodic_receive_window(sync: PeriodicSync) -> RadioReservation:
    """Next synchronized receive window, widened on both sides (Core Vol 6, Part B,
    4.2.4).
    """
    assert sync.info is not None
    if sync.pawr is not None:
        return subevent_receive_window(sync)
    widening = (
        window_widening(sync.next_us - sync.last_rx_us, sync.info.sca)
        + sync.transfer_widening_us
    )
    return RadioReservation(
        sync.next_us - widening - RADIO_SWITCH_TIME_US,
        2 * widening
        + sync.phy.airtime_us(PDU_HEADER_SIZE + PDU_ADV_EXT_PAYLOAD_SIZE_MAX)
        + RADIO_SWITCH_TIME_US,
        sync.info.interval * LL_PERIODIC_INTERVAL_UNIT_US * sync.event_step,
        RadioPriority.PERIODIC_RX,
    )


# A one-shot reservation's next occurrence lies beyond any simulated horizon.
ONE_SHOT_INTERVAL_US = 1 << 48


def subevent_start_us(item: Advertising) -> int:
    """Start of the next AUX_SYNC_SUBEVENT_IND of an enabled PAwR train."""
    assert item.pawr is not None and item.pawr_state is not None
    return item.periodic_next_us + item.pawr.subevent_offset_us(
        item.pawr_state.subevent
    )


def connect_exchange_us(phy: Phy, coding: int) -> int:
    """AUX_CONNECT_REQ, T_IFS and AUX_CONNECT_RSP (Core Vol 6, Part B, 4.4.2.12.2)."""
    return (
        phy.airtime_us(PDU_ADV_CONNECT_SIZE, coding)
        + LL_IFS_US
        + phy.airtime_us(PDU_ADV_AUX_CONNECT_RSP_SIZE)
    )


def subevent_transmit_window(item: Advertising, connecting: bool) -> RadioReservation:
    """Next AUX_SYNC_SUBEVENT_IND, or the AUX_CONNECT_REQ exchange replacing it.

    Later subevents recur every subevent interval; their data is not known yet.
    """
    timing, state = item.pawr, item.pawr_state
    assert timing is not None and state is not None
    entry = state.data.get(state.instance(timing.num_subevents))
    phy = item.secondary_phy
    coding = item.secondary_coding.factor()
    duration = (
        connect_exchange_us(phy, coding)
        if connecting
        else phy.airtime_us(
            len(subevent_pdu(item, entry.data if entry else b'').encode()), coding
        )
    )
    return RadioReservation(
        subevent_start_us(item) - RADIO_SWITCH_TIME_US,
        duration + RADIO_SWITCH_TIME_US,
        timing.subevent_interval_us
        if timing.num_subevents > 1
        else item.periodic_interval * LL_PERIODIC_INTERVAL_UNIT_US,
        RadioPriority.PERIODIC_TX,
    )


def response_receive_window(
    item: Advertising, responses: SubeventResponses
) -> RadioReservation:
    """The remaining response slots the Host asked to listen to after one subevent."""
    timing = item.pawr
    assert timing is not None
    first = responses.slot_start_us(timing)
    last = responses.start_us + timing.slot_offset_us(responses.slots[-1])
    widening = window_widening(last - responses.start_us, LL_SCA_50_PPM)
    return RadioReservation(
        first - widening - RADIO_SWITCH_TIME_US,
        last
        - first
        + timing.response_slot_spacing_us
        + 2 * widening
        + RADIO_SWITCH_TIME_US,
        ONE_SHOT_INTERVAL_US,
        RadioPriority.PERIODIC_RX,
    )


def subevent_receive_window(sync: PeriodicSync) -> RadioReservation:
    """Next synchronized PAwR subevent, widened on both sides (Core Vol 6, Part B,
    4.2.4). An AUX_SYNC_SUBEVENT_IND ends before the first response slot.
    """
    pawr, info = sync.pawr, sync.info
    assert pawr is not None and info is not None
    timing = pawr.timing
    target = sync.next_us + timing.subevent_offset_us(pawr.next_subevent)
    widening = (
        window_widening(target - sync.last_rx_us, info.sca) + sync.transfer_widening_us
    )
    return RadioReservation(
        target - widening - RADIO_SWITCH_TIME_US,
        2 * widening
        + min(
            sync.phy.airtime_us(PDU_HEADER_SIZE + PDU_ADV_EXT_PAYLOAD_SIZE_MAX),
            timing.response_slot_delay_us,
        )
        + RADIO_SWITCH_TIME_US,
        timing.subevent_interval_us
        if len(pawr.subevents) > 1
        else info.interval * LL_PERIODIC_INTERVAL_UNIT_US * sync.event_step,
        RadioPriority.PERIODIC_RX,
    )


def response_transmit_window(response: ScheduledResponse) -> RadioReservation:
    return RadioReservation(
        response.start_us - RADIO_SWITCH_TIME_US,
        response.airtime_us + RADIO_SWITCH_TIME_US,
        ONE_SHOT_INTERVAL_US,
        RadioPriority.PERIODIC_RX,
    )


# Core Vol 4, Part E, 5.3.1 reserves handles above 0x0EFF. Start at one
# to keep this implementation's existing, human-facing handle convention.
LL_CONNECTION_HANDLE_MAX = 0x0EFF


class ConnectionManager:
    def __init__(self, link: RadioLink):
        self.link = link
        self.contexts: dict[int, Connection] = {}

    def allocate_handle(self) -> int:
        """Allocate the LL identity; HCI only serializes the assigned handle."""
        return next(self.link.roles.available_connection_handles())

    def has_capacity(self) -> bool:
        return (
            len(self.contexts) < self.link.config.max_connections
            and self.link.roles.free_connection_handle_count() > 0
        )

    def can_accept(self, peer: Address) -> bool:
        peer = self.link.privacy.identity(peer)
        if peer is None:
            return False
        return self.has_capacity() and not any(
            (
                self.link.privacy.identity(connection.peer, remember=False)
                if connection.peer.is_resolvable
                else connection.peer
            )
            == peer
            for connection in self.contexts.values()
        )

    def release(self, connection: Connection) -> bool:
        """Release only the current owner, even if an old handle has been reused."""
        if self.contexts.get(connection.handle) is not connection:
            return False
        del self.contexts[connection.handle]
        return True

    def accepted(self, result: ConnectionAccepted) -> Connection | None:
        """Commit a validated connection exchange before publishing its outcome."""
        link = self.link
        # An asynchronous RF exchange may have yielded to another allocation.
        if not self.has_capacity():
            return None
        item = result.advertiser
        advertising_handle = None
        if result.pawr_sync is not None and (
            self.link.syncs.get(result.pawr_sync.handle) is not result.pawr_sync
        ):
            return None
        if result.pawr_advertiser is not None and (
            not self.link.roles.advertising._owns(result.pawr_advertiser)
            or not result.pawr_advertiser.periodic_enabled
        ):
            return None
        if item is not None:
            advertising_handle = next(
                (
                    handle
                    for handle, current in link.roles.advertising.contexts.items()
                    if current is item
                ),
                None,
            )
            # A response TX may yield to reset/disable/removal. Its old context
            # cannot create a connection or look up an already released handle.
            if advertising_handle is None or not item.enabled:
                return None
        if result.initiator is not None:
            # A delayed RF result cannot cancel a newer Host initiation request.
            if link.roles.scanning.initiating is not result.initiator:
                return None
            link.roles.scanning.cancel_initiating()
        if item is not None:
            item.enabled = False
        connection = Connection(
            link,
            result.parameters,
            link.privacy.identity(result.peer) or result.peer,
            result.role,
            result.end_us,
            csa2=result.csa2,
            self_address=result.self_address,
            window_delay_us=result.window_delay_us,
            phy=result.phy,
        )
        if item is not None and advertising_handle != -1:
            connection.advertising_handle = advertising_handle
            connection.advertising_events = item.events
        if result.pawr_advertiser is not None:
            connection.pawr_advertising_handle = next(
                (
                    handle
                    for handle, current in link.roles.advertising.contexts.items()
                    if current is result.pawr_advertiser
                ),
                None,
            )
        if result.pawr_sync is not None:
            connection.pawr_sync_handle = result.pawr_sync.handle
        self.contexts[connection.handle] = connection
        connection.local_rpa = result.local_rpa
        connection.peer_rpa = (
            result.peer
            if result.peer.is_resolvable
            and connection.peer.address_type
            in (AddressType.PUBLIC_IDENTITY, AddressType.RANDOM_IDENTITY)
            else None
        )
        connection.report_created()
        return connection

    def prepare(self, target_us: int) -> list[PendingOperation]:
        for connection in list(self.contexts.values()):
            connection.skip_missed_events()
        return [
            PendingOperation(
                connection.next_operation_us - RADIO_SWITCH_TIME_US,
                RadioPriority.CONNECTION,
                connection,
                partial(self.run, connection, target_us),
                connection.last_event_us,
            )
            for connection in self.contexts.values()
        ]

    @property
    def next_acl_flush_us(self) -> int | None:
        return min(
            (
                deadline
                for connection in self.contexts.values()
                if (deadline := connection.next_acl_flush_us) is not None
            ),
            default=None,
        )

    def expire_acl(self) -> None:
        for connection in tuple(self.contexts.values()):
            connection.expire_acl()

    async def run(self, connection: Connection, target_us: int) -> None:
        await connection.advance_to_us(
            target_us, radio_until_us=self.link.resource.deadline_us
        )


class AdvertisingManager:
    def __init__(self, link: RadioLink):
        self.link = link
        self.contexts: dict[int, Advertising] = {}
        # The Host channel classification new periodic trains start with.

    def publish(self, handle: int, item: Advertising) -> None:
        self.contexts[handle] = item

    def remove(self, handle: int) -> None:
        del self.contexts[handle]

    def clear(self) -> None:
        self.contexts.clear()

    def _owns(self, item: Advertising) -> bool:
        return any(current is item for current in self.contexts.values())

    def expire(self, item: Advertising, reason: int) -> None:
        if item.enabled and self._owns(item):
            item.enabled = False
            self.link.listener.on_advertising_ended(item, reason)

    async def expire_deadline(self, item: Advertising) -> None:
        self.expire(item, ErrorCode.ADVERTISING_TIMEOUT_ERROR)

    def prepare(self) -> list[PendingOperation]:
        link = self.link
        operations = []
        for handle, item in tuple(self.contexts.items()):
            if not self._owns(item):
                continue
            if item.enabled and (
                (item.deadline_us and link.now_us >= item.deadline_us)
                or (item.max_events and item.events >= item.max_events)
            ):
                self.expire(
                    item,
                    ErrorCode.ADVERTISING_TIMEOUT_ERROR
                    if item.deadline_us and link.now_us >= item.deadline_us
                    else ErrorCode.LIMIT_REACHED_ERROR,
                )
            # A completion callback may reset or replace any advertising set.
            # Never prepare RF work for a context released by that callback.
            if not self._owns(item):
                continue
            if item.enabled:
                operations.append(
                    PendingOperation(
                        item.next_us,
                        RadioPriority.ADVERTISING,
                        item,
                        partial(self.run, item),
                    )
                )
                if item.deadline_us:
                    operations.append(
                        PendingOperation(
                            item.deadline_us,
                            RadioPriority.EXPIRY,
                            item,
                            partial(self.expire_deadline, item),
                        )
                    )
            # Periodic trains retain a lifetime independent of ADV_EXT_IND.
            if item.periodic_enabled and item.pawr is not None:
                operations.extend(self._pawr_operations(item))
            elif item.periodic_enabled:
                operations.append(
                    PendingOperation(
                        periodic_transmit_start_us(item),
                        RadioPriority.PERIODIC_TX,
                        item,
                        partial(periodic_transmit, link, item, handle),
                    )
                )
        return operations

    def _pawr_operations(self, item: Advertising) -> list[PendingOperation]:
        """The next subevent and each pending response listen of a PAwR train.

        The first Subevent Data Request goes out here, once the enabling command
        has completed (Vol 4, Part E, 7.7.65.36).
        """
        state = item.pawr_state
        assert state is not None and item.pawr is not None
        if not state.requested:
            state.requested = True
            self.request_subevent_data(item, state.instance(item.pawr.num_subevents))
            # The Host callback may disable, remove or restart the train.
            # Prepare only the live generation; its first request stays pending.
            state = item.pawr_state
            if not self._owns(item) or state is None:
                return []
        link = self.link
        return [
            PendingOperation(
                subevent_start_us(item) - RADIO_SWITCH_TIME_US,
                RadioPriority.PERIODIC_TX,
                item,
                partial(subevent_transmit, link, item),
            ),
            *(
                PendingOperation(
                    response_receive_window(item, responses).start_us,
                    RadioPriority.PERIODIC_RX,
                    responses,
                    partial(response_receive, link, item, responses),
                )
                for responses in state.listening
            ),
        ]

    def request_subevent_data(self, item: Advertising, current: int) -> None:
        """Ask for the next window of instances; it overrides the outstanding one
        (Vol 4, Part E, 7.7.65.36).
        """
        timing, state = item.pawr, item.pawr_state
        assert timing is not None and state is not None
        state.window = state.next_window(current, timing.num_subevents)
        self.link.listener.on_subevent_data_request(
            item, state.window.start % timing.num_subevents, len(state.window)
        )

    def skip_missed_subevents(self, item: Advertising) -> None:
        """Pass, untransmitted, every instance the radio is already behind."""
        while (
            item.pawr_state is not None and subevent_start_us(item) <= self.link.now_us
        ):
            current = self.begin_subevent(item, request=False)
            if current is None:
                return
            self.end_subevent(item, current, transmitted=False)

    def begin_subevent(
        self, item: Advertising, *, request: bool = True
    ) -> Subevent | None:
        """Take the next instance's Host data and channel.

        Reaching the first instance of the requested window asks for the next
        window, so the Host has one window's time to answer. A new event first
        applies a channel map update due at its Instant.
        """
        timing, state = item.pawr, item.pawr_state
        if timing is None or state is None or not self._owns(item):
            return None
        if state.subevent == 0:
            self.apply_periodic_channel_map(item)
        instance = state.instance(timing.num_subevents)
        for stale in [key for key in state.data if key < instance]:
            del state.data[stale]
        data = state.data.pop(instance, None)
        if request and state.window and instance >= state.window.start:
            self.request_subevent_data(item, instance + 1)
            # A Host callback may disable, remove or restart this train.
            if not (self._owns(item) and item.pawr_state is state):
                return None
        return Subevent(
            state,
            instance,
            state.subevent,
            subevent_start_us(item),
            channel_selection_2(
                (item.periodic_counter ^ state.subevent) & LL_EVENT_COUNTER_MASK,
                item.periodic_aa,
                item.periodic_channel_map,
            ),
            data,
        )

    def subevent_indication(self, item: Advertising, current: Subevent) -> ExtendedPdu:
        """AUX_SYNC_SUBEVENT_IND for ``current``; new Host data gets a new DID,
        unlike the previous one and the AUX_ADV_IND's (Core Vol 6, Part B,
        4.4.2.11).
        """
        if current.data is None:
            return subevent_pdu(item)
        used = sorted({item.periodic_did, item.did})
        value = self.link.random.randrange(PDU_ADV_ADI_DID_MASK + 1 - len(used))
        for did in used:
            value += value >= did
        item.periodic_did = value
        pdu = subevent_pdu(item, current.data.data)
        timing = item.pawr
        assert timing is not None
        if pdu.acad and not fits(
            pdu,
            item.secondary_phy,
            indication_limit_us(timing, item.pawr_response_slots),
            coding=item.secondary_coding.factor(),
        ):
            # Host data that fits without the update indication keeps priority.
            pdu = replace(pdu, acad=b'')
        return pdu

    def end_subevent(
        self,
        item: Advertising,
        current: Subevent,
        *,
        transmitted: bool,
        sent: bool = False,
    ) -> None:
        """Start listening to the requested response slots, or report that the
        AUX_SYNC_SUBEVENT_IND was not sent, then move to the next subevent.

        ``sent`` marks an AUX_CONNECT_REQ in its place: the train still went out,
        but no response follows it (Vol 4, Part E, 7.7.65.37).
        """
        timing, state = item.pawr, item.pawr_state
        if (
            timing is None
            or state is None
            or state is not current.state
            or not self._owns(item)
            or state.instance(timing.num_subevents) != current.instance
        ):
            return
        entry = current.data
        if entry is not None and entry.slot_count:
            slots = deque(range(entry.slot_start, entry.slot_start + entry.slot_count))
            if transmitted:
                state.listening.append(
                    SubeventResponses(
                        current.subevent,
                        current.start_us,
                        current.channel,
                        slots,
                        transmitted,
                    )
                )
            else:
                self.link.listener.on_response_report(
                    item,
                    current.subevent,
                    False,
                    [SlotResponse(slot, None) for slot in slots],
                )
                if not (self._owns(item) and item.pawr_state is state):
                    return
        state.transmitted |= transmitted or sent
        state.subevent += 1
        if state.subevent == timing.num_subevents:
            if state.transmitted:
                item.periodic_last_counter = item.periodic_counter
            state.subevent = 0
            state.transmitted = False
            state.event += 1
            self.periodic_completed(item)

    def slot_completed(
        self, item: Advertising, responses: SubeventResponses, result: SlotResponse
    ) -> None:
        """Record one slot; report the subevent once its last slot has passed."""
        state = item.pawr_state
        if state is None or not any(entry is responses for entry in state.listening):
            return
        responses.slots.popleft()
        responses.responses.append(result)
        if not responses.slots:
            state.listening.remove(responses)
            self.link.listener.on_response_report(
                item, responses.subevent, True, responses.responses
            )

    def update_periodic_channel_maps(self, channels: int) -> None:
        """Move every periodic train to the Host's channel classification.

        The Channel Map Update Indication goes out in at least six events before
        its Instant (Core Vol 6, Part B, 4.4.2.12.1; LL.TS 4.2.3.13).
        """
        for item in self.contexts.values():
            if item.periodic_enabled and (
                item.periodic_channel_update is not None
                or channels != item.periodic_channel_map
            ):
                item.periodic_channel_update = (
                    (item.periodic_counter + LL_PERIODIC_CHANNEL_MAP_UPDATE_EVENTS)
                    & LL_EVENT_COUNTER_MASK,
                    channels,
                )

    @staticmethod
    def apply_periodic_channel_map(item: Advertising) -> None:
        """Switch to the new map at the event of its Instant."""
        update = item.periodic_channel_update
        if update is None:
            return
        instant, channels = update
        if (
            item.periodic_counter - instant
        ) & LL_EVENT_COUNTER_MASK < LL_INSTANT_PAST_THRESHOLD:
            item.periodic_channel_map = channels
            item.periodic_channel_update = None

    async def run(self, item: Advertising) -> None:
        """Prepare a complete event and commit its next advertising anchor."""
        link = self.link
        item.address, generated = link.privacy.select_local_address(
            item.own_address_type,
            item.identity_address or item.address,
            item.privacy_peer,
        )
        item.local_rpa = item.address if generated else None
        if item.identity_target is not None:
            item.target = link.privacy.target_address(item.identity_target)
        channel = next(
            channel
            for bit, channel in enumerate(LL_ADV_PRIMARY_CHANNELS)
            if item.channel_map & (1 << bit)
        )
        start = link.radio.earliest_start_us(channel, item.primary_phy, transmit=True)
        reservations = link.resource.reservations()
        if reservations:
            duration = advertising_event_time_us(item)
            latest = (
                item.deadline_us - duration
                if item.deadline_us
                else start + max(entry.max_interval_us for entry in reservations)
            )
            gap = first_radio_gap(reservations, start, duration, latest)
            if gap != start:
                item.next_us = (
                    gap
                    if gap is not None
                    else (item.deadline_us or start + item.interval_us)
                )
                return
        item.events += 1
        if item.extended:
            await link.extended.advertise(item)
        else:
            await advertise_legacy(link, item)
        item.next_us = max(
            start
            + item.interval_us
            + (0 if item.high_duty else link.random.randint(0, LL_ADV_DELAY_MAX_US)),
            link.now_us + 1,
        )

    def set_periodic_enabled(self, item: Advertising, enable: bool) -> None:
        link = self.link
        if item.periodic_enabled == enable:
            return
        interval_us = item.periodic_interval * LL_PERIODIC_INTERVAL_UNIT_US
        next_us = link.now_us + interval_us
        item.pawr_state = None
        if enable:
            duration_us = periodic_time_us(item) + LL_MAFS_US
            phase = first_radio_gap(
                link.resource.reservations(exclude=item),
                next_us,
                duration_us,
                next_us + interval_us - duration_us,
            )
            if phase is not None:
                next_us = phase
            item.periodic_aa = link.new_access_address()
            item.periodic_crc = link.random.getrandbits(PDU_CRC_SIZE * PHY_OCTET_BITS)
            item.periodic_channel_map = link.roles.host_channel_map
            item.periodic_channel_update = None
            if item.pawr is not None:
                # RspAA differs from the train's own Access Address (2.1.2).
                item.pawr_state = PawrAdvertiser(
                    link.new_access_address(exclude={item.periodic_aa})
                )
        item.periodic_enabled = enable
        item.periodic_next_us = next_us
        item.periodic_counter = 0
        item.periodic_last_counter = None

    def periodic_completed(self, item: Advertising, *, missed: bool = False) -> None:
        """Advance anchor and counter together, including radio-conflicted events.

        CSA #2 must see the counter of the next actual train event (Core Vol 6,
        Part B, 4.4.2.12); missing an event never refreshes another role's lifetime.
        """
        interval_us = item.periodic_interval * LL_PERIODIC_INTERVAL_UNIT_US
        count = (
            max(0, (self.link.now_us - item.periodic_next_us) // interval_us) + 1
            if missed
            else 1
        )
        item.periodic_next_us += count * interval_us
        item.periodic_counter = (item.periodic_counter + count) & LL_EVENT_COUNTER_MASK


class ScanningManager:
    def __init__(self, link: RadioLink):
        self.link = link
        self.active: Scanning | None = None
        self.initiating: Initiating | None = None
        self.cursor = 0

    def enable(self, scan: Scanning | None) -> None:
        self.active = scan

    def start_initiating(self, item: Initiating) -> None:
        self.initiating = item

    def cancel_initiating(self) -> Initiating | None:
        item, self.initiating = self.initiating, None
        return item

    def pawr_initiating(self, item: Advertising, subevent: int) -> Initiating | None:
        """The pending initiation that connects in this subevent of ``item``."""
        initiating = self.initiating
        if (
            initiating is not None
            and initiating.pawr_advertising is item
            and initiating.pawr_subevent == subevent
        ):
            return initiating
        return None

    def next_context(self) -> Scanning | Initiating | None:
        # Initiating through a PAwR train needs no primary channel scanning.
        scans = [
            item
            for item in (self.initiating, self.active)
            if item is not None
            and not (isinstance(item, Initiating) and item.pawr_advertising is not None)
        ]
        sync = self.link.roles.synchronization.pending
        if self.active is None and sync and sync.info is None:
            # Acquisition uses the same LL scanner, with its own role lifetime.
            # TODO: honor Host scan-enable state here (Core Vol 4, Part E, 7.8.67).
            scans.append(
                Scanning(
                    sync.address,
                    LL_DEFAULT_SCAN_INTERVAL_US,
                    LL_DEFAULT_SCAN_INTERVAL_US,
                )
            )
        scan = scans[self.cursor % len(scans)] if scans else None
        self.cursor += 1
        return scan

    async def run(self, scan: Scanning | Initiating, end: int) -> None:
        event = self.prepare_window(scan, end)
        if event is not None:
            with self.link.resource.claim():
                await receive_scan_event(self.link, event)

    def prepare_window(self, scan: Scanning | Initiating, end: int) -> ScanEvent | None:
        """Apply scan duration/period rules before preparing the next RF slice."""
        link = self.link
        now = link.now_us
        if isinstance(scan, Scanning) and scan.deadline_us and now >= scan.deadline_us:
            if not scan.period_us:
                self.active = None
                link.listener.on_scan_timeout()
                return
            scan.start_us += scan.period_us
            scan.deadline_us = scan.start_us + scan.duration_us
            link.listener.on_scan_period()
        if scan.start_us > now:
            return ScanEvent(min(end, scan.start_us))
        windows = scan.windows or (
            ScanWindow(
                Phy.LE_1M,
                scan.interval_us,
                scan.window_us,
                isinstance(scan, Scanning) and scan.active,
            ),
        )
        window, cycle, boundary = scan_window_at(windows, now - scan.start_us)
        end = min(end, scan.start_us + boundary)
        if isinstance(scan, Scanning):
            if window is not None:
                scan.active = window.active
            if scan.deadline_us:
                end = min(end, scan.deadline_us)
        if window is not None:
            channel = LL_ADV_PRIMARY_CHANNELS[cycle % len(LL_ADV_PRIMARY_CHANNELS)]
            return ScanEvent(
                end,
                channel,
                window.phy,
                scan if scan is self.initiating else None,
            )
        return ScanEvent(end)


class SynchronizationManager:
    def __init__(self, link: RadioLink):
        self.link = link
        self.pending: PeriodicSync | None = None
        self.active: dict[int, PeriodicSync] = {}
        # Synchronizing from LL_PERIODIC_SYNC_IND; the Host learns these handles
        # only from the transfer event (Core Vol 4, Part E, 7.7.65.24).
        self.transfers: dict[int, PeriodicSync] = {}

    def _free_handle(self) -> int | None:
        used = self.active.keys() | self.transfers.keys()
        if self.pending is not None:
            used.add(self.pending.handle)
        return next(
            (
                handle
                for handle in range(1, self.link.config.max_periodic_syncs + 1)
                if handle not in used
            ),
            None,
        )

    def start(self, sync: PeriodicSync) -> None:
        handle = self._free_handle()
        assert handle is not None, 'synchronization_status admits a free handle'
        sync.handle = handle
        self.pending = sync

    def cancel(self) -> PeriodicSync | None:
        item, self.pending = self.pending, None
        return item

    def terminate(self, handle: int) -> None:
        del self.active[handle]

    def configure_reports(
        self, handle: int, *, enabled: bool, filter_duplicates: bool
    ) -> None:
        sync = self.active[handle]
        sync.reports_enabled = enabled
        sync.filter_duplicates = filter_duplicates

    def contexts(self) -> list[PeriodicSync]:
        return (
            list(self.active.values())
            + list(self.transfers.values())
            + ([self.pending] if self.pending else [])
        )

    def tracking(self, address: Address, sid: int) -> bool:
        """Already synchronized with, or synchronizing with, this train (5.1.13)."""
        pending = self.pending
        return any(
            sync.address == address and sync.sid == sid
            for sync in (*self.active.values(), *self.transfers.values())
        ) or bool(
            pending
            and (pending.info is not None or not pending.use_list)
            and pending.address == address
            and pending.sid == sid
        )

    @staticmethod
    def _establishment_failed(link: RadioLink, sync: PeriodicSync) -> bool:
        """Six events without an AUX_SYNC_IND end establishment (4.4.5.1)."""
        assert sync.info is not None
        return (
            link.now_us >= sync.deadline_us
            or (sync.counter - sync.info.event_counter) & LL_EVENT_COUNTER_MASK
            >= LL_PERIODIC_SYNC_ESTABLISHMENT_EVENTS
        )

    def expire(self) -> None:
        link, sync = self.link, self.pending
        if sync and sync.info is not None and self._establishment_failed(link, sync):
            self.pending = None
            link.listener.on_sync_established(
                sync, ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR
            )
        for handle, transfer in list(self.transfers.items()):
            if self._establishment_failed(link, transfer):
                del self.transfers[handle]
                link.listener.on_sync_transfer_received(
                    transfer, ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR
                )
        for handle, active in list(self.active.items()):
            if link.now_us >= active.deadline_us:
                del self.active[handle]
                link.listener.on_sync_lost(active)

    def prepare(self) -> list[PendingOperation]:
        link = self.link
        operations = []
        for sync in self.contexts():
            if sync.info is None:
                continue
            if sync.pawr is None:
                operations.append(
                    PendingOperation(
                        periodic_receive_window(sync).start_us,
                        RadioPriority.PERIODIC_RX,
                        sync,
                        partial(periodic_receive, link, sync),
                    )
                )
                continue
            operations.append(
                PendingOperation(
                    subevent_receive_window(sync).start_us,
                    RadioPriority.PERIODIC_RX,
                    sync,
                    partial(subevent_receive, link, sync),
                )
            )
            operations.extend(
                PendingOperation(
                    response.start_us - RADIO_SWITCH_TIME_US,
                    RadioPriority.PERIODIC_RX,
                    response,
                    partial(response_transmit, link, sync, response),
                )
                for response in sync.pawr.scheduled
            )
        return operations

    def _pawr(self, acad: bytes) -> PawrSync | None:
        """PAwR state for a train announced with this ACAD, or None for a train
        without responses or a Controller without the feature (4.4.5.1.2).
        """
        found = acad_timing(acad)
        if found is None or not (
            self.link.features
            & LeFeatureMask.PERIODIC_ADVERTISING_WITH_RESPONSES_SCANNER
        ):
            return None
        rsp_aa, timing = found
        return PawrSync(timing, rsp_aa)

    def acquired(self, result: SyncInfoReceived) -> None:
        """Commit SyncInfo only to the currently requested acquisition role."""
        sync = self.pending
        if not (sync and sync.info is None):
            return
        link = self.link
        if any(
            active.address == result.address and active.sid == result.sid
            for active in (*self.active.values(), *self.transfers.values())
        ):
            return
        if not (
            (result.address, result.sid) in link.periodic_advertiser_list
            if sync.use_list
            else sync.address == result.address and sync.sid == result.sid
        ):
            return
        pawr = self._pawr(result.acad)
        if pawr is not None and not pawr.timing.valid:
            # A train whose responses cannot be timed is not synchronized with.
            return
        info, packet = result.info, result.packet
        sync.pawr = pawr
        sync.address, sync.sid = result.address, result.sid
        sync.advertiser_address = result.advertiser
        sync.info, sync.next_us, sync.counter = (
            info,
            packet.start_us + info.offset_us,
            info.event_counter,
        )
        sync.last_rx_us, sync.phy = packet.start_us, packet.phy
        sync.deadline_us = sync.next_us + (
            LL_PERIODIC_SYNC_ESTABLISHMENT_EVENTS
            * info.interval
            * LL_PERIODIC_INTERVAL_UNIT_US
        )

    def transfer_received(
        self, connection: Connection, indication: PeriodicSyncIndication
    ) -> None:
        """Synchronize with a transferred train, then notify the Host.

        Core Vol 6, Part B, 5.1.13: act only if the Host enabled receipt and the
        train is neither synchronized nor synchronizing; ignore a PDU without
        exactly one supported PHY bit. Like those, a SyncInfo that cannot
        describe a train, or counters that break 2.4.2.27, are ignored before
        any handle or radio time is taken. The Periodic Advertiser List does not
        apply.
        """
        config = connection.sync_transfer
        phy = indication.transferred_phy
        if not config.enabled or phy is None:
            return
        try:
            info = SyncInfo.decode(indication.sync_info, transfer=True)
        except ValueError:
            return
        if not indication_valid(indication, info, connection):
            return
        link = self.link
        advertiser = indication.advertiser
        address = link.privacy.identity(advertiser)
        if address is None or self.tracking(address, indication.sid):
            return
        pawr = (
            PawrSync(indication.pawr, indication.rsp_aa)
            if indication.pawr is not None
            else None
        )
        sync = PeriodicSync(
            address,
            indication.sid,
            config.timeout_us,
            0,
            config.skip,
            config.reports_enabled,
            phy=phy,
            filter_duplicates=config.filter_duplicates,
            advertiser_address=advertiser,
            transfer=TransferOrigin(connection.handle, indication.identifier),
            cte_type=config.cte_type,
            pawr=pawr,
        )
        if pawr is not None and not pawr.timing.valid:
            # Responses cannot be timed: fail the transfer (LL.TS 4.2.4.24).
            sync.info = info
            link.listener.on_sync_transfer_received(
                sync, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR
            )
            return
        handle = self._free_handle()
        if handle is None:
            sync.info = info
            link.listener.on_sync_transfer_received(
                sync, ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR
            )
            return
        timing = transfer_timing(indication, info, connection)
        sync.handle = handle
        # Count establishment events from the first one listened for.
        sync.info = replace(info, event_counter=timing.counter)
        sync.next_us, sync.counter = timing.anchor_us, timing.counter
        sync.last_rx_us = timing.reference_us
        sync.transfer_widening_us = timing.widening_us
        sync.deadline_us = sync.next_us + (
            LL_PERIODIC_SYNC_ESTABLISHMENT_EVENTS
            * info.interval
            * LL_PERIODIC_INTERVAL_UNIT_US
        )
        self.transfers[handle] = sync

    def _owned(self, sync: PeriodicSync) -> bool:
        return (
            self.pending is sync
            or self.active.get(sync.handle) is sync
            or self.transfers.get(sync.handle) is sync
        )

    def _refresh(self, sync: PeriodicSync, packet: Received, subevent: int) -> None:
        """Retime the train from a received packet: for PAwR, the event anchor
        is its subevent's start less the subevent offset.
        """
        offset = (
            sync.pawr.timing.subevent_offset_us(subevent)
            if sync.pawr is not None
            else 0
        )
        sync.next_us = packet.start_us - offset
        sync.last_rx_us = packet.start_us
        sync.last_counter = sync.counter
        sync.transfer_widening_us = 0
        sync.deadline_us = packet.end_us + sync.timeout_us
        if sync.pawr is not None:
            sync.pawr.last_subevent = subevent

    def received(self, result: PeriodicPacketReceived) -> bool:
        """A validated AUX_SYNC_IND or AUX_SYNC_SUBEVENT_IND refreshes the role,
        then publishes success.

        While synchronizing, an excluded CTE type, or the absence of a CTE,
        fails the attempt (Core Vol 6, Part B, 4.4.3.4). Only a transfer can
        set CTE_Type; it reports Unsupported Remote Feature, as Create Sync
        does (Vol 4, Part E, 7.8.67). A received AUX_SYNC_SUBEVENT_IND times the
        Host responses due in its subevent (4.4.2.12.2).
        """
        sync, packet = result.sync, result.packet
        if not self._owned(sync):
            return False
        if not sync.established and sync.excludes(result.pdu.cte):
            if self.transfers.get(sync.handle) is sync:
                del self.transfers[sync.handle]
                self.link.listener.on_sync_transfer_received(
                    sync, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
                )
            return False
        self._refresh(sync, packet, result.subevent)
        sync.receive_channel_map(result.pdu.acad)
        if sync.pawr is not None:
            self._subevent_received(sync, result)
        if not sync.established:
            sync.established = True
            if self.pending is sync:
                self.pending = None
            transferred = self.transfers.pop(sync.handle, None) is sync
            self.active[sync.handle] = sync
            if transferred:
                self.link.listener.on_sync_transfer_received(sync, ErrorCode.SUCCESS)
            else:
                self.link.listener.on_sync_established(sync, ErrorCode.SUCCESS)
        return True

    def refreshed(
        self, sync: PeriodicSync, packet: Received, subevent: int, channel: int
    ) -> bool:
        """An AUX_CONNECT_REQ sent in place of an AUX_SYNC_SUBEVENT_IND keeps the
        train's timing and supervision, but carries no data (4.4.2.12.2).
        """
        if not self._owned(sync):
            return False
        if sync.established:
            self._refresh(sync, packet, subevent)
        return True

    def _subevent_received(
        self, sync: PeriodicSync, result: PeriodicPacketReceived
    ) -> None:
        pawr = sync.pawr
        assert pawr is not None
        key = (pawr.event, result.subevent)
        pawr.received[key] = (result.packet.start_us, result.channel)
        for pending in [
            item for item in pawr.pending if (item.event, item.subevent) == key
        ]:
            pawr.pending.remove(pending)
            self._schedule(sync, pending, result.packet.start_us, result.channel)

    def _schedule(
        self, sync: PeriodicSync, pending: PendingResponse, start_us: int, channel: int
    ) -> None:
        """Time a response from its subevent's AUX_SYNC_SUBEVENT_IND; include AdvA
        only if the packet still fits the response slot (2.3.1.10, 4.4.2.2.4).
        """
        pawr = sync.pawr
        assert pawr is not None
        timing = pawr.timing
        start = start_us + timing.slot_offset_us(pending.slot)
        if start - RADIO_SWITCH_TIME_US <= self.link.now_us:
            return
        tx_power = RESPONSE_TX_POWER_DBM if pawr.include_tx_power else None
        address = self.link.public_address or self.link.random_address
        pdu = response_pdu(pending.data, address, tx_power)
        if not fits(pdu, sync.phy, response_limit_us(timing)):
            pdu = response_pdu(pending.data, None, tx_power)
        airtime = sync.phy.airtime_us(len(pdu.encode()))
        pawr.scheduled.append(ScheduledResponse(start, channel, pdu, airtime))

    def respond(
        self,
        sync: PeriodicSync,
        request_counter: int,
        request_subevent: int,
        response_subevent: int,
        slot: int,
        data: bytes,
    ) -> int:
        """Accept Host response data for one slot (Vol 4, Part E, 7.8.126).

        The response goes in the first ``response_subevent`` at or after the
        request: in the requesting event, or the next one for an earlier
        subevent. It must be sent within one periodic advertising interval of
        the request (Core Vol 6, Part B, 4.4.2.2.4).
        """
        pawr, info = sync.pawr, sync.info
        assert info is not None
        if (
            pawr is None
            or response_subevent >= pawr.timing.num_subevents
            or response_subevent not in pawr.subevents
        ):
            return ErrorCode.COMMAND_DISALLOWED_ERROR
        timing = pawr.timing
        tx_power = RESPONSE_TX_POWER_DBM if pawr.include_tx_power else None
        if not fits(
            response_pdu(data, None, tx_power), sync.phy, response_limit_us(timing)
        ):
            return ErrorCode.PACKET_TOO_LONG_ERROR
        event = pawr.event + signed_counter(request_counter - sync.counter)
        if response_subevent < request_subevent:
            event += 1
        start = (
            sync.next_us
            + (event - pawr.event) * info.interval * LL_PERIODIC_INTERVAL_UNIT_US
            + timing.subevent_offset_us(response_subevent)
            + timing.slot_offset_us(slot)
        )
        received = pawr.received.get((event, response_subevent))
        if received is not None:
            start = received[0] + timing.slot_offset_us(slot)
        if start - RADIO_SWITCH_TIME_US <= self.link.now_us:
            return ErrorCode.TOO_LATE_ERROR
        pending = PendingResponse(event, response_subevent, slot, bytes(data))
        if received is not None:
            self._schedule(sync, pending, *received)
        else:
            pawr.pending.append(pending)
        return ErrorCode.SUCCESS

    def response_sent(self, sync: PeriodicSync, response: ScheduledResponse) -> None:
        pawr = sync.pawr
        if pawr is not None and any(item is response for item in pawr.scheduled):
            pawr.scheduled.remove(response)

    def set_subevents(
        self, sync: PeriodicSync, subevents: tuple[int, ...], include_tx_power: bool
    ) -> None:
        """Listen to these subevents only, from the next one due (Vol 4, Part E,
        7.8.127).
        """
        pawr = sync.pawr
        assert pawr is not None
        upcoming = (
            pawr.subevents[pawr.cursor] if pawr.cursor < len(pawr.subevents) else 0
        )
        pawr.subevents = tuple(sorted(set(subevents)))
        pawr.include_tx_power = include_tx_power
        pawr.cursor = next(
            (
                index
                for index, subevent in enumerate(pawr.subevents)
                if subevent >= upcoming
            ),
            len(pawr.subevents),
        )
        if pawr.cursor == len(pawr.subevents):
            pawr.cursor = 0
            self.completed(sync)

    def subevent_failed(self, sync: PeriodicSync, subevent: int) -> None:
        """No AUX_SYNC_SUBEVENT_IND in a synchronized subevent listened to
        (Vol 4, Part E, 7.7.65.15)."""
        self.report(sync, b'', None, None, DataStatus.RECEIVE_FAILED, subevent=subevent)

    def subevent_completed(self, sync: PeriodicSync) -> None:
        """Move to the next synchronized subevent; the event ends after the last."""
        pawr = sync.pawr
        if pawr is None or not self._owned(sync):
            return
        pawr.cursor += 1
        if pawr.cursor >= len(pawr.subevents):
            pawr.cursor = 0
            self.completed(sync)

    def completed(
        self, sync: PeriodicSync, *, missed_until_us: int | None = None
    ) -> None:
        """Keep the CSA counter on the anchor grid; apply Host skip after establishment
        (Core Vol 6, Part B, 4.4.5).
        """
        assert sync.info is not None
        step = sync.event_step
        interval_us = sync.info.interval * LL_PERIODIC_INTERVAL_UNIT_US * step
        count = (
            max(0, (self.link.now_us - missed_until_us) // interval_us) + 1
            if missed_until_us is not None
            else 1
        )
        sync.next_us += count * interval_us
        sync.counter = (sync.counter + count * step) & LL_EVENT_COUNTER_MASK
        if sync.pawr is not None:
            previous_event = sync.pawr.event
            sync.pawr.event += count * step
            # Skip advances the next listening anchor, but responses to the
            # event just received are still due in its remaining slots.
            sync.pawr.forget_before(previous_event)

    def report(
        self,
        sync: PeriodicSync,
        data,
        rssi,
        tx_power,
        status,
        *,
        subevent: int | None = None,
    ) -> None:
        # A callback may remove this context before the next fragment arrives.
        if self.active.get(sync.handle) is sync and sync.reports_enabled:
            self.link.listener.on_periodic_report(
                sync,
                data,
                rssi,
                tx_power,
                status,
                counter=sync.counter,
                subevent=subevent,
            )

    def reportable(self, sync: PeriodicSync, adi: int | None) -> bool:
        """Whether this event's data goes to the Host.

        Not while reporting is disabled, nor when filtering finds a duplicate: an
        ADI equal to that of the train's previous report that carried one (Core
        Vol 6, Part B, 4.4.5.1.1). A PDU without ADI is never a duplicate.
        """
        return sync.reports_enabled and not (
            sync.filter_duplicates and adi is not None and adi == sync.reported_adi
        )

    def reported(self, sync: PeriodicSync, adi: int | None, status: int) -> None:
        """Remember the ADI of the report just completed for the Host.

        Record it whether or not filtering is enabled, so enabling filtering later
        suppresses the data already reported. A truncated report becomes the
        previous report but makes nothing a duplicate, so the next copy of its
        data is reported, as for an incomplete advertising report (4.4.3.5).
        """
        if (
            adi is not None
            and self.active.get(sync.handle) is sync
            and sync.reports_enabled
        ):
            sync.reported_adi = adi if status == DataStatus.COMPLETE else None


class LinkRoles:
    def __init__(self, link: RadioLink):
        self.host_channel_map = LL_DATA_CHANNEL_MAP_ALL
        self.connections = ConnectionManager(link)
        self.advertising = AdvertisingManager(link)
        self.scanning = ScanningManager(link)
        self.synchronization = SynchronizationManager(link)
        self.iso = IsoDataPaths(link.listener)
        self.broadcast = BroadcastManager(link, self.iso)

    def _used_connection_handles(self) -> set[int]:
        return (
            self.connections.contexts.keys()
            | self.broadcast.connection_handles()
            | self.iso.connection_handles()
        )

    def available_connection_handles(self) -> Iterator[int]:
        """Return free LL handles, including reservations for pending BIGs."""
        used = self._used_connection_handles()
        return (
            handle
            for handle in range(1, LL_CONNECTION_HANDLE_MAX + 1)
            if handle not in used
        )

    def free_connection_handle_count(self) -> int:
        return LL_CONNECTION_HANDLE_MAX - len(self._used_connection_handles())

    def reset(self) -> None:
        self.broadcast.reset()
        self.iso.reset()
        self.connections.contexts.clear()
        self.advertising.clear()
        self.host_channel_map = LL_DATA_CHANNEL_MAP_ALL
        self.scanning.enable(None)
        self.scanning.cancel_initiating()
        self.scanning.cursor = 0
        self.synchronization.cancel()
        self.synchronization.active.clear()
        self.synchronization.transfers.clear()

    def reservations(self, exclude: object = None) -> list[RadioReservation]:
        reservations = [
            connection_window(item)
            for item in self.connections.contexts.values()
            if item is not exclude
        ]
        for item in self.advertising.contexts.values():
            if not item.periodic_enabled:
                continue
            if item.pawr_state is None:
                if item is not exclude:
                    reservations.append(periodic_transmit_window(item))
                continue
            if item is not exclude:
                reservations.append(
                    subevent_transmit_window(
                        item,
                        self.scanning.pawr_initiating(item, item.pawr_state.subevent)
                        is not None,
                    )
                )
            reservations.extend(
                response_receive_window(item, responses)
                for responses in item.pawr_state.listening
                if responses is not exclude
            )
        for sync in self.synchronization.contexts():
            if sync.info is None:
                continue
            if sync is not exclude:
                reservations.append(periodic_receive_window(sync))
            if sync.pawr is not None:
                reservations.extend(
                    response_transmit_window(response)
                    for response in sync.pawr.scheduled
                    if response is not exclude
                )
        reservations.extend(self.broadcast.reservations(exclude))
        return reservations
