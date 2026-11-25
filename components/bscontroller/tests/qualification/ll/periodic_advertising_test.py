# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.2: Periodic advertising and PAwR advertiser procedures."""

import random

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
    LL_PERIODIC_INTERVAL_MIN,
    PDU_ADV_ADI_DID_BITS,
    PDU_ADV_ADI_DID_MASK,
    AdvertisingPduType,
)
from bscontroller.radio.base import Phy
from tests.qualification import pawr as pawr_peer
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.discovery import (
    ADI_REPORTS_PER_PHASE,
    PAWR_CONNECTION_COMPLETE,
    ErrorCode,
    Operation,
    _nonconnectable_extended_packet,
    _pawr_take_all,
)
from tests.qualification.peer import PeerLink, establish
from tests.qualification.peer import exchange_iut_features as _iut_features
from tests.qualification.periodic import PeriodicTrain
from tests.qualification.tester import (
    DUT_ADDRESS,
    PEER_ADDRESS,
    LinkParameters,
    chain,
    csa2_channel,
    decode_extended,
    ext_pdu,
    extended_enable,
    extended_parameters,
    follow_auxiliary,
    le_features,
)
from tests.qualification.transfer import TransferSession
from tests.support import AD_PERIODIC_RESPONSE_TIMING_INFORMATION

PROCEDURES = ProcedureMap()

PERIODIC_RESPONSE_TIMING_INFORMATION = AD_PERIODIC_RESPONSE_TIMING_INFORMATION


async def periodic_advertising(tester, *, phy=Phy.LE_1M):
    """Extended Advertising, Periodic Advertising.

    Initial Condition:
        Advertising parameters select supported intervals, advertising event
        type, own-address type and primary channels. This implementation uses
        public, non-connectable/non-scannable advertising on all primary channels,
        a 100-ms extended interval, and a 500-ms periodic interval that fits the
        largest Coded chain. Table 4.2-17 selects primary/secondary PHYs 1M/1M
        (BV-26), 1M/2M (BV-55), or Coded/Coded (BV-56).

    Test Case Configuration:
        Table 4.2-18 lengths are 0, 252, 474, 711, 948, the returned capacity,
        and 31 octets. The last row exercises one-octet HCI fragmentation.

    Test Steps:
        1. Read maximum data capacity, receive successful Command Complete,
           require 31..1650 octets and retain the capacity.
        Repeat Steps 2-16 for each Table 4.2-18 row:
        2. If the row's length fits the capacity, proceed to Step 3; otherwise
           skip the row and continue with the next row (see Source Correction).
        3. Set extended advertising parameters with properties zero, all primary
           channels, a supported selected interval and the Table 4.2-17 PHYs.
           Receive successful Command Complete.
        4. Set periodic advertising parameters with a supported selected interval
           and properties zero; receive successful Command Complete.
        5. Set the row's periodic data using seeded random octets 1..254. Lengths
           above 252 use FIRST, zero or more INTERMEDIATE fragments, then LAST.
           Round 7 uses FIRST with one octet, 29 one-octet INTERMEDIATE commands,
           and LAST with one octet. Otherwise use COMPLETE. Receive successful
           Command Complete for every fragment.
        6. Enable periodic advertising with Enable 1 and receive Command Complete.
        7. Enable extended advertising with Duration 0 and receive Command Complete.
        8. Receive ADV_EXT_IND with AdvMode 00 and AuxPtr on the primary PHY.
        9. Follow AuxPtr to AUX_ADV_IND on the secondary PHY, with AdvMode 00
           and SyncInfo. Check the AuxPtr timing and minimum T_MAFS spacing.
        10. Decode SyncInfo, compute the secondary channel independently using
            CSA #2, and receive AUX_SYNC_IND within its SyncInfo time range.
            Require AdvMode 00 and no ADI; incomplete data requires AuxPtr.
        11. Follow every AuxPtr to AUX_CHAIN_IND with AdvMode 00 and further data,
            until no pointer remains and all submitted data has been received.
        12. Repeat Steps 8-11 one hundred times.
        13. Disable only extended advertising and receive Command Complete,
            keeping periodic advertising enabled.
        14. Repeat Steps 10-11 one hundred times to confirm periodic advertising
            continues, advancing the decoded event counter/channel/anchor.
        15. Disable periodic advertising and receive successful Command Complete.
        16. Clear advertising sets and receive successful Command Complete.

    Expected Outcome:
        Pass verdict: capacity is in range and every executed row produces
        ADV_EXT_IND with AuxPtr to AUX_ADV_IND containing SyncInfo. Every AuxPtr
        meets T_MAFS and its encoded channel/time. AUX_SYNC_IND uses the independently
        computed CSA #2 channel, the configured secondary PHY, AdvMode 00 and no
        ADI. The periodic PDU and its chain contain exactly the submitted data,
        with no additional data (including the empty row), and every chain uses
        the same PHY and AdvMode. Both hundred-observation phases succeed,
        including the phase with extended advertising disabled.

    Source Correction:
        Step 2 refers to Step 17, but the procedure ends at Step 16. Skip an
        oversized row entirely and continue at the next row. This preserves
        the table's capacity guard without inventing an additional source step.
        Amber confirms this interpretation of the supplied source excerpt;
        it is a local correction, not an adopted SIG erratum.

    Reference:
        LL.TS.p28.pdf, Section 4.2.3.6, LL/DDI/ADV/BV-26-C,
        LL/DDI/ADV/BV-55-C and LL/DDI/ADV/BV-56-C; PDF pages 151-156,
        printed pages 4.2-58 through 4.2-63, Tables 4.2-17 and 4.2-18.
    """
    capacity = (
        await tester.command(hci.HCI_LE_Read_Maximum_Advertising_Data_Length_Command())
    ).max_advertising_data_length
    assert hci.HCI_LE_ADV_DATA_SIZE_MAX >= capacity >= 31
    primary_phy = Phy.LE_CODED if phy == Phy.LE_CODED else Phy.LE_1M
    generator = random.Random(0)
    for round_number, size in enumerate((0, 252, 474, 711, 948, capacity, 31), 1):
        if size > capacity:
            continue
        await tester.command(
            extended_parameters(
                primary_advertising_phy=primary_phy,
                secondary_advertising_phy=phy,
                primary_advertising_interval_min=160,
                primary_advertising_interval_max=160,
                primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
            )
        )
        await tester.command(
            hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 400, 400, 0)
        )
        data = bytes(generator.randint(1, 254) for _ in range(size))
        limit = 1 if round_number == 7 else 252
        if size <= limit:
            await tester.command(
                hci.HCI_LE_Set_Periodic_Advertising_Data_Command(
                    0, Operation.COMPLETE_DATA, data
                )
            )
        else:
            for offset in range(0, size, limit):
                operation = (
                    Operation.FIRST_FRAGMENT
                    if not offset
                    else (
                        Operation.LAST_FRAGMENT
                        if offset + limit >= size
                        else Operation.INTERMEDIATE_FRAGMENT
                    )
                )
                await tester.command(
                    hci.HCI_LE_Set_Periodic_Advertising_Data_Command(
                        0, operation, data[offset : offset + limit]
                    )
                )
        await tester.command(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
        await tester.command(extended_enable())
        for _ in range(100):
            primary = await tester.expect_pdu(
                kind=AdvertisingPduType.ADV_EXT_IND,
                duration_us=200_000,
                phy=primary_phy,
            )
            first = _nonconnectable_extended_packet(primary, primary_phy)
            assert 'aux' in first
            _, packet, fields = await follow_auxiliary(tester, primary, first)
            assert _nonconnectable_extended_packet(packet, phy) == fields
            assert fields['adi'] == first['adi']
            assert 'sync' in fields
            train = PeriodicTrain.from_advertisement(packet, fields)
            assert train.interval == 400
            assert await train.receive(tester) == data
        await tester.command(extended_enable(0))
        for _ in range(100):
            assert await train.receive(tester) == data
        await tester.command(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0, 0))
        await tester.command(hci.HCI_LE_Clear_Advertising_Sets_Command())


