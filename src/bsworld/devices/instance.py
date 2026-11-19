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
"""Application process instance managed by a BluetoothTopology."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from ..deps import BSWorldSetupError
from ..runtime.pipe import LogCapture
from ..runtime.shell import ProcessShell
from .bscontroller import BSController
from .layout import AppSlot

if TYPE_CHECKING:
    from ..runtime.proc import ProcessManager
    from ..topology import BluetoothTopology


class ProcessInstance:
    """An HCI Host process whose Controller can be attached before startup."""

    def __init__(
        self,
        *,
        command: list[str | Path] | Callable[[AppSlot], list[str | Path]],
        controller: BSController | None = None,
        label: str,
        shell: bool = False,
        log_subdir: Path | str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not callable(command) and not command:
            raise ValueError("process command must not be empty")
        self._command_factory = command
        self._controller = controller
        self._topology: BluetoothTopology | None = None
        self.label = label
        self.shell = ProcessShell(label=label, enabled=shell)
        # Labels are unique within a topology, so the default directory keeps
        # each process's logs, capture and working files apart however its
        # Controller is attached.
        self.log_subdir = Path(log_subdir) if log_subdir is not None else Path(label)
        self.cwd = cwd
        self.env = dict(env) if env is not None else None
        self._pipe: LogCapture | None = None

    @property
    def controller(self) -> BSController | None:
        return self._controller

    def attach(self, controller: BSController) -> ProcessInstance:
        """Bind a BSController before the topology layout is frozen."""
        if self._controller is not None:
            raise RuntimeError("process already has a Controller attached")
        if not isinstance(controller, BSController):
            raise TypeError("attach requires a BSController instance")
        if self._topology is not None:
            self._topology._attach_process_controller(self, controller)
        self._controller = controller
        return self

    def start(
        self,
        pm: "ProcessManager",
        log_dir: Path,
        slot: AppSlot,
    ) -> None:
        command = self._resolve_command(slot)
        executable = Path(command[0])
        if not executable.is_file():
            raise BSWorldSetupError(f"{self.label}: binary missing: {executable}")

        dev_log_dir = log_dir / self.log_subdir
        log_path = dev_log_dir / f"{self.label}.log"
        self._pipe = LogCapture.open(log_path)

        try:
            options = (
                {"env": {**os.environ, **self.env}} if self.env is not None else {}
            )
            proc = pm.spawn(
                self.label,
                command,
                cwd=self.cwd if self.cwd is not None else dev_log_dir,
                log_dir=dev_log_dir,
                stdin=subprocess.PIPE if self.shell.enabled else None,
                stdout=self._pipe.write_fd,
                stderr=self._pipe.write_fd,
                log_fh=self._pipe.log_fh,
                **options,
            )
            self.shell.attach(proc)
        except Exception:
            self.shell.detach()
            self.stop_capture()
            raise

        from ..log import log

        log(f"{self.label}: started (pid={proc.pid})")

    def _resolve_command(self, slot: AppSlot) -> list[str | Path]:
        command = (
            self._command_factory(slot)
            if callable(self._command_factory)
            else self._command_factory
        )
        if not command:
            raise ValueError("process command must not be empty")
        return list(command)

    def stop_capture(self) -> None:
        self.shell.detach()
        if self._pipe is None:
            return
        self._pipe.stop()
        self._pipe = None

    async def wait_log(self, pattern: str, *, timeout: float = 30.0) -> str:
        if self._pipe is None:
            raise RuntimeError(f"{self.label}: not started yet")
        return await self._pipe.wait_log(pattern, timeout=timeout)


__all__ = ["ProcessInstance"]
