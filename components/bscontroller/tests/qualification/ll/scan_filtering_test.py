# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.2: Scan filtering, privacy and advertiser monitoring."""

import random

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_ADV_DELAY_MAX_US,
    LL_ADV_PRIMARY_CHANNELS,
    LL_IFS_US,
    LL_MAFS_US,
    PDU_ADV_ADI_DID_BITS,
    PDU_ADV_ADI_DID_MASK,
    PDU_ADV_DECISION_RESOLVABLE_TAG_MASK,
    PDU_ADV_EXT_AUX_PTR_MASK,
    PDU_ADV_TYPE_MASK,
    AdvertisingMode,
    AdvertisingPduType,
)
from bscontroller.radio.base import Phy
from tests.qualification import privacy as privacy_peer
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.decision import TAG_KEY, address_parameters, flag_vectors
from tests.qualification.discovery import (
    RESPONSE_IFS_TOLERANCE_US,
    ExtendedReport,
    LegacyEventType,
    LegacyReport,
    Report,
    _enable_legacy_scan,
    _scan_compatibility_Flags,
    _scan_phase,
    _scan_reports,
)
from tests.qualification.link import (
    ADVERTISING_SID,
    AUXILIARY_CHANNEL,
    AUXILIARY_OFFSET_US,
    REPORT_DELIVERY_US,
    SCAN_INTERVAL_US,
    SCANNING_ADVERTISING_INTERVAL_US,
    SCANNING_SCAN_INTERVAL,
)
from tests.qualification.tester import (
    DUT_ADDRESS,
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
    PEER_ADDRESS,
    adv_pdu,
    aux_pointer,
    ext_pdu,
    request_pdu,
    scan_parameters,
)

PROCEDURES = ProcedureMap()

# LL/DDI/SCN/BV-127-C Step 10: withhold responses for ten scan intervals.
DUPLICATE_LEGACY_WAIT_INTERVALS = 10

# LL/DDI/SCN/BV-128-C Step 8: withhold responses for five advertising intervals.
DUPLICATE_EXTENDED_WAIT_INTERVALS = 5

# Both procedures continue responding for 128 scan intervals after the first report.
DUPLICATE_OBSERVATION_SCAN_INTERVALS = 128


async def _duplicate_active_scanning(tester, *, extended):
    """Duplicate filtering must not prevent scan retries after initial absent
    responses."""
    peer = LOWER_PERIPHERAL_PUBLIC_ADDRESS
    interval = (
        SCANNING_SCAN_INTERVAL
        if extended
        else SCAN_INTERVAL_US // hci.HCI_LE_SCAN_INTERVAL_UNIT_US
    )
    interval_us = interval * hci.HCI_LE_SCAN_INTERVAL_UNIT_US
    if not extended:
        # Provision the initial list; source Step 3 then selects accept-all scanning.
        await tester.command(hci.HCI_LE_Clear_Filter_Accept_List_Command())
        await tester.command(
            hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(peer.address_type, peer)
        )
    await tester.command(
        scan_parameters(
            extended=extended,
            **(
                {
                    'scan_types': [1],
                    'scan_intervals': [interval],
                    'scan_windows': [interval],
                }
                if extended
                else {
                    'le_scan_type': 1,
                    'le_scan_interval': interval,
                    'le_scan_window': interval,
                }
            ),
        )
    )
    await tester.command(
        hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 1, 0, 0)
        if extended
        else hci.HCI_LE_Set_Scan_Enable_Command(1, 1)
    )
    adi = ADVERTISING_SID << PDU_ADV_ADI_DID_BITS
    start = tester.radio.now_us + REPORT_DELIVERY_US
    first_request_us = None
    first_request_event = 0
    unanswered_requests = sent_responses = 0
    observation_end = None
    ads = responses = 0
    generator = random.Random(0)
    for event_number in range(10000):
        channel = 37
        if extended:
            await tester.transmit(
                channel,
                ext_pdu(
                    adi=adi,
                    mode=AdvertisingMode.SCANNABLE,
                    aux=aux_pointer(AUXILIARY_CHANNEL, AUXILIARY_OFFSET_US),
                ),
                start_us=start,
            )
            channel = AUXILIARY_CHANNEL
            end = await tester.transmit(
                channel,
                ext_pdu(address=peer, adi=adi, mode=AdvertisingMode.SCANNABLE),
                start_us=start + AUXILIARY_OFFSET_US,
            )
        else:
            end = await tester.transmit(
                channel,
                adv_pdu(AdvertisingPduType.ADV_IND, address=peer, data=b''),
                start_us=start,
            )
        request = await tester.receive(channel, duration_us=1000)
        if request:
            assert request.valid_crc and request.pdu == request_pdu(
                AdvertisingPduType.SCAN_REQ, peer, scanner=DUT_ADDRESS
            )
            assert abs(request.start_us - end - LL_IFS_US) <= RESPONSE_IFS_TOLERANCE_US
            if first_request_us is None:
                first_request_us = request.end_us
                first_request_event = event_number
                tester.output.write(
                    f'DUPLICATE WAIT start_us={first_request_us} event={event_number}\n'
                )
            response_due = (
                event_number - first_request_event >= DUPLICATE_EXTENDED_WAIT_INTERVALS
                if extended
                else request.start_us - first_request_us
                >= DUPLICATE_LEGACY_WAIT_INTERVALS * interval_us
            )
            if response_due:
                if not sent_responses:
                    tester.output.write(
                        f'DUPLICATE FIRST RESPONSE request_us={request.start_us} '
                        f'event={event_number}\n'
                    )
                await tester.transmit(
                    channel,
                    (
                        ext_pdu(address=peer, data=b'A')
                        if extended
                        else adv_pdu(
                            AdvertisingPduType.SCAN_RSP, address=peer, data=b'A'
                        )
                    ),
                    start_us=request.end_us + LL_IFS_US,
                )
                sent_responses += 1
            else:
                unanswered_requests += 1
        await tester.wait(REPORT_DELIVERY_US)
        while event := tester.take(
            lambda event: isinstance(event, Report if extended else LegacyReport)
        ):
            for report in event.reports:
                assert (
                    report.address == peer
                    and report.address_type == hci.AddressType.PUBLIC_DEVICE
                )
                scan_response = (
                    bool(report.event_type & _scan_compatibility_Flags.SCAN_RESPONSE)
                    if extended
                    else report.event_type == LegacyEventType.SCAN_RSP
                )
                assert report.event_type == (
                    _scan_compatibility_Flags.SCANNABLE_ADVERTISING
                    | (_scan_compatibility_Flags.SCAN_RESPONSE if scan_response else 0)
                    if extended
                    else (
                        LegacyEventType.SCAN_RSP
                        if scan_response
                        else LegacyEventType.ADV_IND
                    )
                )
                if extended:
                    assert report.primary_phy == report.secondary_phy == Phy.LE_1M
                    assert (
                        report.advertising_sid == ADVERTISING_SID
                        and report.periodic_advertising_interval == 0
                    )
                assert report.data == (b'A' if scan_response else b'')
                if scan_response:
                    assert ads and unanswered_requests and sent_responses
                    responses += 1
                    if observation_end is None:
                        observation_end = (
                            tester.radio.now_us
                            + DUPLICATE_OBSERVATION_SCAN_INTERVALS * interval_us
                        )
                else:
                    ads += 1
        if observation_end is not None and tester.radio.now_us >= observation_end:
            break
        start = max(
            start
            + SCANNING_ADVERTISING_INTERVAL_US
            + generator.randrange(LL_ADV_DELAY_MAX_US + 1),
            tester.radio.now_us + LL_MAFS_US,
        )
    assert (
        ads
        and responses
        and (observation_end is not None)
        and (tester.radio.now_us >= observation_end)
    )
    if ads > 1 or responses > 1:
        tester.output.write(
            f'WARNING duplicate reports:advertising={ads},scan_response={responses}\n'
        )
    await tester.command(
        hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0)
        if extended
        else hci.HCI_LE_Set_Scan_Enable_Command(0, 0)
    )


async def passive_scanning_device_filtering(tester):
    """Passive Scanning Device Filtering.

    Initial Condition:
        Passive scanning uses basic filtered policy 0x01 and a Filter Accept List
        containing one public address. Select channel 37, a 20-ms advertising base
        with a 5-ms mean AdvDelay, and a 75-ms scan interval/window. The interval
        is three times the mean advertising interval. Keep scanning enabled
        across all four address/type phases; disable duplicate filtering.

    Test Steps:
        1. Enable passive scanning with policy 0x01, ignoring unlisted devices.
        2. Configure the Lower Tester on the selected channel with the public
           address present in the Filter Accept List.
        3. Send ADV_NONCONN_IND once per selected advertising interval until
           twenty scan intervals pass or Step 4 completes.
        4. Receive LE Advertising Report for non-connectable advertising with the
           listed public address/type and a transmitted packet's data.
        5. Stop Lower Tester advertising before changing its address.
        6. Configure the same address bytes with random address type.
        7. Send ADV_NONCONN_IND for twenty scan intervals.
        8. Stop Lower Tester advertising.
        9. Configure an unlisted public address differing from the IUT in the
           least significant octet.
        10. Repeat Steps 7-8.
        11. Configure an unlisted random address differing from the IUT in both
            the most and least significant octets.
        12. Repeat Steps 7-8.
        13. Disable LE scanning with LE Set Scan Enable; require Command Complete.

    Expected Outcome:
        Pass verdict: complete the selected-channel/timing procedure, reporting the
        listed public advertiser and its exact data. Reject identical address
        bytes with the wrong type and both unlisted addresses for the full
        twenty-interval phases. Passive scanning must not transmit SCAN_REQ.

    Source Correction:
        Step 13 names BR/EDR Write Scan Enable; use LE Set Scan Enable to stop
        LE scanning. This local command-name correction follows the Core LE
        scanning model and was confirmed with Amber, not an adopted SIG erratum.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4, unnumbered procedure, LL/DDI/SCN/BV-02-C; PDF
        pages 271-273, printed pages 4.2-178-4.2-180, Figure 4.2-78,
        Steps 1-13 and the complete pass verdict. Core Specification 6.3,
        Vol 4, Part E, 7.8.10/7.8.11 define LE scan control.
    """
    await _scan_reports(
        tester, kind=AdvertisingPduType.ADV_NONCONN_IND, active=False, filtered=True
    )


