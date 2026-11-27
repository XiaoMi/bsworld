# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""AUX_CONNECT_REQ and AUX_CONNECT_RSP on secondary advertising channels."""

from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING

from bscontroller.address import Address, AddressType
from bscontroller.linklayer import (
    LL_IFS_US,
    PDU_ADV_ACCESS_ADDRESS,
    PDU_ADV_AUX_CONNECT_RSP_SIZE,
    PDU_ADV_CONNECT_SIZE,
    PDU_ADV_CRC_INIT,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising_pdu import ExtendedPdu, connect_request
from bscontroller.linklayer.results import ConnectionAccepted
from bscontroller.linklayer.scheduler import RadioPriority
from bscontroller.linklayer.state import Role
from bscontroller.linklayer.timing import auxiliary_window_delay_us
from bscontroller.radio.base import PHY_CODED_S8_FACTOR, RADIO_SWITCH_TIME_US, Phy

if TYPE_CHECKING:
    from bscontroller.linklayer.discovery import Advertising, Initiating
    from bscontroller.linklayer.link import RadioLink
    from bscontroller.radio.base import Received


class AuxiliaryConnectionFailure(Enum):
    NOT_SENT = auto()
    NO_RESPONSE = auto()
    CANCELLED = auto()


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


async def request_auxiliary_connection(
    link: RadioLink,
    initiating: Initiating,
    peer: Address,
    channel: int,
    start_us: int,
    phy: Phy,
    *,
    pawr_advertiser: Advertising | None = None,
) -> ConnectionAccepted | AuxiliaryConnectionFailure:
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
        return AuxiliaryConnectionFailure.CANCELLED
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
        return AuxiliaryConnectionFailure.NOT_SENT
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
        return AuxiliaryConnectionFailure.NOT_SENT

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
        return AuxiliaryConnectionFailure.CANCELLED
    response = await link.receive_response(
        channel,
        end_us,
        phy,
        access_address=access_address,
        crc_init=crc_init,
        deadline_us=link.resource.deadline_us if pawr_advertiser is None else None,
    )
    if not current():
        return AuxiliaryConnectionFailure.CANCELLED
    address = _response_peer(
        link, response, peer, own, allow_rpa_change=pawr_advertiser is None
    )
    if address is None:
        return AuxiliaryConnectionFailure.NO_RESPONSE
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
