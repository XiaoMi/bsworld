# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connect through a PAwR train (Core Vol 6, Part B, 4.4.2.12.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import LL_IFS_US, PDU_ADV_CONNECT_SIZE
from bscontroller.linklayer.advertising_pdu import (
    advertising_request_peer,
    connect_response,
)
from bscontroller.linklayer.advertising_requests import connection_request_parameters
from bscontroller.linklayer.auxiliary_connection import (
    AuxiliaryConnectionFailure,
    request_auxiliary_connection,
)
from bscontroller.linklayer.results import ConnectionAccepted
from bscontroller.linklayer.state import Role
from bscontroller.linklayer.timing import auxiliary_window_delay_us

if TYPE_CHECKING:
    from bscontroller.linklayer.discovery import Advertising, Initiating, PeriodicSync
    from bscontroller.linklayer.link import RadioLink
    from bscontroller.radio.base import Received


async def pawr_connect(
    link: RadioLink,
    item: Advertising,
    initiating: Initiating,
    channel: int,
    start_us: int,
) -> None:
    """Send AUX_CONNECT_REQ in place of a PAwR subevent's AUX_SYNC_SUBEVENT_IND,
    then wait T_IFS for AUX_CONNECT_RSP (Core Vol 6, Part B, 4.4.2.12.2).

    Without that response the request is discarded, once, and the Host
    learns that the connection failed (Vol 4, Part E, 7.7.65.10).
    """
    result = await request_auxiliary_connection(
        link,
        initiating,
        initiating.peer,
        channel,
        start_us,
        item.secondary_phy,
        pawr_advertiser=item,
    )
    if isinstance(result, ConnectionAccepted):
        link.roles.accepted(result)
    elif result != AuxiliaryConnectionFailure.CANCELLED:
        link.roles.scanning.cancel_initiating()
        link.listener.on_connect_failed(
            initiating.peer, ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR
        )


async def pawr_accept(
    link: RadioLink, sync: PeriodicSync, channel: int, packet: Received
) -> None:
    """Answer an AUX_CONNECT_REQ naming this device in a synchronized PAwR
    subevent, then become its Peripheral (Core Vol 6, Part B, 4.4.2.12.2).

    AdvA must be one of this device's addresses; InitA becomes the peer.
    """
    pdu = packet.pdu
    if len(pdu) != PDU_ADV_CONNECT_SIZE or sync.info is None:
        return
    for own in (link.public_address, link.random_address):
        if own is not None and (peer := advertising_request_peer(pdu, own)) is not None:
            break
    else:
        return
    if not link.roles.connections.can_accept(peer):
        return
    parameters = connection_request_parameters(link, pdu)
    if parameters is None:
        return
    await link.transmit(
        channel,
        connect_response(own, peer),
        start_us=packet.end_us + LL_IFS_US,
        access_address=sync.info.access_address,
        crc_init=sync.info.crc_init,
        phy=packet.phy,
    )
    link.roles.accepted(
        ConnectionAccepted(
            parameters,
            peer,
            Role.PERIPHERAL,
            packet.end_us,
            csa2=True,
            self_address=own,
            window_delay_us=auxiliary_window_delay_us(packet.phy),
            phy=packet.phy,
            pawr_sync=sync,
        )
    )
