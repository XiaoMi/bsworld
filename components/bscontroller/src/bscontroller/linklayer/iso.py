# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Shared ISO data paths, Host fragment assembly and ISO test mode.

Stream transports register handles and supply their ISOAL geometry. This layer
owns SDU queues and test counters, without depending on BIGs, connections, HCI
packet classes or the simulator (Core Vol 4, E, 5.4.5; Vol 6, B, 7).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from bscontroller.errors import ErrorCode
from bscontroller.linklayer.isoal import (
    IsoalBurst,
    IsoalParameters,
    IsoalReceiver,
    IsoalTransmitter,
    IsoPdu,
    IsoSdu,
    IsoStatus,
)

SUCCESS = ErrorCode.SUCCESS
UNKNOWN = ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
DISALLOWED = ErrorCode.COMMAND_DISALLOWED_ERROR
INVALID = ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
UNSUPPORTED = ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR


class IsoListener(Protocol):
    def on_iso_received(self, handle: int, sdu: IsoSdu) -> None: ...
    def on_iso_completed(self, handle: int, count: int) -> None: ...


@dataclass(frozen=True)
class IsoTxSync:
    sequence_number: int
    timestamp_us: int
    offset_us: int


@dataclass
class IsoTestCounters:
    received: int = 0
    missed: int = 0
    failed: int = 0


@dataclass
class IsoAssembly:
    sequence_number: int
    length: int
    reference_us: int
    data: bytearray = field(default_factory=bytearray)


