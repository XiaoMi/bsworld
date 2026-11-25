# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""HCI.TS.p39, Section 4.15.2: Broadcast Isochronous Streams."""

import pytest

from bscontroller import hci
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.iso import (
    acquire_broadcaster,
    acquire_periodic_receiver,
    acquire_receiver,
    configure_periodic,
    create_command,
    data_path,
    iso_packet,
    observe_broadcaster,
    terminate_broadcaster,
)


async def invalid_big_parameters(tester):
    """Broadcast Isochronous Stream Using Non-Test Command, Invalid BIG Parameters.

    Initial Condition:
        IUT: periodic advertiser with a valid train. Other BIG parameters use
        the supported unframed BN=1 defaults in LL.TS 4.11.2.1.

    Test Steps:
        1. Send Create BIG with each Table 4.111 invalid value: BIG_Handle and
           Advertising_Handle=F0, Num_BIS=20, SDU_Interval=100000,
           Max_SDU=1000, Max_Transport_Latency=0FA1, RTN=20, PHY=09,
           Packing=F0, Framing=F0, Encryption=F0 (hexadecimal).
        2. Require Command Status 0x11 for the PHY row and 0x12 otherwise.
        3. Send all Table 4.112 invalid values together; require an error.

    Expected Outcome:
        Every invalid row is rejected, with the specified status. The final
        command also fails without creating a BIG.

    Reference:
        HCI.TS.p39, HCI/BIS/BI-06-C, Tables 4.111/112, Steps 1–5.
    """
    await tester.masks()
    await configure_periodic(tester)
    rows = [
        ('big_handle', 0xF0),
        ('advertising_handle', 0xF0),
        ('num_bis', 0x20),
        ('sdu_interval', 0x100000),
        ('max_sdu', 0x1000),
        ('max_transport_latency', 0xFA1),
        ('rtn', 0x20),
        ('phy', 9),
        ('packing', 0xF0),
        ('framing', 0xF0),
        ('encryption', 0xF0),
    ]
    for name, value in rows:
        await tester.command(
            create_command(**{name: value}), 0x11 if name == 'phy' else 0x12
        )
    values = dict(rows)
    values['sdu_interval'] = 0x10000
    await tester.command(create_command(**values), (0x11, 0x12))


async def incompatible_latency(tester, *, large_sdu):
    """Reject incompatible framed BIG transport latency.

    Initial Condition:
        IUT: periodic advertiser. Use supported defaults for unspecified fields.

    Test Case Configuration:
        HCI/BIS/BI-07-C: framed, one BIS, SDU_Interval=100 ms, Max_SDU=753,
        Max_Transport_Latency=5 ms. BI-11-C: framed, SDU_Interval=20 ms,
        Max_Transport_Latency=10 ms (Table 4.113).

    Test Steps:
        1. Send Create BIG with the selected incompatible latency.
        2. Receive a failing Command Status, or successful Status followed by
           failing Create BIG Complete. Require a valid parameter error.

    Expected Outcome:
        The impossible configuration is rejected with 0x11 or 0x12.

    Source Correction:
        BI-07-C's alternative 2B calls the same Status both successful and an
        error. Check its error code as required by its alternative and purpose.
        BI-11-C's final reference to round 12 is a leftover; its single Table
        4.113 configuration is executed.

    Reference:
        HCI.TS.p39, HCI/BIS/BI-07-C and BI-11-C; Table 4.113.
    """
    await tester.masks()
    await configure_periodic(tester)
    result = await tester.command(
        create_command(
            framing=1,
            sdu_interval=100000 if large_sdu else 20000,
            max_sdu=753 if large_sdu else 32,
            max_transport_latency=5 if large_sdu else 10,
        ),
        (0, 0x11, 0x12),
    )
    if result.status == 0:
        assert (await tester.event(hci.HCI_LE_Create_BIG_Complete_Event)).status in (
            0x11,
            0x12,
        )


