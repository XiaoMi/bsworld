# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL role owners: lifetime transitions, event preparation and completion.

These managers own active contexts. RF procedures deliver validated outcomes;
the shared-radio dispatcher does not implement advertising or sync lifetimes.
This is an implementation boundary inside Core Vol 6, Part B's Link Layer.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from bscontroller.address import AddressType
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_ADV_DELAY_MAX_US,
    LL_ADV_PRIMARY_CHANNELS,
    LL_CONN_INTERVAL_UNIT_US,
    LL_EVENT_COUNTER_MASK,
    LL_MAFS_US,
    LL_PERIODIC_INTERVAL_UNIT_US,
    LL_PERIODIC_SYNC_ESTABLISHMENT_EVENTS,
    PDU_ADV_EXT_PAYLOAD_SIZE_MAX,
    PDU_CRC_SIZE,
    PDU_HEADER_SIZE,
)
from bscontroller.linklayer.connection import Connection
from bscontroller.linklayer.discovery import (
    Advertising,
    Initiating,
    PeriodicSync,
    Scanning,
    ScanWindow,
    scan_window_at,
)
from bscontroller.linklayer.extended import periodic_time_us
from bscontroller.linklayer.periodic import periodic_receive, periodic_transmit
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
from bscontroller.linklayer.state import DataStatus
from bscontroller.linklayer.timing import (
    LL_CONN_EVENT_GUARD_US,
    LL_DEFAULT_SCAN_INTERVAL_US,
    window_widening,
)
from bscontroller.radio.base import PHY_OCTET_BITS, RADIO_SWITCH_TIME_US, Phy

if TYPE_CHECKING:
    from bscontroller.address import Address
    from bscontroller.linklayer.link import RadioLink
    from bscontroller.linklayer.results import (
        ConnectionAccepted,
        PeriodicPacketReceived,
        SyncInfoReceived,
    )


