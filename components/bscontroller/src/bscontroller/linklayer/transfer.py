# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Periodic Advertising Sync Transfer: LL_PERIODIC_SYNC_IND and its timing.

The sender describes a train relative to a connection event; the recipient
turns that description back into the train's next anchor on its own clock
(Core Vol 6, Part B, 2.4.2.27, 5.1.13 and 5.1.13.1).
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from bscontroller.address import Address, AddressType
from bscontroller.linklayer import (
    LL_EVENT_COUNTER_MASK,
    LL_PERIODIC_INTERVAL_UNIT_US,
    LL_PERIODIC_SYNC_CONN_EVENT_SPAN,
    LL_PERIODIC_SYNC_COUNTER_SPAN_US,
    LL_SCA_50_PPM,
    PDU_ADV_ADI_SID_MAX,
    PDU_ADV_OFFSET_UNIT_300_US,
    PDU_ADV_OFFSET_VALUE_MASK,
    PDU_ADV_SYNC_INFO_SIZE,
    PDU_ADV_SYNC_OFFSET_ADJUST_US,
    PDU_CONTROL_PERIODIC_SYNC_SIZE,
    PDU_CONTROL_PERIODIC_SYNC_WR_SIZE,
    ControlOpcode,
)
from bscontroller.linklayer.advertising_pdu import SyncInfo
from bscontroller.linklayer.pawr import PawrTiming
from bscontroller.linklayer.timing import (
    LL_CLOCK_JITTER_US,
    signed_counter_distance,
    window_widening,
)
from bscontroller.radio.base import Phy

if TYPE_CHECKING:
    from bscontroller.linklayer.connection import Connection
    from bscontroller.linklayer.discovery import Advertising, PeriodicSync

# ID, SyncInfo, connEventCount, lastPaEventCounter, SID/AType/SCA, PHY, AdvA and
# syncConnEventCount (Core Vol 6, Part B, 2.4.2.27, Figure 2.48).
PERIODIC_SYNC_FORMAT = struct.Struct(f'<H{PDU_ADV_SYNC_INFO_SIZE}sHHBB6sH')
assert PERIODIC_SYNC_FORMAT.size + 1 == PDU_CONTROL_PERIODIC_SYNC_SIZE
PERIODIC_SYNC_ATYPE_SHIFT = 4
PERIODIC_SYNC_SCA_SHIFT = 5
# The largest syncPacketWindowOffset an LL_PERIODIC_SYNC_IND can represent.
SYNC_TRANSFER_OFFSET_MAX_US = (
    PDU_ADV_SYNC_OFFSET_ADJUST_US
    + PDU_ADV_OFFSET_VALUE_MASK * PDU_ADV_OFFSET_UNIT_300_US
)
# This Link Layer's sleep clock accuracy, as for window_widening.
LL_LOCAL_SCA = LL_SCA_50_PPM


@dataclass(frozen=True)
class PeriodicSyncIndication:
    """The fields of an LL_PERIODIC_SYNC_IND PDU; SyncInfo stays raw octets.

    With PAwR timing it is an LL_PERIODIC_SYNC_WR_IND, which adds RspAA and the
    train's subevent timing (Core Vol 6, Part B, 2.4.2.40).
    """

    identifier: int
    sync_info: bytes
    conn_event_count: int
    last_counter: int
    sid: int
    random_address: bool
    sca: int
    phy: int
    address: bytes
    sync_conn_event_count: int
    pawr: PawrTiming | None = None
    rsp_aa: int = 0

    def encode(self) -> bytes:
        opcode = (
            ControlOpcode.LL_PERIODIC_SYNC_IND
            if self.pawr is None
            else ControlOpcode.LL_PERIODIC_SYNC_WR_IND
        )
        return (
            bytes([opcode])
            + PERIODIC_SYNC_FORMAT.pack(
                self.identifier,
                self.sync_info,
                self.conn_event_count,
                self.last_counter,
                self.sid
                | int(self.random_address) << PERIODIC_SYNC_ATYPE_SHIFT
                | self.sca << PERIODIC_SYNC_SCA_SHIFT,
                self.phy,
                self.address,
                self.sync_conn_event_count,
            )
            + (self.pawr.encode(self.rsp_aa) if self.pawr is not None else b'')
        )

    @classmethod
    def decode(cls, payload: bytes) -> PeriodicSyncIndication:
        size = {
            ControlOpcode.LL_PERIODIC_SYNC_IND: PDU_CONTROL_PERIODIC_SYNC_SIZE,
            ControlOpcode.LL_PERIODIC_SYNC_WR_IND: PDU_CONTROL_PERIODIC_SYNC_WR_SIZE,
        }.get(payload[0] if payload else None)
        if size is None or len(payload) != size:
            raise ValueError('invalid LL_PERIODIC_SYNC_IND')
        identifier, info, event, last, flags, phy, address, sync_event = (
            PERIODIC_SYNC_FORMAT.unpack(payload[1:PDU_CONTROL_PERIODIC_SYNC_SIZE])
        )
        rsp_aa, pawr = (
            PawrTiming.decode(payload[PDU_CONTROL_PERIODIC_SYNC_SIZE:])
            if size == PDU_CONTROL_PERIODIC_SYNC_WR_SIZE
            else (0, None)
        )
        return cls(
            identifier,
            info,
            event,
            last,
            flags & PDU_ADV_ADI_SID_MAX,
            bool(flags >> PERIODIC_SYNC_ATYPE_SHIFT & 1),
            flags >> PERIODIC_SYNC_SCA_SHIFT,
            phy,
            address,
            sync_event,
            pawr,
            rsp_aa,
        )

    @property
    def advertiser(self) -> Address:
        return Address(
            self.address,
            AddressType.RANDOM_DEVICE
            if self.random_address
            else AddressType.PUBLIC_DEVICE,
        )

    @property
    def transferred_phy(self) -> Phy | None:
        """The single PHY bit, or None for no, several or RFU bits (5.1.13)."""
        if self.phy.bit_count() != 1 or self.phy.bit_length() > max(Phy):
            return None
        return Phy(self.phy.bit_length())


