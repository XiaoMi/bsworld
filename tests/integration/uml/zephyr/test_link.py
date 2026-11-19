# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Exercise Link Layer control procedures with Zephyr and Bumble."""

import asyncio
import struct

from bumble import hci
from tests.integration.uml.zephyr.gatt import verify_notification
from tests.integration.uml.zephyr.peer import TIMEOUT, connect, disconnect


async def test_shell_link_layer_updates(peer):
    connection = await connect(peer)
    for mask, phy in ((2, "LE 2M"), (4, "LE Coded"), (1, "LE 1M")):
        await peer.shell.shell.run(f"bt phy-update {mask}")
        await peer.shell.wait_log(rf"LE PHY updated: TX PHY {phy}, RX PHY {phy}")
    await peer.shell.shell.run("bt data-len-update 251")
    await peer.shell.wait_log(r"LE data len updated: TX \(len: 251 ")
    await peer.shell.shell.run("bt data-len-update 27")
    await peer.shell.wait_log(r"LE data len updated: TX \(len: 27 ")
    await connection.set_data_length(200, 1704)
    await peer.shell.wait_log(r"LE data len updated: .*RX \(len: 200 ")
    await connection.update_parameters(40, 40, 0, 4000)
    await peer.shell.wait_log(r"LE conn param updated: int 0x0020 lat 0 to 400")

    updated = asyncio.Event()
    connection.once(connection.EVENT_CONNECTION_PARAMETERS_UPDATE, updated.set)
    await peer.shell.shell.run("bt conn-update 0030 0030 1 0190")
    await peer.shell.wait_log(r"LE conn param updated: int 0x0030 lat 1 to 400")
    await asyncio.wait_for(updated.wait(), TIMEOUT)
    assert connection.parameters.connection_interval == 60
    assert connection.parameters.peripheral_latency == 1
    assert connection.parameters.supervision_timeout == 4000
    await disconnect(peer, connection)


async def _read_channel_map(bumble, connection) -> bytes:
    # Bumble exposes the opcode without a typed command for LE Read Channel Map.
    complete = await bumble.send_sync_command_raw(
        hci.HCI_Command(
            struct.pack("<H", connection.handle),
            op_code=hci.HCI_LE_READ_CHANNEL_MAP_COMMAND,
        )
    )
    status, handle, channel_map = struct.unpack(
        "<BH5s", complete.return_parameters.data
    )
    assert status == hci.HCI_ErrorCode.SUCCESS
    assert handle == connection.handle
    return channel_map


async def test_shell_updates_channel_map_and_preserves_data(peer):
    await peer.shell.shell.run("gatt register")
    await peer.shell.wait_log("Registered test vendor service")
    connection = await connect(peer, shell_central=True)
    all_channels = bytes.fromhex("ffffffff1f")
    assert await _read_channel_map(peer.bumble, connection) == all_channels
    await peer.shell.shell.run("bt channel-map 0000ff00ff")
    await peer.shell.wait_log("Channel map set")
    expected_map = bytes.fromhex("ff00ff0000")
    async with asyncio.timeout(TIMEOUT):
        while await _read_channel_map(peer.bumble, connection) != expected_map:
            await asyncio.sleep(0.020)
    await verify_notification(peer.shell, connection, bytes(range(200)))
    await disconnect(peer, connection)
