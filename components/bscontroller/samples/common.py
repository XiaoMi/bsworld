# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Bumble GAP and Battery Service helpers shared by the examples."""

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass

from bumble import hci
from bumble.core import AdvertisingData
from bumble.device import (
    Device,
    Peer,
    PeriodicAdvertisingSync,
)
from bumble.profiles.battery_service import BatteryService, BatteryServiceProxy


class Reports:
    """Count actual received advertisements by name, with bounded progress waits."""

    def __init__(self, source, event=Device.EVENT_ADVERTISEMENT):
        self.source, self.event = source, event
        self.counts = Counter()
        self.changed = asyncio.Event()

    def _received(self, report):
        name = (
            report.data.get(AdvertisingData.Type.COMPLETE_LOCAL_NAME)
            if report.data
            else None
        )
        if name:
            self.counts[name] += 1
            self.changed.set()

    def __enter__(self):
        self.source.on(self.event, self._received)
        return self

    def __exit__(self, *args):
        self.source.remove_listener(self.event, self._received)

    async def wait(self, names, *, count=1, after=None, timeout=10):
        baseline = after or {}
        async with asyncio.timeout(timeout):
            while any(
                self.counts[name] - baseline.get(name, 0) < count for name in names
            ):
                self.changed.clear()
                await self.changed.wait()


@asynccontextmanager
async def synchronization(observer, advertiser, *, sid, name):
    """Keep receiving periodic reports until this context is closed."""
    sync = await observer.create_periodic_advertising_sync(
        advertiser.public_address,
        sid=sid,
        sync_timeout=5,
    )
    try:
        with Reports(
            sync, PeriodicAdvertisingSync.EVENT_PERIODIC_ADVERTISEMENT
        ) as reports:
            await reports.wait([name])
            assert sync.state == PeriodicAdvertisingSync.State.ESTABLISHED
            yield reports
    finally:
        await sync.terminate()


class BatterySensor:
    def __init__(self, device):
        self.device = device
        self.level = 100
        self.service = BatteryService(lambda _: self.level)
        device.add_service(self.service)

    async def publish(self, level):
        self.level = level
        await self.device.notify_subscribers(
            self.service.battery_level_characteristic, level
        )


@dataclass
class BatteryConnection:
    connection: object
    proxy: BatteryServiceProxy
    notifications: asyncio.Queue

    async def expect(self, level):
        assert await asyncio.wait_for(self.notifications.get(), 10) == level
        assert await self.proxy.battery_level.read_value() == level


@asynccontextmanager
async def battery_connection(central, peripheral):
    connection = await central.connect(
        peripheral.public_address,
        own_address_type=hci.OwnAddressType.PUBLIC,
        timeout=10,
    )
    try:
        proxy = await Peer(connection).discover_service_and_create_proxy(
            BatteryServiceProxy
        )
        assert proxy is not None, 'Battery Service was not discovered'
        assert await proxy.battery_level.read_value() == 100
        notifications = asyncio.Queue()
        await proxy.battery_level.subscribe(notifications.put_nowait)
        yield BatteryConnection(connection, proxy, notifications)
    finally:
        await connection.disconnect()
