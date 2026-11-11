# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Copy internal LL notifications into detached upper-layer reports.

The LL-facing listener receives committed contexts. Only this adapter reads
their internals; the HCI-facing sink receives frozen values, never ownership of
a connection, advertiser, or synchronization context (Core Vol 1, Part A, 2.1.2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from bscontroller.linklayer.api import (
    AddressSnapshot,
    AdvertisingSnapshot,
    ConnectionSnapshot,
    PeriodicSyncSnapshot,
    address_snapshot,
    advertising_snapshot,
    connection_snapshot,
    synchronization_snapshot,
)
from bscontroller.linklayer.state import AdvertisingReport, DataStatus
from bscontroller.radio.base import Phy

if TYPE_CHECKING:
    from bscontroller.address import Address
    from bscontroller.linklayer.connection import Connection
    from bscontroller.linklayer.discovery import Advertising, PeriodicSync
    from bscontroller.linklayer.link import RadioLink


class NotificationSink(Protocol):
    @property
    def local_version(self) -> tuple[int, int, int]: ...

    def trace(self, message: str) -> None: ...
    def on_connected(self, connection: ConnectionSnapshot) -> None: ...
    def on_disconnected(self, connection: ConnectionSnapshot, reason: int) -> None: ...
    def on_acl_received(
        self, connection: ConnectionSnapshot, continuation: bool, data: bytes
    ) -> None: ...
    def on_acl_completed(self, connection: ConnectionSnapshot) -> None: ...
    def on_connection_updated(
        self, connection: ConnectionSnapshot, status: int
    ) -> None: ...
    def on_phy_updated(self, connection: ConnectionSnapshot, status: int) -> None: ...
    def on_data_length_changed(self, connection: ConnectionSnapshot) -> None: ...
    def on_remote_features(
        self, connection: ConnectionSnapshot, status: int
    ) -> None: ...
    def on_remote_version(
        self, connection: ConnectionSnapshot, status: int
    ) -> None: ...
    def on_remote_parameters(
        self,
        connection: ConnectionSnapshot,
        minimum: int,
        maximum: int,
        latency: int,
        timeout: int,
    ) -> None: ...
    def on_long_term_key_request(
        self, connection: ConnectionSnapshot, rand: bytes, ediv: int
    ) -> None: ...
    def on_encryption_changed(
        self, connection: ConnectionSnapshot, status: int, refresh: bool
    ) -> None: ...
    def on_authenticated_payload_timeout(
        self, connection: ConnectionSnapshot
    ) -> None: ...
    def on_advertising_ended(self, item: AdvertisingSnapshot, status: int) -> None: ...
    def on_scan_request(
        self, item: AdvertisingSnapshot, peer: AddressSnapshot
    ) -> None: ...
    def on_advertisement(
        self,
        address: AddressSnapshot,
        data: bytes,
        rssi: float,
        report: AdvertisingReport,
        sid: int | None,
        interval: int,
        tx_power: int | None,
        *,
        target: AddressSnapshot | None = None,
        primary_phy: int = Phy.LE_1M,
        secondary_phy: int = Phy.LE_1M,
    ) -> None: ...
    def on_scan_timeout(self) -> None: ...
    def on_scan_period(self) -> None: ...
    def on_sync_established(self, sync: PeriodicSyncSnapshot, status: int) -> None: ...
    def on_sync_transfer_received(
        self, sync: PeriodicSyncSnapshot, status: int
    ) -> None: ...
    def on_periodic_report(
        self,
        sync: PeriodicSyncSnapshot,
        data: bytes,
        rssi: float,
        tx_power: int | None,
        data_status: int = DataStatus.COMPLETE,
    ) -> None: ...
    def on_sync_lost(self, sync: PeriodicSyncSnapshot) -> None: ...


class NotificationAdapter:
    def __init__(self, sink: NotificationSink):
        self.sink = sink
        self._link: RadioLink | None = None

    def bind(self, link: RadioLink) -> None:
        self._link = link

    @property
    def local_version(self) -> tuple[int, int, int]:
        return self.sink.local_version

    def trace(self, message: str) -> None:
        self.sink.trace(message)

    def on_connected(self, connection: Connection) -> None:
        self.sink.on_connected(connection_snapshot(connection))

    def on_disconnected(self, connection: Connection, reason: int) -> None:
        self.sink.on_disconnected(connection_snapshot(connection), reason)

    def on_acl_received(
        self, connection: Connection, continuation: bool, data: bytes
    ) -> None:
        self.sink.on_acl_received(connection_snapshot(connection), continuation, data)

    def on_acl_completed(self, connection: Connection) -> None:
        self.sink.on_acl_completed(connection_snapshot(connection))

    def on_connection_updated(self, connection: Connection, status: int) -> None:
        self.sink.on_connection_updated(connection_snapshot(connection), status)

    def on_phy_updated(self, connection: Connection, status: int) -> None:
        self.sink.on_phy_updated(connection_snapshot(connection), status)

    def on_data_length_changed(self, connection: Connection) -> None:
        self.sink.on_data_length_changed(connection_snapshot(connection))

    def on_remote_features(self, connection: Connection, status: int) -> None:
        self.sink.on_remote_features(connection_snapshot(connection), status)

    def on_remote_version(self, connection: Connection, status: int) -> None:
        self.sink.on_remote_version(connection_snapshot(connection), status)

    def on_remote_parameters(
        self,
        connection: Connection,
        minimum: int,
        maximum: int,
        latency: int,
        timeout: int,
    ) -> None:
        self.sink.on_remote_parameters(
            connection_snapshot(connection), minimum, maximum, latency, timeout
        )

    def on_long_term_key_request(
        self, connection: Connection, rand: bytes, ediv: int
    ) -> None:
        self.sink.on_long_term_key_request(connection_snapshot(connection), rand, ediv)

    def on_encryption_changed(
        self, connection: Connection, status: int, refresh: bool
    ) -> None:
        self.sink.on_encryption_changed(
            connection_snapshot(connection), status, refresh
        )

    def on_authenticated_payload_timeout(self, connection: Connection) -> None:
        self.sink.on_authenticated_payload_timeout(connection_snapshot(connection))

    def _advertiser(self, item: Advertising) -> AdvertisingSnapshot:
        assert self._link is not None
        handle = next(
            key for key, value in self._link.advertisers.items() if value is item
        )
        return advertising_snapshot(handle, item)

    def on_advertising_ended(self, item: Advertising, status: int) -> None:
        self.sink.on_advertising_ended(self._advertiser(item), status)

    def on_scan_request(self, item: Advertising, peer: Address) -> None:
        self.sink.on_scan_request(self._advertiser(item), AddressSnapshot(peer))

    def on_advertisement(
        self,
        address: Address,
        data: bytes,
        rssi: float,
        report: AdvertisingReport,
        sid: int | None,
        interval: int,
        tx_power: int | None,
        *,
        target: Address | None = None,
        primary_phy: int = Phy.LE_1M,
        secondary_phy: int = Phy.LE_1M,
    ) -> None:
        self.sink.on_advertisement(
            AddressSnapshot(address),
            bytes(data),
            rssi,
            report,
            sid,
            interval,
            tx_power,
            target=address_snapshot(target),
            primary_phy=primary_phy,
            secondary_phy=secondary_phy,
        )

    def on_scan_timeout(self) -> None:
        self.sink.on_scan_timeout()

    def on_scan_period(self) -> None:
        self.sink.on_scan_period()

    def on_sync_established(self, sync: PeriodicSync, status: int) -> None:
        self.sink.on_sync_established(synchronization_snapshot(sync), status)

    def on_sync_transfer_received(self, sync: PeriodicSync, status: int) -> None:
        self.sink.on_sync_transfer_received(synchronization_snapshot(sync), status)

    def on_periodic_report(
        self,
        sync: PeriodicSync,
        data: bytes,
        rssi: float,
        tx_power: int | None,
        data_status: int = DataStatus.COMPLETE,
    ) -> None:
        self.sink.on_periodic_report(
            synchronization_snapshot(sync), bytes(data), rssi, tx_power, data_status
        )

    def on_sync_lost(self, sync: PeriodicSync) -> None:
        self.sink.on_sync_lost(synchronization_snapshot(sync))
