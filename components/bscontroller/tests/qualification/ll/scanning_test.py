# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.2: Active and passive scanning."""

import csv
import random
from itertools import pairwise
from types import SimpleNamespace

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_ADV_DELAY_MAX_US,
    LL_ADV_PRIMARY_CHANNELS,
    LL_IFS_US,
    LL_MAFS_US,
    PDU_ADV_ADI_DID_BITS,
    PDU_ADV_ADI_DID_MASK,
    PDU_ADV_AUX_OFFSET_PHY_SIZE,
    PDU_ADV_EXT_FLAGS_RESERVED_MASK,
    PDU_ADV_EXT_HEADER_LENGTH_BITS,
    PDU_ADV_EXT_HEADER_OFFSET,
    PDU_ADV_EXT_PAYLOAD_SIZE_MAX,
    PDU_ADV_LEGACY_DATA_SIZE_MAX,
    PDU_ADV_OFFSET_BITS,
    PDU_ADV_OFFSET_UNIT_30_US,
    PDU_ADV_OFFSET_UNIT_300_US,
    PDU_ADV_OFFSET_VALUE_MASK,
    PDU_ADV_RESERVED_MASK,
    PDU_ADV_RXADD_MASK,
    PDU_ADV_TXADD_MASK,
    PDU_HEADER_SIZE,
    AdvertisingMode,
    AdvertisingPduType,
)
from bscontroller.radio.base import PHY_CODED_S2_FACTOR, PHY_CODED_S8_FACTOR, Phy
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.discovery import (
    PHY_PAIRS,
    RESPONSE_IFS_TOLERANCE_US,
    SCAN_MAX_DATA,
    LegacyEventType,
    LegacyReport,
    Report,
    _advertising_public_peer_addresses,
    _check_scan_reports,
    _enable_legacy_scan,
    _enable_scan,
    _scan_compatibility_Flags,
    _scan_phase,
    _scan_reports,
)
from tests.qualification.link import (
    ADVERTISING_SID,
    AUXILIARY_CHANNEL,
    AUXILIARY_OFFSET_US,
    REPORT_DELIVERY_US,
    SCAN_ADV_INTERVAL_US,
    SCAN_INTERVAL_US,
    SCAN_RESPONSE_IFS_TOLERANCE_US,
    SCANNING_ADVERTISING_INTERVAL_US,
    SCANNING_SCAN_INTERVAL,
    _corrupt_advertisements,
    _encoding_take_reports,
    _scanning_check_reports,
    _scanning_Flags,
    _ScanRound,
    _start_scanning,
)
from tests.qualification.periodic import PeriodicTrain
from tests.qualification.tester import (
    DUT_ADDRESS,
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
    PEER_ADDRESS,
    adv_pdu,
    aux_pointer,
    decode_extended,
    ext_pdu,
    request_pdu,
    scan_parameters,
)

PROCEDURES = ProcedureMap()

ReportType = hci.HCI_LE_Advertising_Report_Event.EventType


async def _invalid_scanning_crc(tester, *, active=False):
    """Reject bad advertising CRCs and, when scanning actively, scan-response CRCs."""
    if not active:
        await _start_scanning(tester, False)
    for channel in LL_ADV_PRIMARY_CHANNELS:
        if active:
            await _start_scanning(tester, True)
        requests, reports = await _corrupt_advertisements(
            tester, channel, active=active
        )
        assert not requests and (not reports)
        if active:
            peer = LOWER_PERIPHERAL_PUBLIC_ADDRESS
            data = b'valid advertising'
            deadline = tester.radio.now_us + 20 * SCAN_INTERVAL_US
            generator = random.Random(0)
            start = tester.radio.now_us + 100
            while tester.radio.now_us < deadline:
                end = await tester.transmit(
                    channel, adv_pdu(address=peer, data=data), start_us=start
                )
                request = await tester.receive(channel, duration_us=1000)
                if request is not None:
                    assert request.valid_crc
                    assert request.pdu == request_pdu(
                        AdvertisingPduType.SCAN_REQ,
                        advertiser=peer,
                        scanner=DUT_ADDRESS,
                    )
                    assert (
                        abs(request.start_us - end - LL_IFS_US)
                        <= SCAN_RESPONSE_IFS_TOLERANCE_US
                    )
                    await tester.transmit(
                        channel,
                        adv_pdu(
                            AdvertisingPduType.SCAN_RSP,
                            address=peer,
                            data=b'bad response',
                        ),
                        start_us=request.end_us + LL_IFS_US,
                        bad_crc=True,
                    )
                    await tester.wait(1000)
                    break
                start = (
                    start
                    + SCAN_ADV_INTERVAL_US
                    + generator.randrange(LL_ADV_DELAY_MAX_US + 1)
                )
            else:
                raise AssertionError(
                    'no active scan request within twenty scan intervals'
                )
            reports = _encoding_take_reports(tester)
            assert reports and all(
                report.event_type == ReportType.ADV_IND
                and report.address == peer
                and report.address_type == hci.AddressType.PUBLIC_DEVICE
                and (report.data == data)
                for report in reports
            )
        if active:
            await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(0, 0))
        assert not _encoding_take_reports(tester)
    if not active:
        await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(0, 0))
        assert not _encoding_take_reports(tester)


# LL/DDI/SCN/BV-74-C Steps 6-9: an initial pair and ten repetitions.
EXTENDED_PAIRS = 11

# Legacy-scanner stimulus: 600-us AuxPtr, distinct from extended-scan trains.
LEGACY_AUXILIARY_OFFSET_US = 600


def _rfu_fields(wire, *, header=True, extended=True, context=None):
    """Set only ignored header bits, preserving meaningful address-type fields."""
    fields = decode_extended(wire)
    if context is not None:
        ignored = {'cte': 2}
        if context in ('primary', 'chain', 'response', 'scannable', 'connectable'):
            ignored['sync'] = PeriodicTrain(30000).sync_info(0)
        if context in ('primary', 'chain', 'response'):
            ignored['acad'] = b'\x02\xffZ'
        if context == 'chain':
            ignored['address'] = hci.Address(
                '12:34:56:78:9A:BC', hci.AddressType.PUBLIC_DEVICE
            )
        if context in ('chain', 'response'):
            ignored['target'] = hci.Address(
                '12:34:56:78:9A:BC', hci.AddressType.PUBLIC_DEVICE
            )
        if context in ('scannable', 'connectable'):
            ignored['aux'] = aux_pointer(1, AUXILIARY_OFFSET_US)
        if context == 'scannable':
            ignored['data'] = b'ignored'
        values = {
            'address': (
                hci.Address(fields['address'], hci.AddressType.PUBLIC_DEVICE)
                if 'address' in fields
                else None
            ),
            'target': (
                hci.Address(fields['target'], hci.AddressType.PUBLIC_DEVICE)
                if 'target' in fields
                else None
            ),
            'adi': int.from_bytes(fields['adi'], 'little') if 'adi' in fields else None,
            'aux': fields.get('aux'),
            'sync': fields.get('sync'),
            'cte': fields['cte'][0] if 'cte' in fields else None,
            'tx_power': (
                int.from_bytes(fields['power'], 'little', signed=True)
                if 'power' in fields
                else None
            ),
            'acad': fields.get('acad', b''),
            'data': fields['data'],
            'mode': wire[PDU_HEADER_SIZE] >> PDU_ADV_EXT_HEADER_LENGTH_BITS,
        }
        wire = ext_pdu(**values | ignored)
        fields = decode_extended(wire)
    value = bytearray(wire)
    if header:
        value[0] |= PDU_ADV_RESERVED_MASK
    if extended:
        value[PDU_ADV_EXT_HEADER_OFFSET] |= PDU_ADV_EXT_FLAGS_RESERVED_MASK
    if 'address' not in fields:
        value[0] |= PDU_ADV_TXADD_MASK
    if 'target' not in fields:
        value[0] |= PDU_ADV_RXADD_MASK
    return bytes(value)


# Separate auxiliary channels by12MHz so this discovery test is not an adjacent-channel
# RF test.
AUXILIARY_CHANNEL_STRIDE = 6
ACTIVE_CHAIN_COUNT = 3

# Maximum representable AuxPtr separation for the active test's widely spaced PDUs.
ACTIVE_CHAIN_OFFSET_US = PDU_ADV_OFFSET_VALUE_MASK * PDU_ADV_OFFSET_UNIT_300_US

# Section4.2.4.7: scan response plus three chain PDUs, each229 octets.
ACTIVE_DATA_LENGTH = 229

# Source T_IFS checks use the raw peer's integer-us interface.
IFS_TOLERANCE_US = 2

# Section4.2.4.6: no report may remain incomplete for two seconds; ten truncations fail.
INCOMPLETE_REPORT_TIMEOUT_US = 2000000

# Bounded channel-acquisition opportunities; these are not truncation retries.
MAX_ADVERTISING_ATTEMPTS = 60
MAX_TRUNCATIONS = 10

# Section4.2.4.6: four100-octet chain PDUs, bounded by Scan_Max_Data/4.
PASSIVE_CHAIN_COUNT = 4

# Selected first-passive-chain offset; longer Coded PDUs increase it as required.
PASSIVE_CHAIN_OFFSET_US = 3000
PASSIVE_DATA_LENGTH = 100

# Selected distinct signed-dBm values for Power1..4, not measured RF signal levels.
POWER_LEVELS = (-20, -10, 0, 10)


def _chain_packets(chunks, powers, *, active, phy, adi):
    """Return (offset, PDU, received-data-count) without DUT wire encoders."""
    values = list(zip(chunks, powers))
    if not active:
        values.insert(0, (b'', powers[0]))
    packets = []
    offset = 0
    for index, (data, power) in enumerate(values):
        first, more = (index == 0, index < len(values) - 1)
        fields = {
            'address': LOWER_PERIPHERAL_PUBLIC_ADDRESS if first else None,
            'adi': adi,
            'tx_power': power,
            'kind': (
                AdvertisingPduType.AUX_SCAN_RSP
                if first and active
                else (
                    AdvertisingPduType.AUX_ADV_IND
                    if first
                    else AdvertisingPduType.AUX_CHAIN_IND
                )
            ),
            'data': data,
        }
        distance = ACTIVE_CHAIN_OFFSET_US if active else PASSIVE_CHAIN_OFFSET_US
        if more:
            fields['aux'] = aux_pointer(AUXILIARY_CHANNEL, distance, phy)
            if not active:
                minimum = phy.airtime_us(len(ext_pdu(**fields))) + LL_MAFS_US
                distance = max(
                    distance,
                    (minimum + PDU_ADV_OFFSET_UNIT_30_US - 1)
                    // PDU_ADV_OFFSET_UNIT_30_US
                    * PDU_ADV_OFFSET_UNIT_30_US,
                )
                fields['aux'] = aux_pointer(AUXILIARY_CHANNEL, distance, phy)
        packets.append((offset, ext_pdu(**fields), index + int(active)))
        offset += distance
    return packets


def _check_power_reports(records, chunks, powers, *, active, phy):
    """Discard truncated attempts; score complete results at their RF boundaries."""
    assert records, 'no extended advertising reports'
    if not active:
        for previous, current in pairwise(records):
            if (
                previous[2].event_type >> hci.HCI_LE_EXT_ADV_REPORT_DATA_STATUS_SHIFT
                == Report.DATA_INCOMPLETE_MORE_TO_COME
            ):
                assert current[0] - previous[0] <= INCOMPLETE_REPORT_TIMEOUT_US
        # Section 4.2.4.6 Step 5 discards a truncated attempt before its verdict.
        if (
            records[-1][2].event_type >> hci.HCI_LE_EXT_ADV_REPORT_DATA_STATUS_SHIFT
            == Report.DATA_INCOMPLETE_TRUNCATED_NO_MORE_TO_COME
        ):
            return False
    expected = b''.join(chunks)
    offset = 0
    boundaries = [sum(map(len, chunks[: index + 1])) for index in range(len(chunks))]
    first_seen = set()
    last_power_seen = False
    for index, (_, received, report) in enumerate(records):
        status = report.event_type >> hci.HCI_LE_EXT_ADV_REPORT_DATA_STATUS_SHIFT
        flags = (
            _scan_power_Flags.SCANNABLE_ADVERTISING | _scan_power_Flags.SCAN_RESPONSE
            if active
            else 0
        )
        assert report.event_type & ~hci.HCI_LE_EXT_ADV_REPORT_DATA_STATUS_MASK == flags
        assert report.address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
        assert report.address_type == hci.AddressType.PUBLIC_DEVICE
        assert report.primary_phy == report.secondary_phy == phy
        assert report.advertising_sid == ADVERTISING_SID
        assert report.periodic_advertising_interval == 0
        end = offset + len(report.data)
        assert end <= sum(map(len, chunks[:received])), (
            'report contains data not yet received'
        )
        assert report.data == expected[offset:end], (
            'advertising data lost, reordered or duplicated'
        )
        if active:
            assert received and report.tx_power == powers[received - 1], (
                'stale scan-response TxPower'
            )
        else:
            assert report.tx_power in (
                powers[0],
                powers[-1],
                Report.TX_POWER_INFORMATION_NOT_AVAILABLE,
            )
            assert received == len(chunks) or report.tx_power != powers[-1], (
                'Power2 reported before the last PDU'
            )
            if end > boundaries[-2] or last_power_seen:
                assert report.tx_power != powers[0], (
                    'Power1 reported after the last PDU data or Power2'
                )
            last_power_seen |= report.tx_power == powers[-1]
            for chunk_index, boundary in enumerate(boundaries):
                start = boundaries[chunk_index - 1] if chunk_index else 0
                if (
                    offset < boundary
                    and end > start
                    and (chunk_index not in first_seen)
                ):
                    assert (
                        report.tx_power != Report.TX_POWER_INFORMATION_NOT_AVAILABLE
                    ), 'first report containing this PDU has unknown TxPower'
                    first_seen.add(chunk_index)
        if index < len(records) - 1:
            assert status == Report.DATA_INCOMPLETE_MORE_TO_COME
        else:
            assert status == Report.DATA_COMPLETE, (
                'no final report within the observation timeout'
            )
        offset = end
    assert len(records) > 1, 'the source requires multiple HCI report fragments'
    assert offset == len(expected), 'final complete report omitted advertising data'
    return True


