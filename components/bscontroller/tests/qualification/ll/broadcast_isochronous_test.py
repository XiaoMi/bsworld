# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.11: Broadcast Isochronous Streams."""

import random
from itertools import combinations

import pytest

from bscontroller import hci
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.iso import (
    BigTrain,
    FramedSource,
    acquire_broadcaster,
    acquire_periodic_receiver,
    acquire_receiver,
    configure_periodic,
    data_path,
    finish_receiver,
    iso_packet,
    periodic_packet,
    terminate_broadcaster,
    valid_identity,
)


async def broadcast_setup(tester, *, encrypted):
    """Broadcast Isochronous Stream Setup, BN=1.

    Initial Condition:
        IUT: non-connectable extended advertising and periodic advertising
        without responses. Lower Tester: independent periodic observer.
        Use Table 4.11-2 defaults: Num_BIS=BN=1, ISO/SDU interval 10 ms,
        NSE=IRC=3, PTO=0, Max_SDU=Max_PDU=32, LE 1M, sequential, unframed.
        TSPX_max_iso_pkt=251; Broadcast_Code is the 16-octet IXIT code in iso.py.

    Test Case Configuration:
        LL/BIS/BRD/BV-23-C disables encryption; BV-24-C enables encryption and
        requires GIV/GSKD and valid MIC on every nonempty BIS Data PDU.

    Test Steps:
        1. Create BIG Test with the selected defaults and receive successful
           Command Status and Create BIG Complete with matching parameters.
        2. Follow an extended announcement's SyncInfo to AUX_SYNC_IND, decode
           BIGInfo, and check its encryption fields, PHY and offset encoding.
        3. Receive an empty BIS event on independently calculated CSA #2
           channels and check every subevent's CRC, timing and header.
        4. Read Buffer Size v2 and set up the HCI input ISO data path.
        5. Send random 32-octet SDUs for 50 ISO events. Receive every subevent,
           decrypt if needed, verify MIC, payload, LLID and repeated data.
        6. Sum Number Of Completed Packets for the BIS handle.

    Expected Outcome:
        BIGInfo and Create BIG Complete agree with the selected configuration.
        Each subevent has a valid PDU, Sub_Interval >= MPT + 150 us and the
        final PDU ends at least 150 us before the next anchor. At least 95% of
        the 50 SDUs match the Upper Tester's data. Completed HCI packet count
        equals the number sent; encrypted nonempty payloads authenticate.

    Reference:
        LL.TS.p28, 4.11.4.1, Tables 4.11-2/3/4, Figure 4.11-1, Steps 1–9;
        LL/BIS/BRD/BV-23-C and BV-24-C. Core Vol 6, B, 4.4.6 and G, 2.1.
    """
    big, complete = await acquire_broadcaster(tester, encrypted=encrypted)
    assert (
        complete.nse,
        complete.bn,
        complete.pto,
        complete.irc,
        complete.max_pdu,
        complete.phy,
    ) == (3, 1, 0, 3, 32, 1)
    assert big.nse == 3 and big.bn == 1 and big.irc == 3 and not big.pto
    mpt = big.phy.airtime_us(2 + big.max_pdu + (4 if encrypted else 0))
    assert big.sub_interval >= mpt + 150
    assert (big.nse - 1) * big.sub_interval + mpt + 150 <= big.interval_us
    empty, control = await big.receive(tester)
    assert control is None and all(not data for _, data in empty.values())
    buffers = await tester.command(hci.HCI_LE_Read_Buffer_Size_V2_Command())
    assert buffers.iso_data_packet_length == 251
    handle = complete.connection_handle[0]
    await data_path(tester, handle, 0)
    big.align(tester.radio.now_us)
    generator = random.Random(17)
    matches = 0
    for sequence in range(50):
        data = generator.randbytes(32)
        tester.send(bytes(iso_packet(handle, sequence, data)))
        payloads, control = await big.receive(tester)
        assert control is None
        matches += payloads.get((1, (big.counter - 1) * big.bn)) == (0, data)
    assert matches >= 48
    completed = 0
    while (
        event := tester.take(
            lambda value: isinstance(value, hci.HCI_Number_Of_Completed_Packets_Event)
        )
    ) is not None:
        completed += sum(
            count
            for stream, count in zip(
                event.connection_handles, event.num_completed_packets
            )
            if stream == handle
        )
    assert completed == 50


async def _send_event(tester, peer, payloads, **options):
    while not await peer.step(tester, payloads, **options):
        pass


async def _received_sdus(tester, peer, handle):
    # Let the last BIS event finish without waiting for an HCI packet on a
    # deliberately missing SDU or skipping the peer's next radio activity.
    next_anchor = min(peer.big.anchor, peer.advertiser.train.anchor)
    await tester.wait(max(0, next_anchor - tester.radio.now_us - 1))
    packets = []
    while (
        packet := tester.take(lambda p: isinstance(p, hci.HCI_IsoDataPacket))
    ) is not None:
        assert packet.connection_handle == handle and packet.packet_status_flag in (
            0,
            1,
            2,
        )
        packets.append(packet)
    return packets


def _matching_sdus(packets, sent):
    # Test payloads identify their own position, so losing one SDU does not
    # shift the expected data for all following SDUs.
    indices = {data: index for index, data in enumerate(sent)}
    delivered = []
    for packet in packets:
        if packet.packet_status_flag == 0:
            assert packet.iso_sdu_fragment in indices
            index = indices[packet.iso_sdu_fragment]
            assert not delivered or index > delivered[-1]
            delivered.append(index)
    return delivered


