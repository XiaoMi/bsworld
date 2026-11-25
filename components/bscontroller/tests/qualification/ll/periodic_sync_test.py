# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.2: Periodic synchronization and PAwR scanner procedures."""

import csv
import random
from bisect import bisect_right
from dataclasses import replace
from itertools import pairwise
from types import SimpleNamespace

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_ADV_DELAY_MAX_US,
    LL_CHANNEL_MAP_SIZE,
    LL_DATA_CHANNEL_COUNT,
    LL_DATA_CHANNEL_MAP_ALL,
    LL_EVENT_COUNTER_MASK,
    LL_MAFS_US,
    LL_PERIODIC_INTERVAL_UNIT_US,
    LL_PERIODIC_SYNC_ESTABLISHMENT_EVENTS,
    LL_SCA_500_PPM,
    PDU_ADV_ADI_DID_BITS,
    PDU_ADV_ADI_DID_MASK,
    PDU_ADV_AUX_CA_MASK,
    PDU_ADV_CHSEL_MASK,
    PDU_ADV_EXT_FLAGS_RESERVED_MASK,
    PDU_ADV_EXT_HEADER_OFFSET,
    PDU_ADV_LEGACY_DATA_SIZE_MAX,
    PDU_ADV_OFFSET_UNIT_30_US,
    PDU_ADV_OFFSET_UNIT_300_US,
    PDU_ADV_OFFSET_VALUE_MASK,
    PDU_ADV_RESERVED_MASK,
    PDU_ADV_RXADD_MASK,
    PDU_ADV_SYNC_INFO_SIZE,
    PDU_ADV_SYNC_OFFSET_RESERVED_MASK,
    PDU_ADV_TXADD_MASK,
)
from bscontroller.linklayer.timing import LL_CLOCK_JITTER_US
from bscontroller.radio.base import PHY_TIME_UNITS_PER_SECOND, Phy
from tests.qualification import pawr as pawr_peer
from tests.qualification import privacy as privacy_peer
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.discovery import (
    ADI_REPORTS_PER_PHASE,
    PAWR_CONNECTION_COMPLETE,
    PHY_PAIRS,
    SCAN_MAX_DATA,
    ErrorCode,
    ExtendedReport,
    Report,
    _enable_scan,
    _pawr_take_all,
)
from tests.qualification.link import (
    ADVERTISING_SID,
    AUXILIARY_OFFSET_US,
    REPORT_DELIVERY_US,
    SCANNING_SCAN_INTERVAL,
)
from tests.qualification.peer import PEER_FEATURES, PeerLink, exchange_features
from tests.qualification.periodic import PeriodicAdvertiser, PeriodicTrain
from tests.qualification.tester import (
    DUT_ADDRESS,
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
    PEER_ADDRESS,
    LinkParameters,
    aux_pointer,
    check_access_address,
    create_sync,
    csa2_channel,
    decode_extended,
    ext_pdu,
    le_features,
    request_pdu,
    scan_parameters,
)
from tests.qualification.transfer import SyncIndication, TransferSession, lower_train
from tests.qualification.transfer import disconnect as _disconnect
from tests.qualification.transfer import receive_transfer as _received
from tests.qualification.transfer import set_transfer_mode as _transfer_mode
from tests.qualification.transfer import transfer_link as _link
from tests.support import AD_CHANNEL_MAP_UPDATE_INDICATION
from tests.support import advertising_data as build_advertising_data

PROCEDURES = ProcedureMap()

SyncOptions = hci.HCI_LE_Periodic_Advertising_Create_Sync_Command.Options

# LL/DDI/SCN/BV-69-C: after each reported event, ten events repeat its ADI (Step 10
# or 15 plus nine repetitions).
ADI_DUPLICATES_PER_PHASE = 10

# Selected100-ms base interval plus500-us advertising delay.
PERIODIC_ADVERTISING_INTERVAL_US = 100500
CHAIN_COUNT = 2
CHAIN_DATA_LENGTH = 249

# Maximum representable AuxPtr distance, as requested by Step8.
CHAIN_OFFSET_US = PDU_ADV_OFFSET_VALUE_MASK * PDU_ADV_OFFSET_UNIT_300_US
COMPATIBILITY_ADV_INTERVAL_US = 20500
COMPATIBILITY_AUX_CHANNEL = 0
COMPATIBILITY_AUX_OFFSET_US = 600

# Section4.2.4.19: one hundred seeded random AdvData octets per periodic PDU.
COMPATIBILITY_DATA_LENGTH = 100

# Selected100-ms periodic interval and20-ms base interval plus500-us AdvDelay.
COMPATIBILITY_INTERVAL = 80

# Selected public primary channel and secondary channel; AuxPtr spacing is600us.
COMPATIBILITY_PRIMARY_CHANNEL = 37

# Section4.2.4.8: first min(100, Scan_Max_Data/3), then two min(249, remaining/2).
FIRST_DATA_LENGTH = 100

# Selected five-second periodic interval fits two maximum-separation AuxPtr links.
PERIODIC_PERIODIC_INTERVAL = 4000

# Selected signed-dBm power in AUX_SYNC_IND; chain PDUs omit TxPower.
PERIODIC_POWER_DBM = -20
PERIODIC_SKIP = 3
PeriodicReport = hci.HCI_LE_Periodic_Advertising_Report_Event

# LL/DDI/SCN/BV-89-C: at least five periodic reports despite nonzero RFU fields.
RFU_REQUIRED_REPORTS = 5

# Valid 16-us CTEInfo used only where the field is context-reserved.
RFU_CTE_INFO = 2

# Core Vol4 PartE7.7.65.14: valid periodic synchronization handles.
SYNC_HANDLE_MAX = 3839
SyncEstablished = hci.HCI_LE_Periodic_Advertising_Sync_Established_Event


def _check_periodic_reports(records, chunks, handle):
    """Allow buffered/incremental delivery, but never future data or unknown first
    power."""
    assert len(records) > 1, 'multiple periodic advertising reports required'
    expected = b''.join(chunks)
    offset = 0
    for index, (received, report) in enumerate(records):
        assert report.sync_handle == handle
        assert -127 <= report.rssi <= 20, 'RSSI unavailable or outside the HCI range'
        assert report.cte_type == hci.HCI_LE_PERIODIC_REPORT_CTE_NONE
        assert report.tx_power in (
            (PERIODIC_POWER_DBM,)
            if index == 0
            else (PERIODIC_POWER_DBM, ExtendedReport.TX_POWER_INFORMATION_NOT_AVAILABLE)
        ), 'first report lost AUX_SYNC_IND TxPower'
        end = offset + len(report.data)
        assert end <= sum(map(len, chunks[:received])), 'report contains future data'
        assert report.data == expected[offset:end], 'periodic data lost or reordered'
        assert report.data_status == (
            ExtendedReport.DATA_COMPLETE
            if index == len(records) - 1
            else ExtendedReport.DATA_INCOMPLETE_MORE_TO_COME
        )
        offset = end
    assert offset == len(expected), 'complete report omitted periodic data'


async def _periodic_header_compatibility(tester):
    """Report periodic data despite nonzero RFU fields, without losing sync."""
    generator = random.Random(0)
    data = generator.randbytes(COMPATIBILITY_DATA_LENGTH)
    did = generator.randrange(PDU_ADV_ADI_DID_MASK + 1)
    await tester.command(
        scan_parameters(
            extended=True,
            own_address_type=hci.AddressType.PUBLIC_DEVICE,
            scanning_filter_policy=0,
            scanning_phys=hci.PhyBit.LE_1M,
            scan_types=[0],
            scan_intervals=[SCANNING_SCAN_INTERVAL],
            scan_windows=[SCANNING_SCAN_INTERVAL],
        )
    )
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0))
    train = PeriodicTrain(
        tester.radio.now_us + COMPATIBILITY_INTERVAL * LL_PERIODIC_INTERVAL_UNIT_US,
        interval=COMPATIBILITY_INTERVAL,
    )
    next_advertisement = tester.radio.now_us + REPORT_DELIVERY_US
    announcements_enabled = True

    primary = _rfu_periodic_pdu(
        'primary',
        adi=ADVERTISING_SID << PDU_ADV_ADI_DID_BITS,
        aux=aux_pointer(COMPATIBILITY_AUX_CHANNEL, COMPATIBILITY_AUX_OFFSET_US),
    )
    minimum_offset = Phy.LE_1M.airtime_us(len(primary)) + LL_MAFS_US
    unit = PDU_ADV_OFFSET_UNIT_30_US
    auxiliary_offset = max(
        COMPATIBILITY_AUX_OFFSET_US,
        (minimum_offset + unit - 1) // unit * unit,
    )

    async def announce_until(target):
        nonlocal next_advertisement
        while (
            announcements_enabled
            and next_advertisement + auxiliary_offset + REPORT_DELIVERY_US < target
        ):
            start = max(next_advertisement, tester.radio.now_us + REPORT_DELIVERY_US)
            auxiliary = start + auxiliary_offset
            await tester.transmit(
                COMPATIBILITY_PRIMARY_CHANNEL,
                _rfu_periodic_pdu(
                    'primary',
                    adi=ADVERTISING_SID << PDU_ADV_ADI_DID_BITS,
                    aux=aux_pointer(COMPATIBILITY_AUX_CHANNEL, auxiliary_offset),
                ),
                start_us=start,
            )
            await tester.transmit(
                COMPATIBILITY_AUX_CHANNEL,
                _rfu_periodic_pdu(
                    'auxiliary',
                    address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
                    adi=ADVERTISING_SID << PDU_ADV_ADI_DID_BITS,
                    sync=train.sync_info(auxiliary),
                    data=b'announcement',
                ),
                start_us=auxiliary,
            )
            next_advertisement = start + COMPATIBILITY_ADV_INTERVAL_US

    async def periodic_pdu():
        await announce_until(train.anchor)
        await tester.transmit(
            train.channel,
            _rfu_periodic_pdu(
                'periodic',
                adi=ADVERTISING_SID << PDU_ADV_ADI_DID_BITS | did,
                data=data,
            ),
            start_us=train.anchor,
            **train.parameters,
        )
        train.next_event()

    await periodic_pdu()
    announcement = tester.take(lambda event: isinstance(event, ExtendedReport))
    assert announcement is not None, 'no extended announcement despite valid RF fields'
    report = announcement.reports[0]
    assert report.event_type == Report.EventType.DIRECTED_ADVERTISING
    assert report.periodic_advertising_interval == train.interval
    assert report.direct_address == DUT_ADDRESS
    assert report.direct_address_type == hci.AddressType.PUBLIC_DEVICE
    assert report.address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
    assert report.address_type == hci.AddressType.PUBLIC_DEVICE
    assert report.advertising_sid == ADVERTISING_SID
    assert report.primary_phy == report.secondary_phy == Phy.LE_1M
    assert not any(isinstance(event, PeriodicReport) for event in tester.events)
    await tester.command(
        create_sync(
            options=0,
            advertiser_address=report.address,
            advertiser_address_type=report.address_type,
            advertising_sid=report.advertising_sid,
            skip=0,
            sync_timeout=3
            * train.interval
            * LL_PERIODIC_INTERVAL_UNIT_US
            // hci.HCI_LE_PERIODIC_SYNC_TIMEOUT_UNIT_US,
        )
    )
    established = None
    for _ in range(RFU_REQUIRED_REPORTS):
        await periodic_pdu()
        if established is None:
            established = await tester.event(SyncEstablished)
            assert established.status == hci.HCI_ErrorCode.SUCCESS
            assert 0 <= established.sync_handle <= SYNC_HANDLE_MAX
            assert established.advertising_sid == ADVERTISING_SID
            assert established.advertiser_address == report.address
            assert established.advertiser_address_type == report.address_type
            assert established.advertiser_phy == Phy.LE_1M
            assert established.periodic_advertising_interval == train.interval
            assert established.advertiser_clock_accuracy == 0
        reports = []
        while event := tester.take(lambda event: isinstance(event, PeriodicReport)):
            reports.append(event)
        assert len(reports) == 1, 'each periodic PDU requires one complete report'
        assert reports[0].sync_handle == established.sync_handle
        assert reports[0].data == data
        assert reports[0].data_status == ExtendedReport.DATA_COMPLETE
        assert -127 <= reports[0].rssi <= 20
        assert not any(
            isinstance(
                event,
                (SyncEstablished, hci.HCI_LE_Periodic_Advertising_Sync_Lost_Event),
            )
            for event in tester.events
        ), 'synchronization was lost or established twice'
    announcements_enabled = False
    await tester.command(
        hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(established.sync_handle)
    )
    await periodic_pdu()
    assert not any(
        isinstance(event, (SyncEstablished, SyncLost, PeriodicReport))
        for event in tester.events
    ), 'terminated synchronization still reported periodic traffic'
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


def _rfu_periodic_pdu(role, **fields):
    """Populate RFU fields for the selected primary, auxiliary, or periodic PDU."""
    ignored_sync = PeriodicTrain(30000).sync_info(0)
    if role == 'primary':
        # Table 2.4: SyncInfo, CTEInfo, ACAD, and AdvData are RFU. TargetA is valid.
        fields.update(
            target=DUT_ADDRESS,
            cte=RFU_CTE_INFO,
            sync=ignored_sync,
            acad=b'\x02\xffZ',
            data=b'ignored-primary-data',
        )
    elif role == 'auxiliary':
        # Table 2.5 C.2: the superior PDU already supplies the meaningful TargetA.
        fields.update(target=PEER_ADDRESS, cte=RFU_CTE_INFO)
    elif role == 'periodic':
        # Table 2.6: AdvA, TargetA, and SyncInfo are RFU in AUX_SYNC_IND.
        fields.update(address=PEER_ADDRESS, target=PEER_ADDRESS, sync=ignored_sync)
    else:
        raise ValueError(f'unknown periodic RFU PDU role: {role}')
    if fields.get('sync') is not None:
        sync = bytearray(fields['sync'])
        offset = int.from_bytes(sync[:2], 'little') | PDU_ADV_SYNC_OFFSET_RESERVED_MASK
        sync[:2] = offset.to_bytes(2, 'little')
        fields['sync'] = sync
    wire = bytearray(ext_pdu(**fields))
    wire[0] |= PDU_ADV_RESERVED_MASK | PDU_ADV_CHSEL_MASK
    if role != 'auxiliary':
        wire[0] |= PDU_ADV_TXADD_MASK
    if role != 'primary':
        wire[0] |= PDU_ADV_RXADD_MASK
    assert wire[2] != 0
    wire[PDU_ADV_EXT_HEADER_OFFSET] |= PDU_ADV_EXT_FLAGS_RESERVED_MASK
    return bytes(wire)


# Leave at least100ms for announcements/acquisition, independently of the interval.
ACQUISITION_LEAD_US = 100000

# Selected20-ms base interval plus500-us advertising delay.
PERIODIC_RECEPTION_ADVERTISING_INTERVAL_US = 20500

# Integer-us representation nearest below the source's200-ms AuxOffset.
ANNOUNCEMENT_OFFSET_US = 199980
AdvertisingReport = hci.HCI_LE_Extended_Advertising_Report_Event

# Expected Outcome: complete reports and bounded differences between counters.
COMPLETE_REPORT_PERCENT = 80
COUNTER_DIFFERENCE_PERCENT = 95

# Step5: the first two AdvData octets carry the event counter.
DATA_COUNTER_SIZE = 2

# Step 3 specifies 1 s; Step 10's 5 s is an approved local editorial correction.
PERIODIC_RECEPTION_PERIODIC_INTERVAL = 800

# Two chain offsets, their offset unit, and packet airtime fit the 1-s interval.
PERIODIC_WINDOW_CHAIN_OFFSET_US = 499500

# Selected40ms on1M makes3*interval a valid Host timeout and fits1650-octet
# chains plus announcements;200ms otherwise also fits the full S8 chain.
PERIODIC_INTERVAL_1M = 32
PERIODIC_INTERVAL_OTHER = 160

# A retry bound, not a substitute for either report-count or percentage verdict.
RECEPTION_ATTEMPT_FACTOR = 2

# Steps9/12: count serialized HCI events, not reassembled periodic data sets.
REQUIRED_REPORT_EVENTS = 5
SKIP_ROUND_REPORT_EVENTS = 30
SyncLost = hci.HCI_LE_Periodic_Advertising_Sync_Lost_Event


def _check_counters(counters, skip):
    """Compare modulo16-bit counters, with the source's rounded-down95% threshold."""
    differences = [
        after - before & LL_EVENT_COUNTER_MASK for before, after in pairwise(counters)
    ]
    assert all(differences), 'duplicate periodic event reported'
    good = sum(difference <= skip + 1 for difference in differences)
    assert good >= len(differences) * COUNTER_DIFFERENCE_PERCENT // 100


def _periodic_reception_check_reports(reports, data, boundaries, handle):
    """Validate one event's ordered HCI fragments and whole-PDU truncation."""
    joined = bytearray()
    for index, report in enumerate(reports):
        assert report.sync_handle == handle
        assert report.cte_type == hci.HCI_LE_PERIODIC_REPORT_CTE_NONE
        assert report.tx_power == AdvertisingReport.TX_POWER_INFORMATION_NOT_AVAILABLE
        assert -127 <= report.rssi <= 20
        joined.extend(report.data)
        if index < len(reports) - 1:
            assert report.data_status == AdvertisingReport.DATA_INCOMPLETE_MORE_TO_COME
        else:
            assert report.data_status in (
                AdvertisingReport.DATA_COMPLETE,
                AdvertisingReport.DATA_INCOMPLETE_TRUNCATED_NO_MORE_TO_COME,
            )
    assert bytes(joined) == data[: len(joined)]
    complete = reports[-1].data_status == AdvertisingReport.DATA_COMPLETE
    if complete:
        assert bytes(joined) == data
    else:
        assert len(joined) in boundaries, 'truncation must precede a chain PDU'
    return complete


