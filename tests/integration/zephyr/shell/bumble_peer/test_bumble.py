# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Complete Zephyr BLE shell interoperates with Bumble on either Controller."""

import asyncio

import pytest
from bumble import hci
from bumble.core import AdvertisingData
from bumble.device import (
    AdvertisingEventProperties,
    AdvertisingParameters,
    Connection,
    ConnectionParametersPreferences,
    Peer,
    PeriodicAdvertisingParameters,
    PeriodicAdvertisingSync,
)
from bumble.gatt import Characteristic, Service
from tests.integration.bumble.common import advertising, advertising_data
from tests.integration.zephyr.shell_common import shell_identity

_TIMEOUT = 30
_ADDRESS = "P:F0:F1:F2:F3:F4:01"
_PAYLOAD = bytes(range(200))


def _bumble_address(shell_address: str) -> hci.Address:
    """Convert a shell "P:" or "R:" address into a typed Bumble address."""
    kind = (
        hci.AddressType.RANDOM_DEVICE
        if shell_address.startswith("R:")
        else hci.AddressType.PUBLIC_DEVICE
    )
    return hci.Address(shell_address[2:], kind)


async def _connect(peer, *, shell_central=False):
    shell, bumble = peer.shell, peer.bumble
    if shell_central:
        connected = asyncio.get_running_loop().create_future()
        bumble.once("connection", connected.set_result)
        await peer.stack.enter_async_context(
            advertising(bumble, connectable=True, data=advertising_data("Bumble peer"))
        )
        await shell.shell.run(f"bt connect {_ADDRESS}")
        connection = await asyncio.wait_for(connected, _TIMEOUT)
    else:
        address = await shell_identity(shell)
        await shell.shell.run("bt advertise on identity")
        await shell.wait_log("Advertising started")
        connection = await bumble.connect(
            _bumble_address(address),
            timeout=_TIMEOUT,
        )
    await shell.wait_log("Connected: ")
    return connection


async def _disconnect(peer, connection):
    disconnected = asyncio.get_running_loop().create_future()
    connection.once(Connection.EVENT_DISCONNECTION, disconnected.set_result)
    await peer.shell.shell.run("bt disconnect")
    await peer.shell.wait_log(r"Disconnected: .*reason 0x16")
    assert await asyncio.wait_for(disconnected, _TIMEOUT) == 0x13


@pytest.mark.parametrize("active", [False, True], ids=["passive", "active"])
async def test_shell_scans_bumble(peer, active):
    await peer.stack.enter_async_context(
        advertising(
            peer.bumble,
            connectable=True,
            data=advertising_data("Bumble peer") if not active else b"\x02\x01\x06",
            scan_response_data=advertising_data("Bumble peer") if active else b"",
        )
    )
    await peer.shell.shell.run("bt scan on" if active else "bt scan passive")
    await peer.shell.wait_log(r"\[DEVICE\]: P:F0:F1:F2:F3:F4:01,.*Bumble peer")
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
        report = await asyncio.wait_for(seen, _TIMEOUT)
        assert report.is_connectable
    finally:
        await peer.bumble.stop_scanning()
        peer.bumble.remove_listener("advertisement", advertisement)


async def test_shell_server_encrypted_notifications(peer):
    await peer.shell.shell.run("gatt register")
    await peer.shell.wait_log("Registered test vendor service")
    connection = await _connect(peer)
    await connection.pair()
    await peer.shell.wait_log(r"Security changed: .* level 2")
    client = Peer(connection)
    assert await client.request_mtu(247) >= 203
    await client.discover_all()
    characteristic = next(
        c
        for s in client.services
        for c in s.characteristics
        if str(c.uuid).lower().endswith("56789abcdef5")
    )
    notifications = asyncio.Queue()
    await client.subscribe(characteristic, notifications.put_nowait)
    await peer.shell.shell.run(
        f"gatt notify {characteristic.handle:x} {_PAYLOAD.hex()}"
    )
    assert await asyncio.wait_for(notifications.get(), _TIMEOUT) == _PAYLOAD
    await _disconnect(peer, connection)


