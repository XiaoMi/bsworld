# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Advertising set and periodic train configuration at the LL service boundary."""

from __future__ import annotations

from dataclasses import fields, replace
from typing import TYPE_CHECKING

from bscontroller.address import OwnAddressType
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_ADV_DIRECT_HIGH_DUTY_TIMEOUT_US,
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
    advertising_snapshot,
)
from bscontroller.linklayer.discovery import Advertising
from bscontroller.linklayer.extended import (
    advertising_time_us,
    auxiliary_pdu,
    periodic_time_us,
    required_periodic_interval,
    subevent_pdu,
)
from bscontroller.linklayer.pawr import (
    LL_PAWR_SUBEVENT_INTERVAL_MAX,
    PawrTiming,
    SubeventData,
    fits,
    indication_limit_us,
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
            if handle not in self.link.advertisers:
                item.did = self.link.random.randrange(PDU_ADV_ADI_DID_MASK + 1)
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
        """Choose a new DID for the set or train (Vol 6, Part B, 4.4.2.11)."""
        item = self.link.advertisers[handle]
        value = self.link.random.randrange(PDU_ADV_ADI_DID_MASK)
        if field == 'periodic_data':
            item.periodic_did = value + (value >= item.periodic_did)
        else:
            item.did = value + (value >= item.did)

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
            self.link.roles.advertising.start(
                item, LL_ADV_DIRECT_HIGH_DUTY_TIMEOUT_US if item.high_duty else 0
            )
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
            for handle, duration, _ in sets:
                item = self.link.advertisers[handle]
                if (
                    item.high_duty
                    and not 0 < duration <= LL_ADV_DIRECT_HIGH_DUTY_TIMEOUT_US
                ):
                    return INVALID
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
