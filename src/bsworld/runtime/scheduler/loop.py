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

"""BSWorld asyncio extension points backed by an optional discrete clock."""

from __future__ import annotations

import asyncio
import selectors
import time
from typing import Any

from .clock import DiscreteClock

DEFAULT_MAX_STEP_US = 10_000


class BsimSelector(selectors.BaseSelector):
    """Delegate normal I/O polling and replace only simulated blocking waits."""

    def __init__(
        self,
        selector: selectors.BaseSelector,
        *,
        max_step_us: int = DEFAULT_MAX_STEP_US,
    ) -> None:
        if max_step_us <= 0:
            raise ValueError("max_step_us must be positive")
        self._delegate = selector
        self._clock: DiscreteClock | None = None
        self.max_step_us = int(max_step_us)

    @property
    def clock(self) -> DiscreteClock | None:
        return self._clock

    def bind_clock(self, clock: DiscreteClock) -> None:
        if self._clock is not None and self._clock is not clock:
            raise RuntimeError("a discrete clock is already bound")
        self._clock = clock

    def unbind_clock(self, clock: DiscreteClock | None = None) -> None:
        if clock is not None and self._clock is not clock:
            raise RuntimeError("cannot unbind a different discrete clock")
        self._clock = None

    def register(
        self,
        fileobj: Any,
        events: int,
        data: Any = None,
    ) -> selectors.SelectorKey:
        return self._delegate.register(fileobj, events, data)

    def unregister(self, fileobj: Any) -> selectors.SelectorKey:
        return self._delegate.unregister(fileobj)

    def modify(
        self,
        fileobj: Any,
        events: int,
        data: Any = None,
    ) -> selectors.SelectorKey:
        return self._delegate.modify(fileobj, events, data)

    def select(self, timeout: float | None = None):  # noqa: ANN201
        clock = self._clock
        if clock is None:
            return self._delegate.select(timeout)

        ready = self._delegate.select(0)
        if ready or timeout == 0:
            return ready

        timeout_us = self.max_step_us if timeout is None else round(max(0.0, timeout) * 1_000_000)
        step_us = min(self.max_step_us, timeout_us)
        if step_us <= 0:
            return ready

        before_us = clock.now_us
        after_us = clock.advance_to_us(before_us + step_us)
        if after_us <= before_us:
            raise RuntimeError("discrete clock did not advance")

        return self._delegate.select(0)

    def close(self) -> None:
        self._delegate.close()

    def get_key(self, fileobj: Any) -> selectors.SelectorKey:
        return self._delegate.get_key(fileobj)

    def get_map(self):  # noqa: ANN201
        return self._delegate.get_map()


class BsimEventLoop(asyncio.SelectorEventLoop):
    """SelectorEventLoop that can switch between wall and BabbleSim time."""

    def __init__(self, *, max_step_us: int = DEFAULT_MAX_STEP_US) -> None:
        self.bsim_selector = BsimSelector(
            selectors.DefaultSelector(),
            max_step_us=max_step_us,
        )
        super().__init__(selector=self.bsim_selector)
        self._wall_clock_resolution = self._clock_resolution

    @property
    def discrete_clock(self) -> DiscreteClock | None:
        return self.bsim_selector.clock

    def time(self) -> float:
        clock = self.discrete_clock
        return time.monotonic() if clock is None else clock.now_us / 1_000_000

    def bind_clock(self, clock: DiscreteClock) -> None:
        # Wall-time deadlines must not cross into the simulation time domain.
        if any(not timer.cancelled() for timer in self._scheduled):
            raise RuntimeError("cannot bind a discrete clock with active timers")
        self.bsim_selector.bind_clock(clock)
        # Allow float roundoff at a due timer without admitting the next tick.
        self._clock_resolution = 0.5 / 1_000_000

    def unbind_clock(self, clock: DiscreteClock | None = None) -> None:
        self.bsim_selector.unbind_clock(clock)
        self._clock_resolution = self._wall_clock_resolution