async def test_shell_client_reads_bumble_server(peer):
    characteristic = Characteristic(
        "2A19",
        Characteristic.Properties.READ,
        Characteristic.Permissions.READABLE,
        b"\x64",
    )
    peer.bumble.add_service(Service("180F", [characteristic]))
    connection = await _connect(peer, shell_central=True)
    await peer.shell.shell.run("gatt discover")
    await peer.shell.wait_log("Discover complete")
    await peer.shell.shell.run(f"gatt read {characteristic.handle:x}")
    await peer.shell.wait_log("Read complete: err 0x00 length 1")
    await peer.shell.wait_log(r"00000000:\s+64\b")
    await _disconnect(peer, connection)


async def test_shell_link_layer_updates(peer):
    connection = await _connect(peer)
    for mask, phy in ((2, "LE 2M"), (4, "LE Coded"), (1, "LE 1M")):
        await peer.shell.shell.run(f"bt phy-update {mask}")
        await peer.shell.wait_log(rf"LE PHY updated: TX PHY {phy}, RX PHY {phy}")
    await peer.shell.shell.run("bt data-len-update 251")
    await peer.shell.wait_log(r"LE data len updated: TX \(len: 251 ")
    await connection.set_data_length(200, 1704)
    await peer.shell.wait_log(r"LE data len updated: .*RX \(len: 200 ")
    await connection.update_parameters(40, 40, 0, 4000)
    await peer.shell.wait_log(r"LE conn param updated: int 0x0020 lat 0 to 400")
    await _disconnect(peer, connection)


async def test_shell_filter_accept_list_connects_bumble(peer):
    connected = asyncio.get_running_loop().create_future()
    peer.bumble.once("connection", connected.set_result)
    await peer.stack.enter_async_context(
        advertising(peer.bumble, connectable=True, data=advertising_data("Bumble peer"))
    )
    await peer.shell.shell.run(f"bt fal-add {_ADDRESS}")
    await peer.shell.shell.run("bt fal-connect on")
    connection = await asyncio.wait_for(connected, _TIMEOUT)
    await peer.shell.wait_log(f"Connected: {_ADDRESS}")
    await _disconnect(peer, connection)


async def test_shell_reconnects_and_reuses_bond(peer):
    connection = await _connect(peer)
    await connection.pair()
    await peer.shell.wait_log("Bonded with ")
    await _disconnect(peer, connection)
    connection = await _connect(peer)
    await connection.encrypt()
    await peer.shell.wait_log(r"Security changed: .* level 2")
    await _disconnect(peer, connection)


async def test_shell_coded_advertising_connects_bumble(peer):
    address = await shell_identity(peer.shell)
    await peer.shell.shell.run("bt adv-create conn-nscan ext-adv coded identity")
    await peer.shell.wait_log("Created adv id: 0")
    await peer.shell.shell.run("bt adv-start")
    await peer.shell.wait_log(r"Advertiser\[0\].* set started")
    connection = await peer.bumble.connect(
        _bumble_address(address),
        connection_parameters_preferences={
            hci.Phy.LE_CODED: ConnectionParametersPreferences()
        },
        timeout=_TIMEOUT,
    )
    await peer.shell.wait_log("Connected: ")
    await peer.shell.shell.run("bt info")
    await peer.shell.wait_log("LE PHY: TX PHY LE Coded, RX PHY LE Coded")
    await peer.shell.shell.run("bt phy-update 2")
    await peer.shell.wait_log("LE PHY updated: TX PHY LE 2M, RX PHY LE 2M")
    await _disconnect(peer, connection)


