# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Read the local P-256 public key and reject an invalid peer public key."""

import asyncio

from bumble import hci

from . import runtime


async def run(devices):
    device = devices['controller']
    reports = asyncio.Queue()
    # Bumble dispatches these LE events by name and leaves their payloads as bytes.
    device.host.on_hci_le_read_local_p_256_public_key_complete_event = (
        reports.put_nowait
    )
    device.host.on_hci_le_generate_dhkey_complete_event = reports.put_nowait

    await device.send_async_command(hci.HCI_LE_Read_Local_P_256_Public_Key_Command())
    event = await reports.get()
    status, public_key = event.parameters[1], event.parameters[2:]
    assert status == hci.HCI_SUCCESS and len(public_key) == 64 and any(public_key)

    # The point (0, 0) is outside the P-256 curve.
    await device.send_async_command(
        hci.HCI_Command(
            op_code=hci.HCI_LE_GENERATE_DHKEY_COMMAND,
            parameters=bytes(64),
        )
    )
    event = await reports.get()
    status, dhkey = event.parameters[1], event.parameters[2:]
    assert status == hci.HCI_ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
    assert dhkey == b'\xff' * 32

    return {
        'local_public_key_bytes': len(public_key),
        'invalid_peer_key_status': f'0x{status:02X}',
    }


if __name__ == '__main__':
    runtime.main(run, ('controller',), __doc__)
