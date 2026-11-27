# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""HCI commands for LE transmit power and path loss reporting."""

from bscontroller import hci
from bscontroller.hci.context import SUCCESS, LeControllerContext


class LePowerControlCommands(LeControllerContext):
    def on_hci_le_read_rf_path_compensation_command(self, _command):
        """Read antenna path gains in 0.1 dB (Core Vol 4, Part E, 7.8.75)."""
        tx, rx = self.ll.read_rf_path_compensation()
        return hci.HCI_LE_Read_RF_Path_Compensation_ReturnParameters(SUCCESS, tx, rx)

    def on_hci_le_write_rf_path_compensation_command(self, command):
        """Set antenna path gains in 0.1 dB (Core Vol 4, Part E, 7.8.76)."""
        return hci.HCI_StatusReturnParameters(
            self.ll.set_rf_path_compensation(
                command.rf_tx_path_compensation_value,
                command.rf_rx_path_compensation_value,
            )
        )

    def on_hci_le_enhanced_read_transmit_power_level_command(self, command):
        """Read the per-PHY local ACL power (Core Vol 4, Part E, 7.8.117)."""
        status, current, maximum = self.ll.read_local_transmit_power(
            command.connection_handle, command.phy
        )
        return hci.HCI_LE_Enhanced_Read_Transmit_Power_Level_ReturnParameters(
            status, command.connection_handle, command.phy, current, maximum
        )

    def on_hci_le_read_remote_transmit_power_level_command(self, command):
        """Request the peer's transmit power (Core Vol 4, Part E, 7.8.118)."""
        return self.ll.prepare_read_remote_transmit_power(
            command.connection_handle, command.phy
        )

    def on_hci_le_set_path_loss_reporting_parameters_command(self, command):
        """Configure report boundaries and dwell time (Core Vol 4, Part E, 7.8.119)."""
        status = self.ll.set_path_loss_parameters(
            command.connection_handle,
            command.high_threshold,
            command.high_hysteresis,
            command.low_threshold,
            command.low_hysteresis,
            command.min_time_spent,
        )
        return hci.HCI_StatusAndConnectionHandleReturnParameters(
            status, command.connection_handle
        )

    def on_hci_le_set_path_loss_reporting_enable_command(self, command):
        """Control reports for this ACL (Core Vol 4, Part E, 7.8.120)."""
        status = self.ll.set_path_loss_enabled(
            command.connection_handle, command.enable
        )
        return hci.HCI_StatusAndConnectionHandleReturnParameters(
            status, command.connection_handle
        )

    def on_hci_le_set_transmit_power_reporting_enable_command(self, command):
        """Select local and remote reports (Core Vol 4, Part E, 7.8.121)."""
        status = self.ll.set_transmit_power_reporting(
            command.connection_handle, command.local_enable, command.remote_enable
        )
        return hci.HCI_StatusAndConnectionHandleReturnParameters(
            status, command.connection_handle
        )
