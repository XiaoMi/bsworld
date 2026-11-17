# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Two Centrals, multiple Peripheral links, advertising, scanning and PA/SYNC.

hub-a and hub-b each connect to both leaves; hub-a also accepts a connection
from hub-b. Both hubs transmit a periodic train while scanning and synchronizing
with the other hub. An independent observer scans and synchronizes with both.
GATT notifications, reads, scan reports and periodic reports must all progress
with all five ACL links still connected.
"""

import asyncio
from contextlib import AsyncExitStack

from bscontroller.hosts.bumble import advertising, scanning

from . import runtime
from .common import BatterySensor, Reports, battery_connection, synchronization

NAMES = ('hub-a', 'hub-b', 'leaf-a', 'leaf-b', 'observer')


async def run(devices):
    servers = {
        name: BatterySensor(devices[name]) for name in ('hub-a', 'leaf-a', 'leaf-b')
    }
    edges = [(hub, leaf) for hub in ('hub-a', 'hub-b') for leaf in ('leaf-a', 'leaf-b')]
    edges.append(('hub-b', 'hub-a'))
    expected_connections = {
        'hub-a': 3,
        'hub-b': 3,
        'leaf-a': 2,
        'leaf-b': 2,
        'observer': 0,
    }
    wanted = {
        'hub-a': ('hub-b-pa', 'leaf-a', 'leaf-b'),
        'hub-b': ('hub-a-pa', 'leaf-a', 'leaf-b'),
        'observer': ('hub-a-pa', 'hub-b-pa', 'leaf-a', 'leaf-b'),
    }
    async with AsyncExitStack() as stack:
        connectable = {
            name: await stack.enter_async_context(
                advertising(devices[name], connectable=True)
            )
            for name in servers
        }
        links = []
        for central, peripheral in edges:
            link = await stack.enter_async_context(
                battery_connection(devices[central], devices[peripheral])
            )
            links.append(link)
            # A successful connection ends that advertising set. Re-enable it
            # to admit the next Central and keep advertising during ACL traffic.
            if not connectable[peripheral].enabled:
                await connectable[peripheral].start()

        periodic = []
        for sid, name in enumerate(('hub-a', 'hub-b'), 1):
            periodic.append(
                await stack.enter_async_context(
                    advertising(
                        devices[name],
                        name=f'{name}-pa',
                        legacy=False,
                        sid=sid,
                        periodic_interval=80 + sid * 20,
                    )
                )
            )
        scans = {name: stack.enter_context(Reports(devices[name])) for name in wanted}
        for name in wanted:
            await stack.enter_async_context(scanning(devices[name]))
        await asyncio.gather(
            *(scans[name].wait(names) for name, names in wanted.items())
        )

        syncs = {}
        for observer, advertiser, sid in (
            ('hub-a', 'hub-b', 2),
            ('hub-b', 'hub-a', 1),
            ('observer', 'hub-a', 1),
            ('observer', 'hub-b', 2),
        ):
            name = f'{advertiser}-pa'
            syncs[(observer, name)] = await stack.enter_async_context(
                synchronization(
                    devices[observer],
                    devices[advertiser],
                    sid=sid,
                    name=name,
                )
            )

        scan_before = {name: reports.counts.copy() for name, reports in scans.items()}
        sync_before = {key: reports.counts.copy() for key, reports in syncs.items()}
        level = 42
        await asyncio.gather(*(sensor.publish(level) for sensor in servers.values()))
        await asyncio.gather(
            *(link.expect(level) for link in links),
            *(
                scans[name].wait(names, after=scan_before[name])
                for name, names in wanted.items()
            ),
            *(
                reports.wait([key[1]], after=sync_before[key])
                for key, reports in syncs.items()
            ),
        )
        assert {
            name: len(device.connections) for name, device in devices.items()
        } == expected_connections
        assert all(item.enabled and item.periodic_enabled for item in periodic)
        assert all(item.enabled for item in connectable.values())

        return {
            'connections': len(links),
            'connections_per_device': expected_connections,
            'periodic_syncs': len(syncs),
            'notifications': len(links),
            'gatt_reads': len(links) * 2,
            'scan_reports': {
                name: dict(reports.counts) for name, reports in scans.items()
            },
            'periodic_reports': {
                f'{observer}/{name}': reports.counts[name]
                for (observer, name), reports in syncs.items()
            },
        }


def main():
    runtime.main(run, NAMES, __doc__)


if __name__ == '__main__':
    main()
