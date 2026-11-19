# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Discover, read and subscribe to attributes over GATT."""

from bumble.gatt import Characteristic, Service
from tests.integration.uml.zephyr.gatt import verify_notification
from tests.integration.uml.zephyr.peer import connect, disconnect

_PAYLOAD = bytes(range(200))


async def test_shell_server_encrypted_notifications(peer):
    await peer.shell.shell.run("gatt register")
    await peer.shell.wait_log("Registered test vendor service")
    connection = await connect(peer)
    await connection.pair()
    await peer.shell.wait_log(r"Security changed: .* level 2")
    await verify_notification(peer.shell, connection, _PAYLOAD)
    await disconnect(peer, connection)


async def test_shell_client_reads_bumble_server(peer):
    characteristic = Characteristic(
        "2A19",
        Characteristic.Properties.READ,
        Characteristic.Permissions.READABLE,
        b"\x64",
    )
    peer.bumble.add_service(Service("180F", [characteristic]))
    connection = await connect(peer, shell_central=True)
    await peer.shell.shell.run("gatt discover")
    await peer.shell.wait_log("Discover complete")
    await peer.shell.shell.run(f"gatt read {characteristic.handle:x}")
    await peer.shell.wait_log("Read complete: err 0x00 length 1")
    await peer.shell.wait_log(r"00000000:\s+64\b")
    await disconnect(peer, connection)
