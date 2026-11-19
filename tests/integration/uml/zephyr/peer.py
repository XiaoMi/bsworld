# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connection helpers for Zephyr and Bumble integration tests."""

import asyncio

from bumble import hci
from bumble.device import Connection
from tests.integration.uml.bumble_peer import advertising, advertising_data
from tests.integration.uml.zephyr.shell import shell_identity

TIMEOUT = 30
BUMBLE_ADDRESS = "P:F0:F1:F2:F3:F4:01"


def bumble_address(shell_address: str) -> hci.Address:
    """Convert a shell "P:" or "R:" address into a typed Bumble address."""
    kind = (
        hci.AddressType.RANDOM_DEVICE
        if shell_address.startswith("R:")
        else hci.AddressType.PUBLIC_DEVICE
    )
    return hci.Address(shell_address[2:], kind)


async def connect(peer, *, shell_central=False):
    shell, bumble = peer.shell, peer.bumble
    if shell_central:
        connected = asyncio.get_running_loop().create_future()
        bumble.once("connection", connected.set_result)
        await peer.stack.enter_async_context(
            advertising(bumble, connectable=True, data=advertising_data("Bumble peer"))
        )
        await shell.shell.run(f"bt connect {BUMBLE_ADDRESS}")
        connection = await asyncio.wait_for(connected, TIMEOUT)
    else:
        address = await shell_identity(shell)
        await shell.shell.run("bt advertise on identity")
        await shell.wait_log("Advertising started")
        connection = await bumble.connect(
            bumble_address(address),
            own_address_type=hci.OwnAddressType.PUBLIC,
            timeout=TIMEOUT,
        )
    await shell.wait_log("Connected: ")
    return connection


async def disconnect(peer, connection):
    disconnected = asyncio.get_running_loop().create_future()
    connection.once(Connection.EVENT_DISCONNECTION, disconnected.set_result)
    await peer.shell.shell.run("bt disconnect")
    await peer.shell.wait_log(r"Disconnected: .*reason 0x16")
    assert await asyncio.wait_for(disconnected, TIMEOUT) == 0x13