@dataclass(frozen=True)
class TrainReference:
    """An AUX_SYNC_IND anchor on the sender's clock, and the train it belongs to.

    ``last_counter`` names an AUX_SYNC_IND the sender actually transmitted or
    received (Core Vol 6, Part B, 2.4.2.27).
    """

    anchor_us: int
    counter: int
    last_counter: int
    interval: int
    channel_map: int
    access_address: int
    crc_init: int
    sca: int
    sid: int
    address: Address
    phy: Phy
    # PAwR timing and RspAA, for an LL_PERIODIC_SYNC_WR_IND (2.4.2.40).
    pawr: PawrTiming | None = None
    rsp_aa: int = 0

    def anchor_of(self, counter: int) -> int:
        return (
            self.anchor_us
            + signed_counter_distance(counter, self.counter)
            * self.interval
            * LL_PERIODIC_INTERVAL_UNIT_US
        )


class TrainSource(Protocol):
    """The train a queued LL_PERIODIC_SYNC_IND describes at transmission."""

    @property
    def ended(self) -> bool:
        """The train is gone, so a transfer still waiting for it is dropped."""
        ...

    def reference(self) -> TrainReference | None:
        """The train from an actual AUX_SYNC_IND, or None before there is one."""
        ...


@dataclass(frozen=True)
class AdvertisingSource:
    """A local set's periodic train, as enabled when the Host asked to transfer
    it (Vol 4, Part E, 7.8.90). Enabling again starts a new train with a new
    Access Address.
    """

    item: Advertising
    access_address: int

    @property
    def ended(self) -> bool:
        # A set is removed only after its periodic advertising is disabled.
        item = self.item
        return not item.periodic_enabled or item.periodic_aa != self.access_address

    def reference(self) -> TrainReference | None:
        """The next scheduled event, once the train has transmitted one.

        lastPaEventCounter must name a transmitted AUX_SYNC_IND (2.4.2.27). The
        advertiser's own clock times both events. A PAwR train's events start
        with subevent 0 (2.4.2.40).
        """
        item = self.item
        last = item.periodic_last_counter
        if last is None:
            return None
        return TrainReference(
            item.periodic_next_us,
            item.periodic_counter,
            last,
            item.periodic_interval,
            item.periodic_channel_map,
            item.periodic_aa,
            item.periodic_crc,
            LL_LOCAL_SCA,
            item.sid,
            item.address,
            item.secondary_phy,
            item.pawr,
            item.pawr_state.rsp_aa if item.pawr_state is not None else 0,
        )


@dataclass(frozen=True)
class SynchronizationSource:
    """A synchronized train, until terminated or lost (Vol 4, Part E, 7.8.89)."""

    syncs: Mapping[int, PeriodicSync]
    sync: PeriodicSync

    @property
    def ended(self) -> bool:
        return self.syncs.get(self.sync.handle) is not self.sync

    def reference(self) -> TrainReference:
        """The last AUX_SYNC_IND received from the advertiser, or the start of
        the PAwR event of the last AUX_SYNC_SUBEVENT_IND.
        """
        sync = self.sync
        info = sync.info
        assert info is not None
        pawr = sync.pawr
        return TrainReference(
            sync.last_rx_us
            - (pawr.timing.subevent_offset_us(pawr.last_subevent) if pawr else 0),
            sync.last_counter,
            sync.last_counter,
            info.interval,
            info.channel_map,
            info.access_address,
            info.crc_init,
            info.sca,
            sync.sid,
            sync.advertiser_address or sync.address,
            sync.phy,
            pawr.timing if pawr else None,
            pawr.rsp_aa if pawr else 0,
        )


