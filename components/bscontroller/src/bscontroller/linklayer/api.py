# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Detached configuration and reports at the upper Link Layer boundary.

Core Vol 1, Part A, 2.1.2 separates HCI from Link Layer control. Configuration
contains no radio deadlines or procedure objects; notifications copy committed
values so an upper layer cannot mutate an active LL context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bscontroller.address import Address, AddressType, OwnAddressType
from bscontroller.linklayer import (
    LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
    AdvertisingMode,
    AdvertisingPduType,
)
from bscontroller.linklayer.data_length import DataLengths
from bscontroller.linklayer.parameters import Parameters
from bscontroller.linklayer.timing import LL_DEFAULT_ADV_INTERVAL_US
from bscontroller.radio.base import Phy

if TYPE_CHECKING:
    from bscontroller.linklayer.connection import Connection
    from bscontroller.linklayer.discovery import Advertising, PeriodicSync


@dataclass(frozen=True, eq=False, init=False)
class AddressSnapshot(Address):
    """An Address-compatible value with no shared mutable address fields."""

    address_bytes: bytes
    address_type: AddressType

    def __init__(self, address: Address):
        object.__setattr__(self, 'address_bytes', bytes(address))
        object.__setattr__(self, 'address_type', address.address_type)


def address_snapshot(address: Address | None) -> AddressSnapshot | None:
    return AddressSnapshot(address) if address is not None else None


@dataclass(frozen=True)
class AdvertisingConfig:
    """Decoded advertising parameters, excluding the active event lifecycle."""

    address: Address
    interval_us: int = LL_DEFAULT_ADV_INTERVAL_US
    interval_max_us: int = LL_DEFAULT_ADV_INTERVAL_US
    channel_map: int = LL_ADV_PRIMARY_CHANNEL_MAP_ALL
    pdu_type: int = AdvertisingPduType.ADV_NONCONN_IND
    target: Address | None = None
    high_duty: bool = False
    extended: bool = False
    sid: int = 0
    primary_phy: Phy = Phy.LE_1M
    secondary_phy: Phy = Phy.LE_1M
    mode: int = AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
    notify_scan_request: bool = False
    filter_policy: int = 0
    anonymous: bool = False
    tx_power_dbm: int = 0
    include_tx_power: bool = False
    own_address_type: int = OwnAddressType.PUBLIC
    privacy_peer: Address | None = None
    decision: bool = False
    decision_address: bool = False
    decision_adi: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, 'address', AddressSnapshot(self.address))
        object.__setattr__(self, 'target', address_snapshot(self.target))
        object.__setattr__(self, 'privacy_peer', address_snapshot(self.privacy_peer))


@dataclass(frozen=True)
class AdvertisingSnapshot:
    handle: int
    address: AddressSnapshot
    random_address: AddressSnapshot | None
    data: bytes
    scan_response: bytes
    periodic_data: bytes
    enabled: bool
    extended: bool
    mode: int
    anonymous: bool
    target: AddressSnapshot | None
    periodic_interval: int
    periodic_enabled: bool
    tx_power_dbm: int
    events: int


def advertising_snapshot(handle: int, item: Advertising) -> AdvertisingSnapshot:
    return AdvertisingSnapshot(
        handle,
        AddressSnapshot(item.address),
        address_snapshot(item.random_address),
        bytes(item.data),
        bytes(item.scan_response),
        bytes(item.periodic_data),
        item.enabled,
        item.extended,
        item.mode,
        item.anonymous,
        address_snapshot(item.target),
        item.periodic_interval,
        item.periodic_enabled,
        item.tx_power_dbm,
        item.events,
    )


@dataclass(frozen=True)
class ConnectionSnapshot:
    handle: int
    role: int
    peer: AddressSnapshot
    self_address: AddressSnapshot | None
    parameters: Parameters
    tx_phy: Phy
    rx_phy: Phy
    rssi: int | None
    peer_features: int | None
    peer_version: tuple[int, int, int] | None
    data_lengths: DataLengths
    auth_timeout_us: int
    reported: bool
    advertising_handle: int | None
    advertising_events: int
    csa2: bool
    peer_rpa: AddressSnapshot | None = None
    local_rpa: AddressSnapshot | None = None


def connection_snapshot(connection: Connection) -> ConnectionSnapshot:
    return ConnectionSnapshot(
        connection.handle,
        connection.role,
        AddressSnapshot(connection.peer),
        address_snapshot(connection.self_address),
        connection.parameters,
        connection.tx_phy,
        connection.rx_phy,
        connection.rssi,
        connection.peer_features,
        connection.controls.peer_version,
        connection.controls.data_length.effective(),
        connection.encryption.auth_timeout_us,
        connection.reported,
        connection.advertising_handle,
        connection.advertising_events,
        connection.csa2,
        address_snapshot(connection.peer_rpa),
        address_snapshot(connection.local_rpa),
    )


@dataclass(frozen=True)
class PeriodicSyncSnapshot:
    handle: int
    address: AddressSnapshot
    sid: int
    phy: Phy
    interval: int
    sca: int


@dataclass(frozen=True)
class ScanWindowConfig:
    phy: Phy
    interval_us: int
    window_us: int
    active: bool = False


@dataclass(frozen=True)
class ScanBackoffSnapshot:
    count: int
    upper_limit: int
    successes: int
    failures: int


@dataclass(frozen=True)
class ScanningSnapshot:
    address: AddressSnapshot
    interval_us: int
    window_us: int
    active: bool
    start_us: int
    windows: tuple[ScanWindowConfig, ...]
    duration_us: int
    period_us: int
    deadline_us: int
    filter_policy: int
    backoff: ScanBackoffSnapshot
    own_address_type: int = OwnAddressType.PUBLIC
    extended: bool = False


@dataclass(frozen=True)
class InitiatingConfig:
    address: Address
    peer: Address
    windows: tuple[ScanWindowConfig, ...]
    phy_parameters: tuple[tuple[Phy, Parameters], ...]
    filter_policy: int = 0
    own_address_type: int = OwnAddressType.PUBLIC

    def __post_init__(self) -> None:
        object.__setattr__(self, 'address', AddressSnapshot(self.address))
        object.__setattr__(self, 'peer', AddressSnapshot(self.peer))


@dataclass(frozen=True)
class PeriodicSyncConfig:
    address: Address
    sid: int
    timeout_us: int
    skip: int = 0
    reports_enabled: bool = True
    use_list: bool = False
    filter_duplicates: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, 'address', AddressSnapshot(self.address))


def synchronization_snapshot(sync: PeriodicSync) -> PeriodicSyncSnapshot:
    return PeriodicSyncSnapshot(
        sync.handle,
        AddressSnapshot(sync.address),
        sync.sid,
        sync.phy,
        sync.info.interval if sync.info else 0,
        sync.info.sca if sync.info else 0,
    )
