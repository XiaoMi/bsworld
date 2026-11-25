# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""CIG, BIG and ISO commands and Host data input (Core Vol 4, Part E)."""

from bscontroller import hci
from bscontroller.hci.context import INVALID, SUCCESS, LeControllerContext
from bscontroller.linklayer.api import (
    BigConfig,
    BigSyncConfig,
    BigTestParameters,
    CigConfig,
    CisConfig,
)


class LeIsoCommands(LeControllerContext):
    def on_hci_le_read_buffer_size_v2_command(self, command):
        config = self.controller.config
        return hci.HCI_LE_Read_Buffer_Size_V2_ReturnParameters(
            SUCCESS,
            config.acl_packet_length,
            config.acl_packet_count,
            config.iso_packet_length,
            config.iso_packet_count,
        )

    def on_hci_read_connection_accept_timeout_command(self, command):
        return hci.HCI_Read_Connection_Accept_Timeout_ReturnParameters(
            SUCCESS, self.ll.connection_accept_timeout
        )

    def on_hci_write_connection_accept_timeout_command(self, command):
        # Vol 4, Part E, 6.7: 0.625 ms through 29 s in 625 µs units.
        timeout = command.connection_accept_timeout
        if not 1 <= timeout <= 0xB540:
            return hci.HCI_StatusReturnParameters(INVALID)
        self.ll.set_connection_accept_timeout(timeout)
        return hci.HCI_StatusReturnParameters(SUCCESS)

    def _set_cig_parameters(self, command, streams, **parameters):
        status, handles = self.ll.set_cig_parameters(
            CigConfig(
                cig_id=command.cig_id,
                sdu_interval_c_to_p=command.sdu_interval_c_to_p,
                sdu_interval_p_to_c=command.sdu_interval_p_to_c,
                worst_case_sca=command.worst_case_sca,
                packing=command.packing,
                framing=command.framing,
                cis_parameters=tuple(streams),
                **parameters,
            )
        )
        return hci.HCI_LE_Set_CIG_Parameters_ReturnParameters(
            status, command.cig_id, handles
        )

    def on_hci_le_set_cig_parameters_command(self, command):
        streams = (
            CisConfig(*parameters)
            for parameters in zip(
                command.cis_id,
                command.max_sdu_c_to_p,
                command.max_sdu_p_to_c,
                command.phy_c_to_p,
                command.phy_p_to_c,
                command.rtn_c_to_p,
                command.rtn_p_to_c,
                strict=True,
            )
        )
        return self._set_cig_parameters(
            command,
            streams,
            max_transport_latency_c_to_p=command.max_transport_latency_c_to_p,
            max_transport_latency_p_to_c=command.max_transport_latency_p_to_c,
        )

    def on_hci_le_set_cig_parameters_test_command(self, command):
        streams = (
            CisConfig(
                cis_id=cis_id,
                max_sdu_c_to_p=max_sdu_c_to_p,
                max_sdu_p_to_c=max_sdu_p_to_c,
                phy_c_to_p=phy_c_to_p,
                phy_p_to_c=phy_p_to_c,
                nse=nse,
                max_pdu_c_to_p=max_pdu_c_to_p,
                max_pdu_p_to_c=max_pdu_p_to_c,
                bn_c_to_p=bn_c_to_p,
                bn_p_to_c=bn_p_to_c,
            )
            for (
                cis_id,
                nse,
                max_sdu_c_to_p,
                max_sdu_p_to_c,
                max_pdu_c_to_p,
                max_pdu_p_to_c,
                phy_c_to_p,
                phy_p_to_c,
                bn_c_to_p,
                bn_p_to_c,
            ) in zip(
                command.cis_id,
                command.nse,
                command.max_sdu_c_to_p,
                command.max_sdu_p_to_c,
                command.max_pdu_c_to_p,
                command.max_pdu_p_to_c,
                command.phy_c_to_p,
                command.phy_p_to_c,
                command.bn_c_to_p,
                command.bn_p_to_c,
                strict=True,
            )
        )
        return self._set_cig_parameters(
            command,
            streams,
            max_transport_latency_c_to_p=0,
            max_transport_latency_p_to_c=0,
            iso_interval=command.iso_interval,
            ft_c_to_p=command.ft_c_to_p,
            ft_p_to_c=command.ft_p_to_c,
        )

    def on_hci_le_create_cis_command(self, command):
        status = self.ll.create_cis(
            tuple(
                zip(
                    command.cis_connection_handle,
                    command.acl_connection_handle,
                    strict=True,
                )
            )
        )
        self.controller._send_hci_command_status(status, command.op_code)

    def on_hci_le_remove_cig_command(self, command):
        return hci.HCI_LE_Remove_CIG_ReturnParameters(
            self.ll.remove_cig(command.cig_id), command.cig_id
        )

    def on_hci_le_accept_cis_request_command(self, command):
        status = self.ll.accept_cis(command.connection_handle)
        self.controller._send_hci_command_status(status, command.op_code)

    def on_hci_le_reject_cis_request_command(self, command):
        status = self.ll.reject_cis(command.connection_handle, command.reason)
        return hci.HCI_StatusAndConnectionHandleReturnParameters(
            status, command.connection_handle
        )

    def _create_big(self, command, test=None):
        status = self.ll.create_big(
            BigConfig(
                handle=command.big_handle,
                advertising_handle=command.advertising_handle,
                num_bis=command.num_bis,
                sdu_interval_us=command.sdu_interval,
                max_sdu=command.max_sdu,
                phy=command.phy,
                packing=command.packing,
                framing=command.framing,
                encryption=command.encryption,
                broadcast_code=command.broadcast_code,
                max_transport_latency_ms=command.max_transport_latency
                if test is None
                else 0,
                rtn=command.rtn if test is None else 0,
                test=test,
            )
        )
        self.controller._send_hci_command_status(status, command.op_code)

    def on_hci_le_create_big_command(self, command):
        self._create_big(command)

    def on_hci_le_create_big_test_command(self, command):
        self._create_big(
            command,
            BigTestParameters(
                command.iso_interval,
                command.nse,
                command.max_pdu,
                command.bn,
                command.irc,
                command.pto,
            ),
        )

    def on_hci_le_terminate_big_command(self, command):
        status = self.ll.terminate_big(command.big_handle, command.reason)
        self.controller._send_hci_command_status(status, command.op_code)

    def on_hci_le_big_create_sync_command(self, command):
        status = self.ll.synchronize_big(
            BigSyncConfig(
                command.big_handle,
                command.sync_handle,
                command.encryption,
                command.broadcast_code,
                command.mse,
                command.big_sync_timeout,
                tuple(command.bis),
            )
        )
        self.controller._send_hci_command_status(status, command.op_code)

    def on_hci_le_big_terminate_sync_command(self, command):
        status = self.ll.terminate_big_sync(command.big_handle)
        return hci.HCI_LE_BIG_Terminate_Sync_ReturnParameters(
            status, command.big_handle
        )

    def on_hci_le_setup_iso_data_path_command(self, command):
        status = self.ll.setup_iso_data_path(
            command.connection_handle,
            command.data_path_direction,
            command.data_path_id,
            command.codec_id.codec_id,
            command.codec_configuration,
            command.controller_delay,
        )
        return hci.HCI_StatusAndConnectionHandleReturnParameters(
            status, command.connection_handle
        )

    def on_hci_le_remove_iso_data_path_command(self, command):
        status = self.ll.remove_iso_data_path(
            command.connection_handle, command.data_path_direction
        )
        return hci.HCI_StatusAndConnectionHandleReturnParameters(
            status, command.connection_handle
        )

    def on_hci_le_read_iso_tx_sync_command(self, command):
        status, sync = self.ll.iso_tx_sync(command.connection_handle)
        return hci.HCI_LE_Read_ISO_TX_Sync_ReturnParameters(
            status,
            command.connection_handle,
            sync.sequence_number if sync else 0,
            sync.timestamp_us & 0xFFFFFFFF if sync else 0,
            sync.offset_us if sync else 0,
        )

    def on_hci_le_read_iso_link_quality_command(self, command):
        status, counters = self.ll.iso_link_quality(command.connection_handle)
        return hci.HCI_LE_Read_ISO_Link_Quality_ReturnParameters(
            status, command.connection_handle, *counters
        )

    def on_hci_le_iso_transmit_test_command(self, command):
        status = self.ll.set_iso_test(
            command.connection_handle, True, command.payload_type
        )
        return hci.HCI_StatusAndConnectionHandleReturnParameters(
            status, command.connection_handle
        )

    def on_hci_le_iso_receive_test_command(self, command):
        status = self.ll.set_iso_test(
            command.connection_handle, False, command.payload_type
        )
        return hci.HCI_StatusAndConnectionHandleReturnParameters(
            status, command.connection_handle
        )

    def on_hci_le_iso_read_test_counters_command(self, command):
        status, counters = self.ll.iso_test_counters(command.connection_handle)
        return hci.HCI_LE_ISO_Read_Test_Counters_ReturnParameters(
            status,
            command.connection_handle,
            counters.received,
            counters.missed,
            counters.failed,
        )

    def on_hci_le_iso_test_end_command(self, command):
        status, counters = self.ll.iso_test_counters(
            command.connection_handle, end=True
        )
        return hci.HCI_LE_ISO_Read_Test_Counters_ReturnParameters(
            status,
            command.connection_handle,
            counters.received,
            counters.missed,
            counters.failed,
        )

    def on_iso_packet(self, packet):
        self.ll.receive_iso_packet(
            packet.connection_handle,
            packet.iso_sdu_fragment,
            packet.pb_flag,
            packet.packet_sequence_number,
            packet.iso_sdu_length,
            packet.time_stamp,
        )
