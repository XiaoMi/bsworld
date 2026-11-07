# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

from contextlib import AsyncExitStack

import pytest_asyncio
from tests.integration.bumble.common import (
    ADVERTISER_ADDRESS,
    OBSERVER_ADDRESS,
    add_bumble_controller,
    open_bumble_device,
)


@pytest_asyncio.fixture
async def bumble_pair(topo):
    """Build and start the standard two-device topology for Bumble scenarios."""
    advertiser_ctl = add_bumble_controller(topo, "advertiser", ADVERTISER_ADDRESS)
    observer_ctl = add_bumble_controller(topo, "observer", OBSERVER_ADDRESS)
    topo.set_channel_attenuation(default_db=45.0)
    await topo.start()
    async with AsyncExitStack() as stack:
        advertiser = await open_bumble_device(
            stack, "advertiser", ADVERTISER_ADDRESS, advertiser_ctl.hci_sock
        )
        observer = await open_bumble_device(
            stack, "observer", OBSERVER_ADDRESS, observer_ctl.hci_sock
        )
        yield advertiser, observer
