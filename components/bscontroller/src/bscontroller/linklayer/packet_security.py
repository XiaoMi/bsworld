# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LE packet encryption, counters and retransmission history (Core Vol 6, Part E)."""

from __future__ import annotations

from dataclasses import dataclass

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
