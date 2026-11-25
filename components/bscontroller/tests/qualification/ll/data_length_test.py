# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.3: Data length update."""

import random

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    LL_IFS_US,
    PDU_ACCESS_ADDRESS_SIZE,
    PDU_CONTROL_FEATURE_SET_SIZE,
    PDU_CRC_SIZE,
    PDU_DATA_LLID_MASK,
    PDU_DATA_OCTET_TIME_CODED_S8_US,
    PDU_DATA_PAYLOAD_SIZE_MAX,
    PDU_DATA_PAYLOAD_SIZE_MIN,
    PDU_DATA_PAYLOAD_TIME_CODED_MAX_US,
    PDU_DATA_PAYLOAD_TIME_CODED_MIN_US,
    PDU_DATA_PAYLOAD_TIME_MIN_US,
    PDU_DATA_PAYLOAD_TIME_UNCODED_MAX_US,
    PDU_HEADER_SIZE,
    PDU_PREAMBLE_1M_SIZE,
    PDU_PREAMBLE_2M_SIZE,
    LlId,
)
from bscontroller.radio.base import (
    PHY_1M_OCTET_TIME_US,
    PHY_2M_OCTET_TIME_US,
    PHY_CODED_FEC1_TIME_US,
    PHY_CODED_S8_FACTOR,
    PHY_CODED_TERM2_SIZE_BITS,
    PHY_OCTET_BITS,
    Phy,
)
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.connection import (
    CONTROL_OBSERVATION_EVENTS,
    LENGTH_FIELDS,
    MAXIMUM,
    MINIMUM,
    ErrorCode,
    Opcode,
    _check_changed_events,
    _check_parameters,
    _connection_event,
    _effective_lengths,
    _LengthObserver,
    _restore_peer,
    _sequence_bits,
    _supported_lengths,
)
from tests.qualification.link import _data_pdu, _wire_event
from tests.qualification.peer import change_phy as _change_phy
from tests.qualification.peer import establish, peer_send
from tests.qualification.tester import (
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
)

PROCEDURES = ProcedureMap()

# LL.TS.p28, responding Step 10 and Table 4.3-63 rows 13..15, not S=8 packet airtime.
DLE_CODED_TIME_BOUNDARY_US = 2128
PRESERVATION_REQUEST_ATTEMPTS = 2

# LL.TS.p28, responding Step 8 and initiating Step 10.
RECEIVE_PACKET_COUNT = 20

# LL.TS.p28 Section 4.3.7.34: retain an unacknowledged packet for five intervals.
RETRANSMISSION_INTERVALS = 5

# LL.TS.p28 Section 4.3.7.36: increase TxOctets by twenty, at most ten rounds.
UNCODED_REQUEST_ATTEMPTS = 10
UNCODED_REQUEST_OCTET_INCREMENT = 20

# LL.TS.p28 unsupported-peer Step 1 and preservation Steps 1..5.
UNSUPPORTED_REQUEST_ATTEMPTS = 20


async def _data_length(tester, *, central, local, phy):
    """Execute the complete responding or initiating DLE matrix on LE 1M/2M/Coded."""
    supported = await _supported_lengths(tester)
    buffer = await tester.command(hci.HCI_LE_Read_Buffer_Size_Command())
    declared = await tester.command(hci.HCI_LE_Read_Local_Supported_Features_Command())
    coded = bool(
        int.from_bytes(declared.le_features, 'little') & hci.LeFeatureMask.LE_CODED_PHY
    )
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
        interval=48 if phy == Phy.LE_CODED else 24,
    )
    features = (
        hci.LeFeatureMask.LE_DATA_PACKET_LENGTH_EXTENSION
        | hci.LeFeatureMask.LE_2M_PHY
        | hci.LeFeatureMask.LE_CODED_PHY
    )
    await link.control(
        Opcode.LL_FEATURE_RSP,
        bytes([Opcode.LL_PERIPHERAL_FEATURE_REQ if central else Opcode.LL_FEATURE_REQ])
        + int(features).to_bytes(PDU_CONTROL_FEATURE_SET_SIZE, 'little'),
    )
    previous = (MINIMUM[2], MINIMUM[3], MINIMUM[0], MINIMUM[1])
    if phy != Phy.LE_1M:
        await _change_phy(tester, link, phy, local=True)
        current = _effective_lengths(MINIMUM, MINIMUM, link)
        _check_changed_events(tester, link.handle, previous, current)
        previous = current
    peer = MINIMUM
    requests = 0
    generator = random.Random(0)
    if local:
        rounds = [
            (request, response)
            for response in (MINIMUM, MAXIMUM)
            for request in _initiating_rounds(supported)
        ]
    else:
        vectors = _responding_rounds(supported)
        if coded:
            vectors += [
                tuple(
                    (
                        (
                            DLE_CODED_TIME_BOUNDARY_US
                            if index % 2 and value == PDU_DATA_PAYLOAD_TIME_CODED_MAX_US
                            else value
                        )
                        for index, value in enumerate(row)
                    )
                )
                for row in list(vectors)
                if PDU_DATA_PAYLOAD_TIME_CODED_MAX_US in (row[1], row[3])
            ]
        rounds = [(row, None) for row in vectors]
    for index, (request, response) in enumerate(rounds, 1):
        tester.output.write(f'DLE ROUND {index} request={request} peer={response}\n')
        if local:
            result = await tester.command(
                hci.HCI_LE_Set_Data_Length_Command(link.handle, *request)
            )
            assert result.connection_handle == link.handle
            packet = await link.control(Opcode.LL_LENGTH_REQ, required=False)
            if packet is None:
                tester.output.write('DLE ROW OMITTED: no LL_LENGTH_REQ observed\n')
                continue
            requests += 1
            dut = _check_parameters(packet, Opcode.LL_LENGTH_REQ, supported)
            intermediate = _effective_lengths(dut, peer, link)
            peer = response
            await link.exchange(
                bytes([Opcode.LL_LENGTH_RSP]) + LENGTH_FIELDS.pack(*peer),
                llid=LlId.CONTROL,
            )
        else:
            peer = request
            dut = _check_parameters(
                await link.control(
                    Opcode.LL_LENGTH_RSP,
                    bytes([Opcode.LL_LENGTH_REQ]) + LENGTH_FIELDS.pack(*peer),
                ),
                Opcode.LL_LENGTH_RSP,
                supported,
            )
            intermediate = None
        current = _effective_lengths(dut, peer, link)
        await tester.wait(0)
        _check_changed_events(tester, link.handle, previous, current, intermediate)
        previous = current
        await _transfer_under_limits(
            tester,
            link,
            current,
            buffer.le_acl_data_packet_length,
            buffer.total_num_le_acl_data_packets,
            generator,
        )
        assert not any(
            isinstance(event, hci.HCI_LE_Data_Length_Change_Event)
            for event in tester.events
        ), 'unexpected data-length event during data transfer'
    await link.disconnect(local=True)
    if local and (not requests):
        pytest.skip(
            'Inconclusive: no LL_LENGTH_REQ observed during the initiating matrix'
        )


def _initiating_rounds(supported):
    """Table 4.3-63, including the distinct 2120-us and 2128-us boundary rounds."""
    _, _, tx, tt = supported
    return [
        (octets, time_us)
        for time_us in (
            tt,
            PDU_DATA_PAYLOAD_TIME_MIN_US,
            PDU_DATA_PAYLOAD_TIME_CODED_MAX_US,
            PDU_DATA_PAYLOAD_TIME_UNCODED_MAX_US,
            DLE_CODED_TIME_BOUNDARY_US,
        )
        for octets in (tx, PDU_DATA_PAYLOAD_SIZE_MIN, PDU_DATA_PAYLOAD_SIZE_MAX)
    ]


async def _negotiate_maximum(tester, link, supported):
    """Source-prescribed maximum request, with one intermediate retry if omitted."""
    for attempt in range(PRESERVATION_REQUEST_ATTEMPTS):
        octets, time_us = supported[2:]
        if attempt:
            octets = (
                octets - PDU_DATA_PAYLOAD_SIZE_MIN
            ) // 2 + PDU_DATA_PAYLOAD_SIZE_MIN
            time_us = (
                time_us - PDU_DATA_PAYLOAD_TIME_MIN_US
            ) // 2 + PDU_DATA_PAYLOAD_TIME_MIN_US
        result = await tester.command(
            hci.HCI_LE_Set_Data_Length_Command(link.handle, octets, time_us)
        )
        assert result.connection_handle == link.handle
        if packet := (await link.control(Opcode.LL_LENGTH_REQ, required=False)):
            dut = _check_parameters(packet, Opcode.LL_LENGTH_REQ, supported)
            await link.exchange(
                bytes([Opcode.LL_LENGTH_RSP]) + LENGTH_FIELDS.pack(*MAXIMUM),
                llid=LlId.CONTROL,
            )
            await tester.wait(0)
            current = _effective_lengths(dut, MAXIMUM, link)
            _check_changed_events(
                tester, link.handle, _effective_lengths(MINIMUM, MINIMUM, link), current
            )
            return (dut, current)
    pytest.skip(
        'Inconclusive: no LL_LENGTH_REQ after maximum and intermediate HCI requests'
    )


