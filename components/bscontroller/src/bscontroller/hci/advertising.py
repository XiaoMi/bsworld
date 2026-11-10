# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Legacy, extended and periodic advertising HCI validation and data assembly."""

from bscontroller import hci
from bscontroller.hci import (
    HCI_LE_ADV_FILTER_CONNECT_MASK,
    HCI_LE_ADV_FILTER_POLICY_MASK,
    HCI_LE_ADV_FILTER_SCAN_MASK,
    HCI_LE_ADV_HANDLE_MAX,
    HCI_LE_ADV_INTERVAL_MAX,
    HCI_LE_ADV_INTERVAL_MIN,
    HCI_LE_ADV_INTERVAL_UNIT_US,
    HCI_LE_DURATION_UNIT_US,
    HCI_LE_EXT_ADV_INTERVAL_MAX,
)
from bscontroller.hci.context import (
    DISALLOWED,
    INVALID,
    SUCCESS,
    UNKNOWN_ADV,
    UNSUPPORTED,
    LeControllerContext,
)
from bscontroller.hci.records import AdvertisingSet
from bscontroller.linklayer import (
    LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
    LL_PERIODIC_INTERVAL_MAX,
    LL_PERIODIC_INTERVAL_MIN,
    PDU_ADV_ADI_SID_MAX,
    PDU_ADV_LEGACY_DATA_SIZE_MAX,
    AdvertisingMode,
    AdvertisingPduType,
)
from bscontroller.linklayer.api import AdvertisingConfig
from bscontroller.linklayer.timing import LL_DIRECT_ADV_INTERVAL_US
from bscontroller.radio.base import RADIO_TX_POWER_MAX_DBM, RADIO_TX_POWER_MIN_DBM, Phy

LegacyAdvertisingType = hci.HCI_LE_Set_Advertising_Parameters_Command.AdvertisingType

Properties = (
    hci.HCI_LE_Set_Extended_Advertising_Parameters_Command.AdvertisingProperties
)

TX_POWER_NO_PREFERENCE = (
    hci.HCI_LE_Set_Extended_Advertising_Parameters_Command.TX_POWER_NO_PREFERENCE
)

DataOperation = hci.HCI_LE_Set_Extended_Advertising_Data_Command.Operation

SUPPORTED_ADVERTISING_PROPERTIES = int(
    Properties.CONNECTABLE_ADVERTISING
    | Properties.SCANNABLE_ADVERTISING
    | Properties.DIRECTED_ADVERTISING
    | Properties.HIGH_DUTY_CYCLE_DIRECTED_CONNECTABLE_ADVERTISING
    | Properties.USE_LEGACY_ADVERTISING_PDUS
    | Properties.ANONYMOUS_ADVERTISING
    | Properties.INCLUDE_TX_POWER
    | Properties.USE_DECISION_PDUS
    | Properties.INCLUDE_ADVA_IN_DECISION_PDUS
    | Properties.INCLUDE_ADI_IN_DECISION_PDUS
)

# Periodic trains require extended, nonconnectable, nonscannable, nonanonymous sets.
PERIODIC_INCOMPATIBLE_ADVERTISING_PROPERTIES = (
    Properties.CONNECTABLE_ADVERTISING
    | Properties.SCANNABLE_ADVERTISING
    | Properties.USE_LEGACY_ADVERTISING_PDUS
    | Properties.ANONYMOUS_ADVERTISING
)

PeriodicEnable = hci.HCI_LE_Set_Periodic_Advertising_Enable_Command.Enable
# Enable bit 1 (Include ADI) needs Periodic Advertising ADI Support (7.8.63).
PERIODIC_ENABLE_BITS = PeriodicEnable.ENABLE | PeriodicEnable.INCLUDE_ADI

LEGACY_PDU_TYPES: dict[int, AdvertisingPduType] = {
    LegacyAdvertisingType.ADV_IND: AdvertisingPduType.ADV_IND,
    LegacyAdvertisingType.ADV_DIRECT_IND: AdvertisingPduType.ADV_DIRECT_IND,
    LegacyAdvertisingType.ADV_SCAN_IND: AdvertisingPduType.ADV_SCAN_IND,
    LegacyAdvertisingType.ADV_NONCONN_IND: AdvertisingPduType.ADV_NONCONN_IND,
    LegacyAdvertisingType.ADV_DIRECT_IND_LOW_DUTY: AdvertisingPduType.ADV_DIRECT_IND,
}

