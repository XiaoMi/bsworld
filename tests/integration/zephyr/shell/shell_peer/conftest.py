# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Run each shell scenario against five topology and role assignments."""

import pytest
import pytest_asyncio
from tests.integration.zephyr.controllers import require_controller
from tests.integration.zephyr.shell.devices import (
    create_zephyr_firmware_shell,
    create_zephyr_shell,
)
from tests.integration.zephyr.shell_common import initialize_shell

from bsworld.devices import BSController, ZephyrController

TOPOLOGIES = [
    pytest.param((None, False), id="full-full"),
    pytest.param((ZephyrController, False), id="full-host-zephyr-full-peripheral"),
    pytest.param((ZephyrController, True), id="full-host-zephyr-full-central"),
    pytest.param((BSController, False), id="full-host-bscontroller-full-peripheral"),
    pytest.param((BSController, True), id="full-host-bscontroller-full-central"),
]


def _pair(topo, controller_type, full_central):
    if controller_type is None:
        peripheral = create_zephyr_firmware_shell(topo, name="peripheral", seed=41)
        central = create_zephyr_firmware_shell(topo, name="central", seed=42)
        return peripheral, central
    require_controller(controller_type)
    full = create_zephyr_firmware_shell(topo, name="full", seed=41)
    host = create_zephyr_shell(topo, name="host")
    host.attach(
        topo.add_controller(
            controller_type=controller_type, public_address="F0:F1:F2:F3:F4:02"
        )
    )
    return (host, full) if full_central else (full, host)


async def _start(topo, devices):
    topo.set_channel_attenuation(default_db=45)
    await topo.start()
    for device in devices:
        await initialize_shell(device)


@pytest_asyncio.fixture(params=TOPOLOGIES)
async def shell_pair(request, topo):
    peripheral, central = _pair(topo, *request.param)
    await _start(topo, (peripheral, central))
    return peripheral, central


@pytest_asyncio.fixture(params=TOPOLOGIES)
async def shell_trio(request, topo):
    """The shell_pair devices plus a full-firmware shell as a third device."""
    peripheral, central = _pair(topo, *request.param)
    third = create_zephyr_firmware_shell(topo, name="third", seed=43)
    await _start(topo, (peripheral, central, third))
    return peripheral, central, third
