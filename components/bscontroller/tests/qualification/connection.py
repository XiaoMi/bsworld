# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Lower Tester ACL connection procedures."""

import random
import struct
from dataclasses import replace
from itertools import pairwise

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_ADV_DELAY_MAX_US,
    LL_ADV_PRIMARY_CHANNELS,
    LL_CONN_AUX_WINDOW_DELAY_CODED_US,
    LL_CONN_AUX_WINDOW_DELAY_UNCODED_US,
    LL_CONN_INTERVAL_MAX,
    LL_CONN_INTERVAL_MIN,
    LL_CONN_INTERVAL_UNIT_US,
    LL_CONN_LATENCY_MAX,
    LL_CONN_WINDOW_SIZE_MAX,
    LL_CONN_WINDOW_SIZE_MIN,
    LL_EVENT_COUNTER_MASK,
    LL_IFS_US,
    LL_INSTANT_MIN_LISTENING_EVENTS,
    LL_RESPONSE_TIMEOUT_US,
    LL_SCA_PPM,
    LL_SUPERVISION_TIMEOUT_INTERVAL_FACTOR,
    LL_SUPERVISION_TIMEOUT_MAX,
    LL_SUPERVISION_TIMEOUT_MIN,
    LL_SUPERVISION_TIMEOUT_UNIT_US,
    PDU_ADV_ADDRESS_SIZE,
    PDU_ADV_ADDRESSES_END,
    PDU_ADV_ADI_DID_BITS,
    PDU_ADV_CHSEL_MASK,
    PDU_ADV_CONNECT_PAYLOAD_SIZE,
    PDU_ADV_CONNECT_SCA_SHIFT,
    PDU_ADV_CONNECT_SIZE,
    PDU_ADV_OFFSET_UNIT_30_US,
    PDU_ADV_RXADD_MASK,
    PDU_ADV_TXADD_MASK,
    PDU_ADV_TYPE_MASK,
    PDU_CONTROL_CONN_PARAM_OFFSET_COUNT,
    PDU_CONTROL_CONN_PARAM_OFFSET_UNSPECIFIED,
    PDU_CONTROL_FEATURE_SET_SIZE,
    PDU_CONTROL_VERSION_SIZE,
    PDU_DATA_EMPTY_TIME_CODED_S8_US,
    PDU_DATA_LLID_MASK,
    PDU_DATA_MD_MASK,
    PDU_DATA_NESN_MASK,
    PDU_DATA_OCTET_TIME_CODED_S8_US,
    PDU_DATA_PAYLOAD_SIZE_MAX,
    PDU_DATA_PAYLOAD_SIZE_MIN,
    PDU_DATA_PAYLOAD_TIME_CODED_MAX_US,
    PDU_DATA_PAYLOAD_TIME_CODED_MIN_US,
    PDU_DATA_PAYLOAD_TIME_MIN_US,
    PDU_DATA_SN_MASK,
    PDU_HEADER_SIZE,
    AdvertisingMode,
    AdvertisingPduType,
    ControlOpcode,
    LlId,
)
from bscontroller.linklayer.timing import LL_CLOCK_JITTER_US, LL_LOCAL_SCA_PPM
from bscontroller.radio.base import PHY_TIME_UNITS_PER_SECOND, Phy
from tests.qualification import privacy as privacy_peer
from tests.qualification.link import _check_parameter_update, _data_pdu, _wire_event
from tests.qualification.peer import PeerLink, establish
from tests.qualification.peer import finish_phy_update as _finish_update
from tests.qualification.peer import phy_bit as _bit
from tests.qualification.peer import phy_update_pdu as _update_pdu
from tests.qualification.tester import (
    DUT_ADDRESS,
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_DATA_ACCESS_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
    LinkParameters,
    adv_pdu,
    aux_pointer,
    check_access_address,
    create_connection,
    ext_pdu,
    legacy_parameters,
    request_pdu,
)

ErrorCode = hci.HCI_ErrorCode
PROCEDURE_COLLISION = (
    ErrorCode.LMP_ERROR_TRANSACTION_COLLISION_OR_LL_PROCEDURE_COLLISION_ERROR
)
Opcode = ControlOpcode

# The raw peer uses SID3, DID0 and auxiliary channel0 throughout each round.
ADVERTISING_SID = 3
AUXILIARY_CHANNEL = 0

# Selected primary-to-auxiliary offset, channel and advertising SID.
AUXILIARY_OFFSET_US = 6000

# Table4.3-22 selects scan interval/window0x0010 in625-us HCI units.
AUXILIARY_SCAN_INTERVAL = 16

# Selected finite RF observation budget; not an IUT protocol timer.
INITIATION_OBSERVATION_US = 1000000


async def _initiate_auxiliary(
    tester, *, phy, retry, decision=False, timeout=None, peer_factory=PeerLink
):
    timeout = SUPERVISION_TIMEOUT if timeout is None else timeout
    peer = LOWER_PERIPHERAL_PUBLIC_ADDRESS
    primary_phy = Phy.LE_CODED if phy == Phy.LE_CODED else Phy.LE_1M
    if decision:
        await tester.command(
            hci.HCI_LE_Set_Decision_Instructions_Command(
                [15], [0], [random.Random(30).randbytes(16)]
            )
        )
    await tester.command(
        create_connection(
            extended=True,
            initiator_filter_policy=2 if decision else 0,
            peer_address=peer,
            peer_address_type=peer.address_type,
            initiating_phys=1 << primary_phy - 1,
            scan_intervals=[AUXILIARY_SCAN_INTERVAL],
            scan_windows=[AUXILIARY_SCAN_INTERVAL],
            supervision_timeouts=[timeout],
        )
    )
    adi = ADVERTISING_SID << PDU_ADV_ADI_DID_BITS
    assert AUXILIARY_OFFSET_US % PDU_ADV_OFFSET_UNIT_30_US == 0
    generator = random.Random(0)
    deadline = tester.radio.now_us + INITIATION_OBSERVATION_US
    requests = 0
    while tester.radio.now_us < deadline:
        _no_initiation_complete(tester)
        start = tester.radio.now_us + 100
        await tester.transmit(
            37,
            ext_pdu(
                adi=adi,
                kind=AdvertisingPduType.ADV_DECISION_IND
                if decision
                else AdvertisingPduType.ADV_EXT_IND,
                data=b"\0" + generator.randbytes(6) if decision else b"",
                mode=AdvertisingMode.CONNECTABLE,
                aux=aux_pointer(AUXILIARY_CHANNEL, AUXILIARY_OFFSET_US, phy),
            ),
            start_us=start,
            phy=primary_phy,
        )
        end = await tester.transmit(
            AUXILIARY_CHANNEL,
            ext_pdu(address=peer, adi=adi, mode=AdvertisingMode.CONNECTABLE),
            start_us=start + AUXILIARY_OFFSET_US,
            phy=phy,
        )
        request = await tester.receive(
            AUXILIARY_CHANNEL,
            duration_us=LL_IFS_US + phy.address_us + CONNECT_IFS_TOLERANCE_US,
            phy=phy,
        )
        if request is not None:
            requests += 1
            assert request.valid_crc and request.phy == phy
            assert request.pdu == request_pdu(
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
                timeout,
            )
            check_access_address(parameters.access_address, phy=phy)
            if not retry or requests > 1:
                await tester.transmit(
                    AUXILIARY_CHANNEL,
                    ext_pdu(
                        kind=AdvertisingPduType.AUX_CONNECT_RSP,
                        address=peer,
                        target=DUT_ADDRESS,
                    ),
                    start_us=request.end_us + LL_IFS_US,
                    phy=phy,
                )
                link = peer_factory(
                    tester, parameters, request.end_us, central=True, csa2=True, phy=phy
                )
                delay = (
                    LL_CONN_AUX_WINDOW_DELAY_CODED_US
                    if phy == Phy.LE_CODED
                    else LL_CONN_AUX_WINDOW_DELAY_UNCODED_US
                )
                link.anchor = (
                    request.end_us
                    + delay
                    + parameters.window_offset * LL_CONN_INTERVAL_UNIT_US
                )
                event = await _connection_complete(tester, link, peer)
                assert isinstance(event, hci.HCI_LE_Enhanced_Connection_Complete_Event)
                assert bytes(event.local_resolvable_private_address) == bytes(
                    PDU_ADV_ADDRESS_SIZE
                )
                assert bytes(event.peer_resolvable_private_address) == bytes(
                    PDU_ADV_ADDRESS_SIZE
                )
                assert requests == (2 if retry else 1)
                return link
        next_start = (
            start + ADV_INTERVAL_US + generator.randrange(LL_ADV_DELAY_MAX_US + 1)
        )
        await tester.wait(max(0, next_start - tester.radio.now_us))
    raise AssertionError(
        'auxiliary initiation or unanswered-request retry did not complete'
    )


