# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""BabbleSim session lifecycle and the standalone simulation clock.

Covers ``BabbleSimSession`` and ``BabbleSimClock``:

- a session reuses an existing Host loop (no own clock) or, when a clock device
  is configured, binds a ``RadioEventLoop`` only after the startup barrier and
  unbinds it before stopping workers;
- startup and shutdown order: all workers are started and, on failure or exit,
  every worker is closed before the Host grant is released;
- duplicate controller or clock device identities are rejected, and an empty
  topology is rejected;
- ``BabbleSimClock.advance_to_us`` advances only after the PHY acknowledges and
  rejects non-monotonic targets.

Fake workers use ``SimpleNamespace`` with ``AsyncMock``; the ``endpoints``
fixture supplies a FIFO-backed clock/PHY pair.
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bscontroller.radio.babblesim import (
    HEADER,
    NEVER,
    TIME,
    WAIT,
    WAIT_END,
)
from bscontroller.runtime.session import BabbleSimClock, BabbleSimSession

requires_runner = pytest.mark.skipif(
    not hasattr(asyncio, 'Runner'), reason='Controller loops require Python 3.11'
)

requires_pipe2 = pytest.mark.skipif(
    not hasattr(os, 'pipe2'), reason='FIFO tests require os.pipe2'
)


@pytest.mark.asyncio
async def test_session_can_use_existing_host_loop_without_its_own_clock():
    """Without a clock device the session runs on the caller's Host loop.

    Purpose: Verify a ``BabbleSimSession`` created with no clock device does not
    install its own loop or clock, starts and closes its workers, and leaves the
    caller's running loop in place.

    Steps:
        1. Enter the session on the existing running loop with one fake worker.
        2. Inside the context, check the running loop and that the session has
           no clock or loop of its own.
        3. Exit the context.

    Expected:
        - The running loop is unchanged; ``session.clock`` and ``session.loop``
          are ``None``.
        - The worker's ``start`` is awaited once with the sim name and id, and
          ``close`` is awaited once on exit.
    """
    worker = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
    loop = asyncio.get_running_loop()
    async with BabbleSimSession('test', [(0, worker)]) as session:
        assert asyncio.get_running_loop() is loop
        assert session.clock is None and session.loop is None
        worker.start.assert_awaited_once_with('test', 0)
    worker.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_standalone_session_binds_clock_after_startup_barrier(monkeypatch):
    """A standalone session binds its clock only after the startup barrier.

    Purpose: Verify a session with a clock device performs startup in order:
    start workers, connect the clock, synchronize at the barrier, then bind the
    loop's clock; and shutdown in reverse: unbind, stop workers, disconnect.

    Steps:
        1. Build a session with one worker and a clock device, recording each
           step the worker and clock connection perform.
        2. Monkeypatch the loop's ``bind_clock``/``unbind_clock`` to record.
        3. Enter and exit the session context.

    Expected:
        - On entry the order is start, connect, barrier, bind, and
          ``session.loop`` is the running loop.
        - On exit the full order is start, connect, barrier, bind, unbind, stop,
          disconnect.
    """
    events = []
    worker = SimpleNamespace(
        start=AsyncMock(side_effect=lambda *args: events.append('start')),
        close=AsyncMock(side_effect=lambda: events.append('stop')),
    )
    session = BabbleSimSession('test', [(0, worker)], clock_dev_id=1)
    session.clock.connection.connect = AsyncMock(
        side_effect=lambda timeout: events.append('connect')
    )
    session.clock.connection.wait_sync = lambda *args: events.append('barrier')
    session.clock.connection.close = lambda: events.append('disconnect')
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, 'bind_clock', lambda clock: events.append('bind'), raising=False
    )
    monkeypatch.setattr(
        loop, 'unbind_clock', lambda: events.append('unbind'), raising=False
    )
    async with session:
        assert session.loop is loop
        assert events == ['start', 'connect', 'barrier', 'bind']
    assert events == [
        'start',
        'connect',
        'barrier',
        'bind',
        'unbind',
        'stop',
        'disconnect',
    ]


