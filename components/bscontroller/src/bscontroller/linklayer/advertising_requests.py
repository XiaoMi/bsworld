# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Admission of scan and connection requests by an advertiser."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    PDU_ADV_ADDRESSES_END,
    PDU_ADV_CHSEL_MASK,
    PDU_ADV_CONNECT_SIZE,
    PDU_ADV_SCAN_REQ_SIZE,
    PDU_ADV_TYPE_MASK,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising_pdu import advertising_request_peer
from bscontroller.linklayer.parameters import Parameters
from bscontroller.linklayer.results import ConnectionAccepted
from bscontroller.linklayer.state import (
    LL_ADV_FILTER_CONNECT_MASK,
    LL_ADV_FILTER_SCAN_MASK,
    Role,
)
from bscontroller.linklayer.timing import auxiliary_window_delay_us
from bscontroller.radio.base import Phy

if TYPE_CHECKING:
    from bscontroller.address import Address
    from bscontroller.linklayer.advertiser import AdvertisingManager
    from bscontroller.linklayer.discovery import Advertising
    from bscontroller.linklayer.link import RadioLink
    from bscontroller.radio.base import Received


def _identity(link: RadioLink, item: Advertising, peer: Address) -> Address | None:
    identity = link.privacy.identity(peer)
    if (
        identity is not None
        and item.target is not None
        and identity != link.privacy.identity(item.target)
    ):
        return None
    return identity


def accept_scan_request(
    advertising: AdvertisingManager, item: Advertising, packet: Received
) -> bool:
    """Validate SCAN_REQ and AUX_SCAN_REQ, including the scan filter (6.B.4.3.2)."""
    pdu = packet.pdu
    if len(pdu) != PDU_ADV_SCAN_REQ_SIZE or (
        pdu[0] & PDU_ADV_TYPE_MASK != AdvertisingPduType.SCAN_REQ
    ):
        return False
    link = advertising.link
    peer = advertising_request_peer(pdu, item.address)
    if peer is None or (identity := _identity(link, item, peer)) is None:
        return False
    if (
        item.filter_policy & LL_ADV_FILTER_SCAN_MASK
        and identity not in link.accept_list
    ):
        return False
    if item.notify_scan_request:
        # The Host may change the set from inside the HCI notification callback.
        advertising.report_scan_request(item, identity)
        return advertising.event_is_current(item)
    return True


def connection_request_parameters(link: RadioLink, pdu: bytes) -> Parameters | None:
    """Decode LLData after validating the request addresses."""
    try:
        return Parameters.decode(pdu[PDU_ADV_ADDRESSES_END:])
    except ValueError as error:
        link.trace(f'LL CONNECT REJECTED {error}')
        return None


def accept_advertising_connection(
    advertising: AdvertisingManager, item: Advertising, packet: Received
) -> ConnectionAccepted | None:
    """Validate CONNECT_IND or AUX_CONNECT_REQ before any radio or role change."""
    pdu = packet.pdu
    if len(pdu) != PDU_ADV_CONNECT_SIZE or (
        pdu[0] & PDU_ADV_TYPE_MASK != AdvertisingPduType.CONNECT_IND
    ):
        return None
    link = advertising.link
    peer = advertising_request_peer(pdu, item.address)
    if peer is None or (identity := _identity(link, item, peer)) is None:
        return None
    if not advertising.can_accept_connection(peer):
        return None
    if (
        item.target is None
        and item.filter_policy & LL_ADV_FILTER_CONNECT_MASK
        and identity not in link.accept_list
    ):
        return None
    parameters = connection_request_parameters(link, pdu)
    if parameters is None:
        return None
    return ConnectionAccepted(
        parameters,
        peer,
        Role.PERIPHERAL,
        packet.end_us,
        self_address=item.address,
        local_rpa=item.local_rpa,
        csa2=item.extended or bool(pdu[0] & PDU_ADV_CHSEL_MASK),
        advertiser=item,
        window_delay_us=(
            auxiliary_window_delay_us(packet.phy)
            if item.extended
            else LL_CONN_INTERVAL_UNIT_US
        ),
        phy=packet.phy if item.extended else Phy.LE_1M,
    )