def _no_initiation_complete(tester):
    assert not any(
        isinstance(
            event,
            (
                hci.HCI_LE_Connection_Complete_Event,
                hci.HCI_LE_Enhanced_Connection_Complete_Event,
                hci.HCI_LE_Channel_Selection_Algorithm_Event,
                hci.HCI_Disconnection_Complete_Event,
            ),
        )
        for event in tester.events
    ), 'unanswered auxiliary request produced a connection or failure event'


async def _accept_remote_parameters(
    tester, link, interval, timeout, *, optional_error=None
):
    event = await tester.event(hci.HCI_LE_Remote_Connection_Parameter_Request_Event)
    assert (
        event.connection_handle,
        event.interval_min,
        event.interval_max,
        event.max_latency,
        event.timeout,
    ) == (link.handle, interval, interval, 0, timeout)
    result = await tester.command(
        hci.HCI_LE_Remote_Connection_Parameter_Request_Reply_Command(
            link.handle, interval, interval, 0, timeout, 0, 0
        )
    )
    assert result.connection_handle == link.handle
    if link.central:
        update = await link.control(Opcode.LL_CONNECTION_UPDATE_IND)
    else:
        response = await link.control(Opcode.LL_CONNECTION_PARAM_RSP)
        minimum, _, latency, timeout, *_ = _check_parameter_pdu(response)
        interval = minimum
        update = bytes([Opcode.LL_CONNECTION_UPDATE_IND]) + struct.pack(
            '<BHHHHH',
            LL_CONN_WINDOW_SIZE_MIN,
            0,
            interval,
            latency,
            timeout,
            link.counter + LL_INSTANT_MIN_LISTENING_EVENTS & LL_EVENT_COUNTER_MASK,
        )
        await link.exchange(update, llid=LlId.CONTROL, check_ack=True)
    await _check_parameter_update(
        tester, link, update, interval, timeout, optional_error=optional_error
    )


def _check_feature_pdu(declared, pdu, *, opcode=Opcode.LL_FEATURE_RSP):
    """Apply Send-to-Peer rules and the zero-peer response's USED octet."""
    assert len(pdu) == 1 + PDU_CONTROL_FEATURE_SET_SIZE
    assert pdu[0] == opcode
    response = opcode == Opcode.LL_FEATURE_RSP
    if response:
        assert pdu[1] == 0, 'zero-peer FeatureSet_USED octet was not cleared'
    features = int.from_bytes(pdu[1:], 'little')
    optional = (
        hci.LeFeatureMask.LE_PING
        | hci.LeFeatureMask.LL_PRIVACY
        | hci.LeFeatureMask.EXTENDED_SCANNER_FILTER_POLICIES
        | hci.LeFeatureMask.LE_EXTENDED_ADVERTISING
        | hci.LeFeatureMask.LE_PERIODIC_ADVERTISING
        | hci.LeFeatureMask.CONNECTIONLESS_CTE_TRANSMITTER
        | hci.LeFeatureMask.CONNECTIONLESS_CTR_RECEIVER
        | hci.LeFeatureMask.ANTENNA_SWITCHING_DURING_CTE_TRANSMISSION
        | hci.LeFeatureMask.ANTENNA_SWITCHING_DURING_CTE_RECEPTION
        | hci.LeFeatureMask.PERIODIC_ADVERTISING_ADI_SUPPORT
    )
    forbidden = (
        hci.LeFeatureMask.REMOTE_PUBLIC_KEY_VALIDATION
        | hci.LeFeatureMask.DECISION_BASED_ADVERTISING_FILTERING
        | hci.LeFeatureMask.CHANNEL_SOUNDING_TONE_QUALITY_INDICATION
    )
    defined = sum(
        1 << feature
        for feature in hci.LeFeature
        if feature < PDU_CONTROL_FEATURE_SET_SIZE * 8
    )
    required = declared & ~int(optional | forbidden)
    if response:
        required &= ~((1 << 8) - 1)
    assert features & required == required, 'required local feature absent'
    assert not features & (int(forbidden) | ~defined | ~declared), (
        'forbidden, RFU or undeclared feature reported to peer'
    )


def _check_parameter_pdu(payload):
    fields = struct.unpack('<HHHHBH6H', payload[1:])
    minimum, maximum, latency, timeout = fields[:4]
    assert LL_CONN_INTERVAL_MIN <= minimum <= maximum <= LL_CONN_INTERVAL_MAX
    assert fields[4] <= maximum
    assert 0 <= latency <= LL_CONN_LATENCY_MAX
    assert LL_SUPERVISION_TIMEOUT_MIN <= timeout <= LL_SUPERVISION_TIMEOUT_MAX
    assert (
        timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
        > LL_SUPERVISION_TIMEOUT_INTERVAL_FACTOR
        * (latency + 1)
        * maximum
        * LL_CONN_INTERVAL_UNIT_US
    )
    return fields


def _check_parameter_choice(update, accepted=None):
    """Validate a connection update and, when accepted, its negotiated fields."""
    assert len(update) == 1 + struct.calcsize('<BHHHHH')
    assert update[0] == Opcode.LL_CONNECTION_UPDATE_IND
    size, offset, interval, latency, timeout, _ = struct.unpack('<BHHHHH', update[1:])
    assert LL_CONN_INTERVAL_MIN <= interval <= LL_CONN_INTERVAL_MAX
    assert LL_CONN_WINDOW_SIZE_MIN <= size <= min(LL_CONN_WINDOW_SIZE_MAX, interval - 1)
    assert 0 <= offset <= interval
    assert 0 <= latency <= LL_CONN_LATENCY_MAX
    assert LL_SUPERVISION_TIMEOUT_MIN <= timeout <= LL_SUPERVISION_TIMEOUT_MAX
    assert (
        timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
        > LL_SUPERVISION_TIMEOUT_INTERVAL_FACTOR
        * (latency + 1)
        * interval
        * LL_CONN_INTERVAL_UNIT_US
    )
    if accepted is not None:
        minimum, maximum, agreed_latency, agreed_timeout = accepted[:4]
        assert minimum <= interval <= maximum
        assert (latency, timeout) == (agreed_latency, agreed_timeout)
    return interval, timeout


