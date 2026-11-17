# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""A Bumble Central reads and subscribes to several peripherals at once."""

import asyncio
from contextlib import AsyncExitStack

from bscontroller.hosts.bumble import advertising

from . import runtime
from .common import BatterySensor, battery_connection


def device_names(peripherals=3):
    return ('central', *(f'peripheral-{index}' for index in range(peripherals)))


async def run(devices):
    central = devices['central']
    peripherals = {
        name: device for name, device in devices.items() if name != 'central'
    }
    sensors = {name: BatterySensor(device) for name, device in peripherals.items()}
    async with AsyncExitStack() as stack:
        for peripheral in peripherals.values():
            await stack.enter_async_context(advertising(peripheral, connectable=True))
        links = [
            await stack.enter_async_context(battery_connection(central, peripheral))
            for peripheral in peripherals.values()
        ]
        level = 42
        await asyncio.gather(*(sensor.publish(level) for sensor in sensors.values()))
        await asyncio.gather(*(link.expect(level) for link in links))
        assert len(central.connections) == len(peripherals)
        return {
            'connections': len(links),
            'notifications': len(links),
            'gatt_reads': (2) * len(links),
        }


def main():
    parser = runtime.argument_parser(__doc__)
    parser.add_argument('--peripherals', type=runtime.positive_int, default=3)
    args = parser.parse_args()
    runtime.run(run, device_names(args.peripherals), args)


if __name__ == '__main__':
    main()
