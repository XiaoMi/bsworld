# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Link backend selection; HCI decoding and responses belong to Controller."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from bscontroller import hci
from bscontroller.hci.capabilities import HCI_SUPPORTED_STATE_MASK
from bscontroller.hci.le import LeControllerProcedures
from bscontroller.linklayer.link import RadioLink
from bscontroller.linklayer.notifications import NotificationAdapter
from bscontroller.linklayer.service import LinkLayerService
from bscontroller.radio.base import Radio

if TYPE_CHECKING:
    from bscontroller.address import Address
    from bscontroller.hci.controller import Controller


class ControllerBackend(Protocol):
    @property
    def supported_commands(self) -> frozenset[int]: ...

    @property
    def lmp_features(self) -> hci.LmpFeatureMask: ...

    @property
    def le_states(self) -> bytes: ...

    def bind(
        self, controller: Controller, address: Address
    ) -> LeControllerProcedures: ...


class RadioBackend:
    """Execute LE procedures through a physical-channel simulator."""

    le_features = (
        hci.LeFeatureMask.LE_ENCRYPTION
        | hci.LeFeatureMask.LL_PRIVACY
        | hci.LeFeatureMask.EXTENDED_SCANNER_FILTER_POLICIES
        | hci.LeFeatureMask.DECISION_BASED_ADVERTISING_FILTERING
        | hci.LeFeatureMask.LE_PING
        | hci.LeFeatureMask.LE_EXTENDED_ADVERTISING
        | hci.LeFeatureMask.ADVERTISING_CODING_SELECTION
        | hci.LeFeatureMask.LE_PERIODIC_ADVERTISING
        | hci.LeFeatureMask.PERIODIC_ADVERTISING_ADI_SUPPORT
        | hci.LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_SENDER
        | hci.LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_RECIPIENT
        | hci.LeFeatureMask.PERIODIC_ADVERTISING_WITH_RESPONSES_ADVERTISER
        | hci.LeFeatureMask.PERIODIC_ADVERTISING_WITH_RESPONSES_SCANNER
        | hci.LeFeatureMask.CHANNEL_SELECTION_ALGORITHM_2
        | hci.LeFeatureMask.CONNECTION_PARAMETERS_REQUEST_PROCEDURE
        | hci.LeFeatureMask.EXTENDED_REJECT_INDICATION
        | hci.LeFeatureMask.LE_2M_PHY
        | hci.LeFeatureMask.LE_CODED_PHY
        | hci.LeFeatureMask.LE_DATA_PACKET_LENGTH_EXTENSION
        | hci.LeFeatureMask.PERIPHERAL_INITIATED_FEATURE_EXCHANGE
        | hci.LeFeatureMask.MINIMUM_NUMBER_OF_USED_CHANNELS_PROCEDURE
        | hci.LeFeatureMask.REMOTE_PUBLIC_KEY_VALIDATION
        | hci.LeFeatureMask.LL_EXTENDED_FEATURE_SET
        | hci.LeFeatureMask.MONITORING_ADVERTISERS
        | hci.LeFeatureMask.FRAME_SPACE_UPDATE
        | hci.LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_CENTRAL
        | hci.LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_PERIPHERAL
        | hci.LeFeatureMask.ISOCHRONOUS_BROADCASTER
        | hci.LeFeatureMask.SYNCHRONIZED_RECEIVER
        | hci.LeFeatureMask.SLEEP_CLOCK_ACCURACY_UPDATES
        | hci.LeFeatureMask.LE_POWER_CONTROL_REQUEST
        | hci.LeFeatureMask.LE_POWER_CONTROL_REQUEST_DUP
        | hci.LeFeatureMask.LE_PATH_LOSS_MONITORING
        | hci.LeFeatureMask.CONNECTION_SUBRATING
        | hci.LeFeatureMask.LE_FLUSHABLE_ACL_DATA
        | hci.LeFeatureMask.SHORTER_CONNECTION_INTERVALS
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
            hci.HCI_READ_CONNECTION_ACCEPT_TIMEOUT_COMMAND,
            hci.HCI_WRITE_CONNECTION_ACCEPT_TIMEOUT_COMMAND,
            hci.HCI_READ_LOCAL_VERSION_INFORMATION_COMMAND,
            hci.HCI_READ_REMOTE_VERSION_INFORMATION_COMMAND,
            hci.HCI_READ_LOCAL_SUPPORTED_COMMANDS_COMMAND,
            hci.HCI_READ_LOCAL_SUPPORTED_FEATURES_COMMAND,
            hci.HCI_READ_BD_ADDR_COMMAND,
            hci.HCI_LE_SET_EVENT_MASK_COMMAND,
            hci.HCI_LE_READ_BUFFER_SIZE_COMMAND,
            hci.HCI_LE_READ_BUFFER_SIZE_V2_COMMAND,
            hci.HCI_LE_READ_ISO_TX_SYNC_COMMAND,
            hci.HCI_LE_READ_ISO_LINK_QUALITY_COMMAND,
            hci.HCI_LE_SET_CIG_PARAMETERS_COMMAND,
            hci.HCI_LE_SET_CIG_PARAMETERS_TEST_COMMAND,
            hci.HCI_LE_CREATE_CIS_COMMAND,
            hci.HCI_LE_REMOVE_CIG_COMMAND,
            hci.HCI_LE_ACCEPT_CIS_REQUEST_COMMAND,
            hci.HCI_LE_REJECT_CIS_REQUEST_COMMAND,
            hci.HCI_LE_CREATE_BIG_COMMAND,
            hci.HCI_LE_CREATE_BIG_TEST_COMMAND,
            hci.HCI_LE_TERMINATE_BIG_COMMAND,
            hci.HCI_LE_BIG_CREATE_SYNC_COMMAND,
            hci.HCI_LE_BIG_TERMINATE_SYNC_COMMAND,
            hci.HCI_LE_SETUP_ISO_DATA_PATH_COMMAND,
            hci.HCI_LE_REMOVE_ISO_DATA_PATH_COMMAND,
            hci.HCI_LE_ISO_TRANSMIT_TEST_COMMAND,
            hci.HCI_LE_ISO_RECEIVE_TEST_COMMAND,
            hci.HCI_LE_ISO_READ_TEST_COUNTERS_COMMAND,
            hci.HCI_LE_ISO_TEST_END_COMMAND,
            hci.HCI_LE_READ_LOCAL_SUPPORTED_FEATURES_COMMAND,
            hci.HCI_LE_READ_ALL_LOCAL_SUPPORTED_FEATURES_COMMAND,
            hci.HCI_LE_READ_ALL_REMOTE_FEATURES_COMMAND,
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
            hci.HCI_LE_READ_LOCAL_P_256_PUBLIC_KEY_COMMAND,
            hci.HCI_LE_GENERATE_DHKEY_COMMAND,
            hci.HCI_LE_GENERATE_DHKEY_V2_COMMAND,
            hci.HCI_READ_AUTHENTICATED_PAYLOAD_TIMEOUT_COMMAND,
            hci.HCI_WRITE_AUTHENTICATED_PAYLOAD_TIMEOUT_COMMAND,
            hci.HCI_READ_AUTOMATIC_FLUSH_TIMEOUT_COMMAND,
            hci.HCI_WRITE_AUTOMATIC_FLUSH_TIMEOUT_COMMAND,
            hci.HCI_LE_READ_REMOTE_FEATURES_COMMAND,
            hci.HCI_LE_SET_HOST_FEATURE_COMMAND,
            hci.HCI_LE_SET_HOST_FEATURE_V2_COMMAND,
            hci.HCI_LE_REQUEST_PEER_SCA_COMMAND,
            hci.HCI_LE_MODIFY_SLEEP_CLOCK_ACCURACY_COMMAND,
            hci.HCI_LE_CONNECTION_UPDATE_COMMAND,
            hci.HCI_LE_REMOTE_CONNECTION_PARAMETER_REQUEST_REPLY_COMMAND,
            hci.HCI_LE_REMOTE_CONNECTION_PARAMETER_REQUEST_NEGATIVE_REPLY_COMMAND,
            hci.HCI_LE_SET_DEFAULT_SUBRATE_COMMAND,
            hci.HCI_LE_SUBRATE_REQUEST_COMMAND,
            hci.HCI_LE_CONNECTION_RATE_REQUEST_COMMAND,
            hci.HCI_LE_SET_DEFAULT_RATE_PARAMETERS_COMMAND,
            hci.HCI_LE_READ_MINIMUM_SUPPORTED_CONNECTION_INTERVAL_COMMAND,
            hci.HCI_LE_SET_HOST_CHANNEL_CLASSIFICATION_COMMAND,
            hci.HCI_LE_READ_CHANNEL_MAP_COMMAND,
            hci.HCI_LE_READ_PHY_COMMAND,
            hci.HCI_LE_SET_DEFAULT_PHY_COMMAND,
            hci.HCI_LE_SET_PHY_COMMAND,
            hci.HCI_LE_FRAME_SPACE_UPDATE_COMMAND,
            hci.HCI_LE_SET_DATA_LENGTH_COMMAND,
            hci.HCI_LE_READ_SUGGESTED_DEFAULT_DATA_LENGTH_COMMAND,
            hci.HCI_LE_WRITE_SUGGESTED_DEFAULT_DATA_LENGTH_COMMAND,
            hci.HCI_LE_READ_MAXIMUM_DATA_LENGTH_COMMAND,
            hci.HCI_LE_READ_RF_PATH_COMPENSATION_COMMAND,
            hci.HCI_LE_WRITE_RF_PATH_COMPENSATION_COMMAND,
            hci.HCI_LE_ENHANCED_READ_TRANSMIT_POWER_LEVEL_COMMAND,
            hci.HCI_LE_READ_REMOTE_TRANSMIT_POWER_LEVEL_COMMAND,
            hci.HCI_LE_SET_PATH_LOSS_REPORTING_PARAMETERS_COMMAND,
            hci.HCI_LE_SET_PATH_LOSS_REPORTING_ENABLE_COMMAND,
            hci.HCI_LE_SET_TRANSMIT_POWER_REPORTING_ENABLE_COMMAND,
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
            hci.HCI_LE_SET_EXTENDED_SCAN_PARAMETERS_COMMAND,
            hci.HCI_LE_SET_EXTENDED_SCAN_ENABLE_COMMAND,
            hci.HCI_LE_ADD_DEVICE_TO_MONITORED_ADVERTISERS_LIST_COMMAND,
            hci.HCI_LE_REMOVE_DEVICE_FROM_MONITORED_ADVERTISERS_LIST_COMMAND,
            hci.HCI_LE_CLEAR_MONITORED_ADVERTISERS_LIST_COMMAND,
            hci.HCI_LE_READ_MONITORED_ADVERTISERS_LIST_SIZE_COMMAND,
            hci.HCI_LE_ENABLE_MONITORING_ADVERTISERS_COMMAND,
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
            hci.HCI_LE_SET_PERIODIC_ADVERTISING_PARAMETERS_V2_COMMAND,
            hci.HCI_LE_SET_PERIODIC_ADVERTISING_SUBEVENT_DATA_COMMAND,
            hci.HCI_LE_SET_PERIODIC_ADVERTISING_RESPONSE_DATA_COMMAND,
            hci.HCI_LE_SET_PERIODIC_SYNC_SUBEVENT_COMMAND,
            hci.HCI_LE_EXTENDED_CREATE_CONNECTION_V2_COMMAND,
        }
    )

    def __init__(self, radio: Radio):
        self.radio = radio
        self._link: RadioLink | None = None

    @property
    def link(self) -> RadioLink:
        if self._link is None:
            raise RuntimeError('bind the backend before accessing its radio link')
        return self._link

    def bind(self, controller: Controller, address: Address) -> LeControllerProcedures:
        if self._link is not None:
            raise ValueError('a backend belongs to one Controller')
        procedures = LeControllerProcedures(controller)
        notifications = NotificationAdapter(procedures)
        link = RadioLink(
            self.radio,
            notifications,
            int(self.le_features),
            seed=int.from_bytes(bytes(address), 'little'),
            config=controller.config,
        )
        notifications.bind_advertising(link.roles.advertising.handle_of)
        procedures.ll = LinkLayerService(
            link,
            address,
            hci.HCI_LE_SCAN_INTERVAL_DEFAULT * hci.HCI_LE_SCAN_INTERVAL_UNIT_US,
            hci.HCI_LE_SCAN_WINDOW_DEFAULT * hci.HCI_LE_SCAN_INTERVAL_UNIT_US,
        )
        self._link = link
        return procedures
