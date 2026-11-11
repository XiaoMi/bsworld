# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Periodic Advertising Sync Transfer with the shell's PAST commands."""

import re

import pytest
from tests.integration.zephyr.shell.shell_peer.common import connect, disconnect
from tests.integration.zephyr.shell_common import shell_identity

# Periodic data for the train's reports.
PERIODIC_DATA = "0102030405"
# 0x30 x 1.25 ms: a 60-ms periodic interval.
PERIODIC_INTERVAL = "30"
# Core Vol 6, Part B, 4.6: the Periodic Advertising Sync Transfer - Recipient bit.
PAST_RECIPIENT = 1 << 25


async def start_periodic(device):
    """Create a non-connectable extended set and start its periodic and
    extended advertising, as Zephyr's own PAST test does before a transfer."""
    await device.shell.run("bt adv-create nconn-nscan ext-adv identity")
    await device.wait_log(r"Created adv id: \d+")
    await device.shell.run(f"bt per-adv-param {PERIODIC_INTERVAL} {PERIODIC_INTERVAL}")
    await device.shell.run(f"bt per-adv-data {PERIODIC_DATA}")
    await device.shell.run("bt per-adv on")
    await device.wait_log("Periodic advertising started")
    await device.shell.run("bt adv-start")


async def connect_roles(peripheral, central, sender):
    """Connect and wait for the sender's Feature Exchange.

    Return the sender, the recipient, the sender's identity and the sender's
    address as reported by the recipient's Host. The sender's Controller
    needs the peer's Recipient feature before a transfer (Core Vol 4, Part E,
    7.8.89, 7.8.90).
    """
    peripheral_address = await shell_identity(peripheral)
    central_identity = await shell_identity(central)
    central_address = await connect(peripheral, central, address=peripheral_address)
    if sender == "peripheral":
        roles = (peripheral, central, peripheral_address, peripheral_address)
    else:
        roles = (central, peripheral, central_identity, central_address)
    line = await roles[0].wait_log(r"LE Features: 0x[0-9a-f]{16}")
    features = int(re.search(r"0x([0-9a-f]{16})", line)[1], 16)
    assert features & PAST_RECIPIENT, "peer lacks the PAST Recipient feature"
    # The Zephyr Controller receives a Control PDU only up to its effective
    # maximum RX octets, while LL_PERIODIC_SYNC_IND has 34 octets of CtrData.
    # Core Vol 6, Part B, 4.5.10 limits only LL Data PDUs, so this Data Length
    # Update is an interoperability precondition, not a PAST requirement.
    await roles[0].shell.run("bt data-len-update 251")
    await roles[0].wait_log(r"LE data len updated: TX \(len: 251 ")
    await roles[1].wait_log(r"LE data len updated: .*RX \(len: 251 ")
    return roles


async def subscribe(recipient):
    """Mode 0x02 with Skip 0 for the connection (Vol 4, Part E, 7.8.91)."""
    await recipient.shell.run("bt past-subscribe conn skip 0")
    await recipient.wait_log("Subscribed to PAST")


async def expect_transfer(recipient, advertiser, sender):
    """The recipient synchronizes from the transfer and reports the train."""
    advertiser, sender = re.escape(advertiser), re.escape(sender)
    await recipient.wait_log(
        rf"PER_ADV_SYNC\[\d+\]: \[DEVICE\]: {advertiser} synced, "
        rf"Interval 0x00{PERIODIC_INTERVAL} .*PAST peer {sender}"
    )
    await recipient.wait_log(
        rf"PER_ADV_SYNC\[\d+\]: \[DEVICE\]: {advertiser}, .*data length [1-9]"
    )


@pytest.mark.parametrize("sender", ["peripheral", "central"])
async def test_set_info_transfer(shell_pair, sender):
    """A connected advertiser transfers its own train's SyncInfo (HCI LE
    Periodic Advertising Set Info Transfer); the subscribed peer synchronizes
    from it and reports the train."""
    peripheral, central = shell_pair
    source, recipient, identity, address = await connect_roles(
        peripheral, central, sender
    )
    await start_periodic(source)
    await subscribe(recipient)
    await source.shell.run("bt per-adv-set-info-transfer")
    await expect_transfer(recipient, identity, address)
    await disconnect(peripheral, central)


@pytest.mark.parametrize("sender", ["peripheral", "central"])
async def test_sync_transfer(shell_trio, sender):
    """A device synchronized with a third device's train transfers that sync
    (HCI LE Periodic Advertising Sync Transfer) to its connected peer."""
    peripheral, central, advertiser = shell_trio
    advertiser_address = await shell_identity(advertiser)
    await start_periodic(advertiser)
    source, recipient, _, address = await connect_roles(peripheral, central, sender)
    await source.shell.run("bt scan passive")
    line = await source.wait_log(
        rf"\[DEVICE\]: {re.escape(advertiser_address)}, .*"
        rf"Interval: 0x00{PERIODIC_INTERVAL} .*SID: 0x[0-9a-f]+"
    )
    sid = re.search(r"SID: (0x[0-9a-f]+)", line)[1]
    await source.shell.run(f"bt per-adv-sync-create {advertiser_address} {sid}")
    await source.wait_log(
        rf"PER_ADV_SYNC\[0\]: \[DEVICE\]: {re.escape(advertiser_address)} synced"
    )
    await source.shell.run("bt scan off")
    await subscribe(recipient)
    await source.shell.run("bt per-adv-sync-transfer")
    await expect_transfer(recipient, advertiser_address, address)
    await disconnect(peripheral, central)