# Verify ADV_EXT_IND/AUX_ADV_IND announcements, SyncInfo, periodic packet timing and CSA
# #2 channels, and exact data with and without chaining. Confirm that periodic
# advertising continues after extended advertising is disabled, using each selected PHY
# and data length.
PROCEDURES |= procedure_cases(
    periodic_advertising,
    {
        'LL/DDI/ADV/BV-26-C': (
            'Extended Advertising, Periodic Advertising – LE 1M PHY',
            {'phy': Phy.LE_1M},
        ),
        'LL/DDI/ADV/BV-55-C': (
            'Extended Advertising, Periodic Advertising – LE 2M PHY',
            {'phy': Phy.LE_2M},
        ),
        'LL/DDI/ADV/BV-56-C': (
            'Extended Advertising, Periodic Advertising – LE Coded PHY',
            {'phy': Phy.LE_CODED},
        ),
    },
)


# Section 4.2.3.14: one hundred random periodic data octets.
ADI_DATA_LENGTH = 100

# Selected 100-ms extended advertising and periodic intervals for BV-63-C.
ADI_ADVERTISING_INTERVAL = 160
ADI_PERIODIC_INTERVAL = 80


def _ad_types(data):
    """AD types of the AD structures in an ACAD or AdvData field."""
    types, offset = set(), 0
    while offset < len(data) and data[offset]:
        end = offset + 1 + data[offset]
        assert end <= len(data), 'truncated AD structure'
        types.add(data[offset + 1])
        offset = end
    return types


async def periodic_advertising_adi(tester):
    """ADI in Periodic Advertising when supporting Periodic Advertising Filtering.

    Initial Condition:
        Advertising parameters select a supported interval, event type, own
        address type and channel map, and the Upper Tester has not sent Set
        Default Periodic Advertising Sync Transfer Parameters since reset. This
        implementation uses public non-connectable/non-scannable advertising on
        all primary channels with 100-ms extended and periodic intervals.
        Table 4.2-35 selects AUX_SYNC_IND for LL/DDI/ADV/BV-63-C.

    Test Steps:
        1. Set extended advertising parameters with properties 0x0000, all
           primary channels, a selected interval and LE 1M primary and secondary
           PHYs; receive successful Command Complete.
        2. Execute Alternative 2A for AUX_SYNC_IND:
           2A.1. Set periodic advertising parameters; receive Success.
           2A.2. Set periodic data with length 100 and 100 random octets in
                 1..254; receive Success.
           2A.3. Enable periodic advertising with Enable bit 0 and bit 1
                 (Include ADI) set; receive Command Complete.
           2A.4. Enable extended advertising with Duration[0] 0x0000; receive
                 Success.
           Alternative 2B belongs to the unselected AUX_SYNC_SUBEVENT_IND case.
        3. Receive ADV_EXT_IND with AdvMode 0b00 and AuxPtr.
        4. Follow AuxPtr to AUX_ADV_IND with AdvMode 0b00, SyncInfo and an ADI,
           without Periodic Advertising Response Timing Information.
        5. Use SyncInfo and CSA #2 to receive AUX_SYNC_IND on the SyncInfo
           Access Address, with an ADI whose SID equals the step 4 SID and whose
           DID differs from the step 4 DID. Rerun the test once if they match.
        6. Repeat step 5 for nine periodic advertising intervals.
        7. Execute Alternative 7A: Set Periodic Advertising Data with Operation
           0x04; receive successful Command Complete.
        8. Receive AUX_SYNC_IND with the step 5 SID and a DID that differs from
           the step 5 DID. Rerun the test once if it matches the step 4 DID.
        9. Receive AUX_SYNC_IND with the step 8 SID and DID.
        10. Execute Alternative 10A: repeat step 9 for nine periodic intervals.
            Alternative 10B belongs to the unselected AUX_SYNC_SUBEVENT_IND case.

    Observation Setup:
        Read the local features to establish the ADI initial condition. Step 8
        follows a new ADV_EXT_IND and AUX_ADV_IND, so its timing is checked
        against SyncInfo as the pass verdict requires for steps 5 and 8. Every
        periodic chain must carry exactly the step 2A.2 data, and every
        AUX_CHAIN_IND must repeat the AUX_SYNC_IND ADI.

    Expected Outcome:
        Pass verdict: the AUX_ADV_IND and AUX_SYNC_IND DIDs differ and their SIDs
        match. AUX_SYNC_IND in steps 5 and 8 starts within one Offset unit after
        syncPacketWindowOffset from the AUX_ADV_IND. AUX_ADV_IND has no Periodic
        Advertising Response Timing Information. Operation 0x04 gives
        AUX_SYNC_IND a new DID and keeps its SID.

    Postamble:
        Disable periodic and extended advertising and clear the advertising sets.

    Reference:
        LL.TS.p28.pdf, Section 4.2.3.14, LL/DDI/ADV/BV-63-C; PDF pages 202-206,
        printed pages 4.2-109..113, Table 4.2-35, Figures 4.2-46/47, Steps 1-10
        with Alternatives 2A/7A/10A.
        Source references [19] 2.3.1.7, 4.6.34 and [23] 2.3.1.9.
    """
    assert await le_features(tester) & (
        hci.LeFeatureMask.PERIODIC_ADVERTISING_ADI_SUPPORT
    ), 'this source requires Periodic Advertising ADI Support'
    generator = random.Random(0)
    data = bytes(generator.randint(1, 254) for _ in range(ADI_DATA_LENGTH))

    async def acquire():
        primary = await tester.expect_pdu(
            kind=AdvertisingPduType.ADV_EXT_IND, duration_us=200_000
        )
        first = _nonconnectable_extended_packet(primary, Phy.LE_1M)
        assert 'aux' in first
        _, packet, fields = await follow_auxiliary(tester, primary, first)
        assert _nonconnectable_extended_packet(packet, Phy.LE_1M) == fields
        assert fields['adi'] == first['adi'] and 'sync' in fields
        assert PERIODIC_RESPONSE_TIMING_INFORMATION not in _ad_types(
            fields.get('acad', b'')
        )
        auxiliary = int.from_bytes(fields['adi'], 'little')
        return auxiliary, PeriodicTrain.from_advertisement(packet, fields)

    async def observe(train, count):
        """Receive count events with the same ADI and data; return that ADI."""
        adi = None
        for _ in range(count):
            assert await train.receive(tester, adi=True) == data
            assert adi is None or train.received_adi == adi, 'ADI changed'
            adi = train.received_adi
        return adi

    def did(adi):
        return adi & PDU_ADV_ADI_DID_MASK

    for attempt in range(2):
        await tester.command(
            extended_parameters(
                primary_advertising_interval_min=ADI_ADVERTISING_INTERVAL,
                primary_advertising_interval_max=ADI_ADVERTISING_INTERVAL,
                primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
            )
        )
        await tester.command(
            hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(
                0, ADI_PERIODIC_INTERVAL, ADI_PERIODIC_INTERVAL, 0
            )
        )
        await tester.command(
            hci.HCI_LE_Set_Periodic_Advertising_Data_Command(
                0, Operation.COMPLETE_DATA, data
            )
        )
        await tester.command(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(3, 0))
        await tester.command(extended_enable())
        auxiliary, train = await acquire()
        periodic = await observe(train, ADI_REPORTS_PER_PHASE)
        assert periodic >> PDU_ADV_ADI_DID_BITS == auxiliary >> PDU_ADV_ADI_DID_BITS
        collision = did(periodic) == did(auxiliary)
        if not collision:
            await tester.command(
                hci.HCI_LE_Set_Periodic_Advertising_Data_Command(
                    0, Operation.UNCHANGED_DATA, b''
                )
            )
            _, train = await acquire()
            updated = await observe(train, ADI_REPORTS_PER_PHASE + 1)
            assert updated >> PDU_ADV_ADI_DID_BITS == periodic >> PDU_ADV_ADI_DID_BITS
            assert did(updated) != did(periodic), 'Operation 0x04 kept the DID'
            collision = did(updated) == did(auxiliary)
        await tester.command(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0, 0))
        await tester.command(extended_enable(0))
        await tester.command(hci.HCI_LE_Clear_Advertising_Sets_Command())
        if not collision:
            return
        tester.output.write(f'RERUN attempt={attempt + 1}: equal DIDs\n')
    raise AssertionError('AUX_ADV_IND and AUX_SYNC_IND DIDs matched in both runs')


