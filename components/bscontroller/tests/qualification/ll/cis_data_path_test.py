# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.10.5: Connected Isochronous Stream data paths."""

import random

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
    data_path,
    expect_iso_credits,
    iso_packet,
    send_iso_sdu,
)

MAX_PDU = 251
NESN = 0x04
SN = 0x08
NPI = 0x40


def _take_iso(tester, handle):
    return tester.take(
        lambda event: isinstance(event, hci.HCI_IsoDataPacket)
        and event.connection_handle == handle
    )


def _received_sdus(tester, handle):
    while (packet := _take_iso(tester, handle)) is not None:
        assert packet.pb_flag in (0, 2)
        sequence = packet.packet_sequence_number
        if packet.packet_status_flag:
            assert packet.packet_status_flag == 2 and packet.pb_flag == 2
            yield sequence, None
            continue
        size = packet.iso_sdu_length
        data = bytearray(packet.iso_sdu_fragment)
        if packet.pb_flag == 0:
            while True:
                fragment = _take_iso(tester, handle)
                assert fragment is not None and fragment.pb_flag in (1, 3)
                data.extend(fragment.iso_sdu_fragment)
                if fragment.pb_flag == 3:
                    break
        assert len(data) == size
        yield sequence, bytes(data)


def _acknowledge(packet):
    # A Central data PDU with SN=x is acknowledged with NESN=x^1.
    return 0 if packet is None or packet.pdu[0] & SN else NESN


async def send_unidirectional(tester, *, central):
    """Send unframed data in the first subevent of a unidirectional CIS.

    Initial Condition:
        The CIS uses LE 1M, SDU and ISO intervals of 100 ms, FT and NSE of 1
        and BN=1 only in the IUT's transmit direction. Configure HCI input.

    Test Case Configuration:
        LL/CIS/CEN/BV-45-C uses the IUT as Central. LL/CIS/PER/BV-33-C uses
        the IUT as Peripheral.

    Test Steps:
        1. Send an HCI ISO SDU before the next first subevent.
        2. Exchange the CIS data PDU and the Lower Tester's NULL PDU in
           Central-then-Peripheral order.

    Expected Outcome:
        The Lower Tester receives identical data with LLID=0, NPI=0 and
        correct sequence bits and length. The Host receives one credit for
        each HCI ISO packet it submitted on the CIS handle.

    Reference:
        LL.TS.p28, 4.10.5.10, LL/CIS/CEN/BV-45-C and LL/CIS/PER/BV-33-C,
        Tables 4.10-60 to 4.10-62.
    """
    buffers = await tester.command(hci.HCI_LE_Read_Buffer_Size_V2_Command())
    _, air, event = await establish_peer_cis(
        tester,
        central=central,
        interval_us=100_000,
        test_nse=1,
        sdu_sizes=(MAX_PDU, 0) if central else (0, MAX_PDU),
    )
    handle = event.connection_handle
    await data_path(tester, handle, 0)
    payload = bytes(range(MAX_PDU))
    pending = await send_iso_sdu(tester, handle, 0, payload, buffers)
    parity = air.counter & 1
    if central:

        def header(packet):
            return NPI | _acknowledge(packet)
    else:
        header = NPI | (parity * NESN)
    pdu = (await air.exchange(header=header)).pdu
    assert pdu[0] & (0x03 | NPI | SN | NESN) == parity * SN
    assert pdu[1] == len(payload) and pdu[2:] == payload
    await expect_iso_credits(tester, handle, pending)