async def _reporting_power(tester, *, active=False, phy=Phy.LE_1M):
    """Verify power changes across every packet and serialized HCI data fragment."""
    length = (
        ACTIVE_DATA_LENGTH
        if active
        else min(PASSIVE_DATA_LENGTH, SCAN_MAX_DATA // PASSIVE_CHAIN_COUNT)
    )
    count = ACTIVE_CHAIN_COUNT + 1 if active else PASSIVE_CHAIN_COUNT
    if active and count * length > SCAN_MAX_DATA:
        pytest.skip(
            'INCONCLUSIVE: section4.2.4.7 requires916 octets, exceeding '
            'Scan_Max_Data IXIT'
        )
    generator = random.Random(0)
    chunks = [generator.randbytes(length) for _ in range(count)]
    powers = (
        POWER_LEVELS
        if active
        else (POWER_LEVELS[0],) * (PASSIVE_CHAIN_COUNT - 1) + (POWER_LEVELS[-1],)
    )
    await tester.command(
        scan_parameters(
            extended=True,
            scanning_phys=1 << phy - 1,
            scan_types=[int(active)],
            scan_intervals=[SCANNING_SCAN_INTERVAL],
            scan_windows=[SCANNING_SCAN_INTERVAL],
        )
    )
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0))
    truncated = 0
    for attempt in range(MAX_ADVERTISING_ATTEMPTS):
        adi = ADVERTISING_SID << PDU_ADV_ADI_DID_BITS | attempt
        records = []
        start = tester.radio.now_us + REPORT_DELIVERY_US
        mode = (
            AdvertisingMode.SCANNABLE
            if active
            else AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
        )
        await tester.transmit(
            37,
            ext_pdu(
                adi=adi,
                mode=mode,
                aux=aux_pointer(AUXILIARY_CHANNEL, AUXILIARY_OFFSET_US, phy),
            ),
            start_us=start,
            phy=phy,
        )
        first_start = start + AUXILIARY_OFFSET_US
        if active:
            end = await tester.transmit(
                AUXILIARY_CHANNEL,
                ext_pdu(address=LOWER_PERIPHERAL_PUBLIC_ADDRESS, adi=adi, mode=mode),
                start_us=first_start,
                phy=phy,
            )
            request = await tester.receive(
                AUXILIARY_CHANNEL,
                phy=phy,
                duration_us=LL_IFS_US + phy.address_us + IFS_TOLERANCE_US,
            )
            _scan_power_take_reports(tester, records, 0)
            if request is None:
                assert not records, (
                    'active scanner reported advertising without requesting its'
                    ' scan response'
                )
                await tester.wait(
                    max(
                        0,
                        start
                        + SCANNING_ADVERTISING_INTERVAL_US
                        + generator.randrange(LL_ADV_DELAY_MAX_US + 1)
                        - tester.radio.now_us,
                    )
                )
                continue
            assert request.valid_crc
            assert request.pdu == request_pdu(
                AdvertisingPduType.SCAN_REQ,
                LOWER_PERIPHERAL_PUBLIC_ADDRESS,
                scanner=DUT_ADDRESS,
            )
            assert abs(request.start_us - end - LL_IFS_US) <= IFS_TOLERANCE_US
            assert len(records) == 1
            advertisement = records.pop()[2]
            assert advertisement.event_type == _scan_power_Flags.SCANNABLE_ADVERTISING
            assert advertisement.address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
            assert advertisement.address_type == hci.AddressType.PUBLIC_DEVICE
            assert advertisement.primary_phy == advertisement.secondary_phy == phy
            assert advertisement.advertising_sid == ADVERTISING_SID
            assert advertisement.periodic_advertising_interval == 0
            assert not advertisement.data
            first_start = request.end_us + LL_IFS_US
        for offset, wire, received in _chain_packets(
            chunks, powers, active=active, phy=phy, adi=adi
        ):
            await tester.transmit(
                AUXILIARY_CHANNEL, wire, start_us=first_start + offset, phy=phy
            )
            _scan_power_take_reports(tester, records, received)
        if not active:
            assert (
                await tester.receive(
                    AUXILIARY_CHANNEL, phy=phy, duration_us=REPORT_DELIVERY_US
                )
                is None
            ), 'passive scanner sent a request'
        else:
            await tester.wait(REPORT_DELIVERY_US)
        _scan_power_take_reports(tester, records, count)
        if records:
            if (
                records[-1][2].event_type >> hci.HCI_LE_EXT_ADV_REPORT_DATA_STATUS_SHIFT
                == Report.DATA_INCOMPLETE_MORE_TO_COME
            ):
                await tester.wait(INCOMPLETE_REPORT_TIMEOUT_US)
                _scan_power_take_reports(tester, records, count)
            if _check_power_reports(records, chunks, powers, active=active, phy=phy):
                break
            truncated += 1
            assert truncated < MAX_TRUNCATIONS, 'ten truncated advertising events'
        await tester.wait(
            max(
                0,
                start
                + SCANNING_ADVERTISING_INTERVAL_US
                + generator.randrange(LL_ADV_DELAY_MAX_US + 1)
                - tester.radio.now_us,
            )
        )
    else:
        raise AssertionError('no complete power-reporting advertising event')
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


def _scan_power_take_reports(tester, records, received):
    for event in list(tester.events):
        if isinstance(event, Report):
            records.extend(
                (tester.radio.now_us, received, report) for report in event.reports
            )
            tester.events.remove(event)


# Table4.2-52: minimum twenty advertising intervals and ten reported advertisements.
ADVERTISING_INTERVALS = 20

# Table4.2-52 round18 assigns242 octets to AUX_ADV_IND and1403 to its chain.
FIRST_AUXILIARY_DATA = 242

# Table4.2-78: reserved AUX PHY encoding, followed by an actual LE1M PDU.
INVALID_AUX_PHY = 4

# Section4.2.4.22 repeats the invalid primary/auxiliary pair five times.
INVALID_PHY_ROUNDS = 5
REMAINING_AUXILIARY_DATA = 1403
REQUIRED_REPORTS = 10
SCAN_DURATION = 500


def _packets(
    round_, data, adi, primary_phy, secondary_phy, coding, *, channel=AUXILIARY_CHANNEL
):
    """Pack every nonfinal auxiliary payload to255 octets, without DUT encoders."""
    peer = LOWER_PERIPHERAL_PUBLIC_ADDRESS
    if round_.kind != AdvertisingPduType.ADV_EXT_IND:
        wire = (
            request_pdu(round_.kind, round_.target, scanner=peer)
            if round_.kind == AdvertisingPduType.ADV_DIRECT_IND
            else adv_pdu(round_.kind, address=peer, data=data)
        )
        return ([(37, primary_phy, 0, wire)], ())
    if not round_.auxiliary:
        return (
            [
                (
                    37,
                    primary_phy,
                    0,
                    # Core Vol 6, Part B, Table 2.4: primary-only events
                    # have no meaningful ADI. RFU injection is tested separately.
                    ext_pdu(address=peer, target=round_.target),
                )
            ],
            (),
        )
    pointer = aux_pointer(channel, AUXILIARY_OFFSET_US, secondary_phy)
    # Core Table 2.4 C.1: primary AdvA with AuxPtr is RFU on LE Coded.
    # Correct LL.TS.p28 Table 4.2-52 round 17 by moving AdvA to AUX_ADV_IND.
    primary_address = round_.primary_address and primary_phy == Phy.LE_1M
    result = [
        (
            37,
            primary_phy,
            0,
            ext_pdu(address=peer if primary_address else None, adi=adi, aux=pointer),
        )
    ]
    offset, consumed = (AUXILIARY_OFFSET_US, 0)
    boundaries = []
    first = True
    while True:
        fields = {
            'address': peer if first and not primary_address else None,
            'target': round_.target if first else None,
            'adi': adi,
            'kind': (
                AdvertisingPduType.AUX_ADV_IND
                if first
                else AdvertisingPduType.AUX_CHAIN_IND
            ),
        }
        capacity = (
            PDU_ADV_EXT_PAYLOAD_SIZE_MAX - len(ext_pdu(**fields)) + PDU_HEADER_SIZE
        )
        more = len(data) - consumed > capacity
        if more:
            fields['aux'] = pointer
            capacity = (
                PDU_ADV_EXT_PAYLOAD_SIZE_MAX - len(ext_pdu(**fields)) + PDU_HEADER_SIZE
            )
        chunk = data[consumed : consumed + capacity]
        if more:
            probe = ext_pdu(**fields, data=chunk)
            distance = secondary_phy.airtime_us(len(probe), coding) + LL_MAFS_US
            distance = (
                (distance + PDU_ADV_OFFSET_UNIT_30_US - 1)
                // PDU_ADV_OFFSET_UNIT_30_US
                * PDU_ADV_OFFSET_UNIT_30_US
            )
            fields['aux'] = aux_pointer(channel, distance, secondary_phy)
        result.append((channel, secondary_phy, offset, ext_pdu(**fields, data=chunk)))
        consumed += len(chunk)
        if not more:
            return (result, tuple(boundaries))
        boundaries.append(consumed)
        offset += distance
        first = False


class _AdvertisingPeer:
    """One independent lower-tester radio; no DUT encoders or Host stack."""

    def __init__(self, radio, sid, output):
        self.radio, self.sid, self.output = (radio, sid, output)
        self.data = None
        self.active = self.busy = False

    async def run(self):
        generator = random.Random(self.sid)
        primary, secondary = PHY_PAIRS[self.sid - 1]
        channel = (self.sid - 1) * AUXILIARY_CHANNEL_STRIDE
        adi = self.sid << PDU_ADV_ADI_DID_BITS
        next_start = self.radio.now_us + LL_MAFS_US
        while True:
            if self.data is None:
                await self.radio.advance_to_us(self.radio.now_us + REPORT_DELIVERY_US)
                next_start = self.radio.now_us + LL_MAFS_US
                continue
            self.busy = True
            data = self.data
            start = max(
                self.radio.now_us + LL_MAFS_US, next_start
            ) + generator.randrange(LL_ADV_DELAY_MAX_US + 1)
            advertisement_start = start
            train, _ = _packets(
                _ScanRound(self.sid, len(data)),
                data,
                None if self.active else adi,
                primary,
                secondary,
                PHY_CODED_S8_FACTOR,
                channel=channel,
            )
            if self.active:
                end = await self.radio.transmit(
                    37,
                    ext_pdu(
                        adi=adi,
                        mode=AdvertisingMode.SCANNABLE,
                        aux=aux_pointer(channel, AUXILIARY_OFFSET_US, secondary),
                    ),
                    start_us=start,
                    phy=primary,
                )
                end = await self.radio.transmit(
                    channel,
                    ext_pdu(
                        address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
                        adi=adi,
                        mode=AdvertisingMode.SCANNABLE,
                    ),
                    start_us=start + AUXILIARY_OFFSET_US,
                    phy=secondary,
                )
                request = await self.radio.receive(
                    channel,
                    end
                    + LL_IFS_US
                    + secondary.address_us
                    + SCAN_RESPONSE_IFS_TOLERANCE_US,
                    phy=secondary,
                )
                if request:
                    assert request.valid_crc and request.phy == secondary
                    assert request.pdu == request_pdu(
                        AdvertisingPduType.SCAN_REQ,
                        LOWER_PERIPHERAL_PUBLIC_ADDRESS,
                        scanner=DUT_ADDRESS,
                    )
                    assert (
                        abs(request.start_us - end - LL_IFS_US)
                        <= SCAN_RESPONSE_IFS_TOLERANCE_US
                    )
                    start = request.end_us + LL_IFS_US - AUXILIARY_OFFSET_US
                else:
                    train = []
            for index, (_, phy, offset, wire) in enumerate(train):
                if self.active and index == 0:
                    continue
                await self.radio.transmit(
                    37 if index == 0 else channel,
                    wire,
                    start_us=start + offset,
                    phy=phy,
                )
            self.output.write(
                f'PEER sid={self.sid} start={advertisement_start} end='
                f'{self.radio.now_us} length={len(data)}\n'
            )
            adi = self.sid << PDU_ADV_ADI_DID_BITS | adi + 1 & PDU_ADV_ADI_DID_MASK
            next_start = advertisement_start + SCANNING_ADVERTISING_INTERVAL_US
            self.busy = False


_scan_multiple_Flags = Report.EventType
_scan_power_Flags = Report.EventType
ROUNDS = (
    _ScanRound(1, kind=AdvertisingPduType.ADV_IND, auxiliary=False),
    _ScanRound(
        2, PDU_ADV_LEGACY_DATA_SIZE_MAX, AdvertisingPduType.ADV_IND, auxiliary=False
    ),
    _ScanRound(
        3, kind=AdvertisingPduType.ADV_DIRECT_IND, target=DUT_ADDRESS, auxiliary=False
    ),
    _ScanRound(
        4, kind=AdvertisingPduType.ADV_DIRECT_IND, target=PEER_ADDRESS, auxiliary=False
    ),
    _ScanRound(5, kind=AdvertisingPduType.ADV_NONCONN_IND, auxiliary=False),
    _ScanRound(
        6,
        PDU_ADV_LEGACY_DATA_SIZE_MAX,
        AdvertisingPduType.ADV_NONCONN_IND,
        auxiliary=False,
    ),
    _ScanRound(7, auxiliary=False),
    _ScanRound(8, target=DUT_ADDRESS, auxiliary=False),
    _ScanRound(9, target=PEER_ADDRESS, auxiliary=False),
    _ScanRound(10),
    _ScanRound(11, target=DUT_ADDRESS),
    _ScanRound(12, target=PEER_ADDRESS),
    _ScanRound(13, 191),
    _ScanRound(14, 382),
    _ScanRound(15, SCAN_MAX_DATA),
    _ScanRound(16, target=DUT_ADDRESS, auxiliary=False, duration=SCAN_DURATION),
    _ScanRound(17, PDU_ADV_LEGACY_DATA_SIZE_MAX, primary_address=True),
    _ScanRound(18, FIRST_AUXILIARY_DATA + REMAINING_AUXILIARY_DATA),
)


@pytest.mark.radio_peers(6, factory=_AdvertisingPeer)
async def _multiple_scanning(tester, *, active=False):
    """Receive every set in both six-set,all-PHY discovery matrix rounds."""
    generator = random.Random(0)
    for length in (191, SCAN_MAX_DATA):
        await _enable_scan(tester, active=active)
        data = [
            bytes([sid]) + bytes(generator.randint(1, 254) for _ in range(length - 1))
            for sid in range(1, 7)
        ]
        complete, fragments = (set(), {})
        for peer, payload in zip(tester.peers, data):
            peer.active, peer.data = (active, payload)

        def reports(
            *, fragments=fragments, data=data, length=length, complete=complete
        ):
            while event := tester.take(lambda event: isinstance(event, Report)):
                for report in event.reports:
                    sid = report.advertising_sid
                    assert 1 <= sid <= len(PHY_PAIRS)
                    response = bool(
                        report.event_type & _scan_multiple_Flags.SCAN_RESPONSE
                    )
                    assert active or not response
                    key = (sid, response)
                    collected = fragments.setdefault(key, [])
                    collected.append(report)
                    status = (
                        report.event_type >> hci.HCI_LE_EXT_ADV_REPORT_DATA_STATUS_SHIFT
                    )
                    if status == Report.DATA_INCOMPLETE_MORE_TO_COME:
                        continue
                    primary, secondary = PHY_PAIRS[sid - 1]
                    expected = data[sid - 1] if not active or response else b''
                    _, boundaries = _packets(
                        _ScanRound(sid, length),
                        expected,
                        None if response else sid << PDU_ADV_ADI_DID_BITS,
                        primary,
                        secondary,
                        PHY_CODED_S8_FACTOR,
                    )
                    _, full = _scanning_check_reports(
                        SimpleNamespace(events=[Report(reports=collected)]),
                        _ScanRound(sid, length),
                        expected,
                        sid << PDU_ADV_ADI_DID_BITS,
                        primary,
                        secondary,
                        boundaries,
                        properties=(
                            _scan_multiple_Flags.SCANNABLE_ADVERTISING
                            | (_scan_multiple_Flags.SCAN_RESPONSE if response else 0)
                            if active
                            else 0
                        ),
                    )
                    fragments.pop(key)
                    if full and (not active or response):
                        complete.add(sid)

        deadline = tester.radio.now_us + 10000000
        while len(complete) < len(PHY_PAIRS) and tester.radio.now_us < deadline:
            await tester.wait(REPORT_DELIVERY_US)
            reports()
        assert complete == set(range(1, 7)), (
            'missing advertising sets',
            length,
            complete,
        )
        for peer in tester.peers:
            peer.data = None
        while (
            any(peer.busy for peer in tester.peers) and tester.radio.now_us < deadline
        ):
            await tester.wait(REPORT_DELIVERY_US)
            reports()
        assert not any(peer.busy for peer in tester.peers)
        await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))
    if not active:
        for suffix in ('Tx.csv', 'Txv2.csv'):
            with (tester.phy_results / f'd_2G4_00.{suffix}').open() as capture:
                assert next(csv.DictReader(capture), None) is None, (
                    'passive scanner transmitted'
                )


