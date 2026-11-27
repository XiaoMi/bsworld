# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connect through a PAwR train (Core Vol 6, Part B, 4.4.2.12.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bscontroller.address import Address, AddressType
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_CONN_AUX_WINDOW_DELAY_CODED_US,
    LL_CONN_AUX_WINDOW_DELAY_UNCODED_US,
    LL_IFS_US,
    PDU_ADV_ADDRESS_OFFSET,
    PDU_ADV_ADDRESSES_END,
    PDU_ADV_AUX_CONNECT_RSP_SIZE,
    PDU_ADV_CONNECT_PAYLOAD_SIZE,
    PDU_ADV_CONNECT_SIZE,
    PDU_ADV_RXADD_MASK,
    PDU_ADV_SECOND_ADDRESS_OFFSET,
    PDU_ADV_TXADD_MASK,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising_pdu import ExtendedPdu
from bscontroller.linklayer.extended import connect_request, connect_response_matches
from bscontroller.linklayer.parameters import Parameters
from bscontroller.linklayer.results import ConnectionAccepted
from bscontroller.linklayer.scheduler import RadioPriority
from bscontroller.linklayer.state import Role
from bscontroller.radio.base import RADIO_SWITCH_TIME_US, Phy

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
    peer, phy = initiating.peer, item.secondary_phy
    state = item.pawr_state
    coding = item.secondary_coding.factor()
    initiating.address, generated = link.privacy.select_local_address(
        initiating.own_address_type,
        initiating.identity_address or initiating.address,
        link.privacy.identity(peer),
    )
    local_rpa = initiating.address if generated else None
    window_delay = (
        LL_CONN_AUX_WINDOW_DELAY_CODED_US
        if phy == Phy.LE_CODED
        else LL_CONN_AUX_WINDOW_DELAY_UNCODED_US
    )
    request_end = start_us + phy.airtime_us(PDU_ADV_CONNECT_SIZE, coding)
    parameters = link.connection_parameters(
        (initiating.phy_parameters or {}).get(phy, initiating.parameters),
        request_end,
        window_delay,
        phy,
    )
    response = None
    if (
        parameters is not None
        and link.roles.connections.can_accept(peer)
        and link.resource.admit(
            item,
            start_us - RADIO_SWITCH_TIME_US,
            request_end + LL_IFS_US + phy.airtime_us(PDU_ADV_AUX_CONNECT_RSP_SIZE),
            RadioPriority.PERIODIC_TX,
            earliest_us=link.radio.earliest_start_us(channel, phy, transmit=True)
            - RADIO_SWITCH_TIME_US,
        )
    ):
        train = {
            'access_address': item.periodic_aa,
            'crc_init': item.periodic_crc,
        }
        request_end = await link.transmit(
            channel,
            connect_request(initiating.address, peer, parameters),
            start_us=start_us,
            phy=phy,
            coding=coding,
            power_dbm=item.tx_power_dbm,
            **train,
        )
        if link.initiating is not initiating or item.pawr_state is not state:
            return
        response = await link.receive_response(channel, request_end, phy, **train)
    if link.initiating is not initiating or item.pawr_state is not state:
        return
    try:
        rsp = ExtendedPdu.decode(response.pdu) if response is not None else None
    except ValueError:
        rsp = None
    if rsp is not None and connect_response_matches(rsp, peer, initiating.address):
        assert parameters is not None
        link.roles.accepted(
            ConnectionAccepted(
                parameters,
                peer,
                Role.CENTRAL,
                request_end,
                csa2=True,
                self_address=initiating.address,
                local_rpa=local_rpa,
                window_delay_us=window_delay,
                phy=phy,
                initiator=initiating,
                pawr_advertiser=item,
            )
        )
        return
    link.roles.scanning.cancel_initiating()
    link.listener.on_connect_failed(
        peer, ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR
    )


async def pawr_accept(
    link: RadioLink, sync: PeriodicSync, channel: int, packet: Received
) -> None:
    """Answer an AUX_CONNECT_REQ naming this device in a synchronized PAwR
    subevent, then become its Peripheral (Core Vol 6, Part B, 4.4.2.12.2).

    AdvA must be one of this device's addresses; InitA becomes the peer.
    """
    pdu = packet.pdu
    if (
        len(pdu) != PDU_ADV_CONNECT_SIZE
        or pdu[1] != PDU_ADV_CONNECT_PAYLOAD_SIZE
        or sync.info is None
    ):
        return
    initiator = Address(
        pdu[PDU_ADV_ADDRESS_OFFSET:PDU_ADV_SECOND_ADDRESS_OFFSET],
        AddressType(bool(pdu[0] & PDU_ADV_TXADD_MASK)),
    )
    target = pdu[PDU_ADV_SECOND_ADDRESS_OFFSET:PDU_ADV_ADDRESSES_END]
    own = next(
        (
            address
            for address in (link.public_address, link.random_address)
            if address is not None
            and bytes(address) == target
            and address.is_random == bool(pdu[0] & PDU_ADV_RXADD_MASK)
        ),
        None,
    )
    if own is None or not link.roles.connections.can_accept(initiator):
        return
    try:
        parameters = Parameters.decode(pdu[PDU_ADV_ADDRESSES_END:])
    except ValueError:
        return
    response = ExtendedPdu(
        kind=AdvertisingPduType.AUX_CONNECT_RSP,
        address=bytes(own),
        random_address=own.is_random,
        target=bytes(initiator),
        random_target=initiator.is_random,
    )
    await link.transmit(
        channel,
        response.encode(),
        start_us=packet.end_us + LL_IFS_US,
        access_address=sync.info.access_address,
        crc_init=sync.info.crc_init,
        phy=packet.phy,
    )
    link.roles.accepted(
        ConnectionAccepted(
            parameters,
            initiator,
            Role.PERIPHERAL,
            packet.end_us,
            csa2=True,
            self_address=own,
            window_delay_us=(
                LL_CONN_AUX_WINDOW_DELAY_CODED_US
                if packet.phy == Phy.LE_CODED
                else LL_CONN_AUX_WINDOW_DELAY_UNCODED_US
            ),
            phy=packet.phy,
            pawr_sync=sync,
        )
    )
