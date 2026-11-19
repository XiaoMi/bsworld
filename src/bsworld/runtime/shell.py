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
"""Line-oriented command input for launched processes."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from .proc import Proc


class ProcessShell:
    """Small wrapper around a child process stdin pipe.

    The object is intentionally platform-neutral. Zephyr shell, NuttX NSH, or
    another future firmware console can all consume line-oriented commands from
    stdin when the corresponding process enables shell input.
    """

    def __init__(self, *, label: str, enabled: bool = False) -> None:
        self.label = label
        self.enabled = enabled
        self._proc: Proc | None = None
        self._write_lock = asyncio.Lock()

    def attach(self, proc: "Proc") -> None:
        self._proc = proc

    def detach(self) -> None:
        self._proc = None

    async def run(self, command: str) -> None:
        """Send one command line to the child process."""
        await self.run_many((command,))

    async def run_many(self, commands: Sequence[str]) -> None:
        """Send command lines in order, yielding while the input pipe is full."""
        if not commands:
            return
        async with self._write_lock:
            stdin = self._stdin()
            payload = "".join(_as_line(command) for command in commands).encode()
            try:
                await _write_all(stdin.fileno(), payload)
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"{self.label}: shell input is closed") from exc

    def send(self, command: str) -> None:
        """Send one command line synchronously outside an event loop."""
        self.send_many((command,))

    def send_many(self, commands: Sequence[str]) -> None:
        """Send command lines to a child that can read without an event loop."""
        if not commands:
            return
        stdin = self._stdin()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(f"{self.label}: use await shell.run() in an event loop")
        payload = "".join(_as_line(command) for command in commands).encode()
        try:
            stdin.write(payload)
            stdin.flush()
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"{self.label}: shell input is closed") from exc

    def _stdin(self) -> BinaryIO:
        if not self.enabled:
            raise RuntimeError(f"{self.label}: shell input is not enabled")
        if self._proc is None or self._proc.popen.stdin is None:
            raise RuntimeError(f"{self.label}: shell input is not available")
        return self._proc.popen.stdin


async def _write_all(fd: int, payload: bytes) -> None:
    """Let the simulated clock run whenever a Guest needs time to read."""
    was_blocking = os.get_blocking(fd)
    os.set_blocking(fd, False)
    try:
        remaining = memoryview(payload)
        while remaining:
            try:
                written = os.write(fd, remaining)
            except BlockingIOError:
                await _wait_writable(fd)
            else:
                remaining = remaining[written:]
    finally:
        with suppress(OSError):
            os.set_blocking(fd, was_blocking)


async def _wait_writable(fd: int) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def wake_writer() -> None:
        if not ready.done():
            ready.set_result(None)

    loop.add_writer(fd, wake_writer)
    try:
        await ready
    finally:
        loop.remove_writer(fd)


def _as_line(command: str) -> str:
    return command if command.endswith("\n") else f"{command}\n"


__all__ = ["ProcessShell"]
