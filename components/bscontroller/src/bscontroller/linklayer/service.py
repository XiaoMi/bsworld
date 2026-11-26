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
    LL_MAFS_US,
    LL_PERIODIC_INTERVAL_UNIT_US,
    LL_SCA_500_PPM,
    LL_SCA_MAX,
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
from bscontroller.linklayer.connection_rate import (
    MIN_RATE_INTERVAL,
    ConnectionRateParameters,
)
from bscontroller.linklayer.discovery import (
    Advertising,
    Initiating,
    PeriodicSync,
    Scanning,
    ScanWindow,
)
from bscontroller.linklayer.extended import (
    advertising_time_us,
    auxiliary_pdu,
    periodic_time_us,
    required_periodic_interval,
    subevent_pdu,
)
from bscontroller.linklayer.features import FEATURE_MAX_PAGE, LeFeature, LeFeatureMask
from bscontroller.linklayer.frame_space import FrameSpaceRequest
from bscontroller.linklayer.iso_service import IsoService
from bscontroller.linklayer.llcp import PreparedProcedure
from bscontroller.linklayer.pawr import (
    LL_PAWR_SUBEVENT_INTERVAL_MAX,
    PawrTiming,
    SubeventData,
    fits,
    indication_limit_us,
)
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
        current = self.link.advertisers.get(-1)
        if current is not None and current.enabled:
            return
        candidate = self._candidate(-1, config)
        if current is None:
            self.link.roles.advertising.publish(-1, candidate)
        else:
            self._apply_configuration(current, candidate)

    @staticmethod
    def _apply_configuration(current: Advertising, candidate: Advertising) -> None:
        # A radio operation may still hold the live context when HCI updates it.
        for field in fields(AdvertisingConfig):
            setattr(current, field.name, getattr(candidate, field.name))
        current.identity_address = candidate.identity_address
        current.identity_target = candidate.identity_target

    def configuration_status(
        self, handle: int, secondary_phy: int, *, incompatible_periodic: bool
    ) -> int:
        item = self.link.advertisers.get(handle)
        if item and item.periodic_enabled and incompatible_periodic:
            return INVALID
        if item and (
            item.enabled
            or (item.periodic_enabled and secondary_phy != item.secondary_phy)
        ):
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
            handle,
            item.secondary_phy,
            incompatible_periodic=not item.extended
            or bool(item.mode)
            or item.anonymous,
        )
        if status == SUCCESS:
            status = self.data_status(item)
        if status != SUCCESS:
            return status
        if item.periodic_enabled:
            current = self.link.advertisers[handle]
            if (
                item.secondary_coding != current.secondary_coding
                and not self._active_train_fits(item)
            ):
                return DISALLOWED
            self._apply_configuration(current, item)
        else:
            self.link.roles.advertising.publish(handle, item)
        return SUCCESS

    @staticmethod
    def _active_train_fits(item: Advertising) -> bool:
        """Check the new coding against the train's fixed interval and slots."""
        if required_periodic_interval(item) > item.periodic_interval:
            return False
        if item.pawr is None:
            return True
        state = item.pawr_state
        assert state is not None
        data = max((entry.data for entry in state.data.values()), key=len, default=b'')
        # The subevent executor can omit an ACAD update to preserve Host data.
        return fits(
            replace(subevent_pdu(item, data), acad=b''),
            item.secondary_phy,
            indication_limit_us(item.pawr, item.pawr_response_slots),
            coding=item.secondary_coding.factor(check_capacity=True),
        )

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
        ) or advertising_time_us(item, check_capacity=True) > item.interval_max_us:
            return TOO_LONG
        return SUCCESS

    def set_random_address(self, handle: int, address: Address) -> int:
        item = self.link.advertisers.get(handle)
        if item is None:
            return UNKNOWN_ADV
        if item.enabled and item.mode & AdvertisingMode.CONNECTABLE:
            return DISALLOWED
        address = AddressSnapshot(address)
        item.random_address = address
        if OwnAddressType(item.own_address_type).uses_random_fallback:
            item.identity_address = address
            # Keep the address already sent in the current event through its
            # response. AdvertisingManager.run selects the next event's address.
            if not item.enabled:
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
                and required_periodic_interval(updated) > item.periodic_interval_limit
            )
            or advertising_time_us(updated, check_capacity=True)
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
        if enable:
            if not self._address_available(item):
                return INVALID
            self.link.roles.advertising.start(item)
        else:
            self.link.roles.advertising.stop(item)
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
                if (
                    advertising_time_us(item, check_capacity=True)
                    > item.interval_max_us
                ):
                    return TOO_LONG
        selected = sets or tuple((handle, 0, 0) for handle in self.link.advertisers)
        for handle, duration, max_events in selected:
            item = self.link.advertisers[handle]
            if enable:
                self.link.roles.advertising.start(
                    item, duration, max_events, interval_unit_us=interval_unit_us
                )
            else:
                self.link.roles.advertising.stop(item)
        return SUCCESS

    def remove(self, handle: int) -> int:
        item = self.link.advertisers.get(handle)
        if item is None:
            return UNKNOWN_ADV
        if (
            item.enabled
            or item.periodic_enabled
            or self.link.roles.broadcast.associated_group(handle) is not None
        ):
            return DISALLOWED
        self.link.roles.advertising.remove(handle)
        return SUCCESS

    def clear(self) -> int:
        if any(
            item.enabled
            or item.periodic_enabled
            or self.link.roles.broadcast.associated_group(handle) is not None
            for handle, item in self.link.advertisers.items()
        ):
            return DISALLOWED
        self.link.roles.advertising.clear()
        return SUCCESS

    def set_periodic_interval(
        self,
        handle: int,
        minimum: int,
        maximum: int,
        *,
        pawr: PawrTiming | None = None,
        response_slots: int = 0,
    ) -> int:
        """Configure the train, with responses if ``pawr`` is given (Vol 4, Part
        E, 7.8.61).

        One subevent has no subevent interval: its responses must fit the
        periodic advertising interval, so pick the shortest allowed one that
        holds them; the timing information then carries that interval, up to its
        318.75 ms maximum (Core Vol 6, Part B, 4.4.2.2.4).
        """
        item = self.link.advertisers[handle]
        if item.periodic_enabled:
            return DISALLOWED
        if (
            pawr is not None
            and self.link.roles.broadcast.associated_group(handle) is not None
        ):
            return DISALLOWED
        if not item.extended or item.mode or item.anonymous:
            return INVALID
        interval = minimum
        if pawr is not None and pawr.num_subevents == 1:
            needed = -(
                -(
                    pawr.response_slot_delay_us
                    + response_slots * pawr.response_slot_spacing_us
                )
                // LL_PERIODIC_INTERVAL_UNIT_US
            )
            interval = min(maximum, max(minimum, needed))
            pawr = replace(
                pawr,
                subevent_interval=min(interval, LL_PAWR_SUBEVENT_INTERVAL_MAX),
            )
        candidate = replace(item, pawr=pawr)
        interval = max(interval, required_periodic_interval(candidate))
        if interval > maximum:
            return TOO_LONG
        if not item.periodic_interval:
            # The first periodic configuration seeds a random DID (4.4.2.11).
            item.periodic_did = self.link.random.randrange(PDU_ADV_ADI_DID_MASK + 1)
        item.periodic_interval = interval
        item.periodic_interval_max = maximum
        item.pawr = pawr
        item.pawr_response_slots = response_slots if pawr is not None else 0
        return SUCCESS

    def pawr_status(self, handle: int) -> int:
        """Whether Host subevent data can be accepted at all (Vol 4, Part E,
        7.8.125): the set exists, has responses and asked for data.
        """
        item = self.link.advertisers.get(handle)
        if item is None:
            return UNKNOWN_ADV
        if item.pawr is None or item.pawr_state is None or not item.pawr_state.window:
            return DISALLOWED
        return SUCCESS

    def set_subevent_data(
        self, handle: int, entries: tuple[tuple[int, SubeventData], ...]
    ) -> int:
        """Store data for requested subevents, all or nothing (Vol 4, Part E,
        7.8.125).

        Each subevent names its instance in the outstanding request. The data
        must fit an AUX_SYNC_SUBEVENT_IND before the first response slot (Core
        Vol 6, Part B, 4.4.2.2.4).
        """
        status = self.pawr_status(handle)
        if status != SUCCESS:
            return status
        item = self.link.advertisers[handle]
        timing, state = item.pawr, item.pawr_state
        assert timing is not None and state is not None
        if any(
            entry.slot_start + entry.slot_count > item.pawr_response_slots
            for _, entry in entries
        ):
            return INVALID
        instances = []
        current = state.instance(timing.num_subevents)
        limit = indication_limit_us(timing, item.pawr_response_slots)
        for subevent, entry in entries:
            instance = state.window_instance(subevent, timing.num_subevents)
            if instance is None or instance in state.data:
                return DISALLOWED
            if instance < current:
                return ErrorCode.TOO_LATE_ERROR
            pdu = replace(subevent_pdu(item, entry.data), acad=b'')
            if not fits(
                pdu,
                item.secondary_phy,
                limit,
                coding=item.secondary_coding.factor(check_capacity=True),
            ):
                return TOO_LONG
            instances.append((instance, entry))
        state.data.update(instances)
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
        if enable:
            interval = max(
                item.periodic_interval, required_periodic_interval(candidate)
            )
            if interval > item.periodic_interval_limit:
                return TOO_LONG
            if item.enabled:
                combined = (
                    advertising_time_us(replace(candidate, periodic_enabled=True))
                    + periodic_time_us(candidate)
                    + LL_MAFS_US
                )
                interval = max(interval, combined // LL_PERIODIC_INTERVAL_UNIT_US + 1)
                if interval > item.periodic_interval_limit:
                    return DISALLOWED
            item.periodic_interval = interval
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
        self.cig: CigCommands = link.roles.cig
        self.big: BigCommands = link.roles.broadcast
        self.iso = IsoService(link)
        self.privacy = link.privacy
        self.decision = link.decision
        self._default_scan = (scan_interval_us, scan_window_us)
        self.reset_configuration(address)

    def reset_configuration(self, address: Address) -> None:
        self._scan_parameters = Scanning(AddressSnapshot(address), *self._default_scan)
        self.link.public_address = AddressSnapshot(address)
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

    def reset(self, address: Address) -> None:
        self.link.reset()
        self.reset_configuration(address)

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
        centrals = [
            connection
            for connection in self.link.connections.values()
            if connection.role == Role.CENTRAL
        ]
        if any(connection.controls.busy for connection in centrals):
            return DISALLOWED
        for connection in centrals:
            connection.controls.request_channel_map(value)
        self.link.roles.host_channel_map = value
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

    def request_peer_sca(self, handle: int) -> PreparedProcedure:
        connection = self._connection(handle)
        if connection is None:
            return PreparedProcedure(ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR)
        clock = connection.controls.clock_accuracy
        if not clock.peer_supports_update:
            return PreparedProcedure(ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)
        if clock.pending or clock.queued is not None:
            return PreparedProcedure(DISALLOWED)
        return PreparedProcedure(SUCCESS, clock.queue_read)

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
        if not connection.established or clock.pending or clock.queued is not None:
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

    def prepare_read_remote_transmit_power(
        self, handle: int, phy: int
    ) -> PreparedProcedure:
        connection = self._connection(handle)
        if connection is None:
            return PreparedProcedure(ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR)
        if not PowerPhy.LE_1M <= phy <= PowerPhy.LE_CODED_S2:
            return PreparedProcedure(
                ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR
            )
        return PreparedProcedure(
            SUCCESS, lambda: connection.controls.power.queue_read(PowerPhy(phy))
        )

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

    def prepare_subrate(
        self,
        handle: int,
        minimum: int,
        maximum: int,
        latency: int,
        continuation: int,
        timeout: int,
    ) -> PreparedProcedure:
        connection = self._connection(handle)
        if connection is None:
            return PreparedProcedure(ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR)
        request = SubrateRequest(minimum, maximum, latency, continuation, timeout)
        if not request.valid_host_parameters or not request.fits_connection(
            connection.parameters.interval_us
        ):
            return PreparedProcedure(INVALID)
        return connection.llcp.prepare_subrate(request)

    def set_default_rate(self, parameters: ConnectionRateParameters) -> int:
        if not parameters.valid:
            return INVALID
        if parameters.connection_interval_max < MIN_RATE_INTERVAL:
            return ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR
        self.link.default_rate = parameters
        return SUCCESS

    def prepare_connection_rate(
        self, handle: int, parameters: ConnectionRateParameters
    ) -> PreparedProcedure:
        connection = self._connection(handle)
        if connection is None or not connection.established:
            return PreparedProcedure(ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR)
        return connection.llcp.prepare_connection_rate(parameters)

    def prepare_frame_space(
        self, handle: int, minimum: int, maximum: int, phys: int, spacing: int
    ) -> PreparedProcedure:
        connection = self._connection(handle)
        if connection is None:
            return PreparedProcedure(ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR)
        return connection.llcp.prepare_frame_space(
            FrameSpaceRequest(minimum, maximum, int(phys), int(spacing))
        )

    def prepare_encryption(
        self, handle: int, rand: bytes, ediv: int, key: bytes
    ) -> PreparedProcedure:
        connection = self._connection(handle)
        return (
            connection.llcp.prepare_encryption(rand, ediv, key)
            if connection
            else PreparedProcedure(ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR)
        )

    def prepare_features(
        self, handle: int, pages: int | None = None
    ) -> PreparedProcedure:
        if pages is not None:
            if not 0 <= pages <= FEATURE_MAX_PAGE or not 0 <= handle <= 0x0EFF:
                return PreparedProcedure(ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR)
            if any(
                item.llcp.all_features_pending
                for item in self.link.connections.values()
            ):
                return PreparedProcedure(DISALLOWED)
        connection = self._connection(handle)
        return (
            connection.llcp.prepare_features(pages)
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
