# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Auxiliary advertising, scan-response and connection transactions.

Operations use the owning RadioLink's one radio. No procedure owns an event
loop or schedules a second, concurrent radio operation.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from bscontroller.address import Address, AddressType, OwnAddressType
from bscontroller.capabilities import (
    CONTROLLER_ADV_DATA_SIZE_MAX,
)
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_ADV_PRIMARY_CHANNELS,
    LL_CHANNEL_MAP_SIZE,
    LL_CONN_AUX_WINDOW_DELAY_CODED_US,
    LL_CONN_AUX_WINDOW_DELAY_UNCODED_US,
    LL_DATA_CHANNEL_COUNT,
    LL_DATA_CHANNEL_MAP_ALL,
    LL_EVENT_COUNTER_MASK,
    LL_IFS_US,
    LL_MAFS_US,
    LL_PERIODIC_INTERVAL_MIN,
    LL_PERIODIC_INTERVAL_UNIT_US,
    LL_SCA_50_PPM,
    LL_SCA_500_PPM,
    PDU_ADV_ADDRESS_OFFSET,
    PDU_ADV_ADDRESS_SIZE,
    PDU_ADV_ADDRESSES_END,
    PDU_ADV_ADI_DID_BITS,
    PDU_ADV_ADI_SIZE,
    PDU_ADV_AUX_CONNECT_RSP_SIZE,
    PDU_ADV_AUX_PTR_SIZE,
    PDU_ADV_CONNECT_PAYLOAD_SIZE,
    PDU_ADV_CONNECT_SIZE,
    PDU_ADV_EXT_HEADER_SIZE_MAX,
    PDU_ADV_OFFSET_UNIT_30_US,
    PDU_ADV_OFFSET_UNIT_300_US,
    PDU_ADV_OFFSET_VALUE_MASK,
    PDU_ADV_RXADD_MASK,
    PDU_ADV_SCAN_REQ_SIZE,
    PDU_ADV_SECOND_ADDRESS_OFFSET,
    PDU_ADV_TXADD_MASK,
    PDU_ADV_TYPE_MASK,
    PDU_CONTROL_INSTANT_SIZE,
    AdvertisingMode,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising import (
    AuxPtr,
    ExtendedPdu,
    SyncInfo,
    advertising_request_peer,
    align_offset_us,
    scan_request,
)
from bscontroller.linklayer.bis import BIG_INFO_AD_TYPE
from bscontroller.linklayer.discovery import AD_TYPE_CHANNEL_MAP_UPDATE_INDICATION
from bscontroller.linklayer.filtering import (
    DecisionMode,
    InitiatorFilterPolicy,
    ScanningFilterPolicy,
    scanning_decision_mode,
)
from bscontroller.linklayer.parameters import Parameters
from bscontroller.linklayer.results import ConnectionAccepted, SyncInfoReceived
from bscontroller.linklayer.scheduler import RadioPriority
from bscontroller.linklayer.state import (
    LL_ADV_FILTER_CONNECT_MASK,
    LL_ADV_FILTER_SCAN_MASK,
    AdvertisingProperties,
    AdvertisingReport,
    DataStatus,
    Role,
)
from bscontroller.linklayer.timing import window_widening
from bscontroller.radio.base import RADIO_SWITCH_TIME_US, Phy

if TYPE_CHECKING:
    from bscontroller.linklayer.discovery import Advertising, Initiating, PeriodicSync
    from bscontroller.linklayer.link import RadioLink
    from bscontroller.radio.base import Received


# Auxiliary scheduling policy: preferred start-to-start offset, not a Core minimum.
# Actual offsets also include packet airtime plus LL_MAFS_US and offset rounding.
RADIO_AUX_OFFSET_DEFAULT_US = 600


def auxiliary_offset(pdu: bytes, phy: Phy) -> int:
    return align_offset_us(
        max(RADIO_AUX_OFFSET_DEFAULT_US, phy.airtime_us(len(pdu)) + LL_MAFS_US)
    )


