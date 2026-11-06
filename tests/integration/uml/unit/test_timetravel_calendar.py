# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Protocol contracts of the UML time-travel calendar, without a kernel.

A socket client plays the guest and sends its messages ahead of time, since
the calendar answers each in order. A fake loop records the calendar's timers
and readers, so each test fires them at the simulated time it chooses.
"""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.integration.uml import timetravel
from tests.integration.uml.timetravel import TimeTravelCalendar, _Op

from bsworld.runtime.pipe import LogCapture

_MESSAGE = timetravel._MESSAGE
_WAIT_FOREVER = 2**64 - 1


class _Timer:
    def __init__(self, when: float, callback: Callable[[], None]) -> None:
        self.when = when
        self.callback = callback
        self.cancelled = False
        self.fired = False

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        """Run the callback as the loop would at self.when, even if cancelled."""
        self.fired = True
        self.callback()


class _Loop:
    """The part of an event loop the calendar uses, driven by the test."""

    def __init__(self) -> None:
        self.timers: list[_Timer] = []
        self.readers: dict[object, Callable[[], None]] = {}

    def call_at(self, when: float, callback: Callable[[], None]) -> _Timer:
        timer = _Timer(when, callback)
        self.timers.append(timer)
        return timer

    def add_reader(self, fd: object, callback: Callable[[], None]) -> None:
        self.readers[fd] = callback

    def remove_reader(self, fd: object) -> bool:
        return self.readers.pop(fd, None) is not None

    def is_closed(self) -> bool:
        return False

    def pending(self) -> list[_Timer]:
        return [t for t in self.timers if not (t.cancelled or t.fired)]


class _Guest:
    """The guest's end of the time-travel socket."""

    def __init__(self, path: Path) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(path))
        self.sock.settimeout(5)
        self._seq = 0

    def send(self, op: int, value: int = 0) -> int:
        self._seq += 1
        self.sock.sendall(_MESSAGE.pack(op, self._seq, value))
        return self._seq

    def receive(self) -> tuple[int, int, int]:
        data = b""
        while len(data) < _MESSAGE.size:
            chunk = self.sock.recv(_MESSAGE.size - len(data))
            assert chunk, "the calendar closed the socket"
            data += chunk
        return _MESSAGE.unpack(data)

    def acks(self, count: int) -> list[int]:
        """Read count replies, which must all be ACKs, and return their seqs."""
        replies = [self.receive() for _ in range(count)]
        assert [op for op, _, _ in replies] == [_Op.ACK] * count
        return [seq for _, seq, _ in replies]

    def disconnected(self) -> bool:
        # Closing with unread guest data resets the connection instead.
        try:
            return self.sock.recv(1) == b""
        except ConnectionResetError:
            return True

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def calendar(tmp_path):
    calendar = TimeTravelCalendar(tmp_path / "guest.tt")
    calendar.listen()
    yield calendar
    calendar.close()


@pytest.fixture
def loop():
    return _Loop()


@pytest.fixture
def console(tmp_path):
    capture = LogCapture.open(tmp_path / "guest.log")
    yield capture
    capture.stop()


@pytest.fixture
def guest(calendar):
    guest = _Guest(calendar.path)
    yield guest
    guest.close()


def _connect(calendar, loop, console, clock) -> None:
    calendar.connect(loop, clock, alive=lambda: True, console=console)


def _us(value: int) -> float:
    """The loop time of a simulated microsecond."""
    return value / 1_000_000


def test_boot_answers_the_handshake_at_the_launch_instant(
    calendar, loop, console, guest
) -> None:
    clock = SimpleNamespace(now_us=1_000)
    start = guest.send(_Op.START)
    tod = guest.send(_Op.GET_TOD)
    get = guest.send(_Op.GET)
    request = guest.send(_Op.REQUEST, 1_500_000)
    wait = guest.send(_Op.WAIT, _WAIT_FOREVER)

    _connect(calendar, loop, console, clock)

    data, fds, _, _ = socket.recv_fds(guest.sock, _MESSAGE.size, 2)
    for fd in fds:
        os.close(fd)
    # UML closes two shared-memory descriptors from this ACK, received or not.
    assert (_MESSAGE.unpack(data), len(fds)) == ((_Op.ACK, start, 0), 2)
    assert guest.receive() == (_Op.ACK, tod, timetravel._TIME_OF_DAY_NS + 1_000_000)
    assert guest.receive() == (_Op.ACK, get, 1_000_000)
    assert guest.acks(2) == [request, wait]
    assert [timer.when for timer in loop.pending()] == [_us(1_500)]
    assert not calendar.path.exists()


def test_request_is_due_at_its_microsecond_and_resumed_at_its_nanosecond(
    calendar, loop, console, guest
) -> None:
    clock = SimpleNamespace(now_us=1_000)
    guest.send(_Op.REQUEST, 2_000_999)
    guest.send(_Op.WAIT, _WAIT_FOREVER)
    _connect(calendar, loop, console, clock)
    guest.acks(2)
    (timer,) = loop.pending()
    assert timer.when == _us(2_000)

    # The guest acknowledges RUN, reads its clock and waits again.
    guest.send(_Op.ACK)
    get = guest.send(_Op.GET)
    guest.send(_Op.REQUEST, 3_000_000)
    guest.send(_Op.WAIT, _WAIT_FOREVER)
    clock.now_us = 2_000
    timer.fire()

    assert guest.receive() == (_Op.RUN, 1, 2_000_999)
    assert guest.receive() == (_Op.ACK, get, 2_000_999)
    guest.acks(2)
    assert [timer.when for timer in loop.pending()] == [_us(3_000)]


