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
"""FIFO-backed process log capture integrated with the active event loop."""

from __future__ import annotations

import asyncio
import os
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO


@dataclass
class LogCapture:
    log_path: Path
    log_fh: BinaryIO  # FIFO write side kept open while the child runs
    _fifo_path: Path | None = None
    _fifo_read_fd: int | None = None
    _artifact_fh: BinaryIO | None = None
    _pending_line: bytearray = field(default_factory=bytearray)
    _lines: deque[str] = field(default_factory=deque, init=False, repr=False)
    _changed: asyncio.Event = field(
        default_factory=asyncio.Event, init=False, repr=False
    )
    _loop: asyncio.AbstractEventLoop | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._attach(loop)

    @classmethod
    def open(
        cls,
        log_path: Path,
    ) -> "LogCapture":
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fifo_path = log_path.with_name(f"{log_path.name}.fifo")
        fifo_path.unlink(missing_ok=True)
        os.mkfifo(fifo_path, 0o600)
        read_fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
        write_fd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        log_fh = os.fdopen(write_fd, "wb", buffering=0)
        artifact_fh = open(log_path, "wb", buffering=0)
        return cls(
            log_path=log_path,
            log_fh=log_fh,
            _fifo_path=fifo_path,
            _fifo_read_fd=read_fd,
            _artifact_fh=artifact_fh,
        )

    @property
    def write_fd(self) -> int:
        return self.log_fh.fileno()

    @property
    def read_fd(self) -> int | None:
        """The FIFO's read side, for select(); None once stopped."""
        return self._fifo_read_fd

    def drain(self) -> None:
        """Record whatever the FIFO holds now, without the event loop.

        For callers that block the loop while the child writes, so the child
        never stalls on a full FIFO.
        """
        self._drain_fifo()

    def stop(self) -> None:
        loop = self._loop
        self._loop = None
        if loop is not None and not loop.is_closed() and self._fifo_read_fd is not None:
            loop.remove_reader(self._fifo_read_fd)
        self._drain_fifo()
        try:
            self.log_fh.close()
        except Exception:
            pass
        self._drain_fifo()
        if self._artifact_fh is not None:
            try:
                self._artifact_fh.close()
            except Exception:
                pass
            self._artifact_fh = None
        if self._fifo_read_fd is not None:
            try:
                os.close(self._fifo_read_fd)
            except OSError:
                pass
            self._fifo_read_fd = None
        if self._fifo_path is not None:
            try:
                self._fifo_path.unlink()
            except FileNotFoundError:
                pass
            self._fifo_path = None

    async def wait_log(self, pattern: str, *, timeout: float = 30.0) -> str:
        compiled = re.compile(pattern)

        def _timeout_error() -> TimeoutError:
            return TimeoutError(
                f"wait_log({pattern!r}) timed out after {timeout}s; log={self.log_path}"
            )

        loop = asyncio.get_running_loop()
        self._attach(loop)
        deadline = loop.time() + timeout
        while True:
            self._changed.clear()
            line = self._match_buffered_line(compiled)
            if line is not None:
                return line
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise _timeout_error()
            try:
                async with asyncio.timeout(remaining):
                    await self._changed.wait()
            except TimeoutError:
                line = self._match_buffered_line(compiled)
                if line is not None:
                    return line
                raise _timeout_error() from None

    def _attach(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is loop:
            return
        if self._loop is not None and not self._loop.is_closed():
            raise RuntimeError("log capture is already attached to another event loop")
        if self._fifo_read_fd is None:
            raise RuntimeError("log capture is closed")
        loop.add_reader(self._fifo_read_fd, self._drain_fifo)
        self._loop = loop

    def _drain_fifo(self) -> None:
        if self._fifo_read_fd is None:
            return

        while True:
            try:
                chunk = os.read(self._fifo_read_fd, 65536)
            except BlockingIOError:
                return
            except OSError:
                return
            if not chunk:
                return
            if self._artifact_fh is not None:
                self._artifact_fh.write(chunk)
            self._record_lines(chunk)

    def _record_lines(self, chunk: bytes) -> None:
        self._pending_line.extend(chunk)
        recorded = False
        while True:
            line_end = self._pending_line.find(b"\n")
            if line_end < 0:
                break
            raw_line = bytes(self._pending_line[:line_end])
            del self._pending_line[: line_end + 1]
            self._lines.append(raw_line.decode("utf-8", errors="replace").rstrip())
            recorded = True

        if recorded:
            self._changed.set()

    def _match_buffered_line(self, compiled: re.Pattern[str]) -> str | None:
        self._drain_fifo()
        while self._lines:
            line = self._lines.popleft()
            if compiled.search(line):
                return line
        return None


__all__ = ["LogCapture"]