class IsoStream:
    def __init__(
        self,
        handle: int,
        transmit: bool,
        parameters: IsoalParameters,
        listener: IsoListener,
        synchronization_delay_us: int,
    ):
        self.handle = handle
        self.transmit = transmit
        self.parameters = parameters
        self.listener = listener
        self.synchronization_delay_us = synchronization_delay_us
        self.configured = False
        self.controller_delay_us = 0
        self.queue: deque[IsoSdu] = deque()
        self.assembly: IsoAssembly | None = None
        self.pending_packets = 0
        self.deliveries: deque[IsoSdu] = deque()
        self.tx = IsoalTransmitter(parameters)
        self.rx = IsoalReceiver(parameters)
        self.tx_sync: IsoTxSync | None = None
        self.pending_tx_sync: dict[int, IsoTxSync] = {}
        self.test_type: int | None = None
        self.test_sequence = 0
        self.test_reference_us: int | None = None
        self.counters = IsoTestCounters()
        self.expected_test_sequence: int | None = None

    def discard(self) -> None:
        packets = self.pending_packets
        self.queue.clear()
        self.deliveries.clear()
        self.pending_packets = 0
        self.pending_tx_sync.clear()
        self.assembly = None
        self.tx = IsoalTransmitter(self.parameters)
        if packets:
            self.listener.on_iso_completed(self.handle, packets)

    def accept(
        self,
        fragment: bytes,
        pb_flag: int,
        sequence_number: int | None,
        length: int | None,
        reference_us: int,
    ) -> None:
        """Release copied fragments; retain one Host credit per queued SDU."""
        if not self.configured or not self.transmit or self.test_type is not None:
            self.listener.on_iso_completed(self.handle, 1)
            return
        if pb_flag in (0, 2):
            self.assembly = None
            if (
                sequence_number is not None
                and length is not None
                and 0 <= length <= self.parameters.max_sdu
            ):
                self.assembly = IsoAssembly(sequence_number, length, reference_us)
        assembly = self.assembly
        if assembly is None:
            self.listener.on_iso_completed(self.handle, 1)
            return
        assembly.data.extend(fragment)
        if len(assembly.data) > assembly.length or (
            pb_flag in (2, 3) and len(assembly.data) != assembly.length
        ):
            self.listener.on_iso_completed(self.handle, 1)
            self.assembly = None
            return
        if pb_flag in (2, 3):
            sdu = IsoSdu(
                assembly.sequence_number, bytes(assembly.data), assembly.reference_us
            )
            self.pending_packets += 1
            self.queue.append(sdu)
            self.assembly = None
        else:
            # The final fragment may need this buffer credit to reach us.
            self.listener.on_iso_completed(self.handle, 1)

    def burst(self, anchor_us: int, event_counter: int) -> IsoalBurst:
        parameters = self.parameters
        if self.test_type is not None:
            if self.test_reference_us is None:
                self.test_reference_us = anchor_us - parameters.iso_interval_us
            while self.test_reference_us < anchor_us:
                sequence = (
                    self.test_sequence
                    if parameters.framed
                    else event_counter * parameters.sdus_per_interval + len(self.queue)
                )
                size = (
                    0
                    if self.test_type == 0
                    else 4 + sequence % (parameters.max_sdu - 3)
                    if self.test_type == 1
                    else parameters.max_sdu
                )
                data = (
                    (sequence & 0xFFFFFFFF).to_bytes(4, 'little')
                    + bytes(max(0, size - 4))
                    if size
                    else b''
                )
                self.queue.append(
                    IsoSdu(sequence & 0xFFFF, data, self.test_reference_us)
                )
                self.test_sequence += 1
                self.test_reference_us += parameters.sdu_interval_us
        burst = self.tx.segment(self.queue, anchor_us)
        for index, sdu in burst.started:
            self.pending_tx_sync[event_counter * parameters.bn + index] = IsoTxSync(
                sdu.sequence_number,
                anchor_us,
                anchor_us - sdu.reference_us if parameters.framed else 0,
            )

        if self.configured and burst.completed:
            packets = len(burst.completed)
            self.pending_packets -= packets
            self.listener.on_iso_completed(self.handle, packets)
        return burst

    def transmitted(self, payload_counter: int) -> None:
        if (sync := self.pending_tx_sync.pop(payload_counter, None)) is not None:
            self.tx_sync = sync

    def expire_payloads(self, event_counter: int) -> None:
        boundary = (event_counter + 1) * self.parameters.bn
        self.pending_tx_sync = {
            counter: sync
            for counter, sync in self.pending_tx_sync.items()
            if counter >= boundary
        }

    def received(
        self,
        pdus: tuple[IsoPdu | None, ...],
        anchor_us: int,
        event_counter: int,
        *,
        now_us: int,
    ) -> None:
        reference_us = anchor_us + self.synchronization_delay_us
        for index, sdu in enumerate(self.rx.recombine(pdus, reference_us)):
            if self.test_type is not None:
                self._test_received(sdu, event_counter, index)
            elif self.configured:
                self.deliveries.append(sdu)
        self.flush(now_us)

    @property
    def next_delivery_us(self) -> int | None:
        if self.deliveries:
            return self.deliveries[0].reference_us + self.controller_delay_us
        return None

    def flush(self, now_us: int) -> None:
        while (due := self.next_delivery_us) is not None and due <= now_us:
            self.listener.on_iso_received(self.handle, self.deliveries.popleft())

    def _test_received(self, sdu: IsoSdu, event_counter: int, index: int) -> None:
        parameters = self.parameters
        if sdu.status != IsoStatus.VALID:
            self.counters.missed = (self.counters.missed + 1) & 0xFFFFFFFF
        else:
            sequence = int.from_bytes(sdu.data[:4], 'little')
            if parameters.framed:
                if self.expected_test_sequence is None:
                    self.expected_test_sequence = sequence
                expected = self.expected_test_sequence
            else:
                expected = (
                    event_counter * parameters.sdus_per_interval + index
                ) & 0xFFFFFFFF
            valid = (
                not sdu.data
                if self.test_type == 0
                else len(sdu.data) >= 4
                and (
                    len(sdu.data) <= parameters.max_sdu
                    if self.test_type == 1
                    else len(sdu.data) == parameters.max_sdu
                )
                and sequence == expected
            )
            if valid:
                self.counters.received = (self.counters.received + 1) & 0xFFFFFFFF
            else:
                self.counters.failed = (self.counters.failed + 1) & 0xFFFFFFFF
        if self.expected_test_sequence is not None:
            self.expected_test_sequence = (self.expected_test_sequence + 1) & 0xFFFFFFFF


