# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Directional interference and RF recovery with three Zephyr shells."""

import re

import pytest
from tests.integration.zephyr.shell.devices import create_zephyr_shell
from tests.integration.zephyr.shell_common import (
    initialize_shell,
    shell_identity,
)

from bsworld.interference import StaticAttenuation


async def test_targeted_interference_and_recovery(topo):
    advertiser = create_zephyr_shell(
        topo, name="advertiser", public_address="F0:F1:F2:F3:F4:01"
    )
    protected = create_zephyr_shell(
        topo, name="protected", public_address="F0:F1:F2:F3:F4:02"
    )
    jammed = create_zephyr_shell(
        topo, name="jammed", public_address="F0:F1:F2:F3:F4:03"
    )
    jammers = [
        topo.add_burst_interference(
            frequency, power_dbm=20, windows=[(1, 5_000_000)], rand_seed=1
        )
        for frequency in (2402.0, 2426.0, 2480.0)
    ]
    topo.set_channel_attenuation(
        default_db=100,
        static_paths=[
            StaticAttenuation(tx=advertiser, rx=scanner, attenuation_db=45)
            for scanner in (protected, jammed)
        ]
        + [
            StaticAttenuation(tx=jammer, rx=jammed, attenuation_db=0)
            for jammer in jammers
        ],
    )
    await topo.start()
    for device in (advertiser, protected, jammed):
        await initialize_shell(device)
    address = await shell_identity(advertiser)
    await advertiser.shell.run("bt advertise nconn identity")
    await advertiser.wait_log("Advertising started")
    for scanner in (protected, jammed):
        await scanner.shell.run("bt scan passive")
        await scanner.wait_log("Bluetooth passive scan enabled")
    pattern = rf"\[DEVICE\]: {re.escape(address)},.*RSSI"
    await protected.wait_log(pattern)
    with pytest.raises(TimeoutError):
        await jammed.wait_log(pattern, timeout=2)
    await jammed.wait_log(pattern)
