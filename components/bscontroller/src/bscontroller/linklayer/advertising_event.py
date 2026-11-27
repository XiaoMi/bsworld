# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Advertising packet layout and airtime for validation and radio reservations."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from bscontroller.capabilities import CONTROLLER_ADV_DATA_SIZE_MAX
from bscontroller.linklayer import (
    LL_CHANNEL_MAP_SIZE,
    LL_DATA_CHANNEL_MAP_ALL,
    LL_IFS_US,
    LL_MAFS_US,
    LL_PERIODIC_INTERVAL_MIN,
    LL_PERIODIC_INTERVAL_UNIT_US,
    PDU_ADV_ADDRESS_SIZE,
    PDU_ADV_ADI_DID_BITS,
    PDU_ADV_ADI_SIZE,
    PDU_ADV_AUX_CONNECT_RSP_SIZE,
    PDU_ADV_AUX_PTR_SIZE,
    PDU_ADV_CONNECT_SIZE,
    PDU_ADV_EXT_HEADER_SIZE_MAX,
    PDU_ADV_OFFSET_UNIT_30_US,
    PDU_ADV_SCAN_REQ_SIZE,
    PDU_CONTROL_INSTANT_SIZE,
    PDU_HEADER_SIZE,
    AdvertisingMode,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising_pdu import (
    AuxPtr,
    ExtendedPdu,
    SyncInfo,
    align_offset_us,
)
from bscontroller.linklayer.bis import BIG_INFO_AD_TYPE
from bscontroller.linklayer.discovery import AD_TYPE_CHANNEL_MAP_UPDATE_INDICATION
from bscontroller.linklayer.timing import LL_RESPONSE_START_TOLERANCE_US
from bscontroller.radio.base import (
    PHY_CODED_S8_FACTOR,
    RADIO_SWITCH_TIME_US,
    Phy,
)

if TYPE_CHECKING:
    from bscontroller.linklayer.discovery import Advertising


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


def chain_time_us(packets, phy: Phy, coding: int) -> int:
    return sum(packet.aux_ptr.offset_us for packet in packets[:-1]) + phy.airtime_us(
        len(packets[-1].encode()), coding
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


def _scan_exchange_us(response_size: int) -> int:
    """From the end of a legacy advertisement through the end of SCAN_RSP."""
    return (
        2 * LL_IFS_US
        + LL_RESPONSE_START_TOLERANCE_US
        + Phy.LE_1M.airtime_us(PDU_ADV_SCAN_REQ_SIZE)
        + Phy.LE_1M.airtime_us(response_size)
    )


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
        response_us = _scan_exchange_us(
            PDU_HEADER_SIZE + PDU_ADV_ADDRESS_SIZE + len(item.scan_response)
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
