# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.3: Connection establishment and initiation."""

import random
from itertools import pairwise

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_ADV_DELAY_MAX_US,
    LL_ADV_DIRECT_HIGH_DUTY_TIMEOUT_US,
    LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
    LL_ADV_PRIMARY_CHANNELS,
    LL_CONN_AUX_WINDOW_DELAY_CODED_US,
    LL_CONN_AUX_WINDOW_DELAY_UNCODED_US,
    LL_CONN_ESTABLISHMENT_EVENTS,
    LL_CONN_INTERVAL_UNIT_US,
    LL_IFS_US,
    LL_MAFS_US,
    LL_SCA_PPM,
    LL_SUPERVISION_TIMEOUT_MIN,
    LL_SUPERVISION_TIMEOUT_UNIT_US,
    PDU_ACCESS_ADDRESS_SIZE,
    PDU_ADV_ADDRESS_SIZE,
    PDU_ADV_ADDRESSES_END,
    PDU_ADV_AUX_OFFSET_UNITS_MASK,
    PDU_ADV_CHSEL_MASK,
    PDU_ADV_CONNECT_SCA_SHIFT,
    PDU_ADV_EXT_HEADER_LENGTH_BITS,
    PDU_ADV_OFFSET_BITS,
    PDU_ADV_OFFSET_UNIT_30_US,
    PDU_ADV_OFFSET_UNIT_300_US,
    PDU_ADV_RXADD_MASK,
    PDU_ADV_TXADD_MASK,
    PDU_ADV_TYPE_MASK,
    PDU_DATA_LLID_MASK,
    PDU_DATA_NESN_MASK,
    PDU_HEADER_SIZE,
    AdvertisingMode,
    AdvertisingPduType,
    LlId,
)
from bscontroller.linklayer.timing import LL_CLOCK_JITTER_US, LL_LOCAL_SCA_PPM
from bscontroller.radio.base import PHY_OCTET_BITS, PHY_TIME_UNITS_PER_SECOND, Phy
from tests.qualification import privacy as privacy_peer
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.connection import (
    ADV_FILTER_ACCEPT_LIST_ONLY,
    ADV_INTERVAL_US,
    ADVERTISING_OBSERVATION_US,
    ARQ_PAYLOAD,
    CONNECT_IFS_TOLERANCE_US,
    CONNECTION_HANDLE_MAX,
    CONNECTION_INTERVAL,
    HCI_OBSERVATION_STEP_US,
    INITIATING_SCAN_INTERVAL,
    LEGACY_CONNECTION_EVENT_MASK,
    MAINTAINED_EVENTS,
    PARALLEL_ADVERTISING_DATA,
    PEER_EXCHANGE_BUDGET_US,
    SUPERVISION_TIMEOUT,
    ErrorCode,
    _advertise_connection,
    _advertiser_connection_parameters,
    _check_central_timing,
    _check_rf_stopped,
    _completion_events,
    _connection_complete,
    _disconnected,
    _incoming_connection,
    _initiate,
    _initiate_auxiliary,
    _no_creation_events,
    _no_initiation_complete,
    _observe_advertising,
    _observe_disconnection,
    _receive_central_event,
    _restore_peer,
    _sequence_bits,
    _service_peer,
    check_duplicate_advertiser_request,
    check_duplicate_initiator_request,
)
from tests.qualification.decision import flag_vectors
from tests.qualification.link import _data_pdu, _pending_connection, _wire_event
from tests.qualification.peer import PeerLink, establish
from tests.qualification.tester import (
    DUT_ADDRESS,
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_DATA_ACCESS_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
    PEER_ADDRESS,
    LinkParameters,
    adv_pdu,
    aux_pointer,
    check_access_address,
    create_connection,
    decode_aux,
    decode_extended,
    ext_pdu,
    extended_enable,
    extended_parameters,
    follow_auxiliary,
    legacy_parameters,
    request_pdu,
)

PROCEDURES = ProcedureMap()

Properties = (
    hci.HCI_LE_Set_Extended_Advertising_Parameters_Command.AdvertisingProperties
)


async def _connectable_advertisement(tester, link=None):
    """Follow an auxiliary packet in free RF gaps without starving an existing ACL."""
    deadline = tester.radio.now_us + ADVERTISING_OBSERVATION_US
    while tester.radio.now_us < deadline:
        until = (
            min(deadline, link.anchor - PEER_EXCHANGE_BUDGET_US) if link else deadline
        )
        if until <= tester.radio.now_us:
            await _service_peer([link])
            continue
        primary = await tester.receive(37, duration_us=until - tester.radio.now_us)
        if primary is None:
            continue
        assert primary.valid_crc
        assert primary.phy == Phy.LE_1M
        assert len(primary.pdu) >= PDU_HEADER_SIZE
        assert primary.pdu[1] == len(primary.pdu) - PDU_HEADER_SIZE
        assert primary.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.ADV_EXT_IND
        fields = decode_extended(primary.pdu)
        assert set(fields) == {'adi', 'aux', 'data'} and (not fields['data'])
        assert (
            primary.pdu[PDU_HEADER_SIZE] >> PDU_ADV_EXT_HEADER_LENGTH_BITS
            == AdvertisingMode.CONNECTABLE
        )
        channel, packet, auxiliary = await follow_auxiliary(tester, primary, fields)
        assert packet.phy == Phy.LE_1M
        assert packet.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.ADV_EXT_IND
        assert (
            packet.pdu[PDU_HEADER_SIZE] >> PDU_ADV_EXT_HEADER_LENGTH_BITS
            == AdvertisingMode.CONNECTABLE
        )
        assert not packet.pdu[0] & (PDU_ADV_TXADD_MASK | PDU_ADV_RXADD_MASK)
        assert auxiliary == {
            'address': bytes(DUT_ADDRESS),
            'adi': fields['adi'],
            'data': b'',
        }
        return (channel, packet)
    raise AssertionError(
        'connectable extended advertising absent while maintaining ACL'
    )


# LL/CON/ADV/BV-17..19-C Step 2: six random Decision_Data octets, flags 0x00.
DECISION_ADVERTISING_DATA = random.Random(17).randbytes(6)

# LL/CON/ADV/BV-03-C prescribes thirty NAKs followed by thirty ACK events.
LATENCY_EVENTS = 30
MINIMUM_NAK_RESPONSES = 9
PERIPHERAL_LATENCY = 5

# LL/CON/INI/BV-38-C requires at least ten events after replies resume.
RECOVERY_EVENTS = 10

# Selected IXIT: full 30-ms scan window and 20-ms base advertising interval.
ESTABLISHMENT_SCAN_INTERVAL = 48


def _accepting_timeout_bound(parameters):
    """Compute the source's conditional T2 lower bound from widened RX windows."""
    interval = parameters.interval * LL_CONN_INTERVAL_UNIT_US
    first = (1 + parameters.window_offset) * LL_CONN_INTERVAL_UNIT_US
    size = parameters.window_size * LL_CONN_INTERVAL_UNIT_US
    ppm = LL_SCA_PPM[parameters.hop_sca >> PDU_ADV_CONNECT_SCA_SHIFT]

    def window(index):
        anchor = first + index * interval
        widening = (
            LL_CLOCK_JITTER_US
            + (anchor * (ppm + LL_LOCAL_SCA_PPM) + PHY_TIME_UNITS_PER_SECOND - 1)
            // PHY_TIME_UNITS_PER_SECOND
        )
        return (anchor - widening, anchor + size + widening)

    deadline = LL_CONN_ESTABLISHMENT_EVENTS * interval
    index = 0
    while window(index)[0] < deadline:
        index += 1
    t2 = window(index - 1)[1]
    t1 = window(LL_CONN_ESTABLISHMENT_EVENTS - 1)[1]
    return t2 if t2 < t1 else 0


def _empty_data(packet, expected_sequence=None):
    """Validate received DATA independently of the DUT's ARQ implementation."""
    assert packet.valid_crc
    assert len(packet.pdu) == PDU_HEADER_SIZE and packet.pdu[1] == 0
    assert packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
    if expected_sequence is not None:
        assert _sequence_bits(packet) == expected_sequence


async def _initiating_failure(tester, *, bad_crc=False):
    """Distinguish six-event setup failure from supervision after bad-CRC reception."""
    link, end = await _pending_connection(
        tester,
        central=True,
        parameters=LinkParameters(timeout=LL_SUPERVISION_TIMEOUT_MIN),
        advertising_data=b'\xff',
    )
    _completion_events(tester, link)
    check_access_address(link.parameters.access_address)
    interval = link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
    deadline = end + (
        link.parameters.timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
        if bad_crc
        else LL_CONN_ESTABLISHMENT_EVENTS * interval
    )
    first_anchor = link.anchor
    starts = []

    def reply(packet):
        _empty_data(packet, (0, 0))
        link._accept(packet)
        return _data_pdu(link.sn, link.nesn)

    while link.anchor < deadline:
        channel = link.channel
        packet = await _receive_central_event(link)
        _empty_data(packet, (0, 0))
        starts.append(packet.start_us)
        if bad_crc:
            await tester.transmit(
                channel,
                reply(packet),
                start_us=packet.end_us + LL_IFS_US,
                bad_crc=True,
                **link.radio_parameters,
            )
            link.last_sent_end_us = tester.radio.now_us + 1
            extra = await tester.receive(
                channel, duration_us=1000, **link.radio_parameters
            )
            if extra is not None:
                assert (
                    abs(extra.start_us - link.last_sent_end_us - LL_IFS_US)
                    <= CONNECT_IFS_TOLERANCE_US
                )
                await tester.transmit(
                    channel,
                    reply(extra),
                    start_us=extra.end_us + LL_IFS_US,
                    bad_crc=True,
                    **link.radio_parameters,
                )
    _check_central_timing(link.parameters, first_anchor, starts)
    if not bad_crc:
        expected = min(
            LL_CONN_ESTABLISHMENT_EVENTS, (deadline - 1 - starts[0]) // interval + 1
        )
        assert len(starts) == expected
    event = await _observe_disconnection(
        tester, deadline - HCI_OBSERVATION_STEP_US, deadline + HCI_OBSERVATION_STEP_US
    )
    assert (event.status, event.connection_handle, event.reason) == (
        ErrorCode.SUCCESS,
        link.handle,
        (
            ErrorCode.CONNECTION_TIMEOUT_ERROR
            if bad_crc
            else ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR
        ),
    )
    for _ in range(LL_CONN_ESTABLISHMENT_EVENTS):
        await _check_rf_stopped(link)


ACCESS_ADDRESS_BIT_MINIMUM = 3

# BV-50 requires 36 distinct addresses, with both values at least three times per bit.
ACCESS_ADDRESS_ROUNDS = 36
DIRECTED_TIMING_EVENTS = 1000

# LL.TS.p28 Section4.1.5.2: alter the DUT address's high, low or both end octets.
PEER_ADDRESS_XOR_MASKS = (181419418583040, 90, 98956046500005)

# Tables4.3-12/25 require at least100 measured events; BV-37 requires1000 per CSA.
TIMING_EVENTS = 100


def _check_access_address_set(addresses):
    """Apply the BV-50 population verdict, independently of the DUT allocator."""
    assert len(addresses) == ACCESS_ADDRESS_ROUNDS, 'wrong Access Address round count'
    assert len(set(addresses)) == len(addresses), 'repeated Access Address'
    for address in addresses:
        check_access_address(address)
    for bit in range(PDU_ACCESS_ADDRESS_SIZE * PHY_OCTET_BITS):
        ones = sum(address >> bit & 1 for address in addresses)
        assert (
            ACCESS_ADDRESS_BIT_MINIMUM
            <= ones
            <= len(addresses) - ACCESS_ADDRESS_BIT_MINIMUM
        ), f'Access Address bit{bit} lacks three occurrences of each value'


def _peer_addresses():
    value = int.from_bytes(bytes(DUT_ADDRESS), 'little')
    return (
        LOWER_PERIPHERAL_PUBLIC_ADDRESS,
        *(
            hci.Address(
                (value ^ mask).to_bytes(PDU_ADV_ADDRESS_SIZE, 'little'),
                hci.AddressType.PUBLIC_DEVICE,
            )
            for mask in PEER_ADDRESS_XOR_MASKS
        ),
    )


AdvType = hci.HCI_LE_Set_Advertising_Parameters_Command.AdvertisingType

# BV-52 requires five advertisements after the outgoing connection completes.
CONTINUED_ADVERTISEMENTS = 5


async def _maintain_until(tester, links, target):
    while links and min(link.anchor for link in links) <= target:
        await _service_peer(links)
    if tester.radio.now_us < target:
        await tester.wait(target - tester.radio.now_us)


async def _outgoing_connection(tester, links):
    peer = LOWER_PERIPHERAL_PUBLIC_ADDRESS
    interval_us = INITIATING_SCAN_INTERVAL * hci.HCI_LE_SCAN_INTERVAL_UNIT_US
    deadline = tester.radio.now_us + 7 * interval_us
    generator = random.Random(0)
    next_start = tester.radio.now_us
    while tester.radio.now_us < deadline:
        await _maintain_until(tester, links, next_start)
        while (
            links
            and min(link.anchor for link in links) - tester.radio.now_us
            < PEER_EXCHANGE_BUDGET_US
        ):
            await _service_peer(links)
        start = tester.radio.now_us + 100
        end = await tester.transmit(37, adv_pdu(address=peer), start_us=start)
        request = await tester.receive(
            37, duration_us=LL_IFS_US + Phy.LE_1M.address_us + CONNECT_IFS_TOLERANCE_US
        )
        if (
            request is not None
            and request.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.CONNECT_IND
        ):
            assert request.valid_crc
            assert bytes([request.pdu[0] & ~PDU_ADV_CHSEL_MASK]) + request.pdu[
                1:
            ] == request_pdu(
                AdvertisingPduType.CONNECT_IND,
                scanner=DUT_ADDRESS,
                advertiser=peer,
                data=request.pdu[PDU_ADV_ADDRESSES_END:],
            )
            assert abs(request.start_us - end - LL_IFS_US) <= CONNECT_IFS_TOLERANCE_US
            parameters = LinkParameters.decode(request.pdu[PDU_ADV_ADDRESSES_END:])
            assert (parameters.interval, parameters.latency, parameters.timeout) == (
                CONNECTION_INTERVAL,
                0,
                SUPERVISION_TIMEOUT,
            )
            check_access_address(parameters.access_address)
            link = PeerLink(tester, parameters, request.end_us, central=True)
            await _connection_complete(tester, link, peer, legacy_only=True)
            assert all(link.handle != other.handle for other in links), (
                'connection handle reused'
            )
            return link
        next_start = (
            start + ADV_INTERVAL_US + generator.randrange(LL_ADV_DELAY_MAX_US + 1)
        )
    raise AssertionError(
        'pending initiation did not complete while another role was active'
    )


async def _parallel_connections(tester, *, incoming_first=False):
    """Keep initiation and advertising independent through both completion orders."""
    await tester.masks(le=LEGACY_CONNECTION_EVENT_MASK)
    await tester.command(
        create_connection(
            peer_address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
            peer_address_type=LOWER_PERIPHERAL_PUBLIC_ADDRESS.address_type,
        )
    )
    await tester.command(
        legacy_parameters(
            advertising_type=(
                AdvType.ADV_DIRECT_IND if incoming_first else AdvType.ADV_IND
            ),
            peer_address=LOWER_CENTRAL_PUBLIC_ADDRESS,
            peer_address_type=LOWER_CENTRAL_PUBLIC_ADDRESS.address_type,
        )
    )
    if not incoming_first:
        await tester.command(
            hci.HCI_LE_Set_Advertising_Data_Command(PARALLEL_ADVERTISING_DATA)
        )
    await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
    packet = await _observe_advertising(tester, [], directed=incoming_first)
    assert packet.pdu[0] & PDU_ADV_CHSEL_MASK
    if incoming_first:
        first = await _incoming_connection(tester, [], packet)
        second = await _outgoing_connection(tester, [first])
    else:
        first = await _outgoing_connection(tester, [])
        for _ in range(CONTINUED_ADVERTISEMENTS):
            packet = await _observe_advertising(tester, [first])
        second = await _incoming_connection(tester, [first], packet)
    links = [first, second]
    while min(link.counter for link in links) < MAINTAINED_EVENTS:
        await _service_peer(links)
        _no_creation_events(tester)


# Extended accepting-connection procedures allow at most twenty AUX requests.
AUXILIARY_SETUP_ATTEMPTS = 20

# LL.TS.p28 CSA connection procedures measure100 maintained events.
CSA_TIMING_EVENTS = 100

# Core Vol6 PartB4.4.2.4.2: maximum high-duty directed advertising event interval.
HIGH_DUTY_INTERVAL_MAX_US = 3750
OTHER_PUBLIC = hci.Address('12:34:56:78:9A:BD', hci.AddressType.PUBLIC_DEVICE)


async def _advertising_csa(tester, *, directed=False, csa2=False, extended=False):
    """Select CSA from the connection request, independently of the HCI command path."""
    peer = LOWER_CENTRAL_PUBLIC_ADDRESS
    if extended:
        properties = (
            Properties.USE_LEGACY_ADVERTISING_PDUS | Properties.CONNECTABLE_ADVERTISING
        )
        properties |= (
            Properties.DIRECTED_ADVERTISING
            | Properties.HIGH_DUTY_CYCLE_DIRECTED_CONNECTABLE_ADVERTISING
            if directed
            else Properties.SCANNABLE_ADVERTISING
        )
        await tester.command(
            extended_parameters(
                advertising_event_properties=properties,
                primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
                peer_address_type=peer.address_type,
                peer_address=peer,
            )
        )
        duration = (
            LL_ADV_DIRECT_HIGH_DUTY_TIMEOUT_US // hci.HCI_LE_DURATION_UNIT_US
            if directed
            else 0
        )
        await tester.command(
            hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0], [duration], [0])
        )
    else:
        await tester.command(
            legacy_parameters(
                advertising_type=(
                    AdvType.ADV_DIRECT_IND if directed else AdvType.ADV_IND
                ),
                advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
                peer_address_type=peer.address_type,
                peer_address=peer,
            )
        )
        await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
    kind = AdvertisingPduType.ADV_DIRECT_IND if directed else AdvertisingPduType.ADV_IND
    starts = {}
    for _ in range(2):
        for channel in LL_ADV_PRIMARY_CHANNELS:
            advertisement = await tester.expect_pdu(channel, kind=kind)
            assert advertisement.phy == Phy.LE_1M
            assert advertisement.pdu[0] & PDU_ADV_CHSEL_MASK
            assert not advertisement.pdu[0] & (PDU_ADV_TXADD_MASK | PDU_ADV_RXADD_MASK)
            assert advertisement.pdu[PDU_HEADER_SIZE:] == bytes(DUT_ADDRESS) + (
                bytes(peer) if directed else b''
            )
            if channel in starts:
                elapsed = advertisement.start_us - starts[channel]
                if directed:
                    assert 0 < elapsed <= HIGH_DUTY_INTERVAL_MAX_US
                else:
                    minimum = (
                        hci.HCI_LE_ADV_INTERVAL_MIN * hci.HCI_LE_ADV_INTERVAL_UNIT_US
                    )
                    assert minimum <= elapsed <= minimum + LL_ADV_DELAY_MAX_US
            starts[channel] = advertisement.start_us
    parameters = _advertiser_connection_parameters()
    request = request_pdu(
        AdvertisingPduType.CONNECT_IND, scanner=peer, data=parameters.encode()
    )
    if csa2:
        request = bytes([request[0] | PDU_ADV_CHSEL_MASK]) + request[1:]
    end = await tester.transmit(
        channel, request, start_us=advertisement.end_us + LL_IFS_US
    )
    link = PeerLink(tester, parameters, end, csa2=csa2)
    await _data_events(tester, link, 1, observe_advertising=True)
    await _connection_complete(tester, link, peer, extended=extended)
    await _data_events(tester, link, CSA_TIMING_EVENTS, observe_advertising=True)
    _no_connection_events(tester)
    await link.disconnect()
    _no_connection_events(tester)


async def _data_events(
    tester, link, count, *, observe_advertising=False, advertising_phy=Phy.LE_1M
):
    """Measure every response/anchor; do not infer timing from DUT state."""
    starts = []
    for index in range(count):
        if observe_advertising:
            duration = link.anchor - tester.radio.now_us - 50
            if duration > 0:
                channel = LL_ADV_PRIMARY_CHANNELS[index % len(LL_ADV_PRIMARY_CHANNELS)]
                assert (
                    await tester.receive(
                        channel, duration_us=duration, phy=advertising_phy
                    )
                    is None
                ), 'advertising continued after connection creation'
        previous_sn = link.sn
        packet = await link.exchange(check_ack=True)
        assert packet.phy == link.rx_phy
        assert packet.pdu[0] & PDU_DATA_LLID_MASK in (
            LlId.CONTINUATION,
            LlId.START,
            LlId.CONTROL,
        )
        if link.central and starts:
            assert bool(packet.pdu[0] & PDU_DATA_NESN_MASK) != bool(previous_sn)
        starts.append(packet.start_us)
    interval = link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
    sca_ppm = LL_SCA_PPM[link.parameters.hop_sca >> PDU_ADV_CONNECT_SCA_SHIFT]
    tolerance = 20 if sca_ppm == 500 else 10
    assert all(
        (abs(right - left - interval) <= tolerance for left, right in pairwise(starts))
    )
    assert (
        abs(starts[-1] - starts[0] - (count - 1) * interval)
        <= (count - 1) * interval * sca_ppm / PHY_TIME_UNITS_PER_SECOND
    )


async def _central_timing_events(
    tester,
    link,
    count,
    *,
    silent_until_us=0,
    window_delay_us=LL_CONN_INTERVAL_UNIT_US,
):
    """Measure Central timing, optionally withholding initial Peripheral replies."""
    assert link.central and link.counter == 0 and count > 0
    first_anchor = link.anchor
    starts = []
    replies = 0
    while replies < count:
        channel = link.channel
        packet = await _receive_central_event(link, window_delay_us=window_delay_us)
        if replies:
            assert bool(packet.pdu[0] & PDU_DATA_NESN_MASK) != bool(link.sn)
        elif silent_until_us:
            _empty_data(packet, (0, 0))
        if packet.start_us >= silent_until_us:
            link._accept(packet)
            await link._send(channel, b'', LlId.CONTINUATION, packet.end_us + LL_IFS_US)
            replies += 1
        starts.append(packet.start_us)
    _check_central_timing(
        link.parameters, first_anchor, starts, window_delay_us=window_delay_us
    )


async def _extended_advertisement(
    tester, primary_phy, phy, directed, starts, *, decision=False
):
    """Observe full primary cycles and follow the last primary's AuxPtr."""
    for _ in range(1 if starts else 2):
        primaries = []
        for primary_channel in LL_ADV_PRIMARY_CHANNELS:
            primary = await tester.expect_pdu(
                primary_channel,
                kind=(
                    AdvertisingPduType.ADV_DECISION_IND
                    if decision
                    else AdvertisingPduType.ADV_EXT_IND
                ),
                phy=primary_phy,
            )
            primary_fields = decode_extended(primary.pdu)
            # Step 4: ADV_DECISION_IND carries AuxPtr and optionally ADI (Core
            # Vol 6, Part B, Table 2.10); ADV_EXT_IND requires ADI (Table 2.4).
            assert (
                {'aux', 'data'} <= set(primary_fields) <= {'adi', 'aux', 'data'}
                if decision
                else set(primary_fields) == {'adi', 'aux', 'data'}
            )
            assert primary_fields['data'] == (
                b'\0' + DECISION_ADVERTISING_DATA if decision else b''
            )
            assert (
                primary.pdu[PDU_HEADER_SIZE] >> PDU_ADV_EXT_HEADER_LENGTH_BITS
                == AdvertisingMode.CONNECTABLE
            )
            if primary_channel in starts:
                elapsed = primary.start_us - starts[primary_channel]
                assert (
                    ADV_INTERVAL_US <= elapsed <= ADV_INTERVAL_US + LL_ADV_DELAY_MAX_US
                )
            starts[primary_channel] = primary.start_us
            primaries.append((primary, primary_fields))
        channel, adv, fields = await follow_auxiliary(
            tester, primary, primary_fields, phy=phy
        )
        for parent, header in primaries:
            pointer = int.from_bytes(header['aux'], 'little')
            pointed_channel, offset = decode_aux(header['aux'])
            unit = (
                PDU_ADV_OFFSET_UNIT_300_US
                if pointer & PDU_ADV_AUX_OFFSET_UNITS_MASK
                else PDU_ADV_OFFSET_UNIT_30_US
            )
            assert pointed_channel == channel
            assert pointer >> (PHY_OCTET_BITS + PDU_ADV_OFFSET_BITS) == phy - 1
            assert (
                parent.start_us + offset
                <= adv.start_us
                < parent.start_us + offset + unit
            )
            assert adv.start_us - parent.end_us >= LL_MAFS_US
            if 'adi' in header:
                assert header['adi'] == fields['adi']
        assert adv.phy == phy
        assert adv.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.ADV_EXT_IND
        assert (
            adv.pdu[PDU_HEADER_SIZE] >> PDU_ADV_EXT_HEADER_LENGTH_BITS
            == AdvertisingMode.CONNECTABLE
        )
        assert not adv.pdu[0] & (PDU_ADV_TXADD_MASK | PDU_ADV_RXADD_MASK)
        assert fields['address'] == bytes(DUT_ADDRESS)
        if 'adi' in primary_fields:
            assert fields['adi'] == primary_fields['adi']
        assert set(fields) == {'address', 'adi', 'data'} | (
            {'target'} if directed else set()
        )
        assert not fields['data']
        if directed:
            assert fields['target'] == bytes(LOWER_CENTRAL_PUBLIC_ADDRESS)
    return channel, adv


