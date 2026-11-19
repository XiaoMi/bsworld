# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""NuttX NimBLE Battery Service discovery and notifications with Bumble."""

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path

import pytest_asyncio
from bumble import data_types
from bumble.core import UUID, AdvertisingData
from bumble.device import Peer
from bumble.gatt import Characteristic, CharacteristicValue, Service
from tests.integration.uml.bumble_peer import (
    add_bumble_controller,
    advertising,
    advertising_data,
    open_bumble_device,
    wait_for_advertisement,
)
from tests.integration.uml.common import (
    add_uml_controller,
    open_uml_guest,
    require_guest_program,
)
from tests.integration.uml.nuttx.program import start_nuttx

_NIMBLE_ADDRESS = "C0:FF:EE:20:00:01"
_BUMBLE_ADDRESS = "C0:FF:EE:20:01:01"


@pytest_asyncio.fixture
async def nimble_peers(topo):
    program = require_guest_program(
        "BSWORLD_UML_NUTTX_NIMBLE_BIN", "/opt/bsworld/nuttx-nimble"
    )
    log_subdir = Path("uml") / "nuttx"
    nimble_controller = add_uml_controller(topo, _NIMBLE_ADDRESS, log_subdir=log_subdir)
    bumble_controller = add_bumble_controller(topo, "bumble", _BUMBLE_ADDRESS)
    topo.set_channel_attenuation(default_db=45.0)
    await topo.start()

    async with AsyncExitStack() as stack:
        nimble = await open_uml_guest(
            stack, topo, nimble_controller, label="nimble", log_subdir=log_subdir
        )
        await start_nuttx(nimble, program)
        bumble = await open_bumble_device(
            stack, "bumble", _BUMBLE_ADDRESS, bumble_controller.hci_sock
        )
        yield nimble, bumble, stack


async def test_bumble_discovers_and_subscribes_to_nuttx_battery_service(nimble_peers):
    nimble, bumble, stack = nimble_peers
    await nimble.shell.run("bleprph")
    report = await wait_for_advertisement(bumble, "NuttX NimBLE PRPH")
    connection = await bumble.connect(report.address)
    stack.push_async_callback(connection.disconnect)
    await nimble.wait_log(r"connection established; status=0 handle=\d+")

    peer = Peer(connection)
    (service,) = await peer.discover_service(UUID("180F"))
    (level,) = await service.discover_characteristics([UUID("2A19")])
    value = await level.read_value()
    assert len(value) == 1 and 0 <= value[0] <= 100

    notifications = asyncio.Queue()
    await level.subscribe(notifications.put_nowait)
    value = await asyncio.wait_for(notifications.get(), 10.0)
    assert len(value) == 1 and 0 <= value[0] <= 100


async def test_nuttx_discovers_and_subscribes_to_bumble_battery_service(nimble_peers):
    nimble, bumble, stack = nimble_peers
    writes = asyncio.Queue()
    categories = Characteristic(
        "2A47",
        Characteristic.Properties.READ,
        Characteristic.Permissions.READABLE,
        b"\x01",
    )
    control = Characteristic(
        "2A44",
        Characteristic.Properties.WRITE,
        Characteristic.Permissions.WRITEABLE,
        CharacteristicValue(write=lambda _, value: writes.put_nowait(value)),
    )
    level = Characteristic(
        "2A19",
        Characteristic.Properties.READ | Characteristic.Properties.NOTIFY,
        Characteristic.Permissions.READABLE,
        b"\x49",
    )
    bumble.add_service(Service("1811", [categories, control]))
    bumble.add_service(Service("180F", [level]))
    alert_service = bytes(
        AdvertisingData([data_types.CompleteListOf16BitServiceUUIDs([UUID("1811")])])
    )
    await stack.enter_async_context(
        advertising(bumble, data=advertising_data("Bumble Battery") + alert_service)
    )

    await nimble.shell.run("blecent")
    await nimble.wait_log(r"name\(complete\)=Bumble Battery")
    await nimble.wait_log(r"Connection established handle=\d+")
    await nimble.wait_log(r"Service discovery complete; status=0 conn_handle=\d+")
    await nimble.wait_log(r"Read complete; status=0 conn_handle=\d+")
    assert await asyncio.wait_for(writes.get(), 10.0) == bytes((99, 100))
    await nimble.wait_log(
        r"Subscribe complete; status=0 conn_handle=\d+ attr_handle=\d+"
    )
    await bumble.notify_subscribers(level)
    await nimble.wait_log(r"notification data: 73")