# Verify that passive scanning reports a specific advertiser through address/type
# filtering. Non-connectable advertising on one selected channel carries the packet
# sequence number as data.
PROCEDURES |= procedure_cases(
    passive_scanning_device_filtering,
    {
        'LL/DDI/SCN/BV-02-C': (
            'Passive Scanning Device Filtering',
            {},
        ),
    },
)


async def active_scanning_device_filtering(tester):
    """Active Scanning Device Filtering.

    Initial Condition:
        Active scanning uses a public own address, basic filtered policy 0x01 and
        one public entry in the Filter Accept List. Select a 20-ms advertising
        base with 5-ms mean AdvDelay, and 75-ms scan interval/window, three times
        the mean advertising interval. Keep scanning enabled while changing the
        three peer addresses on each channel; disable duplicate filtering.

    Test Steps:
        1. Enable active scanning with policy 0x01, ignoring unlisted devices.
        2. Select the first supported Lower Tester advertising channel (37).
        3. Configure the listed public advertiser and its scan-response data.
        4. Send ADV_IND on that channel until twenty scan intervals pass or the
           Step 5 scan exchange occurs.
        5. Receive SCAN_REQ at T_IFS after ADV_IND and send SCAN_RSP at T_IFS
           after the request. Validate CRC, PHY, addresses/types and 2-us spacing.
        6. Interleaved with Step 4, receive LE Advertising Report for ADV_IND.
        7. Interleaved with Step 5, receive LE Advertising Report for SCAN_RSP.
        8. Configure an unlisted public advertiser differing from the IUT in its
           least significant octet, with scan-response data available.
        9. Send ADV_IND on the selected channel for twenty scan intervals.
        10. Require no SCAN_REQ at T_IFS after any advertisement.
        11. Configure an unlisted public advertiser differing in both endpoint
            octets, with scan-response data available.
        12. Repeat the transmission/no-response exercise of Steps 9-10 using the
            Step 11 address; do not overwrite it with the Step 8 address.
        13. Disable LE scanning with LE Set Scan Enable; require Command Complete.
        14. Select the second supported advertising channel (38).
        15. Repeat the enable and Steps 3-13 on the newly selected channel.
        16. Select the third supported advertising channel (39).
        17. Repeat the enable and Steps 3-13 on the newly selected channel.

    Expected Outcome:
        Pass verdict: complete every supported channel/timing phase. The IUT
        requests and reports information from the listed advertiser. For each
        unlisted peer require no SCAN_REQ or advertising/scan-response report
        throughout the twenty-interval phase. Listed-peer reports match exact
        address/type/data; report order/grouping is not constrained.

    Source Correction:
        Step 4's self-reference means the exchange in Step 5. Steps 6/7 associate
        the reports with transmissions in Steps 4/5, not configuration/incorrect
        response references. Step 12 must preserve Step 11's third address;
        repeating Step 8 would replace it with the earlier address. Steps 15/17
        must preserve the channel selected in 14/16 and re-enable scanning after
        Step 13; repeating the original first-channel setup would erase that
        selection. Step 13 names BR/EDR Write Scan Enable; use LE Set Scan Enable.
        Amber supports these local numbering/command corrections against the
        source's explicit address, channel and LE filtering intent; they are not
        adopted SIG errata.

    Reference:
        LL.TS.p28.pdf, Section 4.2.4, unnumbered procedure, LL/DDI/SCN/BV-04-C; PDF
        pages 275-278, printed pages 4.2-182-4.2-185, Figure 4.2-80,
        Steps 1-17 and pass verdict. Core Specification 6.3, Vol 4, Part E,
        7.8.10/7.8.11 define the LE scan commands.
    """
    await _scan_reports(
        tester, kind=AdvertisingPduType.ADV_IND, active=True, filtered=True
    )


# Verify that active scanning requests and reports additional information only for the
# advertiser permitted by the filtering policy. The Lower Tester sends connectable
# undirected advertising with event-counter data, one selected channel at a time, and
# responds only when requested.
PROCEDURES |= procedure_cases(
    active_scanning_device_filtering,
    {
        'LL/DDI/SCN/BV-04-C': (
            'Active Scanning Device Filtering',
            {},
        ),
    },
)


async def ignore_directed_advertising_address_not_in_filter_accept_list(tester):
    """Ignore Directed Advertising, Address Not In Filter Accept List.

    Initial Condition:
        The IUT has a configured public device address. The Lower Tester can
        transmit ADV_DIRECT_IND using the common public advertiser address and
        a different public address. Both phases keep TargetA equal to the IUT.
        Use passive scanning with Filter Accept List filtering and no duplicate
        filtering. The continuous scan window equals the scan interval, which
        is three times the mean minimum-base advertising interval including
        AdvDelay. Observe each phase for twenty complete scan intervals.

    Test Steps:
        1. Configure directed advertising with the common public AdvA and the
           IUT's public address as TargetA.
        2. Add the public advertiser to the Filter Accept List and require
           successful Command Complete.
        3. Configure legacy scanning with passive scan type, public own address,
           filter policy 0x01, and the continuous interval/window above.
           Require successful Command Complete.
        4. Enable scanning without duplicate filtering and require successful
           Command Complete.
        5. Send ADV_DIRECT_IND on one selected primary channel for twenty scan
           intervals, using minimum-base advertising plus seeded AdvDelay.
        6. Require at least one LE Advertising Report with directed event type,
           the listed public advertiser address/type, and no advertising data.
           Reject any scan request throughout the observation window.
        7. Disable scanning and require successful Command Complete.
        8. Change AdvA to a different public address absent from the list;
           preserve TargetA, filter policy, and scan timing.
        9. Repeat the enable, twenty-interval observation, and disable sequence.
           Require no advertising reports or scan requests. Repeat both phases
           independently on each supported primary advertising channel.

    Expected Outcome:
        Pass verdict:
            Advertisements with the listed public AdvA are reported correctly.
            Changing only AdvA to an unlisted public address prevents reports,
            despite TargetA still matching the IUT. The scanner remains passive
            in both phases and the observation covers the full required time.

    Reference:
        LL.TS.p28.pdf, LL/DDI/SCN/BV-90-C, unnumbered procedure following
        Section 4.2.4.24; PDF pages 399-401, printed pages 4.2-306 through
        4.2-308, Figure 4.2-125, Steps 1-9 and Expected Outcome.
        Source reference [3] 4.4.3.1.
    """
    common, different = (LOWER_PERIPHERAL_PUBLIC_ADDRESS, LOWER_CENTRAL_PUBLIC_ADDRESS)
    await tester.command(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(common.address_type, common)
    )
    for channel in LL_ADV_PRIMARY_CHANNELS:
        for peer, accepted in ((common, True), (different, False)):
            await _enable_legacy_scan(tester, filtered=True)
            await _scan_phase(
                tester,
                channel,
                peer,
                AdvertisingPduType.ADV_DIRECT_IND,
                active=False,
                accepted=accepted,
                full_duration=True,
            )
            await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(0, 0))
            assert not any(
                isinstance(event, (LegacyReport, Report)) for event in tester.events
            )


# Verify that a scanner ignores directed advertisements whose AdvA is not in its Filter
# Accept List, even when TargetA matches the IUT.
PROCEDURES |= procedure_cases(
    ignore_directed_advertising_address_not_in_filter_accept_list,
    {
        'LL/DDI/SCN/BV-90-C': (
            'Ignore Directed Advertising, Address Not In Filter Accept List',
            {},
        ),
    },
)


