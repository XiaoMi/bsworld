# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Simulation clocks, ready-work draining and PHY admission gates."""

import asyncio
import selectors
import socket
import time
from threading import Event, Thread
from unittest.mock import Mock

import pytest

from bscontroller.runtime.event_loop import (
    ControllerEventLoop,
    RadioEventLoop,
    RadioSelector,
)

requires_runner = pytest.mark.skipif(
    not hasattr(asyncio, 'Runner'), reason='asyncio.Runner requires Python 3.11'
)


class Clock:
    def __init__(self):
        self.now_us = 0
        self.targets = []

    def advance_to_us(self, target_us):
        self.targets.append(target_us)
        self.now_us = target_us
        return target_us


@pytest.mark.parametrize(
    'timeout,target', [(None, 1000), (0.002, 1000), (0.0000011, 1), (0.0000017, 2)]
)
def test_idle_selector_advances_bounded_simulation_time(timeout, target):
    """An idle selector advances simulation time by the bounded step.

    Purpose: Verify ``RadioSelector`` with no ready I/O advances the bound clock
    by at most its max step, honoring a shorter requested timeout rounded up to
    whole microseconds, so idle waits move simulation time deterministically.

    Steps:
        1. Build a selector over a delegate that reports no ready events, with a
           1000-us max step, and attach a recording clock.
        2. Call ``select`` with the parametrized timeout.

    Expected:
        - ``select`` returns no events and the clock advanced once to the
          expected target (full step for None/large, rounded-up microseconds
          otherwise).
    """
    delegate = Mock(spec=selectors.BaseSelector)
    delegate.select.return_value = []
    selector = RadioSelector(delegate, 1000)
    clock = selector.clock = Clock()
    assert selector.select(timeout) == []
    assert clock.targets == [target]


def test_ready_io_and_nonblocking_polls_never_advance_time():
    """Ready I/O and zero-timeout polls leave simulation time unchanged.

    Purpose: Verify ``RadioSelector`` does not advance the clock when the
    delegate already has ready I/O, nor on a non-blocking (timeout 0) poll, so
    pending work runs before time moves.

    Steps:
        1. With ready I/O reported, call ``select(1)``.
        2. With no ready I/O, call ``select(0)``.

    Expected:
        - The first call returns the ready events; the second returns none.
        - The clock recorded no advances.
    """
    delegate = Mock(spec=selectors.BaseSelector)
    delegate.select.return_value = [('ready', selectors.EVENT_READ)]
    selector = RadioSelector(delegate, 1000)
    clock = selector.clock = Clock()
    assert selector.select(1) == delegate.select.return_value
    delegate.select.return_value = []
    assert selector.select(0) == []
    assert clock.targets == []


def test_unbound_selector_keeps_wall_clock_wait():
    """An unbound selector delegates the wait unchanged to wall time.

    Purpose: Verify that without a bound clock, ``RadioSelector.select`` passes
    the timeout straight through to the delegate, preserving normal wall-clock
    waiting.

    Steps:
        1. Build a selector with no clock bound and call ``select(0.25)``.

    Expected:
        - The delegate's ``select`` was called once with 0.25.
    """
    delegate = Mock(spec=selectors.BaseSelector)
    RadioSelector(delegate, 1000).select(0.25)
    delegate.select.assert_called_once_with(0.25)


def test_invalid_step_and_clock_must_advance():
    """The selector rejects a non-positive step and a clock that stalls.

    Purpose: Verify ``RadioSelector`` requires a positive max step, and that an
    idle select raises if the bound clock fails to advance when asked to.

    Steps:
        1. Construct a selector with max step 0.
        2. Build a valid selector, bind a clock whose ``advance_to_us`` returns
           the same time, and call ``select(1)``.

    Expected:
        - Construction raises ``ValueError`` mentioning 'positive'.
        - The idle select raises ``RuntimeError`` mentioning 'did not advance'.
    """
    delegate = Mock(spec=selectors.BaseSelector)
    delegate.select.return_value = []
    with pytest.raises(ValueError, match='positive'):
        RadioSelector(delegate, 0)
    selector = RadioSelector(delegate, 1000)
    selector.clock = Mock(now_us=0, advance_to_us=Mock(return_value=0))
    with pytest.raises(RuntimeError, match='did not advance'):
        selector.select(1)


class WallClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def realtime_selector(monkeypatch, wall, waits):
    """A real-time selector whose delegate records waits and lets wall time pass."""

    def select(timeout):
        waits.append(timeout)
        wall.now += timeout or 0
        return []

    monkeypatch.setattr('bscontroller.runtime.event_loop.time.monotonic', wall)
    delegate = Mock(spec=selectors.BaseSelector)
    delegate.select.side_effect = select
    selector = RadioSelector(delegate, 1000, realtime=True)
    selector.clock = Clock()
    return selector


def test_realtime_selector_holds_each_step_to_wall_clock(monkeypatch):
    """A real-time selector waits on wall time before each simulation step.

    Purpose: Verify that with ``realtime=True`` an idle select waits on the
    delegate until wall-clock time reaches the next simulation time, then
    advances the clock, so simulation time follows wall-clock time.

    Steps:
        1. Build a real-time selector with a 1000-us step over a delegate that
           reports no I/O and lets the waited wall time pass.
        2. Call ``select(None)`` three times.

    Expected:
        - The clock advanced to 1000, 2000 and 3000 us.
        - Before each step the delegate waited 1 ms of wall time.
    """
    wall, waits = WallClock(), []
    selector = realtime_selector(monkeypatch, wall, waits)
    for _ in range(3):
        assert selector.select(None) == []
    assert selector.clock.targets == [1000, 2000, 3000]
    assert [round(wait, 6) for wait in waits if wait] == [0.001] * 3


def test_realtime_selector_does_not_catch_up_after_falling_behind(monkeypatch):
    """A real-time selector that falls behind continues from now.

    Purpose: Verify that when wall-clock time has already passed the next
    simulation time, the selector advances without waiting and then paces later
    steps from the current wall time, instead of running fast to catch up.

    Steps:
        1. Take one paced step, then move wall time 50 ms ahead.
        2. Call ``select(None)`` twice more.

    Expected:
        - The late step advanced without a wall-clock wait.
        - The following step waited a full 1 ms again.
    """
    wall, waits = WallClock(), []
    selector = realtime_selector(monkeypatch, wall, waits)
    selector.select(None)
    wall.now += 0.05
    waits.clear()
    selector.select(None)
    assert [wait for wait in waits if wait] == []
    selector.select(None)
    assert [round(wait, 6) for wait in waits if wait] == [0.001]
    assert selector.clock.targets == [1000, 2000, 3000]


def test_realtime_selector_returns_io_ready_during_the_wait(monkeypatch):
    """I/O that becomes ready during the wall-clock wait runs at the current time.

    Purpose: Verify that a real-time selector returns I/O that becomes ready
    while it waits for wall-clock time, without advancing simulation time.

    Steps:
        1. Build a real-time selector whose delegate reports one ready event
           during the wall-clock wait.
        2. Call ``select(None)``.

    Expected:
        - ``select`` returns the ready event.
        - The clock did not advance.
    """
    monkeypatch.setattr('bscontroller.runtime.event_loop.time.monotonic', WallClock())
    event = (Mock(), selectors.EVENT_READ)
    delegate = Mock(spec=selectors.BaseSelector)
    delegate.select.side_effect = lambda timeout: [event] if timeout else []
    selector = RadioSelector(delegate, 1000, realtime=True)
    selector.clock = Clock()
    assert selector.select(None) == [event]
    assert selector.clock.targets == []


@requires_runner
def test_clock_failure_unbinds_loop_so_runner_can_finalize_tasks():
    """A clock failure unbinds the loop so the runner can finalize cleanly.

    Purpose: Verify that when the bound clock's ``advance_to_us`` raises inside
    a running task, the error propagates out of the runner, the loop unbinds its
    clock, and the task's ``finally`` still runs so cleanup is not lost.

    Steps:
        1. Run a ``RadioEventLoop`` with a clock; bind it, then make
           ``advance_to_us`` raise RuntimeError('broken PHY').
        2. Await a never-completing future in a task with a finalizer.

    Expected:
        - The RuntimeError propagates from the runner.
        - The selector's clock is unbound (``None``) and the finalizer ran.
    """
    clock = Clock()
    finalized = []
    with asyncio.Runner(loop_factory=RadioEventLoop) as runner:
        loop = runner.get_loop()

        async def exercise():
            loop.bind_clock(clock)
            clock.advance_to_us = Mock(side_effect=RuntimeError('broken PHY'))
            try:
                await asyncio.Future()
            finally:
                finalized.append(True)

        with pytest.raises(RuntimeError, match='broken PHY'):
            runner.run(exercise())
        assert loop.radio_selector.clock is None
    assert finalized == [True]


