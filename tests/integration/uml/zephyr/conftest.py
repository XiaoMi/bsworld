# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Run the Zephyr native_sim shell in a UML guest with a Bumble peer."""

from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from random import Random
from types import SimpleNamespace

import pytest_asyncio
from bumble import crypto, pairing
from bumble.device import DeviceConfiguration
from bumble.gatt import GATT_CENTRAL_ADDRESS_RESOLUTION__CHARACTERISTIC, Characteristic
from bumble.keys import MemoryKeyStore
from bumble.profiles.gap import GenericAccessService
from tests.integration.uml.bumble_peer import open_bumble_device
from tests.integration.uml.common import (
    add_uml_controller,
    open_uml_guest,
    require_guest_program,
)
from tests.integration.uml.zephyr.shell import initialize_shell

_LOG_SUBDIR = Path("uml/zephyr")
PERIODIC_ADVERTISER_ADDRESS = "F0:F1:F2:F3:F4:03"


@asynccontextmanager
async def _open_peers(
    topo, monkeypatch, *, with_periodic_advertiser=False, bumble_config=None
):
    # Fixed test inputs include Bumble's SMP nonces and P-256 key.
    random = Random(42)
    monkeypatch.setattr(crypto, "r", lambda: random.randbytes(16))
    monkeypatch.setattr(
        crypto.EccKey,
        "generate",
        classmethod(lambda cls: cls.from_private_key_bytes(bytes(31) + b"\x01")),
    )
    program = require_guest_program(
        "BSWORLD_UML_ZEPHYR_BIN", "/opt/bsworld/zephyr-shell"
    )
    zephyr_controller = add_uml_controller(
        topo, "F0:F1:F2:F3:F4:02", log_subdir=_LOG_SUBDIR
    )
    controller = topo.add_controller(
        public_address="F0:F1:F2:F3:F4:01", log_subdir="bumble"
    )
    advertiser_controller = None
    if with_periodic_advertiser:
        advertiser_controller = topo.add_controller(
            public_address=PERIODIC_ADVERTISER_ADDRESS, log_subdir="periodic-advertiser"
        )
    topo.set_channel_attenuation(default_db=45)
    await topo.start()

    async with AsyncExitStack() as stack:
        shell = await open_uml_guest(
            stack, topo, zephyr_controller, label="zephyr", log_subdir=_LOG_SUBDIR
        )
        await initialize_shell(shell, program)
        bumble = await open_bumble_device(
            stack,
            "Bumble peer",
            controller.public_address,
            controller.hci_sock,
            config=bumble_config,
        )
        bumble.keystore = MemoryKeyStore()
        bumble.pairing_config_factory = lambda _: pairing.PairingConfig(mitm=False)
        advertiser = None
        if advertiser_controller is not None:
            advertiser = await open_bumble_device(
                stack,
                "Periodic advertiser",
                PERIODIC_ADVERTISER_ADDRESS,
                advertiser_controller.hci_sock,
            )
        yield SimpleNamespace(
            shell=shell, bumble=bumble, stack=stack, advertiser=advertiser
        )


@pytest_asyncio.fixture
async def peer(topo, monkeypatch):
    async with _open_peers(topo, monkeypatch) as peers:
        yield peers


@pytest_asyncio.fixture
async def privacy_peer(topo, monkeypatch):
    config = DeviceConfiguration(irk=bytes(range(1, 17)), gap_service_enabled=False)
    async with _open_peers(topo, monkeypatch, bumble_config=config) as peers:
        # Zephyr reads this GAP characteristic before using directed RPAs.
        gap_service = GenericAccessService(peers.bumble.name)
        gap_service.characteristics.append(
            Characteristic(
                GATT_CENTRAL_ADDRESS_RESOLUTION__CHARACTERISTIC,
                Characteristic.Properties.READ,
                Characteristic.Permissions.READABLE,
                b"\x01",
            )
        )
        peers.bumble.add_service(gap_service)
        yield peers


@pytest_asyncio.fixture
async def peer_with_advertiser(topo, monkeypatch):
    async with _open_peers(topo, monkeypatch, with_periodic_advertiser=True) as peers:
        yield peers