@pytest.mark.asyncio
async def test_session_start_failure_closes_started_and_unstarted_workers():
    """A failed worker start closes every worker before propagating.

    Purpose: Verify that when one worker's ``start`` raises during session
    entry, the session closes both the already-started worker and the failing
    one before re-raising the error.

    Steps:
        1. Build a session with a first worker that starts cleanly and a second
           whose ``start`` raises RuntimeError('broken worker').
        2. Call ``__aenter__`` and expect the error.

    Expected:
        - RuntimeError('broken worker') propagates.
        - Both workers' ``close`` are awaited once.
    """
    first = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
    second = SimpleNamespace(
        start=AsyncMock(side_effect=RuntimeError('broken worker')), close=AsyncMock()
    )
    session = BabbleSimSession('test', [(0, first), (1, second)])
    with pytest.raises(RuntimeError, match='broken worker'):
        await session.__aenter__()
    first.close.assert_awaited_once()
    second.close.assert_awaited_once()


def test_session_requires_at_least_one_controller():
    """A session rejects an empty controller topology.

    Purpose: Verify ``BabbleSimSession`` refuses to construct with no
    controllers, since a simulation needs at least one participant.

    Steps:
        1. Construct a session with an empty worker list.

    Expected:
        - ``ValueError`` mentioning 'at least one' is raised.
    """
    with pytest.raises(ValueError, match='at least one'):
        BabbleSimSession('test', [])


@pytest.mark.asyncio
async def test_session_stops_all_workers_before_releasing_host_grant():
    """Shutdown stops every worker before releasing the Host grant.

    Purpose: Verify that on exit the session unbinds the clock, closes all
    workers even when one close raises, and only then releases the clock
    connection, propagating the worker failure.

    Steps:
        1. Build a session with a clock device and two workers whose ``close``
           record their name; the first also raises.
        2. Stub the loop's ``unbind_clock`` and the clock connection's
           ``close`` to record.
        3. Call ``__aexit__`` and expect the error.

    Expected:
        - RuntimeError('broken worker') propagates.
        - The recorded order is unbind, first, second, release.
    """
    events = []

    async def close(name, fail=False):
        events.append(name)
        if fail:
            raise RuntimeError('broken worker')

    workers = [
        SimpleNamespace(close=lambda: close('first', True)),
        SimpleNamespace(close=lambda: close('second')),
    ]
    session = BabbleSimSession('test', list(enumerate(workers)), clock_dev_id=2)
    session.loop = SimpleNamespace(unbind_clock=lambda: events.append('unbind'))
    session.clock.connection.close = lambda: events.append('release')
    with pytest.raises(RuntimeError, match='broken worker'):
        await session.__aexit__(None, None, None)
    assert events == ['unbind', 'first', 'second', 'release']


@pytest.mark.asyncio
async def test_session_preserves_body_and_worker_failures():
    """Shutdown preserves the scenario exception and identifies both failed workers."""
    body = ValueError('scenario failed')
    first, second = RuntimeError('radio failed'), OSError('socket failed')
    workers = [
        (dev_id, SimpleNamespace(start=AsyncMock(), close=AsyncMock(side_effect=error)))
        for dev_id, error in ((3, first), (7, second))
    ]
    with pytest.raises(ValueError) as failure:
        async with BabbleSimSession('cleanup', workers):
            raise body
    assert failure.value is body
    assert all(text in body.__notes__[0] for text in ('Controller 3', 'radio failed'))
    assert all(text in body.__notes__[1] for text in ('Controller 7', 'socket failed'))


@pytest.mark.asyncio
async def test_session_worker_failure_preserves_task_cancellation():
    """A cleanup failure remains visible while the caller receives CancelledError."""
    worker = SimpleNamespace(
        start=AsyncMock(), close=AsyncMock(side_effect=RuntimeError('radio failed'))
    )
    entered = asyncio.Event()

    async def scenario():
        async with BabbleSimSession('cancel', [(3, worker)]):
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(scenario())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    assert task.cancelled()
    assert 'radio failed' in cancelled.value.__notes__[0]


