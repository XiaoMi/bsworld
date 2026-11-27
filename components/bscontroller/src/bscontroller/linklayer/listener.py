# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Internal Link Layer notifications, one interface per procedure.

The Link Layer never imports HCI. Procedures pass their committed contexts to
this internal listener. NotificationAdapter copies them into the frozen values
accepted by the upper-layer NotificationSink; RadioLink also supports internal
listeners for qualification and unit tests.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from bscontroller.linklayer.iso import IsoListener
from bscontroller.linklayer.state import AdvertisingReport, DataStatus
from bscontroller.radio.base import Phy

if TYPE_CHECKING:
    from bscontroller.address import Address
    from bscontroller.linklayer.api import BigSnapshot, CisEstablished
    from bscontroller.linklayer.bis import BigInfo
    from bscontroller.linklayer.connection import Connection
    from bscontroller.linklayer.discovery import Advertising, PeriodicSync
    from bscontroller.linklayer.pawr import SlotResponse


class ConnectionListener(Protocol):
    """ACL lifetime and data flow (Core 6.3, Vol 6, Part B, 4.5)."""

    def trace(self, message: str | Callable[[], str]) -> None: ...
    def on_connected(self, connection: Connection) -> None: ...
    def on_disconnected(self, connection: Connection, reason: int) -> None: ...
    def on_acl_received(
        self, connection: Connection, continuation: bool, data: bytes
    ) -> None: ...
    def on_acl_completed(self, connection: Connection, count: int = 1) -> None: ...


class ControlListener(Protocol):
    """LL control procedure completions (Core 6.3, Vol 6, Part B, 5.1)."""

    @property
    def local_version(self) -> tuple[int, int, int]: ...

    def on_connection_updated(self, connection: Connection, status: int) -> None: ...
    def on_connection_rate_changed(
        self,
        connection: Connection,
        status: int,
        interval: int,
        factor: int,
        latency: int,
        continuation: int,
        timeout: int,
    ) -> None: ...
    def on_phy_updated(self, connection: Connection, status: int) -> None: ...
    def on_frame_space_updated(
        self,
        connection: Connection,
        status: int,
        initiator: int,
        space: int,
        phys: int,
        spacing: int,
    ) -> None: ...
    def on_peer_sca(self, connection: Connection, status: int, sca: int) -> None: ...
    def on_subrate_change(
        self,
        connection: Connection,
        status: int,
        factor: int,
        latency: int,
        continuation: int,
        timeout: int,
    ) -> None: ...
    def on_transmit_power_reporting(
        self,
        connection: Connection,
        status: int,
        reason: int,
        phy: int,
        tx_power_level: int,
        flags: int,
        delta: int,
    ) -> None: ...
    def on_path_loss_threshold(
        self, connection: Connection, path_loss: int, zone: int
    ) -> None: ...
    def on_data_length_changed(self, connection: Connection) -> None: ...
    def on_remote_features(self, connection: Connection, status: int) -> None: ...
    def on_remote_features_all(
        self,
        connection: Connection,
        status: int,
        maximum: int,
        valid: int,
        features: bytes,
    ) -> None: ...
    def on_remote_version(self, connection: Connection, status: int) -> None: ...
    def on_remote_parameters(
        self,
        connection: Connection,
        minimum: int,
        maximum: int,
        latency: int,
        timeout: int,
    ) -> None: ...


class EncryptionListener(Protocol):
    """Encryption and authenticated payload timeout (Core 6.3, Vol 6, Part B, 5.1.3 and
    5.4).
    """

    def on_long_term_key_request(
        self, connection: Connection, rand: bytes, ediv: int
    ) -> None: ...
    def on_encryption_changed(
        self, connection: Connection, status: int, refresh: bool
    ) -> None: ...
    def on_authenticated_payload_timeout(self, connection: Connection) -> None: ...


class AdvertisingListener(Protocol):
    """Advertising state notifications (Core 6.3, Vol 6, Part B, 4.4.2)."""

    def on_advertising_ended(self, item: Advertising, status: int) -> None: ...
    def on_scan_request(self, item: Advertising, peer: Address) -> None: ...
    def on_subevent_data_request(
        self, item: Advertising, start: int, count: int
    ) -> None: ...
    def on_response_report(
        self,
        item: Advertising,
        subevent: int,
        transmitted: bool,
        responses: list[SlotResponse],
    ) -> None: ...


class ScanningListener(Protocol):
    """Scanning state reports (Core 6.3, Vol 6, Part B, 4.4.3)."""

    def on_monitored_advertiser(self, address: Address, condition: int) -> None: ...

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
    ) -> None: ...
    def on_scan_timeout(self) -> None: ...
    def on_scan_period(self) -> None: ...
    def on_connect_failed(self, peer: Address, status: int) -> None: ...


class SynchronizationListener(Protocol):
    """Periodic advertising synchronization (Core 6.3, Vol 6, Part B, 4.4.5)."""

    def on_sync_established(self, sync: PeriodicSync, status: int) -> None: ...
    def on_sync_transfer_received(self, sync: PeriodicSync, status: int) -> None: ...
    def on_periodic_report(
        self,
        sync: PeriodicSync,
        data: bytes,
        rssi: float | None,
        tx_power: int | None,
        data_status: int = DataStatus.COMPLETE,
        *,
        counter: int = 0,
        subevent: int | None = None,
    ) -> None: ...
    def on_sync_lost(self, sync: PeriodicSync) -> None: ...


class BroadcastListener(IsoListener, Protocol):
    """BIG lifetime and ISO data (Core Vol 6, Part B, 4.4.6)."""

    def on_big_created(self, group: BigSnapshot, status: int) -> None: ...
    def on_big_terminated(self, handle: int, reason: int) -> None: ...
    def on_big_synchronized(self, group: BigSnapshot, status: int) -> None: ...
    def on_big_lost(self, handle: int, reason: int) -> None: ...
    def on_big_info(self, sync_handle: int, info: BigInfo) -> None: ...


class ConnectedIsoListener(Protocol):
    """CIS lifetime notifications (Core Vol 6, Part B, 5.1.15)."""

    def on_cis_request(
        self, acl_handle: int, cis_handle: int, cig_id: int, cis_id: int
    ) -> bool: ...
    def on_cis_established(self, result: CisEstablished, status: int) -> None: ...
    def on_cis_disconnected(self, cis_handle: int, reason: int) -> None: ...


class Listener(
    ConnectionListener,
    ControlListener,
    EncryptionListener,
    AdvertisingListener,
    ScanningListener,
    SynchronizationListener,
    BroadcastListener,
    ConnectedIsoListener,
    Protocol,
):
    """Internal procedure callbacks, adapted before entering an upper layer."""
