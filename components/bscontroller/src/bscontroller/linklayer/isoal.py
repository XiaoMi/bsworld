# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Transport-independent ISOAL segmentation and recombination (Core Vol 6, G).

The transport supplies one burst of distinct payloads and its synchronization
reference. Repeated and pre-transmitted BIS packets are resolved by the BIG
receiver before they reach this layer. HCI fragmentation is owned by the data
path, so neither HCI packet boundaries nor radio scheduling enter this codec.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import IntEnum
from typing import Sequence


class IsoStatus(IntEnum):
    VALID = 0
    POSSIBLY_INVALID = 1
    LOST = 2


@dataclass(frozen=True)
class IsoSdu:
    sequence_number: int
    data: bytes
    reference_us: int
    status: IsoStatus = IsoStatus.VALID


@dataclass(frozen=True)
class IsoPdu:
    llid: int
    data: bytes


@dataclass(frozen=True)
class IsoalParameters:
    framed: bool
    bn: int
    max_pdu: int
    max_sdu: int
    iso_interval_us: int
    sdu_interval_us: int

    def __post_init__(self) -> None:
        if not (
            1 <= self.bn <= 15
            and 1 <= self.max_pdu <= 251
            and 1 <= self.max_sdu <= 4095
            and self.iso_interval_us > 0
            and self.sdu_interval_us > 0
        ):
            raise ValueError('invalid ISOAL parameters')
        if self.framed:
            if self.max_pdu < 6:
                raise ValueError('framed PDU cannot hold a first segment')
        else:
            uspi, remainder = divmod(self.iso_interval_us, self.sdu_interval_us)
            if (
                remainder
                or not 1 <= uspi <= self.bn
                or self.bn % uspi
                or self.max_sdu > self.max_pdu * (self.bn // uspi)
            ):
                raise ValueError('unframed SDUs do not fit their allocated PDUs')

    @property
    def sdus_per_interval(self) -> int:
        return self.iso_interval_us // self.sdu_interval_us


@dataclass(frozen=True)
class IsoalBurst:
    pdus: tuple[IsoPdu, ...]
    # Burst PDU index and SDU; a transport records TX timing only after that
    # payload was actually transmitted, including an eventual repetition.
    started: tuple[tuple[int, IsoSdu], ...]
    # SDUs whose storage can be released, including late SDUs that cannot
    # be represented by a framed Time_Offset.
    completed: tuple[IsoSdu, ...]


class IsoalTransmitter:
    def __init__(self, parameters: IsoalParameters):
        self.parameters = parameters
        self.pending: IsoSdu | None = None
        self.position = 0

    def segment(self, sdus: deque[IsoSdu], anchor_us: int) -> IsoalBurst:
        """Consume eligible SDUs; retain a framed continuation across bursts."""
        if self.parameters.framed:
            return self._framed(sdus, anchor_us)
        return self._unframed(sdus, anchor_us)

    def _unframed(self, sdus: deque[IsoSdu], anchor_us: int) -> IsoalBurst:
        parameters = self.parameters
        upps = parameters.bn // parameters.sdus_per_interval
        pdus: list[IsoPdu] = []
        consumed: list[tuple[int, IsoSdu]] = []
        for _ in range(parameters.sdus_per_interval):
            if not sdus or sdus[0].reference_us >= anchor_us:
                pdus.extend(IsoPdu(1, b'') for _ in range(upps))
                continue
            sdu = sdus.popleft()
            if len(sdu.data) > parameters.max_sdu:
                raise ValueError('SDU exceeds Max_SDU')
            consumed.append((len(pdus), sdu))
            for index in range(upps):
                start = index * parameters.max_pdu
                fragment = sdu.data[start : start + parameters.max_pdu]
                # An empty SDU is represented by LLID=0, including each of
                # its allocated empty fragments (Vol 6, Part G, 2.1).
                end = start < len(sdu.data) <= start + parameters.max_pdu
                pdus.append(IsoPdu(0 if end or not sdu.data else 1, fragment))
        return IsoalBurst(
            tuple(pdus), tuple(consumed), tuple(sdu for _, sdu in consumed)
        )

    def _framed(self, sdus: deque[IsoSdu], anchor_us: int) -> IsoalBurst:
        parameters = self.parameters
        pdus: list[IsoPdu] = []
        started: list[tuple[int, IsoSdu]] = []
        completed: list[IsoSdu] = []
        for _ in range(parameters.bn):
            data = bytearray()
            while True:
                continuation = self.pending is not None
                overhead = 2 if continuation else 5
                capacity = parameters.max_pdu - len(data) - overhead
                if capacity < 0:
                    break
                if self.pending is None:
                    if not sdus or sdus[0].reference_us >= anchor_us:
                        break
                    if capacity == 0 and sdus[0].data:
                        break
                    sdu = sdus.popleft()
                    if len(sdu.data) > parameters.max_sdu:
                        raise ValueError('SDU exceeds Max_SDU')
                    offset = anchor_us - sdu.reference_us
                    if offset > 0xFFFFFF:
                        completed.append(sdu)
                        continue
                    self.pending = sdu
                    self.position = 0
                    started.append((len(pdus), sdu))
                sdu = self.pending
                fragment = sdu.data[self.position : self.position + capacity]
                self.position += len(fragment)
                complete = self.position == len(sdu.data)
                data.extend(
                    (
                        int(continuation) | int(complete) << 1,
                        len(fragment) + overhead - 2,
                    )
                )
                if not continuation:
                    data.extend((anchor_us - sdu.reference_us).to_bytes(3, 'little'))
                data.extend(fragment)
                if complete:
                    completed.append(sdu)
                    self.pending = None
                else:
                    break
            pdus.append(IsoPdu(2, bytes(data)))
        return IsoalBurst(tuple(pdus), tuple(started), tuple(completed))


class IsoalReceiver:
    def __init__(self, parameters: IsoalParameters):
        self.parameters = parameters
        self.sequence_number = 0
        self.pending: bytearray | None = None
        self.pending_reference_us = 0
        self.next_reference_us: int | None = None

    def _emit(self, data: bytes, reference_us: int, status: IsoStatus) -> IsoSdu:
        sdu = IsoSdu(self.sequence_number, data, reference_us, status)
        self.sequence_number = (self.sequence_number + 1) & 0xFFFF
        self.next_reference_us = reference_us + self.parameters.sdu_interval_us
        return sdu

    def recombine(
        self, pdus: Sequence[IsoPdu | None], reference_us: int
    ) -> tuple[IsoSdu, ...]:
        """Recombine an ordered BN burst, with None for an unavailable payload.

        The transport supplies its synchronization reference including its timing
        adjustment. A framed start carries the remaining per-SDU Time_Offset.
        """
        if len(pdus) != self.parameters.bn:
            raise ValueError('ISOAL burst length differs from BN')
        if self.parameters.framed:
            return self._framed(pdus, reference_us)
        return self._unframed(pdus, reference_us)

    def _unframed(
        self, pdus: Sequence[IsoPdu | None], reference_us: int
    ) -> tuple[IsoSdu, ...]:
        parameters = self.parameters
        upps = parameters.bn // parameters.sdus_per_interval
        sdus: list[IsoSdu] = []
        for index in range(parameters.sdus_per_interval):
            data = bytearray()
            missing = False
            complete = False
            for pdu in pdus[index * upps : (index + 1) * upps]:
                if complete:
                    # Only padding follows the end. Zero-sized SDUs may encode
                    # every allocated fragment as empty LLID=0 (Part G, 2.1).
                    allowed = (1,) if data else (0, 1)
                    if pdu is not None and (pdu.data or pdu.llid not in allowed):
                        missing = True
                    continue
                if pdu is None or pdu.llid not in (0, 1):
                    missing = True
                    continue
                data.extend(pdu.data)
                complete = pdu.llid == 0
            if len(data) > parameters.max_sdu:
                data = data[: parameters.max_sdu]
                missing = True
            status = (
                IsoStatus.LOST
                if not complete and not data
                else IsoStatus.POSSIBLY_INVALID
                if missing or not complete
                else IsoStatus.VALID
            )
            sdus.append(
                self._emit(
                    bytes(data),
                    reference_us + index * parameters.sdu_interval_us,
                    status,
                )
            )
        return tuple(sdus)

    def _framed(
        self, pdus: Sequence[IsoPdu | None], reference_us: int
    ) -> tuple[IsoSdu, ...]:
        sdus: list[IsoSdu] = []
        parameters = self.parameters

        def discard() -> None:
            if self.pending is not None:
                sdus.append(self._emit(b'', self.pending_reference_us, IsoStatus.LOST))
                self.pending = None

        for pdu in pdus:
            if pdu is None or pdu.llid != 2:
                discard()
                continue
            position = 0
            while position < len(pdu.data):
                if len(pdu.data) - position < 2:
                    discard()
                    break
                flags, length = pdu.data[position : position + 2]
                position += 2
                end = position + length
                continuation, complete = bool(flags & 1), bool(flags & 2)
                if end > len(pdu.data) or (not continuation and length < 3):
                    discard()
                    break
                if not continuation:
                    discard()
                    offset = int.from_bytes(pdu.data[position : position + 3], 'little')
                    position += 3
                    sync_reference = reference_us - offset
                    # Framed headers reconstruct the timing of SDUs that were
                    # entirely missing. A half interval tolerates clock drift.
                    while (
                        self.next_reference_us is not None
                        and self.next_reference_us + parameters.sdu_interval_us // 2
                        < sync_reference
                    ):
                        sdus.append(
                            self._emit(b'', self.next_reference_us, IsoStatus.LOST)
                        )
                    self.pending = bytearray()
                    self.pending_reference_us = sync_reference
                if self.pending is not None:
                    self.pending.extend(pdu.data[position:end])
                    if len(self.pending) > parameters.max_sdu:
                        discard()
                    elif complete:
                        sdus.append(
                            self._emit(
                                bytes(self.pending),
                                self.pending_reference_us,
                                IsoStatus.VALID,
                            )
                        )
                        self.pending = None
                position = end
        return tuple(sdus)
