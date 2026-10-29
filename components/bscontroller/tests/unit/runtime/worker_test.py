# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Controller-thread ownership, deferred HCI delivery and worker lifecycle."""

import asyncio
import json
import socket
import struct
import time
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bscontroller.config import ControllerConfig
from bscontroller.hci.controller import Controller
from bscontroller.radio.babblesim import (
    DISCONNECT,
    HEADER,
    TIME,
    WAIT,
    WAIT_END,
    BabbleSimRadio,
)
from bscontroller.runtime.worker import RadioControllerWorker


@pytest.mark.asyncio
@pytest.mark.parametrize('max_step_us', [1000, 250])
@pytest.mark.parametrize('capture', [False, True], ids=['without-snoop', 'with-snoop'])
async def test_hci_runs_in_owner_thread_only_after_phy_ack(
    monkeypatch, max_step_us, tmp_path, capture
):
    """HCI is handled on the worker thread only once the PHY grants the step.

    Purpose: Verify an in-process ``RadioControllerWorker`` defers an enqueued
    HCI command until the PHY acknowledges the wait, handles it on the worker
    thread at the granted time, delivers the resulting event to the Host on the
    host thread, and optionally captures both to a BTSnoop file, for each step
    size and capture setting.

    Steps:
        1. Start a worker over a radio stub whose ``advance_to_us`` waits for a
           socket byte before granting; patch ``Controller.on_packet`` to record
           the thread and time.
        2. Enqueue a Reset command and assert nothing is handled or delivered.
        3. Send the byte to release the grant and await delivery.

    Expected:
        - Handling runs on the worker thread at ``max_step_us``; delivery runs
          on the host thread with the Command Complete bytes.
        - With capture on, the snoop file holds both records; with it off no
          file exists; setting a sink to None after attach raises; after close
          the thread is gone, ``terminated`` is done and the radio is closed.
    """
    host_thread = get_ident()
    entered = Event()
    read, write = socket.socketpair()
    read.setblocking(False)
    handled = []
    delivered = []
    radio = SimpleNamespace(
        now_us=0, synchronize=AsyncMock(), bind_loop=Mock(), close=Mock()
    )

    async def connect(sim, dev):
        assert get_ident() != host_thread
        assert (sim, dev) == ('test', 0)

    async def advance(target):
        await asyncio.get_running_loop().before_phy_request()
        entered.set()
        asyncio.get_running_loop().wait_for_phy_io(
            read.fileno(), write=False, deadline=time.monotonic() + 5
        )
        assert read.recv(1) == b'a'
        radio.now_us = target
        return target

    radio.connect, radio.advance_to_us = connect, advance
    monkeypatch.setattr('bscontroller.runtime.worker.BabbleSimRadio', lambda: radio)
    original = Controller.on_packet

    def on_packet(controller, packet):
        handled.append((get_ident(), radio.now_us))
        original(controller, packet)

    monkeypatch.setattr(Controller, 'on_packet', on_packet)
    received = asyncio.Event()

    def receive(packet):
        delivered.append((get_ident(), packet))
        received.set()

    path = tmp_path / 'hci.snoop'
    worker = RadioControllerWorker(
        'test', max_step_us=max_step_us, hci_snoop_file=path if capture else None
    )
    worker.set_packet_sink(SimpleNamespace(on_packet=receive))
    try:
        await worker.start('test', 0)
        assert worker._loop.radio_selector.max_step_us == max_step_us
        assert not worker.terminated.done()
        assert await asyncio.to_thread(entered.wait, 5)
        worker.on_packet(bytes.fromhex('01030c00'))
        assert not handled and not delivered
        write.sendall(b'a')
        await asyncio.wait_for(received.wait(), 5)
        assert handled == [(worker._thread.ident, max_step_us)]
        assert delivered == [(host_thread, bytes.fromhex('040e0401030c00'))]
        expected = bytes.fromhex('6274736e6f6f700000000001000003ea')
        for direction, packet in enumerate(
            (bytes.fromhex('01030c00'), bytes.fromhex('040e0401030c00'))
        ):
            # Flag bit 0 is the direction; bit 1 marks commands and events.
            expected += struct.pack(
                '>IIIIQ',
                len(packet),
                len(packet),
                direction | 0x02,
                0,
                0x00DCDDB30F2F8000 + max_step_us,
            )
            expected += packet
        if capture:
            assert path.read_bytes() == expected
        else:
            assert not path.exists()
        with pytest.raises(RuntimeError, match='attach the Host'):
            worker.set_packet_sink(None)
    finally:
        await worker.close()
        read.close()
        write.close()
    assert not worker._thread.is_alive()
    assert worker.terminated.done()
    radio.close.assert_called_once()
    if capture:
        assert path.read_bytes() == expected


