# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Time-travel protocol and Virtio I/O for a single UML guest."""

from __future__ import annotations

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

from .scheduler import UML_TIMEOUT

if TYPE_CHECKING:
    from bsworld.runtime.pipe import LogCapture

    from .scheduler import TimeTravelScheduler
    from .virtio import GuestVirtio

# op, seq, time in nanoseconds, in the host's byte order.
_MESSAGE = struct.Struct("=IIQ")
_NS_PER_US = 1000
# Guest wall-clock time at simulation time zero: 2026-01-01T00:00:00Z.
_TIME_OF_DAY_NS = 1_767_225_600 * 1_000_000_000


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

    The calendar services Virtio requests and drains kernel logs while the
    guest runs. In-band Virtio interrupts can also advance the next wakeup
    of a waiting guest.
    """

    def __init__(
        self,
        path: Path,
        *,
        order: int = 0,
        on_failure: Callable[[Exception], None] | None = None,
    ) -> None:
        self.path = path
        self.order = order
        self._on_failure = on_failure
        self._server: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._scheduler: TimeTravelScheduler | None = None
        self._console: LogCapture | None = None
        self._devices: GuestVirtio | None = None
        self._readers: set[int] = set()
        self._writers: set[int] = set()
        self._frame = bytearray()
        # Last time granted by the calendar, in nanoseconds.
        self._guest_ns = 0
        self._request_ns: int | None = None
        self._last_run_us: int | None = None
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
        scheduler: TimeTravelScheduler,
        *,
        alive: Callable[[], bool],
        console: "LogCapture",
        devices: GuestVirtio | None = None,
    ) -> None:
        """Accept the booting guest and run it until it first waits.

        The scheduler's loop must be bound to its simulation clock.
        alive reports whether the guest process still runs, so a guest that
        exits during boot fails at once instead of at the timeout. console
        captures the guest's output and is drained while the guest runs.
        Simulation time stands still until the guest waits, so it starts at
        the instant its process was launched, however long the boot takes.
        """
        if self._server is None:
            raise RuntimeError("time-travel calendar is not listening")
        self._scheduler = scheduler
        self._console = console
        self._devices = devices
        self._guest_ns = scheduler.clock.now_us * _NS_PER_US
        deadline = time.monotonic() + UML_TIMEOUT
        while not select.select([self._server], [], [], 0.1)[0]:
            if not alive():
                raise RuntimeError("UML guest exited before it connected")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"UML guest did not connect to {self.path}")
        self._conn, _ = self._server.accept()
        self._conn.settimeout(UML_TIMEOUT)
        self._server.close()
        self._server = None
        self.path.unlink(missing_ok=True)
        self._serve_until_wait(time.monotonic() + UML_TIMEOUT)
        scheduler.loop.add_reader(self._conn, self._on_readable)
        self._watch_devices()
        scheduler.add(self)
        if self.next_us == scheduler.clock.now_us:
            scheduler.dispatch()

    def close(self) -> None:
        scheduler = self._scheduler
        if scheduler is not None:
            if not scheduler.loop.is_closed():
                for fd in self._readers:
                    scheduler.loop.remove_reader(fd)
                for fd in self._writers:
                    scheduler.loop.remove_writer(fd)
                if self._conn is not None:
                    scheduler.loop.remove_reader(self._conn)
            self._readers.clear()
            self._writers.clear()
            scheduler.remove(self)
            self._scheduler = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._server is not None:
            self._server.close()
            self._server = None
            self.path.unlink(missing_ok=True)

    @property
    def request_us(self) -> int | None:
        if self._request_ns is not None:
            return self._request_ns // _NS_PER_US
        return None

    @property
    def next_us(self) -> int | None:
        due_us = self.request_us
        notification_us = self._notification_us()
        if notification_us is not None:
            return notification_us if due_us is None else min(due_us, notification_us)
        return due_us

    def devices_changed(self) -> None:
        """Schedule a turn to deliver input already queued by the test."""
        if self._scheduler is not None:
            self._scheduler.io_ready()

    def _notification_us(self) -> int | None:
        if (
            self._scheduler is None
            or self._devices is None
            or not self._devices.has_notifications
        ):
            return None
        now_us = self._scheduler.clock.now_us
        # FREE_UNTIL may have let the guest reach the end of this microsecond
        # without reporting its precise time. Inject new interrupts at or
        # after the next boundary so an IRQ's GET can never move it backwards.
        if self._last_run_us == now_us:
            return now_us + 1
        return now_us

    def _watch_devices(self) -> None:
        if self._scheduler is None or self._devices is None:
            return
        loop = self._scheduler.loop
        readers = set(self._devices.read_fds)
        writers = set(self._devices.write_fds)
        for fd in self._readers - readers:
            loop.remove_reader(fd)
        for fd in self._writers - writers:
            loop.remove_writer(fd)
        for fd in sorted(readers - self._readers):
            loop.add_reader(fd, self.devices_changed)
        for fd in sorted(writers - self._writers):
            loop.add_writer(fd, self.devices_changed)
        self._readers, self._writers = readers, writers

    def service_idle(self, deadline: float) -> None:
        """Service I/O at the current PHY boundary before granting guest time."""
        devices = self._devices
        scheduler = self._scheduler
        if devices is None or scheduler is None:
            return
        try:
            readable, writable, _ = select.select(
                devices.read_fds, devices.write_fds, [], 0
            )
            devices.service(readable, writable)
            if self._notification_us() == scheduler.clock.now_us:
                self._guest_ns = max(
                    self._guest_ns, scheduler.clock.now_us * _NS_PER_US
                )
                self._settle_notifications(deadline)
            self._watch_devices()
        except Exception as exc:
            self._fail(exc)

    def _settle_notifications(self, deadline: float) -> None:
        """Let the waiting guest register the interrupt before advancing PHY."""
        assert self._devices is not None
        self._devices.flush_notifications()
        while self._devices.notifications_pending:
            if self._poll(deadline):
                message = self._read_frame()
                if message is not None and message[0] != _Op.ACK:
                    self._answer(*message)
            self._devices.flush_notifications()

    def _serve_until_wait(self, deadline: float) -> None:
        """Answer the running guest until it hands back its turn."""
        while True:
            op, seq, value = self._receive(deadline)
            if op == _Op.ACK:
                # The guest acknowledges FREE_UNTIL and RUN.
                continue
            if op != _Op.WAIT:
                self._answer(op, seq, value)
                continue
            self._send(_Op.ACK, seq, 0)
            return

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
        elif op == _Op.UPDATE:
            self._guest_ns = value
        elif op != _Op.BROADCAST:
            raise RuntimeError(f"unexpected UML time-travel message: op={op}")
        self._send(_Op.ACK, seq, reply)

    def _run(self) -> None:
        """Resume the guest at exactly the time it requested."""
        assert self._conn is not None and self._request_ns is not None
        assert self._scheduler is not None
        self._guest_ns, self._request_ns = self._request_ns, None
        self._last_run_us = self._scheduler.clock.now_us
        free_until = (self._guest_ns // _NS_PER_US + 1) * _NS_PER_US
        free_seq = (self._seq + 1) % (1 << 32)
        self._seq = (free_seq + 1) % (1 << 32)
        self._conn.sendall(
            _MESSAGE.pack(_Op.FREE_UNTIL, free_seq, free_until)
            + _MESSAGE.pack(_Op.RUN, self._seq, self._guest_ns)
        )

    def resume(self, deadline: float) -> None:
        try:
            self._run()
            self._serve_until_wait(deadline)
        except Exception as exc:
            self._fail(exc)

    def _on_readable(self) -> None:
        """Waiting guests exchange time messages during notification delivery."""
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

    def _poll(self, deadline: float) -> bool:
        """Service the guest's backend while waiting for a time-travel frame."""
        assert self._conn is not None
        fd = self._conn.fileno()
        console = self._console
        console_fd = console.read_fd if console is not None else None
        watched = [fd]
        if console_fd is not None:
            watched.append(console_fd)
        devices = self._devices
        if devices is not None:
            watched.extend(devices.transport_read_fds)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"UML guest did not yield within {UML_TIMEOUT:g} s")
        readable, writable, _ = select.select(
            watched, devices.write_fds if devices is not None else [], [], remaining
        )
        if console is not None and console_fd in readable:
            console.drain()
        if devices is not None:
            devices.service(readable, writable)
        return fd in readable

    def _read_frame(self) -> tuple[int, int, int] | None:
        assert self._conn is not None
        try:
            chunk = os.read(self._conn.fileno(), _MESSAGE.size - len(self._frame))
        except BlockingIOError:
            return None
        if not chunk:
            raise EOFError("UML guest closed its time-travel socket")
        self._frame.extend(chunk)
        if len(self._frame) != _MESSAGE.size:
            return None
        message = _MESSAGE.unpack(self._frame)
        self._frame.clear()
        return message

    def _receive(self, deadline: float) -> tuple[int, int, int]:
        while True:
            if self._poll(deadline) and (message := self._read_frame()) is not None:
                return message

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
