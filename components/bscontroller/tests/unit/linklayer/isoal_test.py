# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""ISOAL boundaries, timing, padding and recovery (Core Vol 6, Part G, 2–3)."""

from collections import deque

import pytest

from bscontroller.linklayer.isoal import (
    IsoalParameters,
    IsoalReceiver,
    IsoalTransmitter,
    IsoPdu,
    IsoSdu,
    IsoStatus,
)


def parameters(
    *, framed=False, bn=1, max_pdu=32, max_sdu=32, interval=10000, sdu_interval=10000
):
    return IsoalParameters(framed, bn, max_pdu, max_sdu, interval, sdu_interval)


def test_unframed_fragment_end_padding_and_zero_length_sdu():
    """Unframed SDUs occupy UPPS slots without mixing adjacent SDUs.

    Steps:
        1. Segment a five-octet SDU into three allocated four-octet slots.
        2. Segment a zero-length SDU and then an interval with no SDU.

    Expected:
        - The final data fragment has LLID=0; padding has LLID=1.
        - A zero-length SDU uses LLID=0; absence of an SDU uses only padding.
    """
    tx = IsoalTransmitter(parameters(bn=3, max_pdu=4, max_sdu=12))
    queue = deque([IsoSdu(7, b'abcde', 1000)])
    burst = tx.segment(queue, 10000)
    assert burst.pdus == (IsoPdu(1, b'abcd'), IsoPdu(0, b'e'), IsoPdu(1, b''))
    assert burst.started == ((0, IsoSdu(7, b'abcde', 1000)),)
    assert burst.completed == (IsoSdu(7, b'abcde', 1000),)
    queue.append(IsoSdu(8, b'', 11000))
    assert tx.segment(queue, 20000).pdus == (IsoPdu(0, b''),) * 3
    assert tx.segment(queue, 30000).pdus == (IsoPdu(1, b''),) * 3


def test_unframed_multiple_sdus_and_partial_loss():
    """USPI grouping preserves per-SDU timing and reports incomplete data.

    Steps:
        1. Recombine two SDUs, with one missing fragment in the second SDU.
        2. Recombine an entirely missing next burst.

    Expected:
        - SDUs have consecutive sequence numbers and references 10 ms apart.
        - Partial data is marked possibly invalid; absent data is marked lost.
    """
    rx = IsoalReceiver(parameters(bn=4, max_pdu=4, max_sdu=8, interval=20000))
    sdus = rx.recombine(
        (IsoPdu(1, b'abcd'), IsoPdu(0, b'ef'), None, IsoPdu(0, b'gh')), 21000
    )
    assert sdus == (
        IsoSdu(0, b'abcdef', 21000),
        IsoSdu(1, b'gh', 31000, IsoStatus.POSSIBLY_INVALID),
    )
    assert rx.recombine((None,) * 4, 41000) == (
        IsoSdu(2, b'', 41000, IsoStatus.LOST),
        IsoSdu(3, b'', 51000, IsoStatus.LOST),
    )


@pytest.mark.parametrize('tail', [IsoPdu(1, b'extra'), IsoPdu(0, b'')])
def test_unframed_receiver_checks_fragments_after_the_end(tail):
    """An SDU with more data or a second end fragment is possibly invalid.

    Steps:
        1. Receive a nonempty complete SDU followed by each invalid fragment.
        2. Receive a zero-size SDU represented by two empty end fragments.

    Expected:
        - Only the first SDU carries the possible-errors status (Vol 6, G, 4).
        - The empty SDU is valid as required by the zero-size rule in 2.1.
    """
    rx = IsoalReceiver(parameters(bn=2))
    (received,) = rx.recombine((IsoPdu(0, b'data'), tail), 10000)
    assert received == IsoSdu(0, b'data', 10000, IsoStatus.POSSIBLY_INVALID)
    assert rx.recombine((IsoPdu(0, b''),) * 2, 20000) == (IsoSdu(1, b'', 20000),)


def test_framed_aggregation_and_time_offset_wire_vector():
    """A framed PDU aggregates complete SDUs with independent Time_Offset.

    Steps:
        1. Segment two short SDUs, one empty, with known reference times.
        2. Inspect segmentation headers and decode using a synchronization point.

    Expected:
        - SC=0, CMPLT=1 and three-octet offsets precede each SDU.
        - Offsets are subtracted from the transport's synchronization reference.
    """
    config = parameters(framed=True, max_pdu=20)
    queue = deque([IsoSdu(8, b'ab', 1000), IsoSdu(9, b'', 11000)])
    burst = IsoalTransmitter(config).segment(queue, 20000)
    assert burst.pdus == (IsoPdu(2, bytes.fromhex('0205384a0061620203282300')),)
    assert len(burst.started) == len(burst.completed) == 2
    assert IsoalReceiver(config).recombine(burst.pdus, 40500) == (
        IsoSdu(0, b'ab', 21500),
        IsoSdu(1, b'', 31500),
    )


