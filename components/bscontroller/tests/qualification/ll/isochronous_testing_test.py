# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.12: Isochronous Testing, selected BIS roles."""

import pytest

from bscontroller import hci
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.cis import disconnect_cis, establish_peer_cis
from tests.qualification.iso import (
    acquire_broadcaster,
    acquire_receiver,
    finish_receiver,
    framed_sdu,
    terminate_broadcaster,
)


async def iso_transmit_test(tester):
    """ISO Transmit Test Mode, BIS.

    Initial Condition:
        IUT: periodic advertiser, then one unframed BN=1 BIS using LL.TS
        Table 4.11-2 defaults. No HCI input ISO data path is set up.

    Test Steps:
        1. Create BIG Test and receive successful Status/Create BIG Complete.
        2. Start ISO Transmit Test with each Payload_Type in Table 4.12-2:
           zero length, variable 4..Max_SDU length, and maximum length.
        3. Receive five distinct SDUs per type using independent BIS decoding.
           For types 1/2, check the first four octets equal the payload counter
           divided by UPPS, in little-endian order.
        4. Attempt Setup ISO Data Path during test mode; require 0x0C.
        5. End ISO Test and require Received/Missed/Failed counters all zero.
        6. Terminate the BIG through its control sequence and completion.

    Expected Outcome:
        All three types produce five valid SDUs of the specified sizes and
        counters. An HCI path cannot be added during test mode.

    Source Correction:
        The source calls Create BIG Test's response Command Complete and
        Test End's response Command Status. Core Vol 4, E, 7.8.104 requires
        Status/Create BIG Complete; 7.8.114 requires Command Complete for
        Test End. Check those required event types and the source's counters.

    Reference:
        LL.TS.p28, LL/IST/BRD/BV-01-C, Table 4.12-2, Figure 4.12-2,
        Steps 1–7; Core Vol 6, B, 7.1.
    """
    big, complete = await acquire_broadcaster(tester)
    handle = complete.connection_handle[0]
    for payload_type in range(3):
        await tester.command(hci.HCI_LE_ISO_Transmit_Test_Command(handle, payload_type))
        big.align(tester.radio.now_us)
        for _ in range(5):
            counter = big.counter
            payloads, _ = await big.receive(tester)
            llid, data = payloads[(1, counter)]
            assert llid == 0
            if payload_type == 0:
                assert not data
            else:
                assert 4 <= len(data) <= big.max_sdu
                assert payload_type != 2 or len(data) == big.max_sdu
                assert int.from_bytes(data[:4], 'little') == counter
    await tester.command(
        hci.HCI_LE_Setup_ISO_Data_Path_Command(
            handle, 0, 0, hci.CodingFormat(hci.CodecID.TRANSPARENT), 0, b''
        ),
        0x0C,
    )
    result = await tester.command(hci.HCI_LE_ISO_Test_End_Command(handle))
    assert (
        result.received_sdu_count
        == result.missed_sdu_count
        == result.failed_sdu_count
        == 0
    )
    big.align(tester.radio.now_us)
    await terminate_broadcaster(tester, big)


