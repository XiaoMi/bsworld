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
"""Small shared builders for BSWorld unit tests."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from bsworld.devices.instance import ProcessInstance
from bsworld.devices.layout import AppSlot
from bsworld.env import Env
from bsworld.topology import BluetoothTopology


def make_env(tmp_path: Path) -> Env:
    """Build an isolated environment without probing the host machine."""
    bsim_dir = tmp_path / "build" / "babblesim" / "bsim"
    return Env(
        run_uid="uid",
        out_dir=tmp_path / "out",
        log_dir=tmp_path / "out" / "log",
        bsim_dir=bsim_dir,
        bs_state_dir=tmp_path / "state",
    )


def register_process(
    topo: BluetoothTopology,
    *,
    exe: str | Path,
    label: str = "device",
    args: tuple[str, ...] = (),
    shell: bool = False,
    log_subdir: Path | str | None = None,
    public_address: str | None = None,
) -> ProcessInstance:
    """Register a process through the same public topology API used by scenarios."""
    binary = Path(exe).absolute()
    process = topo.add_process(
        ProcessInstance(
            command=[binary, *args],
            label=label,
            shell=shell,
            log_subdir=log_subdir,
        )
    )
    process.attach(topo.add_controller(public_address=public_address))
    return process


def slot_for(topo: BluetoothTopology, process: ProcessInstance) -> AppSlot:
    return topo.bsim.plan_devices().application_slot(process)


class SpawnRecorder:
    """Minimal ProcessManager test double that records the spawn boundary."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], dict]] = []
        self.stdin = io.BytesIO()

    def spawn(self, name, command, **kwargs):
        self.calls.append((name, [str(part) for part in command], kwargs))
        popen = SimpleNamespace(
            stdin=self.stdin if kwargs.get("stdin") is not None else None
        )
        return SimpleNamespace(pid=1234, popen=popen, alive=lambda: True)