async def receiver_setup(tester, *, encrypted):
    """Broadcast Isochronous Stream Synchronization Setup, BN=1.

    Initial Condition:
        IUT: passive extended scanner, Filter_Policy=0. Lower Tester: one BIS
        and periodic advertising without responses, using Table 4.11-2 BN=1
        defaults. Broadcast_Code is the IXIT code for the encrypted case and
        all zero for the unencrypted request.

    Test Case Configuration:
        LL/BIS/SNC/BV-15-C disables encryption; BV-16-C enables encryption.

    Test Steps:
        1. Receive Extended Advertising Report, create PA synchronization,
           receive successful Sync Established, PA Report then BIGInfo Report.
        2. Request BIG Create Sync and receive successful BIG Sync Established.
        3. Reject an input ISO path with 0x0C; create the output HCI ISO path.
        4. Send random BIS SDUs for 50 ISO intervals. Require the same data,
           sequence progression and valid status in the Upper Tester's ISO data.
        5. Unencrypted: terminate BIG synchronization through HCI. Encrypted:
           corrupt MIC and require BIG Sync Lost, reason 0x3D.
        6. Repeat with BIGInfo pointing to the second future BIG event instead
           of the first. Keep PA timing and the bisPayloadCount consistent.

    Expected Outcome:
        BIGInfo Report fields match the advertised BIG. Both offset choices
        synchronize, at least 95% of 50 SDUs are delivered correctly, the wrong
        path direction is rejected, and authentication failure ends only BIG sync.

    Source Correction:
        The source's final paragraph names Periodic Advertising Sync Lost for
        MIC failure. Step 13B.2 and Core Vol 6, E, 1 require BIG Sync Lost with
        reason 0x3D; periodic advertising remains independently synchronized.

    Reference:
        LL.TS.p28, 4.11.5.1, Table 4.11-18, Figure 4.11-14, Steps 1–14;
        LL/BIS/SNC/BV-15-C and BV-16-C.
    """
    generator = random.Random(23)
    for offset_events in (1, 2):
        peer, synchronized = await acquire_receiver(
            tester, encrypted=encrypted, offset_events=offset_events
        )
        assert synchronized.status == 0
        handle = synchronized.connection_handle[0]
        await tester.command(
            hci.HCI_LE_Setup_ISO_Data_Path_Command(
                handle, 0, 0, hci.CodingFormat(hci.CodecID.TRANSPARENT), 0, b''
            ),
            0x0C,
        )
        await data_path(tester, handle, 1)
        sent = [generator.randbytes(32) for _ in range(50)]
        for data in sent:
            await _send_event(tester, peer, {1: (0, data)})
        packets = await _received_sdus(tester, peer, handle)
        delivered = _matching_sdus(packets, sent)
        assert len(delivered) >= 48
        # For this unframed BN=1 stream each SDU advances the HCI sequence.
        valid = [p for p in packets if p.packet_status_flag == 0]
        assert all(
            (p.packet_sequence_number - valid[0].packet_sequence_number) & 0xFFFF
            == index - delivered[0]
            for p, index in zip(valid, delivered)
        )
        if encrypted:
            await _send_event(tester, peer, {1: (0, b'MIC failure')}, mic_error=True)
            lost = await tester.event(hci.HCI_LE_BIG_Sync_Lost_Event)
            assert lost.big_handle == 0 and lost.reason == 0x3D
        await finish_receiver(tester, peer, terminate=not encrypted)


async def receiver_loss(tester):
    """Loss of Sync with an Isochronous Broadcaster.

    Initial Condition:
        IUT: synchronized to one unencrypted BN=1 BIS using the common defaults
        and a 100-ms BIG supervision timeout.

    Test Steps:
        1. Establish BIG synchronization through the independent Lower Tester.
        2. Stop all Lower Tester transmissions abruptly.
        3. Receive BIG Sync Lost for the established BIG with reason 0x08.

    Expected Outcome:
        Synchronization times out and reports the correct BIG_Handle and reason.

    Reference:
        LL.TS.p28, LL/BIS/SNC/BV-11-C, Steps 1–2; Core Vol 4, E, 7.7.65.30.
    """
    _, event = await acquire_receiver(tester)
    assert event.status == 0
    await tester.wait(110000)
    lost = await tester.event(hci.HCI_LE_BIG_Sync_Lost_Event)
    assert lost.big_handle == 0 and lost.reason == 8


async def reserved_big_opcodes(tester):
    """Ignore Restricted for Future Use Opcodes, BIS.

    Initial Condition:
        IUT: synchronized receiver of an unencrypted BN=1 BIS, with an output
        HCI path. Lower Tester continues periodic advertising and BIS data.

    Test Steps:
        1. For every opcode F6..FF in Table 4.11-26, send the RFU BIG Control
           PDU on the independently derived control AA/channel at Control_Offset.
        2. Continue 20 BIG events for that opcode, delivering distinct BIS data.
        3. Check all Upper Tester SDUs and absence of BIG Sync Lost.

    Expected Outcome:
        All ten RFU opcodes are ignored; BIS payload and synchronization continue
        throughout every 20-event observation window.

    Reference:
        LL.TS.p28, LL/BIS/SNC/BI-04-C, Table 4.11-26, Steps 1–5.
    """
    peer, event = await acquire_receiver(tester)
    assert event.status == 0
    await data_path(tester, event.connection_handle[0], 1)
    for index, opcode in enumerate(range(0xF6, 0x100)):
        for number in range(20):
            data = bytes([opcode, number]) * 16
            await _send_event(
                tester,
                peer,
                {1: (0, data)},
                control=bytes([opcode]) if number == 0 else None,
                cssn=(index + 1) % 8,
            )
            received = await tester.event(hci.HCI_IsoDataPacket)
            assert (
                received.packet_status_flag == 0 and received.iso_sdu_fragment == data
            )
        assert (
            tester.take(lambda value: isinstance(value, hci.HCI_LE_BIG_Sync_Lost_Event))
            is None
        )


