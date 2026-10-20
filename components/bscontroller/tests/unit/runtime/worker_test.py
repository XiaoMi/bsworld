# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Controller-thread ownership, deferred HCI delivery and worker lifecycle."""

import asyncio
import socket
import struct
import time
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bscontroller.hci.controller import Controller
from bscontroller.radio.babblesim import DISCONNECT, HEADER, TIME, WAIT, WAIT_END, BabbleSimRadio
from bscontroller.runtime.worker import RadioControllerWorker


@pytest.mark.asyncio
@pytest.mark.parametrize('max_step_us', [1000, 250])
@pytest.mark.parametrize('capture', [False, True], ids=['without-snoop', 'with-snoop'])
async def test_hci_runs_in_owner_thread_only_after_phy_ack(monkeypatch, max_step_us, tmp_path, capture):
    host_thread = get_ident()
    entered = Event()
    read, write = socket.socketpair()
    read.setblocking(False)
    handled = []
    delivered = []
    radio = SimpleNamespace(now_us=0, synchronize=AsyncMock(), bind_loop=Mock(), close=Mock())

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
    monkeypatch.setattr(
        'bscontroller.runtime.worker.BabbleSimRadio', lambda: radio
    )
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
    worker = RadioControllerWorker('test', max_step_us=max_step_us, hci_snoop_file=path if capture else None)
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
        for direction, packet in enumerate((bytes.fromhex('01030c00'), bytes.fromhex('040e0401030c00'))):
            expected += struct.pack('>IIIIQ', len(packet), len(packet), direction, 0, 0x00DCDDB30F2F8000 + max_step_us)
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
        now_us=0, synchronize=AsyncMock(),
        connect=AsyncMock(),
        bind_loop=Mock(),
        advance_to_us=advance,
        close=Mock(),
    )
    monkeypatch.setattr(
        'bscontroller.runtime.worker.BabbleSimRadio', lambda: radio
    )
    monkeypatch.setattr(Controller, 'on_packet', on_packet)
    worker = RadioControllerWorker('test')
    worker.on_packet(bytes.fromhex('01030c00'))
    try:
        await worker.start('test', 0)
        assert await asyncio.to_thread(entered.wait, 5)
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_worker_start_failure_is_reported_and_thread_joined(monkeypatch, tmp_path):
    radio = SimpleNamespace(
        connect=AsyncMock(side_effect=RuntimeError('broken radio')), close=Mock()
    )
    monkeypatch.setattr(
        'bscontroller.runtime.worker.BabbleSimRadio', lambda: radio
    )
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
    async def advance(target):
        raise RuntimeError('radio failed after startup')

    radio = SimpleNamespace(
        now_us=0, synchronize=AsyncMock(),
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
    entered = Event()

    async def connect(sim, dev):
        entered.set()
        await asyncio.Future()

    radio = SimpleNamespace(connect=connect, close=Mock())
    monkeypatch.setattr(
        'bscontroller.runtime.worker.BabbleSimRadio', lambda: radio
    )
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
    with pytest.raises(ValueError, match='positive'):
        RadioControllerWorker('test', max_step_us=0)


@pytest.mark.asyncio
async def test_socket_hci_is_owned_by_worker_and_waits_for_phy_ack(monkeypatch, tmp_path):
    host_thread = get_ident()
    entered = Event()
    read, write = socket.socketpair()
    read.setblocking(False)
    handled, written = [], []
    radio = SimpleNamespace(now_us=0, synchronize=AsyncMock(), connect=AsyncMock(), bind_loop=Mock(), close=Mock())

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
    worker = RadioControllerWorker('test', hci_socket=tmp_path / 'hci.sock', hci_snoop_file=tmp_path / 'hci.snoop')
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
async def test_socket_listens_before_phy_connect_but_defers_hci_until_bound(monkeypatch, tmp_path):
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

    radio.connect, radio.bind_loop, radio.advance_to_us = connect, lambda loop: bound.append(True), advance
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
    entered = Event()

    async def connect(sim, dev):
        entered.set()
        await asyncio.Future()

    radio = SimpleNamespace(now_us=0, synchronize=AsyncMock(), connect=connect, close=Mock())
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
    worker = RadioControllerWorker('test', hci_socket=tmp_path / 'hci.sock')
    with pytest.raises(RuntimeError, match='H4 endpoint'):
        worker.on_packet(bytes.fromhex('01030c00'))
    with pytest.raises(RuntimeError, match='H4 endpoint'):
        worker.set_packet_sink(None)
    await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('socket_mode', [False, True], ids=['in-process', 'socket'])
@pytest.mark.parametrize('outcome', ['ack', 'cancel', 'disconnect'])
async def test_startup_barrier_gates_hci_and_releases_resources(monkeypatch, endpoints, tmp_path, socket_mode, outcome):
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
