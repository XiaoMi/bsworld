# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Initiating, ACL, encryption and connected Link Layer procedure HCI commands."""

import asyncio
import logging

from bscontroller import crypto, hci
from bscontroller.capabilities import (
    CONTROLLER_PRIMARY_SCAN_PHY_MASK,
    CONTROLLER_SUPPORTED_PHY_MASK,
)
from bscontroller.hci import (
    HCI_LE_ADV_HANDLE_MAX,
    HCI_LE_ALL_PHYS_RX_MASK,
    HCI_LE_ALL_PHYS_TX_MASK,
    HCI_LE_EXT_SCAN_INTERVAL_MAX,
    HCI_LE_NO_ADVERTISING_HANDLE,
    HCI_LE_NO_SUBEVENT,
    HCI_LE_PHY_OPTIONS_NO_PREFERENCE,
    HCI_LE_PHY_OPTIONS_S2_PREFERRED,
    HCI_LE_PHY_OPTIONS_S8_PREFERRED,
    HCI_LE_SCAN_INTERVAL_MAX,
    HCI_LE_SCAN_INTERVAL_MIN,
    HCI_LE_SCAN_INTERVAL_UNIT_US,
)
from bscontroller.hci.capabilities import (
    HCI_AUTHENTICATED_PAYLOAD_TIMEOUT_UNIT_US,
    HCI_RSSI_NOT_AVAILABLE,
)
from bscontroller.hci.context import (
    DISALLOWED,
    INVALID,
    SUCCESS,
    UNSUPPORTED,
    LeControllerContext,
)
from bscontroller.linklayer import (
    LL_CHANNEL_MAP_SIZE,
    LL_CONN_WINDOW_SIZE_MIN,
    LL_ENCRYPTION_RANDOM_SIZE,
)
from bscontroller.linklayer.api import InitiatingConfig, ScanWindowConfig
from bscontroller.linklayer.data_length import valid_data_length
from bscontroller.linklayer.filtering import (
    LEGACY_INITIATOR_FILTER_POLICIES,
    InitiatorFilterPolicy,
)
from bscontroller.linklayer.parameters import (
    Parameters,
    valid_channel_map,
    valid_timing,
)
from bscontroller.linklayer.pawr import LL_PAWR_SUBEVENTS_MAX
from bscontroller.radio.base import PHY_CODED_S2_FACTOR, PHY_CODED_S8_FACTOR, Phy

logger = logging.getLogger(__name__)


