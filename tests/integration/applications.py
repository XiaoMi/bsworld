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
"""Create and register an externally built Application.

Resolve the executable, create the process and register it with the topology.
The caller attaches its BSController before simulation startup.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from bsworld.devices import AppSlot, ProcessInstance
from bsworld.env import env_path
from bsworld.topology import BluetoothTopology


def require_artifact(binary_env: str, *, guide: str, executable: bool = True) -> Path:
    """Resolve an artifact selected by an environment variable, or skip.

    The variable is read on every call so a test can change the selected
    artifact without reimporting the profiles that name it.
    """
    path = env_path(binary_env)
    if (
        path is None
        or not path.is_file()
        or (executable and not path.stat().st_mode & 0o111)
    ):
        kind = "compiled executable" if executable else "built artifact"
        pytest.skip(f"Set {binary_env} to the {kind}; see {guide}.")
    return path


def create_application(
    topo: BluetoothTopology,
    *,
    argv: Callable[[Path, AppSlot], list[str | Path]],
    label: str,
    binary: Path | None = None,
    binary_env: str | None = None,
    guide: str | None = None,
    log_subdir: Path | str | None = None,
    shell: bool = False,
) -> ProcessInstance:
    """Register an HCI Host for attachment to a BSController."""
    if (binary is None) == (binary_env is None):
        raise TypeError("pass exactly one of binary or binary_env")
    if binary is None:
        if guide is None:
            raise TypeError("binary_env requires guide")
        binary = require_artifact(binary_env, guide=guide)
    executable = binary.absolute()
    return topo.add_process(
        ProcessInstance(
            command=lambda slot: argv(executable, slot),
            label=label,
            shell=shell,
            log_subdir=log_subdir,
        )
    )


__all__ = ["create_application", "require_artifact"]
