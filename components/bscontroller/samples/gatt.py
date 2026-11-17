# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Discover, read and subscribe to a Battery Service over a real LE connection."""

from bscontroller.hosts.bumble import advertising

from . import runtime
from .common import BatterySensor, battery_connection


async def run(devices):
    central, peripheral = devices['central'], devices['peripheral']
    sensor = BatterySensor(peripheral)
    async with advertising(peripheral, connectable=True):
        async with battery_connection(central, peripheral) as link:
            level = 42
            await sensor.publish(level)
            await link.expect(level)
            return {'connections': 1, 'notifications': 1, 'gatt_reads': 2}


def main():
    runtime.main(run, ('central', 'peripheral'), __doc__)


if __name__ == '__main__':
    main()
