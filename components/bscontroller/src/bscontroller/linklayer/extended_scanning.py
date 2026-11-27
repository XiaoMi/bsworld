# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Extended scanning and initiating (Core Vol 6, Part B, 4.4.3 and 4.4.4)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from bscontroller.address import Address, AddressType, OwnAddressType
from bscontroller.linklayer import (
    LL_CONN_AUX_WINDOW_DELAY_CODED_US,
    LL_CONN_AUX_WINDOW_DELAY_UNCODED_US,
    LL_IFS_US,
    PDU_ADV_ADDRESS_SIZE,
    PDU_ADV_ADI_DID_BITS,
    PDU_ADV_AUX_CONNECT_RSP_SIZE,
    PDU_ADV_CONNECT_SIZE,
    AdvertisingMode,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising_pdu import ExtendedPdu, scan_request
from bscontroller.linklayer.extended import connect_request
from bscontroller.linklayer.extended_receive import collect_chain, follow_auxiliary
from bscontroller.linklayer.filtering import (
    DecisionMode,
    InitiatorFilterPolicy,
    ScanningFilterPolicy,
    scanning_decision_mode,
)
from bscontroller.linklayer.results import ConnectionAccepted, SyncInfoReceived
from bscontroller.linklayer.state import AdvertisingProperties, AdvertisingReport, Role
from bscontroller.radio.base import Phy

if TYPE_CHECKING:
    from bscontroller.linklayer.discovery import Initiating, PeriodicSync, Scanning
    from bscontroller.linklayer.link import RadioLink
    from bscontroller.radio.base import Received


@dataclass(frozen=True)
class _Observation:
    primary: Received
    packet: Received
    pdu: ExtendedPdu
    channel: int | None
    address: Address
    identity: Address
    sid: int | None


def _decode_primary(
    link: RadioLink, packet: Received, mode: DecisionMode
) -> ExtendedPdu | None:
    # Primary SyncInfo,CTEInfo,ACAD and AdvData are context-reserved CEAP fields.
    primary = ExtendedPdu.decode(packet.pdu)
    decision = primary.kind == AdvertisingPduType.ADV_DECISION_IND
    if (
        decision
        and (
            mode == DecisionMode.IGNORE_DECISION_PDUS
            or not link.decision.accepts(primary, packet.rssi)
        )
    ) or (not decision and mode == DecisionMode.DECISION_PDUS_ONLY):
        return None
    if decision and (
        primary.aux_ptr is None
        or primary.target is not None
        or primary.sync_info is not None
        or primary.cte is not None
        or primary.acad
    ):
        return None
    # Core Vol 6, Part B, 2.3.1.5, Table 2.4: without AuxPtr, ADI is
    # reserved too. Ignore it before deriving SID or duplicate identity;
    # its physical presence must not manufacture a Host advertising SID.
    return replace(
        primary,
        adi=primary.adi if primary.aux_ptr is not None else None,
        sync_info=None,
        cte=None,
        acad=b'',
        data=b'',
    )


def _decode_auxiliary(
    primary: ExtendedPdu,
    received: Received,
    primary_addresses_valid: bool,
) -> ExtendedPdu | None:
    ext = ExtendedPdu.decode(received.pdu)
    if (
        ext.kind != AdvertisingPduType.AUX_ADV_IND
        or ext.mode != primary.mode
        or (primary.adi is not None and ext.adi != primary.adi)
    ):
        return None
    # Table 2.5 C.1/C.2: repeated auxiliary addresses are RFU.
    if primary_addresses_valid and primary.address is not None:
        ext = replace(
            ext,
            address=primary.address,
            random_address=primary.random_address,
        )
    if primary_addresses_valid and primary.target is not None:
        ext = replace(
            ext,
            target=primary.target,
            random_target=primary.random_target,
        )
    if ext.mode:
        # Core Table2.5 reserves AuxPtr/SyncInfo in connectable/scannable
        # AUX_ADV_IND.
        ext = replace(
            ext,
            aux_ptr=None,
            sync_info=None,
            data=b'' if ext.mode == AdvertisingMode.SCANNABLE else ext.data,
        )
    return ext


async def scan_extended(
    link: RadioLink,
    packet: Received,
    *,
    scan: Scanning | None,
    sync: PeriodicSync | None,
    initiator: Initiating | None,
) -> None:
    """Follow ADV_EXT_IND to its AUX_ADV_IND and report it (Core Vol 6, Part B,
    4.4.3.3, 4.4.3.4).
    """
    mode = (
        InitiatorFilterPolicy(initiator.filter_policy).decision_mode
        if initiator is not None
        else scanning_decision_mode(scan.filter_policy)
        if scan is not None
        else DecisionMode.IGNORE_DECISION_PDUS
    )
    primary = _decode_primary(link, packet, mode)
    if primary is None:
        return
    decision = primary.kind == AdvertisingPduType.ADV_DECISION_IND
    primary_addresses_valid = decision or (not primary.mode and packet.phy == Phy.LE_1M)
    primary_identity = None
    if (
        initiator is None
        and scan is not None
        and link.monitoring.enabled
        and primary.aux_ptr is not None
        and primary_addresses_valid
        and primary.address is not None
    ):
        address = Address(primary.address, AddressType(primary.random_address))
        primary_identity = link.privacy.identity(address)
        if primary_identity is not None:
            # A valid addressed primary PDU is an observation even if its
            # auxiliary PDU is missed (Vol 6, Part B, 4.4.3.8).
            link.monitoring.received(primary_identity, packet.rssi)
            if link.scan is not scan and (sync is None or sync is not link.sync):
                return
    ext, received, channel = primary, packet, None
    if primary.aux_ptr:
        channel = primary.aux_ptr.channel
        received = await follow_auxiliary(link.radio, link.resource, packet, primary)
        if received is None:
            return
        if initiator is not None and initiator is not link.initiating:
            return
        if scan is not link.scan:
            scan = None
        if (
            initiator is None
            and scan is None
            and (sync is None or sync is not link.sync)
        ):
            return
        ext = _decode_auxiliary(primary, received, primary_addresses_valid)
        if ext is None:
            return
    if ext.address is None and (ext.mode or ext.target is not None):
        return
    address = (
        Address(ext.address, AddressType(ext.random_address))
        if ext.address is not None
        else Address(bytes(PDU_ADV_ADDRESS_SIZE), AddressType.ANONYMOUS)
    )
    identity = primary_identity or link.privacy.identity(address)
    if identity is None:
        return
    if initiator is None and scan is not None and ext.address is not None:
        link.monitoring.received(identity, received.rssi)
    target = Address(ext.target, AddressType(ext.random_target)) if ext.target else None
    local = initiator or scan
    target_matches = target is None or (
        local is not None and link.privacy.accept_target(target, local.address, address)
    )
    if (
        target is not None
        and not target_matches
        and (
            local is None
            or initiator is not None
            or not local.filter_policy & ScanningFilterPolicy.ACCEPT_ALL_DIRECTED_RPAS
            or not target.is_resolvable
        )
    ):
        return
    if initiator is not None:
        # Initiation consumes only eligible connectable auxiliary events;
        # it does not produce the scanner reports or active-scan exchange below.
        if (
            target is not None
            and OwnAddressType(initiator.own_address_type).uses_resolvable_address
            and not target.is_resolvable
        ):
            return
        policy = InitiatorFilterPolicy(initiator.filter_policy)
        accepted = (
            (
                identity in link.accept_list
                if policy.filters_non_decision_pdus
                else identity == initiator.peer
            )
            if not decision
            else (
                identity in link.accept_list
                if policy == InitiatorFilterPolicy.FILTER_ACCEPT_LIST_ALL_PDUS
                else True
            )
        )
        if ext.mode == AdvertisingMode.CONNECTABLE and channel is not None and accepted:
            await initiate_extended(link, initiator, channel, received, address)
        return
    sid = ext.adi >> PDU_ADV_ADI_DID_BITS if ext.adi is not None else None
    info = ext.sync_info
    if sync is not None and link.sync is sync and info is not None and sid is not None:
        link.roles.synchronization.acquired(
            SyncInfoReceived(identity, sid, info, received, address, ext.acad)
        )
    if (
        scan is None
        or link.scan is not scan
        or (
            scan.filter_policy & ScanningFilterPolicy.FILTER_ACCEPT_LIST
            and identity not in link.accept_list
        )
    ):
        return

    observation = _Observation(packet, received, ext, channel, address, identity, sid)
    await _report_advertisement(link, scan, observation, target, target_matches)


async def _report_advertisement(
    link: RadioLink,
    scan: Scanning,
    observed: _Observation,
    target: Address | None,
    target_matches: bool,
) -> None:
    """Report the received advertisement and any response to an active scan."""
    ext, channel = observed.pdu, observed.channel

    def scanning():
        return link.scan is scan

    data, status, power = await collect_chain(
        link.radio, link.resource, observed.packet, ext, is_current=scanning
    )
    if not scanning():
        return
    if target is not None:
        target = link.privacy.report_target(
            target, scan.identity_address or scan.address, observed.address
        )
    flags = AdvertisingProperties(ext.mode)
    if target is not None:
        flags |= AdvertisingProperties.DIRECTED_ADVERTISING

    def report(packet, data, status, power, *, response=False):
        link.listener.on_advertisement(
            observed.identity,
            data,
            packet.rssi,
            AdvertisingReport(
                flags | AdvertisingProperties.SCAN_RESPONSE if response else flags,
                status,
                primary_coding=observed.primary.coding,
                secondary_coding=packet.coding if channel is not None else 0,
                adi=ext.adi,
            ),
            observed.sid,
            ext.sync_info.interval if ext.sync_info and not response else 0,
            power,
            target=target,
            primary_phy=observed.primary.phy,
            secondary_phy=observed.packet.phy if channel is not None else 0,
        )

    report(observed.packet, data, status, power)
    if (
        not scanning()
        or not scan.active
        or ext.mode != AdvertisingMode.SCANNABLE
        or not target_matches
        or channel is None
        or not scan.backoff.permit()
    ):
        return
    result = await _request_scan_response(
        link, scan, channel, observed.packet, ext, observed.address, observed.identity
    )
    if result is not None:
        packet, response = result
        data, status, power = await collect_chain(
            link.radio, link.resource, packet, response, is_current=scanning
        )
        if scanning():
            report(packet, data, status, power, response=True)


async def _request_scan_response(
    link: RadioLink, scan, channel, received, ext, address, identity
):
    """Send AUX_SCAN_REQ and bind AUX_SCAN_RSP to this advertiser and ADI."""
    scan.address = link.privacy.local_address(
        scan.own_address_type, scan.identity_address or scan.address, identity
    )
    end_us = await link.transmit(
        channel,
        scan_request(scan.address, address),
        start_us=received.end_us + LL_IFS_US,
        phy=received.phy,
    )
    if link.scan is not scan:
        return None
    response = await link.receive_response(channel, end_us, received.phy)
    if link.scan is not scan:
        return None
    try:
        rsp = ExtendedPdu.decode(response.pdu) if response else None
    except ValueError:
        rsp = None
    if (
        rsp is not None
        and rsp.kind == AdvertisingPduType.AUX_SCAN_RSP
        and not rsp.mode
        and rsp.address == ext.address
        and rsp.random_address == ext.random_address
        and (rsp.adi is None or rsp.adi == ext.adi)
    ):
        scan.backoff.complete(True, link.random)
        return response, rsp
    scan.backoff.complete(False, link.random)
    return None


async def initiate_extended(link: RadioLink, item, channel, packet, address):
    """AUX_CONNECT_REQ / AUX_CONNECT_RSP exchange on the secondary channel (Core Vol
    6, Part B, 4.4.4.2).
    """
    if not link.roles.connections.can_accept(address):
        return
    # 4.4.4.2 reuses the scanner backoff after an unanswered AUX_CONNECT_REQ.
    if not item.backoff.permit():
        return
    item.address, generated = link.privacy.select_local_address(
        item.own_address_type,
        item.identity_address or item.address,
        link.privacy.identity(address),
    )
    local_rpa = item.address if generated else None
    # Core Vol 4, Part E, 7.8.66 permits a supplied PHY's parameters as fallback.
    parameters = (item.phy_parameters or {}).get(packet.phy, item.parameters)
    window_delay = (
        LL_CONN_AUX_WINDOW_DELAY_CODED_US
        if packet.phy == Phy.LE_CODED
        else LL_CONN_AUX_WINDOW_DELAY_UNCODED_US
    )
    request_end = (
        packet.end_us + LL_IFS_US + packet.phy.airtime_us(PDU_ADV_CONNECT_SIZE)
    )
    parameters = link.connection_parameters(
        parameters, request_end, window_delay, packet.phy
    )
    if parameters is None or not link.resource.fits_radio(
        request_end + LL_IFS_US + packet.phy.airtime_us(PDU_ADV_AUX_CONNECT_RSP_SIZE)
    ):
        return
    request = connect_request(item.address, address, parameters)
    request_end = await link.transmit(
        channel, request, start_us=packet.end_us + LL_IFS_US, phy=packet.phy
    )
    if link.initiating is not item:
        return
    response = await link.receive_response(channel, request_end, packet.phy)
    if link.initiating is not item:
        return
    if response is None:
        item.backoff.complete(False, link.random)
        return
    try:
        rsp = ExtendedPdu.decode(response.pdu)
    except ValueError:
        item.backoff.complete(False, link.random)
        return
    response_address = (
        Address(rsp.address, AddressType(rsp.random_address))
        if rsp.address is not None
        else None
    )
    # Device privacy permits a peer to rotate its RPA before responding.
    # Bind a changed AdvA to the same resolving record, then retain the most
    # recent wire address for Enhanced Connection Complete (7.7.65.10).
    same_peer = response_address == address or (
        link.privacy.enabled
        and response_address is not None
        and response_address.is_resolvable
        and (resolved := link.privacy.identity(response_address)) is not None
        and resolved.address_type
        in (AddressType.PUBLIC_IDENTITY, AddressType.RANDOM_IDENTITY)
        and resolved == link.privacy.identity(address)
    )
    if (
        rsp.kind != AdvertisingPduType.AUX_CONNECT_RSP
        or rsp.mode
        or not same_peer
        or rsp.target != bytes(item.address)
        or rsp.random_target != item.address.is_random
    ):
        item.backoff.complete(False, link.random)
        return
    # Keep the attempt pending on a missing or mismatched response. Only the
    # confirmed AUX_CONNECT_RSP lets us commit this connection.
    item.backoff.complete(True, link.random)
    link.roles.accepted(
        ConnectionAccepted(
            parameters,
            response_address,
            Role.CENTRAL,
            request_end,
            csa2=True,
            self_address=item.address,
            local_rpa=local_rpa,
            window_delay_us=window_delay,
            phy=packet.phy,
            initiator=item,
        )
    )
