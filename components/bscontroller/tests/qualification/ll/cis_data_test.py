# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""CIS data transfer and acknowledgement procedures from LL.TS.p28."""

from random import Random

import pytest

from bscontroller import hci
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import ControlOpcode, LlId
from bscontroller.radio.base import Phy
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.cis import (
    CIS_IND,
    CisPeer,
    channels,
    cis_request,
    connect_cis_acl,
    establish_peer_cis,
)
from tests.qualification.iso import data_path, iso_packet

NESN, SN, CIE, NPI = 0x04, 0x08, 0x10, 0x40


async def receive_sdu(tester, handle, payload):
    packet = await tester.expect(
        lambda event: isinstance(event, hci.HCI_IsoDataPacket)
        and event.connection_handle == handle
    )
    assert packet.pb_flag == 2 and packet.packet_status_flag == 0
    assert packet.iso_sdu_length == len(payload) and packet.iso_sdu_fragment == payload


async def encryption_after_cis(tester):
    """Set Encryption After CIS Established.

    Initial Condition:
        IUT is ACL Central with an established unencrypted CIS, NSE=BN=FT=1,
        130-byte maximum SDU/PDU and 20 ms ISO/SDU intervals.

    Test Steps:
        1. Issue LE Enable Encryption on the associated ACL.
        2. Require Command Disallowed in Command Status.

    Reference:
        LL.TS.p28, LL/CIS/CEN/BV-20-C, Table 4.10-7.
        Core Vol 4, Part E, 7.8.24 and Vol 6, Part B, 5.1.3.
    """
    link, _, _ = await establish_peer_cis(
        tester, central=True, test_nse=1, sdu_sizes=(130, 130)
    )
    await tester.command(
        hci.HCI_LE_Enable_Encryption_Command(link.handle, bytes(8), 0, bytes(16)),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )


async def sequences_at_flushpoint(tester, *, central):
    """Transmit and expected sequence numbers increment at a flush point.

    Initial Condition:
        One bidirectional unframed stream with LE 1M, ISO and SDU intervals
        of 100 ms, a 32-byte SDU/PDU limit and NSE=BN=FT=1.

    Test Case Configuration:
        CEN/BV-52 and PER/BV-41 select the IUT role in Table 4.10-68.

    Test Steps:
        1. Queue a different payload before each of four events.
        2. In the first two events send NULL with a negative acknowledgement.
        3. In the last two events send a Data PDU with a CRC error and the
           role-specific acknowledgement value from Steps 7 and 9.
        4. Observe the IUT's current payload and both sequence bits at each
           event. FT=1 makes the following event the first test of the new bits.

    Reference:
        LL.TS.p28, 4.10.5.13, Tables 4.10-68 and 4.10-69, Figure 4.10-60.
    """
    _, peer, complete = await establish_peer_cis(
        tester, central=central, interval_us=100000, test_nse=1, sdu_sizes=(32, 32)
    )
    handle = complete.connection_handle
    await data_path(tester, handle, 0)
    for index in range(4):
        counter = peer.counter
        parity = counter & 1
        data = bytes([index]) * 32
        tester.send(bytes(iso_packet(handle, counter, data)))
        bad_crc = index >= 2
        if bad_crc:
            header = parity * SN | ((parity ^ int(central)) * NESN)
        else:
            header = NPI | parity * NESN
        packet = await peer.exchange(
            b'CRC check' if bad_crc else b'',
            header=header,
            bad_crc=bad_crc,
            expect_rx=None if bad_crc and not central else True,
        )
        if packet is not None:
            assert packet.pdu == bytes([parity * (SN | NESN), len(data)]) + data


async def bidirectional(tester, *, central, encrypted=False):
    """Sending and receiving data in bidirectional CIS.

    Initial Condition:
        An LE 1M CIS with BN=FT=1, six subevents and a 100 ms SDU/ISO
        interval, using 251-byte maximum SDU/PDUs in both directions.

    Test Case Configuration:
        CEN/BV-07 and PER/BV-06 run without encryption. PER/BV-27 starts
        encryption on the ACL before creating CIS.

    Test Steps:
        1. Open HCI paths in both directions. Supply BN SDUs before the event.
        2. Exchange data on the first subevent. Check LLID, CIE, payload length,
           SN and NESN on the IUT transmission and the exact Host delivery.
        3. In the encrypted round continue bidirectional transmission, then
           inject an invalid MIC over the CIS and require reason 0x3D.

    Source Correction:
        Table 4.10-48 requests a 100 ms ISO interval at BN=1. Use 0x50 as
        encoded in 1.25 ms units, since 0xA0 represents 200 ms.

    Reference:
        LL.TS.p28, 4.10.5.3, Tables 4.10-48 and 4.10-49, Figure 4.10-50.
    """
    link = await connect_cis_acl(
        tester, central=central, interval=80, encrypted=encrypted
    )
    _, peer, complete = await establish_peer_cis(
        tester, central=central, link=link, interval_us=100000, test_nse=6
    )
    handle = complete.connection_handle
    while peer.subevent:
        await peer.exchange()
    await data_path(tester, handle, 0)
    await data_path(tester, handle, 1)
    random = Random(41049)
    for _ in range(3 if encrypted else 1):
        counter = peer.counter
        parity = counter & 1
        sent, incoming = random.randbytes(28), random.randbytes(31)
        tester.send(bytes(iso_packet(handle, counter, sent)))
        packet = await peer.exchange(incoming, header=parity * (SN | NESN))
        expected_nesn = parity if central else parity ^ 1
        assert (
            packet.pdu == bytes([parity * SN | expected_nesn * NESN, len(sent)]) + sent
        )
        while peer.subevent:
            await peer.exchange(header=NPI | ((parity ^ 1) * NESN))
        await receive_sdu(tester, handle, incoming)
    if encrypted:
        parity = peer.counter & 1
        await peer.exchange(
            b'corrupted MIC',
            header=parity * (SN | NESN),
            bad_mic=True,
            expect_rx=False,
        )
        event = await tester.event(hci.HCI_Disconnection_Complete_Event)
        assert (event.connection_handle, event.reason) == (
            handle,
            ErrorCode.CONNECTION_TERMINATED_DUE_TO_MIC_FAILURE_ERROR,
        )


