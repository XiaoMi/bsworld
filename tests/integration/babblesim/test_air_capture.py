# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Check air capture artifacts from a topology with PHY dumps disabled."""

import asyncio
import struct
from pathlib import Path


async def _command(reader, writer, opcode, params):
    writer.write(struct.pack("<BHB", 1, opcode, len(params)) + params)
    await writer.drain()
    while True:
        packet_type, event, size = await reader.readexactly(3)
        assert packet_type == 4
        data = await reader.readexactly(size)
        if event == 0x0E and data[1:3] == struct.pack("<H", opcode):
            assert data[3] == 0
            return


def _read_packets(path: Path, direction: int) -> list[tuple[int, bytes]]:
    packets = []
    with path.open("rb") as capture:
        while prefix := capture.read(8):
            kind, length = struct.unpack("<II", prefix)
            data = capture.read(length - 12)
            assert struct.unpack("<I", capture.read(4)) == (length,)
            if kind == 1:
                assert struct.unpack_from("<H", data) == (256,)
            if kind != 6:
                continue
            _, high, low, size, wire_size = struct.unpack_from("<IIIII", data)
            assert size == wire_size
            options = 20 + ((size + 3) & ~3)
            assert struct.unpack_from("<HHI", data, options) == (2, 4, direction)
            frame = data[20 : 20 + size]
            assert frame[0] == 0  # Advertising channel 37 is at 2402 MHz.
            assert frame[10:14] == bytes.fromhex("d6be898e")
            packets.append(((high << 32) | low, frame[14:]))
    return packets


async def test_air_captures_match_between_advertiser_and_scanner(topo):
    advertiser = topo.add_controller(public_address="F0:F1:F2:F3:F4:01")
    scanner = topo.add_controller(log_subdir="observer")
    await topo.start()
    adv_reader, adv_writer = await asyncio.open_unix_connection(advertiser.hci_sock)
    scan_reader, scan_writer = await asyncio.open_unix_connection(scanner.hci_sock)
    try:
        async with asyncio.timeout(1):
            await _command(
                scan_reader, scan_writer, 0x200B, struct.pack("<BHHBB", 0, 16, 16, 0, 0)
            )
            await _command(scan_reader, scan_writer, 0x200C, b"\x01\x00")
            params = struct.pack("<HHBBB6sBB", 160, 160, 3, 0, 0, bytes(6), 1, 0)
            await _command(adv_reader, adv_writer, 0x2006, params)
            await _command(adv_reader, adv_writer, 0x200A, b"\x01")
            await asyncio.sleep(0.35)
        results = topo.env.bsim_dir / "results" / topo.sim_id
        assert not list(results.glob("*.csv"))
    finally:
        adv_writer.close()
        scan_writer.close()
        await asyncio.gather(adv_writer.wait_closed(), scan_writer.wait_closed())
        await topo.stop()

    log = topo.test_log_dir
    transmitted = _read_packets(log / "controllers" / "hci0" / "radio0.pcapng", 2)
    received = _read_packets(log / "observer" / "radio1.pcapng", 1)
    assert received and set(received) <= set(transmitted)
    trace = log / "babblesim" / "ellisys.bttrp"
    assert trace.read_text().splitlines()[4:] == [
        f'Item time={time * 1000} aa=0x8E89BED6 rssi=-40 rfchannel=0 '
        f'phy=1Mbps rawdata="{packet.hex(" ").upper()}"'
        for time, packet in transmitted
    ]