@requires_runner
@pytest.mark.parametrize('loop_factory', [RadioEventLoop, ControllerEventLoop])
def test_event_loop_sleep_timeout_and_socket_use_standard_asyncio(loop_factory):
    """Standard asyncio sleep, timeout and socket I/O run on simulation time.

    Purpose: Verify that with a bound clock, ``asyncio.sleep`` advances in
    bounded microsecond steps, ``wait_for`` times out on simulation time,
    socket reads of ready data do not advance the clock, and unbinding restores
    wall-clock time, for both loop implementations.

    Steps:
        1. Bind a clock to the loop; check ``time()`` is 0 and that re-binding
           raises.
        2. Sleep 25 ms and verify the step schedule and total.
        3. Time out a never-set event after 5 ms.
        4. Read ready bytes from a socket pair and confirm no clock advance.
        5. Unbind and check ``time()`` tracks wall time.

    Expected:
        - Sleep advances in <=1000-us steps to 0.025 s; a second bind raises
          'already bound'; ``wait_for`` raises TimeoutError; the socket read
          returns the data with the clock unchanged; after unbind ``time()``
          approximates ``time.monotonic()``.
    """
    clock = Clock()
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        loop = runner.get_loop()
        loop.bind_clock(clock)
        assert loop.time() == 0
        with pytest.raises(RuntimeError, match='already bound'):
            loop.bind_clock(Clock())

        async def exercise():
            start = loop.time()
            await asyncio.sleep(0.025)
            assert loop.time() - start == pytest.approx(0.025, abs=0.000002)
            assert clock.targets[:2] == [1_000, 2_000]
            assert all(
                after - before <= 1_000
                for before, after in zip([0, *clock.targets], clock.targets)
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.Event().wait(), 0.005)
            reader, writer = socket.socketpair()
            try:
                reader.setblocking(False)
                writer.sendall(b'packet')
                now = clock.now_us
                assert await loop.sock_recv(reader, 6) == b'packet'
                assert clock.now_us == now
            finally:
                reader.close()
                writer.close()

        runner.run(exercise())
        loop.unbind_clock()
        assert loop.time() == pytest.approx(time.monotonic(), abs=0.001)


@requires_runner
def test_worker_can_enqueue_hci_but_host_cannot_execute_during_wait():
    """A worker may queue a Host callback during a wait, run after the wait.

    Purpose: Verify that while the controller loop is waiting on the clock, a
    cross-thread ``call_soon_threadsafe`` enqueues a Host callback but it does
    not execute until the wait (clock advance) completes, so Host work never
    races the PHY wait.

    Steps:
        1. Run a worker thread that, once the loop entered the wait, schedules a
           'host' callback and appends 'controller', then releases the advance.
        2. Make ``advance_to_us`` block until the worker signals, asserting only
           'controller' has run at that point.
        3. In the loop, bind the clock and drive one short timer, then unbind.

    Expected:
        - At advance time only 'controller' had executed.
        - After the wait the recorded order is ['controller', 'host'].
    """
    events = []
    entered, released = Event(), Event()
    clock = Clock()
    with asyncio.Runner(loop_factory=RadioEventLoop) as runner:
        loop = runner.get_loop()

        def worker():
            assert entered.wait(5)
            loop.call_soon_threadsafe(events.append, 'host')
            events.append('controller')
            released.set()

        def advance(target):
            entered.set()
            assert released.wait(5)
            assert events == ['controller']
            clock.now_us = target
            return target

        clock.advance_to_us = advance
        thread = Thread(target=worker)
        thread.start()

        async def exercise():
            loop.bind_clock(clock)
            completed = loop.create_future()
            loop.call_later(0.001, completed.set_result, None)
            await completed
            loop.unbind_clock()
            assert events == ['controller', 'host']

        try:
            runner.run(exercise())
        finally:
            released.set()
            thread.join(5)
            assert not thread.is_alive()