# Verify that a periodic train enabled with Include ADI carries an ADI with the set's
# SID and its own DID, and that Unchanged Data gives the train a new DID.
PROCEDURES |= procedure_cases(
    periodic_advertising_adi,
    {
        'LL/DDI/ADV/BV-63-C': (
            (
                'ADI in Periodic Advertising when supporting Periodic Advertising '
                'Filtering – AUX_SYNC_IND'
            ),
            {},
        ),
    },
)


MULTI_PERIODIC_ROUNDS = (
    (1, (Phy.LE_1M, Phy.LE_1M)),
    (2, (Phy.LE_2M, Phy.LE_2M)),
    (3, (Phy.LE_CODED, Phy.LE_CODED)),
    (10, (Phy.LE_1M, Phy.LE_2M, Phy.LE_CODED)),
    (11, (Phy.LE_2M, Phy.LE_CODED, Phy.LE_1M)),
    (12, (Phy.LE_CODED, Phy.LE_1M, Phy.LE_2M)),
)


async def _set_phase(tester, phys, trains, enabled):
    advertisements = dict.fromkeys(enabled, 0)
    periodic = dict.fromkeys(range(len(phys)), 0)
    primaries = [Phy.LE_CODED if phy == Phy.LE_CODED else Phy.LE_1M for phy in phys]
    for attempt in range(100 * len(phys) * 30):
        if enabled:
            primary_phy = primaries[attempt % len(primaries)]
            primary = await tester.receive(37, duration_us=100000, phy=primary_phy)
            if primary is None:
                continue
            fields = _nonconnectable_extended_packet(primary, primary_phy)
            assert 'aux' in fields and 'adi' in fields
            sid = int.from_bytes(fields['adi'], 'little') >> PDU_ADV_ADI_DID_BITS
            assert sid in enabled, 'Advertising received from a disabled or unknown set'
            assert primary.phy == primaries[sid]
            adi = fields['adi']
            _, packet, fields = await follow_auxiliary(tester, primary, fields)
            assert fields['adi'] == adi
            assert _nonconnectable_extended_packet(packet, phys[sid]) == fields
            assert 'sync' in fields
            assert await chain(tester, packet, fields, expected_phy=phys[sid]) == b''
            train = PeriodicTrain.from_advertisement(packet, fields)
            assert train.interval == (12, 16, 18)[sid]
            if sid in trains:
                assert (
                    train.access_address == trains[sid].access_address
                    and train.crc_init == trains[sid].crc_init
                )
            trains[sid] = train
            advertisements[sid] += 1
        if len(trains) != len(phys):
            continue
        assert len({train.access_address for train in trains.values()}) == len(phys)
        for train in trains.values():
            while train.anchor <= tester.radio.now_us:
                train.next_event()
        sid = min(trains, key=lambda key: (periodic[key], trains[key].anchor))
        data = await trains[sid].receive(tester, required=False)
        if data is not None:
            assert data == b''
            periodic[sid] += 1
        if min(periodic.values()) >= 100 and all(
            count >= 100 for count in advertisements.values()
        ):
            break
    assert min(periodic.values()) >= 100 and all(
        count >= 100 for count in advertisements.values()
    ), (advertisements, periodic)


async def _require_periodic_capacity(tester, command):
    result = await tester.command(
        command, (ErrorCode.SUCCESS, ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR)
    )
    if result.status == ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR:
        pytest.skip(
            'Inconclusive: insufficient resources for the source periodic-set '
            'configuration'
        )