def _no_connection_events(tester):
    assert not any(
        isinstance(
            event,
            (
                hci.HCI_LE_Connection_Complete_Event,
                hci.HCI_LE_Enhanced_Connection_Complete_Event,
                hci.HCI_LE_Channel_Selection_Algorithm_Event,
                hci.HCI_LE_Advertising_Set_Terminated_Event,
                hci.HCI_Disconnection_Complete_Event,
            ),
        )
        for event in tester.events
    ), 'unexpected connection, disconnection, or advertising-termination event'


async def _extended_setup(
    tester,
    *,
    phy=Phy.LE_1M,
    decision=False,
    peer=LOWER_CENTRAL_PUBLIC_ADDRESS,
    random_round=False,
):
    """Check all extended connection address rounds and post-connection RF silence."""
    primary_phy = Phy.LE_CODED if phy == Phy.LE_CODED else Phy.LE_1M
    for directed in (False,) if decision or random_round else (False, True):
        for valid in (True,) if random_round else (True, False):
            properties = Properties.CONNECTABLE_ADVERTISING | (
                Properties.USE_DECISION_PDUS if decision else 0
            )
            if directed:
                properties |= Properties.DIRECTED_ADVERTISING
            await tester.command(
                extended_parameters(
                    advertising_event_properties=properties,
                    primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
                    primary_advertising_phy=primary_phy,
                    secondary_advertising_phy=phy,
                    peer_address=peer,
                    peer_address_type=peer.address_type,
                )
            )
            if decision:
                await tester.command(
                    hci.HCI_LE_Set_Decision_Data_Command(
                        0, 0, DECISION_ADVERTISING_DATA
                    )
                )
            await tester.command(extended_enable())
            starts = {}
            parameters = _advertiser_connection_parameters()
            attempts = AUXILIARY_SETUP_ATTEMPTS if valid else 1
            for _ in range(attempts):
                channel, adv = await _extended_advertisement(
                    tester, primary_phy, phy, directed, starts, decision=decision
                )
                request = request_pdu(
                    AdvertisingPduType.CONNECT_IND,
                    advertiser=DUT_ADDRESS if valid else OTHER_PUBLIC,
                    scanner=peer,
                    data=parameters.encode(),
                )
                await tester.transmit(
                    channel, request, start_us=adv.end_us + LL_IFS_US, phy=phy
                )
                end = tester.radio.now_us + 1
                response = await tester.receive(channel, duration_us=1000, phy=phy)
                if not valid:
                    assert response is None
                    _no_connection_events(tester)
                    continue
                if response is None:
                    continue
                assert (
                    abs(response.start_us - end - LL_IFS_US) <= CONNECT_IFS_TOLERANCE_US
                )
                assert response.valid_crc and response.phy == phy
                assert len(response.pdu) >= PDU_HEADER_SIZE
                assert response.pdu[1] == len(response.pdu) - PDU_HEADER_SIZE
                assert not response.pdu[0] & PDU_ADV_TXADD_MASK
                assert bool(response.pdu[0] & PDU_ADV_RXADD_MASK) == peer.is_random
                assert (
                    response.pdu[PDU_HEADER_SIZE] >> PDU_ADV_EXT_HEADER_LENGTH_BITS
                    == AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
                )
                assert (
                    response.pdu[0] & PDU_ADV_TYPE_MASK
                    == AdvertisingPduType.AUX_CONNECT_RSP
                )
                fields = decode_extended(response.pdu)
                assert fields == {
                    'address': bytes(DUT_ADDRESS),
                    'target': bytes(peer),
                    'data': b'',
                }
                link = PeerLink(
                    tester,
                    parameters,
                    end,
                    csa2=True,
                    phy=phy,
                )
                link.anchor = (
                    end
                    + (
                        LL_CONN_AUX_WINDOW_DELAY_CODED_US
                        if phy == Phy.LE_CODED
                        else LL_CONN_AUX_WINDOW_DELAY_UNCODED_US
                    )
                    + parameters.window_offset * LL_CONN_INTERVAL_UNIT_US
                )
                await _connection_complete(tester, link, peer, extended=True)
                await _data_events(
                    tester,
                    link,
                    100,
                    observe_advertising=True,
                    advertising_phy=primary_phy,
                )
                _no_connection_events(tester)
                await link.disconnect()
                _no_connection_events(tester)
                break
            else:
                assert not valid, 'AUX_CONNECT_RSP absent after twenty requests'
            await tester.command(extended_enable(0))
            if not valid:
                _no_connection_events(tester)


async def _legacy_setup(tester, *, directed=False):
    """Reject a wrong high-duty initiator, then maintain the selected legacy link."""
    if directed:
        await tester.command(
            legacy_parameters(
                advertising_type=AdvType.ADV_DIRECT_IND,
                advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
                peer_address_type=LOWER_CENTRAL_PUBLIC_ADDRESS.address_type,
                peer_address=LOWER_CENTRAL_PUBLIC_ADDRESS,
            )
        )
        await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
        adv = await tester.expect_pdu(37, kind=AdvertisingPduType.ADV_DIRECT_IND)
        await tester.transmit(
            37,
            request_pdu(
                AdvertisingPduType.CONNECT_IND,
                scanner=OTHER_PUBLIC,
                data=_advertiser_connection_parameters().encode(),
            ),
            start_us=adv.end_us + LL_IFS_US,
        )
        await tester.expect_pdu(37, kind=AdvertisingPduType.ADV_DIRECT_IND)
        await tester.wait(LL_ADV_DIRECT_HIGH_DUTY_TIMEOUT_US)
        event = await tester.event(hci.HCI_LE_Connection_Complete_Event)
        assert event.status == ErrorCode.ADVERTISING_TIMEOUT_ERROR
    filtered = not directed
    if filtered:
        await tester.command(hci.HCI_LE_Clear_Filter_Accept_List_Command())
        await tester.command(
            hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(
                LOWER_CENTRAL_PUBLIC_ADDRESS.address_type, LOWER_CENTRAL_PUBLIC_ADDRESS
            )
        )
    for channel in LL_ADV_PRIMARY_CHANNELS if not directed else (37,):
        await tester.command(
            legacy_parameters(
                advertising_type=(
                    AdvType.ADV_DIRECT_IND if directed else AdvType.ADV_IND
                ),
                advertising_channel_map=(
                    LL_ADV_PRIMARY_CHANNEL_MAP_ALL
                    if directed
                    else 1 << (channel - LL_ADV_PRIMARY_CHANNELS[0])
                ),
                peer_address_type=LOWER_CENTRAL_PUBLIC_ADDRESS.address_type,
                peer_address=LOWER_CENTRAL_PUBLIC_ADDRESS,
                advertising_filter_policy=(
                    ADV_FILTER_ACCEPT_LIST_ONLY if filtered else 0
                ),
            )
        )
        await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
        kind = (
            AdvertisingPduType.ADV_DIRECT_IND
            if directed
            else AdvertisingPduType.ADV_IND
        )
        advertisement = await tester.expect_pdu(channel, kind=kind)
        if not directed:
            following = await tester.expect_pdu(channel, kind=kind)
            interval = following.start_us - advertisement.start_us
            assert ADV_INTERVAL_US <= interval <= ADV_INTERVAL_US + LL_ADV_DELAY_MAX_US
            for other in LL_ADV_PRIMARY_CHANNELS:
                if other != channel:
                    assert (
                        await tester.receive(
                            other, duration_us=ADV_INTERVAL_US + LL_ADV_DELAY_MAX_US
                        )
                        is None
                    )
            advertisement = await tester.expect_pdu(channel, kind=kind)
        assert not advertisement.pdu[0] & (PDU_ADV_TXADD_MASK | PDU_ADV_RXADD_MASK)
        assert advertisement.pdu[PDU_HEADER_SIZE:] == bytes(DUT_ADDRESS) + (
            bytes(LOWER_CENTRAL_PUBLIC_ADDRESS) if directed else b''
        )
        parameters = _advertiser_connection_parameters()
        end = await tester.transmit(
            channel,
            request_pdu(
                AdvertisingPduType.CONNECT_IND,
                scanner=LOWER_CENTRAL_PUBLIC_ADDRESS,
                data=parameters.encode(),
            ),
            start_us=advertisement.end_us + LL_IFS_US,
        )
        link = PeerLink(tester, parameters, end)
        await _connection_complete(tester, link, LOWER_CENTRAL_PUBLIC_ADDRESS)
        await _legacy_timing_events(tester, link, directed=directed)
        _completion_events(tester, link)
        await link.disconnect()
        _completion_events(tester, link)


async def _legacy_timing_events(tester, link, *, directed):
    """Count actual DATA responses rather than assuming one reply per event."""
    required = 900 if directed else 100
    # BV-01 permits 900 transmitted events. BV-04 requires 900 responses.
    maximum = 10 * required if directed else 900
    responses = 0
    for index in range(maximum):
        duration = link.anchor - tester.radio.now_us - CONNECT_IFS_TOLERANCE_US
        if duration > 0:
            channel = LL_ADV_PRIMARY_CHANNELS[index % len(LL_ADV_PRIMARY_CHANNELS)]
            assert await tester.receive(channel, duration_us=duration) is None
        packet = await link.exchange(check_ack=True, required=False)
        if packet is not None:
            _empty_data(packet)
            responses += 1
            if responses == required:
                return
    raise AssertionError(f'only {responses}/{required} legacy DATA responses')


async def accepting_connections(tester):
    """Accepting Connections.

    Initial Condition:
        Use public addresses and undirected advertising at a selected 20-ms base
        interval with an empty device name. The Filter Accept List contains only
        the Lower Tester's public Central address; scan and connection requests
        are filtered through that list. Use the common parameters from Sections
        4.3.2.1 and 4.3.3.1: interval=8 (10 ms), WinSize=WinOffset=7, latency=0,
        timeout=30 (300 ms), CRCInit=0x198891, Hop=5, and SCA category 0.
        The reviewed correction uses ChM=0x03, enabling data channels 0 and 1.

    Test Steps:
        1. Configure the Lower Tester as an initiator using the common public
           address and common connection parameters.
        2. Enable legacy undirected advertising on the first supported primary
           channel, 37, with the configured Filter Accept List policy.
        3. Receive ADV_IND on that channel. Check its public advertiser address,
           the selected interval including AdvDelay, and silence on the other
           primary channels.
        4. Receive ADV_IND and transmit CONNECT_IND at T_IFS after its end.
           Repeat up to 20 times or until advertising stops. In this ideal-link
           trial no request loss is injected: the first request is accepted and
           the procedure takes the permitted early-exit path.
        5. Transmit the first correctly formatted empty DATA PDU one connection
           interval after CONNECT_IND, using the common CSA#1 parameters.
        6. Receive correctly formatted DATA replies on the same data channel at
           T_IFS. Continue until 100 replies have been measured, allowing up to
           900 transmitted events. Count replies, not merely attempted events.
        7. Interleaved with Step 6, require Connection Complete with Peripheral
           role, the Lower Tester address/type, and the requested connection
           interval, latency, timeout, and Central clock accuracy.
        8. Terminate the Peripheral connection through HCI and require its
           matching Disconnection Complete event.
        9. Configure the Lower Tester to initiate another connection with the
           same common parameters.
        10. Enable advertising on the second supported primary channel, 38.
        11. Repeat Steps 3-8 on channel 38.
        12. Configure the Lower Tester to initiate another connection with the
            same common parameters.
        13. Enable advertising on the third supported primary channel, 39.
        14. Repeat Steps 3-8 on channel 39.

    Expected Outcome:
        Pass verdict:
            Every channel trial stops advertising, establishes the requested
            connection, and produces at least 100 valid, acknowledged CSA#1
            replies within the 900-event bound. Verify reply timing at T_IFS;
            the simulation's exact integer-us check is stricter than the source
            tolerance around T_IFS. HCI reports the requested connection.
        Fail verdict:
            Any Advertising Set Terminated or Channel Selection Algorithm event
            on this legacy Peripheral command path fails the trial.

    Source Correction:
        The common ChM bit string enables only channel 0, contrary to the Core
        minimum of two used ACL channels. Use the reviewed local correction
        ChM=0x03, not a claimed published SIG erratum. Step 8's handle reference
        to Step 4 means the handle reported by HCI in Step 7; CONNECT_IND does
        not contain an HCI connection handle.

    Review Limitation:
        The PHY uses integer-microsecond timestamps. The 0.1-us measurement
        accuracy specified in the source Notes is not independently exercised;
        passing the integer-us reply checks does not close this limitation.

    Timing Approximation:
        Execute at 1-us timestamp resolution, retaining the limited precision
        label. Fractional guards use the documented integer-us approximation.
        Complete all executable checks without skipping for precision alone;
        only an independent source_conflict label permits a protocol-path skip.

    Reference:
        LL.TS.p28.pdf, LL/CON/ADV/BV-01-C, independently titled procedure after
        Section 4.3.3.2; PDF pages 478-480, printed pages 4.3-5 through 4.3-7.
        Steps 1-14, Expected Outcome, and Notes; common tables on PDF pages
        476-477. Core Vol 6, Part B, Section 4.5.5 defines connection events.
    """
    await _legacy_setup(tester, directed=False)


# Verify that an undirected advertiser accepts a connection request, stops advertising,
# and maintains a correctly timed Peripheral connection using Channel Selection
# Algorithm #1 on each supported primary channel.
PROCEDURES |= procedure_cases(
    accepting_connections,
    {
        'LL/CON/ADV/BV-01-C': pytest.param(
            'Accepting Connections',
            {},
            marks=[
                pytest.mark.limited,
            ],
        ),
    },
)


async def accepting_connections_timeout(tester):
    """Accepting Connections Timeout.

    Initial Condition:
        Use legacy undirected advertising on primary channel 37 with public
        addresses and the selected 20-ms base advertising interval. The Filter
        Accept List contains the Lower Tester's public address; only listed
        peers may send scan or connection requests. Select the common interval
        of 10 ms, WinSize=WinOffset=7, latency=0, supervision timeout=300 ms,
        CRCInit=0x198891, Hop=5, SCA category 0, and corrected ChM=0x03.

    Test Steps:
        1. Configure the Lower Tester as an initiator using the common public
           address and common connection/data-channel parameters.
        2. Enable legacy advertising with LE Set Advertising Enable on the
           selected supported primary channel, 37.
        3. Receive ADV_IND and respond with CONNECT_IND at T_IFS after its end.
           Repeat up to 20 times or until advertising stops. In this ideal-link
           trial the first request is accepted, so no retry is needed.
        4. Send no further RF packets after CONNECT_IND. Monitor primary channel
           37 for continued advertising and observe HCI until establishment
           failure, rather than maintaining the connection with DATA packets.
        5. Interleaved with Step 4, require Connection Complete with the public
           peer identity and requested interval, latency, and timeout; retain
           its connection handle.
        6. Require successful Disconnection Complete with the same handle and
           reason Connection Failed to be Established (0x3E). Independently
           derive T1 and T2 from the requested windows and clock accuracy; if
           T2 precedes T1, reject an event before T2, including a check immediately
           before that boundary.

    Expected Outcome:
        Pass verdict:
            Advertising stops and HCI reports connection establishment failure.
            When T2 is earlier than T1, Disconnection Complete is not earlier
            than T2. T1 is the end of the sixth receive window; T2 is the end of
            the receive window immediately before the first window starting at
            least six connection intervals after CONNECT_IND ends.
        Fail verdict:
            Any Advertising Set Terminated or Channel Selection Algorithm event
            on this legacy Peripheral command path fails the trial.

    Source Correction:
        The common table's one-channel ChM is treated as an editorial error;
        use the reviewed local correction 0x03, not a published SIG erratum.

    Coverage Note:
        For the source's common window parameters, T1 equals T2, so the source's
        conditional T2-before-T1 lower bound does not apply to this vector.
        The independent window-bound algorithm and its pre-expiry observation
        are covered by unit tests with a different, legal window offset.

    Reference:
        LL.TS.p28.pdf, LL/CON/ADV/BV-02-C, independently titled procedure after
        Section 4.3.3.2; PDF pages 480-481, printed pages 4.3-7 through 4.3-8.
        Steps 1-6 and Expected Outcome; common tables on PDF pages 476-477.
        Core Vol 6, Part B, Section 4.5.5 defines connection establishment.
    """
    parameters = _advertiser_connection_parameters()
    await tester.command(hci.HCI_LE_Clear_Filter_Accept_List_Command())
    await tester.command(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(
            LOWER_CENTRAL_PUBLIC_ADDRESS.address_type, LOWER_CENTRAL_PUBLIC_ADDRESS
        )
    )
    link, end = await _pending_connection(
        tester,
        central=False,
        parameters=parameters,
        advertising_filter_policy=ADV_FILTER_ACCEPT_LIST_ONLY,
    )
    _completion_events(tester, link)
    earliest = end + _accepting_timeout_bound(parameters)
    latest = (
        end
        + LL_CONN_ESTABLISHMENT_EVENTS * parameters.interval * LL_CONN_INTERVAL_UNIT_US
    )
    event = None
    while tester.radio.now_us <= latest + HCI_OBSERVATION_STEP_US:
        event = _disconnected(tester)
        if event is not None:
            assert tester.radio.now_us >= earliest
            break
        duration = HCI_OBSERVATION_STEP_US
        if tester.radio.now_us < earliest - 1:
            duration = min(duration, earliest - 1 - tester.radio.now_us)
        assert await tester.receive(37, duration_us=duration) is None
    assert event is not None, 'setup did not time out'
    assert (event.status, event.connection_handle, event.reason) == (
        ErrorCode.SUCCESS,
        link.handle,
        ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR,
    )
    _completion_events(tester, link)


# Verify that an advertiser accepts a connection request and stops advertising, but
# reports establishment failure when the Central sends no DATA before the connection
# establishment timer expires.
PROCEDURES |= procedure_cases(
    accepting_connections_timeout,
    {
        'LL/CON/ADV/BV-02-C': (
            'Accepting Connections Timeout',
            {},
        ),
    },
)


async def central_missing_peripheral_packets(tester):
    """Central Missing Peripheral Packets.

    Initial Condition:
        Use public addresses and legacy undirected advertising on primary
        channel 37. Provision the Lower Tester's public Central address in the
        Filter Accept List, but use the accept-all advertising policy explicitly
        selected by the procedure preamble. Use the common 10-ms interval,
        WinSize=WinOffset=7, supervision timeout=300 ms, CRCInit=0x198891, Hop=5,
        SCA category 0, and corrected ChM=0x03. Override common latency with 5.

    Test Steps:
        1. Configure the Lower Tester as an initiator with the common public
           address, common connection parameters, and Peripheral latency 5.
        2. Enable legacy undirected advertising with LE Set Advertising Enable
           on the selected primary channel, 37.
        3. Receive ADV_IND on that channel and respond with CONNECT_IND at
           T_IFS after its end. The first DATA event begins one common connection
           interval after the request ends.
        4. For each of 30 events, transmit a correctly sequenced empty DATA PDU
           without acknowledging the Peripheral's initial SN=0 transmission:
           retain NESN=0 while advancing the Central's SN when acknowledged.
        5. Interleave with Step 4: receive correctly formatted DATA replies on
           the same data channel at T_IFS. Check unchanged Peripheral SN=0,
           correct NESN, and at least nine responses across the 30 events.
        6. For each of another 30 events, transmit correctly sequenced empty
           DATA PDUs that acknowledge the Peripheral's preceding transmission,
           using the common interval and CSA#1 channel selection.
        7. Interleave with Step 6: receive and validate the Peripheral's DATA
           replies at T_IFS. Permit latency-five omissions, but no more than five
           consecutive missing replies or six intervals between replies.
        8. Interleaved with Steps 4-5, require Connection Complete with the
           public peer identity, requested interval, latency five, and timeout.
        9. Terminate the Peripheral connection through HCI using its reported
           handle, and require the matching Disconnection Complete event.

    Expected Outcome:
        Pass verdict:
            Advertising stops and the Peripheral maintains a CSA#1 connection.
            Before its first transmission is acknowledged, at least nine of
            the first 30 events contain correctly timed replies. Subsequent
            acknowledgements and omissions respect Peripheral latency five.
        Fail verdict:
            Any Advertising Set Terminated or Channel Selection Algorithm event
            on this legacy Peripheral command path fails the trial.

    Source Correction:
        Use the reviewed local correction ChM=0x03 for the invalid one-channel
        common table; this is not a published SIG erratum. The preamble's phrase
        about filtering unknown peers conflicts with its explicit accept-any
        policy: follow that explicit policy while provisioning the stated list.
        Step 9's reference to a handle from nonexistent Step 10 means the HCI
        connection handle reported in Step 8.

    Reference:
        LL.TS.p28.pdf, LL/CON/ADV/BV-03-C, independently titled procedure after
        Section 4.3.3.2; PDF pages 481-483, printed pages 4.3-8 through 4.3-10.
        Steps 1-9 and Expected Outcome; common tables on PDF pages 476-477.
        Core Vol 6, Part B, Sections 4.5.5 and 4.5.9 define establishment and ARQ.
    """
    parameters = _advertiser_connection_parameters(latency=PERIPHERAL_LATENCY)
    await tester.command(hci.HCI_LE_Clear_Filter_Accept_List_Command())
    await tester.command(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(
            LOWER_CENTRAL_PUBLIC_ADDRESS.address_type, LOWER_CENTRAL_PUBLIC_ADDRESS
        )
    )
    link, _ = await _pending_connection(tester, central=False, parameters=parameters)
    _completion_events(tester, link)
    responses = 0
    peer_sn = 0
    acknowledgement = 0
    gap = 0
    for index in range(2 * LATENCY_EVENTS):
        duration = link.anchor - tester.radio.now_us - 50
        if duration > 0:
            assert await tester.receive(37, duration_us=duration) is None
        sent = _data_pdu(peer_sn, acknowledgement)
        packet, _ = await _wire_event(link, lambda _, value=sent: value, required=False)
        gap += 1
        if packet is not None:
            _empty_data(packet)
            sn, nesn = _sequence_bits(packet)
            assert nesn == peer_sn ^ 1
            peer_sn = nesn
            if index < LATENCY_EVENTS:
                assert sn == 0
                responses += 1
            else:
                assert gap <= PERIPHERAL_LATENCY + 1
            acknowledgement = 0 if index < LATENCY_EVENTS - 1 else sn ^ 1
            _restore_peer(link, packet, sent)
            gap = 0
        if index == LATENCY_EVENTS - 1:
            assert responses >= MINIMUM_NAK_RESPONSES
        if index >= LATENCY_EVENTS:
            assert gap <= PERIPHERAL_LATENCY
    _completion_events(tester, link)
    await link.disconnect()


# Verify that a newly connected Peripheral does not use Peripheral latency before the
# Central acknowledges its initial transmission. First withhold acknowledgements, then
# verify normal acknowledgement and latency behavior.
PROCEDURES |= procedure_cases(
    central_missing_peripheral_packets,
    {
        'LL/CON/ADV/BV-03-C': (
            'Central Missing Peripheral Packets',
            {},
        ),
    },
)