class _PeriodicReceptionResults:
    """Reassemble logical periodic data across HCI delivery batches and phases."""

    def __init__(self, length):
        self.length = length
        self.pending = []
        self.current_counter = None
        self.counters = []
        self.reported = set()
        self.complete_counters = set()
        self.complete = 0

    def collect(self, reports, transmitted, handle):
        for report in reports:
            assert report.sync_handle == handle
            assert report.cte_type == hci.HCI_LE_PERIODIC_REPORT_CTE_NONE
            assert (
                report.tx_power == AdvertisingReport.TX_POWER_INFORMATION_NOT_AVAILABLE
            )
            assert -127 <= report.rssi <= 20
            assert report.data_status in (
                AdvertisingReport.DATA_COMPLETE,
                AdvertisingReport.DATA_INCOMPLETE_MORE_TO_COME,
                AdvertisingReport.DATA_INCOMPLETE_TRUNCATED_NO_MORE_TO_COME,
            )
            self.pending.append(report)
            joined = b''.join(fragment.data for fragment in self.pending)
            if self.length:
                if len(joined) >= DATA_COUNTER_SIZE and self.current_counter is None:
                    self.current_counter = int.from_bytes(
                        joined[:DATA_COUNTER_SIZE], 'little'
                    )
                    assert self.current_counter not in self.reported, (
                        'duplicate periodic data'
                    )
                    self.reported.add(self.current_counter)
                    self.counters.append(self.current_counter)
                if self.current_counter is not None:
                    assert self.current_counter in transmitted, (
                        'untransmitted periodic data'
                    )
                    frame = transmitted[self.current_counter]
                    assert joined == frame.data[: len(joined)]
            else:
                assert not joined
            if report.data_status == AdvertisingReport.DATA_INCOMPLETE_MORE_TO_COME:
                continue
            if self.length:
                assert self.current_counter is not None, (
                    'periodic counter is incomplete'
                )
                frame = transmitted[self.current_counter]
                data, boundaries = frame.data, frame.boundaries
            else:
                data, boundaries = b'', ()
            complete = _periodic_reception_check_reports(
                self.pending, data, boundaries, handle
            )
            if complete:
                self.complete += 1
                if self.current_counter is not None:
                    self.complete_counters.add(self.current_counter)
            self.pending.clear()
            self.current_counter = None

    def check_rate(self, prepared):
        assert prepared, 'no prepared periodic receive opportunities'
        if self.length:
            assert self.complete_counters <= prepared, (
                'report without a receive attempt'
            )
        assert self.complete <= len(prepared), (
            'more complete reports than receive attempts'
        )
        assert self.complete * 100 >= len(prepared) * COMPLETE_REPORT_PERCENT, (
            self.complete,
            len(prepared),
        )


def _periodic_reception_opportunities(capture, frames, interval_us, access_address):
    """Count actual PHY receive attempts, once per periodic event including chains."""
    assert frames
    boundaries = [frames[0].start_us - interval_us // 2]
    boundaries += [
        (before.end_us + after.start_us) // 2 for before, after in pairwise(frames)
    ]
    boundaries.append((frames[-1].end_us + frames[-1].start_us + interval_us) // 2)
    prepared = set()
    for request in csv.DictReader(capture):
        if int(request['phy_address'], 0) != access_address:
            continue
        index = bisect_right(boundaries, int(request['start_time'])) - 1
        if 0 <= index < len(frames):
            prepared.add(frames[index].counter)
    return prepared


def _pointer(offset):
    value = aux_pointer(0, offset)
    return bytes([value[0] & ~PDU_ADV_AUX_CA_MASK]) + value[1:]


def _widening(offset):
    return (
        LL_CLOCK_JITTER_US
        + (offset * 500 + PHY_TIME_UNITS_PER_SECOND - 1) // PHY_TIME_UNITS_PER_SECOND
    )


def _periodic_sets(tester, length):
    generator = random.Random(0)
    interval = 1600 if length > 382 else 320
    interval_us = interval * LL_PERIODIC_INTERVAL_UNIT_US
    result = []
    for index, (primary, secondary) in enumerate(PHY_PAIRS):
        while True:
            address = generator.getrandbits(32)
            try:
                check_access_address(address)
            except AssertionError:
                continue
            if address not in [item.train.access_address for item in result]:
                break
        result.append(
            PeriodicAdvertiser(
                PeriodicTrain(
                    tester.radio.now_us
                    + interval_us
                    + index * interval_us // len(PHY_PAIRS),
                    interval=interval,
                    access_address=address,
                    crc_init=LinkParameters.crc_init ^ index,
                    phy=secondary,
                ),
                tester.radio.now_us + 1000 + index * 10000,
                200000,
                sid=index + 1,
                auxiliary_offset_us=AUXILIARY_OFFSET_US,
                primary_phy=primary,
            )
        )
    return result


SYNCHRONIZATION_ADVERTISING_INTERVAL_US = 20500

# Section4.2.4.25: ten complete periodic intervals before the duplicate command.
DUPLICATE_OBSERVATION_EVENTS = 10

# Selected negative window exceeds the three-interval Sync_Timeout; the advertiser
# search must remain pending until canceled if no matching SyncInfo is available.
FILTER_OBSERVATION_EVENTS = 10

# Data fit into a single AUX_SYNC_IND; a sequence prefix detects stale reports.
PERIODIC_DATA_LENGTH = 31

# Selected100-ms periodic interval and20-ms base advertising interval +500-us delay.
SYNCHRONIZATION_PERIODIC_INTERVAL = 80


def _check_established(event, *, phy=Phy.LE_1M):
    assert event.status == ErrorCode.SUCCESS
    assert 0 <= event.sync_handle <= SYNC_HANDLE_MAX
    assert event.advertising_sid == ADVERTISING_SID
    assert event.advertiser_address_type == hci.AddressType.PUBLIC_DEVICE
    assert event.advertiser_address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
    assert event.advertiser_phy == phy
    assert event.periodic_advertising_interval == SYNCHRONIZATION_PERIODIC_INTERVAL
    assert event.advertiser_clock_accuracy == 0
    return event.sync_handle


async def _start_advertiser(tester, *, filtered=False, duplicates=False):
    await tester.command(
        scan_parameters(
            extended=True,
            scanning_filter_policy=int(filtered),
            scanning_phys=hci.PhyBit.LE_1M,
            scan_types=[0],
            scan_intervals=[SCANNING_SCAN_INTERVAL],
            scan_windows=[SCANNING_SCAN_INTERVAL],
        )
    )
    await tester.command(
        hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, int(duplicates), 0, 0)
    )
    interval_us = SYNCHRONIZATION_PERIODIC_INTERVAL * LL_PERIODIC_INTERVAL_UNIT_US
    return PeriodicAdvertiser(
        PeriodicTrain(
            tester.radio.now_us + interval_us,
            interval=SYNCHRONIZATION_PERIODIC_INTERVAL,
        ),
        tester.radio.now_us + 1000,
        SYNCHRONIZATION_ADVERTISING_INTERVAL_US,
    )


def _sync_command(*, use_list=False, filter_duplicates=False):
    options = SyncOptions.USE_PERIODIC_ADVERTISER_LIST if use_list else 0
    if filter_duplicates:
        options |= SyncOptions.DUPLICATE_FILTERING_INITIALLY_ENABLED
    return create_sync(
        options=options,
        advertising_sid=ADVERTISING_SID,
        advertiser_address_type=hci.AddressType.PUBLIC_DEVICE,
        advertiser_address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
        skip=0,
        sync_timeout=3
        * SYNCHRONIZATION_PERIODIC_INTERVAL
        * LL_PERIODIC_INTERVAL_UNIT_US
        // hci.HCI_LE_PERIODIC_SYNC_TIMEOUT_UNIT_US,
    )


async def _transmit_report(tester, advertiser, *, handle=None, filtered=False):
    data = advertiser.train.counter.to_bytes(2, 'little') + random.Random(
        advertiser.train.counter
    ).randbytes(PERIODIC_DATA_LENGTH - 2)
    await advertiser.transmit(tester, data)
    if handle is None:
        established = await tester.event(SyncEstablished)
        handle = _check_established(established)
    report = await tester.event(PeriodicReport)
    assert report.sync_handle == handle and report.data == data
    assert report.data_status == ExtendedReport.DATA_COMPLETE
    assert report.cte_type == hci.HCI_LE_PERIODIC_REPORT_CTE_NONE
    assert -127 <= report.rssi <= 20
    assert not any(
        isinstance(event, (SyncEstablished, SyncLost, PeriodicReport))
        for event in tester.events
    )
    if filtered:
        assert not any(isinstance(event, ExtendedReport) for event in tester.events), (
            'periodic synchronization must not bypass the scan-report filter'
        )
    return handle


# Selected15-ms phase after the twentieth announcement; the next announcement
# can encode the following five-second periodic anchor within SyncInfo's range.
FIRST_ANCHOR_US = REPORT_DELIVERY_US + 20 * PERIODIC_ADVERTISING_INTERVAL_US + 15000

# Table4.2-61: periodic data length and maximum skipped events for each round.
RECEPTION_ROUNDS = (
    (0, 0),
    (PDU_ADV_LEGACY_DATA_SIZE_MAX, 0),
    (191, 0),
    (382, 0),
    (SCAN_MAX_DATA, 0),
    (SCAN_MAX_DATA, 1),
    (SCAN_MAX_DATA, 249),
    (SCAN_MAX_DATA, 499),
    (PDU_ADV_LEGACY_DATA_SIZE_MAX, 15),
)


async def periodic_advertising_report_rssi_power_and_data_status(tester, *, phy):
    """Extended Scanning, Passive, Periodic Advertising Report, RSSI, TX_Power, Data
    Status.

    Initial Condition:
        Continuous passive scanning uses public own address and accept-all policy.
        Scan_Max_Data is 1650 octets; N=min(100, Scan_Max_Data/3). Table 4.2-57
        selects LE 1M/1M for BV-79 and Coded/Coded for BV-80. Choose Power1=-20 dBm.
        The selected five-second periodic interval accommodates two maximum
        AuxPtr offsets and the complete chain.

    Test Case Configuration:
        LL/DDI/SCN/BV-79-C: LE 1M.
        LL/DDI/SCN/BV-80-C: LE Coded.

    Test Steps:
        1. Set extended scan parameters with the assigned Scanning_PHYs, passive
           Scan_Type 0, interval/window 0x0010, public own address and accept-all
           policy; require successful Command Complete.
        2. Enable extended scanning with Filter_Duplicates, Duration and Period
           zero; require successful Command Complete.
        3. Send ADV_EXT_IND with AuxPtr to AUX_ADV_IND on the selected PHY.
           AUX_ADV_IND contains LT AdvA and SyncInfo for AUX_SYNC_IND. Continue
           extended advertising throughout the test until the postamble.
        4. Receive LE Extended Advertising Report with nonzero periodic interval,
           complete status and valid RSSI. Check public peer identity, SID and PHYs.
        5. Send LE Periodic Advertising Create Sync with Options 0, the Step 3
           SID, public LT identity, Skip 3, Sync_CTE_Type 0 and timeout equal to
           (Skip+3) times the Step 4 periodic interval. Convert interval units
           (1.25 ms) to HCI timeout units (10 ms). Require successful Command Status.
        6. Send AUX_SYNC_IND containing N seeded random octets, Power1 and AuxPtr
           to the first AUX_CHAIN_IND.
        7. Receive successful LE Periodic Advertising Sync Established with a
           valid handle and the Step 3 SID before transmitting the chain. Also
           validate peer address/type, PHY, interval and clock-accuracy fields.
        8. Send two AUX_CHAIN_IND PDUs, each with
           min(249, (Scan_Max_Data-N)/2) seeded random octets. The first points to
           the second; the second has no AuxPtr. Space them as far apart as
           practical, using maximum 2,457,300-us AuxPtr offsets. Omit chain TxPower.
        9. Receive multiple LE Periodic Advertising Report events; these may
           begin before Step 8. Every intermediate Data_Status is incomplete-more
           (01), the final status is complete (00), all RSSIs are valid and the
           first report uses Power1. Later reports may use Power1 or 0x7F.

    Expected Outcome:
        Pass verdict: Step 9 produces multiple periodic reports with the specified
        status, RSSI and TxPower. Additionally check the correct sync handle, no
        CTE, exact ordered data and no bytes from a PDU not yet received. Do not
        require fixed report grouping or one report per received PDU.

    Postamble:
        Terminate the established synchronization and disable extended scanning,
        requiring successful command completion.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4.8, LL/DDI/SCN/BV-79-C, LL/DDI/SCN/BV-80-C;
        PDF pages 309-311, printed pages 4.2-216-4.2-218, Table 4.2-57,
        Figure 4.2-92, Steps 1-9 and the complete pass verdict.
    """
    generator = random.Random(0)
    first_length = min(FIRST_DATA_LENGTH, SCAN_MAX_DATA // (CHAIN_COUNT + 1))
    chain_length = min(CHAIN_DATA_LENGTH, (SCAN_MAX_DATA - first_length) // CHAIN_COUNT)
    chunks = [generator.randbytes(first_length)] + [
        generator.randbytes(chain_length) for _ in range(CHAIN_COUNT)
    ]
    await tester.command(
        scan_parameters(
            extended=True,
            own_address_type=hci.AddressType.PUBLIC_DEVICE,
            scanning_filter_policy=0,
            scanning_phys=1 << phy - 1,
            scan_types=[0],
            scan_intervals=[SCANNING_SCAN_INTERVAL],
            scan_windows=[SCANNING_SCAN_INTERVAL],
        )
    )
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0))
    train = PeriodicTrain(
        tester.radio.now_us + FIRST_ANCHOR_US,
        interval=PERIODIC_PERIODIC_INTERVAL,
        phy=phy,
    )
    next_advertisement = tester.radio.now_us + REPORT_DELIVERY_US
    records = []
    received = 0

    def take_reports():
        while event := tester.take(lambda event: isinstance(event, PeriodicReport)):
            records.append((received, event))

    async def announce_until(target):
        nonlocal next_advertisement
        while next_advertisement <= tester.radio.now_us:
            next_advertisement += PERIODIC_ADVERTISING_INTERVAL_US
        announcement = ext_pdu(
            address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
            adi=ADVERTISING_SID << PDU_ADV_ADI_DID_BITS,
            sync=bytes(PDU_ADV_SYNC_INFO_SIZE),
            data=b'announcement',
        )
        budget = (
            AUXILIARY_OFFSET_US + phy.airtime_us(len(announcement)) + REPORT_DELIVERY_US
        )
        while next_advertisement + budget < target:
            await train.announce(
                tester,
                start=next_advertisement,
                primary_phy=phy,
                address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
                auxiliary_offset=AUXILIARY_OFFSET_US,
            )
            take_reports()
            next_advertisement += PERIODIC_ADVERTISING_INTERVAL_US

    report = None
    while (
        report is None
        and next_advertisement < train.anchor - PERIODIC_ADVERTISING_INTERVAL_US
    ):
        await announce_until(next_advertisement + PERIODIC_ADVERTISING_INTERVAL_US)
        event = tester.take(lambda event: isinstance(event, ExtendedReport))
        if event is not None:
            report = event.reports[0]
    assert report is not None, 'no periodic advertising announcement report'
    assert report.event_type == 0
    assert report.address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
    assert report.address_type == hci.AddressType.PUBLIC_DEVICE
    assert report.primary_phy == report.secondary_phy == phy
    assert report.periodic_advertising_interval == train.interval
    assert report.advertising_sid == ADVERTISING_SID
    assert -127 <= report.rssi <= 20
    timeout = (
        (PERIODIC_SKIP + 3)
        * train.interval
        * LL_PERIODIC_INTERVAL_UNIT_US
        // hci.HCI_LE_PERIODIC_SYNC_TIMEOUT_UNIT_US
    )
    await tester.command(
        create_sync(
            advertising_sid=report.advertising_sid,
            advertiser_address_type=report.address_type,
            advertiser_address=report.address,
            skip=PERIODIC_SKIP,
            sync_timeout=timeout,
        )
    )
    await announce_until(train.anchor)
    anchor, channel, parameters = (train.anchor, train.channel, train.parameters)
    await tester.transmit(
        channel,
        ext_pdu(
            tx_power=PERIODIC_POWER_DBM,
            data=chunks[0],
            aux=aux_pointer(channel, CHAIN_OFFSET_US, phy),
        ),
        start_us=anchor,
        **parameters,
    )
    received = 1
    take_reports()
    established = tester.take(lambda event: isinstance(event, SyncEstablished))
    assert established is not None, 'sync establishment delayed until the chain ends'
    assert established.status == hci.HCI_ErrorCode.SUCCESS
    assert 0 <= established.sync_handle <= SYNC_HANDLE_MAX
    assert established.advertising_sid == report.advertising_sid
    assert established.advertiser_address == report.address
    assert established.advertiser_address_type == report.address_type
    assert established.advertiser_phy == phy
    assert established.periodic_advertising_interval == train.interval
    assert established.advertiser_clock_accuracy == 0
    train.next_event()
    for index, chunk in enumerate(chunks[1:], 1):
        target = anchor + index * CHAIN_OFFSET_US
        await announce_until(target)
        await tester.transmit(
            channel,
            ext_pdu(
                data=chunk,
                aux=(
                    aux_pointer(channel, CHAIN_OFFSET_US, phy)
                    if index < CHAIN_COUNT
                    else None
                ),
            ),
            start_us=target,
            **parameters,
        )
        received = index + 1
        take_reports()
    await tester.wait(REPORT_DELIVERY_US)
    take_reports()
    _check_periodic_reports(records, chunks, established.sync_handle)
    await tester.command(
        hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(established.sync_handle)
    )
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


# Verify a passive scanner reports periodic advertising RSSI, TxPower and data status
# while receiving a non-connectable, non-scannable train.
PROCEDURES |= procedure_cases(
    periodic_advertising_report_rssi_power_and_data_status,
    {
        'LL/DDI/SCN/BV-79-C': (
            (
                'Extended Scanning, Passive, Periodic Advertising Report, '
                'RSSI, TX_Power, Data Status – LE 1M PHY'
            ),
            {'phy': Phy.LE_1M},
        ),
        'LL/DDI/SCN/BV-80-C': (
            (
                'Extended Scanning, Passive, Periodic Advertising Report, '
                'RSSI, TX_Power, Data Status – LE Coded PHY'
            ),
            {'phy': Phy.LE_CODED},
        ),
    },
)


async def extended_scanning_periodic_advertising_reception(
    tester, *, primary_phy, secondary_phy
):
    """Extended Scanning, Periodic Advertising Reception.

    Initial Condition:
        Continuous passive scanning uses public own address and accept-all policy.
        Scan_Max_Data is 1650 octets. Table 4.2-60 selects primary/secondary PHYs:
        BV-21 uses LE 1M/1M and rounds 1-9; BV-46/47/142/143/144 use their
        assigned PHY pairs and rounds 1-6 and 9. Each round starts a separate
        periodic train with the counter initially zero.

    Test Case Configuration:
        Table 4.2-61 lengths/Skip: round 1 is empty/0; rounds 2-5 are
        31/191/382/Scan_Max_Data octets with Skip 0. Rounds 6/7/8 use
        Scan_Max_Data with Skip 1/249/499; round 9 uses 31 octets with Skip 15.
        Select 40-ms periodic intervals for LE 1M/1M and 200 ms otherwise,
        allowing the full data train and continuing extended announcements.

    Test Steps:
        1. Set extended scan parameters with the selected primary Scanning_PHYs,
           passive Scan_Type 0, interval/window 0x0010, public own address and
           accept-all policy; require successful Command Complete.
        2. Enable extended scanning with Filter_Duplicates, Duration and Period
           zero; require successful Command Complete.
        3. Start ADV_EXT_IND with AuxPtr to AUX_ADV_IND on the assigned PHY.
           AUX_ADV_IND contains public LT AdvA and SyncInfo for AUX_SYNC_IND.
           Continue extended advertising until Step 10.
        4. Receive LE Extended Advertising Report with a nonzero periodic
           interval; verify the advertised SID, peer identity and PHYs.
        5. Start the periodic stream on independently computed CSA #2 channels.
           When data are nonempty, the first two octets are an unsigned 16-bit
           event counter initially zero and incremented for each periodic event;
           fill the remainder with seeded random octets. Use the round's length,
           link chains through AuxPtr and fill every nonfinal PDU to capacity.
           Continue all events, including those the IUT may skip, through Step 15.
        6. Send LE Periodic Advertising Create Sync with Options 0, the observed
           SID, public LT address/type, table Skip, Sync_CTE_Type 0 and timeout
           (Skip+3) times the periodic interval, converted to 10-ms HCI units.
           Require successful Command Status.
        7. Receive successful LE Periodic Advertising Sync Established with
           valid handle and peer/SID/PHY/interval/clock accuracy matching the train.
        8. Receive one or more LE Periodic Advertising Report events containing
           the transmitted data, with the source's Unused field set to 0xFF
           (now named CTE_Type; see Source Correction). Check handle, RSSI,
           data order, fragment status and legal chain-boundary truncation.
           Keep unfinished HCI data across delivery batches and the Step 10
           boundary; only a terminal Complete with exact reassembled data counts
           as one successful periodic event. Truncated or unfinished data do not.
        9. Repeat Step 8 until thirty serialized HCI report events in round 7
           or five in every other round have arrived.
        10. Stop extended advertising only; keep periodic advertising running.
        11. Continue receiving periodic reports as in Step 8.
        12. Repeat Step 11 until another thirty HCI report events in round 7,
            or another five in every other round, have arrived.
        13. Send LE Periodic Advertising Terminate Sync for the established
            handle; require successful Command Complete.
        14. Disable scanning with LE Set Extended Scan Enable; require successful
            Command Complete (see command Source Correction).
        15. Stop the Lower Tester periodic stream.

    Expected Outcome:
        Pass verdict: synchronize successfully using CSA #2; report exact data in
        order; maintain synchronization after Step 10; stop reception after
        termination. Intermediate fragments are incomplete-more (01); the last
        is complete (00) or truncated (10), only at an AUX_CHAIN_IND boundary.
        At least one complete data result is required in every round.
        SID in Step 4 must match the transmitted advertisement.
        For rounds with data, consecutive logical counters differ by at most
        Skip+1 in at least floor(95% of comparisons), including the phase boundary.
        Require complete data for at least 80% of prepared receive opportunities
        in every round, including nonzero Skip. Derive those opportunities from
        the DUT's PHY receive requests for this train, counting each periodic
        event once even when it has multiple chain-reception requests. Count
        prepared events without a complete report as failures; exclude events
        actually skipped. Do not infer a fixed cadence from the maximum Skip.
        Measure from the first periodic transmission after Create Sync through
        Step 12, including unsuccessful prepared acquisition opportunities.

    Postamble:
        After successful termination and before disabling scanning, continue
        periodic traffic for Skip+1 events to cover the next allowed receive
        opportunity. Require no report, synchronization loss or new establishment.

    Source Correction:
        Step 14 names legacy Scan Enable after extended scan configuration; use
        Extended Scan Enable. The pass text's Event_Type for periodic reports
        means Data_Status. Step 8's older Unused octet is CTE_Type in the current
        HCI event; 0xFF denotes no CTE. These corrections follow the HCI event/
        command definitions and were corroborated with Amber; they are local
        corrections, not adopted SIG errata. The test owner confirmed the 80%
        metric means complete received data divided by prepared receive
        opportunities, excluding actual skipped events. HCI fragments must be
        reassembled into one complete data result; incomplete results earn no
        success. Apply this interpretation to both zero and nonzero Skip, with
        actual receive requests measured independently in the BabbleSim PHY log.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4.10, LL/DDI/SCN/BV-142-C, LL/DDI/SCN/BV-143-C,
        LL/DDI/SCN/BV-144-C, LL/DDI/SCN/BV-21-C, LL/DDI/SCN/BV-46-C,
        LL/DDI/SCN/BV-47-C; PDF pages 317-322, printed pages 4.2-224-4.2-229,
        Tables 4.2-60/61, Figure 4.2-95, Steps 1-15 and every pass condition.
        Core Specification 6.3, Vol 4, Part E, 7.8.67 defines Skip;
        7.7.65.15 defines periodic report fields and fragmentation.
    """
    interval = (
        PERIODIC_INTERVAL_1M
        if primary_phy == secondary_phy == Phy.LE_1M
        else PERIODIC_INTERVAL_OTHER
    )
    interval_us = interval * LL_PERIODIC_INTERVAL_UNIT_US
    for number, (length, skip) in enumerate(RECEPTION_ROUNDS, 1):
        if number in (7, 8) and (not primary_phy == secondary_phy == Phy.LE_1M):
            continue
        tester.events[:] = [
            event for event in tester.events if not isinstance(event, AdvertisingReport)
        ]
        tester.output.write(f'PERIODIC RECEPTION ROUND {number}\n')
        await tester.command(
            scan_parameters(
                extended=True,
                scanning_phys=1 << primary_phy - 1,
                scan_types=[0],
                scan_intervals=[SCANNING_SCAN_INTERVAL],
                scan_windows=[SCANNING_SCAN_INTERVAL],
            )
        )
        await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0))
        train = PeriodicTrain(
            tester.radio.now_us + max(interval_us, ACQUISITION_LEAD_US),
            interval=interval,
            phy=secondary_phy,
        )
        advertiser = PeriodicAdvertiser(
            train,
            tester.radio.now_us + REPORT_DELIVERY_US,
            PERIODIC_RECEPTION_ADVERTISING_INTERVAL_US,
            auxiliary_offset_us=AUXILIARY_OFFSET_US,
            primary_phy=primary_phy,
        )
        announcement = None
        while (
            advertiser.next_advertisement + PERIODIC_RECEPTION_ADVERTISING_INTERVAL_US
            < train.anchor
        ):
            await advertiser.announce_until(
                tester,
                advertiser.next_advertisement
                + PERIODIC_RECEPTION_ADVERTISING_INTERVAL_US,
            )
            await tester.wait(REPORT_DELIVERY_US)
            announcement = tester.take(
                lambda event: isinstance(event, AdvertisingReport)
            )
            if announcement is not None:
                break
        assert announcement is not None, 'no periodic advertiser announcement'
        report = announcement.reports[0]
        assert report.event_type == 0
        assert report.address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
        assert report.address_type == hci.AddressType.PUBLIC_DEVICE
        assert report.advertising_sid == ADVERTISING_SID
        assert (
            report.primary_phy == primary_phy and report.secondary_phy == secondary_phy
        )
        assert report.periodic_advertising_interval == interval
        await tester.command(
            create_sync(
                options=0,
                advertising_sid=report.advertising_sid,
                advertiser_address_type=report.address_type,
                advertiser_address=report.address,
                skip=skip,
                sync_timeout=(skip + 3)
                * interval_us
                // hci.HCI_LE_PERIODIC_SYNC_TIMEOUT_UNIT_US,
                sync_cte_type=0,
            )
        )
        generator = random.Random(number)
        handle = None
        transmitted = {}
        measured = []
        results = _PeriodicReceptionResults(length)
        required = SKIP_ROUND_REPORT_EVENTS if number == 7 else REQUIRED_REPORT_EVENTS
        for advertising in (True, False):
            advertiser.advertising = advertising
            report_events = 0
            for _ in range(required * (skip + 1) * RECEPTION_ATTEMPT_FACTOR):
                counter = train.counter
                data = (
                    counter.to_bytes(DATA_COUNTER_SIZE, 'little')
                    + generator.randbytes(length - DATA_COUNTER_SIZE)
                    if length
                    else b''
                )
                packets = list(train.packets(data))
                consumed = 0
                boundaries = []
                for _, wire in packets[:-1]:
                    consumed += len(decode_extended(wire)['data'])
                    boundaries.append(consumed)
                frame = SimpleNamespace(
                    counter=counter,
                    start_us=train.anchor,
                    end_us=train.anchor
                    + packets[-1][0]
                    + secondary_phy.airtime_us(len(packets[-1][1])),
                    data=data,
                    boundaries=tuple(boundaries),
                )
                assert counter not in transmitted, (
                    'periodic counter wrapped during a round'
                )
                transmitted[counter] = frame
                measured.append(frame)
                await advertiser.transmit(tester, data)
                await tester.wait(REPORT_DELIVERY_US)
                if handle is None:
                    established = tester.take(
                        lambda event: isinstance(event, SyncEstablished)
                    )
                    if established is not None:
                        assert established.status == hci.HCI_ErrorCode.SUCCESS
                        assert 0 <= established.sync_handle <= SYNC_HANDLE_MAX
                        assert established.advertising_sid == ADVERTISING_SID
                        assert established.advertiser_address == report.address
                        assert (
                            established.advertiser_address_type == report.address_type
                        )
                        assert established.advertiser_phy == secondary_phy
                        assert established.periodic_advertising_interval == interval
                        assert established.advertiser_clock_accuracy == 0
                        handle = established.sync_handle
                records = []
                while event := tester.take(
                    lambda event: isinstance(event, PeriodicReport)
                ):
                    records.append(event)
                assert not any(
                    isinstance(event, (SyncLost, SyncEstablished))
                    for event in tester.events
                ), 'periodic reception lost or duplicated synchronization'
                if records:
                    assert handle is not None, (
                        'periodic report preceded Sync Established'
                    )
                    results.collect(records, transmitted, handle)
                    report_events += len(records)
                if report_events >= required:
                    break
            assert report_events >= required, (number, advertising, report_events)
        assert results.complete, 'the round never reported its complete periodic data'
        if length:
            _check_counters(results.counters, skip)
        await tester.command(
            hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle)
        )
        for _ in range(skip + 1):
            data = (
                train.counter.to_bytes(DATA_COUNTER_SIZE, 'little')
                + generator.randbytes(length - DATA_COUNTER_SIZE)
                if length
                else b''
            )
            await advertiser.transmit(tester, data)
            await tester.wait(REPORT_DELIVERY_US)
            assert not any(
                isinstance(event, (PeriodicReport, SyncLost, SyncEstablished))
                for event in tester.events
            )
        await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))
        with (tester.phy_results / 'd_2G4_00.Rx.csv').open() as capture:
            prepared = _periodic_reception_opportunities(
                capture, measured, interval_us, train.access_address
            )
        results.check_rate(prepared)
        tester.output.write(
            f'PERIODIC RECEPTION round={number} skip={skip} '
            f'complete={results.complete} prepared={len(prepared)} '
            f'transmitted={len(measured)} pending_fragments={len(results.pending)}\n'
        )


