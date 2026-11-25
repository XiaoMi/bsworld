# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Periodic train parameters and Lower Tester LL_PERIODIC_SYNC_IND wire encoding."""

import struct

from bscontroller.address import Address, AddressType
from bscontroller.linklayer import LL_DATA_CHANNEL_MAP_ALL, ControlOpcode

PERIODIC_INTERVAL = 80  # 100 ms
SYNC_AA = 0x8E89BED7 ^ 0x13579BDF
SYNC_CRC = 0x445566
ADVERTISER = Address('00:11:22:33:44:77', AddressType.PUBLIC_DEVICE)
SERVICE_DATA = 0xA5C3

# Independent LL_PERIODIC_SYNC_IND CtrData layout (Vol 6, Part B, Figure 2.48).
CTRDATA = struct.Struct('<H18sHHBB6sH')


def sync_info(offset_field, interval=PERIODIC_INTERVAL, counter=0, sca=5):
    return struct.pack(
        '<HH5sI3sH',
        offset_field,
        interval,
        (LL_DATA_CHANNEL_MAP_ALL | sca << 37).to_bytes(5, 'little'),
        SYNC_AA,
        SYNC_CRC.to_bytes(3, 'little'),
        counter,
    )


def indication(
    *,
    info=None,
    event=0,
    last=0,
    sid=3,
    random=False,
    sca=0,
    phy=0x01,
    address=ADVERTISER,
    sync_event=0,
    identifier=SERVICE_DATA,
):
    info = sync_info(100) if info is None else info
    return bytes([ControlOpcode.LL_PERIODIC_SYNC_IND]) + CTRDATA.pack(
        identifier,
        info,
        event,
        last,
        sid | int(random) << 4 | sca << 5,
        phy,
        bytes(address),
        sync_event,
    )