class LeConnectionCommands(LeControllerContext):
    def _start_initiating(self, command, parameters, windows, pawr=None):
        """Commit one validated attempt; a rejected command leaves all state intact.

        ``pawr`` names the local train and subevent of an Extended Create
        Connection [v2]; the filter policy does not apply to it (7.8.66).
        """
        filter_policy = command.initiator_filter_policy if pawr is None else 0
        status = self.ll.initiating_status(command.peer_address, bool(filter_policy))
        if status != SUCCESS:
            self.controller._send_hci_command_status(status, command.op_code)
            return
        if (
            command.initiator_filter_policy
            not in (
                tuple(InitiatorFilterPolicy)
                if isinstance(command, hci.HCI_LE_Extended_Create_Connection_Command)
                else LEGACY_INITIATOR_FILTER_POLICIES
            )
            or command.own_address_type not in tuple(hci.OwnAddressType)
            or command.peer_address_type
            not in (hci.AddressType.PUBLIC_DEVICE, hci.AddressType.RANDOM_DEVICE)
        ):
            self.controller._send_hci_command_status(UNSUPPORTED, command.op_code)
            return
        # Commit after validation. Keep the first window in the common fields
        # and retain the full per-PHY configuration for the selected air PHY.
        status = self.ll.start_initiating(
            InitiatingConfig(
                self._address(command.own_address_type),
                command.peer_address,
                windows,
                tuple(parameters.items()),
                filter_policy=filter_policy,
                own_address_type=command.own_address_type,
                pawr_advertising_handle=pawr[0] if pawr else None,
                pawr_subevent=pawr[1] if pawr else 0,
            )
        )
        self.controller._send_hci_command_status(status, command.op_code)

    def on_hci_le_create_connection_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.12 LE Create Connection Command
        '''
        if (
            command.min_ce_length > command.max_ce_length
            or not valid_timing(
                command.connection_interval_min,
                command.connection_interval_max,
                command.max_latency,
                command.supervision_timeout,
            )
            or not HCI_LE_SCAN_INTERVAL_MIN
            <= command.le_scan_window
            <= command.le_scan_interval
            <= HCI_LE_SCAN_INTERVAL_MAX
        ):
            self.controller._send_hci_command_status(INVALID, command.op_code)
            return
        # Wire identity and hop are allocated only before sending CONNECT_IND.
        parameters = Parameters(
            0,
            0,
            LL_CONN_WINDOW_SIZE_MIN,
            0,
            command.connection_interval_min,
            command.max_latency,
            command.supervision_timeout,
            self.host_channel_map,
            0,
        )
        self._start_initiating(
            command,
            {Phy.LE_1M: parameters},
            (
                ScanWindowConfig(
                    Phy.LE_1M,
                    command.le_scan_interval * HCI_LE_SCAN_INTERVAL_UNIT_US,
                    command.le_scan_window * HCI_LE_SCAN_INTERVAL_UNIT_US,
                ),
            ),
        )

    def on_hci_le_extended_create_connection_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.66 LE Extended Create Connection Command
        '''
        self._extended_create_connection(command)

    def on_hci_le_extended_create_connection_v2_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.66 LE Extended Create Connection [v2]
        Command

        Advertising_Handle and Subevent name a local PAwR train: the Link Layer
        sends AUX_CONNECT_REQ in that subevent instead of scanning, and ignores
        the scan parameters (Core Vol 6, Part B, 4.4.2.12.2).
        '''
        handle, subevent = command.advertising_handle, command.subevent
        none = (
            handle == HCI_LE_NO_ADVERTISING_HANDLE,
            subevent == HCI_LE_NO_SUBEVENT,
        )
        if all(none):
            self._extended_create_connection(command)
            return
        status = (
            INVALID
            if any(none)
            or handle > HCI_LE_ADV_HANDLE_MAX
            or subevent >= LL_PAWR_SUBEVENTS_MAX
            else self.ll.pawr_initiating_status(
                handle, subevent, command.initiating_phys
            )
        )
        if status != SUCCESS:
            self.controller._send_hci_command_status(status, command.op_code)
            return
        self._extended_create_connection(command, pawr=(handle, subevent))

    def _extended_create_connection(self, command, pawr=None):
        if command.initiating_phys & ~CONTROLLER_SUPPORTED_PHY_MASK:
            self.controller._send_hci_command_status(UNSUPPORTED, command.op_code)
            return
        if (
            pawr is None
            and not command.initiating_phys & CONTROLLER_PRIMARY_SCAN_PHY_MASK
        ):
            self.controller._send_hci_command_status(INVALID, command.op_code)
            return
        # Parameter arrays follow set PHY bits, including LE 2M parameters even
        # though only LE 1M/Coded contribute primary-channel scanning windows.
        phys = [phy for phy in Phy if command.initiating_phys & (1 << (phy - 1))]
        values = {}
        for index, phy in enumerate(phys):
            minimum, maximum, latency, timeout = (
                command.connection_interval_mins[index],
                command.connection_interval_maxs[index],
                command.max_latencies[index],
                command.supervision_timeouts[index],
            )
            if (
                not valid_timing(minimum, maximum, latency, timeout)
                or command.min_ce_lengths[index] > command.max_ce_lengths[index]
                or pawr is None
                and not HCI_LE_SCAN_INTERVAL_MIN
                <= command.scan_windows[index]
                <= command.scan_intervals[index]
                <= HCI_LE_EXT_SCAN_INTERVAL_MAX
            ):
                self.controller._send_hci_command_status(INVALID, command.op_code)
                return
            # The selected PHY receives its wire parameters at connection creation.
            values[phy] = Parameters(
                0,
                0,
                LL_CONN_WINDOW_SIZE_MIN,
                0,
                minimum,
                latency,
                timeout,
                self.host_channel_map,
                0,
            )
        self._start_initiating(
            command,
            values,
            ()
            if pawr is not None
            else tuple(
                ScanWindowConfig(
                    phy,
                    command.scan_intervals[index] * HCI_LE_SCAN_INTERVAL_UNIT_US,
                    command.scan_windows[index] * HCI_LE_SCAN_INTERVAL_UNIT_US,
                )
                for index, phy in enumerate(phys)
                if phy != Phy.LE_2M
            ),
            pawr,
        )

    def on_hci_le_create_connection_cancel_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.13 LE Create Connection Cancel Command
        '''
        item = self.ll.cancel_initiating()
        if item is None:
            return hci.HCI_StatusReturnParameters(DISALLOWED)
        asyncio.get_running_loop().call_soon(
            self._emit_hci_packet,
            self._connection_complete_event(
                status=hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR,
                connection_handle=0,
                role=0,
                peer_address_type=item.address_type,
                peer_address=item,
                connection_interval=0,
                peripheral_latency=0,
                supervision_timeout=0,
                central_clock_accuracy=0,
            ),
        )
        return hci.HCI_StatusReturnParameters(SUCCESS)

    def on_hci_disconnect_command(self, command):
        status = self.ll.disconnect(command.connection_handle, command.reason)
        self.controller._send_hci_command_status(status, command.op_code)

    def on_hci_read_rssi_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.5.4 Read RSSI Command
        '''
        connection = self.ll.connection_snapshot(command.handle)
        return hci.HCI_Read_RSSI_ReturnParameters(
            (
                SUCCESS
                if connection
                else hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
            ),
            command.handle,
            (connection.rssi if connection.rssi is not None else HCI_RSSI_NOT_AVAILABLE)
            if connection
            else 0,
        )

    def on_hci_le_request_peer_sca_command(self, command):
        """Read the peer's sleep clock accuracy (Core Vol 4, Part E, 7.8.108)."""
        self._run_ll_request(
            command, self.ll.request_peer_sca(command.connection_handle)
        )

    def on_hci_le_modify_sleep_clock_accuracy_command(self, command):
        """Change the modeled sleep clock for testing (Core Vol 4, Part E, 7.8.94)."""
        return hci.HCI_StatusReturnParameters(
            self.ll.modify_sleep_clock_accuracy(command.action)
        )

    def on_hci_le_enable_encryption_command(self, command):
        self._run_ll_request(
            command,
            self.ll.prepare_encryption(
                command.connection_handle,
                command.random_number,
                command.encrypted_diversifier,
                command.long_term_key,
            ),
        )

    def on_hci_le_encrypt_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.22 LE Encrypt Command
        '''
        return hci.HCI_LE_Encrypt_ReturnParameters(
            SUCCESS, crypto.e(command.key, command.plaintext_data)
        )

    def _supply_long_term_key(self, command, key):
        status = self.ll.supply_key(command.connection_handle, key)
        return hci.HCI_StatusAndConnectionHandleReturnParameters(
            status, command.connection_handle
        )

    def on_hci_le_long_term_key_request_reply_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.25 LE Long Term Key Request Reply Command
        '''
        return self._supply_long_term_key(command, command.long_term_key)

    def on_hci_le_long_term_key_request_negative_reply_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.26 LE Long Term Key Request Negative
        Reply Command
        '''
        return self._supply_long_term_key(command, None)

    def on_hci_read_authenticated_payload_timeout_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.3.93 Read Authenticated Payload Timeout
        Command
        '''
        connection = self.ll.connection_snapshot(command.connection_handle)
        return hci.HCI_Read_Authenticated_Payload_Timeout_ReturnParameters(
            (
                SUCCESS
                if connection
                else DISALLOWED
                if self.ll.has_iso_handle(command.connection_handle)
                else hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
            ),
            command.connection_handle,
            connection.auth_timeout_us // HCI_AUTHENTICATED_PAYLOAD_TIMEOUT_UNIT_US
            if connection
            else 0,
        )

    def on_hci_write_authenticated_payload_timeout_command(self, command):
        status = self.ll.set_authenticated_timeout(
            command.connection_handle,
            command.authenticated_payload_timeout
            * HCI_AUTHENTICATED_PAYLOAD_TIMEOUT_UNIT_US,
        )
        return hci.HCI_StatusAndConnectionHandleReturnParameters(
            status, command.connection_handle
        )

    def on_hci_le_rand_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.23 LE Rand Command
        '''
        return hci.HCI_LE_Rand_ReturnParameters(
            SUCCESS, self.ll.random_bytes(LL_ENCRYPTION_RANDOM_SIZE)
        )

    def on_hci_le_read_remote_features_command(self, command):
        self._run_ll_request(
            command, self.ll.prepare_features(command.connection_handle)
        )

    def on_hci_le_read_all_remote_features_command(self, command):
        self._run_ll_request(
            command,
            self.ll.prepare_features(
                command.connection_handle, command.pages_requested
            ),
        )

    def on_hci_le_set_host_feature_command(self, command):
        """Set a Host-controlled feature bit (Core Vol 4, Part E, 7.8.115)."""
        return hci.HCI_StatusReturnParameters(
            self.ll.set_host_feature(command.bit_number, command.bit_value)
        )

    def on_hci_read_remote_version_information_command(self, command):
        self._run_ll_request(
            command, self.ll.prepare_version(command.connection_handle)
        )

    def on_hci_le_connection_update_command(self, command):
        self._run_ll_request(
            command,
            self.ll.prepare_parameters(
                command.connection_handle,
                command.connection_interval_min,
                command.connection_interval_max,
                command.max_latency,
                command.supervision_timeout,
                valid_ce_lengths=command.min_ce_length <= command.max_ce_length,
            ),
        )

    def on_hci_le_set_default_subrate_command(self, command):
        """Configure future Central ACLs (Core Vol 4, Part E, 7.8.123)."""
        return hci.HCI_StatusReturnParameters(
            self.ll.set_default_subrate(
                command.subrate_min,
                command.subrate_max,
                command.max_latency,
                command.continuation_number,
                command.supervision_timeout,
            )
        )

    def on_hci_le_subrate_request_command(self, command):
        """Request a subrate change for an ACL (Core Vol 4, Part E, 7.8.124)."""
        self._run_ll_request(
            command,
            self.ll.prepare_subrate(
                command.connection_handle,
                command.subrate_min,
                command.subrate_max,
                command.max_latency,
                command.continuation_number,
                command.supervision_timeout,
            ),
        )

    def on_hci_le_remote_connection_parameter_request_reply_command(self, command):
        status = self.ll.reply_parameters(
            command.connection_handle,
            command.interval_min,
            command.interval_max,
            command.max_latency,
            command.timeout,
        )
        return hci.HCI_StatusAndConnectionHandleReturnParameters(
            status, command.connection_handle
        )

    def on_hci_le_remote_connection_parameter_request_negative_reply_command(
        self, command
    ):
        status = self.ll.reject_parameters(command.connection_handle, command.reason)
        return hci.HCI_StatusAndConnectionHandleReturnParameters(
            status, command.connection_handle
        )

    def on_hci_le_set_host_channel_classification_command(self, command):
        value = int.from_bytes(command.channel_map, 'little')
        status = (
            self.ll.classify_channels(value) if valid_channel_map(value) else INVALID
        )
        return hci.HCI_StatusReturnParameters(status)

    def on_hci_le_read_channel_map_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.20 LE Read Channel Map Command
        '''
        connection = self.ll.connection_snapshot(command.connection_handle)
        return hci.HCI_LE_Read_Channel_Map_ReturnParameters(
            (
                SUCCESS
                if connection
                else hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
            ),
            command.connection_handle,
            (connection.parameters.channel_map if connection else 0).to_bytes(
                LL_CHANNEL_MAP_SIZE, 'little'
            ),
        )

    @staticmethod
    def _phys(command):
        if (
            command.all_phys & ~(HCI_LE_ALL_PHYS_TX_MASK | HCI_LE_ALL_PHYS_RX_MASK)
            or (command.tx_phys | command.rx_phys) & ~CONTROLLER_SUPPORTED_PHY_MASK
        ):
            return None
        tx = (
            CONTROLLER_SUPPORTED_PHY_MASK
            if command.all_phys & HCI_LE_ALL_PHYS_TX_MASK
            else command.tx_phys
        )
        rx = (
            CONTROLLER_SUPPORTED_PHY_MASK
            if command.all_phys & HCI_LE_ALL_PHYS_RX_MASK
            else command.rx_phys
        )
        return (tx, rx) if tx and rx else None

    def on_hci_le_read_phy_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.47 LE Read PHY Command
        '''
        connection = self.ll.connection_snapshot(command.connection_handle)
        return hci.HCI_LE_Read_PHY_ReturnParameters(
            (
                SUCCESS
                if connection
                else hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
            ),
            command.connection_handle,
            connection.tx_phy if connection else Phy.LE_1M,
            connection.rx_phy if connection else Phy.LE_1M,
        )

    def on_hci_le_set_default_phy_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.48 LE Set Default PHY Command
        '''
        phys = self._phys(command)
        if phys is None:
            return hci.HCI_StatusReturnParameters(INVALID)
        self.ll.set_default_phys(*phys)
        return hci.HCI_StatusReturnParameters(SUCCESS)

    def on_hci_le_set_phy_command(self, command):
        phys = self._phys(command)
        parameter_status = SUCCESS
        if (command.tx_phys | command.rx_phys) & ~CONTROLLER_SUPPORTED_PHY_MASK:
            parameter_status = UNSUPPORTED
        elif phys is None or command.phy_options not in (
            HCI_LE_PHY_OPTIONS_NO_PREFERENCE,
            HCI_LE_PHY_OPTIONS_S2_PREFERRED,
            HCI_LE_PHY_OPTIONS_S8_PREFERRED,
        ):
            parameter_status = INVALID
        coding = (
            PHY_CODED_S2_FACTOR
            if command.phy_options == HCI_LE_PHY_OPTIONS_S2_PREFERRED
            else PHY_CODED_S8_FACTOR
        )
        self._run_ll_request(
            command,
            self.ll.prepare_phy(
                command.connection_handle, *(phys or (0, 0)), coding, parameter_status
            ),
        )

    def on_hci_acl_data_packet(self, packet):
        connection = self.ll.connection_snapshot(packet.connection_handle)
        if (
            connection is None
            or packet.bc_flag
            or packet.pb_flag
            not in (
                hci.HCI_ACL_PB_FIRST_NON_FLUSHABLE,
                hci.HCI_ACL_PB_CONTINUATION,
                hci.HCI_ACL_PB_FIRST_FLUSHABLE,
            )
        ):
            logger.warning('Invalid ACL handle or flags')
            return
        if len(packet.data) > self.controller.config.acl_packet_length:
            raise ValueError('ACL payload exceeds the HCI buffer')
        self.ll.send_acl(
            packet.connection_handle,
            packet.pb_flag == hci.HCI_ACL_PB_CONTINUATION,
            packet.data,
        )

    def on_hci_le_set_data_length_command(self, command):
        status = self.ll.set_data_length(
            command.connection_handle, command.tx_octets, command.tx_time
        )
        return hci.HCI_StatusAndConnectionHandleReturnParameters(
            status, command.connection_handle
        )

    def on_hci_le_read_suggested_default_data_length_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.34 LE Read Suggested Default Data Length
        Command
        '''
        return hci.HCI_LE_Read_Suggested_Default_Data_Length_ReturnParameters(
            SUCCESS, *self.ll.default_data_length()
        )

    def on_hci_le_write_suggested_default_data_length_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.35 LE Write Suggested Default Data Length
        Command
        '''
        if not valid_data_length(
            command.suggested_max_tx_octets, command.suggested_max_tx_time
        ):
            return hci.HCI_StatusReturnParameters(INVALID)
        self.ll.set_default_data_length(
            command.suggested_max_tx_octets, command.suggested_max_tx_time
        )
        return hci.HCI_StatusReturnParameters(SUCCESS)

    def on_hci_le_read_maximum_data_length_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.46 LE Read Maximum Data Length Command
        '''
        config = self.controller.config
        return hci.HCI_LE_Read_Maximum_Data_Length_ReturnParameters(
            SUCCESS,
            config.max_tx_octets,
            config.max_tx_time,
            config.max_rx_octets,
            config.max_rx_time,
        )