# Verify a passive scanner locates periodic advertising through extended announcements,
# synchronizes using CSA #2 and reports periodic data, including after extended
# announcements stop.
PROCEDURES |= procedure_cases(
    extended_scanning_periodic_advertising_reception,
    {
        'LL/DDI/SCN/BV-142-C': (
            (
                'Extended Scanning, Periodic Advertising Reception – LE 1M '
                'PHY/LE Coded PHY'
            ),
            {'primary_phy': Phy.LE_1M, 'secondary_phy': Phy.LE_CODED},
        ),
        'LL/DDI/SCN/BV-143-C': (
            (
                'Extended Scanning, Periodic Advertising Reception – LE '
                'Coded PHY/LE 1M PHY'
            ),
            {'primary_phy': Phy.LE_CODED, 'secondary_phy': Phy.LE_1M},
        ),
        'LL/DDI/SCN/BV-144-C': (
            (
                'Extended Scanning, Periodic Advertising Reception – LE '
                'Coded PHY/LE 2M PHY'
            ),
            {'primary_phy': Phy.LE_CODED, 'secondary_phy': Phy.LE_2M},
        ),
        'LL/DDI/SCN/BV-21-C': (
            'Extended Scanning, Periodic Advertising Reception – LE 1M PHY',
            {'primary_phy': Phy.LE_1M, 'secondary_phy': Phy.LE_1M},
        ),
        'LL/DDI/SCN/BV-46-C': (
            'Extended Scanning, Periodic Advertising Reception – LE 2M PHY',
            {'primary_phy': Phy.LE_1M, 'secondary_phy': Phy.LE_2M},
        ),
        'LL/DDI/SCN/BV-47-C': (
            'Extended Scanning, Periodic Advertising Reception – LE Coded PHY',
            {'primary_phy': Phy.LE_CODED, 'secondary_phy': Phy.LE_CODED},
        ),
    },
)


async def multiple_periodic_advertisers_all_supported_phys(
    tester,
):
    """Extended Scanning, Multiple Sets, Periodic Advertising Reception, Multiple PHYs
    (All Supported PHYs).

    Initial Condition:
        Continuous passive scanning uses public own address and accept-all policy.
        Scan_Max_Data is 1650 octets. This DUT supports all assigned PHY pairs
        and at least six simultaneous periodic synchronizations. Execute all five
        Table 4.2-65 data-length rounds: empty, 31, 191, 382 and Scan_Max_Data.
        Each set has a distinct SID, valid Access Address/CRC and independent
        CSA #2 train. Use seeded random data octets from 1 through 254.

    Test Steps:
        1. Set extended scan parameters for all supported primary PHYs, passive
           Scan_Type 0, interval 0x0010 times the scan-PHY count, window 0x0010,
           public own address and accept-all policy; require successful Command
           Complete. Two scan PHYs use interval/window 0x0020/0x0010.
        2. Enable extended scanning with Filter_Duplicates, Duration and Period
           zero; require successful Command Complete.
        3. Start six extended sets with distinct SIDs: 1/2 use 1M/1M, 3/4
           Coded/Coded and 5/6 1M/2M. ADV_EXT_IND points to AUX_ADV_IND with
           public LT AdvA and SyncInfo for the corresponding AUX_SYNC_IND.
           Continue each set's extended announcements until its Step 9.
        4. Receive extended reports with SID and nonzero periodic interval for
           each supported set. On the first report for a set, start its Steps
           6-12 while continuing discovery of others. Serialize Step 6; permit
           previously established sets' Steps 7-12 to progress concurrently.
        5. Generate AUX_SYNC_IND and any needed AUX_CHAIN_IND for every train
           on its independently computed CSA #2 channels. Use the round's
           random data length and fill each nonfinal PDU to capacity. Continue
           all periodic trains until Step 14.
        6. Acquire each discovered set:
           6a. Send LE Periodic Advertising Create Sync with Options 0, the
               observed SID, public LT identity and timeout ten times the
               observed periodic interval, converted to HCI units. The source's
               Unused octet is zero; select Skip 0 and Sync_CTE_Type 0.
           6b. Receive successful Command Status.
           6c. Wait for successful Sync Established with valid distinct handle
               and matching peer/SID/PHY/interval/clock accuracy.
           6d. If Command Status or Sync Established reports a synchronization
               capacity limit, defer this acquisition until another set finishes
               Step 12, then retry. This conditional is not activated for the
               selected DUT, which accommodates all six sets; no synthetic
               resource-exhaustion behavior is substituted.
        7. Receive periodic report events containing the transmitted data, with
           the source's Unused octet 0xFF (current CTE_Type). Validate handle,
           RSSI, unavailable TxPower, fragment status and exact data order.
        8. Continue Step 7 until at least one hundred serialized HCI report
           events have arrived for that set.
        9. Stop that set's extended advertising only; continue its periodic train.
        10. Continue periodic reports on the same synchronization as in Step 7.
        11. Continue Step 10 until another one hundred HCI report events arrive.
        12. Terminate that set's synchronization and require successful Command
            Complete. Continue other sets without loss or extra establishment.
        13. After Step 12 completes for every supported set, disable extended
            scanning and require successful Command Complete.
        14. Stop periodic advertising on all sets.

    Expected Outcome:
        Pass verdict: every supported set synchronizes successfully, reports its
        correct data and remains synchronized after its own announcements stop.
        Periodic reception ends on that set's termination command. HCI fragments
        are incomplete-more (01) followed by complete (00), or a final truncated
        result (10) only at a chain boundary. Every set/round must have at least
        one complete data result within Scan_Max_Data. Unsupported PHYs would
        produce no periodic reports; all assigned PHYs are supported here.
        Extended-report SID matches the corresponding advertised SID from Step 3.
        Do not count reassembled data sets as the one hundred serialized reports.

    Source Correction:
        Step 5's ADV_SYNC_IND means AUX_SYNC_IND. The periodic-data pass text's
        Extended Advertising Report and Event_Type mean Periodic Advertising
        Report and Data_Status. Its final SID reference to Step 5 means the
        advertising/discovery context in Steps 3/4, not a SID in periodic PDUs.
        Old Unused labels correspond to current Sync_CTE_Type/CTE_Type with the
        stated values unchanged. Amber corroborated these mechanical corrections
        and the conditional capacity branch. They are local corrections, not
        adopted SIG errata; no limited-capacity receiver path is claimed.

    Reference:
        LL.TS.p28.pdf, standalone LL/DDI/SCN/BV-25-C; PDF pages 328-332,
        printed pages 4.2-235-4.2-239, Table 4.2-65, Figure 4.2-98,
        Steps 1-14 including 6a-6d and every pass condition.
        Core Specification 6.3, Vol 4, Part E, 7.8.67 and 7.7.65.15 define
        synchronization command parameters and periodic report fields.
    """
    for length in (0, 31, 191, 382, SCAN_MAX_DATA):
        tester.events[:] = [
            event for event in tester.events if not isinstance(event, Report)
        ]
        await _enable_scan(tester, active=False)
        advertisers = _periodic_sets(tester, length)
        payloads = []
        for index in range(6):
            generator = random.Random(index + 1)
            payloads.append(bytes(generator.randint(1, 254) for _ in range(length)))
        delays = random.Random(0)
        handles, seen, done = ({}, set(), set())
        counts = [0] * 6
        phases = [0] * 6
        complete = [False] * 6
        pending = None
        for _ in range(2000):
            index = min(range(6), key=lambda item: advertisers[item].train.anchor)
            target = advertisers[index].train.anchor
            while True:
                enabled = [item for item in range(6) if advertisers[item].advertising]
                if not enabled:
                    break
                candidate = min(
                    enabled, key=lambda item: advertisers[item].next_advertisement
                )
                advertiser = advertisers[candidate]
                start = max(advertiser.next_advertisement, tester.radio.now_us + 100)
                if start + AUXILIARY_OFFSET_US + 5000 >= target:
                    break
                await advertiser.train.announce(
                    tester,
                    sid=advertiser.sid,
                    start=start,
                    primary_phy=advertiser.primary_phy,
                    address=advertiser.address,
                    auxiliary_offset=AUXILIARY_OFFSET_US,
                )
                advertiser.next_advertisement = (
                    start
                    + advertiser.advertising_interval_us
                    + delays.randrange(LL_ADV_DELAY_MAX_US + 1)
                )
                await tester.wait(REPORT_DELIVERY_US)
                while event := tester.take(lambda event: isinstance(event, Report)):
                    for report in event.reports:
                        sid = report.advertising_sid
                        assert (
                            1 <= sid <= 6
                            and report.address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
                        )
                        assert report.address_type == hci.AddressType.PUBLIC_DEVICE
                        assert (report.primary_phy, report.secondary_phy) == PHY_PAIRS[
                            sid - 1
                        ]
                        assert (
                            report.periodic_advertising_interval
                            == advertisers[sid - 1].train.interval
                        )
                        assert report.event_type == 0
                        seen.add(sid - 1)
                if pending is None:
                    available = sorted(seen - handles.keys())
                    if available:
                        pending = available[0]
                        item = advertisers[pending]
                        await tester.command(
                            create_sync(
                                options=0,
                                advertising_sid=item.sid,
                                advertiser_address_type=hci.AddressType.PUBLIC_DEVICE,
                                advertiser_address=item.address,
                                skip=0,
                                sync_timeout=10
                                * item.train.interval
                                * LL_PERIODIC_INTERVAL_UNIT_US
                                // hci.HCI_LE_PERIODIC_SYNC_TIMEOUT_UNIT_US,
                                sync_cte_type=0,
                            )
                        )
            train = advertisers[index].train
            boundaries = []
            consumed = 0
            for _, wire in list(train.packets(payloads[index]))[:-1]:
                consumed += len(decode_extended(wire)['data'])
                boundaries.append(consumed)
            await train.transmit(tester, payloads[index])
            await tester.wait(REPORT_DELIVERY_US)
            if established := tester.take(
                lambda event: isinstance(event, SyncEstablished)
            ):
                assert (
                    pending == index and established.status == hci.HCI_ErrorCode.SUCCESS
                )
                assert established.advertising_sid == index + 1
                assert established.advertiser_address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
                assert (
                    established.advertiser_address_type == hci.AddressType.PUBLIC_DEVICE
                )
                assert 0 <= established.sync_handle <= SYNC_HANDLE_MAX
                assert established.advertiser_clock_accuracy == 0
                assert (
                    established.advertiser_phy == train.phy
                    and established.periodic_advertising_interval == train.interval
                )
                assert established.sync_handle not in handles.values()
                handles[index] = established.sync_handle
                pending = None
            reports = []
            while event := tester.take(lambda event: isinstance(event, PeriodicReport)):
                assert (
                    index in handles
                    and index not in done
                    and (event.sync_handle == handles[index])
                )
                reports.append(event)
            if reports:
                complete[index] |= _periodic_reception_check_reports(
                    reports, payloads[index], boundaries, handles[index]
                )
                counts[index] += len(reports)
                if counts[index] >= 100:
                    if phases[index] == 0:
                        phases[index] = 1
                        counts[index] = 0
                        advertisers[index].advertising = False
                    else:
                        assert complete[index], (
                            'no complete periodic result for this set'
                        )
                        await tester.command(
                            hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(
                                handles[index]
                            )
                        )
                        done.add(index)
            assert not any(
                isinstance(event, (SyncLost, SyncEstablished))
                for event in tester.events
            )
            if len(done) == 6:
                break
        assert len(done) == 6, (length, handles, counts, phases)
        await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


