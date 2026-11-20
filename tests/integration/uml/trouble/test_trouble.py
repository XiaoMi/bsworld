# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""TrouBLE Linux Battery Service examples running inside UML."""

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from shlex import join

from bumble.device import Device, Peer
from bumble.hci import Address, OwnAddressType
from bumble.profiles.battery_service import BatteryService, BatteryServiceProxy
from tests.integration.uml.bumble_peer import (
    add_bumble_controller,
    advertising_data,
    open_bumble_device,
    wait_for_advertisement,
)
from tests.integration.uml.common import (
    add_uml_controller,
    open_uml_guest,
    require_guest_program,
)

from bsworld.topology import BluetoothTopology
from bsworld.uml import UmlGuest

_TROUBLE_CONTROLLER_ADDRESS = "C0:FF:EE:30:00:01"
_BUMBLE_CONTROLLER_ADDRESS = "C0:FF:EE:30:01:01"
# The upstream central's accept list targets the peripheral example's address.
_BAS_PERIPHERAL_ADDRESS = "FF:E4:05:1A:8F:FF"
_LOG_SUBDIR = Path("uml") / "trouble"


async def _open_peers(
    stack: AsyncExitStack,
    topo: BluetoothTopology,
    bumble_role: str,
    *,
    bumble_address: str = _BUMBLE_CONTROLLER_ADDRESS,
) -> tuple[UmlGuest, Device]:
    trouble_ctl = add_uml_controller(
        topo, _TROUBLE_CONTROLLER_ADDRESS, log_subdir=_LOG_SUBDIR
    )
    bumble_ctl = add_bumble_controller(topo, bumble_role, _BUMBLE_CONTROLLER_ADDRESS)
    topo.set_channel_attenuation(default_db=45.0)
    await topo.start()

    trouble = await open_uml_guest(
        stack, topo, trouble_ctl, label="trouble", log_subdir=_LOG_SUBDIR
    )
    bumble = await open_bumble_device(
        stack, bumble_role, bumble_address, bumble_ctl.hci_sock
    )
    return trouble, bumble


async def _run_example(guest: UmlGuest, program: Path) -> None:
    command = join((str(program), "0"))
    await guest.shell.run(
        f"hciconfig hci0 down && RUST_LOG=info RUST_LOG_STYLE=never exec {command}"
    )


async def test_bumble_reads_and_subscribes_to_trouble_battery_service(
    topo: BluetoothTopology,
) -> None:
    program = require_guest_program(
        "BSWORLD_UML_TROUBLE_PERIPHERAL_BIN", "/opt/bsworld/trouble/ble_bas_peripheral"
    )
    async with AsyncExitStack() as stack:
        trouble, bumble = await _open_peers(stack, topo, "central")
        await _run_example(trouble, program)
        await trouble.wait_log(r"\[adv\] advertising$")

        report = await wait_for_advertisement(bumble, "Trouble Example")
        assert report.address == Address(_BAS_PERIPHERAL_ADDRESS)
        connection = await bumble.connect(
            report.address, own_address_type=OwnAddressType.PUBLIC, timeout=15.0
        )
        stack.push_async_callback(connection.disconnect)
        await trouble.wait_log(r"\[adv\] connection established$")

        service = await Peer(connection).discover_service_and_create_proxy(
            BatteryServiceProxy
        )
        assert service is not None
        assert 1 <= await service.battery_level.read_value() <= 100

        notifications: asyncio.Queue[int] = asyncio.Queue()
        await service.battery_level.subscribe(notifications.put_nowait)
        first = await asyncio.wait_for(notifications.get(), timeout=10.0)
        second = await asyncio.wait_for(notifications.get(), timeout=10.0)
        assert 1 <= first < 100
        assert second == first + 1


async def test_trouble_reads_and_subscribes_to_bumble_battery_service(
    topo: BluetoothTopology,
) -> None:
    program = require_guest_program(
        "BSWORLD_UML_TROUBLE_CENTRAL_BIN", "/opt/bsworld/trouble/ble_bas_central"
    )
    async with AsyncExitStack() as stack:
        trouble, bumble = await _open_peers(
            stack, topo, "peripheral", bumble_address=_BAS_PERIPHERAL_ADDRESS
        )
        battery_level = 73
        service = BatteryService(lambda _: battery_level)
        bumble.add_service(service)

        await bumble.start_advertising(
            own_address_type=OwnAddressType.RANDOM,
            advertising_data=advertising_data("Bumble Battery"),
        )
        stack.push_async_callback(bumble.stop_advertising)
        await _run_example(trouble, program)
        # The example subscribes before its first read.
        await trouble.wait_log(r"Read value: 73$")

        battery_level = 74
        await bumble.notify_subscribers(
            service.battery_level_characteristic, battery_level
        )
        await trouble.wait_log(r"Got notification: \[74\] \(val: 74\)$")
