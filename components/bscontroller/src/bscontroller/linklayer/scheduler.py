# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Pure radio reservations; no tasks, clocks, HCI, or simulator operations."""

from dataclasses import dataclass
from enum import IntEnum

from bscontroller.linklayer.timing import fixed_time, project_anchor

# A one-shot reservation's next occurrence lies beyond any simulated horizon.
ONE_SHOT_INTERVAL_US = 1 << 48


class RadioPriority(IntEnum):
    """Fixed arbitration order for equal starts and overlapping radio windows."""

    EXPIRY = -1
    CONNECTION = 0
    ISOCHRONOUS = 1
    PERIODIC_TX = 2
    PERIODIC_RX = 3
    ADVERTISING = 4


@dataclass(frozen=True)
class RadioReservation:
    start_us: int
    duration_us: int
    interval_us: int
    priority: RadioPriority = RadioPriority.CONNECTION
    # Fractional parts in millionths of a microsecond.
    interval_fraction: int = 0
    start_fraction: int = 0

    @property
    def max_interval_us(self) -> int:
        return self.interval_us + bool(self.interval_fraction)

    def window_after(self, time_us: int) -> tuple[int, int]:
        """Include a window already in progress, or the next periodic occurrence."""
        if self.interval_fraction:
            anchor = fixed_time(self.start_us, self.start_fraction)
            interval = fixed_time(self.interval_us, self.interval_fraction)
            # A start that rounds down to time_us is already in progress.
            cycles = max(0, (fixed_time(time_us + 1) - 1 - anchor) // interval)
            start, _ = project_anchor(anchor, interval, cycles)
            if start + self.duration_us <= time_us:
                start, _ = project_anchor(anchor, interval, cycles + 1)
        else:
            cycles = max(0, (time_us - self.start_us) // self.interval_us)
            start = self.start_us + cycles * self.interval_us
            if start + self.duration_us <= time_us:
                start += self.interval_us
        return start, start + self.duration_us


def higher_priority_overlap(
    reservations: list[RadioReservation],
    start_us: int,
    end_us: int,
    priority: RadioPriority,
) -> bool:
    """Check half-open windows before admitting a nonpreemptive operation."""
    return start_us < end_us and any(
        reservation.priority < priority and start < end_us and end > start_us
        for reservation in reservations
        for start, end in [reservation.window_after(start_us)]
    )


def first_radio_gap(
    reservations: list[RadioReservation],
    start_us: int,
    duration_us: int,
    latest_us: int,
    *,
    quantum_us: int = 1,
) -> int | None:
    """Find a complete nonpreemptive slot on a grid relative to start_us."""
    candidate = start_us
    while candidate <= latest_us:
        conflicts = [
            end
            for reservation in reservations
            for start, end in [reservation.window_after(candidate)]
            if start < candidate + duration_us and end > candidate
        ]
        if not conflicts:
            return candidate
        # Jump beyond every conflicting window, then round on the caller's
        # original grid. Rounding relative to each conflict would drift that grid.
        offset = max(conflicts) - start_us
        candidate = start_us + ((offset + quantum_us - 1) // quantum_us * quantum_us)
    return None
