# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""HCI ISO framing, RFU handling and two-bit status (Vol 4, Part E, 5.4.5)."""

import pytest

from bscontroller.hci import packets as hci
from bscontroller.hci.transport import H4Parser


@pytest.mark.parametrize('status', [0, 1, 2])
def test_iso_status_and_reserved_bits_at_h4_boundary(status):
    """Packet status uses bits 14–15; reserved fields do not alter length.

    Steps:
        1. Construct a complete ISO SDU with the chosen status.
        2. Set all RFU bits in the handle, length and SDU-length words.
        3. Parse the frame byte by byte through H4, then decode its SDU.

    Expected:
        - Exactly one frame arrives; handle, data and two-bit status survive.
    """
    packet = hci.HCI_IsoDataPacket(
        1,
        7,
        b'abc',
        2,
        packet_sequence_number=42,
        iso_sdu_length=3,
        packet_status_flag=status,
    )
    raw = bytearray(bytes(packet))
    assert raw[8] == status << 6
    raw[2] |= 0x80
    raw[4] |= 0xC0
    raw[8] |= 0x30
    received = []

    class Sink:
        def on_packet(self, data):
            received.append(data)

    parser = H4Parser(Sink())
    for byte in raw:
        parser.feed_data(bytes([byte]))
    assert len(received) == 1
    decoded = hci.HCI_Packet.from_bytes(received[0])
    assert decoded.connection_handle == 1
    assert decoded.data_total_length == 7
    assert decoded.iso_sdu_length == 3
    assert decoded.packet_sequence_number == 42
    assert decoded.packet_status_flag == status
    assert decoded.iso_sdu_fragment == b'abc'


def test_iso_timestamp_and_truncated_length():
    """A timestamp remains distinct from SDU info; truncated frames fail.

    Steps:
        1. Serialize a timestamped first fragment and parse its fields.
        2. Remove a byte without changing Data_Total_Length.

    Expected:
        - The timestamp is preserved and truncation raises InvalidPacketError.
    """
    packet = hci.HCI_IsoDataPacket(
        3,
        11,
        b'abc',
        0,
        time_stamp=0x12345678,
        packet_sequence_number=2,
        iso_sdu_length=8,
        packet_status_flag=0,
    )
    assert hci.HCI_IsoDataPacket.from_bytes(bytes(packet)) == packet
    with pytest.raises(hci.InvalidPacketError):
        hci.HCI_IsoDataPacket.from_bytes(bytes(packet)[:-1])


def test_read_iso_tx_sync_offset_has_three_octets():
    """Read ISO TX Sync uses a 24-bit Time_Offset (Vol 4, Part E, 7.8.96).

    Steps:
        1. Serialize fixed return values and compare the entire wire result.

    Expected:
        - Time_Offset occupies exactly three octets following Time_Stamp.
    """
    result = hci.HCI_LE_Read_ISO_TX_Sync_ReturnParameters(0, 1, 2, 0x12345678, 0xABCDEF)
    assert bytes(result).hex() == '000100020078563412efcdab'


def test_set_cig_test_command_decodes_interleaved_records():
    """Both CIG Test stream records follow Vol 4, Part E, 7.8.98."""
    wire = bytes.fromhex(
        '01 63 20 2b'
        ' 01 204e00 102700 02 01 1000 00 01 00 02'
        ' 02 03 2000 0a00 2100 1000 01 02 01 02'
        ' 04 02 1e00 1600 2800 1200 02 04 02 01'
    )
    command = hci.HCI_Packet.from_bytes(wire)
    assert isinstance(command, hci.HCI_LE_Set_CIG_Parameters_Test_Command)
    assert command.sdu_interval_c_to_p == 20_000
    assert command.sdu_interval_p_to_c == 10_000
    assert (command.ft_c_to_p, command.ft_p_to_c, command.iso_interval) == (2, 1, 16)
    assert command.cis_id == [2, 4]
    assert command.nse == [3, 2]
    assert command.max_sdu_c_to_p == [32, 30]
    assert command.max_pdu_p_to_c == [16, 18]
    assert command.phy_p_to_c == [2, 4]
    assert command.bn_c_to_p == [1, 2]
    assert bytes(command) == wire


@pytest.mark.parametrize(
    ('event_type', 'subevent', 'extension'),
    [
        (hci.HCI_LE_CIS_Established_Event, 0x19, ()),
        (
            hci.HCI_LE_CIS_Established_V2_Event,
            0x2A,
            (400, 42, 32, 10_000, 20_000, 1),
        ),
    ],
)
def test_cis_established_event_round_trip(event_type, subevent, extension):
    """Each LE CIS Established version has its own HCI subevent code."""
    event = event_type(
        0, 3, 500, 500, 10500, 10500, 1, 2, 1, 1, 1, 1, 1, 42, 32, 8, *extension
    )
    wire = bytes(event)
    assert wire[:4] == bytes((0x04, 0x3E, 43 if extension else 29, subevent))
    assert hci.HCI_Packet.from_bytes(wire) == event