async def directed_advertising_connection(tester):
    """Directed Advertising Connection.

    Initial Condition:
        Select public own and initiator addresses and high-duty directed
        advertising on all supported primary channels. The intended initiator
        is the Lower Tester's public Central address. Use the common 10-ms
        connection interval, WinSize=WinOffset=7, latency=0, supervision timeout
        of 300 ms, CRCInit=0x198891, Hop=5, SCA category 0, and corrected ChM=0x03.

    Test Steps:
        1. Configure the Lower Tester to initiate using a public InitA different
           from the directed advertising target.
        2. Enable high-duty directed advertising in the IUT through LE Set
           Advertising Enable, using all supported primary advertising channels.
        3. Receive ADV_DIRECT_IND and respond on the same channel with a
           CONNECT_IND carrying the mismatching InitA at T_IFS after its end.
        4. Require further ADV_DIRECT_IND after the rejected request, then
           observe the high-duty advertising timeout of 1.28 s without sending
           a matching request.
        5. Require Connection Complete with Directed Advertising Timeout status.
        6. Configure the Lower Tester to initiate using the matching public InitA
           and common connection/data-channel parameters.
        7. Enable high-duty directed advertising again on all supported primary
           advertising channels.
        8. Receive ADV_DIRECT_IND containing the IUT AdvA and Lower Tester
           TargetA; respond with matching CONNECT_IND at T_IFS after its end.
        9. Require Connection Complete with Peripheral role, matching public
           peer identity, and the requested interval, latency, and timeout.
        10. Transmit the first correctly formatted DATA PDU one connection
            interval after CONNECT_IND ends, using the common CSA#1 parameters.
        11. Require correctly formatted DATA replies on the same data channel
            at T_IFS and continue until 900 replies have been received. Observe
            that advertising has stopped while maintaining the connection.

        Cleanup: terminate the resulting connection through HCI.

    Expected Outcome:
        Pass verdict:
            The wrong initiator is rejected and directed advertising times out.
            The matching initiator establishes the requested Peripheral
            connection, HCI reports it, and 900 correctly timed CSA#1 replies
            demonstrate that the connection is maintained.
        Fail verdict:
            Any Advertising Set Terminated or Channel Selection Algorithm event
            on this legacy Peripheral command path fails the trial.

    Source Correction:
        Treat the one-channel common ChM as an editorial error and use the
        reviewed local correction 0x03; this is not a published SIG erratum.

    Reference:
        LL.TS.p28.pdf, LL/CON/ADV/BV-04-C, independently titled procedure after
        Section 4.3.3.2; PDF pages 483-485, printed pages 4.3-10 through 4.3-12.
        Steps 1-11 and Expected Outcome; common tables on PDF pages 476-477.
        Core Vol 6, Part B, Section 4.4.2.4 defines directed advertising.
    """
    await _legacy_setup(tester, directed=True)


# Verify that directed advertising rejects a connection request from a mismatching
# initiator, then accepts the intended public initiator and maintains a Peripheral
# connection using Channel Selection Algorithm #1.
PROCEDURES |= procedure_cases(
    directed_advertising_connection,
    {
        'LL/CON/ADV/BV-04-C': (
            'Directed Advertising Connection',
            {},
        ),
    },
)


async def extended_advertising_accepting_connections(tester, *, phy):
    """Extended Advertising, Accepting Connections.

    Initial Condition:
        Use public IUT and Lower Tester addresses, all primary advertising
        channels, accept-all filtering, an empty device name, and the minimum
        20-ms base advertising interval. Table 4.3-2 selects primary/secondary
        PHYs: 1M/1M for LL/CON/ADV/BV-05-C, 1M/2M for LL/CON/ADV/BV-12-C,
        and Coded/Coded for LL/CON/ADV/BV-13-C. All three use ADV_EXT_IND.
        Select the common Lower Tester parameters: interval=8 (10 ms),
        WinSize=WinOffset=7, latency=0, timeout=30 (300 ms), CRCInit=0x198891,
        Hop=5, SCA category 0, and the reviewed common-table ChM=0x03 correction.

    Test Case Configuration:
        LL/CON/ADV/BV-05-C: LE 1M.
        LL/CON/ADV/BV-12-C: LE 2M.
        LL/CON/ADV/BV-13-C: LE Coded.

    Test Steps:
        Execute all four ADV_EXT_IND rounds in Table 4.3-3: properties 0x0001
        with matching/wrong AdvA, then properties 0x0005 with matching/wrong
        AdvA. Each round starts with a newly enabled advertising set.

        1. Send LE Set Extended Advertising Parameters for handle 0, all primary
           channels, minimum advertising interval, the round's properties,
           public own address, the Table 4.3-2 PHYs, and filter policy 0x00.
           For directed rounds, select the Lower Tester's public target address.
           Require successful Command Complete.
        2. Send LE Set Decision Data only for ADV_DECISION_IND cases. This step
           is not applicable to the three selected ADV_EXT_IND cases.
        3. Enable advertising with Duration=0 and Max_Extended_Advertising_Events
           =0; require successful Command Complete.
        4. Receive ADV_EXT_IND with connectable AdvMode and only AuxPtr and ADI
           in its Extended Header. Observe two full primary-channel cycles to
           check the selected interval, including AdvDelay, on channels 37-39.
        5. Follow AuxPtr to AUX_ADV_IND on the selected secondary PHY. Check
           connectable AdvMode, public AdvA, matching ADI, and the Lower Tester's
           public TargetA in directed rounds. Check every observed primary
           AuxPtr against the actual auxiliary channel, PHY, offset window, and
           T_MAFS. Send AUX_CONNECT_REQ at T_IFS after AUX_ADV_IND, using the
           Lower Tester's InitA and matching or wrong AdvA for the current round.
        6. For wrong AdvA, require no AUX_CONNECT_RSP and no connection-related
           HCI event. Disable advertising successfully and proceed to Step 16
           without creating or using a connection handle.
        7. For matching AdvA, require AUX_CONNECT_RSP on the secondary PHY at
           T_IFS within the source tolerance. Check CRC, length, PDU type,
           AdvMode, public AdvA and TargetA. If no response arrives, repeat
           Steps 4-7, up to 20 requests; fail if all attempts are unanswered.
        8. During free RF gaps in the following connection events, scan all
           primary channels on the selected primary PHY. Require advertising
           to have stopped after connection establishment.
        9. Require Connection Complete with Peripheral role, the requested
           public peer identity, interval, latency, supervision timeout, and
           Central clock accuracy. Retain its valid connection handle.
        10. If Channel Selection Algorithm is reported, require algorithm #2
            and the connection handle from Step 9. Steps 10 and 11 may occur
            in either order; the algorithm event is optional.
        11. Require exactly one successful Advertising Set Terminated for
            advertising handle 0 and the connection handle from Step 9.
        12. Send a correctly formatted DATA PDU on the independently derived
            CSA#2 channel. Place the first anchor at the end of AUX_CONNECT_REQ
            plus the PHY-specific transmit-window delay plus WinOffset.
        13. Receive a correctly formatted DATA response on the same channel and
            selected PHY, with a valid acknowledgement and T_IFS spacing.
        14. Send subsequent correctly formatted DATA PDUs at the requested
            connection interval on independently calculated CSA#2 channels.
        15. Repeat the DATA exchange for exactly 100 connection events total.
            Do not repeat the one-shot HCI event in Step 11. Reject additional
            connection or advertising-termination events during the exchange.
        16. Terminate an established connection through HCI and require its
            matching completion. In rejected-address rounds there is no
            connection to disconnect; finish after disabling advertising.

    Expected Outcome:
        Pass verdict:
            In all four rounds, primary and auxiliary headers and PHYs match
            the selected configuration. Every AuxPtr identifies the observed
            auxiliary PDU with at least T_MAFS spacing. Matching AdvA produces
            AUX_CONNECT_RSP within the 2-us tolerance around T_IFS, the required
            HCI connection and advertising-termination events, and 100 valid
            CSA#2 DATA exchanges on the selected PHY without continued
            advertising. Wrong AdvA produces neither a response nor a connection.

    Source Correction:
        Step 15 refers to repeating Steps 11-14, but Step 11 is a one-shot
        Advertising Set Terminated event. Repeat the DATA exchange (Steps
        12-14) only. Step 6 skips to Step 16 even though no connection exists;
        do not issue Disconnect without a handle. These are local editorial
        interpretations supported by Core semantics, not published SIG errata.
        The common ChM=0x01 is corrected to 0x03 as approved during review.

    Reference:
        LL.TS.p28.pdf, Section 4.3.3.3, LL/CON/ADV/BV-05-C, LL/CON/ADV/BV-12-C,
        LL/CON/ADV/BV-13-C; PDF pages 485-490, printed pages 4.3-12 through
        4.3-17. Tables 4.3-2/3, Steps 1-16, and Expected Outcome. Core Vol 6,
        Part B, Sections 4.5.3-4.5.5 define the transmit window and connection
        transition; Vol 4, Part E defines the connection-related HCI events.
    """
    await _extended_setup(tester, phy=phy)


# Verify that a connectable extended advertiser accepts a request on its secondary
# channel, stops advertising, and maintains a Peripheral connection on the selected PHY
# using Channel Selection Algorithm #2. Verify the connection-related HCI events and
# rejection of wrong AdvA.
PROCEDURES |= procedure_cases(
    extended_advertising_accepting_connections,
    {
        'LL/CON/ADV/BV-05-C': (
            'Extended Advertising, Accepting Connections – LE 1M PHY',
            {'phy': Phy.LE_1M},
        ),
        'LL/CON/ADV/BV-12-C': (
            'Extended Advertising, Accepting Connections – LE 2M PHY',
            {'phy': Phy.LE_2M},
        ),
        'LL/CON/ADV/BV-13-C': (
            'Extended Advertising, Accepting Connections – LE Coded PHY',
            {'phy': Phy.LE_CODED},
        ),
    },
)


async def extended_advertising_legacy_pdus_accepting_connections(tester):
    """Extended Advertising, Legacy PDUs, Accepting Connections.

    Initial Condition:
        Select undirected connectable legacy advertising, public IUT and Lower
        Tester addresses, all primary channels, and an empty device name. Use
        the minimum 20-ms advertising interval and common connection parameters:
        interval=8 (10 ms), WinSize=WinOffset=7, latency=0, timeout=30 (300 ms),
        CRCInit=0x198891, Hop=5, SCA category 0, and corrected ChM=0x03.

    Test Steps:
        1. Send LE Set Extended Advertising Parameters with properties 0x0013
           (legacy ADV_IND), all primary channels, minimum advertising interval,
           public own address, primary LE 1M PHY, and accept-all filter policy.
           Require successful Command Complete.
        2. Enable the advertising set with Duration=0 and
           Max_Extended_Advertising_Events=0; require Command Complete success.
        3. Receive ADV_IND on a primary channel using LE 1M. Check ChSel=1,
           public AdvA, PDU length, CRC, and empty advertising data.
        4. Respond on the same channel at T_IFS with CONNECT_IND, ChSel=1,
           public Lower Tester InitA, matching IUT AdvA, and common parameters.
        5. Require Connection Complete with Peripheral role, matching public
           peer identity, requested interval, latency, timeout, and clock
           accuracy. Retain its valid handle. The source permits up to 20
           attempts of Steps 3-5; the ideal link takes the first successful one.
        6. Scan primary channels in free gaps during the DATA exchanges in
           Step 9; fail if advertising continues after connection establishment.
        7. If Channel Selection Algorithm is reported, require algorithm #2 and
           the Step 5 handle. Steps 7 and 8 may occur in either order.
        8. Require exactly one successful Advertising Set Terminated event for
           advertising handle 0 and the Step 5 connection handle.
        9. Exchange correctly formatted DATA PDUs for exactly 100 connection
           events on independently calculated CSA#2 channels at the requested
           interval. Check LE 1M, CRC, valid LLID, acknowledgements, and T_IFS.
        10. Terminate the connection through HCI and require matching completion.
        11. Repeat Steps 1-4, but set CONNECT_IND AdvA to a different public
            address. No connection-related HCI event may be produced.
        12. Continue observing ADV_IND on the same primary channel for at least
            five complete advertising intervals. Respond with wrong-AdvA
            CONNECT_IND each time, and require no DATA response or connection.

        Cleanup: disable the remaining advertising set and reject late
        connection-related HCI events.

    Expected Outcome:
        Pass verdict:
            Legacy ADV_IND uses LE 1M, ChSel=1, and the configured public AdvA.
            A matching CONNECT_IND produces the requested Peripheral connection,
            required Connection Complete and Advertising Set Terminated events,
            and an optional correctly valued Channel Selection Algorithm event.
            Advertising stops while 100 correctly timed CSA#2 DATA exchanges
            complete. Wrong AdvA leaves advertising active for at least five
            complete intervals, without a connection or DATA response.

    Source Correction:
        Step 8 references a connection handle from Step 6, but Step 5 reports
        that handle. The first Expected Outcome bullet refers to Step 4 for
        ADV_IND, which is received in Step 3. Use the reviewed local ChM=0x03
        correction for the invalid one-channel common table. These are local
        editorial corrections, not published SIG errata.

    Reference:
        LL.TS.p28.pdf, LL/CON/ADV/BV-06-C, independently titled procedure after
        Section 4.3.3.3; PDF pages 490-493, printed pages 4.3-17 through 4.3-20.
        Figures 4.3-7/8, Steps 1-12, and Expected Outcome; common tables on PDF
        pages 476-477. Core Vol 6, Part B, Sections 4.4.2.3 and 4.5.8 define
        connection acceptance and Channel Selection Algorithm #2.
    """
    parameters = _advertiser_connection_parameters()
    for valid in (True, False):
        await tester.command(
            extended_parameters(
                advertising_event_properties=(
                    Properties.CONNECTABLE_ADVERTISING
                    | Properties.SCANNABLE_ADVERTISING
                    | Properties.USE_LEGACY_ADVERTISING_PDUS
                ),
                primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
            )
        )
        await tester.command(extended_enable())
        starts = []
        # One initial request, then five complete intervals for the wrong AdvA.
        for _ in range(1 if valid else 6):
            adv = await tester.expect_pdu(37, kind=AdvertisingPduType.ADV_IND)
            assert adv.phy == Phy.LE_1M and adv.valid_crc
            assert adv.pdu == adv_pdu(address=DUT_ADDRESS, data=b'', csa2=True)
            if starts:
                assert (
                    ADV_INTERVAL_US
                    <= adv.start_us - starts[-1]
                    <= ADV_INTERVAL_US + LL_ADV_DELAY_MAX_US
                )
            starts.append(adv.start_us)
            request = request_pdu(
                AdvertisingPduType.CONNECT_IND,
                advertiser=DUT_ADDRESS if valid else OTHER_PUBLIC,
                scanner=LOWER_CENTRAL_PUBLIC_ADDRESS,
                data=parameters.encode(),
            )
            request = bytes([request[0] | PDU_ADV_CHSEL_MASK]) + request[1:]
            end = await tester.transmit(37, request, start_us=adv.end_us + LL_IFS_US)
            if valid:
                link = PeerLink(tester, parameters, end, csa2=True)
                await _connection_complete(
                    tester, link, LOWER_CENTRAL_PUBLIC_ADDRESS, extended=True
                )
                await _data_events(tester, link, 100, observe_advertising=True)
                _no_connection_events(tester)
                await link.disconnect()
            else:
                assert await tester.receive(37, duration_us=1000) is None
            _no_connection_events(tester)
        if not valid:
            assert starts[-1] - starts[0] >= 5 * ADV_INTERVAL_US
        await tester.command(extended_enable(0))
        _no_connection_events(tester)


# Verify that an advertiser controlled through the extended HCI commands accepts a
# legacy CONNECT_IND, stops advertising, and maintains a Peripheral connection using
# Channel Selection Algorithm #2. Verify connection-related HCI events and rejection of
# an incorrect AdvA.
PROCEDURES |= procedure_cases(
    extended_advertising_legacy_pdus_accepting_connections,
    {
        'LL/CON/ADV/BV-06-C': (
            'Extended Advertising, Legacy PDUs, Accepting Connections',
            {},
        ),
    },
)


async def accepting_connections_channel_selection_algorithm_1(tester, *, extended):
    """Accepting Connections, Channel Selection Algorithm #1.

    Initial Condition:
        Use undirected connectable advertising, public IUT and Lower Tester
        addresses, all primary channels, and an empty device name. Select the
        minimum advertising interval where applicable. Use common parameters:
        interval=8 (10 ms), WinSize=WinOffset=7, latency=0, timeout=30 (300 ms),
        CRCInit=0x198891, Hop=5, SCA category 0, and corrected ChM=0x03.
        LL/CON/ADV/BV-07-C uses legacy HCI advertising commands;
        LL/CON/ADV/BV-27-C uses extended HCI commands with legacy PDUs.

    Test Case Configuration:
        LL/CON/ADV/BV-07-C: legacy advertising.
        LL/CON/ADV/BV-27-C: extended advertising.

    Test Steps:
        1. Configure advertising through the command selected by Table 4.3-4,
           using all primary channels and the minimum applicable interval. Set
           advertising type 0x00, public own address, and accept-all filter
           policy. The extended command uses legacy ADV_IND properties 0x0013.
        2. Enable advertising through the Table 4.3-4 command and require
           Command Complete success. On the extended HCI path, set Duration=0
           and Max_Extended_Advertising_Events=0.
        3. Receive ADV_IND with ChSel=1, public AdvA, and LE 1M PHY.
           Check PDU length and CRC. Observe two complete primary-channel
           cycles and verify each channel's advertising interval.
        4. Respond on the same primary channel at T_IFS with CONNECT_IND,
           ChSel=0, public Lower Tester InitA, matching IUT AdvA, and the common
           connection parameters.
        5. Send correctly formatted DATA on the independently calculated CSA#1
           channel and require a valid acknowledgement and T_IFS response. The
           source allows up to 20 requests or until advertising stops; the ideal
           link takes the permitted early exit after the first successful request.
        6. Require Connection Complete with Peripheral role, the public peer
           identity, requested interval, latency, timeout, and clock accuracy.
           Retain the valid connection handle and maintain the unencrypted link.
        7. Only on the extended HCI command path, check the following substeps
           in either order:
           7A. Require one successful Advertising Set Terminated event with
               advertising handle 0 and the Step 6 connection handle.
           7B. If Channel Selection Algorithm is reported, require the Step 6
               handle and value 0 for Channel Selection Algorithm #1.
        8. Send a correctly formatted DATA PDU on the independently derived
           CSA#1 channel. Repeat Steps 8-10 for 100 additional connection events.
        9. Receive a correctly formatted DATA reply on the same channel using
           LE 1M. Check CRC, valid LLID, acknowledgement, and T_IFS spacing.
        10. Send subsequent DATA PDUs at the common connection interval on
            independently calculated CSA#1 channels. Scan all primary channels
            in free RF gaps and require advertising to have stopped.

        Cleanup: disconnect through HCI, require matching completion, and reject
        duplicate or late connection-related HCI events.

    Expected Outcome:
        Pass verdict:
            ADV_IND uses ChSel=1, the configured public addresses, and the
            permitted interval on all primary channels. CONNECT_IND ChSel=0
            establishes the requested Peripheral connection using CSA#1, not
            the algorithm advertised in ADV_IND's ChSel bit alone. Advertising
            stops, HCI reports the matching connection, and the first DATA
            exchange plus 100 subsequent events have valid channels, PHY, ARQ,
            and timing. Extended commands require Advertising Set Terminated
            and permit a correctly valued Channel Selection Algorithm event.
        Fail verdict:
            On the legacy HCI command path, either Advertising Set Terminated
            or Channel Selection Algorithm is an ineligible event and fails
            the trial, including during DATA exchange and cleanup.

    Source Correction:
        Use the reviewed local ChM=0x03 correction for the invalid one-channel
        common table; this is not a published SIG erratum. For the extended
        command path, translate the source's legacy Advertising_Type wording
        to the corresponding legacy-PDU Advertising_Event_Properties value.

    Reference:
        LL.TS.p28.pdf, Section 4.3.3.4, LL/CON/ADV/BV-07-C and
        LL/CON/ADV/BV-27-C; PDF pages 493-496, printed pages 4.3-20 through
        4.3-23. Table 4.3-4, Figure 4.3-9, Steps 1-10 including 7A/7B,
        and Expected Outcome; common tables on PDF pages 476-477. Core Vol 6,
        Part B, Sections 4.4.2 and 4.5.8 define acceptance and channel selection.
    """
    await _advertising_csa(tester, directed=False, csa2=False, extended=extended)


# Verify that an advertiser accepts a legacy CONNECT_IND with ChSel=0, stops
# advertising, and maintains a Peripheral connection using Channel Selection Algorithm
# #1. Check that connection-related HCI events match the legacy or extended command path
# selected by Table 4.3-4.
PROCEDURES |= procedure_cases(
    accepting_connections_channel_selection_algorithm_1,
    {
        'LL/CON/ADV/BV-07-C': (
            'Accepting Connections, Channel Selection Algorithm #1',
            {'extended': False},
        ),
        'LL/CON/ADV/BV-27-C': (
            (
                'Accepting Connections, Channel Selection Algorithm #1, '
                'Extended Advertising'
            ),
            {'extended': True},
        ),
    },
)


async def directed_advertising_connection_channel_selection_algorithm_1(
    tester, *, extended
):
    """Directed Advertising Connection, Channel Selection Algorithm #1.

    Initial Condition:
        Use high-duty directed advertising, public IUT and Lower Tester
        addresses, all primary channels, and an empty device name. Select the
        minimum advertising interval where applicable. Use common parameters:
        interval=8 (10 ms), WinSize=WinOffset=7, latency=0, timeout=30 (300 ms),
        CRCInit=0x198891, Hop=5, SCA category 0, and corrected ChM=0x03.
        LL/CON/ADV/BV-08-C uses legacy HCI advertising commands;
        LL/CON/ADV/BV-28-C uses extended HCI commands with legacy PDUs.

    Test Case Configuration:
        LL/CON/ADV/BV-08-C: legacy advertising.
        LL/CON/ADV/BV-28-C: extended advertising.

    Test Steps:
        1. Configure advertising through the command selected by Table 4.3-5,
           using all primary channels and the minimum applicable interval. Set
           advertising type 0x01, public own and peer addresses, the Lower
           Tester's public target address, and accept-all filter policy.
           The extended command uses legacy high-duty directed properties 0x001D.
        2. Enable advertising through the Table 4.3-5 command and require
           Command Complete success. On the extended HCI path, set Duration to
           1.28 s and Max_Extended_Advertising_Events=0.
        3. Receive ADV_DIRECT_IND with ChSel=1, public AdvA, and LE 1M PHY.
           Require TargetA to match the Lower Tester's public address.
           Check PDU length and CRC. Observe two complete primary-channel
           cycles and verify each channel's advertising interval.
        4. Respond on the same primary channel at T_IFS with CONNECT_IND,
           ChSel=0, public Lower Tester InitA, matching IUT AdvA, and the common
           connection parameters.
        5. Send correctly formatted DATA on the independently calculated CSA#1
           channel and require a valid acknowledgement and T_IFS response. The
           source allows up to 20 requests or until advertising stops; the ideal
           link takes the permitted early exit after the first successful request.
        6. Require Connection Complete with Peripheral role, the public peer
           identity, requested interval, latency, timeout, and clock accuracy.
           Retain the valid connection handle and maintain the unencrypted link.
        7. Only on the extended HCI command path, check the following substeps
           in either order:
           7A. Require one successful Advertising Set Terminated event with
               advertising handle 0 and the Step 6 connection handle.
           7B. If Channel Selection Algorithm is reported, require the Step 6
               handle and value 0 for Channel Selection Algorithm #1.
        8. Send a correctly formatted DATA PDU on the independently derived
           CSA#1 channel. Repeat Steps 8-10 for 100 additional connection events.
        9. Receive a correctly formatted DATA reply on the same channel using
           LE 1M. Check CRC, valid LLID, acknowledgement, and T_IFS spacing.
        10. Send subsequent DATA PDUs at the common connection interval on
            independently calculated CSA#1 channels. Scan all primary channels
            in free RF gaps and require advertising to have stopped.

        Cleanup: disconnect through HCI, require matching completion, and reject
        duplicate or late connection-related HCI events.

    Expected Outcome:
        Pass verdict:
            ADV_DIRECT_IND uses ChSel=1, the configured public addresses, and the
            permitted interval on all primary channels. CONNECT_IND ChSel=0
            establishes the requested Peripheral connection using CSA#1, not
            the algorithm advertised in ADV_DIRECT_IND's ChSel bit alone. Advertising
            stops, HCI reports the matching connection, and the first DATA
            exchange plus 100 subsequent events have valid channels, PHY, ARQ,
            and timing. Extended commands require Advertising Set Terminated
            and permit a correctly valued Channel Selection Algorithm event.
        Fail verdict:
            On the legacy HCI command path, either Advertising Set Terminated
            or Channel Selection Algorithm is an ineligible event and fails
            the trial, including during DATA exchange and cleanup.

    Source Correction:
        Use the reviewed local ChM=0x03 correction for the invalid one-channel
        common table; this is not a published SIG erratum. For the extended
        command path, translate the source's legacy Advertising_Type wording
        to the corresponding legacy-PDU Advertising_Event_Properties value.

    Reference:
        LL.TS.p28.pdf, Section 4.3.3.5, LL/CON/ADV/BV-08-C and
        LL/CON/ADV/BV-28-C; PDF pages 496-499, printed pages 4.3-23 through
        4.3-26. Table 4.3-5, Figure 4.3-10, Steps 1-10 including 7A/7B,
        and Expected Outcome; common tables on PDF pages 476-477. Core Vol 6,
        Part B, Sections 4.4.2 and 4.5.8 define acceptance and channel selection.
    """
    await _advertising_csa(tester, directed=True, csa2=False, extended=extended)