def fragments(first: ExtendedPdu, data: bytes | bytearray, channel: int, phy: Phy):
    """Split data around the first PDU's fields and each continuation's AuxPtr.

    Derive pointer offsets from the encoded packet size and PHY airtime.
    See Bluetooth Core 6.3, Vol 6, Part B, 2.3.1.8 and 2.3.4.5.
    """
    if len(data) > CONTROLLER_ADV_DATA_SIZE_MAX:
        raise ValueError(
            f'extended advertising data exceeds {CONTROLLER_ADV_DATA_SIZE_MAX} octets'
        )
    remaining = data
    result = []
    pdu = first
    while True:
        pdu = replace(pdu, data=b'', aux_ptr=None)
        capacity = pdu.data_capacity()
        if len(remaining) > capacity:
            if first.mode == AdvertisingMode.CONNECTABLE:
                raise ValueError('connectable advertising data must fit one PDU')
            # Reserve AuxPtr space before choosing the data chunk: adding the pointer
            # shrinks the capacity of every non-final fragment (Vol 6, Part B, 2.3.4).
            pdu = replace(
                pdu, aux_ptr=AuxPtr(channel, RADIO_AUX_OFFSET_DEFAULT_US, phy)
            )
            capacity = pdu.data_capacity()
        count = min(len(remaining), capacity)
        chunk, remaining = bytes(remaining[:count]), remaining[count:]
        pdu = replace(pdu, data=chunk, aux_ptr=None)
        if remaining:
            # Measure the offset from packet start, covering airtime plus T_MAFS,
            # then round to a representable unit (Vol 6, Part B, 2.3.4.5; 4.1.2).
            probe = replace(
                pdu, aux_ptr=AuxPtr(channel, RADIO_AUX_OFFSET_DEFAULT_US, phy)
            ).encode()
            pdu = replace(
                pdu, aux_ptr=AuxPtr(channel, auxiliary_offset(probe, phy), phy)
            )
        result.append(pdu)
        if not remaining:
            return result
        # Continuations retain ADI, while the first PDU owns the advertiser's
        # address/target/SyncInfo fields. This changes each fragment's data capacity.
        pdu = ExtendedPdu(adi=first.adi)


def auxiliary_pdu(item: Advertising, sync: SyncInfo | None = None) -> ExtendedPdu:
    """AUX_ADV_IND fields; a PAwR train's SyncInfo comes with its timing ACAD
    (Core Vol 6, Part B, 2.3.4.8).
    """
    return ExtendedPdu(
        address=None if item.anonymous else bytes(item.address),
        random_address=not item.anonymous and item.address.is_random,
        target=bytes(item.target) if item.target else None,
        random_target=bool(item.target and item.target.is_random),
        adi=item.sid << PDU_ADV_ADI_DID_BITS | item.did,
        mode=item.mode,
        sync_info=sync,
        tx_power=item.tx_power_dbm if item.include_tx_power else None,
        acad=(
            item.pawr.acad(item.pawr_state.rsp_aa if item.pawr_state else 0)
            if item.pawr is not None and sync is not None
            else b''
        ),
    )


def channel_map_acad(item: Advertising) -> bytes:
    """Channel Map Update Indication while the train has a pending update
    (Core Vol 6, Part B, 2.3.4.8, 4.4.2.12.1).
    """
    if item.periodic_channel_update is None:
        return b''
    instant, channels = item.periodic_channel_update
    value = channels.to_bytes(LL_CHANNEL_MAP_SIZE, 'little') + (
        instant.to_bytes(PDU_CONTROL_INSTANT_SIZE, 'little')
    )
    return bytes([len(value) + 1, AD_TYPE_CHANNEL_MAP_UPDATE_INDICATION]) + value


def chain_time_us(packets, phy: Phy) -> int:
    return sum(packet.aux_ptr.offset_us for packet in packets[:-1]) + phy.airtime_us(
        len(packets[-1].encode())
    )


def connect_request(initiator: Address, advertiser: Address, parameters) -> bytes:
    """AUX_CONNECT_REQ: InitA, AdvA and LLData (Core Vol 6, Part B, 2.3.3.2)."""
    return (
        bytes(
            [
                AdvertisingPduType.AUX_CONNECT_REQ
                | (PDU_ADV_TXADD_MASK if initiator.is_random else 0)
                | (PDU_ADV_RXADD_MASK if advertiser.is_random else 0),
                PDU_ADV_CONNECT_PAYLOAD_SIZE,
            ]
        )
        + bytes(initiator)
        + bytes(advertiser)
        + parameters.encode()
    )