def _payload_limit(phy, octets, time_us):
    """Unencrypted PDU budget from wire fields, not the DUT's airtime implementation."""
    if phy == Phy.LE_CODED:
        overhead = (
            PHY_CODED_FEC1_TIME_US
            + (
                (PDU_HEADER_SIZE + PDU_CRC_SIZE) * PHY_OCTET_BITS
                + PHY_CODED_TERM2_SIZE_BITS
            )
            * PHY_CODED_S8_FACTOR
        )
        return min(octets, (time_us - overhead) // PDU_DATA_OCTET_TIME_CODED_S8_US)
    unit, preamble = (
        (PHY_1M_OCTET_TIME_US, PDU_PREAMBLE_1M_SIZE)
        if phy == Phy.LE_1M
        else (PHY_2M_OCTET_TIME_US, PDU_PREAMBLE_2M_SIZE)
    )
    overhead = preamble + PDU_ACCESS_ADDRESS_SIZE + PDU_HEADER_SIZE + PDU_CRC_SIZE
    return min(octets, time_us // unit - overhead)


def _responding_rounds(supported):
    """All 16 Table 4.3-61 rounds in RX-first wire order; the tester supports
    protocol maxima."""
    rx, rt, tx, tt = supported
    low, high = (PDU_DATA_PAYLOAD_SIZE_MIN, PDU_DATA_PAYLOAD_SIZE_MAX)
    short, long = (PDU_DATA_PAYLOAD_TIME_MIN_US, PDU_DATA_PAYLOAD_TIME_CODED_MAX_US)
    rounds = [
        MINIMUM,
        MAXIMUM,
        (tx, tt, rx, rt),
        (tx, tt, low, short),
        (low, long, low, long),
        (high, long, low, rt),
        (tx, long, high, short),
        (low, short, high, long),
        (high, short, high, rt),
        (tx, short, rx, short),
        (low, tt, rx, long),
        (high, tt, rx, rt),
    ]
    generator = random.Random(0)
    for _ in range(4):
        rounds.append(
            (
                generator.randrange(low + 1, high),
                generator.randrange(short + 1, long),
                generator.randrange(low + 1, high),
                generator.randrange(short + 1, long),
            )
        )
    return rounds


async def _transfer_under_limits(
    tester, link, effective, buffer_size, buffer_slots, generator
):
    tx_octets, tx_time, rx_octets, rx_time = effective
    data = generator.randbytes(2 * tx_octets + 1)
    chunks = [
        data[index : index + buffer_size] for index in range(0, len(data), buffer_size)
    ]
    assert len(chunks) <= buffer_slots
    await tester.wait(0)
    for index, chunk in enumerate(chunks):
        tester.send(
            bytes(
                hci.HCI_AclDataPacket(
                    link.handle,
                    (
                        hci.HCI_ACL_PB_CONTINUATION
                        if index
                        else hci.HCI_ACL_PB_FIRST_NON_FLUSHABLE
                    ),
                    0,
                    len(chunk),
                    chunk,
                )
            )
        )
    received = b''
    for _ in range(64):
        for packet in await _connection_event(link):
            if packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL:
                continue
            assert packet.pdu[1] <= tx_octets
            assert packet.end_us - packet.start_us <= tx_time
            if packet.pdu[1]:
                assert packet.pdu[0] & PDU_DATA_LLID_MASK == (
                    LlId.CONTINUATION if received else LlId.START
                )
                received += packet.pdu[PDU_HEADER_SIZE:]
        assert data.startswith(received)
        if received == data:
            break
    assert received == data
    for packet in await _connection_event(link):
        assert packet.pdu[0] & PDU_DATA_LLID_MASK != LlId.CONTROL
        assert packet.pdu[1] == 0
    completed = 0
    while event := tester.take(
        lambda event: isinstance(event, hci.HCI_Number_Of_Completed_Packets_Event)
    ):
        assert event.connection_handles == [link.handle]
        completed += sum(event.num_completed_packets)
    assert completed == len(chunks)
    size = _payload_limit(link.tx_phy, rx_octets, rx_time)
    assert size > 0
    for _ in range(RECEIVE_PACKET_COUNT):
        packet = await peer_send(tester, link, generator.randbytes(size))
        assert packet.pdu[1] <= tx_octets
        assert packet.end_us - packet.start_us <= tx_time


async def _unsupported_peer(tester, *, central, phy):
    """Retain initial limits and bidirectional data after an unsupported LENGTH_REQ."""
    supported = await _supported_lengths(tester)
    buffer = await tester.command(hci.HCI_LE_Read_Buffer_Size_Command())
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
        interval=48 if phy == Phy.LE_CODED else 24,
    )
    initial = _effective_lengths(MINIMUM, MINIMUM, link)
    if phy != Phy.LE_1M:
        await _change_phy(tester, link, phy, local=True)
        coded_initial = _effective_lengths(MINIMUM, MINIMUM, link)
        _check_changed_events(tester, link.handle, initial, coded_initial)
        initial = coded_initial
    generator = random.Random(0)
    for attempt in range(UNSUPPORTED_REQUEST_ATTEMPTS):
        request = (
            supported[2:]
            if attempt == 0
            else (
                (min(MINIMUM[2] + 1, supported[2]), min(MINIMUM[3] + 1, supported[3]))
                if attempt == 1
                else (
                    generator.randint(MINIMUM[2], supported[2]),
                    generator.randint(MINIMUM[3], supported[3]),
                )
            )
        )
        result = await tester.command(
            hci.HCI_LE_Set_Data_Length_Command(link.handle, *request)
        )
        assert result.connection_handle == link.handle
        if packet := (await link.control(Opcode.LL_LENGTH_REQ, required=False)):
            _check_parameters(packet, Opcode.LL_LENGTH_REQ, supported)
            await link.exchange(
                bytes([Opcode.LL_UNKNOWN_RSP, Opcode.LL_LENGTH_REQ]), llid=LlId.CONTROL
            )
            break
    else:
        pytest.skip('Inconclusive: no LL_LENGTH_REQ in twenty HCI attempts')
    await tester.wait(0)
    _check_changed_events(tester, link.handle, initial, initial)
    await _transfer_under_limits(
        tester,
        link,
        initial,
        buffer.le_acl_data_packet_length,
        buffer.total_num_le_acl_data_packets,
        generator,
    )
    assert not any(
        isinstance(event, hci.HCI_LE_Data_Length_Change_Event)
        for event in tester.events
    )
    await link.disconnect(local=True)


async def peripheral_responding_to_data_length_update_le_1m_phy(
    tester,
):
    """Peripheral Data Length Update – Responding to Data Length Update Procedure; LE 1M
    PHY.

    Initial Condition:
        An unencrypted connected Peripheral with public own/peer addresses and
        common channel parameters. Read declared supported RX/TX octets/times
        and HCI ACL buffer capacity; require valid Core ranges. The Lower
        Tester acts as Central. Select a 30-ms interval on LE 1M/2M and 60 ms
        on Coded to accommodate maximum-length bidirectional S=8 traffic.
        Specification octet bounds are 27..251; time bounds are 328..17040 us.
        Establish peer DLE/2M/Coded feature support before the procedure.

    Test Steps:
        1. Use alternative 1A:
           1A.1 Retain the initial LE 1M PHY and move to Step 2.
        2. For each round of Table 4.3-61, send LL_LENGTH_REQ with its four
           Lower Tester TX/RX octet/time fields, in RX-first wire order.
        3. Receive LL_LENGTH_RSP and check all four IUT fields are between
           the specification minima and the declared supported IUT maxima.
        4. Derive effective TX/RX octets/times from both PDUs. Require Data
           Length Change with the new values exactly when a limit changes;
           unchanged limits must not produce an event. The source permits
           uncertainty about the first round's previous values.
        5. Send the Host data in the minimum number of HCI ACL packets allowed
           by the HCI buffer size. If multiple packets are needed, submit all
           together after one event closes and before the next begins.
        6. Receive every DATA packet until the complete Host payload arrives.
           Check each packet's effective TX octet and airtime limits.
        7. Require received bytes to equal exactly the Host data from Step 5.
        8. Send at least twenty DATA packets, each using the lesser of the
           effective IUT RX octet limit and the payload that fits its RX time
           budget on the current PHY.
        9. Require HCI ACL Data to reproduce exactly the Step 8 bytes with the
           correct handle, boundary flags and lengths. Repeat Steps 2-9 for
           all sixteen source rows; duplicate rows may be omitted, not required.
        10. If the IUT supports LE Coded, repeat Steps 2-9 for every row with
            either Lower Tester time field at the specification maximum,
            replacing that maximum with 2128 us in each applicable field.

    Expected Outcome:
        Pass verdict:
            The prescribed request/response exchange succeeds. Effective
            changes produce exactly the appropriate HCI length events, while
            unchanged limits produce none. All RF/HCI data matches exactly
            and respects effective octet/time limits.

    Coverage Note:
        Table 4.3-61 is translated to RX-first wire fields, not copied in its
        displayed TX-first order. Seed the four independent random rows for
        reproducibility; exclude specification extrema/tester maxima. Compare
        changed events against previously observed wire-derived limits. Check
        every MD packet, SN/LLID, exact Host bytes and completed-packet credits.
        Drain the data exchanges and disconnect through HCI after completion.

    Reference:
        LL.TS.p28, LL/CON/PER/BV-77-C referral on PDF 695,
        printed 4.3-222; inherits LL/CON/CEN/BV-73-C with
        the IUT role changed to Peripheral.
        Inherited Section 4.3.6.2, Table 4.3-61, Figure 4.3-183;
        PDF 837-841, printed 4.3-364 through 4.3-368. Steps 1-10 and outcome.
    """
    await _data_length(tester, central=False, local=False, phy=Phy.LE_1M)


# Verify that a Peripheral correctly responds to peer LL_LENGTH_REQ on LE 1M, reports
# effective changes and respects the negotiated limits during bidirectional DATA
# transfer.
PROCEDURES |= procedure_cases(
    peripheral_responding_to_data_length_update_le_1m_phy,
    {
        'LL/CON/PER/BV-77-C': (
            (
                'Peripheral Data Length Update – Responding to Data Length '
                'Update Procedure; LE 1M PHY'
            ),
            {},
        ),
    },
)


async def peripheral_initiating_data_length_update_le_1m_phy(
    tester,
):
    """Peripheral Data Length Update – Initiating Data Length Update Procedure; LE 1M
    PHY.

    Initial Condition:
        An unencrypted connected Peripheral with public own/peer addresses and
        common channel parameters. Read declared supported RX/TX octets/times
        and HCI ACL buffer capacity; require valid Core ranges. The Lower
        Tester acts as Central. Select a 30-ms interval on LE 1M/2M and 60 ms
        on Coded to accommodate maximum-length bidirectional S=8 traffic.
        Specification octet bounds are 27..251; time bounds are 328..17040 us.
        Establish peer DLE/2M/Coded feature support before the procedure.

    Test Steps:
        1. Use alternative 1A:
           1A.1 Retain the initial LE 1M PHY and move to Step 2.
        2. Repeat Steps 3-11 for each of the fifteen Table 4.3-63 HCI rows;
           duplicate rows may be omitted, not required.
        3. Send HCI LE Set Data Length with the row's TX octets/time and
           require successful Command Complete for the connection handle.
        4. Receive LL_LENGTH_REQ and check all four IUT fields against the
           specification minima and declared supported IUT maxima. If no
           request is sent, continue with the next row rather than failing.
        5. Send LL_LENGTH_RSP with both octet fields at the specification
           minimum and both time fields at the specification minimum.
        6. Require Data Length Change exactly when effective limits change,
           with the correct values; forbid an event for unchanged values.
           The first round may have unknown previous values. Permit the two
           events described by the source when a local autonomous change and
           the Lower Tester response each change effective values.
        7. Send the Host data in the minimum number of HCI ACL packets allowed
           by the HCI buffer size. If multiple packets are needed, submit all
           together after one event closes and before the next begins.
        8. Receive all DATA until the Host payload is complete, checking each
           packet's effective TX octet and airtime limits.
        9. Require exact equality to the Host data queued in Step 7.
        10. Send at least twenty DATA packets, each using the lesser of the
            effective IUT RX octet limit and the payload that fits its RX time
            budget on the current PHY.
        11. Require HCI ACL Data to reproduce exactly the Step 10 bytes with
            the correct handle, boundary flags and lengths.
        12. Repeat Steps 3-11 with both response octet fields at the
            specification maximum and both time fields at the specification
            maximum. Preserve the distinct 2120-us and 2128-us request rows.

    Expected Outcome:
        Pass verdict:
            The prescribed request/response exchange succeeds. Effective
            changes produce exactly the appropriate HCI length events, while
            unchanged limits produce none. All RF/HCI data matches exactly
            and respects effective octet/time limits.
        Inconclusive verdict:
            No LL_LENGTH_REQ is initiated across the complete matrix.

    Coverage Note:
        Execute both complete fifteen-row matrices. Distinguish the four IUT
        wire fields from the two HCI request fields. Check every MD packet,
        SN/LLID, exact Host bytes and completed-packet credits. Omitted requests
        skip only their row; the entire test is Inconclusive if none is sent.
        Drain the data exchanges and disconnect through HCI after completion.

    Reference:
        LL.TS.p28, LL/CON/PER/BV-78-C referral on PDF 695,
        printed 4.3-222; inherits LL/CON/CEN/BV-74-C with
        the IUT role changed to Peripheral.
        Inherited Section 4.3.6.3, Table 4.3-63, Figure 4.3-184;
        PDF 842-845, printed 4.3-369 through 4.3-372. Steps 1-12 and outcome.
    """
    await _data_length(tester, central=False, local=True, phy=Phy.LE_1M)


# Verify that a Peripheral initiates Data Length Update through LL_LENGTH_REQ on LE 1M
# after HCI requests, accepts the peer response and respects effective limits during
# bidirectional DATA transfer.
PROCEDURES |= procedure_cases(
    peripheral_initiating_data_length_update_le_1m_phy,
    {
        'LL/CON/PER/BV-78-C': (
            (
                'Peripheral Data Length Update – Initiating Data Length '
                'Update Procedure; LE 1M PHY'
            ),
            {},
        ),
    },
)


async def peripheral_data_length_update_central_does_not_support_le_1m_phy(tester):
    """Peripheral Data Length Update – Central does not support; LE 1M PHY.

    Initial Condition:
        An unencrypted connected Peripheral with public own/peer addresses and
        common channel parameters. Read declared supported RX/TX octets/times
        and HCI ACL buffer capacity; require valid Core ranges. The Lower
        Tester acts as Central. Select a 30-ms interval on LE 1M/2M and 60 ms
        on Coded to accommodate maximum-length bidirectional S=8 traffic.
        Specification octet bounds are 27..251; time bounds are 328..17040 us.
        Do not establish remote DLE support through Feature Exchange.

    Test Steps:
        1. Repeat Step 2 until LL_LENGTH_REQ is received, at most twenty
           attempts; no request after all attempts is Inconclusive.
        2. Send HCI LE Set Data Length for the connection handle:
           a. First use the declared maximum supported IUT TX octets/time.
           b. Then use minimum plus one, capped at each supported TX maximum.
           c. For remaining attempts choose octets/time independently and
              uniformly within the inclusive minimum-to-supported ranges.
        3. Check all four received LL_LENGTH_REQ fields against specification
           minima and declared supported IUT maxima.
        4. Send LL_UNKNOWN_RSP with UnknownType=LL_LENGTH_REQ.
        5. Send Host data in the minimum number of HCI ACL packets allowed
           by the HCI buffer size. If multiple packets are needed, submit all
           together after one event closes and before the next begins.
        6. Receive all DATA until the Host payload is complete, checking each
           packet's unchanged effective TX octet and airtime limits.
        7. Require exact equality to the Host data queued in Step 5.
        8. Send at least twenty DATA packets, each using the lesser of the
           effective IUT RX octet limit and the payload that fits its RX time
           budget on the current PHY.
        9. Require HCI ACL Data to reproduce exactly the Step 8 bytes with the
           correct handle, boundary flags and lengths.

    Expected Outcome:
        Pass verdict:
            Receive a valid LL_LENGTH_REQ and preserve the initial effective
            data limits after UNKNOWN_RSP. All RF/HCI data matches exactly
            and respects those limits.
        Fail verdict:
            Any Data Length Change caused by the unsupported DLE procedure.
        Inconclusive verdict:
            No LL_LENGTH_REQ in twenty attempts.

    Coverage Note:
        Seed independent inclusive random HCI requests for reproducibility.
        Check every MD packet, SN/LLID, exact Host bytes and completion credits.
        No Data Length Change is allowed during the rejected procedure or DATA.
        Drain the data exchanges and disconnect through HCI after completion.

    Reference:
        LL.TS.p28, LL/CON/PER/BV-79-C referral on PDF 695,
        printed 4.3-222; inherits LL/CON/CEN/BV-75-C with
        the IUT role changed to Peripheral.
        Inherited unsupported-peer procedure, Figure 4.3-185;
        PDF 846-848, printed 4.3-373 through 4.3-375. Steps 1-9 and outcome.
    """
    await _unsupported_peer(tester, central=False, phy=Phy.LE_1M)


# Verify that a Peripheral maintains DATA communication on LE 1M when its Central does
# not support Data Length Update and answers LL_LENGTH_REQ with LL_UNKNOWN_RSP.
PROCEDURES |= procedure_cases(
    peripheral_data_length_update_central_does_not_support_le_1m_phy,
    {
        'LL/CON/PER/BV-79-C': (
            'Peripheral Data Length Update – Central does not support; LE 1M PHY',
            {},
        ),
    },
)


async def peripheral_responding_to_data_length_update_le_2m_phy(
    tester,
):
    """Peripheral Data Length Update – Responding to Data Length Update Procedure; LE 2M
    PHY.

    Initial Condition:
        An unencrypted connected Peripheral with public own/peer addresses and
        common channel parameters. Read declared supported RX/TX octets/times
        and HCI ACL buffer capacity; require valid Core ranges. The Lower
        Tester acts as Central. Select a 30-ms interval on LE 1M/2M and 60 ms
        on Coded to accommodate maximum-length bidirectional S=8 traffic.
        Specification octet bounds are 27..251; time bounds are 328..17040 us.
        Establish peer DLE/2M/Coded feature support before the procedure.

    Test Steps:
        1. Use alternative 1B for LE 2M:
           1B.1 Send HCI LE Set PHY with ALL_PHYS=0 and TX/RX masks=0x02.
                 Complete the PHY procedure and require PHY Update Complete.
                 If both directions do not use LE 2M, report Inconclusive.
        2. For each round of Table 4.3-61, send LL_LENGTH_REQ with its four
           Lower Tester TX/RX octet/time fields, in RX-first wire order.
        3. Receive LL_LENGTH_RSP and check all four IUT fields are between
           the specification minima and the declared supported IUT maxima.
        4. Derive effective TX/RX octets/times from both PDUs. Require Data
           Length Change with the new values exactly when a limit changes;
           unchanged limits must not produce an event. The source permits
           uncertainty about the first round's previous values.
        5. Send the Host data in the minimum number of HCI ACL packets allowed
           by the HCI buffer size. If multiple packets are needed, submit all
           together after one event closes and before the next begins.
        6. Receive every DATA packet until the complete Host payload arrives.
           Check each packet's effective TX octet and airtime limits.
        7. Require received bytes to equal exactly the Host data from Step 5.
        8. Send at least twenty DATA packets, each using the lesser of the
           effective IUT RX octet limit and the payload that fits its RX time
           budget on the current PHY.
        9. Require HCI ACL Data to reproduce exactly the Step 8 bytes with the
           correct handle, boundary flags and lengths. Repeat Steps 2-9 for
           all sixteen source rows; duplicate rows may be omitted, not required.
        10. If the IUT supports LE Coded, repeat Steps 2-9 for every row with
            either Lower Tester time field at the specification maximum,
            replacing that maximum with 2128 us in each applicable field.

    Expected Outcome:
        Pass verdict:
            The prescribed request/response exchange succeeds. Effective
            changes produce exactly the appropriate HCI length events, while
            unchanged limits produce none. All RF/HCI data matches exactly
            and respects effective octet/time limits.
        Inconclusive verdict:
            The initial PHY update does not select the required PHY both ways.

    Coverage Note:
        Table 4.3-61 is translated to RX-first wire fields, not copied in its
        displayed TX-first order. Seed the four independent random rows for
        reproducibility; exclude specification extrema/tester maxima. Compare
        changed events against previously observed wire-derived limits. Check
        every MD packet, SN/LLID, exact Host bytes and completed-packet credits.
        Drain the data exchanges and disconnect through HCI after completion.

    Reference:
        LL.TS.p28, LL/CON/PER/BV-80-C referral on PDF 695,
        printed 4.3-222; inherits LL/CON/CEN/BV-76-C with
        the IUT role changed to Peripheral.
        Inherited Section 4.3.6.2, Table 4.3-61, Figure 4.3-183;
        PDF 837-841, printed 4.3-364 through 4.3-368. Steps 1-10 and outcome.
    """
    await _data_length(tester, central=False, local=False, phy=Phy.LE_2M)


# Verify that a Peripheral correctly responds to peer LL_LENGTH_REQ on LE 2M, reports
# effective changes and respects the negotiated limits during bidirectional DATA
# transfer.
PROCEDURES |= procedure_cases(
    peripheral_responding_to_data_length_update_le_2m_phy,
    {
        'LL/CON/PER/BV-80-C': (
            (
                'Peripheral Data Length Update – Responding to Data Length '
                'Update Procedure; LE 2M PHY'
            ),
            {},
        ),
    },
)


async def peripheral_initiating_data_length_update_le_2m_phy(
    tester,
):
    """Peripheral Data Length Update – Initiating Data Length Update Procedure; LE 2M
    PHY.

    Initial Condition:
        An unencrypted connected Peripheral with public own/peer addresses and
        common channel parameters. Read declared supported RX/TX octets/times
        and HCI ACL buffer capacity; require valid Core ranges. The Lower
        Tester acts as Central. Select a 30-ms interval on LE 1M/2M and 60 ms
        on Coded to accommodate maximum-length bidirectional S=8 traffic.
        Specification octet bounds are 27..251; time bounds are 328..17040 us.
        Establish peer DLE/2M/Coded feature support before the procedure.

    Test Steps:
        1. Use alternative 1B for LE 2M:
           1B.1 Send HCI LE Set PHY with ALL_PHYS=0 and TX/RX masks=0x02.
                 Complete the PHY procedure and require PHY Update Complete.
                 If both directions do not use LE 2M, report Inconclusive.
        2. Repeat Steps 3-11 for each of the fifteen Table 4.3-63 HCI rows;
           duplicate rows may be omitted, not required.
        3. Send HCI LE Set Data Length with the row's TX octets/time and
           require successful Command Complete for the connection handle.
        4. Receive LL_LENGTH_REQ and check all four IUT fields against the
           specification minima and declared supported IUT maxima. If no
           request is sent, continue with the next row rather than failing.
        5. Send LL_LENGTH_RSP with both octet fields at the specification
           minimum and both time fields at the specification minimum.
        6. Require Data Length Change exactly when effective limits change,
           with the correct values; forbid an event for unchanged values.
           The first round may have unknown previous values. Permit the two
           events described by the source when a local autonomous change and
           the Lower Tester response each change effective values.
        7. Send the Host data in the minimum number of HCI ACL packets allowed
           by the HCI buffer size. If multiple packets are needed, submit all
           together after one event closes and before the next begins.
        8. Receive all DATA until the Host payload is complete, checking each
           packet's effective TX octet and airtime limits.
        9. Require exact equality to the Host data queued in Step 7.
        10. Send at least twenty DATA packets, each using the lesser of the
            effective IUT RX octet limit and the payload that fits its RX time
            budget on the current PHY.
        11. Require HCI ACL Data to reproduce exactly the Step 10 bytes with
            the correct handle, boundary flags and lengths.
        12. Repeat Steps 3-11 with both response octet fields at the
            specification maximum and both time fields at the specification
            maximum. Preserve the distinct 2120-us and 2128-us request rows.

    Expected Outcome:
        Pass verdict:
            The prescribed request/response exchange succeeds. Effective
            changes produce exactly the appropriate HCI length events, while
            unchanged limits produce none. All RF/HCI data matches exactly
            and respects effective octet/time limits.
        Inconclusive verdict:
            No LL_LENGTH_REQ is initiated across the complete matrix.
            The initial PHY update does not select the required PHY both ways.

    Coverage Note:
        Execute both complete fifteen-row matrices. Distinguish the four IUT
        wire fields from the two HCI request fields. Check every MD packet,
        SN/LLID, exact Host bytes and completed-packet credits. Omitted requests
        skip only their row; the entire test is Inconclusive if none is sent.
        Drain the data exchanges and disconnect through HCI after completion.

    Reference:
        LL.TS.p28, LL/CON/PER/BV-81-C referral on PDF 696,
        printed 4.3-223; inherits LL/CON/CEN/BV-77-C with
        the IUT role changed to Peripheral.
        Inherited Section 4.3.6.3, Table 4.3-63, Figure 4.3-184;
        PDF 842-845, printed 4.3-369 through 4.3-372. Steps 1-12 and outcome.
    """
    await _data_length(tester, central=False, local=True, phy=Phy.LE_2M)


# Verify that a Peripheral initiates Data Length Update through LL_LENGTH_REQ on LE 2M
# after HCI requests, accepts the peer response and respects effective limits during
# bidirectional DATA transfer.
PROCEDURES |= procedure_cases(
    peripheral_initiating_data_length_update_le_2m_phy,
    {
        'LL/CON/PER/BV-81-C': (
            (
                'Peripheral Data Length Update – Initiating Data Length '
                'Update Procedure; LE 2M PHY'
            ),
            {},
        ),
    },
)


async def peripheral_responding_to_data_length_update_le_coded_phy(
    tester,
):
    """Peripheral Data Length Update – Responding to Data Length Update Procedure; LE
    Coded PHY.

    Initial Condition:
        An unencrypted connected Peripheral with public own/peer addresses and
        common channel parameters. Read declared supported RX/TX octets/times
        and HCI ACL buffer capacity; require valid Core ranges. The Lower
        Tester acts as Central. Select a 30-ms interval on LE 1M/2M and 60 ms
        on Coded to accommodate maximum-length bidirectional S=8 traffic.
        Specification octet bounds are 27..251; time bounds are 328..17040 us.
        Establish peer DLE/2M/Coded feature support before the procedure.

    Test Steps:
        1. Use alternative 1C for LE Coded:
           1C.1 Send HCI LE Set PHY with ALL_PHYS=0 and TX/RX masks=0x04.
                 Complete the PHY procedure and require PHY Update Complete.
                 If both directions do not use LE Coded, report Inconclusive.
        2. For each round of Table 4.3-61, send LL_LENGTH_REQ with its four
           Lower Tester TX/RX octet/time fields, in RX-first wire order.
        3. Receive LL_LENGTH_RSP and check all four IUT fields are between
           the specification minima and the declared supported IUT maxima.
        4. Derive effective TX/RX octets/times from both PDUs. Require Data
           Length Change with the new values exactly when a limit changes;
           unchanged limits must not produce an event. The source permits
           uncertainty about the first round's previous values.
        5. Send the Host data in the minimum number of HCI ACL packets allowed
           by the HCI buffer size. If multiple packets are needed, submit all
           together after one event closes and before the next begins.
        6. Receive every DATA packet until the complete Host payload arrives.
           Check each packet's effective TX octet and airtime limits.
        7. Require received bytes to equal exactly the Host data from Step 5.
        8. Send at least twenty DATA packets, each using the lesser of the
           effective IUT RX octet limit and the payload that fits its RX time
           budget on the current PHY.
        9. Require HCI ACL Data to reproduce exactly the Step 8 bytes with the
           correct handle, boundary flags and lengths. Repeat Steps 2-9 for
           all sixteen source rows; duplicate rows may be omitted, not required.
        10. If the IUT supports LE Coded, repeat Steps 2-9 for every row with
            either Lower Tester time field at the specification maximum,
            replacing that maximum with 2128 us in each applicable field.

    Expected Outcome:
        Pass verdict:
            The prescribed request/response exchange succeeds. Effective
            changes produce exactly the appropriate HCI length events, while
            unchanged limits produce none. All RF/HCI data matches exactly
            and respects effective octet/time limits.
        Inconclusive verdict:
            The initial PHY update does not select the required PHY both ways.

    Coverage Note:
        Table 4.3-61 is translated to RX-first wire fields, not copied in its
        displayed TX-first order. Seed the four independent random rows for
        reproducibility; exclude specification extrema/tester maxima. Compare
        changed events against previously observed wire-derived limits. Check
        every MD packet, SN/LLID, exact Host bytes and completed-packet credits.
        Drain the data exchanges and disconnect through HCI after completion.

    Reference:
        LL.TS.p28, LL/CON/PER/BV-82-C referral on PDF 696,
        printed 4.3-223; inherits LL/CON/CEN/BV-78-C with
        the IUT role changed to Peripheral.
        Inherited Section 4.3.6.2, Table 4.3-61, Figure 4.3-183;
        PDF 837-841, printed 4.3-364 through 4.3-368. Steps 1-10 and outcome.
    """
    await _data_length(tester, central=False, local=False, phy=Phy.LE_CODED)


# Verify that a Peripheral correctly responds to peer LL_LENGTH_REQ on LE Coded, reports
# effective changes and respects the negotiated limits during bidirectional DATA
# transfer.
PROCEDURES |= procedure_cases(
    peripheral_responding_to_data_length_update_le_coded_phy,
    {
        'LL/CON/PER/BV-82-C': (
            (
                'Peripheral Data Length Update – Responding to Data Length '
                'Update Procedure; LE Coded PHY'
            ),
            {},
        ),
    },
)


async def peripheral_initiating_data_length_update_le_coded_phy(
    tester,
):
    """Peripheral Data Length Update – Initiating Data Length Update Procedure; LE Coded
    PHY.

    Initial Condition:
        An unencrypted connected Peripheral with public own/peer addresses and
        common channel parameters. Read declared supported RX/TX octets/times
        and HCI ACL buffer capacity; require valid Core ranges. The Lower
        Tester acts as Central. Select a 30-ms interval on LE 1M/2M and 60 ms
        on Coded to accommodate maximum-length bidirectional S=8 traffic.
        Specification octet bounds are 27..251; time bounds are 328..17040 us.
        Establish peer DLE/2M/Coded feature support before the procedure.

    Test Steps:
        1. Use alternative 1C for LE Coded:
           1C.1 Send HCI LE Set PHY with ALL_PHYS=0 and TX/RX masks=0x04.
                 Complete the PHY procedure and require PHY Update Complete.
                 If both directions do not use LE Coded, report Inconclusive.
        2. Repeat Steps 3-11 for each of the fifteen Table 4.3-63 HCI rows;
           duplicate rows may be omitted, not required.
        3. Send HCI LE Set Data Length with the row's TX octets/time and
           require successful Command Complete for the connection handle.
        4. Receive LL_LENGTH_REQ and check all four IUT fields against the
           specification minima and declared supported IUT maxima. If no
           request is sent, continue with the next row rather than failing.
        5. Send LL_LENGTH_RSP with both octet fields at the specification
           minimum and both time fields at the specification minimum.
        6. Require Data Length Change exactly when effective limits change,
           with the correct values; forbid an event for unchanged values.
           The first round may have unknown previous values. Permit the two
           events described by the source when a local autonomous change and
           the Lower Tester response each change effective values.
        7. Send the Host data in the minimum number of HCI ACL packets allowed
           by the HCI buffer size. If multiple packets are needed, submit all
           together after one event closes and before the next begins.
        8. Receive all DATA until the Host payload is complete, checking each
           packet's effective TX octet and airtime limits.
        9. Require exact equality to the Host data queued in Step 7.
        10. Send at least twenty DATA packets, each using the lesser of the
            effective IUT RX octet limit and the payload that fits its RX time
            budget on the current PHY.
        11. Require HCI ACL Data to reproduce exactly the Step 10 bytes with
            the correct handle, boundary flags and lengths.
        12. Repeat Steps 3-11 with both response octet fields at the
            specification maximum and both time fields at the specification
            maximum. Preserve the distinct 2120-us and 2128-us request rows.

    Expected Outcome:
        Pass verdict:
            The prescribed request/response exchange succeeds. Effective
            changes produce exactly the appropriate HCI length events, while
            unchanged limits produce none. All RF/HCI data matches exactly
            and respects effective octet/time limits.
        Inconclusive verdict:
            No LL_LENGTH_REQ is initiated across the complete matrix.
            The initial PHY update does not select the required PHY both ways.

    Coverage Note:
        Execute both complete fifteen-row matrices. Distinguish the four IUT
        wire fields from the two HCI request fields. Check every MD packet,
        SN/LLID, exact Host bytes and completed-packet credits. Omitted requests
        skip only their row; the entire test is Inconclusive if none is sent.
        Drain the data exchanges and disconnect through HCI after completion.

    Reference:
        LL.TS.p28, LL/CON/PER/BV-83-C referral on PDF 696,
        printed 4.3-223; inherits LL/CON/CEN/BV-79-C with
        the IUT role changed to Peripheral.
        Inherited Section 4.3.6.3, Table 4.3-63, Figure 4.3-184;
        PDF 842-845, printed 4.3-369 through 4.3-372. Steps 1-12 and outcome.
    """
    await _data_length(tester, central=False, local=True, phy=Phy.LE_CODED)


# Verify that a Peripheral initiates Data Length Update through LL_LENGTH_REQ on LE
# Coded after HCI requests, accepts the peer response and respects effective limits
# during bidirectional DATA transfer.
PROCEDURES |= procedure_cases(
    peripheral_initiating_data_length_update_le_coded_phy,
    {
        'LL/CON/PER/BV-83-C': (
            (
                'Peripheral Data Length Update – Initiating Data Length '
                'Update Procedure; LE Coded PHY'
            ),
            {},
        ),
    },
)


async def peripheral_data_length_update_central_does_not_support_le_coded_phy(tester):
    """Peripheral Data Length Update – Central does not support; LE Coded PHY.

    Initial Condition:
        An unencrypted connected Peripheral with public own/peer addresses and
        common channel parameters. Read declared supported RX/TX octets/times
        and HCI ACL buffer capacity; require valid Core ranges. The Lower
        Tester acts as Central. Select a 30-ms interval on LE 1M/2M and 60 ms
        on Coded to accommodate maximum-length bidirectional S=8 traffic.
        Specification octet bounds are 27..251; time bounds are 328..17040 us.
        Do not establish remote DLE support through Feature Exchange.

    Test Steps:
        Before Step 1, send HCI LE Set PHY with ALL_PHYS=0 and both PHY
        masks=0x04. Complete the role-correct PHY procedure and require PHY
        Update Complete with LE Coded both ways, or report Inconclusive.

        1. Repeat Step 2 until LL_LENGTH_REQ is received, at most twenty
           attempts; no request after all attempts is Inconclusive.
        2. Send HCI LE Set Data Length for the connection handle:
           a. First use the declared maximum supported IUT TX octets/time.
           b. Then use minimum plus one, capped at each supported TX maximum.
           c. For remaining attempts choose octets/time independently and
              uniformly within the inclusive minimum-to-supported ranges.
        3. Check all four received LL_LENGTH_REQ fields against specification
           minima and declared supported IUT maxima.
        4. Send LL_UNKNOWN_RSP with UnknownType=LL_LENGTH_REQ.
        5. Send Host data in the minimum number of HCI ACL packets allowed
           by the HCI buffer size. If multiple packets are needed, submit all
           together after one event closes and before the next begins.
        6. Receive all DATA until the Host payload is complete, checking each
           packet's unchanged effective TX octet and airtime limits.
        7. Require exact equality to the Host data queued in Step 5.
        8. Send at least twenty DATA packets, each using the lesser of the
           effective IUT RX octet limit and the payload that fits its RX time
           budget on the current PHY.
        9. Require HCI ACL Data to reproduce exactly the Step 8 bytes with the
           correct handle, boundary flags and lengths.

    Expected Outcome:
        Pass verdict:
            Receive a valid LL_LENGTH_REQ and preserve the initial effective
            data limits after UNKNOWN_RSP. All RF/HCI data matches exactly
            and respects those limits.
        Fail verdict:
            Any Data Length Change caused by the unsupported DLE procedure.
        Inconclusive verdict:
            No LL_LENGTH_REQ in twenty attempts.
            The initial PHY update does not select the required PHY both ways.

    Coverage Note:
        Seed independent inclusive random HCI requests for reproducibility.
        Check every MD packet, SN/LLID, exact Host bytes and completion credits.
        No Data Length Change is allowed during the rejected procedure or DATA.
        Consume only the initial PHY-induced 27/2704 length notification before
        checking that the subsequent unsupported DLE produces no length event.
        Drain the data exchanges and disconnect through HCI after completion.

    Reference:
        LL.TS.p28, LL/CON/PER/BV-84-C referral on PDF 696,
        printed 4.3-223; inherits LL/CON/CEN/BV-80-C with
        the IUT role changed to Peripheral.
        Inherited unsupported-peer procedure, Figure 4.3-185;
        PDF 846-848, printed 4.3-373 through 4.3-375. Steps 1-9 and outcome.
        Additional Coded pre-step: CEN/BV-80, PDF 849-850,
        printed 4.3-376 through 4.3-377.
    """
    await _unsupported_peer(tester, central=False, phy=Phy.LE_CODED)


# Verify that a Peripheral maintains DATA communication on LE Coded when its Central
# does not support Data Length Update and answers LL_LENGTH_REQ with LL_UNKNOWN_RSP.
PROCEDURES |= procedure_cases(
    peripheral_data_length_update_central_does_not_support_le_coded_phy,
    {
        'LL/CON/PER/BV-84-C': (
            'Peripheral Data Length Update – Central does not support; LE Coded PHY',
            {},
        ),
    },
)


async def central_data_length_update_responding_to_data_length_update_procedure(
    tester, *, phy
):
    """Central Data Length Update – Responding to Data Length Update Procedure.

    Initial Condition:
        Connected Central with declared supported TX/RX octet and time maxima
        within Core Vol 6, Part B, Table 4.8. This implementation uses the IUT's
        HCI Read Maximum Data Length values as IXIT inputs and reads HCI buffer
        capacity for Host fragmentation and credits.
        The full-feature IUT supports Coded: specification minima are 27 octets
        and 328 us, maxima are 251 octets and 17040 us. The Lower Tester also
        declares these protocol maxima. Its random interior values therefore
        cannot equal its declared maxima.
        Start on LE 1M with public peer addresses; use 30 ms intervals on 1M/2M
        and 60 ms on Coded to accommodate bidirectional maximum-size packets.

    Test Case Configuration:
        LL/CON/CEN/BV-73-C: LE 1M.
        LL/CON/CEN/BV-76-C: LE 2M.
        LL/CON/CEN/BV-78-C: LE Coded.

    Test Steps:
        1. Select the alternative from Table 4.3-60:
           1A.1. LE 1M: proceed directly to Step 2.
           1B.1. LE 2M: send HCI_LE_Set_PHY with ALL_PHYS=0 and both PHY masks
                 0x02. Complete PHY Update and require PHY Update Complete.
                 If both directions are not LE 2M, end with Inconclusive.
           1C.1. LE Coded: perform the same exchange with both masks 0x04.
                 If both directions are not LE Coded, end with Inconclusive.
           Repeat Steps 2-9 for every Table 4.3-61 round. Identical-valued
           rounds may be omitted; this implementation retains all 16.
        2. Send LL_LENGTH_REQ with the current table row's payload. Preserve
           the source's TX/RX meanings when encoding the RX-first wire fields.
           Rows 1-12 use the exact minima, maxima and declared-IUT combinations.
           For rows 13-16, choose each TX/RX octet/time value independently
           inside its allowed range and unequal to the tester's declared
           maxima. If no eligible value exists, use round 1's value; the
           declared full-range tester always has eligible interior values.
        3. Receive LL_LENGTH_RSP. Require all four IUT TX/RX octet/time values
           between their specification minima and declared supported maxima.
        4. Derive the four effective TX/RX limits from the observed request,
           response and active PHYs. Require HCI_LE_Data_Length_Change with
           the new values only if an effective limit changed; otherwise
           require no event. The source allows unknown prior limits in the
           first round; this controlled setup starts with known minimum limits.
        5. Submit data in the minimum number of HCI ACL packets allowed by the
           HCI transmit buffer size. If multiple packets are needed, submit
           them together after one connection event and before the next opens.
        6. Receive DATA until all Host data is transmitted. For each packet,
           check payload length <= effective TX octets and actual airtime
           <= effective TX time.
        7. Verify that all Lower Tester data matches the Host bytes in Step 5,
           including fragment boundaries and the completed HCI packet credits.
        8. Send at least 20 DATA packets to the IUT. Each payload uses the
           smaller of effective RX octets and the largest payload fitting the
           effective RX time on the selected PHY.
        9. Receive the corresponding HCI ACL data and verify that the complete
           payload and boundaries match Step 8.
        10. If the IUT supports Coded, repeat Steps 2-9 for every maximum-time
            round, replacing the maximum time values with 2128 us.

    Expected Outcome:
        Pass verdict:
            Every exercised request receives LL_LENGTH_RSP. HCI reports exact
            changed effective limits and no event when none changed. DATA
            delivered to either tester matches the submitted data and obeys
            the directional octet/time limits.
        Inconclusive verdict:
            The Step 1B.1 or 1C.1 PHY selection cannot be established.

    Reference:
        LL.TS.p28.pdf, Section 4.3.6.2; LL/CON/CEN/BV-73-C, BV-76-C and BV-78-C;
        Table 4.3-60, Figure 4.3-183, Table 4.3-61;
        PDF pages 837-841, printed pages 4.3-364..368;
        source references [8] 5.1.9 and [10] 4.5.10.
    """
    await _data_length(tester, central=True, local=False, phy=phy)


# Verify that a Central receives LL_LENGTH_REQ, responds correctly on the selected PHY,
# reports effective-length changes through HCI, and respects the resulting limits while
# transferring data in both directions.
PROCEDURES |= procedure_cases(
    central_data_length_update_responding_to_data_length_update_procedure,
    {
        'LL/CON/CEN/BV-73-C': (
            (
                'Central Data Length Update – Responding to Data Length '
                'Update Procedure; LE 1M PHY'
            ),
            {'phy': Phy.LE_1M},
        ),
        'LL/CON/CEN/BV-76-C': (
            (
                'Central Data Length Update – Responding to Data Length '
                'Update Procedure; LE 2M PHY'
            ),
            {'phy': Phy.LE_2M},
        ),
        'LL/CON/CEN/BV-78-C': (
            (
                'Central Data Length Update – Responding to Data Length '
                'Update Procedure; LE Coded PHY'
            ),
            {'phy': Phy.LE_CODED},
        ),
    },
)


async def central_data_length_update_initiating_data_length_update_procedure(
    tester, *, phy
):
    """Central Data Length Update – Initiating Data Length Update Procedure.

    Initial Condition:
        Connected Central with declared supported TX/RX octet and time maxima
        within Core Vol 6, Part B, Table 4.8. This implementation uses the IUT's
        HCI Read Maximum Data Length values as IXIT inputs and reads HCI buffer
        capacity for Host fragmentation and credits.
        The full-feature IUT supports Coded: specification minima are 27 octets
        and 328 us, maxima are 251 octets and 17040 us.
        Start on LE 1M with public peer addresses; use 30 ms intervals on 1M/2M
        and 60 ms on Coded to accommodate bidirectional maximum-size packets.

    Test Case Configuration:
        LL/CON/CEN/BV-74-C: LE 1M.
        LL/CON/CEN/BV-77-C: LE 2M.
        LL/CON/CEN/BV-79-C: LE Coded.

    Test Steps:
        1. Select the alternative from Table 4.3-62:
           1A.1. LE 1M: proceed directly to Step 2.
           1B.1. LE 2M: send HCI_LE_Set_PHY with ALL_PHYS=0 and both PHY masks
                 0x02. Complete PHY Update and require PHY Update Complete.
                 If both directions are not LE 2M, end with Inconclusive.
           1C.1. LE Coded: perform the same exchange with both masks 0x04.
                 If both directions are not LE Coded, end with Inconclusive.
        2. Repeat Steps 3-11 for each of the 15 Table 4.3-63 rows. Identical
           rows may be omitted; this implementation retains every row.
        3. Send HCI_LE_Set_Data_Length with the row's TxOctets and TxTime.
           The table covers supported/minimum/maximum octets at each supported,
           minimum, maximum, 2120 us and 2128 us time value.
        4. Receive LL_LENGTH_REQ. Require each IUT TX/RX octet/time value
           between its specification minimum and declared supported maximum.
           If no request is sent, proceed to the next table round.
        5. Answer with LL_LENGTH_RSP whose two octet fields are specification
           minima and whose two time fields are specification minima.
        6. Require HCI_LE_Data_Length_Change with the new effective values if
           any effective limit changed; otherwise require no event. The first
           round may have unknown prior limits; this setup starts with known
           minima. Accept the source's possible two events when an autonomous
           local change and the peer response each change effective limits.
        7. Submit data in the minimum number of HCI ACL packets allowed by the
           HCI transmit buffer size. If multiple packets are needed, submit
           them together after one connection event and before the next opens.
        8. Receive DATA until all Host data is transmitted. Check every payload
           against effective TX octets and its airtime against effective TX time.
        9. Verify exact bytes, fragment boundaries and HCI credits for all
           data received by the Lower Tester.
        10. Send at least 20 DATA packets to the IUT. Each payload uses the
            smaller of effective RX octets and the largest payload fitting the
            effective RX time on the selected PHY.
        11. Receive HCI ACL data and verify that all bytes and boundaries match
            the data sent in Step 10.
        12. Repeat Steps 3-11, answering Step 5 with specification maximum
            octets and times in both directions instead of the minima.

    Expected Outcome:
        Pass verdict:
            The IUT initiates at least one LL_LENGTH_REQ. Every exercised row
            produces the required changed-limit HCI events, with exact values
            and no unchanged-limit event. Bidirectional DATA matches the
            submitted bytes and respects effective octet/time limits.
        Inconclusive verdict:
            No LL_LENGTH_REQ is initiated during the entire matrix, or the
            Step 1B.1/1C.1 PHY selection cannot be established. An omitted
            request in one row alone is not a whole-test Inconclusive verdict.

    Reference:
        LL.TS.p28.pdf, Section 4.3.6.3; LL/CON/CEN/BV-74-C, BV-77-C and BV-79-C;
        Table 4.3-62, Figure 4.3-184, Table 4.3-63;
        PDF pages 842-845, printed pages 4.3-369..372;
        source references [8] 5.1.9 and [10] 4.5.10.
    """
    await _data_length(tester, central=True, local=True, phy=phy)


# Verify that a Central initiates Data Length Update on the selected PHY. The Upper
# Tester requests new transmit limits through HCI; the Peripheral Lower Tester observes
# and answers LL_LENGTH_REQ, then verifies data flow and effective-length reporting.
PROCEDURES |= procedure_cases(
    central_data_length_update_initiating_data_length_update_procedure,
    {
        'LL/CON/CEN/BV-74-C': (
            (
                'Central Data Length Update – Initiating Data Length Update'
                ' Procedure; LE 1M PHY'
            ),
            {'phy': Phy.LE_1M},
        ),
        'LL/CON/CEN/BV-77-C': (
            (
                'Central Data Length Update – Initiating Data Length Update'
                ' Procedure; LE 2M PHY'
            ),
            {'phy': Phy.LE_2M},
        ),
        'LL/CON/CEN/BV-79-C': (
            (
                'Central Data Length Update – Initiating Data Length Update'
                ' Procedure; LE Coded PHY'
            ),
            {'phy': Phy.LE_CODED},
        ),
    },
)


async def central_data_length_update_peripheral_does_not_support_le_1m_phy(tester):
    """Central Data Length Update – Peripheral does not support; LE 1M PHY.

    Initial Condition:
        Connected Central on LE 1M, with public peer addresses and declared
        supported TX/RX octet/time maxima within Core Vol 6, Part B,
        Table 4.8. Use the IUT's
        HCI Read Maximum Data Length values as this implementation's IXIT inputs.
        Specification minima are 27 octets and 328 us; this Coded-capable IUT
        has specification maxima 251 octets and 17040 us.
        Read HCI buffer size and credits; do not disclose unsupported DLE
        through Feature Exchange before attempting the procedure.

    Test Steps:
        1. Repeat Step 2 until LL_LENGTH_REQ is received from the IUT or
           20 attempts have completed. With no request after 20 attempts,
           end with Inconclusive.
        2. Send HCI_LE_Set_Data_Length:
           a. First attempt: supportedMaxTxOctetsIUT and supportedMaxTxTimeIUT.
           b. Second attempt: min(specification minimum octets + 1, supported
              TX octets) and min(specification minimum time + 1, supported
              TX time).
           c. Remaining attempts: independently seeded random octets and time
              between the specification minimum and IUT supported TX maximum,
              inclusive.
        3. Validate LL_LENGTH_REQ: all four IUT TX/RX octet/time fields lie
           between their specification minima and declared supported maxima.
        4. Send LL_UNKNOWN_RSP with UnknownType identifying LL_LENGTH_REQ.
        5. Submit data in the minimum number of HCI ACL packets allowed by the
           HCI transmit buffer size. If several packets are needed, send them
           together after one connection event and before the next opens.
        6. Receive DATA until the entire Host payload has arrived. Each packet
           must respect the unchanged effective TX octet and airtime limits.
        7. Check that all Lower Tester bytes and fragment boundaries match the
           Host submission in Step 5; verify all HCI packet credits.
        8. Send at least 20 DATA packets, each using the smaller of effective
           RX octets and the largest payload fitting effective RX time on 1M.
        9. Receive HCI ACL data and verify that all bytes and boundaries match
           the Lower Tester's packets in Step 8.

    Expected Outcome:
        Pass verdict:
            The IUT sends LL_LENGTH_REQ and continues bidirectional data flow
            within the pre-existing effective length limits after UNKNOWN_RSP.
            Payloads and HCI credits match the submitted data.
        Fail verdict:
            HCI_LE_Data_Length_Change is sent during the unsupported procedure
            or subsequent data transfer.
        Inconclusive verdict:
            No LL_LENGTH_REQ is initiated during all 20 HCI attempts.

    Source Correction:
        Step 1's phrase "receives an LL_LENGTH_REQ from the PDU" means receiving
        that PDU from the IUT, as Step 3 explicitly states. This is a local
        grammatical correction with no change to the procedure.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-75-C; Figure 4.3-185, Steps 1-9 and verdicts;
        PDF pages 846-848, printed pages 4.3-373..375; source reference [8] 5.1.9.
    """
    await _unsupported_peer(tester, central=True, phy=Phy.LE_1M)


# Verify that a Central handles a Peripheral without Data Length Update support, retains
# the existing effective lengths after LL_UNKNOWN_RSP, and continues data transfer in
# both directions.
PROCEDURES |= procedure_cases(
    central_data_length_update_peripheral_does_not_support_le_1m_phy,
    {
        'LL/CON/CEN/BV-75-C': (
            'Central Data Length Update – Peripheral does not support; LE 1M PHY',
            {},
        ),
    },
)


async def central_data_length_update_peripheral_does_not_support_le_coded_phy(tester):
    """Central Data Length Update – Peripheral does not support; LE Coded PHY.

    Initial Condition:
        Inherit the connected-Central conditions and declared supported TX/RX
        octet/time maxima of BV-75. Use public peer addresses, HCI maximum-length
        inputs and HCI transmit buffer capacity. Specification minima are
        27 octets and 328 us; maxima are 251 octets and 17040 us.
        Use a 60 ms interval for Coded traffic. Do not disclose unsupported
        DLE through Feature Exchange before attempting the procedure.

    Preamble:
        Before inherited Step 1, send HCI_LE_Set_PHY with ALL_PHYS=0 and both
        TX_PHYS and RX_PHYS=0x04. Complete PHY Update and require
        HCI_LE_PHY_Update_Complete reporting LE Coded in both directions;
        otherwise Inconclusive. Consume any minimum-time Data Length Change
        caused by this PHY setup before checking the unsupported-DLE procedure.

    Test Steps:
        Retain the inherited BV-75 numbering after the Coded preamble.

        1. Repeat Step 2 until LL_LENGTH_REQ is received from the IUT or
           20 attempts have completed. With no request, end with Inconclusive.
        2. Send HCI_LE_Set_Data_Length:
           a. First attempt: the IUT's declared supported TX octets and time.
           b. Second attempt: the smaller of minimum octets + 1 and supported
              TX octets, and the smaller of minimum time + 1 and supported TX
              time.
           c. Remaining attempts: independently seeded random octets and time
              within specification-minimum through supported-maximum bounds,
              inclusive.
        3. Validate all four LL_LENGTH_REQ fields against specification minima
           and the IUT's declared supported TX/RX maxima.
        4. Send LL_UNKNOWN_RSP with UnknownType identifying LL_LENGTH_REQ.
        5. Submit data in the minimum number of HCI ACL packets allowed by the
           HCI transmit buffer size, together between connection events when
           multiple packets are needed.
        6. Receive all Host DATA and enforce the unchanged effective TX octet
           and airtime limits, including Coded's mandatory minimum time.
        7. Verify exact Host bytes, fragment boundaries and HCI packet credits.
        8. Send at least 20 DATA packets at the smaller of effective RX octets
           and the maximum payload fitting effective RX time on Coded.
        9. Require matching HCI ACL payloads and boundary flags for all data.

    Expected Outcome:
        Pass verdict:
            The IUT sends LL_LENGTH_REQ and continues bidirectional data flow
            within the existing effective length limits after UNKNOWN_RSP.
            Payloads and HCI credits match the submitted data.
        Fail verdict:
            HCI_LE_Data_Length_Change is sent during the unsupported-DLE
            procedure or subsequent data transfer, after the PHY preamble.
        Inconclusive verdict:
            No request is initiated during 20 attempts, or both directions
            cannot be established on LE Coded during the preamble.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-80-C; Figure 4.3-186 and Coded preamble;
        PDF pages 849-850, printed pages 4.3-376..377.
        Inherited BV-75 Steps 1-9 and verdicts, PDF 846-848, printed 4.3-373..375.
    """
    await _unsupported_peer(tester, central=True, phy=Phy.LE_CODED)


# Verify that a Central handles a Peripheral without Data Length Update support while
# using LE Coded, retaining the existing effective lengths after LL_UNKNOWN_RSP and
# continuing bidirectional data flow.
PROCEDURES |= procedure_cases(
    central_data_length_update_peripheral_does_not_support_le_coded_phy,
    {
        'LL/CON/CEN/BV-80-C': (
            'Central Data Length Update – Peripheral does not support; LE Coded PHY',
            {},
        ),
    },
)


async def data_length_update_preserve_parameters_after_a_phy_change(
    tester, *, central, phy
):
    """Data Length Update – Preserve Parameters After a PHY Change.

    Initial Condition:
        Connected on LE 1M in the selected role, with supported TX octets/time
        declared within Core limits. Read HCI maximum data lengths as this IUT's
        IXIT inputs. Use public peer addresses and a 60 ms connection interval.
        The Lower Tester advertises unchanged protocol-maximum DLE values.

    Test Case Configuration:
        LL/CON/CEN/BV-126-C: Central, LE 2M.
        LL/CON/CEN/BV-127-C: Central, LE Coded.
        LL/CON/PER/BV-129-C: Peripheral, LE 2M.
        LL/CON/PER/BV-130-C: Peripheral, LE Coded.

    Test Steps:
        1. Send HCI_LE_Set_Data_Length with the supported maximum TX octets
           and time. Require successful HCI_Command_Complete.
        2. The IUT may send LL_LENGTH_REQ.
        3. If received, answer LL_LENGTH_RSP. Keep the Lower Tester's maximum
           RX/TX octet/time fields fixed throughout the test.
        4. Receive Data Length Change for the initiated update and record
           all four reported effective octet/time values.
        5. If no update was initiated, repeat Steps 1-4 once with integer
           intermediate values:
           TxTime = floor((supportedMaxTxTime - 328) / 2) + 328;
           TxOctets = floor((supportedMaxTxOctets - 27) / 2) + 27.
           If the second attempt also produces no update, end Inconclusive.
        6. Send HCI_LE_Set_PHY requesting the selected PHY in both directions
           and require successful HCI_Command_Status.
        7. Require PHY Update Complete with both directions changed to the
           selected PHY. If either direction did not change, end Inconclusive.
        8. Reject a Data Length Change unless the selected PHY is Coded and
           the only difference is an effective TX time, RX time or both rising
           to 2704 us. Octet limits must remain unchanged by the PHY change.
        9. The IUT may optionally initiate another Data Length Update:
           a. Receive its LL_LENGTH_REQ and validate all four fields.
           b. Answer LL_LENGTH_RSP with the Lower Tester's current fixed values.
           c. Require Data Length Change if and only if these fields change
              an effective octet/time limit. Retain newly negotiated values
              as the baseline for subsequent PHY-only preservation.
        10. Send HCI_LE_Set_PHY requesting LE 1M in both directions and require
            successful HCI_Command_Status.
        11. Require PHY Update Complete with both directions changed to LE 1M;
            otherwise end Inconclusive.
        12. Reject a Data Length Change unless returning from Coded removes
            its mandatory time floor, restoring the corresponding recorded
            uncoded times. See Source Correction and Source Conflict below.
        13. The IUT may optionally perform Data Length Update again:
            a. Receive LL_LENGTH_REQ.
            b. Answer LL_LENGTH_RSP with the same current Lower Tester values.
            c. Require Data Length Change exactly when effective values change.

    Expected Outcome:
        Pass verdict:
            A PHY-only transition preserves the negotiated octet/time parameters.
            The only permitted effective-length event at Step 8 is a Coded
            time-floor increase; Step 12 may undo that floor when returning
            from Coded. Optional DLE changes report correct effective values.
        Fail verdict:
            A PHY transition reports a length change outside those exceptions.
        Inconclusive verdict:
            Neither maximum nor intermediate HCI request initiates DLE, or
            the required transmit/receive PHY change cannot be established.

    Source Correction:
        Step 12 and its verdict refer to Coded in Step 10, but Step 10 always
        requests LE 1M. Interpret this as returning from the earlier Coded
        selection in Step 6. Core 4.5.10 and Amber corroborate that removing
        the Coded time floor, not resetting negotiated state, is intended.
        This is a local reference correction, not a SIG erratum.

    Source Conflict:
        Step 9 can renegotiate the underlying times. If those times fall below
        2704 us and differ from Step 4, Core preserves the new negotiation on
        returning to 1M, but Step 12 literally names the old Step 4 values.
        Amber confirms the Core behavior but cannot uniquely resolve that
        wider TS implication. Skip that specific path with an explicit source
        conflict rather than failing a Core-correct IUT or inventing a verdict.

    Coverage Note:
        Observe every received PDU throughout PHY preparation, Instant handling
        and the subsequent observation events. Answer autonomous LENGTH_REQs
        with unchanged Lower Tester limits and retain each negotiated baseline.
        Check the exact ordered HCI length changes, including PHY-only Coded
        time-floor transitions. Only the source_conflict label above remains unresolved.

    Reference:
        LL.TS.p28.pdf, Section 4.3.7.33; Table 4.3-114, Figure 4.3-242,
        Steps 1-13 and verdicts; PDF 973-976, printed 4.3-500..503.
        Core v6.3, Vol 6, Part B, 5.1.9 and 4.5.10.
    """
    supported = await _supported_lengths(tester)
    link = await establish(
        tester,
        central=central,
        interval=48,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    dut, original = await _negotiate_maximum(tester, link, supported)
    observer = _LengthObserver(tester, link, dut, MAXIMUM, supported)
    link.on_receive = observer.receive
    for target in (phy, Phy.LE_1M):
        await _change_phy(tester, link, target, local=True)
        for _ in range(CONTROL_OBSERVATION_EVENTS):
            await link.exchange()
            await tester.wait(0)
            observer.check()
        observer.check(final=True)
        preserved = (*observer.dut[2:], *observer.dut[:2])
        if target == Phy.LE_CODED and any(
            value < PDU_DATA_PAYLOAD_TIME_CODED_MIN_US and value != initial
            for value, initial in zip(preserved[1::2], original[1::2])
        ):
            pytest.skip(
                'Source conflict: Step 9 changes the uncoded time baseline, '
                'but Step 12 refers back to Step 4; Core preserves the new '
                'DLE parameters (LL.TS.p28 PDF 975)'
            )
    link.on_receive = None
    await link.disconnect(local=True)


# Verify that changing from LE 1M to the selected PHY and back preserves negotiated data
# lengths, except for the mandatory Coded effective-time floor. The Lower Tester never
# initiates Data Length Update.
PROCEDURES |= procedure_cases(
    data_length_update_preserve_parameters_after_a_phy_change,
    {
        'LL/CON/CEN/BV-126-C': (
            ('Data Length Update – Preserve Parameters After a PHY Change – LE 2M PHY'),
            {'central': True, 'phy': Phy.LE_2M},
        ),
        'LL/CON/CEN/BV-127-C': pytest.param(
            (
                'Data Length Update – Preserve Parameters After a PHY '
                'Change – LE Coded PHY'
            ),
            {'central': True, 'phy': Phy.LE_CODED},
            marks=[
                pytest.mark.source_conflict,
            ],
        ),
        'LL/CON/PER/BV-129-C': (
            ('Data Length Update – Preserve Parameters After a PHY Change – LE 2M PHY'),
            {'central': False, 'phy': Phy.LE_2M},
        ),
        'LL/CON/PER/BV-130-C': pytest.param(
            (
                'Data Length Update – Preserve Parameters After a PHY '
                'Change – LE Coded PHY'
            ),
            {'central': False, 'phy': Phy.LE_CODED},
            marks=[
                pytest.mark.source_conflict,
            ],
        ),
    },
)


async def data_length_update_retransmission_during_an_update(tester, *, central):
    """Data Length Update – Retransmission During an Update.

    Initial Condition:
        Connected in the selected role. The supported maximum TX octets and
        time are declared as IXIT values within Core limits. Read this IUT's
        HCI maximum data lengths as those inputs. Establish LE 1M with a public
        peer. The minimum octet and time limits used here are 27 and 328 us.

    Test Case Configuration:
        LL/CON/CEN/BV-128-C: Central.
        LL/CON/PER/BV-131-C: Peripheral.

    Test Steps:
        1. Send HCI_LE_Set_Data_Length with supportedMaxTxOctets and
           supportedMaxTxTime. Require successful HCI_Command_Complete.
        2. The IUT may send LL_LENGTH_REQ with new values.
        3. If received, answer LL_LENGTH_RSP accepting the requested values
           and record the IUT's MaxTxOctets.
        4. If the IUT initiated the update, require Data Length Change
           reporting the changed effective values.
        5. Otherwise repeat Steps 1-4 once with integer intermediate values:
           TxTime = floor((supportedMaxTxTime - 328) / 2) + 328;
           TxOctets = floor((supportedMaxTxOctets - 27) / 2) + 27.
           If neither attempt initiates DLE, end Inconclusive.
        6. Send a stream of Host ACL packets, each supportedMaxTxOctets long.
        7. Receive DATA with the negotiated IUT MaxTxOctets length.
        8. Withhold the acknowledgement of one received DATA packet.
        9. Immediately send LL_LENGTH_REQ using the minimum octet and time
           limits in all four RX/TX fields, while retaining the negative ACK.
        10. After five connection intervals, receive the retransmitted DATA.
            Require its original MaxTxOctets length and unchanged payload.

    Expected Outcome:
        Pass verdict:
            After five connection intervals, the original DATA packet is
            retransmitted without refragmentation at its original size.
        Inconclusive verdict:
            Neither HCI data-length request initiates Data Length Update.

    Coverage Note:
        Queue two Host ACL packets to supply the stream. Retain the IUT SN,
        LLID, payload and length through all five retransmission intervals,
        with no premature Host completion credit. Independently preserve the
        Lower Tester's own SN/ACK state; an unacknowledged LENGTH_REQ must not
        become an empty DATA packet under the same SN.
        Compare connection-event anchors, not Peripheral response start times:
        a longer Central request legitimately shifts the response by its extra
        airtime. After the source verdict, acknowledge the held packet, finish
        DLE, check remaining fragmentation and exact bytes under the reduced
        limits, require two Host completion credits, and disconnect via HCI.

    Reference:
        LL.TS.p28.pdf, Section 4.3.7.34; Table 4.3-115, Figure 4.3-243,
        Steps 1-10 and verdicts; PDF 976-978, printed 4.3-503..505.
        Source references [8] 5.1.9 and [10] 4.5.10.
    """
    supported = await _supported_lengths(tester)
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    _, original_limits = await _negotiate_maximum(tester, link, supported)
    payload = random.Random(0).randbytes(supported[2])
    await tester.wait(0)
    for _ in range(2):
        tester.send(
            bytes(
                hci.HCI_AclDataPacket(
                    link.handle,
                    hci.HCI_ACL_PB_FIRST_NON_FLUSHABLE,
                    0,
                    len(payload),
                    payload,
                )
            )
        )
    held_sn, peer_sn = (link.nesn, link.sn)
    request = bytes([Opcode.LL_LENGTH_REQ]) + LENGTH_FIELDS.pack(*MINIMUM)
    channel = link.channel

    def first_reply(packet):
        nonlocal peer_sn
        if central:
            peer_sn = _sequence_bits(packet)[1]
            return _data_pdu(peer_sn, held_sn, request, llid=LlId.CONTROL)
        return _data_pdu(peer_sn, held_sn)

    first, first_sent = await _wire_event(link, first_reply)
    assert first.pdu[0] & PDU_DATA_LLID_MASK == LlId.START
    assert first.pdu[1] == original_limits[0]
    assert first.pdu[PDU_HEADER_SIZE:] == payload[: original_limits[0]]
    origin = first.start_us
    if not central:
        origin -= link.tx_phy.airtime_us(len(first_sent)) + LL_IFS_US
    if not central:
        peer_sn = _sequence_bits(first)[1]
        await tester.transmit(
            channel,
            _data_pdu(peer_sn, held_sn, request, llid=LlId.CONTROL),
            start_us=first.end_us + LL_IFS_US,
            **link.radio_parameters,
        )
    for index in range(1, RETRANSMISSION_INTERVALS + 1):

        def refuse(packet, index=index):
            nonlocal peer_sn
            if central:
                assert _sequence_bits(packet)[1] == peer_sn ^ 1
                peer_sn ^= 1
            # A peer Central has not observed the immediate request's ACK yet.
            data = request if not central and index == 1 else b''
            return _data_pdu(
                peer_sn, held_sn, data, llid=LlId.CONTROL if data else LlId.CONTINUATION
            )

        packet, sent = await _wire_event(link, refuse)
        assert _sequence_bits(packet)[0] == held_sn
        if not central:
            assert _sequence_bits(packet)[1] == peer_sn ^ 1
            peer_sn ^= 1
        assert packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.START
        assert packet.pdu[1:] == first.pdu[1:]
        anchor = packet.start_us
        if not central:
            anchor -= link.tx_phy.airtime_us(len(sent)) + LL_IFS_US
        assert anchor - origin == (
            index * link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
        )
        assert not any(
            isinstance(event, hci.HCI_Number_Of_Completed_Packets_Event)
            for event in tester.events
        )

    def acknowledge(packet):
        nonlocal peer_sn
        if central:
            assert _sequence_bits(packet)[1] == peer_sn ^ 1
            peer_sn ^= 1
        return _data_pdu(peer_sn, held_sn ^ 1)

    packet, sent = await _wire_event(link, acknowledge)
    _restore_peer(link, packet, sent)
    if packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL:
        response = packet.pdu[PDU_HEADER_SIZE:]
    else:
        response = await link.control(Opcode.LL_LENGTH_RSP)
    dut = _check_parameters(response, Opcode.LL_LENGTH_RSP, supported)
    first_fragment = await link.exchange()
    current = _effective_lengths(dut, MINIMUM, link)
    _check_changed_events(tester, link.handle, original_limits, current)
    remaining = payload[original_limits[0] :] + payload
    assert first_fragment.pdu[0] & PDU_DATA_LLID_MASK == (
        LlId.CONTINUATION if original_limits[0] < len(payload) else LlId.START
    )
    assert first_fragment.pdu[1] <= current[0]
    assert first_fragment.end_us - first_fragment.start_us <= current[1]
    received = first_fragment.pdu[PDU_HEADER_SIZE:]
    for _ in range(32):
        for packet in await _connection_event(link):
            assert packet.pdu[0] & PDU_DATA_LLID_MASK != LlId.CONTROL
            assert packet.pdu[1] <= current[0]
            assert packet.end_us - packet.start_us <= current[1]
            received += packet.pdu[PDU_HEADER_SIZE:]
        if received == remaining:
            break
    assert received == remaining
    await link.exchange()
    completed = [
        event
        for event in tester.events
        if isinstance(event, hci.HCI_Number_Of_Completed_Packets_Event)
    ]
    assert all(event.connection_handles == [link.handle] for event in completed)
    assert sum(sum(event.num_completed_packets) for event in completed) == 2
    await link.disconnect(local=True)


# Verify that a pending retransmission is not refragmented when a Data Length Update
# reduces the permitted packet size.
PROCEDURES |= procedure_cases(
    data_length_update_retransmission_during_an_update,
    {
        'LL/CON/CEN/BV-128-C': (
            'Data Length Update – Retransmission During an Update',
            {'central': True},
        ),
        'LL/CON/PER/BV-131-C': (
            'Data Length Update – Retransmission During an Update',
            {'central': False},
        ),
    },
)


async def data_length_update_invalid_parameters(tester, *, central, phy, report_change):
    """Data Length Update - Handling Invalid Data Length Responses.

    Initial Condition:
        Connect in the selected Central/Peripheral role and 1M/2M/Coded PHY.
        Read the IUT's supported lengths as IXIT. For ALT 5A negotiate minimum
        peer limits (no effective change); for ALT 5B negotiate maximum peer
        limits (an effective receive-length change and Host event).

    Test Case Configuration:
        LL/CON/CEN/BI-07-C: Central IUT, LE 1M, ALT 5A, minimum peer lengths.
        LL/CON/CEN/BI-08-C: Central IUT, LE 2M, ALT 5A, minimum peer lengths.
        LL/CON/CEN/BI-09-C: Central IUT, LE Coded, ALT 5A, minimum peer lengths.
        LL/CON/CEN/BI-24-C: Central IUT, LE 1M, ALT 5B, maximum peer lengths.
        LL/CON/CEN/BI-25-C: Central IUT, LE 2M, ALT 5B, maximum peer lengths.
        LL/CON/CEN/BI-26-C: Central IUT, LE Coded, ALT 5B, maximum peer lengths.
        LL/CON/PER/BI-10-C: Peripheral IUT, LE 1M, ALT 5A, minimum peer lengths.
        LL/CON/PER/BI-11-C: Peripheral IUT, LE 2M, ALT 5A, minimum peer lengths.
        LL/CON/PER/BI-12-C: Peripheral IUT, LE Coded, ALT 5A, minimum peer
            lengths.
        LL/CON/PER/BI-31-C: Peripheral IUT, LE 1M, ALT 5B, maximum peer lengths.
        LL/CON/PER/BI-32-C: Peripheral IUT, LE 2M, ALT 5B, maximum peer lengths.
        LL/CON/PER/BI-33-C: Peripheral IUT, LE Coded, ALT 5B, maximum peer
            lengths.

    Test Steps:
        For all twelve Table 4.3-117 rows, exercise both request alternatives:
        1-3. Send valid peer LENGTH_REQ, verify LENGTH_RSP and any corresponding
             effective-length event. Preserve the last valid negotiated limits.
        4A. Request random legal TX limits through HCI, receive LENGTH_REQ and
            answer with the row's faulty LENGTH_RSP. Retry up to ten HCI
            requests if necessary to cause an actual on-air request.
        4B. On a fresh connection, send the same row as a faulty LENGTH_REQ.
        5. Check every emitted length response, rejection and Host length event.
           Use the valid effective limits, never the malformed peer values,
           to select the subsequent DATA size and airtime.
        6. Send DATA at the negotiated receive boundary, verify exact Host ACL
           delivery and acknowledgements, and terminate the connection.

    Expected Outcome:
        Invalid fields do not produce out-of-range local or effective limits
        and do not prevent subsequent valid DATA reception. Any rejection
        identifies the received opcode. Valid values above the IUT's own IXIT
        maximum remain legal peer capabilities if within the Core ranges.

    Source Correction:
        LL.TS.p28 PDF 981 names MinRxTime/MinTxTime; the wire fields are Max*Time.
        Table 4.3-117 does not select 4A/4B, so execute both for every row.
        Table 4.3-116 ALT 5 selects the initial effective-change setup; do not
        infer a second length event merely from a malformed PDU. Core Vol 6,
        Part B, 4.5.10 requires notification when effective limits change.
        Step 4A's retry wording disagrees between ten and ten additional tries;
        use ten total attempts, the explicit Inconclusive threshold.
        The final verdict's valid IUT values refer to 4A.2 and 4B.2, not the
        faulty Lower Tester 4B.1. Figure 4.3-244's 4B response is LENGTH_RSP,
        not its copied LENGTH_REQ label. Core 2.4.2 permits an implementation
        to interpret invalid fields or reject the PDU; therefore accept valid
        bounded responses or correctly addressed rejections, rather than force
        the unconditional LENGTH_RSP in 4B.2. Core 2.4.2.21/4.5.10 define
        shared ranges, so supportedMax+1 is only faulty if outside those ranges.
        These are local corrections, not adopted SIG errata.

    Reference:
        LL.TS.p28, 4.3.7.35, PDF 978-982, Tables 4.3-116/117,
        Figure 4.3-244. Core Vol 6, Part B, 2.4.2, 2.4.2.21, 4.5.10 and 5.1.9.
    """
    supported = await _supported_lengths(tester)
    generator = random.Random(35)
    # Table order is TX first; LENGTH_FIELDS encodes RX first on the wire.
    field_order = (2, 3, 0, 1)
    rows = []
    for bounds, delta in ((MINIMUM, -1), (supported, 1), (MAXIMUM, 1)):
        for field in field_order:
            values = list(MINIMUM)
            values[field] = bounds[field] + delta
            rows.append(tuple(values))
    for number, faulty in enumerate(rows, 1):
        for local in (True, False):
            tester.output.write(
                f'INVALID DLE row={number} local={local} values={faulty}\n'
            )
            link = await establish(
                tester,
                central=central,
                peer_address=(
                    LOWER_PERIPHERAL_PUBLIC_ADDRESS
                    if central
                    else LOWER_CENTRAL_PUBLIC_ADDRESS
                ),
                interval=48 if phy == Phy.LE_CODED else 24,
            )
            features = (
                hci.LeFeatureMask.LE_DATA_PACKET_LENGTH_EXTENSION
                | hci.LeFeatureMask.LE_2M_PHY
                | hci.LeFeatureMask.LE_CODED_PHY
            )
            await link.control(
                Opcode.LL_FEATURE_RSP,
                bytes(
                    [
                        Opcode.LL_PERIPHERAL_FEATURE_REQ
                        if central
                        else Opcode.LL_FEATURE_REQ
                    ]
                )
                + int(features).to_bytes(PDU_CONTROL_FEATURE_SET_SIZE, 'little'),
            )
            if phy != Phy.LE_1M:
                await _change_phy(tester, link, phy, local=True)
            initial = _effective_lengths(MINIMUM, MINIMUM, link)
            _check_changed_events(tester, link.handle, MINIMUM, initial)
            peer = MAXIMUM if report_change else MINIMUM
            dut = _check_parameters(
                await link.control(
                    Opcode.LL_LENGTH_RSP,
                    bytes([Opcode.LL_LENGTH_REQ]) + LENGTH_FIELDS.pack(*peer),
                ),
                Opcode.LL_LENGTH_RSP,
                supported,
            )
            current = _effective_lengths(dut, peer, link)
            _check_changed_events(tester, link.handle, initial, current)
            assert (current != initial) == report_change
            if local:
                for _ in range(10):
                    await tester.command(
                        hci.HCI_LE_Set_Data_Length_Command(
                            link.handle,
                            generator.randint(MINIMUM[2], supported[2]),
                            generator.randint(MINIMUM[3], supported[3]),
                        )
                    )
                    request = await link.control(Opcode.LL_LENGTH_REQ, required=False)
                    if request is not None:
                        dut = _check_parameters(
                            request, Opcode.LL_LENGTH_REQ, supported
                        )
                        break
                else:
                    pytest.skip(
                        'Inconclusive: no LENGTH_REQ after ten legal HCI requests'
                    )
                # The Host change can remain pending until a valid peer reply.
                # Check any notification below, without requiring an early one.
            opcode = Opcode.LL_LENGTH_RSP if local else Opcode.LL_LENGTH_REQ
            packets = [
                await link.exchange(
                    bytes([opcode]) + LENGTH_FIELDS.pack(*faulty), llid=LlId.CONTROL
                )
            ]
            packets.extend([await link.exchange() for _ in range(3)])
            for packet in packets:
                if packet.pdu[0] & PDU_DATA_LLID_MASK != LlId.CONTROL:
                    continue
                payload = packet.pdu[PDU_HEADER_SIZE:]
                if payload[0] in (Opcode.LL_LENGTH_REQ, Opcode.LL_LENGTH_RSP):
                    _check_parameters(payload, Opcode(payload[0]), supported)
                elif payload[0] == Opcode.LL_REJECT_EXT_IND:
                    assert payload[1:] == bytes(
                        [opcode, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR]
                    )
                elif payload[0] == Opcode.LL_UNKNOWN_RSP:
                    assert payload[1:] == bytes([opcode])
                else:
                    raise AssertionError(
                        f'unexpected control after faulty length: {payload.hex()}'
                    )
            while event := tester.take(
                lambda event: isinstance(event, hci.HCI_LE_Data_Length_Change_Event)
            ):
                assert event.connection_handle == link.handle
                values = (
                    event.max_tx_octets,
                    event.max_tx_time,
                    event.max_rx_octets,
                    event.max_rx_time,
                )
                assert all(
                    low <= value <= high
                    for value, low, high in zip(values, MINIMUM, MAXIMUM)
                )
                current = values
            size = _payload_limit(link.tx_phy, current[2], current[3])
            assert size >= PDU_DATA_PAYLOAD_SIZE_MIN
            await peer_send(tester, link, generator.randbytes(size))
            await link.disconnect(local=True)


# Verify that the IUT rejects LL_LENGTH_RSP with invalid values in its selected role/PHY
# and continues receiving Lower Tester data.
PROCEDURES |= procedure_cases(
    data_length_update_invalid_parameters,
    {
        f'LL/CON/{role}/BI-{number:02}-C': (
            f'Data Length Update – Handling Invalid Data Length Responses – {name} PHY',
            {'central': role == 'CEN', 'phy': phy, 'report_change': changed},
        )
        for role, base, changed in (
            ('CEN', 7, False),
            ('CEN', 24, True),
            ('PER', 10, False),
            ('PER', 31, True),
        )
        for offset, phy, name in (
            (0, Phy.LE_1M, 'LE 1M'),
            (1, Phy.LE_2M, 'LE 2M'),
            (2, Phy.LE_CODED, 'LE Coded'),
        )
        for number in (base + offset,)
    },
)


async def data_length_update_peer_does_not_support_le_coded_phy(tester, *, central):
    """Data Length Update – Peer Does Not Support LE Coded PHY.

    Initial Condition:
        Connected in the selected role on LE 1M. The Lower Tester does not
        support LE Coded PHY. Use public peer addresses and supported maximum
        data lengths read through HCI as IXIT inputs. The specification minimum
        octet limit is 27; its maximum octet limit is 251. No Feature Exchange
        is initiated by the Lower Tester before the first length negotiation.

    Test Case Configuration:
        LL/CON/CEN/BV-129-C: Central.
        LL/CON/PER/BV-132-C: Peripheral.

    Test Steps:
        1. Send HCI_LE_Set_Data_Length with supportedMaxTxTime and TxOctets 27.
           Require successful HCI_Command_Complete.
        2. The IUT may issue LL_LENGTH_REQ; answer with LL_LENGTH_RSP.
        3. If the IUT initiated DLE and effective values changed, require Data
           Length Change. Otherwise require no such event.
        4. If no request was sent, repeat from Step 1 with TxOctets increased
           by 20, until the next value exceeds the specification maximum or
           ten rounds have run. If no request occurred, end Inconclusive.
        5. Send peer DATA packets at the size agreed by the length procedure.
        6. Require the IUT to forward those packets to the Upper Tester.
        7. For a Central IUT, execute Steps 9-12 and skip Steps 13-16.
        8. For a Peripheral IUT, skip Steps 9-12 and execute Steps 13-16.
        9. Send HCI LE Read Remote Features Page 0 for this connection and
           require successful Command Status.
        10. If the IUT previously sent LL_FEATURE_REQ, Steps 11-12 are optional.
        11. Receive LL_FEATURE_REQ from the Central IUT.
        12. Answer LL_FEATURE_RSP indicating no LE Coded PHY support.
        13. Send LL_FEATURE_REQ to the Peripheral IUT indicating no Coded PHY.
        14. Receive its LL_FEATURE_RSP.
        15. Send HCI LE Read Remote Features Page 0 for this connection and
            require successful Command Status.
        16. The Peripheral IUT may send LL_PERIPHERAL_FEATURE_REQ; if received,
            answer LL_FEATURE_RSP again indicating no LE Coded PHY support.
        17. Require successful Remote Features Page 0 Complete with this handle
            and no LE Coded PHY in the reported peer feature set.
        18. Send HCI_LE_Set_Data_Length with supportedMaxTxTime and TxOctets 27.
            Require successful HCI_Command_Complete.
        19. The IUT may issue LL_LENGTH_REQ. Fail if either RX or TX time exceeds
            2128 us. Answer LL_LENGTH_RSP.
        20. If the IUT initiated DLE in Step 19, require no Data Length Change;
            receipt of that event fails the test.
        21. If no request was sent, repeat from Step 18 with TxOctets increased
            by 20, stopping at the specification maximum or ten rounds. If no
            request occurred, conclude the test without Steps 22-23.
        22. Send peer DATA at the size agreed by the length procedure.
        23. Require those DATA packets at the Upper Tester.

    Expected Outcome:
        Pass verdict:
            The Step 19 RX/TX time fields are each at most 2128 us, the IUT
            reports no forbidden length change, and peer DATA reaches the Host.
            Step 21 also permits conclusion without another length request.
        Inconclusive verdict:
            No initial LL_LENGTH_REQ occurs before the Step 4 retry limit.

    Coverage Note:
        Execute the fresh connection's Central feature-request branch and both
        Peripheral paths, cached completion or optional on-air request. Use the
        existing HCI_LE_Read_Remote_Features command/event API for the source's
        Page 0 terminology. Keep peer length values fixed at 251 octets and
        2120 us per direction. Send two packets at each DATA checkpoint and
        verify exact bytes, handle and Host ACL flags, then disconnect via HCI.

    Reference:
        LL.TS.p28.pdf, Section 4.3.7.36; Table 4.3-118, Figures 4.3-245..246,
        all Steps 1-23 and verdicts; PDF 982-985, printed 4.3-509..512.
        Source references [8] 5.1.9 and [10] 4.5.10.
    """
    supported = await _supported_lengths(tester)
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    peer = (PDU_DATA_PAYLOAD_SIZE_MAX, PDU_DATA_PAYLOAD_TIME_UNCODED_MAX_US) * 2
    previous = _effective_lengths(MINIMUM, MINIMUM, link)
    for feature_known in (False, True):
        packet = None
        for attempt in range(UNCODED_REQUEST_ATTEMPTS):
            octets = (
                PDU_DATA_PAYLOAD_SIZE_MIN + attempt * UNCODED_REQUEST_OCTET_INCREMENT
            )
            if octets > PDU_DATA_PAYLOAD_SIZE_MAX:
                break
            result = await tester.command(
                hci.HCI_LE_Set_Data_Length_Command(link.handle, octets, supported[3])
            )
            assert result.connection_handle == link.handle
            packet = await link.control(Opcode.LL_LENGTH_REQ, required=False)
            if packet is None:
                continue
            dut = _check_parameters(packet, Opcode.LL_LENGTH_REQ, supported)
            if feature_known:
                assert max(dut[1], dut[3]) <= DLE_CODED_TIME_BOUNDARY_US
            await link.exchange(
                bytes([Opcode.LL_LENGTH_RSP]) + LENGTH_FIELDS.pack(*peer),
                llid=LlId.CONTROL,
            )
            await tester.wait(0)
            current = _effective_lengths(dut, peer, link)
            if feature_known:
                assert current == previous
            _check_changed_events(tester, link.handle, previous, current)
            previous = current
            size = _payload_limit(link.tx_phy, current[2], current[3])
            for _ in range(2):
                await peer_send(tester, link, bytes(range(size)))
            break
        else:
            packet = None
        if packet is None and (not feature_known):
            pytest.skip('Inconclusive: no initial LL_LENGTH_REQ within ten rounds')
        if feature_known:
            break
        features = int(hci.LeFeatureMask.LE_DATA_PACKET_LENGTH_EXTENSION).to_bytes(
            PDU_CONTROL_FEATURE_SET_SIZE, 'little'
        )
        if not central:
            response = await link.control(
                Opcode.LL_FEATURE_RSP, bytes([Opcode.LL_FEATURE_REQ]) + features
            )
            assert len(response) == 1 + PDU_CONTROL_FEATURE_SET_SIZE
        await tester.command(hci.HCI_LE_Read_Remote_Features_Command(link.handle))
        if central:
            await link.control(Opcode.LL_FEATURE_REQ)
            await link.exchange(
                bytes([Opcode.LL_FEATURE_RSP]) + features, llid=LlId.CONTROL
            )
        else:
            for _ in range(16):
                if any(
                    isinstance(event, hci.HCI_LE_Read_Remote_Features_Complete_Event)
                    for event in tester.events
                ):
                    break
                packet = await link.exchange()
                if packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL:
                    payload = packet.pdu[PDU_HEADER_SIZE:]
                    assert payload[0] == Opcode.LL_PERIPHERAL_FEATURE_REQ
                    assert len(payload) == 1 + PDU_CONTROL_FEATURE_SET_SIZE
                    await link.exchange(
                        bytes([Opcode.LL_FEATURE_RSP]) + features, llid=LlId.CONTROL
                    )
        event = await tester.event(hci.HCI_LE_Read_Remote_Features_Complete_Event)
        assert (
            event.status == hci.HCI_ErrorCode.SUCCESS
            and event.connection_handle == link.handle
        )
        assert (
            not int.from_bytes(event.le_features, 'little')
            & hci.LeFeatureMask.LE_CODED_PHY
        )
    assert not any(
        isinstance(event, hci.HCI_LE_Data_Length_Change_Event)
        for event in tester.events
    )
    await link.disconnect(local=True)


# Verify that, once an LE 1M peer is known not to support LE Coded PHY, outgoing
# length-procedure time fields do not exceed 2128 us and the IUT continues forwarding
# negotiated-size peer DATA to its Host.
PROCEDURES |= procedure_cases(
    data_length_update_peer_does_not_support_le_coded_phy,
    {
        'LL/CON/CEN/BV-129-C': (
            (
                'Data Length Update – Peer Does Not Support LE Coded PHY – '
                'supportedMaxTxTime'
            ),
            {'central': True},
        ),
        'LL/CON/PER/BV-132-C': (
            (
                'Data Length Update – Peer Does Not Support LE Coded PHY – '
                'supportedMaxTxTime'
            ),
            {'central': False},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_data_length(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
