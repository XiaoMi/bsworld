# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Interoperate with a Zephyr observer or advertiser."""

import asyncio

import pytest
from bumble import hci
from bumble.core import AdvertisingData
from bumble.device import ConnectionParametersPreferences
from tests.integration.uml.bumble_peer import advertising, advertising_data
from tests.integration.uml.zephyr.peer import (
    BUMBLE_ADDRESS,
    TIMEOUT,
    bumble_address,
    disconnect,
)
from tests.integration.uml.zephyr.shell import shell_identity


@pytest.mark.parametrize("active", [False, True], ids=["passive", "active"])
async def test_shell_scans_bumble(peer, active):
    await peer.stack.enter_async_context(
        advertising(
            peer.bumble,
            connectable=active,
            data=advertising_data("Bumble peer") if not active else b"\x02\x01\x06",
            scan_response_data=(
                advertising_data("Bumble peer") if active else b"\x03\xffBS"
            ),
        )
    )
    await peer.shell.shell.run("bt scan on" if active else "bt scan passive")
    event, flags = (4, "C:1 S:1 D:0 SR:1") if active else (2, "C:0 S:1 D:0 SR:0")
    await peer.shell.wait_log(
        rf"\[DEVICE\]: {BUMBLE_ADDRESS}, AD evt type {event},.*Bumble peer {flags}"
    )
    await peer.shell.shell.run("bt scan off")
    await peer.shell.wait_log("Scan successfully stopped")


async def test_bumble_scans_shell(peer):
    seen = asyncio.get_running_loop().create_future()

    def advertisement(report):
        if (
            not seen.done()
            and report.data.get(AdvertisingData.Type.COMPLETE_LOCAL_NAME)
            == "BSWorld shell"
        ):
            seen.set_result(report)

    peer.bumble.on("advertisement", advertisement)
    try:
        await peer.shell.shell.run("bt advertise on identity")
        await peer.shell.wait_log("Advertising started")
        await peer.bumble.start_scanning(active=True)
        report = await asyncio.wait_for(seen, TIMEOUT)
        assert report.is_connectable
    finally:
        await peer.bumble.stop_scanning()
        peer.bumble.remove_listener("advertisement", advertisement)


async def test_shell_filter_accept_list_connects_bumble(peer):
    connected = asyncio.get_running_loop().create_future()
    peer.bumble.once("connection", connected.set_result)
    await peer.stack.enter_async_context(
        advertising(peer.bumble, connectable=True, data=advertising_data("Bumble peer"))
    )
    await peer.shell.shell.run(f"bt fal-add {BUMBLE_ADDRESS}")
    await peer.shell.shell.run("bt fal-connect on")
    connection = await asyncio.wait_for(connected, TIMEOUT)
    await peer.shell.wait_log(f"Connected: {BUMBLE_ADDRESS}")
    await disconnect(peer, connection)


async def test_shell_coded_advertising_connects_bumble(peer):
    address = await shell_identity(peer.shell)
    await peer.shell.shell.run("bt adv-create conn-nscan ext-adv coded identity")
    await peer.shell.wait_log("Created adv id: 0")
    await peer.shell.shell.run("bt adv-start")
    await peer.shell.wait_log(r"Advertiser\[0\].* set started")
    connection = await peer.bumble.connect(
        bumble_address(address),
        connection_parameters_preferences={
            hci.Phy.LE_CODED: ConnectionParametersPreferences()
        },
        timeout=TIMEOUT,
    )
    await peer.shell.wait_log("Connected: ")
    await peer.shell.shell.run("bt info")
    await peer.shell.wait_log("LE PHY: TX PHY LE Coded, RX PHY LE Coded")
    await peer.shell.shell.run("bt phy-update 2")
    await peer.shell.wait_log("LE PHY updated: TX PHY LE 2M, RX PHY LE 2M")
    await disconnect(peer, connection)