def _select_peer_interval(minimum, maximum, periodicity):
    """Prefer a periodic multiple without leaving the actual peer response range."""
    return next(
        (
            interval
            for interval in range(minimum, maximum + 1)
            if not periodicity or interval % periodicity == 0
        ),
        minimum,
    )


def _parameter_request(link, interval, timeout):
    return bytes([Opcode.LL_CONNECTION_PARAM_REQ]) + struct.pack(
        '<HHHHBH6H',
        interval,
        interval,
        0,
        timeout,
        0,
        link.counter,
        *[PDU_CONTROL_CONN_PARAM_OFFSET_UNSPECIFIED]
        * PDU_CONTROL_CONN_PARAM_OFFSET_COUNT,
    )


# LL.TS.p28, Tables 4.3-61/63: tuples use LL wire order RX octets/time, TX octets/time.
LENGTH_FIELDS = struct.Struct('<HHHH')
MAXIMUM = (PDU_DATA_PAYLOAD_SIZE_MAX, PDU_DATA_PAYLOAD_TIME_CODED_MAX_US) * 2
MINIMUM = (PDU_DATA_PAYLOAD_SIZE_MIN, PDU_DATA_PAYLOAD_TIME_MIN_US) * 2

# Core Vol 6, Part B, 4.5.10: old/new maxima during a DLE transition.
DLE_TRANSITION_PACKETS = 2


def _check_changed_events(tester, handle, before, after, intermediate=None):
    """Allow a local autonomous change followed by the peer's response, as the TS
    permits."""
    current = before
    count = 0
    while event := tester.take(
        lambda event: isinstance(event, hci.HCI_LE_Data_Length_Change_Event)
    ):
        count += 1
        assert count <= 2
        assert event.connection_handle == handle
        values = (
            event.max_tx_octets,
            event.max_tx_time,
            event.max_rx_octets,
            event.max_rx_time,
        )
        assert values != current, 'unchanged effective limits reported again'
        assert values == after or values == intermediate
        current = values
    assert current == after, 'effective change missing from HCI'


def _check_parameters(payload, opcode, supported):
    assert payload[0] == opcode and len(payload) == 1 + LENGTH_FIELDS.size
    values = LENGTH_FIELDS.unpack(payload[1:])
    assert all(
        (low <= value <= high for value, low, high in zip(values, MINIMUM, supported))
    )
    return values


class _LengthObserver:
    """Track wire DLE and PHY changes without discarding control PDUs during setup."""

    def __init__(self, tester, link, dut, peer, supported):
        self.tester, self.link = tester, link
        self.dut, self.peer, self.supported = dut, peer, supported
        self.current = _effective_lengths(dut, peer, link)
        self.changes = []
        self.packet_limits = {}
        self.grace = self.current
        self.grace_packets = 0

    def _change(self):
        current = _effective_lengths(self.dut, self.peer, self.link)
        if current != self.current:
            self.changes.append(current)
            self.current = current

    def receive(self, packet):
        self._change()
        payload = packet.pdu[PDU_HEADER_SIZE:]
        if packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL:
            if payload[0] == Opcode.LL_LENGTH_REQ:
                self.grace = self.current
                self.grace_packets = DLE_TRANSITION_PACKETS
                self.dut = _check_parameters(
                    payload, Opcode.LL_LENGTH_REQ, self.supported
                )
                self._change()
                return bytes([Opcode.LL_LENGTH_RSP]) + LENGTH_FIELDS.pack(*self.peer)
        elif payload:
            limits = self.current
            if self.grace_packets:
                limits = tuple(max(new, old) for new, old in zip(limits, self.grace))
                self.grace_packets -= 1
            self.packet_limits[packet.start_us] = limits
        return None

    def check(self, *, final=False):
        events = []
        while event := self.tester.take(
            lambda event: isinstance(event, hci.HCI_LE_Data_Length_Change_Event)
        ):
            assert event.connection_handle == self.link.handle
            events.append(
                (
                    event.max_tx_octets,
                    event.max_tx_time,
                    event.max_rx_octets,
                    event.max_rx_time,
                )
            )
        assert events == self.changes[: len(events)], (
            'incorrect effective-length changes'
        )
        del self.changes[: len(events)]
        if final:
            assert not self.changes, 'missing effective-length changes'
            assert not self.link.control_responses, 'unanswered Data Length Request'


async def _connection_event(
    link, data=b'', *, llid=LlId.CONTINUATION, response_timeout_after_us=None
):
    """Receive every DUT PDU in one event, following MD and acknowledging each
    sequence."""
    tester, channel = (link.tester, link.channel)
    packets = []
    start = link.anchor
    if not link.central:
        await link._send(channel, data, llid, start)
        data = b''
        start = tester.radio.now_us + 1 + LL_IFS_US
    while True:
        sn = link.nesn
        packet = await tester.receive(
            channel,
            start_us=max(tester.radio.now_us + 1, start - 1),
            duration_us=max(3000, start - tester.radio.now_us + 3000),
            **link.radio_parameters | {'phy': link.rx_phy},
        )
        if packet is None:
            if (
                response_timeout_after_us is not None
                and tester.radio.now_us >= response_timeout_after_us
                and any(
                    isinstance(event, hci.HCI_Disconnection_Complete_Event)
                    and (event.status, event.connection_handle, event.reason)
                    == (
                        ErrorCode.SUCCESS,
                        link.handle,
                        ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR,
                    )
                    for event in tester.events
                )
            ):
                pytest.skip('Inconclusive: DATA total absent at procedure timeout')
            assert packets, 'missing first DATA PDU in connection event'
            break
        assert packet.valid_crc
        assert packet.start_us == start
        assert int(bool(packet.pdu[0] & PDU_DATA_SN_MASK)) == sn
        link._accept(packet)
        packets.append(packet)
        more = bool(packet.pdu[0] & PDU_DATA_MD_MASK)
        if link.central or more:
            await link._send(
                channel,
                data,
                llid if data else LlId.CONTINUATION,
                packet.end_us + LL_IFS_US,
            )
            data = b''
        if not more:
            break
        start = tester.radio.now_us + 1 + LL_IFS_US
        assert start < link.anchor + link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
    link.anchor += link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
    link.counter = link.counter + 1 & LL_EVENT_COUNTER_MASK
    return packets


