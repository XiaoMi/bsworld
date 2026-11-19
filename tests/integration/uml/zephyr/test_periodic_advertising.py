# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Synchronize to periodic advertising and recover after sync loss."""

import asyncio

from bumble import hci
from bumble.device import (
    AdvertisingEventProperties,
    AdvertisingParameters,
    PeriodicAdvertisingParameters,
    PeriodicAdvertisingSync,
)
from tests.integration.uml.bumble_peer import advertising_data
from tests.integration.uml.zephyr.peer import BUMBLE_ADDRESS, TIMEOUT, bumble_address
from tests.integration.uml.zephyr.shell import shell_identity


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
            bumble_address(address),
            0,
            sync_timeout=2,
        )
        reports = asyncio.Queue()
        sync.on(
            PeriodicAdvertisingSync.EVENT_PERIODIC_ADVERTISEMENT, reports.put_nowait
        )
        async with asyncio.timeout(TIMEOUT):
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
    await asyncio.wait_for(lost.wait(), TIMEOUT)
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
        create_sync = f"bt per-adv-sync-create {BUMBLE_ADDRESS} 1 skip 0 timeout 00c8"
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
