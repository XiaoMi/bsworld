# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Coordinate UML guest turns on a topology's BabbleSim clock."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from bscontroller.radio.base import PHY_TIME_UNITS_PER_SECOND

if TYPE_CHECKING:
    import asyncio

    from bscontroller.runtime.event_loop import RadioClock

    from .timetravel import TimeTravelCalendar

# Wall-clock time a guest may take to connect or yield.
UML_TIMEOUT = 60.0


class TimeTravelScheduler:
    """Share one PHY turn across a topology's UML guests."""

    def __init__(self, loop: asyncio.AbstractEventLoop, clock: RadioClock) -> None:
        self.loop = loop
        self.clock = clock
        self._guests: dict[int, TimeTravelCalendar] = {}
        self._timer: asyncio.TimerHandle | None = None
        self._due_us: int | None = None
        self._dispatching = False

    def add(self, guest: TimeTravelCalendar) -> None:
        self._guests[guest.order] = guest
        self._arm()

    def remove(self, guest: TimeTravelCalendar) -> None:
        self._guests.pop(guest.order, None)
        self._arm()

    def io_ready(self) -> None:
        """Collect all ready guest I/O before choosing the next turn."""
        self._arm(ready=True)

    def _arm(self, *, ready: bool = False) -> None:
        if self._dispatching or self.loop.is_closed():
            return
        due_us = (
            self.clock.now_us
            if ready
            else min(
                (
                    due
                    for guest in self._guests.values()
                    if (due := guest.next_us) is not None
                ),
                default=None,
            )
        )
        if due_us == self._due_us:
            return
        if self._timer is not None:
            self._timer.cancel()
        self._due_us = due_us
        self._timer = (
            self.loop.call_at(due_us / PHY_TIME_UNITS_PER_SECOND, self.dispatch)
            if due_us is not None
            else None
        )

    def dispatch(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = None
        self._due_us = None
        self._dispatching = True
        deadline = time.monotonic() + UML_TIMEOUT
        try:
            now_us = self.clock.now_us
            while True:
                for order in sorted(self._guests):
                    self._guests[order].service_idle(deadline)
                guest = min(
                    (
                        guest
                        for guest in self._guests.values()
                        if (due := guest.request_us) is not None and due <= now_us
                    ),
                    key=lambda guest: (guest.request_us, guest.order),
                    default=None,
                )
                if guest is None:
                    return
                guest.resume(deadline)
        finally:
            self._dispatching = False
            self._arm()


__all__ = ["TimeTravelScheduler"]
