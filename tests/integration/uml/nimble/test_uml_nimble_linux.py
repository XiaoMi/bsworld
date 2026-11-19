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

"""Apache NimBLE Linux port running inside a UML guest on simulated time."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from shlex import quote

from tests.integration.uml.bumble_peer import (
    AdvertisingData,
    add_bumble_controller,
    open_bumble_device,
)
from tests.integration.uml.common import (
    add_uml_controller,
    attach_hci_uart,
    open_uml_guest,
    require_guest_program,
)

from bsworld.topology import BluetoothTopology

_NIMBLE_ADDRESS = "AA:BB:CC:DD:EE:FE"
_NIMBLE_NAME = "nimble"
_BUMBLE_CENTRAL_ADDRESS = "F0:F1:F2:F3:F4:12"
_LOG_SUBDIR = Path("uml") / "nimble"
_TIMEOUT = 20.0


async def test_uml_nimble_linux_advertises_and_accepts_connection(
    topo: BluetoothTopology,
) -> None:
    nimble = require_guest_program(
        "BSWORLD_UML_NIMBLE_BIN", "/opt/bsworld/nimble-linux"
    )
    controller = add_uml_controller(topo, _NIMBLE_ADDRESS, log_subdir=_LOG_SUBDIR)
    central_ctl = add_bumble_controller(
        topo, "uml_nimble_central", _BUMBLE_CENTRAL_ADDRESS
    )
    topo.set_channel_attenuation(default_db=45.0)
    await topo.start()

    async with AsyncExitStack() as stack:
        guest = await open_uml_guest(
            stack, topo, controller, label="uml_nimble", log_subdir=_LOG_SUBDIR
        )
        central = await open_bumble_device(
            stack,
            "uml_nimble_central",
            _BUMBLE_CENTRAL_ADDRESS,
            central_ctl.hci_sock,
        )
        found = asyncio.get_running_loop().create_future()

        def on_advertisement(advertisement) -> None:  # noqa: ANN001
            name = advertisement.data.get(AdvertisingData.Type.COMPLETE_LOCAL_NAME)
            if not found.done() and name == _NIMBLE_NAME:
                found.set_result(advertisement)

        central.on("advertisement", on_advertisement)
        await central.start_scanning(active=False, filter_duplicates=False)
        connection = None
        try:
            await attach_hci_uart(guest)
            await guest.shell.run(f"{quote(str(nimble))} 0 &")

            advertisement = await asyncio.wait_for(found, timeout=_TIMEOUT)
            assert (
                advertisement.address.to_string(with_type_qualifier=False)
                == _NIMBLE_ADDRESS
            )

            await central.stop_scanning()
            connection = await central.connect(advertisement.address, timeout=_TIMEOUT)
            assert connection.handle is not None
        finally:
            central.remove_listener("advertisement", on_advertisement)
            if central.is_scanning:
                await central.stop_scanning()
            if connection is not None:
                await connection.disconnect()
