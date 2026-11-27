# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Advertiser radio exchanges, including periodic trains and response slots."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bscontroller.linklayer import (
    LL_ADV_PRIMARY_CHANNELS,
    LL_DATA_CHANNEL_COUNT,
    LL_EVENT_COUNTER_MASK,
    LL_IFS_US,
    LL_MAFS_US,
    LL_PERIODIC_INTERVAL_UNIT_US,
    LL_SCA_50_PPM,
    PDU_ADV_CHSEL_MASK,
    PDU_ADV_OFFSET_UNIT_30_US,
    PDU_ADV_OFFSET_UNIT_300_US,
    PDU_ADV_OFFSET_VALUE_MASK,
    PDU_ADV_RXADD_MASK,
    PDU_ADV_TXADD_MASK,
    AdvertisingMode,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising_event import (
    auxiliary_pdu,
    fragments,
    periodic_pdu,
    periodic_time_us,
    primary_offset_us,
    primary_pdu,
)
from bscontroller.linklayer.advertising_pdu import (
    AuxPtr,
    ExtendedPdu,
    SyncInfo,
    connect_response,
)
from bscontroller.linklayer.advertising_requests import (
    accept_advertising_connection,
    accept_scan_request,
)
from bscontroller.linklayer.channel_selection import channel_selection_2
from bscontroller.linklayer.pawr import SlotResponse
from bscontroller.linklayer.scheduler import RadioPriority
from bscontroller.linklayer.timing import window_widening
from bscontroller.radio.base import RADIO_SWITCH_TIME_US, Phy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bscontroller.linklayer.advertiser import AdvertisingManager
    from bscontroller.linklayer.discovery import Advertising
    from bscontroller.linklayer.pawr import Subevent, SubeventResponses
    from bscontroller.linklayer.resource import RadioResourceManager
    from bscontroller.radio.base import Radio, Received


async def advertise_legacy(advertising: AdvertisingManager, item: Advertising) -> None:
    """One legacy advertising event over the used primary channels in ascending order
    (Core Vol 6, Part B, 4.4.2.1-4.4.2.2).
    """
    link = advertising.link
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
            if not advertising.event_is_current(item):
                return
            # Do not start a high-duty packet that would run past advertising expiry.
            if (
                item.high_duty
                and item.deadline_us is not None
                and link.now_us + 1 + Phy.LE_1M.airtime_us(len(pdu)) > item.deadline_us
            ):
                await link.radio.advance_to_us(item.deadline_us)
                break
            end_us = await link.transmit(channel, pdu, power_dbm=item.tx_power_dbm)
            if item.pdu_type in (
                AdvertisingPduType.ADV_IND,
                AdvertisingPduType.ADV_DIRECT_IND,
                AdvertisingPduType.ADV_SCAN_IND,
            ):
                if not advertising.event_is_current(item):
                    return
                packet = await link.receive_response(
                    channel,
                    end_us,
                    search_deadline_us=item.deadline_us if item.high_duty else None,
                )
                if not advertising.event_is_current(item):
                    return
                if packet and await advertising_request(
                    advertising, item, channel, packet
                ):
                    # A connection ends the entire advertising event, including
                    # transmissions that were still due on other primary channels.
                    break


