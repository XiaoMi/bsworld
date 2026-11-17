# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Independent PAwR Lower Tester: ACAD, XOR-counter hopping and response slots.

The peer uses raw CEAP fields and its own timing arithmetic. It does not import
Controller PAwR state, encoders, timing validation or scheduling functions.
"""

import struct

from bscontroller import hci
from tests.qualification.periodic import PeriodicTrain
from tests.qualification.tester import (
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
    create_sync,
    csa2_channel,
    decode_aux,
    decode_extended,
    ext_pdu,
    extended_parameters,
    follow_auxiliary,
    receive_extended,
    scan_parameters,
)

REQUEST = hci.HCI_LE_Periodic_Advertising_Subevent_Data_Request_Event
REPORT = hci.HCI_LE_Periodic_Advertising_Report_V2_Event
RESPONSES = hci.HCI_LE_Periodic_Advertising_Response_Report_Event
ESTABLISHED = hci.HCI_LE_Periodic_Advertising_Sync_Established_V2_Event
SET_DATA = hci.HCI_LE_Set_Periodic_Advertising_Subevent_Data_Command
SET_RESPONSE = hci.HCI_LE_Set_Periodic_Advertising_Response_Data_Command
SET_SUBEVENTS = hci.HCI_LE_Set_Periodic_Sync_Subevent_Command


def parameters(*, handle=0, interval=800, timing=(10, 80, 8, 25), slots=25):
    return hci.HCI_LE_Set_Periodic_Advertising_Parameters_V2_Command(
        handle, interval, interval, 0, *timing, slots
    )


def timing_information(acad):
    offset = 0
    while offset < len(acad):
        size = acad[offset]
        assert size and offset + size < len(acad)
        if acad[offset + 1] == 0x32:
            assert size == 9
            aa, *timing = struct.unpack('<IBBBB', acad[offset + 2 : offset + size + 1])
            return aa, tuple(timing)
        offset += size + 1
    raise AssertionError('no Periodic Advertising Response Timing Information')


async def configure(
    tester,
    *,
    interval=800,
    timing=(10, 80, 8, 25),
    slots=25,
    adi=False,
    own_type=0,
    random_address=None,
):
    """Configure and enable one PAwR set; keep extended announcements enabled."""
    await tester.masks(le=(1 << 64) - 1)
    await tester.command(extended_parameters(own_address_type=own_type))
    if random_address is not None:
        await tester.command(
            hci.HCI_LE_Set_Advertising_Set_Random_Address_Command(0, random_address)
        )
    await tester.command(parameters(interval=interval, timing=timing, slots=slots))
    await tester.command(
        hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(3 if adi else 1, 0)
    )
    await tester.command(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0], [0], [0])
    )


async def advertiser(tester, **options):
    """Read an actual AUX_ADV_IND and derive all train timing from its fields."""
    await configure(tester, **options)
    for _ in range(options.get('interval', 800) * 1250 // 20_000 + 20):
        _, packet, fields = await receive_extended(tester)
        if 'sync' in fields:
            break
    else:
        raise AssertionError('no representable SyncInfo before the train starts')
    train = PeriodicTrain.from_advertisement(packet, fields)
    train.rsp_aa, train.pawr = timing_information(fields['acad'])
    assert train.rsp_aa != train.access_address
    assert train.pawr == options.get('timing', (10, 80, 8, 25))
    assert train.interval == options.get('interval', 800)
    return train, fields


async def connected_advertiser(session, *, attempts=160):
    """Read the IUT's actual SyncInfo/ACAD while maintaining its ACL connection."""
    tester, link = session.tester, session.link
    for _ in range(attempts):
        session.catch_up()
        remaining = link.anchor - tester.radio.now_us - 2000
        packet = (
            await tester.receive(37, duration_us=remaining) if remaining > 500 else None
        )
        if packet is not None and packet.valid_crc:
            fields = decode_extended(packet.pdu)
            if 'aux' in fields:
                _, offset = decode_aux(fields['aux'])
                if packet.start_us + offset + 1500 < link.anchor:
                    _, auxiliary, fields = await follow_auxiliary(
                        tester, packet, fields
                    )
                    if 'sync' in fields:
                        train = PeriodicTrain.from_advertisement(auxiliary, fields)
                        train.rsp_aa, train.pawr = timing_information(fields['acad'])
                        assert train.rsp_aa != train.access_address
                        return train, fields
        await session.step()
    raise AssertionError('no SyncInfo/ACAD announcement between ACL events')