async def unsegmented_unsupported(tester):
    """LE Create BIG, Framed, Unsegmented Mode Unsupported.

    Initial Condition:
        IUT: periodic advertiser; ISOAL Unsegmented Framed Mode is not claimed.

    Test Steps:
        1. Send Create BIG with Framing=2 and valid remaining parameters.
        2. Require Unsupported Feature or Parameter Value (0x11).

    Expected Outcome:
        The command rejects unsegmented framed mode.

    Source Correction:
        The source names Command Complete. Create BIG is asynchronous and
        Core Vol 4, E, 7.8.103 requires Command Status; check that event and the
        source's required 0x11, without accepting an incorrect event type.

    Reference:
        HCI.TS.p39, HCI/BIS/BI-10-C, Steps 1–2.
    """
    await tester.masks()
    await configure_periodic(tester)
    await tester.command(create_command(framing=2), 0x11)


async def pawr_big_rejected(tester, *, test):
    """Reject a BIG associated with periodic advertising with responses.

    Initial Condition:
        IUT: enabled PAwR advertiser with a valid 100-ms train. A BIG associated
        with PAwR is not claimed; ordinary periodic advertising supports BIGs.

    Test Case Configuration:
        HCI/BIS/BV-09-C uses Create BIG; BV-10-C uses Create BIG Test.

    Test Steps:
        1. Send the selected command naming the PAwR advertising handle.
        2. Accept failing Status, or successful Status followed by failing
           Create BIG Complete. Require Unknown Advertising Identifier (0x42).

    Expected Outcome:
        The PAwR association is rejected with 0x42.

    Reference:
        HCI.TS.p39, 4.15.2.3, Table 4.115, Steps 1–2.
    """
    await tester.masks()
    await configure_periodic(tester, interval=80, pawr=True)
    result = await tester.command(create_command(test=test), (0, 0x42))
    if result.status == 0:
        assert (await tester.event(hci.HCI_LE_Create_BIG_Complete_Event)).status == 0x42


async def coded_periodic_overflow(tester):
    """Broadcast Isochronous Stream testing overlength data on LE Coded PHY.

    Initial Condition:
        IUT: enabled LE Coded periodic advertising at 7.5 ms, with 93 random
        data octets. The data fits until BIGInfo is added to ACAD.

    Test Steps:
        1. Send Create BIG with PHY=04 and Encryption=00; require 0x45 from
           Command Status or the subsequent Create BIG Complete event.
        2. Stop periodic advertising and repeat the identical Create BIG.

    Expected Outcome:
        Both commands report Packet Too Long (0x45).

    Reference:
        HCI.TS.p39, HCI/BIS/BV-11-C, Steps 1–4.
    """
    await tester.masks()
    await configure_periodic(
        tester,
        interval=6,
        secondary_phy=3,
        extended=False,
        data=bytes((n * 37 + 11) & 255 for n in range(93)),
    )
    for enabled in (True, False):
        if not enabled:
            await tester.command(
                hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0, 0)
            )
        result = await tester.command(create_command(phy=4), (0, 0x45))
        if result.status == 0:
            assert (
                await tester.event(hci.HCI_LE_Create_BIG_Complete_Event)
            ).status == 0x45


async def receiver_invalid_commands(tester, *, read_sync=False):
    """Synchronized Receiver rejects invalid ISO path or TX timing commands.

    Initial Condition:
        Lower Tester: one unencrypted BIS with BN=1 defaults. IUT: synchronized
        receiver with a successful BIG Sync Established event and active handle.

    Test Case Configuration:
        HCI/BIS/BI-02-C checks duplicate/invalid Setup ISO Data Path.
        HCI/BIS/BV-04-C checks Read ISO TX Sync on the receiving BIS.

    Test Steps:
        1. For BI-02, create an output HCI path successfully, repeat it and
           require 0x0C, then use invalid handle 0x0EFF and require 0x02.
        2. For BV-04, Read ISO TX Sync on the BIS and require 0x0C.

    Expected Outcome:
        Every command has its specified error.

    Source Correction:
        BV-04's step says ACL handle although its purpose and initial condition
        specify a Synchronized Receiver BIS. Use the receiving BIS; Core Vol 4,
        E, 7.8.96 gives 0x02 for an ACL, and 0x0C for a receive-only BIS.

    Reference:
        HCI.TS.p39, HCI/BIS/BI-02-C, Steps 1–5; BV-04-C, Steps 1–2.
    """
    _, event = await acquire_receiver(tester)
    assert event.status == 0
    handle = event.connection_handle[0]
    if read_sync:
        await tester.command(hci.HCI_LE_Read_ISO_TX_Sync_Command(handle), 0x0C)
    else:
        await data_path(tester, handle, 1)
        for stream, status in ((handle, 0x0C), (0x0EFF, 0x02)):
            await tester.command(
                hci.HCI_LE_Setup_ISO_Data_Path_Command(
                    stream, 1, 0, hci.CodingFormat(hci.CodecID.TRANSPARENT), 0, b''
                ),
                status,
            )


