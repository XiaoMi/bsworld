# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Lower Tester device discovery procedures."""

import random

from bscontroller import hci
from bscontroller.linklayer import (
    LL_ADV_DELAY_MAX_US,
    LL_ADV_PRIMARY_CHANNELS,
    LL_IFS_US,
    PDU_ADV_ADDRESS_OFFSET,
    PDU_ADV_EXT_HEADER_LENGTH_BITS,
    PDU_ADV_RXADD_MASK,
    PDU_ADV_SCAN_REQ_SIZE,
    PDU_ADV_SECOND_ADDRESS_OFFSET,
    PDU_ADV_TXADD_MASK,
    PDU_ADV_TYPE_MASK,
    PDU_HEADER_SIZE,
    AdvertisingMode,
    AdvertisingPduType,
)
from bscontroller.radio.base import Phy
from tests.qualification.link import (
    SCAN_ADV_INTERVAL_US,
    SCAN_INTERVAL_US,
    SCAN_RESPONSE_IFS_TOLERANCE_US,
    SCANNING_SCAN_INTERVAL,
)
from tests.qualification.tester import (
    DUT_ADDRESS,
    adv_pdu,
    decode_extended,
    request_pdu,
    scan_parameters,
)

ErrorCode = hci.HCI_ErrorCode
LegacyEventType = hci.HCI_LE_Advertising_Report_Event.EventType
Operation = hci.HCI_LE_Set_Extended_Advertising_Data_Command.Operation

# The LL test suite permits a deviation of 2 us from the response T_IFS.
RESPONSE_IFS_TOLERANCE_US = 2


def _nonconnectable_extended_packet(packet, phy):
    """Validate the RF envelope for the configured public-address advertising set."""
    assert packet.valid_crc and packet.phy == phy
    assert packet.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.ADV_EXT_IND
    fields = decode_extended(packet.pdu)
    assert (
        packet.pdu[PDU_HEADER_SIZE] >> PDU_ADV_EXT_HEADER_LENGTH_BITS
        == AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
    )
    if 'address' in fields:
        assert not packet.pdu[0] & PDU_ADV_TXADD_MASK
    if 'target' in fields:
        assert not packet.pdu[0] & PDU_ADV_RXADD_MASK
    return fields


LOWER_ADDRESS_LSB_XOR = 90
LOWER_ADDRESS_MSB_XOR = 165

# LL.TS.p28 Section 4.1.5.2 defines the public scanner and address perturbations.
LOWER_TESTER_PUBLIC_ADDRESS = hci.Address(
    '12:34:56:78:9A:BC', hci.AddressType.PUBLIC_DEVICE
)
SCAN_RESPONSE_DATA = b'\x03\tLT'


def _check_scan_reports(tester, peer, kind, sent, accepted):
    report_type = hci.HCI_LE_Advertising_Report_Event.EventType
    expected = {
        AdvertisingPduType.ADV_IND: report_type.ADV_IND,
        AdvertisingPduType.ADV_NONCONN_IND: report_type.ADV_NONCONN_IND,
        AdvertisingPduType.ADV_SCAN_IND: report_type.ADV_SCAN_IND,
        AdvertisingPduType.ADV_DIRECT_IND: report_type.ADV_DIRECT_IND,
    }[kind]
    advertisements = responses = 0
    for event in list(tester.events):
        if not isinstance(event, hci.HCI_LE_Advertising_Report_Event):
            continue
        for report in event.reports:
            assert accepted, 'filtered advertiser was reported'
            assert report.address == peer and report.address_type == peer.address_type
            if report.event_type == report_type.SCAN_RSP:
                assert report.data == SCAN_RESPONSE_DATA
                responses += 1
            else:
                assert report.event_type == expected
                assert (
                    report.data == b''
                    if kind == AdvertisingPduType.ADV_DIRECT_IND
                    else report.data in sent
                )
                advertisements += 1
        tester.events.remove(event)
    return (advertisements, responses)