# Verify that an advertiser accepts a legacy CONNECT_IND with ChSel=0, stops
# advertising, and maintains a Peripheral connection using Channel Selection Algorithm
# #1. Check that connection-related HCI events match the legacy or extended command path
# selected by Table 4.3-5.
PROCEDURES |= procedure_cases(
    directed_advertising_connection_channel_selection_algorithm_1,
    {
        'LL/CON/ADV/BV-08-C': (
            'Directed Advertising Connection, Channel Selection Algorithm #1',
            {'extended': False},
        ),
        'LL/CON/ADV/BV-28-C': (
            (
                'Directed Advertising Connection, Channel Selection '
                'Algorithm #1, Extended Advertising'
            ),
            {'extended': True},
        ),
    },
)


async def accepting_connections_channel_selection_algorithm_2(tester, *, extended):
    """Accepting Connections, Channel Selection Algorithm #2.

    Initial Condition:
        Use undirected connectable advertising, public IUT and Lower Tester
        addresses, all primary channels, and an empty device name. Select the
        minimum advertising interval where applicable. Use common parameters:
        interval=8 (10 ms), WinSize=WinOffset=7, latency=0, timeout=30 (300 ms),
        CRCInit=0x198891, Hop=5, SCA category 0, and corrected ChM=0x03.
        LL/CON/ADV/BV-09-C uses legacy HCI advertising commands;
        LL/CON/ADV/BV-29-C uses extended HCI commands with legacy PDUs.

    Test Case Configuration:
        LL/CON/ADV/BV-09-C: legacy advertising.
        LL/CON/ADV/BV-29-C: extended advertising.

    Test Steps:
        1. Configure advertising through the command selected by Table 4.3-6,
           using all primary channels and the minimum applicable interval. Set
           advertising type 0x00, public own address, and accept-all filter
           policy. The extended command uses legacy ADV_IND properties 0x0013.
        2. Enable advertising through the Table 4.3-6 command and require
           Command Complete success. On the extended HCI path, set Duration=0
           and Max_Extended_Advertising_Events=0.
        3. Receive ADV_IND with ChSel=1, public AdvA, and LE 1M PHY.
           Check PDU length and CRC. Observe two complete primary-channel
           cycles and verify each channel's advertising interval.
        4. Respond on the same primary channel at T_IFS with CONNECT_IND,
           ChSel=1, public Lower Tester InitA, matching IUT AdvA, and the common
           connection parameters.
        5. Send correctly formatted DATA on the independently calculated CSA#2
           channel and require a valid acknowledgement and T_IFS response. The
           source allows up to 20 requests or until advertising stops; the ideal
           link takes the permitted early exit after the first successful request.
        6. Require Connection Complete with Peripheral role, the public peer
           identity, requested interval, latency, timeout, and clock accuracy.
           Retain the valid connection handle and maintain the unencrypted link.
        7. Only on the extended HCI command path, check the following substeps
           in either order:
           7A. Require one successful Advertising Set Terminated event with
               advertising handle 0 and the Step 6 connection handle.
           7B. If Channel Selection Algorithm is reported, require the Step 6
               handle and value 1 for Channel Selection Algorithm #2.
        8. Send a correctly formatted DATA PDU on the independently derived
           CSA#2 channel. Repeat Steps 8-10 for 100 additional connection events.
        9. Receive a correctly formatted DATA reply on the same channel using
           LE 1M. Check CRC, valid LLID, acknowledgement, and T_IFS spacing.
        10. Send subsequent DATA PDUs at the common connection interval on
            independently calculated CSA#2 channels. Scan all primary channels
            in free RF gaps and require advertising to have stopped.

        Cleanup: disconnect through HCI, require matching completion, and reject
        duplicate or late connection-related HCI events.

    Expected Outcome:
        Pass verdict:
            ADV_IND uses ChSel=1, the configured public addresses, and the
            permitted interval on all primary channels. CONNECT_IND ChSel=1
            establishes the requested Peripheral connection using CSA#2.
            Advertising stops, HCI reports the matching connection, and the first DATA
            exchange plus 100 subsequent events have valid channels, PHY, ARQ,
            and timing. Extended commands require Advertising Set Terminated
            and permit a correctly valued Channel Selection Algorithm event.
        Fail verdict:
            On the legacy HCI command path, either Advertising Set Terminated
            or Channel Selection Algorithm is an ineligible event and fails
            the trial, including during DATA exchange and cleanup.

    Source Correction:
        Use the reviewed local ChM=0x03 correction for the invalid one-channel
        common table; this is not a published SIG erratum. For the extended
        command path, translate the source's legacy Advertising_Type wording
        to the corresponding legacy-PDU Advertising_Event_Properties value.

    Reference:
        LL.TS.p28.pdf, Section 4.3.3.6, LL/CON/ADV/BV-09-C and
        LL/CON/ADV/BV-29-C; PDF pages 500-502, printed pages 4.3-27 through
        4.3-29. Table 4.3-6, Figure 4.3-11, Steps 1-10 including 7A/7B,
        and Expected Outcome; common tables on PDF pages 476-477. Core Vol 6,
        Part B, Sections 4.4.2 and 4.5.8 define acceptance and channel selection.
    """
    await _advertising_csa(tester, directed=False, csa2=True, extended=extended)


# Verify that an advertiser accepts a legacy CONNECT_IND with ChSel=1, stops
# advertising, and maintains a Peripheral connection using Channel Selection Algorithm
# #2. Check that connection-related HCI events match the legacy or extended command path
# selected by Table 4.3-6.
PROCEDURES |= procedure_cases(
    accepting_connections_channel_selection_algorithm_2,
    {
        'LL/CON/ADV/BV-09-C': (
            'Accepting Connections, Channel Selection Algorithm #2',
            {'extended': False},
        ),
        'LL/CON/ADV/BV-29-C': (
            (
                'Accepting Connections, Channel Selection Algorithm #2, '
                'Extended Advertising'
            ),
            {'extended': True},
        ),
    },
)


async def directed_advertising_connection_channel_selection_algorithm_2(
    tester, *, extended
):
    """Directed Advertising Connection, Channel Selection Algorithm #2.

    Initial Condition:
        Use high-duty directed advertising, public IUT and Lower Tester
        addresses, all primary channels, and an empty device name. Select the
        minimum advertising interval where applicable. Use common parameters:
        interval=8 (10 ms), WinSize=WinOffset=7, latency=0, timeout=30 (300 ms),
        CRCInit=0x198891, Hop=5, SCA category 0, and corrected ChM=0x03.
        LL/CON/ADV/BV-10-C uses legacy HCI advertising commands;
        LL/CON/ADV/BV-30-C uses extended HCI commands with legacy PDUs.

    Test Case Configuration:
        LL/CON/ADV/BV-10-C: legacy advertising.
        LL/CON/ADV/BV-30-C: extended advertising.

    Test Steps:
        1. Configure advertising through the command selected by Table 4.3-7,
           using all primary channels and the minimum applicable interval. Set
           advertising type 0x01, public own and peer addresses, the Lower
           Tester's public target address, and accept-all filter policy.
           The extended command uses legacy high-duty directed properties 0x001D.
        2. Enable advertising through the Table 4.3-7 command and require
           Command Complete success. On the extended HCI path, set Duration to
           1.28 s and Max_Extended_Advertising_Events=0.
        3. Receive ADV_DIRECT_IND with ChSel=1, public AdvA, and LE 1M PHY.
           Require TargetA to match the Lower Tester's public address.
           Check PDU length and CRC. Observe two complete primary-channel
           cycles and verify each channel's advertising interval.
        4. Respond on the same primary channel at T_IFS with CONNECT_IND,
           ChSel=1, public Lower Tester InitA, matching IUT AdvA, and the common
           connection parameters.
        5. Send correctly formatted DATA on the independently calculated CSA#2
           channel and require a valid acknowledgement and T_IFS response. The
           source allows up to 20 requests or until advertising stops; the ideal
           link takes the permitted early exit after the first successful request.
        6. Require Connection Complete with Peripheral role, the public peer
           identity, requested interval, latency, timeout, and clock accuracy.
           Retain the valid connection handle and maintain the unencrypted link.
        7. Only on the extended HCI command path, check the following substeps
           in either order:
           7A. Require one successful Advertising Set Terminated event with
               advertising handle 0 and the Step 6 connection handle.
           7B. If Channel Selection Algorithm is reported, require the Step 6
               handle and value 1 for Channel Selection Algorithm #2.
        8. Send a correctly formatted DATA PDU on the independently derived
           CSA#2 channel. Repeat Steps 8-10 for 100 additional connection events.
        9. Receive a correctly formatted DATA reply on the same channel using
           LE 1M. Check CRC, valid LLID, acknowledgement, and T_IFS spacing.
        10. Send subsequent DATA PDUs at the common connection interval on
            independently calculated CSA#2 channels. Scan all primary channels
            in free RF gaps and require advertising to have stopped.

        Cleanup: disconnect through HCI, require matching completion, and reject
        duplicate or late connection-related HCI events.

    Expected Outcome:
        Pass verdict:
            ADV_DIRECT_IND uses ChSel=1, the configured public addresses, and the
            permitted interval on all primary channels. CONNECT_IND ChSel=1
            establishes the requested Peripheral connection using CSA#2.
            Advertising stops, HCI reports the matching connection, and the first DATA
            exchange plus 100 subsequent events have valid channels, PHY, ARQ,
            and timing. Extended commands require Advertising Set Terminated
            and permit a correctly valued Channel Selection Algorithm event.
        Fail verdict:
            On the legacy HCI command path, either Advertising Set Terminated
            or Channel Selection Algorithm is an ineligible event and fails
            the trial, including during DATA exchange and cleanup.

    Source Correction:
        Use the reviewed local ChM=0x03 correction for the invalid one-channel
        common table; this is not a published SIG erratum. For the extended
        command path, translate the source's legacy Advertising_Type wording
        to the corresponding legacy-PDU Advertising_Event_Properties value.

    Reference:
        LL.TS.p28.pdf, Section 4.3.3.7, LL/CON/ADV/BV-10-C and
        LL/CON/ADV/BV-30-C; PDF pages 502-505, printed pages 4.3-29 through
        4.3-32. Table 4.3-7, Figure 4.3-12, Steps 1-10 including 7A/7B,
        and Expected Outcome; common tables on PDF pages 476-477. Core Vol 6,
        Part B, Sections 4.4.2 and 4.5.8 define acceptance and channel selection.
    """
    await _advertising_csa(tester, directed=True, csa2=True, extended=extended)


# Verify that an advertiser accepts a legacy CONNECT_IND with ChSel=1, stops
# advertising, and maintains a Peripheral connection using Channel Selection Algorithm
# #2. Check that connection-related HCI events match the legacy or extended command path
# selected by Table 4.3-7.
PROCEDURES |= procedure_cases(
    directed_advertising_connection_channel_selection_algorithm_2,
    {
        'LL/CON/ADV/BV-10-C': (
            'Directed Advertising Connection, Channel Selection Algorithm #2',
            {'extended': False},
        ),
        'LL/CON/ADV/BV-30-C': (
            (
                'Directed Advertising Connection, Channel Selection '
                'Algorithm #2, Extended Advertising'
            ),
            {'extended': True},
        ),
    },
)


async def connection_supervision_timeout_during_fail_connection_setup(tester):
    """Connection Supervision Timeout during Fail Connection Setup.

    Initial Condition:
        Use undirected legacy advertising, public addresses, the minimum
        advertising interval, all primary channels, and empty advertising data.
        Filter scan and connection requests through a Filter Accept List
        containing only the Lower Tester's public address. Use common parameters:
        interval=8 (10 ms), WinSize=WinOffset=7, latency=0, timeout=30 (300 ms),
        CRCInit=0x198891, Hop=5, SCA category 0, and corrected ChM=0x03.
        The Lower Tester observes primary channel 37 and independently hops
        data channels using Channel Selection Algorithm #1.

    Test Steps:
        1. Configure the Lower Tester to initiate with the common connection
           parameters and send DATA with deliberately inverted CRC bits from
           the first connection event onward. No valid DATA is sent beforehand.
        2. Enable undirected advertising on all primary channels, with public
           own address and filtering for both scan and connection requests.
        3. Receive a correctly formatted ADV_IND on the selected channel, 37,
           and validate CRC, PDU length, public AdvA, and LE 1M PHY.
        4. Send CONNECT_IND on that channel at T_IFS after ADV_IND, using public
           InitA/AdvA and the common connection parameters.
        5. Require Connection Complete with Peripheral role, public peer identity,
           the requested interval, latency, supervision timeout and clock accuracy.
           Retain its valid connection handle.
        6. Starting one common connection interval after CONNECT_IND ends, send
           correctly formatted DATA with an invalid CRC on independently derived
           CSA#1 channels. Repeat Steps 6-7 until supervision expires; never send
           CRC-valid DATA that could reset the supervision timer.
        7. Observe the IUT's correctly formatted reply on the same channel at
           T_IFS, without acknowledgement of the invalid DATA. Count all sent
           events and valid replies. Scan primary channels in free gaps to check
           that advertising has stopped.
        8. Require successful Disconnection Complete at the original 300-ms
           supervision deadline, with the Step 5 handle and Connection Timeout
           reason 0x08. Reject earlier termination or establishment failure 0x3E.

        Cleanup: probe the next receive window to verify RF has stopped, forbid
        unexpected connection-related HCI events and delivery of invalid DATA
        to the Host.

    Expected Outcome:
        Pass verdict:
            At least 95% of the invalid-CRC DATA transmissions receive valid,
            correctly timed, nonacknowledging DATA replies. Invalid DATA can
            establish the connection but cannot reset its supervision timer.
            Disconnection Complete reports Status=0x00, the correct handle and
            Reason=0x08 at the original supervision deadline, within the HCI
            observation resolution of 1 ms.

    Source Correction:
        Step 8 and the pass verdict place 0x08 in Disconnection Complete Status.
        Interpret it as the Reason field; Status is 0x00 for successful completion.
        Use the reviewed local ChM=0x03 correction for the invalid one-channel
        common table. These are local corrections, not published SIG errata.

    Reference:
        LL.TS.p28.pdf, LL/CON/ADV/BI-01-C, independently titled procedure after
        Section 4.3.3.7; PDF pages 506-507, printed pages 4.3-33 through 4.3-34.
        Figure 4.3-13, Steps 1-8 and Expected Outcome; common tables on PDF
        pages 476-477. Core Vol 6, Part B, Sections 4.5 and 4.5.2 distinguish
        establishment from supervision timer reset; Vol 4, Part E defines
        Disconnection Complete Status and Reason.
    """
    peer = LOWER_CENTRAL_PUBLIC_ADDRESS
    await tester.command(hci.HCI_LE_Clear_Filter_Accept_List_Command())
    await tester.command(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(peer.address_type, peer)
    )
    parameters = _advertiser_connection_parameters()
    link, end = await _pending_connection(
        tester,
        central=False,
        parameters=parameters,
        advertising_filter_policy=ADV_FILTER_ACCEPT_LIST_ONLY,
        advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
    )
    assert 0 <= link.handle <= CONNECTION_HANDLE_MAX
    _completion_events(tester, link)
    deadline = end + parameters.timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
    total = replies = 0
    while link.anchor < deadline:
        duration = link.anchor - tester.radio.now_us - CONNECT_IFS_TOLERANCE_US
        if duration > 0:
            channel = LL_ADV_PRIMARY_CHANNELS[total % len(LL_ADV_PRIMARY_CHANNELS)]
            assert await tester.receive(channel, duration_us=duration) is None
        packet, _ = await _wire_event(
            link, lambda _: _data_pdu(0, 0), bad_crc=True, required=False
        )
        total += 1
        if packet:
            assert packet.phy == Phy.LE_1M
            _empty_data(packet, (0, 0))
            replies += 1
    assert total > 0 and replies * 100 >= total * 95
    event = await _observe_disconnection(
        tester, deadline - HCI_OBSERVATION_STEP_US, deadline + HCI_OBSERVATION_STEP_US
    )
    assert (event.status, event.connection_handle, event.reason) == (
        ErrorCode.SUCCESS,
        link.handle,
        ErrorCode.CONNECTION_TIMEOUT_ERROR,
    )
    await _check_rf_stopped(link)
    _no_connection_events(tester)
    assert not any(isinstance(event, hci.HCI_AclDataPacket) for event in tester.events)


# Verify that a Peripheral establishes a connection after receiving DATA with the
# correct Access Address but an invalid CRC, does not acknowledge that invalid DATA, and
# terminates at the original supervision deadline.
PROCEDURES |= procedure_cases(
    connection_supervision_timeout_during_fail_connection_setup,
    {
        'LL/CON/ADV/BI-01-C': (
            'Connection Supervision Timeout during Fail Connection Setup',
            {},
        ),
    },
)


async def reject_existing_connection_request(tester, *, privacy=False):
    """Reject Existing Connection Request.

    Initial Condition:
        Use legacy undirected advertising on the first primary channel, 37,
        with public own and Lower Tester addresses, selected 20-ms base interval,
        and the common IUT name as advertising data. Filter scan and connection
        requests through a Filter Accept List containing only the public peer.
        Select a 50-ms connection interval and a 1-s supervision timeout. Enable
        legacy Connection Complete while leaving forbidden CSA and advertising
        termination events observable.

    Test Steps:
        1. Configure the Lower Tester as a public initiator for the original
           connection, using a 50-ms connection interval.
        2. Enable undirected advertising on primary channel 37 with filtering
           for both scan and connection requests.
        3. Observe ADV_IND on channel 37. Check CRC, PDU length, public AdvA,
           common IUT name, and two consecutive advertising starts separated by
           the selected interval plus the permitted AdvDelay.
        4. Send CONNECT_IND at T_IFS after ADV_IND, with the public Lower Tester
           InitA, matching AdvA and a 50-ms connection interval.
        5. Require Connection Complete with the requested Peripheral role,
           public identity, interval, latency, timeout and clock accuracy;
           retain its valid connection handle.
        6. Maintain the original ACL by transmitting empty DATA and checking
           correctly formatted, positively acknowledged empty replies at T_IFS.
        7. Re-enable undirected advertising on channel 37 while maintaining
           the original ACL.
        8. Receive correctly formatted ADV_IND with the same public address
           and IUT name while servicing the existing connection events.
        9. Send another CONNECT_IND at T_IFS, using the same public peer but a
           distinct valid Access Address. Select interval=50 ms and WinSize=5 ms.
           Choose the representable WinOffset closest to a window start 25 ms
           after an original connection anchor, in units of 1.25 ms.
        10. Require no additional Connection Complete for the duplicate request.
        11. Require continued correctly formatted ADV_IND on channel 37.
        12. Send empty DATA for both event streams, each using its own Access
            Address, CRCInit, channel sequence and independently scheduled anchor.
        13. Require correctly timed, acknowledged empty replies only on the
            original ACL; require no response on any duplicate connection event.
        14. Continue Steps 12-13 for at least five seconds after the duplicate
            CONNECT_IND. Observe advertising throughout, with no additional
            connection completion, unexpected disconnection or ineligible event.

        Cleanup: disable advertising and terminate the original connection
        through HCI. Require matching completion and no late creation events.

    Expected Outcome:
        Pass verdict:
            The duplicate CONNECT_IND produces neither another Connection Complete
            nor a DATA reply on its candidate ACL. The IUT continues advertising
            and correctly acknowledges empty DATA on the original connection for
            at least five seconds while both independent streams are exercised.
        Fail verdict:
            Any DATA reply on the duplicate connection, extra connection event,
            unexpected termination, loss of the original ACL or cessation of
            advertising fails the trial.

    Source Correction:
        The first pass-verdict bullet references DATA sent in Step 11, which
        describes advertising. Interpret this as the original-ACL DATA in Step 12,
        answered in Step 13. This is a local editorial correction, not a
        published SIG erratum.

    Reference:
        LL.TS.p28.pdf, LL/CON/ADV/BI-02-C, independently titled procedure after
        Section 4.3.3.8; PDF pages 512-514, printed pages 4.3-39 through 4.3-41.
        Figure 4.3-17, Steps 1-14 and Expected Outcome. Core Vol 6, Part B,
        Section 4.5 specifies connection handling for an already connected peer.
    """
    await check_duplicate_advertiser_request(tester, privacy=privacy)


# Verify that a Peripheral rejects a second connection request from an already connected
# public peer, continues advertising, and maintains only the original ACL connection
# while both candidate event streams are probed.
PROCEDURES |= procedure_cases(
    reject_existing_connection_request,
    {
        'LL/CON/ADV/BI-02-C': (
            'Reject Existing Connection Request',
            {},
        ),
    },
)


