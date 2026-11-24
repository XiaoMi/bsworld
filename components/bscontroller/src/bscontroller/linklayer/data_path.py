# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Data-channel packet security and the execution/LLCP contract.

ARQ execution uses effective limits and packet security without inspecting a
control procedure's state machine. Configuration may change at a sent/ACK packet
boundary inside one connection event (Core Vol 6, Part B, 5.1.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from bscontroller.linklayer import (
    LL_ENCRYPTION_COUNTER_BITS,
    LL_ENCRYPTION_COUNTER_SIZE,
    LL_ENCRYPTION_DIRECTION_MASK,
    LL_ENCRYPTION_IV_HALF_SIZE,
    LL_ENCRYPTION_KEY_SIZE,
    PDU_DATA_AUTHENTICATED_HEADER_MASK,
    PDU_DATA_MIC_SIZE,
)
from bscontroller.radio.base import Phy, Received


class PacketCipher:
    """Wire-order IV/SKD and HCI-order LTK; independent counters in each direction."""

    def __init__(self, key: bytes, iv: bytes, *, central: bool):
        if (
            len(key) != LL_ENCRYPTION_KEY_SIZE
            or len(iv) != 2 * LL_ENCRYPTION_IV_HALF_SIZE
        ):
            raise ValueError('LE encryption requires a 128-bit key and 64-bit IV')
        self.aes = AESCCM(key, tag_length=PDU_DATA_MIC_SIZE)
        self.iv = iv
        self.central = central
        self.tx_counter = self.rx_counter = 0

    def nonce(self, counter: int, central: bool) -> bytes:
        if not 0 <= counter < 1 << LL_ENCRYPTION_COUNTER_BITS:
            raise OverflowError('LE encryption packet counter exhausted')
        return (counter | (LL_ENCRYPTION_DIRECTION_MASK if central else 0)).to_bytes(
            LL_ENCRYPTION_COUNTER_SIZE, 'little'
        ) + self.iv

    def encrypt(self, header: int, payload: bytes) -> bytes:
        """AES-CCM with a 4-octet MIC; nonce = packetCounter, direction bit and IV (Core
        Vol 6, Part E, 2).
        """
        if not payload:
            return payload
        result = self.aes.encrypt(
            self.nonce(self.tx_counter, self.central),
            payload,
            bytes([header & PDU_DATA_AUTHENTICATED_HEADER_MASK]),
        )
        self.tx_counter += 1
        return result

    def decrypt(self, header: int, payload: bytes, *, duplicate: bool) -> bytes:
        """AES-CCM verification; a retransmission reuses the previous counter (Core Vol
        6, Part E, 2).
        """
        if not payload:
            return payload
        if len(payload) <= PDU_DATA_MIC_SIZE:
            raise InvalidTag('an encrypted nonempty PDU requires payload and MIC')
        result = self.aes.decrypt(
            self.nonce(self.rx_counter - int(duplicate), not self.central),
            payload,
            bytes([header & PDU_DATA_AUTHENTICATED_HEADER_MASK]),
        )
        if not duplicate:
            self.rx_counter += 1
        return result


@dataclass
class PacketSecurity:
    """Active CCM modes and retry history, shared at explicit packet boundaries."""

    cipher: PacketCipher | None = None
    tx_enabled: bool = False
    rx_enabled: bool = False
    last_received: tuple[int, bytes, bytes, bool] | None = None

    def encrypt(self, header: int, payload: bytes) -> bytes:
        if not self.tx_enabled:
            return payload
        assert self.cipher is not None
        return self.cipher.encrypt(header, payload)


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

    def sent(self, payload: bytes | None, *, length: int, first: bool) -> None: ...

    def acknowledged(self, payload: bytes) -> int | None: ...

    def receive(self, context: ReceiveContext) -> bytes | None: ...

    def packet_received(self, length: int, *, duplicate: bool) -> None: ...

    def measure_received_power(self, packet: Received) -> None: ...

    def control(self, payload: bytes) -> None: ...

    def event_prepare(self) -> bool: ...
