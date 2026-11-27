# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.10: CIS timing and maintenance."""

import struct
import warnings
from dataclasses import replace
from random import Random

import pytest

from bscontroller import hci
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import ControlOpcode, LlId
from bscontroller.radio.base import Phy
from tests.qualification.cases import collect_cases, procedure_cases, verify_case
from tests.qualification.cis import (
    CIS_IND,
    CIS_REQ,
    CIS_RSP,
    CIS_TERMINATE_IND,
    PEER_CIS_FEATURES,
    cis_request,
    connect_cis_acl,
    establish_configured_cis,
    establish_peer_cis,
)
from tests.qualification.iso import channels, data_path, iso_packet
from tests.qualification.peer import exchange_feature_page
from tests.qualification.tester import LinkParameters

ESTABLISHED = (hci.HCI_LE_CIS_Established_Event, hci.HCI_LE_CIS_Established_V2_Event)
CIS_CONTROL = (CIS_REQ, CIS_RSP, CIS_IND, CIS_TERMINATE_IND)
CHANNEL_MAP = bytes.fromhex('4992244912')


def event_for_handle(event, kind, handle):
    return isinstance(event, kind) and event.connection_handle == handle


async def keep_acl(link, until_us):
    while link.anchor + 4000 < until_us:
        await link.exchange()
    await link.tester.wait(max(0, until_us - link.tester.radio.now_us))
    while link.anchor <= link.tester.radio.now_us:
        link.anchor += link.parameters.interval * 1250
        link.counter = (link.counter + 1) & 0xFFFF


async def receive_established(tester, handle, status=ErrorCode.SUCCESS):
    event = await tester.expect(
        lambda event: event_for_handle(event, ESTABLISHED, handle)
    )
    assert event.status == status


async def stop_cis(tester, link, handle):
    reason = ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    await link.exchange(bytes((CIS_TERMINATE_IND, 1, 1, reason)), llid=LlId.CONTROL)
    await link.exchange()
    event = await tester.expect(
        lambda event: event_for_handle(
            event, hci.HCI_Disconnection_Complete_Event, handle
        )
    )
    assert (event.status, event.reason) == (ErrorCode.SUCCESS, reason)


async def establishment_loss(tester, *, central):
    """CIS considered lost before establishment.

    Initial Condition:
        The IUT and Lower Tester share an ACL. Use the default CIG parameters
        for the role selected by the test case.

    Test Steps:
        1. Perform the setup procedure through LL_CIS_IND on the ACL.
        2. Keep the ACL active and withhold every CIS PDU. Expect a failed
           LE CIS Established event at the six-interval supervision deadline.
        3. Check the ACL for stale CIS control PDUs and establish a CIS again
           using the same CIG and CIS IDs. Central reuses the same CIS handle.
        4. Exchange a data PDU with the newly established stream.

    Reference:
        LL.TS.p28, 4.10.3.6 (CEN BV-33), 4.10.4.3 (PER BV-25),
        and Core Vol 6, Part B, 4.5.12.
    """
    link = await connect_cis_acl(tester, central=central)
    _, peer, handle = await establish_peer_cis(
        tester, central=central, link=link, exchange_first=False
    )
    expiry = peer.anchor_us + 6 * peer.interval_us

    def no_stale_setup(packet):
        assert packet.pdu[0] & 3 != LlId.CONTROL or packet.pdu[2] not in CIS_CONTROL

    link.on_receive = no_stale_setup
    await keep_acl(link, expiry - 1)
    assert not any(
        event_for_handle(event, ESTABLISHED, handle) for event in tester.events
    )
    await keep_acl(link, expiry + 2 * link.parameters.interval * 1250)
    await receive_established(
        tester, handle, ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR
    )
    await link.exchange()
    link.on_receive = None
    if central:
        _, peer, event = await establish_configured_cis(tester, link, handle)
        assert event.connection_handle == handle
    else:
        _, peer, event = await establish_peer_cis(tester, central=False, link=link)
        handle = event.connection_handle
    await data_path(tester, handle, 1)
    payload = b'new CIS'
    header = (peer.counter & 1) * 12
    await peer.exchange(payload, header=header)
    received = await tester.expect(
        lambda event: event_for_handle(event, hci.HCI_IsoDataPacket, handle)
    )
    assert (received.packet_status_flag, received.iso_sdu_fragment) == (0, payload)


