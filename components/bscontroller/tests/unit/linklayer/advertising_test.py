# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Fixed advertising wire vectors, reserved fields and offset quantization."""

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_DATA_CHANNEL_MAP_ALL,
    LL_SCA_50_PPM,
    PDU_ADV_ADDRESS_SIZE,
    PDU_ADV_CHSEL_MASK,
    PDU_ADV_EXT_FLAGS_RESERVED_MASK,
    PDU_ADV_EXT_HEADER_OFFSET,
    PDU_ADV_OFFSET_UNIT_300_US,
    PDU_ADV_RESERVED_MASK,
    PDU_ADV_SYNC_INFO_SIZE,
    PDU_ADV_SYNC_OFFSET_RESERVED_MASK,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising_pdu import (
    AuxPtr,
    ExtendedPdu,
    SyncInfo,
    align_offset_us,
)
from bscontroller.radio.base import Phy
from tests.support import (
    AD_CHANNEL_MAP_UPDATE_INDICATION,
    advertising_data,
)
from tests.unit.helpers import PARAMETERS

PEER_ADDRESS = hci.Address('C0:12:34:56:78:9A')


@pytest.mark.parametrize(
    'pointer,wire',
    [
        (AuxPtr(36, 600, Phy.LE_CODED), bytes.fromhex('641440')),
        (
            AuxPtr(36, 600, Phy.LE_2M, False, PDU_ADV_OFFSET_UNIT_300_US),
            bytes.fromhex('a40220'),
        ),
    ],
)
def test_auxptr_wire_fields(pointer, wire):
    """AuxPtr encodes and decodes its channel, offset, PHY and unit fields.

    Purpose: Verify ``AuxPtr.encode``/``decode`` round-trip the AuxPtr field of
    an extended advertising header (Vol 6, Part B, 2.3.4.5), including the offset
    unit and secondary PHY.

    Steps:
        1. For each parametrized AuxPtr and its wire bytes, encode the AuxPtr.
        2. Decode the wire bytes back to an AuxPtr.

    Expected:
        - The encoding equals the expected wire bytes, covering a 30-us-unit
          Coded pointer and a 300-us-unit 2M pointer.
        - Decoding reproduces the original AuxPtr.
    """
    assert pointer.encode() == wire
    assert AuxPtr.decode(wire) == pointer


def test_sync_info_wire_fields():
    """SyncInfo encodes and decodes the full periodic SyncInfo field.

    Purpose: Verify ``SyncInfo.encode``/``decode`` round-trip the SyncInfo
    field carried in periodic advertising (Vol 6, Part B, 2.3.4.6), covering
    offset, interval, channel map, Access Address, CRCInit, event counter and
    SCA.

    Steps:
        1. Build a SyncInfo from the fixed parameters vector.
        2. Encode it and compare to the known wire string.
        3. Decode the wire string back to a SyncInfo.

    Expected:
        - The encoding equals the expected wire bytes.
        - Decoding reproduces the original SyncInfo.
    """
    info = SyncInfo(
        600,
        0x1234,
        0x1FFFFFFFFF,
        PARAMETERS.access_address,
        PARAMETERS.crc_init,
        0xBEEF,
        LL_SCA_50_PPM,
    )
    wire = bytes.fromhex('14003412ffffffffbf7083329a563412efbe')
    assert info.encode() == wire
    assert SyncInfo.decode(wire) == info


@pytest.mark.parametrize('unit', [0, 31])
def test_sync_info_rejects_unencodable_offset_units(unit):
    """SyncInfo encoding rejects an offset that no offset unit can represent.

    Purpose: Verify ``SyncInfo.encode`` raises when the configured offset unit
    cannot encode the offset within the field's range (Vol 6, Part B, 2.3.4.6).

    Steps:
        1. Build a SyncInfo with a 900-us offset and a parametrized unencodable
           offset unit (0 or 31).
        2. Encode it.

    Expected:
        - ``ValueError`` mentioning 'offset unit' is raised in both cases.
    """
    info = SyncInfo(
        900,
        80,
        LL_DATA_CHANNEL_MAP_ALL,
        PARAMETERS.access_address,
        PARAMETERS.crc_init,
        0,
        offset_unit=unit,
    )
    with pytest.raises(ValueError, match='offset unit'):
        info.encode()