def connect_response_matches(
    pdu: ExtendedPdu, advertiser: Address, initiator: Address
) -> bool:
    """AUX_CONNECT_RSP from ``advertiser``, targeting ``initiator`` (2.3.2.2)."""
    return (
        pdu.kind == AdvertisingPduType.AUX_CONNECT_RSP
        and not pdu.mode
        and pdu.address == bytes(advertiser)
        and pdu.random_address == advertiser.is_random
        and pdu.target == bytes(initiator)
        and pdu.random_target == initiator.is_random
    )


def periodic_pdu(item: Advertising) -> ExtendedPdu:
    """First AUX_SYNC_IND fields: the ADI only while the Host includes it.

    Its SID matches the superior AUX_ADV_IND; its DID is the periodic data's own
    (Core Vol 6, Part B, 2.3.1.7, 4.4.2.11 and 4.4.2.12.1). fragments() repeats
    the ADI in every AUX_CHAIN_IND, as Table 2.7 C.1 requires; a pending
    channel map update stays in the first PDU's ACAD. BIGInfo is included only
    when that header has space (Core Vol 6, Part B, 4.4.6.9), including any
    AuxPtr needed for chained Host data.
    """
    pdu = ExtendedPdu(
        adi=item.sid << PDU_ADV_ADI_DID_BITS | item.periodic_did
        if item.periodic_adi
        else None,
        acad=channel_map_acad(item),
    )
    if item.big_info:
        big_info = bytes([len(item.big_info) + 1, BIG_INFO_AD_TYPE]) + item.big_info
        header_size = (
            1
            + (PDU_ADV_ADI_SIZE if item.periodic_adi else 0)
            + len(pdu.acad)
            + len(big_info)
        )
        if header_size <= PDU_ADV_EXT_HEADER_SIZE_MAX:
            candidate = replace(pdu, acad=pdu.acad + big_info)
            chained = len(item.periodic_data) > candidate.data_capacity()
            if (
                not chained
                or header_size + PDU_ADV_AUX_PTR_SIZE <= PDU_ADV_EXT_HEADER_SIZE_MAX
            ):
                return candidate
    return pdu


def subevent_pdu(item: Advertising, data: bytes = b'') -> ExtendedPdu:
    """AUX_SYNC_SUBEVENT_IND: optional ADI and ACAD, no AdvA, AuxPtr or chain
    (Core Vol 6, Part B, 2.3.1.9).
    """
    return replace(
        periodic_pdu(item),
        data=bytes(data),
        kind=AdvertisingPduType.AUX_SYNC_SUBEVENT_IND,
    )


def periodic_time_us(item: Advertising) -> int:
    """One periodic event's AUX_SYNC_IND chain, or an empty AUX_SYNC_SUBEVENT_IND:
    a PAwR train ignores periodic advertising data.
    """
    if item.pawr is not None:
        return item.secondary_phy.airtime_us(len(subevent_pdu(item).encode()))
    return chain_time_us(
        fragments(periodic_pdu(item), item.periodic_data, 0, item.secondary_phy),
        item.secondary_phy,
    )


def required_periodic_interval(item: Advertising) -> int:
    """Smallest 1.25-ms interval that leaves T_MAFS after this PA event."""
    return (periodic_time_us(item) + LL_MAFS_US) // LL_PERIODIC_INTERVAL_UNIT_US + 1


def primary_pdu(item: Advertising, pointer: AuxPtr) -> ExtendedPdu:
    """Build primary CEAP fields, keeping decision data separate from AdvData."""
    return ExtendedPdu(
        mode=item.mode,
        kind=AdvertisingPduType.ADV_DECISION_IND
        if item.decision
        else AdvertisingPduType.ADV_EXT_IND,
        address=bytes(item.address)
        if item.decision and item.decision_address
        else None,
        random_address=bool(
            item.decision and item.decision_address and item.address.is_random
        ),
        adi=(item.sid << PDU_ADV_ADI_DID_BITS | item.did)
        if not item.decision or item.decision_adi
        else None,
        aux_ptr=pointer,
        data=item.decision_data if item.decision else b'',
        decision_flags=item.decision_flags if item.decision else 0,
        tx_power=item.tx_power_dbm
        if item.decision and item.include_tx_power and item.primary_phy == Phy.LE_1M
        else None,
    )


