# Copyright 2026 Xiaomi Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Bumble central and peripheral connection test."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

from tests.integration.bumble.common import (
    ADVERTISER_NAME,
    TIMEOUT,
    advertising,
    advertising_data,
)


async def test_bumble_central_connects_bumble_peripheral(bumble_pair):
    async with AsyncExitStack() as stack:
        peripheral_connected = asyncio.Event()

        peripheral, central = bumble_pair
        peripheral.on("connection", lambda _: peripheral_connected.set())

        await stack.enter_async_context(
            advertising(
                peripheral,
                connectable=True,
                data=advertising_data(ADVERTISER_NAME),
                interval_min=20.0,
                interval_max=20.0,
            )
        )
        connection = None
        try:
            connection = await central.connect(
                peripheral.public_address, timeout=TIMEOUT
            )
            await asyncio.wait_for(peripheral_connected.wait(), timeout=TIMEOUT)
            assert connection.handle is not None
        finally:
            if connection is not None:
                await connection.disconnect()
