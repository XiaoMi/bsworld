# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Link Layer control procedures between two shells, from either role."""

from tests.integration.zephyr.shell.shell_peer.common import (
    PAYLOAD,
    connect,
    disconnect,
    notify_and_verify,
    wait_both,
)


async def test_link_updates(shell_pair):
    """PHY, data length, connection and channel map updates from either role.

    Every procedure completes when initiated by the side under test, and the
    link still carries a 200-octet notification byte-exactly afterwards.
    """
    peripheral, central = shell_pair
    devices = (central, peripheral)
    await peripheral.shell.run("gatt register")
    await peripheral.wait_log("Registered test vendor service")
    await connect(peripheral, central)

    # PHY Update (Core Vol 6, Part B, 5.1.10). The responder answers with the
    # preference of its own last LE Set PHY, so only the Central initiates
    # here; the Peripheral-initiated case runs in the LE Coded scenario.
    for mask, phy in ((2, "LE 2M"), (4, "LE Coded"), (1, "LE 1M")):
        await central.shell.run(f"bt phy-update {mask}")
        await wait_both(devices, rf"LE PHY updated: TX PHY {phy}, RX PHY {phy}")

    # Data Length Update (5.1.9) grows, shrinks, then grows from the other
    # role. Each side reports its own TX and RX lengths.
    for initiator, responder, octets in (
        (central, peripheral, 251),
        (central, peripheral, 27),
        (peripheral, central, 200),
    ):
        await initiator.shell.run(f"bt data-len-update {octets}")
        await initiator.wait_log(rf"LE data len updated: TX \(len: {octets} ")
        await responder.wait_log(rf"LE data len updated: .*RX \(len: {octets} ")

    # Connection Update (5.1.1) from the Central and Connection Parameters
    # Request (5.1.7) from the Peripheral. Select intervals that differ from
    # the initial 0x0018 so both Hosts receive the update event (Vol 4, Part E,
    # 7.7.65.3).
    await central.shell.run("bt conn-update 0020 0020 0 0190")
    await wait_both(devices, r"LE conn param updated: int 0x0020 lat 0 to 400")
    await peripheral.shell.run("bt conn-update 0030 0030 1 0190")
    await central.wait_log(r"LE conn param req: int \(0x0030, 0x0030\)")
    await wait_both(devices, r"LE conn param updated: int 0x0030 lat 1 to 400")

    # Channel Map Update (5.1.2) from Host classification: channels 0-7 and
    # 16-23 stay usable. The following ATT traffic must survive the new map.
    await central.shell.run("bt channel-map 0000ff00ff")

    await central.shell.run("gatt exchange-mtu")
    await central.wait_log("Exchange successful")
    await notify_and_verify(peripheral, central, PAYLOAD)
    await disconnect(peripheral, central)
