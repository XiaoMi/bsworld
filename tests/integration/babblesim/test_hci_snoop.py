# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Validate Controller-owned BTSnoop files against real HCI traffic."""

from __future__ import annotations

import asyncio
import struct

import pytest

from bsworld.devices import BSController, ZephyrController

_FILE_HEADER = bytes.fromhex("6274736e6f6f700000000001000003ea")
_RECORD = struct.Struct(">IIIIQ")
_FLAG_COMMAND_EVENT = 0x02


@pytest.mark.parametrize(
    "controller_type", [ZephyrController, BSController], ids=["zephyr", "python"]
)
@pytest.mark.parametrize(
    "custom_path", [False, True], ids=["default-path", "application-path"]
)
async def test_snoop_matches_live_h4_traffic_without_host_relay(
    topo, tmp_path, controller_type, custom_path
):
    ctl = topo.add_controller(
        controller_type=controller_type, public_address="F0:F1:F2:F3:F4:01"
    )
    if custom_path:
        ctl.hci_snoop_file = tmp_path / "application" / "hci0.snoop"
    await topo.start()
    reader, writer = await asyncio.open_unix_connection(ctl.hci_sock)
    packets = []
    try:
        async with asyncio.timeout(5):
            for command, response in (
                ("01030c00", "040e0401030c00"),
                ("01091000", "040e0a0109100001f4f3f2f1f0"),
            ):
                packet = bytes.fromhex(command)
                # Fragmented socket writes must produce one captured H4 packet.
                writer.write(packet[:2])
                await writer.drain()
                await asyncio.sleep(0)
                writer.write(packet[2:])
                await writer.drain()
                header = await reader.readexactly(3)
                event = header + await reader.readexactly(header[2])
                assert event == bytes.fromhex(response)
                packets.extend([packet, event])
        # No Python capture reader or relay is running. Every record must be
        # present on disk before the Controller exits.
        before_stop = ctl.hci_snoop_file.read_bytes()
        assert not list(ctl.socket_dir.glob("*.btsnoop.sock"))
    finally:
        writer.close()
        await writer.wait_closed()
        await topo.stop()
    assert ctl.hci_snoop_file.read_bytes() == before_stop
    assert before_stop[:16] == _FILE_HEADER
    offset = 16
    # BSController records only the direction bit.
    command_event_flag = (
        _FLAG_COMMAND_EVENT if controller_type is ZephyrController else 0
    )
    for index, packet in enumerate(packets):
        length, included, flags, drops, _ = _RECORD.unpack_from(before_stop, offset)
        offset += _RECORD.size
        assert length == included == len(packet)
        # Bit 0: Controller to Host; bit 1: every packet is a command or event.
        assert flags == (index % 2) | command_event_flag
        assert drops == 0
        assert before_stop[offset : offset + included] == packet
        offset += included
    assert offset == len(before_stop)