@pytest.mark.parametrize('offset', [30, 600, 245_730, 246_000, 2_457_300])
def test_syncinfo_rfu_bit_preserves_offset_units_and_all_other_fields(offset):
    """Setting the SyncInfo offset RFU bit does not disturb decoded fields.

    Purpose: Verify ``SyncInfo.decode`` ignores the reserved bit in the offset
    word and still recovers the original offset unit and every other field, so
    a peer setting RFU does not shift interpretation (Vol 6, Part B, 2.3.4.6).

    Steps:
        1. Build a SyncInfo for the parametrized offset and encode it.
        2. OR the reserved offset mask into the first two octets.
        3. Decode the modified wire bytes.

    Expected:
        - The decoded SyncInfo equals the original for every offset, which spans
          both the 30-us and 300-us unit ranges.
    """
    info = SyncInfo(
        offset,
        80,
        LL_DATA_CHANNEL_MAP_ALL,
        PARAMETERS.access_address,
        PARAMETERS.crc_init,
        0xBEEF,
        LL_SCA_50_PPM,
    )
    wire = bytearray(info.encode())
    wire[:2] = (
        int.from_bytes(wire[:2], 'little') | PDU_ADV_SYNC_OFFSET_RESERVED_MASK
    ).to_bytes(2, 'little')
    assert SyncInfo.decode(wire) == info


@pytest.mark.parametrize('encoding', range(3, 8))
def test_reserved_aux_phy_codes_are_invalid_values_not_ignorable_bits(encoding):
    """Reserved AuxPtr PHY codes are rejected, not silently accepted.

    Purpose: Verify ``AuxPtr.decode`` treats the three reserved secondary-PHY
    encodings as invalid values and raises, rather than ignoring the bits
    (Vol 6, Part B, 2.3.4.5).

    Steps:
        1. Build AuxPtr wire bytes with a valid channel and offset but a
           parametrized reserved PHY code (3..7).
        2. Decode the bytes.

    Expected:
        - ``ValueError`` mentioning 'PHY' is raised for each reserved code.
    """
    # Valid channel12 and offset600us; only the three-bit Aux PHY is reserved.
    wire = bytes([12, 20, encoding << 5])
    with pytest.raises(ValueError, match='PHY'):
        AuxPtr.decode(wire)


def test_extended_header_with_only_rfu_flag_is_empty_not_malformed():
    """An extended header carrying only the RFU flag decodes to an empty PDU.

    Purpose: Verify ``ExtendedPdu.decode`` accepts an extended header whose only
    set flag is reserved and yields a default (empty) PDU, treating RFU as
    benign rather than malformed (Vol 6, Part B, 2.3.4).

    Steps:
        1. Decode a minimal extended header with just the RFU flag set.

    Expected:
        - The result equals a default ``ExtendedPdu()``.
    """
    assert ExtendedPdu.decode(bytes.fromhex('07020180')) == ExtendedPdu()


@pytest.mark.parametrize(
    'fields',
    [
        {'adi': 0x3001},
        {'address': bytes.fromhex('010203040506'), 'adi': 0x3001},
        {'aux_ptr': AuxPtr(12, 600, Phy.LE_2M)},
        {'cte': 0x12, 'target': bytes.fromhex('0708090a0b0c'), 'tx_power': -20},
    ],
)
def test_extended_rfu_flag_does_not_shift_fields_or_consume_data(fields):
    """Reserved header flags leave extended fields and payload in place.

    Purpose: Verify ``ExtendedPdu.decode`` ignores the reserved header flag and
    the ChSel bit without shifting any present field or consuming payload bytes,
    so the decoded PDU matches the original (Vol 6, Part B, 2.3.4).

    Steps:
        1. Build an ExtendedPdu with the parametrized subset of fields and a
           payload, then encode it.
        2. OR the reserved and ChSel masks into the header, and the reserved
           extended-flag bit into the flags octet.
        3. Decode the modified wire bytes.

    Expected:
        - The decoded PDU equals the original across each field combination
          (ADI, address, AuxPtr, and CTE/target/tx_power).
    """
    original = ExtendedPdu(data=b'payload', **fields)
    wire = bytearray(original.encode())
    wire[0] |= PDU_ADV_RESERVED_MASK | PDU_ADV_CHSEL_MASK
    wire[PDU_ADV_EXT_HEADER_OFFSET] |= PDU_ADV_EXT_FLAGS_RESERVED_MASK
    assert ExtendedPdu.decode(wire) == original