async def receive_unidirectional(tester, *, central):
    """Receive unframed data in the first subevent of a unidirectional CIS.

    Initial Condition:
        The CIS uses LE 1M, SDU and ISO intervals of 100 ms, FT and NSE of 1
        and BN=1 only in the IUT's receive direction. Configure HCI output.

    Test Case Configuration:
        LL/CIS/CEN/BV-46-C uses the IUT as Central. LL/CIS/PER/BV-34-C uses
        the IUT as Peripheral.

    Test Steps:
        1. In the first subevent exchange the IUT's NULL PDU and one payload
           PDU with LLID=0 and NPI=0, in Central-then-Peripheral order.
        2. Observe the data the IUT forwards on the CIS handle.

    Expected Outcome:
        The IUT sends a NULL PDU and forwards the successful SDU intact to
        the Host. The Peripheral IUT acknowledges the payload in its reply.

    Reference:
        LL.TS.p28, 4.10.5.11, LL/CIS/CEN/BV-46-C and LL/CIS/PER/BV-34-C,
        Tables 4.10-63 to 4.10-65.
    """
    _, air, event = await establish_peer_cis(
        tester,
        central=central,
        interval_us=100_000,
        test_nse=1,
        sdu_sizes=(0, MAX_PDU) if central else (MAX_PDU, 0),
    )
    handle = event.connection_handle
    await data_path(tester, handle, 1)
    payload = bytes(range(MAX_PDU))
    counter = air.counter
    delivery_us = air.nominal_us + event.cis_sync_delay
    pdu = (await air.exchange(payload, header=(counter & 1) * SN)).pdu
    ack = ((counter & 1) ^ (not central)) * NESN
    assert pdu[0] & (0x03 | NPI | SN | NESN) == NPI | ack
    assert pdu[1:] == b'\x00'
    await tester.wait(max(1, delivery_us - tester.radio.now_us + 1))
    assert [(seq, data) for seq, data in _received_sdus(tester, handle) if data] == [
        (counter, payload)
    ]


async def _sample_directions(tester, air, event, *, from_host, to_host):
    """Compare fifty independently generated Host and Lower Tester SDUs."""
    handle = event.connection_handle
    sent = received = 0
    expected = {}
    rng = random.Random(air.counter)
    for _ in range(50):
        sequence = air.counter & 1
        delivery_us = air.nominal_us + event.cis_sync_delay
        from_iut, to_iut = rng.randbytes(32), rng.randbytes(32)
        expected[air.counter] = to_iut
        tester.send(bytes(iso_packet(handle, air.counter, from_iut)))
        if air.link.central:

            def header(packet):
                return sequence * SN | _acknowledge(packet)
        else:
            header = sequence * (SN | NESN)
        packet = await air.exchange(to_iut, header=header, expect_rx=None)
        if packet is not None and packet.pdu[2:]:
            assert from_host
            assert packet.pdu[0] & (0x03 | NPI) == 0
            assert packet.pdu[2:] == from_iut
            sent += 1
        # Allow for the Peripheral's SDU synchronization reference at the
        # end of a CIS event with this unframed FT=1 configuration.
        await tester.wait(max(1, delivery_us - tester.radio.now_us + 1))
        for counter, output in _received_sdus(tester, handle):
            assert to_host
            if output is not None:
                assert counter in expected, f'unexpected ISO SDU sequence {counter}'
                assert output == expected.pop(counter)
                received += 1
    return sent, received


async def configure_one_direction_at_a_time(tester, *, central):
    """Enable each CIS data path independently while both sides send SDUs.

    Initial Condition:
        The IUT has a LE 1M ACL to the Lower Tester and enables ISO Channels
        Host Support. Establish an unframed bidirectional CIS using 20 ms
        ISO and SDU intervals and FT, BN and NSE of 1.

    Test Case Configuration:
        LL/CIS/CEN/BV-61-C uses the IUT as Central. LL/CIS/PER/BV-51-C uses
        the IUT as Peripheral.

    Test Steps:
        1. Set up HCI input only. Feed both directions for 50 events, then
           enable HCI output and feed both for 50 more events.
        2. Disconnect and establish a fresh CIS without data paths.
        3. Repeat with HCI output first and HCI input enabled later.
        4. Disconnect the second CIS.

    Expected Outcome:
        The disabled path delivers no payload. Enabled paths convey their
        corresponding SDUs and meet the 48-of-50 verdict thresholds.

    Reference:
        LL.TS.p28, 4.10.5.15, LL/CIS/CEN/BV-61-C and LL/CIS/PER/BV-51-C,
        Tables 4.10-72 and 4.10-73.
    """
    link = None
    for direction in (0, 1):
        link, air, event = await establish_peer_cis(
            tester, central=central, link=link, test_nse=1
        )
        handle = event.connection_handle
        await data_path(tester, handle, direction)
        air.skip_elapsed()
        sent, received = await _sample_directions(
            tester, air, event, from_host=direction == 0, to_host=direction == 1
        )
        if direction == 0:
            assert sent >= 48 and received == 0
        else:
            assert sent == 0 and received >= 48
        await data_path(tester, handle, 1 - direction)
        air.skip_elapsed()
        sent, received = await _sample_directions(
            tester, air, event, from_host=True, to_host=True
        )
        if direction == 0:
            assert sent >= 48 and received > 0
        else:
            assert sent > 0 and received >= 48
        await disconnect_cis(tester, link, handle)
        if central:
            await tester.command(hci.HCI_LE_Remove_CIG_Command(1))


