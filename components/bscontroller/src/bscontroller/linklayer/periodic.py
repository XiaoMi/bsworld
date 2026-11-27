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
    LL_MAFS_US,
    AdvertisingMode,
)
from bscontroller.linklayer.advertising_pdu import ExtendedPdu
from bscontroller.linklayer.channel_selection import channel_selection_2
from bscontroller.linklayer.discovery import Advertising, PeriodicSync
from bscontroller.linklayer.extended import (
    periodic_pdu,
    periodic_time_us,
    transmit_chain,
)
from bscontroller.linklayer.extended_receive import collect_chain
from bscontroller.linklayer.results import PeriodicPacketReceived
from bscontroller.linklayer.scheduler import RadioPriority
from bscontroller.linklayer.timing import window_widening
from bscontroller.radio.base import RADIO_SWITCH_TIME_US

if TYPE_CHECKING:
    from collections.abc import Callable

    from bscontroller.linklayer.advertiser import AdvertisingManager
    from bscontroller.linklayer.synchronization import SynchronizationManager


async def periodic_transmit(
    advertising: AdvertisingManager,
    item: Advertising,
    handle: int,
    big_info: Callable[[int, int], bytes],
) -> None:
    # A single radio cannot replay a train anchor that another event occupied.
    """Transmit one AUX_SYNC_IND train on its CSA #2 channel (Core Vol 6, Part B,
    4.4.2.12, 4.5.8.3).
    """
    link = advertising.link
    advertising.apply_periodic_channel_map(item)
    item.big_info = big_info(handle, item.periodic_next_us)
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
            item.periodic_last_counter = item.periodic_counter
            advertising.periodic_completed(item)


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

            def report_fragment(data, status, received):
                synchronization.report(
                    sync,
                    data,
                    received.rssi,
                    ext.tx_power if ext.tx_power is not None else None,
                    status,
                )

            data, status, _ = await collect_chain(
                link.radio,
                link.resource,
                packet,
                ext,
                is_current=lambda: synchronization.active.get(sync.handle) is sync,
                on_fragment=report_fragment,
                access_address=info.access_address,
                crc_init=info.crc_init,
            )
            report_fragment(data, status, packet)
            observe_big_info(sync, ext.acad, packet.start_us)
            synchronization.reported(sync, ext.adi, status)
    synchronization.completed(sync)
