# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connectionless LE contexts, scan windows/backoff and periodic channel maps.

The Controller configures these records; RadioLink owns and executes active
contexts. No context performs RF I/O or owns an event loop.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace

from bscontroller.address import Address, OwnAddressType
from bscontroller.linklayer import (
    LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
    LL_CHANNEL_MAP_SIZE,
    LL_EVENT_COUNTER_MASK,
    LL_SCAN_BACKOFF_COUNT_MAX,
    LL_SCAN_BACKOFF_COUNT_MIN,
    LL_SCAN_BACKOFF_OUTCOME_COUNT,
    LL_SCAN_BACKOFF_SCALE,
    LL_SYNC_CTE_TYPE_ABSENT,
    PDU_ADV_CTE_TYPE_SHIFT,
    PDU_CONTROL_INSTANT_SIZE,
    AdvertisingMode,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising import SyncInfo
from bscontroller.linklayer.parameters import Parameters, valid_channel_map
from bscontroller.linklayer.timing import (
    LL_DEFAULT_ADV_INTERVAL_US,
    LL_INSTANT_PAST_THRESHOLD,
)
from bscontroller.radio.base import Phy

# Bluetooth Assigned Numbers, Common Data Types: Channel Map Update Indication,
# carried in periodic advertising ACAD (Core Vol 6, Part B, 2.3.4.8).
AD_TYPE_CHANNEL_MAP_UPDATE_INDICATION = 0x28


@dataclass
class Advertising:
    address: Address
    random_address: Address | None = None
    interval_us: int = LL_DEFAULT_ADV_INTERVAL_US
    interval_max_us: int = LL_DEFAULT_ADV_INTERVAL_US
    channel_map: int = LL_ADV_PRIMARY_CHANNEL_MAP_ALL
    pdu_type: int = AdvertisingPduType.ADV_NONCONN_IND
    data: bytearray = field(default_factory=bytearray)
    scan_response: bytearray = field(default_factory=bytearray)
    enabled: bool = False
    next_us: int = 0
    extended: bool = False
    sid: int = 0
    did: int = 0
    periodic_interval: int = 0
    periodic_data: bytearray = field(default_factory=bytearray)
    periodic_enabled: bool = False
    periodic_next_us: int = 0
    periodic_counter: int = 0
    # Assigned when periodic advertising is enabled, not while configuring a set.
    periodic_aa: int = 0
    periodic_crc: int = 0
    # Periodic data has its own Advertising DID (Core Vol 6, Part B, 4.4.2.11).
    # AUX_SYNC_IND carries it in an ADI field only while the Host enabled one.
    periodic_did: int = 0
    periodic_adi: bool = False
    # paEventCounter of the last AUX_SYNC_IND actually transmitted, if any.
    # A sync transfer names it as lastPaEventCounter (Core Vol 6, Part B, 2.4.2.27).
    periodic_last_counter: int | None = None
    events: int = 0
    target: Address | None = None
    high_duty: bool = False
    deadline_us: int = 0
    max_events: int = 0
    primary_phy: Phy = Phy.LE_1M
    secondary_phy: Phy = Phy.LE_1M
    mode: int = AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
    notify_scan_request: bool = False
    filter_policy: int = 0
    anonymous: bool = False
    tx_power_dbm: int = 0
    include_tx_power: bool = False
    own_address_type: int = OwnAddressType.PUBLIC
    identity_address: Address | None = None
    local_rpa: Address | None = None
    identity_target: Address | None = None
    privacy_peer: Address | None = None
    decision: bool = False
    decision_address: bool = False
    decision_adi: bool = False
    decision_flags: int = 0
    decision_data: bytes = b''

    def __post_init__(self) -> None:
        # Public HCI records and RF procedures share mutable advertising buffers.
        if not isinstance(self.data, bytearray):
            self.data = bytearray(self.data)
        if not isinstance(self.scan_response, bytearray):
            self.scan_response = bytearray(self.scan_response)
        if not isinstance(self.periodic_data, bytearray):
            self.periodic_data = bytearray(self.periodic_data)


@dataclass
class ScanWindow:
    phy: Phy
    interval_us: int
    window_us: int
    active: bool = False


def scan_window_at(
    windows: tuple[ScanWindow, ...], elapsed_us: int
) -> tuple[ScanWindow | None, int, int]:
    """Phase PHY windows while preserving each PHY's own interval and channel cycle."""
    phase = 0
    active = []
    boundaries = []
    round_us = sum(window.window_us for window in windows)
    for index, window in enumerate(windows):
        if elapsed_us < phase:
            boundaries.append(phase)
        else:
            # Overcommitted scan windows share a round, without starving a PHY.
            interval = max(window.interval_us, round_us)
            cycle, offset = divmod(elapsed_us - phase, interval)
            start = phase + cycle * interval
            if offset < window.window_us:
                active.append((start, -index, window, cycle))
                boundaries.append(start + window.window_us)
            else:
                boundaries.append(start + interval)
        phase += window.window_us
    if active:
        _, _, window, cycle = max(active, key=lambda entry: entry[:2])
        return window, cycle, min(boundaries)
    return None, 0, min(boundaries)


@dataclass
class ScanBackoff:
    count: int = LL_SCAN_BACKOFF_COUNT_MIN
    upper_limit: int = LL_SCAN_BACKOFF_COUNT_MIN
    successes: int = 0
    failures: int = 0

    def permit(self) -> bool:
        """Decrement backoffCount; a SCAN_REQ may be sent only when it reaches zero
        (Core Vol 6, Part B, 4.4.3.2).
        """
        self.count = max(0, self.count - 1)
        return self.count == 0

    def complete(self, success: bool, generator: random.Random) -> None:
        """Core Vol 6, Part B, 4.4.3.2: two consecutive outcomes halve or double
        upperLimit.
        """
        self.successes = self.successes + 1 if success else 0
        self.failures = self.failures + 1 if not success else 0
        if self.successes == LL_SCAN_BACKOFF_OUTCOME_COUNT:
            self.upper_limit = max(
                LL_SCAN_BACKOFF_COUNT_MIN, self.upper_limit // LL_SCAN_BACKOFF_SCALE
            )
            self.successes = 0
        elif self.failures == LL_SCAN_BACKOFF_OUTCOME_COUNT:
            self.upper_limit = min(
                LL_SCAN_BACKOFF_COUNT_MAX, self.upper_limit * LL_SCAN_BACKOFF_SCALE
            )
            self.failures = 0
        self.count = generator.randint(LL_SCAN_BACKOFF_COUNT_MIN, self.upper_limit)


@dataclass
class Scanning:
    address: Address
    interval_us: int
    window_us: int
    active: bool = False
    start_us: int = 0
    windows: tuple[ScanWindow, ...] = ()
    duration_us: int = 0
    period_us: int = 0
    deadline_us: int = 0
    filter_policy: int = 0
    backoff: ScanBackoff = field(default_factory=ScanBackoff)
    own_address_type: int = OwnAddressType.PUBLIC
    identity_address: Address | None = None
    extended: bool = False


@dataclass
class Initiating:
    address: Address
    peer: Address
    interval_us: int
    window_us: int
    parameters: Parameters
    start_us: int
    windows: tuple[ScanWindow, ...] = ()
    phy_parameters: dict[Phy, Parameters] | None = None
    filter_policy: int = 0
    own_address_type: int = OwnAddressType.PUBLIC
    identity_address: Address | None = None
    local_rpa: Address | None = None
    backoff: ScanBackoff = field(default_factory=ScanBackoff)


@dataclass(frozen=True)
class TransferOrigin:
    """The connection and peer Service_Data that delivered a sync (Core Vol 6, Part
    B, 5.1.13; Vol 4, Part E, 7.7.65.24).
    """

    connection_handle: int
    service_data: int


@dataclass
class PeriodicSync:
    address: Address
    sid: int
    timeout_us: int
    deadline_us: int
    skip: int = 0
    reports_enabled: bool = True
    info: SyncInfo | None = None
    next_us: int = 0
    counter: int = 0
    last_rx_us: int = 0
    established: bool = False
    phy: Phy = Phy.LE_1M
    use_list: bool = False
    handle: int = 0
    channel_map_update: tuple[int, int] | None = None
    # Duplicate filtering compares the ADI of the previous report that carried
    # one, or None if that report was truncated (Core Vol 6, Part B, 4.4.5.1.1).
    filter_duplicates: bool = False
    reported_adi: int | None = None
    # paEventCounter of the AUX_SYNC_IND received at last_rx_us.
    last_counter: int = 0
    # The AdvA that described this train on air: an RPA stays unresolved here,
    # unlike ``address`` (Core Vol 6, Part B, 2.4.2.27).
    advertiser_address: Address | None = None
    # Set while synchronizing from LL_PERIODIC_SYNC_IND, and kept for its event.
    transfer: TransferOrigin | None = None
    # Sender clock drift since syncConnEventCount, widening only the first
    # receive windows (Core Vol 6, Part B, 5.1.13.1).
    transfer_widening_us: int = 0
    # CTE_Type: CTEs, or their absence, that fail synchronization at the first
    # AUX_SYNC_IND; ignored once synchronized (Core Vol 6, Part B, 4.4.3.4).
    cte_type: int = 0

    def excludes(self, cte_info: int | None) -> bool:
        """CTE_Type excludes a packet with this CTEInfo, or without one."""
        kind = (
            LL_SYNC_CTE_TYPE_ABSENT
            if cte_info is None
            else 1 << (cte_info >> PDU_ADV_CTE_TYPE_SHIFT)
        )
        return bool(self.cte_type & kind)

    def receive_channel_map(self, acad: bytes) -> None:
        """Keep the last valid future ChM/Instant update; malformed AD structures end
        parsing.
        """
        structures = []
        offset = 0
        while offset < len(acad):
            length = acad[offset]
            end = offset + length + 1
            if length == 0 or end > len(acad):
                return
            if acad[offset + 1] == AD_TYPE_CHANNEL_MAP_UPDATE_INDICATION:
                structures.append(acad[offset + 2 : end])
            offset = end
        for value in structures:
            if len(value) != LL_CHANNEL_MAP_SIZE + PDU_CONTROL_INSTANT_SIZE:
                continue
            channels = int.from_bytes(value[:LL_CHANNEL_MAP_SIZE], 'little')
            instant = int.from_bytes(value[LL_CHANNEL_MAP_SIZE:], 'little')
            if not valid_channel_map(channels):
                continue
            if (
                instant - self.counter
            ) & LL_EVENT_COUNTER_MASK >= LL_INSTANT_PAST_THRESHOLD:
                continue
            self.channel_map_update = (instant, channels)

    def apply_channel_map(self) -> None:
        """Switch to the ACAD channel map once the event counter reaches its Instant
        (Core Vol 6, Part B, 2.3.4.8).
        """
        if self.channel_map_update is None:
            return
        instant, channels = self.channel_map_update
        if (self.counter - instant) & LL_EVENT_COUNTER_MASK < LL_INSTANT_PAST_THRESHOLD:
            assert self.info is not None
            self.info = replace(self.info, channel_map=channels)
            self.channel_map_update = None

    @property
    def event_step(self) -> int:
        return self.skip + 1 if self.established else 1