async def reserved_big_phy(tester):
    """Broadcast Isochronous Stream Synchronization, Unsupported Features.

    Initial Condition:
        IUT: passive extended scanner with all advertising reports enabled.
        Lower Tester advertises valid periodic timing with BIGInfo PHY=3 (RFU).

    Test Steps:
        1. Receive Extended Advertising Report and establish PA synchronization.
        2. Transmit twenty periodic events carrying the RFU BIGInfo PHY value.
        3. Check PA reports continue, while no BIGInfo Report or BIG Sync
           Established is emitted.

    Expected Outcome:
        The invalid BIGInfo is ignored without losing periodic synchronization.

    Source Correction:
        The source's legacy Scan Parameters/Enable commands precede an extended
        report and PA Create Sync. Core Vol 4, E, 3.1.1 prohibits mixing command
        families after Reset; use passive extended scanning for this preamble.

    Reference:
        LL.TS.p28, 4.11.5.5, Table 4.11-25, LL/BIS/SNC/BI-03-C, Steps 1–8.
    """
    peer, _ = await acquire_periodic_receiver(
        tester, expect_big_info=False, advertised_phy=3
    )
    start = peer.advertiser.train.counter
    while peer.advertiser.train.counter < start + 20:
        await peer.step(tester, omit_bis=True)
    assert (
        tester.take(
            lambda value: isinstance(
                value, hci.HCI_LE_Periodic_Advertising_Report_Event
            )
        )
        is not None
    )
    assert (
        tester.take(
            lambda value: isinstance(
                value,
                (
                    hci.HCI_LE_BIGInfo_Advertising_Report_Event,
                    hci.HCI_LE_BIG_Sync_Established_Event,
                ),
            )
        )
        is None
    )


async def broadcaster_termination(tester, *, encrypted):
    """Isochronous Broadcaster Terminates BIS Stream.

    Initial Condition:
        IUT: one BN=1 BIS using common defaults; periodic advertising remains
        enabled. Lower Tester derives data and control identities from BIGInfo.

    Test Case Configuration:
        LL/BIS/BRD/BV-10-C is unencrypted; BV-18-C is encrypted with the IXIT code.

    Test Steps:
        1. Send Terminate BIG and receive successful Command Status.
        2. Receive at least six identical BIG_TERMINATE_IND control payloads,
           checking reason, instant, CSSN/CSTF, control AA/channel and MIC.
        3. Observe termination at Instant and receive Terminate BIG Complete.
        4. Follow a subsequent AUX_SYNC_IND; require absence of BIGInfo.

    Expected Outcome:
        All control packets meet Core timing and counter rules; completion has
        Reason=0x16. Subsequent periodic advertising contains no terminated BIG.

    Source Correction:
        The verdict names both 0x00 and 0x16 for completion. Step 4 and Core
        Vol 4, E, 7.7.65.28 require Connection Terminated by Local Host, 0x16.

    Reference:
        LL.TS.p28, 4.11.4.4, Table 4.11-12, Figure 4.11-6, Steps 1–5.
    """
    big, _ = await acquire_broadcaster(tester, encrypted=encrypted)
    await terminate_broadcaster(tester, big)
    assert BigTrain.from_periodic(await periodic_packet(tester)) is None