def _effective_lengths(dut, peer, link):
    rx, rt, tx, tt = dut
    peer_rx, peer_rt, peer_tx, peer_tt = peer
    tx, tt, rx, rt = (
        min(tx, peer_rx),
        min(tt, peer_rt),
        min(rx, peer_tx),
        min(rt, peer_tt),
    )
    if link.tx_phy == Phy.LE_CODED:
        rt = max(rt, PDU_DATA_PAYLOAD_TIME_CODED_MIN_US)
    if link.rx_phy == Phy.LE_CODED:
        receive_budget = min(
            rt, PDU_DATA_EMPTY_TIME_CODED_S8_US + rx * PDU_DATA_OCTET_TIME_CODED_S8_US
        )
        tt = max(
            PDU_DATA_PAYLOAD_TIME_CODED_MIN_US,
            min(
                tt,
                link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
                - receive_budget
                - 2 * LL_IFS_US,
            ),
        )
    return (tx, tt, rx, rt)


async def _supported_lengths(tester):
    maximum = await tester.command(hci.HCI_LE_Read_Maximum_Data_Length_Command())
    values = (
        maximum.supported_max_rx_octets,
        maximum.supported_max_rx_time,
        maximum.supported_max_tx_octets,
        maximum.supported_max_tx_time,
    )
    assert all(
        (low <= value <= high for value, low, high in zip(values, MINIMUM, MAXIMUM))
    )
    return values


# Filter both scanning and initiating peers through the Filter Accept List.
ADV_FILTER_ACCEPT_LIST_ONLY = 3
ADV_INTERVAL_US = 20000

# Source Correction: LL.TS.p28 Sections 4.3.2.1/4.3.3.1
# ChM=0x01 is a source error; the reviewed local correction enables channels 0/1.
COMMON_ADVERTISER_CHANNEL_MAP = 0x03


def _advertiser_connection_parameters(**changes):
    return replace(
        LinkParameters(
            access_address=LOWER_DATA_ACCESS_ADDRESS,
            crc_init=0x198891,
            window_size=7,
            window_offset=7,
            interval=8,
            timeout=30,
            channel_map=COMMON_ADVERTISER_CHANNEL_MAP,
            hop_sca=5,
        ),
        **changes,
    )


# Core Vol4, PartE: connection handles0x0F00..0x0FFF are reserved.
CONNECTION_HANDLE_MAX = 3839

# Common connection IXIT values, in1.25-ms interval and10-ms supervision units.
CONNECTION_INTERVAL = 24

# LL.TS.p28 active-mode request timing tolerance.
CONNECT_IFS_TOLERANCE_US = 2

# BV-37: adjacent directed advertising packet starts are at most0.5 ms apart.
DIRECTED_PACKET_INTERVAL_MAX_US = 500

# BI-05 maintains the original ACL and advertising for at least five seconds.
DUPLICATE_OBSERVATION_US = 5000000

# Unsigned, least-significant-octet-first event count used as advertising data.
EVENT_COUNT_SIZE = 4

# Valid static random address used only in the prohibited address-change command.
PROHIBITED_RANDOM_ADDRESS = hci.Address('C0:12:34:56:78:9A')

# Selected IXIT: full 30-ms scan window and 20-ms undirected advertising interval.
INITIATING_SCAN_INTERVAL = 48
SUPERVISION_TIMEOUT = 100


async def _advertise_connection(
    tester,
    peer,
    *,
    channel=37,
    directed=False,
    csa2=False,
    high_duty=False,
    reject=False,
):
    """Observe one complete initiation window without restarting the HCI procedure."""
    interval_us = INITIATING_SCAN_INTERVAL * hci.HCI_LE_SCAN_INTERVAL_UNIT_US
    deadline = tester.radio.now_us + 4 * interval_us + 3 * interval_us
    generator = random.Random(0)
    index = 0
    previous = None
    channels = LL_ADV_PRIMARY_CHANNELS if high_duty or channel is None else (channel,)
    while tester.radio.now_us < deadline:
        selected_channel = channels[index % len(channels)]
        start = tester.radio.now_us + 1
        if index % len(channels) == 0:
            event_start = start
        if high_duty and previous is not None:
            assert start - previous <= DIRECTED_PACKET_INTERVAL_MAX_US
        pdu = _advertisement(
            peer, directed=directed, csa2=csa2, count=index // len(channels)
        )
        end = await tester.transmit(selected_channel, pdu, start_us=start)
        request = await tester.receive(
            selected_channel,
            duration_us=LL_IFS_US + Phy.LE_1M.address_us + CONNECT_IFS_TOLERANCE_US,
        )
        if request is not None:
            assert not reject, 'CONNECT_IND sent to an excluded advertiser'
            assert request.valid_crc
            assert request.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.CONNECT_IND
            assert (
                request.pdu[1]
                == len(request.pdu) - PDU_HEADER_SIZE
                == PDU_ADV_CONNECT_PAYLOAD_SIZE
            )
            assert not request.pdu[0] & PDU_ADV_TXADD_MASK
            assert bool(request.pdu[0] & PDU_ADV_RXADD_MASK) == peer.is_random
            assert abs(request.start_us - end - LL_IFS_US) <= CONNECT_IFS_TOLERANCE_US
            assert not csa2 or request.pdu[0] & PDU_ADV_CHSEL_MASK
            assert request.pdu[PDU_HEADER_SIZE:PDU_ADV_ADDRESSES_END] == bytes(
                DUT_ADDRESS
            ) + bytes(peer)
            parameters = LinkParameters.decode(request.pdu[PDU_ADV_ADDRESSES_END:])
            assert (parameters.interval, parameters.latency, parameters.timeout) == (
                CONNECTION_INTERVAL,
                0,
                SUPERVISION_TIMEOUT,
            )
            check_access_address(parameters.access_address)
            link = PeerLink(tester, parameters, request.end_us, central=True, csa2=csa2)
            await _connection_complete(tester, link, peer)
            return link
        index += 1
        previous = start
        if not high_duty and index % len(channels) == 0:
            next_event = (
                event_start
                + ADV_INTERVAL_US
                + generator.randrange(LL_ADV_DELAY_MAX_US + 1)
            )
            await tester.wait(min(next_event, deadline + 1) - tester.radio.now_us)
    if reject:
        await tester.wait(max(0, deadline + 1 - tester.radio.now_us))
        assert not any(
            isinstance(
                event,
                (
                    hci.HCI_LE_Connection_Complete_Event,
                    hci.HCI_LE_Enhanced_Connection_Complete_Event,
                    hci.HCI_LE_Channel_Selection_Algorithm_Event,
                ),
            )
            for event in tester.events
        ), 'connection event reported for an excluded advertiser'
        return None
    raise AssertionError(
        'CONNECT_IND absent within four scan intervals plus three windows'
    )


def _advertisement(peer, *, directed=False, csa2=False, count=0):
    pdu = (
        request_pdu(
            AdvertisingPduType.ADV_DIRECT_IND, advertiser=DUT_ADDRESS, scanner=peer
        )
        if directed
        else adv_pdu(
            address=peer, csa2=csa2, data=count.to_bytes(EVENT_COUNT_SIZE, 'little')
        )
    )
    if directed and csa2:
        pdu = bytes([pdu[0] | PDU_ADV_CHSEL_MASK]) + pdu[1:]
    return pdu