@requires_runner
def test_controller_gate_drains_nested_callbacks_tasks_and_ready_io():
    """The controller gate drains all ready work before a PHY request.

    Purpose: Verify ``before_phy_request`` flushes nested ``call_soon``
    callbacks, created tasks and ready socket I/O (including work those produce)
    before returning, and does so without advancing the clock.

    Steps:
        1. Register a reader that, on ready data, schedules a task which in turn
           schedules a notification callback.
        2. Queue a chain of 8 nested callbacks and send a byte to the socket.
        3. Await ``before_phy_request``.

    Expected:
        - It returns ``None`` with all nested callbacks run in order, the
          notification scheduled after its task, and no clock advance.
    """
    clock = Clock()
    events = []
    reader, writer = socket.socketpair()
    try:
        with asyncio.Runner(loop_factory=ControllerEventLoop) as runner:
            loop = runner.get_loop()
            loop.bind_clock(clock)

            async def notification():
                events.append('task')
                loop.call_soon(events.append, 'notification')

            def on_read():
                assert reader.recv(1) == b'x'
                loop.remove_reader(reader)
                loop.call_soon(loop.create_task, notification())

            def nested(depth):
                events.append(depth)
                if depth:
                    loop.call_soon(nested, depth - 1)

            async def exercise():
                loop.add_reader(reader, on_read)
                writer.sendall(b'x')
                loop.call_soon(nested, 8)
                assert await loop.before_phy_request() is None
                assert [item for item in events if isinstance(item, int)] == list(
                    range(8, -1, -1)
                )
                assert events.index('notification') > events.index('task')
                assert clock.targets == []
                loop.unbind_clock()

            runner.run(exercise())
    finally:
        reader.close()
        writer.close()


@requires_runner
def test_controller_gate_services_timer_before_future_radio_start():
    """The gate runs a timer due before a future radio start, keeping RF time.

    Purpose: Verify ``before_phy_request(start_us=...)`` lets a timer that is
    due before the requested radio start fire (advancing the clock only to the
    timer), returning no limit and leaving simulation time short of the start.

    Steps:
        1. Schedule a 2 ms timer that records the clock when it fires.
        2. Call ``before_phy_request`` with a 3000-us start.

    Expected:
        - The timer fired at ~2000 us; the returned limit is ``None`` and the
          clock is still below 3000 us.
    """
    clock = Clock()
    events = []
    with asyncio.Runner(loop_factory=ControllerEventLoop) as runner:
        loop = runner.get_loop()
        loop.bind_clock(clock)

        async def exercise():
            loop.call_later(0.002, lambda: events.append(clock.now_us))
            limit = await loop.before_phy_request(start_us=3_000)
            assert events == [pytest.approx(2_000, abs=1)]
            assert limit is None
            assert clock.now_us < 3_000
            loop.unbind_clock()

        runner.run(exercise())


@requires_runner
def test_controller_wait_deadline_uses_simulation_timer():
    """With no radio start the gate returns the next timer's deadline.

    Purpose: Verify ``before_phy_request`` with no ``start_us`` returns the
    simulation-time deadline of the earliest pending timer without advancing the
    clock, so the caller can bound its radio wait by that timer.

    Steps:
        1. Schedule a 0.5 ms timer.
        2. Call ``before_phy_request`` and read the returned limit.

    Expected:
        - The returned limit approximates 500 us and the clock did not advance.
    """
    clock = Clock()
    with asyncio.Runner(loop_factory=ControllerEventLoop) as runner:
        loop = runner.get_loop()
        loop.bind_clock(clock)

        async def exercise():
            timer = loop.call_later(0.0005, lambda: None)
            try:
                limit = await loop.before_phy_request()
                assert limit == pytest.approx(500, abs=1)
                assert clock.targets == []
            finally:
                timer.cancel()
                loop.unbind_clock()

        runner.run(exercise())


@requires_runner
def test_controller_rejects_overlapping_requests_and_clears_cancelled_gate():
    """Only one PHY request is allowed, and a cancelled one clears the gate.

    Purpose: Verify ``before_phy_request`` rejects a second concurrent request,
    that cancelling the first clears the pending gate so a new request
    succeeds, and that ``interrupt`` cancels a pending request.

    Steps:
        1. Start a first ``before_phy_request`` task and let it register.
        2. Attempt a second request; cancel the first and await its
           cancellation.
        3. Issue a fresh request, then call ``interrupt`` and await another.

    Expected:
        - The second request raises RuntimeError 'one pending PHY request'.
        - After cancel the pending gate is ``None`` and a new request returns
          ``None``; after ``interrupt`` the next request raises CancelledError.
    """
    with asyncio.Runner(loop_factory=ControllerEventLoop) as runner:
        loop = runner.get_loop()

        async def exercise():
            first = asyncio.create_task(loop.before_phy_request())
            await asyncio.sleep(0)
            with pytest.raises(RuntimeError, match='one pending PHY request'):
                await loop.before_phy_request()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert loop.radio_selector.pending is None
            assert await loop.before_phy_request() is None
            loop.interrupt()
            with pytest.raises(asyncio.CancelledError):
                await loop.before_phy_request()

        runner.run(exercise())


