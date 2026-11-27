# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connection initiation on primary and secondary advertising channels."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bscontroller.address import Address, AddressType, OwnAddressType
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    PDU_ADV_ACCESS_ADDRESS,
    PDU_ADV_ADDRESS_OFFSET,
    PDU_ADV_ADDRESSES_END,
    PDU_ADV_CHSEL_MASK,
    PDU_ADV_CONNECT_PAYLOAD_SIZE,
    PDU_ADV_CONNECT_SIZE,
    PDU_ADV_CRC_INIT,
    PDU_ADV_DIRECT_SIZE,
    PDU_ADV_LEGACY_SIZE_MIN,
    PDU_ADV_RXADD_MASK,
    PDU_ADV_SECOND_ADDRESS_OFFSET,
    PDU_ADV_TXADD_MASK,
    PDU_ADV_TYPE_MASK,
    PDU_HEADER_SIZE,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising_pdu import (
    ExtendedPdu,
    connect_request,
)
from bscontroller.linklayer.filtering import InitiatorFilterPolicy
from bscontroller.linklayer.results import ConnectionAccepted
from bscontroller.linklayer.scheduler import RadioPriority
from bscontroller.linklayer.state import Role
from bscontroller.linklayer.timing import (
    LL_RESPONSE_START_TOLERANCE_US,
    auxiliary_window_delay_us,
    connect_exchange_us,
)
from bscontroller.radio.base import PHY_CODED_S8_FACTOR, RADIO_SWITCH_TIME_US, Phy

if TYPE_CHECKING:
    from bscontroller.linklayer.discovery import Advertising, Initiating
    from bscontroller.linklayer.link import RadioLink
    from bscontroller.linklayer.parameters import Parameters
    from bscontroller.radio.base import Received


async def initiate_legacy(
    link: RadioLink, channel: int, packet: Received, item: Initiating
) -> None:
    """Send CONNECT_IND T_IFS after an accepted ADV_IND/ADV_DIRECT_IND (Core Vol 6, Part
    B, 4.4.4.1, 4.3.4).
    """
    pdu = packet.pdu
    if item.filter_policy == InitiatorFilterPolicy.DECISION_PDUS_ONLY:
        return
    if (
        len(pdu) < PDU_ADV_LEGACY_SIZE_MIN
        or pdu[0] & PDU_ADV_TYPE_MASK
        not in (AdvertisingPduType.ADV_IND, AdvertisingPduType.ADV_DIRECT_IND)
        or pdu[1] != len(pdu) - PDU_HEADER_SIZE
    ):
        return
    peer = Address(
        pdu[PDU_ADV_ADDRESS_OFFSET:PDU_ADV_SECOND_ADDRESS_OFFSET],
        AddressType(bool(pdu[0] & PDU_ADV_TXADD_MASK)),
    )
    identity = link.privacy.identity(peer)
    if identity is None:
        return
    item.address, generated = link.privacy.select_local_address(
        item.own_address_type, item.identity_address or item.address, identity
    )
    local_rpa = item.address if generated else None
    if pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.ADV_DIRECT_IND:
        if len(pdu) != PDU_ADV_DIRECT_SIZE:
            return
        target = Address(
            pdu[PDU_ADV_SECOND_ADDRESS_OFFSET:PDU_ADV_ADDRESSES_END],
            AddressType(bool(pdu[0] & PDU_ADV_RXADD_MASK)),
        )
        if (
            OwnAddressType(item.own_address_type).uses_resolvable_address
            and not target.is_resolvable
        ) or not link.privacy.accept_target(target, item.address, peer):
            return
    peer = Address(
        pdu[PDU_ADV_ADDRESS_OFFSET:PDU_ADV_SECOND_ADDRESS_OFFSET],
        AddressType(bool(pdu[0] & PDU_ADV_TXADD_MASK)),
    )
    if (
        identity not in link.accept_list
        if InitiatorFilterPolicy(item.filter_policy).filters_non_decision_pdus
        else identity != item.peer
    ):
        return
    if not link.roles.connections.can_accept(peer):
        return
    duration_us = Phy.LE_1M.airtime_us(PDU_ADV_CONNECT_SIZE)
    start_us = link.interframe_slot(channel, packet.end_us, duration_us)
    if start_us is None:
        return
    # Choose a usable first connection window before sending CONNECT_IND;
    # its timing becomes shared state as soon as the peer accepts the request.
    parameters = link.connection_parameters(
        item.parameters, start_us + duration_us, LL_CONN_INTERVAL_UNIT_US
    )
    if parameters is None:
        return
    csa2 = bool(pdu[0] & PDU_ADV_CHSEL_MASK)
    header = (
        AdvertisingPduType.CONNECT_IND
        | (PDU_ADV_CHSEL_MASK if csa2 else 0)
        | (PDU_ADV_TXADD_MASK if item.address.is_random else 0)
        | (PDU_ADV_RXADD_MASK if peer.is_random else 0)
    )
    pdu = (
        bytes([header, PDU_ADV_CONNECT_PAYLOAD_SIZE])
        + bytes(item.address)
        + bytes(peer)
        + parameters.encode()
    )
    end_us = await link.transmit(channel, pdu, start_us=start_us)
    link.roles.accepted(
        ConnectionAccepted(
            parameters,
            peer,
            Role.CENTRAL,
            end_us,
            self_address=item.address,
            local_rpa=local_rpa,
            csa2=csa2,
            initiator=item,
        )
    )