async def reject_connection_request_when_connected_to_the_peer_extended_advertising(
    tester,
):
    """Reject Connection Request when Connected to the Peer, Extended Advertising.

    Initial Condition:
        Use public IUT and Lower Tester addresses, all primary channels, LE 1M
        primary/secondary PHYs, the minimum 20-ms base advertising interval,
        empty advertising data, and accept-all filtering. Select connectable
        undirected extended advertising properties 0x0001, a 30-ms connection
        interval, zero Peripheral latency, and a 1-s supervision timeout.

    Test Steps:
        1. Configure extended advertising handle 0, properties 0x0001, public own
           address, all primary channels, minimum advertising interval and
           filter policy 0x00. Require successful Command Complete.
        2. Enable the set with Duration=0 and Max_Extended_Advertising_Events=0;
           require successful Command Complete.
        3. Receive ADV_EXT_IND with connectable AdvMode, AuxPtr, and mandatory
           ADI. The source's missing ADI is corrected as described below.
        4. Follow AuxPtr to AUX_ADV_IND. Check connectable AdvMode, public AdvA,
           matching ADI, the pointed channel/PHY/offset, and T_MAFS spacing.
           Respond with AUX_CONNECT_REQ at T_IFS using the public Lower Tester
           InitA and matching IUT AdvA. The source's directed-address condition
           does not apply to the selected undirected properties 0x0001.
        5. Require AUX_CONNECT_RSP on that secondary channel at T_IFS within
           the 2-us tolerance, with valid CRC/length, correct public addresses,
           LE 1M and nonconnectable/nonscannable AdvMode.
        6. Require Connection Complete with Peripheral role, public peer identity,
           requested interval, latency, timeout, clock accuracy and valid handle.
        7. Exchange correctly formatted, acknowledged DATA using independently
           calculated CSA#2 data channels.
        8. Require one successful Advertising Set Terminated for the enabled set
           and reported connection handle. This event can already be queued
           immediately after Connection Complete. If CSA is reported, check #2.
        9. Re-enable the same set with Duration=0 and maximum event count 0;
           require successful Command Complete while maintaining the original ACL.
        10. Receive ADV_EXT_IND and AUX_ADV_IND as in Steps 3-4, servicing the
            original connection in RF gaps.
        11. Respond with another AUX_CONNECT_REQ from the same public peer,
            using a distinct valid Access Address for the candidate connection.
        12. Require no AUX_CONNECT_RSP in its response window and no additional
            connection-related HCI events.
        13. Continue acknowledged, correctly timed DATA on the original CSA#2
            connection for ten selected observation events. Require advertising
            to continue and reject duplicate or late connection-related events.

        Cleanup: disable advertising and disconnect the original ACL through
        HCI, checking the corresponding completion and absence of extra events.

    Expected Outcome:
        Pass verdict:
            The duplicate request receives no AUX_CONNECT_RSP and creates no
            additional connection. The original ACL continues exchanging valid,
            acknowledged DATA on its original channel sequence and schedule.

    Source Correction:
        Step 3 requires only AuxPtr, but Core Vol 6, Part B, Section 2.3.1.5,
        Table 2.4 requires both ADI and AuxPtr for connectable ADV_EXT_IND.
        Retain both mandatory fields. Step 5 attributes transmission of
        AUX_CONNECT_REQ to the IUT; the Lower Tester sends it in Step 4.
        These are local editorial corrections, not published SIG errata.

    Reference:
        LL.TS.p28.pdf, LL/CON/ADV/BV-26-C, independently titled procedure after
        Section 4.3.3.9; PDF pages 517-520, printed pages 4.3-44 through 4.3-47.
        Figures 4.3-19/20, Steps 1-13 and Expected Outcome. Core Vol 6, Part B,
        Sections 2.3.1.5 and 4.4.2.7 define mandatory header fields and rejection
        of connection requests from an already connected initiator.
    """
    peer = LOWER_CENTRAL_PUBLIC_ADDRESS
    await tester.command(
        extended_parameters(
            advertising_event_properties=Properties.CONNECTABLE_ADVERTISING,
            primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
        )
    )
    await tester.command(extended_enable())
    channel, advertisement = await _connectable_advertisement(tester)
    parameters = LinkParameters(access_address=LOWER_DATA_ACCESS_ADDRESS)
    end = await tester.transmit(
        channel,
        request_pdu(
            AdvertisingPduType.CONNECT_IND, scanner=peer, data=parameters.encode()
        ),
        start_us=advertisement.end_us + LL_IFS_US,
    )
    response = await tester.expect_pdu(
        channel, kind=AdvertisingPduType.AUX_CONNECT_RSP, duration_us=1000
    )
    assert abs(response.start_us - end - LL_IFS_US) <= CONNECT_IFS_TOLERANCE_US
    assert response.phy == Phy.LE_1M
    assert not response.pdu[0] & (PDU_ADV_TXADD_MASK | PDU_ADV_RXADD_MASK)
    assert (
        response.pdu[PDU_HEADER_SIZE] >> PDU_ADV_EXT_HEADER_LENGTH_BITS
        == AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
    )
    assert decode_extended(response.pdu) == {
        'address': bytes(DUT_ADDRESS),
        'target': bytes(peer),
        'data': b'',
    }
    link = PeerLink(tester, parameters, end, csa2=True)
    link.anchor = end + LL_CONN_AUX_WINDOW_DELAY_UNCODED_US
    await _connection_complete(tester, link, peer, extended=True)
    await _data_events(tester, link, 1, observe_advertising=True)
    await tester.command(extended_enable())
    channel, advertisement = await _connectable_advertisement(tester, link)
    other_parameters = LinkParameters()
    assert other_parameters.access_address != parameters.access_address
    check_access_address(other_parameters.access_address)
    await tester.transmit(
        channel,
        request_pdu(
            AdvertisingPduType.CONNECT_IND, scanner=peer, data=other_parameters.encode()
        ),
        start_us=advertisement.end_us + LL_IFS_US,
    )
    assert (
        await tester.receive(
            channel,
            duration_us=LL_IFS_US + Phy.LE_1M.address_us + CONNECT_IFS_TOLERANCE_US,
        )
        is None
    )
    start_counter = link.counter
    while link.counter - start_counter < MAINTAINED_EVENTS:
        await _service_peer([link])
        _no_creation_events(tester)
    await _connectable_advertisement(tester, link)
    _no_creation_events(tester)
    await tester.command(extended_enable(0))
    await link.disconnect()

    _no_creation_events(tester)


# Verify that an extended advertiser rejects an AUX_CONNECT_REQ from an already
# connected peer without sending AUX_CONNECT_RSP, while the original ACL connection
# continues exchanging correctly formatted DATA.
PROCEDURES |= procedure_cases(
    reject_connection_request_when_connected_to_the_peer_extended_advertising,
    {
        'LL/CON/ADV/BV-26-C': (
            (
                'Reject Connection Request when Connected to the Peer, '
                'Extended Advertising'
            ),
            {},
        ),
    },
)


async def connection_initiation(tester):
    """Connection Initiation.

    Initial Condition:
        Start from Standby with public own/peer addresses, no Filter Accept List
        filtering, and all required HCI events enabled. Select a 30-ms scan
        interval/window, 20-ms advertising base interval, 30-ms connection interval,
        zero Peripheral latency and 1-s supervision timeout under Section 4.1.6.
        The 30-ms connection interval matches the timing example in Table 4.3-12.
        LL/CON/INI/BV-36-C selects the CSA-event-supported row of Table 4.3-11.

    Test Steps:
        1. Configure the Lower Tester to advertise on channel 37 using the common
           public Peripheral address.
        2. Start unfiltered initiation to that public peer through HCI and require
           successful Command Status.
        3. Issue LE Set Random Address while initiation is active; require Command
           Complete with Command Disallowed status 0x0C.
        4. Send ADV_IND with ChSel=0 on the selected channel at the selected
           advertising cadence plus permitted AdvDelay. Stop on CONNECT_IND,
           or fail after 4 * scanInterval + 3 * scanWindow.
        5. Receive CONNECT_IND at T_IFS within 2 us. Check CRC, length, public
           InitA/AdvA, requested interval/latency/timeout and legal Access Address.
        6. Require successful Connection Complete with Central role, matching
           public peer identity, requested connection parameters and valid handle.
        7. Require the Table 4.3-11 CSA event with that handle and algorithm #1,
           after Connection Complete.
        8. Receive the first correctly formatted DATA within the transmit window,
           including the Section 4.3.4 clock-accuracy and jitter bounds; do not
           require a single predetermined timestamp within that legal window.
        9. Reply on the same data channel at T_IFS with correct acknowledgement.
        10. Receive subsequent DATA on independently derived CSA#1 channels at
            the requested interval and reply with correct acknowledgements.
        11. Repeat until at least 100 event starts are measured. Check each
            interval and cumulative drift against Table 4.3-12's SCA limits.
        12. Terminate the connection through HCI using the Step 6 handle; require
            matching completion and no duplicate or late connection-related event.
        13. Select channel 37 and a public peer differing from the IUT address in
            the most significant octet, using the Section 4.1.5.2 XOR mask.
        14. Repeat Steps 2-12.
        15. Select channel 37 and a public peer differing in the least significant
            octet using the specified XOR mask.
        16. Repeat Steps 2-12.
        17. Select channel 37 and a public peer differing in both end octets using
            the specified XOR mask.
        18. Repeat Steps 2-12.
        19. Select channel 38 and the common public Peripheral address.
        20. Repeat Steps 2-12.
        21. Select channel 38 and the most-significant-octet address variant.
        22. Repeat Steps 2-12.
        23. Select channel 38 and the least-significant-octet address variant.
        24. Repeat Steps 2-12.
        25. Select channel 38 and the both-end-octets address variant.
        26. Repeat Steps 2-12.
        27. Select channel 39 and the most-significant-octet address variant.
        28. Repeat Steps 2-12.
        29. Select channel 39 and the least-significant-octet address variant.
        30. Repeat Steps 2-12.
        31. Select channel 39 and the both-end-octets address variant.
        32. Repeat Steps 2-12.

    Expected Outcome:
        Pass verdict:
            Every prescribed channel/address combination produces a correctly timed
            CONNECT_IND and the requested Central connection. Set Random Address
            is rejected while initiating. HCI reports the correct connection and
            CSA#1. The first DATA lies in the legal transmit window, at least 100
            event starts are measured per round, acknowledgements are valid, and
            individual intervals and cumulative drift meet the advertised SCA.
        Fail verdict:
            A connection, HCI field/order, Access Address, acknowledgement or timing
            requirement is unmet. Table 4.3-11's forbidden-CSA branch does not apply
            to the selected BV-36-C supported-event variant.

    Coverage Note:
        The source omits the channel-39/common-public-address round from its
        numbered steps although the verdict covers each channel and address.
        Exercise that additional combination without renumbering the source steps.
        The nominal 30-ms interval and 1-s timeout are explicit selected IXIT
        values under Section 4.1.6, not a decoding of the group packet examples.

    Reference:
        LL.TS.p28.pdf, Section 4.3.4.1, LL/CON/INI/BV-36-C; PDF pages 521-524,
        printed pages 4.3-48 through 4.3-51. Tables 4.3-11/12, Figure 4.3-21,
        Steps 1-32, Expected Outcome and Notes. Section 4.1.5.2 on PDF page 52
        defines the XOR address variants; Section 4.1.6 on PDF page 53 permits
        selected IXIT timings. Section 4.3.4 on PDF page 520 defines the first
        transmit-window and interval formulas.
    """
    for channel in LL_ADV_PRIMARY_CHANNELS:
        for peer in _peer_addresses():
            link = await _initiate(tester, peer, channel=channel, change_address=True)
            await _central_timing_events(tester, link, TIMING_EVENTS)
            _no_connection_events(tester)
            await link.disconnect()
            _no_connection_events(tester)


# Verify initiation with public peers on every primary advertising channel, rejection of
# Set Random Address while initiating, and a maintained CSA#1 Central connection with
# the required first-event and clock accuracy.
PROCEDURES |= procedure_cases(
    connection_initiation,
    {
        'LL/CON/INI/BV-36-C': (
            'Connection Initiation, HCI LE Channel Selection Algorithm supported',
            {},
        ),
    },
)


async def connecting_to_directed_advertising(tester):
    """Connecting to Directed Advertising.

    Initial Condition:
        Start from Standby with public own and common Peripheral addresses,
        no Filter Accept List filtering, and required HCI events enabled. Select
        a 30-ms scan interval/window, 30-ms connection interval, zero Peripheral
        latency and 1-s supervision timeout under Section 4.1.6.
        BV-37-C uses the CSA-event-supported row of Table 4.3-13.

    Test Steps:
        1. Configure the Lower Tester to advertise on all primary channels with
           the common public Peripheral address and at most 500 us between
           adjacent packet starts. Target the IUT's public address.
        2. Start unfiltered initiation through HCI and require successful
           Command Status.
        3. Send correctly formatted ADV_DIRECT_IND with ChSel=0. Continue until
           CONNECT_IND arrives or 4 * scanInterval + 3 * scanWindow expires.
        4. Receive CONNECT_IND at T_IFS within 2 us on the selected channel.
           Validate CRC, length, public InitA/AdvA, requested parameters and
           legal Access Address.
        5. Require Connection Complete with Central role, matching public peer
           identity, requested interval/latency/timeout and a valid handle.
        6. Require the supported CSA event after Connection Complete, with the
           Step 5 handle and algorithm #1 (value 0x00).
        7. Receive the first correctly formatted DATA within its legal transmit
           window, including clock-accuracy and jitter bounds.
        8. Reply on that channel at T_IFS with a correct acknowledgement.
        9. Receive subsequent DATA on independently derived CSA#1 channels at
           the requested interval; reply with correct acknowledgements.
        10. Continue for at least 1000 measured connection events. Check each
            interval and cumulative clock drift against the Central's SCA.
        11. Terminate through HCI using the Step 5 handle, require matching
            completion, and reject duplicate or late connection-related events.

        Repeat Steps 1-11 with advertising ChSel=1. In Step 6 require algorithm
        #2 (value 0x01), and use independently calculated CSA#2 channels in
        Steps 7-10. Measure at least 1000 events in this second round as well.

    Expected Outcome:
        Pass verdict:
            Directed advertising produces the requested Central connection in
            both rounds. HCI reports the correct identity, parameters and CSA.
            The first event is inside its legal transmit window, Access Addresses
            are valid, and at least 1000 correctly acknowledged events per round
            follow CSA#1 then CSA#2, with valid intervals and cumulative drift.
        Fail verdict:
            A request, connection, HCI field/order, Access Address, acknowledgement
            or timing requirement fails. Table 4.3-13's forbidden-CSA branch does
            not apply to the selected BV-37-C supported-event variant.

    Reference:
        LL.TS.p28.pdf, Section 4.3.4.2, LL/CON/INI/BV-37-C; PDF pages 525-527,
        printed pages 4.3-52 through 4.3-54. Table 4.3-13, Figure 4.3-22,
        Steps 1-11, the repeated ChSel=1 round and Expected Outcome. Section
        4.1.6 on PDF page 53 permits selected IXIT timings; Section 4.3.4 on
        PDF page 520 defines first-window and interval accuracy bounds.
    """
    for csa2 in (False, True):
        link = await _initiate(
            tester, LOWER_PERIPHERAL_PUBLIC_ADDRESS, directed=True, csa2=csa2
        )
        await _central_timing_events(tester, link, DIRECTED_TIMING_EVENTS)
        _no_connection_events(tester)
        await link.disconnect()
        _no_connection_events(tester)


# Verify initiation to directed advertising and maintained Central DATA exchange, first
# using CSA#1 and then CSA#2, with correctly valued HCI events, legal first-event timing
# and clock-accuracy bounds.
PROCEDURES |= procedure_cases(
    connecting_to_directed_advertising,
    {
        'LL/CON/INI/BV-37-C': (
            (
                'Connecting to Directed Advertising, HCI LE Channel '
                'Selection Algorithm supported'
            ),
            {},
        ),
    },
)


async def connection_initiation_missed_replies(tester):
    """Connection Initiation Missed Replies.

    Initial Condition:
        Use the common public Peripheral address on selected primary channel 37,
        public IUT own address, accept-all initiation and a 30-ms scan interval
        and window. Select a 30-ms connection interval, zero Peripheral latency,
        and 100-ms supervision timeout under Section 4.1.6. Half that timeout
        precedes the six-event setup deadline. Enable Connection Complete and the
        supported CSA event selected by Table 4.3-14 for BV-38-C.

    Test Steps:
        1. Configure the Lower Tester as an advertiser on channel 37 with the
           common public Peripheral address and selected advertising data.
        2. Enable unfiltered initiation through HCI; require successful
           Command Status and the selected connection parameters.
        3. Send ADV_IND on channel 37 at the selected cadence until CONNECT_IND
           arrives or 4 * scanInterval + 3 * scanWindow expires. The ideal link
           succeeds on the first permitted advertising attempt.
        4. Receive CONNECT_IND at T_IFS, checking CRC, length, public addresses,
           legal Access Address and requested interval/latency/timeout.
        5. Require Connection Complete with Central role, the public peer,
           selected connection parameters and a valid handle.
        6. Require the supported CSA event with the Step 5 handle and algorithm
           #1, after Connection Complete.
        7. Receive the first correctly formatted DATA on its independently
           calculated channel, inside the legal first transmit window with
           clock-accuracy and jitter allowances.
        8. Do not reply initially. Continue receiving DATA on subsequent CSA#1
           channels at the requested interval until half the 100-ms supervision
           duration has elapsed since CONNECT_IND. Check unchanged SN/NESN=0,
           interval timing and clock drift while no reply is acknowledged.
        9. At the first following event, send correctly formatted DATA with a
           valid acknowledgement at T_IFS on the same channel.
        10. Continue for at least ten replied-to events, checking acknowledgements,
            PHY, channel sequence, per-event intervals and cumulative clock drift.
        11. Terminate through HCI using the Step 5 handle and require matching
            completion. Reject unexpected disconnection or late creation events.

    Expected Outcome:
        Pass verdict:
            The IUT continues transmitting before and after valid Peripheral
            replies resume, reports the requested connection, and uses a valid
            Access Address. The first event is inside its legal window; DATA
            remains correctly formatted and timed, initially unacknowledged and
            then correctly acknowledged for at least ten recovery events.
        Fail verdict:
            A required connection, HCI field/order, Access Address, DATA or timing
            condition fails. The forbidden-CSA alternative in Table 4.3-14 does
            not apply to the selected supported-event BV-38-C variant.

    Reference:
        LL.TS.p28.pdf, Section 4.3.4.3, LL/CON/INI/BV-38-C; PDF pages 527-529,
        printed pages 4.3-54 through 4.3-56. Table 4.3-14, Figure 4.3-23,
        Steps 1-11 and Expected Outcome. Section 4.1.6 on PDF page 53 permits
        selected IXIT values; Section 4.3.4 on PDF page 520 defines first-window
        and interval accuracy bounds.
    """
    link, end = await _pending_connection(
        tester,
        central=True,
        parameters=LinkParameters(timeout=LL_SUPERVISION_TIMEOUT_MIN),
    )
    _completion_events(tester, link)
    check_access_address(link.parameters.access_address)
    until = end + link.parameters.timeout * LL_SUPERVISION_TIMEOUT_UNIT_US // 2
    await _central_timing_events(tester, link, RECOVERY_EVENTS, silent_until_us=until)
    assert _disconnected(tester) is None
    _no_connection_events(tester)
    await link.disconnect()
    _no_connection_events(tester)


# Verify that an initiator continues Central transmissions while initial Peripheral
# replies are omitted, then establishes and maintains the connection when valid
# acknowledgements resume before setup expires.
PROCEDURES |= procedure_cases(
    connection_initiation_missed_replies,
    {
        'LL/CON/INI/BV-38-C': (
            (
                'Connection Initiation Missed Replies, HCI LE Channel '
                'Selection Algorithm supported'
            ),
            {},
        ),
    },
)


async def connection_initiation_timeout(tester):
    """Connection Initiation Timeout.

    Initial Condition:
        Use the common public Peripheral address on selected primary channel 37,
        public IUT own address and accept-all initiation. Select a 30-ms scan
        interval/window and 30-ms connection interval under Section 4.1.6.
        Set zero Peripheral latency and the prescribed 100-ms supervision
        timeout. BV-39-C selects the supported-CSA row of Table 4.3-15.

    Test Steps:
        1. Configure the Lower Tester to advertise on channel 37 using the
           common public address and a selected one-octet advertising payload.
        2. Start unfiltered initiation through HCI; require successful
           Command Status and the requested connection parameters.
        3. Send ADV_IND on that channel at the selected cadence until CONNECT_IND
           arrives or 4 * scanInterval + 3 * scanWindow expires. The ideal link
           succeeds on its first permitted advertising attempt.
        4. Receive CONNECT_IND at T_IFS. Check CRC, length, public addresses,
           legal Access Address, interval, zero latency and 100-ms timeout.
        5. Require Connection Complete with Central role, matching public peer,
           requested parameters and valid handle.
        6. Require the supported CSA event after Connection Complete, with the
           Step 5 handle and algorithm #1.
        7. Receive the first correctly formatted DATA inside its legal transmit
           window, including clock-accuracy and jitter bounds.
        8. Do not reply. Receive subsequent correctly formatted DATA on the
           independently calculated CSA#1 channels. Require SN/NESN to remain
           unchanged while nothing has been acknowledged.
        9. Repeat Step 8 until setup stops. Require every initial event whose
           anchor precedes six connection intervals after CONNECT_IND, no more
           than six events, and no later RF activity after the connection is lost.
        10. Require successful Disconnection Complete with the Step 5 handle and
            Connection Failed to be Established reason 0x3E at the setup deadline.

    Expected Outcome:
        Pass verdict:
            The IUT sends the requested CONNECT_IND, transmits every required
            initial event before the six-interval setup deadline, and sends no
            more than six events. It does not resume RF after setup is lost and
            reports the matching handle and reason 0x3E through HCI. The shorter
            configured 100-ms supervision value does not replace the unestablished
            connection's six-interval deadline.
        Fail verdict:
            A required setup event is omitted, DATA resumes after RF stops, the
            IUT exceeds the setup deadline, or a connection/HCI/packet/timing
            requirement fails. The forbidden-CSA row of Table 4.3-15 is not the
            selected BV-39-C variant.

    Reference:
        LL.TS.p28.pdf, Section 4.3.4.4, LL/CON/INI/BV-39-C; PDF pages 529-531,
        printed pages 4.3-56 through 4.3-58. Table 4.3-15, Figure 4.3-24,
        Steps 1-10 and Expected Outcome. Section 4.3.4 on PDF page 520 defines
        first-window and interval bounds. Core Vol 6, Part B, Section 4.5.2
        defines the six-event establishment deadline and supervision distinction.
    """
    await _initiating_failure(tester, bad_crc=False)

    _no_connection_events(tester)


# Verify that an initiator with no Peripheral replies stops the failed connection setup
# after the permitted initial connection events and reports Connection Failed to be
# Established through HCI.
PROCEDURES |= procedure_cases(
    connection_initiation_timeout,
    {
        'LL/CON/INI/BV-39-C': (
            (
                'Connection Initiation Timeout, HCI LE Channel Selection '
                'Algorithm supported'
            ),
            {},
        ),
    },
)


async def initiation_device_filtering_undirected(tester, *, directed):
    """Initiation Device Filtering: Undirected.

    Initial Condition:
        Use public IUT own address and channel 37. Select the same valid static
        random byte pattern as the public/random peer address IXIT, so type alone
        can change without invalidating the address syntax. Select a 30-ms scan
        interval/window, 20-ms advertising base interval, 30-ms connection interval,
        zero latency and 1-s timeout under Section 4.1.6. Table 4.3-16 selects
        ADV_IND for BV-40-C and ADV_DIRECT_IND targeting the IUT for BV-41-C.
        Both selected variants permit and require the supported CSA notification.

    Test Case Configuration:
        LL/CON/INI/BV-40-C: undirected advertising.
        LL/CON/INI/BV-41-C: directed advertising.

    Test Steps:
        1. Clear the Filter Accept List and add the selected common peer address
           with public address type.
        2. Configure the Lower Tester to advertise those same bytes with random
           address type on channel 37.
        3. Start filtered initiation through HCI with the public peer identity
           and require successful Command Status. Keep initiation active across
           both rejection trials and the subsequent matching-address trial.
        4. Select the Table 4.3-16 alternative and advertise for strictly longer
           than 4 * scanInterval + 3 * scanWindow:
           4A.1. BV-40-C sends ADV_IND with an unsigned little-endian event count.
           4B.1. BV-41-C sends ADV_DIRECT_IND targeting the public IUT address.
        5. Require no connection-related HCI event and the selected RF outcome:
           5A.1. BV-40-C receives no CONNECT_IND after any ADV_IND.
           5B.1. BV-41-C receives no CONNECT_IND after any ADV_DIRECT_IND.
        6. Change the Lower Tester to another public address not in the list.
        7. Repeat Steps 4-5 for another complete rejection window without
           cancelling or restarting the IUT's initiation procedure.
        8. Change the Lower Tester to the listed public address and public type.
        9. Select the advertising alternative until a request arrives or
           4 * scanInterval + 3 * scanWindow expires:
           9A.1. BV-40-C sends ADV_IND with the unsigned little-endian event count.
           9B.1. BV-41-C sends ADV_DIRECT_IND targeting the IUT.
        10. Require the selected RF outcome with correct public InitA and peer
            AdvA/address-type bits, valid CRC/length and legal Access Address:
            10A.1. BV-40-C receives CONNECT_IND at T_IFS after ADV_IND.
            10B.1. BV-41-C receives CONNECT_IND at T_IFS after ADV_DIRECT_IND.
        11. Require successful Connection Complete with Central role, matching
            public peer identity, requested interval/latency/timeout and handle.
        12. Require the supported CSA event after Connection Complete, with the
            Step 11 handle and algorithm #1.
        13. Verify the first DATA/acknowledgement exchange on the independently
            derived CSA#1 channel, then terminate through HCI using the Step 11
            handle. Reject duplicate or late connection-related events.
        14. Repeat Steps 1-13 with public and random peer types exchanged:
            list the random peer, reject the same bytes as public, reject another
            random address, and finally connect to the matching random identity.
            Keep the IUT's own address public in both peer-filtering rounds.

    Expected Outcome:
        Pass verdict:
            No request or completion occurs for an incorrect address type or
            unlisted address. The matching address and type produce a correctly
            timed CONNECT_IND with a valid Access Address and the requested
            Central connection. HCI reports the matching peer and CSA#1; DATA
            and clean termination succeed in both public and random rounds.
        Fail verdict:
            Any excluded peer elicits a request or connection, the listed peer
            fails to connect, or a required HCI, packet or cleanup condition fails.
            The forbidden-CSA rows of Table 4.3-16 are not the selected variants.

    Source Correction:
        Step 9's stopping reference is Step 13, which terminates an established
        connection; stop advertising when CONNECT_IND is received in Step 10.
        Step 13's handle is reported in Step 11, not Step 4. The verdict's
        "address ... of the initiator in the advertising packet" exists only as
        the ADV_DIRECT_IND TargetA (BV-41-C, set to the IUT address); ADV_IND
        (BV-40-C) carries no initiator address, so there it reduces to the
        listed advertiser's AdvA and type. Both variants require InitA to be
        the IUT address.
        These are local editorial corrections, not published SIG errata.

    Reference:
        LL.TS.p28.pdf, Section 4.3.4.5, LL/CON/INI/BV-40-C and
        LL/CON/INI/BV-41-C; PDF pages 531-535, printed pages 4.3-58 through
        4.3-62. Table 4.3-16, Figure 4.3-25, Steps 1-14 including all A/B
        alternatives and Expected Outcome. Section 4.1.6 on PDF page 53 permits
        selected IXIT timings and peer address values.
    """
    for address_type in (hci.AddressType.PUBLIC_DEVICE, hci.AddressType.RANDOM_DEVICE):
        peer = hci.Address(bytes(PEER_ADDRESS), address_type)
        await tester.command(hci.HCI_LE_Clear_Filter_Accept_List_Command())
        await tester.command(
            hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(address_type, peer)
        )
        await tester.command(
            create_connection(
                peer_address=peer,
                peer_address_type=address_type,
                initiator_filter_policy=1,
            )
        )
        for excluded in (
            hci.Address(bytes(peer), 1 - address_type),
            hci.Address('C0:55:66:77:88:99', address_type),
        ):
            await _advertise_connection(
                tester, excluded, directed=directed, reject=True
            )
        link = await _advertise_connection(tester, peer, directed=directed)
        await _central_timing_events(tester, link, 1)
        _no_connection_events(tester)
        await link.disconnect()
        _no_connection_events(tester)


