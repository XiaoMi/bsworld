# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Reduce an LE connection interval to 5 ms and use GATT on the new schedule."""

import struct

from bumble import hci
from bumble.device import AdvertisingEventProperties, AdvertisingParameters, Peer

from . import runtime


async def run(devices):
    central, peripheral = devices['central'], devices['peripheral']
    for device in (central, peripheral):
        # LE Set Host Feature v2 carries a two-octet bit number.
        response = await device.send_sync_command_raw(
            hci.HCI_Command(
                op_code=hci.HCI_LE_SET_HOST_FEATURE_V2_COMMAND,
                parameters=struct.pack(
                    '<HB', hci.LeFeature.SHORTER_CONNECTION_INTERVALS_HOST_SUPPORT, 1
                ),
            )
        )
        assert bytes(response)[6] == hci.HCI_SUCCESS
        await device.send_sync_command(
            hci.HCI_LE_Set_Default_Rate_Parameters_Command(
                connection_interval_min=10,
                connection_interval_max=240,
                subrate_min=1,
                subrate_max=1,
                max_latency=0,
                continuation_number=0,
                supervision_timeout=400,
                min_ce_length=1,
                max_ce_length=240,
            )
        )

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
    old_interval = connection.parameters.connection_interval
    # Exchange an ATT request so the ACL is established before requesting a rate.
    peer = Peer(connection)
    assert await peer.discover_services()

    await connection.update_parameters_with_subrate(
        connection_interval_min=5,
        connection_interval_max=5,
        subrate_min=1,
        subrate_max=1,
        max_latency=0,
        continuation_number=0,
        supervision_timeout=4_000,
        min_ce_length=0.125,
        max_ce_length=5,
    )
    assert await peer.discover_services()
    new_interval = connection.parameters.connection_interval
    peer_connection = next(iter(peripheral.connections.values()))
    assert new_interval == peer_connection.parameters.connection_interval == 5

    await connection.disconnect()
    await advertiser.remove()
    return {'before_ms': old_interval, 'after_ms': new_interval}


if __name__ == '__main__':
    runtime.main(run, ('central', 'peripheral'), __doc__)