# Verify periodic advertising discovery, synchronization and data reporting across all
# supported PHYs while several advertising sets remain active.
PROCEDURES |= procedure_cases(
    multiple_periodic_advertisers_all_supported_phys,
    {
        'LL/DDI/SCN/BV-25-C': (
            (
                'Extended Scanning, Multiple Sets, Periodic Advertising '
                'Reception, Multiple PHYs (All Supported PHYs)'
            ),
            {},
        ),
    },
)


async def extended_scanning_periodic_advertising_reception_filter_policies(tester):
    """Extended Scanning, Periodic Advertising Reception, Filter Policies.

    Initial Condition:
        Start idle with Scan_Max_Data 1650 octets. Use public addresses, one
        LE 1M periodic train and data fitting one AUX_SYNC_IND. Select a 100-ms
        periodic interval; negative list phases are observed for ten intervals.
        That observation window is a harness choice, not a normative timeout.

    Test Steps:
        1. Add a seeded random public address different from the LT address to
           the Filter Accept List; require successful Command Complete.
        2. Set extended scan parameters: LE 1M primary PHY, passive Scan_Type 0,
           interval/window 0x0010, public own address and filter policy 0x01;
           require successful Command Complete.
        3. Enable extended scanning with Filter_Duplicates, Duration and Period
           zero; require successful Command Complete.
        4. Begin non-connectable, non-scannable undirected ADV_EXT_IND with
           AuxPtr to AUX_ADV_IND on LE 1M. The auxiliary contains LT AdvA
           and SyncInfo. Continue extended advertisements throughout the test.
        5. Generate periodic AUX_SYNC_IND on independent CSA #2 channels with
           data fitting one PDU; continue the train throughout the test.
        6. Send Periodic Advertising Create Sync with Options bit 0 clear, the
           LT SID/public address, Skip 0, timeout three periodic intervals and
           the source's Unused octet zero (current Sync_CTE_Type). Require
           successful Command Status.
        7. Receive successful Sync Established with matching peer/SID/PHY/
           interval/clock accuracy and a valid handle.
        8. Receive periodic report data matching the transmitted packet, with
           the source's Unused octet 0xFF (current CTE_Type), complete status,
           matching handle and valid RSSI.
        9. Terminate the synchronization; require successful Command Complete.
        10. Clear the Periodic Advertiser List, then add the correct public LT
            address and SID; require successful Command Complete for both.
        11. Repeat Steps 6-9 with Options bit 0 set to Use Advertiser List.
        12. Repeat Step 10 with a different advertiser address.
        13. Repeat Step 6 with Use Advertiser List and require no synchronization
            to the LT advertisements during the selected negative window.
        14. Send Create Sync Cancel; require successful Command Complete.
        15. Repeat Step 10 with the correct LT address but a different SID.
        16. Repeat Step 6 with Use Advertiser List and require no synchronization
            during the selected negative window.
        17. Send Create Sync Cancel; require successful Command Complete.

    Expected Outcome:
        Pass verdict: first and second Create Sync executions synchronize and
        report correct periodic data; third and fourth do not synchronize.
        Fail verdict: any LE Extended Advertising Report anywhere in the test.
        The scan Filter Accept List remains unrelated to the LT, even when direct
        selection or the separate Periodic Advertiser List successfully acquires it.

    Additional Core Checks:
        Wrong-list searches remain pending across the ten-interval observation,
        which exceeds the three-interval Sync_Timeout: that parameter is not a
        discovery-search deadline before a matching candidate is found.
        Following each successful cancel Command Complete, additionally require
        Sync Established with Operation Cancelled by Host (0x44), as specified
        by Core 7.8.68. This event is additional to source Steps 14/17.

    Postamble:
        Stop announcements and disable extended scanning successfully.

    Source Correction:
        Step 5's SyncInfo reference to Step 3 means the advertising setup in
        Step 4. SID belongs to the extended advertising/discovery context, not
        the periodic data PDU. Modernize the Unused field names without changing
        their stated values. Amber corroborated these local term/reference
        corrections; none is an adopted SIG erratum.

    Reference:
        LL.TS.p28.pdf, standalone LL/DDI/SCN/BV-34-C; PDF pages 345-349,
        printed pages 4.2-252-4.2-256, Figures 4.2-102/103, Steps 1-17
        and pass/fail verdicts. Core Specification 6.3, Vol 4, Part E,
        7.8.67/7.8.68 define selection, timeout and cancellation behavior.
    """
    foreign = hci.Address(random.Random(0).randbytes(6), hci.AddressType.PUBLIC_DEVICE)
    assert foreign != LOWER_PERIPHERAL_PUBLIC_ADDRESS
    await tester.command(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(
            foreign.address_type, foreign
        )
    )
    advertiser = await _start_advertiser(tester, filtered=True)
    await advertiser.transmit(tester, b'before-sync')
    assert not any(isinstance(event, ExtendedReport) for event in tester.events)
    for index, (address, sid) in enumerate(
        (
            (LOWER_PERIPHERAL_PUBLIC_ADDRESS, ADVERTISING_SID),
            (LOWER_PERIPHERAL_PUBLIC_ADDRESS, ADVERTISING_SID),
            (foreign, ADVERTISING_SID),
            (LOWER_PERIPHERAL_PUBLIC_ADDRESS, ADVERTISING_SID + 1),
        )
    ):
        if index:
            await tester.command(hci.HCI_LE_Clear_Periodic_Advertiser_List_Command())
            await tester.command(
                hci.HCI_LE_Add_Device_To_Periodic_Advertiser_List_Command(
                    address.address_type, address, sid
                )
            )
        await tester.command(_sync_command(use_list=bool(index)))
        if index < 2:
            handle = await _transmit_report(tester, advertiser, filtered=True)
            await tester.command(
                hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle)
            )
        else:
            for _ in range(FILTER_OBSERVATION_EVENTS):
                await advertiser.transmit(tester, b'unmatched-periodic-data')
                assert not any(
                    isinstance(
                        event,
                        (SyncEstablished, SyncLost, PeriodicReport, ExtendedReport),
                    )
                    for event in tester.events
                ), 'unmatched advertiser search must remain pending'
            await tester.command(
                hci.HCI_LE_Periodic_Advertising_Create_Sync_Cancel_Command()
            )
            canceled = await tester.event(SyncEstablished)
            assert canceled.status == ErrorCode.OPERATION_CANCELLED_BY_HOST_ERROR
        assert not any(isinstance(event, ExtendedReport) for event in tester.events)
    advertiser.advertising = False
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


# Verify periodic synchronization follows either explicit peer selection or the Periodic
# Advertiser List, independently of scan-report filtering.
PROCEDURES |= procedure_cases(
    extended_scanning_periodic_advertising_reception_filter_policies,
    {
        'LL/DDI/SCN/BV-34-C': (
            'Extended Scanning, Periodic Advertising Reception, Filter Policies',
            {},
        ),
    },
)


# LL/DDI/SCN/BV-37-C: omit six initial events, then send the seventh onward.
FIRST_SYNC_MISSED_EVENTS = 6

# Leave a full second for the first SyncInfo to arrive before event one.
FIRST_SYNC_LEAD_US = 1000000


async def first_aux_sync_ind_never_received(tester):
    """First AUX_SYNC_IND Never Received.

    Initial Condition:
        Public-address passive scanning uses LE 1M, accept-all policy and
        selected continuous interval/window. Scan_Max_Data is 1650 octets.
        Select Skip 0, periodic interval 150 ms and extended advertising
        interval 50 ms. The first periodic anchor is initially 1 s ahead.

    Test Steps:
        1. Set extended scan parameters: LE 1M, passive Scan_Type 0, interval/
           window 0x0010, public own address and accept-all policy; require
           successful Command Complete.
        2. Before enabling scanning, send Periodic Advertising Create Sync with
           Options bit 0 clear, LT public address/type and SID, valid Skip 0,
           timeout (Skip+3) times the periodic interval (450 ms) and
           Sync_CTE_Type 0. Require successful Command Status.
        3. Enable extended scanning with Filter_Duplicates, Duration and Period
           zero; require successful Command Complete.
        4. Begin LE 1M ADV_EXT_IND/AUX_ADV_IND with 50-ms advertising interval
           and 150-ms periodic interval. AuxPtr refers to AUX_ADV_IND, whose
           public LT AdvA and SyncInfo describe a valid CSA #2 periodic train.
           Initially transmit no AUX_SYNC_IND; continue extended announcements.
        5. Receive the first LE Extended Advertising Report with nonzero periodic
           interval, marking the synchronization attempt's start. Validate SID,
           public identity, PHYs, complete status and the configured interval.
           Check reports after each announcement and require at least one full
           periodic interval of lead before the first periodic anchor.
        6. Leave the first six periodic events after the initial SyncInfo silent,
           advancing both CSA #2 counter and anchor for every omitted event.
           Begin valid AUX_SYNC_IND at the seventh event; continue the extended
           announcements throughout (see Source Correction).
        7. Receive Sync Established with status 0x3E (Connection Failed To Be
           Established / Synchronization Timeout) after the six missed events.
           Reject premature failure and successful synchronization, including
           after valid periodic packets begin.

    Expected Outcome:
        Pass verdict: the IUT ceases the synchronization attempt and reports
        failure after six silent opportunities; the discovery report's SID
        matches the advertised SID. Fail verdict: successful synchronization
        to Step 6 periodic traffic, any periodic data report, or sync loss
        implying a synchronization that was not reported correctly.

    Postamble:
        Continue valid periodic packets for six events, covering a full new
        acquisition window. Require no further establishment or periodic reports.
        Stop announcements and disable extended scanning. Do not cancel the
        acquisition: its failure must come from the IUT itself.

    Source Correction:
        Final SID references mean the report in Step 5 and advertisement in
        Step 4, not the nonexistent report/advertisement in Steps 4/3. These
        are local reference corrections, not adopted SIG errata. Step 6
        literally starts AUX_SYNC_IND at the sixth event, inside the acquisition
        window: Core Vol 6, Part B, 4.4.5.1 fails synchronization only if none
        is received within 6 periodic advertising events. Omit six AUX_SYNC_IND
        opportunities and transmit from the seventh event, so the expected 0x3E
        (Vol 4, Part E, 7.8.67) is the only valid outcome. This resolves the
        local counting interpretation; it is not an outstanding source conflict.

    Reference:
        LL.TS.p28.pdf, standalone LL/DDI/SCN/BV-37-C; PDF pages 355-357,
        printed pages 4.2-262-4.2-264, Figure 4.2-106, Steps 1-7 and
        pass/fail verdicts. Core Specification 6.3, Vol 6, Part B, 4.4.5.1
        and Vol 4, Part E, 7.8.67 define the six-event acquisition window.
    """
    interval = 120
    interval_us = interval * LL_PERIODIC_INTERVAL_UNIT_US
    await tester.command(
        scan_parameters(
            extended=True,
            scan_types=[0],
            scan_intervals=[SCANNING_SCAN_INTERVAL],
            scan_windows=[SCANNING_SCAN_INTERVAL],
        )
    )
    await tester.command(
        create_sync(
            options=0,
            advertising_sid=ADVERTISING_SID,
            advertiser_address_type=hci.AddressType.PUBLIC_DEVICE,
            advertiser_address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
            skip=0,
            sync_timeout=3 * interval_us // hci.HCI_LE_PERIODIC_SYNC_TIMEOUT_UNIT_US,
            sync_cte_type=0,
        )
    )
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0))
    advertiser = PeriodicAdvertiser(
        PeriodicTrain(tester.radio.now_us + FIRST_SYNC_LEAD_US, interval=interval),
        tester.radio.now_us + REPORT_DELIVERY_US + LL_MAFS_US,
        50000,
    )
    for _ in range(10):
        await advertiser.announce_until(
            tester,
            min(
                advertiser.train.anchor,
                advertiser.next_advertisement + advertiser.advertising_interval_us,
            ),
        )
        await tester.wait(REPORT_DELIVERY_US)
        if announcement := tester.take(lambda event: isinstance(event, ExtendedReport)):
            break
    else:
        raise AssertionError('no first periodic announcement report')
    report = announcement.reports[0]
    assert report.event_type == 0
    assert report.address_type == hci.AddressType.PUBLIC_DEVICE
    assert report.primary_phy == report.secondary_phy == Phy.LE_1M
    assert (
        report.address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
        and report.advertising_sid == ADVERTISING_SID
    )
    assert report.periodic_advertising_interval == interval
    lead = advertiser.train.anchor - tester.radio.now_us
    assert lead >= interval_us, 'first SyncInfo arrived too late for event one'
    tester.output.write(
        f'FIRST SYNC GUIDANCE anchor_us={advertiser.train.anchor} '
        f'reported_us={tester.radio.now_us} lead_us={lead}\n'
    )
    failure = None

    def check_events(missed):
        nonlocal failure
        assert not any(
            isinstance(event, (SyncLost, PeriodicReport)) for event in tester.events
        ), 'a failed acquisition must not receive periodic data or lose sync'
        while event := tester.take(lambda event: isinstance(event, SyncEstablished)):
            assert event.status == ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR
            assert missed >= FIRST_SYNC_MISSED_EVENTS, (
                'premature synchronization failure'
            )
            assert failure is None, 'duplicate synchronization failure'
            failure = event

    check_events(0)
    for missed in range(1, FIRST_SYNC_MISSED_EVENTS + 1):
        anchor = advertiser.train.anchor
        await advertiser.announce_until(tester, anchor)
        check_events(missed - 1)
        await tester.wait(max(0, anchor + REPORT_DELIVERY_US - tester.radio.now_us))
        advertiser.train.next_event()
        tester.output.write(f'FIRST SYNC OMITTED event={missed} anchor_us={anchor}\n')
        check_events(missed)
    for index in range(FIRST_SYNC_MISSED_EVENTS):
        anchor = advertiser.train.anchor
        await advertiser.transmit(tester, b'periodic')
        await tester.wait(REPORT_DELIVERY_US)
        tester.output.write(
            f'FIRST SYNC SENT event={FIRST_SYNC_MISSED_EVENTS + index + 1} '
            f'anchor_us={anchor}\n'
        )
        check_events(FIRST_SYNC_MISSED_EVENTS)
        assert failure is not None, 'no synchronization failure after six missed events'
    advertiser.advertising = False
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))
    check_events(FIRST_SYNC_MISSED_EVENTS)


# Verify synchronization fails after six initial periodic receive opportunities without
# AUX_SYNC_IND, even when valid periodic packets start at the seventh event.
PROCEDURES |= procedure_cases(
    first_aux_sync_ind_never_received,
    {
        'LL/DDI/SCN/BV-37-C': (
            'First AUX_SYNC_IND Never Received',
            {},
        ),
    },
)


