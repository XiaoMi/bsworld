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
"""Guest artifact selection and boot helpers for the integration suites."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bsworld.env import env_path
from bsworld.uml import UmlGuest

if TYPE_CHECKING:
    from contextlib import AsyncExitStack

    from bsworld.devices import BSController
    from bsworld.topology import BluetoothTopology

_GUIDE = "tests/integration/uml/README.md"


def _rootfs() -> Path:
    rootfs = env_path("BSWORLD_UML_ROOTFS")
    if rootfs is None or not (rootfs / "init").is_file():
        pytest.skip(f"Set BSWORLD_UML_ROOTFS to a guest root with /init; see {_GUIDE}.")
    return rootfs


def _artifacts() -> tuple[Path, Path]:
    kernel = env_path("BSWORLD_UML_KERNEL")
    if kernel is None or not kernel.is_file() or not os.access(kernel, os.X_OK):
        pytest.skip(f"Set BSWORLD_UML_KERNEL to the compiled executable; see {_GUIDE}.")
    return kernel, _rootfs()


def require_guest_program(variable: str, default: str) -> Path:
    """Resolve an executable by its absolute path inside the Guest."""
    program = Path(os.getenv(variable, default))
    if not program.is_absolute() or ".." in program.parts:
        pytest.fail(f"{variable} must be an absolute path inside the UML rootfs")
    executable = _rootfs() / program.relative_to("/")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        pytest.skip(f"UML program unavailable: {executable}. See {_GUIDE}.")
    return program


def add_uml_controller(
    topo: BluetoothTopology, address: str, *, log_subdir: Path
) -> BSController:
    """Allocate a guest's Controller once its boot artifacts are available."""
    _artifacts()
    return topo.add_controller(public_address=address, log_subdir=log_subdir)


async def open_uml_guest(
    stack: AsyncExitStack,
    topo: BluetoothTopology,
    controller: BSController,
    *,
    label: str,
    log_subdir: Path,
) -> UmlGuest:
    """Boot a guest on a started topology and wait for its root console."""
    kernel, rootfs = _artifacts()
    guest = stack.enter_context(
        UmlGuest(
            topo,
            controller,
            kernel=kernel,
            rootfs=rootfs,
            init="/init",
            label=label,
            log_subdir=log_subdir,
        )
    )
    await guest.wait_log(r"^BSWORLD_UML_READY$")
    return guest
