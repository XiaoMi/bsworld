# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""H4 framing and a bounded Unix-socket endpoint for an external Host."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol


class TransportSink(Protocol):
    def on_packet(self, packet: bytes) -> None: ...


class TransportSource(Protocol):
    def set_packet_sink(self, sink: TransportSink | None) -> None: ...


class H4Parser:
    def __init__(self, sink):
        self.sink = sink
        self.buffer = bytearray()

    def feed_data(self, data: bytes):
        self.buffer.extend(data)
        while self.buffer:
            packet_type = self.buffer[0]
            header_size = {1: 4, 2: 5, 3: 4, 4: 3, 5: 5}.get(packet_type)
            if header_size is None:
                raise ValueError(f'invalid H4 packet type: {packet_type}')
            if len(self.buffer) < header_size:
                return
            if packet_type in (2, 5):
                length = int.from_bytes(self.buffer[3:5], 'little')
                if packet_type == 5:
                    length &= 0x3FFF
            else:
                length = self.buffer[header_size - 1]
            total = header_size + length
            if len(self.buffer) < total:
                return
            packet = bytes(self.buffer[:total])
            del self.buffer[:total]
            self.sink.on_packet(packet)


class H4Server:
    """Serve one Host, terminating on disconnect or transport failure."""

    def __init__(self, controller, path: Path, *, queue_size: int = 128, ready: asyncio.Event | None = None):
        if queue_size < 1:
            raise ValueError('queue_size must be positive')
        self.controller = controller
        self.path = Path(path)
        self.ready = ready
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.finished = asyncio.get_running_loop().create_future()
        self.server = None
        self.writer = None
        self.task = None
        self._closing = False
        controller.set_packet_sink(self)

    async def __aenter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() or self.path.is_symlink():
            raise FileExistsError(f'HCI socket path already exists: {self.path}')
        self.server = await asyncio.start_unix_server(self._accept, path=self.path)
        return self

    def on_packet(self, packet: bytes):
        if self.writer is None or self.finished.done():
            return
        try:
            self.queue.put_nowait(bytes(packet))
        except asyncio.QueueFull:
            self.finished.set_exception(RuntimeError('HCI Host is not draining Controller output'))

    async def _write(self):
        while True:
            self.writer.write(await self.queue.get())
            await self.writer.drain()

    async def _read(self, reader):
        parser = H4Parser(self.controller)
        while data := await reader.read(4096):
            parser.feed_data(data)
        if parser.buffer:
            raise ValueError('Host disconnected during an H4 packet')

    async def _accept(self, reader, writer):
        if self._closing or self.writer is not None or self.finished.done():
            writer.close()
            await writer.wait_closed()
            return
        self.writer = writer
        self.task = asyncio.current_task()
        tasks = []
        try:
            if self.ready is not None:
                await self.ready.wait()
            tasks = [asyncio.create_task(self._read(reader)), asyncio.create_task(self._write())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self.finished.done():
                self.finished.set_exception(error)
        else:
            if not self.finished.done():
                self.finished.set_result(None)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def __aexit__(self, exc_type, exc, traceback):
        # A connection accepted by asyncio may still have its callback queued.
        # Reject it when that callback runs during server.wait_closed().
        self._closing = True
        if self.server is not None:
            self.server.close()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.server is not None:
            await self.server.wait_closed()
            self.path.unlink(missing_ok=True)
        if self.finished.done() and not self.finished.cancelled():
            self.finished.exception()