async def decision_based_advertisements_ignored_legacy_scanning(tester):
    """Decision-Based Advertisements Ignored, Legacy Scanning.

    Initial Condition:
        Use public, accept-all, passive legacy scanning with interval/window
        0x0010. The independent Lower Tester can send a valid ADV_DECISION_IND
        with Decision Type Flags bit 0 set, a six-octet Resolvable Tag, and an
        AuxPtr referring to AUX_ADV_IND. No shared tag-resolution key is needed
        for this negative legacy-scanning test.

    Test Steps:
        1. Send a Decision PDU followed by its indicated AUX_ADV_IND. The first
           payload octet contains Decision Type Flags 0b000001 and AdvMode
           0b00, followed by Extended Header Flags and the three-octet AuxPtr,
           then the six-octet tag. Do not insert the ExtHdrLen used by ordinary
           ADV_EXT_IND; the Decision PDU has a different payload format.
        2. Send HCI LE Set Scan Parameters with LE_Scan_Type=0x00,
           LE_Scan_Interval=LE_Scan_Window=0x0010 and
           Scanning_Filter_Policy=0b0000; receive successful Command Complete.
        3. Send HCI LE Set Scan Enable with LE_Scan_Enable=0x01; receive
           successful Command Complete. Duplicate filtering is disabled.
        4. Continue Decision PDU/AUX_ADV_IND pairs with minimum-base intervals
           plus seeded AdvDelay for ten complete simulated seconds. Require
           no legacy or extended advertising reports and no scan requests.
           The auxiliary packet carries a valid public AdvA and ADI.

    Additional Controls and Postamble:
        After Step 3 and before the negative window, require a normal ADV_IND
        report with matching public identity/type and data. Repeat this control
        after Step 4 to prove that reception remains functional. These controls
        are outside the ten-second negative window. Finally disable scanning.

    Expected Outcome:
        Pass verdict:
            No LE Advertising Report is generated during the source Step 4
            ten-second window. Additional checks reject extended reports and
            scan requests, and require normal legacy reception before and
            after the window. Positive controls are not part of the negative
            observation period.

    Source Correction:
        Source Step 1 calls the primary PDU AUX_DECISION_IND. Core Vol 6,
        Part B, Section 2.3, Table 2.3 defines ADV_DECISION_IND (type 0b1001),
        and Section 2.3.1.11 requires its auxiliary PDU to be AUX_ADV_IND. Use
        those defined PDU types; this naming
        correction does not change the test's legacy-scanning verdict. Amber
        corroborated the defined names; this is not adopted SIG errata.
        The source purpose restricts applicability to unsupported controllers,
        although the title and numbered procedure test legacy scanning. Execute
        that procedure on this capable IUT: legacy Scanning_Filter_Policy values
        0x00-0x03 (Vol 4, Part E, 7.8.10) keep the "No decisions" mode of Core
        Vol 6, Part B, 4.3.3.2, and Vol 4, Part E, 7.8.11 issues no reports for
        extended advertising PDUs.

    Reference:
        LL.TS.p28.pdf, LL/DDI/SCN/BV-104-C, unnumbered procedure following
        Section 4.2.4.27; PDF pages 439-440, printed pages 4.2-346 through
        4.2-347, Figure 4.2-140, Steps 1-4 and Expected Outcome.
        Source reference [25] 4.4.3.6. Core Vol 6, Part B,
        Section 2.3.1.11, Figures 2.10-2.12 and Tables 2.10-2.11 define the
        Decision PDU, permitted extended fields, flags, and Resolvable Tag.
    """
    decision = (
        bytes([PDU_ADV_DECISION_RESOLVABLE_TAG_MASK, PDU_ADV_EXT_AUX_PTR_MASK])
        + aux_pointer(AUXILIARY_CHANNEL, AUXILIARY_OFFSET_US)
        + bytes.fromhex('123456789a4b')
    )
    wire = bytes([AdvertisingPduType.ADV_DECISION_IND, len(decision)]) + decision
    auxiliary = ext_pdu(
        address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
        adi=ADVERTISING_SID << PDU_ADV_ADI_DID_BITS,
        data=b'ignored',
    )
    start = tester.radio.now_us + REPORT_DELIVERY_US
    await tester.transmit(37, wire, start_us=start)
    await tester.transmit(
        AUXILIARY_CHANNEL, auxiliary, start_us=start + AUXILIARY_OFFSET_US
    )
    await tester.command(
        scan_parameters(
            le_scan_type=0,
            le_scan_interval=SCANNING_SCAN_INTERVAL,
            le_scan_window=SCANNING_SCAN_INTERVAL,
        )
    )
    await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(1, 0))

    async def positive():
        for _ in range(30):
            await tester.transmit(
                37, adv_pdu(address=LOWER_PERIPHERAL_PUBLIC_ADDRESS, data=b'positive')
            )
            await tester.wait(SCANNING_ADVERTISING_INTERVAL_US)
            if event := tester.take(lambda event: isinstance(event, LegacyReport)):
                assert event.reports[0].event_type == LegacyEventType.ADV_IND
                assert event.reports[0].address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
                assert event.reports[0].address_type == hci.AddressType.PUBLIC_DEVICE
                assert event.reports[0].data == b'positive'
                return
        raise AssertionError('legacy scanner positive control absent')

    await positive()
    generator = random.Random(0)
    start = tester.radio.now_us + REPORT_DELIVERY_US
    deadline = start + 10000000
    while start <= deadline:
        await tester.transmit(37, wire, start_us=start)
        await tester.transmit(
            AUXILIARY_CHANNEL,
            auxiliary,
            start_us=start + AUXILIARY_OFFSET_US,
        )
        assert await tester.receive(AUXILIARY_CHANNEL, duration_us=1000) is None
        assert not any(
            isinstance(event, (LegacyReport, Report)) for event in tester.events
        )
        start = max(
            start
            + SCANNING_ADVERTISING_INTERVAL_US
            + generator.randrange(LL_ADV_DELAY_MAX_US + 1),
            tester.radio.now_us + LL_MAFS_US,
        )
    await tester.wait(max(0, deadline - tester.radio.now_us))
    assert not any(isinstance(event, (LegacyReport, Report)) for event in tester.events)
    await positive()
    await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(0, 0))


# Verify that a legacy passive scanner without Decision-Based Advertising Filtering
# support ignores Decision PDUs rather than reporting them as legacy advertisements.
PROCEDURES |= procedure_cases(
    decision_based_advertisements_ignored_legacy_scanning,
    {
        'LL/DDI/SCN/BV-104-C': (
            'Decision-Based Advertisements Ignored, Legacy Scanning',
            {},
        ),
    },
)


async def active_scanning_duplicate_filtering(tester):
    """Active Scanning, Duplicate Filtering.

    Initial Condition:
        The IUT uses its public address and active legacy scanning. Populate
        the Filter Accept List with the Lower Tester's common public address,
        as required by the source precondition. Source Step 3 then explicitly
        selects accept-all filtering; the list does not filter this procedure.

        Use the first supported primary advertising channel and minimum-base
        advertising with seeded AdvDelay. The scan interval is three times the
        mean advertising interval, including AdvDelay; the scan window equals
        that interval. Keep the advertiser address, empty AdvData, and response
        data unchanged throughout the test.

    Test Steps:
        1. Configure the Lower Tester channel map to the first supported
           advertising channel.
        2. Set its common advertiser address with public address type.
        3. Send HCI LE Set Scan Parameters with LE_Scan_Type=0x01 and
           Scanning_Filter_Policy=0x00; receive successful Command Complete.
           Select public addressing and the continuous interval/window above.
        4. Enable legacy scanning with Enable=0x01 and Filter_Duplicates=0x01;
           receive successful Command Complete.
        5. Send identical ADV_IND on the selected channel each advertising event.
        6. Require at least one HCI LE Advertising Report with the Lower Tester
           address and Event_Type=0x00 (see Source Correction). Additional
           advertising reports produce a warning, not a failure.
        7. Receive SCAN_REQ in response to Step 5. Verify CRC, public IUT ScanA,
           public Lower Tester AdvA and T_IFS within the two-microsecond tolerance.
        8. Withhold SCAN_RSP.
        9. Require no scan-response report before any SCAN_RSP is transmitted.
           Additional advertising reports are warnings, as in Steps 6 and 12.C.
        10. After ten complete scan intervals from the first unanswered request,
            respond to the next SCAN_REQ at T_IFS with SCAN_RSP AdvData='A'.
        11. Require at least one Advertising Report with Event_Type=0x04,
            Address=Lower Tester, Data_Length=1 and Data='A'. Additional reports
            produce a warning, not a failure.
        12. Continue unchanged advertising for 128 scan intervals after the
            first response report. If another SCAN_REQ is sent:
            12.A. Receive SCAN_REQ in response to ADV_IND from Step 5.
            12.B. Send SCAN_RSP with the same public AdvA and AdvData='A' at T_IFS.
            12.C. Issue a warning for further reports with the Lower Tester
                  address. Validate every report's type, address and data.

    Postamble:
        Disable legacy scanning and receive successful Command Complete.

    Expected Outcome:
        Pass verdict:
            At least one LE Advertising Report identifies the Lower Tester with
            Event_Type 0x00 for ADV_IND, and at least one later report has
            Event_Type 0x04 with the single data octet 'A'. No scan-response
            report is accepted before an actual response is transmitted. The
            scanner retries after the unanswered phase despite duplicate
            filtering. Duplicate reports are recorded as warnings.

    Source Correction:
        Source Step 6 says Event_Type 0x01, but ADV_IND is reported as 0x00,
        consistent with the source Expected Outcome and HCI LE Advertising
        Report. The filtered initial condition does not override the explicit
        policy 0x00 in Step 3. Provision the initial list, then use that policy.
        Core Vol 4, Part E, 7.7.65.2 and 7.8.10 define these values;
        these are local interpretations, not adopted SIG errata.

        Step 9's blanket report prohibition is a local wording correction:
        apply it to scan-response reports during the unanswered-request phase.
        Core Vol 6, Part B, 4.4.3.5 requires separate duplicate handling for
        advertising and scan-response reports and explicitly permits duplicates.
        This also agrees with the source's warning-only duplicate checks.
        Require an actual transmitted scan response before accepting its report;
        do not turn this correction into a suppression of advertising reports.

    Reference:
        LL.TS.p28.pdf, LL/DDI/SCN/BV-127-C, unnumbered procedure following
        Section 4.2.4.30; PDF pages 463-465, printed pages 4.2-370 through
        4.2-372, Figure 4.2-152. Steps 1-12, including 12.A-C, and Expected
        Outcome; source reference [3] 4.4.3.2. Core Vol 4, Part E,
        Sections 7.7.65.2, 7.8.10, and 7.8.11 define legacy reports, scan
        filter policy, and duplicate filtering.
    """
    await _duplicate_active_scanning(tester, extended=False)


# Verify that duplicate filtering suppresses repeated legacy advertising and
# scan-response reports without preventing scan-request retries after the Lower Tester
# initially withholds responses.
PROCEDURES |= procedure_cases(
    active_scanning_duplicate_filtering,
    {
        'LL/DDI/SCN/BV-127-C': ('Active Scanning, Duplicate Filtering', {}),
    },
)


