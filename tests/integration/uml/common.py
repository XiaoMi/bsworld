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
"""Shared helpers for the User Mode Linux guest test suite.

Like a Bumble device, a UML guest is a Host opened inside the test once the
topology has started; only its Controller is registered beforehand.
"""

from __future__ import annotations

import asyncio
import os
import resource
import socket
import subprocess
import zlib
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from tests.integration.applications import require_artifact
from tests.integration.uml.timetravel import TimeTravelCalendar

from bsworld.devices import Controller
from bsworld.runtime.pipe import LogCapture
from bsworld.runtime.shell import ProcessShell
from bsworld.topology import BluetoothTopology

if TYPE_CHECKING:
    from bsworld.runtime.proc import Proc

_GUIDE = "tests/integration/uml/README.md"
_INIT = Path(__file__).resolve().parent / "source" / "init.sh"


class UmlGuest:
    """A UML kernel whose serial line ttyS0 is its Controller's H:4 socket.

    The guest does not join the PHY. Its clock follows the pytest loop's
    simulation time through UML time-travel, so it starts at the simulated
    instant it is opened. Use it as a context manager, so it is stopped while
    simulation time is still bound. Starting and stopping block the loop on
    purpose: simulation time stands still meanwhile.

    The guest mounts the host's root read-only and runs source/init.sh. The
    console is the process's stdin and stdout: send commands through shell
    and match output with wait_log(). Neither the serial line nor the console
    can wake a waiting guest, so the kernel needs a periodic tick: input is
    handled at the next tick after it arrives.

    getrandom_library is source/getrandom/getrandom.c built as a shared
    library. Preloaded into the UML process, it seeds the guest from its
    label, so guests in one topology differ while each repeats on every run.
    The kernel also mixes its command line into that seed, so the command
    line uses relative paths and the same descriptor number on every run.
    """

    def __init__(
        self,
        topo: BluetoothTopology,
        controller: Controller,
        *,
        kernel: Path,
        getrandom_library: Path,
        label: str,
        log_subdir: Path,
    ) -> None:
        self.topo = topo
        self.controller = controller
        self.kernel = kernel
        self.getrandom_library = getrandom_library
        self.label = label
        self.log_subdir = log_subdir
        self.shell = ProcessShell(label=label, enabled=True)
        self._calendar: TimeTravelCalendar | None = None
        self._pipe: LogCapture | None = None
        self._proc: Proc | None = None

    def __enter__(self) -> UmlGuest:
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def start(self) -> None:
        """Boot the guest and run it until it first waits for simulated time."""
        clock = self.topo.bsim.clock
        if clock is None:
            raise RuntimeError(f"{self.label}: start the topology before the guest")
        if self._calendar is not None:
            raise RuntimeError(f"{self.label}: UML guest is already started")
        # The guest works in the simulation's state directory, beside its
        # Controller's socket, so its own sockets need no absolute path.
        state_dir = self.controller.hci_sock.parent
        log_dir = self.topo.test_log_dir / self.log_subdir
        calendar = TimeTravelCalendar(state_dir / f"{self.label}.tt")
        self._calendar = calendar
        try:
            calendar.listen()
            self._pipe = LogCapture.open(log_dir / f"{self.label}.log")
            hci_fd = _connect_controller(self.controller.hci_sock)
            try:
                self._proc = self.topo.pm.spawn(
                    self.label,
                    self._command(calendar, hci_fd),
                    cwd=state_dir,
                    log_dir=log_dir,
                    stdin=subprocess.PIPE,
                    stdout=self._pipe.write_fd,
                    stderr=self._pipe.write_fd,
                    log_fh=self._pipe.log_fh,
                    env={
                        **os.environ,
                        "LD_PRELOAD": str(self.getrandom_library),
                        "BSWORLD_UML_SEED": str(zlib.crc32(self.label.encode())),
                    },
                    pass_fds=(hci_fd,),
                )
            finally:
                # The guest owns the Controller connection from here on.
                os.close(hci_fd)
            self.shell.attach(self._proc)
            calendar.connect(asyncio.get_running_loop(), clock, alive=self._proc.alive)
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        """Kill the guest where it waits, so it prints nothing more."""
        if self._calendar is not None:
            self._calendar.suspend()
        if self._proc is not None:
            self.topo.pm.stop(self._proc)
            self._proc = None
        if self._calendar is not None:
            self._calendar.close()
            self._calendar = None
        self.shell.detach()
        if self._pipe is not None:
            self._pipe.stop()
            self._pipe = None

    async def wait_log(self, pattern: str, *, timeout: float = 30.0) -> str:
        if self._pipe is None:
            raise RuntimeError(f"{self.label}: not started yet")
        return await self._pipe.wait_log(pattern, timeout=timeout)

    def _command(self, calendar: TimeTravelCalendar, hci_fd: int) -> list[str | Path]:
        return [
            self.kernel,
            "mem=256M",
            # The host's root, read-only, so the guest runs installed programs
            # without changing any host file.
            "root=/dev/root",
            "rootfstype=hostfs",
            "rootflags=/",
            "ro",
            # UML joins its arguments into one kernel command line, so a
            # checkout path with spaces needs the kernel's quotes.
            f'init="{_INIT}"',
            f"time-travel=ext:{calendar.path.name}",
            "con=null",
            "con0=fd:0,fd:1",
            "ssl=null",
            f"ssl0=fd:{hci_fd}",
            f"umid={self.label}",
            "uml_dir=.",
        ]


def _connect_controller(hci_sock: Path) -> int:
    """Connect to the Controller's H:4 socket for the guest to inherit.

    The guest kernel mixes its command line, ssl0=fd:N included, into its
    random pool. The connection therefore takes the highest descriptor the
    process may open, which no other descriptor reaches, so N is the same on
    every run.
    """
    fd = resource.getrlimit(resource.RLIMIT_NOFILE)[0] - 1
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as hci:
        hci.connect(str(hci_sock))
        try:
            os.fstat(fd)
        except OSError:
            os.dup2(hci.fileno(), fd, inheritable=False)
            return fd
    raise RuntimeError(f"descriptor {fd} for the UML guest is already open")


def _artifacts() -> tuple[Path, Path]:
    kernel = require_artifact("BSWORLD_UML_KERNEL", guide=_GUIDE)
    getrandom_library = require_artifact(
        "BSWORLD_UML_GETRANDOM", guide=_GUIDE, executable=False
    )
    return kernel, getrandom_library


def add_uml_controller(
    topo: BluetoothTopology, address: str, *, log_subdir: Path
) -> Controller:
    """Allocate a UML guest's Controller, skipping before startup without one."""
    _artifacts()
    return topo.add_controller(public_address=address, log_subdir=log_subdir)


async def open_uml_guest(
    stack: AsyncExitStack,
    topo: BluetoothTopology,
    controller: Controller,
    *,
    label: str,
    log_subdir: Path,
) -> UmlGuest:
    """Boot a UML guest on a started topology and wait for its root shell."""
    kernel, getrandom_library = _artifacts()
    guest = UmlGuest(
        topo,
        controller,
        kernel=kernel,
        getrandom_library=getrandom_library,
        label=label,
        log_subdir=log_subdir,
    )
    stack.enter_context(guest)
    # init.sh prints this once D-Bus and bluetoothd run and the shell opens.
    await guest.wait_log(r"^BSWORLD_UML_READY$", timeout=30)
    return guest


__all__ = ["UmlGuest", "add_uml_controller", "open_uml_guest"]
