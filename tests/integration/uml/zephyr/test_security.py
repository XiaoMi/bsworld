# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Pair and reconnect using bonded identities."""

from bumble import crypto, hci, pairing
from tests.integration.uml.zephyr.peer import (
    BUMBLE_ADDRESS,
    TIMEOUT,
    bumble_address,
    disconnect,
)
from tests.integration.uml.zephyr.shell import shell_identity


async def test_shell_resolves_bonded_peer_during_rpa_reconnection(privacy_peer):
    peer = privacy_peer
    shell, bumble = peer.shell, peer.bumble
    address = bumble_address(await shell_identity(shell))
    bumble.pairing_config_factory = lambda _: pairing.PairingConfig(
        mitm=False, identity_address_type=pairing.PairingConfig.AddressType.PUBLIC
    )
    prand = bytes.fromhex("102040")
    initial_rpa = hci.Address(crypto.ah(bumble.irk, prand) + prand)
    await bumble.send_sync_command(
        hci.HCI_LE_Set_Random_Address_Command(random_address=initial_rpa)
    )
    bumble.random_address = initial_rpa
    await shell.shell.run("bt advertise on identity")
    await shell.wait_log("Advertising started")
    connection = await bumble.connect(
        address, own_address_type=hci.OwnAddressType.RANDOM, timeout=TIMEOUT
    )
    await shell.wait_log(
        f"Connected: R:{initial_rpa.to_string(with_type_qualifier=False)}"
    )
    await connection.pair()
    await shell.wait_log(f"Bonded with {BUMBLE_ADDRESS}")
    await disconnect(peer, connection)
    await shell.shell.run("bt advertise off")
    await shell.wait_log("Advertising stopped")

    bumble.address_resolution_offload = True
    await bumble.refresh_resolving_list()
    # Require the Controller event to carry the resolved identity.
    bumble.address_resolver = None
    await bumble.send_sync_command(
        hci.HCI_LE_Set_Address_Resolution_Enable_Command(address_resolution_enable=1)
    )

    async def reconnect():
        connection = await bumble.connect(
            address,
            own_address_type=hci.OwnAddressType.RESOLVABLE_OR_PUBLIC,
            timeout=TIMEOUT,
        )
        await shell.wait_log(f"Connected: {BUMBLE_ADDRESS}")
        assert connection.peer_address == address
        assert connection.peer_address.is_resolved
        assert connection.peer_resolvable_address.is_resolvable
        assert connection.self_resolvable_address.is_resolvable
        assert connection.self_resolvable_address != initial_rpa
        await connection.encrypt()
        await shell.wait_log(r"Security changed: .* level 2")
        await disconnect(peer, connection)

    await shell.shell.run("bt advertise on")
    await shell.wait_log("Advertising started")
    await reconnect()
    await shell.shell.run("bt advertise off")
    await shell.wait_log("Advertising stopped")

    await shell.shell.run(
        f"bt adv-create conn-nscan ext-adv directed {BUMBLE_ADDRESS} low dir-rpa"
    )
    await shell.wait_log("Created adv id: 0")
    await shell.shell.run("bt adv-start")
    await shell.wait_log(r"Advertiser\[0\].* set started")
    await reconnect()