def test_framed_continuation_across_events_keeps_first_reference():
    """An SDU spanning events carries Time_Offset only on its first segment.

    Steps:
        1. Segment a 12-octet SDU with Max_PDU=10 across two events.
        2. Recombine both bursts using different synchronization points.

    Expected:
        - The continuation header has SC=1, CMPLT=1 and no Time_Offset.
        - Completion retains the first segment's synchronization reference.
    """
    config = parameters(framed=True, max_pdu=10)
    tx, rx = IsoalTransmitter(config), IsoalReceiver(config)
    queue = deque([IsoSdu(7, b'abcdefghijkl', 1000)])
    first = tx.segment(queue, 10000)
    assert first.pdus == (IsoPdu(2, bytes.fromhex('00082823006162636465')),)
    assert not first.completed
    assert rx.recombine(first.pdus, 30500) == ()
    second = tx.segment(queue, 20000)
    assert second.pdus == (IsoPdu(2, b'\x03\x07fghijkl'),)
    assert not second.started
    assert second.completed == tuple(sdu for _, sdu in first.started)
    assert rx.recombine(second.pdus, 40500) == (IsoSdu(0, b'abcdefghijkl', 21500),)


def test_framed_loss_reports_discard_then_recovers_at_new_start():
    """Lost fragments cannot leak partial bytes into the next framed SDU.

    Steps:
        1. Receive a start, a missing PDU, and an orphan continuation.
        2. Receive a valid new SDU one SDU interval later.

    Expected:
        - The interrupted SDU is reported lost exactly once.
        - The orphan is discarded and the new SDU is reconstructed cleanly.
    """
    config = parameters(framed=True)
    rx = IsoalReceiver(config)
    assert rx.recombine((IsoPdu(2, bytes.fromhex('000428230061')),), 30500) == ()
    assert rx.recombine((None,), 40500) == (IsoSdu(0, b'', 21500, IsoStatus.LOST),)
    assert rx.recombine((IsoPdu(2, b'\x03\x01b'),), 40500) == ()
    assert rx.recombine((IsoPdu(2, bytes.fromhex('0204384a0063')),), 50500) == (
        IsoSdu(1, b'c', 31500),
    )


@pytest.mark.parametrize('data', [b'\x00', b'\x02\x03', b'\x02\x02xx'])
def test_framed_malformed_headers_do_not_emit_valid_sdus(data):
    """Truncated headers and lengths are discarded.

    Steps:
        1. Feed each malformed wire value into a fresh receiver.

    Expected:
        - No valid SDU is delivered and no parsing exception escapes.
    """
    assert (
        IsoalReceiver(parameters(framed=True)).recombine((IsoPdu(2, data),), 10000)
        == ()
    )


def test_framed_receiver_preserves_sdu_with_zero_time_offset():
    """Preserve the complete SDU when a peer supplies a zero Time_Offset.

    Steps:
        1. Feed the framed header observed from SDC with a zero Time_Offset.

    Expected:
        - The SDU is delivered with its reconstructed synchronization time.
          The transmitter still generates strictly positive offsets.
    """
    rx = IsoalReceiver(parameters(framed=True))
    assert rx.recombine((IsoPdu(2, b'\x02\x04\0\0\0a'),), 30500) == (
        IsoSdu(0, b'a', 30500),
    )


def test_future_sdu_waits_and_invalid_unframed_geometry_is_rejected():
    """Future timestamps cannot produce a zero/negative Time_Offset.

    Steps:
        1. Queue an SDU whose reference equals this event's anchor.
        2. Try a non-integral unframed interval and insufficient BN capacity.

    Expected:
        - The SDU remains queued behind padding until the next event.
        - Both impossible unframed parameter combinations raise ValueError.
    """
    tx = IsoalTransmitter(parameters(framed=True))
    queue = deque([IsoSdu(0, b'a', 10000)])
    assert tx.segment(queue, 10000).pdus == (IsoPdu(2, b''),)
    assert len(queue) == 1
    assert tx.segment(queue, 20000).started == ((0, IsoSdu(0, b'a', 10000)),)
    with pytest.raises(ValueError):
        parameters(interval=7500)
    with pytest.raises(ValueError):
        parameters(max_pdu=4, max_sdu=8)