async def extended_scanning_active_duplicate_filtering(tester):
    """Extended Scanning, Active, Duplicate Filtering.

    Initial Condition:
        The IUT uses its public address and active LE 1M extended scanning with
        accept-all filtering and interval/window 0x0010. Scan_Max_Data is at
        least one octet. The Lower Tester uses a fixed public AdvA and SID/DID,
        scannable nonconnectable extended advertising on one selected primary
        channel, and LE 1M auxiliary packets.

    Test Steps:
        1. Send HCI LE Set Extended Scan Parameters with Scan_Type[0]=0x01,
           Scan_Interval[0]=Scan_Window[0]=0x0010, Own_Address_Type=0x00 and
           Scanning_Filter_Policy=0x00. Select LE 1M; receive successful
           HCI Command Complete.
        2. Enable extended scanning with Filter_Duplicates=0x01; receive
           successful Command Complete. Duration and Period are zero.
        3. Begin LE 1M ADV_EXT_IND with AuxPtr referring to scannable
           AUX_ADV_IND carrying the public Lower Tester AdvA and fixed ADI.
        4. Require at least one HCI LE Extended Advertising Report with
           Event_Type=0x02 and Address=Lower Tester. Warn for additional reports;
           verify address type, LE 1M PHYs, SID and zero periodic interval.
        5. Receive AUX_SCAN_REQ for the undirected advertisements from Step 3.
           Verify CRC, exact public ScanA/AdvA and T_IFS timing.
        6. Withhold AUX_SCAN_RSP.
        7. Require no scan-response report before any AUX_SCAN_RSP is sent.
           Additional advertising reports are warnings, as in Steps 4 and 11C.
        8. Repeat Steps 5-7 for five actual advertising intervals after the
           first unanswered request, including AdvDelay in the interval count.
        9. Respond to the next valid AUX_SCAN_REQ at T_IFS with AUX_SCAN_RSP,
           preserving the public AdvA and setting the single AdvData octet 'A'.
        10. Require at least one Extended Advertising Report with
            Event_Type=0x0A, Address=Lower Tester, Data_Length=1 and Data='A'.
            Warn for additional reports and validate their complete identity,
            PHYs, SID and zero periodic interval.
        11. Continue unchanged advertising for 128 scan intervals after the
            first response report. If another AUX_SCAN_REQ is sent:
            11A. Receive AUX_SCAN_REQ from the IUT.
            11B. Send AUX_SCAN_RSP at T_IFS with the same AdvA and AdvData='A'.
            11C. Warn if the IUT sends further Extended Advertising Reports
                 with Address=Lower Tester. Validate all reported fields/data.

    Postamble:
        Disable extended scanning and receive successful Command Complete.

    Expected Outcome:
        Pass verdict:
            At least one correct scannable advertising report (0x02) and one
            scan-response report (0x0A) identify the same Lower Tester. Response
            data are exactly the single octet 'A'. No scan-response report is
            accepted before transmission of a real response. Duplicate
            filtering does not prevent retries after unanswered requests;
            additional duplicate reports are warnings, not automatic failures.

    Source Correction:
        The source Expected Outcome says 0x03, but Step 4 and the scannable,
        nonconnectable event require 0x02. Source Steps 9 and 11B say AdvA='A';
        preserve the six-octet advertiser address and put 'A' in AdvData, as
        required by Step 10 and Core Vol 6, Part B, 2.3.2.3. These are local
        corrections, not adopted SIG errata.

        Step 7's blanket report prohibition is a local wording correction:
        apply it to scan-response reports during the unanswered-request phase.
        Core Vol 6, Part B, 4.4.3.5 requires separate duplicate handling for
        advertising and scan-response reports and explicitly permits duplicates.
        This also agrees with the source's warning-only duplicate checks.
        Require an actual transmitted scan response before accepting its report;
        do not turn this correction into a suppression of advertising reports.

    Reference:
        LL.TS.p28.pdf, LL/DDI/SCN/BV-128-C, unnumbered procedure following
        Section 4.2.4.30; PDF pages 466-468, printed pages 4.2-373 through
        4.2-375, Figure 4.2-153. Steps 1-11, including 11A-C, and Expected
        Outcome; source reference [10] 4.4.3.2. Core Vol 4, Part E,
        Sections 7.7.65.13, 7.8.64, and 7.8.65 define extended reports and
        extended scanning; Vol 6, Part B, Section 2.3.2.3 defines AUX_SCAN_RSP.
    """
    await _duplicate_active_scanning(tester, extended=True)


# Verify that duplicate filtering suppresses repeated extended advertising and
# scan-response reports without preventing retries after initial unanswered AUX_SCAN_REQ
# packets.
PROCEDURES |= procedure_cases(
    extended_scanning_active_duplicate_filtering,
    {
        'LL/DDI/SCN/BV-128-C': ('Extended Scanning, Active, Duplicate Filtering', {}),
    },
)


async def privacy_passive_scanning(tester, *, identity_only=False, device=False):
    """Passive scanning resolves RPAs and applies network/device privacy mode.

    Initial Condition:
        Standby IUT uses a private fallback, an entry with the peer IRK, and
        unfiltered passive scanning. Device mode is selected only for BV-28.

    Test Case Configuration:
        LL/DDI/SCN/BV-13-C: RPA advertiser, Network Privacy Mode.
        LL/DDI/SCN/BV-26-C: identity-address advertiser, Network Privacy Mode.
        LL/DDI/SCN/BV-28-C: identity-address advertiser, Device Privacy Mode.

    Test Steps:
        BV-13 (steps 1-11): add the identity/IRK and enable resolution, advertise
        an RPA for at least 20 advertising intervals, verify resolved reports,
        stop scanning, disable resolution, repeat and verify raw RPA reports.
        BV-26/28 (steps 1-6): populate the resolver, scan, advertise the identity
        for at least 20 intervals, check absence/presence according to Table
        4.2-66, and stop scanning.

    Expected Outcome:
        BV-13 reports the identity with resolution enabled and raw RPA when
        disabled. Network mode rejects identities; device mode accepts them.

    Reference:
        LL.TS.p28, DDI/SCN/BV-13-C, PDF pages 285-288, Figure 4.2-84;
        Section 4.2.4.12, PDF pages 332-334, Table 4.2-66, Figure 4.2-99;
        Core Vol 6, Part B, 4.7 and 6.3.
    """
    await tester.command(hci.HCI_LE_Set_Random_Address_Command(privacy_peer.FALLBACK))
    await privacy_peer.add_peer(tester, device=device)
    address = (
        PEER_ADDRESS
        if identity_only
        else privacy_peer.private_address(privacy_peer.PEER_IRK)
    )
    for resolved in (True,) if identity_only else (True, False):
        if not resolved:
            await tester.command(hci.HCI_LE_Set_Address_Resolution_Enable_Command(0))
        await privacy_peer.scanning(tester, own_type=3)
        reports, requests = await privacy_peer.observe_advertising(
            tester,
            address=address,
            duration_us=400_000,
        )
        assert not requests
        if identity_only and not device:
            assert not reports
        else:
            assert reports
            assert all(
                report.address == (PEER_ADDRESS if resolved else address)
                for report in reports
            )
            expected_type = 3 if resolved and not identity_only else 1
            assert all(report.address_type == expected_type for report in reports)
        await privacy_peer.scan_enable(tester, False)


# Verify passive reports with resolution enabled and disabled, and identity-address
# rejection in network privacy versus acceptance in device privacy.
PROCEDURES |= procedure_cases(
    privacy_passive_scanning,
    {
        'LL/DDI/SCN/BV-13-C': ('Network Privacy – Passive Scanning, Peer IRK', {}),
        'LL/DDI/SCN/BV-26-C': (
            'Network Privacy – Passive Scanning, Peer IRK, Ignore Identity Address',
            {'identity_only': True},
        ),
        'LL/DDI/SCN/BV-28-C': (
            'Device Privacy – Passive Scanning, Peer IRK, Accept Identity Address',
            {'identity_only': True, 'device': True},
        ),
    },
)


async def privacy_active_scanning(tester, *, local_irk, peer_irk, renew=False):
    """Network Privacy: active scanning with zero/nonzero local and peer IRKs.

    Initial Condition:
        Standby; configure a private fallback and the Lower Tester's resolving record.

    Test Case Configuration:
        LL/DDI/SCN/BV-15-C: zero Local IRK, zero Peer IRK.
        LL/DDI/SCN/BV-16-C: nonzero Local IRK, zero Peer IRK, RPA timeout
            variant.
        LL/DDI/SCN/BV-17-C: zero Local IRK, nonzero Peer IRK.
        LL/DDI/SCN/BV-18-C: nonzero Local IRK, nonzero Peer IRK.

    Test Steps:
        For BV-15/17/18, perform steps 1-8 of Section 4.2.4.3: configure the
        advertiser and own address, add IRKs, enable resolution and active
        scanning with own type 3, send advertising events until SCAN_REQ,
        answer at T_IFS, and verify advertising/response reports.
        BV-16 performs steps 1-10 on pages 293-294: use the identity advertiser,
        local IRK and zero peer IRK; restart scanning, wait for address expiry,
        repeat the exchange and verify a new local RPA before disabling.

    Expected Outcome:
        Requests use a valid local RPA when Local IRK is nonzero and the configured
        private fallback otherwise. AdvA remains the received over-air address;
        resolved reports identify the peer. BV-16 renews ScanA after timeout.

    Source Correction:
        Table 4.2-50 names AUX_SCAN_IND for BV-15, a PDU absent from Core; use
        ADV_SCAN_IND. Its common steps prescribe zero IRKs and a static AdvA
        for every case, contradicting the Peer IRK/local RPA variants. Apply
        the per-case IRKs and RPA requirements in the titles/table and Core
        Vol 6, Part B, 6.3. These are local corrections, not SIG errata.

    Reference:
        LL.TS.p28, Section 4.2.4.3 and DDI/SCN/BV-16-C, PDF pages 290-294,
        Table 4.2-50, Figures 4.2-86/87; Core Vol 6, Part B, 6.3 and 6.5.
    """
    await tester.command(hci.HCI_LE_Set_Random_Address_Command(privacy_peer.FALLBACK))
    await tester.command(hci.HCI_LE_Set_Resolvable_Private_Address_Timeout_Command(1))
    await privacy_peer.add_peer(
        tester,
        peer_irk=privacy_peer.PEER_IRK if peer_irk else bytes(16),
        local_irk=privacy_peer.LOCAL_IRK if local_irk else bytes(16),
    )
    address = (
        privacy_peer.private_address(privacy_peer.PEER_IRK)
        if peer_irk
        else PEER_ADDRESS
    )
    old = None
    for repeat in range(2 if renew else 1):
        if repeat:
            await tester.wait(1_000_001)
        await privacy_peer.scanning(tester, active=True, own_type=3)
        reports, requests = await privacy_peer.observe_advertising(
            tester,
            address=address,
            active=True,
            duration_us=400_000,
        )
        assert requests and reports
        sources = [hci.Address(packet.pdu[2:8]) for packet in requests]
        for source in sources:
            if local_irk:
                privacy_peer.check_private_address(source, privacy_peer.LOCAL_IRK)
            else:
                assert source == privacy_peer.FALLBACK
        if old is not None:
            assert sources[0] != old
        old = sources[-1]
        assert any(report.event_type == 4 for report in reports)
        assert all(report.address == PEER_ADDRESS for report in reports)
        await privacy_peer.scan_enable(tester, False)


# Verify active scanning for the selected Local/Peer IRK combinations. The Lower Tester
# checks ScanA, Host reports and local RPA renewal when required.
PROCEDURES |= procedure_cases(
    privacy_active_scanning,
    {
        f'LL/DDI/SCN/BV-{number}-C': (
            'Network Privacy – Active Scanning, '
            f'{"Local IRK" if local else "no Local IRK"}, '
            f'{"Peer IRK" if peer else "no Peer IRK"}',
            {'local_irk': local, 'peer_irk': peer, 'renew': number == 16},
        )
        for number, local, peer in [
            (15, False, False),
            (16, True, False),
            (17, False, True),
            (18, True, True),
        ]
    },
)


