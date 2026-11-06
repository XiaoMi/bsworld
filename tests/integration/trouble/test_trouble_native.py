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

"""Two native TrouBLE Hosts over H4 Unix sockets and BabbleSim time.

Both Hosts are plain Linux processes that join the simulation directly, so no
Linux guest is involved. The Host stack and the Battery Service applications
come from the pinned upstream commit; the transport and executor live in
`source/src/bsim/`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tests.integration.applications import create_application

from bsworld.devices import AppSlot, ProcessInstance
from bsworld.env import env_path
from bsworld.topology import BluetoothTopology

_SOURCE_DIR = Path(__file__).resolve().parent / "source"
_LOG_SUBDIR = Path("trouble")

# The applications end the run from the peripheral's notify tick, so the
# window must cover connection setup plus the asserted ticks.
_SIMULATION_WINDOW_SECONDS = 12.0


def _host_binary() -> Path:
    """Resolve the adapter binary, honouring a configured CARGO_TARGET_DIR."""
    configured = env_path("BSWORLD_TROUBLE_NATIVE_BIN")
    if configured is not None:
        return configured
    return _SOURCE_DIR / "target" / "debug" / "ble_bas"


def _create_host(topo: BluetoothTopology, role: str) -> ProcessInstance:
    binary = _host_binary()
    if not binary.is_file() or not binary.stat().st_mode & 0o111:
        pytest.fail(
            f"TrouBLE adapter binary missing: {binary}\n"
            "Build it with `cargo build` in tests/integration/trouble/source, "
            "or set BSWORLD_TROUBLE_NATIVE_BIN. See "
            "tests/integration/trouble/README.md."
        )

    def argv(executable: Path, slot: AppSlot) -> list[str | Path]:
        return [
            executable,
            role,
            slot.hci_sock.parent,
            slot.phy_id,
            str(slot.dev_id),
            slot.hci_sock,
        ]

    return create_application(
        topo,
        binary=binary,
        argv=argv,
        label=role,
        log_subdir=_LOG_SUBDIR / role,
    )


async def test_trouble_native_hosts_connect_discover_read_and_notify(
    topo: BluetoothTopology,
) -> None:
    central = _create_host(topo, "central")
    peripheral = _create_host(topo, "peripheral")
    for host in (central, peripheral):
        host.attach(topo.add_controller(log_subdir=_LOG_SUBDIR / host.label))
    topo.set_channel_attenuation(default_db=45.0)

    await topo.start()
    loop = asyncio.get_running_loop()
    stop_at = loop.time() + _SIMULATION_WINDOW_SECONDS

    await peripheral.wait_log(r"\[adv\] advertising")
    await peripheral.wait_log(r"\[adv\] connection established")

    await central.wait_log(r"Looking for battery service")
    await central.wait_log(r"Subscribing notifications")
    await central.wait_log(r"Read value: \d+")

    # The peripheral notifies every 2 simulated seconds. Reaching tick 6
    # exercises Host timers well past connection setup.
    await central.wait_log(r"Got notification:.*val: 2\)")
    await central.wait_log(r"Got notification:.*val: 6\)")

    # End at a fixed simulation time so traces are comparable across CPU loads.
    assert loop.time() < stop_at
    await asyncio.sleep(stop_at - loop.time())