@pytest.mark.asyncio
async def test_session_timeout_during_cleanup_overrides_body_error():
    """A timeout during shutdown reaches the caller after the clock is closed."""
    timeout = asyncio.timeout(None)

    async def close_worker():
        timeout.reschedule(asyncio.get_running_loop().time())
        await asyncio.Future()

    worker = SimpleNamespace(start=AsyncMock(), close=close_worker)
    clock = SimpleNamespace(close=Mock(side_effect=OSError('clock close failed')))
    body_error = ValueError('scenario failed')
    with pytest.raises(TimeoutError) as timed_out:
        async with timeout, BabbleSimSession('timeout', [(0, worker)]) as session:
            session.clock = clock
            raise body_error

    clock.close.assert_called_once()
    # Python 3.11's asyncio.timeout uses implicit exception chaining.
    cancelled = timed_out.value.__cause__ or timed_out.value.__context__
    assert isinstance(cancelled, asyncio.CancelledError)
    assert cancelled.__context__ is body_error
    assert 'clock close failed' in cancelled.__notes__[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['unbind', 'close'])
async def test_session_cleanup_preserves_keyboard_interrupt(stage):
    """KeyboardInterrupt during clock cleanup propagates over a scenario failure."""
    interrupt = KeyboardInterrupt()
    worker = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
    clock = SimpleNamespace(
        close=Mock(side_effect=interrupt if stage == 'close' else None),
    )
    loop = SimpleNamespace(
        unbind_clock=Mock(side_effect=interrupt if stage == 'unbind' else None),
    )
    with pytest.raises(KeyboardInterrupt) as interrupted:
        async with BabbleSimSession('interrupt', [(0, worker)]) as session:
            session.clock = clock
            session.loop = loop
            raise ValueError('scenario failed')

    assert interrupted.value is interrupt
    clock.close.assert_called_once()


@pytest.mark.asyncio
async def test_host_clock_changes_only_after_its_own_ack(endpoints):
    """The clock advances only after the PHY acknowledges, and stays monotonic.

    Purpose: Verify ``BabbleSimClock.advance_to_us`` sends a WAIT for the target
    time, holds ``now_us`` until the PHY sends WAIT_END, then reports the target,
    and rejects any non-monotonic target afterwards.

    Steps:
        1. Create a clock on the device endpoint and advance it to 1234 us in a
           thread.
        2. From the PHY side, receive the WAIT request, assert the clock has not
           advanced, then send WAIT_END.
        3. After the ack, request targets 1233, NEVER and -1.

    Expected:
        - The advance returns 1234 and ``now_us`` becomes 1234 only after the
          ack.
        - Each decreasing or invalid target raises ``ValueError`` mentioning
          'nondecreasing'.
    """
    device, phy = endpoints
    clock = BabbleSimClock(device)

    async def respond():
        assert await phy.receive(12) == HEADER.pack(WAIT) + TIME.pack(1234)
        assert clock.now_us == 0
        await phy.send(HEADER.pack(WAIT_END))

    result, _ = await asyncio.gather(
        asyncio.to_thread(clock.advance_to_us, 1234), respond()
    )
    assert result == clock.now_us == 1234
    for invalid in (1233, NEVER, -1):
        with pytest.raises(ValueError, match='nondecreasing'):
            clock.advance_to_us(invalid)


def test_session_rejects_duplicate_controller_or_clock_identity():
    """A session rejects duplicate controller ids or a clock clashing with one.

    Purpose: Verify ``BabbleSimSession`` requires distinct device identities:
    two controllers may not share a device id, and the clock device id may not
    equal a controller's.

    Steps:
        1. Construct a session with two controllers both at device id 0.
        2. Construct a session with one controller at id 0 and the clock also at
           id 0.

    Expected:
        - Both constructions raise ``ValueError`` mentioning 'distinct'.
    """
    with pytest.raises(ValueError, match='distinct'):
        BabbleSimSession('test', [(0, None), (0, None)], clock_dev_id=2)
    with pytest.raises(ValueError, match='distinct'):
        BabbleSimSession('test', [(0, None)], clock_dev_id=0)