def test_extended_header_wire_fields():
    """A fully populated extended header round-trips to its known wire bytes.

    Purpose: Verify ``ExtendedPdu.encode``/``decode`` lay out and recover every
    extended-header field together (address, target, CTE, ADI, AuxPtr,
    SyncInfo, Tx power) plus payload (Vol 6, Part B, 2.3.4).

    Steps:
        1. Build an ExtendedPdu with all optional fields set and a payload.
        2. Encode it and compare to the known wire string.
        3. Decode the wire string back to a PDU.

    Expected:
        - The encoding equals the expected wire bytes plus 'payload'.
        - Decoding reproduces the original PDU.
    """
    pdu = ExtendedPdu(
        data=b'payload',
        address=bytes.fromhex('010203040506'),
        random_address=True,
        target=bytes.fromhex('0708090a0b0c'),
        random_target=True,
        cte=0x12,
        adi=0xABCD,
        aux_ptr=AuxPtr(36, 600, Phy.LE_CODED),
        sync_info=SyncInfo(
            600,
            0x1234,
            0x1FFFFFFFFF,
            PARAMETERS.access_address,
            PARAMETERS.crc_init,
            0xBEEF,
        ),
        tx_power=-5,
    )
    wire = (
        bytes.fromhex(
            'c72e267f0102030405060708090a0b0c12cdab64144014003412ffffffffbf7083329a563412efbefb'
        )
        + b'payload'
    )
    assert pdu.encode() == wire
    assert ExtendedPdu.decode(wire) == pdu


def test_large_unit_auxptr_preserves_wire_unit_at_small_offsets():
    """An AuxPtr received in 300-us units keeps that unit when re-encoded.

    Purpose: Verify ``AuxPtr.decode`` records the received offset unit so a
    re-encode reproduces the same wire bytes, even when the offset is small
    enough to fit a smaller unit (Vol 6, Part B, 2.3.4.5).

    Steps:
        1. Decode an AuxPtr expressing 600 us in 300-us units on 1M.
        2. Inspect its offset and unit, then re-encode.

    Expected:
        - The decoded offset is 600 us with the 300-us unit.
        - Re-encoding reproduces the original raw bytes.
    """
    raw = bytes([7 | 128, 2, 0])  # 600 us in 300-us units, LE 1M.
    pointer = AuxPtr.decode(raw)
    assert pointer.offset_us == 600 and pointer.unit_us == PDU_ADV_OFFSET_UNIT_300_US
    assert pointer.encode() == raw


@pytest.mark.parametrize(
    'offset,expected',
    [(1, 30), (600, 600), (601, 630), (245759, 246000), (245761, 246000)],
)
def test_offset_rounding_and_unit_transition(offset, expected):
    """Offset alignment rounds up and switches units at the field boundary.

    Purpose: Verify ``align_offset_us`` rounds an aux offset up to the next
    representable multiple and crosses from 30-us to 300-us units at the field
    boundary, and that the aligned value round-trips through an AuxPtr (Vol 6,
    Part B, 2.3.4.5).

    Steps:
        1. For each parametrized offset, call ``align_offset_us``.
        2. Encode an AuxPtr at the aligned offset and decode it.

    Expected:
        - The aligned offset equals the expected value, including rounding and
          the unit transition near 246000 us.
        - The decoded AuxPtr offset equals the aligned value.
    """
    assert align_offset_us(offset) == expected
    encoded = AuxPtr(7, expected, Phy.LE_1M).encode()
    assert AuxPtr.decode(encoded).offset_us == expected


def test_auxiliary_connection_response_codec():
    """AUX_CONNECT_RSP encodes its header type and target and round-trips.

    Purpose: Verify ``ExtendedPdu`` encodes an AUX_CONNECT_RSP with the correct
    PDU type and extended-header layout (no AuxPtr) and decodes back to the same
    PDU (Vol 6, Part B, 2.3.4.9).

    Steps:
        1. Build an AUX_CONNECT_RSP with a zero AdvA and a random target.
        2. Encode it and inspect the type and header-flag octets.
        3. Decode the encoding.

    Expected:
        - The first octet is 0x88 and the extended-header mode bits are zero.
        - Decoding reproduces the original PDU.
    """
    packet = ExtendedPdu(
        kind=AdvertisingPduType.AUX_CONNECT_RSP,
        address=bytes(PDU_ADV_ADDRESS_SIZE),
        target=bytes(range(6)),
        random_target=True,
    )
    encoded = packet.encode()
    assert encoded[0] == 0x88 and encoded[2] >> 6 == 0
    assert ExtendedPdu.decode(encoded) == packet