async def privacy_scan_response_address(tester):
    """Privacy: Active Scanning, Wrong AdvA in Response.

    Initial Condition:
        An active scanner uses local/peer IRKs and a Filter Accept List identity.

    Test Steps:
        1-4. Configure the RPA advertiser, resolving record and accept list, and
             enable active scanning with filtering policy 1.
        5-6. Advertise on a primary channel until SCAN_REQ with a local RPA.
        7-9. Respond at T_IFS with a different AdvA generated from another IRK;
             check that only the original advertising data reaches the Host.
        10. Disable scanning.

    Expected Outcome:
        The peer RPA resolves, ScanA resolves with Local IRK, and the wrong-address
        SCAN_RSP is never reported, even though advertising reports are delivered.

    Reference:
        LL.TS.p28, DDI/SCN/BI-03-C, PDF pages 365-367, Figure 4.2-110;
        Core Vol 6, Part B, 4.4.3.2 and 6.3.
    """
    await privacy_peer.add_peer(tester)
    await tester.command(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(1, PEER_ADDRESS)
    )
    await privacy_peer.scanning(tester, active=True, own_type=2, policy=1)
    reports, requests = await privacy_peer.observe_advertising(
        tester,
        address=privacy_peer.private_address(privacy_peer.PEER_IRK),
        response_address=privacy_peer.private_address(bytes(range(16))),
        active=True,
        duration_us=400_000,
    )
    assert requests and reports
    for request in requests:
        privacy_peer.check_private_address(
            hci.Address(request.pdu[2:8]), privacy_peer.LOCAL_IRK
        )
    assert all(
        report.event_type != 4 and report.address == PEER_ADDRESS for report in reports
    )
    await privacy_peer.scan_enable(tester, False)


# Verify that active scanning ignores a SCAN_RSP whose AdvA differs from the address
# used in SCAN_REQ.
PROCEDURES |= procedure_cases(
    privacy_scan_response_address,
    {
        'LL/DDI/SCN/BI-03-C': ('Privacy – Active Scanning, Wrong AdvA in Response', {}),
    },
)


async def privacy_extended_active_scanning(tester):
    """Privacy: Extended Scanning, Active, Core 5.1.

    Initial Condition:
        The scanner and advertiser share IRKs; use accept-list filtering and
        public fallback with own type 2.

    Test Steps:
        1-4. Populate accept/resolving lists and enable extended active scanning.
        5-7. Send scannable AUX_ADV_IND with a TargetA generated from a wrong IRK
             for twenty advertising intervals; expect neither report nor request.
        8-13. Repeat with TargetA generated from Local IRK, receive AUX_SCAN_REQ,
              answer at T_IFS, and verify both directed advertising/response reports.
        14. Disable extended scanning.

    Expected Outcome:
        Only correctly directed events reach the Host or trigger AUX_SCAN_REQ;
        reports contain the peer identity, SID 3, interval zero, and directed/
        scannable bits. The response additionally sets bit 3 and complete data.

    Source Correction:
        Step 14 names legacy Set Scan Enable after extended configuration. Use
        Set Extended Scan Enable as required by Core Vol 4, Part E, 3.1.1 and
        7.8.65. This is a local command-family correction, not SIG errata.

    Reference:
        LL.TS.p28, Section 4.2.4.16, PDF pages 342-345, Table 4.2-72;
        Core Vol 6, Part B, 6.3 and 4.4.2.8.
    """
    peer = LOWER_PERIPHERAL_PUBLIC_ADDRESS
    await privacy_peer.add_peer(tester, identity=peer)
    await tester.command(hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(0, peer))
    await privacy_peer.scanning(
        tester, active=True, extended=True, policy=1, own_type=2
    )
    for valid in (False, True):
        reports, requests = await privacy_peer.observe_advertising(
            tester,
            address=privacy_peer.private_address(privacy_peer.PEER_IRK),
            extended=True,
            active=True,
            mode=2,
            duration_us=400_000,
            target=privacy_peer.private_address(
                privacy_peer.LOCAL_IRK if valid else bytes(range(16))
            ),
        )
        if not valid:
            assert not reports and not requests
        else:
            assert reports and requests
            assert all(
                report.address == peer
                and report.address_type == 2
                and report.event_type & 6 == 6
                and report.advertising_sid == 3
                and report.periodic_advertising_interval == 0
                for report in reports
            )
            assert any(report.event_type & 8 for report in reports)
    await privacy_peer.scan_enable(tester, False, extended=True)


# Verify that extended active scanning reports and answers correctly directed scannable
# advertising, while ignoring an unresolvable TargetA.
PROCEDURES |= procedure_cases(
    privacy_extended_active_scanning,
    {
        'LL/DDI/SCN/BV-63-C': ('Privacy - Extended Scanning, Active, Core 5.1', {}),
    },
)


async def _decision_instructions(tester, flags, fields, parameters):
    await tester.command(
        hci.HCI_LE_Set_Decision_Instructions_Command(flags, fields, parameters)
    )


async def _decision_observation(tester, expected, *, active=False, **parameters):
    reports, requests = await privacy_peer.observe_advertising(
        tester,
        extended=True,
        decision=True,
        active=active,
        **parameters,
    )
    assert bool(reports) == expected, (expected, parameters, reports)
    if not expected:
        assert not requests, 'a failed decision must not trigger AUX_SCAN_REQ'
    elif active and parameters.get('mode') == 2:
        assert requests and any(report.event_type & 8 for report in reports)
    return reports


async def decision_scanning_test_flags(tester, *, active=False, rpa=False):
    """Decision-Based Advertisements, Test Flags.

    Initial Condition:
        The static peer identity is the sole Filter Accept List entry. For the
        RPA variant, install separate IRKs for public/random identities.

    Test Case Configuration:
        LL/DDI/SCN/BV-101-C: passive scanning, no RPA.
        LL/DDI/SCN/BV-129-C: active scanning, no RPA.
        LL/DDI/SCN/BV-130-C: passive scanning, RPA.

    Test Steps:
        1-2. Enable continuous extended scanning, interval/window 0x10, policy 4,
             with Scan_Type from Table 4.2-88 and duplicate filtering disabled.
        3-6. Execute every applicable Table 4.2-90 outer round. Ten deterministically
             selected outer rounds exercise all eight Table 4.2-89 inner rounds;
             the others exercise inner rounds 2, 3 and 5. Install one instruction
             per inner round and advertise for ten scanner intervals. Answer
             AUX_SCAN_REQ at T_IFS in active/scannable passing rounds.

    Expected Outcome:
        Reports and scan requests agree with the independent P/F/A column and
        the corresponding match/nonmatch/absent Test_Flags bit for every round.

    Source Correction:
        Table 4.2-90 places the RPA rounds at 51-62 and Table 4.2-88 assigns
        them to the RPA variant, but the initial condition cites obsolete
        ranges: the shared key for rounds 56-67 and the Filter Accept List
        static identity for rounds 50-55 and 62-67. Follow Table 4.2-90: the
        key resolves rounds 51-62 and the listed static identity is used in
        rounds 44-49 and 57-62.
        Core Vol 4, Part E, 7.8.145 defines the same three flag outcomes.
        This is a local range clarification, not adopted SIG errata.

    Reference:
        LL.TS.p28, Section 4.2.4.27, PDF pages 426-433,
        Tables 4.2-88/89/90, Figure 4.2-137;
        Core Vol 6, Part B, 4.4.3.6; Vol 4, Part E, 7.8.145.
    """
    await tester.command(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(1, PEER_ADDRESS)
    )
    if rpa:
        await privacy_peer.add_peer(
            tester, identity=LOWER_PERIPHERAL_PUBLIC_ADDRESS, enabled=False
        )
        await privacy_peer.add_peer(tester, peer_irk=privacy_peer.LOCAL_IRK)
    await privacy_peer.scanning(tester, extended=True, active=active, policy=4)
    vectors = [vector for vector in flag_vectors() if (vector.number > 50) == rpa]
    full = set(random.Random(101).sample([vector.number for vector in vectors], 10))
    for vector in vectors:
        inner = range(8) if vector.number in full else (1, 2, 4)
        for bits in inner:
            flags = 1 | bits << 1
            await _decision_instructions(
                tester, [flags], [vector.field], [vector.parameters]
            )
            expected = bool(flags & {'P': 2, 'F': 4, 'A': 8}[vector.result])
            tester.output.write(
                f'OUTER {vector.number} FLAGS {flags} EXPECT {expected}\n'
            )
            await _decision_observation(
                tester,
                expected,
                active=active and vector.mode == 2,
                primary_address=vector.address,
                mode=vector.mode,
                decision_data=vector.data,
            )


# Verify decision-test flag results for matching, nonmatching and absent fields across
# the selected passive, active and RPA scanning variants.
PROCEDURES |= procedure_cases(
    decision_scanning_test_flags,
    {
        'LL/DDI/SCN/BV-101-C': (
            'Decision-Based Advertisements, Test Flags – Passive, No RPA',
            {},
        ),
        'LL/DDI/SCN/BV-129-C': (
            'Decision-Based Advertisements, Test Flags – Active, No RPA',
            {'active': True},
        ),
        'LL/DDI/SCN/BV-130-C': (
            'Decision-Based Advertisements, Test Flags – Passive, RPA',
            {'rpa': True},
        ),
    },
)


async def decision_scanning_signal_tests(tester):
    """Decision-Based Advertisements, RSSI and Path Loss.

    Initial Condition:
        Unfiltered passive extended scanning with no duplicate filtering.

    Test Steps:
        1-4. Configure interval/window 0x10 and an all-outcomes instruction;
             advertise at known Default_Power and save reported Default_RSSI.
        5-7. Execute all six Table 4.2-91 rounds for ten scanner intervals each,
             varying the physical transmit power by 0, -20 or -40 dB. Advertise
             TxPower 5 dBm and install the specified RSSI/path-loss thresholds.

    Expected Outcome:
        Only RSSI round 3 and path-loss round 6 produce reports.

    Observation Setup:
        Default_Power is +20 dBm, so the table's -40 dB change remains within
        the simulated transmitter's supported -20 to +20 dBm range. Both
        primary and auxiliary packets use the selected physical power.

    Reference:
        LL.TS.p28, DDI/SCN/BV-102-C, PDF pages 433-436, Table 4.2-91,
        Figure 4.2-138; Core Vol 4, Part E, 7.8.145.
    """
    await _decision_instructions(tester, [15], [0], [bytes(16)])
    await privacy_peer.scanning(tester, extended=True, policy=4)
    reports = await _decision_observation(tester, True, power_dbm=20)
    baseline = reports[-1].rssi
    for field in (7, 8):
        low, high = (
            (baseline - 30, baseline - 10)
            if field == 7
            else (5 - baseline + 10, 5 - baseline + 30)
        )
        parameters = bytes([low & 255, high & 255]) + bytes(14)
        await _decision_instructions(tester, [3], [field], [parameters])
        for delta in (40, 0, 20) if field == 7 else (0, 40, 20):
            await _decision_observation(
                tester, delta == 20, power_dbm=20 - delta, tx_power=5
            )