@requires_runner
def test_controller_phy_io_watchdog_and_closed_loop_interrupt():
    """The PHY I/O wait times out on its deadline and a closed loop interrupts.

    Purpose: Verify ``wait_for_phy_io`` raises a descriptive TimeoutError when
    its deadline has already passed with no I/O, and that ``interrupt`` is safe
    to call after the loop is closed.

    Steps:
        1. Call ``wait_for_phy_io`` for a readable socket with a deadline in the
           past.
        2. After the runner exits (loop closed), call ``interrupt``.

    Expected:
        - ``wait_for_phy_io`` raises TimeoutError mentioning 'waiting for
          BabbleSim'.
        - ``interrupt`` on the closed loop does not raise.
    """
    reader, writer = socket.socketpair()
    try:
        with asyncio.Runner(loop_factory=ControllerEventLoop) as runner:
            loop = runner.get_loop()
            with pytest.raises(TimeoutError, match='waiting for BabbleSim'):
                loop.wait_for_phy_io(
                    reader.fileno(), write=False, deadline=time.monotonic()
                )
        loop.interrupt()
    finally:
        reader.close()
        writer.close()


@pytest.fixture(params=[RadioEventLoop, ControllerEventLoop])
def simulation_loop(request, monkeypatch):
    loop = request.param()
    clock = Clock()
    clock.now_us = 1234567
    loop.bind_clock(clock)
    select = loop.radio_selector.select
    polls = 0

    def checked_select(timeout=None):
        nonlocal polls
        polls += 1
        assert polls < 100, 'timer made no progress after rounding to microseconds'
        return select(timeout)

    monkeypatch.setattr(loop.radio_selector, 'select', checked_select)
    try:
        yield loop, clock
    finally:
        loop.unbind_clock()
        loop.close()


@pytest.mark.parametrize('delay_us', [1, 2])
def test_sleep_uses_integer_microseconds_without_accumulating_error(
    simulation_loop, delay_us
):
    """Repeated short sleeps advance by exact integer microseconds.

    Purpose: Verify successive ``asyncio.sleep`` calls of a few microseconds
    advance the bound clock by exact integer multiples with no accumulated
    rounding drift.

    Steps:
        1. On a bound simulation loop, sleep ``delay_us`` microseconds five
           times, recording the elapsed simulation time each round.

    Expected:
        - The observed offsets are ``delay_us * n`` for n in 1..5.
    """
    loop, clock = simulation_loop
    start_us = clock.now_us

    async def scenario():
        observed = []
        for _ in range(5):
            await asyncio.sleep(delay_us / 1_000_000)
            observed.append(clock.now_us - start_us)
        return observed

    assert loop.run_until_complete(scenario()) == [delay_us * n for n in range(1, 6)]


@pytest.mark.parametrize('delay', [0, 1])
def test_clock_binding_rejects_active_timers_without_changing_them(
    simulation_loop, delay
):
    """Binding a clock is refused while wall timers are active, leaving them intact.

    Purpose: Verify ``bind_clock`` refuses when a non-cancelled wall-clock timer
    is still armed (so wall and simulation time cannot mix), does not disturb
    that timer, and succeeds once the timer is cancelled.

    Steps:
        1. Unbind the clock, arm a cancelled timer and an active timer, and
           record the active timer's deadline.
        2. Attempt ``bind_clock``.
        3. Cancel the active timer and bind again.

    Expected:
        - The first bind raises RuntimeError 'active timers'; the clock stays
          unbound and the active timer's deadline and state are unchanged.
        - After cancelling, binding succeeds and ``time()`` matches the clock.
    """
    loop, clock = simulation_loop
    loop.unbind_clock()
    cancelled = loop.call_later(0, pytest.fail, 'cancelled wall timer fired')
    cancelled.cancel()
    timer = loop.call_later(delay, pytest.fail, 'wall timer crossed clock binding')
    deadline = timer.when()

    with pytest.raises(RuntimeError, match='active timers'):
        loop.bind_clock(clock)
    assert loop.radio_selector.clock is None
    assert timer.when() == deadline
    assert not timer.cancelled()

    timer.cancel()
    loop.bind_clock(clock)
    assert loop.time() == clock.now_us / 1_000_000


