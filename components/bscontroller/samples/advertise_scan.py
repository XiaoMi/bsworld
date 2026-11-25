# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Bumble broadcaster and observer over two BSControllers."""

import asyncio
from contextlib import AsyncExitStack

from bumble import data_types, hci
from bumble.core import AdvertisingData
from bumble.device import AdvertisingType, Device

from . import runtime


async def run(devices):
    advertiser, observer = devices['advertiser'], devices['observer']
    found = asyncio.Event()

    def on_advertisement(report):
        if (
            report.address == advertiser.public_address
            and report.data.get(AdvertisingData.Type.COMPLETE_LOCAL_NAME)
            == advertiser.name
        ):
            found.set()

    async with AsyncExitStack() as stack:
        observer.on(Device.EVENT_ADVERTISEMENT, on_advertisement)
        stack.callback(
            observer.remove_listener, Device.EVENT_ADVERTISEMENT, on_advertisement
        )
        await advertiser.start_advertising(
            AdvertisingType.UNDIRECTED,
            own_address_type=hci.OwnAddressType.PUBLIC,
            advertising_data=bytes(
                AdvertisingData([data_types.CompleteLocalName(advertiser.name)])
            ),
            advertising_interval_min=40,
            advertising_interval_max=60,
        )
        stack.push_async_callback(advertiser.stop_advertising)
        await observer.start_scanning(
            active=False,
            own_address_type=hci.OwnAddressType.PUBLIC,
            scan_interval=60,
            scan_window=30,
            scanning_phys=[hci.Phy.LE_1M],
        )
        stack.push_async_callback(observer.stop_scanning)
        await found.wait()
    return {'advertisements': {advertiser.name: 1}, 'host': 'bumble'}


def main():
    runtime.main(run, ('advertiser', 'observer'), __doc__)


if __name__ == '__main__':
    main()
