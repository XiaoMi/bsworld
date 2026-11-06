# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Grant User Mode Linux its simulated time from the pytest event loop.

A UML kernel booted with ``time-travel=ext:<socket>`` asks this calendar for
every advance of its clock (include/uapi/linux/um_timetravel.h). A requested
time becomes an asyncio timer on the BabbleSim-bound loop, and the loop does
not advance while the guest runs. The guest therefore consumes no simulated
time and runs at the same simulated instants on every run.
"""

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
    from bsworld.runtime.scheduler import BsimClock

# op, seq, time in nanoseconds, in the host's byte order.
_MESSAGE = struct.Struct("=IIQ")
_NS_PER_US = 1000
_US_PER_SECOND = 1_000_000
# Guest wall-clock time at simulation time zero: 2026-01-01T00:00:00Z.
_TIME_OF_DAY_NS = 1_767_225_600 * 1_000_000_000
# Longest wall-clock time the guest may take to connect or to answer.
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
    containing T, and is handed T itself: UML requires RUN to carry exactly
    the requested time. The guest is thus never behind simulation time and
    less than a microsecond ahead of it.

    The guest only talks while it runs. Its serial line and console have no
    time-travel interrupt, so a waiting guest sends nothing until RUN.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._server: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clock: BsimClock | None = None
        # The guest's clock: the last time it was given, in nanoseconds.
        self._guest_ns = 0
        self._request_ns: int | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._suspended = False
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
    ) -> None:
        """Accept the booting guest and run it until it first waits.

        loop must be bound to clock, so its timers fire at simulated times.
        alive reports whether the guest process still runs, so a guest that
        exits during boot fails at once instead of at the timeout.
        Simulation time stands still until the guest waits, so it starts at
        the instant its process was launched, however long the boot takes.
        """
        if self._server is None:
            raise RuntimeError("time-travel calendar is not listening")
        self._loop = loop
        self._clock = clock
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

    def suspend(self) -> None:
        """Leave the waiting guest where it is, before the loop is unbound.

        The connection stays open: a guest whose calendar disconnects panics
        and prints, while a suspended one waits silently until it is killed.
        """
        self._suspended = True
        self._cancel_timer()

    def close(self) -> None:
        self._cancel_timer()
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
        while True:
            op, seq, value = self._receive()
            if op == _Op.ACK:
                # The guest acknowledging RUN.
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
            # UML closes the two shared-memory fds it expects in this ACK even
            # when none arrive, which would close its own stdin. /dev/null
            # cannot be mapped, so the guest falls back to plain messages.
            fds = [os.open(os.devnull, os.O_RDWR) for _ in range(2)]
            try:
                self._send(_Op.ACK, seq, 0, fds)
            finally:
                for fd in fds:
                    os.close(fd)
        elif op == _Op.REQUEST:
            # Each request replaces the previous one.
            self._request_ns = value
            self._send(_Op.ACK, seq, 0)
        elif op == _Op.GET:
            self._send(_Op.ACK, seq, self._guest_ns)
        elif op == _Op.GET_TOD:
            self._send(_Op.ACK, seq, _TIME_OF_DAY_NS + self._guest_ns)
        elif op in (_Op.UPDATE, _Op.BROADCAST):
            # The guest only reports times it was given.
            self._send(_Op.ACK, seq, 0)
        else:
            raise RuntimeError(f"unexpected UML time-travel message: op={op}")

    def _run(self) -> None:
        """Resume the guest at exactly the time it requested."""
        assert self._request_ns is not None
        self._guest_ns, self._request_ns = self._request_ns, None
        self._seq += 1
        self._send(_Op.RUN, self._seq, self._guest_ns)

    def _on_due(self) -> None:
        self._timer = None
        if self._suspended or self._conn is None:
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
            data = self._conn.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            self._fail(RuntimeError("UML guest sent a message while waiting"))
        else:
            self.close()

    def _fail(self, exc: Exception) -> None:
        warn(f"UML time-travel calendar {self.path.name}: {exc}")
        # The guest panics when its calendar disconnects instead of hanging.
        self.close()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _receive(self) -> tuple[int, int, int]:
        assert self._conn is not None
        data = b""
        try:
            while len(data) < _MESSAGE.size:
                chunk = self._conn.recv(_MESSAGE.size - len(data))
                if not chunk:
                    raise EOFError("UML guest closed its time-travel socket")
                data += chunk
        except TimeoutError:
            raise TimeoutError(
                f"UML guest did not answer within {_TIMEOUT:.0f} s"
            ) from None
        return _MESSAGE.unpack(data)

    def _send(self, op: int, seq: int, value: int, fds: list[int] | None = None):
        assert self._conn is not None
        data = _MESSAGE.pack(op, seq, value)
        if fds:
            socket.send_fds(self._conn, [data], fds)
        else:
            self._conn.sendall(data)


__all__ = ["TimeTravelCalendar"]