async def passive_scanning(tester, *, kind):
    """Passive Scanning.

    Initial Condition:
        Passive scanning uses the selected interval/window and accept-all policy
        0x00. Use a 20-ms advertising base with a 5-ms mean AdvDelay, and a 75-ms
        scan interval/window: three times the mean advertising interval. Disable
        duplicate filtering. BV-01 sends ADV_NONCONN_IND; BV-10 sends ADV_IND.

    Test Case Configuration:
        LL/DDI/SCN/BV-01-C: ADV_NONCONN_IND.
        LL/DDI/SCN/BV-10-C: ADV_IND.

    Test Steps:
        1. Configure the Lower Tester on the first supported channel (37) with
           the common public address.
        2. Enable passive scanning in the IUT with the selected timing and policy.
        3. Send the Table 4.2-49 PDU type on that channel with the event count as
           data, at the selected advertising interval, for twenty scan intervals.
        4. Require LE Advertising Report with the matching advertising event type
           and data from a transmitted packet. Also check peer address and type.
        5. Send LE Set Scan Enable to disable scanning; require Command Complete.
        6. Configure the second channel (38) with a public address differing from
           the IUT address in its least significant octet.
        7. Repeat Steps 2-5.
        8. Configure the third channel (39) with a random address differing from
           the IUT address in its most significant octet.
        9. Repeat Steps 2-5.

    Expected Outcome:
        Pass verdict: complete the procedure on every supported channel with the
        correct packet type recognized. Require a matching report in each phase
        and no SCAN_REQ or scan-response report. The source's conditional Filter
        Accept List verdict requires reports only for a matching address/type,
        rejecting wrong types and unlisted addresses; these two selected cases
        explicitly use accept-all, so that conditional path is not activated.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4.2, LL/DDI/SCN/BV-01-C, LL/DDI/SCN/BV-10-C; PDF
        pages 269-271, printed pages 4.2-176-4.2-178, Table 4.2-49, Figure
        4.2-77, Steps 1-9 and the conditional pass verdict.
    """
    await _scan_reports(tester, active=False, filtered=False, kind=kind)


# Verify that a passive scanner detects and reports non-connectable or undirected
# advertising. The Lower Tester uses one primary channel at a time and an event sequence
# number as advertising data.
PROCEDURES |= procedure_cases(
    passive_scanning,
    {
        'LL/DDI/SCN/BV-01-C': (
            'Passive Scanning: Non-connectable',
            {'kind': 2},
        ),
        'LL/DDI/SCN/BV-10-C': (
            'Passive Scanning: Undirected Events',
            {'kind': 0},
        ),
    },
)


async def active_scanning(tester):
    """Active Scanning.

    Initial Condition:
        Active scanning uses a public own address and selected interval/window.
        The source's initial prose lists basic filtering with one public address
        in the Filter Accept List, but Step 3 explicitly selects accept-all 0x00;
        retain that step value. Use a 20-ms advertising base with 5-ms mean
        AdvDelay and a 75-ms scan interval/window, three times that mean interval.
        Disable duplicate filtering; use the common LT scan-response name.

    Test Steps:
        1. Select the first supported Lower Tester advertising channel (37).
        2. Configure the common public Lower Tester address.
        3. Enable active scanning in the IUT with accept-all policy 0x00.
        4. Send ADV_IND on the selected channel with an unsigned little-endian
           event counter until twenty scan intervals pass or Step 5 executes.
        5. Receive SCAN_REQ at T_IFS after ADV_IND. Check CRC, PHY, both addresses
           and their types; return SCAN_RSP with the selected address and LT name
           at T_IFS after the request. Require request spacing within 2 us.
        6. Interleaved with Step 4, receive LE Advertising Report containing the
           transmitted ADV_IND information.
        7. Interleaved with Step 5, receive LE Advertising Report containing the
           scan-response information.
        8. Disable scanning with LE Set Scan Enable; require Command Complete.
        9. Configure a public advertiser differing from the IUT in its most
           significant address octet.
        10. Repeat Steps 3-8.
        11. Configure a public advertiser differing in its least significant octet.
        12. Repeat Steps 3-8.
        13. Configure a public advertiser differing in both endpoint octets.
        14. Repeat Steps 3-8.
        15. Select the second supported advertising channel (38).
        16. Repeat Steps 2-14.
        17. Select the third supported advertising channel (39).
        18. Repeat Steps 2-14.

    Expected Outcome:
        Pass verdict: on every supported channel the IUT sends a scan request to
        each of the four advertisers and reports both advertisement and response
        information. Check exact address/type/data and T_IFS within 2 us.

    Notes:
        Do not require a fixed report order or grouping: one HCI event may contain
        multiple queued reports. The source also permits a listed-only realization
        or accepting unknown devices; this realization uses Step 3's accept-all
        setting and an isolated PHY, so no unrelated advertisers are present.

    Source Correction:
        Retain numbered Step 3's explicit policy 0x00 rather than the conflicting
        filtered initial prose. Amber supports this local source interpretation;
        it is not an adopted SIG erratum.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4, unnumbered procedure, LL/DDI/SCN/BV-03-C; PDF
        pages 273-275, printed pages 4.2-180-4.2-182, Figure 4.2-79,
        Steps 1-18, pass verdict and Notes. Core Specification 6.3, Vol 4,
        Part E, 7.8.10/7.8.11 define LE scan parameters and enable.
    """
    await _scan_reports(
        tester, kind=AdvertisingPduType.ADV_IND, active=True, filtered=False
    )


# Verify that an active scanner detects advertisers, requests additional information and
# reports both advertising and scan-response data. The Lower Tester emulates multiple
# public advertisers on one channel at a time and checks SCAN_REQ contents, exchange
# timing and the corresponding HCI reports.
PROCEDURES |= procedure_cases(
    active_scanning,
    {
        'LL/DDI/SCN/BV-03-C': (
            'Active Scanning',
            {},
        ),
    },
)


async def scanning_for_advertiser_types(tester):
    """Scanning For Advertiser Types.

    Initial Condition:
        Active scanning uses a public own address and selected interval/window.
        Keep one public entry in the Filter Accept List, but select accept-all
        0x00 as explicitly required by the procedure intro rather than its stale
        filtered initial prose. Use a 20-ms advertising base with 5-ms mean
        AdvDelay and a 75-ms scan interval/window, three times the mean interval.
        Keep scanning enabled across the three channel/type phases.

    Test Steps:
        1. Enable active scanning in the IUT with the selected timing/policy.
        2. Configure the first supported channel (37), a public address differing
           from the IUT in its most significant octet and non-connectable events.
        3. Send ADV_NONCONN_IND until twenty scan intervals pass or Step 4 occurs.
        4. Require at least one LE Advertising Report matching the packet data.
        5. Configure the second channel (38), a public address differing in its
           least significant octet and the specified ADV_SCAN_IND event type.
        6. Send ADV_SCAN_IND until Step 7 or thirty scan intervals elapse.
        7. Receive SCAN_REQ at T_IFS after ADV_SCAN_IND, then return SCAN_RSP at
           T_IFS after the request. Check both addresses/types, CRC, LE 1M PHY and
           request spacing within 2 us; response data carries the common LT name.
        8. Interleaved with Step 6, receive LE Advertising Report for ADV_SCAN_IND.
        9. Interleaved with Step 7, receive LE Advertising Report for SCAN_RSP,
           including its advertiser address and exact response data.
        10. Configure the third channel (39), a public address differing in both
            endpoint octets and directed advertising.
        11. Send ADV_DIRECT_IND with TargetA equal to the IUT until fifteen scan
            intervals pass or the current phase's report in Step 12 arrives.
        12. Receive LE Advertising Report containing the directed packet's data.
        13. Disable scanning with LE Set Scan Enable; require Command Complete.

    Expected Outcome:
        Pass verdict: complete every selected address/type phase and report the
        actual advertising contents. Non-connectable and directed events produce
        advertising reports but no SCAN_REQ; ADV_SCAN_IND produces both advertising
        and scan-response reports after the valid exchange. Reports match exact
        type, peer address/type and data without a fixed event order/grouping.

    Notes:
        The source permits listed-only or accept-all realizations. This one follows
        the explicit accept-all intro in an isolated PHY with no unrelated devices.

    Source Correction:
        Step 11 refers to the previous phase's Step 8; use the current directed
        report in Step 12 as its stop condition. Preserve the explicitly named
        ADV_SCAN_IND in Step 6: the pass prose's generic connectable wording does
        not introduce ADV_IND into that phase. Retain the explicit accept-all
        policy over conflicting initial prose. These are local interpretations,
        not adopted SIG errata.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4, unnumbered procedure, LL/DDI/SCN/BV-05-C; PDF
        pages 278-281, printed pages 4.2-185-4.2-188, Figure 4.2-81,
        Steps 1-13, pass verdict and Notes.
    """
    interval = SCAN_INTERVAL_US // hci.HCI_LE_SCAN_INTERVAL_UNIT_US
    await tester.command(hci.HCI_LE_Clear_Filter_Accept_List_Command())
    common, msb, lsb, both = _advertising_public_peer_addresses()
    await tester.command(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(common.address_type, common)
    )
    await tester.command(
        scan_parameters(
            le_scan_type=1,
            scanning_filter_policy=0,
            le_scan_interval=interval,
            le_scan_window=interval,
        )
    )
    await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(1, 0))
    for channel, peer, kind, intervals in (
        (37, msb, AdvertisingPduType.ADV_NONCONN_IND, 20),
        (38, lsb, AdvertisingPduType.ADV_SCAN_IND, 30),
        (39, both, AdvertisingPduType.ADV_DIRECT_IND, 15),
    ):
        await _scan_phase(
            tester, channel, peer, kind, active=True, accepted=True, intervals=intervals
        )
    await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(0, 0))


# Verify that an accept-all active scanner detects different advertising types and
# addresses on multiple channels, requests data only from the scannable type and reports
# the transmitted advertising/response contents.
PROCEDURES |= procedure_cases(
    scanning_for_advertiser_types,
    {
        'LL/DDI/SCN/BV-05-C': (
            'Scanning For Advertiser Types',
            {},
        ),
    },
)


async def passive_scanning_directed_events(tester):
    """Passive Scanning: Directed Events.

    Initial Condition:
        The IUT has a configured own address and passive scanning parameters,
        with a specific public peer in the Filter Accept List. The common peer's
        bits also form a valid random address; its most significant octet 0x12
        has top bits 00. Select a 20-ms advertising base with 5-ms mean AdvDelay
        and a 75-ms scan interval/window, three times the mean interval. Use
        policy 0x01 and TargetA equal to the IUT; disable duplicate filtering.

    Test Steps:
        1. Configure the first supported channel (37), ADV_DIRECT_IND targeting
           the IUT and a common public AdvA in the Filter Accept List whose bits
           could also represent a valid random address.
        2. Enable passive scanning in the IUT.
        3. Send ADV_DIRECT_IND on the selected channel for twenty scan intervals.
        4. Require LE Advertising Report with the directed event type and the
           configured public advertiser identity. Directed reports have no data.
        5. Disable scanning with LE Set Scan Enable; require Command Complete.
        6. Configure the second channel (38), TargetA equal to the IUT and an
           unlisted public AdvA, selected to differ in its least significant octet.
        7. Repeat Steps 2-5, but require no report in Step 4 for the full phase.
        8. Configure the third channel (39), TargetA equal to the IUT and random
           AdvA with exactly the same 48 address bits used in Step 1.
        9. Repeat Steps 2-5, but require no report in Step 4 for the full phase.

    Expected Outcome:
        Pass verdict: complete the selected channel/timing phases, reporting only
        the peer whose address and type both match the Filter Accept List. Do not
        report an unlisted address or the listed bytes with random type. Require
        no SCAN_REQ in every passive phase; observe all twenty scan intervals.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4, unnumbered procedure, LL/DDI/SCN/BV-11-C; PDF
        pages 281-283, printed pages 4.2-188-4.2-190, Figure 4.2-82,
        Steps 1-9 and the complete pass verdict.
    """
    await _scan_reports(
        tester, kind=AdvertisingPduType.ADV_DIRECT_IND, active=False, filtered=True
    )


# Verify that passive scanning detects and reports directed advertising from the
# permitted advertiser. Exercise one primary channel at a time and distinguish the same
# address bytes with a different address type.
PROCEDURES |= procedure_cases(
    passive_scanning_directed_events,
    {
        'LL/DDI/SCN/BV-11-C': (
            'Passive Scanning: Directed Events',
            {},
        ),
    },
)


async def passive_scanning_discoverable_events(tester):
    """Passive Scanning: Discoverable Events.

    Initial Condition:
        Configure the IUT address, passive scan parameters and Filter Accept
        List policy. The common public Lower Tester address is the only list
        entry. Select 20-ms minimum advertising interval plus a 0-10-ms
        AdvDelay averaging 5 ms. The 75-ms scan interval/window is three
        times the mean advertising interval; disable duplicate filtering.

    Test Steps:
        1. Select the first supported advertising channel and common public
           Lower Tester address.
        2. Enable scanning with LE Set Scan Enable; require Command Complete.
        3. Send ADV_SCAN_IND with event-counter data on that channel for
           twenty scan intervals.
        4. Require an Advertising Report with the transmitted event type and
           data matching a transmitted event counter.
        5. Select the second supported channel and a public advertiser address
           differing from the IUT address in the least significant octet.
        6. Send ADV_SCAN_IND with that address and event-counter data for
           twenty scan intervals.
        7. Require no Advertising Report for the Step 6 packets.
        8. Select the third supported channel and a random advertiser address
           differing from the IUT address in the most significant octet.
        9. Send ADV_SCAN_IND with the Step 8 random address and event-counter
           data for twenty scan intervals.
        10. Require no Advertising Report for the Step 9 packets.
        11. Select a supported channel and the common address with random type.
        12. Send ADV_SCAN_IND with the Step 11 random address and event-counter
            data for twenty scan intervals.
        13. Require no Advertising Report for the Step 12 packets.
        14. Disable scanning with LE Set Scan Enable; require successful
            Command Complete.

    Expected Outcome:
        Pass verdict:
            Report the Step 3 advertiser with matching address, type, event
            type and sequence data. Complete all selected-channel phases.
        Fail verdict:
            Any Advertising Report in Steps 7, 10 or 13 for an incorrect
            advertiser address or type.

    Source Correction:
        Step 9's 'public' address uses the random type configured in Step 8.
        Step 10 refers to current Step 9, not Step 6. Step 12 uses the newly
        configured Step 11 address, not Step 8. Step 14 returns Command
        Complete, not Command Status, per Core Vol 4 Part E 7.8.11.

    Coverage Note:
        Preserve passive scanning continuously through the four phases. Each
        phase covers all twenty intervals, including the positive phase; no
        SCAN_REQ is allowed. Check reports after each injected advertisement.

    Reference:
        LL.TS.p28.pdf, standalone LL/DDI/SCN/BV-12-C, Figure 4.2-83,
        Steps 1-14 and pass/fail verdicts; PDF 283-285, printed 4.2-190..192.
        Source reference [3] 4.4.3.1.
    """
    common, msb, lsb, _ = _advertising_public_peer_addresses()
    await tester.command(hci.HCI_LE_Clear_Filter_Accept_List_Command())
    await tester.command(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(common.address_type, common)
    )
    interval = SCAN_INTERVAL_US // hci.HCI_LE_SCAN_INTERVAL_UNIT_US
    await tester.command(
        scan_parameters(
            le_scan_type=0,
            scanning_filter_policy=1,
            le_scan_interval=interval,
            le_scan_window=interval,
        )
    )
    await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(1, 0))
    for channel, peer in (
        (37, common),
        (38, lsb),
        (39, hci.Address(bytes(msb), hci.AddressType.RANDOM_DEVICE)),
        (37, hci.Address(bytes(common), hci.AddressType.RANDOM_DEVICE)),
    ):
        await _scan_phase(
            tester,
            channel,
            peer,
            AdvertisingPduType.ADV_SCAN_IND,
            active=False,
            accepted=peer == common,
            full_duration=True,
        )
    await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(0, 0))
    _check_scan_reports(tester, peer, AdvertisingPduType.ADV_SCAN_IND, set(), False)


