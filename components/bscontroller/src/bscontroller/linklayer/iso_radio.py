# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""BIS RF event execution on the shared radio (Core Vol 6, Part B, 4.4.6).

BroadcastManager owns lifetimes and reservations. This executor applies the
subevent schedule, packet counters, channel selection and CCM, reporting each
distinct associated payload to the shared ISO data path exactly once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidTag

from bscontroller.linklayer.channel_selection import isochronous_channels
from bscontroller.linklayer.isoal import IsoPdu

if TYPE_CHECKING:
    from bscontroller.linklayer.broadcast import Big, BroadcastManager
    from bscontroller.linklayer.link import RadioLink


async def run_big_event(link: RadioLink, manager: BroadcastManager, group: Big) -> None:
    info = group.info
    assert info is not None
    window = group.reservation(link.sleep_clock_accuracy)
    admitted = link.resource.admit(
        group, window.start_us, window.start_us + window.duration_us, window.priority
    )
    received_pdus = (
        {n: [None] * info.bn for n in group.numbers} if not group.transmit else {}
    )
    if not admitted:
        manager.completed(group, received_pdus)
        return
    # Values are zero-based here; on-air subevent numbers are 1..NSE.
    subevents = sorted(
        (
            (number - 1) * info.bis_spacing_us + subevent * info.sub_interval_us,
            number,
            handle,
            subevent,
        )
        for number, handle in zip(group.numbers, group.stream_handles)
        for subevent in range(
            info.nse
            if group.transmit or group.sync_config is None or not group.sync_config.mse
            else min(info.nse, group.sync_config.mse)
        )
    )
    channels = {
        number: isochronous_channels(
            group.counter, info.access_address(number), info.channel_map, info.nse
        )
        for number in group.numbers
    }
    control_announced = True
    received = False
    for offset, number, handle, subevent in subevents:
        nominal = group.next_us + offset
        associated = group.associated_event(subevent)
        # This receiver uses immediate repetitions, with no pre-transmission
        # cache. A future associated payload must never replace a missing SDU
        # of the current event (LL.TS 4.11.5.6, No Pre-Transmission).
        if not group.transmit and associated != group.counter:
            continue
        payload_count = associated * info.bn + subevent % info.bn
        channel = channels[number][subevent]
        if group.transmit:
            pdu = manager.transmit_payload(
                group, associated, number, subevent % info.bn
            )
            if pdu is None:
                return
            header = pdu.llid | group.cssn << 2 | (0x20 if group.control else 0)
            data = (
                group.ciphers[number].encrypt(header, pdu.data, payload_count)
                if info.encrypted
                else pdu.data
            )
            if nominal < link.radio.earliest_start_us(channel, info.phy, transmit=True):
                continue
            await link.transmit(
                channel,
                bytes([header, len(data)]) + data,
                start_us=nominal,
                access_address=info.access_address(number),
                crc_init=info.crc_init(number),
                phy=info.phy,
            )
            if not manager.owns(group):
                return
            manager.iso.get_stream(handle, True).transmitted(payload_count)
        else:
            widening = group.receive_window_us(
                nominal, link.sleep_clock_accuracy, active=received
            )
            until = nominal + widening + info.phy.address_us
            if group.deadline_us:
                until = min(until, group.deadline_us)
            start = max(link.now_us + 1, nominal - widening)
            if start >= until:
                continue
            manager.listening(group)
            packet = await link.radio.receive(
                channel,
                until,
                start_us=start,
                access_address=info.access_address(number),
                crc_init=info.crc_init(number),
                phy=info.phy,
                accept_bad_crc=True,
            )
            if not manager.owns(group):
                return
            if packet is None:
                continue
            if not packet.valid_crc:
                manager.crc_error(group, number)
                continue
            if len(packet.pdu) < 2:
                continue
            header, length = packet.pdu[:2]
            if (
                length != len(packet.pdu) - 2
                or length > info.max_pdu + 4 * info.encrypted
                or header & 3 == 3
            ):
                continue
            try:
                data = (
                    group.ciphers[number].decrypt(header, packet.pdu[2:], payload_count)
                    if info.encrypted
                    else packet.pdu[2:]
                )
            except InvalidTag:
                manager.mic_failure(group)
                return
            received = True
            manager.received(group, packet.start_us, offset)
            if not manager.owns(group):
                return
            control_announced &= bool(header & 0x20)
            index = subevent % info.bn
            if received_pdus[number][index] is None:
                received_pdus[number][index] = IsoPdu(header & 3, data)
    control_needed = (
        group.control is not None if group.transmit else received and control_announced
    )
    if control_needed:
        await _control(link, manager, group)
    manager.completed(group, received_pdus)


async def _control(link: RadioLink, manager: BroadcastManager, group: Big) -> None:
    info = group.info
    assert info is not None
    nominal = group.next_us + info.control_offset_us
    channel = isochronous_channels(
        group.counter, info.access_address(0), info.channel_map, 1
    )[0]
    counter = group.counter * info.bn
    if group.transmit:
        assert group.control is not None
        header = 3 | group.cssn << 2
        payload = group.control.payload
        data = (
            group.ciphers[0].encrypt(header, payload, counter)
            if info.encrypted
            else payload
        )
        if nominal >= link.radio.earliest_start_us(channel, info.phy, transmit=True):
            await link.transmit(
                channel,
                bytes([header, len(data)]) + data,
                start_us=nominal,
                access_address=info.access_address(0),
                crc_init=info.crc_init(0),
                phy=info.phy,
            )
        return
    widening = group.receive_window_us(nominal, link.sleep_clock_accuracy, active=True)
    start = max(link.now_us + 1, nominal - widening)
    until = min(nominal + widening + info.phy.address_us, group.deadline_us)
    if start >= until:
        return
    packet = await link.radio.receive(
        channel,
        until,
        start_us=start,
        access_address=info.access_address(0),
        crc_init=info.crc_init(0),
        phy=info.phy,
    )
    if not manager.owns(group):
        return
    if packet is None or not packet.valid_crc or len(packet.pdu) < 2:
        return
    header, length = packet.pdu[:2]
    if header & 3 != 3 or length != len(packet.pdu) - 2 or not length:
        return
    try:
        data = (
            group.ciphers[0].decrypt(header, packet.pdu[2:], counter)
            if info.encrypted
            else packet.pdu[2:]
        )
    except InvalidTag:
        manager.mic_failure(group)
        return
    manager.control_received(group, data, header >> 2 & 7)