def test_timer_fires_at_deadline_with_large_simulation_time(simulation_loop):
    """A microsecond timer still fires at a very large simulation time.

    Purpose: Verify a 1-us timer scheduled when simulation time is large (where
    asyncio's nanosecond due window would round to zero) still fires exactly one
    microsecond later.

    Steps:
        1. Set the clock to a large microsecond value.
        2. Schedule a 1-us timer that resolves a future and run until done.

    Expected:
        - The clock advanced by exactly one microsecond when the timer fired.
    """
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
    """Ready callbacks run at the current time, not the next timer's time.

    Purpose: Verify a ready ``call_soon`` callback executes at the present
    simulation time while a 1-us timer fires only after the clock advances, so
    ready work never borrows a future timer's timestamp.

    Steps:
        1. Schedule a 1-us timer that records the clock, and a ``call_soon``
           callback that records the clock.
        2. Run until the timer completes.

    Expected:
        - The timer observed ``start_us + 1``; the ready callback observed the
          original ``start_us``.
    """
    loop, clock = simulation_loop
    observed = []
    start_us = clock.now_us
    done = loop.create_future()
    loop.call_at(loop.time() + 0.000001, lambda: done.set_result(clock.now_us))
    loop.call_soon(lambda: observed.append(clock.now_us))

    assert loop.run_until_complete(done) == start_us + 1
    assert observed == [start_us]


def test_timeout_and_cancellation_use_integer_microseconds(simulation_loop):
    """Timeouts and sleeps round to integer microseconds; cancelled timers skip.

    Purpose: Verify ``wait_for`` times out after exactly one microsecond, a
    cancelled 1-us timer never fires, and a following 2-us sleep advances to the
    expected integer-microsecond offset.

    Steps:
        1. Arm and immediately cancel a 1-us timer.
        2. ``wait_for`` a never-set event with a 1-us timeout.
        3. Sleep 2 us.

    Expected:
        - ``wait_for`` raises TimeoutError at ``start_us + 1``; the cancelled
          timer does not fire; after the sleep the clock is ``start_us + 3``.
    """
    loop, clock = simulation_loop
    start_us = clock.now_us
    cancelled = loop.call_later(0.000001, pytest.fail, 'cancelled timer fired')
    cancelled.cancel()

    async def scenario():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.Event().wait(), 0.000001)
        assert clock.now_us == start_us + 1
        await asyncio.sleep(0.000002)
        assert clock.now_us == start_us + 3

    loop.run_until_complete(scenario())


@requires_runner
@pytest.mark.parametrize(
    'delay_us,expected_events,expected_limit', [(500, [500], None), (501, [], 501)]
)
def test_controller_gate_uses_the_same_rounded_deadline_as_timers(
    delay_us, expected_events, expected_limit
):
    """The PHY gate and timers share one rounded deadline comparison.

    Purpose: Verify ``before_phy_request(start_us=501)`` fires a timer whose
    rounded deadline is at or before the start (running it and returning no
    limit), but returns the start as a limit when the timer is later, using the
    same microsecond rounding as timers.

    Steps:
        1. Schedule a timer at ``delay_us`` microseconds that records the clock.
        2. Call ``before_phy_request`` with ``start_us=501``.

    Expected:
        - For 500 us the timer runs and the limit is ``None``; for 501 us the
          timer does not run and the returned limit is 501.
    """
    clock = Clock()
    with asyncio.Runner(loop_factory=ControllerEventLoop) as runner:
        loop = runner.get_loop()
        loop.bind_clock(clock)
        events = []

        async def scenario():
            timer = loop.call_later(
                delay_us / 1_000_000, lambda: events.append(clock.now_us)
            )
            try:
                assert await loop.before_phy_request(start_us=501) == expected_limit
                assert events == expected_events
            finally:
                timer.cancel()
                loop.unbind_clock()

        runner.run(scenario())