async def _connection_complete(
    tester, link, peer, *, legacy_only=False, extended=False
):
    """Validate role-specific completion and event eligibility without reordering."""
    complete_types = (
        hci.HCI_LE_Connection_Complete_Event,
        hci.HCI_LE_Enhanced_Connection_Complete_Event,
    )
    selection_type = hci.HCI_LE_Channel_Selection_Algorithm_Event
    event = await tester.expect(
        lambda value: isinstance(
            value,
            (
                *complete_types,
                selection_type,
                hci.HCI_LE_Advertising_Set_Terminated_Event,
            ),
        )
    )
    assert isinstance(event, complete_types), 'event preceded Connection Complete'
    if legacy_only:
        assert isinstance(event, hci.HCI_LE_Connection_Complete_Event), (
            'masked Enhanced Connection Complete was emitted'
        )
    assert (event.status, event.role, event.peer_address) == (
        ErrorCode.SUCCESS,
        hci.Role.CENTRAL if link.central else hci.Role.PERIPHERAL,
        peer,
    )
    assert event.peer_address_type == peer.address_type
    assert (
        event.connection_interval,
        event.peripheral_latency,
        event.supervision_timeout,
    ) == (link.parameters.interval, link.parameters.latency, link.parameters.timeout)
    if not link.central:
        assert (
            event.central_clock_accuracy
            == link.parameters.hop_sca >> PDU_ADV_CONNECT_SCA_SHIFT
        )
    assert 0 <= event.connection_handle <= CONNECTION_HANDLE_MAX
    link.handle = event.connection_handle
    if link.central:
        selection = await tester.expect(lambda _event: True)
        assert isinstance(selection, selection_type), 'unexpected event before CSA'
        assert (selection.connection_handle, selection.channel_selection_algorithm) == (
            link.handle,
            int(link.csa2),
        )
    if extended:
        _completion_events(tester, link, extended=True)
    else:
        assert (
            tester.take(
                lambda value: isinstance(
                    value, (selection_type, hci.HCI_LE_Advertising_Set_Terminated_Event)
                )
            )
            is None
        )
    return event


async def _initiate(
    tester, peer, *, channel=37, directed=False, csa2=False, change_address=False
):
    await tester.command(
        create_connection(
            peer_address=peer,
            peer_address_type=peer.address_type,
            le_scan_interval=INITIATING_SCAN_INTERVAL,
            le_scan_window=INITIATING_SCAN_INTERVAL,
            connection_interval_min=CONNECTION_INTERVAL,
            connection_interval_max=CONNECTION_INTERVAL,
            supervision_timeout=SUPERVISION_TIMEOUT,
        )
    )
    if change_address:
        await tester.command(
            hci.HCI_LE_Set_Random_Address_Command(PROHIBITED_RANDOM_ADDRESS),
            status=ErrorCode.COMMAND_DISALLOWED_ERROR,
        )
    return await _advertise_connection(
        tester, peer, channel=channel, directed=directed, csa2=csa2, high_duty=directed
    )


# Selected observation budget, not a Controller protocol timer.
ADVERTISING_OBSERVATION_US = 600000

# The common IUT device name used as advertising data in the legacy case.
DEVICE_NAME = b'IUT'

# ADV/BI-02:50-ms connection interval and5-ms duplicate transmit window.
DUPLICATE_CONN_INTERVAL = 40
DUPLICATE_WINDOW_SIZE = 4

# Select legacy completion while keeping forbidden CSA/advertising events observable.
LEGACY_CONNECTION_EVENT_MASK = (1 << 64) - 1 & ~(
    1 << hci.HCI_LE_ENHANCED_CONNECTION_COMPLETE_EVENT - 1
    | 1 << hci.HCI_LE_ENHANCED_CONNECTION_COMPLETE_V2_EVENT - 1
)

# Keep both roles alive beyond setup rather than checking HCI notifications alone.
MAINTAINED_EVENTS = 10

# BV-52's exact two-octet advertising payload.
PARALLEL_ADVERTISING_DATA = b'\x0f\x0f'

# Leave room for advertising and its response before an existing peer's anchor.
PEER_EXCHANGE_BUDGET_US = 5000


async def _incoming_connection(
    tester, links, packet, *, parameters=None, peer=LOWER_CENTRAL_PUBLIC_ADDRESS
):
    end = packet.end_us + LL_IFS_US + Phy.LE_1M.airtime_us(PDU_ADV_CONNECT_SIZE)
    parameters = parameters or _interleaved_parameters(end, links)
    actual_end = await tester.transmit(
        37,
        request_pdu(
            AdvertisingPduType.CONNECT_IND, scanner=peer, data=parameters.encode()
        ),
        start_us=packet.end_us + LL_IFS_US,
    )
    assert actual_end == end
    link = PeerLink(tester, parameters, actual_end)
    await _connection_complete(tester, link, peer, legacy_only=True)
    assert all(link.handle != other.handle for other in links), (
        'connection handle reused'
    )
    return link


def _interleaved_parameters(packet_end, links, parameters=None):
    """Place the new peer halfway between the existing connection's anchors."""
    parameters = parameters or LinkParameters(
        access_address=LOWER_DATA_ACCESS_ADDRESS, window_offset=CONNECTION_INTERVAL - 1
    )
    if links:
        interval_us = parameters.interval * LL_CONN_INTERVAL_UNIT_US
        desired = links[0].anchor + interval_us // 2
        offset = (
            (
                desired
                - packet_end
                - LL_CONN_INTERVAL_UNIT_US
                + LL_CONN_INTERVAL_UNIT_US // 2
            )
            // LL_CONN_INTERVAL_UNIT_US
            % parameters.interval
        )
        parameters = replace(parameters, window_offset=offset)
    return parameters


def _no_creation_events(tester):
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
    ), 'unexpected connection creation, termination or legacy advertising event'


async def _service_peer(links):
    """Exchange the earliest independently scheduled peer event on the shared radio."""
    link = min(links, key=lambda value: value.anchor)
    previous_sn = link.sn
    packet = await link.exchange(check_ack=True)
    assert packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
    assert len(packet.pdu) == PDU_HEADER_SIZE and packet.pdu[1] == 0
    if link.central and link.counter > 1:
        assert bool(packet.pdu[0] & PDU_DATA_NESN_MASK) != bool(previous_sn), (
            'existing ACL stopped acknowledging DATA'
        )
    return link


async def _prepare_preferences(tester, link):
    await tester.command(hci.HCI_LE_Set_PHY_Command(link.handle, 3, 0, 0, 0))
    request = await link.control(Opcode.LL_PHY_REQ, required=False)
    if request is not None:
        if link.central:
            update = await link.control(
                Opcode.LL_PHY_UPDATE_IND,
                bytes([Opcode.LL_PHY_RSP, _bit(Phy.LE_1M), _bit(Phy.LE_1M)]),
            )
        else:
            update = _update_pdu(link, 0, 0)
            await link.exchange(update, llid=LlId.CONTROL, check_ack=True)
        await _finish_update(
            tester,
            link,
            update,
            event_required=True,
            allowed=(_bit(Phy.LE_1M), _bit(Phy.LE_1M)),
            allow_autonomous=link.central,
        )
    else:
        event = await tester.event(hci.HCI_LE_PHY_Update_Complete_Event)
        assert (event.status, event.connection_handle, event.tx_phy, event.rx_phy) == (
            ErrorCode.SUCCESS,
            link.handle,
            Phy.LE_1M,
            Phy.LE_1M,
        )


