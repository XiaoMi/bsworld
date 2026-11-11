# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Link backend selection; HCI decoding and responses belong to Controller."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from bscontroller import hci
from bscontroller.hci.capabilities import HCI_SUPPORTED_STATE_MASK
from bscontroller.hci.le import LeControllerProcedures
from bscontroller.radio.base import Radio

if TYPE_CHECKING:
    from bscontroller.hci.controller import Controller


class ControllerBackend(Protocol):
    @property
    def supported_commands(self) -> frozenset[int]: ...

    def bind(self, controller: Controller) -> None: ...


class RadioBackend:
    """Execute LE procedures through a physical-channel simulator."""

    le_features = (
        hci.LeFeatureMask.LE_ENCRYPTION
        | hci.LeFeatureMask.LL_PRIVACY
        | hci.LeFeatureMask.EXTENDED_SCANNER_FILTER_POLICIES
        | hci.LeFeatureMask.DECISION_BASED_ADVERTISING_FILTERING
        | hci.LeFeatureMask.LE_PING
        | hci.LeFeatureMask.LE_EXTENDED_ADVERTISING
        | hci.LeFeatureMask.LE_PERIODIC_ADVERTISING
        | hci.LeFeatureMask.PERIODIC_ADVERTISING_ADI_SUPPORT
        | hci.LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_SENDER
        | hci.LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_RECIPIENT
        | hci.LeFeatureMask.CHANNEL_SELECTION_ALGORITHM_2
        | hci.LeFeatureMask.CONNECTION_PARAMETERS_REQUEST_PROCEDURE
        | hci.LeFeatureMask.EXTENDED_REJECT_INDICATION
        | hci.LeFeatureMask.LE_2M_PHY
        | hci.LeFeatureMask.LE_CODED_PHY
        | hci.LeFeatureMask.LE_DATA_PACKET_LENGTH_EXTENSION
        | hci.LeFeatureMask.PERIPHERAL_INITIATED_FEATURE_EXCHANGE
        | hci.LeFeatureMask.MINIMUM_NUMBER_OF_USED_CHANNELS_PROCEDURE
    )
    lmp_features = (
        hci.LmpFeatureMask.LE_SUPPORTED_CONTROLLER
        | hci.LmpFeatureMask.BR_EDR_NOT_SUPPORTED
    )
    le_states = HCI_SUPPORTED_STATE_MASK.to_bytes(
        hci.HCI_LE_SUPPORTED_STATES_SIZE, 'little'
    )
    supported_commands: frozenset[int] = frozenset(
        {
            hci.HCI_RESET_COMMAND,
            hci.HCI_SET_EVENT_MASK_COMMAND,
            hci.HCI_SET_EVENT_MASK_PAGE_2_COMMAND,
            hci.HCI_READ_LOCAL_VERSION_INFORMATION_COMMAND,
            hci.HCI_READ_REMOTE_VERSION_INFORMATION_COMMAND,
            hci.HCI_READ_LOCAL_SUPPORTED_COMMANDS_COMMAND,
            hci.HCI_READ_LOCAL_SUPPORTED_FEATURES_COMMAND,
            hci.HCI_READ_BD_ADDR_COMMAND,
            hci.HCI_LE_SET_EVENT_MASK_COMMAND,
            hci.HCI_LE_READ_BUFFER_SIZE_COMMAND,
            hci.HCI_LE_READ_LOCAL_SUPPORTED_FEATURES_COMMAND,
            hci.HCI_LE_READ_SUPPORTED_STATES_COMMAND,
            hci.HCI_LE_SET_RANDOM_ADDRESS_COMMAND,
            hci.HCI_LE_ADD_DEVICE_TO_RESOLVING_LIST_COMMAND,
            hci.HCI_LE_REMOVE_DEVICE_FROM_RESOLVING_LIST_COMMAND,
            hci.HCI_LE_CLEAR_RESOLVING_LIST_COMMAND,
            hci.HCI_LE_READ_RESOLVING_LIST_SIZE_COMMAND,
            hci.HCI_LE_READ_PEER_RESOLVABLE_ADDRESS_COMMAND,
            hci.HCI_LE_READ_LOCAL_RESOLVABLE_ADDRESS_COMMAND,
            hci.HCI_LE_SET_ADDRESS_RESOLUTION_ENABLE_COMMAND,
            hci.HCI_LE_SET_RESOLVABLE_PRIVATE_ADDRESS_TIMEOUT_COMMAND,
            hci.HCI_LE_SET_RESOLVABLE_PRIVATE_ADDRESS_TIMEOUT_V2_COMMAND,
            hci.HCI_LE_SET_PRIVACY_MODE_COMMAND,
            hci.HCI_LE_SET_DECISION_DATA_COMMAND,
            hci.HCI_LE_SET_DECISION_INSTRUCTIONS_COMMAND,
            hci.HCI_LE_READ_FILTER_ACCEPT_LIST_SIZE_COMMAND,
            hci.HCI_LE_CLEAR_FILTER_ACCEPT_LIST_COMMAND,
            hci.HCI_LE_ADD_DEVICE_TO_FILTER_ACCEPT_LIST_COMMAND,
            hci.HCI_LE_REMOVE_DEVICE_FROM_FILTER_ACCEPT_LIST_COMMAND,
            hci.HCI_LE_SET_ADVERTISING_PARAMETERS_COMMAND,
            hci.HCI_LE_READ_ADVERTISING_PHYSICAL_CHANNEL_TX_POWER_COMMAND,
            hci.HCI_LE_SET_ADVERTISING_DATA_COMMAND,
            hci.HCI_LE_SET_SCAN_RESPONSE_DATA_COMMAND,
            hci.HCI_LE_SET_ADVERTISING_ENABLE_COMMAND,
            hci.HCI_LE_SET_SCAN_PARAMETERS_COMMAND,
            hci.HCI_LE_SET_SCAN_ENABLE_COMMAND,
            hci.HCI_LE_CREATE_CONNECTION_COMMAND,
            hci.HCI_LE_CREATE_CONNECTION_CANCEL_COMMAND,
            hci.HCI_DISCONNECT_COMMAND,
            hci.HCI_READ_RSSI_COMMAND,
            hci.HCI_LE_ENABLE_ENCRYPTION_COMMAND,
            hci.HCI_LE_LONG_TERM_KEY_REQUEST_REPLY_COMMAND,
            hci.HCI_LE_LONG_TERM_KEY_REQUEST_NEGATIVE_REPLY_COMMAND,
            hci.HCI_LE_ENCRYPT_COMMAND,
            hci.HCI_LE_RAND_COMMAND,
            hci.HCI_READ_AUTHENTICATED_PAYLOAD_TIMEOUT_COMMAND,
            hci.HCI_WRITE_AUTHENTICATED_PAYLOAD_TIMEOUT_COMMAND,
            hci.HCI_LE_READ_REMOTE_FEATURES_COMMAND,
            hci.HCI_LE_CONNECTION_UPDATE_COMMAND,
            hci.HCI_LE_REMOTE_CONNECTION_PARAMETER_REQUEST_REPLY_COMMAND,
            hci.HCI_LE_REMOTE_CONNECTION_PARAMETER_REQUEST_NEGATIVE_REPLY_COMMAND,
            hci.HCI_LE_SET_HOST_CHANNEL_CLASSIFICATION_COMMAND,
            hci.HCI_LE_READ_CHANNEL_MAP_COMMAND,
            hci.HCI_LE_READ_PHY_COMMAND,
            hci.HCI_LE_SET_DEFAULT_PHY_COMMAND,
            hci.HCI_LE_SET_PHY_COMMAND,
            hci.HCI_LE_SET_DATA_LENGTH_COMMAND,
            hci.HCI_LE_READ_SUGGESTED_DEFAULT_DATA_LENGTH_COMMAND,
            hci.HCI_LE_WRITE_SUGGESTED_DEFAULT_DATA_LENGTH_COMMAND,
            hci.HCI_LE_READ_MAXIMUM_DATA_LENGTH_COMMAND,
            hci.HCI_LE_SET_ADVERTISING_SET_RANDOM_ADDRESS_COMMAND,
            hci.HCI_LE_SET_EXTENDED_ADVERTISING_PARAMETERS_COMMAND,
            hci.HCI_LE_SET_EXTENDED_ADVERTISING_DATA_COMMAND,
            hci.HCI_LE_SET_EXTENDED_SCAN_RESPONSE_DATA_COMMAND,
            hci.HCI_LE_SET_EXTENDED_ADVERTISING_ENABLE_COMMAND,
            hci.HCI_LE_READ_MAXIMUM_ADVERTISING_DATA_LENGTH_COMMAND,
            hci.HCI_LE_READ_NUMBER_OF_SUPPORTED_ADVERTISING_SETS_COMMAND,
            hci.HCI_LE_REMOVE_ADVERTISING_SET_COMMAND,
            hci.HCI_LE_CLEAR_ADVERTISING_SETS_COMMAND,
            hci.HCI_LE_SET_EXTENDED_SCAN_PARAMETERS_COMMAND,
            hci.HCI_LE_SET_EXTENDED_SCAN_ENABLE_COMMAND,
            hci.HCI_LE_EXTENDED_CREATE_CONNECTION_COMMAND,
            hci.HCI_LE_SET_PERIODIC_ADVERTISING_PARAMETERS_COMMAND,
            hci.HCI_LE_SET_PERIODIC_ADVERTISING_DATA_COMMAND,
            hci.HCI_LE_SET_PERIODIC_ADVERTISING_ENABLE_COMMAND,
            hci.HCI_LE_PERIODIC_ADVERTISING_CREATE_SYNC_COMMAND,
            hci.HCI_LE_PERIODIC_ADVERTISING_CREATE_SYNC_CANCEL_COMMAND,
            hci.HCI_LE_PERIODIC_ADVERTISING_TERMINATE_SYNC_COMMAND,
            hci.HCI_LE_SET_PERIODIC_ADVERTISING_RECEIVE_ENABLE_COMMAND,
            hci.HCI_LE_READ_PERIODIC_ADVERTISER_LIST_SIZE_COMMAND,
            hci.HCI_LE_ADD_DEVICE_TO_PERIODIC_ADVERTISER_LIST_COMMAND,
            hci.HCI_LE_REMOVE_DEVICE_FROM_PERIODIC_ADVERTISER_LIST_COMMAND,
            hci.HCI_LE_CLEAR_PERIODIC_ADVERTISER_LIST_COMMAND,
            hci.HCI_LE_PERIODIC_ADVERTISING_SYNC_TRANSFER_COMMAND,
            hci.HCI_LE_PERIODIC_ADVERTISING_SET_INFO_TRANSFER_COMMAND,
            hci.HCI_LE_SET_PERIODIC_ADVERTISING_SYNC_TRANSFER_PARAMETERS_COMMAND,
            hci.HCI_LE_SET_DEFAULT_PERIODIC_ADVERTISING_SYNC_TRANSFER_PARAMETERS_COMMAND,
        }
    )

    def __init__(self, radio: Radio):
        self.radio = radio

    def bind(self, controller: Controller) -> None:
        if hasattr(self, 'link'):
            raise ValueError('a backend belongs to one Controller')
        controller.supported_commands = set(self.supported_commands)
        controller.le_features = self.le_features
        controller.lmp_features = self.lmp_features
        controller.le_states = self.le_states
        controller.le_procedures = LeControllerProcedures(controller, self.radio)
        self.link = controller.le_procedures.link
