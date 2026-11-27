# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Controller-side LL service boundary, independent of HCI packets and transport.

HCI supplies decoded configuration and converts returned Controller error codes
into responses. This boundary validates configuration and delegates lifetime
changes to LL role owners; RadioLink drives their prepared activities. Queries
return detached values. See Core Vol 1, Part A, 2.1.2.1-2.1.2.3.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from bscontroller.address import OwnAddressType
from bscontroller.capabilities import (
    CONTROLLER_SUPPORTED_PHY_MASK,
)
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LEGACY_ADVERTISING_HANDLE,
    LL_SCA_500_PPM,
    LL_SCA_MAX,
)
from bscontroller.linklayer.advertising_service import AdvertisingService
from bscontroller.linklayer.api import (
    AddressSnapshot,
    ConnectionSnapshot,
    InitiatingConfig,
    PeriodicSyncConfig,
    PeriodicSyncSnapshot,
    ScanBackoffSnapshot,
    ScanningSnapshot,
    ScanWindowConfig,
    SyncTransferConfig,
    connection_snapshot,
    synchronization_snapshot,
)
from bscontroller.linklayer.connection_rate import (
    MIN_RATE_INTERVAL,
    ConnectionRateParameters,
)
from bscontroller.linklayer.discovery import (
    Initiating,
    PeriodicSync,
    Scanning,
    ScanWindow,
)
from bscontroller.linklayer.features import FEATURE_MAX_PAGE, LeFeature, LeFeatureMask
from bscontroller.linklayer.frame_space import FrameSpaceRequest
from bscontroller.linklayer.iso_service import IsoService
from bscontroller.linklayer.power_control import (
    MAX_COMPENSATION,
    POWER_UNAVAILABLE,
    PathLossParameters,
    PowerPhy,
    compensated_power,
)
from bscontroller.linklayer.state import Role
from bscontroller.linklayer.subrating import SubrateRequest
from bscontroller.linklayer.transfer import (
    AdvertisingSource,
    SynchronizationSource,
)
from bscontroller.radio.base import Phy

if TYPE_CHECKING:
    from bscontroller.address import Address
    from bscontroller.linklayer.iso_service import BigCommands, CigCommands
    from bscontroller.linklayer.link import RadioLink

