# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LE HCI entry point and reset/command-family boundaries."""

from bscontroller import hci
from bscontroller.hci import (
    HCI_EVENT_MASK_DEFAULT,
    HCI_LE_EVENT_MASK_DEFAULT,
)
from bscontroller.hci.advertising import LeAdvertisingCommands
from bscontroller.hci.connection import LeConnectionCommands
from bscontroller.hci.context import SUCCESS
from bscontroller.hci.iso import LeIsoCommands
from bscontroller.hci.power_control import LePowerControlCommands
from bscontroller.hci.privacy import LePrivacyCommands
from bscontroller.hci.records import LegacyAdvertiser
from bscontroller.hci.scanning import LeScanningCommands

# Core Vol 4, Part E, 3.1.1: one advertising command family per reset.
LEGACY_ADVERTISING_COMMANDS = frozenset(
    (
        hci.HCI_LE_SET_ADVERTISING_PARAMETERS_COMMAND,
        hci.HCI_LE_READ_ADVERTISING_PHYSICAL_CHANNEL_TX_POWER_COMMAND,
        hci.HCI_LE_SET_ADVERTISING_DATA_COMMAND,
        hci.HCI_LE_SET_SCAN_RESPONSE_DATA_COMMAND,
        hci.HCI_LE_SET_ADVERTISING_ENABLE_COMMAND,
        hci.HCI_LE_SET_SCAN_PARAMETERS_COMMAND,
        hci.HCI_LE_SET_SCAN_ENABLE_COMMAND,
        hci.HCI_LE_CREATE_CONNECTION_COMMAND,
    )
)

# The extended family includes scanning, initiating and periodic sync.
EXTENDED_ADVERTISING_COMMANDS = frozenset(
    (
        hci.HCI_LE_SET_ADVERTISING_SET_RANDOM_ADDRESS_COMMAND,
        hci.HCI_LE_SET_EXTENDED_ADVERTISING_PARAMETERS_COMMAND,
        hci.HCI_LE_SET_EXTENDED_ADVERTISING_PARAMETERS_V2_COMMAND,
        hci.HCI_LE_SET_EXTENDED_ADVERTISING_DATA_COMMAND,
        hci.HCI_LE_SET_EXTENDED_SCAN_RESPONSE_DATA_COMMAND,
        hci.HCI_LE_SET_EXTENDED_ADVERTISING_ENABLE_COMMAND,
        hci.HCI_LE_READ_MAXIMUM_ADVERTISING_DATA_LENGTH_COMMAND,
        hci.HCI_LE_READ_NUMBER_OF_SUPPORTED_ADVERTISING_SETS_COMMAND,
        hci.HCI_LE_REMOVE_ADVERTISING_SET_COMMAND,
        hci.HCI_LE_CLEAR_ADVERTISING_SETS_COMMAND,
        hci.HCI_LE_SET_PERIODIC_ADVERTISING_PARAMETERS_COMMAND,
        hci.HCI_LE_SET_PERIODIC_ADVERTISING_DATA_COMMAND,
        hci.HCI_LE_SET_PERIODIC_ADVERTISING_ENABLE_COMMAND,
        hci.HCI_LE_SET_EXTENDED_SCAN_PARAMETERS_COMMAND,
        hci.HCI_LE_SET_EXTENDED_SCAN_ENABLE_COMMAND,
        hci.HCI_LE_EXTENDED_CREATE_CONNECTION_COMMAND,
        hci.HCI_LE_PERIODIC_ADVERTISING_CREATE_SYNC_COMMAND,
        hci.HCI_LE_PERIODIC_ADVERTISING_CREATE_SYNC_CANCEL_COMMAND,
        hci.HCI_LE_PERIODIC_ADVERTISING_TERMINATE_SYNC_COMMAND,
        hci.HCI_LE_ADD_DEVICE_TO_PERIODIC_ADVERTISER_LIST_COMMAND,
        hci.HCI_LE_REMOVE_DEVICE_FROM_PERIODIC_ADVERTISER_LIST_COMMAND,
        hci.HCI_LE_CLEAR_PERIODIC_ADVERTISER_LIST_COMMAND,
        hci.HCI_LE_READ_PERIODIC_ADVERTISER_LIST_SIZE_COMMAND,
        hci.HCI_LE_SET_PERIODIC_ADVERTISING_SYNC_TRANSFER_PARAMETERS_COMMAND,
        hci.HCI_LE_SET_DEFAULT_PERIODIC_ADVERTISING_SYNC_TRANSFER_PARAMETERS_COMMAND,
        hci.HCI_LE_SET_PERIODIC_ADVERTISING_PARAMETERS_V2_COMMAND,
        hci.HCI_LE_SET_PERIODIC_ADVERTISING_SUBEVENT_DATA_COMMAND,
        hci.HCI_LE_SET_PERIODIC_ADVERTISING_RESPONSE_DATA_COMMAND,
        hci.HCI_LE_SET_PERIODIC_SYNC_SUBEVENT_COMMAND,
        hci.HCI_LE_EXTENDED_CREATE_CONNECTION_V2_COMMAND,
    )
)


class LeControllerProcedures(
    LeAdvertisingCommands,
    LeScanningCommands,
    LeConnectionCommands,
    LePrivacyCommands,
    LeIsoCommands,
    LePowerControlCommands,
):
    """One command endpoint, grouped by the LE procedures each command configures."""

    def command_allowed(self, opcode: int) -> bool:
        if opcode in LEGACY_ADVERTISING_COMMANDS:
            extended = False
        elif opcode in EXTENDED_ADVERTISING_COMMANDS:
            extended = True
        else:
            return True
        if self.extended_commands is None:
            self.extended_commands = extended
        return self.extended_commands == extended

    def on_hci_reset_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.3.2 Reset Command
        '''
        # Drop active LL procedures first, then remove Host-facing views and HCI
        # staging buffers so no record can refer to a pre-reset connection or train.
        self.ll.reset()
        self.acl_buffers.reset()
        self.iso_buffers.reset()
        self.iso_assemblies.clear()
        self.cis_established.clear()
        self.deferred_cis_credits.clear()
        self.controller.le_connections.clear()
        self.controller.advertising_sets.clear()
        self.fragments.clear()
        self.duplicate_filter = None
        # Recreate the record to restore parameters as well as payload/enabled state.
        self.controller.le_legacy_advertiser = LegacyAdvertiser(self.controller)
        self.controller.event_mask = HCI_EVENT_MASK_DEFAULT
        self.controller.event_mask_page_2 = 0
        self.controller.le_event_mask = HCI_LE_EVENT_MASK_DEFAULT
        self.extended_commands = None
        self._p256_private_key = None
        return hci.HCI_StatusReturnParameters(SUCCESS)

    def on_hci_le_set_random_address_command(self, command):
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.4 LE Set Random Address Command
        '''
        return hci.HCI_StatusReturnParameters(
            self.ll.set_random_address(command.random_address)
        )