async def active_loss(tester, *, central):
    """CIS Connection Loss.

    Initial Condition:
        An established CIS uses the default Test CIG parameters.

    Test Steps:
        1. Stop all CIS transmissions to the IUT while maintaining the ACL.
        2. Check that the CIS stays up until the supervision deadline and the
           IUT reports disconnection of its CIS handle with reason 0x08.

    Reference:
        LL.TS.p28, 4.10.5.9, Table 4.10-59 and Figure 4.10-56.
    """
    link = await connect_cis_acl(tester, central=central)
    _, peer, handle = await establish_peer_cis(
        tester, central=central, link=link, test_nse=3, exchange_first=False
    )
    anchor = peer.anchor_us
    packet = await peer.exchange()
    await receive_established(tester, handle)
    # The last valid packet ends one empty PDU after its over-air start.
    last_rx = (
        packet.end_us + peer.spacing_us if central else anchor
    ) + Phy.LE_1M.airtime_us(2)
    expiry = last_rx + 10000 * link.parameters.timeout
    kind = hci.HCI_Disconnection_Complete_Event
    await keep_acl(link, expiry - 1)
    assert not any(event_for_handle(event, kind, handle) for event in tester.events)
    await keep_acl(link, expiry + 2 * link.parameters.interval * 1250)
    event = await tester.expect(lambda event: event_for_handle(event, kind, handle))
    assert (event.status, event.reason) == (
        ErrorCode.SUCCESS,
        ErrorCode.CONNECTION_TIMEOUT_ERROR,
    )
    await link.exchange()


async def update_channel_map(tester, *, central):
    """Update a live CIS channel map at the ACL instant.

    Initial Condition:
        All 37 data channels are enabled. One unidirectional CIS uses BN=1
        and NSE=1, with 20 ms ISO intervals for the Central IUT and 100 ms
        intervals for the Peripheral IUT.

    Test Steps:
        1. Central sends LL_CHANNEL_MAP_IND after the Host changes channel
           classification. Peripheral receives that indication from the peer.
        2. Apply the indicated map with the Lower Tester at the wire instant.
           Read out the new map on the Peripheral Host.
        3. Verify data and acknowledgments on the selected CIS channels for
           25 intervals with a Central IUT or 50 with a Peripheral IUT.

    Reference:
        LL.TS.p28, 4.10.3.2, Table 4.10-3 (CEN BV-40), and
        4.10.4.2, Table 4.10-26 (PER BV-03).
    """
    link = await connect_cis_acl(tester, central=central)
    _, peer, event = await establish_peer_cis(
        tester,
        central=central,
        link=link,
        interval_us=20000 if central else 100000,
        test_nse=1,
        sdu_sizes=(251, 0),
    )
    handle = event.connection_handle
    await data_path(tester, handle, 0 if central else 1)
    assert link.parameters.channel_map == (1 << 37) - 1
    if central:
        await tester.command(
            hci.HCI_LE_Set_Host_Channel_Classification_Command(CHANNEL_MAP)
        )
        update = await link.control(ControlOpcode.LL_CHANNEL_MAP_IND)
        assert update[1:6] == CHANNEL_MAP
    else:
        count = (link.counter + 6) & 0xFFFF
        update = (
            bytes([ControlOpcode.LL_CHANNEL_MAP_IND])
            + CHANNEL_MAP
            + struct.pack('<H', count)
        )
        await link.exchange(update, llid=LlId.CONTROL, check_ack=True)
    await link.apply_update(update)
    if not central:
        result = await tester.command(hci.HCI_LE_Read_Channel_Map_Command(link.handle))
        assert result.channel_map == CHANNEL_MAP
    peer.skip_elapsed()
    random = Random(41003)
    for _ in range(25 if central else 50):
        sequence = peer.counter & 1
        data = random.randbytes(24)
        if central:
            tester.send(bytes(iso_packet(handle, peer.counter, data)))
            observed = await peer.exchange(header=0x40 | ((sequence ^ 1) << 2))
            assert observed.pdu[2:] == data
        else:
            observed = await peer.exchange(data, header=sequence << 3)
            assert (observed.pdu[0] & 4) >> 2 == sequence ^ 1