def counters_related(counter: int, last: int, interval_us: int) -> bool:
    """lastPaEventCounter is equal, adjacent or within 5 s (2.4.2.27)."""
    distance = abs(signed_counter_distance(counter, last))
    return distance <= 1 or distance * interval_us <= LL_PERIODIC_SYNC_COUNTER_SPAN_US


def conn_event_in_span(conn_event_count: int, current: int) -> bool:
    """connEventCount within 2^14 events of the (re)transmitting event (2.4.2.27)."""
    return (
        abs(signed_counter_distance(conn_event_count, current))
        < LL_PERIODIC_SYNC_CONN_EVENT_SPAN
    )


def transfer_indication(
    train: TrainReference, connection: Connection, service_data: int
) -> bytes | None:
    """Describe ``train`` relative to the connection event being transmitted.

    Point at the first train event from this event's anchor, keeping the
    lastPaEventCounter rule, and use this event as connEventCount when the
    offset fits. Otherwise use the nearest earlier event. A future event is
    always within 2^14 events; a past one, forced by an old lastPaEventCounter,
    may not be, and then the train cannot be described until it is received
    again: None (2.4.2.27). The Peripheral's anchor is the one synchronized by
    this event's received packet (4.5.7).
    """
    interval_us = train.interval * LL_PERIODIC_INTERVAL_UNIT_US
    connection_us = connection.parameters.interval_us
    anchor = connection.anchor_us
    events = -((train.anchor_us - anchor) // interval_us)
    target = (train.counter + events) & LL_EVENT_COUNTER_MASK
    if not counters_related(target, train.last_counter, interval_us):
        target = (train.last_counter + 1) & LL_EVENT_COUNTER_MASK
    target_us = train.anchor_of(target)
    distance = 0
    if not 0 <= target_us - anchor <= SYNC_TRANSFER_OFFSET_MAX_US:
        distance = (target_us - anchor) // connection_us
    conn_event_count = (connection.event_counter + distance) & LL_EVENT_COUNTER_MASK
    if not conn_event_in_span(conn_event_count, connection.event_counter):
        return None
    info = SyncInfo(
        target_us - anchor - distance * connection_us,
        train.interval,
        train.channel_map,
        train.access_address,
        train.crc_init,
        target,
        train.sca,
    )
    return PeriodicSyncIndication(
        service_data,
        info.encode(transfer=True),
        conn_event_count,
        train.last_counter,
        train.sid,
        train.address.is_random,
        connection.link.sleep_clock_accuracy,
        1 << (train.phy - 1),
        bytes(train.address),
        connection.sync_event_counter,
        train.pawr,
        train.rsp_aa,
    ).encode()


def indication_valid(
    indication: PeriodicSyncIndication, info: SyncInfo, connection: Connection
) -> bool:
    """The counter rules of 2.4.2.27 that a recipient can check.

    connEventCount is within 2^14 events of this receiving event, and
    lastPaEventCounter is equal or adjacent to PeriodicEventCounter or within
    5 s of it. Outside them the anchor and the drift window are unbounded.
    """
    return conn_event_in_span(
        indication.conn_event_count, connection.event_counter
    ) and counters_related(
        info.event_counter,
        indication.last_counter,
        info.interval * LL_PERIODIC_INTERVAL_UNIT_US,
    )


@dataclass(frozen=True)
class TransferTiming:
    """The first AUX_SYNC_IND a recipient listens for, on its own clock."""

    anchor_us: int
    counter: int
    # Drift references: window_widening(anchor_us - reference_us, SyncInfo SCA)
    # covers Da; ``widening_us`` adds Db (Core Vol 6, Part B, 5.1.13.1).
    reference_us: int
    widening_us: int


def transfer_timing(
    indication: PeriodicSyncIndication, info: SyncInfo, connection: Connection
) -> TransferTiming:
    """Tnominal from connEventCount, the offset and paEventCounter (5.1.13.1).

    connEventCount refers to the nearest event with that counter, in the past
    or the future. Listen first for the train's next event after now.
    """
    interval_us = info.interval * LL_PERIODIC_INTERVAL_UNIT_US
    connection_us = connection.parameters.interval_us
    current = connection.event_counter
    anchor = (
        connection.anchor_us
        + signed_counter_distance(indication.conn_event_count, current) * connection_us
        + info.offset_us
    )
    counter = info.event_counter
    now = connection.radio.now_us
    if anchor <= now:
        events = (now - anchor) // interval_us + 1
        anchor += events * interval_us
        counter = (counter + events) & LL_EVENT_COUNTER_MASK
    train_span = (
        abs(signed_counter_distance(counter, indication.last_counter)) * interval_us
    )
    connection_span = (
        abs(signed_counter_distance(current, indication.sync_conn_event_count))
        * connection_us
    )
    return TransferTiming(
        anchor,
        counter,
        anchor - train_span,
        window_widening(
            connection_span,
            indication.sca,
            local_sca=connection.link.sleep_clock_accuracy,
        )
        - LL_CLOCK_JITTER_US,
    )