@pytest.mark.asyncio
async def test_deferred_hci_notifications_precede_next_phy_request(monkeypatch):
    """HCI command and its deferred notification run before the next PHY wait.

    Purpose: Verify that a command enqueued before startup and the notification
    it schedules both execute, in order, before the worker's first PHY wait
    begins.

    Steps:
        1. Patch ``Controller.on_packet`` to append 'command' and schedule a
           'notification' callback.
        2. Make ``advance_to_us`` assert, after ``before_phy_request``, that
           both ran in order, then block.
        3. Enqueue a Reset command and start the worker.

    Expected:
        - The advance's assertion holds: events are ['command', 'notification']
          when the first wait is reached.
    """
    entered = Event()
    events = []

    def on_packet(controller, packet):
        events.append('command')
        asyncio.get_running_loop().call_soon(events.append, 'notification')

    async def advance(target):
        await asyncio.get_running_loop().before_phy_request()
        assert events == ['command', 'notification']
        entered.set()
        await asyncio.Future()

    radio = SimpleNamespace(
        now_us=0,
        synchronize=AsyncMock(),
        connect=AsyncMock(),
        bind_loop=Mock(),
        advance_to_us=advance,
        close=Mock(),
    )
    monkeypatch.setattr('bscontroller.runtime.worker.BabbleSimRadio', lambda: radio)
    monkeypatch.setattr(Controller, 'on_packet', on_packet)
    worker = RadioControllerWorker('test')
    worker.on_packet(bytes.fromhex('01030c00'))
    try:
        await worker.start('test', 0)
        assert await asyncio.to_thread(entered.wait, 5)
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_worker_start_failure_is_reported_and_thread_joined(
    monkeypatch, tmp_path
):
    """A radio connect failure at start is reported and the thread is joined.

    Purpose: Verify that when the radio's ``connect`` raises during start, the
    error surfaces from both ``start`` and ``close``, the worker thread is
    joined, ``terminated`` completes, the radio is closed, and the opened snoop
    capture is closed too.

    Steps:
        1. Patch the radio so ``connect`` raises RuntimeError('broken radio')
           and wrap BtsnoopWriter to record instances.
        2. Start the worker with a snoop file, expecting the error.
        3. Close the worker, expecting the same error.

    Expected:
        - Both ``start`` and ``close`` raise 'broken radio'; the thread is not
          alive; ``terminated`` is done; the radio is closed once; the capture
          file is closed.
    """
    radio = SimpleNamespace(
        connect=AsyncMock(side_effect=RuntimeError('broken radio')), close=Mock()
    )
    monkeypatch.setattr('bscontroller.runtime.worker.BabbleSimRadio', lambda: radio)
    captures = []
    from bscontroller.hci.snoop import BtsnoopWriter

    def writer(*args):
        capture = BtsnoopWriter(*args)
        captures.append(capture)
        return capture

    monkeypatch.setattr('bscontroller.runtime.worker.BtsnoopWriter', writer)
    worker = RadioControllerWorker('test', hci_snoop_file=tmp_path / 'hci.snoop')
    with pytest.raises(RuntimeError, match='broken radio'):
        await worker.start('test', 0)
    with pytest.raises(RuntimeError, match='broken radio'):
        await worker.close()
    assert not worker._thread.is_alive()
    assert worker.terminated.done()
    radio.close.assert_called_once()
    assert captures[0]._file.closed


