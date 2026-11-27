# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Extended advertising PDUs, auxiliary chains and advertiser radio events."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from bscontroller.capabilities import (
    CONTROLLER_ADV_DATA_SIZE_MAX,
)
from bscontroller.linklayer import (
    LL_ADV_PRIMARY_CHANNELS,
    LL_CHANNEL_MAP_SIZE,
    LL_DATA_CHANNEL_COUNT,
    LL_DATA_CHANNEL_MAP_ALL,
    LL_EVENT_COUNTER_MASK,
    LL_IFS_US,
    LL_MAFS_US,
    LL_PERIODIC_INTERVAL_MIN,
    LL_PERIODIC_INTERVAL_UNIT_US,
    PDU_ADV_ADI_DID_BITS,
    PDU_ADV_ADI_SIZE,
    PDU_ADV_AUX_CONNECT_RSP_SIZE,
    PDU_ADV_AUX_PTR_SIZE,
    PDU_ADV_CONNECT_SIZE,
    PDU_ADV_EXT_HEADER_SIZE_MAX,
    PDU_ADV_OFFSET_UNIT_30_US,
    PDU_ADV_OFFSET_UNIT_300_US,
    PDU_ADV_OFFSET_VALUE_MASK,
    PDU_ADV_SCAN_REQ_SIZE,
    PDU_CONTROL_INSTANT_SIZE,
    AdvertisingMode,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising_pdu import (
    AuxPtr,
    ExtendedPdu,
    SyncInfo,
    align_offset_us,
    connect_response,
)
from bscontroller.linklayer.advertising_requests import (
    accept_advertising_connection,
    accept_scan_request,
)
from bscontroller.linklayer.bis import BIG_INFO_AD_TYPE
from bscontroller.linklayer.discovery import AD_TYPE_CHANNEL_MAP_UPDATE_INDICATION
from bscontroller.radio.base import PHY_CODED_S8_FACTOR, RADIO_SWITCH_TIME_US, Phy

if TYPE_CHECKING:
    from bscontroller.linklayer.advertiser import AdvertisingManager
    from bscontroller.linklayer.discovery import (
        Advertising,
    )
    from bscontroller.radio.base import Received


# Auxiliary scheduling policy: preferred start-to-start offset, not a Core minimum.
# Actual offsets also include packet airtime plus LL_MAFS_US and offset rounding.
RADIO_AUX_OFFSET_DEFAULT_US = 600


def auxiliary_offset(pdu: bytes, phy: Phy, coding: int) -> int:
    return align_offset_us(
        max(RADIO_AUX_OFFSET_DEFAULT_US, phy.airtime_us(len(pdu), coding) + LL_MAFS_US)
    )


def fragments(
    first: ExtendedPdu,
    data: bytes | bytearray,
    channel: int,
    phy: Phy,
    *,
    coding: int = PHY_CODED_S8_FACTOR,
):
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
                pdu, aux_ptr=AuxPtr(channel, auxiliary_offset(probe, phy, coding), phy)
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


def chain_time_us(packets, phy: Phy, coding: int) -> int:
    return sum(packet.aux_ptr.offset_us for packet in packets[:-1]) + phy.airtime_us(
        len(packets[-1].encode()), coding
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


def periodic_time_us(item: Advertising, *, check_capacity: bool = False) -> int:
    """One periodic event's AUX_SYNC_IND chain, or an empty AUX_SYNC_SUBEVENT_IND:
    a PAwR train ignores periodic advertising data.
    """
    phy = item.secondary_phy
    coding = item.secondary_coding.factor(check_capacity=check_capacity)
    if item.pawr is not None:
        return phy.airtime_us(len(subevent_pdu(item).encode()), coding)
    return chain_time_us(
        fragments(periodic_pdu(item), item.periodic_data, 0, phy, coding=coding),
        phy,
        coding,
    )


def required_periodic_interval(item: Advertising) -> int:
    """Smallest 1.25-ms interval that leaves T_MAFS after this PA event."""
    return (
        periodic_time_us(item, check_capacity=True) + LL_MAFS_US
    ) // LL_PERIODIC_INTERVAL_UNIT_US + 1


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


def primary_offset_us(item: Advertising, *, check_capacity: bool = False) -> int:
    """AuxPtr offset from the first ADV_EXT_IND to the shared AUX_ADV_IND, at least
    T_MAFS (Core Vol 6, Part B, 4.1.2, 4.4.2.2.2).
    """
    primary = primary_pdu(
        item, AuxPtr(0, RADIO_AUX_OFFSET_DEFAULT_US, item.secondary_phy)
    ).encode()
    coding = item.primary_coding.factor(check_capacity=check_capacity)
    return align_offset_us(
        max(
            RADIO_AUX_OFFSET_DEFAULT_US,
            item.channel_map.bit_count()
            * item.primary_phy.airtime_us(len(primary), coding)
            + (item.channel_map.bit_count() - 1) * RADIO_SWITCH_TIME_US
            + LL_MAFS_US,
        )
    )


def advertising_time_us(item: Advertising, *, check_capacity: bool = False) -> int:
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
    coding = item.secondary_coding.factor(check_capacity=check_capacity)
    duration = primary_offset_us(item, check_capacity=check_capacity) + chain_time_us(
        fragments(auxiliary_pdu(item, sync), data, 0, phy, coding=coding), phy, coding
    )
    if item.mode == AdvertisingMode.SCANNABLE:
        # Budget a possible request and the full response chain up front, so the
        # radio reservation covers the entire exchange without preemption.
        response = ExtendedPdu(address=bytes(item.address))
        duration += (
            2 * LL_IFS_US
            + phy.airtime_us(PDU_ADV_SCAN_REQ_SIZE)
            + chain_time_us(
                fragments(response, item.scan_response, 0, phy, coding=coding),
                phy,
                coding,
            )
        )
    elif item.mode == AdvertisingMode.CONNECTABLE:
        duration += (
            2 * LL_IFS_US
            + phy.airtime_us(PDU_ADV_CONNECT_SIZE)
            + phy.airtime_us(PDU_ADV_AUX_CONNECT_RSP_SIZE, coding)
        )
    return duration


async def transmit_chain(
    advertising: AdvertisingManager,
    channel,
    first,
    data,
    start,
    phy,
    *,
    coding,
    advertising_event: Advertising,
    **parameters,
):
    link = advertising.link
    for pdu in fragments(
        link.with_radiative_power(first), data, channel, phy, coding=coding
    ):
        if not advertising.event_is_current(advertising_event):
            return
        await link.transmit(
            channel,
            pdu.encode(),
            start_us=start,
            phy=phy,
            coding=coding,
            **parameters,
        )
        if pdu.aux_ptr:
            start += pdu.aux_ptr.offset_us


async def advertise_extended(
    advertising: AdvertisingManager, item: Advertising
) -> None:
    """One extended advertising event: ADV_EXT_IND on each used primary channel,
    then the AUX chain (Core Vol 6, Part B, 4.4.2.2.2).
    """
    link = advertising.link
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
        if not advertising.event_is_current(item):
            return
        start = link.radio.earliest_start_us(channel, item.primary_phy, transmit=True)
        offset = (
            (aux_start - start) // PDU_ADV_OFFSET_UNIT_30_US * PDU_ADV_OFFSET_UNIT_30_US
        )
        primary = primary_pdu(item, AuxPtr(aux_channel, offset, item.secondary_phy))
        await link.transmit(
            channel,
            link.with_radiative_power(primary).encode(),
            start_us=start,
            phy=item.primary_phy,
            coding=item.primary_coding.factor(),
            power_dbm=item.tx_power_dbm,
        )
    await transmit_chain(
        advertising,
        aux_channel,
        auxiliary_pdu(item, info),
        b'' if item.mode == AdvertisingMode.SCANNABLE else item.data,
        aux_start,
        item.secondary_phy,
        coding=item.secondary_coding.factor(),
        advertising_event=item,
        power_dbm=item.tx_power_dbm,
    )
    if item.mode and advertising.event_is_current(item):
        packet = await link.receive_response(
            aux_channel,
            link.now_us + 1,
            item.secondary_phy,
        )
        if packet and advertising.event_is_current(item):
            await respond_to_aux_request(advertising, item, aux_channel, packet)


async def respond_to_aux_request(
    advertising: AdvertisingManager, item: Advertising, channel: int, packet: Received
) -> bool:
    """Answer AUX_SCAN_REQ or AUX_CONNECT_REQ on the secondary channel (Core Vol 6,
    Part B, 4.4.2.5.2, 4.4.2.7, 4.4.4.2).
    """
    link = advertising.link
    if item.mode == AdvertisingMode.SCANNABLE and accept_scan_request(
        advertising, item, packet
    ):
        await transmit_chain(
            advertising,
            channel,
            ExtendedPdu(
                address=bytes(item.address),
                random_address=item.address.is_random,
            ),
            item.scan_response,
            packet.end_us + LL_IFS_US,
            item.secondary_phy,
            coding=item.secondary_coding.factor(),
            advertising_event=item,
            power_dbm=item.tx_power_dbm,
        )
        return False
    if item.mode != AdvertisingMode.CONNECTABLE:
        return False
    result = accept_advertising_connection(link, item, packet)
    if result is None:
        return False
    # The on-air acknowledgment creates the Peripheral (Core 6.B.4.4.2.7).
    with link.roles.sending_connect_response(result):
        await link.transmit(
            channel,
            connect_response(result.self_address, result.peer),
            start_us=packet.end_us + LL_IFS_US,
            phy=item.secondary_phy,
            coding=item.secondary_coding.factor(),
            power_dbm=item.tx_power_dbm,
        )
    return True
