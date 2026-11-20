# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Start a NuttX sim executable on the guest's HCI User Channel."""

from pathlib import Path
from shlex import join

from bsworld.uml import UmlGuest


async def start_nuttx(guest: UmlGuest, program: Path) -> None:
    command = join((str(program), "--bt-dev=hci0"))
    # Wait until the Linux shell is executing its final command line before
    # sending NSH commands. NSH's first prompt is not newline-terminated.
    await guest.shell.run(f"echo 'Starting NuttX'; exec {command}")
    await guest.wait_log(r"^Starting NuttX$")
    await guest.shell.run("ifup bnep0")
