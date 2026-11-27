# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Pure radio reservations; no tasks, clocks, HCI, or simulator operations."""

from dataclasses import dataclass
from enum import IntEnum

from bscontroller.radio.base import PHY_TIME_UNITS_PER_SECOND

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
        elapsed = time_us - self.start_us
        if self.interval_fraction:
            scale = PHY_TIME_UNITS_PER_SECOND
            interval = self.interval_us * scale + self.interval_fraction
            phase = self.start_fraction
        else:
            scale, interval, phase = 1, self.interval_us, 0
        # A start that rounds down to elapsed is already in progress.
        cycles = max(0, ((elapsed + 1) * scale - 1 - phase) // interval)
        offset = cycles * interval + phase
        if offset // scale + self.duration_us <= elapsed:
            # Half-open intervals allow a new operation exactly at the old end.
            offset += interval
        start = self.start_us + offset // scale
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