@pytest.mark.asyncio
async def test_snoop_open_failure_releases_radio_and_worker(monkeypatch, tmp_path):
    """A snoop open failure releases the radio and reports from start and close.

    Purpose: Verify that when the snoop path cannot be opened (a directory), the
    worker surfaces the error from ``start`` and ``close``, joins its thread and
    closes the radio.

    Steps:
        1. Point the snoop file at ``tmp_path`` (a directory).
        2. Start the worker, expecting ``IsADirectoryError``.
        3. Close the worker, expecting the same error.

    Expected:
        - Both ``start`` and ``close`` raise ``IsADirectoryError``; the thread
          is not alive and the radio is closed once.
    """
    radio = SimpleNamespace(close=Mock())
    monkeypatch.setattr('bscontroller.runtime.worker.BabbleSimRadio', lambda: radio)
    worker = RadioControllerWorker('test', hci_snoop_file=tmp_path)
    with pytest.raises(IsADirectoryError):
        await worker.start('test', 0)
    with pytest.raises(IsADirectoryError):
        await worker.close()
    assert not worker._thread.is_alive()
    radio.close.assert_called_once()


@pytest.mark.asyncio
async def test_worker_runtime_failure_is_reported_when_closed(monkeypatch):
    """A radio failure after startup terminates the worker and reports on close.

    Purpose: Verify that an exception raised by ``advance_to_us`` after a
    successful start terminates the worker and is reported as an ExceptionGroup
    when the worker is closed, with the thread joined and radio closed.

    Steps:
        1. Make ``advance_to_us`` raise RuntimeError('radio failed after
           startup').
        2. Start the worker and wait for ``terminated``.
        3. Close the worker inside a ``finally``.

    Expected:
        - ``close`` raises an ExceptionGroup 'unhandled errors' whose first
          exception is the radio error; the thread is not alive; the radio is
          closed once.
    """

    async def advance(target):
        raise RuntimeError('radio failed after startup')

    radio = SimpleNamespace(
        now_us=0,
        synchronize=AsyncMock(),
        connect=AsyncMock(),
        bind_loop=Mock(),
        advance_to_us=advance,
        close=Mock(),
    )
    monkeypatch.setattr('bscontroller.runtime.worker.BabbleSimRadio', lambda: radio)
    worker = RadioControllerWorker('test')
    try:
        await worker.start('test', 0)
        await asyncio.wait_for(worker.terminated, 5)
    finally:
        with pytest.raises(ExceptionGroup, match='unhandled errors') as error:
            await worker.close()
    assert str(error.value.exceptions[0]) == 'radio failed after startup'
    assert not worker._thread.is_alive()
    radio.close.assert_called_once()


@pytest.mark.asyncio
async def test_cancelled_start_releases_connecting_worker(monkeypatch):
    """Cancelling start while connecting releases the worker cleanly.

    Purpose: Verify that cancelling the ``start`` task while the radio is still
    in ``connect`` propagates CancelledError, and a subsequent ``close`` joins
    the thread, completes ``terminated`` and closes the radio.

    Steps:
        1. Make ``connect`` signal it entered, then block forever.
        2. Create the ``start`` task, wait until connecting, then cancel it.
        3. Await the cancellation and then ``close`` the worker.

    Expected:
        - The start raises CancelledError; after close the thread is gone,
          ``terminated`` is done and the radio is closed once.
    """
    entered = Event()

    async def connect(sim, dev):
        entered.set()
        await asyncio.Future()

    radio = SimpleNamespace(connect=connect, close=Mock())
    monkeypatch.setattr('bscontroller.runtime.worker.BabbleSimRadio', lambda: radio)
    worker = RadioControllerWorker('test')
    start = asyncio.create_task(worker.start('test', 0))
    assert await asyncio.to_thread(entered.wait, 5)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    await worker.close()
    assert not worker._thread.is_alive()
    assert worker.terminated.done()
    radio.close.assert_called_once()


def test_worker_step_must_be_positive():
    """The worker rejects a non-positive maximum step.

    Purpose: Verify ``RadioControllerWorker`` refuses a ``max_step_us`` of zero,
    since the simulation step must be positive.

    Steps:
        1. Construct a worker with ``max_step_us=0``.

    Expected:
        - ``ValueError`` mentioning 'positive' is raised.
    """
    with pytest.raises(ValueError, match='positive'):
        RadioControllerWorker('test', max_step_us=0)