def primary_offset_us(item: Advertising) -> int:
    """AuxPtr offset from the first ADV_EXT_IND to the shared AUX_ADV_IND, at least
    T_MAFS (Core Vol 6, Part B, 4.1.2, 4.4.2.2.2).
    """
    primary = primary_pdu(
        item, AuxPtr(0, RADIO_AUX_OFFSET_DEFAULT_US, item.secondary_phy)
    ).encode()
    return align_offset_us(
        max(
            RADIO_AUX_OFFSET_DEFAULT_US,
            item.channel_map.bit_count() * item.primary_phy.airtime_us(len(primary))
            + (item.channel_map.bit_count() - 1) * RADIO_SWITCH_TIME_US
            + LL_MAFS_US,
        )
    )


def advertising_time_us(item: Advertising) -> int:
    """Air time of one extended advertising event, including any request/response
    exchange (Core Vol 6, Part B, 4.4.2.2.2).
    """
    if not item.extended:
        return 0
    sync = (
        SyncInfo(
            PDU_ADV_OFFSET_UNIT_30_US,
            max(LL_PERIODIC_INTERVAL_MIN, item.periodic_interval),
            LL_DATA_CHANNEL_MAP_ALL,
            item.periodic_aa,
            item.periodic_crc,
            0,
        )
        if item.periodic_enabled
        else None
    )
    data = b'' if item.mode == AdvertisingMode.SCANNABLE else item.data
    phy = item.secondary_phy
    duration = primary_offset_us(item) + chain_time_us(
        fragments(auxiliary_pdu(item, sync), data, 0, phy), phy
    )
    if item.mode == AdvertisingMode.SCANNABLE:
        # Budget a possible request and the full response chain up front, so the
        # radio reservation covers the entire exchange without preemption.
        response = ExtendedPdu(address=bytes(item.address))
        duration += (
            2 * LL_IFS_US
            + phy.airtime_us(PDU_ADV_SCAN_REQ_SIZE)
            + chain_time_us(fragments(response, item.scan_response, 0, phy), phy)
        )
    elif item.mode == AdvertisingMode.CONNECTABLE:
        duration += (
            2 * LL_IFS_US
            + phy.airtime_us(PDU_ADV_CONNECT_SIZE)
            + phy.airtime_us(PDU_ADV_AUX_CONNECT_RSP_SIZE)
        )
    return duration


