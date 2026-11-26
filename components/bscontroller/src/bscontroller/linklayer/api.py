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
from bscontroller.linklayer.advertising import AdvertisingCoding
from bscontroller.linklayer.bis import BigInfo
from bscontroller.linklayer.data_length import DataLengths
from bscontroller.linklayer.parameters import Parameters
from bscontroller.linklayer.pawr import PawrTiming
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
    primary_coding: AdvertisingCoding = AdvertisingCoding.NO_PREFERENCE
    secondary_coding: AdvertisingCoding = AdvertisingCoding.NO_PREFERENCE
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
    reported: bool
    advertising_handle: int | None
    advertising_events: int | None
    csa2: bool
    peer_rpa: AddressSnapshot | None = None
    local_rpa: AddressSnapshot | None = None
    pawr_advertising_handle: int | None = None
    pawr_sync_handle: int | None = None


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
        connection.reported,
        connection.advertising_handle,
        connection.advertising_events,
        connection.csa2,
        address_snapshot(connection.peer_rpa),
        address_snapshot(connection.local_rpa),
        connection.pawr_advertising_handle,
        connection.pawr_sync_handle,
    )


@dataclass(frozen=True)
class PeriodicSyncSnapshot:
    handle: int
    address: AddressSnapshot
    sid: int
    phy: Phy
    interval: int
    sca: int
    # The delivering connection and Service_Data of a transferred sync.
    connection_handle: int = 0
    service_data: int = 0
    # PAwR timing; None for a train without responses.
    pawr: PawrTiming | None = None


@dataclass(frozen=True)
class SyncTransferConfig:
    """How a connection handles received sync transfers (Core Vol 4, Part E,
    7.8.91 and 7.8.92); disabled ignores them. ``cte_type`` holds the CTE_Type
    bits that synchronization must not accept.
    """

    enabled: bool = False
    reports_enabled: bool = False
    filter_duplicates: bool = False
    skip: int = 0
    timeout_us: int = 0
    cte_type: int = 0


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
    # Connect on a local PAwR train's subevent (Vol 4, Part E, 7.8.66 [v2]).
    pawr_advertising_handle: int | None = None
    pawr_subevent: int = 0

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
        sync.transfer.connection_handle if sync.transfer else 0,
        sync.transfer.service_data if sync.transfer else 0,
        sync.pawr.timing if sync.pawr else None,
    )


@dataclass(frozen=True)
class BigTestParameters:
    iso_interval: int
    nse: int
    max_pdu: int
    bn: int
    irc: int
    pto: int


@dataclass(frozen=True)
class BigConfig:
    handle: int
    advertising_handle: int
    num_bis: int
    sdu_interval_us: int
    max_sdu: int
    phy: int
    packing: int
    framing: int
    encryption: int
    broadcast_code: bytes
    max_transport_latency_ms: int = 0
    rtn: int = 0
    test: BigTestParameters | None = None


@dataclass(frozen=True)
class BigSyncConfig:
    handle: int
    sync_handle: int
    encryption: int
    broadcast_code: bytes
    mse: int
    timeout: int
    bis: tuple[int, ...]


@dataclass(frozen=True)
class BigSnapshot:
    handle: int
    info: BigInfo | None
    stream_handles: tuple[int, ...]


@dataclass(frozen=True)
class CisConfig:
    cis_id: int
    max_sdu_c_to_p: int
    max_sdu_p_to_c: int
    phy_c_to_p: int
    phy_p_to_c: int
    rtn_c_to_p: int = 0
    rtn_p_to_c: int = 0
    nse: int | None = None
    max_pdu_c_to_p: int = 0
    max_pdu_p_to_c: int = 0
    bn_c_to_p: int = 0
    bn_p_to_c: int = 0


@dataclass(frozen=True)
class CigConfig:
    cig_id: int
    sdu_interval_c_to_p: int
    sdu_interval_p_to_c: int
    worst_case_sca: int
    packing: int
    framing: int
    max_transport_latency_c_to_p: int
    max_transport_latency_p_to_c: int
    cis_parameters: tuple[CisConfig, ...]
    iso_interval: int | None = None
    ft_c_to_p: int = 1
    ft_p_to_c: int = 1


@dataclass(frozen=True)
class CisEstablished:
    connection_handle: int
    cig_sync_delay: int = 0
    cis_sync_delay: int = 0
    transport_latency_c_to_p: int = 0
    transport_latency_p_to_c: int = 0
    phy_c_to_p: int = 0
    phy_p_to_c: int = 0
    nse: int = 0
    bn_c_to_p: int = 0
    bn_p_to_c: int = 0
    ft_c_to_p: int = 0
    ft_p_to_c: int = 0
    max_pdu_c_to_p: int = 0
    max_pdu_p_to_c: int = 0
    iso_interval: int = 0
    sub_interval: int = 0
    max_sdu_c_to_p: int = 0
    max_sdu_p_to_c: int = 0
    sdu_interval_c_to_p: int = 0
    sdu_interval_p_to_c: int = 0
    framing: int = 0
