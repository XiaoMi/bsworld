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

from __future__ import annotations

import asyncio
import socket
import time

import pytest
from tests.unit.scheduler_support import ManualClock, run_on_clock

from bsworld.runtime.scheduler import BsimEventLoop


def test_asyncio_sleep_uses_discrete_time() -> None:
    clock = ManualClock()

    async def scenario() -> float:
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        await asyncio.sleep(3_600)
        return loop.time() - started_at

    wall_started_at = time.monotonic()
    elapsed = run_on_clock(scenario(), clock=clock, max_step_us=3_600_000_000)

    assert elapsed == pytest.approx(3_600)
    assert clock.targets_us == [3_600_000_000]
    assert time.monotonic() - wall_started_at < 0.5


def test_asyncio_wait_for_uses_discrete_timeout() -> None:
    clock = ManualClock()

    async def scenario() -> float:
        started_at = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.Event().wait(), timeout=7.5)
        return asyncio.get_running_loop().time() - started_at

    assert run_on_clock(scenario(), clock=clock, max_step_us=20_000_000) == pytest.approx(7.5)


def test_callback_chain_is_drained_before_time_advances() -> None:
    callbacks: list[str] = []
    snapshots: list[tuple[str, ...]] = []
    clock = ManualClock(
        before_advance=lambda _now, _target: snapshots.append(tuple(callbacks))
    )

    async def scenario() -> None:
        loop = asyncio.get_running_loop()

        def second() -> None:
            callbacks.append("second")

        def first() -> None:
            callbacks.append("first")
            loop.call_soon(second)

        loop.call_soon(first)
        await asyncio.sleep(1)

    run_on_clock(scenario(), clock=clock, max_step_us=1_000_000)

    assert callbacks == ["first", "second"]
    assert snapshots[0] == ("first", "second")


def test_queue_event_and_future_complete_before_time_advances() -> None:
    activity: list[str] = []
    snapshots: list[tuple[str, ...]] = []
    clock = ManualClock(
        before_advance=lambda _now, _target: snapshots.append(tuple(activity))
    )

    async def scenario() -> None:
        queue: asyncio.Queue[int] = asyncio.Queue()
        completed = asyncio.Event()

        async def consume() -> None:
            value = await queue.get()
            activity.append(f"consumed:{value}")
            completed.set()

        task = asyncio.create_task(consume())
        asyncio.get_running_loop().call_soon(queue.put_nowait, 42)
        await completed.wait()
        await task
        await asyncio.sleep(1)

    run_on_clock(scenario(), clock=clock, max_step_us=1_000_000)

    assert activity == ["consumed:42"]
    assert snapshots[0] == ("consumed:42",)


def test_socket_data_is_consumed_before_the_next_step() -> None:
    activity: list[str] = []
    snapshots: list[tuple[int, tuple[str, ...]]] = []
    clock = ManualClock(
        before_advance=lambda now, _target: snapshots.append((now, tuple(activity)))
    )

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        reader, writer = socket.socketpair()
        reader.setblocking(False)
        writer.setblocking(False)
        try:

            async def send_later() -> None:
                await asyncio.sleep(1)
                await loop.sock_sendall(writer, b"hci")
                activity.append("sent")

            sender = asyncio.create_task(send_later())
            data = await loop.sock_recv(reader, 3)
            activity.append(f"received:{data.decode()}")
            await sender
            await asyncio.sleep(1)
        finally:
            reader.close()
            writer.close()

    run_on_clock(scenario(), clock=clock, max_step_us=1_000_000)

    assert activity == ["sent", "received:hci"]
    assert snapshots == [
        (0, ()),
        (1_000_000, ("sent", "received:hci")),
    ]


async def test_asyncio_unix_streams_complete_before_time_advances(tmp_path) -> None:
    loop = asyncio.get_running_loop()
    assert isinstance(loop, BsimEventLoop)
    activity: list[str] = []
    snapshots: list[tuple[str, ...]] = []
    clock = ManualClock(
        before_advance=lambda _now, _target: snapshots.append(tuple(activity))
    )
    socket_path = tmp_path / "stream.sock"

    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        data = await reader.readexactly(4)
        activity.append(f"server:{data.decode()}")
        writer.write(b"pong")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle_client, path=socket_path)
    loop.bind_clock(clock)
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        writer.write(b"ping")
        await writer.drain()
        response = await reader.readexactly(4)
        activity.append(f"client:{response.decode()}")
        writer.close()
        await writer.wait_closed()

        await asyncio.sleep(0.01)
    finally:
        loop.unbind_clock(clock)
        server.close()
        await server.wait_closed()

    assert activity == ["server:ping", "client:pong"]
    assert snapshots[0] == ("server:ping", "client:pong")