SUCCESS = ErrorCode.SUCCESS
INVALID = ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
DISALLOWED = ErrorCode.COMMAND_DISALLOWED_ERROR
UNKNOWN_ADV = ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR
TOO_LONG = ErrorCode.PACKET_TOO_LONG_ERROR


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
        self.cig: CigCommands = link.roles.cig
        self.big: BigCommands = link.roles.broadcast
        self.iso = IsoService(link)
        self.privacy = link.privacy
        self.decision = link.decision
        self._default_scan = (scan_interval_us, scan_window_us)
        self.link.public_address = AddressSnapshot(address)
        self._reset_configuration()

    def _reset_configuration(self) -> None:
        self._scan_parameters = Scanning(self.public_address, *self._default_scan)
        self.link.default_tx_phys = self.link.default_rx_phys = (
            CONTROLLER_SUPPORTED_PHY_MASK
        )

    @property
    def features(self) -> int:
        return self.link.features

    def set_host_feature(self, bit: int, value: int) -> int:
        """Set a Host-controlled bit (Core Vol 4, Part E, 7.8.115)."""
        controller_features = {
            LeFeature.CONNECTED_ISOCHRONOUS_STREAM: (
                LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_CENTRAL
                | LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_PERIPHERAL
            ),
            LeFeature.CONNECTION_SUBRATING_HOST_SUPPORT: (
                LeFeatureMask.CONNECTION_SUBRATING
            ),
            LeFeature.ADVERTISING_CODING_SELECTION_HOST_SUPPORT: (
                LeFeatureMask.ADVERTISING_CODING_SELECTION
            ),
            LeFeature.CHANNEL_SOUNDING_HOST_SUPPORT: LeFeatureMask.CHANNEL_SOUNDING,
            LeFeature.SHORTER_CONNECTION_INTERVALS_HOST_SUPPORT: (
                LeFeatureMask.SHORTER_CONNECTION_INTERVALS
            ),
        }
        if value not in (0, 1):
            return INVALID
        if bit not in controller_features or (
            value and not self.features & controller_features[bit]
        ):
            return ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR
        if self.link.connections:
            return DISALLOWED
        mask = 1 << bit
        self.link.features = self.link.features & ~mask | value * mask
        return SUCCESS

    @property
    def scan_parameters(self) -> ScanningSnapshot:
        scan = self.link.scan or self._scan_parameters
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
        windows: tuple[ScanWindowConfig, ...] = (),
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
            windows=tuple(
                ScanWindow(item.phy, item.interval_us, item.window_us, item.active)
                for item in windows
            ),
        )
        return SUCCESS

    @property
    def public_address(self) -> Address:
        address = self.link.public_address
        assert address is not None
        return address

    def set_public_address(self, address: Address) -> None:
        snapshot = AddressSnapshot(address)
        self.link.public_address = snapshot
        # A running scan has its own address. Refresh the fallback for its next start.
        scan = self._scan_parameters
        if not OwnAddressType(scan.own_address_type).uses_random_fallback:
            scan.address = scan.identity_address = snapshot

    @property
    def random_address(self) -> Address | None:
        return self.link.random_address

    def set_random_address(self, address: Address) -> int:
        """Apply the global random fallback while discovery roles are idle.

        Core Vol 4, Part E, 7.8.4 excludes legacy advertising, scanning and
        initiation. Extended sets use their separate per-set addresses.
        Refresh configured scanning too: address assignment may follow the scan
        parameter command and must take effect on its next enable.
        """
        legacy = self.link.advertisers.get(LEGACY_ADVERTISING_HANDLE)
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
        self.link.random_address = AddressSnapshot(address)
        return SUCCESS

    def enable_scanning(
        self,
        enable: bool,
        *,
        extended: bool | None = None,
        duration_us: int = 0,
        period_us: int = 0,
    ) -> int:
        scanning = self.link.roles.scanning
        if not enable:
            scanning.stop()
            return SUCCESS
        scan = self._scan_parameters
        if extended is not None and scan.extended != extended:
            # The Host may enable scanning with the default parameters.
            scan = replace(scan, extended=extended)
        if scanning.active is not None and not scan.extended:
            # A repeated legacy enable only updates Filter_Duplicates (7.8.11).
            return SUCCESS
        fallback = scan.identity_address or scan.address
        # 7.8.11/65: own 0x01/0x03 need the random address regardless of
        # the resolving list.
        if not self.privacy.address_available(scan.own_address_type, fallback):
            return INVALID
        address = self.privacy.local_address(scan.own_address_type, fallback)
        scanning.start(scan, address, duration_us, period_us)
        return SUCCESS

    def initiating_status(self, peer: Address, use_list: bool) -> int:
        if not use_list and any(
            connection.peer == peer for connection in self.link.connections.values()
        ):
            return ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR
        if self.link.initiating is not None:
            return DISALLOWED
        if not self.link.roles.connections.has_capacity():
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
            first = windows[0] if windows else ScanWindow(Phy.LE_1M, 0, 0)
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
                pawr_advertising=(
                    self.link.advertisers[config.pawr_advertising_handle]
                    if config.pawr_advertising_handle is not None
                    else None
                ),
                pawr_subevent=config.pawr_subevent,
            )
            self.link.roles.scanning.start_initiating(item)
        return status

    def pawr_initiating_status(self, handle: int, subevent: int, phys: int) -> int:
        """Whether Extended Create Connection [v2] can use this train (Vol 4,
        Part E, 7.8.66): an enabled PAwR set with that subevent, on one of the
        initiating PHYs.
        """
        item = self.link.advertisers.get(handle)
        if item is None:
            return UNKNOWN_ADV
        if item.pawr is None or not item.periodic_enabled:
            return DISALLOWED
        if subevent >= item.pawr.num_subevents or not phys & (
            1 << (item.secondary_phy - 1)
        ):
            return INVALID
        return SUCCESS

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
            skip=config.skip,
            reports_enabled=config.reports_enabled,
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

    def sync_subevents_status(self, handle: int, subevents: tuple[int, ...]) -> int:
        """Whether a synchronized train can listen to these subevents only."""
        sync = self.link.syncs.get(handle)
        if sync is None:
            return UNKNOWN_ADV
        if sync.pawr is None:
            return DISALLOWED
        if len(subevents) > sync.pawr.timing.num_subevents or any(
            subevent >= sync.pawr.timing.num_subevents for subevent in subevents
        ):
            return INVALID
        return SUCCESS

    def set_sync_subevents(
        self, handle: int, subevents: tuple[int, ...], *, include_tx_power: bool
    ) -> int:
        """Synchronize with these PAwR subevents only (Vol 4, Part E, 7.8.127)."""
        status = self.sync_subevents_status(handle, subevents)
        if status == SUCCESS:
            self.link.roles.synchronization.set_subevents(
                self.link.syncs[handle], subevents, include_tx_power
            )
        return status

    def set_response_data(
        self,
        handle: int,
        request_event: int,
        request_subevent: int,
        response_subevent: int,
        slot: int,
        data: bytes,
    ) -> int:
        """Send Host data in one response slot (Vol 4, Part E, 7.8.126)."""
        sync = self.link.syncs.get(handle)
        if sync is None:
            return UNKNOWN_ADV
        return self.link.roles.synchronization.respond(
            sync, request_event, request_subevent, response_subevent, slot, data
        )

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
    def active_scan_extended(self) -> bool | None:
        scan = self.link.scan
        return scan.extended if scan is not None else None

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

    @property
    def host_channel_map(self) -> int:
        return self.link.roles.host_channel_map

    def random_bytes(self, length: int) -> bytes:
        return self.link.random.randbytes(length)

    def default_data_length(self) -> tuple[int, int]:
        return self.link.default_tx_octets, self.link.default_tx_time

    def reset(self) -> None:
        self.link.reset()
        self._reset_configuration()

    def add_monitored_advertiser(
        self, address: Address, low: int, high: int, timeout: int
    ) -> int:
        return self.link.monitoring.add(AddressSnapshot(address), low, high, timeout)

    def remove_monitored_advertiser(self, address: Address) -> int:
        return self.link.monitoring.remove(AddressSnapshot(address))

    def clear_monitored_advertisers(self) -> None:
        self.link.monitoring.clear()

    def enable_advertiser_monitoring(self, enabled: bool) -> None:
        self.link.monitoring.enable(enabled)

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
        self.link.roles.host_channel_map = value
        for connection in self.link.connections.values():
            if connection.role == Role.CENTRAL:
                connection.controls.request_channel_map(value)
        self.link.roles.advertising.update_periodic_channel_maps(value)
        self.link.roles.broadcast.update_channel_map(value)
        return SUCCESS

    def set_default_data_length(self, octets: int, time_us: int) -> None:
        self.link.default_tx_octets, self.link.default_tx_time = octets, time_us

    def _connection(self, handle: int):
        connection = self.link.connections.get(handle)
        return connection if connection and connection.reported else None

    def connection_snapshot(self, handle: int) -> ConnectionSnapshot | None:
        connection = self._connection(handle)
        return connection_snapshot(connection) if connection is not None else None

    def request_peer_sca(self, handle: int) -> int:
        connection = self._connection(handle)
        if connection is None:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        clock = connection.controls.clock_accuracy
        if not clock.peer_supports_update:
            return ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
        if clock.pending or connection.llcp.has_pending('clock_accuracy'):
            return DISALLOWED
        clock.queue_read()
        return SUCCESS

    def modify_sleep_clock_accuracy(self, action: int) -> int:
        if action not in (0, 1):
            return INVALID
        target = LL_SCA_MAX if action == 0 else LL_SCA_500_PPM
        if target == self.link.sleep_clock_accuracy:
            return ErrorCode.LIMIT_REACHED_ERROR
        connections = self.link.connections
        if not self.link.can_change_sleep_clock_accuracy:
            return ErrorCode.CONTROLLER_BUSY_ERROR
        if not connections:
            self.link.sleep_clock_accuracy = target
            return SUCCESS
        connection = next(iter(connections.values()))
        clock = connection.controls.clock_accuracy
        if (
            not connection.established
            or clock.pending
            or connection.llcp.has_pending('clock_accuracy')
        ):
            return ErrorCode.CONTROLLER_BUSY_ERROR
        if action == 1 and not clock.peer_supports_update:
            return DISALLOWED
        clock.queue_change(target)
        return SUCCESS

    def read_rf_path_compensation(self) -> tuple[int, int]:
        return self.link.rf_tx_path_compensation, self.link.rf_rx_path_compensation

    def set_rf_path_compensation(self, tx: int, rx: int) -> int:
        if max(abs(tx), abs(rx)) > MAX_COMPENSATION:
            return INVALID
        self.link.rf_tx_path_compensation = tx
        self.link.rf_rx_path_compensation = rx
        return SUCCESS

    def radiative_transmit_power(self, dbm: int) -> int:
        return self.link.radiative_transmit_power(dbm)

    def advertising_transmit_power(self, requested_dbm: int) -> int:
        # Round the output down so the selected radiated level stays within the limit.
        output = (10 * requested_dbm - self.link.rf_tx_path_compensation) // 10
        return compensated_power(output, 0)

    def reported_rssi(self, dbm: float) -> int:
        return self.link.reported_rssi(dbm)

    def read_local_transmit_power(self, handle: int, phy: int) -> tuple[int, int, int]:
        connection = self._connection(handle)
        if connection is None:
            return (
                ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR,
                POWER_UNAVAILABLE,
                POWER_UNAVAILABLE,
            )
        if not PowerPhy.LE_1M <= phy <= PowerPhy.LE_CODED_S2:
            return (
                ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
                POWER_UNAVAILABLE,
                POWER_UNAVAILABLE,
            )
        return SUCCESS, *connection.controls.power.read_local(PowerPhy(phy))

    def read_remote_transmit_power(self, handle: int, phy: int) -> int:
        connection = self._connection(handle)
        if connection is None:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        if not PowerPhy.LE_1M <= phy <= PowerPhy.LE_CODED_S2:
            return ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR
        connection.controls.power.queue_read(PowerPhy(phy))
        return SUCCESS

    def set_path_loss_parameters(
        self,
        handle: int,
        high: int,
        high_hysteresis: int,
        low: int,
        low_hysteresis: int,
        minimum_events: int,
    ) -> int:
        connection = self._connection(handle)
        if connection is None:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        parameters = PathLossParameters(
            high, high_hysteresis, low, low_hysteresis, minimum_events
        )
        if not parameters.valid:
            return INVALID
        connection.controls.power.path_loss.configure(parameters)
        return SUCCESS

    def set_path_loss_enabled(self, handle: int, enabled: int) -> int:
        connection = self._connection(handle)
        if connection is None:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        if enabled not in (0, 1):
            return INVALID
        monitoring = connection.controls.power.path_loss
        if monitoring.parameters is None:
            return DISALLOWED
        monitoring.set_enabled(bool(enabled))
        return SUCCESS

    def set_transmit_power_reporting(self, handle: int, local: int, remote: int) -> int:
        connection = self._connection(handle)
        if connection is None:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        if local not in (0, 1) or remote not in (0, 1):
            return INVALID
        power = connection.controls.power
        power.report_local, power.report_remote = bool(local), bool(remote)
        return SUCCESS

    def set_default_subrate(
        self, minimum: int, maximum: int, latency: int, continuation: int, timeout: int
    ) -> int:
        request = SubrateRequest(minimum, maximum, latency, continuation, timeout)
        if not request.valid_host_parameters:
            return INVALID
        self.link.default_subrate = request
        return SUCCESS

    def request_subrate(
        self,
        handle: int,
        minimum: int,
        maximum: int,
        latency: int,
        continuation: int,
        timeout: int,
    ) -> int:
        connection = self._connection(handle)
        if connection is None:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        request = SubrateRequest(minimum, maximum, latency, continuation, timeout)
        if not request.valid_host_parameters or not request.fits_connection(
            connection.parameters.interval_us
        ):
            return INVALID
        return connection.llcp.request_subrate(request)

    def set_default_rate(self, parameters: ConnectionRateParameters) -> int:
        if not parameters.valid:
            return INVALID
        if parameters.connection_interval_max < MIN_RATE_INTERVAL:
            return ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR
        self.link.default_rate = parameters
        return SUCCESS

    def request_connection_rate(
        self, handle: int, parameters: ConnectionRateParameters
    ) -> int:
        connection = self._connection(handle)
        if connection is None or not connection.established:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        return connection.llcp.request_connection_rate(parameters)

    def request_frame_space(
        self, handle: int, minimum: int, maximum: int, phys: int, spacing: int
    ) -> int:
        connection = self._connection(handle)
        if connection is None:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        return connection.llcp.request_frame_space(
            FrameSpaceRequest(minimum, maximum, int(phys), int(spacing))
        )

    def enable_encryption(self, handle: int, rand: bytes, ediv: int, key: bytes) -> int:
        connection = self._connection(handle)
        return (
            connection.llcp.enable_encryption(rand, ediv, key)
            if connection
            else ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        )

    def read_remote_features(self, handle: int, pages: int | None = None) -> int:
        if pages is not None:
            if not 0 <= pages <= FEATURE_MAX_PAGE or not 0 <= handle <= 0x0EFF:
                return ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
            if any(
                item.llcp.all_features_pending
                for item in self.link.connections.values()
            ):
                return DISALLOWED
        connection = self._connection(handle)
        return (
            connection.llcp.read_remote_features(pages)
            if connection
            else ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        )

    def read_remote_version(self, handle: int) -> int:
        connection = self._connection(handle)
        return (
            connection.llcp.read_remote_version()
            if connection
            else ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        )

    def request_phy(self, handle: int, tx: int, rx: int, coding: int) -> int:
        connection = self._connection(handle)
        return (
            connection.llcp.request_phy(tx, rx, coding)
            if connection
            else ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        )

    def request_connection_update(
        self,
        handle: int,
        minimum: int,
        maximum: int,
        latency: int,
        timeout: int,
        *,
        min_ce_length: int = 0,
        max_ce_length: int = 0,
    ) -> int:
        connection = self._connection(handle)
        if connection is None:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        if connection.llcp.parameter_update_pending:
            return DISALLOWED
        if min_ce_length > max_ce_length:
            return INVALID
        return connection.llcp.request_connection_update(
            minimum, maximum, latency, timeout
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

    def authenticated_timeout(self, handle: int) -> tuple[int, int]:
        connection = self._connection(handle)
        if connection is not None:
            return SUCCESS, connection.encryption.auth_timeout_us
        if self.link.roles.iso.contains(handle):
            return DISALLOWED, 0
        return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR, 0

    def set_authenticated_timeout(self, handle: int, timeout_us: int) -> int:
        if self.link.roles.iso.contains(handle):
            return DISALLOWED
        connection = self._connection(handle)
        if connection is None:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        if not timeout_us:
            return INVALID
        if timeout_us < connection.listening_interval_us:
            return DISALLOWED
        connection.encryption.set_authenticated_timeout(timeout_us)
        return SUCCESS

    def disconnect(self, handle: int, reason: int) -> int:
        connection = self._connection(handle)
        if connection is None:
            return self.link.roles.cig.disconnect(handle, reason)
        connection.terminate(reason)
        return SUCCESS

    def acl_flush_timeout(self, handle: int) -> tuple[int, int]:
        connection = self._connection(handle)
        if connection is None:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR, 0
        return SUCCESS, connection.event.acl_flush_timeout_us

    def set_acl_flush_timeout(self, handle: int, timeout_us: int) -> int:
        connection = self._connection(handle)
        if connection is None:
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        connection.event.acl_flush_timeout_us = timeout_us
        return SUCCESS

    def send_acl(
        self, handle: int, continuation: bool, data: bytes, *, flushable: bool = False
    ) -> None:
        connection = self._connection(handle)
        if connection is not None:
            connection.enqueue_acl(continuation, data, flushable=flushable)