LEGACY_ADVERTISING_PROPERTIES: dict[int, AdvertisingPduType] = {
    Properties.USE_LEGACY_ADVERTISING_PDUS: AdvertisingPduType.ADV_NONCONN_IND,
    Properties.USE_LEGACY_ADVERTISING_PDUS
    | Properties.SCANNABLE_ADVERTISING: AdvertisingPduType.ADV_SCAN_IND,
    Properties.USE_LEGACY_ADVERTISING_PDUS
    | Properties.CONNECTABLE_ADVERTISING
    | Properties.SCANNABLE_ADVERTISING: AdvertisingPduType.ADV_IND,
    Properties.USE_LEGACY_ADVERTISING_PDUS
    | Properties.CONNECTABLE_ADVERTISING
    | Properties.DIRECTED_ADVERTISING: AdvertisingPduType.ADV_DIRECT_IND,
    Properties.USE_LEGACY_ADVERTISING_PDUS
    | Properties.CONNECTABLE_ADVERTISING
    | Properties.DIRECTED_ADVERTISING
    | Properties.HIGH_DUTY_CYCLE_DIRECTED_CONNECTABLE_ADVERTISING: (
        AdvertisingPduType.ADV_DIRECT_IND
    ),
}


class LeAdvertisingCommands(LeControllerContext):
    def on_hci_le_set_decision_data_command(self, command):
        """Set per-set decision data (Core Vol 4, Part E, 7.8.144).

        Forward the decoded flags and payload to the LL advertising service,
        which validates the handle and payload before replacing the stored data.
        Updates are allowed while advertising is enabled; the event executor
        reads the stored data when building an ADV_DECISION_IND PDU.
        """
        return hci.HCI_StatusReturnParameters(
            self.ll.advertising.set_decision_data(
                command.advertising_handle,
                command.decision_type_flags,
                command.decision_data,
            )
        )

    def on_hci_le_read_advertising_physical_channel_tx_power_command(self, _command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.6 LE Read Advertising Physical Channel Tx
        Power Command
        '''
        # Core Vol 4, Part E, 7.8.6: report the power used by legacy advertising.
        return hci.HCI_LE_Read_Advertising_Physical_Channel_Tx_Power_ReturnParameters(
            status=SUCCESS,
            tx_power_level=(
                state.tx_power_dbm
                if (state := self.ll.advertising.snapshot(-1)) is not None
                else 0
            ),
        )

    def _legacy_configuration(self) -> AdvertisingConfig:
        """Decode HCI parameters without creating or changing an active advertiser."""
        record = self.controller.le_legacy_advertiser
        pdu_type = LEGACY_PDU_TYPES[record.advertising_type]
        high_duty = record.advertising_type == LegacyAdvertisingType.ADV_DIRECT_IND
        return AdvertisingConfig(
            self._address(record.own_address_type),
            interval_us=LL_DIRECT_ADV_INTERVAL_US
            if high_duty
            else record.advertising_interval_min * HCI_LE_ADV_INTERVAL_UNIT_US,
            channel_map=record.advertising_channel_map,
            pdu_type=pdu_type,
            target=record.peer_address
            if pdu_type == AdvertisingPduType.ADV_DIRECT_IND
            else None,
            high_duty=high_duty,
            filter_policy=record.advertising_filter_policy,
            own_address_type=record.own_address_type,
            privacy_peer=record.peer_address,
        )

    def on_hci_le_set_advertising_parameters_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.5 LE Set Advertising Parameters Command
        '''
        if self.ll.advertising.any_enabled():
            return hci.HCI_StatusReturnParameters(DISALLOWED)
        if (
            command.advertising_type not in LEGACY_PDU_TYPES
            or command.own_address_type not in tuple(hci.OwnAddressType)
        ):
            return hci.HCI_StatusReturnParameters(UNSUPPORTED)
        if (
            command.advertising_type != LegacyAdvertisingType.ADV_DIRECT_IND
            and not HCI_LE_ADV_INTERVAL_MIN
            <= command.advertising_interval_min
            <= command.advertising_interval_max
            <= HCI_LE_ADV_INTERVAL_MAX
        ) or not 1 <= command.advertising_channel_map <= LL_ADV_PRIMARY_CHANNEL_MAP_ALL:
            return hci.HCI_StatusReturnParameters(INVALID)
        if command.advertising_filter_policy not in (
            0,
            HCI_LE_ADV_FILTER_SCAN_MASK,
            HCI_LE_ADV_FILTER_CONNECT_MASK,
            HCI_LE_ADV_FILTER_POLICY_MASK,
        ):
            return hci.HCI_StatusReturnParameters(INVALID)
        if command.peer_address_type not in (
            hci.AddressType.PUBLIC_DEVICE,
            hci.AddressType.RANDOM_DEVICE,
        ):
            return hci.HCI_StatusReturnParameters(INVALID)
        self.controller.le_legacy_advertiser.advertising_interval_min = (
            command.advertising_interval_min
        )
        self.controller.le_legacy_advertiser.advertising_interval_max = (
            command.advertising_interval_max
        )
        self.controller.le_legacy_advertiser.advertising_type = command.advertising_type
        self.controller.le_legacy_advertiser.own_address_type = command.own_address_type
        self.controller.le_legacy_advertiser.peer_address_type = (
            command.peer_address_type
        )
        self.controller.le_legacy_advertiser.peer_address = command.peer_address
        self.controller.le_legacy_advertiser.advertising_channel_map = (
            command.advertising_channel_map
        )
        self.controller.le_legacy_advertiser.advertising_filter_policy = (
            command.advertising_filter_policy
        )
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_le_set_advertising_data_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.7 LE Set Advertising Data Command
        '''
        if len(command.advertising_data) > PDU_ADV_LEGACY_DATA_SIZE_MAX:
            return hci.HCI_StatusReturnParameters(INVALID)
        self.ll.advertising.configure_legacy(self._legacy_configuration())
        self.ll.advertising.store_data(-1, 'data', command.advertising_data)
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_le_set_scan_response_data_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.8 LE Set Scan Response Data Command
        '''
        if len(command.scan_response_data) > PDU_ADV_LEGACY_DATA_SIZE_MAX:
            return hci.HCI_StatusReturnParameters(INVALID)
        self.ll.advertising.configure_legacy(self._legacy_configuration())
        self.ll.advertising.store_data(-1, 'scan_response', command.scan_response_data)
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_le_set_advertising_enable_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.9 LE Set Advertising Enable Command
        '''
        if command.advertising_enable not in (0, 1):
            return hci.HCI_StatusReturnParameters(INVALID)
        if command.advertising_enable and (self.ll.advertising.has_extended_sets()):
            return hci.HCI_StatusReturnParameters(DISALLOWED)
        self.ll.advertising.configure_legacy(self._legacy_configuration())
        return hci.HCI_StatusReturnParameters(
            self.ll.advertising.enable_legacy(bool(command.advertising_enable))
        )

    def _extended_advertising_status(self, command):
        """Validate parameters against the live set without changing it.

        See Bluetooth Core 6.3, Vol 4, Part E, 7.8.53.
        """
        status = SUCCESS
        properties = command.advertising_event_properties
        status = self.ll.advertising.configuration_status(
            command.advertising_handle,
            bool(properties & PERIODIC_INCOMPATIBLE_ADVERTISING_PROPERTIES),
        )
        if status != SUCCESS:
            return status
        if (
            command.own_address_type not in tuple(hci.OwnAddressType)
            or command.advertising_filter_policy
            not in (
                0,
                HCI_LE_ADV_FILTER_SCAN_MASK,
                HCI_LE_ADV_FILTER_CONNECT_MASK,
                HCI_LE_ADV_FILTER_POLICY_MASK,
            )
            or command.primary_advertising_phy not in (Phy.LE_1M, Phy.LE_CODED)
            or command.secondary_advertising_phy not in tuple(Phy)
        ):
            status = UNSUPPORTED
        elif (
            properties & ~SUPPORTED_ADVERTISING_PROPERTIES
            or (
                properties & Properties.USE_DECISION_PDUS
                and properties & Properties.DIRECTED_ADVERTISING
            )
            or (
                not properties & Properties.USE_DECISION_PDUS
                and properties
                & (
                    Properties.INCLUDE_ADVA_IN_DECISION_PDUS
                    | Properties.INCLUDE_ADI_IN_DECISION_PDUS
                )
            )
            or (
                command.advertising_tx_power != TX_POWER_NO_PREFERENCE
                and not RADIO_TX_POWER_MIN_DBM
                <= command.advertising_tx_power
                <= RADIO_TX_POWER_MAX_DBM
            )
            or command.scan_request_notification_enable
            not in (
                0,
                1,
            )
            or properties & Properties.USE_LEGACY_ADVERTISING_PDUS
            and (
                properties not in LEGACY_ADVERTISING_PROPERTIES
                or command.primary_advertising_phy != Phy.LE_1M
            )
            or not properties & Properties.USE_LEGACY_ADVERTISING_PDUS
            and (
                properties
                & (
                    Properties.CONNECTABLE_ADVERTISING
                    | Properties.SCANNABLE_ADVERTISING
                )
                == (
                    Properties.CONNECTABLE_ADVERTISING
                    | Properties.SCANNABLE_ADVERTISING
                )
                or properties
                & Properties.HIGH_DUTY_CYCLE_DIRECTED_CONNECTABLE_ADVERTISING
                or (
                    properties & Properties.ANONYMOUS_ADVERTISING
                    and properties
                    & (
                        Properties.CONNECTABLE_ADVERTISING
                        | Properties.SCANNABLE_ADVERTISING
                        | Properties.INCLUDE_ADVA_IN_DECISION_PDUS
                    )
                )
            )
            or (
                (
                    not properties
                    & Properties.HIGH_DUTY_CYCLE_DIRECTED_CONNECTABLE_ADVERTISING
                    and not HCI_LE_ADV_INTERVAL_MIN
                    <= command.primary_advertising_interval_min
                    <= command.primary_advertising_interval_max
                    <= HCI_LE_EXT_ADV_INTERVAL_MAX
                )
                or not 1
                <= command.primary_advertising_channel_map
                <= LL_ADV_PRIMARY_CHANNEL_MAP_ALL
                or not 0 <= command.advertising_sid <= PDU_ADV_ADI_SID_MAX
                or not 0 <= command.advertising_handle <= HCI_LE_ADV_HANDLE_MAX
            )
        ):
            status = INVALID
        return status

    def _extended_advertising_configuration(self, command) -> AdvertisingConfig:
        """Translate decoded HCI fields; LL retains payloads and runtime state."""
        properties = command.advertising_event_properties
        high_duty = bool(
            properties & Properties.HIGH_DUTY_CYCLE_DIRECTED_CONNECTABLE_ADVERTISING
        )
        return AdvertisingConfig(
            # Extended sets have independent random fallbacks (7.8.52/53).
            # The LL service substitutes an address assigned to this handle;
            # the global LE Set Random Address value belongs to other roles.
            address=hci.Address.ANY_RANDOM
            if hci.OwnAddressType(command.own_address_type).uses_random_fallback
            else self.controller.public_address,
            interval_us=LL_DIRECT_ADV_INTERVAL_US
            if high_duty
            else (
                command.primary_advertising_interval_min * HCI_LE_ADV_INTERVAL_UNIT_US
            ),
            interval_max_us=(
                command.primary_advertising_interval_max * HCI_LE_ADV_INTERVAL_UNIT_US
            ),
            channel_map=command.primary_advertising_channel_map,
            extended=not properties & Properties.USE_LEGACY_ADVERTISING_PDUS,
            pdu_type=LEGACY_ADVERTISING_PROPERTIES.get(
                properties, AdvertisingPduType.ADV_NONCONN_IND
            ),
            target=(
                command.peer_address
                if properties & Properties.DIRECTED_ADVERTISING
                else None
            ),
            high_duty=high_duty,
            mode=int(
                properties
                & (
                    Properties.CONNECTABLE_ADVERTISING
                    | Properties.SCANNABLE_ADVERTISING
                )
            ),
            primary_phy=Phy(command.primary_advertising_phy),
            secondary_phy=Phy(command.secondary_advertising_phy),
            notify_scan_request=bool(command.scan_request_notification_enable),
            filter_policy=command.advertising_filter_policy,
            anonymous=bool(properties & Properties.ANONYMOUS_ADVERTISING),
            tx_power_dbm=(
                0
                if command.advertising_tx_power == TX_POWER_NO_PREFERENCE
                else command.advertising_tx_power
            ),
            include_tx_power=bool(properties & Properties.INCLUDE_TX_POWER),
            sid=command.advertising_sid,
            own_address_type=command.own_address_type,
            privacy_peer=command.peer_address,
            decision=bool(properties & Properties.USE_DECISION_PDUS),
            decision_address=bool(
                properties & Properties.INCLUDE_ADVA_IN_DECISION_PDUS
            ),
            decision_adi=bool(properties & Properties.INCLUDE_ADI_IN_DECISION_PDUS),
        )

    def on_hci_le_set_extended_advertising_parameters_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.53 LE Set Extended Advertising Parameters
        Command
        '''
        status = self._extended_advertising_status(command)
        if status != SUCCESS:
            return hci.HCI_LE_Set_Extended_Advertising_Parameters_ReturnParameters(
                status, 0
            )
        config = self._extended_advertising_configuration(command)
        # Parameter validity alone is insufficient: changing mode or PHY can make
        # previously accepted data illegal or too long for the interval.
        status = self.ll.advertising.configure(command.advertising_handle, config)
        if status == SUCCESS:
            record = self.controller.advertising_sets.setdefault(
                command.advertising_handle,
                AdvertisingSet(self.controller, command.advertising_handle),
            )
            record.parameters = command
        return hci.HCI_LE_Set_Extended_Advertising_Parameters_ReturnParameters(
            status, config.tx_power_dbm if status == SUCCESS else 0
        )

    def on_hci_le_set_advertising_set_random_address_command(self, command):
        return hci.HCI_StatusReturnParameters(
            self.ll.advertising.set_random_address(
                command.advertising_handle, command.random_address
            )
        )

    def _set_data(self, handle, operation, data, field):
        """Assemble and publish one advertising payload from HCI fragments.

        See Bluetooth Core 6.3, Vol 4, Part E, 7.8.54, 7.8.55 and 7.8.62.

        ``field`` selects advertising, scan-response or periodic data. Keep each
        partial assembly in ``self.fragments`` until LAST/COMPLETE publishes it
        to the advertiser. Check the operation and state before changing buffers.
        Once FIRST/COMPLETE discards the previous payload, later length/airtime
        failures leave it empty rather than restoring the old data.
        """
        item = self.ll.advertising.snapshot(handle)
        if item is None:
            return hci.HCI_StatusReturnParameters(UNKNOWN_ADV)

        # Stage fragments by set and payload field, so interleaved commands cannot
        # append advertising data to a scan-response or periodic-data sequence.
        key = (handle, field)
        maximum = (
            self.controller.config.max_advertising_data_length
            if item.extended
            else PDU_ADV_LEGACY_DATA_SIZE_MAX
        )
        # Scannable extended events put their data in AUX_SCAN_RSP; other modes
        # cannot provide scan data (Vol 6, Part B, 4.4.2.11; Vol 4, Part E, 7.8.55).
        if item.extended and (
            (field == 'data' and item.mode == AdvertisingMode.SCANNABLE and data)
            or (
                field == 'scan_response'
                and item.mode != AdvertisingMode.SCANNABLE
                and data
            )
        ):
            return hci.HCI_StatusReturnParameters(INVALID)
        if item.target is not None and not item.extended and data:
            # A legacy directed PDU contains the two addresses, with no data field.
            return hci.HCI_StatusReturnParameters(INVALID)
        if operation not in tuple(DataOperation):
            return hci.HCI_StatusReturnParameters(INVALID)
        if (
            field != 'periodic_data'
            and not item.extended
            and (
                operation != DataOperation.COMPLETE_DATA
                or len(data) > PDU_ADV_LEGACY_DATA_SIZE_MAX
            )
        ):
            # Legacy PDUs still use a single, at-most-31-octet payload even when
            # they are configured through the extended advertising commands.
            return hci.HCI_StatusReturnParameters(INVALID)
        if (
            operation not in (DataOperation.COMPLETE_DATA, DataOperation.UNCHANGED_DATA)
            and not data
        ):
            return hci.HCI_StatusReturnParameters(INVALID)

        if (
            item.enabled or (field == 'periodic_data' and item.periodic_enabled)
        ) and operation not in (
            DataOperation.COMPLETE_DATA,
            DataOperation.UNCHANGED_DATA,
        ):
            # Require a complete replacement while the advertiser is active, keeping
            # partially assembled data out of the running train.
            return hci.HCI_StatusReturnParameters(DISALLOWED)
        if operation == DataOperation.UNCHANGED_DATA:
            # TODO: reject this operation for scan-response data (7.8.55).
            # Periodic data requires its own train to be enabled (7.8.62).
            enabled = (
                item.periodic_enabled if field == 'periodic_data' else item.enabled
            )
            if data or not enabled or not getattr(item, field):
                return hci.HCI_StatusReturnParameters(INVALID)
            # Renew the data identity without replacing the payload bytes.
            self.ll.advertising.renew_data_identity(handle, field)
            return hci.HCI_StatusReturnParameters(SUCCESS)

        if (
            operation
            in (DataOperation.INTERMEDIATE_FRAGMENT, DataOperation.LAST_FRAGMENT)
            and key not in self.fragments
        ):
            # Reject an orphaned continuation instead of appending it to an unrelated
            # or already discarded sequence.
            return hci.HCI_StatusReturnParameters(DISALLOWED)
        if operation in (DataOperation.FIRST_FRAGMENT, DataOperation.COMPLETE_DATA):
            # FIRST/COMPLETE starts a new sequence: discard both staged and published
            # data. A later failure leaves the payload empty rather than restoring it.
            self.fragments.pop(key, None)
            self.ll.advertising.store_data(handle, field, b'')

        value = (
            self.fragments.get(key, b'')
            if operation
            in (DataOperation.INTERMEDIATE_FRAGMENT, DataOperation.LAST_FRAGMENT)
            else b''
        ) + data
        if len(value) > maximum:
            # Abandon the entire oversized sequence; later fragments must not revive it.
            self.fragments.pop(key, None)
            self.ll.advertising.store_data(handle, field, b'')
            return hci.HCI_StatusReturnParameters(
                hci.HCI_ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR
            )

        # LL checks the candidate's PDU placement and airtime without publishing it.
        if self.ll.advertising.validate_data(handle, field, value) != SUCCESS:
            # Check airtime separately from byte capacity. Connectable data must fit
            # one AUX_ADV_IND; other trains must fit the configured interval
            # (Vol 4, Part E, 7.8.54/7.8.55/7.8.62; 7.8.56).
            self.fragments.pop(key, None)
            self.ll.advertising.store_data(handle, field, b'')
            return hci.HCI_StatusReturnParameters(
                hci.HCI_ErrorCode.PACKET_TOO_LONG_ERROR
            )

        if operation in (DataOperation.LAST_FRAGMENT, DataOperation.COMPLETE_DATA):
            # Reserve room for both the advertising work and the running periodic
            # train before publishing the completed payload.
            if self.ll.advertising.publication_status(handle, field, value) != SUCCESS:
                self.fragments.pop(key, None)
                return hci.HCI_StatusReturnParameters(DISALLOWED)

            # Publish only on LAST/COMPLETE, renewing the payload's data identity
            # once, and release staging storage after the payload becomes visible
            # to LL.
            self.ll.advertising.store_data(handle, field, value, renew=True)
            self.fragments.pop(key, None)
        else:
            self.fragments[key] = value
        return hci.HCI_StatusReturnParameters(SUCCESS)

    def on_hci_le_set_extended_advertising_data_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.54 LE Set Extended Advertising Data
        Command
        '''
        return self._set_data(
            command.advertising_handle,
            command.operation,
            command.advertising_data,
            'data',
        )

    def on_hci_le_set_extended_scan_response_data_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.55 LE Set Extended Scan Response Data
        Command
        '''
        return self._set_data(
            command.advertising_handle,
            command.operation,
            command.scan_response_data,
            'scan_response',
        )

    def on_hci_le_set_extended_advertising_enable_command(self, command):
        handles = command.advertising_handles
        if (
            command.enable not in (0, 1)
            or len(set(handles)) != len(handles)
            or (command.enable and not handles)
        ):
            return hci.HCI_StatusReturnParameters(INVALID)
        sets = tuple(
            (handle, duration * HCI_LE_DURATION_UNIT_US, maximum)
            for handle, duration, maximum in zip(
                handles, command.durations, command.max_extended_advertising_events
            )
        )
        return hci.HCI_StatusReturnParameters(
            self.ll.advertising.enable(
                bool(command.enable), sets, interval_unit_us=HCI_LE_ADV_INTERVAL_UNIT_US
            )
        )

    def on_hci_le_read_maximum_advertising_data_length_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.57 LE Read Maximum Advertising Data
        Length Command
        '''
        return hci.HCI_LE_Read_Maximum_Advertising_Data_Length_ReturnParameters(
            0, self.controller.config.max_advertising_data_length
        )

    def on_hci_le_read_number_of_supported_advertising_sets_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.58 LE Read Number of Supported
        Advertising Sets Command
        '''
        return hci.HCI_LE_Read_Number_Of_Supported_Advertising_Sets_ReturnParameters(
            SUCCESS, self.controller.config.advertising_set_count
        )

    def on_hci_le_remove_advertising_set_command(self, command):
        handle = command.advertising_handle
        status = self.ll.advertising.remove(handle)
        if status == SUCCESS:
            self.controller.advertising_sets.pop(handle, None)
            self.fragments = {
                key: value for key, value in self.fragments.items() if key[0] != handle
            }
        return hci.HCI_StatusReturnParameters(status)

    def on_hci_le_clear_advertising_sets_command(self, command):
        status = self.ll.advertising.clear()
        if status == SUCCESS:
            self.controller.advertising_sets.clear()
            self.fragments.clear()
        return hci.HCI_StatusReturnParameters(status)

    def on_hci_le_set_periodic_advertising_parameters_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.61 LE Set Periodic Advertising Parameters
        Command
        '''
        item = self.ll.advertising.snapshot(command.advertising_handle)
        if item is None:
            return hci.HCI_StatusReturnParameters(UNKNOWN_ADV)
        if item.periodic_enabled:
            return hci.HCI_StatusReturnParameters(DISALLOWED)
        if not item.extended or item.mode or item.anonymous:
            return hci.HCI_StatusReturnParameters(INVALID)
        if command.periodic_advertising_properties:
            return hci.HCI_StatusReturnParameters(UNSUPPORTED)
        if (
            not LL_PERIODIC_INTERVAL_MIN
            <= command.periodic_advertising_interval_min
            <= command.periodic_advertising_interval_max
            <= LL_PERIODIC_INTERVAL_MAX
        ):
            return hci.HCI_StatusReturnParameters(INVALID)
        return hci.HCI_StatusReturnParameters(
            self.ll.advertising.set_periodic_interval(
                command.advertising_handle,
                command.periodic_advertising_interval_min,
                command.periodic_advertising_interval_max,
            )
        )

    def on_hci_le_set_periodic_advertising_data_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.62 LE Set Periodic Advertising Data
        Command
        '''
        item = self.ll.advertising.snapshot(command.advertising_handle)
        if item is None:
            return hci.HCI_StatusReturnParameters(UNKNOWN_ADV)
        if not item.periodic_interval:
            return hci.HCI_StatusReturnParameters(DISALLOWED)
        return self._set_data(
            command.advertising_handle,
            command.operation,
            command.advertising_data,
            'periodic_data',
        )

    def on_hci_le_set_periodic_advertising_enable_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.63 LE Set Periodic Advertising Enable
        Command
        '''
        item = self.ll.advertising.snapshot(command.advertising_handle)
        if item is None:
            return hci.HCI_StatusReturnParameters(UNKNOWN_ADV)
        if command.enable & ~int(PERIODIC_ENABLE_BITS):
            return hci.HCI_StatusReturnParameters(UNSUPPORTED)
        enable = bool(command.enable & PeriodicEnable.ENABLE)
        if enable and (command.advertising_handle, 'periodic_data') in self.fragments:
            return hci.HCI_StatusReturnParameters(DISALLOWED)
        # Bit 1 asks for the ADI in AUX_SYNC_IND; disabling ignores it (7.8.63).
        return hci.HCI_StatusReturnParameters(
            self.ll.advertising.enable_periodic(
                command.advertising_handle,
                enable,
                include_adi=bool(command.enable & PeriodicEnable.INCLUDE_ADI),
            )
        )