async def extended_scanning_periodic_advertising_reception_reporting_initially_disabled(
    tester, *, primary_phy, secondary_phy
):
    """Extended Scanning, Periodic Advertising Reception, Reporting Initially Disabled.

    Initial Condition:
        Public-address continuous passive scanning uses accept-all filtering.
        Table 4.2-74 assigns 1M/1M to BV-38, 1M/2M to BV-60 and Coded/Coded
        to BV-61. Use a 100-ms periodic train, Skip 0 and data fitting one PDU.
        Sequence-tagged seeded payloads distinguish stale reports from new data.

    Test Case Configuration:
        LL/DDI/SCN/BV-38-C: LE 1M primary, LE 1M secondary.
        LL/DDI/SCN/BV-60-C: LE 1M primary, LE 2M secondary.
        LL/DDI/SCN/BV-61-C: LE Coded primary, LE Coded secondary.

    Test Steps:
        1. Set extended scan parameters with the assigned primary Scanning_PHYs,
           passive Scan_Type 0, interval/window 0x0010, public own address and
           accept-all policy; require successful Command Complete.
        2. Enable extended scanning with Filter_Duplicates, Duration and Period
           zero; require successful Command Complete.
        3. Begin ADV_EXT_IND with AuxPtr to AUX_ADV_IND on the assigned PHYs.
           The auxiliary contains public LT AdvA and SyncInfo. Continue
           announcements throughout the procedure until the postamble.
        4. Generate AUX_SYNC_IND on the specified secondary PHY with independent
           CSA #2 channels; continue periodic advertising until Step 16.
        5. Receive LE Extended Advertising Report with nonzero periodic interval.
           Validate public advertiser identity, SID and both PHYs.
        6. Send Periodic Advertising Create Sync with Use List clear and
           Reporting Initially Disabled set, the Step 5 SID, public LT identity,
           Skip 0, timeout three periodic intervals converted to HCI units and
           Sync_CTE_Type 0. Require successful Command Status.
        7. Receive successful Sync Established with matching identity/PHY/
           interval/clock accuracy and a valid handle.
        8. Receive no periodic reports for at least ten complete periodic
           intervals. The harness transmits eleven events to cover this window.
        9. Send Periodic Advertising Receive Enable for that handle with Enable
           1; require successful Command Complete.
        10. Receive periodic report events and verify handle, exact sequence/data,
            fragment statuses and no CTE. Reassemble fragments for each dataset.
        11. Repeat Step 10 until at least thirty HCI report events have arrived.
            Count the serialized fragments, not the reassembled data sets.
        12. Send Periodic Advertising Receive Enable with Enable 0; require
            successful Command Complete.
        13. Receive no periodic reports for at least ten complete intervals;
            again transmit eleven events to cover the full window.
        14. Send Periodic Advertising Terminate Sync for that handle; require
            successful Command Complete.
        15. Disable scanning with LE Set Extended Scan Enable; require successful
            Command Complete (see Source Correction).
        16. Stop the Lower Tester periodic stream.

    Expected Outcome:
        Pass verdict: synchronization succeeds using CSA #2 and the advertised
        SID. Periodic reports occur only while reporting is enabled; both
        disabled phases remain silent without losing synchronization.
        Termination ends periodic reception. The source requires thirty HCI
        reports, not thirty reassembled multireport data sets.

    Postamble:
        Between termination and scan disable, transmit one more periodic event
        and require no report, new establishment or synchronization-loss event.
        Stop extended announcements before disabling scanning.

    Source Correction:
        Step 6 names Options bit 2 as Reporting Initially Disabled; use bit 1.
        Bit 2 enables initial duplicate filtering instead. Step 4's ADV_SYNC_IND
        means AUX_SYNC_IND; the command's CTE_Type means Sync_CTE_Type.
        Step 15's legacy Scan Enable means Extended Scan Enable. These are
        local field/command corrections, not adopted SIG errata.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4.18, LL/DDI/SCN/BV-38-C, LL/DDI/SCN/BV-60-C,
        LL/DDI/SCN/BV-61-C; PDF pages 357-360, printed pages 4.2-264-4.2-267,
        Table 4.2-74, Figure 4.2-107, Steps 1-16 and every pass condition.
        Core Specification 6.3, Vol 4, Part E, 7.8.67 and 7.8.65 define
        the Options field and extended scan control.
    """
    await tester.command(
        scan_parameters(
            extended=True,
            scanning_phys=1 << primary_phy - 1,
            scan_types=[0],
            scan_intervals=[SCANNING_SCAN_INTERVAL],
            scan_windows=[SCANNING_SCAN_INTERVAL],
        )
    )
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0))
    interval_us = SYNCHRONIZATION_PERIODIC_INTERVAL * LL_PERIODIC_INTERVAL_UNIT_US
    advertiser = PeriodicAdvertiser(
        PeriodicTrain(
            tester.radio.now_us + interval_us,
            interval=SYNCHRONIZATION_PERIODIC_INTERVAL,
            phy=secondary_phy,
        ),
        tester.radio.now_us + REPORT_DELIVERY_US,
        SYNCHRONIZATION_ADVERTISING_INTERVAL_US,
        auxiliary_offset_us=AUXILIARY_OFFSET_US,
        primary_phy=primary_phy,
    )
    await advertiser.transmit(tester, b'before-sync')
    report = (await tester.event(ExtendedReport)).reports[0]
    assert report.address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
    assert report.address_type == hci.AddressType.PUBLIC_DEVICE
    assert report.advertising_sid == ADVERTISING_SID
    assert report.primary_phy == primary_phy and report.secondary_phy == secondary_phy
    assert report.periodic_advertising_interval == SYNCHRONIZATION_PERIODIC_INTERVAL
    command = _sync_command()
    command.options = SyncOptions.REPORTING_INITIALLY_DISABLED
    await tester.command(command)
    handle = None
    for _ in range(6):
        await advertiser.transmit(tester, b'acquisition')
        await tester.wait(REPORT_DELIVERY_US)
        if established := tester.take(lambda event: isinstance(event, SyncEstablished)):
            handle = _check_established(established, phy=secondary_phy)
            break
    assert handle is not None, 'periodic synchronization was not established'

    async def send(sequence, *, enabled):
        data = sequence.to_bytes(2, 'little') + random.Random(sequence).randbytes(
            PERIODIC_DATA_LENGTH - 2
        )
        await advertiser.transmit(tester, data)
        await tester.wait(REPORT_DELIVERY_US)
        if enabled:
            reports = [await tester.event(PeriodicReport)]
            while (
                reports[-1].data_status == ExtendedReport.DATA_INCOMPLETE_MORE_TO_COME
            ):
                reports.append(await tester.event(PeriodicReport))
            assert _periodic_reception_check_reports(reports, data, (), handle)
        assert not any(
            isinstance(event, (PeriodicReport, SyncLost, SyncEstablished))
            for event in tester.events
        )
        return len(reports) if enabled else 0

    for index in range(11):
        await send(index, enabled=False)
    await tester.command(
        hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command(handle, 1)
    )
    report_count = 0
    sequence = 11
    while report_count < 30:
        report_count += await send(sequence, enabled=True)
        sequence += 1
    await tester.command(
        hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command(handle, 0)
    )
    for _ in range(11):
        await send(sequence, enabled=False)
        sequence += 1
    await tester.command(hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle))
    await send(sequence, enabled=False)
    advertiser.advertising = False
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


# Verify periodic synchronization can succeed while reporting is initially disabled,
# reports start when enabled and stop when disabled again.
PROCEDURES |= procedure_cases(
    extended_scanning_periodic_advertising_reception_reporting_initially_disabled,
    {
        'LL/DDI/SCN/BV-38-C': (
            (
                'Extended Scanning, Periodic Advertising Reception, '
                'Reporting Initially Disabled – LE 1M PHY'
            ),
            {'primary_phy': Phy.LE_1M, 'secondary_phy': Phy.LE_1M},
        ),
        'LL/DDI/SCN/BV-60-C': (
            (
                'Extended Scanning, Periodic Advertising Reception, '
                'Reporting Initially Disabled – LE 2M PHY'
            ),
            {'primary_phy': Phy.LE_1M, 'secondary_phy': Phy.LE_2M},
        ),
        'LL/DDI/SCN/BV-61-C': (
            (
                'Extended Scanning, Periodic Advertising Reception, '
                'Reporting Initially Disabled – LE Coded PHY'
            ),
            {'primary_phy': Phy.LE_CODED, 'secondary_phy': Phy.LE_CODED},
        ),
    },
)


async def periodic_advertising_reception_channel_map_update(tester):
    """Periodic Advertising Reception, Channel Map Update.

    Initial Condition:
        Public-address passive scanning with accept-all filtering. All primary channels
        and all 37 secondary data channels are enabled in the initial channel map.

    Test Steps:
        1. Send HCI LE Set Extended Scan Parameters with Scanning_PHYs=LE 1M,
           Scan_Type[0]=0x00, Scan_Interval[0]=Scan_Window[0]=0x0010,
           Own_Address_Type=0x00 and Scanning_Filter_Policy=0x00. Receive
           successful HCI Command Complete.
        2. Enable extended scanning with Filter_Duplicates=Duration=Period=0;
           receive successful Command Complete.
        3. Begin ADV_EXT_IND and AUX_ADV_IND advertising on LE 1M. The primary
           AuxPtr points to the auxiliary PDU containing the Lower Tester AdvA
           and SyncInfo for AUX_SYNC_IND. Continue until Step 9.
        4. Generate AUX_SYNC_IND on the CSA#2 channels specified by SyncInfo.
           Every PDU, including those before synchronization, contains 31
           seeded random AdvData octets in the inclusive range 1..254.
        5. Receive HCI LE Extended Advertising Report. Verify the public Lower
           Tester address/type, SID, LE 1M primary/secondary PHY, event type
           and the periodic interval.
        6. Send HCI LE Periodic Advertising Create Sync with Options bit 0=0,
           Advertising_SID from Step 5, Advertiser_Address_Type=0x00,
           Advertiser_Address=Lower Tester, Skip=0x0000,
           Sync_Timeout=3 x Periodic_Advertising_Interval and Sync_CTE_Type=0.
           Receive successful HCI Command Status.
        7. Receive HCI LE Periodic Advertising Sync Established with Status=0
           and fields matching the Lower Tester advertisements.
        8. Receive HCI LE Periodic Advertising Report. Verify the established
           handle, exact data, complete status and absence of CTE.
        9. Cease extended advertising, continuing the periodic train.
        10. Send Channel Map Update Indication in the AUX_SYNC_IND extended
            header for at least six events before Instant. channelMapNEW
            enables only even-numbered data channels.
        11. At Instant, switch the independent CSA#2 channel calculation to
            channelMapNEW and continue periodic advertising.
        12. Continue receiving periodic reports as in Step 8. Check six reports
            before Instant and twelve after it, retaining the original handle.
            Include an event whose channel differs under the old and new maps;
            reject synchronization loss or a second establishment.

    Postamble:
        Terminate synchronization and disable extended scanning.

    Expected Outcome:
        Pass verdict: the IUT maintains synchronization and reports the correct periodic
        data after the Channel Map Update Instant, including transmissions that cannot
        be received using the old map.

    Reference:
        LL.TS.p28.pdf, standalone LL/DDI/SCN/BV-67-C; PDF pages 367-369,
        printed pages 4.2-274..276, Figure 4.2-111, Steps 1-12 and pass verdict.
        Source references [10] 4.4.2.13.1, 4.4.3.4 and [18] 1.20.
    """
    advertiser = await _start_advertiser(tester)
    train = advertiser.train
    assert train.channel_map == LL_DATA_CHANNEL_MAP_ALL
    generator = random.Random(0)
    data = bytes(generator.randint(1, 254) for _ in range(PERIODIC_DATA_LENGTH))
    await advertiser.transmit(tester, data)
    report = (await tester.event(ExtendedReport)).reports[0]
    assert (
        report.address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
        and report.address_type == hci.AddressType.PUBLIC_DEVICE
        and report.advertising_sid == ADVERTISING_SID
        and report.primary_phy == report.secondary_phy == Phy.LE_1M
        and report.periodic_advertising_interval == train.interval
        and report.event_type == 0
    )
    await tester.command(_sync_command())
    data = bytes(generator.randint(1, 254) for _ in range(PERIODIC_DATA_LENGTH))
    await advertiser.transmit(tester, data)
    handle = _check_established(await tester.event(SyncEstablished))
    event = await tester.event(PeriodicReport)
    assert event.sync_handle == handle and event.data == data
    assert event.data_status == ExtendedReport.DATA_COMPLETE
    assert event.cte_type == hci.HCI_LE_PERIODIC_REPORT_CTE_NONE
    advertiser.advertising = False
    channels = sum(1 << channel for channel in range(0, LL_DATA_CHANNEL_COUNT, 2))
    instant = train.counter + 6 & LL_EVENT_COUNTER_MASK
    acad = bytes(
        build_advertising_data(
            [
                (
                    AD_CHANNEL_MAP_UPDATE_INDICATION,
                    channels.to_bytes(LL_CHANNEL_MAP_SIZE, 'little')
                    + instant.to_bytes(2, 'little'),
                )
            ]
        )
    )
    changed_channel_events = 0
    for index in range(18):
        if index == 6:
            train.channel_map = channels
        if index >= 6 and train.channel != csa2_channel(
            train.counter, train.access_address, LL_DATA_CHANNEL_MAP_ALL
        ):
            changed_channel_events += 1
        data = bytes(generator.randint(1, 254) for _ in range(PERIODIC_DATA_LENGTH))
        await tester.transmit(
            train.channel,
            ext_pdu(acad=acad if index < 6 else b'', data=data),
            start_us=train.anchor,
            **train.parameters,
        )
        train.next_event()
        event = await tester.event(PeriodicReport)
        assert event.sync_handle == handle and event.data == data
        assert event.data_status == ExtendedReport.DATA_COMPLETE
        assert event.cte_type == hci.HCI_LE_PERIODIC_REPORT_CTE_NONE
        assert not any(
            isinstance(event, (SyncLost, SyncEstablished, PeriodicReport))
            for event in tester.events
        )
    assert changed_channel_events, 'the updated map must exercise a different channel'
    await tester.command(hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle))
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


# Verify that a passive scanner locates and receives periodic advertising, reports its
# data, and maintains synchronization across a Channel Map Update Indication from the
# Lower Tester.
PROCEDURES |= procedure_cases(
    periodic_advertising_reception_channel_map_update,
    {
        'LL/DDI/SCN/BV-67-C': (
            'Periodic Advertising Reception, Channel Map Update',
            {},
        ),
    },
)


def _adi_data(generator):
    """Section 4.2.4.19: one hundred random AdvData octets in 1..254."""
    return bytes(generator.randint(1, 254) for _ in range(COMPATIBILITY_DATA_LENGTH))


async def _synchronize_with_adi(tester, data, did, *, filter_duplicates=False):
    """Section 4.2.4.19 steps 1-7, with an ADI in every AUX_SYNC_IND.

    Start passive scanning and the announcements, check the train's extended
    report, create the sync and send AUX_SYNC_IND until it is established.
    ``filter_duplicates`` sets both scanning Filter_Duplicates and Create Sync
    Options bit 2: BV-69-C sets both, BV-70-C neither. Return the advertiser,
    the sync handle and the number of reports from the establishing event.
    """
    assert await le_features(tester) & (
        hci.LeFeatureMask.PERIODIC_ADVERTISING_ADI_SUPPORT
    ), 'this source requires Periodic Advertising ADI Support'
    adi = ADVERTISING_SID << PDU_ADV_ADI_DID_BITS | did
    advertiser = await _start_advertiser(tester, duplicates=filter_duplicates)
    await advertiser.transmit(tester, data, adi=adi)
    report = (await tester.event(ExtendedReport)).reports[0]
    assert report.address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
    assert report.address_type == hci.AddressType.PUBLIC_DEVICE
    assert report.advertising_sid == ADVERTISING_SID
    assert report.periodic_advertising_interval == SYNCHRONIZATION_PERIODIC_INTERVAL
    await tester.command(_sync_command(filter_duplicates=filter_duplicates))
    for _ in range(LL_PERIODIC_SYNC_ESTABLISHMENT_EVENTS):
        await advertiser.transmit(tester, data, adi=adi)
        await tester.wait(REPORT_DELIVERY_US)
        if established := tester.take(lambda event: isinstance(event, SyncEstablished)):
            break
    else:
        raise AssertionError('periodic synchronization was not established')
    handle = _check_established(established)
    reports = 0
    while event := tester.take(lambda event: isinstance(event, PeriodicReport)):
        assert event.sync_handle == handle and event.data == data
        reports += 1
    return advertiser, handle, reports


async def _send_adi_event(tester, advertiser, handle, data, did):
    """Send one AUX_SYNC_IND with the ADI; return how many reports it caused.

    Each report must carry this event's data, complete, with a valid RSSI, and
    the sync must stay established.
    """
    await advertiser.transmit(
        tester, data, adi=ADVERTISING_SID << PDU_ADV_ADI_DID_BITS | did
    )
    await tester.wait(REPORT_DELIVERY_US)
    reports = []
    while event := tester.take(lambda event: isinstance(event, PeriodicReport)):
        assert event.sync_handle == handle and event.data == data
        assert event.data_status == ExtendedReport.DATA_COMPLETE
        assert -127 <= event.rssi <= 20
        reports.append(event)
    assert len(reports) <= 1, 'one periodic event produced several reports'
    assert not any(
        isinstance(event, (SyncLost, SyncEstablished)) for event in tester.events
    ), 'synchronization was lost or established twice'
    return len(reports)


async def periodic_adi_duplicates_filtered(tester):
    """Scanner supporting Periodic Advertising Filtering accepts AUX_SYNC_IND with
    ADI.

    Initial Condition:
        Passive scanning with a selected interval and window and accept-all
        filtering. The IUT supports Periodic Advertising ADI and has not received
        Set Default Periodic Advertising Sync Transfer Parameters since its last
        reset.

    Test Case Configuration:
        The new random DID in step 12 differs from the previous one.

    Test Steps:
        1. Send HCI LE Set Extended Scan Parameters with Scanning_PHYs=0x01,
           Scan_Type[0]=0x00, Scan_Interval[0]=Scan_Window[0]=0x0010,
           Own_Address_Type=0x00 and Scanning_Filter_Policy=0x00. Receive
           successful HCI Command Complete.
        2. Enable extended scanning with Filter_Duplicates=0x01 and
           Duration=Period=0; receive successful Command Complete.
        3. Begin ADV_EXT_IND and AUX_ADV_IND advertising on LE 1M. The primary
           AuxPtr points to the auxiliary PDU, which contains the Lower Tester
           AdvA and SyncInfo referring to AUX_SYNC_IND. Continue until the end.
        4. Generate AUX_SYNC_IND on the CSA #2 channels specified in SyncInfo,
           with AdvData length 100, 100 random octets and a random ADI DID.
        5. Receive HCI LE Extended Advertising Report with a nonzero periodic
           interval.
        6. Send HCI LE Periodic Advertising Create Sync with Options bit 0 = 0,
           Options bit 2 = 1 (duplicate filtering), the step 5 Advertising_SID,
           Advertiser_Address_Type=0x00 and the Lower Tester address. Receive
           successful Command Status.
        7. Receive HCI LE Periodic Advertising Sync Established with Status=0
           and fields matching the Lower Tester advertisements.
        8. The IUT receives AUX_SYNC_IND.
        9. Receive HCI LE Periodic Advertising Report.
        10. The IUT receives AUX_SYNC_IND with the step 8 ADI DID and may send a
            report; the Lower Tester warns if it does.
        11. Repeat step 10 for nine periodic advertising intervals.
        12. Generate AUX_SYNC_IND with 100 new random octets that differ from
            step 4 and a random ADI DID.
        13. The IUT receives AUX_SYNC_IND with the different ADI DID.
        14. Receive HCI LE Periodic Advertising Report.
        15. The IUT receives AUX_SYNC_IND with the step 13 ADI DID and may send a
            report; the Lower Tester warns if it does.
        16. Repeat step 15 for nine periodic advertising intervals.

    Observation Setup:
        Read the local features to establish the ADI initial condition. The
        Lower Tester announces its 100-ms train every 20.5 ms, and Sync_Timeout
        is three periodic intervals. The AUX_SYNC_IND that establishes the sync
        is the step 8 event. These are local choices, not source steps.

    Additional Checks and Postamble:
        Every report must contain the original handle, the current 100-octet
        data, complete status and a valid RSSI, without loss or a second
        establishment. Terminate the sync and disable extended scanning after
        step 16.

    Expected Outcome:
        Pass verdict: the IUT reports steps 9 and 14 and sends no report after
        steps 10 and 15, as the source's verdict requires.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4.19, independently titled LL/DDI/SCN/BV-69-C;
        PDF pages 373-375, printed pages 4.2-280..282, Figure 4.2-114,
        Steps 1-16 and pass verdict.
        Source references [19] 2.3.1.7 and 4.6.34.
    """
    generator = random.Random(0)
    data = _adi_data(generator)
    did = generator.randrange(PDU_ADV_ADI_DID_MASK + 1)
    advertiser, handle, reports = await _synchronize_with_adi(
        tester, data, did, filter_duplicates=True
    )

    async def duplicates():
        """Steps 10-11 and 15-16: no report for the repeated ADI."""
        for _ in range(ADI_DUPLICATES_PER_PHASE):
            assert not await _send_adi_event(tester, advertiser, handle, data, did), (
                'duplicate filtering reported a repeated ADI'
            )

    # Steps 8-9: the event that establishes the sync is reported.
    assert reports == 1, 'the first AUX_SYNC_IND was not reported'
    await duplicates()
    # Step 12: new data and a new random DID.
    data = _adi_data(generator)
    did = (did + 1 + generator.randrange(PDU_ADV_ADI_DID_MASK)) & PDU_ADV_ADI_DID_MASK
    # Steps 13-14.
    assert await _send_adi_event(tester, advertiser, handle, data, did), (
        'the new DID was not reported'
    )
    await duplicates()
    advertiser.advertising = False
    await tester.command(hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle))
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


