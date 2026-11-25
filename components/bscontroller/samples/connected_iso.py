# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Exchange ISO data in both directions over one Connected Isochronous Stream."""

import asyncio
from contextlib import closing

from bumble import hci
from bumble.device import (
    AdvertisingEventProperties,
    AdvertisingParameters,
    CigParameters,
    CisLink,
    ConnectionParametersPreferences,
)
from bumble.utils import EventWatcher

from . import runtime


async def run(devices):
    central, peripheral = devices['central'], devices['peripheral']
    for device in (central, peripheral):
        await device.send_sync_command(
            hci.HCI_LE_Set_Host_Feature_Command(
                bit_number=hci.LeFeature.CONNECTED_ISOCHRONOUS_STREAM, bit_value=1
            )
        )

    advertiser = await peripheral.create_advertising_set(
        advertising_parameters=AdvertisingParameters(
            advertising_event_properties=AdvertisingEventProperties(
                is_connectable=True
            ),
            own_address_type=hci.OwnAddressType.PUBLIC,
        )
    )
    acl = await central.connect(
        peripheral.public_address,
        own_address_type=hci.OwnAddressType.PUBLIC,
        connection_parameters_preferences={
            hci.Phy.LE_1M: ConnectionParametersPreferences(40, 40)
        },
    )

    cig = CigParameters(
        cig_id=1,
        sdu_interval_c_to_p=20_000,
        sdu_interval_p_to_c=20_000,
        max_transport_latency_c_to_p=50,
        max_transport_latency_p_to_c=50,
        cis_parameters=[
            CigParameters.CisParameters(
                cis_id=1,
                max_sdu_c_to_p=32,
                max_sdu_p_to_c=32,
                phy_c_to_p=hci.PhyBit.LE_1M,
                phy_p_to_c=hci.PhyBit.LE_1M,
                rtn_c_to_p=1,
                rtn_p_to_c=1,
            )
        ],
    )
    (handle,) = await central.setup_cig(cig)

    # Create on the Central while the Peripheral waits for and accepts the request.
    request = asyncio.get_running_loop().create_future()

    async def accept_request():
        stream = await request
        await peripheral.accept_cis_request(stream)
        return stream

    with closing(EventWatcher()) as watcher:
        watcher.once(peripheral, peripheral.EVENT_CIS_REQUEST, request.set_result)
        (central_cis,), peripheral_cis = await asyncio.gather(
            central.create_cis([(handle, acl)]), accept_request()
        )

    to_central, to_peripheral = asyncio.Queue(), asyncio.Queue()
    for stream, inbox in ((central_cis, to_central), (peripheral_cis, to_peripheral)):
        stream.sink = inbox.put_nowait
        await stream.setup_data_path(CisLink.Direction.CONTROLLER_TO_HOST)
        await stream.setup_data_path(CisLink.Direction.HOST_TO_CONTROLLER)

    central_data = b'Hello from Central'
    peripheral_data = b'Hello from Peripheral'
    central_cis.write(central_data)
    peripheral_cis.write(peripheral_data)
    for inbox, expected in (
        (to_central, peripheral_data),
        (to_peripheral, central_data),
    ):
        packet = await inbox.get()
        # Idle ISO events may appear before the first nonempty SDU.
        while not packet.iso_sdu_fragment:
            packet = await inbox.get()
        assert packet.packet_status_flag == 0
        assert packet.iso_sdu_fragment == expected

    await central_cis.disconnect()
    await central.send_sync_command(hci.HCI_LE_Remove_CIG_Command(cig.cig_id))
    await acl.disconnect()
    await advertiser.remove()

    return {'cis': 1, 'iso_interval_ms': central_cis.iso_interval, 'sdus_received': 2}


if __name__ == '__main__':
    runtime.main(run, ('central', 'peripheral'), __doc__)
