# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Change the LE 1M inter-frame space from Central to Peripheral."""

import asyncio
import struct

from bumble import hci
from bumble.device import AdvertisingEventProperties, AdvertisingParameters, Peer

from . import runtime


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

    update = hci.HCI_LE_Frame_Space_Update_Command
    direction = update.SpacingType.T_IFS_ACL_CP
    completion = asyncio.Queue()
    # Bumble dispatches this LE event by name with raw HCI parameters.
    central.host.on_hci_le_frame_space_update_complete_event = completion.put_nowait
    peripheral.host.on_hci_le_frame_space_update_complete_event = lambda event: None
    await central.send_async_command(
        update(connection.handle, 200, 200, hci.PhyBit.LE_1M, direction)
    )
    event = await completion.get()
    status, handle, _, spacing, phy, types = struct.unpack(
        '<BHBHBH', event.parameters[1:]
    )
    assert (status, handle, spacing, phy, types) == (
        hci.HCI_SUCCESS,
        connection.handle,
        200,
        hci.PhyBit.LE_1M,
        direction,
    )
    services = await Peer(connection).discover_services()
    assert services

    await connection.disconnect()
    await advertiser.remove()
    return {'central_to_peripheral_us': spacing, 'services_discovered': len(services)}


if __name__ == '__main__':
    runtime.main(run, ('central', 'peripheral'), __doc__)
