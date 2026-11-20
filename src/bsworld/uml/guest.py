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
"""User Mode Linux guests using the topology clock and Virtio devices."""

from __future__ import annotations

import asyncio
import os
import zlib
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from bsworld.runtime.pipe import LogCapture
from bsworld.runtime.shell import ProcessShell

from .timetravel import TimeTravelCalendar
from .virtio import GuestVirtio

if TYPE_CHECKING:
    from bsworld.devices import BSController
    from bsworld.runtime.proc import Proc
    from bsworld.topology import BluetoothTopology


class UmlGuest:
    """A UML kernel with Virtio Bluetooth and a Virtio console.

    The guest does not join the PHY. Its clock follows the pytest loop's
    simulation time through UML time-travel, so it starts at the simulated
    instant it is opened. Use it as a context manager, so it is stopped while
    simulation time is still bound. Starting and stopping block the loop on
    purpose: simulation time stands still meanwhile.

    The supplied rootfs is mounted read-only through hostfs. The init path is
    absolute within that root. Send commands through shell and match output
    with wait_log(). Both Virtio devices can wake a guest that is waiting.

    The test kernel uses UML_RANDOM_SEED derived from the guest label. It also
    mixes its command line into the random pool, so the guest uses stable
    relative socket and instance paths.
    """

    def __init__(
        self,
        topo: BluetoothTopology,
        controller: BSController,
        *,
        kernel: Path,
        init: str | Path,
        label: str,
        rootfs: Path = Path("/"),
        log_subdir: Path | str | None = None,
    ) -> None:
        if not label or Path(label).name != label or label in {".", ".."}:
            raise ValueError("UML guest label must be a single path component")
        self.init = Path(init)
        if not self.init.is_absolute():
            raise ValueError(
                "UML init must be an absolute path within the guest rootfs"
            )
        self.rootfs = rootfs.resolve()
        self.topo = topo
        self.controller = controller
        self.kernel = kernel.resolve()
        self.label = label
        self.log_subdir = log_subdir if log_subdir is not None else Path("uml") / label
        self.shell = ProcessShell(label=label, enabled=True)
        self._calendar: TimeTravelCalendar | None = None
        self._devices: GuestVirtio | None = None
        self._pipe: LogCapture | None = None
        self._proc: Proc | None = None
        self._failure: asyncio.Future[Exception] | None = None
        self._failure_reported = False

    def __enter__(self) -> UmlGuest:
        self.start()
        return self

    def __exit__(self, _exc_type, error: BaseException | None, _traceback) -> None:
        if error is None:
            self.stop()
        else:
            self._stop_with_error(error)

    def start(self) -> None:
        """Boot the guest and run it until it first waits for simulated time."""
        if self._calendar is not None:
            raise RuntimeError(f"{self.label}: UML guest is already started")
        scheduler = self.topo._claim_uml_guest(self)
        try:
            # The guest works beside its Controller's socket, so its own
            # sockets need no absolute path or separate PHY device number.
            state_dir = self.controller.hci_sock.parent
            log_dir = self.topo.test_log_dir / self.log_subdir
            umid = f"uml-{self.controller.controller_dev_id}"
            loop = asyncio.get_running_loop()
            self._failure = failure = loop.create_future()
            self._failure_reported = False
            self._calendar = calendar = TimeTravelCalendar(
                state_dir / f"{umid}.tt",
                on_failure=failure.set_result,
                order=self.controller.controller_dev_id,
            )
            calendar.listen()
            pipe = self._pipe = LogCapture.open(log_dir / f"{self.label}.log")
            self._devices = devices = GuestVirtio(
                state_dir, umid, self.controller.hci_sock, pipe
            )
            devices.listen()
            env = os.environ.copy()
            env["UML_RANDOM_SEED"] = str(zlib.crc32(self.label.encode()))
            self._proc = self.topo.pm.spawn(
                self.label,
                self._command(calendar, devices, umid),
                cwd=state_dir,
                log_dir=log_dir,
                stdout=pipe.write_fd,
                stderr=pipe.write_fd,
                log_fh=pipe.log_fh,
                env=env,
            )
            self.shell.attach_writer(self._write_console)
            calendar.connect(
                scheduler, alive=self._proc.alive, console=pipe, devices=devices
            )
        except BaseException as error:
            self._stop_with_error(error)
            raise

    def stop(self) -> None:
        """Stop the guest and raise any failure that has not been reported."""
        failure = self._failure
        pending_error = None
        if not self._failure_reported:
            if failure is not None and failure.done():
                pending_error = failure.result()
            elif self._proc is not None and not self._proc.alive():
                # Teardown can precede the calendar's socket-read callback.
                pending_error = EOFError(f"{self.label}: UML guest exited unexpectedly")
        try:
            self._close()
        except Exception as shutdown_error:
            if pending_error is None:
                raise
            pending_error.add_note(f"{self.label}: cleanup failed: {shutdown_error}")
        if pending_error is not None:
            raise pending_error

    def _stop_with_error(self, error: BaseException) -> None:
        try:
            self.stop()
        except Exception as shutdown_error:
            error.add_note(
                f"{self.label}: guest shutdown also raised "
                f"{type(shutdown_error).__name__}: {shutdown_error}"
            )

    def _close(self) -> None:
        with ExitStack() as cleanup:
            cleanup.callback(self.topo._release_uml_guest, self)
            if self._failure is not None and not self._failure.done():
                self._failure.set_result(
                    RuntimeError(f"{self.label}: UML guest stopped")
                )
            self._failure = None
            if self._pipe is not None:
                cleanup.callback(self._pipe.stop)
                self._pipe = None
            cleanup.callback(self.shell.detach)
            if self._devices is not None:
                devices, self._devices = self._devices, None
                cleanup.callback(
                    devices.close, disconnect_hci=self.controller._disconnect_hci
                )
            if self._calendar is not None:
                calendar, self._calendar = self._calendar, None
                # UML panics on calendar EOF. Stop it synchronously before
                # closing the socket and returning to the event loop.
                cleanup.callback(calendar.close)
            if self._proc is not None:
                proc, self._proc = self._proc, None
                self.topo.pm.stop(proc)

    async def wait_log(self, pattern: str, *, timeout: float = 30.0) -> str:
        failure = self._failure
        if self._pipe is None or failure is None:
            raise RuntimeError(f"{self.label}: not started yet")
        log = asyncio.create_task(self._pipe.wait_log(pattern, timeout=timeout))
        try:
            done, _ = await asyncio.wait(
                (log, failure), return_when=asyncio.FIRST_COMPLETED
            )
            if failure in done:
                if failure is self._failure:
                    self._failure_reported = True
                raise failure.result()
            return log.result()
        finally:
            if log.done():
                if not log.cancelled():
                    log.exception()
            else:
                log.cancel()
                with suppress(asyncio.CancelledError):
                    await log

    def _command(
        self, calendar: TimeTravelCalendar, devices: GuestVirtio, umid: str
    ) -> list[str | Path]:
        return [
            self.kernel,
            "mem=256M",
            "root=/dev/root",
            "rootfstype=hostfs",
            f'rootflags="{self.rootfs}"',
            "ro",
            # UML joins its arguments into one kernel command line, so a
            # checkout path with spaces needs the kernel's quotes.
            f'init="{self.init}"',
            f"time-travel=ext:{calendar.path.name}",
            "con=null",
            # Keep host stdout available for low-level UML diagnostics.
            "con0=fd:0,fd:1",
            "console=hvc0",
            *devices.arguments,
            f"umid={umid}",
            "uml_dir=.",
        ]

    def _write_console(self, payload: bytes) -> None:
        assert self._devices is not None and self._calendar is not None
        self._devices.write_console(payload)
        self._calendar.devices_changed()


__all__ = ["UmlGuest"]