async def iso_receive_test(tester):
    """ISO Receive Test Mode, BIS.

    Initial Condition:
        IUT: synchronized receiver of one BN=1 BIS, ISO interval 400 ms, no
        output HCI path. Use SDU interval 400 ms for the valid USPI=1 geometry.

    Test Steps:
        1. For every Table 4.12-4 row, synchronize to a BIG with the selected
           Framing value and receive successful BIG Sync Established.
        2. Start ISO Receive Test using the selected Payload_Type.
        3. Send five SDUs: zero length; variable length with counter; maximum
           length with counter; framed maximum; framed variable length.
        4. Read Test Counters; require Received=5 and Failed=0. Verify the
           counter's 32-bit wire order.
        5. Attempt Setup ISO Data Path; require 0x0C. End ISO Test successfully.
        6. Terminate synchronization and recreate it when Framing changes.

    Expected Outcome:
        Each row receives its five valid test SDUs and rejects an output path
        during test mode. Test End returns successful counters.

    Source Conflict:
        The initial condition sets ISO_Interval=400 ms but Step 1 also names
        Table 4.11-2 defaults: BN=1 and SDU_Interval=10 ms. Unframed ISOAL
        requires BN=USPI*UPPS, which would require BN>=40. Keep the explicit
        400-ms ISO interval and use 400-ms SDUs; this necessary deviation
        remains PARTIAL until the source parameter contradiction is resolved.

    Source Correction:
        Test End is synchronous and returns Command Complete (Core 7.8.114).
        The receiver terminates with BIG Terminate Sync (Core 7.8.107), since
        Terminate BIG applies to a broadcaster and must be rejected here.

    Reference:
        LL.TS.p28, LL/IST/SNC/BV-01-C, Table 4.12-4, Figure 4.12-4,
        Steps 1–11; Core Vol 6, B, 7.2 and G, 2.1.
    """
    rows = ((False, 0), (False, 1), (False, 2), (True, 2), (True, 1))
    for framed, payload_type in rows:
        peer, complete = await acquire_receiver(
            tester, framed=framed, interval=320, sdu_interval=400000
        )
        assert complete.status == 0
        handle = complete.connection_handle[0]
        await tester.command(hci.HCI_LE_ISO_Receive_Test_Command(handle, payload_type))
        for index in range(5):
            counter = index if framed else peer.big.counter
            size = (
                0
                if payload_type == 0
                else 4 + index % (peer.big.max_sdu - 3)
                if payload_type == 1
                else peer.big.max_sdu
            )
            data = (
                counter.to_bytes(4, 'little') + bytes(max(0, size - 4)) if size else b''
            )
            payload = (2, framed_sdu(data)) if framed else (0, data)
            while not await peer.step(tester, {1: payload}):
                pass
        counters = await tester.command(
            hci.HCI_LE_ISO_Read_Test_Counters_Command(handle)
        )
        assert counters.received_sdu_count == 5
        assert counters.failed_sdu_count == 0
        await tester.command(
            hci.HCI_LE_Setup_ISO_Data_Path_Command(
                handle, 1, 0, hci.CodingFormat(hci.CodecID.TRANSPARENT), 0, b''
            ),
            0x0C,
        )
        ended = await tester.command(hci.HCI_LE_ISO_Test_End_Command(handle))
        assert ended.received_sdu_count == 5 and ended.failed_sdu_count == 0
        await finish_receiver(tester, peer)


async def _next_cis_event(peer):
    while peer.subevent:
        await peer.exchange()


async def _check_cis_path_busy(tester, handle, direction):
    await tester.command(
        hci.HCI_LE_Setup_ISO_Data_Path_Command(
            handle, direction, 0, hci.CodingFormat(hci.CodecID.TRANSPARENT), 0, b''
        ),
        0x0C,
    )


async def _close_cis(tester, link, handle):
    await disconnect_cis(tester, link, handle)
    await link.disconnect()


async def cis_transmit_test(tester, *, central):
    """ISO Transmit Test Mode, CIS.

    Initial Condition:
        One unframed BN=1 CIS at 20 ms with three subevents and no HCI ISO
        data path. Run in each IUT role in Table 4.12-1.

    Test Steps:
        1. Start transmit test mode for each Payload_Type in Table 4.12-2.
        2. Receive five distinct CIS payloads per type. Check LLID, size and
           the unframed SDU counter. Acknowledge each payload.
        3. Require Command Disallowed when setting up an input data path.
        4. End each round with a successful Test End.

    Source Correction:
        Test End returns Command Complete as required by Core Vol 4, Part E,
        7.8.114.

    Reference:
        LL.TS.p28, 4.12.3, Tables 4.12-1 and 4.12-2, Figure 4.12-1.
    """
    link, peer, established = await establish_peer_cis(
        tester, central=central, test_nse=3, offset_us=2500
    )
    handle = established.connection_handle
    for payload_type in range(3):
        await _next_cis_event(peer)
        await tester.command(hci.HCI_LE_ISO_Transmit_Test_Command(handle, payload_type))
        for _ in range(5):
            counter = peer.counter
            packet = await peer.exchange(header=0x40 | ((counter & 1) << 2))
            header, size = packet.pdu[:2]
            assert header & 0x43 == 0 and size == len(packet.pdu) - 2
            if payload_type:
                assert 4 <= size <= 251
                assert payload_type != 2 or size == 251
                assert int.from_bytes(packet.pdu[2:6], 'little') == counter
            else:
                assert size == 0
            await peer.exchange(header=0x40 | (((counter + 1) & 1) << 2))
            await _next_cis_event(peer)
        await _check_cis_path_busy(tester, handle, 0)
        await tester.command(hci.HCI_LE_ISO_Test_End_Command(handle))
    await _close_cis(tester, link, handle)