async def negotiate_peripheral_stream(tester, link, request, *, address, delays):
    """Send test-selected parameters and return the peer before its first subevent."""
    await link.exchange(request, llid=LlId.CONTROL)
    requested = await tester.event(hci.HCI_LE_CIS_Request_Event)
    assert (requested.acl_connection_handle, requested.cig_id, requested.cis_id) == (
        link.handle,
        request[1],
        request[2],
    )
    handle = requested.cis_connection_handle
    await tester.command(hci.HCI_LE_Accept_CIS_Request_Command(handle))
    response = await link.control(ControlOpcode.LL_CIS_RSP)
    offset = int.from_bytes(response[1:4], 'little')
    count = int.from_bytes(response[-2:], 'little')
    indication = b''.join(
        (
            bytes([CIS_IND]),
            address.to_bytes(4, 'little'),
            offset.to_bytes(3, 'little'),
            delays[0].to_bytes(3, 'little'),
            delays[1].to_bytes(3, 'little'),
            count.to_bytes(2, 'little'),
        )
    )
    await link.exchange(indication, llid=LlId.CONTROL, check_ack=True)
    relative = (count - link.counter + 32768) % 65536 - 32768
    anchor = link.anchor + relative * link.parameters.interval * 1250 + offset
    peer = CisPeer(
        link,
        address,
        anchor,
        1250 * int.from_bytes(request[26:28], 'little'),
        request[19],
        int.from_bytes(request[20:23], 'little'),
    )
    return peer, handle


async def observe_established(tester, peer, handle):
    await peer.exchange()
    event = await tester.expect(
        lambda event: isinstance(event, hci.HCI_LE_CIS_Established_Event)
        and event.connection_handle == handle
    )
    assert event.status == ErrorCode.SUCCESS


async def no_unsolicited_peripheral_pdu(tester):
    """CIS Peripheral Response, No PDU Received in Subevent.

    Initial Condition:
        20 ms ISO intervals with five subevents, only P-to-C payloads,
        an input HCI path and the Lower Tester in the Central role.

    Test Steps:
        1. Supply data for each of five subevents in 50 consecutive events.
        2. On matching event/subevent parity, send NULL with a NACK and
           require the Peripheral's data response.
        3. On opposite parity, listen across the Central transmit opportunity,
           T_IFS_CIS and receiver margin without transmitting. Require silence.

    Reference:
        LL.TS.p28, LL/CIS/PER/BV-28-C, Figure 4.10-38; Core Vol 6, B, 4.5.13.2.
    """
    link = await connect_cis_acl(tester, interval=16)
    request = cis_request(
        link.counter + 6,
        c_sdu=0,
        p_sdu=251,
        nse=5,
        subinterval_us=2700,
        offset_us=2500,
    )
    max_packet_c = Phy.LE_1M.airtime_us(2)
    max_packet_p = Phy.LE_1M.airtime_us(2 + 251 + 4)
    delay = 4 * 2700 + max_packet_c + 150 + max_packet_p
    peer, handle = await negotiate_peripheral_stream(
        tester, link, request, address=0x7654BA98, delays=(delay, delay)
    )
    await observe_established(tester, peer, handle)
    while peer.subevent:
        await peer.exchange()
    await data_path(tester, handle, 0)

    margin = Phy.LE_1M.airtime_us(2)
    outgoing = b'CIS Peripheral data'
    for event_index in range(50):
        counter = peer.counter
        parity = counter & 1
        for index in range(5):
            tester.send(bytes(iso_packet(handle, event_index * 5 + index, outgoing)))
            if (index + 1) % 2 == parity:
                response = await peer.exchange(header=NPI | parity * NESN)
                assert response.pdu == bytes([parity * SN, len(outgoing)]) + outgoing
                continue
            nominal = peer.nominal_us
            while link.anchor + 1000 < nominal:
                await link.exchange()
            channel = channels(
                counter, peer.access_address, link.parameters.channel_map, peer.nse
            )[index]
            clear = await tester.receive(
                channel,
                start_us=max(tester.radio.now_us + 1, nominal - margin),
                duration_us=nominal + max_packet_c + 150 + margin - tester.radio.now_us,
                access_address=peer.access_address,
                crc_init=link.parameters.crc_init,
                phy=Phy.LE_1M,
            )
            assert clear is None, (
                f'Peripheral transmitted in silent subevent {index + 1}'
            )
            peer.step()


