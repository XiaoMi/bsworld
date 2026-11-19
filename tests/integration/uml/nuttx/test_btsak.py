# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""NuttX BTSak and Bumble advertising and scanning on the simulated radio."""

import asyncio
import re
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from tests.integration.uml.bumble_peer import (
    add_bumble_controller,
    advertising,
    advertising_data,
    open_bumble_device,
)
from tests.integration.uml.common import (
    add_uml_controller,
    open_uml_guest,
    require_guest_program,
)
from tests.integration.uml.nuttx.program import start_nuttx

from bsworld.interference import StaticAttenuation
from bsworld.uml import UmlGuest

_NUTTX_ADDRESS = "C0:FF:EE:00:00:01"
_BUMBLE_ADDRESSES = ("C0:FF:EE:00:01:01", "C0:FF:EE:00:01:02")
_TIMEOUT = 20.0
_SCAN_ADDRESS = re.compile(r"\d+\.\s+addr:\s+([\da-f:]{17})\s+type:", re.I)


def _btsak_program() -> Path:
    return require_guest_program("BSWORLD_UML_NUTTX_BIN", "/opt/bsworld/nuttx-btsak")


async def _stage_scan(guest: UmlGuest) -> None:
    commands = ["bt bnep0 scan start"]
    for _ in range(15):
        commands.extend(("sleep 1", "bt bnep0 scan get"))
    await guest.shell.run_many(commands)


async def _scan_addresses(guest: UmlGuest, expected: set[str]) -> None:
    remaining = set(expected)
    async with asyncio.timeout(_TIMEOUT):
        while remaining:
            line = await guest.wait_log(_SCAN_ADDRESS.pattern)
            match = _SCAN_ADDRESS.search(line)
            assert match is not None
            remaining.discard(match[1].upper())


async def test_bumble_scans_nuttx_advertising(topo):
    program = _btsak_program()
    log_subdir = Path("uml") / "nuttx"
    nuttx_controller = add_uml_controller(topo, _NUTTX_ADDRESS, log_subdir=log_subdir)
    bumble_controller = add_bumble_controller(topo, "scanner", _BUMBLE_ADDRESSES[0])
    topo.set_channel_attenuation(default_db=45.0)
    await topo.start()

    async with AsyncExitStack() as stack:
        nuttx = await open_uml_guest(
            stack, topo, nuttx_controller, label="nuttx", log_subdir=log_subdir
        )
        await start_nuttx(nuttx, program)
        bumble = await open_bumble_device(
            stack, "scanner", _BUMBLE_ADDRESSES[0], bumble_controller.hci_sock
        )
        found = asyncio.get_running_loop().create_future()

        def on_advertisement(report):
            address = report.address.to_string(with_type_qualifier=False)
            if not found.done() and address == _NUTTX_ADDRESS:
                found.set_result(report)

        bumble.on("advertisement", on_advertisement)
        await bumble.start_scanning(active=False, filter_duplicates=False)
        try:
            await nuttx.shell.run("bt bnep0 advertise start")
            report = await asyncio.wait_for(found, _TIMEOUT)
            assert report.is_connectable
        finally:
            await bumble.stop_scanning()
            bumble.remove_listener("advertisement", on_advertisement)


@pytest.mark.parametrize("number_of_advertisers", (1, 2), ids=("single", "multiple"))
async def test_nuttx_scans_bumble_advertisers(topo, number_of_advertisers):
    program = _btsak_program()
    log_subdir = Path("uml") / "nuttx"
    nuttx_controller = add_uml_controller(topo, _NUTTX_ADDRESS, log_subdir=log_subdir)
    advertisers = [
        (
            f"advertiser{index}",
            address,
            add_bumble_controller(topo, f"advertiser{index}", address),
        )
        for index, address in enumerate(_BUMBLE_ADDRESSES[:number_of_advertisers], 1)
    ]
    topo.set_channel_attenuation(default_db=45.0)
    await topo.start()

    async with AsyncExitStack() as stack:
        nuttx = await open_uml_guest(
            stack, topo, nuttx_controller, label="nuttx", log_subdir=log_subdir
        )
        await start_nuttx(nuttx, program)
        for name, address, controller in advertisers:
            bumble = await open_bumble_device(stack, name, address, controller.hci_sock)
            await stack.enter_async_context(
                advertising(bumble, data=advertising_data(name))
            )
        await _stage_scan(nuttx)
        await _scan_addresses(nuttx, {address for _, address, _ in advertisers})


async def test_one_nuttx_scanner_recovers_after_targeted_interference(topo):
    program = _btsak_program()
    scanner_specs = [
        (label, Path("uml") / label, address)
        for label, address in (
            ("nuttx-protected", _NUTTX_ADDRESS),
            ("nuttx-jammed", "C0:FF:EE:00:00:02"),
        )
    ]
    protected, jammed = (
        add_uml_controller(topo, address, log_subdir=path)
        for _, path, address in scanner_specs
    )
    peripheral = add_bumble_controller(topo, "advertiser", _BUMBLE_ADDRESSES[0])
    interference_end_us = 6_000_000
    jammers = [
        topo.add_burst_interference(
            frequency,
            power_dbm=20.0,
            interf_type="CW",
            windows=[(1, interference_end_us)],
        )
        for frequency in (2402.0, 2426.0, 2480.0)
    ]
    topo.set_channel_attenuation(
        default_db=100.0,
        static_paths=[
            StaticAttenuation(tx=peripheral, rx=protected, attenuation_db=45.0),
            StaticAttenuation(tx=peripheral, rx=jammed, attenuation_db=45.0),
            *(
                StaticAttenuation(tx=jammer, rx=jammed, attenuation_db=0.0)
                for jammer in jammers
            ),
        ],
    )
    await topo.start()

    async with AsyncExitStack() as stack:
        guests = []
        for (label, path, _), controller in zip(
            scanner_specs, (protected, jammed), strict=True
        ):
            guest = await open_uml_guest(
                stack, topo, controller, label=label, log_subdir=path
            )
            await start_nuttx(guest, program)
            guests.append(guest)
        bumble = await open_bumble_device(
            stack, "advertiser", _BUMBLE_ADDRESSES[0], peripheral.hci_sock
        )
        await stack.enter_async_context(
            advertising(bumble, data=advertising_data("interference advertiser"))
        )
        protected_guest, jammed_guest = guests
        for guest in guests:
            await _stage_scan(guest)
        pattern = rf"(?i)\d+\.\s+addr:\s+{_BUMBLE_ADDRESSES[0]}\s+type:"
        await protected_guest.wait_log(pattern)
        remaining = interference_end_us / 1_000_000 - asyncio.get_running_loop().time()
        assert remaining > 0, "The protected scanner must report during interference"
        with pytest.raises(TimeoutError):
            await jammed_guest.wait_log(pattern, timeout=remaining)
        await jammed_guest.wait_log(pattern)
