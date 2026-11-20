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

from tests.integration.uml.bumble_peer import (
    AdvertisingData,
    add_bumble_controller,
    advertising,
    advertising_data,
    open_bumble_device,
)
from tests.integration.uml.common import add_uml_controller, open_uml_guest

from bsworld.devices import BSController
from bsworld.topology import BluetoothTopology
from bsworld.uml import UmlGuest

_CONTROLLER_ADDRESS = "AA:BB:CC:DD:EE:FF"
_BUMBLE_OBSERVER_ADDRESS = "F0:F1:F2:F3:F4:10"
_BUMBLE_PERIPHERAL_ADDRESS = "F0:F1:F2:F3:F4:11"
_BUMBLE_PERIPHERAL_NAME = "BSWorld-Peer"
_LOG_SUBDIR = Path("uml") / "bluez"
_TIMEOUT = 20.0


def _add_bluez_controller(
    topo: BluetoothTopology,
    address: str = _CONTROLLER_ADDRESS,
    *,
    log_subdir: Path = _LOG_SUBDIR,
) -> BSController:
    return add_uml_controller(topo, address, log_subdir=log_subdir)


async def _open_bluez(
    stack: AsyncExitStack,
    topo: BluetoothTopology,
    controller: BSController,
    *,
    label: str = "uml_bluez",
    log_subdir: Path = _LOG_SUBDIR,
) -> UmlGuest:
    guest = await open_uml_guest(
        stack, topo, controller, label=label, log_subdir=log_subdir
    )
    await guest.shell.run_many(
        (
            "mount -t tmpfs tmpfs /var/lib/bluetooth && mkdir /run/dbus && "
            "dbus-daemon --system --fork",
            "/usr/libexec/bluetooth/bluetoothd --nodetach "
            "--configfile=/opt/bsworld/bluez.conf &",
        )
    )
    await guest.shell.run("bluetoothctl")
    await guest.wait_log(r"Agent registered", timeout=10)
    await guest.shell.run("list")
    address = controller.public_address
    await guest.wait_log(
        rf"Controller {address} BlueZ \d+\.\d+ \[default\]", timeout=10
    )
    # The adapter may already be powered when bluetoothctl discovers it, so
    # query its state rather than requiring a power-change notification.
    await guest.shell.run("show")
    await guest.wait_log(
        rf"^Controller {address} \(public\)$",
        timeout=10,
    )
    await guest.wait_log(
        rf"(?:^\s*Powered: yes|Controller {address} Powered: yes)$",
        timeout=10,
    )
    return guest


async def test_uml_bluez_guests_advertise_to_bumble_observer(
    topo: BluetoothTopology,
) -> None:
    advertisers = {
        "BSWorld-BlueZ-A": _CONTROLLER_ADDRESS,
        "BSWorld-BlueZ-B": "AA:BB:CC:DD:EE:FD",
    }
    controllers = {
        name: _add_bluez_controller(topo, address, log_subdir=_LOG_SUBDIR / name)
        for name, address in advertisers.items()
    }
    observer_ctl = add_bumble_controller(
        topo, "bluez_observer", _BUMBLE_OBSERVER_ADDRESS
    )
    topo.set_channel_attenuation(default_db=45.0)
    await topo.start()

    async with AsyncExitStack() as stack:
        guests = {
            name: await _open_bluez(
                stack, topo, controller, label=name, log_subdir=_LOG_SUBDIR / name
            )
            for name, controller in controllers.items()
        }
        observer = await open_bumble_device(
            stack,
            "bluez_observer",
            _BUMBLE_OBSERVER_ADDRESS,
            observer_ctl.hci_sock,
        )
        found = asyncio.get_running_loop().create_future()
        addresses = {}

        def on_advertisement(advertisement) -> None:  # noqa: ANN001
            name = advertisement.data.get(AdvertisingData.Type.COMPLETE_LOCAL_NAME)
            if found.done() or name not in advertisers:
                return
            addresses[name] = advertisement.address.to_string(with_type_qualifier=False)
            if addresses.keys() == advertisers.keys():
                found.set_result(addresses)

        observer.on("advertisement", on_advertisement)
        await observer.start_scanning(active=True, filter_duplicates=False)
        try:
            for name, guest in guests.items():
                await guest.shell.run_many(
                    ("menu advertise", f"name {name}", "back", "advertise on")
                )
                await guest.wait_log(r"Advertising object registered", timeout=_TIMEOUT)
            assert await asyncio.wait_for(found, timeout=_TIMEOUT) == advertisers
        finally:
            observer.remove_listener("advertisement", on_advertisement)
            await observer.stop_scanning()


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
        await stack.enter_async_context(
            advertising(
                peripheral,
                data=advertising_data(_BUMBLE_PERIPHERAL_NAME),
                interval_min=20.0,
                interval_max=20.0,
            )
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
            await guest.shell.run("quit")
