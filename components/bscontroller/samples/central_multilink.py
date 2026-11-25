# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""A Bumble Central reads and subscribes to several peripherals at once."""

import asyncio
from contextlib import AsyncExitStack

from bumble import data_types, hci
from bumble.core import AdvertisingData
from bumble.device import Peer
from bumble.profiles.battery_service import BatteryService, BatteryServiceProxy

from . import runtime


def device_names(peripherals=3):
    return ('central', *(f'peripheral-{index}' for index in range(peripherals)))


async def run(devices):
    central = devices['central']
    peripherals = [device for name, device in devices.items() if name != 'central']
    levels = dict.fromkeys((device.name for device in peripherals), 100)
    async with AsyncExitStack() as stack:
        links = []
        for peripheral in peripherals:
            name = peripheral.name
            service = BatteryService(lambda _, name=name: levels[name])
            peripheral.add_service(service)
            await peripheral.start_advertising(
                own_address_type=hci.OwnAddressType.PUBLIC,
                advertising_data=bytes(
                    AdvertisingData([data_types.CompleteLocalName(name)])
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
            assert battery is not None, f'Battery Service missing on {name}'
            assert await battery.battery_level.read_value() == levels[name]
            notifications = asyncio.Queue()
            await battery.battery_level.subscribe(notifications.put_nowait)
            links.append((peripheral, service, battery, notifications))

        assert len(central.connections) == len(peripherals)
        for peripheral, service, _, _ in links:
            levels[peripheral.name] = 42
            await peripheral.notify_subscribers(
                service.battery_level_characteristic, 42
            )
        for _, _, battery, notifications in links:
            assert await notifications.get() == 42
            assert await battery.battery_level.read_value() == 42

    return {
        'connections': len(links),
        'notifications': len(links),
        'gatt_reads': 2 * len(links),
    }


def main():
    parser = runtime.argument_parser(__doc__)
    parser.add_argument('--peripherals', type=runtime.positive_int, default=3)
    args = parser.parse_args()
    runtime.run(run, device_names(args.peripherals), args)


if __name__ == '__main__':
    main()