# Verify passive discovery of undirected advertising on each selected channel, including
# rejection of an unlisted address or address type. Advertising payloads identify their
# event sequence.
PROCEDURES |= procedure_cases(
    passive_scanning_discoverable_events,
    {
        'LL/DDI/SCN/BV-12-C': (
            'Passive Scanning: Discoverable Events',
            {},
        ),
    },
)


async def extended_scanning_passive(
    tester, *, primary_phy, secondary_phy, coding_selection=False
):
    """Extended Scanning, Passive.

    Initial Condition:
        The IUT uses passive scanning with accept-all filtering. The Lower Tester
        uses a common public address; directed packets target either the IUT or
        a different device, as specified by the round. Scan_Max_Data is 1650
        octets for this implementation.

    Test Case Configuration:
        BV-91 additionally enables Advertising Coding Selection (Host Support).
        Execute rounds 1-18 for the 1M/1M pair and rounds 10-18 for the other
        PHY pairs. Legacy PDUs occur only in the 1M/1M case. Coded transmissions
        use S=2 in odd-numbered rounds and S=8 in even-numbered rounds.
        Table 4.2-52: rounds 1/2 use ADV_IND with 0/31 data octets; 3/4 use
        ADV_DIRECT_IND targeting IUT/Not IUT; 5/6 use ADV_NONCONN_IND with 0/31
        octets; 7/8/9 use primary-only ADV_EXT_IND, undirected/IUT/Not IUT.
        Rounds 10/11/12 use primary-plus-auxiliary PDUs with no data and the same
        three target choices. Rounds 13/14/15 carry 191/382/Scan_Max_Data octets.
        Round 16 is primary-only directed at the IUT with Duration 0x01F4 (5 s).
        Round 17 places AdvA in the primary and 31 data octets in the auxiliary
        for LE 1M; use the approved Coded correction below. Round 18 has 242
        first-auxiliary octets and 1403 chained octets. Other Durations are zero.

    Test Steps:
        1. For each assigned Table 4.2-52 round, send LE Set Extended Scan
           Parameters with the selected primary PHY, passive scan type, public
           own address, accept-all filtering, and interval/window 0x0010.
           Require successful Command Complete.
        2. Enable extended scanning with Filter_Duplicates and Period zero.
           Duration is zero except in round 16, which uses 0x01F4 (five seconds).
           Require successful Command Complete.
        3. Transmit the round's PDUs, addresses, TargetA, and data lengths using
           the selected PHYs. On Coded PHY, odd rounds use S=2 and even rounds
           S=8. Include AuxPtr whenever an auxiliary PDU follows. Put the table's
           fields in AUX_ADV_IND when present, except the explicit round 17
           placement and its Coded correction below. Populate AdvData with
           seeded random octets; fill each nonfinal PDU to capacity and link
           AUX_CHAIN_IND through AuxPtr. Omit TxPower from extended PDUs.
           With Duration zero, advertise for at least twenty intervals; otherwise
           continue through the round. Use the minimum base plus AdvDelay,
           deferring the next event if needed to avoid overlapping a long chain.
        4. Check every received report's event properties, address/type, PHYs,
           SID, TargetA, zero periodic interval, unavailable TxPower, data order,
           and fragment status for undirected or IUT-directed advertising. Coded
           PHY is reported as 0x04 for S=2 if Host coding-selection support is
           set, and 0x03 otherwise. Reject reports for another TargetA and any
           passive-scanner RF response.
        5. Follow the branch selected by Duration:
           5A.1. With Duration zero, repeat Step 4 until ten logical results have
                 been reported for accepted advertisements. Change AdvData, if
                 any, after a reported result; extended PDUs also change DID.
                 Wrong-target rounds instead require no reports throughout the
                 Step 3 twenty-interval observation.
           5A.2. Disable scanning with LE Set Extended Scan Enable and require
                 successful Command Complete (see command Source Correction).
           5B.1. With nonzero Duration, repeat Step 4 until that time elapses,
                 then receive LE Scan Timeout. Do not impose 5A.1's ten-report
                 threshold on this branch. Require at least one accepted report.

    Expected Outcome:
        Pass verdict:
            Undirected advertisements and those directed at the IUT produce LE
            Extended Advertising Report events with the correct properties,
            public advertiser identity, PHYs, SID, and directed-address fields.
            SID matches meaningful ADI in auxiliary events and is 0xFF for
            primary-only or legacy events. With Host coding-selection support,
            Coded S=2 reports PHY 0x04 and Coded S=8 reports 0x03. With that bit
            clear, both report PHY 0x03. Advertisements directed elsewhere
            produce no reports.

            Data match the transmitted bytes in order. Intermediate fragments
            have Data_Status 0b01; the final fragment is complete (0b00) or
            truncated (0b10). Truncation occurs only at an AUX_CHAIN_IND
            boundary, never inside one PDU. Each accepted round includes at
            least one complete result for data within Scan_Max_Data.

            A nonzero Duration produces LE Scan Timeout and stops scanning;
            subsequent advertisements produce no reports. The passive scanner
            sends no scan request.

    Postamble:
        For timed round 16, send three more advertisements after Scan Timeout and
        require no reports, then disable extended scanning successfully. This
        confirms the stop behavior in the source pass verdict, without adding
        source step numbers. Every accepted round must have a complete data
        result at least once when its data fit within Scan_Max_Data.

    Source Correction:
        Step 5A.2 names legacy Scan Enable after extended scanning; use Extended
        Scan Enable to preserve the command-family boundary. Step 4's generic
        extended-secondary-PHY wording must mean zero when no secondary packet
        exists (rounds 7/8/9/16). Core Vol 4, Part E, 7.8.65 and 7.7.65.13
        support these corrections. Apply the ten-report loop only to accepted
        advertisements, since the wrong-target verdict explicitly forbids reports.
        Table 4.2-51 includes round 17 for Coded primary PHY cases BV-43-C,
        BV-91-C, BV-134-C and BV-135-C, but Table 4.2-52 puts AdvA only in ADV_EXT_IND.
        This contradicts Core Vol 6, Part B, Section 2.3.1.5, Table 2.4 C.1:
        primary AdvA with AuxPtr is RFU on LE Coded and must be ignored.
        These variants instead put AdvA in AUX_ADV_IND and retain the
        advertiser-address and 31-octet data verdicts.
        The reviewer previously approved this local correction; it does not
        establish the test author's intended erratum. LE 1M variants retain the
        original round 17 with AdvA only in ADV_EXT_IND. Amber corroborated the
        command, report-PHY and branch corrections but did not independently
        determine round 17's authored intent. None is an adopted SIG erratum.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4.4, Tables 4.2-51 and 4.2-52; PDF pages
        294-299, printed pages 4.2-201 through 4.2-206. Selected cases:
        LL/DDI/SCN/BV-19-C, LL/DDI/SCN/BV-42-C, LL/DDI/SCN/BV-43-C, LL/DDI/SCN/BV-91-C,
        LL/DDI/SCN/BV-133-C, LL/DDI/SCN/BV-134-C, and LL/DDI/SCN/BV-135-C.
        Steps 1-5, including 5A.1/5A.2/5B.1 and the complete pass verdict.
        The round 17 source row is on PDF page 298, printed page 4.2-205.
        Core Vol 6, Part B, Section 2.3.1.5, Table 2.4 C.1 defines the primary
        address restriction; Section 2.3.1.6, Table 2.5 defines auxiliary fields.
    """
    if coding_selection:
        await tester.command(hci.HCI_LE_Set_Host_Feature_Command(41, 1))
    generator = random.Random(0)
    rounds = ROUNDS if primary_phy == secondary_phy == Phy.LE_1M else ROUNDS[9:]
    for round_ in rounds:
        tester.output.write(f'SCAN ROUND {round_.number}\n')
        await tester.command(
            scan_parameters(
                extended=True,
                scanning_phys=1 << primary_phy - 1,
                scan_types=[0],
                scan_intervals=[SCANNING_SCAN_INTERVAL],
                scan_windows=[SCANNING_SCAN_INTERVAL],
            )
        )
        enabled_at = tester.radio.now_us
        await tester.command(
            hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, round_.duration, 0)
        )
        deadline = enabled_at + round_.duration * hci.HCI_LE_DURATION_UNIT_US
        coding = PHY_CODED_S2_FACTOR if round_.number % 2 else PHY_CODED_S8_FACTOR
        report_phys = (
            (0x04 if phy == Phy.LE_CODED else phy)
            if coding_selection and coding == PHY_CODED_S2_FACTOR
            else phy
            for phy in (primary_phy, secondary_phy)
        )
        report_primary, report_secondary = report_phys
        data = generator.randbytes(round_.length)
        did = reports = complete = 0
        start = tester.radio.now_us + REPORT_DELIVERY_US
        observation_start = start
        accepted = round_.target is None or round_.target == DUT_ADDRESS
        attempts = max(
            ADVERTISING_INTERVALS * REQUIRED_REPORTS,
            round_.duration
            * hci.HCI_LE_DURATION_UNIT_US
            // SCANNING_ADVERTISING_INTERVAL_US
            + 2,
        )
        for attempt in range(attempts):
            adi = ADVERTISING_SID << PDU_ADV_ADI_DID_BITS | did
            train, boundaries = _packets(
                round_, data, adi, primary_phy, secondary_phy, coding
            )
            for channel, phy, offset, wire in train:
                await tester.transmit(
                    channel, wire, start_us=start + offset, phy=phy, coding=coding
                )
            assert (
                await tester.receive(
                    train[-1][0], phy=train[-1][1], duration_us=REPORT_DELIVERY_US
                )
                is None
            ), f'round{round_.number}: passive scanner transmitted a response'
            seen, full = _scanning_check_reports(
                tester, round_, data, adi, report_primary, report_secondary, boundaries
            )
            reports += seen
            complete += full
            if any(
                isinstance(event, hci.HCI_LE_Scan_Timeout_Event)
                for event in tester.events
            ):
                assert round_.duration and tester.radio.now_us >= deadline, (
                    'premature Scan Timeout'
                )
            if seen:
                data = generator.randbytes(round_.length)
                did += 1
            if round_.duration and tester.radio.now_us >= deadline:
                break
            if (
                not round_.duration
                and attempt + 1 >= ADVERTISING_INTERVALS
                and (
                    tester.radio.now_us - observation_start
                    >= ADVERTISING_INTERVALS * SCANNING_ADVERTISING_INTERVAL_US
                )
                and (not accepted or reports >= REQUIRED_REPORTS)
            ):
                break
            start = max(
                start
                + SCANNING_ADVERTISING_INTERVAL_US
                + generator.randrange(LL_ADV_DELAY_MAX_US + 1),
                tester.radio.now_us + LL_MAFS_US,
            )
        else:
            assert not round_.duration, 'scan duration failed to elapse'
        if accepted:
            required_reports = 1 if round_.duration else REQUIRED_REPORTS
            assert reports >= required_reports, (round_.number, reports)
            assert complete, f'round{round_.number}: no complete data result'
        else:
            assert not reports
        if round_.duration:
            await tester.event(hci.HCI_LE_Scan_Timeout_Event)
            for _ in range(3):
                await tester.transmit(37, train[0][3], phy=primary_phy, coding=coding)
                await tester.wait(SCANNING_ADVERTISING_INTERVAL_US)
                assert not any(isinstance(event, Report) for event in tester.events)
        await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))
        assert not any(isinstance(event, Report) for event in tester.events)


# Verify that a passive scanner correctly detects and reports legacy and extended
# advertising, including directed and undirected events, primary and auxiliary PDUs, and
# advertisements with or without data.
PROCEDURES |= procedure_cases(
    extended_scanning_passive,
    {
        'LL/DDI/SCN/BV-133-C': (
            'Extended Scanning, Passive – LE 1M PHY/LE Coded PHY',
            {'primary_phy': Phy.LE_1M, 'secondary_phy': Phy.LE_CODED},
        ),
        'LL/DDI/SCN/BV-134-C': (
            'Extended Scanning, Passive – LE Coded PHY/LE 1M PHY',
            {'primary_phy': Phy.LE_CODED, 'secondary_phy': Phy.LE_1M},
        ),
        'LL/DDI/SCN/BV-135-C': (
            'Extended Scanning, Passive – LE Coded PHY/LE 2M PHY',
            {'primary_phy': Phy.LE_CODED, 'secondary_phy': Phy.LE_2M},
        ),
        'LL/DDI/SCN/BV-19-C': (
            'Extended Scanning, Passive – LE 1M PHY/LE 1M PHY',
            {'primary_phy': Phy.LE_1M, 'secondary_phy': Phy.LE_1M},
        ),
        'LL/DDI/SCN/BV-42-C': (
            'Extended Scanning, Passive – LE 1M PHY/LE 2M PHY',
            {'primary_phy': Phy.LE_1M, 'secondary_phy': Phy.LE_2M},
        ),
        'LL/DDI/SCN/BV-43-C': (
            'Extended Scanning, Passive – LE Coded PHY/LE Coded PHY',
            {'primary_phy': Phy.LE_CODED, 'secondary_phy': Phy.LE_CODED},
        ),
        'LL/DDI/SCN/BV-91-C': (
            'Extended Scanning, Passive – LE Coded PHY/LE Coded PHY, '
            'Advertising Coding Selection (Host Support) bit set',
            {
                'primary_phy': Phy.LE_CODED,
                'secondary_phy': Phy.LE_CODED,
                'coding_selection': True,
            },
        ),
    },
)