# Verify that duplicate filtering enabled at Create Sync reports the first AUX_SYNC_IND
# of each ADI and suppresses the repeats.
PROCEDURES |= procedure_cases(
    periodic_adi_duplicates_filtered,
    {
        'LL/DDI/SCN/BV-69-C': (
            (
                'Scanner supporting Periodic Advertising Filtering accepts '
                'AUX_SYNC_IND with ADI'
            ),
            {},
        ),
    },
)


async def periodic_duplicate_filtering_enabled_after_sync(tester):
    """Scanner enables Duplicate Filtering after initially disabled.

    Initial Condition:
        Passive scanning with a selected interval and window and accept-all
        filtering. The IUT supports Periodic Advertising ADI and has not received
        Set Default Periodic Advertising Sync Transfer Parameters since its last
        reset.

    Test Case Configuration:
        Steps 11-13 cover the ten periodic events after Receive Enable 0x03,
        with step 11 the first of them; steps 16-17 cover the ten events after
        the first one with the step 14 DID.

    Test Steps:
        1. Send HCI LE Set Extended Scan Parameters with Scanning_PHYs=0x01,
           Scan_Type[0]=0x00, Scan_Interval[0]=Scan_Window[0]=0x0010,
           Own_Address_Type=0x00 and Scanning_Filter_Policy=0x00. Receive
           successful HCI Command Complete.
        2. Enable extended scanning with Filter_Duplicates, Duration and Period
           zero; receive successful Command Complete.
        3. Begin ADV_EXT_IND and AUX_ADV_IND advertising on LE 1M, with the
           Lower Tester AdvA and SyncInfo in AUX_ADV_IND. Continue until step 22.
        4. Generate AUX_SYNC_IND on the CSA #2 channels specified in SyncInfo,
           with AdvData length 100, 100 random octets and a random ADI DID.
        5. Receive HCI LE Extended Advertising Report with a nonzero periodic
           interval.
        6. Send HCI LE Periodic Advertising Create Sync with Options bit 0 = 0,
           Options bit 2 = 0, the step 5 Advertising_SID,
           Advertiser_Address_Type=0x00 and the Lower Tester address. Receive
           successful Command Status.
        7. Receive HCI LE Periodic Advertising Sync Established with Status=0
           and fields matching the Lower Tester advertisements.
        8. Receive HCI LE Periodic Advertising Report for the AUX_SYNC_IND.
        9. Repeat step 8 for nine events.
        10. Send HCI LE Set Periodic Advertising Receive Enable with the step 7
            Sync_Handle and Enable 0x03 (duplicate filtering enabled); receive
            successful Command Complete.
        11. The IUT may report the AUX_SYNC_IND with the step 4 DID.
        12. The IUT receives AUX_SYNC_IND with the step 4 DID and may report it;
            the Lower Tester warns if it does.
        13. Repeat step 12 for nine periodic advertising intervals.
        14. Generate AUX_SYNC_IND with the step 4 data and SID and a different
            ADI DID.
        15. Receive HCI LE Periodic Advertising Report.
        16. The IUT receives AUX_SYNC_IND with the step 14 DID and may report it;
            the Lower Tester warns if it does.
        17. Repeat step 16 for nine periodic advertising intervals.
        18. Send HCI LE Set Periodic Advertising Receive Enable with the step 7
            Sync_Handle and Enable 0x01 (duplicate filtering disabled); receive
            successful Command Complete.
        19. The IUT receives AUX_SYNC_IND with the step 14 DID.
        20. Receive HCI LE Periodic Advertising Report for each AUX_SYNC_IND.
        21. Repeat steps 19 and 20 for nine events.
        22. The Lower Tester stops advertising.

    Observation Setup:
        Read the local features to establish the ADI initial condition. The
        Lower Tester announces its 100-ms train every 20.5 ms, and Sync_Timeout
        is three periodic intervals. A report from the event that establishes
        the sync is accepted, but not counted in step 8. These are local
        choices, not source steps.

    Expected Outcome:
        Pass verdict: steps 8-9 report every event; steps 11-13 and 16-17 do not
        report every event; step 15 reports the new DID; steps 19-21 report every
        event. Every report has the step 7 handle, the step 4 data and complete
        status, and synchronization is never lost. Reports from steps 12-13 and
        16-17 are recorded as warnings, as the source's Lower Tester does.

    Postamble:
        Terminate the sync and disable extended scanning.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4.19, independently titled LL/DDI/SCN/BV-70-C;
        PDF pages 376-379, printed pages 4.2-283..286, Figures 4.2-115/116,
        Steps 1-22 and pass verdict.
        Source references [19] 2.3.1.7 and 4.6.34.
    """
    generator = random.Random(0)
    data = _adi_data(generator)
    did = generator.randrange(PDU_ADV_ADI_DID_MASK + 1)
    advertiser, handle, _ = await _synchronize_with_adi(tester, data, did)

    async def send():
        return await _send_adi_event(tester, advertiser, handle, data, did)

    def warn(step, reports):
        if reports:
            tester.output.write(f'WARNING step {step}: {reports} duplicate reports\n')

    # Steps 8-9: reports for ten events with duplicate filtering disabled.
    assert all([await send() for _ in range(ADI_REPORTS_PER_PHASE)])
    receive_enable = hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command
    await tester.command(receive_enable(handle, 0x03))
    # Steps 11-13: the first event may be reported, later ones only warn.
    counts = [await send() for _ in range(ADI_REPORTS_PER_PHASE)]
    assert not all(counts), 'duplicate filtering reported every event'
    warn('12-13', sum(counts[1:]))
    did = (did + 1 + generator.randrange(PDU_ADV_ADI_DID_MASK)) & PDU_ADV_ADI_DID_MASK
    # Steps 14-15: the new DID is reported.
    assert await send(), 'the new DID was not reported'
    # Steps 16-17.
    counts = [await send() for _ in range(ADI_REPORTS_PER_PHASE)]
    assert not all(counts), 'duplicate filtering reported every event'
    warn('16-17', sum(counts))
    await tester.command(receive_enable(handle, 0x01))
    # Steps 19-21: every event is reported again.
    assert all([await send() for _ in range(ADI_REPORTS_PER_PHASE)])
    advertiser.advertising = False
    await tester.command(hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle))
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


# Verify that Receive Enable starts and stops duplicate filtering on a running
# synchronization.
PROCEDURES |= procedure_cases(
    periodic_duplicate_filtering_enabled_after_sync,
    {
        'LL/DDI/SCN/BV-70-C': (
            'Scanner enables Duplicate Filtering after initially disabled',
            {},
        ),
    },
)


async def extended_scanning_periodic_advertising_properly_ignore_rfu_fields(tester):
    """Extended Scanning, Periodic Advertising, Properly Ignore RFU Fields.

    Initial Condition:
        Standby with the selected Scan Max Data IXIT. Use LE 1M throughout and public,
        accept-all passive scanning. Select a nonconnectable, nonscannable event
        directed to the IUT: the primary PDU supplies its meaningful TargetA, so the
        repeated auxiliary TargetA is RFU under the CEAP field-presence conditions.

    Test Steps:
        1. Send HCI LE Set Extended Scan Parameters with Scanning_PHYs=0x01,
           Scan_Type[0]=0x00, Scan_Interval[0]=Scan_Window[0]=0x0010,
           Own_Address_Type=0x00 and Scanning_Filter_Policy=0x00. Receive
           successful HCI Command Complete.
        2. Enable extended scanning with Filter_Duplicates=Duration=Period=0;
           receive successful Command Complete.
        3. Begin ADV_EXT_IND and AUX_ADV_IND advertising on LE 1M, populating
           all context-reserved fields. The primary AuxPtr points to the
           auxiliary PDU, which contains the real Lower Tester AdvA and
           SyncInfo referring to AUX_SYNC_IND. Continue until Step 10.
        4. Generate AUX_SYNC_IND on the CSA#2 channels specified by SyncInfo,
           with all RFU fields populated. Continue until Step 13.
        5. Receive HCI LE Extended Advertising Report with a nonzero periodic
           interval. Verify public advertiser identity, SID, directed-to-IUT
           identity and LE 1M primary/secondary PHYs.
        6. Send HCI LE Periodic Advertising Create Sync: direct address/list
           disabled, Advertising_SID from Step 5, Advertiser_Address_Type=0x00,
           Advertiser_Address=Lower Tester, Sync_Timeout=3 x reported periodic
           interval and the source's Unused=0. Encode Options=0,
           Sync_CTE_Type=0 and select Skip=0; receive successful Command Status.
        7. Receive HCI LE Periodic Advertising Sync Established with Status=0
           and fields matching the advertisements. Validate handle range,
           public address/type, SID, PHY, interval and clock accuracy.
        8. Receive one or more HCI LE Periodic Advertising Reports. If AdvData
           is present, verify that the report data matches the transmitted data.
        9. Repeat Step 8 until at least five reports have been received. Here
           each complete report contains 100 seeded data octets, the original
           handle and valid RSSI; reject sync loss or a second establishment.
        10. Cease extended advertising.
        11. Send HCI LE Periodic Advertising Terminate Sync; receive successful
            Command Complete.
        12. Disable scanning; receive successful Command Complete. Use HCI LE
            Set Extended Scan Enable, as explained in Source Correction.
        13. Cease periodic advertising.

    RFU Stimulus and Additional Check:
        Populate primary CTEInfo, SyncInfo, ACAD and AdvData; preserve the
        meaningful primary TargetA and fill auxiliary CTEInfo and the repeated
        TargetA with different data. AUX_SYNC_IND carries RFU AdvA, TargetA and
        SyncInfo. Set applicable RFU header bits, Extended Header Flags bit 7
        and SyncInfo offset RFU bits without changing meaningful timing fields.
        Round AuxOffset up from the enlarged primary airtime plus T_MAFS to an
        encodable offset. After Step 11 and before Step 12, send another periodic
        PDU and reject any periodic report, sync loss or new establishment.

    Expected Outcome:
        Pass verdict: RFU fields do not affect advertiser or target identity, timing,
        successful CSA#2 synchronization, or the contents of periodic reports. At
        least five periodic reports contain the transmitted data, synchronization is
        maintained, and successful termination prevents subsequent periodic reports.

    Source Correction:
        Source Step 6 uses the older Filter_Policy and Unused field names; encode
        Options 0 and Sync_CTE_Type 0 in LE Periodic Advertising Create Sync. Step 12
        names the legacy scan-disable command after extended scanning; use LE Set
        Extended Scan Enable. These local corrections preserve the test intent;
        Amber corroborated both mappings. They are not adopted SIG errata.

    Reference:
        LL.TS.p28.pdf, standalone LL/DDI/SCN/BV-89-C; PDF pages 396-399,
        printed pages 4.2-303..306, Figure 4.2-124, Steps 1-13 and pass verdict.
        Source reference [10] 2.3, 4.4.2.13.1, 4.4.3.4. Core Vol 6 Part B
        2.3.1.5-2.3.1.7 defines CEAP field permissions and address presence;
        Vol 4 Part E 7.8.65 and 7.8.67 define scan enable and Create Sync.
    """
    await _periodic_header_compatibility(tester)


# Verify that a scanner ignores all RFU fields in extended announcements and periodic
# packets, establishes synchronization, reports the advertised data, and terminates
# periodic reception when instructed by the Host.
PROCEDURES |= procedure_cases(
    extended_scanning_periodic_advertising_properly_ignore_rfu_fields,
    {
        'LL/DDI/SCN/BV-89-C': (
            'Extended Scanning, Periodic Advertising, Properly Ignore RFU Fields',
            {},
        ),
    },
)


async def pawr_trains_with_same_identity_do_not_synchronize(tester):
    """Periodic Advertising with Responses Trains, Do Not Synchronize with Same Address,
    Address Type, Advertising SID.

    Initial Condition:
        The IUT starts in Standby or Device Address Set. Table 4.2-85 selects
        AUX_SYNC_IND for LL/DDI/SCN/BV-96-C; PAwR subevents are not used in this
        variant despite the shared source procedure's title. The Lower Tester
        uses a public address and independent CSA#2 periodic traffic on LE 1M.

    Test Steps:
        1. Set extended scan parameters to public own address, accept-all
           filtering, LE 1M, passive scan type, and interval/window 0x0010.
           Require successful Command Complete.
        2. Enable scanning with Filter_Duplicates 0x01 and zero Duration/Period.
           Require successful Command Complete.
        3. Start ADV_EXT_IND announcements whose AuxPtr refers to AUX_ADV_IND
           carrying the public AdvA, SID, and valid SyncInfo. Continue these
           announcements throughout the test. The unselected
           AUX_SYNC_SUBEVENT_IND branch additionally supplies PAwR Timing
           Information (subeventInterval=0x0C, responseSlotDelay=0x01,
           responseSlotSpacing=0x0A); it is inapplicable to BV-96-C.
        4. Transmit AUX_SYNC_IND on the independently calculated CSA#2 channels
           and event times described by SyncInfo.
        5. Require a complete extended advertising report with matching public
           advertiser identity, SID, PHYs, and nonzero periodic interval.
        6. Send LE Periodic Advertising Create Sync using that identity and
           SID, Options 0, Skip 0, Sync_CTE_Type 0, and a timeout of three
           periodic intervals. Require successful Command Status.
        7. Require successful Sync Established with matching identity, SID, PHY,
           periodic interval, and clock accuracy. Validate periodic data on
           the established handle.
        8. Continue receiving data for ten full periodic intervals, then repeat
           Create Sync with the same advertiser address, public address type,
           SID and Options=0x00 (direct address, reporting initially enabled,
           duplicate filtering disabled).
        9. Select Alternative 9A or 9B according to HCI Command Status:
           9A.1. Receive Command Status with a valid nonzero error code.
           9B.1. Receive successful Command Status.
           9B.2. Receive Sync Established with a valid nonzero error code.
           Reject RFU error codes or a second successful synchronization.

    Additional Check and Postamble:
        Verify further periodic data on the original handle without sync loss,
        then terminate synchronization and disable extended scanning.

    Expected Outcome:
        Pass verdict:
            The duplicate request fails through source alternative 9A or 9B,
            with a valid error code rather than a second successful sync.
            As an additional continuity check, the original synchronization
            continues to deliver the exact data on its original handle.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4.25, Table 4.2-85,
        LL/DDI/SCN/BV-96-C; PDF pages 413-415, printed pages 4.2-320 through
        4.2-322, Figure 4.2-131. Steps 1-9, including Alternatives 9A/9B,
        and Expected Outcome; source reference [23] 4.4.5.1.2.
    """
    advertiser = await _start_advertiser(tester, duplicates=True)
    await advertiser.transmit(tester, b'before-sync')
    report = (await tester.event(ExtendedReport)).reports[0]
    assert report.address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
    assert report.address_type == hci.AddressType.PUBLIC_DEVICE
    assert report.advertising_sid == ADVERTISING_SID
    assert (
        report.event_type == 0
        and report.periodic_advertising_interval == SYNCHRONIZATION_PERIODIC_INTERVAL
    )
    assert report.primary_phy == report.secondary_phy == hci.Phy.LE_1M
    await tester.command(_sync_command())
    handle = await _transmit_report(tester, advertiser)
    for _ in range(DUPLICATE_OBSERVATION_EVENTS):
        await _transmit_report(tester, advertiser, handle=handle)
    valid_statuses = tuple(
        code for code in ErrorCode if code != ErrorCode.RESERVED_FOR_FUTURE_USE
    )
    result = await tester.command(_sync_command(), valid_statuses)
    if result.status == ErrorCode.SUCCESS:
        event = await tester.event(SyncEstablished)
        assert event.status in valid_statuses and event.status != ErrorCode.SUCCESS
    await _transmit_report(tester, advertiser, handle=handle)
    await tester.command(hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle))
    advertiser.advertising = False
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


# Verify that an IUT already synchronized to a periodic advertiser rejects a second
# synchronization request with the same address, address type, and Advertising SID.
PROCEDURES |= procedure_cases(
    pawr_trains_with_same_identity_do_not_synchronize,
    {
        'LL/DDI/SCN/BV-96-C': (
            (
                'Periodic Advertising with Responses Trains, Do Not '
                'Synchronize with Same Address, Address Type, Advertising '
                'SID – AUX_SYNC_IND'
            ),
            {},
        ),
    },
)


