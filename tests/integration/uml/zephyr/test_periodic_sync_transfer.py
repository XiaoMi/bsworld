# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Transfer periodic advertising syncs over a connection."""

import asyncio

import pytest
from bumble import hci
from bumble.device import (
    AdvertisingEventProperties,
    AdvertisingParameters,
    PeriodicAdvertisingParameters,
    PeriodicAdvertisingSync,
)
from tests.integration.uml.zephyr.peer import (
    BUMBLE_ADDRESS,
    TIMEOUT,
    bumble_address,
    connect,
    disconnect,
)
from tests.integration.uml.zephyr.shell import shell_identity

_PERIODIC_DATA = bytes.fromhex("03ff4253")
_SERVICE_DATA = 0xB501


async def _connect_for_transfer(peer, shell_central):
    connection = await connect(peer, shell_central=shell_central)
    await peer.shell.wait_log(r"LE Features: 0x[0-9a-f]{16}")
    return connection


async def _subscribe_bumble(peer, connection):
    # Configure PAST reception with periodic reports and no skipped events.
    await peer.bumble.send_sync_command(
        hci.HCI_LE_Set_Periodic_Advertising_Sync_Transfer_Parameters_Command(
            connection_handle=connection.handle,
            mode=2,
            skip=0,
            sync_timeout=200,
            cte_type=0,
        )
    )


async def _expect_periodic_transfer(peer, connection, command, address):
    received = asyncio.get_running_loop().create_future()
    reports = asyncio.Queue()

    def on_transfer(sync, source):
        sync.on(
            PeriodicAdvertisingSync.EVENT_PERIODIC_ADVERTISEMENT, reports.put_nowait
        )
        received.set_result((sync, source))

    peer.bumble.once(peer.bumble.EVENT_PERIODIC_ADVERTISING_SYNC_TRANSFER, on_transfer)
    await peer.shell.shell.run(command)
    sync, source = await asyncio.wait_for(received, TIMEOUT)
    peer.stack.push_async_callback(sync.terminate)
    assert source is connection
    assert sync.state is PeriodicAdvertisingSync.State.ESTABLISHED
    assert sync.advertiser_address == bumble_address(address)
    assert (await asyncio.wait_for(reports.get(), TIMEOUT)).data_bytes == _PERIODIC_DATA


@pytest.mark.parametrize("shell_central", [False, True], ids=["peripheral", "central"])
async def test_shell_transfers_periodic_advertising_set_info(peer, shell_central):
    connection = await _connect_for_transfer(peer, shell_central)
    await _subscribe_bumble(peer, connection)
    address = await shell_identity(peer.shell)
    await peer.shell.shell.run_many(
        (
            "bt adv-create nconn-nscan ext-adv identity",
            "bt per-adv-param 0030 0030",
            "bt per-adv-data 02ff4253",
            "bt per-adv on",
            "bt adv-start",
        )
    )
    await peer.shell.wait_log(r"Advertiser\[0\].* set started")
    await _expect_periodic_transfer(
        peer, connection, "bt per-adv-set-info-transfer", address
    )
    await disconnect(peer, connection)


async def _start_periodic_advertising(stack, advertiser):
    advertising_set = await advertiser.create_advertising_set(
        advertising_parameters=AdvertisingParameters(
            advertising_event_properties=AdvertisingEventProperties(
                is_connectable=False, is_scannable=False
            ),
            advertising_sid=1,
            own_address_type=hci.OwnAddressType.PUBLIC,
        ),
        periodic_advertising_parameters=PeriodicAdvertisingParameters(
            periodic_advertising_interval_min=60,
            periodic_advertising_interval_max=60,
        ),
        periodic_advertising_data=_PERIODIC_DATA,
    )
    stack.push_async_callback(advertising_set.remove)
    stack.push_async_callback(advertising_set.stop)
    await advertising_set.start_periodic()
    stack.push_async_callback(advertising_set.stop_periodic)
    return advertising_set


@pytest.mark.parametrize("shell_central", [False, True], ids=["peripheral", "central"])
async def test_shell_transfers_third_device_periodic_sync(
    peer_with_advertiser, shell_central
):
    peer = peer_with_advertiser
    connection = await _connect_for_transfer(peer, shell_central)
    await _subscribe_bumble(peer, connection)
    address = f"P:{peer.advertiser.public_address.to_string(with_type_qualifier=False)}"
    await _start_periodic_advertising(peer.stack, peer.advertiser)
    await peer.shell.shell.run("bt scan passive")
    await peer.shell.wait_log(rf"\[DEVICE\]: {address},.*SID: 0x0?1\b")
    await peer.shell.shell.run(f"bt per-adv-sync-create {address} 1")
    await peer.shell.wait_log(rf"PER_ADV_SYNC\[0\]: \[DEVICE\]: {address} synced")
    await peer.shell.shell.run("bt scan off")
    await _expect_periodic_transfer(
        peer, connection, "bt per-adv-sync-transfer", address
    )
    await disconnect(peer, connection)


async def _subscribe_shell(shell):
    await shell.shell.run("bt past-subscribe conn skip 0")
    await shell.wait_log("Subscribed to PAST")


async def _expect_shell_periodic_transfer(shell, address):
    prefix = rf"PER_ADV_SYNC\[\d+\]: \[DEVICE\]: {address}"
    await shell.wait_log(
        rf"{prefix} synced, Interval 0x0030 .*SD 0x{_SERVICE_DATA:04X},"
        rf" PAST peer {BUMBLE_ADDRESS}"
    )
    await shell.wait_log(rf"{prefix}, .*data length {len(_PERIODIC_DATA)}")


@pytest.mark.parametrize("shell_central", [False, True], ids=["peripheral", "central"])
async def test_shell_receives_periodic_advertising_set_info(peer, shell_central):
    connection = await _connect_for_transfer(peer, shell_central)
    await _subscribe_shell(peer.shell)
    advertiser = await _start_periodic_advertising(peer.stack, peer.bumble)
    await advertiser.transfer_periodic_info(connection, service_data=_SERVICE_DATA)
    await _expect_shell_periodic_transfer(peer.shell, BUMBLE_ADDRESS)
    await disconnect(peer, connection)


@pytest.mark.parametrize("shell_central", [False, True], ids=["peripheral", "central"])
async def test_shell_receives_third_device_periodic_sync(
    peer_with_advertiser, shell_central
):
    peer = peer_with_advertiser
    connection = await _connect_for_transfer(peer, shell_central)
    await _subscribe_shell(peer.shell)
    await _start_periodic_advertising(peer.stack, peer.advertiser)
    await peer.bumble.start_scanning(active=False)
    peer.stack.push_async_callback(peer.bumble.stop_scanning)
    sync = await peer.bumble.create_periodic_advertising_sync(
        peer.advertiser.public_address, 1, sync_timeout=2
    )
    peer.stack.push_async_callback(sync.terminate)
    reports = asyncio.Queue()
    sync.on(PeriodicAdvertisingSync.EVENT_PERIODIC_ADVERTISEMENT, reports.put_nowait)
    assert (await asyncio.wait_for(reports.get(), TIMEOUT)).data_bytes == _PERIODIC_DATA
    await sync.transfer(connection, service_data=_SERVICE_DATA)
    address = f"P:{peer.advertiser.public_address.to_string(with_type_qualifier=False)}"
    await _expect_shell_periodic_transfer(peer.shell, address)
    await disconnect(peer, connection)
