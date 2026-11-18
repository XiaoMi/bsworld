# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""BIS wire identities, BIGInfo and security (Core Vol 6, Part B, 2.6.2, 4.4.6).

These values and codecs have no radio, HCI or Host dependency. BIG role owners
use them to describe the same stream to a peer and to the ISO data path.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from random import Random

from cryptography.hazmat.primitives.ciphers import algorithms
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.cmac import CMAC

from bscontroller.linklayer.bitstream import valid_access_address
from bscontroller.linklayer.parameters import valid_channel_map
from bscontroller.radio.base import Phy

BIG_INFO_AD_TYPE = 0x2C
BIG_COUNTER_MASK = (1 << 39) - 1
BIG_INTERVAL_UNIT_US = 1250
BIG_INFO_CLEAR_SIZE = 33
BIG_INFO_ENCRYPTED_SIZE = 57
BIS_PDU_DATA_MAX = 251
BIS_MIC_SIZE = 4


def bis_access_address(seed: int, number: int) -> int:
    """Derive a BIS AA, or control AA for number 0 (Vol 6, Part B, 2.1.2)."""
    diversifier = (35 * number + 42) % 128
    word = (
        (0xFC000000 if diversifier & 1 else 0)
        | (diversifier & 2) << 24
        | (diversifier & 0x40) << 18
        | (diversifier & 2) << 22
        | (diversifier & 0x30) << 16
        | (diversifier & 0x0C) << 15
    )
    return seed ^ word


def generate_big_seed(
    generator: Random, count: int, used: set[int], seeds: Iterable[int] = ()
) -> int:
    """Seeded simulation identity satisfying every derived AA's PHY constraints."""
    seeds = tuple(seeds)
    while True:
        seed = generator.getrandbits(32)
        bit15 = seed >> 15 & 1
        seed &= ~((1 << 16) | (1 << 19) | (1 << 22) | (1 << 25))
        seed |= bit15 << 19 | (1 - bit15) << 16 | (1 - bit15) << 22 | 1 << 23
        addresses = {bis_access_address(seed, n) for n in range(count + 1)}
        if (
            not addresses & used
            and all(((seed ^ previous) & 0xFFFF).bit_count() >= 2 for previous in seeds)
            and all(map(valid_access_address, addresses))
        ):
            return seed


