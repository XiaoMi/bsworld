# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""CIG airtime reservations and CIS anchor negotiation (Core Vol 6, Part B, 5.1.15)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from math import gcd
from typing import TYPE_CHECKING

from bscontroller.linklayer import LL_EVENT_COUNTER_MASK
from bscontroller.linklayer.cis_parameters import (
    MIN_CIS_OFFSET_US,
    CisWindow,
    event_anchor,
    plan_cig,
    valid_cis_supervision_timeout,
)
from bscontroller.linklayer.scheduler import RadioReservation, first_radio_gap
from bscontroller.linklayer.timing import fixed_time, split_fixed_time

if TYPE_CHECKING:
    from bscontroller.linklayer.cig import Cig, Cis
    from bscontroller.linklayer.cis import CisEventParameters
    from bscontroller.linklayer.connection import Connection


def _spaces(stream: Cis) -> tuple[int, int]:
    params = stream.parameters
    return stream.connection.controls.frame_space.cis_spaces(
        params.c_to_p.phy, params.p_to_c.phy
    )


def plan_frame_spaces(group: Cig, stream: Cis) -> None:
    if group.reference_event is not None:
        ifs, mss = _spaces(stream)
        params = stream.parameters
        params = replace(params, spacing_us=ifs, minimum_subevent_space_us=mss)
        if params.nse > 1 and params.subinterval_us < params.minimum_subevent_us:
            raise ValueError('negotiated frame spaces exceed the reserved CIS slot')
        stream.parameters = params
        return
    spaces = {
        item.cis_id: _spaces(item)
        for item in group.streams.values()
        if item.connection is not None
    }
    for cis_id, params in plan_cig(group.config, spaces=spaces).items():
        group.streams[cis_id].parameters = params


def new_cis_window(
    group: Cig, stream: Cis, active_reservations: Callable[[], list[RadioReservation]]
) -> CisWindow:
    connection, params = stream.connection, stream.parameters
    validate_supervision(connection, params.interval_us)
    interval = connection.parameters.interval_us
    minimum = max(MIN_CIS_OFFSET_US, connection.first_exchange_us(max_rx=False))
    maximum = interval - params.cis_sync_delay_us - params.minimum_subevent_space_us
    minimum = max(minimum, params.cig_sync_delay_us - params.cis_sync_delay_us)
    if minimum > maximum:
        raise ValueError('CIS event does not fit the ACL interval')
    offset = params.cig_sync_delay_us - params.cis_sync_delay_us
    reference = group.reference_event
    if reference is None:
        counter = (connection.event_counter + 6) & LL_EVENT_COUNTER_MASK
        minimum = max(minimum, min(maximum, 2000 + offset))
        # The minimum already protects the associated ACL's first exchange.
        # A group spanning multiple ACLs must leave room on both sides.
        reservations = connection.link.resource.reservations(
            exclude=None if group.spans_acls else connection
        )
        if reservations:
            anchor = event_anchor(connection, counter) - offset
            interval_fixed = params.interval_us * connection.clock_rate
            gap = available_cig_offsets(
                params, interval_fixed, reservations, anchor, minimum, maximum
            )
            # Different clocks or intervals can occasionally overlap. Prefer
            # a permanently free phase, but only other CIGs require one.
            if gap is None and (active := active_reservations()):
                gap = available_cig_offsets(
                    params, interval_fixed, active, anchor, minimum, maximum
                )
                if gap is None:
                    raise ValueError('no recurring gap between CIGs')
            if gap is not None:
                minimum, maximum = gap
        return CisWindow(minimum, maximum, counter)
    # Use the running CIS so a later stream joins the same CIG even after
    # the Central has changed its sleep clock accuracy.
    offset = reference.parameters.cis_sync_delay_us - params.cis_sync_delay_us
    for ahead in range(6, 100):
        counter = (connection.event_counter + ahead) & LL_EVENT_COUNTER_MASK
        anchor = event_anchor(connection, counter)
        reference_us, _ = reference.anchor_at_or_after(anchor + minimum - offset)
        selected = reference_us + offset - anchor
        if selected <= maximum:
            return CisWindow(selected, selected, counter)
    raise ValueError('CIS event does not align with the CIG')


def validate_supervision(connection: Connection, interval_us: int) -> None:
    pending = connection.controls.pending
    timeout = connection.parameters.timeout
    if pending is not None and pending.supervision_timeout is not None:
        # The new ACL timeout may already be in effect at the first CIS event.
        timeout = min(timeout, pending.supervision_timeout)
    if not valid_cis_supervision_timeout(interval_us, timeout):
        raise ValueError('CIS interval exceeds the ACL supervision budget')


def available_cig_offsets(
    params: CisEventParameters,
    interval_fixed: int,
    reservations: list[RadioReservation],
    anchor_us: int,
    minimum: int,
    maximum: int,
) -> tuple[int, int] | None:
    # A gap must recur for every pair of CIG events. Project the windows
    # onto their shared period and offer the largest clear window within
    # one ISO interval, so the peer can choose a phase on its radio too.
    duration = params.cig_sync_delay_us
    recurring = []
    # Relative starts can round one PHY tick either way when the new CIG
    # accumulates a fractional microsecond each interval.
    _, interval_fraction = split_fixed_time(interval_fixed)
    rounding_us = int(interval_fraction != 0)
    for reservation in reservations:
        period = gcd(
            interval_fixed,
            fixed_time(reservation.interval_us, reservation.interval_fraction),
        )
        occupied = reservation.duration_us + 2 * rounding_us
        if fixed_time(duration + occupied) > period:
            return None
        start = fixed_time(reservation.start_us - anchor_us, reservation.start_fraction)
        start_us, phase = split_fixed_time(start % period - period)
        interval_us, fraction = split_fixed_time(period)
        recurring.append(
            replace(
                reservation,
                start_us=start_us - rounding_us,
                start_fraction=phase,
                duration_us=occupied,
                interval_us=interval_us,
                interval_fraction=fraction,
            )
        )
    maximum = min(maximum, minimum + params.interval_us - 1)
    best = None
    while (start := first_radio_gap(recurring, minimum, duration, maximum)) is not None:
        next_start = min(item.window_after(start)[0] for item in recurring)
        end = min(maximum, next_start - duration)
        if best is None or end - start > best[1] - best[0]:
            best = (start, end)
        minimum = next_start
    return best


def future_window(stream: Cis, window: CisWindow) -> CisWindow:
    connection, period = stream.connection, stream.parameters.interval_us
    old_start = window.anchor_us(connection)
    if old_start >= connection.next_anchor_us:
        return window
    for ahead in range(2, 100):
        counter = (connection.event_counter + ahead) & LL_EVENT_COUNTER_MASK
        offset = window.offset_at(connection, counter, period)
        if offset < MIN_CIS_OFFSET_US:
            offset += ((MIN_CIS_OFFSET_US - offset + period - 1) // period) * period
        if offset + window.width_us < connection.parameters.interval_us:
            return CisWindow(offset, offset + window.width_us, counter)
    raise ValueError('CIS window does not fit a future ACL interval')