class ExtendedProcedures:
    def __init__(self, link: RadioLink):
        self.link = link

    async def transmit_chain(self, channel, first, data, start, phy, **parameters):
        for pdu in fragments(first, data, channel, phy):
            await self.link._transmit(
                channel, pdu.encode(), start_us=start, phy=phy, **parameters
            )
            if pdu.aux_ptr:
                start += pdu.aux_ptr.offset_us

    async def follow(self, packet: Received, ext: ExtendedPdu, **parameters):
        """Receive the auxiliary packet an AuxPtr points to, widened per Core Vol 6,
        Part B, 4.2.4 Table 4.1.
        """
        aux = ext.aux_ptr
        assert aux is not None
        start = packet.start_us + aux.offset_us
        phy = Phy(aux.phy)
        widening = window_widening(
            aux.offset_us, LL_SCA_50_PPM if aux.clock_accuracy else LL_SCA_500_PPM
        )
        unit = aux.unit_us
        if start + unit <= packet.end_us + LL_MAFS_US:
            return None
        until = start + widening + unit + phy.address_us
        if until <= self.link.now_us or not self.link.resource.fits_radio(until):
            return None
        received = await self.link.radio.receive(
            aux.channel,
            until,
            start_us=max(self.link.now_us + 1, start - widening),
            phy=phy,
            **parameters,
        )
        return (
            received
            if received and received.start_us >= packet.end_us + LL_MAFS_US
            else None
        )

    async def collect(
        self, packet: Received, first: ExtendedPdu, *, on_fragment=None, **parameters
    ):
        """Follow an auxiliary chain, retaining a bounded prefix on failure.

        With ``on_fragment``, flush accumulated bytes before following the next
        PDU and return the unreported tail; otherwise return the assembled bytes.
        Track cumulative size in both modes and return status and the last TxPower.
        See Bluetooth Core 6.3, Vol 6, Part B, 2.3.1.8 and Vol 4, Part E, 7.7.65.13.
        """
        data = first.data
        current = first
        total = len(data)
        # Any failed follow/decode/identity/size check leaves a truncated report.
        # Only reaching a PDU without AuxPtr proves the chain is complete.
        status = DataStatus.TRUNCATED
        while current.aux_ptr:
            if current.aux_ptr.phy != packet.phy:
                break
            if on_fragment is not None and data:
                # Release delivered chunks when streaming, but retain the cumulative
                # length so callbacks cannot bypass the assembly limit.
                on_fragment(
                    data,
                    DataStatus.INCOMPLETE,
                    packet,
                )
                data = b''
            received = await self.follow(packet, current, **parameters)
            if received is None:
                break
            try:
                ext = ExtendedPdu.decode(received.pdu)
            except ValueError:
                break
            if (
                ext.kind != AdvertisingPduType.AUX_CHAIN_IND
                or ext.mode
                or (first.adi is not None and ext.adi != first.adi)
            ):
                # Stop on a different ADI rather than joining data from two trains
                # (Vol 6, Part B, 2.3.1.8).
                break
            if ext.aux_ptr and not ext.data:
                break
            # Core Vol6 PartB Table2.7:chain addresses/SyncInfo/ACAD are ignored.
            current = ext
            if total + len(ext.data) > CONTROLLER_ADV_DATA_SIZE_MAX:
                break
            data += ext.data
            total += len(ext.data)
            packet = received
        else:
            status = DataStatus.COMPLETE
        return (
            data,
            status,
            (current.tx_power if current.tx_power is not None else None),
        )

    async def advertise(self, item: Advertising) -> None:
        """One extended advertising event: ADV_EXT_IND on each used primary channel,
        then the AUX chain (Core Vol 6, Part B, 4.4.2.2.2).
        """
        link = self.link
        start = link.radio.earliest_start_us(
            next(
                channel
                for bit, channel in enumerate(LL_ADV_PRIMARY_CHANNELS)
                if item.channel_map & (1 << bit)
            ),
            item.primary_phy,
            transmit=True,
        )
        aux_channel = item.did % LL_DATA_CHANNEL_COUNT
        aux_start = start + primary_offset_us(item)
        anchor_us, counter = item.periodic_next_us, item.periodic_counter
        if item.pawr is not None:
            # Point at subevent 0 of an event that has not started: the next
            # PAwR event's anchor once the current one is under way (2.3.4.6).
            while anchor_us - aux_start < PDU_ADV_OFFSET_UNIT_30_US:
                anchor_us += item.periodic_interval * LL_PERIODIC_INTERVAL_UNIT_US
                counter = (counter + 1) & LL_EVENT_COUNTER_MASK
        info = (
            SyncInfo(
                anchor_us - aux_start,
                item.periodic_interval,
                item.periodic_channel_map,
                item.periodic_aa,
                item.periodic_crc,
                counter,
            )
            if item.periodic_enabled
            and PDU_ADV_OFFSET_UNIT_30_US
            <= anchor_us - aux_start
            < (PDU_ADV_OFFSET_VALUE_MASK + 1) * PDU_ADV_OFFSET_UNIT_300_US
            else None
        )
        # All enabled primary channels point to one auxiliary event, not three
        # independent copies of the advertising data chain.
        for bit, channel in enumerate(LL_ADV_PRIMARY_CHANNELS):
            if not item.channel_map & (1 << bit):
                continue
            start = link.radio.earliest_start_us(
                channel, item.primary_phy, transmit=True
            )
            offset = (
                (aux_start - start)
                // PDU_ADV_OFFSET_UNIT_30_US
                * PDU_ADV_OFFSET_UNIT_30_US
            )
            primary = primary_pdu(item, AuxPtr(aux_channel, offset, item.secondary_phy))
            await link._transmit(
                channel,
                primary.encode(),
                start_us=start,
                phy=item.primary_phy,
                power_dbm=item.tx_power_dbm,
            )
        await self.transmit_chain(
            aux_channel,
            auxiliary_pdu(item, info),
            b'' if item.mode == AdvertisingMode.SCANNABLE else item.data,
            aux_start,
            item.secondary_phy,
            power_dbm=item.tx_power_dbm,
        )
        if item.mode:
            packet = await link.receive_response(
                aux_channel,
                link.now_us + 1,
                item.secondary_phy,
            )
            if packet:
                await self.request(item, aux_channel, packet)

    async def request(self, item: Advertising, channel: int, packet: Received) -> bool:
        """Answer AUX_SCAN_REQ or AUX_CONNECT_REQ on the secondary channel (Core Vol 6,
        Part B, 4.4.2.5.2, 4.4.2.7, 4.4.4.2).
        """
        link, pdu = self.link, packet.pdu
        peer = advertising_request_peer(pdu, item.address, None)
        if peer is None:
            return False
        identity = link.privacy.identity(peer)
        if identity is None or (
            item.target is not None and identity != link.privacy.identity(item.target)
        ):
            return False
        if (
            item.mode == AdvertisingMode.SCANNABLE
            and pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.SCAN_REQ
            and len(pdu) == PDU_ADV_SCAN_REQ_SIZE
        ):
            if (
                item.filter_policy & LL_ADV_FILTER_SCAN_MASK
                and identity not in link.accept_list
            ):
                return False
            if item.notify_scan_request:
                link.listener.on_scan_request(item, identity)
            await self.transmit_chain(
                channel,
                ExtendedPdu(
                    address=bytes(item.address),
                    random_address=item.address.is_random,
                ),
                item.scan_response,
                packet.end_us + LL_IFS_US,
                item.secondary_phy,
                power_dbm=item.tx_power_dbm,
            )
        elif (
            item.mode == AdvertisingMode.CONNECTABLE
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
            except ValueError:
                return False
            response = ExtendedPdu(
                kind=AdvertisingPduType.AUX_CONNECT_RSP,
                address=bytes(item.address),
                random_address=item.address.is_random,
                target=bytes(peer),
                random_target=peer.is_random,
            )
            # Unlike a legacy CONNECT_IND, an auxiliary request needs an explicit
            # AUX_CONNECT_RSP before this advertiser can become a Peripheral.
            await link._transmit(
                channel,
                response.encode(),
                start_us=packet.end_us + LL_IFS_US,
                phy=item.secondary_phy,
                power_dbm=item.tx_power_dbm,
            )
            link.roles.connections.accepted(
                ConnectionAccepted(
                    parameters,
                    peer,
                    Role.PERIPHERAL,
                    packet.end_us,
                    csa2=True,
                    self_address=item.address,
                    local_rpa=item.local_rpa,
                    advertiser=item,
                    window_delay_us=(
                        LL_CONN_AUX_WINDOW_DELAY_CODED_US
                        if packet.phy == Phy.LE_CODED
                        else LL_CONN_AUX_WINDOW_DELAY_UNCODED_US
                    ),
                    phy=packet.phy,
                )
            )
            return True
        return False

    async def scan(self, packet: Received, *, initiating: bool = False) -> None:
        """Follow ADV_EXT_IND to its AUX_ADV_IND and report it (Core Vol 6, Part B,
        4.4.3.3, 4.4.3.4).
        """
        link = self.link
        # Primary SyncInfo,CTEInfo,ACAD and AdvData are context-reserved CEAP fields.
        primary = ExtendedPdu.decode(packet.pdu)
        decision = primary.kind == AdvertisingPduType.ADV_DECISION_IND
        local = link.initiating if initiating else link.scan
        if local is None and decision:
            return
        mode = (
            InitiatorFilterPolicy(local.filter_policy).decision_mode
            if initiating
            else scanning_decision_mode(local.filter_policy)
            if local is not None
            else DecisionMode.IGNORE_DECISION_PDUS
        )
        if (
            decision
            and (
                mode == DecisionMode.IGNORE_DECISION_PDUS
                or not link.decision.accepts(primary, packet.rssi)
            )
        ) or (not decision and mode == DecisionMode.DECISION_PDUS_ONLY):
            return
        if decision and (
            primary.aux_ptr is None
            or primary.target is not None
            or primary.sync_info is not None
            or primary.cte is not None
            or primary.acad
        ):
            return
        # Core Vol 6, Part B, 2.3.1.5, Table 2.4: without AuxPtr, ADI is
        # reserved too. Ignore it before deriving SID or duplicate identity;
        # its physical presence must not manufacture a Host advertising SID.
        primary = replace(
            primary,
            adi=primary.adi if primary.aux_ptr is not None else None,
            sync_info=None,
            cte=None,
            acad=b'',
            data=b'',
        )
        if primary.kind not in (
            AdvertisingPduType.ADV_EXT_IND,
            AdvertisingPduType.ADV_DECISION_IND,
        ):
            return
        primary_addresses_valid = decision or (
            not primary.mode and packet.phy == Phy.LE_1M
        )
        primary_identity = None
        if (
            not initiating
            and link.scan is not None
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
        ext, received, channel = primary, packet, None
        if primary.aux_ptr:
            channel = primary.aux_ptr.channel
            received = await self.follow(packet, primary)
            if received is None:
                return
            ext = ExtendedPdu.decode(received.pdu)
            if (
                ext.kind != AdvertisingPduType.AUX_ADV_IND
                or ext.mode != primary.mode
                or (primary.adi is not None and ext.adi != primary.adi)
            ):
                return
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
        if ext.address is None and (ext.mode or ext.target is not None):
            return
        if channel is not None and ext.mode:
            # Core Table2.5 reserves AuxPtr/SyncInfo in connectable/scannable
            # AUX_ADV_IND.
            ext = replace(
                ext,
                aux_ptr=None,
                sync_info=None,
                data=b'' if ext.mode == AdvertisingMode.SCANNABLE else ext.data,
            )
        address = (
            Address(ext.address, AddressType(ext.random_address))
            if ext.address is not None
            else Address(bytes(PDU_ADV_ADDRESS_SIZE), AddressType.ANONYMOUS)
        )
        identity = primary_identity or link.privacy.identity(address)
        if identity is None:
            return
        if not initiating and link.scan is not None and ext.address is not None:
            link.monitoring.received(identity, received.rssi)
        target = (
            Address(ext.target, AddressType(ext.random_target)) if ext.target else None
        )
        local = link.initiating if initiating else link.scan
        target_matches = target is None or (
            local is not None
            and link.privacy.accept_target(target, local.address, address)
        )
        if (
            target is not None
            and not target_matches
            and (
                local is None
                or initiating
                or not local.filter_policy
                & ScanningFilterPolicy.ACCEPT_ALL_DIRECTED_RPAS
                or not target.is_resolvable
            )
        ):
            return
        if initiating:
            # Initiation consumes only eligible connectable auxiliary events;
            # it does not produce the scanner reports or active-scan exchange below.
            item = link.initiating
            if item is None:
                return
            if (
                target is not None
                and OwnAddressType(item.own_address_type).uses_resolvable_address
                and not target.is_resolvable
            ):
                return
            policy = InitiatorFilterPolicy(item.filter_policy)
            accepted = (
                (
                    identity in link.accept_list
                    if policy.filters_non_decision_pdus
                    else identity == item.peer
                )
                if not decision
                else (
                    identity in link.accept_list
                    if policy == InitiatorFilterPolicy.FILTER_ACCEPT_LIST_ALL_PDUS
                    else True
                )
            )
            if (
                ext.mode == AdvertisingMode.CONNECTABLE
                and channel is not None
                and accepted
            ):
                await self.initiate(channel, received, ext, address)
            return
        sid = ext.adi >> PDU_ADV_ADI_DID_BITS if ext.adi is not None else None
        info = ext.sync_info
        if info is not None and sid is not None:
            link.roles.synchronization.acquired(
                SyncInfoReceived(identity, sid, info, received, address, ext.acad)
            )
        if (
            link.scan
            and link.scan.filter_policy & ScanningFilterPolicy.FILTER_ACCEPT_LIST
            and identity not in link.accept_list
        ):
            return
        data, status, tx_power = await self.collect(received, ext)
        if target is not None:
            target = link.privacy.report_target(
                target, local.identity_address or local.address, address
            )
        flags = ext.mode | (AdvertisingProperties.DIRECTED_ADVERTISING if target else 0)
        link.listener.on_advertisement(
            identity,
            data,
            received.rssi,
            AdvertisingReport(AdvertisingProperties(flags), status),
            sid,
            info.interval if info else 0,
            tx_power,
            target=target,
            primary_phy=packet.phy,
            secondary_phy=received.phy if channel is not None else 0,
        )
        if (
            link.scan
            and link.scan.active
            and ext.mode == AdvertisingMode.SCANNABLE
            and target_matches
            and channel is not None
            and not ext.aux_ptr
            and link.scan.backoff.permit()
        ):
            scan = link.scan
            scan.address = link.privacy.local_address(
                scan.own_address_type, scan.identity_address or scan.address, identity
            )
            request = scan_request(scan.address, address)
            end_us = await link._transmit(
                channel, request, start_us=received.end_us + LL_IFS_US, phy=received.phy
            )
            response = await link.receive_response(channel, end_us, received.phy)
            if response:
                try:
                    rsp = ExtendedPdu.decode(response.pdu)
                except ValueError:
                    scan.backoff.complete(False, link.random)
                    return
                if (
                    rsp.kind == AdvertisingPduType.AUX_SCAN_RSP
                    and not rsp.mode
                    and rsp.address == ext.address
                    and rsp.random_address == ext.random_address
                    and (rsp.adi is None or rsp.adi == ext.adi)
                ):
                    # Bind AUX_SCAN_RSP to this advertiser and data identity before
                    # following its chain or declaring the active-scan attempt
                    # successful.
                    scan.backoff.complete(True, link.random)
                    data, status, tx_power = await self.collect(response, rsp)
                    link.listener.on_advertisement(
                        identity,
                        data,
                        response.rssi,
                        AdvertisingReport(
                            AdvertisingProperties(flags)
                            | AdvertisingProperties.SCAN_RESPONSE,
                            status,
                        ),
                        sid,
                        0,
                        tx_power,
                        target=target,
                        primary_phy=packet.phy,
                        secondary_phy=received.phy,
                    )
                    return
            scan.backoff.complete(False, link.random)

    async def initiate(self, channel, packet, ext, address):
        """AUX_CONNECT_REQ / AUX_CONNECT_RSP exchange on the secondary channel (Core Vol
        6, Part B, 4.4.4.2).
        """
        link, item = self.link, self.link.initiating
        assert item is not None
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
            request_end
            + LL_IFS_US
            + packet.phy.airtime_us(PDU_ADV_AUX_CONNECT_RSP_SIZE)
        ):
            return
        request = connect_request(item.address, address, parameters)
        request_end = await link._transmit(
            channel, request, start_us=packet.end_us + LL_IFS_US, phy=packet.phy
        )
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
        link.roles.connections.accepted(
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

    async def pawr_connect(
        self, item: Advertising, initiating: Initiating, channel: int, start_us: int
    ) -> None:
        """Send AUX_CONNECT_REQ in place of a PAwR subevent's AUX_SYNC_SUBEVENT_IND,
        then wait T_IFS for AUX_CONNECT_RSP (Core Vol 6, Part B, 4.4.2.12.2).

        Without that response the request is discarded, once, and the Host
        learns that the connection failed (Vol 4, Part E, 7.7.65.10).
        """
        link = self.link
        peer, phy = initiating.peer, item.secondary_phy
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
        request_end = start_us + phy.airtime_us(PDU_ADV_CONNECT_SIZE)
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
            request_end = await link._transmit(
                channel,
                connect_request(initiating.address, peer, parameters),
                start_us=start_us,
                phy=phy,
                power_dbm=item.tx_power_dbm,
                **train,
            )
            response = await link.receive_response(channel, request_end, phy, **train)
        if link.initiating is not initiating:
            return
        try:
            rsp = ExtendedPdu.decode(response.pdu) if response is not None else None
        except ValueError:
            rsp = None
        if rsp is not None and connect_response_matches(rsp, peer, initiating.address):
            assert parameters is not None
            link.roles.connections.accepted(
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
        self, sync: PeriodicSync, channel: int, packet: Received
    ) -> None:
        """Answer an AUX_CONNECT_REQ naming this device in a synchronized PAwR
        subevent, then become its Peripheral (Core Vol 6, Part B, 4.4.2.12.2).

        AdvA must be one of this device's addresses; InitA becomes the peer.
        """
        link, pdu = self.link, packet.pdu
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
        await link._transmit(
            channel,
            response.encode(),
            start_us=packet.end_us + LL_IFS_US,
            access_address=sync.info.access_address,
            crc_init=sync.info.crc_init,
            phy=packet.phy,
        )
        link.roles.connections.accepted(
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
