# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Controller-side LL service boundary, independent of HCI packets and transport.

HCI supplies decoded configuration and converts returned Controller error codes
into responses. This boundary validates configuration and delegates lifetime
changes to LL role owners; RadioLink drives their prepared activities. Queries
return detached values. See Core Vol 1, Part A, 2.1.2.1-2.1.2.3.
"""

from __future__ import annotations

from dataclasses import fields, replace
from typing import TYPE_CHECKING

from bscontroller.address import OwnAddressType
from bscontroller.capabilities import (
    CONTROLLER_SUPPORTED_PHY_MASK,
)
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_ADV_DIRECT_HIGH_DUTY_TIMEOUT_US,
    LL_DATA_CHANNEL_MAP_ALL,
    LL_MAFS_US,
    LL_PERIODIC_INTERVAL_UNIT_US,
    PDU_ADV_ADI_DID_MASK,
    PDU_ADV_DECISION_DATA_SIZE_MAX,
    PDU_ADV_DECISION_RESOLVABLE_TAG_MASK,
    PDU_ADV_DECISION_RESOLVABLE_TAG_SIZE,
    PDU_ADV_LEGACY_DATA_SIZE_MAX,
    AdvertisingMode,
)
from bscontroller.linklayer.api import (
    AddressSnapshot,
    AdvertisingConfig,
    AdvertisingSnapshot,
    ConnectionSnapshot,
    InitiatingConfig,
    PeriodicSyncConfig,
    PeriodicSyncSnapshot,
    ScanBackoffSnapshot,
    ScanningSnapshot,
    ScanWindowConfig,
    SyncTransferConfig,
    advertising_snapshot,
    connection_snapshot,
    synchronization_snapshot,
)
from bscontroller.linklayer.discovery import (
    Advertising,
    Initiating,
    PeriodicSync,
    ScanBackoff,
    Scanning,
    ScanWindow,
)
from bscontroller.linklayer.extended import (
    advertising_time_us,
    auxiliary_pdu,
    periodic_time_us,
)
from bscontroller.linklayer.llcp import PreparedProcedure
from bscontroller.linklayer.state import Role
from bscontroller.linklayer.transfer import (
    AdvertisingSource,
    SynchronizationSource,
)

if TYPE_CHECKING:
    from bscontroller.address import Address
    from bscontroller.linklayer.link import RadioLink

SUCCESS = ErrorCode.SUCCESS
INVALID = ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
DISALLOWED = ErrorCode.COMMAND_DISALLOWED_ERROR
UNKNOWN_ADV = ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR
TOO_LONG = ErrorCode.PACKET_TOO_LONG_ERROR


class AdvertisingService:
    """Configure and publish advertisers; HCI fragment staging remains above this
    API.
    """

    def __init__(self, link: RadioLink):
        self.link = link

    def snapshot(self, handle: int) -> AdvertisingSnapshot | None:
        item = self.link.advertisers.get(handle)
        return advertising_snapshot(handle, item) if item is not None else None

    def any_enabled(self) -> bool:
        return any(item.enabled for item in self.link.advertisers.values())

    def set_decision_data(self, handle: int, flags: int, data: bytes) -> int:
        """Commit decision bytes independently of AdvData (Vol 4, Part E, 7.8.144)."""
        item = self.link.advertisers.get(handle)
        if item is None:
            return UNKNOWN_ADV
        if len(data) > PDU_ADV_DECISION_DATA_SIZE_MAX or (
            flags & PDU_ADV_DECISION_RESOLVABLE_TAG_MASK
            and len(data) < PDU_ADV_DECISION_RESOLVABLE_TAG_SIZE
        ):
            return INVALID
        item.decision_flags, item.decision_data = flags, bytes(data)
        return SUCCESS

    def has_extended_sets(self) -> bool:
        return any(handle != -1 for handle in self.link.advertisers)

    def _candidate(self, handle: int, config: AdvertisingConfig) -> Advertising:
        if not isinstance(config, AdvertisingConfig):
            raise TypeError('advertising configuration must not contain live state')
        original = self.link.advertisers.get(handle)
        values = {field.name: getattr(config, field.name) for field in fields(config)}
        if original is None:
            return Advertising(
                **values, identity_address=config.address, identity_target=config.target
            )
        if original.random_address is not None and config.address.is_random:
            values['address'] = original.random_address
        # Keep counters, deadlines, periodic identity and payloads in LL. Upper
        # layers replace only configuration, never a running context.
        return replace(
            original,
            **values,
            data=bytes(original.data),
            scan_response=bytes(original.scan_response),
            periodic_data=bytes(original.periodic_data),
            identity_address=values['address'],
            identity_target=config.target,
        )

    def configure_legacy(self, config: AdvertisingConfig) -> None:
        candidate = self._candidate(-1, config)
        current = self.link.advertisers.get(-1)
        if current is None:
            self.link.roles.advertising.publish(-1, candidate)
        else:
            # Data commands may run during a radio await. Keep the context held
            # by that event executor; only LL applies the decoded configuration.
            for field in fields(config):
                setattr(current, field.name, getattr(candidate, field.name))
            current.identity_address = candidate.identity_address
            current.identity_target = candidate.identity_target

    def configuration_status(self, handle: int, incompatible_periodic: bool) -> int:
        item = self.link.advertisers.get(handle)
        if item and item.periodic_enabled and incompatible_periodic:
            return INVALID
        if item and (item.enabled or item.periodic_enabled):
            return DISALLOWED
        if (
            item is None
            and len(self.link.advertisers) >= self.link.config.advertising_set_count
        ):
            return ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR
        return SUCCESS

    def configure(self, handle: int, config: AdvertisingConfig) -> int:
        item = self._candidate(handle, config)
        status = self.configuration_status(
            handle, not item.extended or bool(item.mode) or item.anonymous
        )
        if status == SUCCESS:
            status = self.data_status(item)
        if status == SUCCESS:
            self.link.roles.advertising.publish(handle, item)
        return status

    @staticmethod
    def data_status(item: Advertising) -> int:
        """Validate payload placement and complete-event airtime before a configuration
        commit.

        Only stored advertising data can make an event type invalid (Vol 4, Part E,
        7.8.53). Scan response data kept from an earlier scannable configuration is
        not an error; a non-scannable set simply never transmits it.
        """
        if (
            (
                not item.extended
                and (
                    len(item.data) > PDU_ADV_LEGACY_DATA_SIZE_MAX
                    or len(item.scan_response) > PDU_ADV_LEGACY_DATA_SIZE_MAX
                )
            )
            or (item.target is not None and not item.extended and item.data)
            or (item.extended and item.mode == AdvertisingMode.SCANNABLE and item.data)
        ):
            return INVALID
        if (
            item.extended
            and item.mode == AdvertisingMode.CONNECTABLE
            and len(item.data) > auxiliary_pdu(item).data_capacity()
        ) or advertising_time_us(item) > item.interval_max_us:
            return TOO_LONG
        return SUCCESS

    def set_random_address(self, handle: int, address: Address) -> int:
        item = self.link.advertisers.get(handle)
        if item is None:
            return UNKNOWN_ADV
        if item.enabled:
            return DISALLOWED
        address = AddressSnapshot(address)
        item.random_address = address
        if OwnAddressType(item.own_address_type).uses_random_fallback:
            item.identity_address = address
            item.address = address
        return SUCCESS

    def store_data(
        self, handle: int, field: str, data: bytes, *, renew: bool = False
    ) -> None:
        """Commit only the selected payload; a completed HCI sequence renews its
        identity once.
        """
        if field not in ('data', 'scan_response', 'periodic_data'):
            raise ValueError('unknown advertising payload')
        item = self.link.advertisers[handle]
        setattr(item, field, bytearray(data))
        if renew:
            self.renew_data_identity(handle, field)

    def renew_data_identity(self, handle: int, field: str) -> None:
        """Advance the DID of the payload the Host just provided again.

        The advertising set and its periodic train have separate DIDs (Core Vol 6,
        Part B, 4.4.2.11). The periodic DID takes a new random value that differs
        from the last one.
        """
        item = self.link.advertisers[handle]
        if field == 'periodic_data':
            value = self.link.random.randrange(PDU_ADV_ADI_DID_MASK)
            item.periodic_did = value + (value >= item.periodic_did)
            return
        # Preserve existing DID policy here; it is independent of HCI assembly.
        # TODO: random advertising-set DIDs (Core Vol 6, Part B, 4.4.2.11).
        item.did = (item.did + 1) & PDU_ADV_ADI_DID_MASK

    def validate_data(self, handle: int, field: str, value: bytes) -> int:
        item = self.link.advertisers[handle]
        updated = replace(item, **{field: value})
        if (
            (
                item.extended
                and item.mode == AdvertisingMode.CONNECTABLE
                and field == 'data'
                and len(value) > auxiliary_pdu(item).data_capacity()
            )
            or (
                field == 'periodic_data'
                and item.periodic_interval
                and periodic_time_us(updated) + LL_MAFS_US
                >= item.periodic_interval * LL_PERIODIC_INTERVAL_UNIT_US
            )
            or advertising_time_us(updated)
            > (item.interval_us if item.enabled else item.interval_max_us)
        ):
            return TOO_LONG
        return SUCCESS

    def publication_status(self, handle: int, field: str, value: bytes) -> int:
        item = self.link.advertisers[handle]
        candidate = replace(item, **{field: value})
        if (
            item.periodic_enabled
            and advertising_time_us(candidate)
            + periodic_time_us(candidate)
            + LL_MAFS_US
            >= item.periodic_interval * LL_PERIODIC_INTERVAL_UNIT_US
        ):
            return DISALLOWED
        return SUCCESS

    def enable_legacy(self, enable: bool) -> int:
        item = self.link.advertisers[-1]
        if enable and not self._address_available(item):
            return INVALID
        item.enabled = enable
        item.next_us = self.link.now_us + 1
        item.events = 0
        item.deadline_us = (
            self.link.now_us + LL_ADV_DIRECT_HIGH_DUTY_TIMEOUT_US
            if item.high_duty
            else 0
        )
        return SUCCESS

    def _address_available(self, item: Advertising) -> bool:
        # Enable validates all sets atomically. Only the RF event owner selects
        # an RPA, so re-enabling cannot change an in-flight request's address.
        return item.anonymous or self.link.privacy.address_available(
            item.own_address_type,
            item.identity_address or item.address,
            item.privacy_peer,
        )

    def enable(
        self,
        enable: bool,
        sets: tuple[tuple[int, int, int], ...],
        *,
        interval_unit_us: int,
    ) -> int:
        """Preflight the entire request before committing any set (Vol 4, Part E,
        7.8.56).
        """
        handles = [handle for handle, _, _ in sets]
        if any(handle not in self.link.advertisers for handle in handles):
            return UNKNOWN_ADV
        if enable:
            for handle in handles:
                item = self.link.advertisers[handle]
                if not self._address_available(item):
                    return INVALID
                if (
                    item.extended
                    and item.mode == AdvertisingMode.SCANNABLE
                    and not item.scan_response
                ):
                    return DISALLOWED
                if advertising_time_us(item) > item.interval_max_us:
                    return TOO_LONG
        selected = sets or tuple((handle, 0, 0) for handle in self.link.advertisers)
        for handle, duration, max_events in selected:
            item = self.link.advertisers[handle]
            if enable:
                airtime = advertising_time_us(item)
                item.interval_us = max(
                    item.interval_us,
                    (airtime + interval_unit_us - 1)
                    // interval_unit_us
                    * interval_unit_us,
                )
            item.enabled = enable
            item.next_us = self.link.now_us + 1
            item.events = 0
            item.max_events = max_events
            if item.high_duty:
                # TODO: reject invalid high-duty Duration instead of clamping (7.8.56).
                duration = min(
                    duration or LL_ADV_DIRECT_HIGH_DUTY_TIMEOUT_US,
                    LL_ADV_DIRECT_HIGH_DUTY_TIMEOUT_US,
                )
            item.deadline_us = self.link.now_us + duration if duration else 0
        return SUCCESS

    def remove(self, handle: int) -> int:
        item = self.link.advertisers.get(handle)
        if item is None:
            return UNKNOWN_ADV
        if item.enabled or item.periodic_enabled:
            return DISALLOWED
        self.link.roles.advertising.remove(handle)
        return SUCCESS

    def clear(self) -> int:
        if any(
            item.enabled or item.periodic_enabled
            for item in self.link.advertisers.values()
        ):
            return DISALLOWED
        self.link.roles.advertising.clear()
        return SUCCESS

    def set_periodic_interval(self, handle: int, minimum: int, maximum: int) -> int:
        item = self.link.advertisers[handle]
        if item.periodic_enabled:
            return DISALLOWED
        if not item.extended or item.mode or item.anonymous:
            return INVALID
        if (
            periodic_time_us(item) + LL_MAFS_US
            >= maximum * LL_PERIODIC_INTERVAL_UNIT_US
        ):
            return TOO_LONG
        if not item.periodic_interval:
            # The first periodic configuration seeds a random DID (4.4.2.11).
            item.periodic_did = self.link.random.randrange(PDU_ADV_ADI_DID_MASK + 1)
        item.periodic_interval = minimum
        return SUCCESS

    def enable_periodic(
        self, handle: int, enable: bool, *, include_adi: bool = False
    ) -> int:
        """Enable or disable the train, checking airtime with the requested ADI.

        The ADI adds two octets to AUX_SYNC_IND and to every AUX_CHAIN_IND, so
        the interval checks use the train as it would be transmitted. Disabling
        ignores ``include_adi`` (Core Vol 4, Part E, 7.8.63).
        """
        item = self.link.advertisers[handle]
        if (
            not item.extended
            or not item.periodic_interval
            or item.mode
            or item.anonymous
        ):
            return DISALLOWED
        candidate = replace(item, periodic_adi=enable and include_adi)
        if (
            enable
            and periodic_time_us(candidate) + LL_MAFS_US
            >= item.periodic_interval * LL_PERIODIC_INTERVAL_UNIT_US
        ):
            return TOO_LONG
        if (
            enable
            and advertising_time_us(replace(candidate, periodic_enabled=True))
            + periodic_time_us(candidate)
            + LL_MAFS_US
            >= item.periodic_interval * LL_PERIODIC_INTERVAL_UNIT_US
        ):
            return DISALLOWED
        # Re-enabling a running train only changes whether it carries the ADI.
        item.periodic_adi = candidate.periodic_adi
        self.link.roles.advertising.set_periodic_enabled(item, enable)
        return SUCCESS


class LinkLayerService:
    """LL control operations; live state is committed here before upper-layer
    notification.
    """

    def __init__(
        self,
        link: RadioLink,
        address: Address,
        scan_interval_us: int,
        scan_window_us: int,
    ):
        self.link = link
        self.advertising = AdvertisingService(link)
        self.privacy = link.privacy
        self.decision = link.decision
        self._default_scan = (scan_interval_us, scan_window_us)
        self.reset_configuration(address)

    def reset_configuration(self, address: Address) -> None:
        self._scan_parameters = Scanning(AddressSnapshot(address), *self._default_scan)
        self.host_channel_map = LL_DATA_CHANNEL_MAP_ALL
        self.link.default_tx_phys = self.link.default_rx_phys = (
            CONTROLLER_SUPPORTED_PHY_MASK
        )

    @property
    def scan_parameters(self) -> ScanningSnapshot:
        scan = self._scan_parameters
        return ScanningSnapshot(
            AddressSnapshot(scan.address),
            scan.interval_us,
            scan.window_us,
            scan.active,
            scan.start_us,
            tuple(
                ScanWindowConfig(
                    window.phy, window.interval_us, window.window_us, window.active
                )
                for window in scan.windows
            ),
            scan.duration_us,
            scan.period_us,
            scan.deadline_us,
            scan.filter_policy,
            ScanBackoffSnapshot(
                scan.backoff.count,
                scan.backoff.upper_limit,
                scan.backoff.successes,
                scan.backoff.failures,
            ),
            scan.own_address_type,
            scan.extended,
        )

    def configure_scanning(
        self,
        address: Address,
        interval_us: int,
        window_us: int,
        active: bool,
        policy: int,
        *,
        own_address_type: int = OwnAddressType.PUBLIC,
        extended: bool = False,
    ) -> int:
        if self.link.scan is not None:
            return DISALLOWED
        self._scan_parameters = Scanning(
            AddressSnapshot(address),
            interval_us,
            window_us,
            active,
            filter_policy=policy,
            own_address_type=own_address_type,
            identity_address=AddressSnapshot(address),
            extended=extended,
        )
        return SUCCESS

    def set_random_address(self, address: Address) -> int:
        """Apply the global random fallback while discovery roles are idle.

        Core Vol 4, Part E, 7.8.4 excludes legacy advertising, scanning and
        initiation. Extended sets use their separate per-set addresses.
        Refresh configured scanning too: address assignment may follow the scan
        parameter command and must take effect on its next enable.
        """
        legacy = self.link.advertisers.get(-1)
        if (
            legacy is not None
            and legacy.enabled
            or self.link.scan is not None
            or self.link.initiating is not None
        ):
            return DISALLOWED
        if OwnAddressType(self._scan_parameters.own_address_type).uses_random_fallback:
            self._scan_parameters.identity_address = AddressSnapshot(address)
            self._scan_parameters.address = AddressSnapshot(address)
        return SUCCESS

    def configure_scan_windows(self, windows: tuple[ScanWindowConfig, ...]) -> None:
        self._scan_parameters.windows = tuple(
            ScanWindow(item.phy, item.interval_us, item.window_us, item.active)
            for item in windows
        )

    def enable_scanning(self, enable: bool) -> int:
        scan = self._scan_parameters
        if enable:
            fallback = scan.identity_address or scan.address
            # 7.8.11/65: own 0x01/0x03 need the random address regardless of
            # the resolving list.
            if not self.privacy.address_available(scan.own_address_type, fallback):
                return INVALID
            scan.address = self.privacy.local_address(scan.own_address_type, fallback)
        scan.start_us = self.link.now_us
        scan.backoff = ScanBackoff()
        self.link.roles.scanning.enable(scan if enable else None)
        return SUCCESS

    def set_scan_period(self, duration_us: int, period_us: int) -> None:
        scan = self._scan_parameters
        scan.duration_us, scan.period_us = duration_us, period_us
        scan.deadline_us = self.link.now_us + duration_us if duration_us else 0

    def initiating_status(self, peer: Address, use_list: bool) -> int:
        if not use_list and any(
            connection.peer == peer for connection in self.link.connections.values()
        ):
            return ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR
        if self.link.initiating is not None:
            return DISALLOWED
        if len(self.link.connections) >= self.link.config.max_connections:
            return ErrorCode.CONNECTION_LIMIT_EXCEEDED_ERROR
        return SUCCESS

    def start_initiating(self, config: InitiatingConfig) -> int:
        status = self.initiating_status(config.peer, bool(config.filter_policy))
        if status == SUCCESS:
            address = config.address
            # 7.8.12/66: only policy 0x00 has a peer whose resolving-list entry
            # can replace an unset random fallback.
            if not self.privacy.address_available(
                config.own_address_type,
                address,
                config.peer if not config.filter_policy else None,
            ):
                return INVALID
            windows = tuple(
                ScanWindow(item.phy, item.interval_us, item.window_us, item.active)
                for item in config.windows
            )
            parameters = dict(config.phy_parameters)
            first = windows[0]
            item = Initiating(
                address,
                config.peer,
                first.interval_us,
                first.window_us,
                next(iter(parameters.values())),
                self.link.now_us,
                windows=windows,
                phy_parameters=parameters,
                filter_policy=config.filter_policy,
                own_address_type=config.own_address_type,
                identity_address=config.address,
            )
            self.link.roles.scanning.start_initiating(item)
        return status

    def cancel_initiating(self) -> Address | None:
        item = self.link.roles.scanning.cancel_initiating()
        return AddressSnapshot(item.peer) if item is not None else None

    def synchronization_status(self, address: Address, sid: int, use_list: bool) -> int:
        if self.link.sync is not None:
            return DISALLOWED
        synchronization = self.link.roles.synchronization
        trains = (*self.link.syncs.values(), *synchronization.transfers.values())
        if not use_list and any(
            train.address == address and train.sid == sid for train in trains
        ):
            return ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR
        if len(trains) >= self.link.config.max_periodic_syncs:
            return ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR
        return SUCCESS

    def start_synchronizing(self, config: PeriodicSyncConfig) -> int:
        status = self.synchronization_status(
            config.address, config.sid, config.use_list
        )
        if status != SUCCESS:
            return status
        sync = PeriodicSync(
            config.address,
            config.sid,
            config.timeout_us,
            0,
            config.skip,
            config.reports_enabled,
            use_list=config.use_list,
            filter_duplicates=config.filter_duplicates,
        )
        self.link.roles.synchronization.start(sync)
        return SUCCESS

    def cancel_synchronizing(self) -> PeriodicSyncSnapshot | None:
        sync = self.link.sync
        if sync is None or sync.established:
            return None
        self.link.roles.synchronization.cancel()
        return synchronization_snapshot(sync)

    def terminate_synchronization(self, handle: int) -> int:
        if handle not in self.link.syncs:
            return UNKNOWN_ADV
        self.link.roles.synchronization.terminate(handle)
        return SUCCESS

    def configure_periodic_reports(
        self, handle: int, *, enabled: bool, filter_duplicates: bool
    ) -> int:
        if handle not in self.link.syncs:
            return UNKNOWN_ADV
        self.link.roles.synchronization.configure_reports(
            handle, enabled=enabled, filter_duplicates=filter_duplicates
        )
        return SUCCESS

    def transfer_synchronization(
        self, handle: int, service_data: int, sync_handle: int
    ) -> int:
        """Send a synchronized train's SyncInfo (Vol 4, Part E, 7.8.89)."""
        sync = self.link.syncs.get(sync_handle)
        if sync is None:
            return UNKNOWN_ADV
        connection = self._connection(handle)
        if connection is None:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        return connection.llcp.transfer(
            SynchronizationSource(self.link.syncs, sync), service_data
        )

    def transfer_advertising(
        self, handle: int, service_data: int, advertising_handle: int
    ) -> int:
        """Send a local set's current periodic SyncInfo (Vol 4, Part E, 7.8.90)."""
        item = self.link.advertisers.get(advertising_handle)
        if item is None or not item.extended:
            return UNKNOWN_ADV
        if not item.periodic_enabled:
            return DISALLOWED
        connection = self._connection(handle)
        if connection is None:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        return connection.llcp.transfer(
            AdvertisingSource(item, item.periodic_aa), service_data
        )

    def configure_sync_transfer(self, handle: int, config: SyncTransferConfig) -> int:
        """Set a connection's transfer mode; already received information keeps
        the mode it was received with (Vol 4, Part E, 7.8.91).
        """
        connection = self._connection(handle)
        if connection is None:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        connection.sync_transfer = config
        return SUCCESS

    def configure_default_sync_transfer(self, config: SyncTransferConfig) -> None:
        """Set the transfer mode of later connections only (Vol 4, Part E, 7.8.92)."""
        self.link.sync_transfer = config

    @property
    def scanning_enabled(self) -> bool:
        return self.link.scan is not None

    @property
    def initiating_enabled(self) -> bool:
        return self.link.initiating is not None

    @property
    def synchronizing(self) -> bool:
        return self.link.sync is not None

    def filter_in_use(self) -> bool:
        return (
            any(
                item.enabled and item.filter_policy
                for item in self.link.advertisers.values()
            )
            or bool(self.link.scan and self.link.scan.filter_policy)
            or bool(self.link.initiating and self.link.initiating.filter_policy)
        )

    @property
    def now_us(self) -> int:
        return self.link.now_us

    def random_bytes(self, length: int) -> bytes:
        return self.link.random.randbytes(length)

    def default_data_length(self) -> tuple[int, int]:
        return self.link.default_tx_octets, self.link.default_tx_time

    def reset(self, address: Address) -> None:
        self.link.reset()
        self.reset_configuration(address)

    def edit_filter(self, address: Address | None, add: bool, capacity: int) -> int:
        if self.filter_in_use():
            return DISALLOWED
        items = self.link.accept_list
        if address is None:
            items.clear()
        elif add:
            if address not in items and len(items) >= capacity:
                return ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR
            items.add(AddressSnapshot(address))
        else:
            items.discard(address)
        return SUCCESS

    def edit_periodic_list(
        self, key: tuple[Address, int] | None, add: bool, capacity: int
    ) -> int:
        if self.link.sync is not None:
            return DISALLOWED
        items = self.link.periodic_advertiser_list
        if key is None:
            items.clear()
        elif add:
            if key in items:
                return INVALID
            if len(items) >= capacity:
                return ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR
            items.add((AddressSnapshot(key[0]), key[1]))
        else:
            if key not in items:
                return UNKNOWN_ADV
            items.remove(key)
        return SUCCESS

    def set_default_phys(self, tx: int, rx: int) -> None:
        self.link.default_tx_phys, self.link.default_rx_phys = tx, rx

    def classify_channels(self, value: int) -> int:
        centrals = [
            connection
            for connection in self.link.connections.values()
            if connection.role == Role.CENTRAL
        ]
        if any(connection.controls.busy for connection in centrals):
            return DISALLOWED
        for connection in centrals:
            connection.controls.request_channel_map(value)
        self.host_channel_map = value
        return SUCCESS

    def set_default_data_length(self, octets: int, time_us: int) -> None:
        self.link.default_tx_octets, self.link.default_tx_time = octets, time_us

    def _connection(self, handle: int):
        connection = self.link.connections.get(handle)
        return connection if connection and connection.reported else None

    def connection_snapshot(self, handle: int) -> ConnectionSnapshot | None:
        connection = self._connection(handle)
        return connection_snapshot(connection) if connection is not None else None

    def prepare_encryption(
        self, handle: int, rand: bytes, ediv: int, key: bytes
    ) -> PreparedProcedure:
        connection = self._connection(handle)
        return (
            connection.llcp.prepare_encryption(rand, ediv, key)
            if connection
            else PreparedProcedure(ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR)
        )

    def prepare_features(self, handle: int) -> PreparedProcedure:
        connection = self._connection(handle)
        return (
            connection.llcp.prepare_features()
            if connection
            else PreparedProcedure(ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR)
        )

    def prepare_version(self, handle: int) -> PreparedProcedure:
        connection = self._connection(handle)
        return (
            connection.llcp.prepare_version()
            if connection
            else PreparedProcedure(ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR)
        )

    def prepare_phy(
        self, handle: int, tx: int, rx: int, coding: int, parameter_status: int
    ) -> PreparedProcedure:
        connection = self._connection(handle)
        return (
            connection.llcp.prepare_phy(tx, rx, coding, parameter_status)
            if connection
            else PreparedProcedure(ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR)
        )

    def prepare_parameters(
        self,
        handle: int,
        minimum: int,
        maximum: int,
        latency: int,
        timeout: int,
        *,
        valid_ce_lengths: bool,
    ) -> PreparedProcedure:
        connection = self._connection(handle)
        return (
            connection.llcp.prepare_parameters(
                minimum, maximum, latency, timeout, valid_ce_lengths=valid_ce_lengths
            )
            if connection
            else PreparedProcedure(ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR)
        )

    def supply_key(self, handle: int, key: bytes | None) -> int:
        connection = self._connection(handle)
        return (
            connection.llcp.supply_key(key)
            if connection
            else ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        )

    def reply_parameters(
        self, handle: int, minimum: int, maximum: int, latency: int, timeout: int
    ) -> int:
        connection = self._connection(handle)
        return (
            connection.llcp.reply_parameters(minimum, maximum, latency, timeout)
            if connection
            else ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        )

    def reject_parameters(self, handle: int, reason: int) -> int:
        connection = self._connection(handle)
        return (
            connection.llcp.reject_parameters(reason)
            if connection
            else ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        )

    def set_data_length(self, handle: int, octets: int, time_us: int) -> int:
        connection = self._connection(handle)
        return (
            connection.llcp.set_data_length(octets, time_us)
            if connection
            else ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        )

    def set_authenticated_timeout(self, handle: int, timeout_us: int) -> int:
        connection = self._connection(handle)
        if connection is None:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        if not timeout_us:
            return INVALID
        connection.encryption.auth_timeout_us = timeout_us
        return SUCCESS

    def disconnect(self, handle: int, reason: int) -> int:
        connection = self._connection(handle)
        if connection is None:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        connection.terminate(reason)
        return SUCCESS

    def send_acl(self, handle: int, continuation: bool, data: bytes) -> None:
        connection = self._connection(handle)
        if connection is not None:
            connection.enqueue_acl(continuation, data)
