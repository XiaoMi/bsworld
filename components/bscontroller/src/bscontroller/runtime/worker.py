# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Thread-owned Controllers and H4 sockets, with optional in-process HCI handoff."""

from __future__ import annotations

import asyncio
import sys
from concurrent.futures import Future
from contextlib import AsyncExitStack, ExitStack
from functools import partial
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Thread

from bscontroller.hci.backend import RadioBackend
from bscontroller.hci.controller import Controller
from bscontroller.hci.snoop import BtsnoopWriter
from bscontroller.hci.transport import H4Server
from bscontroller.radio.babblesim import BabbleSimRadio
from bscontroller.radio.base import PHY_TIME_UNITS_PER_SECOND
from bscontroller.runtime.event_loop import DEFAULT_MAX_STEP_US, ControllerEventLoop


class _ControllerOutput:
    def __init__(self, worker):
        self.worker = worker

    def on_packet(self, packet: bytes) -> None:
        if self.worker.sink is not None:
            self.worker._host_loop.call_soon_threadsafe(self.worker.sink.on_packet, packet)


# Wall-clock watchdog for terminating the worker thread.
RADIO_WORKER_JOIN_TIMEOUT_SECONDS = 5.0


class RadioControllerWorker:
    """An HCI source/sink whose Controller is constructed and used in one thread.

    Socket mode owns H4 reads and writes in the Controller loop. Without a socket,
    in-process Host input is consumed between radio steps through a packet queue.
    """

    def __init__(
        self,
        name: str,
        public_address: str = '00:00:00:00:00:00',
        *,
        max_step_us: int = DEFAULT_MAX_STEP_US,
        hci_snoop_file: Path | None = None,
        hci_socket: Path | None = None,
    ):
        if max_step_us <= 0:
            raise ValueError('max_step_us must be positive')
        self.name = name
        self.public_address = public_address
        self.max_step_us = max_step_us
        self.hci_snoop_file = hci_snoop_file
        self.hci_socket = Path(hci_socket) if hci_socket is not None else None
        self.sink = None
        self._input: SimpleQueue[bytes] | None = SimpleQueue() if self.hci_socket is None else None
        self._ready: Future[None] = Future()
        self._result: Future[None] = Future()
        self._stopping = Event()
        self._thread: Thread | None = None
        self._loop: ControllerEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._host_loop = asyncio.get_running_loop()
        self.terminated: asyncio.Future[None] = self._host_loop.create_future()

    def set_packet_sink(self, sink) -> None:
        if self.hci_socket is not None:
            raise RuntimeError('socket Controllers use their H4 endpoint')
        if self._thread is not None:
            raise RuntimeError('attach the Host before starting the Controller')
        self.sink = sink

    def on_packet(self, packet: bytes) -> None:
        if self._input is None:
            raise RuntimeError('socket Controllers receive HCI through their H4 endpoint')
        self._input.put(bytes(packet))

    async def start(self, sim_id: str, dev_id: int) -> None:
        """Wait for socket listening, or FIFO connection for an in-process Host."""
        if self._thread is not None:
            raise RuntimeError('Controller worker is already started')
        self._thread = Thread(target=self._run, args=(sim_id, dev_id), name=f'radio:{self.name}')
        self._thread.start()
        # Shield the cross-thread Future so cancellation cannot race set_result().
        await asyncio.shield(asyncio.wrap_future(self._ready))

    def _run(self, sim_id: str, dev_id: int) -> None:
        try:
            if sys.version_info < (3, 11):
                raise RuntimeError('radio workers require Python 3.11 or newer')
            with asyncio.Runner(loop_factory=partial(ControllerEventLoop, self.max_step_us)) as runner:
                loop = runner.get_loop()
                assert isinstance(loop, ControllerEventLoop)
                self._loop = loop
                runner.run(self._drive(sim_id, dev_id))
        except asyncio.CancelledError:
            self._ready.cancel()
            self._result.set_result(None)
        except BaseException as error:  # noqa: BLE001 - report thread failure to owner.
            if not self._ready.done():
                self._ready.set_exception(error)
            self._result.set_exception(error)
        else:
            self._result.set_result(None)
        finally:
            self._host_loop.call_soon_threadsafe(self._terminate)

    def _terminate(self) -> None:
        if not self.terminated.done():
            self.terminated.set_result(None)

    def _handle_input(self, controller: Controller) -> None:
        assert self._input is not None
        while True:
            try:
                packet = self._input.get_nowait()
            except Empty:
                return
            controller.on_packet(packet)

    async def _receive_hci(self, controller: Controller) -> None:
        while True:
            await asyncio.sleep(self.max_step_us / PHY_TIME_UNITS_PER_SECOND)
            self._handle_input(controller)

    async def _advance_radio(self, radio: BabbleSimRadio, backend: RadioBackend) -> None:
        while True:
            await backend.link.advance_to_us(radio.now_us + self.max_step_us)

    async def _drive(self, sim_id: str, dev_id: int) -> None:
        loop = self._loop
        assert loop is not None
        self._task = asyncio.current_task()
        radio = BabbleSimRadio()
        with ExitStack() as resources:
            resources.callback(radio.close)
            snoop = (
                resources.enter_context(BtsnoopWriter(self.hci_snoop_file, lambda: radio.now_us))
                if self.hci_snoop_file is not None
                else None
            )
            backend = RadioBackend(radio)
            controller = Controller(
                self.name,
                backend=backend,
                public_address=self.public_address,
                host_sink=_ControllerOutput(self) if self.hci_socket is None else None,
                snoop=snoop,
            )
            async with AsyncExitStack() as endpoints:
                if self._stopping.is_set():
                    raise asyncio.CancelledError
                endpoint = None
                radio_ready = asyncio.Event()
                if self.hci_socket is not None:
                    endpoint = await endpoints.enter_async_context(H4Server(controller, self.hci_socket, ready=radio_ready))
                    # Listen before PHY connection: external Hosts must be able
                    # to start and register while this worker is connecting.
                    self._ready.set_result(None)
                try:
                    await radio.connect(sim_id, dev_id)
                    if endpoint is None:
                        # The session must start every participant before the
                        # PHY can acknowledge this worker's startup barrier.
                        self._ready.set_result(None)
                    await radio.synchronize()
                    radio.bind_loop(loop)
                    radio_ready.set()
                    async with asyncio.TaskGroup() as tasks:
                        if endpoint is not None:
                            radio_task = tasks.create_task(self._advance_radio(radio, backend))
                            try:
                                await endpoint.finished
                            finally:
                                radio_task.cancel()
                        else:
                            input_task = tasks.create_task(self._receive_hci(controller))
                            try:
                                while True:
                                    self._handle_input(controller)
                                    await backend.link.advance_to_us(radio.now_us + self.max_step_us)
                            finally:
                                input_task.cancel()
                finally:
                    if loop.radio_selector.clock is not None:
                        loop.unbind_clock()

    async def close(self) -> None:
        if self._thread is None:
            self._terminate()
            return
        self._stopping.set()
        if self._loop is not None and self._task is not None and self._thread.is_alive():
            try:
                self._loop.interrupt()
                self._loop.call_soon_threadsafe(self._task.cancel)
            except RuntimeError:
                if not self._loop.is_closed():
                    raise
        await asyncio.to_thread(self._thread.join, RADIO_WORKER_JOIN_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            raise TimeoutError(f'Controller thread did not stop: {self.name}')
        self._result.result()
