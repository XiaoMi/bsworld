# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""BTSnoop wire format and capture at Controller-local HCI boundaries."""

import asyncio
import struct
from types import SimpleNamespace

import pytest

from bscontroller.hci.backend import RadioBackend
from bscontroller.hci.controller import Controller
from bscontroller.hci.snoop import BtsnoopWriter

HEADER = bytes.fromhex('6274736e6f6f700000000001000003ea')
EPOCH = 0x00DCDDB30F2F8000


def test_empty_capture_and_completed_records_are_visible_before_close(tmp_path):
    """A BTSnoop capture writes the header, then one record per packet as it goes.

    Purpose: Verify ``BtsnoopWriter`` creates parent directories, writes the
    fixed BTSnoop header immediately, and appends each captured packet as a
    big-endian record (including the H4 type and a simulation-time timestamp)
    that is visible before close. Commands and events set flag bit 1.

    Steps:
        1. Open a writer at a nested path with a settable ``now_us`` clock.
        2. Capture a sent Reset command at 11000 us and read the file.
        3. Advance the clock and capture a received Command Complete event.

    Expected:
        - The file holds only the header before any capture.
        - After each capture the file equals the header plus the expected
          big-endian records, and the content persists after close.
    """
    path = tmp_path / 'nested' / 'hci0.snoop'
    now_us = 11000
    with BtsnoopWriter(path, lambda: now_us) as snoop:
        assert path.read_bytes() == HEADER
        command = bytes.fromhex('01030c00')
        snoop.capture(command, received=False)
        # Big-endian record including the H4 type; flags: bit 0 Controller to
        # Host, bit 1 command or event.
        expected = HEADER + bytes.fromhex(
            '00000004 00000004 00000002 00000000 00dcddb30f2faaf8 01030c00'
        )
        assert path.read_bytes() == expected
        now_us = 12345
        event = bytes.fromhex('040e0401030c00')
        snoop.capture(event, received=True)
        expected += struct.pack('>IIIIQ', 7, 7, 3, 0, EPOCH + now_us) + event
        assert path.read_bytes() == expected
    assert path.read_bytes() == expected


@pytest.mark.parametrize('received', [False, True])
@pytest.mark.parametrize(
    'packet',
    [
        bytes.fromhex('0201000300616263'),  # ACL
        bytes.fromhex('03010003616263'),  # SCO
        bytes.fromhex('0501000300616263'),  # ISO
        b'\x02\x01\x00\x00\x10' + bytes(4096),
    ],
)
def test_capture_preserves_complete_h4_packets(tmp_path, received, packet):
    """Capture preserves each complete H4 packet verbatim with correct metadata.

    Purpose: Verify ``BtsnoopWriter.capture`` records the full H4 packet bytes
    and a record whose lengths, direction flag and timestamp are correct for
    ACL, SCO, ISO and a large packet, in both directions.

    Steps:
        1. Open a writer with a fixed ``now_us``.
        2. Capture the parametrized packet with the parametrized direction.

    Expected:
        - The file equals the header plus a big-endian record carrying the
          packet length twice, the direction flag with the command/event flag
          clear, and EPOCH + now_us, followed
          by the packet bytes.
    """
    path = tmp_path / 'hci.snoop'
    with BtsnoopWriter(path, lambda: 9876543210) as snoop:
        snoop.capture(packet, received=received)
        assert path.read_bytes() == (
            HEADER
            + struct.pack(
                '>IIIIQ', len(packet), len(packet), int(received), 0, EPOCH + 9876543210
            )
            + packet
        )


def test_partial_file_writes_are_completed_and_write_failures_propagate(
    tmp_path, monkeypatch
):
    """Short writes are retried to completion and a stalled write raises.

    Purpose: Verify ``BtsnoopWriter.capture`` loops until the whole record is
    written when the underlying write is short, and raises when the write makes
    no progress at all, closing the file on context exit.

    Steps:
        1. Open a writer and patch the file's ``write`` to accept only three
           bytes at a time, then capture a Reset command.
        2. Patch ``write`` to accept nothing, then capture again.

    Expected:
        - After the short-write capture the file holds the complete header,
          record and packet.
        - The stalled capture raises ``OSError`` mentioning 'no progress'; the
          file is closed afterwards.
    """
    with BtsnoopWriter(tmp_path / 'hci.snoop', lambda: 0) as snoop:
        original = snoop._file.write
        monkeypatch.setattr(snoop._file, 'write', lambda data: original(data[:3]))
        packet = bytes.fromhex('01030c00')
        snoop.capture(packet, received=False)
        assert (tmp_path / 'hci.snoop').read_bytes() == HEADER + struct.pack(
            '>IIIIQ', 4, 4, 2, 0, EPOCH
        ) + packet
        monkeypatch.setattr(snoop._file, 'write', lambda data: 0)
        with pytest.raises(OSError, match='no progress'):
            snoop.capture(packet, received=False)
    assert snoop._file.closed


@pytest.mark.asyncio
async def test_capture_precedes_host_loop_delivery_and_does_not_advance_time(tmp_path):
    """Capture happens at the Controller boundary, before Host delivery and clock.

    Purpose: Verify that when a ``Controller`` with a snoop writer handles a
    command, both the command and its event are captured at the Controller's
    simulation time before the event is delivered to the Host loop, and that
    capture does not advance the radio clock.

    Steps:
        1. Build a Controller over a radio stub at 123 us with a snoop writer
           and a recording Host sink.
        2. Feed a Reset command and read the file before yielding to the loop.
        3. Advance the radio clock and yield once so the Host loop delivers.

    Expected:
        - The clock stays at 123 and nothing is delivered until the loop runs.
        - The file holds both records timestamped at 123 us; after the loop
          turn the Host has received the Command Complete event.
    """
    radio = SimpleNamespace(now_us=123)
    delivered = []
    path = tmp_path / 'hci.snoop'
    with BtsnoopWriter(path, lambda: radio.now_us) as snoop:
        controller = Controller(
            'test',
            backend=RadioBackend(radio),
            host_sink=SimpleNamespace(on_packet=delivered.append),
            snoop=snoop,
        )
        controller.on_packet(bytes.fromhex('01030c00'))
        assert radio.now_us == 123
        assert not delivered
        expected = (
            HEADER
            + struct.pack('>IIIIQ', 4, 4, 2, 0, EPOCH + 123)
            + bytes.fromhex('01030c00')
            + struct.pack('>IIIIQ', 7, 7, 3, 0, EPOCH + 123)
            + bytes.fromhex('040e0401030c00')
        )
        assert path.read_bytes() == expected
        radio.now_us = 10000
        await asyncio.sleep(0)
        assert delivered == [bytes.fromhex('040e0401030c00')]
        assert path.read_bytes() == expected
