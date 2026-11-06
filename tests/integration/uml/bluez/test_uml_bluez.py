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

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path

from tests.integration.bumble.common import (
    AdvertisingData,
    AdvertisingType,
    OwnAddressType,
    add_bumble_controller,
    advertising_data,
    open_bumble_device,
)
from tests.integration.uml.common import UmlGuest, add_uml_controller, open_uml_guest

from bsworld.devices import Controller
from bsworld.topology import BluetoothTopology

_CONTROLLER_ADDRESS = "AA:BB:CC:DD:EE:FF"
_BLUEZ_ADVERTISING_NAME = "BSWorld-BlueZ"
_BUMBLE_OBSERVER_ADDRESS = "F0:F1:F2:F3:F4:10"
_BUMBLE_PERIPHERAL_ADDRESS = "F0:F1:F2:F3:F4:11"
_BUMBLE_PERIPHERAL_NAME = "BSWorld-Peer"
_LOG_SUBDIR = Path("uml") / "bluez"
_TIMEOUT = 20.0


def _add_bluez_controller(topo: BluetoothTopology) -> Controller:
    return add_uml_controller(topo, _CONTROLLER_ADDRESS, log_subdir=_LOG_SUBDIR)


async def _open_bluez(
    stack: AsyncExitStack, topo: BluetoothTopology, controller: Controller
) -> UmlGuest:
    guest = await open_uml_guest(
        stack, topo, controller, label="uml_bluez", log_subdir=_LOG_SUBDIR
    )
    await guest.shell.run("btattach -B /dev/ttyS0 -P h4 &")
    await guest.wait_log(r"Device index 0 attached", timeout=10)

    await guest.shell.run("bluetoothctl")
    await guest.wait_log(r"Agent registered", timeout=10)
    await guest.shell.run("list")
    await guest.wait_log(
        rf"Controller {_CONTROLLER_ADDRESS} BlueZ \d+\.\d+ \[default\]", timeout=10
    )
    # The adapter may already be powered when bluetoothctl discovers it, so
    # query its state rather than requiring a power-change notification.
    await guest.shell.run("show")
    await guest.wait_log(
        rf"^Controller {_CONTROLLER_ADDRESS} \(public\)$",
        timeout=10,
    )
    await guest.wait_log(
        rf"(?:^\s*Powered: yes|Controller {_CONTROLLER_ADDRESS} Powered: yes)$",
        timeout=10,
    )
    return guest


async def test_uml_bluez_initializes_virtual_controller(
    topo: BluetoothTopology,
) -> None:
    controller = _add_bluez_controller(topo)
    await topo.start()

    async with AsyncExitStack() as stack:
        guest = await _open_bluez(stack, topo, controller)
        await guest.shell.run("quit")


async def test_uml_bluez_advertises_to_bumble_observer(
    topo: BluetoothTopology,
) -> None:
    controller = _add_bluez_controller(topo)
    observer_ctl = add_bumble_controller(
        topo, "bluez_observer", _BUMBLE_OBSERVER_ADDRESS
    )
    topo.set_channel_attenuation(default_db=45.0)
    await topo.start()

    async with AsyncExitStack() as stack:
        guest = await _open_bluez(stack, topo, controller)
        observer = await open_bumble_device(
            stack,
            "bluez_observer",
            _BUMBLE_OBSERVER_ADDRESS,
            observer_ctl.hci_sock,
        )
        found = asyncio.get_running_loop().create_future()

        def on_advertisement(advertisement) -> None:  # noqa: ANN001
            name = advertisement.data.get(AdvertisingData.COMPLETE_LOCAL_NAME)
            if not found.done() and name == _BLUEZ_ADVERTISING_NAME:
                found.set_result(advertisement)

        observer.on("advertisement", on_advertisement)
        await observer.start_scanning(active=True, filter_duplicates=False)
        try:
            await guest.shell.run("menu advertise")
            await guest.shell.run(f"name {_BLUEZ_ADVERTISING_NAME}")
            await guest.shell.run("name")
            await guest.wait_log(
                rf"LocalName: {_BLUEZ_ADVERTISING_NAME}", timeout=_TIMEOUT
            )
            await guest.shell.run("back")
            await guest.shell.run("advertise on")
            await guest.wait_log(r"Advertising object registered", timeout=_TIMEOUT)

            advertisement = await asyncio.wait_for(found, timeout=_TIMEOUT)
            assert (
                advertisement.address.to_string(with_type_qualifier=False)
                == _CONTROLLER_ADDRESS
            )
        finally:
            observer.remove_listener("advertisement", on_advertisement)
            await observer.stop_scanning()
            await guest.shell.run("advertise off")
            await guest.shell.run("quit")


async def test_uml_bluez_scans_and_connects_bumble_peripheral(
    topo: BluetoothTopology,
) -> None:
    controller = _add_bluez_controller(topo)
    peripheral_ctl = add_bumble_controller(
        topo, "bluez_peripheral", _BUMBLE_PERIPHERAL_ADDRESS
    )
    topo.set_channel_attenuation(default_db=45.0)
    await topo.start()

    async with AsyncExitStack() as stack:
        guest = await _open_bluez(stack, topo, controller)
        peripheral = await open_bumble_device(
            stack,
            "bluez_peripheral",
            _BUMBLE_PERIPHERAL_ADDRESS,
            peripheral_ctl.hci_sock,
        )
        connected = asyncio.get_running_loop().create_future()
        peripheral.on("connection", lambda connection: connected.set_result(connection))
        await peripheral.start_advertising(
            advertising_type=AdvertisingType.UNDIRECTED_CONNECTABLE_SCANNABLE,
            own_address_type=OwnAddressType.PUBLIC,
            advertising_data=advertising_data(_BUMBLE_PERIPHERAL_NAME),
            advertising_interval_min=20.0,
            advertising_interval_max=20.0,
        )
        connection = None
        try:
            await guest.shell.run("scan le")
            await guest.wait_log(r"Discovery started", timeout=_TIMEOUT)
            await guest.wait_log(
                rf"Device {_BUMBLE_PERIPHERAL_ADDRESS} {_BUMBLE_PERIPHERAL_NAME}",
                timeout=_TIMEOUT,
            )
            await guest.shell.run("scan off")
            await guest.wait_log(r"Discovery stopped", timeout=_TIMEOUT)

            await guest.shell.run(f"connect {_BUMBLE_PERIPHERAL_ADDRESS}")
            await guest.wait_log(
                rf"Device {_BUMBLE_PERIPHERAL_ADDRESS} Connected: yes",
                timeout=_TIMEOUT,
            )
            await guest.wait_log(r"Connection successful", timeout=_TIMEOUT)
            connection = await asyncio.wait_for(connected, timeout=_TIMEOUT)
            assert connection.handle is not None
        finally:
            if connection is not None:
                await guest.shell.run(f"disconnect {_BUMBLE_PERIPHERAL_ADDRESS}")
                # The guest runs the host's bluetoothctl; older releases print
                # "Successful disconnected".
                await guest.wait_log(
                    r"Disconnection successful|Successful disconnected",
                    timeout=_TIMEOUT,
                )
            await peripheral.stop_advertising()
            await guest.shell.run("quit")