async def two_cis_one_cig(tester):
    """Sending and receiving data in multiple CISes on one ACL, Peripheral BN=1.

    Initial Condition:
        A Peripheral ACL with two LE 1M unframed CISes in one interleaved CIG.
        Each CIS has one subevent, BN=FT=1, 251-byte maximum SDU/PDU and a
        50 ms ISO/SDU interval.

    Test Steps:
        1. Complete both CIS handshakes and configure both HCI directions.
        2. Queue two distinct HCI ISO SDUs before the event. Send a different
           payload on each CIS and check the corresponding wire response and
           HCI ISO payload/handle.
        3. Repeat the two-stream exchange three times.

    Reference:
        LL.TS.p28, LL/CIS/PER/BV-32-C, Table 4.10-36, Figure 4.10-40.
    """
    link = await connect_cis_acl(tester, interval=80)
    count = (link.counter + 12) & 0xFFFF
    exchanges = []
    spacing = 5000
    duration = 2 * Phy.LE_1M.airtime_us(2 + 251 + 4) + 150
    for index, address in enumerate((0x7654BA98, 0x334C8571)):
        request = bytearray(
            cis_request(
                count,
                interval_us=50000,
                subinterval_us=0,
                nse=1,
                offset_us=2500 + index * spacing,
            )
        )
        request[2] = 1 + index
        peer, handle = await negotiate_peripheral_stream(
            tester,
            link,
            bytes(request),
            address=address,
            delays=(duration + spacing, duration + (1 - index) * spacing),
        )
        exchanges.append((peer, handle))
    for peer, handle in exchanges:
        await observe_established(tester, peer, handle)
        await data_path(tester, handle, 0)
        await data_path(tester, handle, 1)
    for repeated in range(3):
        observed = []
        for index, (peer, handle) in enumerate(exchanges):
            outgoing = bytes([0, index, repeated]) * 13
            incoming = bytes([1, index, repeated]) * 17
            tester.send(bytes(iso_packet(handle, repeated, outgoing)))
            observed.append((peer, handle, outgoing, incoming))
        for peer, _, outgoing, incoming in observed:
            parity = peer.counter & 1
            response = await peer.exchange(incoming, header=parity * (SN | NESN))
            assert response.pdu == (
                bytes([parity * SN | ((parity ^ 1) * NESN), len(outgoing)]) + outgoing
            )
        for _, handle, _, incoming in observed:
            await receive_sdu(tester, handle, incoming)


PROCEDURES = ProcedureMap()

# The encryption command changes ACL key state only before CIS creation.
PROCEDURES |= procedure_cases(
    encryption_after_cis,
    {'LL/CIS/CEN/BV-20-C': ('Set Encryption After CIS Established', {})},
)

# In each role an unacknowledged payload expires and advances both counters.
PROCEDURES |= procedure_cases(
    sequences_at_flushpoint,
    {
        'LL/CIS/CEN/BV-52-C': (
            'Central advances sequence at flushpoint',
            {'central': True},
        ),
        'LL/CIS/PER/BV-41-C': (
            'Peripheral advances sequence at flushpoint',
            {'central': False},
        ),
    },
)

# Check the data PDU and Host delivery in each selected role and encryption state.
PROCEDURES |= procedure_cases(
    bidirectional,
    {
        'LL/CIS/CEN/BV-07-C': ('Central bidirectional CIS data', {'central': True}),
        'LL/CIS/PER/BV-06-C': ('Peripheral bidirectional CIS data', {'central': False}),
        'LL/CIS/PER/BV-27-C': (
            'Peripheral encrypted CIS data and MIC failure',
            {'central': False, 'encrypted': True},
        ),
    },
)

# Only the subevents with a peer transmission may produce a Peripheral response.
PROCEDURES |= procedure_cases(
    no_unsolicited_peripheral_pdu,
    {'LL/CIS/PER/BV-28-C': ('Peripheral silent when the Central sends no PDU', {})},
)

# Each stream must retain its own HCI handle and over-air payloads.
PROCEDURES |= procedure_cases(
    two_cis_one_cig,
    {'LL/CIS/PER/BV-32-C': ('Two bidirectional CISes in a Peripheral CIG', {})},
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks', collect_cases(PROCEDURES)
)
async def test_cis_data(air_dut, identifier, description, procedure, parameters, marks):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
