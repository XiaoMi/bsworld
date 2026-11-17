# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Periodic train transmission and synchronization receive RF procedures.

These stateless operations execute on the owning link's one radio and use its
admission and lifetime transitions. They never start an independent task.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bscontroller.linklayer import (
    LL_EVENT_COUNTER_MASK,
    LL_MAFS_US,
    LL_SCA_50_PPM,
    PDU_ADV_TYPE_MASK,
    AdvertisingMode,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising import ExtendedPdu
from bscontroller.linklayer.channel_selection import channel_selection_2
from bscontroller.linklayer.discovery import Advertising, PeriodicSync
from bscontroller.linklayer.extended import periodic_pdu, periodic_time_us
from bscontroller.linklayer.pawr import (
    ScheduledResponse,
    SlotResponse,
    SubeventResponses,
)
from bscontroller.linklayer.results import PeriodicPacketReceived
from bscontroller.linklayer.scheduler import RadioPriority
from bscontroller.linklayer.state import DataStatus
from bscontroller.linklayer.timing import window_widening
from bscontroller.radio.base import RADIO_SWITCH_TIME_US

if TYPE_CHECKING:
    from bscontroller.linklayer.link import RadioLink


async def periodic_transmit(link: RadioLink, item: Advertising) -> None:
    # A single radio cannot replay a train anchor that another event occupied.
    """Transmit one AUX_SYNC_IND train on its CSA #2 channel (Core Vol 6, Part B,
    4.4.2.12, 4.5.8.3).
    """
    link.roles.advertising.apply_periodic_channel_map(item)
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
        link.roles.advertising.periodic_completed(item, missed=True)
        return
    await link.extended.transmit_chain(
        channel,
        periodic_pdu(item),
        item.periodic_data,
        item.periodic_next_us,
        item.secondary_phy,
        access_address=item.periodic_aa,
        crc_init=item.periodic_crc,
        power_dbm=item.tx_power_dbm,
    )
    item.periodic_last_counter = item.periodic_counter
    link.roles.advertising.periodic_completed(item)


async def periodic_receive(link: RadioLink, sync: PeriodicSync) -> None:
    """Receive one periodic event in the Synchronization state (Core Vol 6, Part B,
    4.4.5.1, 4.2.4).
    """
    sync.apply_channel_map()
    info = sync.info
    assert info is not None
    widening = (
        window_widening(sync.next_us - sync.last_rx_us, info.sca)
        + sync.transfer_widening_us
    )
    # Search on both sides of the predicted anchor, allowing for clock drift,
    # SyncInfo offset quantization and time needed to recognize the Access Address.
    window_end = sync.next_us + widening + info.unit_us + sync.phy.address_us
    if window_end <= link.now_us or not link.resource.admit(
        sync,
        max(link.now_us + 1, sync.next_us - widening),
        window_end,
        RadioPriority.PERIODIC_RX,
    ):
        link.roles.synchronization.completed(sync, missed_until_us=window_end)
        return
    packet = await link.radio.receive(
        channel_selection_2(sync.counter, info.access_address, info.channel_map),
        window_end,
        start_us=max(link.now_us + 1, sync.next_us - widening),
        access_address=info.access_address,
        crc_init=info.crc_init,
        phy=sync.phy,
    )
    if packet:
        try:
            ext = ExtendedPdu.decode(packet.pdu)
        except ValueError:
            ext = None
        if ext is not None and ext.mode == AdvertisingMode.NONCONNECTABLE_NONSCANNABLE:
            synchronization = link.roles.synchronization
            if not synchronization.received(PeriodicPacketReceived(sync, packet, ext)):
                return
            # Decide once per event, before following the chain: the Link Layer
            # need not receive data it will not report (Core Vol 6, Part B,
            # 4.4.5.1.1). received() has already refreshed the timing and applied
            # the ACAD, which AUX_CHAIN_IND does not carry.
            if not synchronization.reportable(sync, ext.adi):
                synchronization.completed(sync)
                return

            def report_fragment(data, status, received):
                synchronization.report(
                    sync,
                    data,
                    received.rssi,
                    ext.tx_power if ext.tx_power is not None else None,
                    status,
                )

            data, status, _ = await link.extended.collect(
                packet,
                ext,
                on_fragment=report_fragment,
                access_address=info.access_address,
                crc_init=info.crc_init,
            )
            report_fragment(data, status, packet)
            synchronization.reported(sync, ext.adi, status)
    link.roles.synchronization.completed(sync)


async def subevent_transmit(link: RadioLink, item: Advertising) -> None:
    """Transmit the next AUX_SYNC_SUBEVENT_IND of a PAwR train, or the
    AUX_CONNECT_REQ the Host asked for in its place (Core Vol 6, Part B,
    4.4.2.12.2). Instances already behind the radio pass untransmitted.
    """
    manager = link.roles.advertising
    manager.skip_missed_subevents(item)
    current = manager.begin_subevent(item)
    if current is None:
        return
    initiating = link.roles.scanning.pawr_initiating(item, current.subevent)
    if initiating is not None:
        await link.extended.pawr_connect(
            item, initiating, current.channel, current.start_us
        )
        manager.end_subevent(item, current, transmitted=False, sent=True)
        return
    pdu = manager.subevent_indication(item, current).encode()
    phy = item.secondary_phy
    start_us, channel = current.start_us, current.channel
    if not link.resource.admit(
        item,
        start_us - RADIO_SWITCH_TIME_US,
        start_us + phy.airtime_us(len(pdu)),
        RadioPriority.PERIODIC_TX,
        earliest_us=link.radio.earliest_start_us(channel, phy, transmit=True)
        - RADIO_SWITCH_TIME_US,
    ):
        manager.end_subevent(item, current, transmitted=False)
        return
    await link._transmit(
        channel,
        pdu,
        start_us=start_us,
        access_address=item.periodic_aa,
        crc_init=item.periodic_crc,
        phy=phy,
        power_dbm=item.tx_power_dbm,
    )
    manager.end_subevent(item, current, transmitted=True)


async def response_receive(
    link: RadioLink, item: Advertising, responses: SubeventResponses
) -> None:
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
    start = max(link.now_us + 1, nominal - widening)
    end = nominal + widening + phy.address_us
    result = SlotResponse(slot, None)
    if end > link.now_us and link.resource.admit(
        responses, start, end, RadioPriority.PERIODIC_RX
    ):
        packet = await link.radio.receive(
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
    link.roles.advertising.slot_completed(item, responses, result)


async def subevent_receive(link: RadioLink, sync: PeriodicSync) -> None:
    """Listen for the next synchronized subevent of a PAwR train (Core Vol 6,
    Part B, 4.4.5.1.2).

    An AUX_CONNECT_REQ there replaces the AUX_SYNC_SUBEVENT_IND; one naming
    this device starts a Peripheral connection (4.4.2.12.2).
    """
    synchronization = link.roles.synchronization
    pawr, info = sync.pawr, sync.info
    assert pawr is not None and info is not None
    sync.apply_channel_map()
    info = sync.info
    assert info is not None
    subevent = pawr.next_subevent
    target = sync.next_us + pawr.timing.subevent_offset_us(subevent)
    widening = (
        window_widening(target - sync.last_rx_us, info.sca) + sync.transfer_widening_us
    )
    window_end = target + widening + info.unit_us + sync.phy.address_us
    start = max(link.now_us + 1, target - widening)
    if window_end <= link.now_us or not link.resource.admit(
        sync, start, window_end, RadioPriority.PERIODIC_RX
    ):
        synchronization.subevent_failed(sync, subevent)
        synchronization.subevent_completed(sync)
        return
    channel = channel_selection_2(
        (sync.counter ^ subevent) & LL_EVENT_COUNTER_MASK,
        info.access_address,
        info.channel_map,
    )
    packet = await link.radio.receive(
        channel,
        window_end,
        start_us=start,
        access_address=info.access_address,
        crc_init=info.crc_init,
        phy=sync.phy,
    )
    ext = None
    if (
        packet
        and packet.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.AUX_CONNECT_REQ
    ):
        if not synchronization.refreshed(sync, packet, subevent, channel):
            return
        await link.extended.pawr_accept(sync, channel, packet)
        synchronization.subevent_completed(sync)
        return
    if packet:
        try:
            ext = ExtendedPdu.decode(packet.pdu)
        except ValueError:
            ext = None
    if (
        ext is None
        or ext.kind != AdvertisingPduType.AUX_SYNC_SUBEVENT_IND
        or ext.mode != AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
    ):
        synchronization.subevent_failed(sync, subevent)
        synchronization.subevent_completed(sync)
        return
    if not synchronization.received(
        PeriodicPacketReceived(sync, packet, ext, subevent, channel)
    ):
        return
    if synchronization.reportable(sync, ext.adi):
        synchronization.report(
            sync,
            ext.data,
            packet.rssi,
            ext.tx_power,
            DataStatus.COMPLETE,
            subevent=subevent,
        )
        synchronization.reported(sync, ext.adi, DataStatus.COMPLETE)
    synchronization.subevent_completed(sync)


async def response_transmit(
    link: RadioLink, sync: PeriodicSync, response: ScheduledResponse
) -> None:
    """Send Host response data in its slot, on RspAA (Core Vol 6, Part B,
    4.4.2.12.2). The data is sent once, or discarded if the radio is busy.
    """
    pawr, info = sync.pawr, sync.info
    assert pawr is not None and info is not None
    link.roles.synchronization.response_sent(sync, response)
    if not link.resource.admit(
        response,
        response.start_us - RADIO_SWITCH_TIME_US,
        response.start_us + sync.phy.airtime_us(len(response.pdu)),
        RadioPriority.PERIODIC_RX,
        earliest_us=link.radio.earliest_start_us(
            response.channel, sync.phy, transmit=True
        )
        - RADIO_SWITCH_TIME_US,
    ):
        return
    await link._transmit(
        response.channel,
        response.pdu,
        start_us=response.start_us,
        access_address=pawr.rsp_aa,
        crc_init=info.crc_init,
        phy=sync.phy,
    )