def test_request_within_the_current_microsecond_runs_without_a_timer(
    calendar, loop, console, guest
) -> None:
    clock = SimpleNamespace(now_us=2_000)
    guest.send(_Op.REQUEST, 2_000_400)
    guest.send(_Op.WAIT, _WAIT_FOREVER)
    guest.send(_Op.ACK)
    guest.send(_Op.REQUEST, 5_000_000)
    guest.send(_Op.WAIT, _WAIT_FOREVER)

    _connect(calendar, loop, console, clock)

    guest.acks(2)
    assert guest.receive() == (_Op.RUN, 1, 2_000_400)
    guest.acks(2)
    assert [timer.when for timer in loop.pending()] == [_us(5_000)]


@pytest.mark.parametrize(
    ("requests", "resumed_ns"),
    [((3_000_000, 5_000_000), 5_000_000), ((5_000_000, 3_000_000), 3_000_000)],
    ids=["later", "earlier"],
)
def test_each_request_replaces_the_previous_one(
    calendar, loop, console, guest, requests, resumed_ns
) -> None:
    clock = SimpleNamespace(now_us=1_000)
    for request in requests:
        guest.send(_Op.REQUEST, request)
    guest.send(_Op.WAIT, _WAIT_FOREVER)
    _connect(calendar, loop, console, clock)
    guest.acks(3)

    (timer,) = loop.pending()
    assert timer.when == _us(resumed_ns // 1_000)
    guest.send(_Op.WAIT, _WAIT_FOREVER)
    clock.now_us = resumed_ns // 1_000
    timer.fire()
    assert guest.receive() == (_Op.RUN, 1, resumed_ns)


def test_wait_without_a_request_arms_no_timer(calendar, loop, console, guest) -> None:
    guest.send(_Op.WAIT, _WAIT_FOREVER)
    _connect(calendar, loop, console, SimpleNamespace(now_us=0))
    guest.acks(1)
    assert loop.pending() == []
    assert list(loop.readers) == [calendar._conn]


def test_suspend_cancels_the_timer_and_keeps_the_guest_connected(
    calendar, loop, console, guest
) -> None:
    guest.send(_Op.REQUEST, 5_000_000)
    guest.send(_Op.WAIT, _WAIT_FOREVER)
    _connect(calendar, loop, console, SimpleNamespace(now_us=1_000))
    guest.acks(2)
    (timer,) = loop.pending()

    calendar.suspend()
    # A handle that fires anyway resumes nothing.
    timer.fire()

    assert timer.cancelled
    guest.sock.settimeout(0.2)
    with pytest.raises(TimeoutError):
        guest.sock.recv(1)


def test_close_disconnects_the_guest(calendar, loop, console, guest) -> None:
    guest.send(_Op.REQUEST, 5_000_000)
    guest.send(_Op.WAIT, _WAIT_FOREVER)
    _connect(calendar, loop, console, SimpleNamespace(now_us=1_000))
    guest.acks(2)

    calendar.close()

    assert loop.pending() == []
    assert loop.readers == {}
    assert guest.disconnected()


def test_guest_exit_while_waiting_closes_quietly(
    calendar, loop, console, guest, capsys
) -> None:
    guest.send(_Op.WAIT, _WAIT_FOREVER)
    _connect(calendar, loop, console, SimpleNamespace(now_us=0))
    guest.acks(1)

    guest.close()
    (on_readable,) = loop.readers.values()
    on_readable()

    assert loop.readers == {}
    assert "time-travel calendar" not in capsys.readouterr().err


def test_message_from_a_waiting_guest_disconnects_it(
    calendar, loop, console, guest, capsys
) -> None:
    guest.send(_Op.WAIT, _WAIT_FOREVER)
    _connect(calendar, loop, console, SimpleNamespace(now_us=0))
    guest.acks(1)

    guest.send(_Op.GET)
    (on_readable,) = loop.readers.values()
    on_readable()

    assert loop.readers == {}
    assert guest.disconnected()
    assert "sent a message while waiting" in capsys.readouterr().err


def test_unexpected_operation_fails_the_boot(calendar, loop, console, guest) -> None:
    guest.send(_Op.FREE_UNTIL, 1_000)
    with pytest.raises(RuntimeError, match="unexpected UML time-travel message"):
        _connect(calendar, loop, console, SimpleNamespace(now_us=0))


def test_guest_that_exits_before_connecting_fails_at_once(
    calendar, loop, console
) -> None:
    with pytest.raises(RuntimeError, match="exited before it connected"):
        calendar.connect(
            loop, SimpleNamespace(now_us=0), alive=lambda: False, console=console
        )


def test_silent_guest_times_out(calendar, loop, console, guest, monkeypatch) -> None:
    monkeypatch.setattr(timetravel, "_TIMEOUT", 0.2)
    with pytest.raises(TimeoutError, match="did not answer"):
        _connect(calendar, loop, console, SimpleNamespace(now_us=0))


def test_console_is_drained_while_the_guest_runs(
    calendar, loop, console, guest, monkeypatch
) -> None:
    # Without draining, the guest blocks on the full FIFO until the timeout.
    monkeypatch.setattr(timetravel, "_TIMEOUT", 5)
    line = b"x" * 1023 + b"\n"
    lines = 256
    # Like UML in external time-travel mode: blocking writes, then WAIT.
    output = os.dup(console.write_fd)
    os.set_blocking(output, True)

    def run_guest() -> None:
        try:
            data = memoryview(line * lines)
            while data:
                data = data[os.write(output, data) :]
            guest.send(_Op.WAIT, _WAIT_FOREVER)
        finally:
            os.close(output)

    thread = threading.Thread(target=run_guest, daemon=True)
    thread.start()
    _connect(calendar, loop, console, SimpleNamespace(now_us=0))
    thread.join(timeout=5)
    console.drain()

    assert not thread.is_alive()
    assert console.log_path.read_bytes() == line * lines
