# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Read remote LE transmit power and observe a path loss zone."""

import asyncio
import struct

from bumble import hci
from bumble.device import AdvertisingEventProperties, AdvertisingParameters

from . import runtime

LE_1M = 1
REMOTE_POWER_READ = 2
MIDDLE_ZONE = 1


async def run(devices):
    central, peripheral = devices['central'], devices['peripheral']
    advertiser = await peripheral.create_advertising_set(
        advertising_parameters=AdvertisingParameters(
            advertising_event_properties=AdvertisingEventProperties(
                is_connectable=True
            ),
            own_address_type=hci.OwnAddressType.PUBLIC,
        )
    )
    connection = await central.connect(
        peripheral.public_address, own_address_type=hci.OwnAddressType.PUBLIC
    )
    handle = struct.pack('<H', connection.handle)

    # Bumble dispatches these LE events by name and leaves their payloads as bytes.
    power_reports, path_loss_reports = asyncio.Queue(), asyncio.Queue()
    central.host.on_hci_le_transmit_power_reporting_event = power_reports.put_nowait
    central.host.on_hci_le_path_loss_threshold_event = path_loss_reports.put_nowait
    await central.send_async_command(
        hci.HCI_Command(
            op_code=hci.HCI_LE_READ_REMOTE_TRANSMIT_POWER_LEVEL_COMMAND,
            parameters=handle + bytes([LE_1M]),
        )
    )
    event = await power_reports.get()
    status, reported_handle, reason, phy, tx_power, _, _ = struct.unpack(
        '<BHBBbBb', event.parameters[1:]
    )
    assert (status, reported_handle, reason, phy) == (
        hci.HCI_SUCCESS,
        connection.handle,
        REMOTE_POWER_READ,
        LE_1M,
    )
    assert -127 <= tx_power <= 20

    # Use 70 dB and 20 dB thresholds, 2 dB hysteresis and three connection events.
    await central.send_sync_command(
        hci.HCI_Command(
            op_code=hci.HCI_LE_SET_PATH_LOSS_REPORTING_PARAMETERS_COMMAND,
            parameters=handle + struct.pack('<BBBBH', 70, 2, 20, 2, 3),
        )
    )
    await central.send_sync_command(
        hci.HCI_Command(
            op_code=hci.HCI_LE_SET_PATH_LOSS_REPORTING_ENABLE_COMMAND,
            parameters=handle + b'\x01',
        )
    )
    event = await path_loss_reports.get()
    reported_handle, path_loss, zone = struct.unpack('<HBB', event.parameters[1:])
    assert (reported_handle, zone) == (connection.handle, MIDDLE_ZONE)
    assert 20 < path_loss < 70

    await connection.disconnect()
    await advertiser.remove()
    return {
        'remote_tx_power_dbm': tx_power,
        'path_loss_db': path_loss,
        'zone': 'middle',
    }


if __name__ == '__main__':
    runtime.main(run, ('central', 'peripheral'), __doc__)
