# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Advertising reception, scan requests, and delivery to interested roles."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from bscontroller.address import Address, AddressType
from bscontroller.linklayer import (
    PDU_ADV_ADDRESS_OFFSET,
    PDU_ADV_ADDRESS_SIZE,
    PDU_ADV_ADDRESSES_END,
    PDU_ADV_ADI_DID_BITS,
    PDU_ADV_DIRECT_SIZE,
    PDU_ADV_EXT_PAYLOAD_SIZE_MAX,
    PDU_ADV_LEGACY_SIZE_MAX,
    PDU_ADV_LEGACY_SIZE_MIN,
    PDU_ADV_RXADD_MASK,
    PDU_ADV_SCAN_REQ_SIZE,
    PDU_ADV_SECOND_ADDRESS_OFFSET,
    PDU_ADV_TXADD_MASK,
    PDU_ADV_TYPE_MASK,
    PDU_HEADER_SIZE,
    AdvertisingMode,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising_pdu import ExtendedPdu, scan_request
from bscontroller.linklayer.extended_receive import collect_chain, follow_auxiliary
from bscontroller.linklayer.filtering import (
    DecisionMode,
    InitiatorFilterPolicy,
    ScanningFilterPolicy,
    scanning_decision_mode,
)
from bscontroller.linklayer.initiating_radio import (
    accepts_extended_advertisement,
    initiate_extended,
    initiate_legacy,
)
from bscontroller.linklayer.results import SyncInfoReceived
from bscontroller.linklayer.state import AdvertisingProperties, AdvertisingReport
from bscontroller.radio.base import Phy

if TYPE_CHECKING:
    from bscontroller.linklayer.discovery import Initiating, PeriodicSync, Scanning
    from bscontroller.linklayer.link import RadioLink
    from bscontroller.linklayer.results import ScanEvent
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


async def receive_scan_event(link: RadioLink, event: ScanEvent) -> None:
    """Execute a prepared window without deciding the scanning role's lifetime."""
    if event.channel is None:
        await link.advance_idle_to_us(event.end_us)
        return
    # A single RX can serve both scanning and periodic sync acquisition. Each
    # recipient must still own its request when the primary or auxiliary PDU arrives.
    scanner = link.scan
    sync = link.sync if scanner is not None else None
    packet = await link.radio.receive(
        event.channel,
        event.end_us,
        phy=event.phy,
        deadline_us=link.resource.deadline_us,
    )
    if not packet or not packet.pdu:
        return
    initiator = event.initiator
    if initiator is not None and initiator is not link.initiating:
        return
    if scanner is not link.scan:
        scanner = None
    if sync is not link.sync:
        sync = None
    if packet.pdu[0] & PDU_ADV_TYPE_MASK in (
        AdvertisingPduType.ADV_EXT_IND,
        AdvertisingPduType.ADV_DECISION_IND,
    ):
        if scanner is not None and not scanner.extended:
            scanner = None
        if initiator is None and scanner is None and sync is None:
            return
        try:
            await scan_extended(
                link, packet, scan=scanner, sync=sync, initiator=initiator
            )
        except ValueError as error:
            link.trace(f'LL EXTENDED REJECTED {error}')
    elif initiator is not None:
        await initiate_legacy(link, event.channel, packet, initiator)
    elif scanner is not None:
        await scan_packet(link, event.channel, packet, scanner)


async def scan_packet(
    link: RadioLink, channel: int, packet: Received, scan: Scanning
) -> None:
    """Report a legacy advertising PDU and run active scanning with backoff (Core Vol 6,
    Part B, 4.4.3, 4.3.3).
    """
    pdu = packet.pdu
    if len(pdu) < PDU_HEADER_SIZE or pdu[1] != len(pdu) - PDU_HEADER_SIZE:
        return
    if scanning_decision_mode(scan.filter_policy) == DecisionMode.DECISION_PDUS_ONLY:
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
    # Monitoring is independent of the scanner's Filter Accept List
    # (Core Vol 6, Part B, 4.4.3.8).
    link.monitoring.received(identity, packet.rssi)
    if link.scan is not scan or (
        scan.filter_policy & ScanningFilterPolicy.FILTER_ACCEPT_LIST
        and identity not in link.accept_list
    ):
        return
    if pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.ADV_DIRECT_IND:
        if len(pdu) != PDU_ADV_DIRECT_SIZE:
            return
        target = Address(
            pdu[PDU_ADV_SECOND_ADDRESS_OFFSET:PDU_ADV_ADDRESSES_END],
            AddressType(bool(pdu[0] & PDU_ADV_RXADD_MASK)),
        )
        if not link.privacy.accept_target(target, scan.address, address) and not (
            scan.filter_policy & ScanningFilterPolicy.ACCEPT_ALL_DIRECTED_RPAS
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
                target, scan.identity_address or scan.address, address
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
        link.scan is not scan
        or not scan.active
        or pdu[0] & PDU_ADV_TYPE_MASK
        not in (AdvertisingPduType.ADV_IND, AdvertisingPduType.ADV_SCAN_IND)
    ):
        return
    # The response length is unknown when the scanner commits to SCAN_REQ.
    slot = link.interframe_slot(
        channel,
        packet.end_us,
        PDU_ADV_SCAN_REQ_SIZE,
        response_size=PDU_ADV_LEGACY_SIZE_MAX,
    )
    if slot is not None and scan.backoff.permit():
        # Consume backoff only for an eligible advertisement. A missing response
        # or one from another advertiser counts as a failed attempt.
        scan.address = link.privacy.local_address(
            scan.own_address_type,
            scan.identity_address or scan.address,
            identity,
        )
        request = scan_request(scan.address, address)
        end_us = await link.transmit(channel, request, start_us=slot[0])
        if link.scan is not scan:
            return
        response = await link.receive_response(
            channel, end_us, deadline_us=link.resource.deadline_us
        )
        if link.scan is not scan:
            return
        success = bool(
            response
            and PDU_ADV_LEGACY_SIZE_MIN <= len(response.pdu) <= PDU_ADV_LEGACY_SIZE_MAX
            and response.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.SCAN_RSP
            and response.pdu[1] == len(response.pdu) - PDU_HEADER_SIZE
            and response.pdu[PDU_ADV_ADDRESS_OFFSET:PDU_ADV_SECOND_ADDRESS_OFFSET]
            == bytes(address)
            and bool(response.pdu[0] & PDU_ADV_TXADD_MASK) == address.is_random
        )
        scan.backoff.complete(success, link.random)
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
    if initiator is not None:
        if (
            channel is not None
            and ext.mode == AdvertisingMode.CONNECTABLE
            and accepts_extended_advertisement(
                link,
                initiator,
                identity,
                target=target,
                target_matches=target_matches,
                decision=decision,
            )
        ):
            await initiate_extended(link, initiator, channel, received, address)
        return
    if (
        target is not None
        and not target_matches
        and (
            scan is None
            or not scan.filter_policy & ScanningFilterPolicy.ACCEPT_ALL_DIRECTED_RPAS
            or not target.is_resolvable
        )
    ):
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
    slot = link.interframe_slot(
        channel,
        received.end_us,
        PDU_ADV_SCAN_REQ_SIZE,
        received.phy,
        response_size=PDU_HEADER_SIZE + PDU_ADV_EXT_PAYLOAD_SIZE_MAX,
    )
    if slot is None or not scan.backoff.permit():
        return None
    scan.address = link.privacy.local_address(
        scan.own_address_type, scan.identity_address or scan.address, identity
    )
    end_us = await link.transmit(
        channel,
        scan_request(scan.address, address),
        start_us=slot[0],
        phy=received.phy,
    )
    if link.scan is not scan:
        return None
    response = await link.receive_response(
        channel, end_us, received.phy, deadline_us=link.resource.deadline_us
    )
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
