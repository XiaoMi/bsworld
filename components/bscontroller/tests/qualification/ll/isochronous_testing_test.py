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


PROCEDURES: ProcedureMap = {}

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
