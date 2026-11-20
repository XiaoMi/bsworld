# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Protocol contracts of the UML time-travel calendar, without a kernel.

A socket client plays the guest. Most tests use a fake loop to inspect the
calendar's timers and select when to fire them.
"""

from __future__ import annotations

import asyncio
import os
import select
import socket
import threading
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.helpers import bind_test_clock

from bsworld.runtime.pipe import LogCapture
from bsworld.uml import timetravel
from bsworld.uml.timetravel import TimeTravelCalendar, _Op

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
def failures():
    return []


@pytest.fixture
def calendar(tmp_path, failures):
    calendar = TimeTravelCalendar(tmp_path / "guest.tt", on_failure=failures.append)
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

    # The guest acknowledges the grant and requests the next microsecond.
    guest.send(_Op.ACK)
    guest.send(_Op.ACK)
    get = guest.send(_Op.GET)
    request = guest.send(_Op.REQUEST, 2_001_000)
    wait = guest.send(_Op.WAIT, _WAIT_FOREVER)
    clock.now_us = 2_000
    timer.fire()

    assert guest.receive() == (_Op.FREE_UNTIL, 1, 2_001_000)
    assert guest.receive() == (_Op.RUN, 2, 2_000_999)
    assert guest.receive() == (_Op.ACK, get, 2_000_999)
    assert guest.acks(2) == [request, wait]
    (next_timer,) = loop.pending()
    assert next_timer.when == _us(2_001)

    wait = guest.send(_Op.WAIT, _WAIT_FOREVER)
    clock.now_us = 2_001
    next_timer.fire()

    assert guest.receive() == (_Op.FREE_UNTIL, 3, 2_002_000)
    assert guest.receive() == (_Op.RUN, 4, 2_001_000)
    assert guest.acks(1) == [wait]
    assert loop.pending() == []


def test_request_within_the_current_microsecond_runs_without_a_timer(
    calendar, loop, console, guest
) -> None:
    clock = SimpleNamespace(now_us=2_000)
    guest.send(_Op.REQUEST, 2_000_400)
    guest.send(_Op.WAIT, _WAIT_FOREVER)
    guest.send(_Op.ACK)
    guest.send(_Op.ACK)
    guest.send(_Op.REQUEST, 5_000_000)
    guest.send(_Op.WAIT, _WAIT_FOREVER)

    _connect(calendar, loop, console, clock)

    guest.acks(2)
    assert guest.receive() == (_Op.FREE_UNTIL, 1, 2_001_000)
    assert guest.receive() == (_Op.RUN, 2, 2_000_400)
    guest.acks(2)
    assert [timer.when for timer in loop.pending()] == [_us(5_000)]


async def test_two_calendars_run_serially_at_the_same_simulated_time(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(timetravel, "_TIMEOUT", 1)
    loop = asyncio.get_running_loop()
    guests = {}

    with bind_test_clock(lambda: None), ExitStack() as resources:
        clock = loop.radio_selector.clock
        for label, target_ns in (("first", 2_000_400), ("second", 2_000_900)):
            calendar = TimeTravelCalendar(tmp_path / f"{label}.tt")
            calendar.listen()
            resources.callback(calendar.close)
            guest = _Guest(calendar.path)
            resources.callback(guest.close)
            guest.sock.settimeout(1)
            console = LogCapture.open(tmp_path / f"{label}.log")
            resources.callback(console.stop)
            request = guest.send(_Op.REQUEST, target_ns)
            wait = guest.send(_Op.WAIT, _WAIT_FOREVER)
            _connect(calendar, loop, console, clock)
            assert guest.acks(2) == [request, wait]
            guests[guest.sock] = (guest, target_ns)

        def exchange_turns() -> None:
            ready, _, _ = select.select(list(guests), [], [], 1)
            (first_sock,) = ready
            (second_sock,) = (sock for sock in guests if sock is not first_sock)

            for sock in (first_sock, second_sock):
                guest, target_ns = guests[sock]
                assert guest.receive() == (_Op.FREE_UNTIL, 1, 2_001_000)
                assert guest.receive() == (_Op.RUN, 2, target_ns)
                get = guest.send(_Op.GET)
                assert guest.receive() == (_Op.ACK, get, target_ns)
                assert clock.now_us == 2_000
                if sock is first_sock:
                    # Keep the first guest running long enough to detect another grant.
                    assert not select.select([second_sock], [], [], 0.05)[0]
                wait = guest.send(_Op.WAIT, _WAIT_FOREVER)
                assert guest.acks(1) == [wait]

        await asyncio.to_thread(exchange_turns)


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
    assert guest.receive() == (_Op.FREE_UNTIL, 1, resumed_ns + 1_000)
    assert guest.receive() == (_Op.RUN, 2, resumed_ns)


def test_wait_without_a_request_arms_no_timer(calendar, loop, console, guest) -> None:
    guest.send(_Op.WAIT, _WAIT_FOREVER)
    _connect(calendar, loop, console, SimpleNamespace(now_us=0))
    guest.acks(1)
    assert loop.pending() == []
    assert list(loop.readers) == [calendar._conn]


def test_reads_a_frame_across_short_socket_reads(
    calendar, loop, console, guest, monkeypatch
) -> None:
    def short_read(fd: int, size: int) -> bytes:
        return os.read(fd, min(size, 5))

    monkeypatch.setattr(timetravel, "os", SimpleNamespace(read=short_read))
    request = guest.send(_Op.REQUEST, 5_000_000)
    wait = guest.send(_Op.WAIT, _WAIT_FOREVER)
    _connect(calendar, loop, console, SimpleNamespace(now_us=1_000))

    assert guest.acks(2) == [request, wait]
    assert [timer.when for timer in loop.pending()] == [_us(5_000)]


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
    calendar, loop, console, guest, failures, capsys
) -> None:
    guest.send(_Op.WAIT, _WAIT_FOREVER)
    _connect(calendar, loop, console, SimpleNamespace(now_us=0))
    guest.acks(1)

    guest.close()
    (on_readable,) = loop.readers.values()
    on_readable()

    assert loop.readers == {}
    (failure,) = failures
    assert isinstance(failure, EOFError)
    assert "exited while waiting" in str(failure)
    assert "time-travel calendar" not in capsys.readouterr().err


def test_message_from_a_waiting_guest_disconnects_it(
    calendar, loop, console, guest, failures
) -> None:
    guest.send(_Op.WAIT, _WAIT_FOREVER)
    _connect(calendar, loop, console, SimpleNamespace(now_us=0))
    guest.acks(1)

    guest.send(_Op.GET)
    (on_readable,) = loop.readers.values()
    on_readable()

    assert loop.readers == {}
    assert guest.disconnected()
    (failure,) = failures
    assert isinstance(failure, RuntimeError)
    assert "sent a message while waiting" in str(failure)


def test_guest_exit_during_a_turn_reports_failure(
    calendar, loop, console, guest, failures
) -> None:
    clock = SimpleNamespace(now_us=0)
    request = guest.send(_Op.REQUEST, 2_000_000)
    wait = guest.send(_Op.WAIT, _WAIT_FOREVER)
    _connect(calendar, loop, console, clock)
    assert guest.acks(2) == [request, wait]

    (timer,) = loop.pending()
    guest.sock.shutdown(socket.SHUT_WR)
    clock.now_us = 2_000
    timer.fire()

    assert guest.receive() == (_Op.FREE_UNTIL, 1, 2_001_000)
    assert guest.receive() == (_Op.RUN, 2, 2_000_000)
    assert guest.disconnected()
    assert loop.readers == {}
    (failure,) = failures
    assert isinstance(failure, EOFError)
    assert "closed its time-travel socket" in str(failure)


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
    with pytest.raises(TimeoutError, match="did not yield"):
        _connect(calendar, loop, console, SimpleNamespace(now_us=0))


def test_guest_must_yield_even_when_it_keeps_requesting_time(
    calendar, loop, console, guest, monkeypatch
) -> None:
    wall_clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        timetravel, "time", SimpleNamespace(monotonic=lambda: wall_clock.now)
    )
    monkeypatch.setattr(timetravel, "_TIMEOUT", 2)
    answer = calendar._answer

    def answer_after_delay(op: int, seq: int, value: int) -> None:
        answer(op, seq, value)
        wall_clock.now += 1

    monkeypatch.setattr(calendar, "_answer", answer_after_delay)
    requests = [guest.send(_Op.GET) for _ in range(3)]
    guest.send(_Op.WAIT, _WAIT_FOREVER)

    with pytest.raises(TimeoutError, match="did not yield within 2 s"):
        _connect(calendar, loop, console, SimpleNamespace(now_us=0))

    assert guest.acks(2) == requests[:2]


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