def read_window(payload):
    minimum = int.from_bytes(payload[-8:-5], 'little')
    maximum = int.from_bytes(payload[-5:-2], 'little')
    count = int.from_bytes(payload[-2:], 'little')
    return minimum, maximum, count


def write_window(minimum, maximum, count):
    return (
        minimum.to_bytes(3, 'little')
        + maximum.to_bytes(3, 'little')
        + struct.pack('<H', count & 0xFFFF)
    )


def check_equivalent(offered, selected, acl_us, iso_us):
    lo, hi, count = offered
    start, end, instant = selected
    assert 500 <= start <= end < acl_us
    phase = ((instant - count) * acl_us + start - lo) % iso_us
    assert phase + end - start <= hi - lo


async def central_offsets(tester):
    """Central CIS Setup Procedure, CIS Offset.

    Initial Condition:
        The Central IUT uses a 100 ms ACL and the default Test CIG parameters.

    Test Steps:
        1. Create a CIS in each of the eight response-window configurations.
        2. Check that the indicated anchor fits the equivalent response window.
        3. Acknowledge an empty ISO PDU and verify establishment, then terminate
           the CIS and remove its CIG before the next round.

    Reference:
        LL.TS.p28, CEN BV-50, Table 4.10-21.
    """
    link = await connect_cis_acl(tester, central=True, interval=80)
    for count_delta, shift, trim_min, trim_max in (
        (2, 0, 0, 0),
        (0, 2, 0, 0),
        (0, 1, 0, 0),
        (0, -1, 0, 0),
        (0, 0, 1, -1),
        (-2, 0, 1, -1),
        (0, 1, 1, -1),
        (0, -1, -1, -1),
    ):
        offered = None

        def choose_response(request):
            nonlocal offered
            minimum, maximum, count = read_window(request)
            iso_us = int.from_bytes(request[26:28], 'little') * 1250
            assert iso_us == 20000
            quarter = (maximum - minimum) // 4
            start = max(500, minimum + shift * iso_us + trim_min * quarter)
            end = min(99999, maximum + shift * iso_us + trim_max * quarter)
            phase = (start - minimum) % iso_us
            # If a nominal endpoint lies outside the valid range, trim it to
            # the first equivalent start and last valid end as required in Step 4.
            if phase + end - start > maximum - minimum:
                end = start + maximum - minimum - phase
            offered = start, end, (count + count_delta) & 0xFFFF
            check_equivalent((minimum, maximum, count), offered, 100000, iso_us)
            return write_window(*offered)

        _, peer, handle = await establish_peer_cis(
            tester,
            central=True,
            link=link,
            test_nse=3,
            choose_response=choose_response,
            exchange_first=False,
        )
        minimum, maximum, count = offered
        difference = (count - link.counter + 0x8000) % 0x10000 - 0x8000
        lower = link.anchor + difference * 100000 + minimum
        assert (peer.anchor_us - lower) % peer.interval_us <= maximum - minimum
        packet = await peer.exchange()
        assert packet.pdu[1] == 0
        await receive_established(tester, handle)
        peer.skip_elapsed()
        await stop_cis(tester, link, handle)
        await tester.command(hci.HCI_LE_Remove_CIG_Command(1))


async def peripheral_offsets(tester):
    """Peripheral CIS Setup Procedure, CIS Offset.

    Initial Condition:
        The Peripheral IUT uses a 100 ms ACL. The Lower Tester proposes a
        20 ms ISO interval and a CIS_Offset_Min of 500 us.

    Test Steps:
        1. Validate the IUT's accepted anchor window on LL_CIS_RSP.
        2. Select the first anchor using each of the five LL_CIS_IND offsets.
        3. Send an empty ISO PDU and verify the radio reply and HCI completion.
           Terminate the CIS before trying the next offset on the same ACL.

    Source Correction:
        Use LL_CIS_TERMINATE_IND to terminate the CIS in Step 11. In round 4,
        advance a negative offset by whole ISO intervals to encode its
        equivalent nonnegative phase.

    Reference:
        LL.TS.p28, PER BV-38, Table 4.10-37.
    """
    link = await connect_cis_acl(tester, interval=80)
    for count_delta, shift, trim in (
        (2, 0, False),
        (0, 2, False),
        (-2, 1, False),
        (1, -1, False),
        (-2, 0, True),
    ):

        def choose_indication(request, response):
            minimum, maximum, count = read_window(response)
            check_equivalent(
                read_window(request), (minimum, maximum, count), 100000, 20000
            )
            offset = minimum + shift * 20000 + (maximum - minimum) // 4 * trim
            while offset < 500:
                offset += 20000
            count = (count + count_delta) & 0xFFFF
            check_equivalent(
                read_window(response), (offset, offset, count), 100000, 20000
            )
            return offset, count

        _, peer, handle = await establish_peer_cis(
            tester,
            central=False,
            link=link,
            test_nse=3,
            offset_us=500,
            offset_max_us=85500,
            choose_indication=choose_indication,
            exchange_first=False,
        )
        packet = await peer.exchange(header=0)
        assert packet.pdu[0] & 4
        await receive_established(tester, handle)
        peer.skip_elapsed()
        await stop_cis(tester, link, handle)


