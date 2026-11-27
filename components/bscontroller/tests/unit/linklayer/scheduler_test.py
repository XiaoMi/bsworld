# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Half-open reservations, quantized gaps and fixed-priority admission.

Covers the radio scheduler primitives that place and arbitrate radio events on
one timeline:

- ``RadioReservation.window_after`` rolls a periodic window at its exclusive
  end, so an endpoint is in the active window but not the next one.
- ``first_radio_gap`` finds the earliest admissible start on a request-relative
  microsecond grid, checking the full duration against every reservation.
- ``higher_priority_overlap`` arbitrates overlaps using half-open windows and a
  fixed ``RadioPriority`` order.

These are arithmetic invariants; how connection, advertising and scanning
roles use the scheduler on air is covered by ``linklayer/air/``.
"""

import pytest

from bscontroller.linklayer.scheduler import (
    RadioPriority,
    RadioReservation,
    first_radio_gap,
    higher_priority_overlap,
)


@pytest.mark.parametrize('quantum,expected', [(1, 20), (7, 21)])
def test_radio_gap_checks_full_duration_and_keeps_the_requested_grid(quantum, expected):
    """First gap respects the full duration and the request's quantum grid.

    Purpose: Verify ``first_radio_gap`` returns the earliest start that fits the
    whole requested duration clear of an existing reservation, aligned to the
    request-relative quantum grid, and that the reservation's next window still
    rolls correctly.

    Steps:
        1. Place one reservation occupying [5, 15) repeating every 50 us.
        2. Ask for a 10 us slot searching from 0, latest start 40, for each
           quantum (1 and 7).
        3. Query the reservation's window after 20 us.

    Expected:
        - The gap is 20 for quantum 1 and 21 for quantum 7 (next grid point
          clear of the reservation).
        - ``window_after(20)`` is the next period (55, 70).
    """
    reservation = RadioReservation(5, 15, 50)
    assert first_radio_gap([reservation], 0, 10, 40, quantum_us=quantum) == expected
    assert reservation.window_after(20) == (55, 70)


def test_overloaded_radio_has_no_admissible_window():
    """A fully occupied radio offers no admissible start.

    Purpose: Verify ``first_radio_gap`` returns ``None`` when a reservation
    occupies the radio continuously, so no start of the requested duration fits
    before the latest allowed time.

    Steps:
        1. Place a reservation [0, 10) repeating every 10 us (back-to-back).
        2. Ask for a 1 us slot searching from 0 with latest start 100.

    Expected:
        - The result is ``None``: no gap exists.
    """
    assert first_radio_gap([RadioReservation(0, 10, 10)], 0, 1, 100) is None


@pytest.mark.parametrize(
    'start,end,priority,expected',
    [
        (0, 10, RadioPriority.PERIODIC_TX, False),
        (0, 11, RadioPriority.PERIODIC_TX, True),
        (15, 20, RadioPriority.PERIODIC_TX, True),
        (15, 15, RadioPriority.PERIODIC_TX, False),
        (20, 30, RadioPriority.PERIODIC_TX, False),
        (0, 11, RadioPriority.CONNECTION, False),
        (40, 51, RadioPriority.PERIODIC_TX, True),
    ],
)
def test_overlap_arbitration_uses_full_windows_and_fixed_priority(
    start, end, priority, expected
):
    """Overlap arbitration uses half-open windows and the fixed priority order.

    Purpose: Verify ``higher_priority_overlap`` reports a blocking conflict only
    when a candidate's half-open [start, end) window actually overlaps a
    reservation held at an equal-or-higher ``RadioPriority``; touching at an
    exclusive endpoint does not overlap, and a lower-priority reservation never
    blocks.

    Steps:
        1. Hold one reservation at [10, 20) with priority PERIODIC_TX.
        2. For each parametrized candidate window and priority, call
           ``higher_priority_overlap``.

    Expected:
        - The result matches the expected flag for every case, covering
          endpoint touches (no overlap), genuine overlaps, a zero-width window
          and a candidate at higher priority (CONNECTION) that is not blocked.
    """
    reservation = RadioReservation(10, 10, 40)
    assert higher_priority_overlap([reservation], start, end, priority) is expected


@pytest.mark.parametrize(
    'time_us,window',
    [
        (0, (10, 15)),
        (10, (10, 15)),
        (14, (10, 15)),
        (15, (30, 35)),
        (55, (70, 75)),
    ],
)
def test_reservations_include_active_windows_and_roll_at_the_exclusive_end(
    time_us, window
):
    """A reservation window is inclusive at start and rolls at its exclusive end.

    Purpose: Verify ``RadioReservation.window_after`` returns the window that
    contains the given time while it is still active, and advances to the next
    period exactly at the window's exclusive end.

    Steps:
        1. Build a reservation starting at 10 us, 5 us long, every 20 us.
        2. For each parametrized query time, call ``window_after``.

    Expected:
        - Times inside [10, 15) return that window; at 15 (the exclusive end)
          and later the window rolls to the next period (30, 35) then (70, 75).
    """
    assert RadioReservation(10, 5, 20).window_after(time_us) == window


def test_one_shot_reservation_blocks_gaps_until_its_exclusive_end():
    """An active one-shot window expires at its end and has no later occurrence."""
    reservation = RadioReservation(10, 5)
    assert reservation.max_interval_us is None
    assert reservation.window_after(0) == (10, 15)
    assert reservation.window_after(14) == (10, 15)
    assert reservation.window_after(15) is None
    assert higher_priority_overlap([reservation], 14, 15, RadioPriority.ISOCHRONOUS)
    assert not higher_priority_overlap([reservation], 15, 16, RadioPriority.ISOCHRONOUS)
    assert first_radio_gap([reservation], 8, 3, 15) == 15


@pytest.mark.parametrize(
    'start,duration,latest,quantum,expected',
    [
        (3, 2, 3, 4, 3),
        (3, 3, 25, 4, 23),
        (3, 3, 22, 4, None),
        (20, 3, 25, 1, 21),
    ],
)
def test_gap_search_rechecks_other_reservations_on_the_request_grid(
    start, duration, latest, quantum, expected
):
    """Gap search re-checks every reservation after shifting onto the grid.

    Purpose: Verify ``first_radio_gap`` keeps shifting a candidate past each
    conflicting reservation and re-validates against all reservations on the
    request-relative quantum grid, returning ``None`` when no aligned start
    fits before the latest allowed time.

    Steps:
        1. Hold two reservations: [5, 9) and [12, 21), each every 30 us.
        2. For each parametrized request (start, duration, latest, quantum),
           call ``first_radio_gap``.

    Expected:
        - The earliest admissible grid-aligned start equals the expected value,
          or ``None`` when the latest allowed start is reached first.
    """
    reservations = [RadioReservation(5, 4, 30), RadioReservation(12, 9, 30)]
    assert (
        first_radio_gap(reservations, start, duration, latest, quantum_us=quantum)
        == expected
    )


def test_unreserved_request_can_start_at_its_latest_allowed_time():
    """With no reservations a request may start at its latest allowed time.

    Purpose: Verify ``first_radio_gap`` admits a request at the boundary start
    time when nothing is reserved, so the latest allowed start is inclusive.

    Steps:
        1. Call ``first_radio_gap`` with no reservations, start 17, duration 4,
           latest start 17 and a 5 us quantum.

    Expected:
        - The admissible start is 17, the latest allowed time itself.
    """
    assert first_radio_gap([], 17, 4, 17, quantum_us=5) == 17