async def extended_scanning_passive_periodic_advertising_report_window_widening(
    tester, *, v2=False
):
    """Extended Scanning, Passive, Periodic Advertising Report, Window Widening.

    Initial Condition:
        The IUT supports passive extended scanning and periodic synchronization
        with the selected report version. Use public addresses, accept-all filtering,
        and a continuous LE 1M scan window. The Lower Tester advertises a 500-ppm
        clock (CA=0, SCA=0). Scan_Max_Data is at least three octets, so source
        Steps 15 and 16 apply.

    Test Case Configuration:
        LL/DDI/SCN/BV-131-C: Periodic Advertising Report v1.
        LL/DDI/SCN/BV-132-C: Periodic Advertising Report v2 for the same
        AUX_SYNC_IND/AUX_CHAIN_IND train; PAwR is not required by this case.

    Test Steps:
        1. Send LE Set Extended Scan Parameters: Scanning_PHYs=0x01, passive
           Scan_Type=0x00, Scan_Interval=Scan_Window=0x0010, public own address,
           and accept-all filter policy. Require successful Command Complete.
        2. Enable extended scanning with Filter_Duplicates, Duration, and Period
           all zero. Require successful Command Complete.
        3. Send ADV_EXT_IND with AuxPtr CA=0 and nominal 200-ms AuxOffset to
           AUX_ADV_IND. Include the Lower Tester's public AdvA and SyncInfo with
           SCA=0 and Interval=1 s. Continue extended advertisements until stopped.
        4. Require an Extended Advertising Report with a nonzero periodic
           interval, Complete data status, and valid RSSI. Also check the public
           advertiser address, SID, and the selected 1-s interval.
        5. After two nominal reports, send AUX_ADV_IND 0.5 us inside the early
           widened receive-window edge. Widening is 500/1000000 * AuxOffset
           + 16 us; integer-microsecond vector limitations are recorded below.
        6. Require an Extended Advertising Report for the early-edge packet,
           with a nonzero periodic interval, Complete status, and valid RSSI.
        7. After two early-edge reports, send AUX_ADV_IND 0.5 us inside the late
           widened receive-window edge, using the same widening calculation.
        8. Require an Extended Advertising Report for the late-edge packet,
           with a nonzero periodic interval, Complete status, and valid RSSI.
        9. Send LE Periodic Advertising Create Sync: Options=0, matching SID and
           public advertiser address, Skip=0, Sync_Timeout=three times the
           reported periodic interval (3 s), and Sync_CTE_Type=0. Require
           successful Command Status, correcting the source event name.
        10. Send AUX_SYNC_IND in the 1-s train with one random data octet and an
            AuxPtr to the first AUX_CHAIN_IND. Place it 0.5 us inside the early
            acquisition-window edge, one offset unit after receiveWindowStart,
            using widening 500/1000000 * SyncOffset + 16 us.
        11. Require successful Periodic Advertising Sync Established with a
            valid handle and the advertised SID. Also check advertiser identity,
            LE 1M PHY, 1-s interval, and 500-ppm clock accuracy.
        12. Terminate synchronization using the handle from Step 11 and require
            successful Command Complete.
        13. Repeat Steps 9-11, now placing AUX_SYNC_IND 0.5 us inside the late
            acquisition-window edge, before receiveWindowEnd.
        14. Skip Steps 15-16 if Scan_Max_Data is below three octets. This IUT
            supports at least three octets, so execute both steps.
        15. Send two AUX_CHAIN_IND PDUs, each with one random data octet. Place
            the first 0.5 us inside its early widened window and the second
            0.5 us inside its late window. Separate them as far as practical
            within the 1-s interval; retain extended advertisements between
            them. Use widening 500/1000000 * AuxOffset + 16 us.
        16. Require multiple selected-version Periodic Advertising Reports on
            the same handle.
            All except the last must have Data_Status=0x01 (More); the last must
            have Data_Status=0x00 (Complete). Their concatenated data must equal
            the three transmitted octets, in order.

        Cleanup: terminate the remaining synchronization and disable scanning.

    Expected Outcome:
        Pass verdict:
            Source Steps 6 and 8 produce complete extended reports at the
            widened auxiliary edges. Step 11 establishes synchronization at
            both acquisition edges. Step 16 reports all three periodic data
            octets without truncation, with the required fragment statuses.

    Source Correction:
        Step 10's 5-s interval is treated as an editorial error: use the 1-s
        interval advertised by SyncInfo in Step 3 throughout. This interpretation
        was approved during review; it is not a published SIG erratum. Step 9
        requires Command Status, not the source's Command Complete, because
        LE Periodic Advertising Create Sync is an asynchronous HCI command.

    Review Limitation:
        The source requires 0.5-us boundary guards. The current vectors use
        rounded integer-microsecond widening and 1-us guards, with an encoded
        AuxOffset of 199980 us below the nominal 200 ms. These exercise reception
        near the boundaries but do not establish exact 0.5-us boundary coverage.
        Keep this case limited even when its executable assertions pass.

    Timing Approximation:
        Execute at 1-us timestamp resolution, retaining the limited precision
        label. Fractional guards use the documented integer-us approximation.
        Complete all executable checks without skipping for precision alone;
        only an independent source_conflict label permits a protocol-path skip.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4.31, LL/DDI/SCN/BV-131-C/BV-132-C;
        Table 4.2-99, PDF pages 468-473,
        printed pages 4.2-375 through 4.2-380. Steps 1-16 and Expected Outcome.
        Core Vol 6, Part B, Sections 4.2.4 (window widening), 4.4.3.4 and
        4.4.5.1 define the receive windows; Vol 4, Part E, Section 7.7.65.15
        defines both periodic report versions.
    """
    report_type = (
        hci.HCI_LE_Periodic_Advertising_Report_V2_Event if v2 else PeriodicReport
    )
    if v2:
        from tests.support import LE_EVENT_MASK_V1

        await tester.masks(
            le=LE_EVENT_MASK_V1
            | 1 << (hci.HCI_LE_PERIODIC_ADVERTISING_REPORT_V2_EVENT - 1)
        )
    await tester.command(
        scan_parameters(
            extended=True,
            scan_types=[0],
            scan_intervals=[SCANNING_SCAN_INTERVAL],
            scan_windows=[SCANNING_SCAN_INTERVAL],
        )
    )
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0))
    train = PeriodicTrain(
        tester.radio.now_us + PHY_TIME_UNITS_PER_SECOND,
        interval=PERIODIC_RECEPTION_PERIODIC_INTERVAL,
    )
    generator = random.Random(0)
    adi = ADVERTISING_SID << PDU_ADV_ADI_DID_BITS

    async def announce(shift=0):
        start = tester.radio.now_us + 1000
        reference = start + ANNOUNCEMENT_OFFSET_US + shift
        while train.anchor <= reference:
            train.next_event()
        sync = train.sync_info(reference)
        await tester.transmit(
            37, ext_pdu(adi=adi, aux=_pointer(ANNOUNCEMENT_OFFSET_US)), start_us=start
        )
        await tester.transmit(
            0,
            ext_pdu(address=LOWER_PERIPHERAL_PUBLIC_ADDRESS, adi=adi, sync=sync),
            start_us=reference,
        )
        await tester.wait(1000)
        if event := tester.take(lambda event: isinstance(event, AdvertisingReport)):
            report = event.reports[0]
            assert (
                report.event_type == 0
                and report.address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
            )
            assert report.address_type == hci.AddressType.PUBLIC_DEVICE
            assert report.advertising_sid == ADVERTISING_SID
            assert (
                report.periodic_advertising_interval
                == PERIODIC_RECEPTION_PERIODIC_INTERVAL
            )
            assert -127 <= report.rssi <= 20
            return (
                reference,
                PeriodicTrain.from_advertisement(
                    SimpleNamespace(start_us=reference, phy=Phy.LE_1M), {'sync': sync}
                ),
            )
        return None

    async def continue_announcements(until):
        while tester.radio.now_us + ANNOUNCEMENT_OFFSET_US + 2000 < until:
            await announce()

    margin = _widening(ANNOUNCEMENT_OFFSET_US)
    for shift in (0, -margin + 1, margin - 1):
        for _ in range(2):
            for _ in range(20):
                train.anchor = tester.radio.now_us + PHY_TIME_UNITS_PER_SECOND
                if await announce(shift):
                    break
            else:
                raise AssertionError('no auxiliary window-edge report')
    for latest in (False, True):
        await tester.command(
            create_sync(
                options=0,
                advertising_sid=ADVERTISING_SID,
                advertiser_address_type=hci.AddressType.PUBLIC_DEVICE,
                advertiser_address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
                skip=0,
                sync_timeout=3
                * PERIODIC_RECEPTION_PERIODIC_INTERVAL
                * LL_PERIODIC_INTERVAL_UNIT_US
                // hci.HCI_LE_PERIODIC_SYNC_TIMEOUT_UNIT_US,
                sync_cte_type=0,
            )
        )
        train.anchor = tester.radio.now_us + PHY_TIME_UNITS_PER_SECOND
        for _ in range(20):
            if observed := (await announce()):
                break
        else:
            raise AssertionError('no acquisition SyncInfo report')
        reference, candidate = observed
        margin = _widening(candidate.anchor - reference)
        start = (
            candidate.anchor + margin + candidate.offset_unit - 1
            if latest
            else candidate.anchor - margin + candidate.offset_unit + 1
        )
        data = bytes(generator.randint(1, 255) for _ in range(3))
        await tester.transmit(
            candidate.channel,
            ext_pdu(data=data[:1], aux=_pointer(PERIODIC_WINDOW_CHAIN_OFFSET_US)),
            start_us=start,
            **candidate.parameters,
        )
        event = await tester.event(SyncEstablished)
        assert event.status == hci.HCI_ErrorCode.SUCCESS
        assert (
            event.advertising_sid == ADVERTISING_SID
            and event.advertiser_address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
        )
        assert event.advertiser_address_type == hci.AddressType.PUBLIC_DEVICE
        assert event.advertiser_phy == Phy.LE_1M
        assert (
            event.periodic_advertising_interval == PERIODIC_RECEPTION_PERIODIC_INTERVAL
        )
        assert event.advertiser_clock_accuracy == LL_SCA_500_PPM
        handle = event.sync_handle
        if not latest:
            await tester.command(
                hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle)
            )
            tester.events[:] = [
                event for event in tester.events if not isinstance(event, report_type)
            ]
            continue
        train.anchor = (
            start + PERIODIC_RECEPTION_PERIODIC_INTERVAL * LL_PERIODIC_INTERVAL_UNIT_US
        )
        train.counter = candidate.counter + 1 & LL_EVENT_COUNTER_MASK
        first = (
            start
            + PERIODIC_WINDOW_CHAIN_OFFSET_US
            - _widening(PERIODIC_WINDOW_CHAIN_OFFSET_US)
            + 1
        )
        await continue_announcements(first)
        await tester.transmit(
            0,
            ext_pdu(data=data[1:2], aux=_pointer(PERIODIC_WINDOW_CHAIN_OFFSET_US)),
            start_us=first,
            **candidate.parameters,
        )
        second = (
            first
            + PERIODIC_WINDOW_CHAIN_OFFSET_US
            + PDU_ADV_OFFSET_UNIT_300_US
            + _widening(PERIODIC_WINDOW_CHAIN_OFFSET_US)
            - 1
        )
        last_pdu = ext_pdu(data=data[2:])
        assert second + candidate.phy.airtime_us(len(last_pdu)) < train.anchor
        await continue_announcements(second)
        await tester.transmit(0, last_pdu, start_us=second, **candidate.parameters)
        await tester.wait(1000)
        reports = []
        while report := tester.take(lambda event: isinstance(event, report_type)):
            assert type(report) is report_type and report.sync_handle == handle
            if v2:
                assert report.subevent == 0xFF
            reports.append(report)
        assert len(reports) > 1
        assert b''.join(report.data for report in reports) == data
        assert all(
            report.data_status == AdvertisingReport.DATA_INCOMPLETE_MORE_TO_COME
            for report in reports[:-1]
        )
        assert reports[-1].data_status == AdvertisingReport.DATA_COMPLETE
        await tester.command(
            hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle)
        )
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


# Verify reception of extended and periodic advertising packets at both edges of the
# receive window widened for the Lower Tester's clock accuracy.
PROCEDURES |= procedure_cases(
    extended_scanning_passive_periodic_advertising_report_window_widening,
    {
        'LL/DDI/SCN/BV-131-C': pytest.param(
            (
                'Extended Scanning, Passive, Periodic Advertising Report, '
                'Window Widening, LE Periodic Advertising Report [v1]'
            ),
            {},
            marks=[
                pytest.mark.limited,
            ],
        ),
        'LL/DDI/SCN/BV-132-C': pytest.param(
            'Extended Scanning, Passive, Periodic Advertising Report, '
            'Window Widening, LE Periodic Advertising Report [v2]',
            {'v2': True},
            marks=[pytest.mark.limited],
        ),
    },
)


async def privacy_periodic_reception(tester, *, random_identity):
    """Extended Scanning, Periodic Advertising Reception, Privacy.

    Initial Condition:
        Passive extended scanning; the advertiser uses a peer-IRK-generated RPA.

    Test Case Configuration:
        LL/DDI/SCN/BV-72-C: public identity address.
        LL/DDI/SCN/BV-73-C: random identity address.

    Test Steps:
        1-5. Populate resolver and accept list with the public/random identity,
             enable resolution and passive scanning with accept-list policy 1.
        6-8. Send ADV_EXT_IND/AUX_ADV_IND announcing a periodic train and verify
             nonzero interval, identity address and type 2/3 in the Host report.
        9. Create synchronization by identity/SID with no list, Skip zero, timeout
           three periodic intervals converted to the command's 10-ms units.
        10. Continue announcements and CSA#2 AUX_SYNC_IND; receive successful
            Sync Established matching the train and receive its periodic data.

    Expected Outcome:
        RPA resolution supplies the advertised identity to the Host and allows
        identity-based periodic acquisition; the train's CSA#2 data is reported.

    Reference:
        LL.TS.p28, Section 4.2.4.21, PDF pages 384-386, Table 4.2-77,
        Figure 4.2-119; Core Vol 6, Part B, 4.4.3.4 and 6.6.1.
    """
    identity = PEER_ADDRESS if random_identity else LOWER_PERIPHERAL_PUBLIC_ADDRESS
    await privacy_peer.add_peer(tester, identity=identity)
    await tester.command(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(
            identity.address_type,
            identity,
        )
    )
    await privacy_peer.scanning(tester, extended=True, policy=1)
    train = PeriodicTrain(tester.radio.now_us + 100_000)
    rpa = privacy_peer.private_address(privacy_peer.PEER_IRK)
    for attempt in range(20):
        await train.announce(tester, address=rpa, primary_channel=37 + attempt % 3)
        await tester.wait(1000)
        reports = privacy_peer.reports(tester)
        if reports:
            break
    assert reports
    assert all(
        report.address == identity
        and report.address_type == 2 + random_identity
        and report.periodic_advertising_interval == train.interval
        for report in reports
    )
    await tester.command(
        create_sync(
            advertiser_address_type=identity.address_type,
            advertiser_address=identity,
            sync_timeout=30,
            advertising_sid=3,
        )
    )
    advertiser = PeriodicAdvertiser(
        train, tester.radio.now_us + 1100, 20_003, address=rpa
    )
    for _ in range(3):
        await advertiser.transmit(tester, b'private periodic')
    event = await tester.event(hci.HCI_LE_Periodic_Advertising_Sync_Established_Event)
    assert event.status == 0 and event.advertiser_address == identity
    assert (
        event.advertising_sid == 3
        and event.periodic_advertising_interval == train.interval
    )
    report = await tester.event(hci.HCI_LE_Periodic_Advertising_Report_Event)
    assert (
        report.sync_handle == event.sync_handle and report.data == b'private periodic'
    )
    await tester.command(
        hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(event.sync_handle)
    )
    await privacy_peer.scan_enable(tester, False, extended=True)


# Verify periodic synchronization with an advertiser using an RPA when the Host selects
# its public or random identity address.
PROCEDURES |= procedure_cases(
    privacy_periodic_reception,
    {
        'LL/DDI/SCN/BV-72-C': (
            'Extended Scanning, Periodic Advertising Reception – '
            'Public Identity Address',
            {'random_identity': False},
        ),
        'LL/DDI/SCN/BV-73-C': (
            'Extended Scanning, Periodic Advertising Reception – '
            'Random Identity Address',
            {'random_identity': True},
        ),
    },
)


async def pawr_scanner_assigned_subevents(tester):
    """Sync with a Periodic Advertiser and Listen for the Assigned Subevent.

    Initial Condition:
        Five subevents at 15 ms and RSD 1.25 ms; LE 1M scanning.

    Test Steps:
        1. For Table 4.2-83, establish the train with RSS 1.25 or 0.375 ms.
        2. Select the assigned subevent (1 or 3); transmit ten-octet data and
           verify the v2 report's event counter and subevent.
        3. Submit two response octets for the table's response slot and
           receive the PDU at RSD + slot * RSS on RspAA.
        4. In round 3 select subevent 4, require no report for 3, then stop
           transmitting and require a failed-reception report for 4.

    Expected Outcome:
        Only selected subevents report. Response data, CRC, channel and timing
        match the independent peer. A missed selected subevent reports 0xFF.

    Reference:
        LL.TS.p28, LL/DDI/SCN/BV-94-C, Table 4.2-83, Steps 1-16.
    """
    for spacing, selected, slot in ((10, 1, 0), (3, 3, 2), (3, 3, 1)):
        train, handle = await pawr_peer.scanner(tester, timing=(5, 12, 1, spacing))
        await tester.command(pawr_peer.SET_SUBEVENTS(handle, 0, [selected]))
        # Selection can take effect in the next periodic event.
        train.next_event()
        start, channel = await pawr_peer.transmit_indication(
            tester, train, selected, b'0123456789'
        )
        report = await tester.event(pawr_peer.REPORT)
        assert (report.subevent, report.periodic_event_counter, report.data) == (
            selected,
            train.counter,
            b'0123456789',
        )
        await tester.command(
            pawr_peer.SET_RESPONSE(
                handle, train.counter, selected, selected, slot, b'OK'
            )
        )
        target = start + 1250 + slot * spacing * 125
        packet = await tester.receive(
            channel,
            start_us=target - 2,
            duration_us=target - tester.radio.now_us + 100,
            access_address=train.rsp_aa,
            crc_init=train.crc_init,
        )
        assert packet is not None and packet.valid_crc
        assert abs(packet.start_us - target) <= 2
        assert decode_extended(packet.pdu)['data'] == b'OK'
        if selected == 3 and slot == 1:
            await tester.command(pawr_peer.SET_SUBEVENTS(handle, 0, [4]))
            train.next_event()
            await pawr_peer.transmit_indication(tester, train, 3, b'not selected')
            await pawr_peer.transmit_indication(tester, train, 4, b'selected')
            reports = _pawr_take_all(tester, pawr_peer.REPORT)
            assert reports and all(r.subevent == 4 for r in reports)
            await tester.wait(train.interval * 1250 + 1000)
            assert any(
                r.subevent == 4 and r.data_status == 255
                for r in _pawr_take_all(tester, pawr_peer.REPORT)
            )
        await tester.command(
            hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle)
        )
        await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


# Verify reception only in selected PAwR subevents, correctly timed responses in
# assigned slots, and failure reports for missed selected subevents.
PROCEDURES |= procedure_cases(
    pawr_scanner_assigned_subevents,
    {
        'LL/DDI/SCN/BV-94-C': (
            'Sync with a Periodic Advertiser and Listen for the Assigned Subevent.',
            {},
        ),
    },
)


async def pawr_scanner_failure(tester):
    """Transition to Standby State, no AUX_SYNC_SUBEVENT_IND.

    Initial Condition:
        LE 1M passive scanning; valid PAwR announcements with SI 15 ms.

    Test Steps:
        1. Announce SyncInfo and response timing, then start acquisition.
        2. Withhold every periodic indication for six advertising events.
        3. Observe Sync Established v2 and verify no periodic report.

    Expected Outcome:
        Synchronization fails with 0x3E after six expected events; no live
        synchronization handle or successful data report is published.

    Reference:
        LL.TS.p28, LL/DDI/SCN/BI-07-C, Steps 1-7; Core 4.4.5.1.2.
    """
    train, _ = await pawr_peer.scanner(tester, timing=(5, 12, 1, 10), acquire=False)
    await tester.wait(7 * train.interval * 1250)
    event = await tester.event(pawr_peer.ESTABLISHED)
    assert event.status == 0x3E and event.sync_handle == 0
    assert not _pawr_take_all(tester, pawr_peer.REPORT)


# Verify failed PAwR synchronization after six expected events without indications,
# with no live synchronization handle or successful data report.
PROCEDURES |= procedure_cases(
    pawr_scanner_failure,
    {
        'LL/DDI/SCN/BI-07-C': (
            'Transition to Standby State, no AUX_SYNC_SUBEVENT_IND.',
            {},
        ),
    },
)


