# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""A Zephyr shell and a Bumble peer on separate BSControllers."""

from contextlib import AsyncExitStack
from random import Random
from types import SimpleNamespace

import pytest_asyncio
from bumble import crypto, pairing
from bumble.keys import MemoryKeyStore
from tests.integration.bumble.common import open_bumble_device
from tests.integration.zephyr.shell.devices import create_zephyr_shell
from tests.integration.zephyr.shell_common import initialize_shell


@pytest_asyncio.fixture
async def peer(topo, monkeypatch):
    # Fixed test inputs include Bumble's SMP nonces, IRK and P-256 key.
    random = Random(42)
    monkeypatch.setattr(crypto, "r", lambda: random.randbytes(16))
    monkeypatch.setattr(
        crypto.EccKey,
        "generate",
        classmethod(lambda cls: cls.from_private_key_bytes(bytes(31) + b"\x01")),
    )
    shell = create_zephyr_shell(topo, name="zephyr", public_address="F0:F1:F2:F3:F4:02")
    controller = topo.add_controller(
        public_address="F0:F1:F2:F3:F4:01",
        log_subdir="bumble",
    )
    topo.set_channel_attenuation(default_db=45)
    await topo.start()
    await initialize_shell(shell)
    async with AsyncExitStack() as stack:
        bumble = await open_bumble_device(
            stack, "Bumble peer", controller.public_address, controller.hci_sock
        )
        bumble.keystore = MemoryKeyStore()
        bumble.pairing_config_factory = lambda _: pairing.PairingConfig(mitm=False)
        yield SimpleNamespace(
            shell=shell, bumble=bumble, controller=controller, stack=stack
        )
