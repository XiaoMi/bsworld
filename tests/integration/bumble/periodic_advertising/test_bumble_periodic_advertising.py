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
"""Bumble periodic advertiser and periodic sync test."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path

from tests.integration.bumble.common import (
    ADVERTISER_ADDRESS,
    OBSERVER_ADDRESS,
    PERIODIC_NAME,
    TIMEOUT,
    AdvertisingData,
    AdvertisingEventProperties,
    AdvertisingParameters,
    OwnAddressType,
    PeriodicAdvertisingParameters,
    PeriodicAdvertisingSync,
    advertising_data,
    open_bumble_device,
    wait_for_advertisement,
)


async def test_bumble_observer_syncs_periodic_advertising(topo):
    advertiser_ctl = topo.add_controller(
        public_address=ADVERTISER_ADDRESS,
        log_subdir=Path("bumble") / "periodic_advertiser",
    )
    observer_ctl = topo.add_controller(
        public_address=OBSERVER_ADDRESS,
        log_subdir=Path("bumble") / "periodic_observer",
    )
    topo.set_channel_attenuation(default_db=45.0)

    await topo.start()

    sync_established = asyncio.Event()
    periodic_report = asyncio.Event()

    async with AsyncExitStack() as stack:
        advertiser = await open_bumble_device(
            stack,
            "periodic_advertiser",
            ADVERTISER_ADDRESS,
            advertiser_ctl.hci_sock,
        )
        observer = await open_bumble_device(
            stack,
            "periodic_observer",
            OBSERVER_ADDRESS,
            observer_ctl.hci_sock,
        )

        advertising_set = await advertiser.create_advertising_set(
            advertising_parameters=AdvertisingParameters(
                advertising_event_properties=AdvertisingEventProperties(
                    is_connectable=False,
                    is_scannable=False,
                ),
                advertising_sid=1,
                own_address_type=OwnAddressType.PUBLIC,
                primary_advertising_interval_min=20.0,
                primary_advertising_interval_max=20.0,
            ),
            advertising_data=advertising_data(PERIODIC_NAME),
            periodic_advertising_parameters=PeriodicAdvertisingParameters(
                periodic_advertising_interval_min=20.0,
                periodic_advertising_interval_max=20.0,
            ),
            periodic_advertising_data=advertising_data(PERIODIC_NAME),
        )
        sync = None
        await advertising_set.start_periodic()
        try:
            periodic_advertisement = await wait_for_advertisement(
                observer,
                PERIODIC_NAME,
                stop_scanning=False,
            )
            sync = await observer.create_periodic_advertising_sync(
                periodic_advertisement.address,
                sid=periodic_advertisement.sid,
                sync_timeout=5.0,
            )
            sync.on(
                PeriodicAdvertisingSync.EVENT_ESTABLISHMENT,
                lambda: sync_established.set(),
            )

            def on_periodic_advertisement(advertisement) -> None:  # noqa: ANN001
                if (
                    advertisement.data
                    and advertisement.data.get(AdvertisingData.Type.COMPLETE_LOCAL_NAME)
                    == PERIODIC_NAME
                ):
                    periodic_report.set()

            sync.on(
                PeriodicAdvertisingSync.EVENT_PERIODIC_ADVERTISEMENT,
                on_periodic_advertisement,
            )
            await asyncio.wait_for(sync_established.wait(), timeout=TIMEOUT)
            await asyncio.wait_for(periodic_report.wait(), timeout=TIMEOUT)
        finally:
            await observer.stop_scanning()
            if sync is not None:
                await sync.terminate()
            await advertising_set.stop_periodic()
            await advertising_set.stop()
            await advertising_set.remove()