async def radio_subevent(peer, data=b'', *, header=0x40, shift_us=0):
    """Exchange once without advancing the ACL or retrying a missing reply."""
    link = peer.link
    tester = link.tester
    nominal = peer.nominal_us + shift_us
    channel = channels(
        peer.counter, peer.access_address, link.parameters.channel_map, peer.nse
    )[peer.subevent]
    kwargs = {
        'access_address': peer.access_address,
        'crc_init': link.parameters.crc_init,
        'phy': Phy.LE_1M,
    }
    packet = None
    pdu = bytes((header, len(data))) + data
    if link.central:
        if tester.radio.now_us + 1 < nominal:
            packet = await tester.receive(
                channel,
                start_us=max(tester.radio.now_us + 1, nominal - 10),
                duration_us=nominal - tester.radio.now_us + 200,
                **kwargs,
            )
            if packet is not None:
                assert abs(packet.start_us - nominal) <= 2
                await tester.transmit(
                    channel, pdu, start_us=packet.end_us + peer.spacing_us, **kwargs
                )
    elif tester.radio.now_us + 1 < nominal:
        end = await tester.transmit(channel, pdu, start_us=nominal, **kwargs)
        packet = await tester.receive(
            channel, duration_us=peer.spacing_us + 200, **kwargs
        )
        if packet is not None:
            assert abs(packet.start_us - end - peer.spacing_us) <= 2
    if packet is not None:
        assert packet.valid_crc
        assert packet.pdu[1] == len(packet.pdu) - 2
    peer.step()
    return packet


async def receive_iso(tester, handle, sequence, payload):
    received = await tester.expect(
        lambda event: event_for_handle(event, hci.HCI_IsoDataPacket, handle)
        and event.packet_sequence_number == sequence
    )
    assert (received.packet_status_flag, received.iso_sdu_fragment) == (0, payload)