# Verify decision filtering against configured RSSI and path-loss ranges using the
# Lower Tester's transmitted advertising PDUs.
PROCEDURES |= procedure_cases(
    decision_scanning_signal_tests,
    {
        'LL/DDI/SCN/BV-102-C': (
            'Decision-Based Advertisements, RSSI and Path Loss',
            {},
        ),
    },
)


async def decision_scanning_groups(tester):
    """Decision-Based Advertisements, Test Groups.

    Initial Condition:
        Passive continuous scanning, with a shared resolvable-tag key.

    Test Steps:
        1-2. Enable extended scanning with policy 4.
        3-6. Execute all sixteen Table 4.2-92 instruction / 4.2-93 payload rounds,
             observing ten scanner intervals per round.
        7-8. Disable, switch to decision-only policy 12, and repeat all rounds.

    Expected Outcome:
        Rounds 1-3, 5, 11 and 12 report advertisements; all others remain silent.
        AND is applied within each group and OR between groups.

    Reference:
        LL.TS.p28, DDI/SCN/BV-103-C, PDF pages 436-439,
        Tables 4.2-92/93, Figure 4.2-139;
        Core Vol 4, Part E, 7.8.145; Vol 6, Part B, 4.4.3.6.
    """
    tag = bytes(privacy_peer.private_address(TAG_KEY))
    bad_tag = bytes(privacy_peer.private_address(bytes(range(16))))
    for policy in (4, 12):
        await privacy_peer.scanning(tester, extended=True, policy=policy)
        for number in range(1, 17):
            if number <= 4:
                mode = (0, 1, 2, 0)[number - 1]
                flags, fields = [3, 2], [0, 6]
                parameters = [TAG_KEY, bytes([1 << mode]) + bytes(15)]
                data = b'\1' + (tag if number < 4 else bad_tag)
            else:
                flags, fields = [9, 2, 2, 3, 2], [0, 6, 36, 0, 18]
                parameters = [
                    bytes(16),
                    bytes([4]) + bytes(15),
                    bytes.fromhex('ffffffff000000004142434400000000'),
                    TAG_KEY,
                    bytes.fromhex('dfdf0000000000004254000000000000'),
                ]
                mode = 2 if number <= 7 else 0
                arbitrary = {
                    5: b'ABCDEF',
                    6: b'abcdef',
                    7: b'',
                    8: b'bT',
                    9: b'bt',
                    10: b'Tb',
                    11: b'bT',
                    12: b'bt',
                    13: b'Tb',
                    14: b'bT',
                    15: b'bt',
                    16: b'Tb',
                }[number]
                data = (
                    b'\1' + (tag if number in (11, 12, 13) else bad_tag)
                    if number == 7 or number >= 11
                    else b'\0'
                ) + arbitrary
            await _decision_instructions(tester, flags, fields, parameters)
            await _decision_observation(
                tester,
                number in (1, 2, 3, 5, 11, 12),
                mode=mode,
                decision_data=data,
            )
        await privacy_peer.scan_enable(tester, False, extended=True)


# Verify acceptance when any decision-test group passes and rejection when no group
# satisfies all of its tests.
PROCEDURES |= procedure_cases(
    decision_scanning_groups,
    {
        'LL/DDI/SCN/BV-103-C': ('Decision-Based Advertisements, Test Groups', {}),
    },
)


async def decision_scanning_without_instructions(tester):
    """Decision-Based Advertisements Ignored, No Instructions Set.

    Initial Condition:
        Passive scanning; no decision instructions have been provided.

    Test Steps:
        1-3. Advertise decision PDUs pointing to AUX_ADV_IND and enable extended
             passive scanning with interval/window 0x10 and policy 4.
        4. Continue advertising for ten seconds and observe no Host reports.

    Expected Outcome:
        No Extended Advertising Report is emitted throughout the observation.

    Source Correction:
        The source names AUX_DECISION_IND and sets the tag flag with zero data.
        Core Vol 6, Part B, 2.3.1.11 defines ADV_DECISION_IND and requires at
        least six octets for a tag. Send valid tagged PDUs so missing instructions
        are the rejection reason; this is a local correction, not SIG errata.

    Reference:
        LL.TS.p28, DDI/SCN/BV-105-C, PDF pages 441-442, Figure 4.2-141;
        Core Vol 6, Part B, 4.4.3.6.
    """
    await privacy_peer.scanning(tester, extended=True, policy=4)
    await _decision_observation(
        tester,
        False,
        duration_us=10_000_000,
        decision_data=b'\1' + bytes(privacy_peer.private_address(TAG_KEY)),
    )


# Verify that a scanner ignores decision PDUs when the Upper Tester has not configured
# decision instructions.
PROCEDURES |= procedure_cases(
    decision_scanning_without_instructions,
    {
        'LL/DDI/SCN/BV-105-C': (
            'Decision-Based Advertisements Ignored, No Instructions Set',
            {},
        ),
    },
)


async def decision_scanning_disabled(tester):
    """Decision-Based Advertisements Ignored, Disabled Scanning.

    Initial Condition:
        Passive extended scanning with all-outcomes instructions.

    Test Steps:
        1-4. Advertise valid tagged decision PDUs, set interval/window 0x10,
             install an instruction accepting all outcomes and enable scanning.
        5-6. Check reports for ten scanner intervals.
        7-9. Disable scanning and advertise for another ten intervals.

    Expected Outcome:
        Reports are present while scanning and absent after disable.

    Source Correction:
        Step 2 omits a decision filter policy, whose reset value ignores decisions
        (Core Vol 6, Part B, 4.3.3.2; Vol 4, Part E, 7.8.64 bits 2-3).
        Select policy 4 to exercise the step 5 report required by the source.
        Use ADV_DECISION_IND (Core Vol 6, Part B, 2.3.1.11) for the undefined
        AUX_DECISION_IND name. These are local corrections, not SIG errata.

    Reference:
        LL.TS.p28, DDI/SCN/BV-106-C, PDF pages 442-444, Figure 4.2-142;
        Core Vol 6, Part B, 4.4.3.6; Vol 4, Part E, 7.8.145.
    """
    await _decision_instructions(tester, [15], [0], [bytes(16)])
    await privacy_peer.scanning(tester, extended=True, policy=4)
    # Step 1: Decision Type Flags 0b00001 with a valid six-octet Resolvable Tag.
    tagged = b'\1' + bytes(privacy_peer.private_address(TAG_KEY))
    await _decision_observation(tester, True, decision_data=tagged)
    await privacy_peer.scan_enable(tester, False, extended=True)
    await _decision_observation(tester, False, decision_data=tagged)


# Verify that decision PDUs produce no Host reports after the Upper Tester disables
# scanning.
PROCEDURES |= procedure_cases(
    decision_scanning_disabled,
    {
        'LL/DDI/SCN/BV-106-C': (
            'Decision-Based Advertisements Ignored, Disabled Scanning',
            {},
        ),
    },
)


async def decision_scanning_filter_policy(tester):
    """Decision-Based Advertisements, Ignore Non-Decision PDUs.

    Initial Condition:
        One advertiser identity in the Filter Accept List.

    Test Steps:
        1-2. Start ordinary and decision advertising sets and add their address
             to the accept list.
        3-8. For all Table 4.2-94 policies (3, 7, 15), configure scan interval/
             window 0x10, accept-all instructions, enable scanning, advertise
             both sets for ten intervals, verify the selected PDU classes, disable.

    Expected Outcome:
        Policy 3 reports ordinary events, 7 reports both, 15 reports decisions.

    Reference:
        LL.TS.p28, DDI/SCN/BV-107-C, PDF pages 444-446, Table 4.2-94;
        Core Vol 6, Part B, 4.3.3.2 and 4.4.3.6; Vol 4, Part E, 7.8.64.
    """
    await tester.command(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(1, PEER_ADDRESS)
    )
    for policy in (3, 7, 15):
        await _decision_instructions(tester, [15], [0], [bytes(16)])
        await privacy_peer.scanning(tester, extended=True, policy=policy)
        for decision in (False, True):
            reports, requests = await privacy_peer.observe_advertising(
                tester,
                extended=True,
                decision=decision,
                primary_address=PEER_ADDRESS,
            )
            assert not requests
            assert bool(reports) == (policy == 7 or decision == (policy == 15))
        await privacy_peer.scan_enable(tester, False, extended=True)


# Verify that the scanning decision filter policy accepts eligible decision PDUs and
# ignores non-decision advertising.
PROCEDURES |= procedure_cases(
    decision_scanning_filter_policy,
    {
        'LL/DDI/SCN/BV-107-C': (
            'Decision-Based Advertisements, Ignore Non-Decision PDUs',
            {},
        ),
    },
)