# Verify that initiation filters advertising by both peer address bytes and address
# type. Reject mismatching types and unlisted addresses, then connect to the matching
# Filter Accept List entry through the selected PDU type.
PROCEDURES |= procedure_cases(
    initiation_device_filtering_undirected,
    {
        'LL/CON/INI/BV-40-C': (
            (
                'Initiation Device Filtering: Undirected, HCI LE Channel '
                'Selection Algorithm supported'
            ),
            {'directed': False},
        ),
        'LL/CON/INI/BV-41-C': (
            (
                'Initiation Device Filtering: Directed, HCI LE Channel '
                'Selection Algorithm supported'
            ),
            {'directed': True},
        ),
    },
)


async def extended_scanning_connection_initiation(tester, *, phy):
    """Extended Scanning, Connection Initiation.

    Initial Condition:
        Use public IUT and common Peripheral addresses without filtering.
        Select a 30-ms connection interval, zero latency, 1-s supervision timeout,
        20-ms advertising base interval plus seeded AdvDelay, and a 10-ms scan
        interval/window (HCI 0x0010) under Section 4.1.6. Table 4.3-22 selects:
        BV-13-C: initiating LE 1M, primary LE 1M, secondary LE 1M;
        BV-25-C: initiating LE 1M, primary LE 1M, secondary LE 2M;
        BV-26-C: initiating LE Coded, primary LE Coded, secondary LE Coded.
        These three selected cases all use ADV_EXT_IND, not decision PDUs.

    Test Case Configuration:
        LL/CON/INI/BV-13-C: LE 1M.
        LL/CON/INI/BV-25-C: LE 2M.
        LL/CON/INI/BV-26-C: LE Coded.

    Test Steps:
        1. Set Decision Instructions only for Table 4.3-22 decision-PDU cases.
           This step does not apply to the three selected ADV_EXT_IND variants.
        2. Send Extended Create Connection with the selected initiating PHY bit,
           Scan_Interval[0]=Scan_Window[0]=0x0010, filter policy 0x00, public own
           address, matching public peer and selected connection parameters.
           Require successful Command Status.
        3. Advertise ADV_EXT_IND on the selected primary PHY with ADI and an
           AuxPtr pointing to connectable AUX_ADV_IND on the selected secondary
           PHY. Include public AdvA and matching ADI. Decision flags/data are
           not applicable to these selected variants.
        4. Receive AUX_CONNECT_REQ on that secondary channel and PHY at T_IFS
           within 2 us. Check CRC, length, public InitA/AdvA, requested connection
           parameters and valid Access Address, including Coded-specific rules.
        5. Send correctly addressed AUX_CONNECT_RSP on the secondary channel
           and PHY at T_IFS after AUX_CONNECT_REQ.
        6. Require Enhanced Connection Complete with Central role, public peer,
           zero local/peer RPAs, requested interval/latency/timeout and valid
           handle, immediately followed by CSA#2 for the same handle.
        7. Receive the first valid DATA on the independently calculated CSA#2
           channel inside its legal transmit window measured from AUX_CONNECT_REQ.
           Include the 2.5-ms uncoded or 3.75-ms Coded transmit-window delay,
           WinOffset, WinSize, clock-accuracy and jitter allowances.
        8. Reply on the same channel and PHY at T_IFS with a correct acknowledgement.
        9. Receive subsequent correctly formatted DATA on independently derived
           CSA#2 channels at the requested interval; reply with acknowledgements.
        10. Continue until 100 event starts have been measured. Check selected
            PHY, valid CRC/LLID, ARQ, individual intervals and cumulative drift
            against Table 4.3-23 and the Central's SCA.
        11. Terminate through HCI with the Step 6 handle, require matching
            completion and reject duplicate or late connection-related events.
        12. Repeat Steps 2-11, but do not answer the first AUX_CONNECT_REQ in
            Step 5. Continue advertising and require a second correctly timed
            request following backoff before sending AUX_CONNECT_RSP. Forbid
            premature completion or failure while the first request is unanswered.

    Expected Outcome:
        Pass verdict:
            In both rounds the IUT sends a correctly timed AUX_CONNECT_REQ with
            a valid Access Address, reports the requested Enhanced Connection
            Complete immediately followed by CSA#2, and maintains 100 correctly
            acknowledged events on the selected PHY. The first event is in its
            legal transmit window; per-event intervals and accumulated drift meet
            the SCA limits. An unanswered request is followed by a valid retry.

    Source Correction:
        Step 5 describes the Lower Tester sending AUX_CONNECT_RSP "from the IUT".
        It is the Lower Tester's response to the IUT request received in Step 4.
        This is a local editorial correction, not a published SIG erratum.

    Reference:
        LL.TS.p28.pdf, Section 4.3.4.11, LL/CON/INI/BV-13-C,
        LL/CON/INI/BV-25-C and LL/CON/INI/BV-26-C; PDF pages 549-552,
        printed pages 4.3-76 through 4.3-79. Tables 4.3-22/23, Figure 4.3-31,
        Steps 1-12, Expected Outcome and Notes. Section 4.3.4 on PDF page 520
        defines first-window timing; Core Vol 6, Part B, Sections 4.4.4.2 and
        4.5.3 define unanswered-request backoff and PHY-specific window delays.
    """
    for retry in (False, True):
        link = await _initiate_auxiliary(tester, phy=phy, retry=retry)
        await _central_timing_events(
            tester,
            link,
            TIMING_EVENTS,
            window_delay_us=(
                LL_CONN_AUX_WINDOW_DELAY_CODED_US
                if phy == Phy.LE_CODED
                else LL_CONN_AUX_WINDOW_DELAY_UNCODED_US
            ),
        )
        _no_connection_events(tester)
        await link.disconnect()
        _no_connection_events(tester)


# Verify initiation on a secondary advertising channel, correct HCI completion and
# CSA#2, first-event and maintained-connection timing, and retry after an unanswered
# AUX_CONNECT_REQ on each selected PHY.
PROCEDURES |= procedure_cases(
    extended_scanning_connection_initiation,
    {
        'LL/CON/INI/BV-13-C': (
            'Extended Scanning, Connection Initiation – LE 1M PHY',
            {'phy': Phy.LE_1M},
        ),
        'LL/CON/INI/BV-25-C': (
            'Extended Scanning, Connection Initiation – LE 2M PHY',
            {'phy': Phy.LE_2M},
        ),
        'LL/CON/INI/BV-26-C': (
            'Extended Scanning, Connection Initiation – LE Coded PHY',
            {'phy': Phy.LE_CODED},
        ),
    },
)


async def connection_initiation_channel_selection_algorithm_1(tester, *, directed):
    """Connection Initiation, Channel Selection Algorithm #1.

    Initial Condition:
        Use public IUT and common Peripheral addresses with accept-all initiation.
        Select 30-ms scan interval/window and connection interval, zero Peripheral
        latency and 1-s supervision timeout under Section 4.1.6. Table 4.3-24
        selects ADV_IND for BV-14-C or ADV_DIRECT_IND targeting the IUT for
        BV-15-C. Use the selected advertising cadence and enable the required
        connection and Channel Selection Algorithm events.

    Test Case Configuration:
        LL/CON/INI/BV-14-C: undirected advertising.
        LL/CON/INI/BV-15-C: directed advertising.

    Test Steps:
        1. Send LE Create Connection with filter policy 0x00, matching public peer
           address/type and selected connection parameters. Require successful
           Command Status.
        2. Execute the Table 4.3-24 advertising alternative:
           2A.1. BV-14-C sends ADV_IND with ChSel=0.
           2A.2. Receive CONNECT_IND with ChSel=0 or 1 at T_IFS after ADV_IND.
           2B.1. BV-15-C sends ADV_DIRECT_IND with ChSel=0, targeting the IUT.
           2B.2. Receive CONNECT_IND with ChSel=0 or 1 at T_IFS after ADV_DIRECT_IND.
           Check request CRC, length, public InitA/AdvA and address-type bits,
           selected interval/latency/timeout and valid Access Address.
        3. Require successful Connection Complete with Central role, matching
           public peer identity, requested parameters and valid handle. Immediately
           afterward require the same handle and algorithm #1 in the CSA event.
        4. Receive the first correctly formatted DATA inside its legal transmit
           window, including WinOffset, WinSize, clock accuracy and jitter, on
           the independently derived CSA#1 channel.
        5. Send a correctly formatted reply on the same channel at T_IFS using
           the acknowledgement scheme.
        6. Receive subsequent DATA at connection intervals on independently
           calculated CSA#1 channels, and reply with correct acknowledgements.
        7. Continue until at least 100 event starts have been measured. Check PHY,
           CRC, valid LLID, ARQ, each interval and cumulative SCA drift against
           Table 4.3-25; do not prescribe one exact legal first-event timestamp.
        8. Terminate through HCI using the Step 3 handle and require matching
           completion. Reject duplicate or late connection-related HCI events.

    Expected Outcome:
        Pass verdict:
            The IUT sends CONNECT_IND at T_IFS within 2 us, uses a legal Access
            Address, and reports the requested Central connection immediately
            followed by CSA#1. At least 100 correctly acknowledged DATA events
            use CSA#1 channels, begin inside the legal first window, and satisfy
            the selected PHY, interval tolerance and cumulative clock-drift bound.

    Source Correction:
        Step 8 refers to a connection handle from Step 4, which receives DATA.
        Use the handle reported in Step 3. This is a local editorial correction,
        not a published SIG erratum.

    Reference:
        LL.TS.p28.pdf, Section 4.3.4.12, LL/CON/INI/BV-14-C and
        LL/CON/INI/BV-15-C; PDF pages 553-556, printed pages 4.3-80 through
        4.3-83. Tables 4.3-24/25, Figure 4.3-32, Steps 1-8 including
        alternatives 2A/2B, Expected Outcome and Notes. Section 4.3.4 on PDF
        page 520 defines first-window and interval formulas; Section 4.1.6 on
        PDF page 53 permits the selected IXIT timings.
    """
    link = await _initiate(
        tester, LOWER_PERIPHERAL_PUBLIC_ADDRESS, directed=directed, csa2=False
    )
    await _central_timing_events(tester, link, TIMING_EVENTS)
    _no_connection_events(tester)
    await link.disconnect()
    _no_connection_events(tester)


# Verify that the Central selects CSA#1 when the Lower Tester's legacy advertising ChSel
# is 0, reports the selected algorithm through HCI, and maintains correctly timed DATA
# on independently calculated channels.
PROCEDURES |= procedure_cases(
    connection_initiation_channel_selection_algorithm_1,
    {
        'LL/CON/INI/BV-14-C': (
            'Connection Initiation, Channel Selection Algorithm #1',
            {'directed': False},
        ),
        'LL/CON/INI/BV-15-C': (
            'Connecting to Directed Advertising, Channel Selection Algorithm\xa0#1',
            {'directed': True},
        ),
    },
)


async def connection_initiation_channel_selection_algorithm_2(tester, *, directed):
    """Connection Initiation, Channel Selection Algorithm #2.

    Initial Condition:
        Use public IUT and common Peripheral addresses with accept-all initiation.
        Select 30-ms scan interval/window and connection interval, zero Peripheral
        latency and 1-s supervision timeout under Section 4.1.6. Table 4.3-26
        selects ADV_IND for BV-16-C or ADV_DIRECT_IND targeting the IUT for
        BV-17-C. Use the selected advertising cadence and enable the required
        connection and Channel Selection Algorithm events.

    Test Case Configuration:
        LL/CON/INI/BV-16-C: undirected advertising.
        LL/CON/INI/BV-17-C: directed advertising.

    Test Steps:
        1. Send LE Create Connection with filter policy 0x00, matching public peer
           address/type and selected connection parameters. Require successful
           Command Status.
        2. Execute the Table 4.3-26 advertising alternative:
           2A.1. BV-16-C sends ADV_IND with ChSel=1.
           2A.2. Receive CONNECT_IND with ChSel=1 at T_IFS after ADV_IND.
           2B.1. BV-17-C sends ADV_DIRECT_IND with ChSel=1, targeting the IUT.
           2B.2. Receive CONNECT_IND with ChSel=1 at T_IFS after ADV_DIRECT_IND.
           Check request CRC, length, public InitA/AdvA and address-type bits,
           selected interval/latency/timeout and valid Access Address.
        3. Require successful Connection Complete with Central role, matching
           public peer identity, requested parameters and valid handle. Immediately
           afterward require the same handle and algorithm #2 in the CSA event.
        4. Receive the first correctly formatted DATA inside its legal transmit
           window, including WinOffset, WinSize, clock accuracy and jitter, on
           the independently derived CSA#2 channel.
        5. Send a correctly formatted reply on the same channel at T_IFS using
           the acknowledgement scheme.
        6. Receive subsequent DATA at connection intervals on independently
           calculated CSA#2 channels, and reply with correct acknowledgements.
        7. Continue until at least 100 event starts have been measured. Check PHY,
           CRC, valid LLID, ARQ, each interval and cumulative SCA drift against
           Table 4.3-27; do not prescribe one exact legal first-event timestamp.
        8. Terminate through HCI using the Step 3 handle and require matching
           completion. Reject duplicate or late connection-related HCI events.

    Expected Outcome:
        Pass verdict:
            The IUT sends CONNECT_IND at T_IFS within 2 us, uses a legal Access
            Address, and reports the requested Central connection immediately
            followed by CSA#2. At least 100 correctly acknowledged DATA events
            use CSA#2 channels, begin inside the legal first window, and satisfy
            the selected PHY, interval tolerance and cumulative clock-drift bound.

    Source Correction:
        Step 8 refers to a connection handle from Step 4, which receives DATA.
        Use the handle reported in Step 3. This is a local editorial correction,
        not a published SIG erratum.

    Reference:
        LL.TS.p28.pdf, Section 4.3.4.13, LL/CON/INI/BV-16-C and
        LL/CON/INI/BV-17-C; PDF pages 556-559, printed pages 4.3-83 through
        4.3-86. Tables 4.3-26/27, Figure 4.3-33, Steps 1-8 including
        alternatives 2A/2B, Expected Outcome and Notes. Section 4.3.4 on PDF
        page 520 defines first-window and interval formulas; Section 4.1.6 on
        PDF page 53 permits the selected IXIT timings.
    """
    link = await _initiate(
        tester, LOWER_PERIPHERAL_PUBLIC_ADDRESS, directed=directed, csa2=True
    )
    await _central_timing_events(tester, link, TIMING_EVENTS)
    _no_connection_events(tester)
    await link.disconnect()
    _no_connection_events(tester)


# Verify that the Central selects CSA#2 when the Lower Tester's legacy advertising ChSel
# is 1, reports the selected algorithm through HCI, and maintains correctly timed DATA
# on independently calculated channels.
PROCEDURES |= procedure_cases(
    connection_initiation_channel_selection_algorithm_2,
    {
        'LL/CON/INI/BV-16-C': (
            'Connection Initiation, Channel Selection Algorithm #2',
            {'directed': False},
        ),
        'LL/CON/INI/BV-17-C': (
            'Connecting to Directed Advertising, Channel Selection Algorithm\xa0#2',
            {'directed': True},
        ),
    },
)


async def connection_initiation_invalid_crc(tester):
    """Connection Initiation Invalid CRC.

    Initial Condition:
        Use public IUT and common public Peripheral addresses on selected primary
        channel 37, with no Filter Accept List filtering. Select a 30-ms scan
        interval/window and 20-ms advertising base interval plus seeded AdvDelay.
        Use valid selected connection parameters; their values are not material
        to this no-connection verdict. Corrupt each advertising CRC by inverting
        all checksum bits as specified in Section 4.1.5.1.

    Test Steps:
        1. Configure the Lower Tester to advertise the common public Peripheral
           address on channel 37 with the selected advertising data and interval.
        2. Start unfiltered initiation to that public peer through HCI and require
           successful Command Status.
        3. Send ADV_IND with an invalid CRC on channel 37 every selected
           advertising interval plus AdvDelay, continuing strictly beyond
           4 * scanInterval + 3 * scanWindow.
        4. Listen after every packet and require no CONNECT_IND at T_IFS and no
           premature Connection Complete, CSA or disconnection event.
        5. Send LE Create Connection Cancel, require successful Command Complete,
           then require the cancellation Connection Complete with Unknown
           Connection Identifier status 0x02. Reject extra connection events.

    Expected Outcome:
        Pass verdict:
            No invalid-CRC advertising packet produces a connection request or
            connection. Initiation remains pending for the full observation window
            and is stopped through the expected HCI cancellation completions.

    Reference:
        LL.TS.p28.pdf, LL/CON/INI/BI-01-C, independently titled procedure after
        Section 4.3.4.16; PDF pages 566-567, printed pages 4.3-93 through 4.3-94.
        Figure 4.3-37, Steps 1-5 and Expected Outcome. Section 4.1.5.1 on PDF
        page 52 defines invalid CRC; Core Vol 4, Part E defines Create Connection
        Cancel command completion and cancellation Connection Complete status.
    """
    await tester.command(
        create_connection(
            peer_address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
            peer_address_type=hci.AddressType.PUBLIC_DEVICE,
            le_scan_interval=ESTABLISHMENT_SCAN_INTERVAL,
            le_scan_window=ESTABLISHMENT_SCAN_INTERVAL,
        )
    )
    interval_us = ESTABLISHMENT_SCAN_INTERVAL * hci.HCI_LE_SCAN_INTERVAL_UNIT_US
    deadline = tester.radio.now_us + 4 * interval_us + 3 * interval_us
    generator = random.Random(0)
    while True:
        advertised = tester.radio.now_us + 100
        await tester.transmit(
            37,
            adv_pdu(address=LOWER_PERIPHERAL_PUBLIC_ADDRESS),
            start_us=advertised,
            bad_crc=True,
        )
        assert await tester.receive(37, duration_us=1000) is None
        _no_initiation_complete(tester)
        if advertised > deadline:
            break
        next_event = (
            advertised + ADV_INTERVAL_US + generator.randrange(LL_ADV_DELAY_MAX_US + 1)
        )
        await tester.wait(next_event - tester.radio.now_us)
    await tester.command(hci.HCI_LE_Create_Connection_Cancel_Command())
    event = await tester.event(hci.HCI_LE_Connection_Complete_Event)
    assert event.status == ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR

    _no_initiation_complete(tester)


# Verify that invalid-CRC legacy advertising cannot trigger a connection request and
# that the still-pending initiation procedure can be cancelled successfully through HCI
# after the complete observation window.
PROCEDURES |= procedure_cases(
    connection_initiation_invalid_crc,
    {
        'LL/CON/INI/BI-01-C': (
            'Connection Initiation Invalid CRC',
            {},
        ),
    },
)


async def peripheral_packets_invalid_crc(tester):
    """Peripheral Packets Invalid CRC.

    Initial Condition:
        Use public IUT and common Peripheral addresses, selected primary channel
        37, accept-all initiation, and a 30-ms scan interval/window and connection
        interval. Set zero Peripheral latency and the prescribed 100-ms timeout.
        Advertise one data octet. BV-04-C selects the supported-CSA row of Table
        4.3-31. Invert all CRC bits in every Peripheral DATA reply.

    Test Steps:
        1. Configure the Lower Tester as a public advertiser on channel 37.
        2. Send LE Create Connection through HCI with the selected parameters;
           require successful Command Status.
        3. Send ADV_IND with one data octet and receive CONNECT_IND at T_IFS.
           The source allows advertising until the scan deadline or four attempts;
           the ideal link takes the first successful attempt. Check request CRC,
           length, addresses/type bits, legal Access Address and parameters.
        4. Require Connection Complete with Central role, matching public peer,
           selected interval/latency/timeout and valid connection handle.
        5. Require the supported CSA event after Connection Complete with the
           Step 4 handle and algorithm #1.
        6. Receive the first correctly formatted DATA within its legal transmit
           window, including clock-accuracy and jitter bounds.
        7. Perform the source substeps:
           a) Reply on the same channel at T_IFS with a correctly formatted,
              acknowledging DATA PDU but inverted CRC. If the IUT transmits a
              second DATA PDU in this event, reply with the same invalid-CRC fault.
           b) Receive correctly formatted DATA on the next independently derived
              CSA#1 channel at the requested interval with permitted clock drift.
           c) Send another correctly formatted acknowledging reply at T_IFS,
              again with an invalid CRC.
           d) If another DATA PDU occurs in this same event, validate its timing
              and send a correctly formatted invalid-CRC acknowledgement.
        8. First permitted alternative: repeat Steps 7b-7c until supervision
           expires. This no-second-PDU-observation alternative is not selected.
        9. OR, as the source's alternative separator states.
        10. Selected alternative: repeat Steps 7b-7d until supervision expires.
            Check unchanged IUT SN/NESN, correct DATA/PHY and interval/cumulative
            timing. Never send CRC-valid DATA that could reset supervision.
        11. Require successful Disconnection Complete with the Step 4 handle and
            Connection Timeout reason 0x08 at the original 100-ms deadline,
            within the 1-ms HCI observation resolution.

        Cleanup: probe subsequent connection windows for stopped RF and reject
        extra connection-related HCI events.

    Expected Outcome:
        Pass verdict:
            The requested connection is created with a valid Access Address. The
            Central continues correctly timed DATA without acknowledging corrupted
            replies and terminates at the original supervision deadline with the
            correct handle and Connection Timeout reason. Invalid CRC reception
            establishes the physical connection but cannot refresh supervision.
        Fail verdict:
            A request, connection, HCI field/order, DATA, acknowledgement or timing
            condition fails. The forbidden-CSA row of Table 4.3-31 is not the
            selected BV-04-C variant.

    Source Correction:
        Step 11 references a handle from Step 7; the handle is reported in
        Step 4. This is a local editorial correction, not a published SIG erratum.

    Reference:
        LL.TS.p28.pdf, Section 4.3.4.17, LL/CON/INI/BI-04-C; PDF pages 568-570,
        printed pages 4.3-95 through 4.3-97. Table 4.3-31, Figure 4.3-38,
        Steps 1-11 including substeps 7a-7d and alternatives 8-10, and Expected
        Outcome. Core Vol 6, Part B, Sections 4.5 and 4.5.2 distinguish physical
        establishment from valid-CRC supervision reset.
    """
    await _initiating_failure(tester, bad_crc=True)

    _no_connection_events(tester)


# Verify that a Central receiving only invalid-CRC Peripheral replies continues
# unacknowledged transmissions until the original supervision deadline, then reports
# Connection Timeout rather than extending the timer.
PROCEDURES |= procedure_cases(
    peripheral_packets_invalid_crc,
    {
        'LL/CON/INI/BI-04-C': (
            (
                'Peripheral Packets Invalid CRC, HCI LE Channel Selection '
                'Algorithm supported'
            ),
            {},
        ),
    },
)