async def cis_receive_test(tester, *, central):
    """ISO Receive Test Mode, CIS.

    Initial Condition:
        CIS with no HCI output path and ISO_Interval=400 ms. Use a 400 ms
        SDU interval to preserve the default BN=1 in the unframed rounds.

    Test Steps:
        1. Establish the CIS using each Framing setting in Table 4.12-4.
        2. Start the receive test with each Payload_Type for that framing.
        3. Send five test SDUs per round, with little-endian counters and
           the payload sizes in Table 4.12-4.
        4. Require five received SDUs and zero failed or missed SDUs.
        5. Reject an output data path while the test is active. End the test.

    Source Conflict:
        The 400 ms ISO interval and the default 20 ms SDU interval would
        require BN >= 20 for unframed data. Use a 400 ms SDU interval to
        exercise the BN=1 profile according to Core Vol 6, Part G, 2.1.

    Source Correction:
        Test End returns Command Complete (Vol 4, Part E, 7.8.114).

    Reference:
        LL.TS.p28, 4.12.4, Tables 4.12-3 and 4.12-4, Figure 4.12-3.
    """
    for framed, payload_types in ((False, (0, 1, 2)), (True, (2, 1))):
        link, peer, established = await establish_peer_cis(
            tester, central=central, interval_us=400000, test_nse=3, framed=framed
        )
        handle = established.connection_handle
        max_sdu = 246 if framed else 251
        for payload_type in payload_types:
            await _next_cis_event(peer)
            await tester.command(
                hci.HCI_LE_ISO_Receive_Test_Command(handle, payload_type)
            )
            for index in range(5):
                counter = peer.counter
                size = (0, 4 + index, max_sdu)[payload_type]
                data = counter.to_bytes(4, 'little') + bytes(size - 4) if size else b''
                header = ((counter & 1) << 3) | (2 if framed else 0)
                await peer.exchange(framed_sdu(data) if framed else data, header=header)
                await _next_cis_event(peer)
            result = await tester.command(
                hci.HCI_LE_ISO_Read_Test_Counters_Command(handle)
            )
            assert (
                result.received_sdu_count,
                result.missed_sdu_count,
                result.failed_sdu_count,
            ) == (5, 0, 0)
            await _check_cis_path_busy(tester, handle, 1)
            await tester.command(hci.HCI_LE_ISO_Test_End_Command(handle))
        await _close_cis(tester, link, handle)
        if central:
            await tester.command(hci.HCI_LE_Remove_CIG_Command(1))


async def cis_receive_missing(tester, *, central):
    """ISO Receive Test Mode missing PDU.

    Initial Condition:
        The Lower Tester initially sends NULL PDUs on a 400 ms CIS.

    Test Steps:
        1. Start ISO Receive Test with Payload_Type=0.
        2. Send one nonzero-length test payload, then a valid zero-length
           payload. Omit two scheduled data PDUs, with a valid payload
           between them and another valid payload after them.
        3. Read the counters. Require Failed=1 and Missed=2.
        4. End the test successfully.

    Source Conflict:
        Section 4.12.4 specifies an ISO interval incompatible with its
        default SDU interval and BN=1. Apply the same 400 ms SDU interval
        as the CIS receive test.

    Reference:
        LL.TS.p28, 4.12.5, Table 4.12-5 and Figure 4.12-5.
    """
    link, peer, established = await establish_peer_cis(
        tester, central=central, interval_us=400000, test_nse=3
    )
    handle = established.connection_handle
    await _next_cis_event(peer)
    await tester.command(hci.HCI_LE_ISO_Receive_Test_Command(handle, 0))
    for payload in (b'invalid', b'', None, b'', None, b''):
        if payload is None:
            await peer.exchange()
        else:
            await peer.exchange(payload, header=(peer.counter & 1) << 3)
        await _next_cis_event(peer)
    result = await tester.command(hci.HCI_LE_ISO_Read_Test_Counters_Command(handle))
    assert (result.failed_sdu_count, result.missed_sdu_count) == (1, 2)
    await tester.command(hci.HCI_LE_ISO_Test_End_Command(handle))
    await _close_cis(tester, link, handle)


PROCEDURES = ProcedureMap()

# Exercise all test payload patterns and counter outcomes for both CIS roles.
for number, procedure, description in (
    (1, cis_transmit_test, 'ISO Transmit Test Mode'),
    (3, cis_receive_test, 'ISO Receive Test Mode'),
    (5, cis_receive_missing, 'ISO Receive Test Mode missing PDU'),
):
    PROCEDURES |= procedure_cases(
        procedure,
        {
            f'LL/IST/{role}/BV-{number:02}-C': pytest.param(
                f'{description}, CIS {role}',
                {'central': role == 'CEN'},
                marks=() if number == 1 else pytest.mark.source_conflict,
            )
            for role in ('CEN', 'PER')
        },
    )

# Section 4.12 selects the broadcaster and synchronized receiver ISO test modes;
# every row in the referenced payload tables runs within its case procedure.
PROCEDURES |= procedure_cases(
    iso_transmit_test, {'LL/IST/BRD/BV-01-C': ('ISO Transmit Test Mode, BIS', {})}
)
PROCEDURES |= procedure_cases(
    iso_receive_test,
    {
        'LL/IST/SNC/BV-01-C': pytest.param(
            'ISO Receive Test Mode, BIS', {}, marks=pytest.mark.source_conflict
        )
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
