# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""PAwR subevent and response-slot radio I/O (Core Vol 6, Part B)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bscontroller.linklayer import (
    LL_EVENT_COUNTER_MASK,
    LL_SCA_50_PPM,
    AdvertisingMode,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising_pdu import ExtendedPdu
from bscontroller.linklayer.channel_selection import channel_selection_2
from bscontroller.linklayer.discovery import Advertising, PeriodicSync
from bscontroller.linklayer.pawr import (
    RESPONSE_TX_POWER_DBM,
    ScheduledResponse,
    SlotResponse,
    Subevent,
    SubeventResponses,
)
from bscontroller.linklayer.scheduler import RadioPriority
from bscontroller.linklayer.timing import window_widening
from bscontroller.radio.base import RADIO_SWITCH_TIME_US

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bscontroller.linklayer.resource import RadioResourceManager
    from bscontroller.radio.base import Radio, Received


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


async def subevent_receive(
    radio: Radio,
    resource: RadioResourceManager,
    sync: PeriodicSync,
    subevent: int,
) -> tuple[Received, int] | None:
    """Listen in a synchronized subevent (Core Vol 6, Part B, 4.4.5.1.2)."""
    pawr, info = sync.pawr, sync.info
    assert pawr is not None and info is not None
    target = sync.next_us + pawr.timing.subevent_offset_us(subevent)
    widening = (
        window_widening(target - sync.last_rx_us, info.sca) + sync.transfer_widening_us
    )
    window_end = target + widening + info.unit_us + sync.phy.address_us
    start = max(radio.now_us + 1, target - widening)
    if window_end <= radio.now_us or not resource.admit(
        sync, start, window_end, RadioPriority.PERIODIC_RX
    ):
        return None
    channel = channel_selection_2(
        (sync.counter ^ subevent) & LL_EVENT_COUNTER_MASK,
        info.access_address,
        info.channel_map,
    )
    packet = await radio.receive(
        channel,
        window_end,
        start_us=start,
        access_address=info.access_address,
        crc_init=info.crc_init,
        phy=sync.phy,
    )
    return (packet, channel) if packet is not None and packet.pdu else None


async def response_transmit(
    radio: Radio,
    resource: RadioResourceManager,
    sync: PeriodicSync,
    response: ScheduledResponse,
    pdu: bytes,
    transmit: Callable[..., Awaitable[int]],
) -> None:
    """Send Host response data in its slot, on RspAA (Core Vol 6, Part B,
    4.4.2.12.2). The data is sent once, or discarded if the radio is busy.
    """
    pawr, info = sync.pawr, sync.info
    assert pawr is not None and info is not None
    if not resource.admit(
        response,
        response.start_us - RADIO_SWITCH_TIME_US,
        response.start_us + response.airtime_us,
        RadioPriority.PERIODIC_RX,
        earliest_us=radio.earliest_start_us(response.channel, sync.phy, transmit=True)
        - RADIO_SWITCH_TIME_US,
    ):
        return
    await transmit(
        response.channel,
        pdu,
        start_us=response.start_us,
        access_address=pawr.rsp_aa,
        crc_init=info.crc_init,
        phy=sync.phy,
        power_dbm=RESPONSE_TX_POWER_DBM,
    )
