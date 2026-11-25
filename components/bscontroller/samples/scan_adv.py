# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Three devices advertise and scan simultaneously."""

import asyncio
from collections import Counter
from contextlib import AsyncExitStack
from functools import partial

from bumble import data_types, hci
from bumble.core import AdvertisingData
from bumble.device import AdvertisingType, Device

from . import runtime


async def run(devices):
    reports = {name: Counter() for name in devices}
    found_every_peer = asyncio.Event()

    def on_advertisement(observer, report):
        name = report.data.get(AdvertisingData.Type.COMPLETE_LOCAL_NAME)
        if (
            name in devices
            and name != observer
            and report.address == devices[name].public_address
        ):
            reports[observer][name] += 1
            if all(len(seen) == len(devices) - 1 for seen in reports.values()):
                found_every_peer.set()

    async with AsyncExitStack() as stack:
        for name, device in devices.items():
            listener = partial(on_advertisement, name)
            device.on(Device.EVENT_ADVERTISEMENT, listener)
            stack.callback(device.remove_listener, Device.EVENT_ADVERTISEMENT, listener)
            await device.start_advertising(
                AdvertisingType.UNDIRECTED,
                own_address_type=hci.OwnAddressType.PUBLIC,
                advertising_data=bytes(
                    AdvertisingData([data_types.CompleteLocalName(name)])
                ),
                advertising_interval_min=40,
                advertising_interval_max=60,
            )
            stack.push_async_callback(device.stop_advertising)
            await device.start_scanning(
                active=False,
                own_address_type=hci.OwnAddressType.PUBLIC,
                scan_interval=60,
                scan_window=30,
                scanning_phys=[hci.Phy.LE_1M],
            )
            stack.push_async_callback(device.stop_scanning)
        await found_every_peer.wait()
        return {'advertisements': {name: dict(seen) for name, seen in reports.items()}}


def main():
    runtime.main(run, ('scanner-a', 'scanner-b', 'scanner-c'), __doc__)


if __name__ == '__main__':
    main()
