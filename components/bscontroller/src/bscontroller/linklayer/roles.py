# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connection and scanning roles, LL handles and shared-radio reservations."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from bscontroller.address import AddressType
from bscontroller.linklayer import (
    LEGACY_ADVERTISING_HANDLE,
    LL_ADV_PRIMARY_CHANNELS,
    LL_CONN_INTERVAL_UNIT_US,
    LL_DATA_CHANNEL_MAP_ALL,
)
from bscontroller.linklayer.advertiser import (
    AdvertisingManager,
)
from bscontroller.linklayer.broadcast import BroadcastManager
from bscontroller.linklayer.cig import CigManager
from bscontroller.linklayer.connection import Connection
from bscontroller.linklayer.discovery import (
    Advertising,
    Initiating,
    ScanBackoff,
    Scanning,
    ScanWindow,
    scan_window_at,
)
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.initiating_radio import pawr_connect
from bscontroller.linklayer.iso import IsoDataPaths
from bscontroller.linklayer.resource import PendingOperation
from bscontroller.linklayer.results import ScanEvent
from bscontroller.linklayer.scanning_radio import receive_scan_event
from bscontroller.linklayer.scheduler import RadioPriority, RadioReservation
from bscontroller.linklayer.state import Role
from bscontroller.linklayer.synchronization import (
    SynchronizationManager,
)
from bscontroller.linklayer.synchronization_radio import pawr_accept
from bscontroller.linklayer.timing import split_fixed_time
from bscontroller.radio.base import RADIO_SWITCH_TIME_US, Phy

if TYPE_CHECKING:
    from collections.abc import Callable

    from bscontroller.address import Address
    from bscontroller.linklayer.link import RadioLink
    from bscontroller.linklayer.results import ConnectionAccepted


def connection_window(connection: Connection) -> RadioReservation:
    """Protect the first packet exchange at each scheduled connection anchor."""
    subrating = connection.controls.subrating
    index = connection.next_scheduled_event
    period = connection.event_interval_fixed_us
    anchor, phase = connection.project_anchor(index - connection.event_index)
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
    own_interval, _ = split_fixed_time(period + phase)
    duration = min(
        duration + connection.event_guard_us, own_interval + anchor_uncertainty
    )
    # During a change or a continuation burst, protect the temporary extra
    # radio slots. The periodic reservation then follows the committed phase.
    schedule = subrating.schedule
    regular = schedule.next_event(index) == index
    if regular and subrating.transition is None and connection.controls.pending is None:
        period *= schedule.factor
    interval, fraction = split_fixed_time(period)
    return RadioReservation(
        start - RADIO_SWITCH_TIME_US,
        duration + RADIO_SWITCH_TIME_US,
        interval,
        interval_fraction=fraction,
        start_fraction=phase,
    )


# Core Vol 4, Part E, 5.3.1 reserves handles above 0x0EFF. Start at one
# to keep this implementation's existing, human-facing handle convention.
LL_CONNECTION_HANDLE_MAX = 0x0EFF


