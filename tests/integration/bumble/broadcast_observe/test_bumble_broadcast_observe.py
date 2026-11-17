# Copyright 2026 Xiaomi Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Bumble advertiser and observer running on BSWorld Virtual Controllers."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from tests.integration.bumble.common import (
    ADVERTISER_ADDRESS,
    ADVERTISER_NAME,
    OBSERVER_ADDRESS,
    TIMEOUT,
    AdvertisingData,
    advertising,
    advertising_data,
    open_bumble_device,
)

from bsworld.interference import StaticAttenuation

_JAMMED_OBSERVER_ADDRESS = "F0:F1:F2:F3:F4:03"
_BLE_ADVERTISING_CHANNELS_MHZ = (2402.0, 2426.0, 2480.0)
_INTERFERENCE_END_US = 5_000_000


async def test_bumble_observer_finds_bumble_advertiser(bumble_pair):
    async with AsyncExitStack() as stack:
        found = asyncio.Event()

        advertiser, observer = bumble_pair

        def on_advertisement(advertisement) -> None:  # noqa: ANN001
            if (
                advertisement.data.get(AdvertisingData.Type.COMPLETE_LOCAL_NAME)
                == ADVERTISER_NAME
            ):
                found.set()

        observer.on("advertisement", on_advertisement)
        await observer.start_scanning(active=False, filter_duplicates=False)
        try:
            await stack.enter_async_context(
                advertising(
                    advertiser,
                    connectable=True,
                    data=advertising_data(ADVERTISER_NAME),
                    interval_min=20.0,
                    interval_max=20.0,
                )
            )
            await asyncio.wait_for(found.wait(), timeout=TIMEOUT)
        finally:
            await observer.stop_scanning()


async def test_bumble_targeted_interference_isolates_and_recovers(topo):
    advertiser_ctl = topo.add_controller(
        public_address=ADVERTISER_ADDRESS,
        log_subdir=Path("bumble") / "interference_advertiser",
    )
    protected_observer_ctl = topo.add_controller(
        public_address=OBSERVER_ADDRESS,
        log_subdir=Path("bumble") / "interference_observer_protected",
    )
    jammed_observer_ctl = topo.add_controller(
        public_address=_JAMMED_OBSERVER_ADDRESS,
        log_subdir=Path("bumble") / "interference_observer_jammed",
    )
    jammers = [
        topo.add_burst_interference(
            frequency_mhz,
            power_dbm=20.0,
            interf_type="CW",
            windows=[(1, _INTERFERENCE_END_US)],
        )
        for frequency_mhz in _BLE_ADVERTISING_CHANNELS_MHZ
    ]
    topo.set_channel_attenuation(
        default_db=100.0,
        static_paths=[
            StaticAttenuation(
                tx=advertiser_ctl, rx=protected_observer_ctl, attenuation_db=45.0
            ),
            StaticAttenuation(
                tx=advertiser_ctl, rx=jammed_observer_ctl, attenuation_db=45.0
            ),
            *(
                StaticAttenuation(
                    tx=jammer, rx=protected_observer_ctl, attenuation_db=100.0
                )
                for jammer in jammers
            ),
            *(
                StaticAttenuation(tx=jammer, rx=jammed_observer_ctl, attenuation_db=0.0)
                for jammer in jammers
            ),
        ],
    )

    await topo.start()

    protected_found = asyncio.Event()
    jammed_found = asyncio.Event()

    async with AsyncExitStack() as stack:
        advertiser = await open_bumble_device(
            stack,
            "interference_advertiser",
            ADVERTISER_ADDRESS,
            advertiser_ctl.hci_sock,
        )
        protected_observer = await open_bumble_device(
            stack,
            "interference_observer_protected",
            OBSERVER_ADDRESS,
            protected_observer_ctl.hci_sock,
        )
        jammed_observer = await open_bumble_device(
            stack,
            "interference_observer_jammed",
            _JAMMED_OBSERVER_ADDRESS,
            jammed_observer_ctl.hci_sock,
        )

        def on_protected_advertisement(advertisement) -> None:  # noqa: ANN001
            if (
                advertisement.data.get(AdvertisingData.Type.COMPLETE_LOCAL_NAME)
                == ADVERTISER_NAME
            ):
                protected_found.set()

        def on_jammed_advertisement(advertisement) -> None:  # noqa: ANN001
            if (
                advertisement.data.get(AdvertisingData.Type.COMPLETE_LOCAL_NAME)
                == ADVERTISER_NAME
            ):
                jammed_found.set()

        protected_observer.on("advertisement", on_protected_advertisement)
        jammed_observer.on("advertisement", on_jammed_advertisement)
        await protected_observer.start_scanning(active=False, filter_duplicates=False)
        await jammed_observer.start_scanning(active=False, filter_duplicates=False)
        await stack.enter_async_context(
            advertising(
                advertiser,
                connectable=True,
                data=advertising_data(ADVERTISER_NAME),
                interval_min=20.0,
                interval_max=20.0,
            )
        )
        try:
            await asyncio.wait_for(protected_found.wait(), timeout=TIMEOUT)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(jammed_found.wait(), timeout=2.0)
            await asyncio.wait_for(jammed_found.wait(), timeout=TIMEOUT)
        finally:
            await protected_observer.stop_scanning()
            await jammed_observer.stop_scanning()
