# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Advertising set lifetime, train scheduling and radio reservations."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_ADV_DELAY_MAX_US,
    LL_ADV_PRIMARY_CHANNELS,
    LL_EVENT_COUNTER_MASK,
    LL_IFS_US,
    LL_INSTANT_MIN_LISTENING_EVENTS,
    LL_MAFS_US,
    LL_PERIODIC_INTERVAL_UNIT_US,
    LL_SCA_50_PPM,
    PDU_ADV_ADI_DID_MASK,
    PDU_ADV_AUX_CONNECT_RSP_SIZE,
    PDU_ADV_CONNECT_SIZE,
    PDU_CRC_SIZE,
)
from bscontroller.linklayer.advertising_pdu import ExtendedPdu
from bscontroller.linklayer.channel_selection import channel_selection_2
from bscontroller.linklayer.discovery import Advertising
from bscontroller.linklayer.extended import (
    advertise_extended,
    advertising_time_us,
    periodic_time_us,
    subevent_pdu,
)
from bscontroller.linklayer.pawr import (
    PawrAdvertiser,
    SlotResponse,
    Subevent,
    SubeventResponses,
    fits,
    indication_limit_us,
)
from bscontroller.linklayer.pawr_radio import response_receive, subevent_transmit
from bscontroller.linklayer.periodic import periodic_transmit
from bscontroller.linklayer.primary import advertise_legacy, advertising_event_time_us
from bscontroller.linklayer.resource import PendingOperation
from bscontroller.linklayer.scheduler import (
    ONE_SHOT_INTERVAL_US,
    RadioPriority,
    RadioReservation,
    first_radio_gap,
)
from bscontroller.linklayer.timing import LL_INSTANT_PAST_THRESHOLD, window_widening
from bscontroller.radio.base import PHY_OCTET_BITS, RADIO_SWITCH_TIME_US, Phy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bscontroller.linklayer.discovery import Initiating
    from bscontroller.linklayer.link import RadioLink

# A Channel Map Update Indication goes out in at least six events before its
# Instant; one more covers a PAwR event already under way (LL.TS 4.2.3.13).
LL_PERIODIC_CHANNEL_MAP_UPDATE_EVENTS = LL_INSTANT_MIN_LISTENING_EVENTS + 1


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