@pytest.mark.asyncio
async def test_socket_hci_is_owned_by_worker_and_waits_for_phy_ack(
    monkeypatch, tmp_path
):
    """Socket-mode HCI is owned by the worker and gated on the PHY ack.

    Purpose: Verify that in Unix-socket HCI mode the worker handles a Host
    command on its own thread only after the PHY grants the step, writes the
    reply from the worker thread without blocking H4 input, and captures both
    directions to the snoop file.

    Steps:
        1. Start a socket-mode worker over a radio whose ``advance_to_us`` waits
           for a byte before granting; patch ``on_packet`` and ``H4Server._write``
           to record threads.
        2. Connect a client, send a fragmented Reset command, then release the
           grant.
        3. Read the reply and inspect the recorded threads and snoop size.

    Expected:
        - Nothing is handled before the grant; after it the command is handled
          at time 1000 on the worker thread, the reply is written from the
          worker thread, the reply bytes reach the client, and the snoop file
          holds the expected total length; after close the thread is gone and
          the socket is removed.
    """
    host_thread = get_ident()
    entered = Event()
    read, write = socket.socketpair()
    read.setblocking(False)
    handled, written = [], []
    radio = SimpleNamespace(
        now_us=0,
        synchronize=AsyncMock(),
        connect=AsyncMock(),
        bind_loop=Mock(),
        close=Mock(),
    )

    async def advance(target):
        loop = asyncio.get_running_loop()
        await loop.before_phy_request()
        entered.set()
        loop.wait_for_phy_io(read.fileno(), write=False, deadline=time.monotonic() + 5)
        assert read.recv(1) == b'a'
        radio.now_us = target
        return target

    radio.advance_to_us = advance
    monkeypatch.setattr('bscontroller.runtime.worker.BabbleSimRadio', lambda: radio)
    original = Controller.on_packet

    def on_packet(controller, packet):
        handled.append((get_ident(), radio.now_us))
        original(controller, packet)

    monkeypatch.setattr(Controller, 'on_packet', on_packet)
    from bscontroller.hci.transport import H4Server

    original_write = H4Server._write

    async def write_hci(endpoint):
        written.append(get_ident())
        await original_write(endpoint)

    monkeypatch.setattr(H4Server, '_write', write_hci)
    worker = RadioControllerWorker(
        'test', hci_socket=tmp_path / 'hci.sock', hci_snoop_file=tmp_path / 'hci.snoop'
    )
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        await worker.start('test', 0)
        assert await asyncio.to_thread(entered.wait, 5)
        client.connect(str(worker.hci_socket))
        client.sendall(bytes.fromhex('0103'))
        client.sendall(bytes.fromhex('0c00'))
        assert handled == []
        write.sendall(b'a')
        # Blocking the framework thread must not block H4 input or its reply.
        assert client.recv(7) == bytes.fromhex('040e0401030c00')
        assert handled == [(worker._thread.ident, 1000)]
        assert written == [worker._thread.ident]
        assert worker._thread.ident != host_thread
        assert worker._input is None
        assert len((tmp_path / 'hci.snoop').read_bytes()) == 16 + 24 + 4 + 24 + 7
    finally:
        client.close()
        await worker.close()
        read.close()
        write.close()
    assert not worker._thread.is_alive()
    assert not worker.hci_socket.exists()


@pytest.mark.asyncio
async def test_socket_listens_before_phy_connect_but_defers_hci_until_bound(
    monkeypatch, tmp_path
):
    """The HCI socket listens before PHY connect but defers HCI until bound.

    Purpose: Verify the worker accepts a Host socket connection while the PHY is
    still connecting, but does not process a received command until the radio
    loop is bound.

    Steps:
        1. Make ``connect`` block after signalling, and ``bind_loop`` record;
           patch ``on_packet`` to assert the loop is bound when it runs.
        2. Start the worker, connect a client and send a command while connect
           is still pending, asserting no bind yet.
        3. Release connect and read the reply.

    Expected:
        - The command is handled only after ``bind_loop`` ran; the client
          receives the Command Complete reply; the socket is removed on close.
    """
    connecting = Event()
    resume = None
    bound = []
    radio = SimpleNamespace(now_us=0, synchronize=AsyncMock(), close=Mock())

    async def connect(sim, dev):
        nonlocal resume
        resume = asyncio.Event()
        connecting.set()
        await resume.wait()

    async def advance(target):
        await asyncio.Future()

    radio.connect, radio.bind_loop, radio.advance_to_us = (
        connect,
        lambda loop: bound.append(True),
        advance,
    )
    monkeypatch.setattr('bscontroller.runtime.worker.BabbleSimRadio', lambda: radio)
    original = Controller.on_packet

    def on_packet(controller, packet):
        assert bound == [True]
        original(controller, packet)

    monkeypatch.setattr(Controller, 'on_packet', on_packet)
    worker = RadioControllerWorker('test', hci_socket=tmp_path / 'hci.sock')
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        await worker.start('test', 0)
        assert await asyncio.to_thread(connecting.wait, 5)
        assert bound == []
        client.connect(str(worker.hci_socket))
        client.sendall(bytes.fromhex('01030c00'))
        worker._loop.call_soon_threadsafe(resume.set)
        assert client.recv(7) == bytes.fromhex('040e0401030c00')
        client.close()
        await asyncio.wait_for(worker.terminated, 5)
    finally:
        client.close()
        await worker.close()
    assert not worker.hci_socket.exists()


