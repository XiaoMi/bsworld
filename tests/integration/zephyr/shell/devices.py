# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Register the Zephyr Bluetooth shell on its own BSController."""

from pathlib import Path

from tests.integration.applications import create_application

from bsworld.devices import AppSlot, ProcessInstance
from bsworld.topology import BluetoothTopology


def _argv(binary: Path, slot: AppSlot) -> list[str | Path]:
    return [
        binary,
        f"-s={slot.sim_id}",
        f"-p={slot.phy_id}",
        f"-d={slot.dev_id}",
        "-mro=1000",
        f"--bt-dev={slot.hci_sock}",
    ]


def create_zephyr_shell(
    topo: BluetoothTopology, *, name: str, public_address: str
) -> ProcessInstance:
    """Register a Zephyr shell and attach its BSController before startup."""
    shell = create_application(
        topo,
        binary_env="BSWORLD_ZEPHYR_SHELL_BIN",
        guide="tests/integration/zephyr/shell/README.md",
        argv=_argv,
        label=name,
        log_subdir=Path("zephyr") / "shell" / name,
        shell=True,
    )
    return shell.attach(topo.add_controller(public_address=public_address))