def accepts_extended_advertisement(
    link: RadioLink,
    item: Initiating,
    identity: Address,
    *,
    target: Address | None,
    target_matches: bool,
    decision: bool,
) -> bool:
    """Apply the initiator's directed address and filter policy rules."""
    if not target_matches or (
        target is not None
        and OwnAddressType(item.own_address_type).uses_resolvable_address
        and not target.is_resolvable
    ):
        return False
    policy = InitiatorFilterPolicy(item.filter_policy)
    if decision:
        return (
            policy != InitiatorFilterPolicy.FILTER_ACCEPT_LIST_ALL_PDUS
            or identity in link.accept_list
        )
    if policy.filters_non_decision_pdus:
        return identity in link.accept_list
    return identity == item.peer


async def initiate_extended(
    link: RadioLink, item: Initiating, channel: int, packet: Received, address: Address
) -> None:
    """Start an AUX_CONNECT exchange and back off on an unanswered request."""
    if link.initiating is not item or not link.roles.connections.can_accept(address):
        return
    phy = packet.phy
    start_us = link.interframe_slot(
        channel,
        packet.end_us,
        connect_exchange_us(phy) + LL_RESPONSE_START_TOLERANCE_US,
        phy,
    )
    # Core Vol 6, Part B, 4.4.4.2 reuses the scanner backoff.
    if start_us is None or not item.backoff.permit():
        return
    own, generated = link.privacy.select_local_address(
        item.own_address_type,
        item.identity_address or item.address,
        link.privacy.identity(address),
    )
    item.address = own
    parameters = link.connection_parameters(
        (item.phy_parameters or {}).get(phy, item.parameters),
        start_us + phy.airtime_us(PDU_ADV_CONNECT_SIZE),
        auxiliary_window_delay_us(phy),
        phy,
    )
    if parameters is None:
        return

    end_us = await link.transmit(
        channel,
        connect_request(own, address, parameters),
        start_us=start_us,
        access_address=PDU_ADV_ACCESS_ADDRESS,
        crc_init=PDU_ADV_CRC_INIT,
        phy=phy,
        coding=PHY_CODED_S8_FACTOR,
    )
    if link.initiating is not item:
        return
    response = await link.receive_response(
        channel, end_us, phy, deadline_us=link.resource.deadline_us
    )
    if link.initiating is not item:
        return
    result = _accept_auxiliary_response(
        link, response, address, own, generated, item, parameters, end_us, phy
    )
    item.backoff.complete(result is not None, link.random)
    if result is not None:
        link.roles.accepted(result)


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
    state = item.pawr_state

    def current() -> bool:
        return (
            link.initiating is initiating
            and state is not None
            and item.pawr_state is state
            and link.roles.advertising.owns(item)
        )

    if not current():
        return
    peer = initiating.peer

    def failed() -> None:
        link.roles.scanning.cancel_initiating()
        link.listener.on_connect_failed(
            peer, ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR
        )

    phy, coding = item.secondary_phy, item.secondary_coding.factor()
    own, generated = link.privacy.select_local_address(
        initiating.own_address_type,
        initiating.identity_address or initiating.address,
        link.privacy.identity(peer),
    )
    initiating.address = own
    request_end_us = start_us + phy.airtime_us(PDU_ADV_CONNECT_SIZE, coding)
    # HCI permits a supplied PHY's connection parameters as fallback (4.E.7.8.66).
    parameters = link.connection_parameters(
        (initiating.phy_parameters or {}).get(phy, initiating.parameters),
        request_end_us,
        auxiliary_window_delay_us(phy),
        phy,
    )
    if parameters is None or not link.roles.connections.can_accept(peer):
        failed()
        return
    if not link.resource.admit(
        item,
        start_us - RADIO_SWITCH_TIME_US,
        start_us + connect_exchange_us(phy, request_coding=coding),
        RadioPriority.PERIODIC_TX,
        earliest_us=link.radio.earliest_start_us(channel, phy, transmit=True)
        - RADIO_SWITCH_TIME_US,
    ):
        failed()
        return

    access_address, crc_init = item.periodic_aa, item.periodic_crc
    end_us = await link.transmit(
        channel,
        connect_request(own, peer, parameters),
        start_us=start_us,
        access_address=access_address,
        crc_init=crc_init,
        phy=phy,
        coding=coding,
        power_dbm=item.tx_power_dbm,
    )
    if not current():
        return
    response = await link.receive_response(
        channel, end_us, phy, access_address=access_address, crc_init=crc_init
    )
    if not current():
        return
    result = _accept_auxiliary_response(
        link,
        response,
        peer,
        own,
        generated,
        initiating,
        parameters,
        end_us,
        phy,
        pawr_advertiser=item,
    )
    if result is None:
        failed()
    else:
        link.roles.accepted(result)