async def extended_scanning_active(tester, *, primary_phy, secondary_phy):
    """Extended Scanning, Active.

    Initial Condition:
        Active scanning uses public own address, accept-all policy 0x00 and
        continuous selected interval/window. Scan_Max_Data is 1650 octets.
        The six selected cases are Core 5.1 variants in Table 4.2-53: BV-64
        uses LE 1M/1M and rounds 1-10; BV-65/66/139/140/141 use the assigned
        primary/secondary PHY pairs and rounds 5-8. Core 5.0 variants and
        round 11 are not selected by the workbook. For every assigned round,
        skip that round if ScanData exceeds Scan_Max_Data.

    Test Case Configuration:
        Table 4.2-54 rounds 1/2 use ADV_IND with 0/31 response octets; 3/4 use
        ADV_SCAN_IND with 0/31 response octets. Rounds 5/6/7 use scannable
        ADV_EXT_IND plus AUX_ADV_IND, one response octet, and no TargetA/IUT/Not
        IUT respectively. Round 8 uses 191 response octets, round 9 uses 382
        and round 10 uses Scan_Max_Data. AdvData stays empty in the scannable
        advertisement; ScanData is transmitted in the response and its chain.

    Test Steps:
        1. Set extended scan parameters with Scanning_PHYs from Table 4.2-53,
           active Scan_Type 1, interval/window 0x0010, public own address and
           accept-all policy. Require successful Command Complete.
        2. Enable extended scanning with Filter_Duplicates, Duration and Period
           zero; require successful Command Complete.
        3. Advertise the Table 4.2-54 type on the assigned PHYs. Include AuxPtr
           for AUX_ADV_IND and place the specified AdvA/TargetA only in the
           auxiliary PDU. Use the common public LT address and the round's IUT
           or Not-IUT target. Repeat for at least twenty advertising intervals
           or until Step 4; use the minimum base interval plus seeded AdvDelay.
        4. For undirected or IUT-directed advertising, receive SCAN_REQ for
           legacy PDUs or AUX_SCAN_REQ for extended PDUs on the appropriate
           channel. ScanA is the IUT address and AdvA the LT address. Check CRC,
           PHY, exact addresses/types and T_IFS within 2 us. Receive the matching
           LE Extended Advertising Report with scan-response bit clear, complete
           status, zero periodic interval and empty advertising data. If TargetA
           is not the IUT, require no request/report and proceed to Step 7.
        5. Select the branch for the received request:
           5A.1. After SCAN_REQ, send SCAN_RSP at T_IFS with the table's ScanData,
                 populated with seeded random octets from 1 through 254.
           5B.1. After AUX_SCAN_REQ, send AUX_SCAN_RSP at T_IFS with AdvMode 00
                 and the same ScanData rule. If needed, include AuxPtr and
                 continue with AUX_CHAIN_IND; fill every nonfinal PDU to capacity.
        6. Receive one or more LE Extended Advertising Report events for the
           response, with scan-response bit set and zero periodic interval.
           Validate exact ordered ScanData and fragment status: intermediate
           reports incomplete-more and the final report complete, subject to
           the source pass verdict's chain-boundary truncation allowance.
        7. Disable scanning with LE Set Extended Scan Enable and require
           successful Command Complete (see Source Correction).

    Expected Outcome:
        Pass verdict: accepted undirected/IUT-directed advertisements elicit a
        valid scan request and matching response reports. Reports retain correct
        properties, public address/type, primary/secondary PHYs, directed address
        when applicable and zero periodic interval. Step 4 reports have the
        advertisement's SID from ADI or 0xFF without ADI. For the selected Core
        5.1 cases, response SID comes from AUX_SCAN_RSP's ADI, or AUX_ADV_IND
        when absent; this realization sends the same valid ADI in both.
        Wrong-target directed advertisements produce neither requests nor reports.

        Response data match in order, whether reported whole, fragmented or
        truncated. Intermediate status is incomplete-more (01); final status
        is complete (00) or truncated-no-more (10). Truncation may occur only
        at an AUX_CHAIN_IND boundary, not inside a PDU. Every accepted round
        with data within Scan_Max_Data must yield one complete result at least
        once; retry whole events if earlier results were truncated. Do not
        impose a fixed report grouping.

    Source Correction:
        Step 7 names legacy LE Set Scan Enable after extended scan configuration;
        use LE Set Extended Scan Enable. Core Vol 4, Part E, 3.1.1 returns
        Command Disallowed for a legacy command after extended ones, and 7.8.65
        defines extended scan disable. Amber corroborates this
        command-family correction. This is a local correction, not an adopted
        SIG erratum. Table ScanData describes response contents, not data to
        insert into the scannable AUX_ADV_IND.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4.5, LL/DDI/SCN/BV-139-C, LL/DDI/SCN/BV-140-C,
        LL/DDI/SCN/BV-141-C, LL/DDI/SCN/BV-64-C, LL/DDI/SCN/BV-65-C,
        LL/DDI/SCN/BV-66-C; PDF pages 299-304, printed pages 4.2-206-4.2-211,
        Tables 4.2-53/54, Figure 4.2-89, Steps 1-7 including 5A.1/5B.1 and
        the complete pass verdict.
    """
    rounds = (
        _ScanRound(1, kind=AdvertisingPduType.ADV_IND, auxiliary=False),
        _ScanRound(2, 31, AdvertisingPduType.ADV_IND, auxiliary=False),
        _ScanRound(3, kind=AdvertisingPduType.ADV_SCAN_IND, auxiliary=False),
        _ScanRound(4, 31, AdvertisingPduType.ADV_SCAN_IND, auxiliary=False),
        _ScanRound(5, 1),
        _ScanRound(6, 1, target=DUT_ADDRESS),
        _ScanRound(7, 1, target=PEER_ADDRESS),
        _ScanRound(8, 191),
        _ScanRound(9, 382),
        _ScanRound(10, SCAN_MAX_DATA),
    )
    generator = random.Random(0)
    if primary_phy != Phy.LE_1M or secondary_phy != Phy.LE_1M:
        rounds = rounds[4:8]
    peer = LOWER_PERIPHERAL_PUBLIC_ADDRESS
    for round_ in rounds:
        if round_.length > SCAN_MAX_DATA:
            continue
        tester.output.write(f'ACTIVE SCAN ROUND {round_.number}\n')
        await tester.command(
            scan_parameters(
                extended=True,
                scanning_phys=1 << primary_phy - 1,
                scan_types=[1],
                scan_intervals=[SCANNING_SCAN_INTERVAL],
                scan_windows=[SCANNING_SCAN_INTERVAL],
            )
        )
        await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0))
        accepted = round_.target is None or round_.target == DUT_ADDRESS
        data = bytes(generator.randint(1, 254) for _ in range(round_.length))
        observation_start = tester.radio.now_us
        start = observation_start + REPORT_DELIVERY_US
        complete = False
        for attempt in range(ADVERTISING_INTERVALS * REQUIRED_REPORTS):
            adi = ADVERTISING_SID << PDU_ADV_ADI_DID_BITS | attempt
            channel = LL_ADV_PRIMARY_CHANNELS[0]
            if round_.auxiliary:
                await tester.transmit(
                    channel,
                    ext_pdu(
                        adi=adi,
                        mode=AdvertisingMode.SCANNABLE,
                        aux=aux_pointer(
                            AUXILIARY_CHANNEL, AUXILIARY_OFFSET_US, secondary_phy
                        ),
                    ),
                    start_us=start,
                    phy=primary_phy,
                )
                channel = AUXILIARY_CHANNEL
                end = await tester.transmit(
                    channel,
                    ext_pdu(
                        address=peer,
                        target=round_.target,
                        adi=adi,
                        mode=AdvertisingMode.SCANNABLE,
                    ),
                    start_us=start + AUXILIARY_OFFSET_US,
                    phy=secondary_phy,
                )
            else:
                end = await tester.transmit(
                    channel,
                    adv_pdu(round_.kind, address=peer, data=b''),
                    start_us=start,
                )
            phy = secondary_phy if round_.auxiliary else primary_phy
            request = await tester.receive(
                channel,
                duration_us=LL_IFS_US + phy.address_us + SCAN_RESPONSE_IFS_TOLERANCE_US,
                phy=phy,
            )
            seen, _ = _scanning_check_reports(
                tester,
                round_,
                b'',
                adi,
                primary_phy,
                secondary_phy,
                (),
                properties=(
                    _scanning_Flags.SCANNABLE_ADVERTISING if round_.auxiliary else 0
                ),
            )
            if not accepted:
                assert request is None, 'scan request for another TargetA'
            elif request:
                assert request.valid_crc and request.phy == phy
                assert request.pdu == request_pdu(
                    AdvertisingPduType.SCAN_REQ, peer, scanner=DUT_ADDRESS
                )
                assert (
                    abs(request.start_us - end - LL_IFS_US)
                    <= SCAN_RESPONSE_IFS_TOLERANCE_US
                )
                assert seen, 'no advertising report before scan response'
                if round_.auxiliary:
                    response_round = _ScanRound(round_.number, round_.length)
                    train, boundaries = _packets(
                        response_round,
                        data,
                        adi,
                        primary_phy,
                        secondary_phy,
                        PHY_CODED_S8_FACTOR,
                    )
                    for response_channel, response_phy, offset, wire in train[1:]:
                        await tester.transmit(
                            response_channel,
                            wire,
                            start_us=request.end_us
                            + LL_IFS_US
                            + offset
                            - AUXILIARY_OFFSET_US,
                            phy=response_phy,
                        )
                else:
                    boundaries = ()
                    await tester.transmit(
                        channel,
                        adv_pdu(AdvertisingPduType.SCAN_RSP, address=peer, data=data),
                        start_us=request.end_us + LL_IFS_US,
                    )
                await tester.wait(REPORT_DELIVERY_US)
                seen, complete = _scanning_check_reports(
                    tester,
                    round_,
                    data,
                    adi,
                    primary_phy,
                    secondary_phy,
                    boundaries,
                    properties=_scanning_Flags.SCAN_RESPONSE
                    | (
                        _scanning_Flags.SCANNABLE_ADVERTISING if round_.auxiliary else 0
                    ),
                )
                assert seen, 'no scan-response report'
                if complete:
                    break
            if (
                not accepted
                and attempt + 1 >= ADVERTISING_INTERVALS
                and (
                    tester.radio.now_us - observation_start
                    >= ADVERTISING_INTERVALS * SCANNING_ADVERTISING_INTERVAL_US
                )
            ):
                break
            start = max(
                start
                + SCANNING_ADVERTISING_INTERVAL_US
                + generator.randrange(LL_ADV_DELAY_MAX_US + 1),
                tester.radio.now_us + LL_MAFS_US,
            )
        assert complete == accepted, f'round{round_.number}: missing complete response'
        await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


# Verify that active scanning requests and reports information from scannable extended
# advertising on primary and secondary channels. Exercise directed and undirected
# advertisements and their HCI reports.
PROCEDURES |= procedure_cases(
    extended_scanning_active,
    {
        'LL/DDI/SCN/BV-139-C': (
            'Extended Scanning, Active – LE 1M PHY/LE Coded PHY, Core 5.1',
            {'primary_phy': Phy.LE_1M, 'secondary_phy': Phy.LE_CODED},
        ),
        'LL/DDI/SCN/BV-140-C': (
            'Extended Scanning, Active – LE Coded PHY/LE 1M PHY, Core 5.1',
            {'primary_phy': Phy.LE_CODED, 'secondary_phy': Phy.LE_1M},
        ),
        'LL/DDI/SCN/BV-141-C': (
            'Extended Scanning, Active – LE Coded PHY/LE 2M PHY, Core 5.1',
            {'primary_phy': Phy.LE_CODED, 'secondary_phy': Phy.LE_2M},
        ),
        'LL/DDI/SCN/BV-64-C': (
            'Extended Scanning, Active – LE 1M PHY, Core 5.1',
            {'primary_phy': Phy.LE_1M, 'secondary_phy': Phy.LE_1M},
        ),
        'LL/DDI/SCN/BV-65-C': (
            'Extended Scanning, Active – LE 2M Phy, Core 5.1',
            {'primary_phy': Phy.LE_1M, 'secondary_phy': Phy.LE_2M},
        ),
        'LL/DDI/SCN/BV-66-C': (
            'Extended Scanning, Active – LE Coded PHY, Core 5.1',
            {'primary_phy': Phy.LE_CODED, 'secondary_phy': Phy.LE_CODED},
        ),
    },
)


async def extended_scanning_passive_reporting_tx_power(tester, *, phy):
    """Extended Scanning, Passive, Reporting TX Power.

    Initial Condition:
        Continuous passive scanning uses accept-all filtering and valid selected
        parameters. Scan_Max_Data is 1650 octets. Table 4.2-55 selects LE 1M/1M
        for BV-75 and Coded/Coded for BV-76. Use public addresses, minimum-base
        advertising and interval/window 0x0010. Select distinct Power1=-20 dBm
        and Power2=10 dBm. Each of four data chunks is min(100, Scan_Max_Data/4).

    Test Case Configuration:
        LL/DDI/SCN/BV-75-C: LE 1M.
        LL/DDI/SCN/BV-76-C: LE Coded.

    Test Steps:
        1. Set extended scan parameters with the assigned Scanning_PHYs, passive
           Scan_Type 0, accept-all policy and other valid selected values;
           require successful Command Complete.
        2. Enable extended scanning with Period and Duration zero; require
           successful Command Complete. This realization also disables duplicates.
        3. Send ADV_EXT_IND with AuxPtr to AUX_ADV_IND, which has no AdvData
           and an AuxPtr to the first AUX_CHAIN_IND.
        4. Send four AUX_CHAIN_IND PDUs, each containing the selected count of
           seeded random octets. AUX_ADV_IND and chains 1-3 carry Power1;
           chain 4 carries the distinct Power2.
        5. Receive multiple LE Extended Advertising Report events with periodic
           interval zero and incomplete-more status (01) except the last.
           If the final report is truncated (10), discard the entire attempt
           and return to Step 3 before power, data or grouping verdicts. Fail on
           the tenth truncated attempt. The incomplete-report timeout remains
           applicable before a final report arrives.

    Expected Outcome:
        Pass verdict: multiple reports reproduce the four chunks in order, with
        all intermediate statuses incomplete-more and final status complete (00).
        Each TX_Power is Power1, Power2 or unavailable (0x7F). Also check correct
        event properties, public address/type, PHYs and SID.

        Fail verdict: no subsequent report within two seconds of an incomplete
        report; retry limit reached; Power2 reported before chain 4 was received;
        Power1 reported with chain 4 data or after Power2; or the first report
        containing bytes from any chain has unavailable TX_Power. Reject future,
        missing, reordered or duplicated data. Buffered or incremental reports
        are permitted; do not require one report per PDU.

    Postamble:
        Disable extended scanning successfully after a complete result.

    Source Correction:
        Step 5 explicitly fails on ten truncations, while the separate fail prose
        says more than ten. Use Step 5's explicit threshold. Amber supports this
        local precedence; it is not an adopted SIG erratum.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4.6, LL/DDI/SCN/BV-75-C, LL/DDI/SCN/BV-76-C;
        PDF pages 304-306, printed pages 4.2-211-4.2-213, Table 4.2-55,
        Figure 4.2-90, Steps 1-5 and every pass/fail condition.
    """
    await _reporting_power(tester, active=False, phy=phy)


# Verify that passive scanning reports TxPower from the latest received advertising PDU,
# using a non-connectable, non-scannable auxiliary train.
PROCEDURES |= procedure_cases(
    extended_scanning_passive_reporting_tx_power,
    {
        'LL/DDI/SCN/BV-75-C': (
            'Extended Scanning, Passive, Reporting TX Power – LE 1M PHY',
            {'phy': Phy.LE_1M},
        ),
        'LL/DDI/SCN/BV-76-C': (
            'Extended Scanning, Passive, Reporting TX Power – LE Coded PHY',
            {'phy': Phy.LE_CODED},
        ),
    },
)


