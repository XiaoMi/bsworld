# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""ACL packet execution and LLCP operations at transmit and receive boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from bscontroller.linklayer.procedure import ProcedureContext
from bscontroller.radio.base import Phy, Received


@dataclass(frozen=True)
class ReceiveContext:
    """Uncommitted receive facts supplied by ARQ to LLCP.

    The prospective control ACK is observation only: authentication, allowed-PDU
    checks, and length validation must all succeed before execution consumes it.
    """

    header: int
    payload: bytes
    duplicate: bool
    acknowledged_control: bytes | None = None


@dataclass(frozen=True)
class PacketLimits:
    """Effective receive/transmit limits and safe TX size during a transition."""

    rx_octets: int
    rx_time: int
    tx_payload: int
    rx_payload: int


@dataclass(frozen=True)
class TransmissionConstraints:
    """Published packet budget while a PHY or interval transition is pending."""

    tx_phys: frozenset[Phy]
    rx_phy: Phy
    interval_us: int


class PacketControl(Protocol):
    """The only procedure-facing operations needed by packet execution."""

    def packet_limits(self) -> PacketLimits: ...

    def transmit_power_dbm(self, control_payload: bytes | None) -> int: ...

    def can_send(self, llid: int, payload: bytes) -> bool: ...

    def prepare(self, payload: bytes) -> bytes: ...

    def sent(
        self,
        payload: bytes | None,
        *,
        length: int,
        first: bool,
        procedure: ProcedureContext | None = None,
    ) -> None: ...

    def acknowledged(
        self, payload: bytes, procedure: ProcedureContext | None = None
    ) -> int | None: ...

    def receive(self, context: ReceiveContext) -> bytes | None: ...

    def packet_received(self, length: int, *, duplicate: bool) -> None: ...

    def tx_spacing_us(self, opcode: int | None) -> int: ...

    def rx_spacing_window_us(self, opcode: int | None = None) -> tuple[int, int]: ...

    def response_received(self, spacing_us: int) -> None: ...

    def measure_received_power(self, packet: Received) -> None: ...

    def control(self, payload: bytes) -> None: ...

    def event_prepare(self) -> bool: ...
