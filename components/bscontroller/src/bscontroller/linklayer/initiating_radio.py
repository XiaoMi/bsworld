# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connection initiation on primary and secondary advertising channels."""

from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING

from bscontroller.address import Address, AddressType, OwnAddressType
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    LL_IFS_US,
    PDU_ADV_ACCESS_ADDRESS,
    PDU_ADV_ADDRESS_OFFSET,
    PDU_ADV_ADDRESSES_END,
    PDU_ADV_AUX_CONNECT_RSP_SIZE,
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
from bscontroller.linklayer.timing import auxiliary_window_delay_us
from bscontroller.radio.base import PHY_CODED_S8_FACTOR, RADIO_SWITCH_TIME_US

if TYPE_CHECKING:
    from bscontroller.linklayer.discovery import Advertising, Initiating
    from bscontroller.linklayer.link import RadioLink
    from bscontroller.radio.base import Phy, Received


class _AuxiliaryConnectionFailure(Enum):
    NOT_SENT = auto()
    NO_RESPONSE = auto()
    CANCELLED = auto()


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
    slot = link.interframe_slot(channel, packet.end_us, PDU_ADV_CONNECT_SIZE)
    if slot is None:
        return
    start_us, request_end = slot
    # Choose a usable first connection window before sending CONNECT_IND;
    # its timing becomes shared state as soon as the peer accepts the request.
    parameters = link.connection_parameters(
        item.parameters, request_end, LL_CONN_INTERVAL_UNIT_US
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
    if not link.roles.connections.can_accept(address):
        return
    slot = link.interframe_slot(
        channel,
        packet.end_us,
        PDU_ADV_CONNECT_SIZE,
        packet.phy,
        response_size=PDU_ADV_AUX_CONNECT_RSP_SIZE,
    )
    # Core Vol 6, Part B, 4.4.4.2 reuses the scanner backoff.
    if slot is None or not item.backoff.permit():
        return
    result = await _request_auxiliary_connection(
        link, item, address, channel, slot[0], packet.phy
    )
    if isinstance(result, ConnectionAccepted):
        item.backoff.complete(True, link.random)
        link.roles.accepted(result)
    elif result == _AuxiliaryConnectionFailure.NO_RESPONSE:
        item.backoff.complete(False, link.random)


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
    result = await _request_auxiliary_connection(
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
    elif result != _AuxiliaryConnectionFailure.CANCELLED:
        link.roles.scanning.cancel_initiating()
        link.listener.on_connect_failed(
            initiating.peer, ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR
        )


async def _request_auxiliary_connection(
    link: RadioLink,
    initiating: Initiating,
    peer: Address,
    channel: int,
    start_us: int,
    phy: Phy,
    *,
    pawr_advertiser: Advertising | None = None,
) -> ConnectionAccepted | _AuxiliaryConnectionFailure:
    """Run one exchange and return its validated ACL parameters to the caller."""
    state = pawr_advertiser.pawr_state if pawr_advertiser is not None else None

    def current() -> bool:
        return link.initiating is initiating and (
            pawr_advertiser is None
            or (
                state is not None
                and pawr_advertiser.pawr_state is state
                and link.roles.advertising.owns(pawr_advertiser)
            )
        )

    if not current():
        return _AuxiliaryConnectionFailure.CANCELLED
    if pawr_advertiser is None:
        access_address, crc_init = PDU_ADV_ACCESS_ADDRESS, PDU_ADV_CRC_INIT
        coding, power_dbm = PHY_CODED_S8_FACTOR, 0
    else:
        access_address, crc_init = (
            pawr_advertiser.periodic_aa,
            pawr_advertiser.periodic_crc,
        )
        coding = pawr_advertiser.secondary_coding.factor()
        power_dbm = pawr_advertiser.tx_power_dbm
    own, generated = link.privacy.select_local_address(
        initiating.own_address_type,
        initiating.identity_address or initiating.address,
        link.privacy.identity(peer),
    )
    initiating.address = own
    window_delay = auxiliary_window_delay_us(phy)
    end_us = start_us + phy.airtime_us(PDU_ADV_CONNECT_SIZE, coding)
    # HCI permits a supplied PHY's connection parameters as fallback (4.E.7.8.66).
    parameters = link.connection_parameters(
        (initiating.phy_parameters or {}).get(phy, initiating.parameters),
        end_us,
        window_delay,
        phy,
    )
    if parameters is None:
        return _AuxiliaryConnectionFailure.NOT_SENT
    if pawr_advertiser is not None and (
        not link.roles.connections.can_accept(peer)
        or not link.resource.admit(
            pawr_advertiser,
            start_us - RADIO_SWITCH_TIME_US,
            end_us + LL_IFS_US + phy.airtime_us(PDU_ADV_AUX_CONNECT_RSP_SIZE),
            RadioPriority.PERIODIC_TX,
            earliest_us=link.radio.earliest_start_us(channel, phy, transmit=True)
            - RADIO_SWITCH_TIME_US,
        )
    ):
        return _AuxiliaryConnectionFailure.NOT_SENT

    end_us = await link.transmit(
        channel,
        connect_request(own, peer, parameters),
        start_us=start_us,
        access_address=access_address,
        crc_init=crc_init,
        phy=phy,
        coding=coding,
        power_dbm=power_dbm,
    )
    if not current():
        return _AuxiliaryConnectionFailure.CANCELLED
    response = await link.receive_response(
        channel,
        end_us,
        phy,
        access_address=access_address,
        crc_init=crc_init,
        deadline_us=link.resource.deadline_us if pawr_advertiser is None else None,
    )
    if not current():
        return _AuxiliaryConnectionFailure.CANCELLED
    address = _response_peer(
        link, response, peer, own, allow_rpa_change=pawr_advertiser is None
    )
    if address is None:
        return _AuxiliaryConnectionFailure.NO_RESPONSE
    return ConnectionAccepted(
        parameters,
        address,
        Role.CENTRAL,
        end_us,
        self_address=own,
        local_rpa=own if generated else None,
        csa2=True,
        window_delay_us=window_delay,
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
