# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Run five ACL links while devices scan, advertise and receive periodic data."""

import asyncio
from collections import Counter
from contextlib import AsyncExitStack
from functools import partial

from bumble.core import AdvertisingData
from bumble.device import (
    AdvertisingEventProperties,
    AdvertisingParameters,
    Peer,
    PeriodicAdvertisingParameters,
)
from bumble.hci import OwnAddressType, Phy
from bumble.profiles.battery_service import BatteryService, BatteryServiceProxy

from . import runtime

NAMES = ('hub-a', 'hub-b', 'leaf-a', 'leaf-b', 'observer')
LOCAL_NAME = AdvertisingData.Type.COMPLETE_LOCAL_NAME


async def run(devices):
    # Each hub connects to both leaves, and hub-b also connects to hub-a.
    edges = [(hub, leaf) for hub in NAMES[:2] for leaf in NAMES[2:4]]
    edges.append(('hub-b', 'hub-a'))
    expected_connections = dict(zip(NAMES, (3, 3, 2, 2, 0), strict=True))
    wanted = {
        'hub-a': {'hub-b-pa', 'leaf-a', 'leaf-b'},
        'hub-b': {'hub-a-pa', 'leaf-a', 'leaf-b'},
        'observer': {'hub-a-pa', 'hub-b-pa', 'leaf-a', 'leaf-b'},
    }
    level = 100
    async with AsyncExitStack() as stack:
        services = {}
        connectable = {}
        for name in ('hub-a', 'leaf-a', 'leaf-b'):
            device = devices[name]
            service = BatteryService(lambda _: level)
            device.add_service(service)
            services[name] = service
            advertiser = await device.create_advertising_set(
                advertising_parameters=AdvertisingParameters(
                    advertising_event_properties=AdvertisingEventProperties(
                        is_connectable=True, is_scannable=True, is_legacy=True
                    ),
                    own_address_type=OwnAddressType.PUBLIC,
                    primary_advertising_interval_min=40,
                    primary_advertising_interval_max=60,
                ),
                advertising_data=bytes(AdvertisingData([(LOCAL_NAME, name.encode())])),
            )
            stack.push_async_callback(advertiser.remove)
            stack.push_async_callback(advertiser.stop)
            connectable[name] = advertiser

        subscriptions = []
        for central, peripheral in edges:
            link = await devices[central].connect(
                devices[peripheral].public_address,
                own_address_type=OwnAddressType.PUBLIC,
            )
            stack.push_async_callback(link.disconnect)
            proxy = await Peer(link).discover_service_and_create_proxy(
                BatteryServiceProxy
            )
            assert proxy is not None
            initial_level = await proxy.battery_level.read_value()
            assert initial_level == level
            notifications = asyncio.Queue()
            await proxy.battery_level.subscribe(notifications.put_nowait)
            subscriptions.append((proxy.battery_level, notifications))
            # The connection consumes the advertising set. Restart it for scanning
            # and for the next Central to connect.
            await connectable[peripheral].start()

        # Each hub advertises a periodic train and listens to the other hub's train.
        periodic = []
        # 83 and 97 units of 1.25 ms move the trains through different gaps
        # between the 15-ms ACL anchors on the receiving hubs.
        for sid, name, interval in ((1, 'hub-a', 103.75), (2, 'hub-b', 121.25)):
            data = bytes(AdvertisingData([(LOCAL_NAME, f'{name}-pa'.encode())]))
            advertiser = await devices[name].create_advertising_set(
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
            periodic.append(advertiser)

        scans = {}
        for name in wanted:
            device = devices[name]
            queue = asyncio.Queue()
            device.on(device.EVENT_ADVERTISEMENT, queue.put_nowait)
            stack.callback(
                device.remove_listener, device.EVENT_ADVERTISEMENT, queue.put_nowait
            )
            await device.start_scanning(
                active=False,
                own_address_type=OwnAddressType.PUBLIC,
                scan_interval=60,
                scan_window=30,
                scanning_phys=[Phy.LE_1M],
            )
            stack.push_async_callback(device.stop_scanning)
            scans[name] = queue

        syncs = {}
        periodic_reports = Counter()
        for observer, advertiser, sid in (
            ('hub-a', 'hub-b', 2),
            ('hub-b', 'hub-a', 1),
            ('observer', 'hub-a', 1),
            ('observer', 'hub-b', 2),
        ):
            sync = await devices[observer].create_periodic_advertising_sync(
                devices[advertiser].public_address, sid, sync_timeout=5
            )
            queue = asyncio.Queue()
            sync.on(sync.EVENT_PERIODIC_ADVERTISEMENT, queue.put_nowait)
            sync.once(sync.EVENT_ESTABLISHMENT_ERROR, partial(queue.put_nowait, None))
            name = f'{advertiser}-pa'
            report = None if sync.state == sync.State.ERROR else await queue.get()
            if report is None:
                raise RuntimeError(
                    f'{observer} sync to {name} failed (HCI 0x{sync.status:02X})'
                )
            stack.push_async_callback(sync.terminate)
            assert report.data.get(LOCAL_NAME) == name
            syncs[(observer, name)] = queue
            periodic_reports[f'{observer}/{name}'] += 1

        # Check that fresh radio reports arrive while all links carry GATT data.
        for queue in (*scans.values(), *syncs.values()):
            while not queue.empty():
                queue.get_nowait()
        level = 42
        await asyncio.gather(
            *(
                devices[name].notify_subscribers(
                    service.battery_level_characteristic, level
                )
                for name, service in services.items()
            )
        )
        for battery_level, notifications in subscriptions:
            notification = await notifications.get()
            current_level = await battery_level.read_value()
            assert notification == level
            assert current_level == level

        scan_reports = {}
        for observer, names in wanted.items():
            counts = Counter()
            while names - counts.keys():
                report = await scans[observer].get()
                name = report.data.get(LOCAL_NAME)
                if name:
                    counts[name] += 1
            scan_reports[observer] = dict(counts)
        for (observer, name), queue in syncs.items():
            report = await queue.get()
            assert report.data.get(LOCAL_NAME) == name
            periodic_reports[f'{observer}/{name}'] += 1

        assert {
            name: len(device.connections) for name, device in devices.items()
        } == expected_connections
        assert all(item.enabled and item.periodic_enabled for item in periodic)
        assert all(item.enabled for item in connectable.values())
        return {
            'connections': len(subscriptions),
            'connections_per_device': expected_connections,
            'periodic_syncs': len(syncs),
            'notifications': len(subscriptions),
            'gatt_reads': len(subscriptions) * 2,
            'scan_reports': scan_reports,
            'periodic_reports': dict(periodic_reports),
        }


def main():
    runtime.main(run, NAMES, __doc__)


if __name__ == '__main__':
    main()
