# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Advertise with S=2 on the primary PHY and S=8 on the secondary PHY."""

import asyncio
from contextlib import AsyncExitStack

from bumble import data_types, hci
from bumble.core import AdvertisingData
from bumble.device import Device

from . import runtime


async def run(devices):
    advertiser, scanner = devices['advertiser'], devices['scanner']
    for device in (advertiser, scanner):
        await device.send_sync_command(
            hci.HCI_LE_Set_Host_Feature_Command(
                bit_number=hci.LeFeature.ADVERTISING_CODING_SELECTION_HOST_SUPPORT,
                bit_value=1,
            )
        )

    handle = 0
    data = bytes(AdvertisingData([data_types.CompleteLocalName('coded advertiser')]))
    parameters_v1 = hci.HCI_LE_Set_Extended_Advertising_Parameters_Command(
        advertising_handle=handle,
        advertising_event_properties=0,
        primary_advertising_interval_min=128,
        primary_advertising_interval_max=128,
        primary_advertising_channel_map=0x07,
        own_address_type=hci.OwnAddressType.PUBLIC,
        peer_address_type=0,
        peer_address=hci.Address.ANY,
        advertising_filter_policy=0,
        advertising_tx_power=0x7F,
        primary_advertising_phy=hci.Phy.LE_CODED,
        secondary_advertising_max_skip=0,
        secondary_advertising_phy=hci.Phy.LE_CODED,
        advertising_sid=0,
        scan_request_notification_enable=0,
    )
    # The v2 command appends the primary and secondary PHY options to v1.
    response = await advertiser.send_sync_command_raw(
        hci.HCI_Command(
            op_code=hci.HCI_LE_SET_EXTENDED_ADVERTISING_PARAMETERS_V2_COMMAND,
            parameters=parameters_v1.parameters + b'\x03\x04',  # Require S=2, S=8.
        )
    )
    assert response.parameters[3] == hci.HCI_SUCCESS

    received = asyncio.get_running_loop().create_future()

    def on_advertisement(report):
        if report.data_bytes == data and not received.done():
            received.set_result(report)

    async with AsyncExitStack() as stack:
        stack.push_async_callback(
            advertiser.send_sync_command,
            hci.HCI_LE_Remove_Advertising_Set_Command(handle),
        )
        await advertiser.send_sync_command(
            hci.HCI_LE_Set_Extended_Advertising_Data_Command(
                advertising_handle=handle,
                operation=hci.HCI_LE_Set_Extended_Advertising_Data_Command.Operation.COMPLETE_DATA,
                fragment_preference=parameters_v1.SHOULD_NOT_FRAGMENT,
                advertising_data=data,
            )
        )

        scanner.on(Device.EVENT_ADVERTISEMENT, on_advertisement)
        stack.callback(
            scanner.remove_listener, Device.EVENT_ADVERTISEMENT, on_advertisement
        )
        await scanner.start_scanning(
            active=False,
            own_address_type=hci.OwnAddressType.PUBLIC,
            scan_interval=10,
            scan_window=10,
            scanning_phys=[hci.Phy.LE_CODED],
        )
        stack.push_async_callback(scanner.stop_scanning)

        await advertiser.send_sync_command(
            hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [handle], [0], [0])
        )
        stack.push_async_callback(
            advertiser.send_sync_command,
            hci.HCI_LE_Set_Extended_Advertising_Enable_Command(0, [handle], [0], [0]),
        )
        report = await received
        # The extended advertising report uses 0x04 for S=2 and 0x03 for S=8.
        assert (report.primary_phy, report.secondary_phy) == (0x04, 0x03)

    return {'primary_coding': 2, 'secondary_coding': 8}


if __name__ == '__main__':
    runtime.main(run, ('advertiser', 'scanner'), __doc__)