@pytest.mark.parametrize("start_us", [0, 1234567])
def test_clock_switch_uses_each_clocks_own_time(monkeypatch, start_us) -> None:
    monkeypatch.setattr(time, "monotonic", lambda: 524288.123456789)
    loop = BsimEventLoop()
    clock = ManualClock()
    clock.now_us = start_us
    wall_resolution = loop._clock_resolution
    try:
        assert loop.time() == time.monotonic()
        loop.bind_clock(clock)
        assert loop.time() == start_us / 1_000_000
        clock.advance_to_us(start_us + 2_000_000)
        assert loop.time() == (start_us + 2_000_000) / 1_000_000
        loop.unbind_clock(clock)
        assert loop.time() == time.monotonic()
        assert loop._clock_resolution == wall_resolution
    finally:
        loop.close()


async def test_pytest_loop_uses_wall_time_until_bsim_clock_is_bound() -> None:
    loop = asyncio.get_running_loop()
    assert isinstance(loop, BsimEventLoop)
    assert loop.discrete_clock is None

    started_at = time.monotonic()
    await asyncio.sleep(0.01)
    assert time.monotonic() - started_at >= 0.005


@pytest.fixture
def simulation_loop(monkeypatch):
    loop = BsimEventLoop()
    clock = ManualClock()
    clock.now_us = 1234567
    loop.bind_clock(clock)
    select = loop.bsim_selector.select
    polls = 0

    def checked_select(timeout=None):
        nonlocal polls
        polls += 1
        assert polls < 100, "timer made no progress after rounding to microseconds"
        return select(timeout)

    monkeypatch.setattr(loop.bsim_selector, "select", checked_select)
    try:
        yield loop, clock
    finally:
        loop.unbind_clock(clock)
        loop.close()


@pytest.mark.parametrize("delay_us", [1, 2])
def test_sleep_uses_integer_microseconds_without_accumulating_error(simulation_loop, delay_us):
    loop, clock = simulation_loop
    start_us = clock.now_us

    async def scenario():
        observed = []
        for _ in range(5):
            await asyncio.sleep(delay_us / 1_000_000)
            observed.append(clock.now_us - start_us)
        return observed

    assert loop.run_until_complete(scenario()) == [delay_us * n for n in range(1, 6)]


@pytest.mark.parametrize("delay", [0, 1])
def test_clock_binding_rejects_active_timers_without_changing_them(simulation_loop, delay):
    loop, clock = simulation_loop
    loop.unbind_clock(clock)
    cancelled = loop.call_later(0, pytest.fail, "cancelled wall timer fired")
    cancelled.cancel()
    timer = loop.call_later(delay, pytest.fail, "wall timer crossed clock binding")
    deadline = timer.when()

    with pytest.raises(RuntimeError, match="active timers"):
        loop.bind_clock(clock)
    assert loop.discrete_clock is None
    assert timer.when() == deadline
    assert not timer.cancelled()

    timer.cancel()
    loop.bind_clock(clock)
    assert loop.time() == clock.now_us / 1_000_000


def test_timer_fires_at_deadline_with_large_simulation_time(simulation_loop):
    loop, clock = simulation_loop
    # At this magnitude asyncio's default nanosecond due window rounds to zero.
    clock.now_us = 33_554_432_000_000
    start_us = clock.now_us
    done = loop.create_future()
    timer = loop.call_later(0.000001, done.set_result, None)
    try:
        loop.run_until_complete(done)
        assert clock.now_us == start_us + 1
    finally:
        timer.cancel()


def test_ready_work_does_not_run_the_next_microsecond_timer_early(simulation_loop):
    loop, clock = simulation_loop
    observed = []
    start_us = clock.now_us
    done = loop.create_future()
    loop.call_at(loop.time() + 0.000001, lambda: done.set_result(clock.now_us))
    loop.call_soon(lambda: observed.append(clock.now_us))

    assert loop.run_until_complete(done) == start_us + 1
    assert observed == [start_us]


def test_timeout_and_cancellation_use_integer_microseconds(simulation_loop):
    loop, clock = simulation_loop
    start_us = clock.now_us
    cancelled = loop.call_later(0.000001, pytest.fail, "cancelled timer fired")
    cancelled.cancel()

    async def scenario():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.Event().wait(), 0.000001)
        assert clock.now_us == start_us + 1
        await asyncio.sleep(0.000002)
        assert clock.now_us == start_us + 3

    loop.run_until_complete(scenario())
