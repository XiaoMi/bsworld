# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Zephyr shell topologies with one BSController per Host."""

import pytest_asyncio
from tests.integration.zephyr.shell.devices import create_zephyr_shell
from tests.integration.zephyr.shell_common import initialize_shell


def _pair(topo):
    peripheral = create_zephyr_shell(
        topo, name="peripheral", public_address="F0:F1:F2:F3:F4:01"
    )
    central = create_zephyr_shell(
        topo, name="central", public_address="F0:F1:F2:F3:F4:02"
    )
    return peripheral, central


async def _start(topo, devices):
    topo.set_channel_attenuation(default_db=45)
    await topo.start()
    for device in devices:
        await initialize_shell(device)


@pytest_asyncio.fixture
async def shell_pair(topo):
    devices = _pair(topo)
    await _start(topo, devices)
    return devices


@pytest_asyncio.fixture
async def shell_trio(topo):
    """Add a periodic advertiser to the shell pair."""
    devices = (
        *_pair(topo),
        create_zephyr_shell(
            topo, name="advertiser", public_address="F0:F1:F2:F3:F4:03"
        ),
    )
    await _start(topo, devices)
    return devices
