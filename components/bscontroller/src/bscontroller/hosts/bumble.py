# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connect an upstream Bumble Host to a Controller's Unix H4 endpoint.

The Host keeps its own HCI types and only exchanges serialized packets with the
Controller. Importing BSController does not import or require Bumble.
"""

from contextlib import asynccontextmanager
from pathlib import Path


@asynccontextmanager
async def open_device(name: str, address: str, hci_socket: str | Path):
    """Own the Bumble transport and powered Host on the caller's asyncio clock.

    The caller's event loop must use the same simulation clock as the
    Controllers; standalone programs can use RadioEventLoop and RadioSession.
    """
    try:
        from bumble.device import Device
        from bumble.hci import Address
        from bumble.transport import open_transport
    except ModuleNotFoundError as error:
        if error.name != 'bumble':
            raise
        raise ModuleNotFoundError(
            'Bumble Host support requires: pip install "bscontroller[bumble]"',
            name='bumble',
        ) from error

    async with await open_transport(f'unix:{hci_socket}') as transport:
        device = Device.with_hci(
            name, Address(address), transport.source, transport.sink
        )
        try:
            await device.power_on()
            yield device
        finally:
            await device.power_off()


def advertising_data(name):
    from bumble import data_types
    from bumble.core import AdvertisingData

    return bytes(
        AdvertisingData(
            [
                data_types.Flags(
                    AdvertisingData.Flags.LE_GENERAL_DISCOVERABLE_MODE
                    | AdvertisingData.Flags.BR_EDR_NOT_SUPPORTED
                ),
                data_types.CompleteLocalName(name),
            ]
        )
    )


@asynccontextmanager
async def advertising(
    device,
    *,
    name=None,
    connectable=False,
    legacy=True,
    sid=0,
    periodic_interval=None,
    scan_response_data=b'',
    data=None,
    interval_min=40,
    interval_max=60,
):
    """Own a Bumble advertising set, optionally including a periodic train."""
    from bumble import hci
    from bumble.device import (
        AdvertisingEventProperties,
        AdvertisingParameters,
        PeriodicAdvertisingParameters,
    )

    name = name or device.name
    parameters = AdvertisingParameters(
        advertising_event_properties=AdvertisingEventProperties(
            is_connectable=connectable,
            is_scannable=legacy and (connectable or bool(scan_response_data)),
            is_legacy=legacy,
        ),
        own_address_type=hci.OwnAddressType.PUBLIC,
        primary_advertising_interval_min=interval_min,
        primary_advertising_interval_max=interval_max,
        advertising_sid=sid,
    )
    periodic = (
        PeriodicAdvertisingParameters(periodic_interval, periodic_interval)
        if periodic_interval
        else None
    )
    advertiser = await device.create_advertising_set(
        advertising_parameters=parameters,
        advertising_data=advertising_data(name) if data is None else data,
        scan_response_data=scan_response_data,
        periodic_advertising_parameters=periodic,
        periodic_advertising_data=advertising_data(name) if periodic else b'',
        auto_start=False,
    )
    try:
        if periodic:
            await advertiser.start_periodic()
        await advertiser.start()
        yield advertiser
    finally:
        await advertiser.stop_periodic()
        if advertiser.enabled:
            await advertiser.stop()
        await advertiser.remove()


@asynccontextmanager
async def scanning(device, *, active=False):
    from bumble import hci

    await device.start_scanning(
        active=active,
        own_address_type=hci.OwnAddressType.PUBLIC,
        scan_interval=60,
        scan_window=30,
        scanning_phys=[hci.Phy.LE_1M],
        filter_duplicates=False,
    )
    try:
        yield
    finally:
        await device.stop_scanning()