@pytest.mark.asyncio
async def test_socket_start_failure_preserves_existing_path(monkeypatch, tmp_path):
    """A socket already in use is preserved and the start fails cleanly.

    Purpose: Verify that starting socket-mode over a path that already holds a
    file raises ``FileExistsError`` from ``start`` and ``close``, leaves the
    existing file untouched, joins the thread and closes the radio.

    Steps:
        1. Write a file at the intended socket path.
        2. Start the worker, expecting ``FileExistsError``.
        3. Close the worker, expecting the same error.

    Expected:
        - Both raise ``FileExistsError``; the file content is unchanged; the
          thread is not alive and the radio is closed once.
    """
    path = tmp_path / 'hci.sock'
    path.write_text('owned by another endpoint')
    radio = SimpleNamespace(close=Mock())
    monkeypatch.setattr('bscontroller.runtime.worker.BabbleSimRadio', lambda: radio)
    worker = RadioControllerWorker('test', hci_socket=path)
    with pytest.raises(FileExistsError):
        await worker.start('test', 0)
    with pytest.raises(FileExistsError):
        await worker.close()
    assert path.read_text() == 'owned by another endpoint'
    assert not worker._thread.is_alive()
    radio.close.assert_called_once()


@pytest.mark.asyncio
async def test_socket_close_cancels_pending_phy_connection(monkeypatch, tmp_path):
    """Closing during a pending PHY connect cancels it and cleans up.

    Purpose: Verify that closing a socket-mode worker while the radio is still
    in ``connect`` cancels the connection, joins the thread, removes the socket
    and closes the radio.

    Steps:
        1. Make ``connect`` signal entry then block forever.
        2. Start the worker and wait until connecting.
        3. Close the worker.

    Expected:
        - After close the thread is not alive, the socket path is gone and the
          radio is closed once.
    """
    entered = Event()

    async def connect(sim, dev):
        entered.set()
        await asyncio.Future()

    radio = SimpleNamespace(
        now_us=0, synchronize=AsyncMock(), connect=connect, close=Mock()
    )
    monkeypatch.setattr('bscontroller.runtime.worker.BabbleSimRadio', lambda: radio)
    worker = RadioControllerWorker('test', hci_socket=tmp_path / 'hci.sock')
    await worker.start('test', 0)
    assert await asyncio.to_thread(entered.wait, 5)
    await worker.close()
    assert not worker._thread.is_alive()
    assert not worker.hci_socket.exists()
    radio.close.assert_called_once()


@pytest.mark.asyncio
async def test_socket_mode_rejects_cross_thread_packet_api(tmp_path):
    """In socket mode the in-process packet API is rejected.

    Purpose: Verify that a worker configured with an HCI socket refuses the
    cross-thread ``on_packet``/``set_packet_sink`` API, which is only valid for
    in-process mode.

    Steps:
        1. Create a socket-mode worker.
        2. Call ``on_packet`` and ``set_packet_sink``.

    Expected:
        - Both raise RuntimeError mentioning 'H4 endpoint'.
    """
    worker = RadioControllerWorker('test', hci_socket=tmp_path / 'hci.sock')
    with pytest.raises(RuntimeError, match='H4 endpoint'):
        worker.on_packet(bytes.fromhex('01030c00'))
    with pytest.raises(RuntimeError, match='H4 endpoint'):
        worker.set_packet_sink(None)
    await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('socket_mode', [False, True], ids=['in-process', 'socket'])
