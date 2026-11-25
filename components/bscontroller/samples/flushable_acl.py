# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Expire an ACL packet before transmission and deliver the next ATT write."""

import asyncio
import struct
from contextlib import AsyncExitStack

from bumble import hci
from bumble.att import ATT_CID, ATT_Write_Command
from bumble.core import UUID
from bumble.device import ConnectionParametersPreferences
from bumble.gatt import Characteristic, Service
from bumble.l2cap import L2CAP_PDU

from . import runtime


def enqueue_write(queue, handle, attribute_handle, value, *, flushable=False):
    """Build the ATT write directly to select its HCI packet boundary flag."""
    att = ATT_Write_Command(attribute_handle=attribute_handle, attribute_value=value)
    pdu = bytes(L2CAP_PDU(ATT_CID, bytes(att)))
    boundary = (
        hci.HCI_ACL_PB_FIRST_FLUSHABLE
        if flushable
        else hci.HCI_ACL_PB_FIRST_NON_FLUSHABLE
    )
    queue.enqueue(hci.HCI_AclDataPacket(handle, boundary, 0, len(pdu), pdu), handle)


async def run(devices):
    central, peripheral = devices['central'], devices['peripheral']
    attribute = Characteristic(
        UUID('8B598A1C-805A-4D38-8E21-797F756F1C86'),
        Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
        Characteristic.Permissions.WRITEABLE,
    )
    peripheral.add_service(
        Service(UUID('7ACED21C-0212-43B5-90DF-3FE3C195E0DB'), [attribute])
    )
    writes = asyncio.Queue()

    def on_write(_, value):
        writes.put_nowait(value)

    async with AsyncExitStack() as stack:
        attribute.on(attribute.EVENT_WRITE, on_write)
        stack.callback(attribute.remove_listener, attribute.EVENT_WRITE, on_write)
        await peripheral.start_advertising(own_address_type=hci.OwnAddressType.PUBLIC)
        stack.push_async_callback(peripheral.stop_advertising)
        connection = await central.connect(
            peripheral.public_address,
            own_address_type=hci.OwnAddressType.PUBLIC,
            connection_parameters_preferences={
                hci.Phy.LE_1M: ConnectionParametersPreferences(
                    connection_interval_min=40, connection_interval_max=40
                )
            },
        )
        stack.push_async_callback(connection.disconnect)

        handle = connection.handle
        response = await central.send_sync_command_raw(
            hci.HCI_Command(
                op_code=hci.HCI_WRITE_AUTOMATIC_FLUSH_TIMEOUT_COMMAND,
                parameters=struct.pack('<HH', handle, 2),  # 2 × 0.625 ms.
            )
        )
        assert response.parameters[3] == hci.HCI_SUCCESS
        queue = central.host.get_data_packet_queue(handle)
        assert queue is not None

        enqueue_write(queue, handle, attribute.handle, b'anchor')
        assert await writes.get() == b'anchor'
        await queue.drain(handle)

        # Send between connection events so the 1.25 ms timeout can elapse.
        interval_s = connection.parameters.connection_interval / 1000
        await asyncio.sleep(interval_s / 4)
        completed = queue.completed
        enqueue_write(queue, handle, attribute.handle, b'expire', flushable=True)
        enqueue_write(queue, handle, attribute.handle, b'keep')
        assert await writes.get() == b'keep'
        await queue.drain(handle)
        assert queue.completed == completed + 2
        assert writes.empty()

    return {'expired': 1, 'delivered': 1, 'credits_returned': 2}


if __name__ == '__main__':
    runtime.main(run, ('central', 'peripheral'), __doc__)
