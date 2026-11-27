# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Simulation session and optional caller clock ownership."""

from __future__ import annotations

import asyncio
from pathlib import Path
from traceback import format_exception
from typing import TYPE_CHECKING

from bscontroller.radio.babblesim import (
    BSIM_CONNECT_TIMEOUT_SECONDS,
    BSIM_IO_TIMEOUT_SECONDS,
    NEVER,
    PhyConnection,
    simulation_directory,
)

if TYPE_CHECKING:
    from bscontroller.runtime.event_loop import RadioEventLoop
    from bscontroller.runtime.worker import RadioControllerWorker


class BabbleSimClock:
    """The Host's blocking time endpoint, independent of Controller threads."""

    def __init__(
        self, connection: PhyConnection, step_timeout: float = BSIM_IO_TIMEOUT_SECONDS
    ):
        self.connection = connection
        self.now_us = 0
        self.step_timeout = step_timeout

    @classmethod
    def for_device(cls, directory: Path, dev_id: int) -> BabbleSimClock:
        """Create the clock participant for a caller-owned topology."""
        return cls(PhyConnection(directory, dev_id))

    async def connect(self, timeout: float = BSIM_CONNECT_TIMEOUT_SECONDS) -> None:
        await self.connection.connect(timeout)

    def bind(self, loop: RadioEventLoop) -> None:
        """Obtain the first PHY grant before starting the Host clock."""
        self.advance_to_us(0)
        loop.bind_clock(self)

    def close(self) -> None:
        self.connection.close()

    def advance_to_us(self, target_us: int) -> int:
        if not self.now_us <= target_us < NEVER:
            raise ValueError('clock target must be a finite, nondecreasing uint64 time')
        self.connection.wait_sync(target_us, self.step_timeout)
        self.now_us = target_us
        return target_us


class BabbleSimSession:
    """Own radio worker threads and optionally bind a standalone Host clock."""

    def __init__(
        self,
        sim_id: str,
        controllers: list[tuple[int, RadioControllerWorker]],
        *,
        clock_dev_id: int | None = None,
    ):
        ids = [dev_id for dev_id, _ in controllers]
        if not ids:
            raise ValueError('a session requires at least one Controller')
        all_ids = ids + ([clock_dev_id] if clock_dev_id is not None else [])
        if len(set(all_ids)) != len(all_ids):
            raise ValueError('Controller and clock device ids must be distinct')
        self.sim_id = sim_id
        self.controllers = list(controllers)
        self.clock = (
            BabbleSimClock.for_device(simulation_directory(sim_id), clock_dev_id)
            if clock_dev_id is not None
            else None
        )
        self.loop = None

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        try:
            # Register all radio participants before requesting the Host's first
            # PHY grant; startup synchronization needs every configured device present.
            for dev_id, worker in self.controllers:
                await worker.start(self.sim_id, dev_id)
            if self.clock is not None:
                await self.clock.connect()
                self.clock.bind(loop)
                self.loop = loop
            return self
        except BaseException as error:
            # Cancellation during startup has the same cleanup obligations as an
            # error: workers already started must not keep FIFO endpoints alive.
            await self.__aexit__(type(error), error, error.__traceback__)
            raise

    async def __aexit__(self, exc_type, exc, traceback):
        failures: list[BaseException] = []
        interrupted = None
        if self.loop is not None:
            try:
                self.loop.unbind_clock()
            except BaseException as error:
                failures.append(error)
            self.loop = None
        try:
            results = await asyncio.gather(
                *(worker.close() for _, worker in self.controllers),
                return_exceptions=True,
            )
            for (dev_id, _), result in zip(self.controllers, results, strict=True):
                if isinstance(result, BaseException) and result is not exc:
                    result.add_note(f'while closing Controller {dev_id}')
                    failures.append(result)
        except asyncio.CancelledError as error:
            interrupted = error
        except BaseException as error:
            failures.append(error)
        finally:
            # Keep the Host's PHY grant until every radio has stopped submitting work.
            if self.clock is not None:
                try:
                    self.clock.close()
                except BaseException as error:
                    failures.append(error)
        if exc is not None and interrupted is not None:
            failures.append(interrupted)
        original = exc if exc is not None else interrupted
        if original is not None:
            for failure in failures:
                original.add_note(
                    'Radio session cleanup failed:\n'
                    + ''.join(format_exception(failure, chain=False)).rstrip()
                )
            if exc is None:
                raise original
        elif len(failures) == 1:
            raise failures[0]
        elif failures:
            raise BaseExceptionGroup('errors while closing the radio session', failures)