@dataclass(frozen=True)
class BigInfo:
    offset_us: int
    iso_interval: int
    num_bis: int
    nse: int
    bn: int
    sub_interval_us: int
    pto: int
    bis_spacing_us: int
    irc: int
    max_pdu: int
    seed_access_address: int
    sdu_interval_us: int
    max_sdu: int
    base_crc_init: int
    channel_map: int
    phy: Phy
    payload_count: int = 0
    framed: bool = False
    framing_mode: int = 0
    giv: bytes = b''
    gskd: bytes = b''

    def __post_init__(self) -> None:
        if not (
            4 <= self.iso_interval <= 3200
            and 1 <= self.num_bis <= 31
            and 1 <= self.bn <= 7
            and 1 <= self.nse <= 31
            and self.nse % self.bn == 0
            and 1 <= self.irc <= self.nse // self.bn
            and 0 <= self.pto <= 15
            and (self.nse == self.bn * self.irc or self.pto > 0)
            and 1 <= self.max_pdu <= BIS_PDU_DATA_MAX
            and 1 <= self.max_sdu <= 4095
            and 255 <= self.sdu_interval_us <= 0x0FFFFF
            and 1 <= self.sub_interval_us <= 0x0FFFFF
            and 1 <= self.bis_spacing_us <= 0x0FFFFF
            and 0 <= self.payload_count <= BIG_COUNTER_MASK
            and 0 <= self.seed_access_address <= 0xFFFFFFFF
            and 0 <= self.base_crc_init <= 0xFFFF
            and self.framing_mode in (0, 1)
            and valid_channel_map(self.channel_map)
            and self.phy in tuple(Phy)
            and (
                (not self.giv and not self.gskd)
                or (len(self.giv) == 8 and len(self.gskd) == 16)
            )
        ):
            raise ValueError('invalid BIGInfo parameters')

    @property
    def encrypted(self) -> bool:
        return bool(self.giv)

    @property
    def interval_us(self) -> int:
        return self.iso_interval * BIG_INTERVAL_UNIT_US

    @property
    def sync_delay_us(self) -> int:
        return (
            (self.num_bis - 1) * self.bis_spacing_us
            + (self.nse - 1) * self.sub_interval_us
            + self.phy.airtime_us(2 + self.max_pdu + BIS_MIC_SIZE * self.encrypted)
        )

    @property
    def control_offset_us(self) -> int:
        """Start of the optional control subevent (Vol 6, Part B, 4.4.6.7)."""
        if self.bis_spacing_us >= self.nse * self.sub_interval_us:
            return self.num_bis * self.bis_spacing_us
        return self.nse * self.sub_interval_us

    @property
    def duration_us(self) -> int:
        """Reserve the longer of the data and maximum control subevents."""
        control_time = self.phy.airtime_us(10 + BIS_MIC_SIZE * self.encrypted)
        return max(self.sync_delay_us, self.control_offset_us + control_time)

    @property
    def transport_latency_us(self) -> int:
        return (
            self.sync_delay_us
            + (self.pto * (self.nse // self.bn - self.irc) + 1) * self.interval_us
            + (self.sdu_interval_us if self.framed else -self.sdu_interval_us)
        )

    def access_address(self, number: int) -> int:
        return bis_access_address(self.seed_access_address, number)

    def crc_init(self, number: int) -> int:
        return self.base_crc_init << 8 | number

    def encode(self) -> bytes:
        """Pack BIGInfo least-significant bit first (Vol 6, Part B, 4.4.6.11)."""
        unit = 300 if self.offset_us >= 491460 else 30
        offset = self.offset_us // unit
        if not 0 < offset < 1 << 14 or offset * unit <= 600:
            raise ValueError('BIG offset is outside its wire range')
        fields = (
            (offset, 14),
            (int(unit == 300), 1),
            (self.iso_interval, 12),
            (self.num_bis, 5),
            (self.nse, 5),
            (self.bn, 3),
            (self.sub_interval_us, 20),
            (self.pto, 4),
            (self.bis_spacing_us, 20),
            (self.irc, 4),
            (self.max_pdu, 8),
            (0, 7),
            (self.framing_mode, 1),
            (self.seed_access_address, 32),
            (self.sdu_interval_us, 20),
            (self.max_sdu, 12),
            (self.base_crc_init, 16),
            (self.channel_map, 37),
            (int(self.phy) - 1, 3),
            (self.payload_count, 39),
            (int(self.framed), 1),
        )
        value = shift = 0
        for field, width in fields:
            value |= field << shift
            shift += width
        return value.to_bytes(BIG_INFO_CLEAR_SIZE, 'little') + self.giv + self.gskd

    @classmethod
    def decode(cls, data: bytes) -> BigInfo:
        if len(data) not in (BIG_INFO_CLEAR_SIZE, BIG_INFO_ENCRYPTED_SIZE):
            raise ValueError('invalid BIGInfo length')
        value = int.from_bytes(data[:BIG_INFO_CLEAR_SIZE], 'little')

        def take(width: int) -> int:
            nonlocal value
            result = value & ((1 << width) - 1)
            value >>= width
            return result

        offset, units = take(14), take(1)
        iso_interval, num_bis, nse, bn = take(12), take(5), take(5), take(3)
        sub_interval, pto, spacing, irc, max_pdu = (
            take(20),
            take(4),
            take(20),
            take(4),
            take(8),
        )
        take(7)  # Reserved bits do not affect reception.
        mode = take(1)
        seed, sdu_interval, max_sdu, crc = take(32), take(20), take(12), take(16)
        channel_map, phy, counter, framed = take(37), take(3), take(39), take(1)
        if offset * (300 if units else 30) <= 600:
            raise ValueError('invalid BIGInfo offset')
        return cls(
            offset_us=offset * (300 if units else 30),
            iso_interval=iso_interval,
            num_bis=num_bis,
            nse=nse,
            bn=bn,
            sub_interval_us=sub_interval,
            pto=pto,
            bis_spacing_us=spacing,
            irc=irc,
            max_pdu=max_pdu,
            seed_access_address=seed,
            sdu_interval_us=sdu_interval,
            max_sdu=max_sdu,
            base_crc_init=crc,
            channel_map=channel_map,
            phy=Phy(phy + 1),
            payload_count=counter,
            framed=bool(framed),
            framing_mode=mode,
            giv=data[33:41],
            gskd=data[41:57],
        )


def group_session_key(broadcast_code: bytes, gskd: bytes) -> bytes:
    """HCI-order GSK via h7/h6/h8 (Vol 6, Part E, 1.1.1 and 2.5)."""
    if len(broadcast_code) != 16 or len(gskd) != 16:
        raise ValueError('Broadcast Code and GSKD must each contain 16 octets')

    def cmac(key: bytes, data: bytes) -> bytes:
        context = CMAC(algorithms.AES(key))
        context.update(data)
        return context.finalize()

    igltk = cmac(bytes(12) + b'BIG1', broadcast_code[::-1])
    gltk = cmac(igltk, b'BIG2')
    return cmac(cmac(gskd[::-1], gltk), b'BIG3')[::-1]


class BisCipher:
    """Counter-addressed CCM; repeats of one payload reuse its nonce (Vol 6, E, 2)."""

    def __init__(self, key: bytes, giv: bytes, access_address: int):
        self.aes = AESCCM(key[::-1], tag_length=BIS_MIC_SIZE)
        self.iv = (int.from_bytes(giv[:4], 'little') ^ access_address).to_bytes(
            4, 'little'
        ) + giv[4:]

    def nonce(self, payload_count: int) -> bytes:
        if not 0 <= payload_count <= BIG_COUNTER_MASK:
            raise OverflowError('BIS payload counter exhausted')
        return (payload_count | 1 << 39).to_bytes(5, 'little') + self.iv

    def encrypt(self, header: int, data: bytes, payload_count: int) -> bytes:
        return (
            self.aes.encrypt(self.nonce(payload_count), data, bytes([header & 0xC3]))
            if data
            else b''
        )

    def decrypt(self, header: int, data: bytes, payload_count: int) -> bytes:
        return (
            self.aes.decrypt(self.nonce(payload_count), data, bytes([header & 0xC3]))
            if data
            else b''
        )
