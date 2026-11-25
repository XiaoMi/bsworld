# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Discover, read and subscribe to a Battery Service over a real LE connection."""

import asyncio
from contextlib import AsyncExitStack

from bumble import data_types, hci
from bumble.core import AdvertisingData
from bumble.device import Peer
from bumble.profiles.battery_service import BatteryService, BatteryServiceProxy

from . import runtime


async def run(devices):
    central, peripheral = devices['central'], devices['peripheral']
    level = 100
    service = BatteryService(lambda _: level)
    peripheral.add_service(service)

    async with AsyncExitStack() as stack:
        await peripheral.start_advertising(
            own_address_type=hci.OwnAddressType.PUBLIC,
            advertising_data=bytes(
                AdvertisingData([data_types.CompleteLocalName(peripheral.name)])
            ),
        )
        stack.push_async_callback(peripheral.stop_advertising)
        connection = await central.connect(
            peripheral.public_address, own_address_type=hci.OwnAddressType.PUBLIC
        )
        stack.push_async_callback(connection.disconnect)

        battery = await Peer(connection).discover_service_and_create_proxy(
            BatteryServiceProxy
        )
        assert battery is not None, 'Battery Service was not discovered'
        assert await battery.battery_level.read_value() == level

        notifications = asyncio.Queue()
        await battery.battery_level.subscribe(notifications.put_nowait)
        level = 42
        await peripheral.notify_subscribers(service.battery_level_characteristic, level)
        assert await notifications.get() == level
        assert await battery.battery_level.read_value() == level

    return {'connections': 1, 'notifications': 1, 'gatt_reads': 2}


def main():
    runtime.main(run, ('central', 'peripheral'), __doc__)


if __name__ == '__main__':
    main()