def _advertising_public_peer_addresses():
    """Common public peer followed by the MSB, LSB and both-byte variants of AdvA."""
    variants = [LOWER_TESTER_PUBLIC_ADDRESS]
    for msb, lsb in (
        (LOWER_ADDRESS_MSB_XOR, 0),
        (0, LOWER_ADDRESS_LSB_XOR),
        (LOWER_ADDRESS_LSB_XOR, LOWER_ADDRESS_MSB_XOR),
    ):
        address = bytearray(bytes(DUT_ADDRESS))
        address[-1] ^= msb
        address[0] ^= lsb
        variants.append(hci.Address(bytes(address), hci.AddressType.PUBLIC_DEVICE))
    return tuple(variants)


async def _scan_phase(
    tester, channel, peer, kind, *, active, accepted, intervals=20, full_duration=False
):
    deadline = tester.radio.now_us + intervals * SCAN_INTERVAL_US
    start = tester.radio.now_us + 100
    seen_adv = seen_response = requests = 0
    sent = set()
    generator = random.Random(0)
    delays = []
    for _ in range(3 * intervals // 2):
        value = generator.randrange(LL_ADV_DELAY_MAX_US + 1)
        delays.extend((value, LL_ADV_DELAY_MAX_US - value))
    generator.shuffle(delays)
    scannable = kind in (AdvertisingPduType.ADV_IND, AdvertisingPduType.ADV_SCAN_IND)
    for sequence, delay in enumerate(delays):
        data = sequence.to_bytes(2, 'little')
        sent.add(data)
        pdu = (
            request_pdu(kind, advertiser=DUT_ADDRESS, scanner=peer)
            if kind == AdvertisingPduType.ADV_DIRECT_IND
            else adv_pdu(kind, address=peer, data=data)
        )
        end = await tester.transmit(channel, pdu, start_us=start)
        request = await tester.receive(channel, duration_us=1000)
        if request is not None:
            assert active and scannable and accepted, 'unexpected scan request'
            assert request.valid_crc and request.phy == Phy.LE_1M
            assert len(request.pdu) == PDU_ADV_SCAN_REQ_SIZE
            assert request.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.SCAN_REQ
            assert request.pdu[
                PDU_ADV_ADDRESS_OFFSET:PDU_ADV_SECOND_ADDRESS_OFFSET
            ] == bytes(DUT_ADDRESS)
            assert request.pdu[PDU_ADV_SECOND_ADDRESS_OFFSET:] == bytes(peer)
            assert not request.pdu[0] & PDU_ADV_TXADD_MASK
            assert bool(request.pdu[0] & PDU_ADV_RXADD_MASK) == peer.is_random
            assert (
                abs(request.start_us - end - LL_IFS_US)
                <= SCAN_RESPONSE_IFS_TOLERANCE_US
            )
            await tester.transmit(
                channel,
                adv_pdu(
                    AdvertisingPduType.SCAN_RSP, address=peer, data=SCAN_RESPONSE_DATA
                ),
                start_us=request.end_us + LL_IFS_US,
            )
            requests += 1
        await tester.wait(1000)
        adv_count, response_count = _check_scan_reports(
            tester, peer, kind, sent, accepted
        )
        seen_adv += adv_count
        seen_response += response_count
        if (
            not full_duration
            and accepted
            and seen_adv
            and (not active or not scannable or (requests and seen_response))
        ):
            break
        start += SCAN_ADV_INTERVAL_US + delay
    else:
        await tester.wait(max(0, deadline - tester.radio.now_us))
        adv_count, response_count = _check_scan_reports(
            tester, peer, kind, sent, accepted
        )
        seen_adv += adv_count
        seen_response += response_count
    assert seen_adv > 0 if accepted else seen_adv == 0
    if accepted and active and scannable:
        assert requests and seen_response
    else:
        assert not requests and (not seen_response)


async def _scan_reports(
    tester, *, kind=AdvertisingPduType.ADV_NONCONN_IND, active=False, filtered=False
):
    """Exercise the selected scan/address/filter phases for the prescribed interval
    duration."""
    await tester.command(hci.HCI_LE_Clear_Filter_Accept_List_Command())
    common = _advertising_public_peer_addresses()[0]
    await tester.command(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(common.address_type, common)
    )
    interval = SCAN_INTERVAL_US // hci.HCI_LE_SCAN_INTERVAL_UNIT_US
    previous_channel = None
    for channel, peer in _scan_variants(kind, active, filtered):
        if channel != previous_channel or not filtered:
            if previous_channel is not None:
                await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(0, 0))
            await tester.command(
                scan_parameters(
                    le_scan_type=int(active),
                    scanning_filter_policy=int(filtered),
                    le_scan_interval=interval,
                    le_scan_window=interval,
                )
            )
            await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(1, 0))
            previous_channel = channel
        accepted = not filtered or peer == common
        await _scan_phase(
            tester,
            channel,
            peer,
            kind,
            active=active,
            accepted=accepted,
            full_duration=not active
            and (not filtered or kind == AdvertisingPduType.ADV_DIRECT_IND),
        )
    await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(0, 0))
    _check_scan_reports(tester, peer, kind, set(), accepted)