async def authenticated_timeout_rejected(tester, *, broadcaster):
    """Reject ACL Authenticated Payload Timeout commands on BIS handles.

    Initial Condition:
        HCI/BIS/BI-12-C: encrypted broadcaster, ISO/SDU interval 100 ms,
        NSE=IRC=3, Max_SDU=Max_PDU=8, BN=1, LE 1M.
        BI-14-C: IUT synchronized to a Lower Tester BIS using BN=1 defaults.

    Test Case Configuration:
        BI-12-C selects the broadcaster; BI-14-C selects the receiver.

    Test Steps:
        1. Read Authenticated Payload Timeout using the BIS handle.
        2. Require Command Complete with Status=0x0C.
        3. Write Authenticated Payload Timeout using the same BIS handle.
        4. Require Command Complete with Status=0x0C.

    Expected Outcome:
        Both ACL-only operations are disallowed for the established BIS.

    Reference:
        HCI.TS.p39, HCI/BIS/BI-12-C, Table 4.114; BI-14-C, Steps 1–4.
    """
    if broadcaster:
        _, event = await acquire_broadcaster(
            tester,
            encrypted=True,
            interval=80,
            sdu_interval=100000,
            max_sdu=8,
            max_pdu=8,
        )
    else:
        _, event = await acquire_receiver(tester)
    handle = event.connection_handle[0]
    await tester.command(
        hci.HCI_Read_Authenticated_Payload_Timeout_Command(handle), 0x0C
    )
    await tester.command(
        hci.HCI_Write_Authenticated_Payload_Timeout_Command(handle, 3000), 0x0C
    )


async def encryption_mode_mismatch(tester):
    """Invalid LE BIG Create Sync Encryption Parameter, BIS.

    Initial Condition:
        IUT: synchronized to the Lower Tester's periodic advertising. Use one
        BIS with BN=1 and supported default parameters.

    Test Steps:
        1. Advertise an encrypted BIG; request BIG synchronization unencrypted.
        2. Require Encryption Mode Not Acceptable (0x25).
        3. Repeat with an unencrypted BIG and encrypted synchronization request.

    Expected Outcome:
        Both opposite encryption modes fail with 0x25.

    Reference:
        HCI.TS.p39, HCI/BIS/BI-09-C, Table 4.107, Steps 1–3.
    """
    for encrypted in (True, False):
        peer, event = await acquire_receiver(
            tester, encrypted=encrypted, encryption=not encrypted
        )
        assert event.status == 0x25
        await tester.command(
            hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(peer.sync_handle)
        )
        await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


