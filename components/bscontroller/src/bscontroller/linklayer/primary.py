# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Legacy primary-channel advertising, scanning and initiating RF procedures.

These stateless operations execute on the owning link's one radio and use its
admission and lifetime transitions. They never start an independent task.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bscontroller.address import Address, AddressType, OwnAddressType
from bscontroller.linklayer import (
    LL_ADV_PRIMARY_CHANNELS,
    LL_CONN_INTERVAL_UNIT_US,
    LL_IFS_US,
    PDU_ADV_ADDRESS_OFFSET,
    PDU_ADV_ADDRESS_SIZE,
    PDU_ADV_ADDRESSES_END,
    PDU_ADV_CHSEL_MASK,
    PDU_ADV_CONNECT_PAYLOAD_SIZE,
    PDU_ADV_CONNECT_SIZE,
    PDU_ADV_DIRECT_SIZE,
    PDU_ADV_LEGACY_SIZE_MAX,
    PDU_ADV_LEGACY_SIZE_MIN,
    PDU_ADV_RXADD_MASK,
    PDU_ADV_SCAN_REQ_SIZE,
    PDU_ADV_SECOND_ADDRESS_OFFSET,
    PDU_ADV_TXADD_MASK,
    PDU_ADV_TYPE_MASK,
    PDU_HEADER_SIZE,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising import (
    advertising_request_peer,
    scan_request,
)
from bscontroller.linklayer.discovery import Advertising
from bscontroller.linklayer.extended import advertising_time_us
from bscontroller.linklayer.filtering import (
    DecisionMode,
    InitiatorFilterPolicy,
    ScanningFilterPolicy,
    scanning_decision_mode,
)
from bscontroller.linklayer.parameters import Parameters
from bscontroller.linklayer.results import ConnectionAccepted
from bscontroller.linklayer.state import (
    LL_ADV_FILTER_CONNECT_MASK,
    LL_ADV_FILTER_SCAN_MASK,
    AdvertisingProperties,
    AdvertisingReport,
    Role,
)
from bscontroller.linklayer.timing import LL_RESPONSE_START_TOLERANCE_US
from bscontroller.radio.base import RADIO_SWITCH_TIME_US, Phy, Received

if TYPE_CHECKING:
    from bscontroller.linklayer.link import RadioLink
    from bscontroller.linklayer.results import ScanEvent


async def receive_scan_event(link: RadioLink, event: ScanEvent) -> None:
    """Execute a prepared window without deciding the scanning role's lifetime."""
    if event.channel is None:
        await link.advance_idle_to_us(event.end_us)
        return
    packet = await link.radio.receive(event.channel, event.end_us, phy=event.phy)
    if packet:
        if event.initiator is not None:
            # A Host cancellation/restart may occur while waiting for RX. An old
            # window cannot initiate on behalf of the newly configured request.
            if link.initiating is not event.initiator:
                return
            await initiate(link, event.channel, packet)
        else:
            await scan_packet(link, event.channel, packet)


def advertising_event_time_us(item: Advertising) -> int:
    """Reserve a complete primary event, including possible request exchanges."""
    if item.extended:
        return advertising_time_us(item) + (
            LL_RESPONSE_START_TOLERANCE_US if item.mode else 0
        )
    payload_size = (
        PDU_HEADER_SIZE
        + PDU_ADV_ADDRESS_SIZE
        + (PDU_ADV_ADDRESS_SIZE if item.target else len(item.data))
    )
    response_us = 0
    if item.pdu_type in (
        AdvertisingPduType.ADV_IND,
        AdvertisingPduType.ADV_SCAN_IND,
    ):
        response_us = (
            2 * LL_IFS_US
            + LL_RESPONSE_START_TOLERANCE_US
            + Phy.LE_1M.airtime_us(PDU_ADV_SCAN_REQ_SIZE)
            + Phy.LE_1M.airtime_us(
                PDU_HEADER_SIZE + PDU_ADV_ADDRESS_SIZE + len(item.scan_response)
            )
        )
    elif item.pdu_type == AdvertisingPduType.ADV_DIRECT_IND:
        response_us = (
            LL_IFS_US
            + LL_RESPONSE_START_TOLERANCE_US
            + Phy.LE_1M.airtime_us(PDU_ADV_CONNECT_SIZE)
        )
    return item.channel_map.bit_count() * (
        Phy.LE_1M.airtime_us(payload_size) + RADIO_SWITCH_TIME_US + response_us
    )


async def advertising_request(
    link: RadioLink, item: Advertising, channel: int, packet: Received
) -> bool:
    """Answer SCAN_REQ with SCAN_RSP or accept CONNECT_IND after T_IFS (Core Vol 6, Part
    B, 4.4.2.3-4.4.2.5, 4.3.2).
    """
    pdu = packet.pdu
    peer = advertising_request_peer(pdu, item.address, None)
    if peer is None:
        return False
    identity = link.privacy.identity(peer)
    if identity is None or (
        item.target is not None and identity != link.privacy.identity(item.target)
    ):
        return False
    if (
        item.pdu_type in (AdvertisingPduType.ADV_IND, AdvertisingPduType.ADV_SCAN_IND)
        and pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.SCAN_REQ
        and len(pdu) == PDU_ADV_SCAN_REQ_SIZE
    ):
        if (
            item.filter_policy & LL_ADV_FILTER_SCAN_MASK
            and identity not in link.accept_list
        ):
            return False
        payload = bytes(item.address) + item.scan_response
        await link._transmit(
            channel,
            bytes(
                [
                    AdvertisingPduType.SCAN_RSP
                    | (PDU_ADV_TXADD_MASK if item.address.is_random else 0),
                    len(payload),
                ]
            )
            + payload,
            start_us=packet.end_us + LL_IFS_US,
            power_dbm=item.tx_power_dbm,
        )
    elif (
        item.pdu_type in (AdvertisingPduType.ADV_IND, AdvertisingPduType.ADV_DIRECT_IND)
        and pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.CONNECT_IND
        and len(pdu) == PDU_ADV_CONNECT_SIZE
    ):
        if not link.roles.connections.can_accept(peer):
            return False
        if (
            item.target is None
            and item.filter_policy & LL_ADV_FILTER_CONNECT_MASK
            and identity not in link.accept_list
        ):
            return False
        try:
            parameters = Parameters.decode(pdu[PDU_ADV_ADDRESSES_END:])
        except ValueError as error:
            link.trace(f'LL CONNECT REJECTED {error}')
            return False
        # The role owner disables this advertiser only after a fully validated
        # CONNECT_IND transfers the radio activity to the new connection.
        link.roles.connections.accepted(
            ConnectionAccepted(
                parameters,
                peer,
                Role.PERIPHERAL,
                packet.end_us,
                self_address=item.address,
                local_rpa=item.local_rpa,
                csa2=bool(pdu[0] & PDU_ADV_CHSEL_MASK),
                advertiser=item,
            )
        )
        return True
    return False


async def initiate(link: RadioLink, channel: int, packet: Received) -> None:
    """Send CONNECT_IND T_IFS after an accepted ADV_IND/ADV_DIRECT_IND (Core Vol 6, Part
    B, 4.4.4.1, 4.3.4).
    """
    item = link.initiating
    pdu = packet.pdu
    if pdu and pdu[0] & PDU_ADV_TYPE_MASK in (
        AdvertisingPduType.ADV_EXT_IND,
        AdvertisingPduType.ADV_DECISION_IND,
    ):
        await scan_extended(link, packet, initiating=True)
        return
    if (
        item is not None
        and item.filter_policy == InitiatorFilterPolicy.DECISION_PDUS_ONLY
    ):
        return
    if (
        item is None
        or len(pdu) < PDU_ADV_LEGACY_SIZE_MIN
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
    # Choose a usable first connection window before sending CONNECT_IND;
    # its timing becomes shared state as soon as the peer accepts the request.
    request_end = packet.end_us + LL_IFS_US + Phy.LE_1M.airtime_us(PDU_ADV_CONNECT_SIZE)
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
    end_us = await link._transmit(channel, pdu, start_us=packet.end_us + LL_IFS_US)
    link.roles.connections.accepted(
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


async def scan_packet(link: RadioLink, channel: int, packet: Received) -> None:
    """Report a legacy advertising PDU and run active scanning with backoff (Core Vol 6,
    Part B, 4.4.3, 4.3.3).
    """
    pdu = packet.pdu
    if len(pdu) < PDU_HEADER_SIZE or pdu[1] != len(pdu) - PDU_HEADER_SIZE:
        return
    decision = pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.ADV_DECISION_IND
    if pdu[0] & PDU_ADV_TYPE_MASK in (
        AdvertisingPduType.ADV_EXT_IND,
        AdvertisingPduType.ADV_DECISION_IND,
    ):
        if not decision or (link.scan is not None and link.scan.extended):
            await scan_extended(link, packet)
        return
    if (
        link.scan is not None
        and scanning_decision_mode(link.scan.filter_policy)
        == DecisionMode.DECISION_PDUS_ONLY
    ):
        return
    flags = AdvertisingProperties
    types: dict[int, AdvertisingProperties] = {
        AdvertisingPduType.ADV_IND: flags.LEGACY_ADVERTISING_PDU_USED
        | flags.CONNECTABLE_ADVERTISING
        | flags.SCANNABLE_ADVERTISING,
        AdvertisingPduType.ADV_DIRECT_IND: flags.LEGACY_ADVERTISING_PDU_USED
        | flags.CONNECTABLE_ADVERTISING
        | flags.DIRECTED_ADVERTISING,
        AdvertisingPduType.ADV_NONCONN_IND: flags.LEGACY_ADVERTISING_PDU_USED,
        AdvertisingPduType.ADV_SCAN_IND: flags.LEGACY_ADVERTISING_PDU_USED
        | flags.SCANNABLE_ADVERTISING,
    }
    if (
        not PDU_ADV_LEGACY_SIZE_MIN <= len(pdu) <= PDU_ADV_LEGACY_SIZE_MAX
        or pdu[0] & PDU_ADV_TYPE_MASK not in types
    ):
        return
    address = Address(
        pdu[PDU_ADV_ADDRESS_OFFSET:PDU_ADV_SECOND_ADDRESS_OFFSET],
        AddressType(bool(pdu[0] & PDU_ADV_TXADD_MASK)),
    )
    identity = link.privacy.identity(address)
    if identity is None:
        return
    if link.scan is not None:
        # Monitoring is independent of the scanner's Filter Accept List
        # (Core Vol 6, Part B, 4.4.3.8).
        link.monitoring.received(identity, packet.rssi)
    if (
        link.scan
        and link.scan.filter_policy & ScanningFilterPolicy.FILTER_ACCEPT_LIST
        and identity not in link.accept_list
    ):
        return
    if pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.ADV_DIRECT_IND:
        if len(pdu) != PDU_ADV_DIRECT_SIZE or link.scan is None:
            return
        target = Address(
            pdu[PDU_ADV_SECOND_ADDRESS_OFFSET:PDU_ADV_ADDRESSES_END],
            AddressType(bool(pdu[0] & PDU_ADV_RXADD_MASK)),
        )
        if not link.privacy.accept_target(target, link.scan.address, address) and not (
            link.scan.filter_policy & ScanningFilterPolicy.ACCEPT_ALL_DIRECTED_RPAS
            and target.is_resolvable
        ):
            return
        # Directed legacy advertising carries a target address instead of AdvData
        # and cannot be followed by the active-scan exchange below.
        link.listener.on_advertisement(
            identity,
            b'',
            packet.rssi,
            AdvertisingReport(types[AdvertisingPduType.ADV_DIRECT_IND]),
            None,
            0,
            None,
            target=link.privacy.report_target(
                target, link.scan.identity_address or link.scan.address, address
            ),
        )
        return
    # Report the advertisement independently of whether a scan response arrives.
    link.listener.on_advertisement(
        identity,
        pdu[PDU_ADV_SECOND_ADDRESS_OFFSET:],
        packet.rssi,
        AdvertisingReport(types[pdu[0] & PDU_ADV_TYPE_MASK]),
        None,
        0,
        None,
    )
    if (
        link.scan
        and link.scan.active
        and pdu[0] & PDU_ADV_TYPE_MASK
        in (AdvertisingPduType.ADV_IND, AdvertisingPduType.ADV_SCAN_IND)
        and link.scan.backoff.permit()
    ):
        # Consume backoff only for an eligible advertisement. A missing response
        # or one from another advertiser counts as a failed attempt.
        link.scan.address = link.privacy.local_address(
            link.scan.own_address_type,
            link.scan.identity_address or link.scan.address,
            identity,
        )
        request = scan_request(link.scan.address, address)
        end_us = await link._transmit(
            channel, request, start_us=packet.end_us + LL_IFS_US
        )
        response = await link.receive_response(channel, end_us)
        success = bool(
            response
            and PDU_ADV_LEGACY_SIZE_MIN <= len(response.pdu) <= PDU_ADV_LEGACY_SIZE_MAX
            and response.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.SCAN_RSP
            and response.pdu[1] == len(response.pdu) - PDU_HEADER_SIZE
            and response.pdu[PDU_ADV_ADDRESS_OFFSET:PDU_ADV_SECOND_ADDRESS_OFFSET]
            == bytes(address)
            and bool(response.pdu[0] & PDU_ADV_TXADD_MASK) == address.is_random
        )
        link.scan.backoff.complete(success, link.random)
        if success and response is not None:
            link.listener.on_advertisement(
                identity,
                response.pdu[PDU_ADV_SECOND_ADDRESS_OFFSET:],
                response.rssi,
                AdvertisingReport(
                    types[pdu[0] & PDU_ADV_TYPE_MASK] | flags.SCAN_RESPONSE
                ),
                None,
                0,
                None,
            )


async def scan_extended(
    link: RadioLink, packet: Received, *, initiating: bool = False
) -> None:
    try:
        await link.extended.scan(packet, initiating=initiating)
    except ValueError as error:
        link.trace(f'LL EXTENDED REJECTED {error}')


async def advertise_legacy(link: RadioLink, item: Advertising) -> None:
    """One legacy advertising event over the used primary channels in ascending order
    (Core Vol 6, Part B, 4.4.2.1-4.4.2.2).
    """
    payload = bytes(item.address) + (bytes(item.target) if item.target else item.data)
    chsel = (
        PDU_ADV_CHSEL_MASK
        if item.pdu_type
        in (AdvertisingPduType.ADV_IND, AdvertisingPduType.ADV_DIRECT_IND)
        else 0
    )
    pdu = (
        bytes(
            [
                item.pdu_type
                | chsel
                | (PDU_ADV_TXADD_MASK if item.address.is_random else 0)
                | (PDU_ADV_RXADD_MASK if item.target and item.target.is_random else 0),
                len(payload),
            ]
        )
        + payload
    )
    for bit, channel in enumerate(LL_ADV_PRIMARY_CHANNELS):
        if item.channel_map & (1 << bit):
            # Do not start a high-duty packet that would run past advertising expiry.
            if (
                item.high_duty
                and link.now_us + 1 + Phy.LE_1M.airtime_us(len(pdu)) > item.deadline_us
            ):
                await link.radio.advance_to_us(item.deadline_us)
                break
            end_us = await link._transmit(channel, pdu, power_dbm=item.tx_power_dbm)
            if item.pdu_type in (
                AdvertisingPduType.ADV_IND,
                AdvertisingPduType.ADV_DIRECT_IND,
                AdvertisingPduType.ADV_SCAN_IND,
            ):
                packet = await link.receive_response(
                    channel,
                    end_us,
                    deadline_us=item.deadline_us if item.high_duty else None,
                )
                if packet and await advertising_request(link, item, channel, packet):
                    # A connection ends the entire advertising event, including
                    # transmissions that were still due on other primary channels.
                    break