async def connection_initiation_with_valid_access_address(tester):
    """Connection Initiation with Valid Access Address.

    Initial Condition:
        Use public IUT and common Peripheral addresses with accept-all initiation
        on selected primary channel 37. Select 30-ms scan interval/window and
        connection interval, zero Peripheral latency, 1-s timeout, and 20-ms
        advertising base interval plus AdvDelay under Section 4.1.6. Keep the
        same Controller instance without resetting it between the 36 connections.
        BV-50-C selects the supported-CSA row of Table 4.3-33.

    Test Steps:
        1. Configure the Lower Tester to advertise the common public Peripheral
           address on selected primary channel 37.
        2. Enable unfiltered initiation through HCI and require successful
           Command Status with the selected connection parameters.
        3. Send ADV_IND and receive CONNECT_IND at T_IFS within 2 us. Check
           CRC, length, public addresses/type bits, connection parameters and
           every Access Address constraint; retain the generated address.
        4. Require successful Connection Complete with Central role, matching
           public peer identity, requested parameters and valid handle.
        5. Require the supported CSA event immediately after Connection Complete,
           with the Step 4 handle and algorithm #1.
        6. Send Disconnect for that connection handle through HCI and require
           successful Command Status.
        7. Require the matching successful Disconnection Complete, with no
           duplicate or late connection-related events.
        8. Repeat Steps 1-7 another 35 times. Require all 36 Access Addresses to
           be unique and each of their 32 bit positions to contain at least
           three zeros and at least three ones across the full sample.

    Expected Outcome:
        Pass verdict:
            Every CONNECT_IND contains a legal Access Address; all 36 addresses
            are distinct; and every bit position meets the three-zero/three-one
            requirement. The requested connections, matching HCI notifications,
            CSA#1 selection and clean termination succeed in each round.
        Fail verdict:
            Any address or bit-diversity condition fails, or a required HCI or
            packet condition is unmet. The forbidden-CSA row of Table 4.3-33 is
            not the selected supported-event BV-50-C variant.

    Reference:
        LL.TS.p28.pdf, Section 4.3.4.19, LL/CON/INI/BV-50-C; PDF pages 575-577,
        printed pages 4.3-102 through 4.3-104. Table 4.3-33, Figure 4.3-41,
        Steps 1-8 and Expected Outcome. Core Vol 6, Part B, Section 2.1.2
        defines Access Address constraints.
    """
    addresses = []
    for _ in range(ACCESS_ADDRESS_ROUNDS):
        link = await _initiate(tester, LOWER_PERIPHERAL_PUBLIC_ADDRESS)
        addresses.append(link.parameters.access_address)
        _no_connection_events(tester)
        await link.disconnect()
        _no_connection_events(tester)
    _check_access_address_set(addresses)


# Verify that every generated connection Access Address is legal, that 36 consecutive
# connections use distinct addresses, and that each bit position contains at least three
# zeros and three ones across the set.
PROCEDURES |= procedure_cases(
    connection_initiation_with_valid_access_address,
    {
        'LL/CON/INI/BV-50-C': (
            (
                'Connection Initiation with Valid Access Address, HCI LE '
                'Channel Selection Algorithm supported'
            ),
            {},
        ),
    },
)


async def parallel_connection_requests_incoming_connect_indication_first(tester):
    """Parallel connection requests, incoming connect indication first.

    Initial Condition:
        The IUT is in Standby. Mask Enhanced Connection Complete while leaving
        legacy completion and CSA events enabled. Use public LT1/LT2 addresses,
        selected scan timing, and common connection/channel-selection parameters.

    Test Steps:
        1. Configure LT2 to monitor IUT advertising packets.
        2. Send LE Create Connection targeting LT1, with filter policy 0 and
           valid parameters; require successful Command Status.
        3. Set legacy advertising type 0x01 (high-duty directed), filter policy
           0, and LT2 peer address/type; require successful Command Complete.
        4. Enable advertising; require successful Command Complete.
        5. Receive ADV_DIRECT_IND targeting LT2. Require ChSel=1 for this
           CSA2-capable advertiser, applying the Source Correction below.
        6. LT2 sends CONNECT_IND with ChSel=0 at T_IFS after the advertisement.
        7. After Step 6, require successful legacy Connection Complete with LT2
           address/type, Peripheral role, and a valid connection handle.
        8. LT1 sends ADV_IND packets with ChSel=0 to the still-initiating IUT.
        9. Receive the IUT CONNECT_IND to LT1; validate addresses, parameters,
           Access Address and T_IFS, independently of the incoming connection.
        10. Require successful legacy Connection Complete identifying LT1,
            Central role, and a valid handle distinct from the Peripheral handle.
        11. For BV-51-C, require the Central completion to be immediately followed
            by a matching-handle CSA event with algorithm value 0 (CSA #1).

    Expected Outcome:
        Pass verdict:
            Steps 7 and 10 complete in the prescribed order with correct public
            peers, roles, handles, and connection parameters. The pending LT1
            initiation survives LT2 establishment and both ACLs remain usable.
        Fail verdict:
            Advertising Set Terminated is emitted for legacy advertising after
            Step 6, or a CSA event identifies the Peripheral handle from Step 7.

    Source Correction:
        PDF 578 Step 5 says advertiser ChSel=0. Core 6.3 Vol 6, Part B,
        Section 2.3.1.2 mandates ChSel=1 when the advertiser supports CSA #2;
        Amber corroborates this local correction. Preserve Step 6 ChSel=0,
        selecting CSA #1, and the source prohibition on Peripheral CSA events.
        This is a local source interpretation, not a published SIG erratum.

    Coverage Note:
        Maintain at least ten independently hopped, acknowledged empty DATA
        events per ACL and reject extra creation/termination events. The fixture
        closes both links after the normative observations.

    Reference:
        LL.TS.p28, Section 4.3.4.20, Table 4.3-34, Figure 4.3-42;
        LL/CON/INI/BV-51-C; PDF 577-579, printed 4.3-104 through 4.3-106.
    """
    await _parallel_connections(tester, incoming_first=True)


# Verify that starting advertising during initiation does not lose the pending outgoing
# connection when an incoming connection completes first.
PROCEDURES |= procedure_cases(
    parallel_connection_requests_incoming_connect_indication_first,
    {
        'LL/CON/INI/BV-51-C': (
            ('Parallel connection requests, incoming connect indication first, CSA #2'),
            {},
        ),
    },
)


async def parallel_connection_requests_outgoing_connection_request_first(tester):
    """Parallel connection requests, outgoing connection request first.

    Initial Condition:
        The IUT is in Standby. Mask Enhanced Connection Complete, leaving legacy
        completion and CSA events enabled. Use public LT1/LT2 peers, selected scan
        timing, and common connection/channel-selection parameters.

    Test Steps:
        1. Configure LT2 to monitor IUT advertising packets.
        2. Send LE Create Connection for LT1 with filter policy 0 and valid
           parameters; require successful Command Status.
        3. Set legacy advertising type 0x00 (undirected), filter policy 0, and
           valid remaining parameters; require successful Command Complete.
        4. Set advertising data length 2 and exact bytes 0F 0F; require success.
        5. Enable advertising; require successful Command Complete.
        6. Receive IUT ADV_IND packets at LT2, checking public AdvA and data.
        7. LT1 sends ADV_IND packets with ChSel=0 to the IUT.
        8. Receive the IUT CONNECT_IND to LT1; validate addresses, Access
           Address, parameters, and T_IFS after the LT1 advertisement.
        9. Require successful legacy Connection Complete with LT1 address/type,
           Central role, and a valid connection handle.
        10. For BV-52-C, require the immediate matching-handle CSA event with
            algorithm value 0 (CSA #1).
        11. Observe continued IUT ADV_IND packets while maintaining the LT1 ACL.
        12. After LT2 receives five advertisements following outgoing completion,
            send CONNECT_IND with ChSel=0 at T_IFS after the fifth ADV_IND.
        13. Require successful legacy Connection Complete with LT2 address/type,
            Peripheral role, and a valid handle distinct from the Central handle.

    Expected Outcome:
        Pass verdict:
            Steps 9 and 13 identify the correct peers, roles and valid handles.
            Advertising continues after outgoing completion as required in
            Step 11, including the five packets before LT2 connects.
        Fail verdict:
            Advertising Set Terminated is emitted for legacy advertising after
            Step 12, or a CSA event identifies the Peripheral handle from Step 13.

    Coverage Note:
        Check full connection timing/clock fields, maintain at least ten empty
        DATA events per ACL with independent hopping and ACK state, and reject
        extra creation/termination events. The fixture then closes both links.

    Reference:
        LL.TS.p28, Section 4.3.4.21, Table 4.3-35, Figure 4.3-43;
        LL/CON/INI/BV-52-C; PDF 579-581, printed 4.3-106 through 4.3-108.
    """
    await _parallel_connections(tester, incoming_first=False)


# Verify that an initiator can start advertising, complete its outgoing connection
# first, continue advertising, and accept an incoming connection.
PROCEDURES |= procedure_cases(
    parallel_connection_requests_outgoing_connection_request_first,
    {
        'LL/CON/INI/BV-52-C': (
            (
                'Parallel connection requests, outgoing connection request '
                'first, HCI LE Channel Selection Algorithm supported'
            ),
            {},
        ),
    },
)


async def duplicate_connection_request(tester, *, privacy=False):
    """Duplicate Connection Request.

    Initial Condition:
        Initiating with selected scan interval/window, no Filter Accept List,
        public Lower Tester peer, supported own address type, and common
        connection interval, Peripheral latency, timeout and channel parameters.

    Test Steps:
        1. Configure the Lower Tester for connectable undirected advertising on
           all three primary advertising channels.
        2. Send LE Create Connection for that public peer and require successful
           Command Status.
        3. Send ADV_IND at the selected advertising interval on each event.
        4. Receive CONNECT_IND at T_IFS after an ADV_IND; validate its addresses,
           Access Address and connection parameters.
        5. Require successful Connection Complete with the Lower Tester peer
           address, selected interval, Central role, and valid connection handle.
        6. For BI-05-C, require an immediate matching-handle CSA event with
           algorithm value 0 (CSA #1).
        7. Maintain the connection by answering IUT DATA with correctly sequenced
           empty DATA at T_IFS on the same data channel.
        8. Continue ADV_IND on every primary channel, listening for CONNECT_IND
           at T_IFS after each advertisement.
        9. Send a second LE Create Connection for the same peer address/type.
        10. Continue correctly timed DATA replies and Step 8 advertising for at
            least five seconds after the Step 11 response, preserving the ACL.
        11. Accept exactly one of the following response alternatives:
            11A.1. Command Status reports Connection Already Exists (0x0B).
            11B.1. Command Status reports success (0x00).
            11B.2. Connection Complete reports Connection Already Exists (0x0B).
            11C.1. Command Status reports success without Connection Complete.
            11C.2. After five seconds, send LE Create Connection Cancel.
            11C.3. Require successful Command Complete, followed by Connection
                   Complete with Unknown Connection Identifier (0x02).

    Expected Outcome:
        Pass verdict:
            The IUT follows alternative 11A, 11B or 11C with the specified status
            and event ordering while maintaining the original ACL.
        Fail verdict:
            Any CONNECT_IND is sent to the existing peer after Step 5, or the
            original connection is not maintained. Table 4.3-36 permits the
            initial CSA event for the selected BI-05-C variant; no duplicate
            connection or extra CSA/advertising-termination event is permitted.

    Source Correction:
        Step 11's introductory sentence mentions only alternatives 11A/11B,
        although the same step and pass verdict explicitly include 11C. Preserve
        all three alternatives. The purpose calls the IUT Peripheral, but it is
        the initiator/Central; the Lower Tester supplies Peripheral responses.

    Coverage Note:
        Repeat advertising with seeded 20-ms plus advertising-delay cadence
        during establishment. Maintain positive DATA ACKs during the five-second
        duplicate-request observation and disconnect the original link via HCI.

    Reference:
        LL.TS.p28, Section 4.3.4.22, Table 4.3-36, Figure 4.3-44;
        LL/CON/INI/BI-05-C; PDF 582-585, printed 4.3-109 through 4.3-112.
    """
    await check_duplicate_initiator_request(tester, privacy=privacy)


# Verify that an initiator does not send another connection request to a peer with which
# it already maintains an ACL connection.
PROCEDURES |= procedure_cases(
    duplicate_connection_request,
    {
        'LL/CON/INI/BI-05-C': (
            (
                'Duplicate Connection Request, HCI LE Channel Selection '
                'Algorithm supported'
            ),
            {},
        ),
    },
)


async def peripheral_asymmetric_connections(tester):
    """Peripheral Asymmetric Connections.

    Initial Condition:
        An unencrypted connected Peripheral with public address, common channel
        parameters, latency 5 and supervision timeout 200 ms. Select a 7.5-ms
        common interval, satisfying timeout > 2 * (latency + 1) * interval.
        The Lower Tester acts as Central and tracks DATA SN/NESN independently.

    Test Steps:
        1. Send LE Read Buffer Size and require successful Command Complete with
           usable LE ACL packet length and packet count.
        2. Optionally send Read Buffer Size. Omit this optional command because
           Step 1 reports usable dedicated LE buffers.
        3. Repeat Steps 3-5 for thirty consecutive connection events. Send one
           DATA PDU per interval with nonempty payload, following the common
           channel selection and advancing SN/NESN only after valid ACKs.
        4. For each successfully acknowledged payload, require an HCI ACL Data
           packet with the connection handle, exact payload, and first-fragment
           PB flag; reject incorrect length or broadcast flags.
        5. Receive the empty Peripheral response at T_IFS after the sent packet.
           Require SN matching current NESN and NESN acknowledging the next SN.
           Missing RF responses do not advance the peer acknowledgement state.
        6. Execute Peripheral Connection Terminated for the established handle
           and connection parameters; require HCI termination completion.

    Expected Outcome:
        Pass verdict:
            The Peripheral responds in at least five of the thirty events, with
            correct DATA acknowledgement, packet timing and Host data delivery.

    Coverage Note:
        The source response threshold assumes BER no worse than 0.1 percent.
        This deterministic trial injects no bit errors; valid received packets
        must satisfy the packet/ACK assertions. Optional Step 2 is not selected.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-02-C procedure in
        Section 4.3.5, Figure 4.3-47; PDF 592-593, printed 4.3-119 through 4.3-120.
    """
    link = await establish(
        tester,
        central=False,
        peer_address=LOWER_CENTRAL_PUBLIC_ADDRESS,
        interval=6,
        latency=5,
        timeout=20,
    )
    capacity = await tester.command(hci.HCI_LE_Read_Buffer_Size_Command())
    assert capacity.le_acl_data_packet_length and capacity.total_num_le_acl_data_packets
    responses = 0
    for _ in range(30):
        sent_sn = link.sn
        expected_sn = link.nesn
        packet, _sent = await _wire_event(
            link,
            lambda _: _data_pdu(link.sn, link.nesn, ARQ_PAYLOAD, llid=LlId.START),
            required=False,
        )
        if packet:
            assert packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
            assert len(packet.pdu) == PDU_HEADER_SIZE and packet.pdu[1] == 0
            assert _sequence_bits(packet) == (expected_sn, sent_sn ^ 1)
            link._accept(packet)
            responses += 1
            event = await tester.event(hci.HCI_AclDataPacket)
            assert event.connection_handle == link.handle and event.data == ARQ_PAYLOAD
            assert event.bc_flag == 0 and event.data_total_length == len(ARQ_PAYLOAD)
            assert event.pb_flag == hci.HCI_ACL_PB_FIRST_FLUSHABLE
    assert responses >= 5
    await link.disconnect()


# Verify Peripheral responses, DATA acknowledgement and Host delivery when the
# connection allows nonzero Peripheral latency.
PROCEDURES |= procedure_cases(
    peripheral_asymmetric_connections,
    {
        'LL/CON/PER/BV-02-C': (
            'Peripheral Asymmetric Connections',
            {},
        ),
    },
)


async def decision_advertising_accepting_connections(tester, *, phy):
    """Decision-Based Advertising, Accepting Connections.

    Initial Condition:
        Public local/peer addresses, all primary channels, minimum interval;
        Table 4.3-2 selects 1M/1M, 1M/2M or Coded/Coded PHYs.

    Test Case Configuration:
        LL/CON/ADV/BV-17-C: LE 1M.
        LL/CON/ADV/BV-18-C: LE 2M.
        LL/CON/ADV/BV-19-C: LE Coded.

    Test Steps:
        Execute Section 4.3.3.3 steps 1-16 for Table 4.3-3 rounds 5 and 6:
        configure properties 0x0081 and six random decision-data octets with
        flags 0x00, enable, check primary decision PDUs (AuxPtr, optional ADI)
        and AuxPtr timing on every primary channel, follow the
        connectable auxiliary PDU and send matching/wrong-AdvA AUX_CONNECT_REQ.
        Wrong AdvA must receive no response or connection event. Matching AdvA
        must receive AUX_CONNECT_RSP within two microseconds of T_IFS, connection
        and advertising-termination events, CSA#2 and 100 valid data exchanges
        on the selected PHY. Verify advertising stops, then disconnect.

    Expected Outcome:
        Only matching-AdvA requests establish a connection; decision data,
        primary intervals, auxiliary timing, Host events and data-channel traffic
        conform to the independently observed source procedure.

    Source Correction:
        Repeat data steps 12-14 for 100 events; Advertising Set Terminated in
        step 11 is a one-shot event. This follows Core Vol 4, Part E, 7.7.65.18,
        and is a local clarification rather than adopted SIG errata.

    Reference:
        LL.TS.p28, Section 4.3.3.3, PDF pages 485-490, Tables 4.3-2/3;
        Core Vol 6, Part B, 2.3.1.11, 4.4.2.3 and 4.4.4.2.
    """
    await _extended_setup(tester, phy=phy, decision=True)


# Verify connection acceptance from decision advertising on each selected PHY. The
# Lower Tester checks auxiliary requests, advertising termination, CSA#2 and Peripheral
# data exchanges.
PROCEDURES |= procedure_cases(
    decision_advertising_accepting_connections,
    {
        f'LL/CON/ADV/BV-{number}-C': (
            f'Decision-Based Advertising, Accepting Connections – {name} PHY',
            {'phy': phy},
        )
        for number, phy, name in [
            (17, Phy.LE_1M, 'LE 1M'),
            (18, Phy.LE_2M, 'LE 2M'),
            (19, Phy.LE_CODED, 'LE Coded'),
        ]
    },
)


async def extended_advertising_random_initiator(tester, *, phy):
    """Extended Advertising, Accepting Connections with Random address.

    Initial Condition:
        Public IUT advertiser, accept-all policy, all primary channels.

    Test Case Configuration:
        LL/CON/ADV/BV-14-C: LE 1M.
        LL/CON/ADV/BV-15-C: LE 2M.
        LL/CON/ADV/BV-16-C: LE Coded.

    Test Steps:
        Execute steps 1-11 for every Table 4.3-9 InitA class: random static,
        non-resolvable private, and resolvable private address. Configure
        connectable extended advertising, enable, observe primary cycles and
        follow AuxPtr, send AUX_CONNECT_REQ at T_IFS, verify AUX_CONNECT_RSP
        has random RxAdd and the exact InitA as TargetA, receive Host connection/
        CSA#2/termination events, exchange valid data and disconnect.

    Expected Outcome:
        All three address classes establish and maintain a Peripheral connection
        on the specified secondary PHY; request/response timing and types match.

    Source Correction:
        Step 1 specifies LE 2M as Primary PHY in the 2M case. Core Vol 4, Part E,
        7.8.53 permits only LE 1M or Coded on primary channels; use 1M/2M as in
        Table 4.3-2. Step 3 says only AuxPtr, but Core Vol 6, Part B, Table 2.4
        makes ADI mandatory in a connectable ADV_EXT_IND, so require both
        fields. These are local clarifications, not adopted SIG errata.

    Reference:
        LL.TS.p28, Section 4.3.3.8, PDF pages 509-512, Tables 4.3-8/9,
        Figures 4.3-15/16; Core Vol 6, Part B, 4.4.4.2 and 4.5.
    """
    for peer in (
        PEER_ADDRESS,
        privacy_peer.FALLBACK,
        privacy_peer.private_address(privacy_peer.PEER_IRK),
    ):
        await _extended_setup(tester, phy=phy, peer=peer, random_round=True)


# Verify that extended advertising accepts random static, non-resolvable private and
# resolvable private initiator addresses, then maintains a Peripheral connection on
# each selected PHY.
PROCEDURES |= procedure_cases(
    extended_advertising_random_initiator,
    {
        f'LL/CON/ADV/BV-{number}-C': (
            'Extended Advertising, Accepting Connections with '
            f'Random address – {name} PHY',
            {'phy': phy},
        )
        for number, phy, name in [
            (14, Phy.LE_1M, 'LE 1M'),
            (15, Phy.LE_2M, 'LE 2M'),
            (16, Phy.LE_CODED, 'LE Coded'),
        ]
    },
)


async def _privacy_initiating_advertisement(
    tester, address, identity, *, target=None, own_type=2, reject=False
):
    deadline = tester.radio.now_us + 400_000
    attempt = 0
    while tester.radio.now_us < deadline:
        channel = 37 + attempt % 3
        pdu = (
            adv_pdu(address=address)
            if target is None
            else request_pdu(
                AdvertisingPduType.ADV_DIRECT_IND,
                advertiser=target,
                scanner=address,
            )
        )
        start = tester.radio.now_us + 100
        end = await tester.transmit(channel, pdu, start_us=start)
        request = await tester.receive(channel, duration_us=200)
        if request is not None:
            assert not reject, 'CONNECT_IND sent for a rejected privacy vector'
            assert request.pdu[0] & 15 == AdvertisingPduType.CONNECT_IND
            assert request.pdu[8:14] == bytes(address)
            assert bool(request.pdu[0] & PDU_ADV_RXADD_MASK) == address.is_random
            assert abs(request.start_us - end - LL_IFS_US) <= 2
            local = hci.Address(
                request.pdu[2:8], int(bool(request.pdu[0] & PDU_ADV_TXADD_MASK))
            )
            if own_type >= 2:
                privacy_peer.check_private_address(local, privacy_peer.LOCAL_IRK)
                if target is not None:
                    assert local != target
            else:
                assert local == DUT_ADDRESS
            parameters = LinkParameters.decode(request.pdu[14:])
            check_access_address(parameters.access_address)
            link = PeerLink(
                tester, parameters, request.end_us, central=True, csa2=False
            )
            reported = (
                hci.Address(bytes(identity), 2 + int(identity.is_random))
                if address.is_resolvable
                else identity
            )
            event = await _connection_complete(tester, link, reported)
            assert event.local_resolvable_private_address == (
                local if own_type >= 2 else hci.Address.ANY_RANDOM
            )
            assert event.peer_resolvable_private_address == (
                address if address.is_resolvable else hci.Address.ANY_RANDOM
            )
            return link, local
        attempt += 1
        await tester.wait(max(0, start + 20_003 - tester.radio.now_us))
    assert reject, 'no CONNECT_IND in the privacy observation interval'
    _no_initiation_complete(tester)
    return None


