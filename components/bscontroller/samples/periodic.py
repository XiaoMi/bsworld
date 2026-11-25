# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Keep two periodic advertising synchronizations on one observer."""

import asyncio
from contextlib import AsyncExitStack

from bumble.core import AdvertisingData
from bumble.device import (
    AdvertisingEventProperties,
    AdvertisingParameters,
    PeriodicAdvertisingParameters,
)
from bumble.hci import OwnAddressType, Phy

from . import runtime

NAMES = ('periodic-a', 'periodic-b', 'observer')
LOCAL_NAME = AdvertisingData.Type.COMPLETE_LOCAL_NAME


async def run(devices):
    observer = devices['observer']
    sources = [devices[name] for name in NAMES[:-1]]
    async with AsyncExitStack() as stack:
        for sid, source in enumerate(sources, 1):
            data = bytes(AdvertisingData([(LOCAL_NAME, source.name.encode())]))
            interval = 80 + sid * 20
            advertiser = await source.create_advertising_set(
                advertising_parameters=AdvertisingParameters(
                    advertising_event_properties=AdvertisingEventProperties(
                        is_connectable=False
                    ),
                    own_address_type=OwnAddressType.PUBLIC,
                    advertising_sid=sid,
                    primary_advertising_interval_min=40,
                    primary_advertising_interval_max=60,
                ),
                advertising_data=data,
                periodic_advertising_parameters=PeriodicAdvertisingParameters(
                    interval, interval
                ),
                periodic_advertising_data=data,
            )
            stack.push_async_callback(advertiser.remove)
            stack.push_async_callback(advertiser.stop)
            await advertiser.start_periodic()
            stack.push_async_callback(advertiser.stop_periodic)

        advertisements = asyncio.Queue()
        observer.on(observer.EVENT_ADVERTISEMENT, advertisements.put_nowait)
        await observer.start_scanning(
            active=False,
            own_address_type=OwnAddressType.PUBLIC,
            scan_interval=60,
            scan_window=30,
            scanning_phys=[Phy.LE_1M],
        )
        stack.push_async_callback(observer.stop_scanning)
        unseen = {source.name for source in sources}
        while unseen:
            report = await advertisements.get()
            unseen.discard(report.data.get(LOCAL_NAME))
        observer.remove_listener(
            observer.EVENT_ADVERTISEMENT, advertisements.put_nowait
        )

        reports = {}
        received = {}
        for sid, source in enumerate(sources, 1):
            sync = await observer.create_periodic_advertising_sync(
                source.public_address, sid, sync_timeout=5
            )
            stack.push_async_callback(sync.terminate)
            queue = asyncio.Queue()
            sync.on(sync.EVENT_PERIODIC_ADVERTISEMENT, queue.put_nowait)
            report = await queue.get()
            assert report.data.get(LOCAL_NAME) == source.name
            reports[source.name] = queue
            received[source.name] = 1

        # Check for fresh data from both trains with both syncs active.
        for queue in reports.values():
            while not queue.empty():
                queue.get_nowait()
        for name, queue in reports.items():
            report = await queue.get()
            assert report.data.get(LOCAL_NAME) == name
            received[name] += 1
        return {'syncs': len(reports), 'periodic_reports': received}


def main():
    runtime.main(run, NAMES, __doc__)


if __name__ == '__main__':
    main()