async def broadcaster_channel_map(tester, *, encrypted):
    """Broadcast Isochronous Group Channel Map Update Procedure, Broadcaster.

    Initial Condition:
        IUT: one BN=1 BIS with default timing and an HCI input path. Lower
        Tester receives every subevent using the old BIGInfo channel map.

    Test Case Configuration:
        LL/BIS/BRD/BV-09-C disables encryption; BV-17-C enables encryption.

    Test Steps:
        1. Send continuous HCI SDUs; disable the BIG's periodic advertising.
        2. Set Host Channel Classification to 0x0AAAAAAAAA.
        3. Receive BIG_CHANNEL_MAP_IND for at least six consecutive events.
           Require fixed eight-octet payload, stable CSSN, CSTF and Instant
           at least six events after its first transmission; authenticate MIC.
        4. Apply the indicated map exactly at Instant and receive 20 events,
           checking their independent CSA #2 channels and exact SDU bytes.
        5. Re-enable periodic advertising and verify the new BIGInfo ChM.
        6. Terminate the BIG through its control sequence.

    Expected Outcome:
        At least 95% of SDUs in the first 20 events after Instant match. The
        new map appears on air and in BIGInfo; all control headers and MICs hold.

    Reference:
        LL.TS.p28, 4.11.4.3, Table 4.11-11, Figure 4.11-5, Steps 1–11.
    """
    big, complete = await acquire_broadcaster(tester, encrypted=encrypted)
    handle = complete.connection_handle[0]
    await data_path(tester, handle, 0)
    await tester.command(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0, 0))
    big.align(tester.radio.now_us)
    # Start the request after a complete observed event, leaving a full interval
    # for its HCI response before the first new control transmission.
    await big.receive(tester)
    new_map = 0x0AAAAAAAAA
    await tester.command(
        hci.HCI_LE_Set_Host_Channel_Classification_Command(
            new_map.to_bytes(5, 'little')
        )
    )
    sequence = 0

    def supply():
        nonlocal sequence
        tester.send(bytes(iso_packet(handle, sequence, bytes([sequence & 255]) * 32)))
        sequence += 1

    await big.receive_control(
        tester, b'\0' + new_map.to_bytes(5, 'little'), supply=supply
    )
    updated_cssn = big.last_cssn
    big.channel_map = new_map
    valid = 0
    for _ in range(20):
        data = bytes([sequence & 255]) * 32
        tester.send(bytes(iso_packet(handle, sequence, data)))
        sequence += 1
        payloads, control = await big.receive(tester)
        assert control is None and big.last_cssn == updated_cssn
        valid += (0, data) in payloads.values()
    assert valid >= 19
    await tester.command(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    updated = BigTrain.from_periodic(await periodic_packet(tester))
    assert updated is not None and updated.channel_map == new_map
    await terminate_broadcaster(tester, big)


async def non_test_broadcast(tester, *, phy):
    """Broadcast Isochronous Stream Using Non-Test Command.

    Initial Condition:
        IUT: periodic advertiser. Lower Tester observes its BIGInfo and BIS.
        RTN=1, BN=1 defaults, encryption disabled; ISO buffers are 251 octets.

    Test Case Configuration:
        LL/BIS/BRD/BV-13-C uses LE 1M and 30-ms latency; BV-14-C uses LE 2M
        and 30 ms; BV-15-C uses LE Coded and 160 ms (Table 4.11-13).

    Test Steps:
        1. Create BIG with Framing=0 and the selected PHY/latency. Verify
           successful Complete, legal BIGInfo geometry and empty BIS data.
        2. Read Buffer Size v2, set up the HCI input path, and transmit alternating
           81/16 octets. Verify on-air SDUs and clear RFU fields.
        3. Configure a second periodic train. Terminate the first BIG by observing
           six BIG_TERMINATE_IND packets and its completion.
        4. Create BIG on the second train with Framing=1. Send alternating FA/ED
           octets and verify segmentable framed payloads and latency.
        5. Terminate the second BIG through its control instant.

    Expected Outcome:
        Both BIGs use the selected PHY. Reported and independently computed
        latencies do not exceed the requested maximum, and every SDU matches.

    Reference:
        LL.TS.p28, 4.11.4.5, Table 4.11-13, Figures 4.11-7/8, Steps 1–13.
    """
    latency = 160 if phy == 4 else 30
    for framed, pair in ((False, b'\x81\x16'), (True, b'\xfa\xed')):
        big, complete = await acquire_broadcaster(
            tester,
            advertising_handle=int(framed),
            test=False,
            framed=framed,
            phy=phy,
            rtn=1,
            latency_ms=latency,
            configured=framed,
        )
        assert complete.phy == {1: 1, 2: 2, 4: 3}[phy]
        assert (
            complete.transport_latency_big == big.transport_latency_us <= latency * 1000
        )
        empty, _ = await big.receive(tester)
        assert all(not data for _, data in empty.values())
        assert (
            await tester.command(hci.HCI_LE_Read_Buffer_Size_V2_Command())
        ).iso_data_packet_length == 251
        handle = complete.connection_handle[0]
        await data_path(tester, handle, 0)
        big.align(tester.radio.now_us)
        for sequence in range(20):
            data = (pair * 16)[: big.max_sdu]
            tester.send(bytes(iso_packet(handle, sequence, data)))
            payloads, _ = await big.receive(tester)
            llid, payload = payloads[(1, (big.counter - 1) * big.bn)]
            assert llid == (2 if framed else 0)
            if framed:
                assert payload[0] & 3 == 2
                payload = payload[5:]
            assert payload == data
        if not framed:
            await configure_periodic(tester, 1)
            big.align(tester.radio.now_us)
        await terminate_broadcaster(tester, big)


async def force_framed_broadcast(tester, *, phy):
    """Broadcast Isochronous Stream Setup, Force Framed PDUs.

    Initial Condition:
        IUT: periodic advertiser. Lower Tester observes BIGInfo. One BIS,
        SDU_Interval=87072 us, RTN=3, Max_Transport_Latency=800 ms,
        Max_SDU=32, Framing=0 and encryption disabled.

    Test Case Configuration:
        LL/BIS/BRD/BV-20-C uses LE 1M; BV-21-C LE 2M; BV-22-C LE Coded.

    Test Steps:
        1. Create BIG using the non-test command and selected PHY.
        2. Verify successful Complete and legal BIGInfo with Framed=1.
        3. Read Buffer Size v2 and configure the HCI input ISO data path.
        4. Send alternating 7C/DE data; verify framed BIS payload bytes.
        5. Compare the reported latency with the independently computed ISOAL
           transport latency and the command's 800-ms limit.

    Expected Outcome:
        The incompatible unframed interval causes segmentable framed PDUs;
        PHY, geometry, payload and the requested latency bound all hold.

    Source Conflict:
        Step 1 sets 800 ms but the final verdict additionally requires 80 ms.
        Framed BIS latency is BIG_Sync_Delay + ISO_Interval + SDU_Interval
        even without pre-transmission, already greater than 87072 us. Execute
        the command's 800-ms bound; the contradictory 80-ms bound remains an
        unresolved source conflict and this case reports PARTIAL.

    Reference:
        LL.TS.p28, 4.11.4.7, Table 4.11-16, Figure 4.11-10, Steps 1–8;
        Core Vol 6, G, 3.2.1.
    """
    big, complete = await acquire_broadcaster(
        tester,
        test=False,
        phy=phy,
        sdu_interval=87072,
        max_sdu=32,
        rtn=3,
        latency_ms=800,
        expected_framed=True,
    )
    assert big.framed and not big.mode
    assert complete.phy == {1: 1, 2: 2, 4: 3}[phy]
    assert complete.transport_latency_big == big.transport_latency_us <= 800000
    empty, _ = await big.receive(tester)
    assert all(not data for _, data in empty.values())
    buffers = await tester.command(hci.HCI_LE_Read_Buffer_Size_V2_Command())
    assert buffers.iso_data_packet_length == 251
    handle = complete.connection_handle[0]
    await data_path(tester, handle, 0)
    big.align(tester.radio.now_us)
    data = b'\x7c\xde' * 16
    tester.send(bytes(iso_packet(handle, 0, data)))
    payloads, _ = await big.receive(tester)
    llid, payload = payloads[(1, (big.counter - 1) * big.bn)]
    assert llid == 2 and payload[0] & 3 == 2 and payload[5:] == data


async def framing_parameter(tester, *, framed):
    """LE Create BIG Test Framing Parameter.

    Initial Condition:
        IUT: periodic advertiser. ISO_Interval=SDU_Interval=20 ms; use default
        BN=1, NSE=IRC=3, LE 1M, no encryption. Lower Tester observes BIGInfo.

    Test Case Configuration:
        LL/BIS/BRD/BV-31-C selects Framing=0, Framed=0, Framing_Mode=0;
        BV-32-C selects Framing=1, Framed=1, Framing_Mode=0.

    Test Steps:
        1. Create BIG Test with the selected framing and receive success.
        2. Receive periodic BIGInfo and verify Framed/Framing_Mode.
        3. Read Buffer Size v2 and create the HCI input data path.
        4. Send random SDU data of min(251-meta, 4095, Max_SDU) octets and
           verify the BIS format and exact recovered bytes.

    Expected Outcome:
        The selected format appears in both BIGInfo and BIS data; no SDU
        contents are changed by ISOAL framing.

    Reference:
        LL.TS.p28, 4.11.4.8, Table 4.11-17, Figure 4.11-13, Steps 1–9.
    """
    big, complete = await acquire_broadcaster(
        tester, framed=framed, interval=16, sdu_interval=20000
    )
    assert big.framed == framed and big.mode == 0
    buffers = await tester.command(hci.HCI_LE_Read_Buffer_Size_V2_Command())
    await data_path(tester, complete.connection_handle[0], 0)
    big.align(tester.radio.now_us)
    data = random.Random(37).randbytes(
        min(buffers.iso_data_packet_length - 4, 4095, big.max_sdu)
    )
    tester.send(bytes(iso_packet(complete.connection_handle[0], 0, data)))
    payloads, _ = await big.receive(tester)
    llid, payload = payloads[(1, (big.counter - 1) * big.bn)]
    assert llid == (2 if framed else 0)
    assert (payload[5:] if framed else payload) == data


async def maximum_broadcast_groups(tester, *, encrypted):
    """Maximum Supported Broadcast Isochronous Groups.

    Initial Condition:
        IUT: TSPX_max_tx_bigs=4 and TSPX_max_tx_bises=16. Establish four BIGs,
        each with one BIS, ISO/SDU intervals 101.25 ms, BN=NSE=IRC=1,
        Max_SDU=Max_PDU=16 and its own periodic advertiser (Table 4.11-14).
        Each BIG's SDU repeats its group number in every octet.

    Test Case Configuration:
        LL/BIS/BRD/BV-16-C disables encryption; BV-19-C enables encryption.

    Test Steps:
        1. Create all four BIGs, receive their periodic BIGInfo independently,
           read Buffer Size v2 and configure all four HCI input data paths.
        2. Check every pair of SAA low words differs by at least two bits.
           Check all BIS/control AAs against the full waveform constraints.
           For encryption, require distinct GIVs and distinct GSKDs.
        3. Supply group-number SDUs and observe every BIG on its own schedule,
           validating data, AA, CRC, timing, channel selection and MIC.
        4. Terminate the first BIG through six control events and Instant,
           continuing to supply SDUs for all still active groups.
        5. Repeat Terminate BIG for that handle; require 0x42.
        6. Terminate each remaining BIG through its control procedure.

    Expected Outcome:
        Four independent groups operate with distinct valid identities and
        security material. Every group's data is observed and all terminate.

    Reference:
        LL.TS.p28, 4.11.4.6, Tables 4.11-14/15, Figure 4.11-9, Steps 1–9.
    """
    groups = []
    for number in range(4):
        big, complete = await acquire_broadcaster(
            tester,
            encrypted=encrypted,
            advertising_handle=number,
            big_handle=number,
            interval=81,
            sdu_interval=101250,
            nse=1,
            irc=1,
            max_sdu=16,
            max_pdu=16,
        )
        assert (
            big.interval,
            big.sdu_interval,
            big.nse,
            big.irc,
            big.max_sdu,
            big.max_pdu,
        ) == (81, 101250, 1, 1, 16, 16)
        assert (
            await tester.command(hci.HCI_LE_Read_Buffer_Size_V2_Command())
        ).iso_data_packet_length == 251
        await data_path(tester, complete.connection_handle[0], 0)
        groups.append((big, complete.connection_handle[0], number))
    for (first, _, _), (second, _, _) in combinations(groups, 2):
        assert ((first.seed ^ second.seed) & 0xFFFF).bit_count() >= 2
        assert not encrypted or (first.giv != second.giv and first.gskd != second.gskd)
    addresses = [big.address(n) for big, _, _ in groups for n in range(big.num_bis + 1)]
    assert len(addresses) == len(set(addresses)) and all(map(valid_identity, addresses))
    for big, _, _ in groups:
        assert (big.seed >> 19 & 1) == (big.seed >> 15 & 1)
        assert (big.seed >> 22 & 1) == (big.seed >> 16 & 1) != (big.seed >> 15 & 1)
        assert not big.seed & 1 << 25 and big.seed & 1 << 23
        big.align(tester.radio.now_us)
    sequence = 0

    def supply():
        nonlocal sequence
        for _, handle, number in groups:
            tester.send(bytes(iso_packet(handle, sequence, bytes([number + 1]) * 16)))
        sequence += 1

    # Keep a short continuous stream queued: a Host write just before an anchor
    # may reach the Controller after that event's radio preparation boundary.
    supply()
    supply()
    await tester.wait(max(big.interval_us for big, _, _ in groups))
    for _ in range(5):
        for big, _, _ in groups:
            big.align(tester.radio.now_us)
        supply()
        for big, _, number in sorted(groups, key=lambda entry: entry[0].anchor):
            big.align(tester.radio.now_us)
            payloads, _ = await big.receive(tester)
            assert (0, bytes([number + 1]) * 16) in payloads.values()

    while groups:
        big, _, number = groups[0]
        big.align(tester.radio.now_us)
        await terminate_broadcaster(tester, big, number, supply=supply)
        await tester.command(hci.HCI_LE_Terminate_BIG_Command(number, 0x13), 0x42)
        groups.pop(0)


async def receiver_channel_map(tester, *, encrypted):
    """BIG Channel Map Update Procedure, Synchronized Receiver.

    Initial Condition:
        IUT: synchronized to one BN=1 BIS and its periodic train, with an output
        HCI path. Lower Tester uses 31 C5 octets in PA and 32 BA octets per SDU.

    Test Case Configuration:
        LL/BIS/SNC/BV-09-C changes both independent maps without encryption.
        BV-13-C is encrypted and stops PA to isolate BIG control handling.

    Test Steps:
        1. Continue BIS data and stop extended advertising. Encrypted: stop PA.
        2. Unencrypted: send PA Channel Map Update Indication for at least six
           PA events, then change PA channels to 0x1939393939 at its Instant.
        3. Send BIG_CHANNEL_MAP_IND with map 0x1AAAAAAAAA for at least six BIG
           events; select the first BIG instant following the PA map instant.
        4. Switch BIG channels exactly at Instant and continue 20 BIG events.
        5. Check every Upper Tester BIS SDU and enabled periodic data reports.

    Expected Outcome:
        Both map changes preserve synchronization and data. Encrypted BIG
        control is authenticated without assistance from periodic advertising.

    Reference:
        LL.TS.p28, 4.11.5.3, Table 4.11-23, Figure 4.11-19, Steps 1–8.
    """
    peer, event = await acquire_receiver(tester, encrypted=encrypted)
    assert event.status == 0
    await data_path(tester, event.connection_handle[0], 1)
    peer.advertiser.advertising = False
    peer.periodic_enabled = not encrypted
    peer.periodic_data = b'\xc5' * 31
    train, big = peer.advertiser.train, peer.big
    pa_instant = train.counter + 6
    pa_time = train.anchor + 6 * train.interval * 1250
    instant = big.counter + max(7, (pa_time - big.anchor) // big.interval_us + 1)
    if not encrypted:
        peer.periodic_update = (0x1939393939, pa_instant)
    new_map = 0x1AAAAAAAAA
    peer.big_update = (new_map, instant)
    control = (
        b'\0' + new_map.to_bytes(5, 'little') + (instant & 0xFFFF).to_bytes(2, 'little')
    )
    controls, post_periodic = 0, 0
    while big.counter < instant + 20:
        changing = big.counter < instant
        await _send_event(
            tester,
            peer,
            {1: (0, b'\xba' * 32)},
            control=control if changing else None,
            cssn=1,
        )
        controls += changing
        received = await tester.event(hci.HCI_IsoDataPacket)
        assert (
            received.packet_status_flag == 0
            and received.iso_sdu_fragment == b'\xba' * 32
        )
        while (
            report := tester.take(
                lambda value: isinstance(
                    value, hci.HCI_LE_Periodic_Advertising_Report_Event
                )
            )
        ) is not None:
            if report.data:
                assert report.data == b'\xc5' * 31
                post_periodic += train.counter > pa_instant + 1
    assert controls >= 6 and (encrypted or post_periodic > 0)
    assert (
        tester.take(lambda value: isinstance(value, hci.HCI_LE_BIG_Sync_Lost_Event))
        is None
    )


async def receiver_termination(tester, *, encrypted):
    """Broadcast Isochronous Stream Termination.

    Initial Condition:
        IUT: one synchronized BN=1 BIS. Lower Tester advertises periodic timing
        and uses the selected encryption mode and IXIT Broadcast_Code.

    Test Case Configuration:
        LL/BIS/SNC/BV-10-C disables encryption; BV-14-C enables encryption.

    Test Steps:
        1. Stop BIGInfo in PA ACAD and transmit six consecutive encrypted or
           clear BIG_TERMINATE_IND packets with Reason=13 and a future Instant.
        2. Stop BIS at Instant; require BIG Sync Lost with Reason=13.
        3. Synchronize to a new BIG using a different BIG_Handle (1), with a
           fresh periodic preamble and fresh encryption material.
        4. Send BIG Terminate Sync for the previous handle (0); require 0x42.

    Expected Outcome:
        Remote termination is reported at Instant, the new BIG synchronizes,
        and the previous BIG handle remains invalid.

    Observation Setup:
        A different handle in Step 3 makes Step 4 name the previous BIG as the
        source requires, avoiding termination of the newly synchronized BIG.

    Reference:
        LL.TS.p28, 4.11.5.4, Table 4.11-24, Figure 4.11-20, Steps 1–4.
    """
    peer, event = await acquire_receiver(tester, encrypted=encrypted)
    assert event.status == 0
    peer.include_info = False
    instant = peer.big.counter + 6
    control = b'\x01\x13' + (instant & 0xFFFF).to_bytes(2, 'little')
    while peer.big.counter < instant:
        await _send_event(tester, peer, {1: (0, b'\xba' * 32)}, control=control, cssn=1)
    lost = await peer.until_event(tester, hci.HCI_LE_BIG_Sync_Lost_Event, omit_bis=True)
    assert lost.big_handle == 0 and lost.reason == 0x13
    assert tester.radio.now_us >= peer.big.anchor - peer.big.interval_us
    await finish_receiver(tester, peer, terminate=False)
    peer, event = await acquire_receiver(tester, encrypted=encrypted, big_handle=1)
    assert event.status == 0 and event.big_handle == 1
    await tester.command(hci.HCI_LE_BIG_Terminate_Sync_Command(0), 0x42)
    await finish_receiver(tester, peer)


async def receiver_no_pretransmission(tester):
    """Broadcast Isochronous Stream Synchronization No Pre-Transmission.

    Initial Condition:
        Lower Tester: one unencrypted unframed BIS, BN=1, IRC=2, NSE=3, PTO=1,
        SDU_Interval=ISO_Interval=10 ms. IUT uses immediate repetitions and no
        pre-transmission cache. A valid periodic BIGInfo report is supplied.

    Test Steps:
        1. Establish PA synchronization. If BIGInfo is absent for 20 PA intervals,
           end with Pass. Otherwise establish BIG sync and the HCI output path.
        2. Send identical counter-bearing data in subevents 1/2 and all FF octets
           in subevent 3, which belongs to the future associated event.
        3. Every third event, alternately omit subevents 1/2 or corrupt their CRC.
           Continue transmitting subevent 3 normally.
        4. Repeat for 120 ISO intervals; compare each delivered SDU and status.
        5. Terminate BIG synchronization through HCI.

    Expected Outcome:
        At least 95% of undamaged SDUs arrive intact. Damaged events report an
        error. All-FF future payloads are never delivered to the Upper Tester.

    Reference:
        LL.TS.p28, LL/BIS/SNC/BV-22-C, Figures 4.11-27/28,
        Steps 1–12.
    """
    peer, event = await acquire_receiver(
        tester, irc=2, nse=3, pto=1, optional_info_intervals=20
    )
    if event is None:
        return
    assert event.status == 0
    await data_path(tester, event.connection_handle[0], 1)
    sent = []
    for index in range(120):
        data = peer.big.counter.to_bytes(4, 'little') + b'\xba' * 28
        sent.append(data)
        damaged = index % 3 == 2
        omit = damaged and index // 3 % 2 == 1
        payloads = {1: (0, data), (1, 2): (0, b'\xff' * 32)}
        if omit:
            payloads |= {(1, 0): None, (1, 1): None}
        await _send_event(
            tester,
            peer,
            payloads,
            bad_crc=lambda subevent: damaged and not omit and subevent < 2,
        )
    packets = await _received_sdus(tester, peer, event.connection_handle[0])
    assert all(packet.iso_sdu_fragment != b'\xff' * 32 for packet in packets)
    delivered = _matching_sdus(packets, sent)
    assert len(delivered) >= 76 and all(index % 3 != 2 for index in delivered)
    first = next(packet for packet in packets if packet.packet_status_flag == 0)
    first_sequence = first.packet_sequence_number - delivered[0]
    errors = {
        (packet.packet_sequence_number - first_sequence) & 0xFFFF
        for packet in packets
        if packet.packet_status_flag in (1, 2)
    }
    assert set(range(2, 120, 3)) <= errors
    await finish_receiver(tester, peer)


async def receiver_sdu_length(tester, *, framed):
    """BIS change of SDU length during reception.

    Initial Condition:
        Lower Tester: one clear BIS, BN=1, NSE=IRC=2, SDU interval 10 ms.
        Use unframed Max_SDU/Max_PDU=32/32 or framed 19/37. IUT is synchronized
        to periodic advertising and BIG, with an HCI output path.

    Test Case Configuration:
        LL/BIS/SNC/BV-23-C: unframed, ISO interval 10 ms.
        BV-24-C: framed, ISO interval 13.75 ms (1.375 times the SDU interval).

    Test Steps:
        1. Establish BIG synchronization and configure the output path.
        2. Send random Max_SDU-length data for at least 20 ISO events.
        3. Change to ceil(0.3*Max_SDU) and continue at least 20 ISO events.
        4. Change each following SDU to a different random length in 1..Max_SDU
           and continue at least 20 ISO events.
        5. For framed data, generate SDUs at 10-ms intervals and independently
           segment/aggregate them into 37-octet PDUs at 13.75-ms ISO intervals.
        6. Compare every delivered SDU against the ordered source data, including
           the first SDU after each length change.

    Expected Outcome:
        At least 90% of transmitted SDUs arrive and their bytes are unchanged;
        the first SDU after each change is received in both framing modes.

    Reference:
        LL.TS.p28, 4.11.5.7, Table 4.11-29, Figure 4.11-29, Steps 1–7.
    """
    peer, event = await acquire_receiver(
        tester, framed=framed, nse=2, irc=2, spacing=1200, interval=11 if framed else 8
    )
    assert event.status == 0
    await data_path(tester, event.connection_handle[0], 1)
    big = peer.big
    encoder = FramedSource(big.max_pdu)
    sent = []
    generator = random.Random(41)
    reference = big.anchor - 1000
    first_changed = {}
    previous_size = big.max_sdu

    def next_data(phase):
        nonlocal previous_size
        if phase == 0:
            size = big.max_sdu
        elif phase == 1:
            size = (3 * big.max_sdu + 9) // 10
        else:
            size = generator.randint(1, big.max_sdu)
            if size == previous_size:
                size = size % big.max_sdu + 1
        previous_size = size
        data = bytes([len(sent)]) + generator.randbytes(size - 1)
        if phase:
            first_changed.setdefault(phase, len(sent))
        sent.append(data)
        return data

    for phase in range(3):
        for _ in range(20):
            if framed:
                while reference < big.anchor:
                    encoder.enqueue(next_data(phase), reference)
                    reference += big.sdu_interval
                payload = (2, encoder.pdu(big.anchor))
            else:
                payload = (0, next_data(phase))
            await _send_event(tester, peer, {1: payload})
    while encoder.pending:
        await _send_event(tester, peer, {1: (2, encoder.pdu(big.anchor))})
    packets = await _received_sdus(tester, peer, event.connection_handle[0])
    delivered = _matching_sdus(packets, sent)
    assert len(delivered) * 10 >= len(sent) * 9
    assert first_changed[1] in delivered and first_changed[2] in delivered
    await finish_receiver(tester, peer)


PROCEDURES: ProcedureMap = {}

# Section 4.11.4.1 selects the BN=1 preparation for clear and encrypted BIGs.
PROCEDURES |= procedure_cases(
    broadcast_setup,
    {
        'LL/BIS/BRD/BV-23-C': (
            'Broadcast Isochronous Stream Setup – Encryption Disabled – BN=1',
            {'encrypted': False},
        ),
        'LL/BIS/BRD/BV-24-C': (
            'Broadcast Isochronous Stream Setup – Encryption Enabled – BN=1',
            {'encrypted': True},
        ),
    },
)


# Section 4.11.5 uses an independent broadcaster and validates Host delivery,
# including authentication failure, RFU control opcodes and unsupported BIGInfo.
PROCEDURES |= procedure_cases(
    receiver_setup,
    {
        'LL/BIS/SNC/BV-15-C': (
            'Broadcast Isochronous Stream Synchronization Setup – '
            'Encryption Disabled, BN=1',
            {'encrypted': False},
        ),
        'LL/BIS/SNC/BV-16-C': (
            'Broadcast Isochronous Stream Synchronization Setup – '
            'Encryption Enabled, BN=1',
            {'encrypted': True},
        ),
    },
)
PROCEDURES |= procedure_cases(
    receiver_loss,
    {'LL/BIS/SNC/BV-11-C': ('Loss of Sync with an Isochronous Broadcaster', {})},
)
PROCEDURES |= procedure_cases(
    reserved_big_opcodes,
    {'LL/BIS/SNC/BI-04-C': ('Ignores Restricted for Future Use Opcodes, BIS', {})},
)
PROCEDURES |= procedure_cases(
    reserved_big_phy,
    {
        'LL/BIS/SNC/BI-03-C': (
            'Broadcast Isochronous Stream Synchronization, Unsupported Features',
            {},
        )
    },
)


# Broadcaster control procedures preserve each event's header sequence and use
# the control logical link's own address, channel and encrypted payload counter.
PROCEDURES |= procedure_cases(
    broadcaster_termination,
    {
        'LL/BIS/BRD/BV-10-C': (
            'Isochronous Broadcaster Terminates BIS Stream, Not Encrypted',
            {'encrypted': False},
        ),
        'LL/BIS/BRD/BV-18-C': (
            'Isochronous Broadcaster Terminates BIS Stream, Encrypted',
            {'encrypted': True},
        ),
    },
)
PROCEDURES |= procedure_cases(
    broadcaster_channel_map,
    {
        'LL/BIS/BRD/BV-09-C': (
            'BIG Channel Map Update Procedure, Broadcaster, Not Encrypted',
            {'encrypted': False},
        ),
        'LL/BIS/BRD/BV-17-C': (
            'BIG Channel Map Update Procedure, Broadcaster, Encrypted',
            {'encrypted': True},
        ),
    },
)

# Non-test creation and forced framing retain their different source parameters.
PROCEDURES |= procedure_cases(
    non_test_broadcast,
    {
        f'LL/BIS/BRD/BV-{number:02d}-C': (
            f'Broadcast Isochronous Stream Using Non-Test Command, {name}',
            {'phy': phy},
        )
        for number, name, phy in (
            (13, 'LE 1M PHY', 1),
            (14, 'LE 2M PHY', 2),
            (15, 'LE Coded PHY', 4),
        )
    },
)
PROCEDURES |= procedure_cases(
    force_framed_broadcast,
    {
        f'LL/BIS/BRD/BV-{number:02d}-C': pytest.param(
            f'Broadcasting a Broadcast Isochronous Stream, {name}',
            {'phy': phy},
            marks=pytest.mark.source_conflict,
        )
        for number, name, phy in (
            (20, 'LE 1M PHY', 1),
            (21, 'LE 2M PHY', 2),
            (22, 'LE Coded PHY', 4),
        )
    },
)
PROCEDURES |= procedure_cases(
    framing_parameter,
    {
        'LL/BIS/BRD/BV-31-C': (
            'LE Create BIG Test Framing Parameter, Unframed',
            {'framed': False},
        ),
        'LL/BIS/BRD/BV-32-C': (
            'LE Create BIG Test Framing Parameter, Segmentable',
            {'framed': True},
        ),
    },
)


# Section 4.11.4.6 exercises the declared four-group capacity and checks identity
# and key separation without consulting live Controller group state.
PROCEDURES |= procedure_cases(
    maximum_broadcast_groups,
    {
        'LL/BIS/BRD/BV-16-C': (
            'Maximum Supported Broadcast Isochronous Groups, Encryption Disabled',
            {'encrypted': False},
        ),
        'LL/BIS/BRD/BV-19-C': (
            'Maximum Supported Broadcast Isochronous Groups, Encryption Enabled',
            {'encrypted': True},
        ),
    },
)


# Receiver maps change independently of PA maps. Re-synchronization then proves
# that a new BIG does not revive the previous Host-assigned handle.
PROCEDURES |= procedure_cases(
    receiver_channel_map,
    {
        'LL/BIS/SNC/BV-09-C': (
            'BIG Channel Map Update Procedure, Synchronized Receiver, Not Encrypted',
            {'encrypted': False},
        ),
        'LL/BIS/SNC/BV-13-C': (
            'BIG Channel Map Update Procedure, Synchronized Receiver, Encrypted',
            {'encrypted': True},
        ),
    },
)
PROCEDURES |= procedure_cases(
    receiver_termination,
    {
        'LL/BIS/SNC/BV-10-C': (
            'Broadcast Isochronous Stream Termination, Not Encrypted',
            {'encrypted': False},
        ),
        'LL/BIS/SNC/BV-14-C': (
            'Broadcast Isochronous Stream Termination, Encrypted',
            {'encrypted': True},
        ),
    },
)


# A receiver without a pre-transmission cache must report the damaged current
# SDU, while variable-length reception also covers fractional ISO/SDU geometry.
PROCEDURES |= procedure_cases(
    receiver_no_pretransmission,
    {
        'LL/BIS/SNC/BV-22-C': (
            'Broadcast Isochronous Stream Synchronization No Pre-Transmission',
            {},
        )
    },
)
PROCEDURES |= procedure_cases(
    receiver_sdu_length,
    {
        'LL/BIS/SNC/BV-23-C': (
            'BIS change of SDU length during reception, Unframed',
            {'framed': False},
        ),
        'LL/BIS/SNC/BV-24-C': (
            'BIS change of SDU length during reception, Framed',
            {'framed': True},
        ),
    },
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


@pytest.mark.asyncio
async def test_iso_link_quality_for_a_synchronized_bis(air_dut):
    """Report a failed CRC and two unreceived payloads (Core 7.8.116)."""
    peer, established = await acquire_receiver(air_dut, irc=1, nse=1)
    (handle,) = established.connection_handle
    command = hci.HCI_LE_Read_ISO_Link_Quality_Command(handle)
    before = await air_dut.command(command)
    await _send_event(air_dut, peer, {1: (0, b'a')})
    await _send_event(air_dut, peer, {1: (0, b'b')}, bad_crc=lambda _: True)
    await _send_event(air_dut, peer, {(1, 0): None})
    await _send_event(air_dut, peer, {1: (0, b'c')})
    quality = await air_dut.command(command)
    assert quality.connection_handle == handle
    assert quality.crc_error_packets == before.crc_error_packets + 1
    assert quality.rx_unreceived_packets == before.rx_unreceived_packets + 2
    await finish_receiver(air_dut, peer)
