# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""GATT traffic helpers for the Zephyr shell's vendor service."""

import asyncio

from bumble.core import UUID
from bumble.device import Peer
from tests.integration.uml.zephyr.peer import TIMEOUT

_NOTIFICATION_UUID = UUID("12345678-1234-5678-2340-56789abcdef5")


async def verify_notification(shell, connection, payload: bytes) -> None:
    client = Peer(connection)
    assert await client.request_mtu(247) >= len(payload) + 3
    await client.discover_all()
    (characteristic,) = client.get_characteristics_by_uuid(_NOTIFICATION_UUID)
    notifications = asyncio.Queue()
    await client.subscribe(characteristic, notifications.put_nowait)
    await shell.shell.run(f"gatt notify {characteristic.handle:x} {payload.hex()}")
    assert await asyncio.wait_for(notifications.get(), TIMEOUT) == payload
