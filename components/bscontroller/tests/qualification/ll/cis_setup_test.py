# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, 4.10.3 and 4.10.4: CIS establishment."""

from math import ceil
from random import Random

import pytest

from bscontroller import hci
from bscontroller.linklayer import ControlOpcode, LlId
from bscontroller.radio.base import Phy
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.cis import (
    cis_request,
    connect_cis_acl,
    disconnect_cis,
    establish_configured_cis,
    establish_peer_cis,
)
from tests.qualification.iso import data_path, iso_packet

CIS_COMPLETE = hci.HCI_LE_CIS_Established_Event
NPI, NESN, SN, RFU = 0x40, 0x04, 0x08, 0x80


def _integer(data, start, length):
    return int.from_bytes(data[start : start + length], 'little')


def _check_initial_packet(tester, handle):
    assert (
        tester.take(
            lambda event: isinstance(event, CIS_COMPLETE)
            and event.connection_handle == handle
        )
        is None
    )


async def _start_central_cis(tester, link, command, phys, sizes, *, framed, sdu_us):
    configured = await tester.command(command)
    (handle,) = configured.connection_handle
    assert configured.cig_id == 1 and handle <= 0xEFF

    def check_request(request):
        assert request[1:3] == b'\x01\x01'
        assert request[3:5] == bytes(1 << (phy.value - 1) for phy in phys)
        assert _integer(request, 5, 2) == sizes[0] | (framed << 15)
        assert _integer(request, 7, 2) == sizes[1]
        assert _integer(request, 9, 3) == _integer(request, 12, 3) == sdu_us
        pdu_sizes = _integer(request, 15, 2), _integer(request, 17, 2)
        bn = request[23] & 15, request[23] >> 4
        assert bn == tuple(bool(size) for size in sizes)
        assert max(bn) <= request[19] <= 31 and request[24:26] == b'\x01\x01'
        if isinstance(command, hci.HCI_LE_Set_CIG_Parameters_Test_Command):
            assert pdu_sizes == sizes
            assert request[19] == command.nse[0]
            assert _integer(request, 26, 2) == command.iso_interval
        subinterval = _integer(request, 20, 3)
        packet_time = sum(
            phy.airtime_us(size + 6) for phy, size in zip(phys, pdu_sizes)
        )
        assert subinterval >= packet_time + 300 if request[19] > 1 else subinterval == 0
        duration = packet_time + 300 + (request[19] - 1) * subinterval
        assert (
            500
            <= _integer(request, 28, 3)
            <= _integer(request, 31, 3)
            <= link.parameters.interval * 1250 - duration
        )
        for sdu, pdu in zip(sizes, pdu_sizes):
            if framed and sdu:
                factor = 1.0001 * _integer(request, 26, 2) * 1250 / sdu_us
                assert pdu - 2 >= ceil(factor) * 5 + ceil(factor * sdu)

    def check_indication(request, indication):
        assert (
            _integer(request, 28, 3)
            <= _integer(indication, 5, 3)
            <= _integer(request, 31, 3)
        )
        assert _integer(indication, 8, 3) == _integer(indication, 11, 3) > 0
        assert indication[-2:] == request[-2:]

    _, peer, _ = await establish_configured_cis(
        tester,
        link,
        handle,
        check_request=check_request,
        check_indication=check_indication,
        exchange_first=False,
    )
    _check_initial_packet(tester, handle)
    packet = await peer.exchange(header=_acknowledge)
    assert packet.pdu in (b'\x40\x00', bytes((2 if framed else 0, 0)))
    complete = await tester.event(CIS_COMPLETE)
    assert (complete.status, complete.connection_handle) == (0, handle)
    assert (complete.iso_interval, complete.nse) == (peer.interval_us // 1250, peer.nse)
    assert (complete.bn_c_to_p, complete.bn_p_to_c) == tuple(bool(n) for n in sizes)
    assert (complete.ft_c_to_p, complete.ft_p_to_c) == (1, 1)
    return peer, complete


def _acknowledge(packet):
    return NPI | (0 if packet is None or packet.pdu[0] & SN else NESN)


async def _finish_event(peer):
    while peer.subevent:
        await peer.exchange(header=_acknowledge, expect_rx=None)


async def central_setup_test(tester, *, phys, sizes, interval_us, nse):
    """CIS Setup Procedure, Central Initiated with the Test command.

    Initial Condition:
        A Central ACL with CIS Host support enabled and the Central-to-
        Peripheral PHY in Table 4.10-1. The Lower Tester is Peripheral.

    Test Case Configuration:
        CEN/BV-25 uses LE Coded in both directions, CEN/BV-32 combines
        LE 1M data with LE Coded Null replies, and CEN/BV-39 uses LE 2M.

    Test Steps:
        1. Configure one CIS with the exact parameters from Tables 4.10-1
           and 4.10-2 and create it on the ACL.
        2. Respond to the request and check the indication and the resulting
           air timing, then verify establishment after the first peer PDU.
        3. Send 50 different Host payloads, acknowledging each over the air.
           Require at least 48 full, matching payloads.

    Reference:
        LL.TS.p28, 4.10.3.1, Figures 4.10-1 and 4.10-2.
    """
    link = await connect_cis_acl(tester, central=True, interval=64, acl_phy=phys[0])
    masks = tuple(1 << (phy.value - 1) for phy in phys)
    command = hci.HCI_LE_Set_CIG_Parameters_Test_Command(
        1,
        interval_us,
        interval_us,
        1,
        1,
        interval_us // 1250,
        0,
        0,
        0,
        [1],
        [nse],
        [sizes[0]],
        [sizes[1]],
        [sizes[0]],
        [sizes[1]],
        [masks[0]],
        [masks[1]],
        [bool(sizes[0])],
        [bool(sizes[1])],
    )
    peer, event = await _start_central_cis(
        tester, link, command, phys, sizes, framed=False, sdu_us=interval_us
    )
    assert (event.max_pdu_c_to_p, event.max_pdu_p_to_c) == sizes
    await data_path(tester, event.connection_handle, 0)
    await _finish_event(peer)
    rng = Random(41001)
    received = 0
    for _ in range(50):
        payload = rng.randbytes(sizes[0])
        tester.send(bytes(iso_packet(event.connection_handle, peer.counter, payload)))
        counter = peer.counter
        found = False
        while peer.counter == counter:
            packet = await peer.exchange(header=_acknowledge, expect_rx=None)
            if packet is not None and packet.pdu[1]:
                assert packet.pdu[0] & (NPI | RFU | 3) == 0
                assert packet.pdu[2:] == payload
                found = True
        received += found
    assert received >= 48


async def _normal_parameters(
    tester, link, phy, sdu_us, sizes, latency_ms, *, framed, prefer_unframed=False
):
    mask = 1 << (phy.value - 1)
    return await _start_central_cis(
        tester,
        link,
        hci.HCI_LE_Set_CIG_Parameters_Command(
            1,
            sdu_us,
            sdu_us,
            0,
            0,
            int(framed and not prefer_unframed),
            latency_ms,
            latency_ms,
            [1],
            [sizes[0]],
            [sizes[1]],
            [mask],
            [mask],
            [3],
            [3],
        ),
        (phy, phy),
        sizes,
        sdu_us=sdu_us,
        framed=framed,
    )


async def _check_framed_data(tester, peer, event, sdu_us, max_latency_ms, size):
    expected_latency = event.cig_sync_delay + peer.interval_us + sdu_us
    assert event.transport_latency_c_to_p == event.transport_latency_p_to_c
    assert event.transport_latency_c_to_p == expected_latency <= max_latency_ms * 1000
    await data_path(tester, event.connection_handle, 0)
    await _finish_event(peer)
    payload = bytes(range(size))
    timestamp = tester.radio.now_us
    tester.send(bytes(iso_packet(event.connection_handle, 0, payload)))
    for _ in range(2 * peer.nse):
        packet = await peer.exchange(header=_acknowledge, expect_rx=None)
        if packet is not None and packet.pdu[1]:
            pdu = packet.pdu
            assert pdu[0] & (NPI | RFU | 3) == 2
            assert pdu[2:4] == bytes((2, len(payload) + 3))
            assert 0 < _integer(pdu, 4, 3) <= packet.start_us - timestamp
            assert pdu[7:] == payload
            return
    pytest.fail('No framed CIS payload received')


async def central_normal_setup(tester, *, phy, latency_ms):
    """Connected Isochronous Stream using the normal CIG command.

    Initial Condition:
        An ACL to a Peripheral Lower Tester with CIS Host support enabled.

    Test Case Configuration:
        CEN/BV-26, BV-27 and BV-28 select LE 1M, LE 2M and LE Coded,
        respectively, with the transport latency in Table 4.10-8.

    Test Steps:
        1. Configure a segmentable framed CIS carrying 16-byte SDUs.
        2. Complete the LL creation sequence, compare the HCI latency and
           receive and acknowledge framed data with the RFU bit clear.
        3. Disconnect, acknowledge the termination, and remove the CIG.
        4. Establish a receive-only CIS with BN_C_To_P and Max_SDU_C_To_P
           both zero. Receive a Null PDU and deliver ISO data to the Host
           50 times.

    Reference:
        LL.TS.p28, 4.10.3.4, Table 4.10-8 and Figures 4.10-7 and 4.10-8.
    """
    link = await connect_cis_acl(tester, central=True, interval=64)
    peer, event = await _normal_parameters(
        tester, link, phy, 20000, (16, 16), latency_ms, framed=True
    )
    await _check_framed_data(tester, peer, event, 20000, latency_ms, 16)
    await disconnect_cis(tester, link, event.connection_handle)
    removed = await tester.command(hci.HCI_LE_Remove_CIG_Command(1))
    assert removed.cig_id == 1

    peer, event = await _normal_parameters(
        tester, link, Phy.LE_1M, 20000, (0, 251), 40, framed=False
    )
    handle = event.connection_handle
    assert event.max_pdu_c_to_p == 0
    await data_path(tester, handle, 1)
    await _finish_event(peer)
    rng = Random(41008)
    for _ in range(50):
        counter = peer.counter
        payload = rng.randbytes(23)
        packet = await peer.exchange(payload, header=(counter & 1) * SN)
        assert packet.pdu[0] & (NPI | RFU | SN) == NPI and packet.pdu[1] == 0
        await _finish_event(peer)
        got = await tester.expect(
            lambda packet: isinstance(packet, hci.HCI_IsoDataPacket)
            and packet.connection_handle == handle
            and packet.packet_sequence_number == counter
        )
        assert got.packet_status_flag == 0 and got.iso_sdu_fragment == payload


async def central_forced_framing(tester, *, phy):
    """Connected Isochronous Stream using the normal CIG command, force framed.

    Initial Condition:
        A Central ACL with the Lower Tester as Peripheral.

    Test Case Configuration:
        CEN/BV-36, BV-37 and BV-38 select LE 1M, LE 2M and LE Coded.

    Test Steps:
        1. Request unframed mode with 10.884 ms SDU intervals, 32-byte SDUs,
           RTN=3, and both maximum transport latencies set to 40 ms.
        2. Require the negotiated CIS to use segmentable framing. Check
           the negotiated latency and bandwidth against the requested values.
        3. Send an HCI ISO packet and check its framed over-the-air payload.

    Reference:
        LL.TS.p28, 4.10.3.7, Table 4.10-18 and Figure 4.10-17.
    """
    link = await connect_cis_acl(tester, central=True, interval=64)
    peer, event = await _normal_parameters(
        tester, link, phy, 10884, (32, 32), 40, framed=True, prefer_unframed=True
    )
    await _check_framed_data(tester, peer, event, 10884, 40, 32)


async def _start_peripheral_cis(
    tester, link, *, phy, sizes, interval_us, nse, subinterval, offset_us, rfu=False
):
    def check_response(request, response):
        assert _integer(response, 1, 3) == _integer(response, 4, 3) == offset_us
        counter = _integer(response, 7, 2)
        assert counter == _integer(request, 34, 2)
        return offset_us, counter

    _, peer, handle = await establish_peer_cis(
        tester,
        central=False,
        link=link,
        interval_us=interval_us,
        test_nse=nse,
        sdu_sizes=sizes,
        phys=(phy, phy),
        subinterval_us=subinterval,
        offset_us=offset_us,
        request_rfu=rfu,
        choose_indication=check_response,
        exchange_first=False,
    )
    _check_initial_packet(tester, handle)
    await peer.exchange()
    complete = await tester.event(CIS_COMPLETE)
    assert (complete.status, complete.connection_handle) == (0, handle)
    assert (complete.bn_c_to_p, complete.bn_p_to_c) == tuple(
        bool(size) for size in sizes
    )
    assert (complete.max_pdu_c_to_p, complete.max_pdu_p_to_c) == sizes
    assert (complete.iso_interval, complete.nse) == (interval_us // 1250, nse)
    return peer, complete


async def _iso_received(tester, handle, counter):
    return await tester.expect(
        lambda packet: isinstance(packet, hci.HCI_IsoDataPacket)
        and packet.connection_handle == handle
        and packet.packet_sequence_number == counter
    )


async def peripheral_setup(tester, *, phy, sizes, interval_us, nse, subinterval):
    """CIS Setup Procedure, Peripheral Responding.

    Initial Condition:
        An ACL on the PHY in Table 4.10-24, with CIS Host support enabled.
        The Lower Tester is Central.

    Test Case Configuration:
        PER/BV-01 and BV-40 use LE 1M with two subevents. BV-40 has no
        Peripheral payload direction. BV-29 uses LE 2M and BV-30 LE Coded
        with one subevent, as specified by Tables 4.10-24 and 4.10-25.

    Test Steps:
        1. Enable LE CIS Request events and send the configured LL_CIS_REQ
           with all RFU fields set. Accept the requested handle over HCI.
        2. Check the response's offset window and acknowledge the indication.
           Check for successful establishment after the first CIS PDU.
        3. Set up an HCI output path and send 50 PDUs to the IUT. Verify each
           HCI ISO SDU and each ACK. In the receive-only case allow silence
           or a Null PDU with SN=0.

    Reference:
        LL.TS.p28, 4.10.4.1, Tables 4.10-24 and 4.10-25, Figure 4.10-26.
    """
    link = await connect_cis_acl(tester, interval=interval_us // 625, acl_phy=phy)
    # An LE Coded ACL exchange occupies more of the interval before the CIS.
    offset_us = 2500 if phy == Phy.LE_CODED else 1250
    peer, event = await _start_peripheral_cis(
        tester,
        link,
        phy=phy,
        sizes=sizes,
        interval_us=interval_us,
        nse=nse,
        subinterval=subinterval,
        offset_us=offset_us,
        rfu=True,
    )
    handle = event.connection_handle
    await data_path(tester, handle, 1)
    while peer.subevent:
        await peer.exchange(expect_rx=None)
    rng = Random(41024)
    for _ in range(50):
        counter = peer.counter
        parity = counter & 1
        delivery_us = peer.nominal_us + event.cis_sync_delay
        payload = rng.randbytes(sizes[0])
        packet = await peer.exchange(
            payload, header=parity * SN, expect_rx=True if sizes[1] else None
        )
        if packet is not None and sizes[1]:
            assert bool(packet.pdu[0] & NESN) != parity
        elif packet is not None:
            assert packet.pdu[0] & (NPI | RFU | SN) == NPI and packet.pdu[1] == 0
        while peer.subevent:
            reply = await peer.exchange(expect_rx=None)
            if not sizes[1] and reply is not None:
                assert reply.pdu[0] & (NPI | RFU | SN) == NPI and reply.pdu[1] == 0
        # At 7.5 ms the next ACL anchor may follow delivery by less than the
        # test harness's 1 ms HCI polling step.
        await tester.wait(max(0, delivery_us + 1 - tester.radio.now_us))
        received = await _iso_received(tester, handle, counter)
        assert received.packet_status_flag == 0 and received.iso_sdu_fragment == payload


async def peripheral_accepts_nse(tester):
    """CIS Peripheral Accepts All Supported NSE Values.

    Initial Condition:
        A Peripheral ACL with CIS Host support. The declared maximum BN
        is one, so the required Test_BN=NSE range has one round.

    Test Steps:
        1. Establish a bidirectional CIS with NSE=BN=FT=1 and 200 ms ISO
           and SDU intervals, and configure HCI input and output.
        2. Send nonempty SDUs in both directions for 20 ISO intervals.
           Compare the data at the air and HCI endpoints and require at
           least 18 matching intervals in each direction.
        3. Terminate from the Lower Tester and verify the ACK and HCI event.

    Reference:
        LL.TS.p28, 4.10.4, LL/CIS/PER/BV-39-C, Figure 4.10-27.
    """
    link = await connect_cis_acl(tester)
    peer, event = await _start_peripheral_cis(
        tester,
        link,
        phy=Phy.LE_1M,
        sizes=(251, 251),
        interval_us=200000,
        nse=1,
        subinterval=0,
        offset_us=7500,
    )
    handle = event.connection_handle
    await data_path(tester, handle, 0)
    await data_path(tester, handle, 1)
    rng = Random(41039)
    tx = rx = 0
    for _ in range(20):
        counter = peer.counter
        outbound, inbound = rng.randbytes(19), rng.randbytes(18)
        tester.send(bytes(iso_packet(handle, counter, outbound)))
        packet = await peer.exchange(
            inbound, header=(counter & 1) * (SN | NESN), expect_rx=None
        )
        if packet is not None:
            assert packet.pdu[2:] == outbound
            tx += 1
        received = await _iso_received(tester, handle, counter)
        if received.packet_status_flag == 0:
            assert received.iso_sdu_fragment == inbound
            rx += 1
    assert min(tx, rx) >= 18
    reason = hci.HCI_ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    await link.exchange(
        bytes((ControlOpcode.LL_CIS_TERMINATE_IND, 1, 1, reason)),
        llid=LlId.CONTROL,
        check_ack=True,
    )
    event = await tester.event(hci.HCI_Disconnection_Complete_Event)
    assert (event.status, event.connection_handle, event.reason) == (0, handle, reason)


async def peripheral_recommended_offset(tester):
    """CIS Setup Response Procedure, Peripheral at the recommended bound.

    Initial Condition:
        LE 1M ACL with a 10 ms connection interval and CIS Host support.

    Test Steps:
        1. Offer a fixed 5470 µs offset with NSE=BN=FT=1, 251-byte data
           in both directions, and 20 ms ISO and SDU intervals.
        2. Accept the Host request and verify that the response retains
           the exact offset. Send the indication and check its ACK.
        3. Exchange the first peer CIS packet and check establishment
           with the same CIS handle.

    Source Correction:
        Core encodes CIS_Offset in microseconds. LL_CIS_RSP is received
        by the Lower Tester on the ACL.

    Reference:
        LL.TS.p28, LL/CIS/PER/BV-48-C, Figure 4.10-44; Core Vol 6, B, 2.4.2.29.
    """
    link = await connect_cis_acl(tester, interval=8)
    await _start_peripheral_cis(
        tester,
        link,
        phy=Phy.LE_1M,
        sizes=(251, 251),
        interval_us=20000,
        nse=1,
        subinterval=0,
        offset_us=5470,
    )


PROCEDURES = ProcedureMap()

# Explicit test parameters cover LE Coded, asymmetric PHYs and LE 2M.
PROCEDURES |= procedure_cases(
    central_setup_test,
    {
        'LL/CIS/CEN/BV-25-C': (
            'CIS Setup Procedure, LE Coded',
            {
                'phys': (Phy.LE_CODED, Phy.LE_CODED),
                'sizes': (50, 50),
                'interval_us': 40000,
                'nse': 2,
            },
        ),
        'LL/CIS/CEN/BV-32-C': (
            'CIS Setup Procedure, LE 1M / LE Coded',
            {
                'phys': (Phy.LE_1M, Phy.LE_CODED),
                'sizes': (50, 0),
                'interval_us': 40000,
                'nse': 2,
            },
        ),
        'LL/CIS/CEN/BV-39-C': (
            'CIS Setup Procedure, LE 2M',
            {
                'phys': (Phy.LE_2M, Phy.LE_2M),
                'sizes': (130, 50),
                'interval_us': 20000,
                'nse': 1,
            },
        ),
    },
)

# Exercise both a framed and a receive-only CIS in each normal command case.
PROCEDURES |= procedure_cases(
    central_normal_setup,
    {
        f'LL/CIS/CEN/BV-{case:02}-C': (
            f'CIS Setup Using Non-Test Command, {phy.name}',
            {'phy': phy, 'latency_ms': latency},
        )
        for case, phy, latency in (
            (26, Phy.LE_1M, 60),
            (27, Phy.LE_2M, 60),
            (28, Phy.LE_CODED, 80),
        )
    },
)

# An SDU interval that cannot divide an ISO interval requires framed PDUs.
PROCEDURES |= procedure_cases(
    central_forced_framing,
    {
        f'LL/CIS/CEN/BV-{case:02}-C': (
            f'CIS Setup with Forced Framing, {phy.name}',
            {'phy': phy},
        )
        for case, phy in ((36, Phy.LE_1M), (37, Phy.LE_2M), (38, Phy.LE_CODED))
    },
)


# Peripheral setup checks RFU handling, wire ACK and ISO delivery on all PHYs.
PROCEDURES |= procedure_cases(
    peripheral_setup,
    {
        f'LL/CIS/PER/BV-{case:02}-C': (
            f'CIS Setup Response, {phy.name}',
            {
                'phy': phy,
                'sizes': sizes,
                'interval_us': interval,
                'nse': nse,
                'subinterval': subinterval,
            },
        )
        for case, phy, sizes, interval, nse, subinterval in (
            (1, Phy.LE_1M, (160, 160), 7500, 2, 3100),
            (29, Phy.LE_2M, (128, 128), 10000, 1, 0),
            (30, Phy.LE_CODED, (64, 64), 30000, 1, 0),
            (40, Phy.LE_1M, (160, 0), 7500, 2, 2500),
        )
    },
)

# Exercise the declared NSE range and the specified offset at the boundary.
PROCEDURES |= procedure_cases(
    peripheral_accepts_nse,
    {'LL/CIS/PER/BV-39-C': ('CIS Peripheral Accepts Supported NSE', {})},
)
PROCEDURES |= procedure_cases(
    peripheral_recommended_offset,
    {'LL/CIS/PER/BV-48-C': ('CIS Setup Response near the Offset Limit', {})},
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES, radio_cases=PROCEDURES, radio=True),
)
async def test_cis_setup(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)


@pytest.mark.parametrize('c_to_p,p_to_c', [(0x81, 0x01), (0x01, 0x84)])
async def test_cis_rejects_phy_with_supported_and_reserved_bits(
    air_dut, c_to_p, p_to_c
):
    """Reject a PHY field with multiple bits set (Core Vol 6, Part B, 5.1.15)."""
    link = await connect_cis_acl(air_dut)
    request = cis_request(
        (link.counter + 6) & 0xFFFF,
        c_to_p=c_to_p,
        p_to_c=p_to_c,
        nse=1,
        subinterval_us=0,
    )
    reject = ControlOpcode.LL_REJECT_EXT_IND
    response = await link.control(reject, request)
    assert response[:2] == bytes((reject, ControlOpcode.LL_CIS_REQ))
    assert air_dut.take(lambda e: isinstance(e, hci.HCI_LE_CIS_Request_Event)) is None
