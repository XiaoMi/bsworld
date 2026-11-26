# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Legacy advertising state (Core Vol 6, Part B, 4.4.2; Vol 4, Part E, 7.8.5-7.8.9).

Matrix: {ADV_IND, ADV_DIRECT_IND high/low duty, ADV_SCAN_IND, ADV_NONCONN_IND}
x {no request, SCAN_REQ, CONNECT_IND, filtered, malformed, wrong address}
x {event timing, duration, channel map}.

The IUT advertises; the Lower Tester plays the scanner or initiator. Scenarios
run on the ``air/harness.py`` Lower Tester / Upper Tester harness: ``ut`` sends
HCI commands and reads events, ``lt_tx`` scripts a peer PDU in the IUT's next
listen window, ``lt_rx`` reads the IUT's transmissions, and ``run`` advances the
shared clock. Extended advertising lives in ``extended_advertising_test.py``;
the scanner and initiator sides live in ``legacy_scanning_test.py``.
"""

from itertools import pairwise

import pytest

from bscontroller.hci import HCI_LE_ADV_INTERVAL_UNIT_US
from bscontroller.linklayer import (
    LL_ADV_DIRECT_HIGH_DUTY_TIMEOUT_US,
    LL_IFS_US,
    PDU_ADV_LEGACY_DATA_SIZE_MAX,
)
from bscontroller.linklayer.timing import LL_RESPONSE_START_TOLERANCE_US
from bscontroller.radio.base import RADIO_SWITCH_TIME_US, Phy
from tests.unit.linklayer.air.advertising import (
    LEGACY_INTERVAL,
    AdvType,
    configure_legacy,
    enable_legacy,
)
from tests.unit.linklayer.air.harness import (
    CONNECT_PARAMETERS,
    IUT_ADDRESS,
    OTHER,
    PEER,
    ErrorCode,
    PduType,
    address_of,
    connect_ind,
    hci,
    scan_req,
)

INTERVAL_US = LEGACY_INTERVAL * HCI_LE_ADV_INTERVAL_UNIT_US
# The Host unmasks every LE event, so the Enhanced form is used (7.7.65.10).
CONNECTED = hci.HCI_LE_Enhanced_Connection_Complete_Event
ADV_CHANNELS = [37, 38, 39]


def one_event(iut):
    """Run exactly one advertising event and return its transmissions."""
    iut.run(1)
    return iut.lt_rx_all()


# ---------------------------------------------------------------------------
# PDU content and event layout
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'kind,pdu_type,chsel',
    [
        (AdvType.ADV_IND, PduType.ADV_IND, True),
        (AdvType.ADV_SCAN_IND, PduType.ADV_SCAN_IND, False),
        (AdvType.ADV_NONCONN_IND, PduType.ADV_NONCONN_IND, False),
    ],
)
def test_undirected_pdu_on_each_channel(iut, kind, pdu_type, chsel):
    """An undirected event sends the chosen PDU on all three primary channels.

    Purpose: Verify that enabling undirected legacy advertising transmits one PDU
    of the configured type on channels 37, 38 and 39, each carrying the IUT
    address and the configured AdvData, with the ChSel bit set only on the
    connectable type (Vol 6, Part B, 2.3.1, 4.4.2.2).

    Steps:
        1. Upper Tester configures the advertising type with AdvData 02 01 06.
        2. Upper Tester enables advertising.
        3. Run one advertising event and read every transmission.

    Expected:
        - Transmissions occur on channels 37, 38, 39 in order.
        - Each PDU is the expected type, carries the IUT address and the AdvData.
        - The ChSel bit is set only for ADV_IND.
        - Nothing reaches the Host.

    Parameters range over ADV_IND (ChSel set), ADV_SCAN_IND and ADV_NONCONN_IND.
    """
    configure_legacy(iut, kind, data=b'\x02\x01\x06')
    enable_legacy(iut)
    packets = one_event(iut)
    assert [packet.channel for packet in packets] == ADV_CHANNELS
    for packet in packets:
        assert packet.kind == pdu_type
        assert address_of(packet.pdu) == IUT_ADDRESS
        assert packet.pdu[8:] == b'\x02\x01\x06'
        # ChSel is set only on connectable PDUs (2.3.1).
        assert bool(packet.pdu[0] & 0x20) == chsel
    iut.ut_rx_q_is_empty()


@pytest.mark.parametrize(
    'kind', [AdvType.ADV_DIRECT_IND, AdvType.ADV_DIRECT_IND_LOW_DUTY]
)
def test_directed_pdu_carries_target_not_data(iut, kind):
    """A directed event sends ADV_DIRECT_IND with AdvA and TargetA only.

    Purpose: Verify that directed legacy advertising transmits an ADV_DIRECT_IND
    carrying the IUT address and the peer target address and no AdvData, for both
    high and low duty cycle (Vol 6, Part B, 2.3.1.3, 4.4.2.4.3-4.4.2.4.4).

    Steps:
        1. Upper Tester configures directed advertising toward the peer.
        2. Upper Tester enables advertising.
        3. Run one advertising event and read the first transmission.

    Expected:
        - The PDU is ADV_DIRECT_IND.
        - AdvA is the IUT address and TargetA is the peer.
        - The PDU is 14 octets (header plus two addresses, no data).

    Parameters range over high and low duty cycle directed advertising.
    """
    configure_legacy(iut, kind)
    enable_legacy(iut)
    packet = one_event(iut)[0]
    assert packet.kind == PduType.ADV_DIRECT_IND
    assert address_of(packet.pdu) == IUT_ADDRESS
    assert address_of(packet.pdu, 8, rx=True) == PEER
    assert len(packet.pdu) == 14


@pytest.mark.parametrize('channel_map,channels', [(1, [37]), (2, [38]), (5, [37, 39])])
def test_channel_map_selects_primary_channels_in_order(iut, channel_map, channels):
    """The channel map selects which primary channels the event uses, in order.

    Purpose: Verify that the advertising channel map restricts transmissions to
    the enabled primary channels and visits them in ascending order (Vol 6,
    Part B, 4.4.2.2; Vol 4, Part E, 7.8.5).

    Steps:
        1. Upper Tester configures ADV_NONCONN_IND with the parametrized map.
        2. Upper Tester enables advertising.
        3. Run one advertising event and list the channels used.

    Expected:
        - The transmissions use exactly the mapped channels, in order.

    Parameters range over maps selecting {37}, {38}, and {37, 39}.
    """
    configure_legacy(iut, AdvType.ADV_NONCONN_IND, channel_map=channel_map)
    enable_legacy(iut)
    assert [packet.channel for packet in one_event(iut)] == channels


def test_connectable_event_listens_for_response_after_each_pdu(iut):
    """A connectable event opens a T_IFS response window after each PDU.

    Purpose: Verify that an ADV_IND event listens on each advertising channel one
    T_IFS after the PDU it just transmitted, so the inter-PDU gap and listen
    window start match the T_IFS timing and radio switch budget (Vol 6, Part B,
    4.4.2.3-4.4.2.4).

    Steps:
        1. Upper Tester configures ADV_IND.
        2. Upper Tester enables advertising.
        3. Run one advertising event and read every transmission.

    Expected:
        - Consecutive PDUs are spaced by T_IFS plus the address, response
          tolerance and radio switch times.
        - Three listen windows open, one per channel, each on the PDU's channel
          starting T_IFS after that PDU, adjusted by the response tolerance.
    """
    configure_legacy(iut)
    enable_legacy(iut)
    packets = one_event(iut)
    for previous, current in pairwise(packets):
        gap = current.start_us - previous.end_us
        assert gap == (
            LL_IFS_US
            + Phy.LE_1M.address_us
            + LL_RESPONSE_START_TOLERANCE_US
            + RADIO_SWITCH_TIME_US
            + 1
        )
    listens = iut.radio.listens
    assert len(listens) == 3
    for (start, _, channel, _), packet in zip(listens, packets):
        assert channel == packet.channel
        assert start == packet.end_us + LL_IFS_US - LL_RESPONSE_START_TOLERANCE_US


def test_nonconnectable_event_does_not_listen(iut):
    """A non-connectable, non-scannable event never listens for a response.

    Purpose: Verify that ADV_NONCONN_IND advertising transmits without opening any
    response window, since it accepts neither SCAN_REQ nor CONNECT_IND (Vol 6,
    Part B, 4.4.2.2).

    Steps:
        1. Upper Tester configures ADV_NONCONN_IND.
        2. Upper Tester enables advertising.
        3. Run one advertising event.

    Expected:
        - The radio recorded no listen windows.
    """
    configure_legacy(iut, AdvType.ADV_NONCONN_IND)
    enable_legacy(iut)
    one_event(iut)
    assert not iut.radio.listens


def test_advertising_interval_includes_random_delay(iut):
    """Events repeat at advInterval plus a varying advDelay.

    Purpose: Verify that consecutive advertising events are spaced by the
    configured advInterval plus a pseudo-random advDelay in [0, 10 ms], and that
    the delay actually varies between events (Vol 6, Part B, 4.4.2.2.1).

    Steps:
        1. Upper Tester configures ADV_NONCONN_IND.
        2. Upper Tester enables advertising.
        3. Run ten advertising intervals and collect the channel-37 start times.

    Expected:
        - At least eight events occurred.
        - Every inter-event gap lies in [advInterval, advInterval + 10 ms].
        - The gaps are not all identical.
    """
    configure_legacy(iut, AdvType.ADV_NONCONN_IND)
    enable_legacy(iut)
    iut.run(10 * INTERVAL_US)
    starts = [packet.start_us for packet in iut.lt_rx_all() if packet.channel == 37]
    gaps = [b - a for a, b in pairwise(starts)]
    assert len(starts) >= 8
    # advInterval + advDelay, advDelay in [0, 10 ms] (4.4.2.2.1).
    assert all(INTERVAL_US <= gap <= INTERVAL_US + 10_000 for gap in gaps)
    assert len(set(gaps)) > 1


def test_disable_stops_advertising(iut):
    """Disabling advertising stops all further transmissions.

    Purpose: Verify that Set Advertising Enable with disable halts the event
    chain so nothing is transmitted afterward (Vol 4, Part E, 7.8.9).

    Steps:
        1. Upper Tester configures ADV_NONCONN_IND and enables advertising.
        2. Run one advertising event.
        3. Upper Tester disables advertising.
        4. Run several more intervals.

    Expected:
        - No PDU is transmitted after the disable.
    """
    configure_legacy(iut, AdvType.ADV_NONCONN_IND)
    enable_legacy(iut)
    one_event(iut)
    enable_legacy(iut, False)
    iut.run(5 * INTERVAL_US)
    iut.lt_rx_q_is_empty()


# ---------------------------------------------------------------------------
# Scannable: SCAN_REQ / SCAN_RSP (4.4.2.3)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('kind', [AdvType.ADV_IND, AdvType.ADV_SCAN_IND])
def test_scan_request_answered_with_scan_response(iut, kind):
    """A matching SCAN_REQ is answered with SCAN_RSP T_IFS later.

    Purpose: Verify that a scannable advertiser replies to a SCAN_REQ addressed to
    it with a SCAN_RSP one T_IFS after the request on the same channel, carrying
    the IUT address and scan-response data, then continues the event on the
    remaining channels (Vol 6, Part B, 4.4.2.3).

    Steps:
        1. Upper Tester configures the scannable type with scan-response data.
        2. Upper Tester enables advertising.
        3. Lower Tester scripts a SCAN_REQ on channel 37.
        4. Run one advertising event and read every transmission.

    Expected:
        - The channel-37 sequence is advertisement, SCAN_RSP, then the event
          continues on channels 38 and 39.
        - The SCAN_RSP is on channel 37, starts T_IFS after the request, carries
          the IUT address and the scan-response data.
        - Nothing reaches the Host.

    Parameters range over ADV_IND and ADV_SCAN_IND.
    """
    configure_legacy(iut, kind, scan_response=b'\x03\x09ab')
    enable_legacy(iut)
    iut.lt_tx(scan_req(), channel=37)
    packets = one_event(iut)
    advertised = PduType.ADV_IND if kind == AdvType.ADV_IND else PduType.ADV_SCAN_IND
    assert [packet.kind for packet in packets] == [
        advertised,
        PduType.SCAN_RSP,
        advertised,
        advertised,
    ]
    response = packets[1]
    request = iut.radio.delivered[0]
    assert response.channel == 37
    assert response.start_us == request.end_us + LL_IFS_US
    assert address_of(response.pdu) == IUT_ADDRESS
    assert response.pdu[8:] == b'\x03\x09ab'
    # The event continues on the remaining channels.
    assert [packet.channel for packet in packets[2:]] == [38, 39]
    iut.ut_rx_q_is_empty()


@pytest.mark.parametrize(
    'request_pdu',
    [
        scan_req(advertiser=OTHER),  # addressed to another advertiser
        scan_req()[:-1],  # truncated
        bytes([scan_req()[0], 13]) + scan_req()[2:],  # length mismatch
        bytes([scan_req()[0] | 0x80]) + scan_req()[1:],  # wrong RxAdd type
    ],
    ids=['other-advertiser', 'truncated', 'length', 'rxadd'],
)
def test_invalid_scan_request_is_ignored(iut, request_pdu):
    """An invalid SCAN_REQ draws no SCAN_RSP.

    Purpose: Verify that a scannable advertiser ignores a SCAN_REQ that is
    addressed to another advertiser, truncated, length-mismatched, or carries the
    wrong RxAdd type, transmitting no SCAN_RSP (Vol 6, Part B, 4.4.2.3).

    Steps:
        1. Upper Tester configures ADV_IND with scan-response data.
        2. Upper Tester enables advertising.
        3. Lower Tester scripts the invalid SCAN_REQ on channel 37.
        4. Run one advertising event.

    Expected:
        - No transmission in the event is a SCAN_RSP.

    Parameters range over SCAN_REQ variants: wrong advertiser, truncated, length
    mismatch, and wrong RxAdd type.
    """
    configure_legacy(iut, scan_response=b'rsp')
    enable_legacy(iut)
    iut.lt_tx(request_pdu, channel=37)
    assert all(packet.kind != PduType.SCAN_RSP for packet in one_event(iut))


def test_nonscannable_types_ignore_scan_request(iut):
    """A non-scannable type never answers a SCAN_REQ.

    Purpose: Verify that a low duty cycle directed advertiser ignores a SCAN_REQ
    addressed to it, as directed advertising is not scannable (Vol 6, Part B,
    4.4.2.4.4).

    Steps:
        1. Upper Tester configures low duty cycle directed advertising.
        2. Upper Tester enables advertising.
        3. Lower Tester scripts a SCAN_REQ on channel 37.
        4. Run one advertising event.

    Expected:
        - No transmission in the event is a SCAN_RSP.
    """
    configure_legacy(iut, AdvType.ADV_DIRECT_IND_LOW_DUTY)
    enable_legacy(iut)
    iut.lt_tx(scan_req(), channel=37)
    assert all(packet.kind != PduType.SCAN_RSP for packet in one_event(iut))


@pytest.mark.parametrize('listed', [False, True])
@pytest.mark.parametrize('policy', [0, 1, 2, 3])
def test_scan_request_filter_policy(iut, policy, listed):
    """SCAN_REQ filtering follows the advertising filter policy bit 0.

    Purpose: Verify that a SCAN_REQ is answered only when the policy does not
    restrict scan requests, or when it does and the scanner is on the Filter
    Accept List (Vol 4, Part E, 7.8.5).

    Steps:
        1. If listed, Upper Tester adds the peer to the Filter Accept List.
        2. Upper Tester configures ADV_IND with the parametrized filter policy.
        3. Upper Tester enables advertising.
        4. Lower Tester scripts a SCAN_REQ from the peer on channel 37.
        5. Run one advertising event.

    Expected:
        - A SCAN_RSP is sent iff policy bit 0 is clear or the peer is listed.

    Parameters range over all four policies crossed with listed/unlisted.
    """
    if listed:
        iut.ut(
            hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(PEER.address_type, PEER)
        )
    configure_legacy(iut, scan_response=b'rsp', policy=policy)
    enable_legacy(iut)
    iut.lt_tx(scan_req(), channel=37)
    answered = any(packet.kind == PduType.SCAN_RSP for packet in one_event(iut))
    # Bit 0 restricts scan requests to the Filter Accept List (Vol 4, 7.8.5).
    assert answered == (not policy & 1 or listed)


# ---------------------------------------------------------------------------
# Connectable: CONNECT_IND (4.4.2.4)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('chsel', [False, True])
@pytest.mark.parametrize('reenable', [False, True])
def test_connect_request_creates_peripheral_connection(iut, chsel, reenable):
    """A CONNECT_IND makes the IUT a Peripheral and ends the event.

    Purpose: Verify that a CONNECT_IND to a connectable advertiser creates a
    Peripheral connection with the request's LLData, ends the advertising event
    without transmitting on later channels, raises no CSA event for legacy
    advertising, and anchors the connection transmitWindowDelay after the request
    (Vol 6, Part B, 4.4.2.4; Vol 4, Part E, 7.7.65.10, 7.7.65.20).

    Steps:
        1. Upper Tester configures ADV_IND and enables advertising.
        2. Lower Tester scripts a CONNECT_IND on channel 38, with the parametrized
           ChSel bit.
        3. Optionally repeat enable during RX on channel 38. Finish the event.

    Expected:
        - Transmissions stop after channel 38 (no channel 39).
        - The Host gets Enhanced Connection Complete as Peripheral with the
          request's interval and timeout, and nothing else.
        - The connection exists, advertising is disabled, CSA #2 use matches the
          ChSel bit, and the self address is the IUT address.
        - The first anchor is 1250 us (transmitWindowDelay) after the request.

    Parameters range over the ChSel bit clear and set.
    """
    configure_legacy(iut)
    enable_legacy(iut)
    iut.lt_tx(connect_ind(chsel=chsel), channel=38)
    if reenable:
        with iut.pause_radio(
            iut.link.advance_to_us(iut.now_us + 1), 'receive', occurrence=2
        ):
            enable_legacy(iut)
        packets = iut.lt_rx_all()
    else:
        packets = one_event(iut)
    # The connection ends the event: nothing is sent on channel 39.
    assert [packet.channel for packet in packets] == [37, 38]
    event = iut.ut_rx(
        CONNECTED,
        status=0,
        role=hci.Role.PERIPHERAL,
        peer_address=PEER,
        connection_interval=CONNECT_PARAMETERS.interval,
        supervision_timeout=CONNECT_PARAMETERS.timeout,
    )
    # No Channel Selection Algorithm event for legacy advertising: Vol 4, Part E,
    # 7.7.65.20 limits it to 7.8.12, 7.8.56 and 7.8.66.
    iut.ut_rx_q_is_empty()
    assert event.connection_handle in iut.link.connections
    assert not iut.ll.advertising.any_enabled()
    (connection,) = iut.link.connections.values()
    assert connection.csa2 == chsel and connection.self_address == IUT_ADDRESS
    # The first anchor follows transmitWindowDelay + WinOffset after CONNECT_IND.
    request = iut.radio.delivered[0]
    assert connection.anchor_us == request.end_us + 1250


@pytest.mark.parametrize(
    'lldata',
    [
        CONNECT_PARAMETERS.encode()[:-1],
        CONNECT_PARAMETERS.encode() + b'\x00',
    ],
    ids=['short', 'long'],
)
def test_malformed_connect_request_is_ignored(iut, lldata):
    """A CONNECT_IND with wrong LLData length is ignored.

    Purpose: Verify that a CONNECT_IND whose LLData is a byte short or a byte long
    is dropped, so the event runs on all three channels, no connection forms and
    advertising stays enabled (Vol 6, Part B, 2.3.3.1).

    Steps:
        1. Upper Tester configures ADV_IND and enables advertising.
        2. Lower Tester scripts the malformed CONNECT_IND on channel 37.
        3. Run one advertising event.

    Expected:
        - The event still sends three PDUs.
        - Nothing reaches the Host and advertising is still enabled.

    Parameters range over short and long LLData.
    """
    configure_legacy(iut)
    enable_legacy(iut)
    iut.lt_tx(connect_ind(parameters=lldata), channel=37)
    assert len(one_event(iut)) == 3
    iut.ut_rx_q_is_empty()
    assert iut.ll.advertising.any_enabled()


def test_connect_request_with_invalid_parameters_is_ignored(iut):
    """A CONNECT_IND with out-of-range LLData is ignored.

    Purpose: Verify that a CONNECT_IND carrying an illegal connection interval is
    rejected, so the event completes on all three channels and no connection is
    created (Vol 6, Part B, 2.3.3.1, 4.5.1).

    Steps:
        1. Upper Tester configures ADV_IND and enables advertising.
        2. Lower Tester scripts a CONNECT_IND whose interval is 5 on channel 37.
        3. Run one advertising event.

    Expected:
        - The event still sends three PDUs.
        - No connection is created.
    """
    from dataclasses import replace

    configure_legacy(iut)
    enable_legacy(iut)
    bad = replace(CONNECT_PARAMETERS, interval=5)
    iut.lt_tx(connect_ind(parameters=bad.encode()), channel=37)
    assert len(one_event(iut)) == 3
    assert not iut.link.connections


@pytest.mark.parametrize('kind', [AdvType.ADV_SCAN_IND, AdvType.ADV_NONCONN_IND])
def test_nonconnectable_types_ignore_connect_request(iut, kind):
    """A non-connectable type never accepts a CONNECT_IND.

    Purpose: Verify that scannable and non-connectable undirected advertisers
    ignore a CONNECT_IND, forming no connection (Vol 6, Part B, 4.4.2.2).

    Steps:
        1. Upper Tester configures the non-connectable type and enables it.
        2. Lower Tester scripts a CONNECT_IND on channel 37.
        3. Run one advertising event.

    Expected:
        - No connection is created.

    Parameters range over ADV_SCAN_IND and ADV_NONCONN_IND.
    """
    configure_legacy(iut, kind)
    enable_legacy(iut)
    iut.lt_tx(connect_ind(), channel=37)
    one_event(iut)
    assert not iut.link.connections


@pytest.mark.parametrize('listed', [False, True])
@pytest.mark.parametrize('policy', [0, 1, 2, 3])
def test_connect_request_filter_policy(iut, policy, listed):
    """CONNECT_IND filtering follows the advertising filter policy bit 1.

    Purpose: Verify that a CONNECT_IND creates a connection only when the policy
    does not restrict connection requests, or when it does and the initiator is
    on the Filter Accept List (Vol 4, Part E, 7.8.5).

    Steps:
        1. If listed, Upper Tester adds the peer to the Filter Accept List.
        2. Upper Tester configures ADV_IND with the parametrized filter policy.
        3. Upper Tester enables advertising.
        4. Lower Tester scripts a CONNECT_IND from the peer on channel 37.
        5. Run one advertising event.

    Expected:
        - A connection exists iff policy bit 1 is clear or the peer is listed.

    Parameters range over all four policies crossed with listed/unlisted.
    """
    if listed:
        iut.ut(
            hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(PEER.address_type, PEER)
        )
    configure_legacy(iut, policy=policy)
    enable_legacy(iut)
    iut.lt_tx(connect_ind(), channel=37)
    one_event(iut)
    # Bit 1 restricts connection requests to the Filter Accept List.
    assert bool(iut.link.connections) == (not policy & 2 or listed)


def test_connect_from_existing_peer_is_ignored(iut):
    """A second CONNECT_IND from an already-connected peer is ignored.

    Purpose: Verify that once a peer is connected, re-enabling advertising and
    receiving another CONNECT_IND from the same peer creates no duplicate
    connection while advertising continues (Vol 6, Part B, 4.4.2.4).

    Steps:
        1. Upper Tester configures ADV_IND and enables advertising.
        2. Lower Tester scripts a CONNECT_IND; run one event; Host gets connected.
        3. Upper Tester re-enables advertising.
        4. Lower Tester scripts another CONNECT_IND from the same peer.
        5. Run past one interval plus the maximum advDelay.

    Expected:
        - Exactly one connection exists and advertising is still enabled.
    """
    configure_legacy(iut)
    enable_legacy(iut)
    iut.lt_tx(connect_ind(), channel=37)
    one_event(iut)
    iut.ut_rx(CONNECTED, status=0)
    enable_legacy(iut)
    iut.radio.script.clear()
    iut.lt_tx(connect_ind(), channel=37)
    iut.run(INTERVAL_US + 10_001)
    assert len(iut.link.connections) == 1 and iut.ll.advertising.any_enabled()


# ---------------------------------------------------------------------------
# Directed advertising (4.4.2.4.3, 4.4.2.4.4)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'kind', [AdvType.ADV_DIRECT_IND, AdvType.ADV_DIRECT_IND_LOW_DUTY]
)
def test_directed_accepts_only_its_target(iut, kind):
    """Directed advertising connects only to its configured target.

    Purpose: Verify that a directed advertiser ignores a CONNECT_IND from a
    non-target initiator and accepts one from the configured peer (Vol 6,
    Part B, 4.4.2.4.3-4.4.2.4.4).

    Steps:
        1. Upper Tester configures directed advertising toward the peer.
        2. Upper Tester enables advertising.
        3. Lower Tester scripts a CONNECT_IND from another device on channel 37.
        4. Lower Tester scripts a CONNECT_IND from the peer on channel 38.
        5. Run one advertising event.

    Expected:
        - The Host gets Enhanced Connection Complete with the peer as the
          connected device.

    Parameters range over high and low duty cycle directed advertising.
    """
    configure_legacy(iut, kind)
    enable_legacy(iut)
    iut.lt_tx(connect_ind(initiator=OTHER), channel=37)
    iut.lt_tx(connect_ind(), channel=38)
    one_event(iut)
    iut.ut_rx(CONNECTED, status=0, peer_address=PEER)


def test_directed_ignores_filter_policy_for_target(iut):
    """Directed advertising connects to its target regardless of filter policy.

    Purpose: Verify that directed advertising accepts a CONNECT_IND from its
    configured target even when the filter policy would otherwise restrict
    connection requests, because directed advertising filters by TargetA, not the
    Filter Accept List (Vol 6, Part B, 4.4.2.4.4; Vol 4, Part E, 7.8.5).

    Steps:
        1. Upper Tester configures low duty cycle directed advertising with the
           most restrictive filter policy.
        2. Upper Tester enables advertising.
        3. Lower Tester scripts a CONNECT_IND from the peer on channel 37.
        4. Run one advertising event.

    Expected:
        - The Host gets Enhanced Connection Complete.
    """
    configure_legacy(iut, AdvType.ADV_DIRECT_IND_LOW_DUTY, policy=3)
    enable_legacy(iut)
    iut.lt_tx(connect_ind(), channel=37)
    one_event(iut)
    iut.ut_rx(CONNECTED, status=0)


def test_high_duty_directed_times_out_after_1_28_s(iut):
    """High duty cycle directed advertising stops with a timeout at 1.28 s.

    Purpose: Verify that high duty cycle directed advertising repeats at no more
    than 3.75 ms, stops by the 1.28 s deadline, and reports an advertising
    timeout to the Host (Vol 6, Part B, 4.4.2.4.3).

    Steps:
        1. Upper Tester configures high duty cycle directed advertising.
        2. Upper Tester enables advertising.
        3. Run past the high duty cycle deadline and read all transmissions.

    Expected:
        - Channel-37 events are spaced by at most 3.75 ms.
        - The last transmission ends by the deadline.
        - The Host gets Enhanced Connection Complete with
          ADVERTISING_TIMEOUT_ERROR for the peer, and nothing else.
        - Advertising is disabled.
    """
    configure_legacy(iut, AdvType.ADV_DIRECT_IND)
    enable_legacy(iut)
    start = iut.now_us
    iut.run(LL_ADV_DIRECT_HIGH_DUTY_TIMEOUT_US + 10_000)
    packets = iut.lt_rx_all()
    # High duty cycle: events at <= 3.75 ms (4.4.2.4.3), never past the deadline.
    starts = [packet.start_us for packet in packets if packet.channel == 37]
    assert all(b - a <= 3750 for a, b in pairwise(starts))
    assert packets[-1].end_us <= start + LL_ADV_DIRECT_HIGH_DUTY_TIMEOUT_US + 1
    iut.ut_rx(
        CONNECTED,
        status=ErrorCode.ADVERTISING_TIMEOUT_ERROR,
        peer_address=PEER,
    )
    iut.ut_rx_q_is_empty()
    assert not iut.ll.advertising.any_enabled()


def test_low_duty_directed_has_no_timeout(iut):
    """Low duty cycle directed advertising never times out on its own.

    Purpose: Verify that low duty cycle directed advertising keeps running with no
    automatic timeout, unlike the high duty cycle form (Vol 6, Part B,
    4.4.2.4.4).

    Steps:
        1. Upper Tester configures low duty cycle directed advertising.
        2. Upper Tester enables advertising.
        3. Run for twice the high duty cycle deadline.

    Expected:
        - Nothing reaches the Host.
        - Advertising is still enabled.
    """
    configure_legacy(iut, AdvType.ADV_DIRECT_IND_LOW_DUTY)
    enable_legacy(iut)
    iut.run(2 * LL_ADV_DIRECT_HIGH_DUTY_TIMEOUT_US)
    iut.ut_rx_q_is_empty()
    assert iut.ll.advertising.any_enabled()


# ---------------------------------------------------------------------------
# Host commands (Vol 4, Part E, 7.8.5-7.8.9)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'fields,status',
    [
        (
            dict(advertising_interval_min=0x1F),
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
        (
            dict(advertising_interval_min=0x40, advertising_interval_max=0x30),
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
        (dict(advertising_channel_map=0), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (dict(advertising_channel_map=8), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (dict(advertising_filter_policy=4), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (
            dict(advertising_type=5),
            ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
        ),
        (
            dict(own_address_type=4),
            ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
        ),
        (dict(peer_address_type=2), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
    ],
)
def test_parameter_validation(iut, fields, status):
    """Set Advertising Parameters rejects out-of-range and unsupported fields.

    Purpose: Verify that Set Advertising Parameters returns the right error for
    each invalid field: an interval below the minimum or an inverted range, an
    empty or reserved channel map, an out-of-range filter policy, and an
    unsupported advertising type, own address type or peer address type (Vol 4,
    Part E, 7.8.5).

    Steps:
        1. Upper Tester sends Set Advertising Parameters with one field overridden
           to an invalid value.

    Expected:
        - The command completes with the parametrized error status.

    Parameters range over interval too small, inverted interval range, empty and
    reserved channel maps, bad filter policy, unsupported advertising type,
    unsupported own address type, and bad peer address type.
    """
    values = dict(
        advertising_interval_min=LEGACY_INTERVAL,
        advertising_interval_max=LEGACY_INTERVAL,
        advertising_type=AdvType.ADV_IND,
        own_address_type=hci.OwnAddressType.PUBLIC,
        peer_address_type=PEER.address_type,
        peer_address=PEER,
        advertising_channel_map=7,
        advertising_filter_policy=0,
    )
    iut.ut(hci.HCI_LE_Set_Advertising_Parameters_Command(**(values | fields)), status)


def test_high_duty_directed_ignores_interval_range(iut):
    """High duty cycle directed advertising accepts a zero interval range.

    Purpose: Verify that Set Advertising Parameters accepts an interval min and
    max of zero for high duty cycle directed advertising, whose interval is fixed
    by the type rather than the command parameters (Vol 4, Part E, 7.8.5).

    Steps:
        1. Upper Tester sends Set Advertising Parameters for high duty cycle
           directed advertising with interval min and max of zero.

    Expected:
        - The command succeeds (the harness asserts SUCCESS by default).
    """
    iut.ut(
        hci.HCI_LE_Set_Advertising_Parameters_Command(
            0, 0, AdvType.ADV_DIRECT_IND, 0, PEER.address_type, PEER, 7, 0
        )
    )


def test_parameters_disallowed_while_enabled(iut):
    """Set Advertising Parameters is disallowed while advertising is enabled.

    Purpose: Verify that Set Advertising Parameters is rejected with Command
    Disallowed while advertising is active (Vol 4, Part E, 7.8.5).

    Steps:
        1. Upper Tester configures ADV_IND and enables advertising.
        2. Upper Tester sends Set Advertising Parameters again.

    Expected:
        - The command completes with COMMAND_DISALLOWED_ERROR.
    """
    configure_legacy(iut)
    enable_legacy(iut)
    iut.ut(
        hci.HCI_LE_Set_Advertising_Parameters_Command(
            LEGACY_INTERVAL, LEGACY_INTERVAL, AdvType.ADV_IND, 0, 0, PEER, 7, 0
        ),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )


@pytest.mark.parametrize('command', ['data', 'scan_response'])
def test_data_longer_than_31_octets_is_rejected(iut, command):
    """Legacy advertising and scan-response data over 31 octets is rejected.

    Purpose: Verify that Set Advertising Data and Set Scan Response Data reject
    payloads larger than the 31-octet legacy maximum (Vol 4, Part E, 7.8.7,
    7.8.8).

    Steps:
        1. Upper Tester sends the chosen data command with 32 octets.

    Expected:
        - The command completes with INVALID_COMMAND_PARAMETERS_ERROR.

    Parameters range over the advertising-data and scan-response-data commands.
    """
    oversized = bytes(PDU_ADV_LEGACY_DATA_SIZE_MAX + 1)
    packet = (
        hci.HCI_LE_Set_Advertising_Data_Command(oversized)
        if command == 'data'
        else hci.HCI_LE_Set_Scan_Response_Data_Command(oversized)
    )
    iut.ut(packet, ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR)


def test_data_update_while_enabled_applies_to_next_event(iut):
    """Updating AdvData while enabled takes effect on the next event.

    Purpose: Verify that Set Advertising Data accepted during active advertising
    replaces the payload transmitted in subsequent events (Vol 4, Part E,
    7.8.7).

    Steps:
        1. Upper Tester configures ADV_NONCONN_IND with AdvData "old" and enables
           advertising.
        2. Run one event and confirm the payload is "old".
        3. Upper Tester sends Set Advertising Data with "new".
        4. Run past one interval plus the maximum advDelay.

    Expected:
        - The first event carries "old".
        - The next event carries "new".
    """
    configure_legacy(iut, AdvType.ADV_NONCONN_IND, data=b'old')
    enable_legacy(iut)
    assert one_event(iut)[0].pdu[8:] == b'old'
    iut.ut(hci.HCI_LE_Set_Advertising_Data_Command(b'new'))
    iut.run(INTERVAL_US + 10_001)
    assert iut.lt_rx_all()[0].pdu[8:] == b'new'


def test_random_own_address_uses_controller_random_address(iut):
    """A random own address type advertises the controller random address.

    Purpose: Verify that with own address type RANDOM the advertiser uses the
    address set by Set Random Address, with the PDU's TxAdd bit marking it random
    (Vol 6, Part B, 1.3.2.1; Vol 4, Part E, 7.8.4).

    Steps:
        1. Upper Tester sets the controller random address.
        2. Upper Tester configures ADV_NONCONN_IND with own address type RANDOM.
        3. Upper Tester enables advertising.
        4. Run one event and read the first transmission.

    Expected:
        - The PDU's TxAdd bit is set.
        - AdvA is the configured random address.
    """
    random = hci.Address('C1:22:33:44:55:66', hci.AddressType.RANDOM_DEVICE)
    iut.ut(hci.HCI_LE_Set_Random_Address_Command(random))
    configure_legacy(iut, AdvType.ADV_NONCONN_IND, own=hci.OwnAddressType.RANDOM)
    enable_legacy(iut)
    packet = one_event(iut)[0]
    assert packet.pdu[0] & 0x40
    assert address_of(packet.pdu) == random


def test_read_advertising_tx_power(iut):
    """Read Advertising Physical Channel Tx Power returns the fixed level.

    Purpose: Verify that Read Advertising Physical Channel Tx Power reports the
    controller's advertising transmit power (Vol 4, Part E, 7.8.6).

    Steps:
        1. Upper Tester sends Read Advertising Physical Channel Tx Power.

    Expected:
        - The reported Tx power level is 0 dBm.
    """
    result = iut.ut(hci.HCI_LE_Read_Advertising_Physical_Channel_Tx_Power_Command())
    assert result.tx_power_level == 0
