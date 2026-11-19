# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Ellisys and PCAPNG output for simulated Bluetooth LE packets."""

import struct
from io import BytesIO, StringIO

import pytest

from bscontroller.radio.base import AirPacket, Phy
from bscontroller.radio.capture import PcapngWriter, write_ellisys

_ADV_AA = 0x8E89BED6
_PDU_AND_CRC = bytes.fromhex('01 00 AA BB CC')


def test_ellisys_preserves_legacy_time_order_and_payload():
    """Check the legacy text header, ordering, PHY metadata, and packet bytes.

    Purpose: Keep output compatible with traces converted from BabbleSim TX CSV.
    Steps:
        1. Supply unsorted records with a time tie, duplicates, and Coded PHY.
        2. Serialize them to a text stream.
    Expected:
        - Output uses nanoseconds, physical RF channels and uppercase hex.
        - Tied records sort by their full text and duplicates are preserved.
    """
    repeated = AirPacket(100, 37, _ADV_AA, Phy.LE_1M, 0, _PDU_AND_CRC)
    packets = [
        AirPacket(100, 38, 0xBBBBBBBB, Phy.LE_2M, 0, _PDU_AND_CRC),
        repeated,
        AirPacket(100, 39, 0x1234, Phy.LE_CODED, 2, _PDU_AND_CRC),
        AirPacket(100, 11, 0x1234, Phy.LE_CODED, 8, _PDU_AND_CRC),
        repeated,
        AirPacket(2, 0, 0x1234, Phy.LE_1M, 0, _PDU_AND_CRC),
    ]
    output = StringIO()

    write_ellisys(packets, output)

    assert output.getvalue() == (
        'FileFormat:Bluetooth\n                version=1.0\n\n'
        'ItemFormat:LE version=1.1\n'
        'Item time=2000 aa=0x00001234 rssi=-40 rfchannel=1 '
        'phy=1Mbps rawdata="01 00 AA BB CC"\n'
        'Item time=100000 aa=0x00001234 rssi=-40 rfchannel=13 '
        'phy=Coded coding=Coded8 rawdata="01 00 AA BB CC"\n'
        'Item time=100000 aa=0x00001234 rssi=-40 rfchannel=39 '
        'phy=Coded coding=Coded2 rawdata="01 00 AA BB CC"\n'
        + 'Item time=100000 aa=0x8E89BED6 rssi=-40 rfchannel=0 '
        'phy=1Mbps rawdata="01 00 AA BB CC"\n'
        * 2
        + 'Item time=100000 aa=0xBBBBBBBB rssi=-40 rfchannel=12 '
        'phy=2Mbps rawdata="01 00 AA BB CC"\n'
    )


def _read_block(stream: BytesIO, block_type: int) -> bytes:
    kind, length = struct.unpack('<II', stream.read(8))
    assert kind == block_type
    assert length >= 12 and length % 4 == 0
    body = stream.read(length - 12)
    assert stream.read(4) == struct.pack('<I', length)
    return body


@pytest.mark.parametrize(
    ('phy', 'coding', 'rssi', 'crc_valid', 'flags', 'ci', 'signal_dbm', 'direction'),
    [
        (Phy.LE_1M, 0, None, None, 0x0001, b'', 0, 2),
        (Phy.LE_2M, 0, -50.6, False, 0x4403, b'', -51, 1),
        (Phy.LE_CODED, 8, -129.2, True, 0x8C03, b'\x00', -128, 1),
        (Phy.LE_CODED, 2, None, True, 0x8C01, b'\x01', 0, 2),
    ],
)
def test_pcapng_writes_ble_pseudo_header_and_direction(
    phy, coding, rssi, crc_valid, flags, ci, signal_dbm, direction
):
    """Check the PCAPNG block headers and per-packet LE radio metadata.

    Purpose: Preserve the selected PHY and the receiver's frame observations.
    Steps:
        1. Write a packet at a timestamp exceeding the low 32 bits.
        2. Decode the section, interface and packet blocks from the stream.
    Expected:
        - The interface uses link type 256 with microsecond timestamps.
        - Each packet has a PHY and direction, with optional RSSI and CRC
          flags and an extra coding indicator octet for LE Coded.
    """
    output = BytesIO()
    writer = PcapngWriter(output)
    writer.write(
        AirPacket(2**32 + 42, 38, _ADV_AA, phy, coding, _PDU_AND_CRC, rssi, crc_valid)
    )
    output.seek(0)

    assert struct.unpack('<IHHq', _read_block(output, 0x0A0D0D0A)) == (
        0x1A2B3C4D,
        1,
        0,
        -1,
    )
    assert _read_block(output, 1) == struct.pack(
        '<HHIHHB3xHH', 256, 0, 512, 9, 1, 6, 0, 0
    )
    block = _read_block(output, 6)
    length = 14 + len(ci) + len(_PDU_AND_CRC)
    assert struct.unpack_from('<IIIII', block) == (0, 1, 42, length, length)
    assert struct.unpack_from('<BbbBIHI', block, 20) == (
        12,
        signal_dbm,
        0,
        0,
        0,
        flags,
        _ADV_AA,
    )
    assert block[34 : 20 + length] == ci + _PDU_AND_CRC
    padding = (4 - length % 4) % 4
    assert block[20 + length : 20 + length + padding] == bytes(padding)
    assert struct.unpack_from('<HHIHH', block, 20 + length + padding) == (
        2,
        4,
        direction,
        0,
        0,
    )
    assert output.read() == b''