# A tester observation bound, not a protocol response deadline.
CONTROL_OBSERVATION_EVENTS = 16

# The MSCs prescribe one 0xFF data octet and four for the normal Central scheme.
ARQ_PAYLOAD = b'\xff'


def _restore_peer(link, packet, sent):
    """Restore normal peer sequencing after a CRC-valid, positively acknowledged
    exchange."""
    if link.central:
        link.sn = int(bool(sent[0] & PDU_DATA_SN_MASK))
        link.nesn = int(bool(sent[0] & PDU_DATA_NESN_MASK))
    else:
        sn, nesn = _sequence_bits(packet)
        link.sn, link.nesn = (nesn, sn ^ 1)


def _sequence_bits(packet):
    return (
        int(bool(packet.pdu[0] & PDU_DATA_SN_MASK)),
        int(bool(packet.pdu[0] & PDU_DATA_NESN_MASK)),
    )


def _completion_events(tester, link, *, extended=False):
    """Check event eligibility from the command path, not feature support alone."""
    assert 0 <= link.handle <= CONNECTION_HANDLE_MAX
    selections = []
    terminations = []
    while event := tester.take(
        lambda value: isinstance(
            value,
            (
                hci.HCI_LE_Channel_Selection_Algorithm_Event,
                hci.HCI_LE_Advertising_Set_Terminated_Event,
            ),
        )
    ):
        if isinstance(event, hci.HCI_LE_Channel_Selection_Algorithm_Event):
            selections.append(event)
        else:
            terminations.append(event)
    if not link.central and (not extended):
        assert not selections and (not terminations), (
            'legacy Peripheral emitted an ineligible event'
        )
    else:
        assert len(selections) <= 1
        if link.central:
            assert len(selections) == 1
        for event in selections:
            assert event.connection_handle == link.handle
            assert event.channel_selection_algorithm == int(link.csa2)
        assert len(terminations) == int(extended and (not link.central))
        for event in terminations:
            assert event.status == ErrorCode.SUCCESS
            assert event.advertising_handle == 0
            assert event.connection_handle == link.handle


def _central_first_window(
    parameters, anchor, *, window_delay_us=LL_CONN_INTERVAL_UNIT_US
):
    """Inclusive integer-us bounds from LL.TS.p28 Section 4.3.4's formula."""
    delay = window_delay_us + parameters.window_offset * LL_CONN_INTERVAL_UNIT_US
    size = parameters.window_size * LL_CONN_INTERVAL_UNIT_US
    ppm = LL_SCA_PPM[parameters.hop_sca >> PDU_ADV_CONNECT_SCA_SHIFT] + LL_LOCAL_SCA_PPM
    return (
        anchor - delay * ppm // PHY_TIME_UNITS_PER_SECOND - LL_CLOCK_JITTER_US,
        anchor
        + size
        + (delay + size) * ppm // PHY_TIME_UNITS_PER_SECOND
        + LL_CLOCK_JITTER_US,
    )


def _check_central_timing(
    parameters, anchor, starts, *, window_delay_us=LL_CONN_INTERVAL_UNIT_US
):
    """Check the first window and Table 4.3-12's individual/cumulative drift."""
    earliest, latest = _central_first_window(
        parameters, anchor, window_delay_us=window_delay_us
    )
    assert earliest <= starts[0] <= latest, 'first DATA outside transmit window'
    interval = parameters.interval * LL_CONN_INTERVAL_UNIT_US
    ppm = LL_SCA_PPM[parameters.hop_sca >> PDU_ADV_CONNECT_SCA_SHIFT]
    tolerance = 20 if ppm == 500 else 10
    assert all(
        abs(right - left - interval) <= tolerance for left, right in pairwise(starts)
    ), 'Central event interval outside the source tolerance'
    elapsed = (len(starts) - 1) * interval
    assert (
        abs(starts[-1] - starts[0] - elapsed) * PHY_TIME_UNITS_PER_SECOND
        <= elapsed * ppm
    ), 'Central cumulative clock drift exceeds its SCA'


async def _receive_central_event(link, *, window_delay_us=LL_CONN_INTERVAL_UNIT_US):
    """Receive a Central event within its allowed start window."""
    tester = link.tester
    ppm = LL_SCA_PPM[link.parameters.hop_sca >> PDU_ADV_CONNECT_SCA_SHIFT]
    tolerance = 20 if ppm == 500 else 10
    earliest, latest = (
        (link.anchor - tolerance, link.anchor + tolerance)
        if link.counter
        else _central_first_window(
            link.parameters, link.anchor, window_delay_us=window_delay_us
        )
    )
    # RX timeout ends address search, not the permitted packet-start window.
    packet = await tester.expect_pdu(
        link.channel,
        start_us=max(tester.radio.now_us + 1, earliest),
        duration_us=latest + link.rx_phy.address_us - tester.radio.now_us + 1,
        **(link.radio_parameters | {'phy': link.rx_phy}),
    )
    assert earliest <= packet.start_us <= latest
    assert packet.phy == link.rx_phy
    assert packet.pdu[0] & PDU_DATA_LLID_MASK in (
        LlId.CONTINUATION,
        LlId.START,
        LlId.CONTROL,
    )
    link.last_received = packet
    link.counter = (link.counter + 1) & LL_EVENT_COUNTER_MASK
    link.anchor = packet.start_us + link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
    return packet


# HCI observation resolution; PHY packet assertions retain microsecond timestamps.
HCI_OBSERVATION_STEP_US = 1000


async def _check_rf_stopped(link):
    """Observe the next Central event or probe the next Peripheral receive window."""
    while link.anchor <= link.tester.radio.now_us:
        link.anchor += link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
        link.counter = link.counter + 1 & LL_EVENT_COUNTER_MASK
    packet, _ = await _wire_event(
        link, lambda _packet: _data_pdu(link.sn, link.nesn), required=False
    )
    assert packet is None, 'connection RF continued after disconnection'


def _disconnected(tester):
    return tester.take(
        lambda event: isinstance(event, hci.HCI_Disconnection_Complete_Event)
    )


async def _observe_disconnection(tester, earliest, latest):
    """Reject an early event, then observe the first delivery without skipping its
    deadline."""
    assert tester.radio.now_us < earliest
    while tester.radio.now_us <= latest:
        event = _disconnected(tester)
        if event is not None:
            assert tester.radio.now_us >= earliest, (
                'disconnection before the allowed expiry window'
            )
            return event
        if tester.radio.now_us == latest:
            break
        target = min(tester.radio.now_us + HCI_OBSERVATION_STEP_US, latest)
        if tester.radio.now_us < earliest - 1:
            target = min(target, earliest - 1)
        await tester.wait(target - tester.radio.now_us)
    raise AssertionError('disconnection absent by the protocol deadline')