async def multiple_periodic_sets(tester):
    """Extended Advertising, Periodic Advertising, Multiple Sets, Multiple PHYs (All
    Supported PHYs).

    Initial Condition:
        Advertising parameters select supported intervals, event type,
        own-address type and channel map. Supported Simultaneous Advertising
        Sets is an IXIT declaration. This DUT declares both optional PHYs and
        at least three concurrent sets, verified by Read Number of Supported
        Advertising Sets during preparation. Table 4.2-28 therefore selects
        rows 1, 2, 3, 10, 11 and 12. Use public AdvA, distinct handles/SIDs
        and all primary channels. Round 1 always applies; optional-PHY rounds
        apply according to the declaration, not by silently omitting a set.

    Test Case Configuration:
        Table 4.2-28 rows 1/2/3 use two sets on 1M/1M, 2M/2M, or Coded/Coded
        secondary PHYs respectively; uncoded primary PHYs are 1M. Rows 10/11/12
        use three secondary PHYs ordered (1M,2M,Coded), (2M,Coded,1M), and
        (Coded,1M,2M), with matching Coded primary or otherwise 1M primary.
        The IXIT selects these six rows. Clear advertising sets after Step 12
        as the postamble before the next row.

    Test Steps:
        Repeat Steps 1-12 for every selected Table 4.2-28 row:
        1. Configure all specified extended advertising sets, using 50-ms intervals,
           properties zero, all primary channels and the row's primary/secondary
           PHYs. Require successful Command Complete for each parameter command.
        2. Configure all periodic sets with properties zero and intervals 15 ms,
           20 ms and, when present, 22.5 ms for the first, second and third set.
           Require successful Command Complete for every parameter command.
        3. Enable periodic advertising for each set with Enable 1, only after
           all sets have completed Steps 1/2.
        4. Enable the associated extended sets with one command, all Durations 0.
        5. Monitor the supported primary PHYs. Receive each enabled set's
           interlaced ADV_EXT_IND with AdvMode 00 and AuxPtr on its assigned PHY.
        6. Follow each AuxPtr to the assigned secondary PHY's AUX_ADV_IND with
           AdvMode 00 and SyncInfo. Check pointer timing, T_MAFS and matching ADI.
        7. Repeat Steps 5-6 one hundred times per enabled set while using each
           SyncInfo to synchronize to its periodic train. Receive AUX_SYNC_IND
           with AdvMode 00 on the independently computed CSA #2 channels and
           configured PHYs. This receiver also samples at least 100 periodic
           PDUs per train during this phase.
        8. Disable extended advertising for the first set while retaining
           synchronization with every periodic train.
        9. Repeat Steps 5-7 for the remaining associated advertisers and all
           periodic trains. Continue sampling every configured primary PHY and
           reject primary PDUs from disabled or unknown sets; do not stop
           watching Coded solely because the disabled first set used that PHY.
        10. Disable extended advertising for the remaining set(s), maintaining
            synchronization with every periodic train.
        11. For every configured train, receive at least 100 AUX_SYNC_IND PDUs
            on its assigned secondary PHY with correct timing and CSA #2 channels.
        12. Disable periodic advertising on all configured sets.

    Expected Outcome:
        Pass verdict: every configured set sends ADV_EXT_IND with AuxPtr to an
        AUX_ADV_IND containing valid SyncInfo on its assigned secondary PHY.
        Periodic PDUs use correct timing, PHY, AdvMode and independently calculated
        CSA #2 channel indices. All trains continue through both associated-disable
        phases, including at least 100 periodic PDUs per train in Step 11.
        Configured intervals and per-train Access Address/CRCInit remain consistent.
        Inconclusive verdict: Memory Capacity Exceeded while setting extended or
        periodic advertising parameters skips the test rather than passing it.
        Any other unexpected HCI status or RF mismatch fails.

    Source Correction:
        Step 11 says "both sets", but Table 4.2-28 rows 10-12 configure three.
        Apply the observation to all configured sets, consistent with the earlier
        per-set steps and pass verdict. Amber supports this interpretation of the
        supplied source; no officially adopted SIG erratum is claimed.

    Reference:
        LL.TS.p28.pdf, Section 4.2, LL/DDI/ADV/BV-33-C;
        PDF pages 174-178, printed pages 4.2-81 through 4.2-85,
        Table 4.2-28, both optional PHYs and maximum sets >=3 column.
    """
    result = await tester.command(
        hci.HCI_LE_Read_Number_Of_Supported_Advertising_Sets_Command()
    )
    assert result.num_supported_advertising_sets >= 3
    for number, phys in MULTI_PERIODIC_ROUNDS:
        tester.output.write(f'PERIODIC ROUND {number}\n')
        handles = list(range(len(phys)))
        for handle, phy in enumerate(phys):
            primary = Phy.LE_CODED if phy == Phy.LE_CODED else Phy.LE_1M
            await _require_periodic_capacity(
                tester,
                extended_parameters(
                    advertising_handle=handle,
                    advertising_sid=handle,
                    primary_advertising_interval_min=80,
                    primary_advertising_interval_max=80,
                    primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
                    primary_advertising_phy=primary,
                    secondary_advertising_phy=phy,
                ),
            )
        for handle in handles:
            interval = (12, 16, 18)[handle]
            await _require_periodic_capacity(
                tester,
                hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(
                    handle, interval, interval, 0
                ),
            )
        for handle in handles:
            await tester.command(
                hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, handle)
            )
        await tester.command(
            hci.HCI_LE_Set_Extended_Advertising_Enable_Command(
                1, handles, [0] * len(handles), [0] * len(handles)
            )
        )
        trains = {}
        await _set_phase(tester, phys, trains, handles)
        await tester.command(
            hci.HCI_LE_Set_Extended_Advertising_Enable_Command(0, [0], [0], [0])
        )
        await _set_phase(tester, phys, trains, handles[1:])
        await tester.command(
            hci.HCI_LE_Set_Extended_Advertising_Enable_Command(
                0, handles[1:], [0] * (len(handles) - 1), [0] * (len(handles) - 1)
            )
        )
        await _set_phase(tester, phys, trains, [])
        for handle in handles:
            await tester.command(
                hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0, handle)
            )
        await tester.command(hci.HCI_LE_Clear_Advertising_Sets_Command())


# Verify simultaneous periodic advertising sets on the same or different supported PHYs,
# their announcement timing and packet contents, independent CSA #2 channel sequences,
# and continued synchronization when the associated extended advertising is disabled in
# stages.
PROCEDURES |= procedure_cases(
    multiple_periodic_sets,
    {
        'LL/DDI/ADV/BV-33-C': (
            (
                'Extended Advertising, Periodic Advertising, Multiple Sets,'
                ' Multiple PHYs (All Supported PHYs)'
            ),
            {},
        ),
    },
)


async def stable_sync_info(tester):
    """Periodic Advertising validating SyncInfo fields.

    Initial Condition:
        Advertising parameters select a supported own-address type and channel
        map. This implementation uses public AdvA, all primary channels, LE 1M
        on both PHYs and the minimum periodic interval.

    Test Steps:
        1. Set extended advertising parameters: properties zero, public
           Own_Address_Type, all primary channels, and LE 1M on both PHYs.
        2. Set periodic advertising parameters using the minimum interval and
           properties zero.
        3. Set one periodic data octet with value 0x00.
        4. Enable periodic advertising with Enable 1.
        5. Enable extended advertising with Duration 0.
        6. Receive ADV_EXT_IND with AdvMode 00 and AuxPtr.
        7. Follow the pointer to AUX_ADV_IND with AdvMode 00 and SyncInfo.
           Independently decode its Access Address and CRCInit and compare them
           with the first observation. AUX_SYNC_IND transmission may occur in
           the background; this procedure checks the SyncInfo announcements.
        8. Repeat Steps 6-7 ten times.

    Postamble:
        Disable extended and periodic advertising after the observations.

    Expected Outcome:
        Pass verdict: all observed AUX_ADV_IND SyncInfo fields describing this
        periodic train contain identical Access Address and CRCInit. Every
        observation has the required primary/auxiliary types, modes and PHYs,
        a valid AuxPtr timing/channel and at least T_MAFS spacing.

    Reference:
        LL.TS.p28.pdf, Section 4.2, LL/DDI/ADV/BV-43-C; PDF pages 194-195, printed pages
        4.2-101 through 4.2-102, Figure 4.2-41.
    """
    await tester.command(
        extended_parameters(
            primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL
        )
    )
    await tester.command(
        hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(
            0, LL_PERIODIC_INTERVAL_MIN, LL_PERIODIC_INTERVAL_MIN, 0
        )
    )
    await tester.command(
        hci.HCI_LE_Set_Periodic_Advertising_Data_Command(
            0, Operation.COMPLETE_DATA, b'\x00'
        )
    )
    await tester.command(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    await tester.command(extended_enable())
    reference = None
    for _ in range(10):
        primary = await tester.expect_pdu(kind=AdvertisingPduType.ADV_EXT_IND)
        first = _nonconnectable_extended_packet(primary, Phy.LE_1M)
        assert 'aux' in first
        _, packet, fields = await follow_auxiliary(tester, primary, first)
        assert _nonconnectable_extended_packet(packet, Phy.LE_1M) == fields
        assert fields['adi'] == first['adi'] and 'sync' in fields
        train = PeriodicTrain.from_advertisement(packet, fields)
        pair = (train.access_address, train.crc_init)
        if reference is None:
            reference = pair
        assert pair == reference
    await tester.command(extended_enable(0))
    await tester.command(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0, 0))