async def cis_window_widening(tester):
    """Receive a CIS payload at both edges of the widened sleep-clock window.

    Initial Condition:
        The Lower Tester uses a 500 ppm sleep clock and a bidirectional CIS
        with a 100 ms ISO interval, NSE=1 and one-byte C-to-P SDUs.

    Test Steps:
        1. Send ten one-byte ISO SDUs and check the IUT's empty reply and
           HCI payload for each SDU.
        2. Suppress both ACL and CIS traffic for ten ISO intervals.
        3. Send one SDU at the early window boundary calculated from the last
           valid CIS packet, then repeat the sequence at the late boundary.

    Reference:
        LL.TS.p28, PER BV-45, Table 4.10-39 and Figure 4.10-43.
    """
    # SCA field zero represents 251–500 ppm. Leaving the high bits clear
    # advertises the maximum window widening, with the usual hop value 7.
    link = await connect_cis_acl(
        tester,
        connection_parameters=LinkParameters(
            timeout=400,
            interval=32,
            hop_sca=7,
        ),
    )
    _, peer, event = await establish_peer_cis(
        tester,
        central=False,
        link=link,
        interval_us=100000,
        test_nse=1,
        sdu_sizes=(1, 251),
    )
    handle = event.connection_handle
    await data_path(tester, handle, 0)
    await data_path(tester, handle, 1)

    async def check_sdu(index, shift=0):
        data = bytes([index])
        sequence = peer.counter
        header = (sequence & 1) << 3
        if shift:
            reply = await radio_subevent(peer, data, header=header, shift_us=shift)
        else:
            reply = await peer.exchange(data, header=header)
        assert reply is not None
        assert reply.pdu[0] & 0x48 == 0x40 and reply.pdu[1] == 0
        await receive_iso(tester, handle, sequence, data)

    for direction in (-1, 1):
        for index in range(10):
            await check_sdu(index)
        last_start = peer.anchor_us - peer.interval_us
        for _ in range(10):
            peer.step()
        # No radio operation may fill either ACL or CIS slots while the peer
        # waits for the widened window. Resume the ACL clock after reception.
        elapsed = peer.anchor_us - last_start
        shift = direction * (elapsed * 500 // 1_000_000 + 16)
        await check_sdu(10, shift)
        # Both streams use the Lower Tester's Central clock (Core 4.2.4,
        # Table 4.1). Resume ACL and CIS at the same observed clock offset.
        link.anchor += shift
        peer.anchor_us += shift
        peer.skip_elapsed()


def acl_update(interval, timeout, count, *, offset=0):
    return struct.pack(
        '<BBHHHHH',
        ControlOpcode.LL_CONNECTION_UPDATE_IND,
        1,
        offset,
        interval,
        0,
        timeout,
        (count + 6) & 0xFFFF,
    )


def apply_acl_update(link, update):
    size, offset, interval, latency, timeout, instant = struct.unpack(
        '<BHHHHH', update[1:]
    )
    assert link.counter == instant
    link.anchor += offset * 1250
    link.parameters = replace(
        link.parameters,
        window_size=size,
        window_offset=offset,
        interval=interval,
        latency=latency,
        timeout=timeout,
    )
    return size * 1250 if link.central and offset else 0


async def maintain_cis_during_update(tester, *, central):
    """Keep separate ACL and CIS event schedules across six interval changes.

    Initial Condition:
        The ACL interval and default bidirectional ISO interval are 20 ms.
        The Central IUT uses NSE=1 and Peripheral IUT uses the default NSE.

    Test Steps:
        1. Update the ACL interval to 80, 50, 10, 67.5, 140, and 60 ms.
        2. Follow each LL_CONNECTION_UPDATE_IND instant on the ACL and check
           the HCI Connection Update Complete values.
        3. Observe both links for one second after each instant. At least
           50% of ACL and CIS events occur, and each of the first two ACL
           intervals carries every CIS event unless the radio slots overlap.

    Reference:
        LL.TS.p28, CEN BV-63, PER BV-54, Table 4.10-23.
    """
    link = await connect_cis_acl(tester, central=central, interval=16)
    _, peer, _ = await establish_peer_cis(
        tester,
        central=central,
        link=link,
        test_nse=1 if central else 3,
    )
    peer.skip_elapsed()
    timeout = link.parameters.timeout
    update_kind = hci.HCI_LE_Connection_Update_Complete_Event

    async def acl_event(*, payload=b'', window=0):
        # Consume ISO activity before an ACL event without changing its phase.
        while peer.nominal_us + 400 < link.anchor:
            await radio_subevent(peer)
        return await link.exchange(
            payload,
            llid=LlId.CONTROL if payload else LlId.CONTINUATION,
            required=False,
            window_us=window,
        )

    for interval in (64, 40, 8, 54, 112, 48):
        if central:
            await tester.command(
                hci.HCI_LE_Connection_Update_Command(
                    link.handle,
                    interval,
                    interval,
                    0,
                    timeout,
                    0,
                    0,
                )
            )
            for _ in range(16):
                packet = await acl_event()
                if (
                    packet
                    and packet.pdu[0] & 3 == LlId.CONTROL
                    and packet.pdu[2] == ControlOpcode.LL_CONNECTION_UPDATE_IND
                ):
                    update = packet.pdu[2:]
                    break
            else:
                pytest.fail('missing LL_CONNECTION_UPDATE_IND')
            assert int.from_bytes(update[4:6], 'little') == interval
        else:
            update = acl_update(interval, timeout, link.counter)
            for _ in range(3):
                sequence = link.sn
                packet = await acl_event(payload=update)
                if packet is not None and link.sn != sequence:
                    break
            else:
                pytest.fail('LL_CONNECTION_UPDATE_IND not acknowledged')
        instant = int.from_bytes(update[-2:], 'little')
        while link.counter != instant:
            await acl_event()
        window = apply_acl_update(link, update)
        first_acl = link.anchor
        interval_us = interval * 1250
        end = first_acl + 1_000_000
        first_two = first_acl + 2 * interval_us
        n_acl = received_acl = 0
        iso_received = {}
        iso_collided = set()
        while min(link.anchor, peer.nominal_us) < end:
            if peer.nominal_us + 400 < link.anchor:
                anchor = peer.anchor_us
                if anchor not in iso_received and anchor >= first_acl:
                    iso_received[anchor] = False
                if await radio_subevent(peer) is not None and anchor >= first_acl:
                    iso_received[anchor] = True
            else:
                while peer.nominal_us < link.anchor + 400:
                    iso_collided.add(peer.anchor_us)
                    if peer.anchor_us >= first_acl:
                        iso_received.setdefault(peer.anchor_us, False)
                    peer.step()
                n_acl += 1
                received_acl += await acl_event(window=window) is not None
                window = 0
        event = await tester.expect(
            lambda event: event_for_handle(event, update_kind, link.handle)
        )
        assert (event.status, event.connection_interval) == (
            ErrorCode.SUCCESS,
            interval,
        )
        assert received_acl * 2 >= n_acl > 0
        assert sum(iso_received.values()) * 2 >= len(iso_received) > 0
        assert all(
            seen or anchor in iso_collided
            for anchor, seen in iso_received.items()
            if anchor < first_two
        )
        assert not any(
            isinstance(event, hci.HCI_Disconnection_Complete_Event)
            for event in tester.events
        )
        peer.skip_elapsed()


async def acl_between_subevents(tester):
    """Move the ACL anchor between two CIS subevents without interrupting either.

    Initial Condition:
        The Peripheral IUT uses a 20 ms ACL and ISO interval, NSE=2, BN=1,
        FT=1, 10 ms subinterval and 5 ms CIS offset.

    Test Steps:
        1. Exchange 100 one-byte payloads on both the ACL and CIS. Do not ACK
           IUT traffic during the observation rounds.
        2. Move the ACL anchor 10 ms at an instant six connection events ahead.
        3. Exchange another 100 payloads with each ACL exchange between the
           two CIS subevents. Check the ACL payloads and first-transmission
           ACKs, HCI ISO delivery and radio data in each CIS subevent.

    Reference:
        LL.TS.p28, PER BV-49, Figure 4.10-45.
    """
    link = await connect_cis_acl(tester, interval=16)
    _, peer, event = await establish_peer_cis(
        tester,
        central=False,
        link=link,
        test_nse=2,
        subinterval_us=10000,
        offset_us=5000,
    )
    await peer.exchange()
    handle = event.connection_handle
    await data_path(tester, handle, 0)
    await data_path(tester, handle, 1)
    random = Random(41049)
    peer.skip_elapsed()

    async def activity(*, acl_after_first):
        inbound_acl = acl_acks = 0
        expected_iso = {}
        cis_transmissions = [0, 0]
        for _ in range(100):
            outbound_acl, incoming_acl, outbound_iso, incoming_iso = (
                random.randbytes(1) for _ in range(4)
            )
            tester.send(
                bytes(
                    hci.HCI_AclDataPacket(
                        link.handle,
                        hci.HCI_ACL_PB_FIRST_NON_FLUSHABLE,
                        0,
                        1,
                        outbound_acl,
                    )
                )
            )
            tester.send(bytes(iso_packet(handle, peer.counter, outbound_iso)))
            expected_iso[peer.counter] = incoming_iso
            header = (peer.counter & 1) * 12

            async def acl():
                nonlocal inbound_acl, acl_acks
                assert abs(link.anchor - peer.anchor_us) == 5000
                sn, nesn = link.sn, link.nesn
                observed = await link.exchange(incoming_acl, llid=LlId.START)
                acl_acks += int(bool(observed.pdu[0] & 4) != sn)
                link.nesn = nesn
                received = tester.take(
                    lambda event: event_for_handle(
                        event, hci.HCI_AclDataPacket, link.handle
                    )
                )
                if received is not None:
                    assert received.data == incoming_acl
                    inbound_acl += 1

            async def cis(index):
                assert peer.subevent == index
                observed = await radio_subevent(peer, incoming_iso, header=header)
                cis_transmissions[index] += (
                    observed is not None and observed.pdu[2:] == outbound_iso
                )

            if not acl_after_first:
                await acl()
            await cis(0)
            if acl_after_first:
                await acl()
            await cis(1)
        target = min(link.anchor, peer.nominal_us) - 1
        await tester.wait(target - tester.radio.now_us)
        inbound_iso = 0
        while received := tester.take(
            lambda event: event_for_handle(event, hci.HCI_IsoDataPacket, handle)
        ):
            payload = expected_iso.pop(received.packet_sequence_number, None)
            if received.packet_status_flag == 0:
                assert received.iso_sdu_fragment == payload
                inbound_iso += 1
        assert inbound_acl == 100
        if acl_acks < 90:
            # The test suite classifies this threshold as a warning.
            warnings.warn(
                f'{acl_acks}/100 ACL first transmissions acknowledged', stacklevel=2
            )
        assert inbound_iso >= 95
        assert min(cis_transmissions) >= 95

    await activity(acl_after_first=False)
    update = acl_update(
        link.parameters.interval, link.parameters.timeout, link.counter, offset=8
    )
    await link.exchange(update, llid=LlId.CONTROL, check_ack=True)
    instant = int.from_bytes(update[-2:], 'little')
    while link.counter != instant:
        while peer.nominal_us < link.anchor - 400:
            await radio_subevent(peer)
        await link.exchange()
    apply_acl_update(link, update)
    while peer.nominal_us < link.anchor - 5000:
        await radio_subevent(peer)
    # The update changes the ACL phase only. The upcoming CIS begins before
    # the first ACL packet at the new instant.
    assert peer.nominal_us == link.anchor - 5000
    # Ignore HCI reports for deliberately empty transition intervals.
    while (
        tester.take(
            lambda event: event_for_handle(event, hci.HCI_IsoDataPacket, handle)
        )
        is not None
    ):
        pass
    await activity(acl_after_first=True)
    assert not any(
        isinstance(event, hci.HCI_Disconnection_Complete_Event)
        for event in tester.events
    )


PROCEDURES = procedure_cases(
    establishment_loss,
    {
        'LL/CIS/CEN/BV-33-C': (
            'CIS lost before establishment, Central',
            {'central': True},
        ),
        'LL/CIS/PER/BV-25-C': (
            'CIS lost before establishment, Peripheral',
            {'central': False},
        ),
    },
)

# CIS supervision uses the ACL timeout after the first valid peer PDU.
PROCEDURES |= procedure_cases(
    active_loss,
    {
        'LL/CIS/CEN/BV-34-C': ('CIS connection loss, Central', {'central': True}),
        'LL/CIS/PER/BV-26-C': ('CIS connection loss, Peripheral', {'central': False}),
    },
)

# The CIS uses the parent ACL's channel map at its update instant.
PROCEDURES |= procedure_cases(
    update_channel_map,
    {
        'LL/CIS/CEN/BV-40-C': ('CIS channel map update, Central', {'central': True}),
        'LL/CIS/PER/BV-03-C': (
            'CIS channel map update, Peripheral',
            {'central': False},
        ),
    },
)

# Offset exchanges can name a different ACL event or ISO-period equivalent.
PROCEDURES |= procedure_cases(
    central_offsets,
    {'LL/CIS/CEN/BV-50-C': ('Equivalent Central CIS anchor windows', {})},
)
PROCEDURES |= procedure_cases(
    peripheral_offsets,
    {'LL/CIS/PER/BV-38-C': ('Equivalent Peripheral CIS anchors', {})},
)


# The Lower Tester exercises the Peripheral's sleep-clock receive window.
PROCEDURES |= procedure_cases(
    cis_window_widening, {'LL/CIS/PER/BV-45-C': ('CIS window widening', {})}
)

# The CIS phase and radio traffic survive the ACL interval change.
PROCEDURES |= procedure_cases(
    maintain_cis_during_update,
    {
        'LL/CIS/CEN/BV-63-C': (
            'Central ACL interval update with active CIS',
            {'central': True},
        ),
        'LL/CIS/PER/BV-54-C': (
            'Peripheral ACL interval update with active CIS',
            {'central': False},
        ),
    },
)


# The Peripheral maintains both links when the ACL moves between subevents.
PROCEDURES |= procedure_cases(
    acl_between_subevents, {'LL/CIS/PER/BV-49-C': ('ACL between CIS subevents', {})}
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks', collect_cases(PROCEDURES)
)
async def test_cis_timing(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)


@pytest.mark.asyncio
async def test_central_cis_requires_supervision_above_two_iso_intervals(air_dut):
    """Reject the equality boundary before sending LL_CIS_REQ (Core 6.B.4.5.13.2)."""
    link = await connect_cis_acl(air_dut, central=True, timeout=10)
    config = hci.HCI_LE_Set_CIG_Parameters_Test_Command(
        1,
        50_000,
        50_000,
        1,
        1,
        40,
        0,
        0,
        0,
        [1],
        [1],
        [50],
        [50],
        [50],
        [50],
        [1],
        [1],
        [1],
        [1],
    )
    (handle,) = (await air_dut.command(config)).connection_handle
    await air_dut.command(hci.HCI_LE_Create_CIS_Command([handle], [link.handle]))
    await link.exchange()
    await receive_established(
        air_dut, handle, ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR
    )
    packet = await link.exchange()
    assert packet.pdu[0] & 3 != LlId.CONTROL or packet.pdu[2] != CIS_REQ


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'timeout,iso_us,nse,subinterval_us',
    [
        pytest.param(10, 50_000, 1, 0, id='supervision-boundary'),
        pytest.param(100, 20_000, 3, 7500, id='overlapping-subevents'),
    ],
)
async def test_peripheral_rejects_invalid_cis_timing(
    air_dut, timeout, iso_us, nse, subinterval_us
):
    """Reject invalid CIS timing before notifying the Host (Core 6.B.4.5.13.2)."""
    link = await connect_cis_acl(air_dut, timeout=timeout)
    request = cis_request(
        link.counter + 6,
        interval_us=iso_us,
        nse=nse,
        subinterval_us=subinterval_us,
        offset_us=5000,
    )
    assert await link.control(ControlOpcode.LL_REJECT_EXT_IND, request) == bytes(
        (
            ControlOpcode.LL_REJECT_EXT_IND,
            CIS_REQ,
            ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR,
        )
    )
    assert not any(
        isinstance(event, hci.HCI_LE_CIS_Request_Event) for event in air_dut.events
    )