def response_data(events, subevent, slots):
    """Reassemble complete response fragments and reject reports for other slots."""
    partial, received = {}, {}
    for event in events:
        assert event.advertising_handle == 0 and event.subevent == subevent
        assert event.tx_status == 0
        for entry in event.responses:
            slot = entry.response_slot
            assert slot in slots, 'response reported outside the requested slots'
            if entry.data_status == 0xFF:
                assert not entry.data and slot not in partial
                continue
            assert entry.data_status in (0, 1)
            assert slot not in received, 'response completed more than once'
            partial.setdefault(slot, bytearray()).extend(entry.data)
            if entry.data_status == 0:
                received[slot] = bytes(partial.pop(slot))
    assert not partial, 'response fragments have no final Complete status'
    return received


async def stop_advertising(tester):
    await tester.command(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(0, [], [], [])
    )
    await tester.command(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0, 0))
    await tester.command(hci.HCI_LE_Clear_Advertising_Sets_Command())


async def receive_indication(tester, train, subevent, *, required=True):
    start = train.anchor + subevent * train.pawr[1] * 1250
    assert start > tester.radio.now_us, (start, tester.radio.now_us)
    packet = await tester.receive(
        csa2_channel(train.counter ^ subevent, train.access_address, train.channel_map),
        start_us=start - 1,
        duration_us=start - tester.radio.now_us + train.offset_unit + 100,
        **train.parameters,
    )
    assert packet is not None or not required, ('no subevent', train.counter, subevent)
    if packet is not None:
        assert packet.valid_crc
        assert start <= packet.start_us <= start + train.offset_unit
    return packet


async def transmit_indication(
    tester, train, subevent=0, data=b'indication', *, at=None, acad=b''
):
    start = train.anchor + subevent * train.pawr[1] * 1250 if at is None else at
    channel = csa2_channel(
        train.counter ^ subevent, train.access_address, train.channel_map
    )
    await tester.transmit(
        channel, ext_pdu(data=data, acad=acad), start_us=start, **train.parameters
    )
    return start, channel


async def scanner(
    tester,
    *,
    timing=(5, 16, 8, 10),
    interval=100,
    acquire=True,
    observe_us=0,
    options=0,
):
    """Announce an independently encoded PAwR train and optionally establish it."""
    # A prior round may have queued failed-reception reports before its
    # Terminate Sync completed; they do not belong to the new acquisition.
    while tester.take(lambda event: isinstance(event, REPORT)) is not None:
        pass
    await tester.masks(le=(1 << 64) - 1)
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))
    await tester.command(
        scan_parameters(
            extended=True, scan_types=[0], scan_intervals=[16], scan_windows=[16]
        )
    )
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0))
    train = PeriodicTrain(
        tester.radio.now_us + 100_000,
        interval=interval,
        access_address=0x9DDE2508,
        crc_init=0x445566,
        pawr=timing,
        rsp_aa=0x9A328370,
    )
    await train.announce(tester, address=LOWER_PERIPHERAL_PUBLIC_ADDRESS)
    announcement = await tester.event(hci.HCI_LE_Extended_Advertising_Report_Event)
    assert any(
        report.address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
        and report.periodic_advertising_interval == interval
        and report.advertising_sid == 3
        for report in announcement.reports
    )
    if observe_us:
        deadline = tester.radio.now_us + observe_us
        while tester.radio.now_us < deadline:
            await train.announce(tester, address=LOWER_PERIPHERAL_PUBLIC_ADDRESS)
            await transmit_indication(tester, train)
            train.next_event()
    train.anchor = max(train.anchor, tester.radio.now_us + 100_000)
    await tester.command(
        create_sync(
            options=options,
            advertising_sid=3,
            advertiser_address_type=LOWER_PERIPHERAL_PUBLIC_ADDRESS.address_type,
            advertiser_address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
            sync_timeout=1000,
        )
    )
    for channel in (37, 38, 39):
        await train.announce(
            tester, address=LOWER_PERIPHERAL_PUBLIC_ADDRESS, primary_channel=channel
        )
    if not acquire:
        return train, None
    await transmit_indication(tester, train)
    event = await tester.event(ESTABLISHED)
    assert event.status == 0 and event.num_subevents == timing[0]
    assert event.advertiser_address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
    if not options & 2:
        report = await tester.event(REPORT)
        assert report.data_status == 0 and report.data == b'indication'
    return train, event.sync_handle


async def next_request(tester, *, limit_us=5_000_000):
    """Wait for a new request without imposing the generic 100-ms HCI bound."""
    end = tester.radio.now_us + limit_us
    while tester.radio.now_us < end:
        if event := tester.take(lambda event: isinstance(event, REQUEST)):
            return event
        await tester.wait(10_000)
    raise AssertionError('no Subevent Data Request')
