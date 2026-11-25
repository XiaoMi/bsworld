# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Host commands for scripted legacy and extended advertising and scanning."""

from bscontroller import hci
from bscontroller.radio.base import Phy
from tests.unit.linklayer.air.harness import PEER, ErrorCode

AdvType = hci.HCI_LE_Set_Advertising_Parameters_Command.AdvertisingType
LEGACY_INTERVAL = 32  # 20 ms, the minimum legacy interval for undirected advertising
SCAN_WINDOW = 16  # 10 ms
Operation = hci.HCI_LE_Set_Extended_Advertising_Data_Command.Operation
NO_PREFERENCE = (
    hci.HCI_LE_Set_Extended_Advertising_Parameters_Command.TX_POWER_NO_PREFERENCE
)
EXTENDED_INTERVAL = 160  # 100 ms


def configure_legacy(
    iut,
    kind=AdvType.ADV_IND,
    *,
    data=b'',
    scan_response=b'',
    peer=PEER,
    channel_map=0x07,
    policy=0,
    own=hci.OwnAddressType.PUBLIC,
):
    iut.ut(
        hci.HCI_LE_Set_Advertising_Parameters_Command(
            LEGACY_INTERVAL,
            LEGACY_INTERVAL,
            kind,
            own,
            peer.address_type,
            peer,
            channel_map,
            policy,
        )
    )
    iut.ut(hci.HCI_LE_Set_Advertising_Data_Command(data))
    iut.ut(hci.HCI_LE_Set_Scan_Response_Data_Command(scan_response))


def enable_legacy(iut, on=True, status=ErrorCode.SUCCESS):
    iut.ut(hci.HCI_LE_Set_Advertising_Enable_Command(int(on)), status)


def scan_legacy(
    iut,
    *,
    active=False,
    policy=0,
    duplicates=0,
    interval=SCAN_WINDOW,
    window=SCAN_WINDOW,
):
    iut.ut(
        hci.HCI_LE_Set_Scan_Parameters_Command(
            int(active), interval, window, hci.OwnAddressType.PUBLIC, policy
        )
    )
    iut.ut(hci.HCI_LE_Set_Scan_Enable_Command(1, duplicates))


def extended_parameters(handle=0, properties=0, **changes):
    values = dict(
        advertising_handle=handle,
        advertising_event_properties=properties,
        primary_advertising_interval_min=EXTENDED_INTERVAL,
        primary_advertising_interval_max=EXTENDED_INTERVAL,
        primary_advertising_channel_map=7,
        own_address_type=hci.OwnAddressType.PUBLIC,
        peer_address_type=PEER.address_type,
        peer_address=PEER,
        advertising_filter_policy=0,
        advertising_tx_power=NO_PREFERENCE,
        primary_advertising_phy=Phy.LE_1M,
        secondary_advertising_max_skip=0,
        secondary_advertising_phy=Phy.LE_1M,
        advertising_sid=5,
        scan_request_notification_enable=0,
    )
    return hci.HCI_LE_Set_Extended_Advertising_Parameters_Command(**(values | changes))


def configure_extended(
    iut, properties=0, *, data=b'', scan_response=b'', handle=0, **changes
):
    iut.ut(extended_parameters(handle, properties, **changes))
    if data:
        iut.ut(
            hci.HCI_LE_Set_Extended_Advertising_Data_Command(
                handle, Operation.COMPLETE_DATA, 0, data
            )
        )
    if scan_response:
        iut.ut(
            hci.HCI_LE_Set_Extended_Scan_Response_Data_Command(
                handle, Operation.COMPLETE_DATA, 0, scan_response
            )
        )


def enable_extended(
    iut, handle=0, duration=0, max_events=0, on=1, status=ErrorCode.SUCCESS
):
    iut.ut(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(
            on, [handle], [duration], [max_events]
        ),
        status,
    )


def split_primary_auxiliary(packets):
    primary = [packet for packet in packets if packet.channel in (37, 38, 39)]
    auxiliary = [packet for packet in packets if packet.channel not in (37, 38, 39)]
    return primary, auxiliary