async def _procedure_timeout(tester, *, central=True, parameters=False):
    """Acknowledge a request without its response and check transaction expiry."""
    setup_before = tester.radio.now_us
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    if parameters:
        await link.control(
            Opcode.LL_FEATURE_RSP,
            bytes(
                [Opcode.LL_PERIPHERAL_FEATURE_REQ if central else Opcode.LL_FEATURE_REQ]
            )
            + int(hci.LeFeatureMask.CONNECTION_PARAMETERS_REQUEST_PROCEDURE).to_bytes(
                PDU_CONTROL_FEATURE_SET_SIZE, 'little'
            ),
        )
    opcode = Opcode.LL_CONNECTION_PARAM_REQ if parameters else Opcode.LL_FEATURE_REQ
    initial = link.last_received
    autonomous = (
        not parameters
        and central
        and initial.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
        and initial.pdu[PDU_HEADER_SIZE : PDU_HEADER_SIZE + 1] == bytes([opcode])
    )
    before = setup_before if autonomous else tester.radio.now_us
    await tester.command(
        hci.HCI_LE_Connection_Update_Command(
            link.handle, LL_CONN_INTERVAL_MIN, LL_CONN_INTERVAL_MIN, 0, 300, 0, 0
        )
        if parameters
        else hci.HCI_LE_Read_Remote_Features_Command(link.handle)
    )
    request = (
        initial.pdu[PDU_HEADER_SIZE:] if autonomous else await link.control(opcode)
    )
    request_at = link.last_received.start_us
    if parameters:
        _check_parameter_pdu(request)
    else:
        assert len(request) == 1 + PDU_CONTROL_FEATURE_SET_SIZE
    if central:
        assert link.last_received.pdu[PDU_HEADER_SIZE:] == request
    else:
        await link.exchange()
    # Queueing occurs after the trigger and no later than the first RF request.
    latest = request_at + LL_RESPONSE_TIMEOUT_US + HCI_OBSERVATION_STEP_US
    earliest = before + LL_RESPONSE_TIMEOUT_US - HCI_OBSERVATION_STEP_US
    completion_type = (
        hci.HCI_LE_Connection_Update_Complete_Event
        if parameters
        else hci.HCI_LE_Read_Remote_Features_Complete_Event
    )
    while link.anchor + HCI_OBSERVATION_STEP_US < earliest:
        assert _disconnected(tester) is None
        assert not any(isinstance(event, completion_type) for event in tester.events)
        packet = await link.exchange()
        if packet.pdu[1]:
            assert packet.pdu[PDU_HEADER_SIZE:] == request
    completed = False
    event = None
    terminal_types = (completion_type, hci.HCI_Disconnection_Complete_Event)
    while tester.radio.now_us <= latest:
        event = tester.take(lambda event: isinstance(event, terminal_types))
        if event is not None:
            assert tester.radio.now_us >= earliest, 'premature control timeout'
            if isinstance(event, hci.HCI_Disconnection_Complete_Event):
                break
            assert not completed, 'duplicate procedure completion'
            assert (event.status, event.connection_handle) == (
                ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR,
                link.handle,
            )
            completed = True
            continue
        if tester.radio.now_us == latest:
            raise AssertionError('control timeout absent by the protocol deadline')
        target = min(tester.radio.now_us + HCI_OBSERVATION_STEP_US, latest)
        if tester.radio.now_us < earliest - 1:
            target = min(target, earliest - 1)
        await tester.wait(target - tester.radio.now_us)
    assert isinstance(event, hci.HCI_Disconnection_Complete_Event)
    assert (event.status, event.connection_handle, event.reason) == (
        ErrorCode.SUCCESS,
        link.handle,
        ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR,
    )
    await _check_rf_stopped(link)
    assert not any(isinstance(event, terminal_types) for event in tester.events), (
        'procedure completion or duplicate disconnection after disconnection'
    )


# Independent peer version information, including nonzero manufacturer/subversion.
PEER_VERSION = (hci.SpecificationVersion.BLUETOOTH_CORE_5_4, 4660, 22136)


def _completion(tester):
    return tester.take(
        lambda event: isinstance(
            event, hci.HCI_Read_Remote_Version_Information_Complete_Event
        )
    )


async def _quiet_events(link, count, *, no_completion=False):
    """Maintain ARQ while requiring no extra version PDU or unsolicited Host
    completion."""
    for _ in range(count):
        sent_sn = link.sn
        packet = await link.exchange(check_ack=True)
        if link.central:
            assert int(bool(packet.pdu[0] & PDU_DATA_NESN_MASK)) != sent_sn
        assert _version_payload(packet) is None, (
            'extra LL_VERSION_IND in one connection'
        )
        assert _disconnected(link.tester) is None
        if no_completion:
            assert _completion(link.tester) is None


async def _version_link(tester, *, central=False, **parameters):
    return await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
        **parameters,
    )


def _version_payload(packet):
    if packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL and packet.pdu[
        PDU_HEADER_SIZE : PDU_HEADER_SIZE + 1
    ] == bytes([Opcode.LL_VERSION_IND]):
        assert len(packet.pdu) == PDU_HEADER_SIZE + PDU_CONTROL_VERSION_SIZE
        return packet.pdu[PDU_HEADER_SIZE:]
    return None


def _check_advertising(packet, *, directed=False, data=PARALLEL_ADVERTISING_DATA):
    assert packet.valid_crc
    expected = (
        AdvertisingPduType.ADV_DIRECT_IND if directed else AdvertisingPduType.ADV_IND
    )
    assert packet.pdu[0] & PDU_ADV_TYPE_MASK == expected
    assert not packet.pdu[0] & (PDU_ADV_TXADD_MASK | PDU_ADV_RXADD_MASK)
    assert packet.pdu[1] == len(packet.pdu) - PDU_HEADER_SIZE
    assert packet.pdu[PDU_HEADER_SIZE:] == bytes(DUT_ADDRESS) + (
        bytes(LOWER_CENTRAL_PUBLIC_ADDRESS) if directed else data
    )


async def _observe_advertising(
    tester, links, *, directed=False, data=PARALLEL_ADVERTISING_DATA
):
    deadline = tester.radio.now_us + ADVERTISING_OBSERVATION_US
    while tester.radio.now_us < deadline:
        until = min(
            deadline,
            min(
                (link.anchor - PEER_EXCHANGE_BUDGET_US for link in links),
                default=deadline,
            ),
        )
        if until <= tester.radio.now_us:
            await _service_peer(links)
            continue
        packet = await tester.receive(37, duration_us=until - tester.radio.now_us)
        if packet is None:
            continue
        _check_advertising(packet, directed=directed, data=data)
        return packet
    raise AssertionError('advertising absent while maintaining the existing ACL')


def _check_completion(event, handle, *, status=ErrorCode.SUCCESS):
    assert event is not None
    assert (event.status, event.connection_handle) == (status, handle)
    if status == ErrorCode.SUCCESS:
        assert (
            event.version,
            event.manufacturer_name,
            event.subversion,
        ) == PEER_VERSION