async def receive_variable_pdu_sizes(tester, *, central):
    """Receive an unframed CIS with fixed and variable SDU lengths.

    Initial Condition:
        The IUT has a receive-only CIS using LE 1M, Max_SDU=Max_PDU=251,
        20 ms ISO and SDU intervals, BN=FT=1 in the receive direction and
        four subevents per event. Configure HCI output on the CIS.

    Test Case Configuration:
        LL/CIS/CEN/BV-62-C uses the IUT as Central. LL/CIS/PER/BV-53-C uses
        the IUT as Peripheral.

    Test Steps:
        1. Send 20 SDUs of 251 bytes, then 20 of 76 bytes, transmitting on
           every subevent.
        2. Send 20 more SDUs with random lengths from 1 through 251 bytes.
           Each length differs from the previous SDU length.

    Expected Outcome:
        At least 90% of the SDUs and the first at each size transition are
        forwarded. Every successful HCI ISO SDU matches the transmitted data.

    Reference:
        LL.TS.p28, 4.10.5.17, LL/CIS/CEN/BV-62-C and LL/CIS/PER/BV-53-C,
        Table 4.10-76.
    """
    _, air, event = await establish_peer_cis(
        tester, central=central, receive_only=True, test_nse=4, offset_us=2500
    )
    handle = event.connection_handle
    await data_path(tester, handle, 1)
    air.skip_elapsed()
    while air.subevent:
        await air.exchange(expect_rx=None)
    rng = random.Random(0)
    reduced = (MAX_PDU * 3 + 9) // 10
    previous = reduced
    expected = {}
    delivered = set()
    transitions = set()
    for index in range(60):
        if index < 20:
            size = MAX_PDU
        elif index < 40:
            size = reduced
        else:
            size = rng.randrange(1, MAX_PDU)
            if size >= previous:
                size += 1
            previous = size
        payload = rng.randbytes(size)
        expected[air.counter] = payload
        if index in (20, 40):
            transitions.add(air.counter)
        header = (air.counter & 1) * SN
        for _ in range(air.nse):
            packet = await air.exchange(payload, header=header)
            assert packet.pdu[0] & NPI and packet.pdu[1:] == b'\x00'
        await tester.wait(1)
        for counter, output in _received_sdus(tester, handle):
            if output is not None:
                assert counter in expected, f'unexpected ISO SDU sequence {counter}'
                assert output == expected.pop(counter)
                delivered.add(counter)
    assert len(delivered) >= 54
    assert transitions <= delivered


PROCEDURES = ProcedureMap()

# Check the data and flow-control credit independently of the ACL exchange.
PROCEDURES |= procedure_cases(
    send_unidirectional,
    {
        'LL/CIS/CEN/BV-45-C': ('Send Unidirectional CIS, Central', {'central': True}),
        'LL/CIS/PER/BV-33-C': (
            'Send Unidirectional CIS, Peripheral',
            {'central': False},
        ),
    },
)

# The direction with BN=0 carries NULL PDUs only.
PROCEDURES |= procedure_cases(
    receive_unidirectional,
    {
        'LL/CIS/CEN/BV-46-C': (
            'Receive Unidirectional CIS, Central',
            {'central': True},
        ),
        'LL/CIS/PER/BV-34-C': (
            'Receive Unidirectional CIS, Peripheral',
            {'central': False},
        ),
    },
)

# Both roles enable the data sink and source in opposite orders on fresh CISes.
PROCEDURES |= procedure_cases(
    configure_one_direction_at_a_time,
    {
        'LL/CIS/CEN/BV-61-C': ('CIS Data Path Setup, Central', {'central': True}),
        'LL/CIS/PER/BV-51-C': ('CIS Data Path Setup, Peripheral', {'central': False}),
    },
)

# Each role receives all three SDU length phases with the same air procedure.
PROCEDURES |= procedure_cases(
    receive_variable_pdu_sizes,
    {
        'LL/CIS/CEN/BV-62-C': ('Variable PDU Size, Central', {'central': True}),
        'LL/CIS/PER/BV-53-C': ('Variable PDU Size, Peripheral', {'central': False}),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES, radio_cases=PROCEDURES, radio=True),
)
async def test_cis_data_path(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
