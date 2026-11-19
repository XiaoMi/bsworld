# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Startup and identity commands for the Zephyr Bluetooth shell."""

import re
from pathlib import Path
from shlex import join

from tests.integration.uml.common import attach_hci_uart

from bsworld.uml import UmlGuest


async def initialize_shell(guest: UmlGuest, program: Path) -> None:
    await attach_hci_uart(guest)
    # The native_sim driver binds HCI_CHANNEL_USER on a powered-off device.
    command = join((str(program), "--bt-dev=hci0", "-rt"))
    await guest.shell.run(f"hciconfig hci0 down && exec {command}")
    await guest.wait_log(r"Before any Bluetooth commands")
    await guest.shell.run("bt init sync")
    await guest.wait_log(r"Bluetooth initialized")


async def shell_identity(device: UmlGuest) -> str:
    await device.shell.run("bt id-show")
    line = await device.wait_log(r"\*0: [PR]:[0-9A-F:]+")
    return re.search(r"\*0: ([PR]:[0-9A-F:]+)", line)[1]
