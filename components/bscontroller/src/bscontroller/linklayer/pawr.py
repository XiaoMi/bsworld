# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Periodic Advertising with Responses (PAwR): timing, ACAD and train state.

A PAwR event holds numSubevents subevents. Each subevent starts with an
AUX_SYNC_SUBEVENT_IND, followed by response slots in which synchronized devices
send AUX_SYNC_SUBEVENT_RSP (Core Vol 6, Part B, 4.4.2.2.4 and 4.4.2.12.2).
These records hold no radio operation; periodic.py runs the RF procedures.

Subevents are numbered within an event. An *instance* numbers one subevent of
one event, ``event * numSubevents + subevent``, with an unbounded event index
that does not wrap like paEventCounter.
"""

from __future__ import annotations

import struct
from collections import deque
from dataclasses import dataclass, field

from bscontroller.address import Address
from bscontroller.linklayer import (
    LL_IFS_US,
    PDU_ACCESS_ADDRESS_SIZE,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising_pdu import ExtendedPdu
from bscontroller.radio.base import PHY_CODED_S8_FACTOR, Phy

# Bluetooth Assigned Numbers, Common Data Types: Periodic Advertising Response
# Timing Information, carried in the ACAD of AUX_ADV_IND (Core Vol 6, Part B,
# 2.3.4.8; Supplement to the CSS, Part A, 1.27).
AD_TYPE_PERIODIC_ADVERTISING_RESPONSE_TIMING_INFORMATION = 0x32
# RspAA, numSubevents, subeventInterval, responseSlotDelay, responseSlotSpacing.
PAWR_TIMING_FORMAT = struct.Struct('<IBBBB')
assert PAWR_TIMING_FORMAT.size == PDU_ACCESS_ADDRESS_SIZE + 4

LL_PAWR_SUBEVENT_INTERVAL_UNIT_US = 1250
LL_PAWR_RESPONSE_SLOT_DELAY_UNIT_US = 1250
LL_PAWR_RESPONSE_SLOT_SPACING_UNIT_US = 125
# Core Vol 6, Part B, 4.4.2.2.4: up to 128 subevents of 7.5 ms to 318.75 ms;
# the first response slot 1.25 ms to 317.5 ms after the subevent starts; slots
# 0.25 ms to 31.875 ms apart.
LL_PAWR_SUBEVENTS_MAX = 0x80
LL_PAWR_SUBEVENT_INTERVAL_MIN = 0x06
LL_PAWR_SUBEVENT_INTERVAL_MAX = 0xFF
LL_PAWR_RESPONSE_SLOT_DELAY_MIN = 0x01
LL_PAWR_RESPONSE_SLOT_DELAY_MAX = 0xFE
LL_PAWR_RESPONSE_SLOT_SPACING_MIN = 0x02
# HCI Response_Slot_Spacing is in 0.125 ms; the other two in 1.25 ms.
LL_PAWR_SPACING_PER_DELAY_UNIT = (
    LL_PAWR_RESPONSE_SLOT_DELAY_UNIT_US // LL_PAWR_RESPONSE_SLOT_SPACING_UNIT_US
)
# Implementation policy: subevents asked from the Host by one Subevent Data
# Request (Vol 4, Part E, 7.7.65.36). Fewer than numSubevents, so a Host can
# tell an overriding request from the one before.
LL_PAWR_REQUEST_SUBEVENTS_MAX = 8
# Nominal transceiver output for AUX_SYNC_SUBEVENT_RSP.
RESPONSE_TX_POWER_DBM = 0


@dataclass(frozen=True)
class PawrTiming:
    """The timing every device of a PAwR train shares (2.3.4.8, 2.4.2.40)."""

    num_subevents: int
    subevent_interval: int
    response_slot_delay: int
    response_slot_spacing: int

    @property
    def subevent_interval_us(self) -> int:
        return self.subevent_interval * LL_PAWR_SUBEVENT_INTERVAL_UNIT_US

    @property
    def response_slot_delay_us(self) -> int:
        return self.response_slot_delay * LL_PAWR_RESPONSE_SLOT_DELAY_UNIT_US

    @property
    def response_slot_spacing_us(self) -> int:
        return self.response_slot_spacing * LL_PAWR_RESPONSE_SLOT_SPACING_UNIT_US

    def subevent_offset_us(self, subevent: int) -> int:
        """Start of a subevent from the start of its event."""
        return subevent * self.subevent_interval_us

    def slot_offset_us(self, slot: int) -> int:
        """Start of a response slot from the start of its subevent."""
        return self.response_slot_delay_us + slot * self.response_slot_spacing_us

    @property
    def valid(self) -> bool:
        """Whether a synchronized device can use this timing (4.4.2.2.4)."""
        return (
            1 <= self.num_subevents <= LL_PAWR_SUBEVENTS_MAX
            and LL_PAWR_SUBEVENT_INTERVAL_MIN <= self.subevent_interval
            and LL_PAWR_RESPONSE_SLOT_DELAY_MIN
            <= self.response_slot_delay
            < self.subevent_interval
            and self.response_slot_spacing >= LL_PAWR_RESPONSE_SLOT_SPACING_MIN
        )

    def encode(self, rsp_aa: int) -> bytes:
        return PAWR_TIMING_FORMAT.pack(
            rsp_aa,
            self.num_subevents,
            self.subevent_interval,
            self.response_slot_delay,
            self.response_slot_spacing,
        )

    @classmethod
    def decode(cls, data: bytes) -> tuple[int, PawrTiming]:
        """RspAA and the timing; ValueError if the field has the wrong size."""
        if len(data) != PAWR_TIMING_FORMAT.size:
            raise ValueError('invalid PAwR timing information')
        rsp_aa, *values = PAWR_TIMING_FORMAT.unpack(data)
        return rsp_aa, cls(*values)

    def acad(self, rsp_aa: int) -> bytes:
        """The AD structure an AUX_ADV_IND carries in its ACAD."""
        value = self.encode(rsp_aa)
        return (
            bytes(
                [
                    len(value) + 1,
                    AD_TYPE_PERIODIC_ADVERTISING_RESPONSE_TIMING_INFORMATION,
                ]
            )
            + value
        )


def acad_structures(acad: bytes) -> list[tuple[int, bytes]]:
    """AD type and value of each ACAD structure; ValueError if one is malformed."""
    structures = []
    offset = 0
    while offset < len(acad):
        length = acad[offset]
        end = offset + length + 1
        if length == 0 or end > len(acad):
            raise ValueError('malformed ACAD')
        structures.append((acad[offset + 1], acad[offset + 2 : end]))
        offset = end
    return structures


def acad_timing(acad: bytes) -> tuple[int, PawrTiming] | None:
    """RspAA and timing from AUX_ADV_IND's ACAD, or None for a train without
    responses.
    """
    try:
        structures = acad_structures(acad)
    except ValueError:
        return None
    for ad_type, value in structures:
        if ad_type == AD_TYPE_PERIODIC_ADVERTISING_RESPONSE_TIMING_INFORMATION:
            try:
                return PawrTiming.decode(value)
            except ValueError:
                return None
    return None


def fits(
    pdu: ExtendedPdu, phy: Phy, limit_us: int, *, coding: int = PHY_CODED_S8_FACTOR
) -> bool:
    """Whether the encoded PDU fits before the next slot (Vol 4, Part E, 7.8.125)."""
    try:
        return phy.airtime_us(len(pdu.encode()), coding) < limit_us
    except ValueError:
        return False


def indication_limit_us(timing: PawrTiming, response_slots: int) -> int:
    """AUX_SYNC_SUBEVENT_IND must be shorter than this (4.4.2.2.4)."""
    return (
        timing.response_slot_delay_us if response_slots else timing.subevent_interval_us
    ) - LL_IFS_US


def response_limit_us(timing: PawrTiming) -> int:
    """AUX_SYNC_SUBEVENT_RSP must be shorter than this (4.4.2.2.4)."""
    return timing.response_slot_spacing_us - LL_IFS_US


def response_pdu(
    data: bytes, address: Address | None, tx_power: int | None
) -> ExtendedPdu:
    """AUX_SYNC_SUBEVENT_RSP: AdvMode 0, optional AdvA and TxPower, no ADI
    (2.3.1.10).
    """
    return ExtendedPdu(
        data=bytes(data),
        address=bytes(address) if address is not None else None,
        random_address=bool(address and address.is_random),
        tx_power=tx_power,
        kind=AdvertisingPduType.AUX_SYNC_SUBEVENT_RSP,
    )


@dataclass(frozen=True)
class SubeventData:
    """Host data for one instance and the response slots to listen to after it."""

    data: bytes
    slot_start: int
    slot_count: int


@dataclass(frozen=True)
class Subevent:
    """The instance being transmitted, and its Host data if any."""

    state: PawrAdvertiser = field(repr=False, compare=False)
    instance: int
    subevent: int
    start_us: int
    channel: int
    data: SubeventData | None


@dataclass(frozen=True)
class SlotResponse:
    """One requested response slot: received data, or None if none arrived."""

    slot: int
    data: bytes | None
    rssi: float = 0
    tx_power: int | None = None


@dataclass(eq=False)
class SubeventResponses:
    """The response slots of one subevent, reported together once they end."""

    subevent: int
    start_us: int
    channel: int
    slots: deque[int]
    transmitted: bool
    responses: list[SlotResponse] = field(default_factory=list)

    def slot_start_us(self, timing: PawrTiming) -> int:
        return self.start_us + timing.slot_offset_us(self.slots[0])


@dataclass
class PawrAdvertiser:
    """The live state of an enabled PAwR train.

    ``event`` and ``subevent`` name the next AUX_SYNC_SUBEVENT_IND; the event
    starts at the set's periodic_next_us. The Host provides data only for the
    instances in ``window``, the subevents of the latest Subevent Data Request
    (Vol 4, Part E, 7.7.65.36).
    """

    rsp_aa: int
    event: int = 0
    subevent: int = 0
    window: range = range(0)
    data: dict[int, SubeventData] = field(default_factory=dict)
    listening: deque[SubeventResponses] = field(default_factory=deque)
    # An AUX_SYNC_SUBEVENT_IND, or AUX_CONNECT_REQ, went out in this event.
    transmitted: bool = False
    # The first Subevent Data Request waits for the radio, after the enabling
    # command completes.
    requested: bool = False

    def instance(self, num_subevents: int) -> int:
        return self.event * num_subevents + self.subevent

    def next_window(self, current: int, num_subevents: int) -> range:
        """The request after the outstanding one, from ``current`` at the latest.

        A window ends with its event, so subevent numbers never wrap within one
        request and every earlier subevent is outside it.
        """
        start = max(self.window.stop, current)
        count = min(
            LL_PAWR_REQUEST_SUBEVENTS_MAX, num_subevents - start % num_subevents
        )
        return range(start, start + count)

    def window_instance(self, subevent: int, num_subevents: int) -> int | None:
        """The requested instance of ``subevent``, if the window holds one."""
        window = self.window
        if not window:
            return None
        instance = window.start - window.start % num_subevents + subevent
        return instance if instance in window else None


@dataclass(frozen=True)
class PendingResponse:
    """Host response data for one slot of one instance (Vol 4, Part E, 7.8.126)."""

    event: int
    subevent: int
    slot: int
    data: bytes


@dataclass(frozen=True, eq=False)
class ScheduledResponse:
    """An AUX_SYNC_SUBEVENT_RSP timed from its received AUX_SYNC_SUBEVENT_IND."""

    start_us: int
    channel: int
    pdu: ExtendedPdu
    airtime_us: int


@dataclass
class PawrSync:
    """PAwR state of a synchronized train.

    ``event`` is the unbounded index of the event at the sync's next_us, and
    ``cursor`` the index in ``subevents`` of the next one listened for in it.
    Received AUX_SYNC_SUBEVENT_IND starts time the responses (4.4.2.12.2).
    """

    timing: PawrTiming
    rsp_aa: int
    subevents: tuple[int, ...] = (0,)
    cursor: int = 0
    event: int = 0
    last_subevent: int = 0
    received: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)
    pending: list[PendingResponse] = field(default_factory=list)
    scheduled: list[ScheduledResponse] = field(default_factory=list)
    include_tx_power: bool = False

    @property
    def next_subevent(self) -> int:
        return self.subevents[self.cursor]

    def forget_before(self, event: int) -> None:
        """Drop what no response can still use: a response is due within one
        periodic advertising interval of its request (4.4.2.2.4).
        """
        self.received = {
            key: value for key, value in self.received.items() if key[0] >= event
        }
        self.pending = [item for item in self.pending if item.event >= event]
