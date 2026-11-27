# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Radio exchanges owned by periodic synchronization and PAwR responders."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bscontroller.linklayer import (
    LL_EVENT_COUNTER_MASK,
    LL_IFS_US,
    PDU_ADV_CONNECT_SIZE,
    AdvertisingMode,
)
from bscontroller.linklayer.advertising_pdu import (
    ExtendedPdu,
    advertising_request_peer,
    connect_response,
)
from bscontroller.linklayer.advertising_requests import connection_request_parameters
from bscontroller.linklayer.channel_selection import channel_selection_2
from bscontroller.linklayer.extended_receive import receive_chain
from bscontroller.linklayer.pawr import RESPONSE_TX_POWER_DBM
from bscontroller.linklayer.results import ConnectionAccepted, PeriodicPacketReceived
from bscontroller.linklayer.scheduler import RadioPriority
from bscontroller.linklayer.state import Role
from bscontroller.linklayer.timing import auxiliary_window_delay_us, window_widening
from bscontroller.radio.base import RADIO_SWITCH_TIME_US

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bscontroller.linklayer.discovery import PeriodicSync
    from bscontroller.linklayer.link import RadioLink
    from bscontroller.linklayer.pawr import ScheduledResponse
    from bscontroller.linklayer.resource import RadioResourceManager
    from bscontroller.linklayer.synchronization import SynchronizationManager
    from bscontroller.radio.base import Radio, Received


async def periodic_receive(
    synchronization: SynchronizationManager,
    sync: PeriodicSync,
    observe_big_info: Callable[[PeriodicSync, bytes, int], None],
) -> None:
    """Receive one periodic event in the Synchronization state (Core Vol 6, Part B,
    4.4.5.1, 4.2.4).
    """
    link = synchronization.link
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
        synchronization.completed(sync, missed_until_us=window_end)
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
            if not synchronization.received(PeriodicPacketReceived(sync, packet, ext)):
                return
            # Decide once per event, before following the chain: the Link Layer
            # need not receive data it will not report (Core Vol 6, Part B,
            # 4.4.5.1.1). received() has already refreshed the timing and applied
            # the ACAD, which AUX_CHAIN_IND does not carry.
            if not synchronization.reportable(sync, ext.adi):
                observe_big_info(sync, ext.acad, packet.start_us)
                synchronization.completed(sync)
                return

            async for fragment in receive_chain(
                link.radio,
                link.resource,
                packet,
                ext,
                is_current=lambda: synchronization.active.get(sync.handle) is sync,
                access_address=info.access_address,
                crc_init=info.crc_init,
            ):
                synchronization.report(
                    sync, fragment.data, fragment.rssi, ext.tx_power, fragment.status
                )
            observe_big_info(sync, ext.acad, packet.start_us)
            synchronization.reported(sync, ext.adi, fragment.status)
    synchronization.completed(sync)


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


async def pawr_accept(
    link: RadioLink, sync: PeriodicSync, channel: int, packet: Received
) -> None:
    """Answer an AUX_CONNECT_REQ naming this device in a synchronized PAwR
    subevent, then become its Peripheral (Core Vol 6, Part B, 4.4.2.12.2).

    AdvA must be one of this device's addresses; InitA becomes the peer.
    """
    pdu = packet.pdu
    if len(pdu) != PDU_ADV_CONNECT_SIZE or sync.info is None:
        return
    for own in (link.public_address, link.random_address):
        if own is not None and (peer := advertising_request_peer(pdu, own)) is not None:
            break
    else:
        return
    if not link.roles.connections.can_accept(peer):
        return
    parameters = connection_request_parameters(link, pdu)
    if parameters is None:
        return
    await link.transmit(
        channel,
        connect_response(own, peer),
        start_us=packet.end_us + LL_IFS_US,
        access_address=sync.info.access_address,
        crc_init=sync.info.crc_init,
        phy=packet.phy,
    )
    link.roles.accepted(
        ConnectionAccepted(
            parameters,
            peer,
            Role.PERIPHERAL,
            packet.end_us,
            csa2=True,
            self_address=own,
            window_delay_us=auxiliary_window_delay_us(packet.phy),
            phy=packet.phy,
            pawr_sync=sync,
        )
    )