async def _privacy_initiating_scenario(tester, *, scenario):
    peer = LOWER_PERIPHERAL_PUBLIC_ADDRESS
    own_type = 0 if scenario in ('directed_peer', 'device_directed') else 2
    device = scenario in ('device', 'device_directed')
    zero_peer = scenario in ('local_only', 'resolution_disabled')
    disabled = scenario in ('resolution_disabled', 'ignore_disabled')
    await tester.command(hci.HCI_LE_Set_Resolvable_Private_Address_Timeout_Command(1))
    await privacy_peer.add_peer(
        tester,
        identity=peer,
        peer_irk=bytes(16) if zero_peer else privacy_peer.PEER_IRK,
        device=device,
        enabled=not disabled,
    )
    if scenario == 'resolution_disabled':
        for index in (1, 2):
            other = hci.Address(bytes([index]) + bytes(peer)[1:], 0)
            await privacy_peer.add_peer(
                tester,
                identity=other,
                peer_irk=bytes(16),
                local_irk=bytes([index]) * 16,
                enabled=False,
            )
            await tester.command(
                hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(0, other)
            )
        await tester.command(
            hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(0, peer)
        )

    async def initiate():
        await tester.command(
            create_connection(
                own_address_type=own_type,
                peer_address_type=0,
                peer_address=peer,
                initiator_filter_policy=int(scenario == 'resolution_disabled'),
                le_scan_interval=INITIATING_SCAN_INTERVAL,
                le_scan_window=INITIATING_SCAN_INTERVAL,
                connection_interval_min=CONNECTION_INTERVAL,
                connection_interval_max=CONNECTION_INTERVAL,
                supervision_timeout=SUPERVISION_TIMEOUT,
            )
        )

    await initiate()
    directed = scenario in (
        'directed_peer',
        'wrong_target',
        'identity_target',
        'ignore_directed',
        'device_directed',
    )
    address = (
        peer
        if zero_peer or device or scenario.startswith('ignore')
        else privacy_peer.private_address(privacy_peer.PEER_IRK)
    )
    target = (
        (
            DUT_ADDRESS
            if own_type == 0
            else privacy_peer.private_address(privacy_peer.LOCAL_IRK)
        )
        if directed
        else None
    )
    if scenario.startswith('ignore'):
        await _privacy_initiating_advertisement(
            tester, address, peer, target=target, own_type=own_type, reject=True
        )
        await tester.command(hci.HCI_LE_Create_Connection_Cancel_Command())
        return
    if scenario in ('peer', 'directed_peer'):
        await _privacy_initiating_advertisement(
            tester,
            privacy_peer.private_address(bytes(range(16))),
            peer,
            target=target,
            own_type=own_type,
            reject=True,
        )
    if scenario in ('wrong_target', 'identity_target'):
        bad = (
            DUT_ADDRESS
            if scenario == 'identity_target'
            else privacy_peer.private_address(bytes(range(16)))
        )
        await _privacy_initiating_advertisement(
            tester, address, peer, target=bad, own_type=own_type, reject=True
        )
    previous = None
    for iteration in range(2 if scenario in ('wrong_target', 'identity_target') else 1):
        if iteration:
            await initiate()
        link, local = await _privacy_initiating_advertisement(
            tester, address, peer, target=target, own_type=own_type
        )
        if previous is not None:
            assert local != previous
        previous = local
        await _data_events(tester, link, 1 if scenario == 'local_only' else 100)
        await link.disconnect(local=True)


async def privacy_initiation_local_irk(tester):
    """Privacy initiation local irk.

    Initial Condition:
        Standby; shared IRKs and public peer identity as configured by the source.

    Test Steps:
        Configure identity advertising, add a zero Peer IRK and nonzero Local
        IRK, enable initiation, receive CONNECT_IND with local RPA, check
        Enhanced Connection Complete and CSA event zero, then exchange the first
        data packet.

    Expected Outcome:
        Every negative phase remains silent; positive phases complete the specified
        RPA/address checks, Host events and independently timed data exchanges.

    Reference:
        LL.TS.p28, Section 4.3.4.6, PDF pages 535-537, Table 4.3-17;
        Core Vol 6, Part B, 4.7 and 6.4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_initiating_scenario(tester, scenario='local_only')


# Verify that an initiator with only a Local IRK preserves the received AdvA and
# generates a resolvable private InitA in CONNECT_IND.
PROCEDURES |= procedure_cases(
    privacy_initiation_local_irk,
    {
        'LL/CON/INI/BV-42-C': (
            'Network Privacy – Connection Establishment responding to '
            'connectable undirected advertising, Initiator, HCI LE Channel '
            'Selection Algorithm supported',
            {},
        ),
    },
)


async def privacy_initiation_peer_resolution(tester):
    """Privacy initiation peer resolution.

    Initial Condition:
        Standby; shared IRKs and public peer identity as configured by the source.

    Test Steps:
        Steps 1-7: advertise an RPA generated with the wrong IRK for at least
        four scan intervals plus three scan windows and verify no CONNECT_IND.
        Steps 8-16: switch to a valid peer RPA, receive CONNECT_IND, verify
        Enhanced Connection Complete with peer identity and RPA, CSA event zero,
        100 data events and local disconnection.

    Expected Outcome:
        Every negative phase remains silent; positive phases complete the specified
        RPA/address checks, Host events and independently timed data exchanges.

    Reference:
        LL.TS.p28, Section 4.3.4.7, PDF pages 537-539, Table 4.3-18;
        Core Vol 6, Part B, 4.7 and 6.4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_initiating_scenario(tester, scenario='peer')


# Verify that initiation ignores an unresolved peer RPA and connects after the Lower
# Tester switches to an RPA matching the resolving list.
PROCEDURES |= procedure_cases(
    privacy_initiation_peer_resolution,
    {
        'LL/CON/INI/BV-43-C': (
            'Network Privacy – Connection Establishment using resolving list, '
            'Initiator, HCI LE Channel Selection Algorithm supported',
            {},
        ),
    },
)


async def privacy_initiation_directed_peer(tester):
    """Privacy initiation directed peer.

    Initial Condition:
        Standby; shared IRKs and public peer identity as configured by the source.

    Test Steps:
        Steps 1-7: advertise directed events to the local public identity with
        an unresolved peer RPA for twenty advertising intervals. Steps 8-16:
        switch to a resolvable peer RPA, check unchanged wire AdvA and public
        InitA in CONNECT_IND, Enhanced Connection Complete, CSA event zero, 100
        data events and local disconnection.

    Expected Outcome:
        Every negative phase remains silent; positive phases complete the specified
        RPA/address checks, Host events and independently timed data exchanges.

    Reference:
        LL.TS.p28, Section 4.3.4.8, PDF pages 540-542, Table 4.3-19;
        Core Vol 6, Part B, 4.7 and 6.4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_initiating_scenario(tester, scenario='directed_peer')


# Verify that initiation accepts directed advertising only after the peer RPA resolves,
# while using the local public address as InitA.
PROCEDURES |= procedure_cases(
    privacy_initiation_directed_peer,
    {
        'LL/CON/INI/BV-44-C': (
            'Network Privacy – Connection Establishment using directed '
            'advertising and resolving list, Initiator, HCI LE Channel '
            'Selection Algorithm supported',
            {},
        ),
    },
)


async def privacy_initiation_wrong_target(tester):
    """Privacy initiation wrong target.

    Initial Condition:
        Standby; shared IRKs and public peer identity as configured by the source.

    Test Steps:
        Steps 1-7: install mutual IRKs and advertise valid peer RPAs with a
        wrong-IRK TargetA for twenty intervals; require no CONNECT_IND. Steps
        8-15: use a correct TargetA, verify generated local RPA differs from
        TargetA, Enhanced Connection Complete, CSA zero and 100 data events.
        Steps 16-18: retain the connection beyond the one-second RPA timeout,
        disconnect, repeat initiation and require a new InitA.

    Expected Outcome:
        Every negative phase remains silent; positive phases complete the specified
        RPA/address checks, Host events and independently timed data exchanges.

    Source Correction:
        The source calls the directed advertisement's target InitA. Core Vol 6,
        Part B, 2.3.1.2 names that field TargetA; InitA belongs to CONNECT_IND.
        Use the Core field name. This is a local correction, not SIG errata.

    Reference:
        LL.TS.p28, Section 4.3.4.9, PDF pages 543-545, Table 4.3-20;
        Core Vol 6, Part B, 4.7 and 6.4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_initiating_scenario(tester, scenario='wrong_target')


# Verify rejection of directed advertising with an unresolvable TargetA, connection
# with a valid target, and local RPA renewal after timeout.
PROCEDURES |= procedure_cases(
    privacy_initiation_wrong_target,
    {
        'LL/CON/INI/BV-45-C': (
            'Network Privacy – Connection Establishment using directed '
            'advertising with wrong address and resolving list, Initiator, HCI '
            'LE Channel Selection Algorithm supported',
            {},
        ),
    },
)


async def privacy_initiation_identity_target(tester):
    """Privacy initiation identity target.

    Initial Condition:
        Standby; shared IRKs and public peer identity as configured by the source.

    Test Steps:
        Steps 1-6: initiate with own type 2 and advertise valid peer RPAs
        directed to the local public identity for twenty intervals; require no
        CONNECT_IND. Steps 7-14: replace TargetA with an RPA resolving with
        Local IRK, verify local RPA in CONNECT_IND, Enhanced Connection
        Complete, CSA zero and 100 data events. Steps 15-17: disconnect after
        timeout, reconnect and verify renewed InitA.

    Expected Outcome:
        Every negative phase remains silent; positive phases complete the specified
        RPA/address checks, Host events and independently timed data exchanges.

    Source Correction:
        The source calls the directed advertisement's target InitA. Core Vol 6,
        Part B, 2.3.1.2 names that field TargetA; InitA belongs to CONNECT_IND.
        Use the Core field name. This is a local correction, not SIG errata.

    Reference:
        LL.TS.p28, Section 4.3.4.10, PDF pages 546-548, Table 4.3-21;
        Core Vol 6, Part B, 4.7 and 6.4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_initiating_scenario(tester, scenario='identity_target')


# Verify that private initiation rejects directed advertising targeting the local
# identity, accepts a resolvable TargetA, and renews InitA after timeout.
PROCEDURES |= procedure_cases(
    privacy_initiation_identity_target,
    {
        'LL/CON/INI/BV-46-C': (
            'Network Privacy – Connection Establishment using directed '
            'advertising with identity address and resolving list, Initiator, '
            'HCI LE Channel Selection Algorithm supported',
            {},
        ),
    },
)


async def privacy_initiation_device_identity(tester):
    """Privacy initiation device identity.

    Initial Condition:
        Standby; shared IRKs and public peer identity as configured by the source.

    Test Steps:
        Steps 1-5: install the peer IRK in device privacy mode, enable
        initiation and advertise the peer identity until CONNECT_IND. Steps
        6-12: verify the identity in Enhanced Connection Complete, CSA zero, 100
        data exchanges and local termination.

    Expected Outcome:
        Every negative phase remains silent; positive phases complete the specified
        RPA/address checks, Host events and independently timed data exchanges.

    Reference:
        LL.TS.p28, Section 4.3.4.15, PDF pages 562-564, Table 4.3-29;
        Core Vol 6, Part B, 4.7 and 6.4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_initiating_scenario(tester, scenario='device')


# Verify that device privacy permits connection to a peer advertising its identity
# address despite an associated Peer IRK in the resolving list.
PROCEDURES |= procedure_cases(
    privacy_initiation_device_identity,
    {
        'LL/CON/INI/BV-47-C': (
            'Device Privacy – Connection Establishment using resolving list, '
            'Initiator, Accept Identity Address, HCI LE Channel Selection '
            'Algorithm supported',
            {},
        ),
    },
)


async def privacy_initiation_device_directed(tester):
    """Privacy initiation device directed.

    Initial Condition:
        Standby; shared IRKs and public peer identity as configured by the source.

    Test Steps:
        Steps 1-5: install device privacy mode, enable initiation with a public
        local identity and advertise the peer identity directed to it. Steps
        6-12: verify identity addresses in CONNECT_IND and Enhanced Connection
        Complete, CSA zero, 100 data exchanges and local termination.

    Expected Outcome:
        Every negative phase remains silent; positive phases complete the specified
        RPA/address checks, Host events and independently timed data exchanges.

    Reference:
        LL.TS.p28, Section 4.3.4.16, PDF pages 564-566, Table 4.3-30;
        Core Vol 6, Part B, 4.7 and 6.4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_initiating_scenario(tester, scenario='device_directed')


# Verify that device privacy permits connection to directed advertising from a peer
# using its identity address despite an associated Peer IRK.
PROCEDURES |= procedure_cases(
    privacy_initiation_device_directed,
    {
        'LL/CON/INI/BV-48-C': (
            'Device Privacy – Connection Establishment using directed '
            'advertising and resolving list, Initiator, Accept Identity '
            'Address, HCI LE Channel Selection Algorithm supported',
            {},
        ),
    },
)


async def privacy_initiation_resolution_disabled(tester):
    """Privacy initiation resolution disabled.

    Initial Condition:
        Standby; shared IRKs and public peer identity as configured by the source.

    Test Steps:
        Steps 1-4: add the peer with zero Peer IRK and valid Local IRK, plus two
        other records with distinct Local IRKs and three accept-list entries;
        keep resolution disabled. Steps 5-11: initiate with own type 2 and
        policy 1, advertise the peer identity and verify InitA resolves with
        that peer record's Local IRK, Enhanced Connection Complete and CSA zero.
        Steps 12-14: exchange data and terminate.

    Expected Outcome:
        Every negative phase remains silent; positive phases complete the specified
        RPA/address checks, Host events and independently timed data exchanges.

    Reference:
        LL.TS.p28, Section 4.3.4.18, PDF pages 573-575, Table 4.3-32;
        Core Vol 6, Part B, 4.7 and 6.4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_initiating_scenario(tester, scenario='resolution_disabled')


# Verify that initiation with address resolution disabled still matches a peer identity
# in the Filter Accept List despite a stored Local IRK.
PROCEDURES |= procedure_cases(
    privacy_initiation_resolution_disabled,
    {
        'LL/CON/INI/BV-49-C': (
            'Network Privacy – Connection Establishment using Filter Accept '
            'List and resolving list with address resolution disabled, HCI LE '
            'Channel Selection Algorithm supported',
            {},
        ),
    },
)


async def privacy_initiation_reject_identity(tester, *, directed=False, enabled=True):
    """Network Privacy: Connection Establishment, Ignore Identity Address.

    Initial Condition:
        A nonzero Peer IRK is associated with the public peer identity in network
        privacy mode. Address resolution is enabled or disabled per Table 4.3-28.

    Test Case Configuration:
        LL/CON/INI/BV-18-C: undirected advertising, address resolution enabled.
        LL/CON/INI/BV-19-C: directed advertising, address resolution enabled.
        LL/CON/INI/BV-24-C: undirected advertising, address resolution disabled.

    Test Steps:
        1-3. Configure identity advertising, populate the resolving list and
             start initiation using the peer identity and local private address.
        4-6. Send ADV_IND or directed ADV_DIRECT_IND for twenty advertising
             intervals; verify no CONNECT_IND or connection event is produced.
        7. Stop advertising and cancel the pending initiation as postamble.

    Expected Outcome:
        Network mode rejects peer identity advertising even with resolution off.

    Reference:
        LL.TS.p28, Section 4.3.4.14, PDF pages 560-562, Table 4.3-28,
        Figure 4.3-34; Core Vol 6, Part B, 4.7 and 6.4.
    """
    scenario = (
        'ignore_directed' if directed else 'ignore' if enabled else 'ignore_disabled'
    )
    await _privacy_initiating_scenario(tester, scenario=scenario)


# Verify that network privacy rejects peer identity advertising with an associated Peer
# IRK, for directed or undirected events and with resolution enabled or disabled.
PROCEDURES |= procedure_cases(
    privacy_initiation_reject_identity,
    {
        'LL/CON/INI/BV-18-C': (
            'Network Privacy – Connection Establishment using resolving list, '
            'Initiator, Ignore Identity Address',
            {},
        ),
        'LL/CON/INI/BV-19-C': (
            'Network Privacy – Connection Establishment using directed '
            'advertising and resolving list, Initiator, Ignore Identity '
            'Address',
            {'directed': True},
        ),
        'LL/CON/INI/BV-24-C': (
            'Network Privacy – Connection Establishment using resolving list, '
            'Initiator, Ignore Identity Address, with address resolution '
            'disabled',
            {'enabled': False},
        ),
    },
)


async def decision_initiation_phys(tester, *, phy):
    """Extended Scanning, Connection Initiation, Decision-Based Advertising.

    Initial Condition:
        Public local and peer addresses; Table 4.3-22 selects the primary and
        secondary PHYs and continuous scanning interval/window 0x10.

    Test Case Configuration:
        LL/CON/INI/BV-30-C: LE 1M.
        LL/CON/INI/BV-31-C: LE 2M.
        LL/CON/INI/BV-32-C: LE Coded.

    Test Steps:
        1-3. Install one all-outcomes decision instruction, initiate with policy 2,
             and advertise connectable decisions with six arbitrary data octets.
        4-6. Verify AUX_CONNECT_REQ at T_IFS, send AUX_CONNECT_RSP, and check
             Enhanced Connection Complete immediately followed by CSA#2 event.
        7-11. Verify the first transmit window, 100 data events with per-event and
              accumulated drift checks, then terminate the Central connection.
        12. Repeat, withholding the first response and requiring a second request.

    Expected Outcome:
        Both attempts establish on the selected PHY, with a valid Access Address,
        proper T_IFS/window timing and all 100 data exchanges; the missing first
        response leaves initiation pending for the retry.

    Reference:
        LL.TS.p28, Section 4.3.4.11, PDF pages 549-552, Tables 4.3-22/23,
        Figure 4.3-31; Core Vol 6, Part B, 4.4.4.2, 4.4.3.6 and 4.5.3.
    """
    for retry in (False, True):
        link = await _initiate_auxiliary(tester, phy=phy, retry=retry, decision=True)
        await _central_timing_events(
            tester,
            link,
            TIMING_EVENTS,
            window_delay_us=LL_CONN_AUX_WINDOW_DELAY_CODED_US
            if phy == Phy.LE_CODED
            else LL_CONN_AUX_WINDOW_DELAY_UNCODED_US,
        )
        await link.disconnect(local=True)


# Verify that an initiator follows decision advertising and establishes a Central
# connection on each selected PHY. The Lower Tester checks CSA#2 and subsequent data-
# channel traffic.
PROCEDURES |= procedure_cases(
    decision_initiation_phys,
    {
        f'LL/CON/INI/BV-{number}-C': (
            f'Extended Scanning, Connection Initiation – {name} PHY, '
            'Decision-Based Advertising',
            {'phy': phy},
        )
        for number, phy, name in [
            (30, Phy.LE_1M, 'LE 1M'),
            (31, Phy.LE_2M, 'LE 2M'),
            (32, Phy.LE_CODED, 'LE Coded'),
        ]
    },
)


async def _decision_initiating_observation(
    tester, *, decision=True, data=b'\0ABCD', expected
):
    deadline = tester.radio.now_us + 200_030
    attempt = 0
    found = False
    while tester.radio.now_us < deadline:
        start = tester.radio.now_us + 100
        await tester.transmit(
            37 + attempt % 3,
            ext_pdu(
                kind=AdvertisingPduType.ADV_DECISION_IND
                if decision
                else AdvertisingPduType.ADV_EXT_IND,
                mode=1,
                adi=0x3000,
                aux=aux_pointer(0, 900),
                data=data if decision else b'',
            ),
            start_us=start,
        )
        await tester.transmit(
            0,
            ext_pdu(address=LOWER_PERIPHERAL_PUBLIC_ADDRESS, mode=1, adi=0x3000),
            start_us=start + 900,
        )
        request = await tester.receive(0, duration_us=500)
        if request is not None:
            assert expected, 'AUX_CONNECT_REQ sent for a failed decision/policy'
            assert request.pdu[0] & 15 == AdvertisingPduType.CONNECT_IND
            assert request.pdu[2:14] == bytes(DUT_ADDRESS) + bytes(
                LOWER_PERIPHERAL_PUBLIC_ADDRESS
            )
            found = True
            break
        attempt += 1
        await tester.wait(max(0, start + 20_003 - tester.radio.now_us))
    assert found == expected
    await tester.command(hci.HCI_LE_Create_Connection_Cancel_Command())
    event = await tester.event(hci.HCI_LE_Enhanced_Connection_Complete_Event)
    assert event.status != 0


async def decision_initiation_field_checks(tester):
    """Extended Scanning, Connection Initiation, Decision-Based Advertising checks.

    Initial Condition:
        Public addresses; explicit decision-only initiation on LE 1M.

    Test Steps:
        Repeat steps 1-5 for every Table 4.3-37 round: install the field and
        parameters with Test_Flags 3, initiate with policy 2 and interval/window
        0x10, advertise the specified connectable decision payload, and require
        AUX_CONNECT_REQ only for passing rounds. Withhold its response; rejected
        rounds are observed for ten advertising intervals. Cancel and verify
        successful Command Complete followed by failed Enhanced Connection Complete.

    Expected Outcome:
        Rounds 1, 5, 7, 9 and 10 attempt connections; the others remain silent.

    Source Correction:
        The source mentions General_Flags, absent from Core Vol 4, Part E,
        7.8.145; omit it. Several row labels say fields 19/35/51 while their
        numeric columns specify 20/36/52; use the numeric columns, which match
        four-octet presence and the listed outcomes. These are local corrections,
        not adopted SIG errata.

    Reference:
        LL.TS.p28, CON/INI/BV-33-C, PDF pages 585-588, Table 4.3-37;
        Core Vol 4, Part E, 7.8.145/66; Vol 6, Part B, 4.4.3.6.
    """
    vectors = flag_vectors()
    selected = [vectors[index - 1] for index in (1, 2, 3, 6, 7, 12, 13, 18, 19)]
    for field, parameters, data, expected in [
        *(
            (vector.field, vector.parameters, vector.data, vector.result == 'P')
            for vector in selected
        ),
        (6, b'\2' + bytes(15), b'\0', True),
    ]:
        await tester.command(
            hci.HCI_LE_Set_Decision_Instructions_Command([3], [field], [parameters])
        )
        await tester.command(
            create_connection(
                extended=True,
                initiator_filter_policy=2,
                peer_address_type=0,
                peer_address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
                scan_intervals=[16],
                scan_windows=[16],
            )
        )
        await _decision_initiating_observation(tester, data=data, expected=expected)


# Verify that an initiator sends no connection request when the relevant decision-data
# field fails its configured check.
PROCEDURES |= procedure_cases(
    decision_initiation_field_checks,
    {
        'LL/CON/INI/BV-33-C': (
            'Extended Scanning, Connection Initiation, Decision-Based Advertising',
            {},
        ),
    },
)


async def decision_initiation_filter_policy(tester):
    """Extended Scanning, Connection Initiation, Decision-Based Advertising policy.

    Initial Condition:
        Public advertiser identity; a separate random address is used as the
        nonmatching accept-list entry.

    Test Steps:
        Repeat steps 1-7 for all nine Table 4.3-38 rounds: clear/add the specified
        accept-list entry, install field 20 masked ABCD comparison, initiate with
        policy 2/3/4 and interval/window 0x10, send ordinary or decision primary
        PDUs pointing to connectable AUX_ADV_IND. For passing rounds observe a
        request without replying; for failing rounds observe ten advertising
        intervals without a request. Cancel and check the failed completion event.

    Expected Outcome:
        Requests occur in rounds 2, 4, 5, 7, 8 and 9 only.

    Source Correction:
        Omit General_Flags, absent from Core Vol 4, Part E, 7.8.145. Interpret
        the displayed mask/value in octet order as FF FF FF FF 00 00 00 00 and
        41 42 43 44 00 00 00 00, consistent with the ABCD payload and that
        section's field comparison. These are local clarifications, not SIG errata.

    Reference:
        LL.TS.p28, CON/INI/BV-34-C, PDF pages 589-591, Table 4.3-38;
        Core Vol 4, Part E, 7.8.66/145; Vol 6, Part B, 4.3.4.
    """
    for listed, policy, decision, expected in [
        (False, 2, False, False),
        (False, 2, True, True),
        (False, 3, True, False),
        (True, 3, False, True),
        (True, 3, True, True),
        (False, 4, False, False),
        (True, 4, False, True),
        (False, 4, True, True),
        (True, 4, True, True),
    ]:
        peer = LOWER_PERIPHERAL_PUBLIC_ADDRESS if listed else PEER_ADDRESS
        await tester.command(hci.HCI_LE_Clear_Filter_Accept_List_Command())
        await tester.command(
            hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(peer.address_type, peer)
        )
        await tester.command(
            hci.HCI_LE_Set_Decision_Instructions_Command(
                [3],
                [20],
                [bytes.fromhex('ffffffff000000004142434400000000')],
            )
        )
        await tester.command(
            create_connection(
                extended=True,
                initiator_filter_policy=policy,
                peer_address_type=0,
                peer_address=LOWER_PERIPHERAL_PUBLIC_ADDRESS,
                scan_intervals=[16],
                scan_windows=[16],
            )
        )
        await _decision_initiating_observation(
            tester, decision=decision, expected=expected
        )


# Verify that extended initiation applies each selected Initiator Filter Policy to
# decision and non-decision advertising and Filter Accept List membership.
PROCEDURES |= procedure_cases(
    decision_initiation_filter_policy,
    {
        'LL/CON/INI/BV-34-C': (
            'Extended Scanning, Connection Initiation, Decision-Based Advertising, '
            'Initiator Filter Policy',
            {},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_connection(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