async def test_shell_periodic_advertiser_sync_loss_and_recovery(peer):
    address = await shell_identity(peer.shell)
    await peer.shell.shell.run("bt adv-create nconn-nscan ext-adv identity")
    await peer.shell.wait_log("Created adv id: 0")
    await peer.shell.shell.run_many(
        (
            "bt per-adv-param 0050 0050",
            "bt per-adv-data 02ff4253",
            "bt per-adv on",
            "bt adv-start",
        )
    )
    await peer.shell.wait_log(r"Advertiser\[0\].* set started")
    await peer.bumble.start_scanning(active=False)

    async def synchronize(expected):
        sync = await peer.bumble.create_periodic_advertising_sync(
            _bumble_address(address),
            0,
            sync_timeout=2,
        )
        reports = asyncio.Queue()
        sync.on(
            PeriodicAdvertisingSync.EVENT_PERIODIC_ADVERTISEMENT, reports.put_nowait
        )
        async with asyncio.timeout(_TIMEOUT):
            while True:
                report = await reports.get()
                if report.data_bytes == expected:
                    return sync

    # The shell takes a payload length; the over-the-air AD length also
    # includes the type octet.
    sync = await synchronize(bytes.fromhex("03ff4253"))
    lost = asyncio.Event()
    sync.once(PeriodicAdvertisingSync.EVENT_LOSS, lost.set)
    await peer.shell.shell.run("bt per-adv off")
    await peer.shell.wait_log("Periodic advertising stopped")
    await asyncio.wait_for(lost.wait(), _TIMEOUT)
    # Bumble retains lost sync objects in its registry. The Controller has
    # already deleted the handle on Sync Lost, so only discard the Host entry.
    assert sync.state == PeriodicAdvertisingSync.State.LOST
    peer.bumble.periodic_advertising_syncs.remove(sync)
    await peer.shell.shell.run_many(("bt per-adv-data 03ff425357", "bt per-adv on"))
    await peer.shell.wait_log("Periodic advertising started")
    sync = await synchronize(bytes.fromhex("04ff425357"))
    await sync.terminate()
    await peer.bumble.stop_scanning()


async def test_shell_synchronizes_bumble_periodic_advertiser(peer):
    advertiser = await peer.bumble.create_advertising_set(
        advertising_parameters=AdvertisingParameters(
            advertising_event_properties=AdvertisingEventProperties(
                is_connectable=False, is_scannable=False
            ),
            advertising_sid=1,
            own_address_type=hci.OwnAddressType.PUBLIC,
        ),
        advertising_data=advertising_data("Bumble periodic"),
        periodic_advertising_parameters=PeriodicAdvertisingParameters(),
        periodic_advertising_data=bytes.fromhex("03ff4253"),
    )
    await advertiser.start_periodic()
    try:
        await peer.shell.shell.run("bt scan passive")
        await peer.shell.wait_log(r"\[DEVICE\]: P:F0:F1:F2:F3:F4:01,.*SID: 0x0?1\b")
        create_sync = f"bt per-adv-sync-create {_ADDRESS} 1 skip 0 timeout 00c8"
        await peer.shell.shell.run(create_sync)
        await peer.shell.wait_log(r"PER_ADV_SYNC\[\d+\]: .* synced")
        await peer.shell.wait_log(r"PER_ADV_SYNC\[\d+\]: .*data length 4")
        await advertiser.stop_periodic()
        await peer.shell.wait_log(r"PER_ADV_SYNC\[\d+\]: .*sync terminated")
        await advertiser.set_periodic_advertising_data(bytes.fromhex("04ff425357"))
        await advertiser.start_periodic()
        await peer.shell.shell.run(create_sync)
        await peer.shell.wait_log(r"PER_ADV_SYNC\[\d+\]: .* synced")
        await peer.shell.wait_log(r"PER_ADV_SYNC\[\d+\]: .*data length 5")
    finally:
        await advertiser.stop_periodic()
        await advertiser.stop()
        await advertiser.remove()