async def transmit_iso_data(tester, *, variant):
    """Transmit HCI ISO payloads with RFU fields, timing query or timestamps.

    Initial Condition:
        IUT: one BIS, BN=1, NSE=IRC=3, LE 1M, encryption disabled.
        BI-01 uses Table 4.109: 100-ms ISO/SDU intervals, Max_SDU=Max_PDU=8.
        BV-03 uses unframed BN=1 defaults; BV-05 uses framed defaults with
        19-octet SDUs. Lower Tester follows actual periodic BIGInfo.

    Test Case Configuration:
        HCI/BIS/BI-01-C sets every HCI ISO RFU bit; BV-03-C queries unframed TX
        timing; BV-05-C timestamps framed SDUs with continuously counting data.

    Test Steps:
        1. Create BIG Test, receive Complete, read Buffer Size v2 and create
           the HCI input data path.
        2. Send SDUs. BI-01 sets handle, length and SDU-info RFU bits;
           BV-05 includes the Controller-clock timestamp in each HCI packet.
        3. Receive BIS PDUs, verify CRC, LLID and exact SDU bytes. For framed
           data, strip only the segmentation header and Time_Offset.
        4. BV-03 reads ISO TX Sync and requires Time_Offset=0. BV-05 checks
           the on-air offset against the timestamp and measured BIG anchor.

    Expected Outcome:
        RFU fields and timestamps never become SDU bytes. Unframed TX timing
        returns zero offset; framed SDUs retain the continuous octet counter.

    Reference:
        HCI.TS.p39, HCI/BIS/BI-01-C, Table 4.109; BV-03-C, Steps 1–9;
        BV-05-C, Steps 1–2. Core Vol 4, E, 5.4.5 and 7.8.96.
    """
    framed, small = variant == 'timestamp', variant == 'rfu'
    big, complete = await acquire_broadcaster(
        tester,
        framed=framed,
        interval=80 if small else 8,
        sdu_interval=100000 if small else 10000,
        max_sdu=8 if small else 19 if framed else 32,
        max_pdu=8 if small else 37 if framed else 32,
    )
    if variant == 'sync':
        empty, _ = await big.receive(tester)
        assert all(not data for _, data in empty.values())
    assert (
        await tester.command(hci.HCI_LE_Read_Buffer_Size_V2_Command())
    ).iso_data_packet_length == 251
    handle = complete.connection_handle[0]
    await data_path(tester, handle, 0)
    big.align(tester.radio.now_us)
    size = 8 if small else 19 if framed else 32
    # BV-05 continues the octet counter across SDUs and through FF to 00.
    for sequence in range(256 // size + 1 if framed else 3):
        data = bytes((sequence * size + n) & 255 for n in range(size))
        stamp = tester.radio.now_us if framed else None
        raw = bytearray(bytes(iso_packet(handle, sequence, data, timestamp=stamp)))
        if small:
            raw[2] |= 0x80
            raw[4] |= 0xC0
            raw[8] |= 0xF0
        tester.send(bytes(raw))
        anchor, counter = big.anchor, big.counter
        payloads, _ = await big.receive(tester)
        llid, payload = payloads[(1, counter)]
        assert llid == (2 if framed else 0)
        if framed:
            assert payload[0] & 3 == 2 and payload[1] == size + 3
            offset = int.from_bytes(payload[2:5], 'little')
            assert anchor - stamp - 30 <= offset <= anchor - stamp + 30
            payload = payload[5:]
        assert payload == data
    if variant == 'sync':
        response = await tester.command(hci.HCI_LE_Read_ISO_TX_Sync_Command(handle))
        assert response.packet_sequence_number == 2 and response.time_offset == 0


async def non_test_commands(tester):
    """Broadcast Isochronous Stream Using Non-Test Command, all PHYs.

    Initial Condition:
        IUT: periodic advertiser, all PHYs supported, max TX BISes=16,
        ISO HCI packet length=251, at least two periodic trains supported.

    Test Steps:
        1. Reject a nonexistent advertising train with 0x42, configure a valid
           train, and reject 17 requested BISes with Limited Resources.
        2. Create an unencrypted BIG, receive Complete, observe BIGInfo and
           empty BIS packets, read Buffer Size v2 and create an HCI input path.
        3. Send an ISO SDU. Reject reuse of the advertising train by another
           BIG with 0x42, duplicate path with 0x0C, invalid path handle with 0x02,
           and BIG Terminate Sync on the broadcaster with 0x0C.
        4. Open a second periodic train; reject reuse of the first BIG_Handle
           with 0x0C. Terminate the first BIG through its control instant.
        5. Reject PHY=F8 with 0x11 and latency values 0004/0FA1 with 0x12.
           The unsupported-PHY alternative does not apply to this all-PHY IUT.

    Expected Outcome:
        All valid operations succeed, the BIS payload is transmitted, and each
        invalid operation returns the specified error. Termination completes.

    Reference:
        HCI.TS.p39, HCI/BIS/BV-01-C, Table 4.108, Steps 1–21.
    """
    await tester.masks()
    await tester.command(create_command(advertising_handle=15), 0x42)
    await configure_periodic(tester)
    await tester.command(create_command(num_bis=17), 0x0D)
    await tester.command(create_command())
    complete = await tester.event(hci.HCI_LE_Create_BIG_Complete_Event)
    assert complete.status == 0
    big = await observe_broadcaster(tester)
    await big.receive(tester)
    assert (
        await tester.command(hci.HCI_LE_Read_Buffer_Size_V2_Command())
    ).iso_data_packet_length == 251
    handle = complete.connection_handle[0]
    await data_path(tester, handle, 0)
    big.align(tester.radio.now_us)
    tester.send(bytes(iso_packet(handle, 0, b'non-test ISO')))
    payloads, _ = await big.receive(tester)
    assert (0, b'non-test ISO') in payloads.values()
    await tester.command(create_command(big_handle=1), 0x42)
    for stream, status in ((handle, 0x0C), (0x0EFF, 0x02)):
        await tester.command(
            hci.HCI_LE_Setup_ISO_Data_Path_Command(
                stream, 0, 0, hci.CodingFormat(hci.CodecID.TRANSPARENT), 0, b''
            ),
            status,
        )
    await tester.command(hci.HCI_LE_BIG_Terminate_Sync_Command(0), 0x0C)
    await configure_periodic(tester, 1)
    await tester.command(create_command(advertising_handle=1), 0x0C)
    big.align(tester.radio.now_us)
    await terminate_broadcaster(tester, big)
    await tester.command(create_command(advertising_handle=1, phy=0xF8), 0x11)
    for latency in (4, 0xFA1):
        await tester.command(
            create_command(advertising_handle=1, max_transport_latency=latency), 0x12
        )


async def failure_to_sync(tester):
    """Reporting Failure to Sync to BIS.

    Initial Condition:
        Lower Tester: LE 1M periodic advertiser carrying valid BIGInfo, but no
        BIS is ever transmitted. IUT: extended scanner, then periodic receiver.

    Test Steps:
        1. Receive extended advertising, establish PA synchronization and check
           Periodic Advertising Report followed by BIGInfo Advertising Report.
        2. Request BIG Create Sync for the advertised handle.
        3. Continue PA with BIGInfo while omitting every BIS transmission.
        4. After six BIS opportunities, require failed BIG Sync
           Established with status 0x3E or 0x08 and no BIS handles.

    Expected Outcome:
        No false BIG synchronization success is reported; acquisition fails.

    Reference:
        HCI.TS.p39, HCI/BIS/BI-16-C, Steps 1–9.
    """
    peer, sync_handle = await acquire_periodic_receiver(tester, omit_bis=True)
    await tester.command(
        hci.HCI_LE_BIG_Create_Sync_Command(0, sync_handle, 0, bytes(16), 0, 10, [1])
    )
    first_counter = peer.big.counter
    for _ in range(100):
        event = tester.take(
            lambda value: isinstance(value, hci.HCI_LE_BIG_Sync_Established_Event)
        )
        if event is not None:
            assert event.status in (0x3E, 0x08) and not event.connection_handle
            assert peer.big.counter - first_counter == 6
            return
        await peer.step(tester, omit_bis=True)
    raise AssertionError('BIG acquisition did not fail')


PROCEDURES = ProcedureMap()

# Section 4.15.2 distinguishes invalid ranges from incompatible timing and
# unsupported framing; every row executes its own normative command sequence.
PROCEDURES |= procedure_cases(
    invalid_big_parameters,
    {
        'HCI/BIS/BI-06-C': (
            'Broadcast Isochronous Stream Using Non-Test Command, '
            'Invalid BIG Parameters',
            {},
        )
    },
)
PROCEDURES |= procedure_cases(
    incompatible_latency,
    {
        'HCI/BIS/BI-07-C': (
            'Broadcast Isochronous Stream Using Non-Test Command, '
            'Invalid Transport Latency',
            {'large_sdu': True},
        ),
        'HCI/BIS/BI-11-C': (
            'Broadcast Isochronous Stream Using Non-Test Command, '
            'Invalid BIG Parameters',
            {'large_sdu': False},
        ),
    },
)
PROCEDURES |= procedure_cases(
    unsegmented_unsupported,
    {'HCI/BIS/BI-10-C': ('LE Create BIG, Framed, Unsegmented Mode Unsupported', {})},
)

# Section 4.15.2.3 checks both BIG creation commands against PAwR advertising.
PROCEDURES |= procedure_cases(
    pawr_big_rejected,
    {
        'HCI/BIS/BV-09-C': (
            'Reject BIG created from Periodic Advertising with Responses, '
            'LE Create BIG',
            {'test': False},
        ),
        'HCI/BIS/BV-10-C': (
            'Reject BIG created from Periodic Advertising with Responses, '
            'LE Create BIG Test',
            {'test': True},
        ),
    },
)
PROCEDURES |= procedure_cases(
    coded_periodic_overflow,
    {
        'HCI/BIS/BV-11-C': (
            'Broadcast Isochronous Stream testing overlength data on LE Coded PHY',
            {},
        )
    },
)

# Receiver-only commands and ACL-only authenticated payload supervision keep
# their separate role preambles and normative status codes.
PROCEDURES |= procedure_cases(
    receiver_invalid_commands,
    {
        'HCI/BIS/BI-02-C': (
            'Broadcast Isochronous Stream, Synchronized Receiver, '
            'Reject Invalid Commands',
            {'read_sync': False},
        ),
        'HCI/BIS/BV-04-C': (
            'Broadcast Isochronous Stream, Invalid LE Read ISO TX Sync Parameters',
            {'read_sync': True},
        ),
    },
)
PROCEDURES |= procedure_cases(
    authenticated_timeout_rejected,
    {
        'HCI/BIS/BI-12-C': (
            'Broadcaster, HCI Read/Write Authenticated Payload Timeout error',
            {'broadcaster': True},
        ),
        'HCI/BIS/BI-14-C': (
            'Synchronized Receiver, HCI Read/Write Authenticated Payload Timeout error',
            {'broadcaster': False},
        ),
    },
)
PROCEDURES |= procedure_cases(
    encryption_mode_mismatch,
    {'HCI/BIS/BI-09-C': ('Invalid LE BIG Create Sync Encryption Parameter, BIS', {})},
)


# HCI ISO RFU fields and timestamps are checked against actual BIS payloads;
# acquisition failure keeps the periodic advertiser alive throughout the timeout.
PROCEDURES |= procedure_cases(
    transmit_iso_data,
    {
        'HCI/BIS/BI-01-C': (
            'Ignoring RFU Bits in HCI ISO Data Packets, BIS',
            {'variant': 'rfu'},
        ),
        'HCI/BIS/BV-03-C': (
            'Broadcast Isochronous Stream Using Test Command, Time_Offset',
            {'variant': 'sync'},
        ),
        'HCI/BIS/BV-05-C': (
            'Broadcast Isochronous Stream, Time_Stamp, Isochronous Broadcaster',
            {'variant': 'timestamp'},
        ),
    },
)
PROCEDURES |= procedure_cases(
    non_test_commands,
    {
        'HCI/BIS/BV-01-C': (
            'Broadcast Isochronous Stream Using Non-Test Command, all PHYs',
            {},
        )
    },
)
PROCEDURES |= procedure_cases(
    failure_to_sync, {'HCI/BIS/BI-16-C': ('Reporting Failure to Sync to BIS', {})}
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES, radio_cases=PROCEDURES, radio=True),
)
async def test_procedure(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