async def decision_scanning_arbitrary_lengths(tester):
    """Decision-Based Advertisements, Arbitrary Data Test Flags.

    Initial Condition:
        Passive continuous extended scanning, policy 4, no duplicate filtering.

    Test Steps:
        1-3. Enable scanning and advertise each Table 4.2-95 data layout.
        4-7. Test all 24 field values (17-24, 33-40, 49-56), flags 7 and zero
             parameters, observing reports for ten scanner intervals each.
        8. Repeat with flags 9 and invert the expected field-presence outcome.

    Expected Outcome:
        Reports agree with every field-presence set explicitly listed in the table;
        tag bytes do not count as arbitrary data and empty arbitrary data is absent.

    Reference:
        LL.TS.p28, DDI/SCN/BV-108-C, PDF pages 446-448, Table 4.2-95;
        Core Vol 4, Part E, 7.8.145; Vol 6, Part B, 4.4.3.6.
    """
    await privacy_peer.scanning(tester, extended=True, policy=4)
    tag = bytes(privacy_peer.private_address(TAG_KEY))
    for data, present in [
        (b'\0abc', {19, 33, 34, 35, *range(51, 57)}),
        (b'\1' + tag + b'a', {17, 33, *range(49, 57)}),
        (b'\1' + tag, set()),
    ]:
        for flags in (7, 9):
            for field in (*range(17, 25), *range(33, 41), *range(49, 57)):
                await _decision_instructions(tester, [flags], [field], [bytes(16)])
                await _decision_observation(
                    tester,
                    (field in present) == (flags == 7),
                    decision_data=data,
                )


# Verify arbitrary-data decision tests across the specified data lengths and flag
# combinations, including fields treated as absent.
PROCEDURES |= procedure_cases(
    decision_scanning_arbitrary_lengths,
    {
        'LL/DDI/SCN/BV-108-C': (
            'Decision-Based Advertisements, Arbitrary Data Test Flags',
            {},
        ),
    },
)


async def decision_scanning_primary_address(tester):
    """Decision-Based Advertisements, AdvA in Decision PDU.

    Initial Condition:
        Two public advertiser addresses; extended passive scanning.

    Test Steps:
        1-3. Install a field-9 check accepting either address, flags 3; enable
             policy 4 scanning with interval/window 0x10.
        4-7. Advertise with primary AdvA then without it; check presence/absence
             of reports and the first address reported.
        8-12. Replace flags with 9 (pass when absent), repeat both layouts and
              verify that only the no-primary-address event reports its AUX AdvA.

    Expected Outcome:
        Field presence is tested on the decision PDU. Primary AdvA, when present,
        supplies the report address; otherwise the auxiliary address supplies it.

    Reference:
        LL.TS.p28, DDI/SCN/BV-109-C, PDF pages 448-451, Figures 4.2-145/146;
        Core Vol 4, Part E, 7.8.145; Vol 6, Part B, 2.3.1.11 and 4.4.3.6.
    """
    first, second = LOWER_PERIPHERAL_PUBLIC_ADDRESS, LOWER_CENTRAL_PUBLIC_ADDRESS
    parameters = address_parameters(2, first, second)
    await _decision_instructions(tester, [3], [9], [parameters])
    await privacy_peer.scanning(tester, extended=True, policy=4)
    for flags in (3, 9):
        await _decision_instructions(tester, [flags], [9], [parameters])
        for primary in (first, None):
            expected = (primary is not None) == (flags == 3)
            reports = await _decision_observation(
                tester,
                expected,
                address=second,
                primary_address=primary,
                decision_data=b'\1' + bytes(privacy_peer.private_address(TAG_KEY)),
            )
            if expected:
                assert all(report.address == (primary or second) for report in reports)


# Verify decision-PDU processing when AdvA is present, including address checks and the
# address reported to the Host.
PROCEDURES |= procedure_cases(
    decision_scanning_primary_address,
    {
        'LL/DDI/SCN/BV-109-C': (
            'Decision-Based Advertisements, AdvA in Decision PDU',
            {},
        ),
    },
)


async def privacy_directed_scanning_policies(tester):
    """Extended Scanning, Passive, Scanning Filter Policy, Resolvable Private Addresses.

    Initial Condition:
        Public IUT address supported; use continuous passive extended scanning.

    Test Steps:
        Repeat all ten source steps for each of the 25 Table 4.2-76 rounds:
        set a random address, clear/populate the resolving list as specified,
        enable resolution, select own address and filtering policy, advertise
        directed nonconnectable primary-only ADV_EXT_IND for at least twenty
        advertising intervals, check reports and Direct Address fields, disable.

    Expected Outcome:
        Rounds 18, 20 and 24 produce no reports. Other rounds report TargetA
        directly, the resolved local identity, or unresolved type 0xFE exactly
        as permitted by the table. Periodic interval is zero and the directed
        event bit is set; no scan requests are sent.

    Source Correction:
        Step 10 names legacy Set Scan Enable after extended configuration; use
        Set Extended Scan Enable (Core Vol 4, Part E, 3.1.1 and 7.8.65).
        This is a local command-family correction, not adopted SIG errata.

    Reference:
        LL.TS.p28, DDI/SCN/BV-71-C, PDF pages 380-384, Table 4.2-76,
        Figure 4.2-118; Core Vol 6, Part B, 6.3 and 4.3.3;
        Vol 4, Part E, 7.7.65.13.
    """
    random_own = hci.Address('C1:23:45:67:89:AB')
    peer = LOWER_PERIPHERAL_PUBLIC_ADDRESS
    # Table order: own type, filter policy, TargetA class, resolving record,
    # accept-list membership. Rounds 20/25 differ only in own address type.
    rounds = [
        (0, 2, 'public', False, False),
        (1, 2, 'random', False, False),
        (1, 2, 'rpa', True, False),
        (2, 2, 'rpa', True, False),
        (3, 2, 'rpa', True, False),
        (0, 2, 'rpa', False, False),
        (0, 3, 'public', False, True),
        (1, 3, 'random', False, True),
        (1, 3, 'rpa', True, True),
        (2, 3, 'rpa', True, True),
        (3, 3, 'rpa', True, True),
        (0, 3, 'rpa', False, True),
        (0, 0, 'public', False, False),
        (1, 0, 'random', False, True),
        (1, 0, 'rpa', True, False),
        (2, 0, 'rpa', True, False),
        (3, 0, 'rpa', True, False),
        (0, 0, 'rpa', False, False),
        (0, 1, 'public', False, True),
        (0, 1, 'random', False, True),
        (1, 1, 'rpa', True, True),
        (2, 1, 'rpa', True, True),
        (3, 1, 'rpa', True, True),
        (0, 1, 'rpa', False, True),
        (1, 1, 'random', False, True),
    ]
    for number, (own, policy, kind, resolving, listed) in enumerate(rounds, 1):
        await tester.command(hci.HCI_LE_Set_Random_Address_Command(random_own))
        await tester.command(hci.HCI_LE_Clear_Resolving_List_Command())
        await tester.command(hci.HCI_LE_Clear_Filter_Accept_List_Command())
        if resolving:
            await privacy_peer.add_peer(
                tester, identity=peer, peer_irk=bytes(16), enabled=False
            )
        await tester.command(hci.HCI_LE_Set_Address_Resolution_Enable_Command(1))
        if listed:
            await tester.command(
                hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(0, peer)
            )
        target = (
            DUT_ADDRESS
            if kind == 'public'
            else random_own
            if kind == 'random'
            else privacy_peer.private_address(privacy_peer.LOCAL_IRK)
        )
        await privacy_peer.scanning(tester, extended=True, own_type=own, policy=policy)
        deadline = tester.radio.now_us + 400_000
        index = 0
        while tester.radio.now_us < deadline:
            await tester.transmit(37 + index % 3, ext_pdu(address=peer, target=target))
            await tester.wait(20_000)
            index += 1
        reports = privacy_peer.reports(tester)
        assert bool(reports) == (number not in (18, 20, 24)), number
        for report in reports:
            assert report.address == peer and report.periodic_advertising_interval == 0
            assert report.event_type & 4
            if kind == 'rpa':
                expected_type = 2 + (own & 1) if resolving else 0xFE
                assert report.direct_address_type == expected_type, number
                assert report.direct_address in (
                    target,
                    random_own if own & 1 else DUT_ADDRESS,
                )
            else:
                assert report.direct_address_type == target.address_type
                assert report.direct_address == target
        await privacy_peer.scan_enable(tester, False, extended=True)


# Verify directed advertising reports for the specified scanning policies and
# resolving-list configurations, including Direct_Address and its address type.
PROCEDURES |= procedure_cases(
    privacy_directed_scanning_policies,
    {
        'LL/DDI/SCN/BV-71-C': (
            'Extended Scanning, Passive, Scanning Filter Policy, '
            'Resolvable Private Addresses, Public Address supported',
            {},
        ),
    },
)


async def _monitor_scan(tester, *, extended, active, policy, enabled):
    if not enabled:
        command = (
            hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0)
            if extended
            else hci.HCI_LE_Set_Scan_Enable_Command(0, 0)
        )
        await tester.command(command)
        return
    await tester.command(
        scan_parameters(
            extended=True,
            scan_types=[int(active)],
            scan_intervals=[16],
            scan_windows=[16],
            scanning_filter_policy=policy,
        )
        if extended
        else scan_parameters(
            le_scan_type=int(active),
            le_scan_interval=16,
            le_scan_window=16,
            scanning_filter_policy=policy,
        )
    )
    await tester.command(
        hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0)
        if extended
        else hci.HCI_LE_Set_Scan_Enable_Command(1, 0)
    )


async def _monitor_advertisements(tester, count, power, *, extended, active, policy):
    """Send 100-ms events and check advertising and monitoring reports."""
    monitor = hci.HCI_LE_Monitored_Advertisers_Report_Event
    report_type = ExtendedReport if extended else LegacyReport
    found = []
    reports = 0
    for sequence in range(count):
        start = tester.radio.now_us + 100
        data = sequence.to_bytes(2, 'little')
        adi = ADVERTISING_SID << PDU_ADV_ADI_DID_BITS | sequence & PDU_ADV_ADI_DID_MASK
        for channel in (37, 38, 39):
            if extended:
                primary_start = tester.radio.now_us + 100
                await tester.transmit(
                    channel,
                    ext_pdu(adi=adi, aux=aux_pointer(0, 1500)),
                    start_us=primary_start,
                    power_dbm=power,
                )
                end = await tester.transmit(
                    0,
                    ext_pdu(
                        address=LOWER_PERIPHERAL_PUBLIC_ADDRESS, adi=adi, data=data
                    ),
                    start_us=primary_start + 1500,
                    power_dbm=power,
                )
            else:
                end = await tester.transmit(
                    channel,
                    adv_pdu(address=LOWER_PERIPHERAL_PUBLIC_ADDRESS, data=data),
                    power_dbm=power,
                )
            if active and not extended:
                request = await tester.receive(channel, duration_us=500)
                if request is not None:
                    assert not policy
                    assert (
                        request.pdu[0] & PDU_ADV_TYPE_MASK
                        == AdvertisingPduType.SCAN_REQ
                    )
                    await tester.transmit(
                        channel,
                        adv_pdu(
                            AdvertisingPduType.SCAN_RSP,
                            address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
                            data=data,
                        ),
                        start_us=request.end_us + LL_IFS_US,
                        power_dbm=power,
                    )
            await tester.wait(max(0, end + 1000 - tester.radio.now_us))
        await tester.wait(max(0, start + 100_000 - tester.radio.now_us))
        while (event := tester.take(lambda e: isinstance(e, report_type))) is not None:
            assert not policy, 'Filter Accept List rejected advertiser was reported'
            for report in event.reports:
                assert report.address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
                assert report.address_type == 0
                assert report.data == data
                if extended:
                    assert report.advertising_sid == ADVERTISING_SID
                if power == -40:
                    assert report.rssi < -65
                elif power == -5:
                    assert -65 < report.rssi < -35
                else:
                    assert report.rssi >= -35
                reports += 1
        while (event := tester.take(lambda e: isinstance(e, monitor))) is not None:
            assert event.subevent_code == 0x34
            assert event.address_type == 0
            assert event.address == LOWER_PERIPHERAL_PUBLIC_ADDRESS
            found.append(event.condition)
    if not policy:
        assert reports, 'no received advertising reports in this RSSI phase'
    return found