async def check_duplicate_advertiser_request(tester, *, privacy):
    await tester.masks(le=LEGACY_CONNECTION_EVENT_MASK)
    peer = LOWER_CENTRAL_PUBLIC_ADDRESS
    wire_peer = privacy_peer.private_address(privacy_peer.PEER_IRK) if privacy else peer
    await tester.command(hci.HCI_LE_Clear_Filter_Accept_List_Command())
    await tester.command(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(peer.address_type, peer)
    )
    await tester.command(
        legacy_parameters(
            advertising_filter_policy=0 if privacy else ADV_FILTER_ACCEPT_LIST_ONLY
        )
    )
    await tester.command(hci.HCI_LE_Set_Advertising_Data_Command(DEVICE_NAME))
    await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
    packet = await _observe_advertising(tester, [], data=DEVICE_NAME)
    previous = packet
    packet = await _observe_advertising(tester, [], data=DEVICE_NAME)
    assert (
        ADV_INTERVAL_US
        <= packet.start_us - previous.start_us
        <= ADV_INTERVAL_US + LL_ADV_DELAY_MAX_US
    )
    parameters = LinkParameters(
        access_address=LOWER_DATA_ACCESS_ADDRESS,
        interval=DUPLICATE_CONN_INTERVAL,
        window_offset=DUPLICATE_CONN_INTERVAL - 1,
    )
    link = await _incoming_connection(
        tester, [], packet, parameters=parameters, peer=wire_peer
    )
    await _service_peer([link])
    if privacy:
        await privacy_peer.add_peer(tester, identity=peer)
        await tester.command(
            legacy_parameters(advertising_filter_policy=ADV_FILTER_ACCEPT_LIST_ONLY)
        )
        wire_peer = privacy_peer.private_address(privacy_peer.PEER_IRK, 0x512345)
    await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
    packet = await _observe_advertising(tester, [link], data=DEVICE_NAME)
    end = packet.end_us + LL_IFS_US + Phy.LE_1M.airtime_us(PDU_ADV_CONNECT_SIZE)
    parameters = _interleaved_parameters(
        end,
        [link],
        LinkParameters(
            interval=DUPLICATE_CONN_INTERVAL, window_size=DUPLICATE_WINDOW_SIZE
        ),
    )
    assert parameters.access_address != link.parameters.access_address
    check_access_address(parameters.access_address)
    await tester.transmit(
        37,
        request_pdu(
            AdvertisingPduType.CONNECT_IND, scanner=wire_peer, data=parameters.encode()
        ),
        start_us=packet.end_us + LL_IFS_US,
    )
    duplicate = PeerLink(tester, parameters, end)
    deadline = end + DUPLICATE_OBSERVATION_US
    last_advertisement = end
    observed_advertisements = 0
    while tester.radio.now_us < deadline:
        next_peer = min((link, duplicate), key=lambda value: value.anchor)
        until = min(next_peer.anchor - PEER_EXCHANGE_BUDGET_US, deadline)
        if until > tester.radio.now_us:
            packet = await tester.receive(37, duration_us=until - tester.radio.now_us)
            if packet is not None:
                _check_advertising(packet, data=DEVICE_NAME)
                last_advertisement = packet.start_us
                observed_advertisements += 1
                continue
        if tester.radio.now_us >= deadline:
            break
        if next_peer is link:
            await _service_peer([link])
        else:
            channel = duplicate.channel
            await duplicate._send(channel, b'', LlId.CONTINUATION, duplicate.anchor)
            assert (
                await tester.receive(
                    channel,
                    duration_us=LL_IFS_US
                    + Phy.LE_1M.address_us
                    + CONNECT_IFS_TOLERANCE_US,
                    **duplicate.radio_parameters,
                )
                is None
            ), 'DUT accepted the duplicate connection'
            duplicate.anchor += parameters.interval * LL_CONN_INTERVAL_UNIT_US
            duplicate.counter = duplicate.counter + 1 & LL_EVENT_COUNTER_MASK
        _no_creation_events(tester)
        assert tester.radio.now_us - last_advertisement <= ADVERTISING_OBSERVATION_US, (
            'advertising stopped after duplicate CONNECT_IND'
        )
    assert tester.radio.now_us - last_advertisement <= ADVERTISING_OBSERVATION_US
    assert (
        observed_advertisements
        and duplicate.counter
        >= DUPLICATE_OBSERVATION_US // (parameters.interval * LL_CONN_INTERVAL_UNIT_US)
    )
    _no_creation_events(tester)

    await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(0))
    await link.disconnect()
    _no_creation_events(tester)


async def check_duplicate_initiator_request(tester, *, privacy):
    peer = (
        privacy_peer.private_address(privacy_peer.PEER_IRK)
        if privacy
        else LOWER_PERIPHERAL_PUBLIC_ADDRESS
    )
    link = await _initiate(tester, peer, channel=None)
    complete_types = (
        hci.HCI_LE_Connection_Complete_Event,
        hci.HCI_LE_Enhanced_Connection_Complete_Event,
    )

    async def advertising_cycle():
        for channel in LL_ADV_PRIMARY_CHANNELS:
            await tester.transmit(channel, adv_pdu(address=peer))
            assert (
                await tester.receive(
                    channel,
                    duration_us=LL_IFS_US
                    + Phy.LE_1M.address_us
                    + CONNECT_IFS_TOLERANCE_US,
                )
                is None
            ), 'second CONNECT_IND sent for an existing ACL'

    async def connection_event():
        previous_sn = link.sn
        packet = await link.exchange()
        if link.counter > 1:
            assert bool(packet.pdu[0] & PDU_DATA_NESN_MASK) != bool(previous_sn), (
                'original ACL stopped acknowledging peer DATA'
            )

    await connection_event()
    if privacy:
        await privacy_peer.add_peer(tester, identity=LOWER_PERIPHERAL_PUBLIC_ADDRESS)
        peer = privacy_peer.private_address(privacy_peer.PEER_IRK, 0x512345)
    await advertising_cycle()
    status = await tester.command(
        create_connection(peer_address=peer, peer_address_type=peer.address_type),
        status=(ErrorCode.SUCCESS, ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR),
    )
    deadline = tester.radio.now_us + DUPLICATE_OBSERVATION_US
    rejected = status.status == ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR
    while tester.radio.now_us < deadline:
        await connection_event()
        await advertising_cycle()
        while completion := tester.take(
            lambda event: isinstance(event, complete_types)
        ):
            assert not rejected, 'unexpected additional connection completion'
            assert completion.status == ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR
            rejected = True
            deadline = tester.radio.now_us + DUPLICATE_OBSERVATION_US
        assert not any(
            isinstance(
                event,
                (
                    hci.HCI_Disconnection_Complete_Event,
                    hci.HCI_LE_Channel_Selection_Algorithm_Event,
                    hci.HCI_LE_Advertising_Set_Terminated_Event,
                ),
            )
            for event in tester.events
        ), 'original ACL lost or duplicate connection reported'
    if not rejected:
        await tester.wait(0)
        command = hci.HCI_LE_Create_Connection_Cancel_Command()
        tester.send(bytes(command))
        event = await tester.expect(
            lambda event: isinstance(
                event, (hci.HCI_Command_Complete_Event, *complete_types)
            )
        )
        assert isinstance(event, hci.HCI_Command_Complete_Event)
        assert event.command_opcode == command.op_code
        assert event.num_hci_command_packets == 1
        assert event.return_parameters.status == ErrorCode.SUCCESS
        event = await tester.event(hci.HCI_LE_Connection_Complete_Event)
        assert event.status == ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
    _no_creation_events(tester)
    await link.disconnect()
    _no_creation_events(tester)