def _accept_auxiliary_response(
    link: RadioLink,
    response: Received | None,
    peer: Address,
    own: Address,
    generated: bool,
    initiating: Initiating,
    parameters: Parameters,
    end_us: int,
    phy: Phy,
    *,
    pawr_advertiser: Advertising | None = None,
) -> ConnectionAccepted | None:
    address = _response_peer(
        link, response, peer, own, allow_rpa_change=pawr_advertiser is None
    )
    if address is None:
        return None
    return ConnectionAccepted(
        parameters,
        address,
        Role.CENTRAL,
        end_us,
        self_address=own,
        local_rpa=own if generated else None,
        csa2=True,
        window_delay_us=auxiliary_window_delay_us(phy),
        phy=phy,
        initiator=initiating,
        pawr_advertiser=pawr_advertiser,
    )


def _response_peer(
    link: RadioLink,
    response: Received | None,
    peer: Address,
    initiator: Address,
    *,
    allow_rpa_change: bool,
) -> Address | None:
    if response is None or not response.valid_crc:
        return None
    try:
        pdu = ExtendedPdu.decode(response.pdu)
    except ValueError:
        return None
    if (
        pdu.kind != AdvertisingPduType.AUX_CONNECT_RSP
        or pdu.mode
        or pdu.address is None
        or pdu.target != bytes(initiator)
        or pdu.random_target != initiator.is_random
    ):
        return None
    address = Address(pdu.address, AddressType(pdu.random_address))
    if address == peer:
        return address
    if allow_rpa_change and link.privacy.enabled and address.is_resolvable:
        # Extended initiating can recognize a new AdvA from the same identity.
        # Keep the latest wire address for Enhanced Connection Complete (4.E.7.7.65.10).
        resolved = link.privacy.identity(address, remember=False)
        if (
            resolved is not None
            and resolved.address_type
            in (AddressType.PUBLIC_IDENTITY, AddressType.RANDOM_IDENTITY)
            and resolved == link.privacy.identity(peer, remember=False)
        ):
            return address
    return None