@pytest.mark.asyncio
async def test_acl_host_updates_preserve_cis_supervision(air_dut):
    """Reject ACL timeout reductions at the live CIS bound (Core 6.B.4.5.2)."""
    for bit in (
        hci.LeFeature.CONNECTION_SUBRATING_HOST_SUPPORT,
        hci.LeFeature.SHORTER_CONNECTION_INTERVALS_HOST_SUPPORT,
    ):
        await air_dut.command(hci.HCI_LE_Set_Host_Feature_V2_Command(bit, 1))
    link = await connect_cis_acl(
        air_dut,
        central=True,
        timeout=20,
        features=(
            PEER_CIS_FEATURES
            | hci.LeFeatureMask.CONNECTION_SUBRATING
            | hci.LeFeatureMask.CONNECTION_SUBRATING_HOST_SUPPORT
            | hci.LeFeatureMask.LL_EXTENDED_FEATURE_SET
        ),
    )
    await exchange_feature_page(
        link,
        int(
            hci.LeFeatureMask.SHORTER_CONNECTION_INTERVALS
            | hci.LeFeatureMask.SHORTER_CONNECTION_INTERVALS_HOST_SUPPORT
        )
        >> 64,
    )
    _, peer, _ = await establish_peer_cis(
        air_dut, central=True, link=link, interval_us=50_000, test_nse=1
    )
    handle = link.handle
    for command in (
        hci.HCI_LE_Connection_Update_Command(handle, 32, 32, 0, 10, 0, 0),
        hci.HCI_LE_Subrate_Request_Command(handle, 1, 1, 0, 0, 10),
        hci.HCI_LE_Connection_Rate_Request_Command(
            handle, 320, 320, 1, 1, 0, 0, 10, 0, 0
        ),
    ):
        await air_dut.command(command, ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR)
    await peer.exchange()
