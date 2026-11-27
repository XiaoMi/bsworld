# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Shared-radio arbitration over role-published reservations.

Role owners compute protocol-specific windows and handle missed events. The
arbiter only orders operations, grants deadlines, and checks overlapping windows.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass

from bscontroller.linklayer.scheduler import (
    RadioPriority,
    RadioReservation,
    higher_priority_overlap,
)


@dataclass(frozen=True)
class PendingOperation:
    """One due radio activity; run() performs it on the owning link."""

    due_us: int
    priority: RadioPriority
    owner: object
    run: Callable[[], Awaitable[object]]
    # Equal-time connections rotate by their last served event.
    order: int = 0
    protected: Callable[[], RadioReservation] | None = None


class RadioResourceManager:
    def __init__(
        self,
        now_us: Callable[[], int],
        reservations: Callable[[object], list[RadioReservation]],
    ):
        self._now_us = now_us
        self._reservations = reservations
        self.deadline_us: int | None = None

    @staticmethod
    def select(operations: list[PendingOperation]) -> PendingOperation:
        """Earliest due event, then radio priority and per-role fairness."""
        return min(
            operations, key=lambda entry: (entry.due_us, entry.priority, entry.order)
        )

    @contextmanager
    def claim(self, owner: object = None, *, protected: RadioReservation | None = None):
        """Bound an event's work by the next competing reservation; release on all
        exits.
        """
        previous = self.deadline_us
        self.deadline_us = self.radio_limit(owner, protected=protected)
        try:
            yield self.deadline_us
        finally:
            self.deadline_us = previous

    async def execute(self, operation: PendingOperation) -> None:
        protected = operation.protected() if operation.protected is not None else None
        with self.claim(operation.owner, protected=protected):
            await operation.run()

    def reservations(self, *, exclude: object = None) -> list[RadioReservation]:
        """Role owners publish windows; the arbiter does not traverse their state."""
        return self._reservations(exclude)

    def admit(
        self,
        owner: object,
        start_us: int,
        end_us: int,
        priority: RadioPriority,
        *,
        earliest_us: int | None = None,
    ) -> bool:
        """Admit an anchored event only when it can start and retain its priority.

        Role managers decide how to advance a missed event. The radio arbiter owns
        overlap policy rather than duplicating it in transmit/receive executors.
        """
        return (
            earliest_us is None or start_us >= earliest_us
        ) and not higher_priority_overlap(
            self.reservations(exclude=owner), start_us, end_us, priority
        )

    def radio_limit(
        self, owner: object = None, *, protected: RadioReservation | None = None
    ) -> int | None:
        now = self._now_us() + 1
        protected_window = (
            protected.window_after(now) if protected is not None else None
        )
        starts = []
        for reservation in self.reservations(exclude=owner):
            window = reservation.window_after(now)
            if window is None:
                continue
            if (
                protected is not None
                and protected_window is not None
                and reservation.priority > protected.priority
                and protected_window[0] < window[1]
                and window[0] < protected_window[1]
            ):
                # A lower-priority exchange cannot run in the reserved first
                # exchange; only its later appearances bound work.
                last = protected_window[1]
                window = reservation.window_after(max(window[1], last))
                if window is not None and window[0] < last:
                    window = reservation.window_after(window[1])
            if window is not None and window[0] > now:
                starts.append(window[0])
        return min(starts) if starts else None

    def fits_radio(self, end_us: int) -> bool:
        return self.deadline_us is None or end_us <= self.deadline_us