async def monitoring_advertisers(tester, *, extended, active):
    """Monitoring Advertisers.

    Initial Condition:
        The IUT is in standby, with an empty Filter Accept List. Select LE 1M,
        scan interval/window 0x0010, duplicate filtering disabled, public LT
        address, 100-ms advertising interval, thresholds -65/-35 dBm and 10 s
        timeout. Powers -40/-5/+20 dBm with the fixture's 45-dB path loss select
        below-low, intermediate and above-high RSSI, verified in received reports.

    Test Case Configuration:
        LL/DDI/SCN/BV-111-C: Legacy Advertising, passive scanning.
        LL/DDI/SCN/BV-112-C: Legacy Advertising, active scanning.
        LL/DDI/SCN/BV-113-C: Extended Advertising, passive scanning.
        LL/DDI/SCN/BV-114-C: Extended Advertising, active scanning.

    Test Steps:
        1-7. Configure and enable the selected scan, add the LT, remove it, then
             add it with the selected thresholds and 10-second timeout.
        8-11. Start intermediate-RSSI advertising, check normal reports, and
              enable monitoring with successful Command Complete.
        12-14. After 20 intervals send below-low RSSI for 5 seconds, then
               intermediate RSSI for 5 seconds. Require no monitoring event.
        15-17. Send intermediate RSSI for ten more intervals, then below-low
               RSSI. Require one condition-0 report at the 10-second timeout.
        18-21. Continue low RSSI for 20 intervals, intermediate for 20 intervals,
               then high RSSI. Require condition 1 on the first received high PDU.
        22-27. Send intermediate RSSI for ten intervals and high RSSI again;
               require no duplicate high report. Disable monitoring and send
               low RSSI for 10 seconds without further monitoring events.
        28-31. Clear the list, disable scanning, and repeat with scanning filter
               policy 1 and an empty Filter Accept List. Monitoring still works;
               normal advertising reports and scan requests are suppressed.

    Expected Outcome:
        Pass verdict: successful commands, exactly one low and subsequent high
        report per policy, correct address and condition, and silent disabled
        monitoring. Normal reports use the selected legacy/extended event type.

    Source Correction:
        Steps 17 and 21 say Subevent_Code 0x3E. Core Vol 4, Part E, 7.7.65.47
        defines subevent 0x34 inside the outer LE Meta event 0x3E.

    Reference:
        LL.TS.p28, Section 4.2.4.28, Table 4.2-96, Figures 4.2-147/148,
        printed pages 4.2-358 through 4.2-363; Core Vol 6, Part B, 4.4.3.8.
    """
    await tester.command(hci.HCI_LE_Clear_Filter_Accept_List_Command())
    for policy in (0, 1):
        await _monitor_scan(
            tester, extended=extended, active=active, policy=policy, enabled=True
        )
        add = hci.HCI_LE_Add_Device_To_Monitored_Advertisers_List_Command(
            0, LOWER_PERIPHERAL_PUBLIC_ADDRESS, -65, -35, 10
        )
        await tester.command(add)
        await tester.command(
            hci.HCI_LE_Remove_Device_From_Monitored_Advertisers_List_Command(
                0, LOWER_PERIPHERAL_PUBLIC_ADDRESS
            )
        )
        await tester.command(add)

        async def burst(count, power):
            return await _monitor_advertisements(
                tester, count, power, extended=extended, active=active, policy=policy
            )

        assert not await burst(1, -5)
        await tester.command(hci.HCI_LE_Enable_Monitoring_Advertisers_Command(1))
        assert not await burst(20, -5)
        assert not await burst(50, -40)
        assert not await burst(50, -5)
        assert not await burst(10, -5)
        # The scanner can miss an entire advertising event while following AuxPtr.
        # Sample the timeout across the last three 100-ms reception opportunities;
        # exact deadline/threshold boundaries are covered by the air unit tests.
        assert not await burst(96, -40)
        assert await burst(5, -40) == [0]
        assert not await burst(20, -40)
        assert not await burst(20, -5)
        assert await burst(1, 20) == [1]
        assert not await burst(10, -5)
        assert not await burst(1, 20)
        await tester.command(hci.HCI_LE_Enable_Monitoring_Advertisers_Command(0))
        assert not await burst(100, -40)
        await tester.command(hci.HCI_LE_Clear_Monitored_Advertisers_List_Command())
        await _monitor_scan(
            tester, extended=extended, active=active, policy=policy, enabled=False
        )


# Verify RSSI supervision for legacy/extended and passive/active scanners,
# including Filter Accept List independence and clear-list behavior.
PROCEDURES |= procedure_cases(
    monitoring_advertisers,
    {
        'LL/DDI/SCN/BV-111-C': (
            'Monitoring Advertisers, Legacy Advertising, Passive Scanning',
            {'extended': False, 'active': False},
        ),
        'LL/DDI/SCN/BV-112-C': (
            'Monitoring Advertisers, Legacy Advertising, Active Scanning',
            {'extended': False, 'active': True},
        ),
        'LL/DDI/SCN/BV-113-C': (
            'Monitoring Advertisers, Extended Advertising, Passive Scanning',
            {'extended': True, 'active': False},
        ),
        'LL/DDI/SCN/BV-114-C': (
            'Monitoring Advertisers, Extended Advertising, Active Scanning',
            {'extended': True, 'active': True},
        ),
    },
)


async def monitoring_advertisers_clear_list(tester, *, extended, active):
    """Monitoring Advertisers, Clear List.

    Initial Condition:
        Standby IUT, public LT address, LE 1M, 100-ms advertising interval,
        scan interval/window 0x0010, accept-all policy, no duplicate filtering.
        Select thresholds -65/-35 dBm and timeout 10 seconds.

    Test Case Configuration:
        LL/DDI/SCN/BV-123-C: Legacy Advertising, passive scanning.
        LL/DDI/SCN/BV-124-C: Legacy Advertising, active scanning.
        LL/DDI/SCN/BV-125-C: Extended Advertising, passive scanning.
        LL/DDI/SCN/BV-126-C: Extended Advertising, active scanning.

    Test Steps:
        1-5. Enable the selected scan. Add the LT twice using identical valid
             parameters and require successful Command Complete for both adds.
        6-10. Start intermediate-RSSI advertising and enable monitoring. Switch
              below low and require condition 0 after the 10-second timeout.
        11-13. Send intermediate RSSI, clear the list while monitoring remains
               enabled, and require successful Command Complete.
        14-16. Send above-high RSSI, then intermediate RSSI for 20 advertising
               intervals; require no further monitoring events.

    Expected Outcome:
        Pass verdict: duplicate add succeeds, low supervision reports correctly,
        and clearing prevents the pending high report while scanning continues.

    Source Correction:
        Step 10's Subevent_Code 0x3E is the outer event; the defined subevent is
        0x34 (Core Vol 4, Part E, 7.7.65.47).

    Reference:
        LL.TS.p28, Section 4.2.4.30, Table 4.2-98, Figure 4.2-150,
        printed pages 4.2-367 through 4.2-370; Core Vol 6, Part B, 4.4.3.8.
    """
    await _monitor_scan(
        tester, extended=extended, active=active, policy=0, enabled=True
    )
    add = hci.HCI_LE_Add_Device_To_Monitored_Advertisers_List_Command(
        0, LOWER_PERIPHERAL_PUBLIC_ADDRESS, -65, -35, 10
    )
    await tester.command(add)
    await tester.command(add)

    async def burst(count, power):
        return await _monitor_advertisements(
            tester, count, power, extended=extended, active=active, policy=0
        )

    assert not await burst(1, -5)
    await tester.command(hci.HCI_LE_Enable_Monitoring_Advertisers_Command(1))
    assert not await burst(96, -40)
    assert await burst(5, -40) == [0]
    assert not await burst(1, -5)
    await tester.command(hci.HCI_LE_Clear_Monitored_Advertisers_List_Command())
    assert not await burst(1, 20)
    assert not await burst(20, -5)
    await _monitor_scan(
        tester, extended=extended, active=active, policy=0, enabled=False
    )


PROCEDURES |= procedure_cases(
    monitoring_advertisers_clear_list,
    {
        'LL/DDI/SCN/BV-123-C': (
            'Monitoring Advertisers, Clear List, Legacy Advertising, Passive Scanning',
            {'extended': False, 'active': False},
        ),
        'LL/DDI/SCN/BV-124-C': (
            'Monitoring Advertisers, Clear List, Legacy Advertising, Active Scanning',
            {'extended': False, 'active': True},
        ),
        'LL/DDI/SCN/BV-125-C': (
            (
                'Monitoring Advertisers, Clear List, Extended Advertising, '
                'Passive Scanning'
            ),
            {'extended': True, 'active': False},
        ),
        'LL/DDI/SCN/BV-126-C': (
            'Monitoring Advertisers, Clear List, Extended Advertising, Active Scanning',
            {'extended': True, 'active': True},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_scan_filtering(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