async def extended_scanning_active_reporting_tx_power(tester, *, phy):
    """Extended Scanning, Active, Reporting TX Power.

    Initial Condition:
        Continuous active scanning uses public own address, accept-all policy and
        selected parameters. Scan_Max_Data is 1650 octets. Table 4.2-56 selects
        LE 1M/1M for BV-77 and Coded/Coded for BV-78. Response data total
        4 * 229 = 916 octets; mark inconclusive if this exceeds Scan_Max_Data.
        Select four distinct powers -20, -10, 0 and 10 dBm.

    Test Case Configuration:
        LL/DDI/SCN/BV-77-C: LE 1M.
        LL/DDI/SCN/BV-78-C: LE Coded.

    Test Steps:
        1. Set extended scan parameters with the assigned Scanning_PHYs, active
           Scan_Type 1, interval/window 0x0010, public own address and accept-all
           policy; require successful Command Complete.
        2. Enable extended scanning with Filter_Duplicates, Duration and Period
           zero; require successful Command Complete.
        3. Send scannable undirected ADV_EXT_IND with AuxPtr to AUX_ADV_IND.
           The auxiliary advertisement contains no advertising data.
        4. Receive LE Extended Advertising Report with event type 0b0000010:
           scannable undirected advertising and complete status.
        5. Receive AUX_SCAN_REQ with LT AdvA and IUT ScanA. Also validate CRC,
           public address types and T_IFS within 2 us after AUX_ADV_IND.
        6. Send AUX_SCAN_RSP at T_IFS with LT AdvA, Power1, 229 seeded random
           octets and an AuxPtr to the chain. Space response PDUs as far apart
           as practical, using the maximum representable 2,457,300-us offset.
        7. Send three AUX_CHAIN_IND PDUs with 229 seeded random octets each and
           distinct Power2, Power3 and Power4 respectively.
        8. Receive multiple LE Extended Advertising Report events with periodic
           interval zero, low event bits 0b01010, intermediate incomplete-more
           status (01) and a final complete status (00). Each report's TX_Power
           must equal the last PDU received at that report's observation boundary.

    Expected Outcome:
        Pass verdict: Step 8 produces multiple reports with the specified event
        properties/status and latest received TxPower. Data reproduce all four
        chunks in order and contain no not-yet-received bytes. Validate public
        identity, PHYs and SID, allowing buffered or incremental report grouping.
        Inconclusive verdict: response length exceeds the Scan_Max_Data IXIT.
        The passive-power procedure's two-second incomplete-report deadline does
        not apply to this active procedure and its widely spaced response chain.

    Postamble:
        Disable extended scanning successfully after the complete response.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4.7, LL/DDI/SCN/BV-77-C, LL/DDI/SCN/BV-78-C;
        PDF pages 306-308, printed pages 4.2-213-4.2-215, Table 4.2-56,
        Figure 4.2-91, Steps 1-8 and pass/inconclusive verdicts.
    """
    await _reporting_power(tester, active=True, phy=phy)


# Verify active scanning detects scannable undirected extended advertising, requests
# additional information and reports data with the latest received PDU's TxPower.
PROCEDURES |= procedure_cases(
    extended_scanning_active_reporting_tx_power,
    {
        'LL/DDI/SCN/BV-77-C': (
            'Extended Scanning, Active, Reporting TX Power – LE 1M PHY',
            {'phy': Phy.LE_1M},
        ),
        'LL/DDI/SCN/BV-78-C': (
            'Extended Scanning, Active, Reporting TX Power – LE Coded PHY',
            {'phy': Phy.LE_CODED},
        ),
    },
)


@pytest.mark.radio_peers(6, factory=_AdvertisingPeer)
async def extended_scanning_multiple_sets_passive_multiple_phys_all_supported_phys(
    tester,
):
    """Extended Scanning, Multiple Sets, Passive, Multiple PHYs (All Supported PHYs).

    Initial Condition:
        Passive scanning uses public own address, accept-all policy and selected
        interval/window on all supported primary PHYs. Scan_Max_Data is 1650
        octets. This DUT supports LE 1M and Coded scanning and LE 1M/2M/Coded
        auxiliary reception, so the unsupported-PHY conditional is not activated.
        Repeat the complete procedure for both Table 4.2-62 data-length rounds:
        191 octets and Scan_Max_Data.

    Test Steps:
        1. Set extended scan parameters with all supported Scanning_PHYs. For
           each primary PHY use passive Scan_Type 0, interval 0x0010 times the
           number of scan PHYs and window 0x0010, public own address and accept-all
           policy. Require successful Command Complete. The two primary PHYs
           therefore use interval 0x0020 and window 0x0010.
        2. Enable extended scanning with Filter_Duplicates, Duration and Period
           zero; require successful Command Complete.
        3. Start six advertising sets with distinct SIDs: sets 1/2 use 1M/1M,
           3/4 use Coded/Coded and 5/6 use 1M/2M. ADV_EXT_IND points to
           AUX_ADV_IND containing public LT AdvA and seeded random AdvData of
           the round's length. Chain remaining data through AuxPtr and fill
           each nonfinal PDU to capacity. Repeat for at least twenty advertising
           intervals or until Step 4 occurs for every supported set.
        4. For each set, receive one or more LE Extended Advertising Report
           events with matching type, SID, primary/secondary PHYs, zero periodic
           interval and exact data. Track interleaved fragments separately for
           each SID and require one complete data result from each set.
        5. Disable scanning with LE Set Extended Scan Enable and require
           successful Command Complete (see Source Correction).

    Expected Outcome:
        Pass verdict: every set using supported PHYs is reported with exact
        identity, properties, SID, PHYs and ordered data. Intermediate fragments
        are incomplete-more (01); the final fragment is complete (00), or
        truncated (10) only at an AUX_CHAIN_IND boundary. At least once per set
        and round, data within Scan_Max_Data are not truncated. A passive scanner
        sends no requests. The conditional unsupported-PHY verdict requires no
        reports/data for such advertisements; all assigned PHYs are supported here.

    Source Correction:
        Step 5 names legacy Scan Enable after extended scan configuration; use
        Extended Scan Enable, as defined by Core Vol 4, Part E, 7.8.65. Amber
        corroborated this local command correction; it is not an adopted SIG erratum.

    Reference:
        LL.TS.p28.pdf, standalone LL/DDI/SCN/BV-23-C; PDF pages 322-325,
        printed pages 4.2-229-4.2-232, Table 4.2-62, Figure 4.2-96,
        Steps 1-5 and every pass condition.
    """
    await _multiple_scanning(tester, active=False)


# Verify passive scanning detects and reports multiple non-connectable, non-scannable
# advertising sets across every supported PHY pair.
PROCEDURES |= procedure_cases(
    extended_scanning_multiple_sets_passive_multiple_phys_all_supported_phys,
    {
        'LL/DDI/SCN/BV-23-C': (
            (
                'Extended Scanning, Multiple Sets, Passive, Multiple PHYs '
                '(All Supported PHYs)'
            ),
            {},
        ),
    },
)


@pytest.mark.radio_peers(6, factory=_AdvertisingPeer)
async def extended_scanning_multiple_sets_active_multiple_phys_all_supported_phys(
    tester,
):
    """Extended Scanning, Multiple Sets, Active, Multiple PHYs (All Supported PHYs).

    Initial Condition:
        Active scanning uses public own address, accept-all policy and selected
        interval/window on all supported primary PHYs. Scan_Max_Data is 1650
        octets. This DUT supports all assigned primary/secondary PHY pairs.
        Execute both Table 4.2-64 rounds, with 191 and Scan_Max_Data response
        octets. The selected BV-62 uses the Core 5.1 SID expectation in Table
        4.2-63; the Core 5.0 BV-24 variant is not selected.

    Test Steps:
        1. Set extended scan parameters for all supported primary PHYs, with
           active Scan_Type 1, interval 0x0010 times the scan-PHY count, window
           0x0010, public own address and accept-all policy. Require successful
           Command Complete. This realization uses 0x0020/0x0010 per PHY.
        2. Enable extended scanning with Filter_Duplicates, Duration and Period
           zero; require successful Command Complete.
        3. Start six distinct-SID scannable sets: 1/2 use 1M/1M, 3/4 Coded/Coded
           and 5/6 1M/2M. ADV_EXT_IND has AuxPtr to AUX_ADV_IND; put public
           LT AdvA only in the auxiliary PDU and no advertising data. Repeat
           until twenty advertising intervals or Step 4 for the supported sets.
        4. Receive AUX_SCAN_REQ on each set's appropriate channel/PHY, with
           IUT ScanA and LT AdvA. Also validate public types, CRC and T_IFS.
        5. Respond at T_IFS with AUX_SCAN_RSP, AdvMode 00, seeded random octets
           from 1 through 254 and the set ID as the first data octet. Omit ADI
           from the response. Chain remaining data through AuxPtr, filling each
           nonfinal PDU to capacity; response chains also omit ADI.
        6. Receive LE Extended Advertising Report events for each response with
           scan-response bit set, periodic interval zero and exact ordered data.
           Reassemble interleaved fragments per SID and response/advertising type.
        7. Disable scanning with LE Set Extended Scan Enable and require
           successful Command Complete (see Source Correction).

    Expected Outcome:
        Pass verdict: each supported set elicits a correctly addressed request
        and response reports with low event bits 01010, matching set identity,
        PHYs and exact data. Intermediate fragments use incomplete-more (01);
        the final report is complete (00) or truncated (10) only at a chain
        boundary. Require a complete result at least once for every set and round.
        For BV-62 the reported SID matches the response's first octet; with no
        response ADI it is inherited from the matching advertising-set context,
        not invented as zero. Unsupported PHYs would produce neither requests
        nor reports; this conditional is not activated for the supported matrix.

    Source Correction:
        Step 7's legacy Scan Enable must be Extended Scan Enable. The pass text
        calls intermediate status incomplete-more but gives bits 0b10; use the
        HCI-defined 0b01, retaining 0b10 for truncation only. Amber corroborated
        both local corrections against Core Vol 4, Part E, 7.8.65 and 7.7.65.13;
        they are not adopted SIG errata. Table ScanData describes response data,
        not data to place in scannable AUX_ADV_IND.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4.11, LL/DDI/SCN/BV-62-C; PDF pages
        325-328, printed pages 4.2-232-4.2-235, Tables 4.2-63/64,
        Figure 4.2-97, Steps 1-7 and every pass condition.
    """
    await _multiple_scanning(tester, active=True)


# Verify active scanning requests information on the secondary channel and reports each
# of multiple scannable advertising sets across supported PHYs.
PROCEDURES |= procedure_cases(
    extended_scanning_multiple_sets_active_multiple_phys_all_supported_phys,
    {
        'LL/DDI/SCN/BV-62-C': (
            (
                'Extended Scanning, Multiple Sets, Active, Multiple PHYs '
                '(All Supported PHYs), Core 5.1'
            ),
            {},
        ),
    },
)


async def active_scanning_invalid_crc(tester):
    """Active Scanning Invalid CRC.

    Initial Condition:
        Active scanning uses public own address and selected interval/window.
        The source initially specifies basic filtering with one public Filter
        Accept List entry, but its Notes permit an unfiltered realization.
        Use that alternative in the isolated PHY, retaining the common public
        entry. Select a 20-ms advertising base with mean 5-ms AdvDelay and
        a continuous 75-ms scan interval/window, three times the mean interval.

    Test Steps:
        1. Enable active scanning in the IUT with the selected parameters.
        2. Select the first supported Lower Tester advertising channel (37).
        3. Configure the common public advertiser with an invalid checksum.
        4. Send ADV_IND with the whole 24-bit CRC inverted at the selected
           advertising cadence for a duration exceeding twenty scan intervals.
        5. Require no SCAN_REQ at T_IFS after any invalid advertisement.
        6. Require no LE Advertising Report for those invalid packets.
        7. Configure a different public advertiser and a correct checksum.
        8. Send valid ADV_IND until twenty scan intervals elapse or Step 9 occurs.
        9. Receive SCAN_REQ at T_IFS; validate CRC, exact public addresses,
           packet length and request spacing within 2 us.
        10. Send SCAN_RSP with an invalid CRC at T_IFS after the request.
        11. Interleaved with Step 8, receive LE Advertising Report with the
            valid advertisement's public identity and exact data.
        12. Interleaved with Step 10, require no report for the invalid response.
        13. Disable scanning with LE Set Scan Enable; require Command Complete.
        14. Select the second supported advertising channel (38).
        15. Repeat Steps 3-13, re-enabling scanning after the previous disable.
        16. Select the third supported advertising channel (39).
        17. Repeat Steps 3-13, again re-enabling scanning.

    Expected Outcome:
        Pass verdict: complete every supported advertising channel with the
        selected timing combination, ignoring CRC-invalid advertisements and
        responses. Valid advertising must still elicit the correctly addressed
        scan request and HCI report, so invalid-response silence is not caused
        by a disabled scanner.

    Source Correction:
        Step 7's different-address reference to Step 2 means the advertiser
        address configured in Step 3, not the channel selected in Step 2.
        Repeated phases must re-enable scanning after Step 13; the source's
        repeat range omits this necessary enable. Amber corroborated these
        local reference/state corrections; they are not adopted SIG errata.

    Reference:
        LL.TS.p28.pdf, standalone LL/DDI/SCN/BI-01-C; PDF pages 361-363,
        printed pages 4.2-268-4.2-270, Figure 4.2-108, Steps 1-17,
        pass verdict and the unfiltered realization allowed by Notes.
    """
    await _invalid_scanning_crc(tester, active=True)


# Verify active scanning ignores advertising indications with invalid CRC, sends no scan
# request for them and does not report a CRC-invalid scan response to a valid
# advertisement.
PROCEDURES |= procedure_cases(
    active_scanning_invalid_crc,
    {
        'LL/DDI/SCN/BI-01-C': (
            'Active Scanning Invalid CRC',
            {},
        ),
    },
)


async def passive_scanning_invalid_crc(tester):
    """Passive Scanning Invalid CRC.

    Initial Condition:
        Passive scanning uses public own address and selected interval/window.
        Keep the common public address in the Filter Accept List, but select
        accept-all 0x00 as explicitly allowed by the procedure and Notes.
        Use a 20-ms advertising base with mean 5-ms AdvDelay and a 75-ms scan
        interval/window, three times the mean interval. Keep one scan session
        active throughout all three channel phases.

    Test Steps:
        1. Enable passive scanning in the IUT with the selected parameters.
        2. Select the first supported Lower Tester advertising channel (37).
        3. Configure the common public advertiser with an invalid checksum.
        4. Send ADV_NONCONN_IND with the whole 24-bit CRC inverted for a
           duration exceeding twenty scan intervals at the selected cadence.
        5. Require no LE Advertising Report for those packets.
        6. Select the second supported advertising channel (38).
        7. Configure the common public advertiser with an invalid checksum.
        8. Repeat the transmission/no-report checks of Steps 4-5 without
           restarting scanning or overwriting the selected channel.
        9. Select the third supported advertising channel (39).
        10. Configure the common public advertiser with an invalid checksum.
        11. Repeat Steps 4-5 on that channel, still in the same scan session.
        12. Disable LE scanning with LE Set Scan Enable; require Command Complete.

    Expected Outcome:
        Pass verdict: complete the procedure separately on every supported
        channel without any advertising report for a CRC-invalid packet.
        The passive scanner must not transmit SCAN_REQ. No active scan-response
        phase belongs to this source procedure.

    Source Correction:
        Steps 8/11 say repeat Steps 4-6; repeating Step 6 would reselect channel
        38. Preserve the newly selected channel and repeat the substantive
        Steps 4-5. Step 12 names BR/EDR Write Scan Enable; use LE Set Scan Enable.
        The explicit accept-all procedure/Notes alternative controls the chosen
        policy despite filtered initial prose. Amber corroborated these local
        corrections; they are not adopted SIG errata.

    Reference:
        LL.TS.p28.pdf, standalone LL/DDI/SCN/BI-02-C; PDF pages 363-365,
        printed pages 4.2-270-4.2-272, Figure 4.2-109, Steps 1-12,
        pass verdict and Notes. Core Vol 4, Part E, 7.8.11 defines LE scan enable.
    """
    await _invalid_scanning_crc(tester, active=False)