async def advertising_request(
    advertising: AdvertisingManager, item: Advertising, channel: int, packet: Received
) -> bool:
    """Answer SCAN_REQ with SCAN_RSP or accept CONNECT_IND after T_IFS (Core Vol 6, Part
    B, 4.4.2.3-4.4.2.5, 4.3.2).
    """
    link = advertising.link
    if item.pdu_type in (
        AdvertisingPduType.ADV_IND,
        AdvertisingPduType.ADV_SCAN_IND,
    ) and accept_scan_request(advertising, item, packet):
        payload = bytes(item.address) + item.scan_response
        await link.transmit(
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
        return False
    if item.pdu_type not in (
        AdvertisingPduType.ADV_IND,
        AdvertisingPduType.ADV_DIRECT_IND,
    ):
        return False
    result = accept_advertising_connection(advertising, item, packet)
    if result is None:
        return False
    advertising.accept_connection(result)
    return True


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
    result = accept_advertising_connection(advertising, item, packet)
    if result is None:
        return False
    # The on-air acknowledgment creates the Peripheral (Core 6.B.4.4.2.7).
    with advertising.sending_connect_response(result):
        await link.transmit(
            channel,
            connect_response(result.self_address, result.peer),
            start_us=packet.end_us + LL_IFS_US,
            phy=item.secondary_phy,
            coding=item.secondary_coding.factor(),
            power_dbm=item.tx_power_dbm,
        )
    return True


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


async def periodic_transmit(
    advertising: AdvertisingManager,
    item: Advertising,
) -> None:
    # A single radio cannot replay a train anchor that another event occupied.
    """Transmit one AUX_SYNC_IND train on its CSA #2 channel (Core Vol 6, Part B,
    4.4.2.12, 4.5.8.3).
    """
    link = advertising.link
    channel = channel_selection_2(
        item.periodic_counter, item.periodic_aa, item.periodic_channel_map
    )
    if not link.resource.admit(
        item,
        item.periodic_next_us - RADIO_SWITCH_TIME_US,
        item.periodic_next_us + periodic_time_us(item) + LL_MAFS_US,
        RadioPriority.PERIODIC_TX,
        earliest_us=link.radio.earliest_start_us(
            channel, item.secondary_phy, transmit=True
        )
        - RADIO_SWITCH_TIME_US,
    ):
        advertising.periodic_completed(item, missed=True)
        return

    with advertising.transmitting(item, periodic=True):
        await transmit_chain(
            advertising,
            channel,
            periodic_pdu(item),
            item.periodic_data,
            item.periodic_next_us,
            item.secondary_phy,
            coding=item.secondary_coding.factor(),
            advertising_event=item,
            access_address=item.periodic_aa,
            crc_init=item.periodic_crc,
            power_dbm=item.tx_power_dbm,
        )
        if advertising.event_is_current(item):
            advertising.periodic_completed(item, transmitted=True)


async def subevent_transmit(
    radio: Radio,
    resource: RadioResourceManager,
    item: Advertising,
    current: Subevent,
    pdu: bytes,
    transmit: Callable[..., Awaitable[int]],
) -> bool:
    """Transmit AUX_SYNC_SUBEVENT_IND if the shared radio is available."""
    phy = item.secondary_phy
    coding = item.secondary_coding.factor()
    start_us, channel = current.start_us, current.channel
    if not resource.admit(
        item,
        start_us - RADIO_SWITCH_TIME_US,
        start_us + phy.airtime_us(len(pdu), coding),
        RadioPriority.PERIODIC_TX,
        earliest_us=radio.earliest_start_us(channel, phy, transmit=True)
        - RADIO_SWITCH_TIME_US,
    ):
        return False
    await transmit(
        channel,
        pdu,
        start_us=start_us,
        access_address=item.periodic_aa,
        crc_init=item.periodic_crc,
        phy=phy,
        coding=coding,
        power_dbm=item.tx_power_dbm,
    )
    return True


async def response_receive(
    radio: Radio,
    resource: RadioResourceManager,
    item: Advertising,
    responses: SubeventResponses,
) -> SlotResponse:
    """Listen in the next requested response slot of a transmitted subevent.

    The responder times the slot from the AUX_SYNC_SUBEVENT_IND it received,
    so the window covers both clocks from that packet (Core Vol 6, Part B,
    4.2.4, 4.4.2.12.2).
    """
    timing, state = item.pawr, item.pawr_state
    assert timing is not None and state is not None
    slot = responses.slots[0]
    nominal = responses.slot_start_us(timing)
    phy = item.secondary_phy
    widening = window_widening(nominal - responses.start_us, LL_SCA_50_PPM)
    start = max(radio.now_us + 1, nominal - widening)
    end = nominal + widening + phy.address_us
    result = SlotResponse(slot, None)
    if end > radio.now_us and resource.admit(
        responses, start, end, RadioPriority.PERIODIC_RX
    ):
        packet = await radio.receive(
            responses.channel,
            end,
            start_us=start,
            access_address=state.rsp_aa,
            crc_init=item.periodic_crc,
            phy=phy,
        )
        if packet:
            try:
                ext = ExtendedPdu.decode(packet.pdu)
            except ValueError:
                ext = None
            if (
                ext is not None
                and ext.kind == AdvertisingPduType.AUX_SYNC_SUBEVENT_RSP
                and ext.mode == AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
            ):
                result = SlotResponse(slot, ext.data, packet.rssi, ext.tx_power)
    return result
