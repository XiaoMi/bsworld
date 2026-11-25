# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Negotiate an LE subrate and read a GATT characteristic on the updated connection."""

from bumble import hci
from bumble.device import AdvertisingEventProperties, AdvertisingParameters, Peer
from bumble.profiles.battery_service import BatteryService, BatteryServiceProxy

from . import runtime


async def run(devices):
    central, peripheral = devices['central'], devices['peripheral']
    for device in (central, peripheral):
        await device.send_sync_command(
            hci.HCI_LE_Set_Host_Feature_Command(
                bit_number=hci.LeFeature.CONNECTION_SUBRATING_HOST_SUPPORT,
                bit_value=1,
            )
        )

    peripheral.add_service(BatteryService(lambda _: 42))
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

    # A factor of three spaces the regular connection events three intervals apart.
    await connection.update_subrate(
        subrate_min=3,
        subrate_max=3,
        max_latency=0,
        continuation_number=1,
        supervision_timeout=5_000,
    )
    assert connection.parameters.subrate_factor == 3
    battery = await Peer(connection).discover_service_and_create_proxy(
        BatteryServiceProxy
    )
    assert battery is not None
    level = await battery.battery_level.read_value()
    assert level == 42

    await connection.disconnect()
    await advertiser.remove()
    return {
        'connection_interval_ms': connection.parameters.connection_interval,
        'subrate_factor': connection.parameters.subrate_factor,
        'battery_level': level,
    }


if __name__ == '__main__':
    runtime.main(run, ('central', 'peripheral'), __doc__)