# Verify passive scanning ignores and does not report advertising packets with invalid
# CRC on every supported primary advertising channel.
PROCEDURES |= procedure_cases(
    passive_scanning_invalid_crc,
    {
        'LL/DDI/SCN/BI-02-C': (
            'Passive Scanning Invalid CRC',
            {},
        ),
    },
)


async def passive_scanning_invalid_or_unsupported_phy(tester):
    """Passive Scanning, Invalid or Unsupported PHY.

    Initial Condition:
        Passive scanning with all Lower Tester advertisements accepted through
        the Filter Accept List. Provision its public address before enabling
        filtered scanning. Select a continuous LE 1M scan window. Table 4.2-78
        selects AUX PHY 0b100 (RFU) for LL/DDI/SCN/BI-06-C; the actual
        AUX_ADV_IND uses LE 1M, encoded as 0b000.

    Test Steps:
        1. Send HCI LE Set Extended Scan Parameters enabling passive scanning;
           receive successful HCI Command Complete. Select public addressing,
           LE 1M, continuous interval/window 0x4000 and filter policy 0x01.
        2. Send HCI LE Set Extended Scan Enable; receive successful Command
           Complete. Filter_Duplicates, Duration and Period are zero.
        3. Send ADV_EXT_IND with the Table 4.2-78 AUX PHY value 0b100 (RFU).
        4. Send AUX_ADV_IND on the indicated secondary channel at the AuxPtr
           offset, using the table's actual PHY 0b000 (LE 1M).
        5. Require no HCI LE Extended Advertising Report.
        6. Repeat Steps 3-5 for a total of five primary/auxiliary pairs, using
           distinct data to detect any unexpected report.

    Additional Controls and Postamble:
        The Filter Accept List setup satisfies the source Initial Condition;
        it is not an additional numbered source step.
        Before Step 3, require a valid public ADV_EXT_IND/AUX_ADV_IND report.
        The long continuous window retains this acquired primary channel during
        all five pairs. After Step 6, require another valid report with matching
        address/type, data and PHYs, proving that scanning remains operational.
        Disable extended scanning after both positive and negative verdicts.

    Expected Outcome:
        Pass verdict: The IUT does not send an HCI_LE_Extended_Advertising_Report to the
        Upper Tester.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4.22, LL/DDI/SCN/BI-06-C; PDF pages 386-388,
        printed pages 4.2-293..295, Table 4.2-78, Figure 4.2-120,
        Steps 1-6 and pass verdict; source reference [19] 4.4.3.
    """
    await tester.command(hci.HCI_LE_Clear_Filter_Accept_List_Command())
    await tester.command(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(
            hci.AddressType.PUBLIC_DEVICE, LOWER_PERIPHERAL_PUBLIC_ADDRESS
        )
    )
    await tester.command(
        scan_parameters(
            extended=True,
            scanning_filter_policy=1,
            scanning_phys=hci.PhyBit.LE_1M,
            scan_types=[0],
            scan_intervals=[hci.HCI_LE_SCAN_INTERVAL_MAX],
            scan_windows=[hci.HCI_LE_SCAN_INTERVAL_MAX],
        )
    )
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0))
    valid = aux_pointer(AUXILIARY_CHANNEL, AUXILIARY_OFFSET_US)
    field = int.from_bytes(valid[1:], 'little') | INVALID_AUX_PHY << PDU_ADV_OFFSET_BITS
    invalid = valid[:1] + field.to_bytes(PDU_ADV_AUX_OFFSET_PHY_SIZE, 'little')

    async def send(pointer, data):
        start = tester.radio.now_us + REPORT_DELIVERY_US + LL_MAFS_US
        await tester.transmit(
            LL_ADV_PRIMARY_CHANNELS[0],
            ext_pdu(adi=ADVERTISING_SID << PDU_ADV_ADI_DID_BITS, aux=pointer),
            start_us=start,
        )
        await tester.transmit(
            AUXILIARY_CHANNEL,
            ext_pdu(
                address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
                adi=ADVERTISING_SID << PDU_ADV_ADI_DID_BITS,
                data=data,
            ),
            start_us=start + AUXILIARY_OFFSET_US,
        )
        await tester.wait(REPORT_DELIVERY_US)

    async def positive(data):
        await send(valid, data)
        report = (await tester.event(Report)).reports[0]
        assert report.address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
        assert report.address_type == hci.AddressType.PUBLIC_DEVICE
        assert report.primary_phy == report.secondary_phy == Phy.LE_1M
        assert report.event_type == 0 and report.data == data
        assert not any(isinstance(event, Report) for event in tester.events)

    await positive(b'valid-before')
    for index in range(INVALID_PHY_ROUNDS):
        await send(invalid, bytes([index]))
        assert not any(isinstance(event, Report) for event in tester.events), (
            'reserved AUX PHY must not produce an advertising report'
        )
    await positive(b'valid-after')
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


# Verify that a scanner rejects an extended advertising AuxPtr whose AUX PHY value is
# reserved, rather than reporting its auxiliary advertisement.
PROCEDURES |= procedure_cases(
    passive_scanning_invalid_or_unsupported_phy,
    {
        'LL/DDI/SCN/BI-06-C': (
            'Passive Scanning, Invalid or Unsupported PHY, RFU Bits Specified',
            {},
        ),
    },
)


async def legacy_scanning_ignore_extended_advertising_packets(tester):
    """Legacy Scanning Ignore Extended Advertising Packets.

    Initial Condition:
        Public-address passive scanning with accept-all filtering and duplicate
        filtering disabled. Use the minimum base advertising interval and a full scan
        window; the scan interval is three times the mean advertising interval,
        including AdvDelay.

    Test Steps:
        Use the minimum base advertising interval and maximum continuous scan
        window. The scan interval is three times the mean advertising interval,
        including AdvDelay. Repeat the procedure on channels 37, 38 and 39.
        1. Configure the Lower Tester with the selected first supported
           advertising channel and a common public address.
        2. Send HCI LE Set Scan Parameters with LE_Scan_Type=0x00 and
           Scanning_Filter_Policy=0x00. Receive successful Command Complete.
        3. Send HCI LE Set Scan Enable with Enable=0x01; receive successful
           Command Complete. Duplicate filtering is disabled.
        4. Send ADV_NONCONN_IND on only the selected channel each advertising
           event for twenty complete scan intervals, carrying the event count.
        5. Receive HCI LE Advertising Report with the Step 4 event type and
           data present in an actually transmitted advertisement. Verify the
           Lower Tester public address and address type.
        6. Send ADV_EXT_IND with AuxPtr pointing to the Step 7 auxiliary PDU.
        7. Send AUX_ADV_IND on the indicated secondary channel at that offset.
        8. Require no HCI LE Extended Advertising Report for the Lower Tester.
           Also reject these PDUs being misclassified as legacy reports.
        9. Repeat Steps 6-8 ten more times (eleven pairs in total).
        10. Without restarting scanning, send sequenced ADV_NONCONN_IND on the
            same channel for another twenty complete scan intervals.
        11. Receive HCI LE Advertising Report with the Step 10 event type and
            actually transmitted data; again verify address and address type.

    Postamble:
        Disable legacy scanning after each channel's procedure and reject any
        unexpected extended report throughout the test.

    Expected Outcome:
        Pass verdict: the pre- and post-extended-traffic phases both produce legacy
        reports with the correct event type, address, and actually transmitted sequence
        data. The intervening ADV_EXT_IND and AUX_ADV_IND produce neither extended
        reports nor misclassified legacy reports.

    Reference:
        LL.TS.p28.pdf, standalone LL/DDI/SCN/BV-74-C; PDF pages 388-390,
        printed pages 4.2-295..297, Figure 4.2-121, Steps 1-11 and pass verdict.
        Source references [3] 4.4.3.1 and [20] 7.8.1.
    """
    peer = LOWER_PERIPHERAL_PUBLIC_ADDRESS
    for channel in LL_ADV_PRIMARY_CHANNELS:
        await _enable_legacy_scan(tester)
        await _scan_phase(
            tester,
            channel,
            peer,
            AdvertisingPduType.ADV_NONCONN_IND,
            active=False,
            accepted=True,
            full_duration=True,
        )
        assert not any(isinstance(event, Report) for event in tester.events)
        for sequence in range(EXTENDED_PAIRS):
            start = tester.radio.now_us + REPORT_DELIVERY_US
            adi = ADVERTISING_SID << PDU_ADV_ADI_DID_BITS | sequence
            await tester.transmit(
                channel,
                ext_pdu(
                    adi=adi,
                    aux=aux_pointer(AUXILIARY_CHANNEL, LEGACY_AUXILIARY_OFFSET_US),
                ),
                start_us=start,
            )
            await tester.transmit(
                AUXILIARY_CHANNEL,
                ext_pdu(address=peer, adi=adi, data=b'extended-only'),
                start_us=start + LEGACY_AUXILIARY_OFFSET_US,
            )
            await tester.wait(start + SCAN_ADV_INTERVAL_US - tester.radio.now_us)
            assert not any(
                isinstance(event, (LegacyReport, Report)) for event in tester.events
            ), 'legacy scanning reported an extended advertisement'
        await _scan_phase(
            tester,
            channel,
            peer,
            AdvertisingPduType.ADV_NONCONN_IND,
            active=False,
            accepted=True,
            full_duration=True,
        )
        await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(0, 0))
        assert not any(isinstance(event, Report) for event in tester.events)


# Verify that a legacy passive scanner reports sequenced nonconnectable legacy
# advertisements, ignores intervening extended advertisements, and continues reporting
# the subsequent legacy traffic.
PROCEDURES |= procedure_cases(
    legacy_scanning_ignore_extended_advertising_packets,
    {
        'LL/DDI/SCN/BV-74-C': (
            'Legacy Scanning Ignore Extended Advertising Packets',
            {},
        ),
    },
)


async def legacy_scanning_properly_ignore_rfu_fields_legacy_advertising(tester):
    """Legacy Scanning, Properly Ignore RFU Fields, Legacy Advertising.

    Initial Condition:
        Standby with the selected Scan Max Data IXIT. Execute Table 4.2-79's
        active-scanning case on LE 1M with the minimum advertising interval and
        continuous scanning.

    Test Steps:
        Repeat Steps 1-8 for all Table 4.2-80 rounds: ADV_SCAN_IND, ADV_IND,
        ADV_DIRECT_IND with TargetA=IUT, and ADV_NONCONN_IND. Table 4.2-79
        selects active scanning and Steps 5-7 only for rounds 1/2.
        1. Send HCI LE Set Scan Parameters with LE_Scan_Type=0x01,
           LE_Scan_Interval=LE_Scan_Window=0x0010, Own_Address_Type=0x00
           and Scanning_Filter_Policy=0x00. Receive successful Command Complete.
        2. Enable legacy scanning with Filter_Duplicates=0; receive successful
           Command Complete.
        3. Advertise on LE 1M with the round's PDU type and TargetA, public
           AdvA=Lower Tester and RFU Header bit=1. Require HCI LE Advertising
           Reports for received undirected events or events targeted at the IUT.
           Continue for at least twenty advertising intervals or until Step 6.
        4. In rounds 3/4, fail if the Lower Tester receives SCAN_REQ.
        5. In rounds 1/2, require SCAN_REQ with ScanA=IUT and AdvA=Lower Tester.
           Verify CRC and T_IFS timing. In rounds 3/4, skip Steps 5-7 and
           continue to Step 8.
        6. Send SCAN_RSP at T_IFS after SCAN_REQ, with AdvA=Lower Tester.
        7. Require one or more HCI LE Advertising Reports with Event_Type=4
           (scan response) and data matching the transmitted ScanData.
        8. Disable scanning with HCI LE Set Scan Enable; receive successful
           HCI Command Complete.

    Expected Outcome:
        Pass verdict: RFU fields do not prevent valid advertising or scan-response
        reports. ADV_IND and ADV_SCAN_IND receive scan requests and response reports;
        ADV_DIRECT_IND and ADV_NONCONN_IND never receive scan requests.

    Source Correction:
        The source's ADV_NONCONN_IND Fail verdict incorrectly says that not receiving
        SCAN_REQ is a failure. Table 4.2-80, Step 4, and Core Vol 6, Part B require the
        opposite: receiving SCAN_REQ is a failure for this nonscannable advertising
        type. Amber corroborated this local correction; it is not adopted SIG
        errata.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4.23, LL/DDI/SCN/BV-86-C; PDF pages 390-392,
        printed pages 4.2-297..299, Tables 4.2-79/80, Figure 4.2-122,
        Steps 1-8 and pass/fail verdicts; source reference [10] 2.3, 4.4.3.2.
    """
    peer = LOWER_PERIPHERAL_PUBLIC_ADDRESS
    generator = random.Random(0)
    for kind in (
        AdvertisingPduType.ADV_SCAN_IND,
        AdvertisingPduType.ADV_IND,
        AdvertisingPduType.ADV_DIRECT_IND,
        AdvertisingPduType.ADV_NONCONN_IND,
    ):
        await tester.command(
            scan_parameters(
                le_scan_type=1,
                le_scan_interval=SCANNING_SCAN_INTERVAL,
                le_scan_window=SCANNING_SCAN_INTERVAL,
            )
        )
        await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(1, 0))
        wire = (
            request_pdu(kind, DUT_ADDRESS, scanner=peer)
            if kind == AdvertisingPduType.ADV_DIRECT_IND
            else adv_pdu(kind, address=peer, data=b'')
        )
        wire = bytes([wire[0] | PDU_ADV_RESERVED_MASK]) + wire[1:]
        scannable = kind in (
            AdvertisingPduType.ADV_SCAN_IND,
            AdvertisingPduType.ADV_IND,
        )
        seen = response_seen = 0
        start = tester.radio.now_us + REPORT_DELIVERY_US
        deadline = start + 20 * SCANNING_ADVERTISING_INTERVAL_US
        for attempt in range(200):
            end = await tester.transmit(37, wire, start_us=start)
            request = await tester.receive(37, duration_us=1000)
            if request:
                assert request.valid_crc
                assert scannable and request.pdu == request_pdu(
                    AdvertisingPduType.SCAN_REQ, peer, scanner=DUT_ADDRESS
                )
                assert abs(request.start_us - end - LL_IFS_US) <= 2
                await tester.transmit(
                    37,
                    adv_pdu(AdvertisingPduType.SCAN_RSP, address=peer, data=b'A'),
                    start_us=request.end_us + LL_IFS_US,
                )
            await tester.wait(REPORT_DELIVERY_US)
            while event := tester.take(lambda event: isinstance(event, LegacyReport)):
                for report in event.reports:
                    assert (
                        report.address == peer
                        and report.address_type == hci.AddressType.PUBLIC_DEVICE
                    )
                    if report.event_type == LegacyEventType.SCAN_RSP:
                        assert scannable and report.data == b'A'
                        response_seen += 1
                    else:
                        assert (
                            report.event_type
                            == {
                                AdvertisingPduType.ADV_IND: LegacyEventType.ADV_IND,
                                AdvertisingPduType.ADV_SCAN_IND: (
                                    LegacyEventType.ADV_SCAN_IND
                                ),
                                AdvertisingPduType.ADV_DIRECT_IND: (
                                    LegacyEventType.ADV_DIRECT_IND
                                ),
                                AdvertisingPduType.ADV_NONCONN_IND: (
                                    LegacyEventType.ADV_NONCONN_IND
                                ),
                            }[kind]
                        )
                        assert not report.data
                        seen += 1
            if seen and (
                response_seen
                if scannable
                else attempt >= 19 and tester.radio.now_us >= deadline
            ):
                break
            start = max(
                start
                + SCANNING_ADVERTISING_INTERVAL_US
                + generator.randrange(LL_ADV_DELAY_MAX_US + 1),
                tester.radio.now_us + LL_MAFS_US,
            )
        assert seen and (response_seen or not scannable)
        await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(0, 0))


