# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Periodic synchronization, transfer and response-slot scheduling."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_EVENT_COUNTER_MASK,
    LL_PERIODIC_INTERVAL_UNIT_US,
    LL_PERIODIC_SYNC_ESTABLISHMENT_EVENTS,
    PDU_ADV_EXT_PAYLOAD_SIZE_MAX,
    PDU_ADV_TYPE_MASK,
    PDU_HEADER_SIZE,
    AdvertisingMode,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising_pdu import ExtendedPdu, SyncInfo
from bscontroller.linklayer.discovery import PeriodicSync, TransferOrigin
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.pawr import (
    RESPONSE_TX_POWER_DBM,
    PawrSync,
    PendingResponse,
    ScheduledResponse,
    acad_timing,
    fits,
    response_limit_us,
    response_pdu,
)
from bscontroller.linklayer.resource import PendingOperation
from bscontroller.linklayer.results import PeriodicPacketReceived
from bscontroller.linklayer.scheduler import (
    ONE_SHOT_INTERVAL_US,
    RadioPriority,
    RadioReservation,
)
from bscontroller.linklayer.state import DataStatus
from bscontroller.linklayer.synchronization_radio import (
    periodic_receive,
    response_transmit,
    subevent_receive,
)
from bscontroller.linklayer.timing import (
    counter_distance,
    signed_counter_distance,
    window_widening,
)
from bscontroller.linklayer.transfer import (
    indication_valid,
    transfer_timing,
)
from bscontroller.radio.base import RADIO_SWITCH_TIME_US

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bscontroller.address import Address
    from bscontroller.linklayer.connection import Connection
    from bscontroller.linklayer.link import RadioLink
    from bscontroller.linklayer.results import SyncInfoReceived
    from bscontroller.linklayer.transfer import PeriodicSyncIndication
    from bscontroller.radio.base import Received


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


class SynchronizationManager:
    def __init__(
        self,
        link: RadioLink,
        *,
        accept_connection: Callable[[PeriodicSync, int, Received], Awaitable[None]],
        observe_big_info: Callable[[PeriodicSync, bytes, int], None],
    ):
        self.link = link
        self._accept_connection = accept_connection
        self._observe_big_info = observe_big_info
        self.pending: PeriodicSync | None = None
        self.active: dict[int, PeriodicSync] = {}
        # Synchronizing from LL_PERIODIC_SYNC_IND; the Host learns these handles
        # only from the transfer event (Core Vol 4, Part E, 7.7.65.24).
        self.transfers: dict[int, PeriodicSync] = {}

    def reset(self) -> None:
        self.pending = None
        self.active.clear()
        self.transfers.clear()

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

    def scan_disabled(self) -> None:
        """Keep Create Sync pending; discard an incomplete acquisition (HCI 7.8.67)."""
        if self.pending is not None:
            # A returning primary, auxiliary or first train RX still owns the old
            # object. Established and transferred syncs have separate lifetimes.
            self.pending = replace(self.pending, info=None, pawr=None, deadline_us=0)

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
            or counter_distance(sync.counter, sync.info.event_counter)
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

    def reservations(self, exclude: object) -> Iterator[RadioReservation]:
        for sync in self.contexts():
            if sync.info is None:
                continue
            if sync is not exclude:
                yield periodic_receive_window(sync)
            if sync.pawr is not None:
                for response in sync.pawr.scheduled:
                    if response is not exclude:
                        yield response_transmit_window(response)

    def prepare(self) -> list[PendingOperation]:
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
                        partial(self.run_periodic, sync),
                    )
                )
                continue
            operations.append(
                PendingOperation(
                    subevent_receive_window(sync).start_us,
                    RadioPriority.PERIODIC_RX,
                    sync,
                    partial(self.run_subevent, sync),
                )
            )
            operations.extend(
                PendingOperation(
                    response.start_us - RADIO_SWITCH_TIME_US,
                    RadioPriority.PERIODIC_RX,
                    response,
                    partial(self._send_response, sync, response),
                )
                for response in sync.pawr.scheduled
            )
        return operations

    async def run_periodic(self, sync: PeriodicSync) -> None:
        await periodic_receive(self, sync, self._observe_big_info)

    async def run_subevent(self, sync: PeriodicSync) -> None:
        sync.apply_channel_map()
        pawr = sync.pawr
        assert pawr is not None
        subevent = pawr.next_subevent
        link = self.link
        received = await subevent_receive(link.radio, link.resource, sync, subevent)
        if received is None:
            self.subevent_failed(sync, subevent)
            self.subevent_completed(sync)
            return
        packet, channel = received
        ext = None
        # AUX_CONNECT_REQ replaces AUX_SYNC_SUBEVENT_IND on this train.
        if packet.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.AUX_CONNECT_REQ:
            if self.refreshed(sync, packet, subevent):
                await self._accept_connection(sync, channel, packet)
                self.subevent_completed(sync)
            return
        try:
            ext = ExtendedPdu.decode(packet.pdu)
        except ValueError:
            pass
        if (
            ext is None
            or ext.kind != AdvertisingPduType.AUX_SYNC_SUBEVENT_IND
            or ext.mode != AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
        ):
            self.subevent_failed(sync, subevent)
        elif not self.received(
            PeriodicPacketReceived(sync, packet, ext, subevent, channel)
        ):
            return
        elif self.reportable(sync, ext.adi):
            self.report(
                sync,
                ext.data,
                packet.rssi,
                ext.tx_power,
                DataStatus.COMPLETE,
                subevent=subevent,
            )
            self.reported(sync, ext.adi, DataStatus.COMPLETE)
        self.subevent_completed(sync)

    async def _send_response(
        self, sync: PeriodicSync, response: ScheduledResponse
    ) -> None:
        self.response_sent(sync, response)
        link = self.link
        await response_transmit(
            link.radio,
            link.resource,
            sync,
            response,
            link.with_radiative_power(response.pdu).encode(),
            link.transmit,
        )

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

    def refreshed(self, sync: PeriodicSync, packet: Received, subevent: int) -> bool:
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
        event = pawr.event + signed_counter_distance(request_counter, sync.counter)
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