class IsoDataPaths:
    def __init__(self, listener: IsoListener):
        self.listener = listener
        self.streams: dict[int, dict[bool, IsoStream]] = {}

    def contains(self, handle: int) -> bool:
        return handle in self.streams

    def connection_handles(self) -> set[int]:
        return set(self.streams)

    def add(
        self,
        handle: int,
        transmit: bool,
        parameters: IsoalParameters,
        *,
        synchronization_delay_us: int = 0,
    ) -> None:
        directions = self.streams.setdefault(handle, {})
        if transmit in directions:
            raise ValueError('ISO direction already registered')
        directions[transmit] = IsoStream(
            handle, transmit, parameters, self.listener, synchronization_delay_us
        )

    def get_stream(self, handle: int, transmit: bool) -> IsoStream:
        return self.streams[handle][transmit]

    def release(self, handle: int) -> None:
        for stream in self.streams.pop(handle).values():
            stream.discard()

    def reset(self) -> None:
        # HCI Reset discards pending packets without completion events.
        self.streams.clear()

    @property
    def next_delivery_us(self) -> int | None:
        return min(
            (
                due
                for directions in self.streams.values()
                if (rx := directions.get(False)) is not None
                and (due := rx.next_delivery_us) is not None
            ),
            default=None,
        )

    def flush(self, now_us: int) -> None:
        for directions in tuple(self.streams.values()):
            if (stream := directions.get(False)) is not None:
                stream.flush(now_us)

    def setup(
        self,
        handle: int,
        direction: int,
        path_id: int,
        codec: int,
        configuration: bytes,
        controller_delay_us: int = 0,
    ) -> int:
        directions = self.streams.get(handle)
        if directions is None:
            return UNKNOWN
        if (
            direction not in (0, 1)
            or path_id == 0xFF
            or not 0 <= controller_delay_us <= 4000000
        ):
            return INVALID
        if path_id != 0:
            return DISALLOWED
        stream = directions.get(direction == 0)
        if stream is None:
            return DISALLOWED
        if stream.configured or stream.test_type is not None:
            return DISALLOWED
        # HCI path carries transparent, codec-independent SDUs. Coding format
        # 0x03 is Transparent; encoded audio remains a Host responsibility.
        if codec != 0x03 or configuration:
            return INVALID
        stream.configured = True
        stream.controller_delay_us = controller_delay_us
        return SUCCESS

    def remove(self, handle: int, direction: int) -> int:
        directions = self.streams.get(handle)
        if directions is None:
            return UNKNOWN
        if direction not in (1, 2, 3):
            return INVALID
        # Check every requested direction before removing any data path.
        # A direction with no registered path also counts as not set up.
        selected = []
        for transmit, bit in ((True, 1), (False, 2)):
            if not direction & bit:
                continue
            stream = directions.get(transmit)
            if stream is None or not stream.configured:
                return DISALLOWED
            selected.append(stream)
        for stream in selected:
            stream.discard()
            stream.configured = False
        return SUCCESS

    def test(self, handle: int, transmit: bool, payload_type: int) -> int:
        directions = self.streams.get(handle)
        if directions is None:
            return UNKNOWN
        stream = directions.get(transmit)
        if stream is None:
            return UNSUPPORTED
        if payload_type not in (0, 1, 2) or (
            payload_type and stream.parameters.max_sdu < 4
        ):
            return INVALID
        if stream.configured:
            return DISALLOWED
        stream.discard()
        stream.rx = IsoalReceiver(stream.parameters)
        stream.test_type = payload_type
        stream.test_sequence = 0
        stream.test_reference_us = None
        stream.counters = IsoTestCounters()
        stream.expected_test_sequence = None
        return SUCCESS

    def input(
        self,
        handle: int,
        fragment: bytes,
        pb_flag: int,
        sequence_number: int | None,
        length: int | None,
        timestamp_us: int | None,
        now_us: int,
    ) -> None:
        directions = self.streams.get(handle)
        if directions is None:
            return
        stream = directions.get(True)
        if stream is None:
            self.listener.on_iso_completed(handle, 1)
            return
        if timestamp_us is None:
            reference_us = now_us - stream.controller_delay_us
        else:
            # HCI Time_Stamp wraps at 32 bits; resolve it around the current
            # Controller clock before ISOAL computes a positive Time_Offset.
            reference_us = (
                now_us + ((timestamp_us - now_us + (1 << 31)) & 0xFFFFFFFF) - (1 << 31)
            )
        stream.accept(fragment, pb_flag, sequence_number, length, reference_us)

    def read_tx_sync(self, handle: int) -> tuple[int, IsoTxSync | None]:
        directions = self.streams.get(handle)
        if directions is None:
            return UNKNOWN, None
        stream = directions.get(True)
        if stream is None or stream.tx_sync is None:
            return DISALLOWED, None
        return SUCCESS, stream.tx_sync

    def counters(
        self, handle: int, *, end: bool = False
    ) -> tuple[int, IsoTestCounters]:
        directions = self.streams.get(handle)
        if directions is None:
            return UNKNOWN, IsoTestCounters()
        active = [
            stream for stream in directions.values() if stream.test_type is not None
        ]
        receiver = directions.get(False)
        if not active or not end and receiver not in active:
            return UNSUPPORTED, IsoTestCounters()
        counters = receiver.counters if receiver in active else IsoTestCounters()
        if end:
            for stream in active:
                stream.test_type = None
                stream.discard()
        return SUCCESS, IsoTestCounters(
            counters.received, counters.missed, counters.failed
        )