# Verify that every SyncInfo describing the same periodic advertising train has
# identical Access Address and CRCInit, using non-connectable and non-scannable
# advertisements.
PROCEDURES |= procedure_cases(
    stable_sync_info,
    {
        'LL/DDI/ADV/BV-43-C': (
            'Periodic Advertising validating SyncInfo fields',
            {},
        ),
    },
)


async def _pawr_fill_requests(tester, selected, data, *, slot_start=0, slot_count=10):
    """Publish requested instances only, with each HCI command within 255 octets."""
    requests = _pawr_take_all(tester, pawr_peer.REQUEST)
    published = 0
    for event in requests[-1:]:
        subevents = [
            sub
            for sub in selected
            if event.subevent_start
            <= sub
            < event.subevent_start + event.subevent_data_count
        ]
        capacity = max(1, 253 // (4 + len(data)))
        for start in range(0, len(subevents), capacity):
            batch = subevents[start : start + capacity]
            await tester.command(
                pawr_peer.SET_DATA(
                    0,
                    batch,
                    [slot_start] * len(batch),
                    [slot_count] * len(batch),
                    [data] * len(batch),
                )
            )
            published += len(batch)
    return published


async def _pawr_response_packet(
    tester, train, indication, subevent, slot, data, *, aa=None
):
    start = indication.start_us + train.pawr[2] * 1250 + slot * train.pawr[3] * 125
    channel = csa2_channel(
        train.counter ^ subevent, train.access_address, train.channel_map
    )
    await tester.transmit(
        channel,
        ext_pdu(data=data),
        start_us=start,
        access_address=train.rsp_aa if aa is None else aa,
        crc_init=train.crc_init,
        phy=train.phy,
    )
    return start


async def pawr_advertising_responses(tester, *, wrong_slot=False):
    """Transmit Periodic Advertising with Responses and filter response slots.

    Initial Condition:
        Standby; public non-connectable/non-scannable extended advertising.

    Test Case Configuration:
        LL/DDI/ADV/BV-88-C: responses in requested slots.
        LL/DDI/ADV/BI-08-C: responses outside requested slots.

    Test Steps:
        1. For BV-88, execute all six Table 4.2-42 rounds, configuring ten
           100-ms subevents, RSD 10 ms, each row's RSS and 25 slots.
        2. Read SyncInfo and the independent response-timing ACAD from air.
        3. For ten events with extended advertising on and ten with it off,
           answer data requests for only the row's selected subevents.
        4. Check every IND's channel, timing, addresses and exact payload.
           Send 3-25 seeded random slot responses, including requested slots;
           reassemble reports and check each ten-iteration success rate.
        5. For BI-08, use ten 65-ms subevents and RSD 1.25 ms. Request only
           slot 3; send in slot 2 first, then random slots except 3 for five
           completed data-response iterations. Failure reports are optional.
        6. Disable periodic advertising and clear the set.

    Expected Outcome:
        Requested data appears once per instance. At least 95% of requested
        responses complete in each ten-iteration phase. Reports are emitted only
        in requested slots, with exact payload and complete final fragments.
        Wrong-slot data is never reported as received. Extended advertising
        may stop while the periodic train and response reporting continue.

    Source Correction:
        BI-08 Expected Outcome copies timing values and responseDuration from
        another procedure. Use its explicit Step 2 fields (SI 0x34, RSD 0x01,
        RSS 0x0A) and the Core Periodic Advertising Response Timing Information
        format. This local reading is not an adopted SIG erratum.

    Reference:
        LL.TS.p28, LL/DDI/ADV/BV-88-C, Table 4.2-42, Steps 1-16;
        LL/DDI/ADV/BI-08-C, Steps 1-13. Core Vol 6, Part B, 4.4.2.12.2.
    """
    generator = random.Random(0)
    rounds = (
        [(10, (0,), 10)]
        if wrong_slot
        else [
            (10, (0,), 1),
            (10, (1, 3), 25),
            (10, (3,), 50),
            (15, (9,), 127),
            (10, (3, 4, 5), 25),
            (25, (0, 9), 249),
        ]
    )
    for spacing, selected, size in rounds:
        timing = (10, 52, 1, spacing) if wrong_slot else (10, 80, 8, spacing)
        train, _ = await pawr_peer.advertiser(
            tester, interval=520 if wrong_slot else 800, timing=timing
        )
        data = bytes(generator.randrange(1, 255) for _ in range(size))
        first_slot = 3 if wrong_slot else generator.randrange(15)
        allowed = {3} if wrong_slot else set(range(first_slot, first_slot + 10))
        for phase, iterations in enumerate((5,) if wrong_slot else (10, 10)):
            if phase:
                await tester.command(
                    hci.HCI_LE_Set_Extended_Advertising_Enable_Command(0, [], [], [])
                )
            observed = dict.fromkeys(selected, 0)
            sent_total = received_total = 0
            for _ in range(iterations + 2):
                if min(observed.values()) >= iterations:
                    break
                for sub in range(train.pawr[0]):
                    await _pawr_fill_requests(
                        tester,
                        selected,
                        data,
                        slot_start=first_slot,
                        slot_count=1 if wrong_slot else 10,
                    )
                    packet = await pawr_peer.receive_indication(tester, train, sub)
                    fields = decode_extended(packet.pdu)
                    assert not any(
                        name in fields for name in ('address', 'target', 'aux', 'sync')
                    )
                    if not fields['data']:
                        continue
                    assert sub in selected and fields['data'] == data
                    observed[sub] += 1
                    if wrong_slot:
                        slots = (
                            [2]
                            if observed[sub] == 1
                            else [
                                generator.choice(
                                    [slot for slot in range(20) if slot != 3]
                                )
                            ]
                        )
                    else:
                        slots = generator.sample(range(25), generator.randint(3, 25))
                        if not allowed.intersection(slots):
                            slots[0] = generator.choice(sorted(allowed))
                    sent = {}
                    # Leave T_IFS before the following slot; ext_pdu has 88 us
                    # of overhead on LE 1M when no optional fields are included.
                    capacity = (spacing * 125 - 150 - 88) // 8
                    for slot in sorted(slots):
                        payload = bytes(
                            generator.randrange(1, 255)
                            for _ in range(generator.randint(1, min(249, capacity)))
                        )
                        await _pawr_response_packet(
                            tester, train, packet, sub, slot, payload
                        )
                        if slot in allowed:
                            sent[slot] = payload
                    end = (
                        packet.start_us
                        + timing[2] * 1250
                        + (max(max(slots), max(allowed)) + 1) * spacing * 125
                    )
                    await tester.wait(max(0, end - tester.radio.now_us))
                    received = pawr_peer.response_data(
                        _pawr_take_all(tester, pawr_peer.RESPONSES), sub, allowed
                    )
                    assert all(
                        slot in sent and payload == sent[slot]
                        for slot, payload in received.items()
                    )
                    if wrong_slot:
                        assert not received
                    else:
                        sent_total += len(sent)
                        received_total += len(received)
                train.next_event()
            assert min(observed.values()) >= iterations, (
                'requested data was never exercised'
            )
            if not wrong_slot:
                assert sent_total and received_total * 100 >= sent_total * 95
        await pawr_peer.stop_advertising(tester)


# Verify requested PAwR subevent data and response reports in assigned slots, rejecting
# wrong-slot responses and continuing periodic advertising after extended advertising
# is disabled.
PROCEDURES |= procedure_cases(
    pawr_advertising_responses,
    {
        'LL/DDI/ADV/BV-88-C': (
            'Transmit Periodic Advertising with Responses and filter response slots.',
            {},
        ),
        'LL/DDI/ADV/BI-08-C': (
            'Transmit Periodic Advertising with Responses and filter response slots.',
            {'wrong_slot': True},
        ),
    },
)


async def pawr_advertising_adi(tester):
    """ADI in Periodic Advertising when supporting Periodic Advertising Filtering.

    Initial Condition:
        A public extended set and ten 100-ms PAwR subevents on LE 1M.

    Test Steps:
        1. Enable ADI and submit 100 random octets for each requested instance.
        2. Observe ten periodic events and their subevents while extended
           advertising remains enabled, following source alternative 10B.
        3. Compare the periodic ADI with the superior AUX_ADV_IND ADI and the
           preceding periodic indication whenever new Host data is sent.

    Expected Outcome:
        The SID matches the set. Every new payload has a different DID from
        the advertising set and the preceding periodic indication. Data is
        complete and no AuxPtr or address fields appear in a subevent.

    Source Correction:
        The Expected Outcome copies the no-response ACAD rule into the PAwR
        case. Follow alternative 2B and Step 4, which require response timing
        in AUX_ADV_IND. The Lower Tester verifies those timing fields.

    Reference:
        LL.TS.p28, 4.2.3.14, LL/DDI/ADV/BV-87-C, Table 4.2-35 alternative 2B.
        Core Vol 6, Part B, 4.4.2.11.
    """
    data = bytes(random.Random(0).sample(range(1, 255), 100))
    for retry in range(2):
        train, announcement = await pawr_peer.advertiser(
            tester, timing=(10, 80, 1, 10), slots=5, adi=True
        )
        superior = int.from_bytes(announcement['adi'], 'little')
        previous = None
        data_indications = 0
        collision = False
        for _ in range(10):
            for sub in range(10):
                await _pawr_fill_requests(tester, range(10), data, slot_count=0)
                fields = decode_extended(
                    (await pawr_peer.receive_indication(tester, train, sub)).pdu
                )
                assert not any(
                    name in fields for name in ('address', 'target', 'aux', 'sync')
                )
                adi = int.from_bytes(fields['adi'], 'little')
                assert adi >> 12 == superior >> 12
                if adi == superior:
                    collision = True
                    break
                if fields['data']:
                    assert adi != previous and fields['data'] == data
                    data_indications += 1
                previous = adi
            if collision:
                break
            train.next_event()
        await pawr_peer.stop_advertising(tester)
        if not collision:
            assert data_indications, 'no Host data was verified'
            return
        assert retry == 0, 'periodic and extended DIDs collide after the allowed rerun'


# Verify PAwR subevent data, SID/DID changes, and the absence of address and AuxPtr
# fields in AUX_SYNC_SUBEVENT_IND.
PROCEDURES |= procedure_cases(
    pawr_advertising_adi,
    {
        'LL/DDI/ADV/BV-87-C': (
            'ADI in Periodic Advertising when supporting Periodic '
            'Advertising Filtering.',
            {},
        ),
    },
)


async def pawr_advertising_channel_map(tester):
    """AUX_SYNC_SUBEVENT_IND PDU with Channel Map Update Indication.

    Initial Condition:
        A ten-subevent public PAwR advertiser using all 37 data channels.

    Test Steps:
        1. Start the train and read its SyncInfo and response timing.
        2. Supply one data octet for subevent 0. Execute all four Table
           4.2-44 maps: odd, even, odd and even channels. Retry rejected
           classifications up to twenty times with five seconds between tries.
        3. Receive subevents and decode ACAD type 0x28 independently.
        4. Check that the first indication names an Instant at least six
           events ahead. At that Instant switch the Lower Tester's CSA#2 map;
           continue for two further periodic events.

    Expected Outcome:
        Each indication contains the requested map and an Instant at least
        six events ahead. Before the Instant old-map channels are used;
        thereafter the train remains receivable on the new map.

    Reference:
        LL.TS.p28, LL/DDI/ADV/BV-90-C, Table 4.2-44; Core 4.4.2.12.1.
    """
    train, _ = await pawr_peer.advertiser(tester, timing=(10, 80, 8, 10))
    await _pawr_fill_requests(tester, [0], b'\x5a', slot_count=0)
    odd = sum(1 << channel for channel in range(1, 37, 2))
    even = sum(1 << channel for channel in range(0, 37, 2))
    generator = random.Random(0)
    for requested in (odd, even, odd, even):
        channels = requested
        for attempt in range(20):
            result = await tester.command(
                hci.HCI_LE_Set_Host_Channel_Classification_Command(
                    channels.to_bytes(5, 'little')
                ),
                tuple(range(256)),
            )
            if result.status == 0:
                break
            await tester.wait(5_000_000)
            while train.anchor <= tester.radio.now_us + 1000:
                train.next_event()
            enabled = [channel for channel in range(37) if requested >> channel & 1]
            channels = sum(
                1 << channel
                for channel in generator.sample(
                    enabled, generator.randint(2, len(enabled))
                )
            )
        else:
            pytest.skip('Inconclusive: twenty channel classifications rejected')
        instant = None
        after = 0
        for _ in range(12):
            if instant is not None and train.counter == instant:
                train.channel_map = channels
            for sub in range(10):
                fields = decode_extended(
                    (await pawr_peer.receive_indication(tester, train, sub)).pdu
                )
                acad = fields.get('acad', b'')
                offset = 0
                while offset < len(acad):
                    size = acad[offset]
                    assert size and offset + size < len(acad)
                    if acad[offset + 1] == 0x28:
                        value = acad[offset + 2 : offset + size + 1]
                        assert len(value) == 7
                        assert int.from_bytes(value[:5], 'little') == channels
                        update = int.from_bytes(value[5:7], 'little')
                        if instant is None:
                            assert 6 <= (update - train.counter) & 0xFFFF < 0x8000
                        else:
                            assert update == instant
                        instant = update
                    offset += size + 1
            if train.channel_map == channels:
                after += 1
                if after >= 2:
                    train.next_event()
                    break
            train.next_event()
        if instant is None:
            pytest.skip('Inconclusive: no Channel Map Update Indication')
        assert train.channel_map == channels and after >= 2
    await tester.command(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(0, [], [], [])
    )
    await pawr_peer.stop_advertising(tester)


# Verify the PAwR channel map update indication, its future Instant, and use of the
# old map before the Instant and the new map afterwards.
PROCEDURES |= procedure_cases(
    pawr_advertising_channel_map,
    {
        'LL/DDI/ADV/BV-90-C': (
            'AUX_SYNC_SUBEVENT_IND PDU with Channel Map Update Indication.',
            {},
        ),
    },
)


async def pawr_advertising_request_window(tester):
    """Periodic Advertising with Responses, Reject Subevent Data With Invalid Subevent.

    Initial Condition:
        128 subevents at 200 ms, PAI 25.6 s, RSD 10 ms and one response slot.

    Test Steps:
        1. Enable periodic/extended advertising and receive its SyncInfo.
        2. Disable extended advertising and observe consecutive data requests.
        3. After a new window overrides the old one, send the old window's
           first and last subevent numbers; then submit the current first one.
        4. Receive its IND on the independently calculated channel.

    Expected Outcome:
        Old-window data returns 0x0C. Current-window data succeeds and only
        that payload is transmitted. Every request count is below 128.

    Reference:
        LL.TS.p28, LL/DDI/ADV/BI-11-C, Steps 1-15; Core 7.7.65.36.
    """
    train, _ = await pawr_peer.advertiser(
        tester, interval=0x5000, timing=(128, 160, 8, 10), slots=1
    )
    await tester.command(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(0, [], [], [])
    )
    first = await pawr_peer.next_request(tester, limit_us=120_000_000)
    second = await pawr_peer.next_request(tester, limit_us=120_000_000)
    assert first.subevent_data_count < 128
    for sub in (
        first.subevent_start,
        first.subevent_start + first.subevent_data_count - 1,
    ):
        await tester.command(pawr_peer.SET_DATA(0, [sub], [0], [0], [b'old']), 0x0C)
    await tester.command(
        pawr_peer.SET_DATA(0, [second.subevent_start], [0], [0], [b'current'])
    )
    # The replacement window may span into the next periodic event.
    sub = second.subevent_start
    while train.anchor + sub * 200_000 <= tester.radio.now_us:
        train.next_event()
    packet = await pawr_peer.receive_indication(tester, train, sub)
    assert decode_extended(packet.pdu)['data'] == b'current'
    await pawr_peer.stop_advertising(tester)


# Verify rejection of subevent data outside the current request window and
# transmission of data accepted for the current window.
PROCEDURES |= procedure_cases(
    pawr_advertising_request_window,
    {
        'LL/DDI/ADV/BI-11-C': (
            'Periodic Advertising with Responses, Reject Subevent Data '
            'With Invalid Subevent.',
            {},
        ),
    },
)


async def pawr_advertising_future_response(tester):
    """Receive Subevent Response Data in Future Subevent, Advertiser.

    Initial Condition:
        Five subevents, SI 318.75 ms, RSD 317.5 ms, RSS 250 us and four slots.

    Test Steps:
        1. Select subevent 2 and the table's response subevent (4 or 1).
        2. Submit data only for subevent 2 but request four response slots
           for both; send one-octet responses in the future selected subevent.
        3. Exercise current-event subevent 4 and next-event subevent 1.
        4. Repeat after disabling extended advertising.

    Expected Outcome:
        Response Report uses the actual response subevent and slot, carries
        the exact response payload and a complete final Data_Status.

    Reference:
        LL.TS.p28, LL/DDI/ADV/BV-91-C, Table 4.2-45; Core 4.4.2.12.2.
    """
    generator = random.Random(0)
    for target in (4, 1):
        data = bytes(generator.randint(1, 254) for _ in range(3))
        data_seen = 0
        train, _ = await pawr_peer.advertiser(
            tester, interval=1275, timing=(5, 255, 254, 2), slots=4
        )
        for event in range(3):
            if event == 1:
                await tester.command(
                    hci.HCI_LE_Set_Extended_Advertising_Enable_Command(0, [], [], [])
                )
            for sub in range(5):
                for req in _pawr_take_all(tester, pawr_peer.REQUEST)[-1:]:
                    for selected in (2, target):
                        if (
                            req.subevent_start
                            <= selected
                            < req.subevent_start + req.subevent_data_count
                        ):
                            await tester.command(
                                pawr_peer.SET_DATA(
                                    0,
                                    [selected],
                                    [0],
                                    [4],
                                    [data if selected == 2 else b''],
                                )
                            )
                packet = await pawr_peer.receive_indication(tester, train, sub)
                fields = decode_extended(packet.pdu)
                if fields['data']:
                    assert sub == 2 and fields['data'] == data
                    data_seen += 1
                if sub == target and event:
                    slots = generator.sample(range(4), generator.randint(1, 4))
                    sent = {}
                    for slot in sorted(slots):
                        sent[slot] = bytes([generator.randint(1, 254)])
                        await _pawr_response_packet(
                            tester, train, packet, sub, slot, sent[slot]
                        )
                    # Wait for all four configured response windows to close.
                    end = packet.start_us + 317_500 + 4 * 250
                    await tester.wait(max(0, end - tester.radio.now_us))
                    reports = _pawr_take_all(tester, pawr_peer.RESPONSES)
                    for report in reports:
                        assert report.advertising_handle == 0
                        assert report.subevent in (2, target)
                        if report.subevent != target:
                            assert all(
                                entry.data_status == 0xFF and not entry.data
                                for entry in report.responses
                            )
                    received = pawr_peer.response_data(
                        [report for report in reports if report.subevent == sub],
                        sub,
                        set(range(4)),
                    )
                    assert received == sent
            train.next_event()
        assert data_seen, 'subevent-2 Host data was never verified'
        await pawr_peer.stop_advertising(tester)


# Verify reception of a response in a future PAwR subevent, including the reported
# subevent, response slot, exact payload and complete final data status.
PROCEDURES |= procedure_cases(
    pawr_advertising_future_response,
    {
        'LL/DDI/ADV/BV-91-C': (
            'Receive Subevent Response Data in Future Subevent, Advertiser.',
            {},
        ),
    },
)


def _pawr_initiate_command(*, peer=PEER_ADDRESS, own_type=0, subevent=5, phys=1):
    return hci.HCI_LE_Extended_Create_Connection_V2_Command(
        0,
        subevent,
        0,
        own_type,
        peer.address_type,
        peer,
        phys,
        [0x1111],
        [0x1111],
        [24],
        [24],
        [0],
        [100],
        [0],
        [0],
    )


async def _pawr_accept_request(tester, train, packet, *, own_address=DUT_ADDRESS):
    """Answer a PAwR AUX_CONNECT_REQ and serve the first ACL event."""
    pdu = packet.pdu
    assert pdu[0] & 15 == 5 and pdu[8:14] == bytes(PEER_ADDRESS)
    init = hci.Address(
        pdu[2:8],
        hci.AddressType.RANDOM_DEVICE if pdu[0] & 64 else hci.AddressType.PUBLIC_DEVICE,
    )
    await tester.transmit(
        csa2_channel(train.counter ^ 5, train.access_address, train.channel_map),
        ext_pdu(kind=8, address=PEER_ADDRESS, target=init),
        start_us=packet.end_us + 150,
        **train.parameters,
    )
    parameters = LinkParameters.decode(pdu[14:])
    link = PeerLink(tester, parameters, packet.end_us + 1250, central=True, csa2=True)
    await link.exchange()
    event = await tester.event(PAWR_CONNECTION_COMPLETE)
    assert (
        event.status == 0
        and event.advertising_handle == 0
        and event.sync_handle == 0xFFFF
    )
    link.handle = event.connection_handle
    return link


async def pawr_advertiser_connection(tester, *, random_address=False):
    """Reconnect with a Synchronized Device using PAwR, Public or Random Address.

    Initial Condition:
        A ten-subevent PAwR advertiser on LE 1M with SI 65 ms. The random
        variant uses distinct Set Random Address (ADDR1) and set address (ADDR2).

    Test Case Configuration:
        LL/DDI/ADV/BV-89-C: public address.
        LL/DDI/ADV/BV-110-C: random initiator and advertising addresses.

    Test Steps:
        1. Enable the train, read its announcements, and observe ten seconds.
        2. Request connection in subevent 5 but withhold AUX_CONNECT_RSP;
           require Enhanced Connection Complete v2 with status 0x3E.
        3. Repeat, answer at T_IFS and maintain the first ACL events.
        4. Disconnect, publish subevent data, and repeat successful initiation;
           the connect request must take precedence over queued data.
        5. While the first successful ACL is active, receive extended and
           periodic advertising and complete the Host-requested Feature
           Exchange. Then disconnect before testing the data variant.

    Expected Outcome:
        Connection requests use the train's AA/CRC and the chosen subevent.
        The random variant advertises ADDR2 but initiates with ADDR1.
        Successful v2 completion carries Advertising_Handle 0 and Sync_Handle
        0xFFFF; the advertiser remains active.

    Reference:
        LL.TS.p28, 4.2.3.18, LL/DDI/ADV/BV-89-C and BV-110-C,
        Table 4.2-43, Steps 1-34; Core 4.4.2.12.2.
    """
    addr1 = hci.Address('C0:11:22:33:44:01', hci.AddressType.RANDOM_DEVICE)
    addr2 = hci.Address('C0:11:22:33:44:02', hci.AddressType.RANDOM_DEVICE)
    if random_address:
        await tester.command(hci.HCI_LE_Set_Random_Address_Command(addr1))
    train, fields = await pawr_peer.advertiser(
        tester,
        interval=520,
        timing=(10, 52, 1, 10),
        own_type=int(random_address),
        random_address=addr2 if random_address else None,
    )
    if random_address:
        assert fields['address'] == bytes(addr2)
    await tester.wait(10_000_000)
    for attempt, success in enumerate((False, True, True)):
        while train.anchor + 5 * 65_000 <= tester.radio.now_us + 5000:
            train.next_event()
        if attempt == 2:
            # Source Steps 27-28 require a request covering subevent 5 and an
            # accepted data command before asking to replace it by CONNECT_REQ.
            _pawr_take_all(tester, pawr_peer.REQUEST)
            for _ in range(20):
                request = await pawr_peer.next_request(tester)
                if (
                    request.subevent_start
                    <= 5
                    < (request.subevent_start + request.subevent_data_count)
                ):
                    await tester.command(
                        pawr_peer.SET_DATA(0, [5], [0], [0], [b'\x5a'])
                    )
                    break
            else:
                raise AssertionError('no request covered the queued-data subevent')
            while train.anchor + 5 * 65_000 <= tester.radio.now_us + 5000:
                train.next_event()
        await tester.command(_pawr_initiate_command(own_type=int(random_address)))
        packet = await pawr_peer.receive_indication(tester, train, 5)
        assert packet.pdu[0] & 15 == 5
        assert packet.pdu[2:8] == bytes(addr1 if random_address else DUT_ADDRESS)
        if not success:
            await tester.wait(10 * 24 * 1250)
            event = await tester.event(PAWR_CONNECTION_COMPLETE)
            assert event.status == 0x3E
        else:
            link = await _pawr_accept_request(tester, train, packet)
            for _ in range(5):
                await link.exchange()
            if attempt == 1:
                session = TransferSession(tester, link, train, mode='receive')
                _, fields = await pawr_peer.connected_advertiser(session)
                assert fields['address'] == bytes(
                    addr2 if random_address else DUT_ADDRESS
                )
                await session.run_for(2 * train.interval * 1250)
                assert session.received, 'periodic advertising stopped while connected'
                await _iut_features(tester, link)
            await link.disconnect(local=True)
        train.next_event()
        while train.anchor <= tester.radio.now_us + 1000:
            train.next_event()
        assert (await pawr_peer.receive_indication(tester, train, 0)).pdu[0] & 15 == 7
    await pawr_peer.stop_advertising(tester)


# Verify PAwR connection initiation in the selected subevent with public and random
# addresses, failed and successful attempts, and continued periodic advertising.
PROCEDURES |= procedure_cases(
    pawr_advertiser_connection,
    {
        'LL/DDI/ADV/BV-89-C': (
            'Reconnect with a Synchronized Device using PAwR, Public or '
            'Random Address.',
            {},
        ),
        'LL/DDI/ADV/BV-110-C': (
            'Reconnect with a Synchronized Device using PAwR, Public or '
            'Random Address.',
            {'random_address': True},
        ),
    },
)


async def pawr_advertiser_existing_connection(tester):
    """Reject Connection Request When Connected to a Peer, PAwR advertiser.

    Initial Condition:
        The IUT and Lower Tester have an existing extended ACL connection.

    Test Steps:
        1. Enable a ten-subevent PAwR train while maintaining the ACL link.
        2. After ten seconds, request another connection to the same peer
           through subevent 5 with the source's selected initiating PHY.
        3. Observe the asynchronous command's error and maintain the ACL link.

    Expected Outcome:
        Source alternative 11B returns a nonzero Command Status. No second
        connection replaces or disconnects the existing peer.

    Reference:
        LL.TS.p28, LL/DDI/ADV/BI-09-C, Steps 1-11; Core 7.8.66.
    """
    link = await establish(tester, central=True, extended=True)
    await pawr_peer.configure(tester, timing=(10, 52, 1, 10), interval=520)
    session = TransferSession(tester, link, mode='off')
    await session.run_for(10_000_000)
    await session.command(_pawr_initiate_command(phys=2), status=(0x0B, 0x12))
    for _ in range(5):
        await session.step()
    await pawr_peer.stop_advertising(tester)
    await link.disconnect(local=True)


# Verify that a PAwR advertiser rejects another connection to an already connected
# peer while preserving the existing ACL connection.
PROCEDURES |= procedure_cases(
    pawr_advertiser_existing_connection,
    {
        'LL/DDI/ADV/BI-09-C': (
            'Reject Connection Request When Connected to a Peer, PAwR advertiser.',
            {},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_periodic_advertising(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