@pytest.mark.parametrize('outcome', ['ack', 'cancel', 'disconnect'])
async def test_startup_barrier_gates_hci_and_releases_resources(
    monkeypatch, endpoints, tmp_path, socket_mode, outcome
):
    """The startup barrier gates HCI and releases resources on every outcome.

    Purpose: Verify that during the first PHY wait the worker accepts an HCI
    command but holds it until the PHY acknowledges, and that on ack, cancel or
    disconnect the worker delivers or withholds the event and always releases
    its resources, for in-process and socket modes.

    Steps:
        1. Start the worker over a real FIFO-backed radio; receive the initial
           WAIT request on the PHY side.
        2. Send a Reset command (via socket or in-process) and assert nothing is
           handled while the clock is unbound.
        3. Apply the parametrized ``outcome``: WAIT_END (ack), no send (cancel),
           or DISCONNECT.

    Expected:
        - For 'ack' the command is handled at time 0 and, in socket mode, the
          reply reaches the client.
        - For 'disconnect' ``close`` raises 'PHY disconnected'.
        - In every case the thread ends and the socket path is removed; for
          non-ack outcomes nothing was handled.
    """
    device, phy = endpoints
    radio = BabbleSimRadio()
    radio.connection = device
    radio.connect = AsyncMock()
    monkeypatch.setattr('bscontroller.runtime.worker.BabbleSimRadio', lambda: radio)
    handled = []
    delivered = Event()
    original = Controller.on_packet

    def on_packet(controller, packet):
        assert device.controller_loop is not None
        handled.append(radio.now_us)
        original(controller, packet)
        delivered.set()

    monkeypatch.setattr(Controller, 'on_packet', on_packet)
    path = tmp_path / 'hci.sock'
    worker = RadioControllerWorker('test', hci_socket=path if socket_mode else None)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        # Startup must let the caller start the other PHY participants, even
        # while this worker is waiting for its first grant.
        await asyncio.wait_for(worker.start('test', 0), 5)
        request = await asyncio.wait_for(phy.receive(12), 5)
        assert request == HEADER.pack(WAIT) + TIME.pack(0)
        if socket_mode:
            client.connect(str(path))
            client.sendall(bytes.fromhex('01030c00'))
        else:
            worker.on_packet(bytes.fromhex('01030c00'))
        assert handled == []
        assert worker._loop.radio_selector.clock is None
        if outcome == 'ack':
            await phy.send(HEADER.pack(WAIT_END))
            assert await asyncio.to_thread(delivered.wait, 5)
            assert handled == [0]
            if socket_mode:
                assert client.recv(7) == bytes.fromhex('040e0401030c00')
        elif outcome == 'disconnect':
            await phy.send(HEADER.pack(DISCONNECT))
            await asyncio.wait_for(worker.terminated, 5)
            with pytest.raises(RuntimeError, match='PHY disconnected'):
                await worker.close()
    finally:
        client.close()
        if outcome != 'disconnect':
            await worker.close()
    assert not worker._thread.is_alive()
    assert not path.exists()
    if outcome != 'ack':
        assert handled == []


async def test_worker_selects_its_config_entry_by_name(tmp_path):
    """A worker given a shared configuration uses the entry for its own name.

    Purpose: Verify that ``RadioController`` resolves a mapping or file with its
    own name, so one document configures several Controllers and the name is
    written only once.

    Steps:
        1. Write a document with hci0 and hci1 entries.
        2. Create workers named hci0 (file) and hci1 (mapping), and one with an
           explicit ``ControllerConfig``.

    Expected:
        - Each named worker holds its own entry; the explicit one is unchanged.
    """
    document = {
        'controllers': {'hci0': {'maxConnections': 1}, 'hci1': {'maxConnections': 5}}
    }
    path = tmp_path / 'bsc.json'
    path.write_text(json.dumps(document))
    explicit = ControllerConfig(max_connections=9)
    assert RadioControllerWorker('hci0', config=path).config.max_connections == 1
    assert RadioControllerWorker('hci1', config=document).config.max_connections == 5
    assert RadioControllerWorker('hci0', config=explicit).config is explicit