def connection_window(connection: Connection) -> RadioReservation:
    """Next connection event, from its (widened) start to the guard before the next
    anchor.
    """
    return RadioReservation(
        connection.event_start_us - RADIO_SWITCH_TIME_US,
        max(
            connection.next_exchange_us(),
            (
                connection.parameters.window_size * LL_CONN_INTERVAL_UNIT_US
                if not connection.established
                else 0
            ),
        )
        + LL_CONN_EVENT_GUARD_US
        + RADIO_SWITCH_TIME_US,
        connection.parameters.interval * LL_CONN_INTERVAL_UNIT_US,
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
    widening = window_widening(sync.next_us - sync.last_rx_us, sync.info.sca)
    return RadioReservation(
        sync.next_us - widening - RADIO_SWITCH_TIME_US,
        2 * widening
        + sync.phy.airtime_us(PDU_HEADER_SIZE + PDU_ADV_EXT_PAYLOAD_SIZE_MAX)
        + RADIO_SWITCH_TIME_US,
        sync.info.interval * LL_PERIODIC_INTERVAL_UNIT_US * sync.event_step,
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
        return next(
            handle
            for handle in range(1, LL_CONNECTION_HANDLE_MAX + 1)
            if handle not in self.contexts
        )

    def can_accept(self, peer: Address) -> bool:
        peer = self.link.privacy.identity(peer)
        if peer is None:
            return False
        return len(self.contexts) < self.link.config.max_connections and not any(
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
        item = result.advertiser
        advertising_handle = None
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

    async def run(self, connection: Connection, target_us: int) -> None:
        await connection.advance_to_us(
            target_us, radio_until_us=self.link.resource.deadline_us
        )


class AdvertisingManager:
    def __init__(self, link: RadioLink):
        self.link = link
        self.contexts: dict[int, Advertising] = {}

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
        for item in list(self.contexts.values()):
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
            if item.periodic_enabled:
                operations.append(
                    PendingOperation(
                        periodic_transmit_start_us(item),
                        RadioPriority.PERIODIC_TX,
                        item,
                        partial(periodic_transmit, link, item),
                    )
                )
        return operations

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
                else start + max(entry.interval_us for entry in reservations)
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
        item.periodic_enabled = enable
        item.periodic_next_us = next_us
        item.periodic_counter = 0

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

    def next_context(self) -> Scanning | Initiating | None:
        scans = [item for item in (self.initiating, self.active) if item is not None]
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

    def start(self, sync: PeriodicSync) -> None:
        sync.handle = next(
            handle
            for handle in range(1, self.link.config.max_periodic_syncs + 1)
            if handle not in self.active
        )
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
        return list(self.active.values()) + ([self.pending] if self.pending else [])

    def expire(self) -> None:
        link, sync = self.link, self.pending
        if (
            sync
            and sync.info is not None
            and (
                link.now_us >= sync.deadline_us
                or (sync.counter - sync.info.event_counter) & LL_EVENT_COUNTER_MASK
                >= LL_PERIODIC_SYNC_ESTABLISHMENT_EVENTS
            )
        ):
            self.pending = None
            link.listener.on_sync_established(
                sync, ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR
            )
        for handle, active in list(self.active.items()):
            if link.now_us >= active.deadline_us:
                del self.active[handle]
                link.listener.on_sync_lost(active)

    def prepare(self) -> list[PendingOperation]:
        return [
            PendingOperation(
                periodic_receive_window(sync).start_us,
                RadioPriority.PERIODIC_RX,
                sync,
                partial(periodic_receive, self.link, sync),
            )
            for sync in self.contexts()
            if sync.info is not None
        ]

    def acquired(self, result: SyncInfoReceived) -> None:
        """Commit SyncInfo only to the currently requested acquisition role."""
        sync = self.pending
        if not (sync and sync.info is None):
            return
        link = self.link
        if any(
            active.address == result.address and active.sid == result.sid
            for active in self.active.values()
        ):
            return
        if not (
            (result.address, result.sid) in link.periodic_advertiser_list
            if sync.use_list
            else sync.address == result.address and sync.sid == result.sid
        ):
            return
        info, packet = result.info, result.packet
        sync.address, sync.sid = result.address, result.sid
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

    def received(self, result: PeriodicPacketReceived) -> bool:
        """A validated AUX_SYNC_IND refreshes the role, then publishes success."""
        sync, packet = result.sync, result.packet
        if self.pending is not sync and self.active.get(sync.handle) is not sync:
            return False
        sync.next_us = packet.start_us
        sync.last_rx_us = packet.start_us
        sync.deadline_us = packet.end_us + sync.timeout_us
        sync.receive_channel_map(result.pdu.acad)
        if not sync.established:
            sync.established = True
            if self.pending is sync:
                self.pending = None
            self.active[sync.handle] = sync
            self.link.listener.on_sync_established(sync, ErrorCode.SUCCESS)
        return True

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

    def report(self, sync: PeriodicSync, data, rssi, tx_power, status) -> None:
        # A callback may remove this context before the next fragment arrives.
        if self.active.get(sync.handle) is sync and sync.reports_enabled:
            self.link.listener.on_periodic_report(sync, data, rssi, tx_power, status)

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
        self.connections = ConnectionManager(link)
        self.advertising = AdvertisingManager(link)
        self.scanning = ScanningManager(link)
        self.synchronization = SynchronizationManager(link)

    def reset(self) -> None:
        self.connections.contexts.clear()
        self.advertising.clear()
        self.scanning.enable(None)
        self.scanning.cancel_initiating()
        self.scanning.cursor = 0
        self.synchronization.cancel()
        self.synchronization.active.clear()

    def reservations(self, exclude: object = None) -> list[RadioReservation]:
        return (
            [
                connection_window(item)
                for item in self.connections.contexts.values()
                if item is not exclude
            ]
            + [
                periodic_transmit_window(item)
                for item in self.advertising.contexts.values()
                if item is not exclude and item.periodic_enabled
            ]
            + [
                periodic_receive_window(item)
                for item in self.synchronization.contexts()
                if item is not exclude and item.info is not None
            ]
        )
