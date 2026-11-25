# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Read the peer's sleep clock accuracy before and after it improves."""

import asyncio
import struct

from bumble import hci
from bumble.device import AdvertisingEventProperties, AdvertisingParameters

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

    # Bumble dispatches this LE event by name and leaves its payload as bytes.
    reports = asyncio.Queue()
    central.host.on_hci_le_request_peer_sca_complete_event = reports.put_nowait

    async def read_peer_sca():
        await central.send_async_command(
            hci.HCI_Command(
                op_code=hci.HCI_LE_REQUEST_PEER_SCA_COMMAND,
                parameters=struct.pack('<H', connection.handle),
            )
        )
        event = await reports.get()
        status, handle, sca = struct.unpack('<BHB', event.parameters[1:])
        assert (status, handle) == (hci.HCI_SUCCESS, connection.handle)
        return sca

    before = await read_peer_sca()
    await peripheral.send_sync_command(
        hci.HCI_Command(
            op_code=hci.HCI_LE_MODIFY_SLEEP_CLOCK_ACCURACY_COMMAND,
            parameters=b'\x00',  # Improve the local sleep clock accuracy.
        )
    )
    # Give the Link Layer time to exchange the new value before the next read.
    await asyncio.sleep(3 * connection.parameters.connection_interval / 1000)
    after = await read_peer_sca()
    assert (before, after) == (5, 7)  # From 31–50 ppm to 0–20 ppm.

    await connection.disconnect()
    await advertiser.remove()
    return {'peer_sca_before': before, 'peer_sca_after': after}


if __name__ == '__main__':
    runtime.main(run, ('central', 'peripheral'), __doc__)