class ConnectionManager:
    def __init__(
        self,
        link: RadioLink,
        *,
        available_handles: Callable[[], Iterator[int]],
        free_handle_count: Callable[[], int],
    ):
        self.link = link
        self._available_handles = available_handles
        self._free_handle_count = free_handle_count
        self.contexts: dict[int, Connection] = {}

    def allocate_handle(self) -> int:
        """Allocate the LL identity; HCI only serializes the assigned handle."""
        return next(self._available_handles())

    def has_capacity(self) -> bool:
        return (
            len(self.contexts) < self.link.config.max_connections
            and self._free_handle_count() > 0
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
        connection.closed = True
        del self.contexts[connection.handle]
        return True

    def reset(self) -> None:
        for connection in self.contexts.values():
            connection.closed = True
        self.contexts.clear()

    def create(
        self,
        result: ConnectionAccepted,
        *,
        advertising_handle: int | None,
        completed_events: int | None,
        pawr_handle: int | None,
    ) -> Connection:
        link = self.link
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
        if advertising_handle not in (None, LEGACY_ADVERTISING_HANDLE):
            connection.advertising_handle = advertising_handle
            connection.advertising_events = completed_events
        connection.pawr_advertising_handle = pawr_handle
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
                partial(connection_window, connection),
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


class ScanningManager:
    def __init__(self, link: RadioLink, scan_disabled: Callable[[], None]):
        self.link = link
        self._scan_disabled = scan_disabled
        self.active: Scanning | None = None
        self.initiating: Initiating | None = None
        self.cursor = 0

    def start(
        self, parameters: Scanning, address: Address, duration_us: int, period_us: int
    ) -> None:
        now = self.link.now_us
        self.active = replace(
            parameters,
            address=address,
            start_us=now,
            backoff=ScanBackoff(),
            duration_us=duration_us,
            period_us=period_us,
            deadline_us=now + duration_us if duration_us else None,
        )

    def stop(self) -> None:
        self.active = None
        self._scan_disabled()

    def reset(self) -> None:
        self.active = None
        self.initiating = None
        self.cursor = 0

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
        if (
            isinstance(scan, Scanning)
            and scan.deadline_us is not None
            and now >= scan.deadline_us
        ):
            if not scan.period_us:
                self.stop()
                link.listener.on_scan_timeout()
                return
            scan.start_us += scan.period_us
            scan.deadline_us = scan.start_us + scan.duration_us
            link.listener.on_scan_period()
            if self.active is not scan:
                return None
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
            if scan.deadline_us is not None:
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


class LinkRoles:
    """Wire the radio roles and commit exchanges that change more than one role."""

    def __init__(self, link: RadioLink):
        self.host_channel_map = LL_DATA_CHANNEL_MAP_ALL
        self._pending_response: ConnectionAccepted | None = None
        self.connections = ConnectionManager(
            link,
            available_handles=self.available_connection_handles,
            free_handle_count=self.free_connection_handle_count,
        )
        self.synchronization = SynchronizationManager(
            link,
            accept_connection=partial(pawr_accept, link),
            observe_big_info=lambda sync, acad, start_us: self.broadcast.observed_info(
                sync, acad, start_us
            ),
        )
        self.scanning = ScanningManager(link, self.synchronization.scan_disabled)
        self.advertising = AdvertisingManager(
            link,
            pending_initiation=self.scanning.pawr_initiating,
            connect=partial(pawr_connect, link),
            channel_map=lambda: self.host_channel_map,
            big_info=lambda handle, start_us: self.broadcast.advertising_info(
                handle, start_us
            ),
        )
        self.iso = IsoDataPaths(link.listener)
        self.broadcast = BroadcastManager(
            link,
            self.iso,
            available_handles=self.available_connection_handles,
            free_handle_count=self.free_connection_handle_count,
            find_advertiser=self.advertising.contexts.get,
            set_big_info=self.advertising.set_big_info,
            find_sync=self.synchronization.active.get,
            channel_map=lambda: self.host_channel_map,
        )
        self.cig = CigManager(
            link.radio,
            link.listener,
            self.iso,
            available_handles=self.available_connection_handles,
            find_connection=self.connections.contexts.get,
            host_supports_cis=lambda: bool(
                link.features & LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM
            ),
            new_access_address=partial(link.new_access_address, minimum_distance=2),
            used_access_addresses=link.used_access_addresses,
        )

    def _used_connection_handles(self) -> set[int]:
        return (
            self.connections.contexts.keys()
            | self.broadcast.connection_handles()
            | self.cig.connection_handles()
            | self.iso.connection_handles()
        )

    def available_connection_handles(self) -> Iterator[int]:
        """Return free LL handles, including reservations for pending ISO links."""
        used = self._used_connection_handles()
        return (
            handle
            for handle in range(1, LL_CONNECTION_HANDLE_MAX + 1)
            if handle not in used
        )

    def free_connection_handle_count(self) -> int:
        return LL_CONNECTION_HANDLE_MAX - len(self._used_connection_handles())

    @contextmanager
    def sending_connect_response(self, result: ConnectionAccepted) -> Iterator[None]:
        """TX completion creates the Peripheral even if the Host has stopped the set."""
        assert result.advertiser is not None
        assert self.advertising.owns(result.advertiser)
        handle = result.advertiser.handle
        self._pending_response = result
        try:
            yield
            if self._pending_response is result:
                self.accepted(result, response_handle=handle)
        finally:
            self._pending_response = None

    def accepted(
        self, result: ConnectionAccepted, *, response_handle: int | None = None
    ) -> Connection | None:
        """Commit a validated connection exchange before publishing its outcome."""
        # An asynchronous RF exchange may have yielded to another allocation.
        if not self.connections.has_capacity():
            return None
        item = result.advertiser
        advertising_handle = response_handle
        completed_events = None
        if result.pawr_sync is not None and (
            self.synchronization.active.get(result.pawr_sync.handle)
            is not result.pawr_sync
        ):
            return None
        if result.pawr_advertiser is not None and (
            not self.advertising.owns(result.pawr_advertiser)
            or not result.pawr_advertiser.periodic_enabled
        ):
            return None
        if item is not None:
            if response_handle is None:
                if not self.advertising.owns(item) or not item.enabled:
                    return None
                advertising_handle = item.handle
                completed_events = item.events
            elif self.advertising.event_is_current(item):
                # Only the original event can terminate its advertising set
                # and report its completed-event count (Vol 4, Part E, 7.7.65.18).
                completed_events = item.events
        if result.initiator is not None:
            # A delayed RF result cannot cancel a newer Host initiation request.
            if self.scanning.initiating is not result.initiator:
                return None
            self.scanning.cancel_initiating()
        if item is not None and completed_events is not None:
            self.advertising.stop(item, event_completed=True)
        return self.connections.create(
            result,
            advertising_handle=advertising_handle,
            completed_events=completed_events,
            pawr_handle=(
                result.pawr_advertiser.handle
                if result.pawr_advertiser is not None
                else None
            ),
        )

    def reset(self) -> None:
        # TX may return after Reset; its old exchange can no longer commit.
        self._pending_response = None
        self.broadcast.reset()
        self.cig.reset()
        self.iso.reset()
        self.connections.reset()
        self.advertising.clear()
        self.host_channel_map = LL_DATA_CHANNEL_MAP_ALL
        self.scanning.reset()
        self.synchronization.reset()

    def reservations(self, exclude: object = None) -> list[RadioReservation]:
        reservations = [
            connection_window(item)
            for item in self.connections.contexts.values()
            if item is not exclude
        ]
        reservations.extend(self.advertising.reservations(exclude))
        reservations.extend(self.synchronization.reservations(exclude))
        reservations.extend(self.broadcast.reservations(exclude))
        reservations.extend(self.cig.reservations(exclude))
        return reservations
