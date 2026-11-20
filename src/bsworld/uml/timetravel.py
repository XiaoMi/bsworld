# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Grant User Mode Linux simulated time on the BabbleSim-bound pytest loop."""

from __future__ import annotations

import asyncio
import os
import select
import socket
import struct
import time
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING

from bsworld.log import warn

if TYPE_CHECKING:
    from bsworld.runtime.pipe import LogCapture
    from bsworld.runtime.scheduler import BsimClock

# op, seq, time in nanoseconds, in the host's byte order.
_MESSAGE = struct.Struct("=IIQ")
_NS_PER_US = 1000
_US_PER_SECOND = 1_000_000
# Guest wall-clock time at simulation time zero: 2026-01-01T00:00:00Z.
_TIME_OF_DAY_NS = 1_767_225_600 * 1_000_000_000
# Longest wall-clock time the guest may take to connect or yield.
_TIMEOUT = 60.0


class _Op(IntEnum):
    ACK = 0
    START = 1
    REQUEST = 2
    WAIT = 3
    GET = 4
    UPDATE = 5
    RUN = 6
    FREE_UNTIL = 7
    GET_TOD = 8
    BROADCAST = 9


class TimeTravelCalendar:
    """Schedule one UML guest on the simulation clock of the running loop.

    BabbleSim counts microseconds while UML counts nanoseconds. A guest that
    asks to run at T is resumed when simulation time reaches the microsecond
    containing T, and RUN carries the requested T. FREE_UNTIL lets the guest
    handle its own events strictly before the next microsecond boundary.
    The pytest loop holds its PHY turn until the guest yields.

    The guest's serial line and console use ordinary interrupts, so a waiting
    guest sends nothing until RUN.

    The calendar holds the event loop while the guest runs, so it also drains
    the guest's console: UML writes its output blocking in this mode, and a
    guest that fills the console FIFO would otherwise never wait again.
    """

    def __init__(
        self, path: Path, *, on_failure: Callable[[Exception], None] | None = None
    ) -> None:
        self.path = path
        self._on_failure = on_failure
        self._server: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clock: BsimClock | None = None
        self._console: LogCapture | None = None
        # Last time granted by the calendar, in nanoseconds.
        self._guest_ns = 0
        self._request_ns: int | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._seq = 0

    def listen(self) -> None:
        """Open the socket the guest connects to while it boots."""
        self.path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.path))
            server.listen(1)
        except BaseException:
            server.close()
            raise
        self._server = server

    def connect(
        self,
        loop: asyncio.AbstractEventLoop,
        clock: "BsimClock",
        *,
        alive: Callable[[], bool],
        console: "LogCapture",
    ) -> None:
        """Accept the booting guest and run it until it first waits.

        loop must be bound to clock, so its timers fire at simulated times.
        alive reports whether the guest process still runs, so a guest that
        exits during boot fails at once instead of at the timeout. console
        captures the guest's output and is drained while the guest runs.
        Simulation time stands still until the guest waits, so it starts at
        the instant its process was launched, however long the boot takes.
        """
        if self._server is None:
            raise RuntimeError("time-travel calendar is not listening")
        self._loop = loop
        self._clock = clock
        self._console = console
        self._guest_ns = clock.now_us * _NS_PER_US
        deadline = time.monotonic() + _TIMEOUT
        while not select.select([self._server], [], [], 0.1)[0]:
            if not alive():
                raise RuntimeError("UML guest exited before it connected")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"UML guest did not connect to {self.path}")
        self._conn, _ = self._server.accept()
        self._conn.settimeout(_TIMEOUT)
        self._server.close()
        self._server = None
        self.path.unlink(missing_ok=True)
        self._serve_until_wait()
        loop.add_reader(self._conn, self._on_readable)

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._conn is not None:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.remove_reader(self._conn)
            self._conn.close()
            self._conn = None
        if self._server is not None:
            self._server.close()
            self._server = None
            self.path.unlink(missing_ok=True)

    def _serve_until_wait(self) -> None:
        """Answer the running guest until it waits for a later microsecond.

        A wait within the current microsecond is granted at once: no other
        participant can act before that time.
        """
        assert self._loop is not None and self._clock is not None
        deadline = time.monotonic() + _TIMEOUT
        while True:
            op, seq, value = self._receive(deadline)
            if op == _Op.ACK:
                # The guest acknowledges FREE_UNTIL and RUN.
                continue
            if op != _Op.WAIT:
                self._answer(op, seq, value)
                continue
            self._send(_Op.ACK, seq, 0)
            if self._request_ns is None:
                # Only an interrupt could wake it; a periodic tick always asks.
                return
            due_us = self._request_ns // _NS_PER_US
            if due_us > self._clock.now_us:
                self._timer = self._loop.call_at(due_us / _US_PER_SECOND, self._on_due)
                return
            self._run()

    def _answer(self, op: int, seq: int, value: int) -> None:
        if op == _Op.START:
            # UML closes both fds after processing the START ACK. Sending
            # /dev/null twice provides two descriptors and selects socket mode.
            fd = os.open(os.devnull, os.O_RDWR)
            try:
                self._send(_Op.ACK, seq, 0, [fd, fd])
            finally:
                os.close(fd)
            return

        reply = 0
        if op == _Op.REQUEST:
            # Each request replaces the previous one.
            self._request_ns = value
        elif op == _Op.GET:
            reply = self._guest_ns
        elif op == _Op.GET_TOD:
            reply = _TIME_OF_DAY_NS + self._guest_ns
        elif op not in (_Op.UPDATE, _Op.BROADCAST):
            raise RuntimeError(f"unexpected UML time-travel message: op={op}")
        self._send(_Op.ACK, seq, reply)

    def _run(self) -> None:
        """Resume the guest at exactly the time it requested."""
        assert self._conn is not None and self._request_ns is not None
        self._guest_ns, self._request_ns = self._request_ns, None
        free_until = (self._guest_ns // _NS_PER_US + 1) * _NS_PER_US
        free_seq = (self._seq + 1) % (1 << 32)
        self._seq = (free_seq + 1) % (1 << 32)
        self._conn.sendall(
            _MESSAGE.pack(_Op.FREE_UNTIL, free_seq, free_until)
            + _MESSAGE.pack(_Op.RUN, self._seq, self._guest_ns)
        )

    def _on_due(self) -> None:
        self._timer = None
        if self._conn is None:
            return
        try:
            self._run()
            self._serve_until_wait()
        except Exception as exc:
            self._fail(exc)

    def _on_readable(self) -> None:
        """A waiting guest only becomes readable when it exits."""
        if self._conn is None:
            return
        try:
            data = os.read(self._conn.fileno(), 1)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            self._fail(RuntimeError("UML guest sent a message while waiting"))
        else:
            self.close()
            if self._on_failure is not None:
                self._on_failure(EOFError("UML guest exited while waiting"))

    def _fail(self, exc: Exception) -> None:
        warn(f"UML time-travel calendar {self.path.name}: {exc}")
        self.close()
        if self._on_failure is not None:
            self._on_failure(exc)

    def _receive(self, deadline: float) -> tuple[int, int, int]:
        """Read one frame and drain any console output while the guest runs."""
        assert self._conn is not None
        fd = self._conn.fileno()
        console = self._console
        console_fd = console.read_fd if console is not None else None
        watched = [fd]
        if console_fd is not None:
            watched.append(console_fd)

        data = b""
        while len(data) < _MESSAGE.size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"UML guest did not yield within {_TIMEOUT:g} s")
            readable = select.select(watched, [], [], remaining)[0]
            if console is not None and console_fd in readable:
                console.drain()
            if fd not in readable:
                continue
            try:
                # The socket timeout bounds writes. Read the nonblocking fd
                # directly after select to avoid socket.recv polling again.
                chunk = os.read(fd, _MESSAGE.size - len(data))
            except BlockingIOError:
                continue
            if not chunk:
                raise EOFError("UML guest closed its time-travel socket")
            data += chunk
        return _MESSAGE.unpack(data)

    def _send(
        self, op: int, seq: int, value: int, fds: list[int] | None = None
    ) -> None:
        assert self._conn is not None
        data = _MESSAGE.pack(op, seq, value)
        if fds:
            socket.send_fds(self._conn, [data], fds)
        else:
            self._conn.sendall(data)


__all__ = ["TimeTravelCalendar"]