def _scan_variants(kind, active, filtered):
    common, msb, lsb, both = _advertising_public_peer_addresses()
    if kind == AdvertisingPduType.ADV_DIRECT_IND:
        return (
            (37, common),
            (38, lsb),
            (39, hci.Address(bytes(common), hci.AddressType.RANDOM_DEVICE)),
        )
    if filtered:
        peers = (
            (common, lsb, both)
            if active
            else (
                common,
                hci.Address(bytes(common), hci.AddressType.RANDOM_DEVICE),
                lsb,
                hci.Address(bytes(both), hci.AddressType.RANDOM_DEVICE),
            )
        )
        channels = LL_ADV_PRIMARY_CHANNELS if active else (37,)
        return tuple((channel, peer) for channel in channels for peer in peers)
    if active:
        return tuple(
            (channel, peer)
            for channel in LL_ADV_PRIMARY_CHANNELS
            for peer in (common, msb, lsb, both)
        )
    return (
        (37, common),
        (38, lsb),
        (39, hci.Address(bytes(msb), hci.AddressType.RANDOM_DEVICE)),
    )


# Sections 4.2.3.14 and 4.2.4.19: ten periodic events per ADI (one step plus nine
# repetitions).
ADI_REPORTS_PER_PHASE = 10
ExtendedReport = hci.HCI_LE_Extended_Advertising_Report_Event
LegacyReport = hci.HCI_LE_Advertising_Report_Event
Report = hci.HCI_LE_Extended_Advertising_Report_Event


async def _enable_legacy_scan(tester, *, filtered=False):
    interval = SCAN_INTERVAL_US // hci.HCI_LE_SCAN_INTERVAL_UNIT_US
    await tester.command(
        scan_parameters(
            le_scan_interval=interval,
            le_scan_window=interval,
            scanning_filter_policy=int(filtered),
        )
    )
    await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(1, 0))


# Tables4.2-62/64/65:two independent SIDs per primary/secondary pair.
PHY_PAIRS = (
    ((Phy.LE_1M, Phy.LE_1M),) * 2
    + ((Phy.LE_CODED, Phy.LE_CODED),) * 2
    + ((Phy.LE_1M, Phy.LE_2M),) * 2
)

# All supported primary scan PHYs are1M and Coded;2M is secondary-only.
SCAN_PHYS = hci.PhyBit.LE_1M | hci.PhyBit.LE_CODED


async def _enable_scan(tester, *, active):
    await tester.command(
        scan_parameters(
            extended=True,
            scanning_phys=SCAN_PHYS,
            scan_types=[int(active)] * 2,
            scan_intervals=[SCANNING_SCAN_INTERVAL * 2] * 2,
            scan_windows=[SCANNING_SCAN_INTERVAL] * 2,
        )
    )
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0))


# Scan_Max_Data IXIT for this DUT, equal to its declared1650-octet receive capacity.
SCAN_MAX_DATA = hci.HCI_LE_ADV_DATA_SIZE_MAX
_scan_compatibility_Flags = Report.EventType
PAWR_CONNECTION_COMPLETE = hci.HCI_LE_Enhanced_Connection_Complete_V2_Event


def _pawr_take_all(tester, kind):
    result = []
    while event := tester.take(lambda event: isinstance(event, kind)):
        result.append(event)
    return result
