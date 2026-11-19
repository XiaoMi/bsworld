# Copyright 2018 Oticon A/S
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Serialize simulated LE packets for Ellisys and PCAPNG."""

import struct
from collections.abc import Iterable
from typing import BinaryIO, TextIO

from bscontroller.radio.base import (
    PHY_CODED_S2_FACTOR,
    PHY_CODED_S8_FACTOR,
    PHY_DATA_CHANNEL_SPACING_MHZ,
    AirPacket,
    Phy,
    channel_frequency_mhz,
)

_ELLISYS_HEADER = (
    'FileFormat:Bluetooth\n                version=1.0\n\nItemFormat:LE version=1.1\n'
)
_ELLISYS_PHY = {Phy.LE_1M: '1Mbps', Phy.LE_2M: '2Mbps'}
_MICROSECONDS_TO_NANOSECONDS = 1000

# PCAPNG block types and LE link-layer capture format with a radio pseudo-header.
_SECTION_HEADER_BLOCK = 0x0A0D0D0A
_INTERFACE_DESCRIPTION_BLOCK = 1
_ENHANCED_PACKET_BLOCK = 6
_BLUETOOTH_LE_LL_WITH_PHDR = 256
# Includes the radio header, access address, Coded CI and complete LL packet.
_SNAPSHOT_LENGTH = 512

# LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR flag bits.
_DEWHITENED = 0x0001
_RSSI_VALID = 0x0002
_CRC_CHECKED = 0x0400
_CRC_VALID = 0x0800
_PHY_FLAGS = {Phy.LE_1M: 0, Phy.LE_2M: 0x4000, Phy.LE_CODED: 0x8000}
_CODED_INDICATOR = {PHY_CODED_S8_FACTOR: b'\x00', PHY_CODED_S2_FACTOR: b'\x01'}


def _rf_channel(channel: int) -> int:
    return (channel_frequency_mhz(channel) - 2402) // PHY_DATA_CHANNEL_SPACING_MHZ


def _ellisys_line(packet: AirPacket) -> str:
    if packet.phy == Phy.LE_CODED:
        phy = f'Coded coding=Coded{packet.coding}'
    else:
        phy = _ELLISYS_PHY[packet.phy]
    timestamp = packet.start_us * _MICROSECONDS_TO_NANOSECONDS
    data = packet.packet.hex(' ').upper()
    return (
        f'Item time={timestamp} aa=0x{packet.access_address:08X} rssi=-40 '
        f'rfchannel={_rf_channel(packet.channel)} phy={phy} rawdata="{data}"\n'
    )


def write_ellisys(packets: Iterable[AirPacket], output: TextIO) -> None:
    """Write a merged TX trace with the historical Ellisys import ordering."""
    records = [(packet.start_us, _ellisys_line(packet)) for packet in packets]
    records.sort()
    output.write(_ELLISYS_HEADER)
    for _, record in records:
        output.write(record)


class PcapngWriter:
    """Write a single-interface BLE capture to a caller-owned binary stream."""

    def __init__(self, output: BinaryIO) -> None:
        self._output = output
        self._write_block(
            _SECTION_HEADER_BLOCK, struct.pack('<IHHq', 0x1A2B3C4D, 1, 0, -1)
        )
        self._write_block(
            _INTERFACE_DESCRIPTION_BLOCK,
            struct.pack('<HHI', _BLUETOOTH_LE_LL_WITH_PHDR, 0, _SNAPSHOT_LENGTH)
            # if_tsresol: one byte with decimal microsecond resolution.
            + struct.pack('<HHB3xHH', 9, 1, 6, 0, 0),
        )

    def write(self, packet: AirPacket) -> None:
        """Append one packet in capture order with its simulation timestamp."""
        flags = _DEWHITENED | _PHY_FLAGS[packet.phy]
        rssi = 0
        if packet.rssi is not None:
            flags |= _RSSI_VALID
            rssi = max(-128, min(127, round(packet.rssi)))
        if packet.crc_valid is not None:
            flags |= _CRC_CHECKED
            if packet.crc_valid:
                flags |= _CRC_VALID

        # Radio pseudo-header, access address, optional Coded CI, and LL bytes.
        payload = struct.pack(
            '<BbbBIHI',
            _rf_channel(packet.channel),
            rssi,
            0,  # Noise power.
            0,  # Access address offenses.
            0,  # Reference access address.
            flags,
            packet.access_address,
        )
        if packet.phy == Phy.LE_CODED:
            payload += _CODED_INDICATOR[packet.coding]
        payload += packet.packet

        timestamp_high, timestamp_low = divmod(packet.start_us, 1 << 32)
        length = len(payload)
        header = struct.pack('<IIIII', 0, timestamp_high, timestamp_low, length, length)
        received = packet.rssi is not None
        # epb_flags: one 32-bit direction word (inbound=1, outbound=2).
        options = struct.pack('<HHIHH', 2, 4, 1 if received else 2, 0, 0)
        self._write_block(
            _ENHANCED_PACKET_BLOCK, header + payload + bytes(-length % 4) + options
        )

    def _write_block(self, block_type: int, body: bytes) -> None:
        length = len(body) + 12
        self._output.write(
            struct.pack('<II', block_type, length) + body + struct.pack('<I', length)
        )
