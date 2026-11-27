# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Pure radio reservations; no tasks, clocks, HCI, or simulator operations."""

from dataclasses import dataclass
from enum import IntEnum

from bscontroller.linklayer.timing import fixed_time, project_anchor


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
    """A half-open radio window that recurs only when an interval is set."""

    start_us: int
    duration_us: int
    interval_us: int | None = None
    priority: RadioPriority = RadioPriority.CONNECTION
    # Fractional parts in millionths of a microsecond.
    interval_fraction: int = 0
    start_fraction: int = 0

    @property
    def max_interval_us(self) -> int | None:
        if self.interval_us is None:
            return None
        return self.interval_us + bool(self.interval_fraction)

    def window_after(self, time_us: int) -> tuple[int, int] | None:
        """Return the active or next window, or None after a one-shot window ends."""
        if self.interval_us is None:
            end = self.start_us + self.duration_us
            return (self.start_us, end) if time_us < end else None
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
        reservation.priority < priority
        and (window := reservation.window_after(start_us)) is not None
        and window[0] < end_us
        and window[1] > start_us
        for reservation in reservations
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
            window[1]
            for reservation in reservations
            if (window := reservation.window_after(candidate)) is not None
            and window[0] < candidate + duration_us
            and window[1] > candidate
        ]
        if not conflicts:
            return candidate
        # Jump beyond every conflicting window, then round on the caller's
        # original grid. Rounding relative to each conflict would drift that grid.
        offset = max(conflicts) - start_us
        candidate = start_us + ((offset + quantum_us - 1) // quantum_us * quantum_us)
    return None