async def pawr_invalid_transfer_timing(tester):
    """Periodic Advertising with Responses, Invalid Parameters.

    Initial Condition:
        An extended ACL connection to the Lower Tester on LE 1M; PAST enabled.

    Test Steps:
        1. Advertise ten 15-ms subevents with RSD 1.25 ms and RSS 1.25 ms,
           receive an extended report, enable transfer reception and wait
           ten seconds while maintaining the ACL and advertisements.
        2. Send each Table 4.2-84 WR_IND timing combination: delay beyond SI,
           SI below 7.5 ms, zero delay and zero response spacing.
        3. Follow source alternatives 8A/8B/8C: validate a successful event,
           accept an unsuccessful event, or check LL_UNKNOWN_RSP identifies
           WR_IND. Terminate a successful synchronization before the next row.
        4. Send valid timing and require synchronization and a successful event.

    Expected Outcome:
        Each invalid timing row takes one of the three source outcomes.
        A subsequent valid transfer establishes synchronization with the
        original ten-subevent timing.

    Reference:
        LL.TS.p28, LL/DDI/SCN/BI-08-C, Table 4.2-84, Steps 1-10.
    """
    link = await _link(tester, central=True, interval=240, pawr=True)
    await exchange_features(link, PEER_FEATURES)
    train = lower_train(link, interval=480, pawr=True)
    train.pawr = (10, 12, 1, 10)
    session = TransferSession(tester, link, train)
    session.data = lambda counter: bytes(range(1, 101))
    session.announce = dict(address=LOWER_PERIPHERAL_PUBLIC_ADDRESS, sid=5)
    await session.command(
        scan_parameters(
            extended=True,
            scan_types=[0],
            scan_intervals=[16],
            scan_windows=[16],
        )
    )
    await session.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0))
    announced = None
    for _ in range(64):
        session.announce['primary_channel'] = 37 + link.counter % 3
        await session.step()
        announced = tester.take(
            lambda event: isinstance(
                event, hci.HCI_LE_Extended_Advertising_Report_Event
            )
        )
        if announced is not None:
            break
    assert announced is not None and any(
        r.periodic_advertising_interval == train.interval for r in announced.reports
    )
    await _transfer_mode(session, 2)
    await session.run_for(10_000_000)
    unknown = []

    def receive(packet):
        if packet.pdu[0] & 3 == 3 and packet.pdu[2:4] == bytes([7, 0x2A]):
            unknown.append(packet)

    link.on_receive = receive

    def next_indication():
        session.catch_up()
        periods = max(0, (link.anchor - train.anchor) // (train.interval * 1250) + 1)
        return session.indication(
            address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
            counter=train.counter + periods,
        )

    for si, delay, spacing in ((6, 13, 5), (5, 1, 5), (6, 0, 5), (6, 1, 0)):
        unknown.clear()
        indication = SyncIndication.decode(next_indication())
        await session.send_control(
            replace(indication, pawr=(10, si, delay, spacing)).encode()
        )
        outcome = None
        for _ in range(32):
            outcome = tester.take(
                lambda event: isinstance(
                    event,
                    hci.HCI_LE_Periodic_Advertising_Sync_Transfer_Received_V2_Event,
                )
            )
            if outcome is not None or unknown:
                break
            await session.step()
        assert outcome is not None or unknown, (
            'none of source alternatives 8A/8B/8C occurred'
        )
        if outcome is not None and outcome.status == 0:
            assert (
                outcome.subevent_interval,
                outcome.response_slot_delay,
                outcome.response_slot_spacing,
            ) == (si, delay, spacing)
            await session.command(
                hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(
                    outcome.sync_handle
                )
            )
    await session.send_control(next_indication())
    event = await _received(session, sid=5, address=LOWER_PERIPHERAL_PUBLIC_ADDRESS)
    assert (
        event.num_subevents,
        event.subevent_interval,
        event.response_slot_delay,
        event.response_slot_spacing,
    ) == (10, 12, 1, 10)
    await session.command(
        hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(event.sync_handle)
    )
    session.mode, session.announce = 'off', None
    await session.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))
    await _disconnect(tester, link)


# Verify rejection of invalid WR_IND timing without allocating a synchronization
# handle, followed by successful synchronization from a valid transfer.
PROCEDURES |= procedure_cases(
    pawr_invalid_transfer_timing,
    {
        'LL/DDI/SCN/BI-08-C': (
            'Periodic Advertising with Responses, Invalid Parameters.',
            {},
        ),
    },
)


async def pawr_scanner_same_identity(tester):
    """PAwR Trains, Do Not Synchronize with Same Address, Address Type and SID.

    Initial Condition:
        The IUT has synchronized to the Lower Tester's PAwR train.

    Test Steps:
        1. Continue periodic indications for ten intervals.
        2. Request a second sync for the same address, address type and SID.
        3. Observe the asynchronous command response and continue the train.

    Expected Outcome:
        The second request returns Command Disallowed, without replacing the
        original handle or emitting another successful establishment event.

    Reference:
        LL.TS.p28, 4.2.4.25, LL/DDI/SCN/BV-97-C, alternative 9A.
    """
    train, handle = await pawr_peer.scanner(
        tester, timing=(2, 12, 1, 10), observe_us=10_000_000
    )
    for _ in range(10):
        train.next_event()
        await pawr_peer.transmit_indication(tester, train)
    await tester.command(
        create_sync(
            advertising_sid=3,
            advertiser_address_type=LOWER_PERIPHERAL_PUBLIC_ADDRESS.address_type,
            advertiser_address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
        ),
        0x0B,
    )
    assert not _pawr_take_all(tester, pawr_peer.ESTABLISHED)
    train.next_event()
    await pawr_peer.transmit_indication(tester, train, data=b'original sync')
    report = await tester.expect(
        lambda event: isinstance(event, pawr_peer.REPORT)
        and event.sync_handle == handle
        and event.data == b'original sync'
    )
    assert report.data_status == 0 and report.periodic_event_counter == train.counter
    await tester.command(hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle))


# Verify rejection of a second PAwR synchronization with the same address, address
# type and SID, preserving the original synchronization handle.
PROCEDURES |= procedure_cases(
    pawr_scanner_same_identity,
    {
        'LL/DDI/SCN/BV-97-C': (
            'PAwR Trains, Do Not Synchronize with Same Address, Address Type and SID.',
            {},
        ),
    },
)


async def pawr_scanner_future_response(tester):
    """Sync with a Periodic Advertiser, Send Response Data in Future Subevent.

    Initial Condition:
        Five subevents, SI 318.75 ms, RSD 317.5 ms and RSS 250 us on LE 1M.

    Test Steps:
        1. Acquire the train and select subevents 1, 2 and 4.
        2. Receive data in subevent 2; submit a response in current subevent 4
           or next-event subevent 1 as specified in Table 4.2-86.
        3. Receive the target indication and verify a response in slot 3.
        4. For round 3, wait until subevent 2 in the next periodic event has
           passed before requesting a response to the old event; require
           Too Late and observe the target slot to prove no transmission.

    Expected Outcome:
        The first two responses use the requested slot and exact payload
        within one PAI of the request. The late request produces no response.

    Source Conflict:
        Step 12A.2 requires AdvA, but Core Vol 6, Part B, 2.3.1.10 makes that
        field optional. Check its value when present; timing, CRC, RspAA and
        exact response data are required in either format. The Controller
        omits AdvA, so this does not satisfy that explicit TS address check.
        Retain source_conflict and a PARTIAL protocol verdict; no SIG erratum
        is claimed.

    Reference:
        LL.TS.p28, LL/DDI/SCN/BV-98-C, Table 4.2-86, Steps 1-14.
    """
    generator = random.Random(0)
    train, handle = await pawr_peer.scanner(
        tester, timing=(5, 255, 254, 2), interval=1275
    )
    await tester.command(pawr_peer.SET_SUBEVENTS(handle, 0, [1, 2, 4]))
    for target in (4, 1, 2):
        train.next_event()
        await pawr_peer.transmit_indication(tester, train, 1, b'')
        data = bytes(generator.randint(1, 254) for _ in range(10))
        await pawr_peer.transmit_indication(tester, train, 2, data)
        report = await tester.expect(
            lambda event: isinstance(event, pawr_peer.REPORT)
            and event.subevent == 2
            and event.data == data
        )
        assert (
            report.data_status == 0 and report.periodic_event_counter == train.counter
        )
        counter, request_start = train.counter, train.anchor + 2 * 318_750
        if target == 2:
            # Table 4.2-86 round 3 issues the command in the next event,
            # after subevent 2 (and the one-interval response deadline).
            await pawr_peer.transmit_indication(tester, train, 4, b'')
            train.next_event()
            await pawr_peer.transmit_indication(tester, train, 1, b'')
            await pawr_peer.transmit_indication(tester, train, 2, b'')
            await tester.command(
                pawr_peer.SET_RESPONSE(handle, counter, 2, 2, 3, b'x'), 0x46
            )
            due = train.anchor + 2 * 318_750 + 317_500 + 3 * 250
            channel = csa2_channel(
                train.counter ^ 2, train.access_address, train.channel_map
            )
            packet = await tester.receive(
                channel,
                duration_us=due - tester.radio.now_us + 250,
                access_address=train.rsp_aa,
                crc_init=train.crc_init,
            )
            assert packet is None, 'Too Late command still produced a response'
            await pawr_peer.transmit_indication(tester, train, 4, b'')
            continue
        payload = bytes([generator.randint(1, 254)])
        await tester.command(
            pawr_peer.SET_RESPONSE(handle, counter, 2, target, 3, payload)
        )
        if target == 1:
            await pawr_peer.transmit_indication(tester, train, 4, b'')
            train.next_event()
        start, channel = await pawr_peer.transmit_indication(tester, train, target, b'')
        due = start + 317_500 + 3 * 250
        packet = await tester.receive(
            channel,
            start_us=due - 2,
            duration_us=due - tester.radio.now_us + 100,
            access_address=train.rsp_aa,
            crc_init=train.crc_init,
        )
        assert packet is not None and packet.valid_crc
        assert abs(packet.start_us - due) <= 2
        assert packet.start_us - request_start < train.interval * 1250
        fields = decode_extended(packet.pdu)
        assert fields['data'] == payload
        if 'address' in fields:
            assert fields['address'] == bytes(DUT_ADDRESS)
    await tester.command(hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle))
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


# Verify responses queued for a future subevent use the requested slot and exact
# payload, while a late response request produces no transmission.
PROCEDURES |= procedure_cases(
    pawr_scanner_future_response,
    {
        'LL/DDI/SCN/BV-98-C': pytest.param(
            'Sync with a Periodic Advertiser, Send Response Data in Future Subevent.',
            {},
            marks=pytest.mark.source_conflict,
        ),
    },
)


async def pawr_scanner_connection(tester):
    """Synchronized Device Reconnects with an Advertiser using PAwR.

    Initial Condition:
        Synchronize to a ten-subevent LE 1M train with SI 15 ms.

    Test Steps:
        1. Select subevent 5 and receive 100 octets there.
        2. Send AUX_CONNECT_REQ in unselected subevent 6; require silence.
        3. In the next selected subevent 5, send a valid AUX_CONNECT_REQ,
           receive AUX_CONNECT_RSP at T_IFS and serve the first ACL event.
        4. Continue periodic reception and check its counter, handle and data.
        5. Complete LL_FEATURE_REQ/RSP on the ACL, then disconnect and
           disable scanning, following source Steps 18-23.

    Expected Outcome:
        Only a selected subevent accepts the request. AUX_CONNECT_RSP uses
        the train AA/CRC; completion v2 has the original Sync_Handle and
        Advertising_Handle 0xFF. Synchronization continues beside the ACL.

    Reference:
        LL.TS.p28, LL/DDI/SCN/BV-95-C, Steps 1-23; Core 4.4.2.12.2.
    """
    train, handle = await pawr_peer.scanner(
        tester, timing=(10, 12, 1, 10), interval=160, observe_us=10_000_000
    )
    await tester.command(pawr_peer.SET_SUBEVENTS(handle, 0, [5]))
    train.next_event()
    data = random.Random(0).randbytes(100)
    await pawr_peer.transmit_indication(tester, train, 5, data)
    report = await tester.event(pawr_peer.REPORT)
    assert report.subevent == 5 and report.data == data
    parameters = LinkParameters(interval=80, timeout=400)
    request = request_pdu(
        5,
        advertiser=DUT_ADDRESS,
        scanner=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
        data=parameters.encode(),
    )
    for sub, answer in ((6, False), (5, True)):
        if sub == 5:
            train.next_event()
        start = train.anchor + sub * 15_000
        channel = csa2_channel(
            train.counter ^ sub, train.access_address, train.channel_map
        )
        end = await tester.transmit(
            channel, request, start_us=start, **train.parameters
        )
        packet = await tester.receive(
            channel, start_us=end + 148, duration_us=150 + 100, **train.parameters
        )
        if not answer:
            assert packet is None
            continue
        assert packet is not None and packet.valid_crc
        assert packet.start_us == end + 150
        fields = decode_extended(packet.pdu)
        assert fields['address'] == bytes(DUT_ADDRESS) and fields['target'] == bytes(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS
        )
        link = PeerLink(tester, parameters, end + 1250, central=False, csa2=True)
        await link.exchange()
        event = await tester.event(PAWR_CONNECTION_COMPLETE)
        assert (
            event.status == 0
            and event.sync_handle == handle
            and event.advertising_handle == 255
        )
        link.handle = event.connection_handle
        for _ in range(2):
            train.next_event()
            target = train.anchor + 5 * 15_000
            while link.anchor < target:
                await link.exchange()
            await pawr_peer.transmit_indication(tester, train, 5, b'continued')
            await link.exchange()
            report = await tester.expect(
                lambda event: isinstance(event, pawr_peer.REPORT)
                and event.sync_handle == handle
                and event.data == b'continued'
            )
            assert (
                report.subevent == 5 and report.periodic_event_counter == train.counter
            )
            assert report.data_status == 0
        await exchange_features(link, PEER_FEATURES)
        await link.disconnect(local=False)
    await tester.command(hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle))
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


# Verify that only a selected PAwR subevent accepts AUX_CONNECT_REQ, that connection
# completion identifies the original sync, and that synchronization continues.
PROCEDURES |= procedure_cases(
    pawr_scanner_connection,
    {
        'LL/DDI/SCN/BV-95-C': (
            'Synchronized Device Reconnects with an Advertiser using PAwR.',
            {},
        ),
    },
)


async def pawr_scanner_existing_connection(tester, *, central):
    """Reject Connection Request When Connected to a Peer, PAwR synchronizer.

    Initial Condition:
        The IUT has an ACL link in the selected role and an established PAwR
        sync acquired from advertisements by the same Lower Tester.

    Test Case Configuration:
        LL/DDI/SCN/BI-09-C: Central IUT.
        LL/DDI/SCN/BI-10-C: Peripheral IUT.

    Test Steps:
        1. Scan the independent ten-subevent train for ten seconds. Acquire
           with reporting initially disabled, select subevent 0, enable
           reporting and check its 100-octet data.
        2. Replace an indication in that selected subevent with AUX_CONNECT_REQ
           whose InitA names the already connected Lower Tester.
        3. Listen at T_IFS, then continue serving the original ACL link.

    Expected Outcome:
        No AUX_CONNECT_RSP or second Connection Complete is emitted. The
        original ACL remains alive in both Central and Peripheral variants.

    Reference:
        LL.TS.p28, 4.2.4.26, LL/DDI/SCN/BI-09-C and BI-10-C,
        Table 4.2-87; Core 4.4.2.12.2.
    """
    link = await _link(tester, central=central, interval=240, pawr=True)
    await tester.masks(le=(1 << 64) - 1)
    peer = LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
    train = lower_train(link, interval=480, pawr=True)
    train.pawr = (10, 12, 1, 10)
    session = TransferSession(tester, link, train)
    session.announce = dict(address=peer, sid=3)

    async def scan_step():
        # A 300-ms ACL interval is a multiple of the 10-ms scan window.
        # Rotate primary channels to avoid testing only one fixed scan phase.
        session.announce['primary_channel'] = 37 + link.counter % 3
        await session.step()

    async def scanning_until(predicate, *, events=64):
        for _ in range(events):
            if (result := tester.take(predicate)) is not None:
                return result
            await scan_step()
        return tester.take(predicate)

    await session.command(
        scan_parameters(
            extended=True,
            scan_types=[0],
            scan_intervals=[16],
            scan_windows=[16],
        )
    )
    await session.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0))
    announcement = await scanning_until(
        lambda event: isinstance(event, hci.HCI_LE_Extended_Advertising_Report_Event)
        and any(
            r.address == peer and r.periodic_advertising_interval == train.interval
            for r in event.reports
        ),
        events=64,
    )
    assert announcement is not None
    deadline = tester.radio.now_us + 10_000_000
    while link.anchor < deadline:
        await scan_step()
    await session.command(
        create_sync(
            options=2,
            advertising_sid=3,
            advertiser_address_type=peer.address_type,
            advertiser_address=peer,
            sync_timeout=300,
        )
    )
    event = await scanning_until(
        lambda event: isinstance(event, pawr_peer.ESTABLISHED), events=64
    )
    assert event is not None and event.status == 0 and event.num_subevents == 10
    assert event.advertiser_address == peer and not session.reports()
    await session.command(pawr_peer.SET_SUBEVENTS(event.sync_handle, 0, [0]))
    await session.command(
        hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command(event.sync_handle, 1)
    )
    data = bytes(random.Random(0).sample(range(1, 255), 100))
    session.data = lambda counter: data
    report = await scanning_until(
        lambda event: isinstance(event, pawr_peer.REPORT) and event.data == data,
        events=64,
    )
    assert (
        report is not None
        and report.subevent == 0
        and report.sync_handle == event.sync_handle
    )
    assert report.data_status == 0
    session.mode, session.announce = 'off', None
    while train.anchor <= tester.radio.now_us + 2000:
        train.next_event()
    while link.anchor < train.anchor:
        await session.step()
    channel = train.channel
    request = request_pdu(
        5, advertiser=DUT_ADDRESS, scanner=peer, data=LinkParameters().encode()
    )
    end = await tester.transmit(
        channel, request, start_us=train.anchor, **train.parameters
    )
    packet = await tester.receive(
        channel, start_us=end + 148, duration_us=250, **train.parameters
    )
    assert packet is None
    await session.run(5)
    complete = (
        hci.HCI_LE_Connection_Complete_Event,
        hci.HCI_LE_Enhanced_Connection_Complete_Event,
        PAWR_CONNECTION_COMPLETE,
    )
    assert not _pawr_take_all(tester, complete)
    await session.command(
        hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(event.sync_handle)
    )
    await session.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))
    await _disconnect(tester, link)


# Verify that a PAwR synchronizer ignores connection requests from an already
# connected peer, preserving the original ACL connection in both roles.
PROCEDURES |= procedure_cases(
    pawr_scanner_existing_connection,
    {
        'LL/DDI/SCN/BI-09-C': (
            'Reject Connection Request When Connected to a Peer, PAwR synchronizer.',
            {'central': True},
        ),
        'LL/DDI/SCN/BI-10-C': (
            'Reject Connection Request When Connected to a Peer, PAwR synchronizer.',
            {'central': False},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_periodic_sync(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
