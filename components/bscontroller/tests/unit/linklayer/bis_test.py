# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""BIG identities, advertised timing and authentication against wire vectors."""

from random import Random

import pytest
from cryptography.exceptions import InvalidTag

from bscontroller.linklayer.bis import (
    BIG_COUNTER_MASK,
    BigInfo,
    BisCipher,
    bis_access_address,
    generate_big_seed,
    group_session_key,
)
from bscontroller.radio.base import Phy
from tests.unit.helpers import bis_info, check_access_address


def test_bis_diversifier_and_crc_octet():
    """Derive the BIS #1 diversifier and CRC ordering from Core Vol 6, B, 2.1.2.

    Steps:
        1. Derive BIS #1 and its CRC from a fixed seed and BaseCRCInit.

    Expected:
        - The diversifier is 0xFD060000; the BIS number is the CRC's low octet.
    """
    info = bis_info()
    assert info.access_address(1) == 0x12345678 ^ 0xFD060000
    assert info.crc_init(1) == 0x123401


def test_big_seeds_validate_every_derived_address_and_separate_groups():
    """Validate generated identities with an independent bit-string oracle.

    Steps:
        1. Generate ten groups of 31 BISes and their control links.
        2. Check every AA and the low-16-bit distance from previous groups.
        3. Replay the same seeded generator.

    Expected:
        - No AA repeats, all waveform constraints hold, seed distance is at
          least two bits, and replay is deterministic.
    """
    generator = Random(17)
    used = set()
    seeds = []
    for _ in range(10):
        seed = generate_big_seed(generator, 31, used, seeds)
        assert all(((seed ^ previous) & 0xFFFF).bit_count() >= 2 for previous in seeds)
        for number in range(32):
            address = bis_access_address(seed, number)
            check_access_address(address)
            assert address not in used
            used.add(address)
        seeds.append(seed)
    assert generate_big_seed(Random(17), 31, set()) == seeds[0]


@pytest.mark.parametrize('encrypted', [False, True])
@pytest.mark.parametrize('phy', list(Phy))
def test_biginfo_wire_fields_and_reserved_bits(encrypted, phy):
    """BIGInfo preserves timing, PHY and key material in its packed AD value.

    Steps:
        1. Encode fixed parameters, setting Framing_Mode and a 39-bit counter.
        2. Inspect fixed byte boundaries and set all seven RFU bits.
        3. Decode and compare the complete parameter set.

    Expected:
        - Framing_Mode occupies bit 7 of byte 12, the seed is little endian,
          reserved bits are ignored, and keys append GIV then GSKD.
    """
    info = bis_info(
        phy=phy,
        framed=True,
        framing_mode=1,
        payload_count=BIG_COUNTER_MASK,
        giv=bytes(range(8)) if encrypted else b'',
        gskd=bytes(range(16)) if encrypted else b'',
    )
    encoded = info.encode()
    assert len(encoded) == (57 if encrypted else 33)
    assert encoded[:4] == bytes.fromhex('21000408')
    assert encoded[12] == 0x80
    assert encoded[13:17] == bytes.fromhex('78563412')
    assert encoded[28:33] == bytes.fromhex('ffffffffff')
    assert encoded[33:] == info.giv + info.gskd
    reserved = encoded[:12] + bytes([encoded[12] | 0x7F]) + encoded[13:]
    assert BigInfo.decode(reserved) == info


def test_biginfo_quantizes_large_offsets_and_rejects_truncation():
    """BIG_Offset uses the required unit and rejects unusable advertisements.

    Steps:
        1. Encode offsets on both sides of the 491460-us unit transition.
        2. Decode a truncated structure and attempt a <=600-us offset.

    Expected:
        - Quantization brackets the anchor within one unit; malformed values
          raise ValueError instead of silently starting synchronization.
    """
    for offset, unit in ((491459, 30), (491460, 300)):
        decoded = BigInfo.decode(bis_info(offset_us=offset).encode())
        assert decoded.offset_us <= offset < decoded.offset_us + unit
    with pytest.raises(ValueError):
        BigInfo.decode(bis_info().encode()[:-1])
    with pytest.raises(ValueError):
        bis_info(offset_us=600).encode()


def test_group_session_key_core_vector():
    """Derive the Core Vol 6, Part C, group session key sample.

    Steps:
        1. Supply the published Broadcast_Code and GSKD in HCI octet order.

    Expected:
        - The complete h7/h6/h8 chain produces the published GSK.
    """
    code = bytes.fromhex('000000006573756f4820656e72b8c342')[::-1]
    gskd = bytes.fromhex('55188b3d32f6bb9a900afcfbeed4e72a')[::-1]
    assert (
        group_session_key(code, gskd)
        == bytes.fromhex('be2a16fc7ac464e752301bccc818812c')[::-1]
    )


def test_bis_ccm_core_cipher_vector_and_mutable_header_bits():
    """CCM matches the Core cipher vector with BIS nonce/AAD construction.

    Steps:
        1. Select GIV so its AA-mixed IV matches Core Vol 6, Part C, 1.2.
        2. Encrypt header 0x03, payload 0x06 and counter 0.
        3. Change CSSN/CSTF, then corrupt LLID, payload and counter separately.

    Expected:
        - Ciphertext/MIC is 9FCDA7F448; CSSN/CSTF do not affect verification,
          while authenticated changes raise InvalidTag. Empty padding has no MIC.
    """
    key = bytes.fromhex('99ad1b5226a37e3e058e3b8e27c2c666')[::-1]
    cipher = BisCipher(key, bytes.fromhex('24abdcbabebaafde'), 0)
    encrypted = cipher.encrypt(0x03, b'\x06', 0)
    assert encrypted.hex() == '9fcda7f448'
    assert cipher.decrypt(0x3F, encrypted, 0) == b'\x06'
    for header, data, counter in (
        (0x02, encrypted, 0),
        (3, encrypted[:-1] + b'\0', 0),
        (3, encrypted, 1),
    ):
        with pytest.raises(InvalidTag):
            cipher.decrypt(header, data, counter)
    assert cipher.encrypt(3, b'', 0) == b''
    with pytest.raises(OverflowError):
        cipher.nonce(BIG_COUNTER_MASK + 1)