def test_syncinfo_keeps_received_offset_unit_even_when_delay_fits_smaller_units():
    """SyncInfo preserves a received 300-us unit when re-encoding.

    Purpose: Verify ``SyncInfo.decode`` keeps the received offset unit so a
    re-encode reproduces the same bytes, even when the offset would fit the
    smaller 30-us unit (Vol 6, Part B, 2.3.4.6).

    Steps:
        1. Build a SyncInfo with a 900-us offset forced into 300-us units and
           encode it.
        2. Decode the encoding and inspect its unit, then re-encode.

    Expected:
        - The decoded unit is the 300-us unit.
        - Re-encoding reproduces the original wire bytes.
    """
    info = SyncInfo(
        900,
        80,
        LL_DATA_CHANNEL_MAP_ALL,
        PARAMETERS.access_address,
        PARAMETERS.crc_init,
        1,
        offset_unit=PDU_ADV_OFFSET_UNIT_300_US,
    )
    decoded = SyncInfo.decode(info.encode())
    assert decoded.unit_us == PDU_ADV_OFFSET_UNIT_300_US
    assert decoded.encode() == info.encode()


def test_extended_header_preserves_acad_separately_from_advertising_data():
    """ACAD is encoded and recovered separately from the advertising payload.

    Purpose: Verify ``ExtendedPdu`` keeps Additional Controller Advertising Data
    distinct from the advertising data payload across a round trip, with or
    without an AdvA present (Vol 6, Part B, 2.3.4.8).

    Steps:
        1. Build ACAD holding a Channel Map Update Indication.
        2. For a PDU with ACAD plus payload, and a PDU with address plus ACAD,
           encode then decode each.

    Expected:
        - Each decoded PDU equals its original, so ACAD and advertising data are
          not conflated.
    """
    acad = bytes(
        advertising_data(
            [
                (
                    AD_CHANNEL_MAP_UPDATE_INDICATION,
                    b'\x15\x00\x00\x00\x00\x14\x00',
                )
            ]
        )
    )
    for pdu in (
        ExtendedPdu(acad=acad, data=b'payload'),
        ExtendedPdu(address=bytes(PEER_ADDRESS), acad=acad),
    ):
        assert ExtendedPdu.decode(pdu.encode()) == pdu


@pytest.mark.parametrize(
    'data', [b'', bytes(2), bytes([37, 1, 0]), bytes([1, 0, 0]), bytes([1, 1, 0xE0])]
)
def test_invalid_aux_pointer(data):
    """Malformed AuxPtr bytes are rejected.

    Purpose: Verify ``AuxPtr.decode`` raises on byte strings that are too short
    or carry an invalid channel/PHY encoding (Vol 6, Part B, 2.3.4.5).

    Steps:
        1. For each parametrized byte string, decode it as an AuxPtr.

    Expected:
        - ``ValueError`` is raised for every case.
    """
    with pytest.raises(ValueError):
        AuxPtr.decode(data)


@pytest.mark.parametrize(
    'data',
    [
        b'',
        bytes(PDU_ADV_SYNC_INFO_SIZE - 1),
        bytes(PDU_ADV_SYNC_INFO_SIZE),
        b'\x01\x80' + bytes(16),
    ],
)
def test_invalid_sync_info(data):
    """Malformed SyncInfo bytes are rejected.

    Purpose: Verify ``SyncInfo.decode`` raises on wrong-length input or an
    otherwise invalid SyncInfo field (Vol 6, Part B, 2.3.4.6).

    Steps:
        1. For each parametrized byte string, decode it as a SyncInfo.

    Expected:
        - ``ValueError`` is raised for every case, covering empty, one short of
          the field size, an all-zero field and an invalid non-zero field.
    """
    with pytest.raises(ValueError):
        SyncInfo.decode(data)


@pytest.mark.parametrize(
    'data',
    [b'', b'\x07\x01\x02', b'\x07\x02\x01\x10', b'\x07\x01\xc0'],
)
def test_malformed_extended_header(data):
    """Malformed extended headers are rejected.

    Purpose: Verify ``ExtendedPdu.decode`` raises when the extended header
    length or flags are inconsistent with the available bytes (Vol 6, Part B,
    2.3.4).

    Steps:
        1. For each parametrized byte string, decode it as an ExtendedPdu.

    Expected:
        - ``ValueError`` is raised for every case.
    """
    with pytest.raises(ValueError):
        ExtendedPdu.decode(data)