class AdvertisingManager:
    def __init__(
        self,
        link: RadioLink,
        *,
        pending_initiation: Callable[[Advertising, int], Initiating | None],
        connect: Callable[[Advertising, Initiating, int, int], Awaitable[None]],
        channel_map: Callable[[], int],
        big_info: Callable[[int, int], bytes],
    ):
        self.link = link
        self._pending_initiation = pending_initiation
        self._connect = connect
        self._channel_map = channel_map
        self._big_info = big_info
        self.contexts: dict[int, Advertising] = {}
        self._current_event: Advertising | None = None
        self._periodic_event = False

    def publish(self, handle: int, item: Advertising) -> None:
        self.contexts[handle] = item

    def remove(self, handle: int) -> None:
        if self.contexts.pop(handle) is self._current_event:
            self._current_event = None

    def clear(self) -> None:
        self.contexts.clear()
        self._current_event = None

    def handle_of(self, item: Advertising) -> int | None:
        return next(
            (handle for handle, current in self.contexts.items() if current is item),
            None,
        )

    def owns(self, item: Advertising) -> bool:
        return self.handle_of(item) is not None

    @contextmanager
    def transmitting(
        self, item: Advertising, *, periodic: bool = False
    ) -> Iterator[None]:
        """One radio runs one event. Each advertising enable controls its own train."""
        self._current_event = item
        self._periodic_event = periodic
        try:
            yield
        finally:
            self._current_event = None

    def _end_event(self, item: Advertising, *, periodic: bool = False) -> None:
        if self._current_event is item and self._periodic_event == periodic:
            self._current_event = None

    def stop(self, item: Advertising, *, event_completed: bool = False) -> None:
        item.enabled = False
        # A Host stop cancels the remaining event after the current radio PDU.
        # A connection ends the advertising event through its normal completion.
        if not event_completed:
            self._end_event(item)

    def start(
        self,
        item: Advertising,
        duration_us: int = 0,
        max_events: int = 0,
        *,
        interval_unit_us: int | None = None,
    ) -> None:
        """Start a new duration and event count, including on re-enable (7.8.56)."""
        if not item.enabled:
            self._end_event(item)
            # A new enable takes the configured public address. Re-enabling
            # during an exchange leaves the active advertising identity intact.
            if item.use_public_address and self.link.public_address is not None:
                item.address = item.identity_address = self.link.public_address
        if interval_unit_us is not None:
            airtime = advertising_time_us(item)
            item.interval_us = max(
                item.interval_us,
                (airtime + interval_unit_us - 1) // interval_unit_us * interval_unit_us,
            )
        item.enabled = True
        item.enable_count += 1
        item.next_us = self.link.now_us + 1
        item.events = 0
        item.max_events = max_events
        item.deadline_us = self.link.now_us + duration_us if duration_us else 0

    def event_is_current(self, item: Advertising) -> bool:
        return self._current_event is item and self.owns(item)

    def expire(self, item: Advertising, reason: int) -> None:
        if item.enabled and self.owns(item):
            self.stop(item)
            self.link.listener.on_advertising_ended(item, reason)

    async def expire_deadline(self, item: Advertising) -> None:
        self.expire(item, ErrorCode.ADVERTISING_TIMEOUT_ERROR)

    def reservations(self, exclude: object) -> Iterator[RadioReservation]:
        for item in self.contexts.values():
            if not item.periodic_enabled:
                continue
            state = item.pawr_state
            if state is None:
                if item is not exclude:
                    yield periodic_transmit_window(item)
                continue
            if item is not exclude:
                initiating = self._pending_initiation(item, state.subevent)
                yield subevent_transmit_window(item, initiating is not None)
            for responses in state.listening:
                if responses is not exclude:
                    yield response_receive_window(item, responses)

    def prepare(self) -> list[PendingOperation]:
        link = self.link
        operations = []
        for handle, item in tuple(self.contexts.items()):
            if not self.owns(item):
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
            if not self.owns(item):
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
                        partial(self.run_periodic, item, handle),
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
            if not self.owns(item) or state is None:
                return []
        return [
            PendingOperation(
                subevent_start_us(item) - RADIO_SWITCH_TIME_US,
                RadioPriority.PERIODIC_TX,
                item,
                partial(self._run_subevent, item),
            ),
            *(
                PendingOperation(
                    response_receive_window(item, responses).start_us,
                    RadioPriority.PERIODIC_RX,
                    responses,
                    partial(self._listen_slot, item, responses),
                )
                for responses in state.listening
            ),
        ]

    async def run_periodic(self, item: Advertising, handle: int) -> None:
        await periodic_transmit(self, item, handle, self._big_info)

    async def _run_subevent(self, item: Advertising) -> None:
        self.skip_missed_subevents(item)
        current = self.begin_subevent(item)
        if current is None:
            return
        initiating = self._pending_initiation(item, current.subevent)
        if initiating is not None:
            await self._connect(item, initiating, current.channel, current.start_us)
            self.end_subevent(item, current, transmitted=False, sent=True)
            return
        pdu = self.subevent_indication(item, current).encode()
        link = self.link
        sent = await subevent_transmit(
            link.radio, link.resource, item, current, pdu, link.transmit
        )
        self.end_subevent(item, current, transmitted=sent)

    async def _listen_slot(
        self, item: Advertising, responses: SubeventResponses
    ) -> None:
        result = await response_receive(
            self.link.radio, self.link.resource, item, responses
        )
        self.slot_completed(item, responses, result)

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
        if timing is None or state is None or not self.owns(item):
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
            if not (self.owns(item) and item.pawr_state is state):
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
            or not self.owns(item)
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
                if not (self.owns(item) and item.pawr_state is state):
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
        if (
            not self.owns(item)
            or state is None
            or not any(entry is responses for entry in state.listening)
        ):
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
            if item.deadline_us:
                latest = item.deadline_us - duration
            else:
                # Derive the horizon from recurring reservations' actual periods.
                horizon = max(
                    (
                        entry.max_interval_us
                        for entry in reservations
                        if entry.interval_us != ONE_SHOT_INTERVAL_US
                    ),
                    default=item.interval_us,
                )
                latest = start + horizon
            gap = first_radio_gap(reservations, start, duration, latest)
            if gap != start:
                item.next_us = (
                    gap
                    if gap is not None
                    else (item.deadline_us or start + item.interval_us)
                )
                return
        enable_count = item.enable_count
        item.events += 1
        with self.transmitting(item):
            if item.extended:
                await advertise_extended(self, item)
            else:
                await advertise_legacy(self, item)
            if not self.event_is_current(item) or item.enable_count != enable_count:
                return
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
        self._end_event(item, periodic=True)
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
            item.periodic_channel_map = self._channel_map()
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