# Verify that an active scanner ignores reserved fields in received legacy advertising
# PDUs, reports the advertising correctly, and requests scan responses only for
# scannable advertising types.
PROCEDURES |= procedure_cases(
    legacy_scanning_properly_ignore_rfu_fields_legacy_advertising,
    {
        'LL/DDI/SCN/BV-86-C': (
            'Legacy Scanning, Active, Properly Ignore RFU Fields',
            {},
        ),
    },
)


async def extended_scanning_properly_ignore_rfu_fields_extended_advertising(tester):
    """Extended Scanning, Properly Ignore RFU Fields, Extended Advertising.

    Initial Condition:
        Standby. Record the Scan Max Data IXIT (the maximum received scan data).
        Use the minimum advertising interval and continuous active LE 1M scanning.
        Table 4.2-81 selects Scan_Type[0]=0x01 for BV-88 and performs Steps 5-7
        and rounds 12-15. Public Lower Tester and IUT addresses are selected.

    Test Steps:
        Repeat Steps 1-8 for every round in Table 4.2-82.
        1. Send HCI LE Set Extended Scan Parameters: Scanning_PHYs=0x01,
           Scan_Type[0]=0x01, Scan_Interval[0]=Scan_Window[0]=0x0010,
           Own_Address_Type=0x00 and Scanning_Filter_Policy=0x00. Receive
           successful HCI Command Complete.
        2. Enable extended scanning with Filter_Duplicates, Duration and Period
           all zero; receive successful Command Complete.
        3. Advertise on LE 1M using the round's PDU types and RFU fields.
           Set the designated RFU fields only in the last Scan Response PDU
           when ScanData is specified; otherwise only in the last listed PDU.
           Link ADV_EXT_IND to AUX_ADV_IND and AUX_ADV_IND to AUX_CHAIN_IND
           with valid AuxPtr fields where applicable. Set AdvA to the Lower
           Tester and, for directed events, TargetA to the IUT; omit TargetA
           otherwise. The IUT reports each received undirected or IUT-targeted
           event through HCI LE Extended Advertising Report. Repeat for at
           least twenty advertising intervals or until Step 6 occurs.
        4. Fail if a scan request is received for a nonscannable event or during
           passive scanning.
        5. For nonscannable events or when this step is not applicable, go to
           Step 8. Otherwise require AUX_SCAN_REQ at T_IFS after AUX_ADV_IND
           on the appropriate channel, with ScanA=IUT and AdvA=Lower Tester.
           For directed events, verify the reported Direct Address Type and
           Direct Address. A directed event not addressed to the IUT skips
           to Step 8; undirected scannable rounds continue to Step 6.
        6. Send AUX_SCAN_RSP at T_IFS after AUX_SCAN_REQ with AdvMode=0b00.
           Populate AdvData with the round's number of random octets in 1..255.
           If it does not fit one PDU, include AuxPtr and send the remaining
           bytes in one or more AUX_CHAIN_IND PDUs.
        7. Receive one or more HCI LE Extended Advertising Reports with
           Event_Type bit 3 (Scan response) set. The received data must contain
           the ScanData included in the transmitted response packets.
        8. Disable scanning with HCI LE Set Extended Scan Enable and receive
           successful HCI Command Complete.

    Test Rounds:
        Table 4.2-82, all LE 1M; EXT/AUX/CHAIN below name ADV_EXT_IND,
        AUX_ADV_IND and AUX_CHAIN_IND. Mode applies to each listed PDU.
        1. Nonscannable, nonconnectable undirected, EXT(00), RFU Header bit=1.
        2. Same event and PDU, Extended Header Flags bit 7=1.
        3. Same event and PDU, valid data in all CEAP X fields.
        4. Nonscannable, nonconnectable undirected, EXT(00)/AUX(00), all X fields.
        5. Same event, EXT(00)/AUX(00)/CHAIN(00), all CEAP RFU fields.
        6. Nonscannable, nonconnectable directed, EXT(00), all CEAP RFU fields.
        7. Same directed event, EXT(00)/AUX(00), all CEAP RFU fields.
        8. Connectable undirected, EXT(01)/AUX(01), all CEAP RFU fields.
        9. Connectable directed, EXT(01)/AUX(01), all CEAP RFU fields.
        10. Scannable undirected, EXT(10)/AUX(10), all RFU fields, ScanData=1.
        11. Scannable directed, same PDUs/mode/RFU, ScanData=1.
        12. Scannable undirected, same PDUs/mode/RFU, ScanData=191.
        13. Scannable directed, same PDUs/mode/RFU, ScanData=191.
        14. Scannable undirected, same PDUs/mode/RFU, ScanData=382.
        15. Scannable directed, same PDUs/mode/RFU, ScanData=382.
        For rounds 10-15 the RFU fields belong only to the last response PDU,
        not the listed announcement PDUs.

    Expected Outcome:
        Pass verdict:
            Ignore RFU fields and issue one or more HCI LE Extended Advertising
            Reports. Report each received undirected event or event directed to
            the IUT in Step 3, and report the requested scan data in Step 7.
            The forbidden scan request in Step 4 is a failure.

    Source Correction:
        The final verdict names the legacy Advertising Report, inconsistent with
        Step 3 and extended scanning. Use Extended Advertising Report.
        Step 5's unconditional skip for advertisements not directed at the IUT
        would exclude undirected scannable rounds 10/12/14 despite their required
        ScanData. Scope that skip to directed events targeting another device.
        Its AUX_ADV_IND cross-reference is Step 3, not Step 4. The source
        tables and Core Vol 6, Part B, 2.3.1.5/6 and 4.4.3.5 support these
        readings; the explicit PDU in Step 3 resolves the cross-reference.
        These are local corrections, not adopted SIG errata.

        In rounds 3/6, "all X fields" is qualified by Step 3's explicit
        event identity. Keep AuxPtr absent for the no-auxiliary rounds, and
        TargetA absent for undirected rounds. Core Vol 6, Part B, 2.3.1.5
        defines those fields' presence as event discriminators, so filling
        them would test a different CEAP row rather than RFU handling.
        Populate all other context-reserved fields and applicable header bits.
        Preserve mandatory ADI and valid links in auxiliary/chain rounds.
        For directed nonscannable nonconnectable auxiliary rounds put the real
        target in the primary and a different target in the auxiliary field.
        Table 2.5 C.2
        makes that repeated field RFU. In response/chain contexts fill all
        reserved address, CTEInfo, SyncInfo and ACAD fields as applicable.
        This is a local correction, not an adopted SIG erratum.

    Reference:
        LL.TS.p28, Section 4.2.4.24, LL/DDI/SCN/BV-88-C; PDF 392-396,
        printed 4.2-299..303; Tables 4.2-81/82, Figure 4.2-123.
        Core reference [10], Vol 6 Part B Sections 2.3 and 4.4.3.2;
        CEAP field presence: Section 2.3.1.5, event reports: Vol 4 Part E
        Section 7.7.65.13.
    """
    peer = LOWER_PERIPHERAL_PUBLIC_ADDRESS
    generator = random.Random(88)
    ignored_target = LOWER_CENTRAL_PUBLIC_ADDRESS
    for number in range(1, 16):
        directed = number in (6, 7, 9, 11, 13, 15)
        auxiliary = number not in (1, 2, 3, 6)
        mode = (
            AdvertisingMode.SCANNABLE
            if number >= 10
            else AdvertisingMode.CONNECTABLE
            if number in (8, 9)
            else AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
        )
        scan_length = (
            1
            if number in (10, 11)
            else 191
            if number in (12, 13)
            else 382
            if number in (14, 15)
            else 0
        )
        scan_data = bytes(generator.randint(1, 255) for _ in range(scan_length))
        adi = ADVERTISING_SID << PDU_ADV_ADI_DID_BITS | number
        await tester.command(
            scan_parameters(
                extended=True,
                scan_types=[1],
                scan_intervals=[0x10],
                scan_windows=[0x10],
            )
        )
        await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0))
        seen = responses = 0
        adv_pending, response_pending = bytearray(), bytearray()
        start = tester.radio.now_us + 100
        tester.output.write(
            f'RFU ROUND {number} directed={directed} mode={mode} '
            f'scan_data={scan_length}\n'
        )
        for _ in range(20):
            primary_fields = dict(
                address=None if auxiliary else peer,
                target=DUT_ADDRESS
                if directed and mode == AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
                else None,
                adi=adi if auxiliary else None,
                mode=mode,
            )
            if auxiliary:
                primary_fields['aux'] = aux_pointer(
                    AUXILIARY_CHANNEL, AUXILIARY_OFFSET_US
                )
            elif number in (3, 6):
                # Keep the source's explicit undirected/no-auxiliary identity.
                # AuxPtr/TargetA presence would select a different CEAP row.
                primary_fields |= dict(
                    cte=2,
                    adi=adi,
                    sync=PeriodicTrain(30000).sync_info(0),
                    acad=b'\x02\xffZ',
                    data=b'ignored',
                )
            primary = ext_pdu(**primary_fields)
            if number == 1:
                primary = _rfu_fields(primary, header=True, extended=False)
            elif number == 2:
                primary = _rfu_fields(primary, header=False, extended=True)
            elif number in (3, 6):
                primary = _rfu_fields(primary)
            await tester.transmit(37, primary, start_us=start)
            channel = 37
            if auxiliary:
                channel = AUXILIARY_CHANNEL
                auxiliary_start = start + AUXILIARY_OFFSET_US
                fields = dict(
                    address=peer,
                    adi=adi,
                    mode=mode,
                    data=b'' if scan_length else b'payload',
                )
                if directed:
                    # Table 2.5 C.2 reserves a repeated TargetA when a mode-0
                    # primary owns it. Other modes need the real auxiliary target.
                    fields['target'] = (
                        ignored_target
                        if mode == AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
                        else DUT_ADDRESS
                    )
                if number == 5:
                    fields['aux'] = aux_pointer(channel, AUXILIARY_OFFSET_US)
                wire = ext_pdu(**fields)
                if number in (4, 7):
                    wire = _rfu_fields(ext_pdu(**fields | {'cte': 2}))
                elif number in (8, 9):
                    wire = _rfu_fields(wire, context='connectable')
                elif number == 5:
                    # RFU fields belong to the last listed PDU, the chain.
                    wire = ext_pdu(**fields)
                else:
                    # ScanData rounds put RFU fields only in the final response.
                    wire = ext_pdu(
                        **fields | ({'target': DUT_ADDRESS} if directed else {})
                    )
                end = await tester.transmit(channel, wire, start_us=auxiliary_start)
            else:
                end = tester.radio.now_us + 1
            request = await tester.receive(channel, duration_us=1000)
            if request is not None:
                assert scan_length and request.valid_crc
                assert request.pdu == request_pdu(
                    AdvertisingPduType.SCAN_REQ, advertiser=peer, scanner=DUT_ADDRESS
                )
                assert (
                    abs(request.start_us - end - LL_IFS_US) <= RESPONSE_IFS_TOLERANCE_US
                )
                response_start = request.end_us + LL_IFS_US
                first = scan_data[:191]
                if len(scan_data) > len(first):
                    await tester.transmit(
                        channel,
                        ext_pdu(
                            address=peer,
                            adi=adi,
                            aux=aux_pointer(channel, AUXILIARY_OFFSET_US),
                            data=first,
                        ),
                        start_us=response_start,
                    )
                    tail = _rfu_fields(
                        ext_pdu(adi=adi, data=scan_data[len(first) :]), context='chain'
                    )
                    await tester.transmit(
                        channel, tail, start_us=response_start + AUXILIARY_OFFSET_US
                    )
                else:
                    response = _rfu_fields(
                        ext_pdu(address=peer, adi=adi, data=scan_data),
                        context='response',
                    )
                    await tester.transmit(channel, response, start_us=response_start)
            if number == 5:
                await tester.transmit(
                    channel,
                    _rfu_fields(ext_pdu(adi=adi, data=b'chain'), context='chain'),
                    start_us=auxiliary_start + AUXILIARY_OFFSET_US,
                )
            await tester.wait(REPORT_DELIVERY_US)
            while event := tester.take(lambda event: isinstance(event, Report)):
                for report in event.reports:
                    assert (
                        report.address == peer
                        and report.address_type == hci.AddressType.PUBLIC_DEVICE
                    )
                    assert report.primary_phy == Phy.LE_1M
                    assert report.secondary_phy == (Phy.LE_1M if auxiliary else 0)
                    assert report.advertising_sid == (
                        ADVERTISING_SID if auxiliary else 0xFF
                    )
                    assert report.periodic_advertising_interval == 0
                    assert (
                        bool(
                            report.event_type
                            & _scan_compatibility_Flags.DIRECTED_ADVERTISING
                        )
                        == directed
                    )
                    if directed:
                        assert report.direct_address == DUT_ADDRESS
                        assert (
                            report.direct_address_type == hci.AddressType.PUBLIC_DEVICE
                        )
                    is_response = bool(
                        report.event_type & _scan_compatibility_Flags.SCAN_RESPONSE
                    )
                    pending = response_pending if is_response else adv_pending
                    pending.extend(report.data)
                    status = (
                        report.event_type >> hci.HCI_LE_EXT_ADV_REPORT_DATA_STATUS_SHIFT
                        & hci.HCI_LE_EXT_ADV_REPORT_DATA_STATUS_VALUE_MASK
                    )
                    assert status in (
                        Report.DATA_COMPLETE,
                        Report.DATA_INCOMPLETE_MORE_TO_COME,
                    ), 'RFU fields caused truncated advertising data'
                    if status == Report.DATA_COMPLETE:
                        expected = (
                            scan_data
                            if is_response
                            else b''
                            if scan_length or not auxiliary
                            else b'payloadchain'
                            if number == 5
                            else b'payload'
                        )
                        assert bytes(pending) == expected
                        pending.clear()
                        if is_response:
                            assert request is not None
                            responses += 1
                        else:
                            seen += 1
            if responses:
                break
            start += SCANNING_ADVERTISING_INTERVAL_US + generator.randrange(
                LL_ADV_DELAY_MAX_US + 1
            )
        assert seen and (responses if scan_length else not responses)
        assert not adv_pending and not response_pending
        await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


# Verify that a scanner ignores RFU fields in Advertising and Scanning PDUs. The Lower
# Tester sends extended events on one channel at a time, and the IUT reports the
# advertising to the Upper Tester.
PROCEDURES |= procedure_cases(
    extended_scanning_properly_ignore_rfu_fields_extended_advertising,
    {
        'LL/DDI/SCN/BV-88-C': (
            'Extended Scanning, Active, Properly Ignore RFU Fields',
            {},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_scanning(air_dut, identifier, description, procedure, parameters, marks):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
