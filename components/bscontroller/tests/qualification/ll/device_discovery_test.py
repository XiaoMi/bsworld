# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.2: Advertising and advertising sets."""

import random
from itertools import pairwise

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_ADV_DELAY_MAX_US,
    LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
    LL_ADV_PRIMARY_CHANNELS,
    LL_CONN_ESTABLISHMENT_EVENTS,
    LL_CONN_INTERVAL_UNIT_US,
    LL_DATA_CHANNEL_COUNT,
    LL_HOP_INCREMENT_MAX,
    LL_HOP_INCREMENT_MIN,
    LL_IFS_US,
    LL_MAFS_US,
    LL_SCA_500_PPM,
    LL_SCA_PPM,
    PDU_ADV_ADDRESS_OFFSET,
    PDU_ADV_ADDRESS_SIZE,
    PDU_ADV_ADI_DID_BITS,
    PDU_ADV_ADI_DID_MASK,
    PDU_ADV_ADI_SID_MAX,
    PDU_ADV_AUX_OFFSET_UNITS_MASK,
    PDU_ADV_CHSEL_MASK,
    PDU_ADV_CONNECT_HOP_MASK,
    PDU_ADV_EXT_HEADER_LENGTH_BITS,
    PDU_ADV_LEGACY_DATA_SIZE_MAX,
    PDU_ADV_OFFSET_BITS,
    PDU_ADV_OFFSET_UNIT_30_US,
    PDU_ADV_OFFSET_UNIT_300_US,
    PDU_ADV_RXADD_MASK,
    PDU_ADV_TXADD_MASK,
    PDU_ADV_TYPE_MASK,
    PDU_HEADER_SIZE,
    AdvertisingMode,
    AdvertisingPduType,
    LlId,
)
from bscontroller.radio.base import (
    PHY_CODED_S2_FACTOR,
    PHY_CODED_S8_FACTOR,
    PHY_OCTET_BITS,
    PHY_TIME_UNITS_PER_SECOND,
    Phy,
)
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.discovery import (
    RESPONSE_IFS_TOLERANCE_US,
    ErrorCode,
    Operation,
    _nonconnectable_extended_packet,
)
from tests.qualification.peer import PeerLink
from tests.qualification.tester import (
    DUT_ADDRESS,
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_DATA_ACCESS_ADDRESS,
    LinkParameters,
    chain,
    decode_aux,
    decode_extended,
    extended_enable,
    extended_parameters,
    follow_auxiliary,
    legacy_parameters,
    receive_extended,
    request_pdu,
    set_extended_data,
)
from tests.support import AD_COMPLETE_LOCAL_NAME

PROCEDURES = ProcedureMap()

AdvType = hci.HCI_LE_Set_Advertising_Parameters_Command.AdvertisingType
Properties = (
    hci.HCI_LE_Set_Extended_Advertising_Parameters_Command.AdvertisingProperties
)

# PHY_Options values for mandatory coding in Core Vol 4, Part E, 7.8.53.
REQUIRED_ADV_CODING = {0x03: PHY_CODED_S2_FACTOR, 0x04: PHY_CODED_S8_FACTOR}

# Endpoint jitter in the common advertising timing notes, 16 us at each endpoint.
ADVERTISING_INTERVAL_JITTER_US = 32

# Selected IXIT: minimum advertising interval, public AdvA, all three channels
# supported.
ADVERTISING_INTERVAL_US = hci.HCI_LE_ADV_INTERVAL_MIN * hci.HCI_LE_ADV_INTERVAL_UNIT_US

# Maximum-length payload prescribed by Tables 4.2-9 and 4.2-11.
LEGACY_MAXIMUM_DATA = b'\xf8' + bytes(PDU_ADV_LEGACY_DATA_SIZE_MAX - 1)

# Device name prescribed by the common discovery preconditions, Section 4.1.6.
IUT_NAME_DATA = bytes([4, AD_COMPLETE_LOCAL_NAME]) + b'IUT'


def advertising_interval_limits(interval_us):
    """Apply the source's 500-ppm drift and two-endpoint jitter bounds."""
    ppm = LL_SCA_PPM[LL_SCA_500_PPM]
    maximum = interval_us + LL_ADV_DELAY_MAX_US

    def drift(value):
        return (
            value * ppm + PHY_TIME_UNITS_PER_SECOND - 1
        ) // PHY_TIME_UNITS_PER_SECOND

    return (
        interval_us - drift(interval_us) - ADVERTISING_INTERVAL_JITTER_US,
        maximum + drift(maximum) + ADVERTISING_INTERVAL_JITTER_US,
    )


def public_peer_addresses():
    """Default public address and the three XOR vectors from Section 4.1.5.2."""
    iut = int.from_bytes(bytes(DUT_ADDRESS), 'little')
    return (LOWER_CENTRAL_PUBLIC_ADDRESS,) + tuple(
        hci.Address((iut ^ mask).to_bytes(6, 'little'), hci.AddressType.PUBLIC_DEVICE)
        for mask in (181419418583040, 90, 98956046500005)
    )


def _check_advertisement(packet, data):
    assert packet.phy == Phy.LE_1M
    assert not packet.pdu[0] & PDU_ADV_TXADD_MASK
    assert packet.pdu[PDU_ADV_ADDRESS_OFFSET:] == bytes(DUT_ADDRESS) + data


def _check_advertising_update(previous, observed, did, expected):
    """Apply BV-27's RF-to-RF DID rule before accepting a new payload."""
    if previous is not None and observed != previous[0]:
        assert did != previous[1], 'Advertising data changed without a new DID'
    if previous is None or did != previous[1]:
        assert observed == expected, 'New DID does not carry the submitted data'
    return observed, did


def _overlapping_advertisement(packet, mode):
    """Decode one primary packet's RF identity and quantized auxiliary window."""
    assert packet.valid_crc and packet.phy == Phy.LE_1M
    assert packet.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.ADV_EXT_IND
    assert packet.pdu[PDU_HEADER_SIZE] >> PDU_ADV_EXT_HEADER_LENGTH_BITS == mode
    fields = decode_extended(packet.pdu)
    assert 'aux' in fields and 'adi' in fields
    value = int.from_bytes(fields['aux'], 'little')
    assert value >> (PHY_OCTET_BITS + PDU_ADV_OFFSET_BITS) == Phy.LE_1M - 1
    channel, offset = decode_aux(fields['aux'])
    unit = (
        PDU_ADV_OFFSET_UNIT_300_US
        if value & PDU_ADV_AUX_OFFSET_UNITS_MASK
        else PDU_ADV_OFFSET_UNIT_30_US
    )
    target = packet.start_us + offset
    return fields, channel, target, target + unit


async def _observe_intervals(tester, channel, kind, data, count, *, chsel=None):
    minimum, maximum = advertising_interval_limits(ADVERTISING_INTERVAL_US)
    previous = None
    short_intervals = 0
    for _ in range(count + 1):
        packet = await tester.expect_pdu(channel, kind=kind)
        _check_advertisement(packet, data)
        if kind in (
            AdvertisingPduType.ADV_NONCONN_IND,
            AdvertisingPduType.ADV_SCAN_IND,
        ):
            assert not packet.pdu[0] & PDU_ADV_CHSEL_MASK
        if kind == AdvertisingPduType.ADV_DIRECT_IND:
            assert not packet.pdu[0] & PDU_ADV_RXADD_MASK
        if chsel is not None:
            assert bool(packet.pdu[0] & PDU_ADV_CHSEL_MASK) == chsel
        if previous is not None:
            interval = packet.start_us - previous
            assert minimum <= interval <= maximum, (interval, minimum, maximum)
            short_intervals += (
                interval <= ADVERTISING_INTERVAL_US + LL_ADV_DELAY_MAX_US // 2
            )
        previous = packet.start_us
    return short_intervals


async def _data_rounds(tester, kind):
    types = {
        AdvertisingPduType.ADV_IND: AdvType.ADV_IND,
        AdvertisingPduType.ADV_NONCONN_IND: AdvType.ADV_NONCONN_IND,
        AdvertisingPduType.ADV_SCAN_IND: AdvType.ADV_SCAN_IND,
    }
    await tester.command(legacy_parameters(advertising_type=types[kind]))
    for length in (1, 0, PDU_ADV_LEGACY_DATA_SIZE_MAX, 0):
        data = bytes([length]) + bytes(length - 1) if length else b''
        await tester.command(hci.HCI_LE_Set_Advertising_Data_Command(data))
        await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
        await _observe_intervals(tester, 37, kind, data, 50)
        await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(0))


async def _scan_response(tester, channel, kind, scanner, data):
    advertisement = await tester.expect_pdu(channel, kind=kind)
    _check_advertisement(advertisement, IUT_NAME_DATA)
    end = await tester.transmit(
        channel,
        request_pdu(AdvertisingPduType.SCAN_REQ, scanner=scanner),
        start_us=advertisement.end_us + LL_IFS_US,
    )
    response = await tester.receive(channel, duration_us=1000)
    if response is not None:
        assert response.valid_crc
        assert response.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.SCAN_RSP
        assert response.pdu[1] == len(response.pdu) - PDU_HEADER_SIZE
        assert abs(response.start_us - end - LL_IFS_US) <= RESPONSE_IFS_TOLERANCE_US
        _check_advertisement(response, data)
    return response


async def _scan_response_rounds(tester, *, scannable):
    kind = AdvertisingPduType.ADV_SCAN_IND if scannable else AdvertisingPduType.ADV_IND
    await tester.command(
        legacy_parameters(
            advertising_type=AdvType.ADV_SCAN_IND if scannable else AdvType.ADV_IND,
            advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
        )
    )
    await tester.command(hci.HCI_LE_Set_Advertising_Data_Command(IUT_NAME_DATA))
    await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
    peers = public_peer_addresses()
    for channel in LL_ADV_PRIMARY_CHANNELS:
        for scanner in (peers[0], peers[1], peers[3]):
            for data in (
                b'',
                bytes(
                    [
                        PDU_ADV_LEGACY_DATA_SIZE_MAX - 1,
                        AD_COMPLETE_LOCAL_NAME,
                    ]
                )
                + bytes(PDU_ADV_LEGACY_DATA_SIZE_MAX - 2),
            ):
                await tester.command(hci.HCI_LE_Set_Scan_Response_Data_Command(data))
                replies = 0
                for _ in range(30):
                    response = await _scan_response(
                        tester, channel, kind, scanner, data
                    )
                    replies += response is not None
                    if not scannable and replies:
                        break
                assert replies == (30 if scannable else 1)
                advertisement = await tester.expect_pdu(channel, kind=kind)
                _check_advertisement(advertisement, IUT_NAME_DATA)
    await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(0))


async def _accept_connection(tester, channel, peer, advertisement, silent_intervals):
    parameters = LinkParameters(access_address=LOWER_DATA_ACCESS_ADDRESS)
    end = await tester.transmit(
        channel,
        request_pdu(
            AdvertisingPduType.CONNECT_IND, scanner=peer, data=parameters.encode()
        ),
        start_us=advertisement.end_us + LL_IFS_US,
    )
    link = PeerLink(tester, parameters, end)
    await link.exchange()
    deadline = tester.radio.now_us + silent_intervals * ADVERTISING_INTERVAL_US
    while tester.radio.now_us < deadline:
        until = min(deadline, link.anchor - 1)
        assert until >= tester.radio.now_us
        assert (
            await tester.receive(channel, duration_us=until - tester.radio.now_us)
            is None
        )
        if link.anchor <= deadline:
            await link.exchange()
    event = await tester.event(hci.HCI_LE_Connection_Complete_Event)
    assert type(event) is hci.HCI_LE_Connection_Complete_Event
    assert event.status == ErrorCode.SUCCESS and event.role == hci.Role.PERIPHERAL
    assert event.peer_address == peer and event.peer_address_type == peer.address_type
    assert event.connection_interval == parameters.interval
    assert event.peripheral_latency == parameters.latency
    assert event.supervision_timeout == parameters.timeout
    link.handle = event.connection_handle
    await link.disconnect()


async def _invalid_crc_round(tester, channel, kind, request):
    for _ in range(30):
        advertisement = await tester.expect_pdu(channel, kind=kind)
        _check_advertisement(advertisement, IUT_NAME_DATA)
        await tester.transmit(
            channel, request, start_us=advertisement.end_us + LL_IFS_US, bad_crc=True
        )
        assert await tester.receive(channel, duration_us=1000) is None
    advertisement = await tester.expect_pdu(channel, kind=kind)
    _check_advertisement(advertisement, IUT_NAME_DATA)
    assert not any(
        isinstance(
            event,
            (
                hci.HCI_LE_Connection_Complete_Event,
                hci.HCI_LE_Enhanced_Connection_Complete_Event,
            ),
        )
        for event in tester.events
    )


async def advertising_events(tester, *, nonconnectable):
    """Non-Connectable or Undirected Advertising Events.

    Initial Condition:
        Table 4.2-1 selects non-connectable or undirected advertising. Use a
        public own address and the minimum advertising interval as the IXIT.

    Test Case Configuration:
        BV-01 selects ADV_NONCONN_IND and repeats the complete procedure on
        channels 37/38/39. BV-02 selects ADV_IND on the selected channel 37.
        Select public AdvA, the minimum IXIT interval and the common IUT name.
        Receive 101 packets to measure 100 complete intervals, not 100 packets.

    Test Steps:
        1. Configure the Lower Tester to monitor IUT advertising packets.
        2. Enable the Table 4.2-1 advertising type on a selected channel, using
           an advertising interval within the supported minimum/maximum.
        3. Receive the selected advertising PDU on the selected channel.
        4. Measure the next event from the first packet's start; count intervals
           no longer than advInterval + 5 ms.
        5. Repeat steps 3-4 until 100 advertising intervals have been detected.
        6. Send LE Set Advertising Enable to disable advertising and receive
           Command Complete.

    Expected Outcome:
        Pass verdict: all 100 intervals lie within the common 500-ppm drift and
        32-us endpoint-jitter bounds around advInterval through advInterval+10 ms.
        Between 35 and 65 intervals have delay no greater than 5 ms on every
        required channel, and all received packets contain the configured AdvA.

    Reference:
        LL.TS.p28.pdf, Section 4.2.3.2.1, LL/DDI/ADV/BV-01-C and LL/DDI/ADV/BV-02-C;
        PDF pages 97-99, printed pages 4.2-4 through 4.2-6, Table 4.2-1.
        Source reference [4], Sections 4.4.2.6, 4.4.2.2 and 4.4.2.3;
        Figure 4.2-1 and the timing Notes on PDF page 99.
    """
    kind = (
        AdvertisingPduType.ADV_NONCONN_IND
        if nonconnectable
        else AdvertisingPduType.ADV_IND
    )
    for channel in LL_ADV_PRIMARY_CHANNELS if nonconnectable else (37,):
        await tester.command(
            legacy_parameters(
                advertising_type=(
                    AdvType.ADV_NONCONN_IND if nonconnectable else AdvType.ADV_IND
                ),
                advertising_channel_map=1 << channel - LL_ADV_PRIMARY_CHANNELS[0],
            )
        )
        await tester.command(hci.HCI_LE_Set_Advertising_Data_Command(IUT_NAME_DATA))
        await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
        short = await _observe_intervals(tester, channel, kind, IUT_NAME_DATA, 100)
        assert 35 <= short <= 65, short
        await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(0))


# Verify the selected advertising PDU, contents, interval bounds and distribution of the
# random advertising delay.
PROCEDURES |= procedure_cases(
    advertising_events,
    {
        'LL/DDI/ADV/BV-01-C': (
            'Non-Connectable Advertising Events',
            {'nonconnectable': True},
        ),
        'LL/DDI/ADV/BV-02-C': (
            'Undirected Advertising Events',
            {'nonconnectable': False},
        ),
    },
)


async def advertising_data(tester, *, nonconnectable):
    """Advertising Data: Non-Connectable or Undirected.

    Initial Condition:
        Table 4.2-2 selects ADV_NONCONN_IND or ADV_IND. Configure public AdvA,
        channel 37 and the selected minimum advertising interval.

    Test Case Configuration:
        Table 4.2-2 selects ADV_NONCONN_IND for BV-03 and ADV_IND for BV-04.
        Use the selected channel 37 and minimum IXIT interval; the four data
        rounds are 1, 0, 31 and 0 octets, each measuring 50 complete intervals.

    Test Steps:
        1. Configure the Lower Tester to monitor IUT advertising packets.
        2. Configure the Table 4.2-2 type, selected channel and an interval within
           the supported minimum/maximum.
        3. Send LE Set Advertising Data with one octet containing its data length;
           receive Command Complete.
        4. Enable advertising with LE Set Advertising Enable; receive Command
           Complete.
        5. Receive the selected PDU containing the step-3 data, beginning an event
           on the selected channel.
        6. Check the next event's start relative to the first packet's start.
        7. Repeat steps 5-6 until 50 advertising intervals have been detected.
        8. Disable advertising; receive Command Complete.
        9. Set empty advertising data; receive Command Complete.
        10. Enable advertising; receive Command Complete.
        11. Receive the selected PDU without advertising data, beginning an event
            on the selected channel.
        12. Check the next event's start relative to the first packet's start.
        13. Repeat steps 11-12 until 50 advertising intervals have been detected.
        14. Disable advertising; receive Command Complete.
        15. Set maximum-length data: 31 octets by default, or the smaller IXIT
            maximum if declared. The first octet encodes the length unsigned,
            least-significant bit first; all remaining octets are zero. Receive
            Command Complete. This DUT selects the default 31-octet maximum.
        16. Repeat steps 4-14 with the maximum-length data followed by empty data.

    Expected Outcome:
        Pass verdict: all four rounds transmit the selected PDU with the exact
        data, AdvA and address type; all 200 intervals satisfy the common timing
        bounds. Every setup, data, enable and disable command succeeds.

    Reference:
        LL.TS.p28.pdf, Section 4.2.3.2.2, LL/DDI/ADV/BV-03-C and LL/DDI/ADV/BV-04-C;
        PDF pages 99-102, printed pages 4.2-6 through 4.2-9, Table 4.2-2.
        Source reference [3], Section 4.4.2.6; Figure 4.2-2, steps 1-16.
    """
    await _data_rounds(
        tester,
        (
            AdvertisingPduType.ADV_NONCONN_IND
            if nonconnectable
            else AdvertisingPduType.ADV_IND
        ),
    )


# Verify that advertising carries each Host-provided data round exactly.
PROCEDURES |= procedure_cases(
    advertising_data,
    {
        'LL/DDI/ADV/BV-03-C': (
            'Advertising Data: Non-Connectable',
            {'nonconnectable': True},
        ),
        'LL/DDI/ADV/BV-04-C': (
            'Advertising Data: Undirected',
            {'nonconnectable': False},
        ),
    },
)


async def scan_request_connectable(tester):
    """Scan Request: Undirected Connectable.

    Initial Condition:
        ADV_IND uses public AdvA, all supported channels and unrestricted filtering.
        The Lower Tester uses the three public scanner identities of Section 4.1.5.2.

    Test Steps:
        1. Configure undirected advertising on all supported primary channels
           using an interval in the supported minimum/maximum range.
        2. Configure the Lower Tester to monitor advertising/scan responses and
           send SCAN_REQ on an IXIT channel using its common public address.
        3. Configure empty scan-response data (source device-name length zero).
        4. Receive ADV_IND and send SCAN_REQ on that channel T_IFS after its end.
        5. Receive SCAN_RSP from the IUT T_IFS after the request's end.
        6. Repeat steps 4-5 up to 30 times or until SCAN_RSP is received.
        7. Configure maximum-length scan-response data (source length 31),
           applying the total-data-length correction below.
        8. Repeat steps 4-6.
        9. Select the public scanner address differing from the IUT in its most
           significant octet.
        10. Configure empty scan-response data.
        11. Repeat steps 4-6.
        12. Configure maximum-length scan-response data.
        13. Repeat steps 4-6.
        14. Select the public scanner address differing in both most/least
            significant octets.
        15. Repeat steps 4-6, first restoring empty response data as specified
            by the local source correction below.
        16. Configure maximum-length scan-response data.
        17. Repeat steps 4-6.

    Observation Setup:
        Run the procedure on each supported primary channel. Keep advertising
        enabled across response-data and scanner changes; check that unchanged
        ADV_IND advertising resumes after every response round. Disable only in
        a separate postamble after all rounds.

    Expected Outcome:
        Pass verdict: every required identity, data length and channel receives
        a valid response within 30 attempts. Response timing is T_IFS +/- 2 us;
        advertising continues after the scan response.

    Source Correction:
        The source's 31-byte device-name wording exceeds legacy AD capacity.
        Use 31 total ScanRspData octets: Length = 30, Complete Local Name type
        and 29 name octets, consistent with Core Vol 6, Part B, Section 2.3.2.2
        and Vol 3, Part C, Section 11. Before step 15, restore empty response
        data: the source omits that configuration after changing to the third
        scanner, unlike the symmetric BV-17 procedure. Amber supports both local
        interpretations; neither is claimed as adopted SIG errata.

    Reference:
        LL.TS.p28.pdf, Section 4.2, LL/DDI/ADV/BV-05-C;
        PDF pages 102-104, printed pages 4.2-9 through 4.2-11, Figure 4.2-3.
        Source reference [3], Sections 4.4.2.3 and 4.3.2, steps 1-17;
        the common timing Notes in Section 4.2.3.2.1 apply.
    """
    await _scan_response_rounds(tester, scannable=False)


# Verify scan response timing, data and continued connectable advertising.
PROCEDURES |= procedure_cases(
    scan_request_connectable,
    {
        'LL/DDI/ADV/BV-05-C': (
            'Scan Request: Undirected Connectable',
            {},
        ),
    },
)


async def connection_request(tester):
    """Connection Request.

    Initial Condition:
        Public undirected advertising uses all primary channels and accepts any
        initiator. The Lower Tester uses the four public addresses of Section 4.1.5.2.

    Test Steps:
        1. Enable undirected advertising on all supported primary channels with
           an interval in the supported minimum/maximum range.
        2. Configure the Lower Tester to monitor advertising/connection and send
           CONNECT_IND on the first supported advertising channel.
        3. Select the common public Lower Tester address for CONNECT_IND.
        4. Receive ADV_IND and send CONNECT_IND T_IFS after its end.
        5. Observe no more ADV_IND after the advertising interval; wait four
           advertising intervals and check that no ADV_IND is received.
        6. Receive LE Connection Complete containing the parameters from step 4.
        7. Send HCI Disconnect with that handle; require successful Command Status.
        8. Receive Disconnection Complete.
        9. Select the public address differing from the IUT in its most significant
           octet.
        10. Repeat steps 4-8.
        11. Select the public address differing in its least significant octet.
        12. Repeat steps 4-8.
        13. Select the public address differing in both most/least significant octets.
        14. Repeat steps 4-8.
        15. Select the second supported advertising channel.
        16. Repeat steps 3-14.
        17. Select the third supported advertising channel.
        18. Repeat steps 3-14.

    Observation Setup:
        Enable the legacy connection-complete event required by step 6 before
        the procedure. Re-enable advertising for every repeated request. During
        the four-interval silence observation, maintain the established ACL
        without skipping data anchors, then verify the queued HCI notification.

    Expected Outcome:
        Pass verdict: all twelve rounds establish the requested connection,
        stop advertising for the required observation period and disconnect.

    Reference:
        LL.TS.p28.pdf, Section 4.2, LL/DDI/ADV/BV-06-C;
        PDF pages 104-106, printed pages 4.2-11 through 4.2-13, Figure 4.2-4.
        Source reference [3], Sections 4.4.2.3 and 4.3.2, steps 1-18.
    """
    await tester.masks(le=1 << (hci.HCI_LE_CONNECTION_COMPLETE_EVENT - 1))
    await tester.command(
        legacy_parameters(advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL)
    )
    await tester.command(hci.HCI_LE_Set_Advertising_Data_Command(IUT_NAME_DATA))
    for channel in LL_ADV_PRIMARY_CHANNELS:
        for peer in public_peer_addresses():
            await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
            advertisement = await tester.expect_pdu(
                channel, kind=AdvertisingPduType.ADV_IND
            )
            _check_advertisement(advertisement, IUT_NAME_DATA)
            await _accept_connection(tester, channel, peer, advertisement, 4)


# Verify connection acceptance, complete Host notification and stopped advertising.
PROCEDURES |= procedure_cases(
    connection_request,
    {
        'LL/DDI/ADV/BV-06-C': (
            'Connection Request',
            {},
        ),
    },
)


async def scan_then_connection_request(tester):
    """Scan Request Connection Request.

    Initial Condition:
        Public undirected advertising uses all primary channels and unrestricted
        filtering; select channel 37 and the common public Lower Tester address.

    Test Steps:
        1. Enable undirected advertising on all supported channels, using a
           supported interval and accept-all scan/connection policy (0x00).
        2. Set Scan Response Data to the literal "IUT"; receive Command Complete.
        3. Configure the Lower Tester to monitor advertising, scan response and
           connection procedures, sending SCAN_REQ/CONNECT_IND on an IXIT channel.
        4. Receive ADV_IND and send SCAN_REQ on that channel T_IFS after its end.
        5. Receive SCAN_RSP from the IUT T_IFS after the request's end.
        6. Repeat steps 4-5 up to 30 times or until SCAN_RSP is received.
        7. Receive the next ADV_IND and send CONNECT_IND T_IFS after its end.
        8. Receive no further ADV_IND after the advertising interval, confirming
           that the IUT stopped advertising following the connection request.
        9. Receive LE Connection Complete with the requested connection parameters.
        10. Perform Peripheral Connection Terminated with the selected interval,
            latency, timeout, channel map, unencrypted state and step-9 handle.

    Observation Setup:
        Select channel 37 and the common public Lower Tester address. Enable
        legacy connection completion before step 1, check exact response data,
        identity, CRC and T_IFS, and maintain the ACL during silence observation.

    Expected Outcome:
        Pass verdict: the scan response is valid and the subsequent connection
        succeeds with the requested parameters; advertising stops and termination
        produces a successful HCI Disconnection Complete event.

    Reference:
        LL.TS.p28.pdf, Section 4.2, LL/DDI/ADV/BV-07-C;
        PDF pages 106-108, printed pages 4.2-13 through 4.2-15, Figure 4.2-5.
        Source reference [3], Section 4.4.2.3, steps 1-10.
    """
    await tester.masks(le=1 << (hci.HCI_LE_CONNECTION_COMPLETE_EVENT - 1))
    await tester.command(
        legacy_parameters(advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL)
    )
    await tester.command(hci.HCI_LE_Set_Advertising_Data_Command(IUT_NAME_DATA))
    await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
    await tester.command(hci.HCI_LE_Set_Scan_Response_Data_Command(b'IUT'))
    for _ in range(30):
        response = await _scan_response(
            tester, 37, AdvertisingPduType.ADV_IND, LOWER_CENTRAL_PUBLIC_ADDRESS, b'IUT'
        )
        if response is not None:
            break
    else:
        raise AssertionError('no SCAN_RSP in 30 permitted attempts')
    advertisement = await tester.expect_pdu(37, kind=AdvertisingPduType.ADV_IND)
    _check_advertisement(advertisement, IUT_NAME_DATA)
    await _accept_connection(tester, 37, LOWER_CENTRAL_PUBLIC_ADDRESS, advertisement, 1)


# Verify that scan-response handling does not prevent a subsequent connection.
PROCEDURES |= procedure_cases(
    scan_then_connection_request,
    {
        'LL/DDI/ADV/BV-07-C': (
            'Scan Request Connection Request',
            {},
        ),
    },
)


async def discoverable_advertising_data(tester):
    """Advertising Data: Discoverable.

    Initial Condition:
        Configure discoverable advertising on channel 37 with public AdvA and
        the minimum selected interval, with advertising initially disabled.

    Test Steps:
        Configure the Lower Tester to monitor advertising (unnumbered preamble).
        1. Configure discoverable undirected advertising on the selected channel
           with a supported interval; apply the step-1 correction below.
        2. Set one advertising data octet indicating its length; receive Command
           Complete.
        3. Enable advertising; receive Command Complete.
        4. Receive ADV_SCAN_IND containing the step-2 data, beginning an event
           on the selected channel.
        5. Measure the next event relative to the first packet's start.
        6. Repeat steps 4-5 until 50 intervals are detected (corrected reference).
        7. Disable advertising; receive Command Complete.
        8. Set empty advertising data; receive Command Complete.
        9. Enable advertising; receive Command Complete.
        10. Receive ADV_SCAN_IND without advertising data on the selected channel.
        11. Measure the next event relative to the first packet's start.
        12. Repeat steps 10-11 until 50 intervals are detected (corrected reference).
        13. Disable advertising; receive Command Complete.
        14. Set 31 octets, the first encoding the data length unsigned,
            least-significant bit first, with the rest zero; receive Command
            Complete.
        15. Repeat steps 3-13 once with maximum-length data followed by empty
            data, applying the source's corrected repeat bounds below.

    Expected Outcome:
        Pass verdict: the four data rounds transmit ADV_SCAN_IND containing
        exactly the submitted bytes and public AdvA on LE 1M, with valid intervals.

    Source Correction:
        Step 1 says "enables", but Figure 4.2-10 sends Set Advertising Parameters
        there and only enables through HCI in step 3. Step 4's data reference
        means step 2, not step 3. Steps 6 and 12 self-reference; their intended
        packet/interval loops are 4-5 and 10-11. Step 15's 4-14 skips re-enable
        and includes another data setup; use 3-13, matching the MSC's two outer
        rounds and the intact BV-03/BV-04 procedure. These are local annotations,
        not adopted SIG errata. Amber supports this reconstruction after checking
        the MSC's complete four-phase repetition rather than only the prose.

    Reference:
        LL.TS.p28.pdf, Section 4.2, LL/DDI/ADV/BV-16-C;
        PDF pages 119-121, printed pages 4.2-26 through 4.2-28, Figure 4.2-10.
        Source reference [3], Section 4.4.2.5; all steps 1-15.
    """
    await _data_rounds(tester, AdvertisingPduType.ADV_SCAN_IND)


# Verify exact Host-provided data in discoverable ADV_SCAN_IND events.
PROCEDURES |= procedure_cases(
    discoverable_advertising_data,
    {
        'LL/DDI/ADV/BV-16-C': (
            'Advertising Data: Discoverable',
            {},
        ),
    },
)


async def scan_request_discoverable(tester):
    """Scan Request: Discoverable.

    Initial Condition:
        Public ADV_SCAN_IND uses all primary channels and unrestricted filtering.
        Use the common public scanner, MSB-XOR and both-XOR addresses.

    Test Steps:
        1. Enable discoverable undirected advertising on all supported primary
           channels using a selected supported interval.
        2. Configure the Lower Tester to monitor advertising/scan responses and
           send SCAN_REQ on an IXIT channel using its common public address.
        3. Configure empty scan-response data (source device-name length zero).
        4. Receive ADV_SCAN_IND and send SCAN_REQ on that channel T_IFS after
           the advertisement's end.
        5. Receive SCAN_RSP from the IUT T_IFS after the request's end.
        6. Repeat steps 4-5 thirty times.
        7. Configure maximum-length scan-response data (source length 31).
        8. Repeat steps 4-6.
        9. Select the public scanner address differing from the IUT in its most
           significant octet.
        10. Configure empty scan-response data.
        11. Repeat steps 4-6.
        12. Configure maximum-length scan-response data.
        13. Repeat steps 4-6.
        14. Select the public scanner address differing in both most/least
            significant octets.
        15. Configure empty scan-response data.
        16. Repeat steps 4-6.
        17. Configure maximum-length scan-response data.
        18. Repeat steps 4-6.

    Observation Setup:
        Run all identities/data lengths on each supported primary channel;
        keep advertising enabled across those changes. Verify CRC, exact
        AdvA/data, LE 1M, T_IFS and continued advertising after each round.
        Disable advertising only after all rounds, as a separate postamble.

    Expected Outcome:
        Pass verdict: thirty valid replies occur in every identity/data/channel
        round with T_IFS +/- 2 us and no loss of discoverable advertising.

    Source Correction:
        Interpret device-name length 31 as 31 significant ScanRspData octets,
        including the AD Length/type octets, leaving 29 name octets. A literal
        31-octet name would exceed the legacy payload limit. Amber supports
        this Core-consistent interpretation; no adopted SIG erratum is claimed.

    Reference:
        LL.TS.p28.pdf, Section 4.2, LL/DDI/ADV/BV-17-C;
        PDF pages 121-123, printed pages 4.2-28 through 4.2-30, Figure 4.2-11.
        Source reference [3], Sections 4.4.2.5 and 4.4.3.2, steps 1-18;
        the common timing Notes in Section 4.2.3.2.1 apply.
    """
    await _scan_response_rounds(tester, scannable=True)


# Verify scan responses and continued discoverable advertising.
PROCEDURES |= procedure_cases(
    scan_request_discoverable,
    {
        'LL/DDI/ADV/BV-17-C': (
            'Scan Request: Discoverable',
            {},
        ),
    },
)


async def scan_request_invalid_crc(tester, *, scannable):
    """Scan Request Invalid CRC.

    Initial Condition:
        Table 4.2-7 selects ADV_IND or ADV_SCAN_IND, using public AdvA, the
        minimum interval, all primary channels and unrestricted filtering.

    Test Case Configuration:
        LL/DDI/ADV/BI-01-C: ADV_IND.
        LL/DDI/ADV/BI-10-C: ADV_SCAN_IND.

    Test Steps:
        1. Configure the Lower Tester for active scanning with invalid-CRC
           SCAN_REQ packets.
        2. Enable the Table 4.2-7 advertising type on all supported channels.
        3. Receive the selected advertising PDU and send an invalid-CRC SCAN_REQ
           on a randomly selected channel T_IFS after the advertisement's end.
        4. Verify continued advertising without a scan response.
        5. Repeat steps 3-4 thirty times.
        6. Disable advertising with LE Set Advertising Enable; receive Command
           Complete.

    Test Condition:
        The Lower Tester listens only on the selected advertising channel.
        Use the supported minimum interval and accept-all filtering policy.
        A seeded random choice selects the channel; invert all CRC bits as
        required by Section 4.1.5.1. Configure a nonempty scan response first
        so the absence of data cannot mask incorrect request acceptance.

    Expected Outcome:
        Pass verdict: all thirty corrupted requests are ignored; advertising
        continues on the selected channel and no connection is reported.

    Reference:
        LL.TS.p28.pdf, Section 4.2.3.2.3, LL/DDI/ADV/BI-01-C and LL/DDI/ADV/BI-10-C;
        PDF pages 127-129, printed pages 4.2-34 through 4.2-36, Table 4.2-7.
        CRC corruption follows the all-bit inversion in Section 4.1.5.1.
        Source reference [3], Sections 3.1 and 4.4.2.3; Figure 4.2-13,
        all steps 1-6 and the common timing Notes in Section 4.2.3.2.1.
    """
    await tester.command(
        legacy_parameters(
            advertising_type=AdvType.ADV_SCAN_IND if scannable else AdvType.ADV_IND,
            advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
        )
    )
    await tester.command(hci.HCI_LE_Set_Scan_Response_Data_Command(b'IUT'))
    await tester.command(hci.HCI_LE_Set_Advertising_Data_Command(IUT_NAME_DATA))
    await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
    channel = random.Random(0).choice(LL_ADV_PRIMARY_CHANNELS)
    await _invalid_crc_round(
        tester,
        channel,
        AdvertisingPduType.ADV_SCAN_IND if scannable else AdvertisingPduType.ADV_IND,
        request_pdu(AdvertisingPduType.SCAN_REQ, scanner=LOWER_CENTRAL_PUBLIC_ADDRESS),
    )
    await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(0))


# Verify that invalid-CRC scan requests are ignored without stopping advertising.
PROCEDURES |= procedure_cases(
    scan_request_invalid_crc,
    {
        'LL/DDI/ADV/BI-01-C': (
            'Scan Request Invalid CRC, Connectable and Scannable Undirected',
            {'scannable': False},
        ),
        'LL/DDI/ADV/BI-10-C': (
            'Scan Request Invalid CRC, Scannable Undirected',
            {'scannable': True},
        ),
    },
)


async def connection_request_invalid_crc(tester):
    """Connection Request Invalid CRC.

    Initial Condition:
        Public ADV_IND uses all primary channels, the minimum interval and no
        filtering. The connection request contains otherwise valid parameters.

    Test Steps:
        1. Configure the Lower Tester to initiate using invalid-CRC CONNECT_IND.
        2. Enable undirected advertising on all supported channels.
        3. Receive ADV_IND and send invalid-CRC CONNECT_IND on a randomly
           selected channel T_IFS after the advertisement's end.
        4. Verify that the IUT continues advertising.
        5. Repeat steps 3-4 thirty times.
        6. Disable advertising with LE Set Advertising Enable; receive Command
           Complete.

    Test Condition:
        Listen only on the selected channel in each repetition of the procedure.
        Randomize channel order with a fixed seed and cover all three supported
        primary channels to satisfy the all-channel pass verdict. Use accept-all
        filtering, otherwise-valid common connection parameters, and invert all
        CRC bits. Also reject any HCI connection event or RF response.

    Expected Outcome:
        Pass verdict: the corrupted request is ignored in all ninety rounds;
        advertising continues on every supported channel and no ACL is reported.

    Reference:
        LL.TS.p28.pdf, Section 4.2, LL/DDI/ADV/BI-02-C;
        PDF pages 129-130, printed pages 4.2-36 through 4.2-37, Figure 4.2-14.
        Repeat across channels to satisfy the all-supported-channel Pass verdict.
        Source reference [3], Sections 3.1 and 4.4.2.3; steps 1-6 and the
        common timing Notes in Section 4.2.3.2.1.
    """
    await tester.command(
        legacy_parameters(advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL)
    )
    await tester.command(hci.HCI_LE_Set_Advertising_Data_Command(IUT_NAME_DATA))
    await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
    request = request_pdu(
        AdvertisingPduType.CONNECT_IND,
        scanner=LOWER_CENTRAL_PUBLIC_ADDRESS,
        data=LinkParameters(access_address=LOWER_DATA_ACCESS_ADDRESS).encode(),
    )
    for channel in random.Random(0).sample(
        LL_ADV_PRIMARY_CHANNELS, len(LL_ADV_PRIMARY_CHANNELS)
    ):
        await _invalid_crc_round(tester, channel, AdvertisingPduType.ADV_IND, request)
    await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(0))


# Verify that corrupted connection requests cannot establish an ACL or stop advertising.
PROCEDURES |= procedure_cases(
    connection_request_invalid_crc,
    {
        'LL/DDI/ADV/BI-02-C': (
            'Connection Request Invalid CRC',
            {},
        ),
    },
)


async def low_duty_directed_advertising(tester):
    """Low Duty Cycle Directed Advertising Events.

    Initial Condition:
        Configure public AdvA and the common public initiator as TargetA, using
        low-duty directed advertising on channel 37 at the minimum interval.

    Test Steps:
        1. Configure the Lower Tester to scan and monitor IUT advertising.
        2. Enable low-duty directed advertising on the selected channel with
           a supported advertising interval.
        3. Receive ADV_DIRECT_IND starting an event on the selected channel.
        4. Measure the next event relative to the first packet's start.
        5. Repeat steps 3-4 until 100 complete advertising intervals are detected.
        6. Configure the Lower Tester to initiate a connection.
        7. Receive ADV_DIRECT_IND and send CONNECT_IND T_IFS after its end;
           do not send any LL data packets to the IUT.
        8. Receive no further ADV_DIRECT_IND after the advertising interval.
        9. Repeat steps 7-8 until advertising stops. In the ideal RF path,
           the first valid request is accepted and this loop exits immediately.
        10. Receive LE Connection Complete with the parameters sent in step 7.
        11. Receive Disconnection Complete after the Establishment Timeout.

    Test Condition:
        The Lower Tester listens only on the selected channel 37. Select the
        minimum supported interval and public AdvA/TargetA. Enable legacy
        connection completion, verify all PDU address bits/contents and interval
        bounds, and observe silence beyond the six-event establishment window.

    Expected Outcome:
        Pass verdict: all directed intervals satisfy the common timing bounds;
        advertising stops after connection acceptance. Success is reported for
        creation, followed by establishment-failure disconnection of that handle.
        A failed creation event cannot substitute for the two required events.

    Reference:
        LL.TS.p28.pdf, Section 4.2, LL/DDI/ADV/BV-19-C;
        PDF pages 131-132, printed pages 4.2-38 through 4.2-39, Figure 4.2-15.
        Source reference [3], Section 4.4.2.4, all steps 1-11 and the common
        timing Notes in Section 4.2.3.2.1.
    """
    peer = LOWER_CENTRAL_PUBLIC_ADDRESS
    await tester.masks(le=1 << (hci.HCI_LE_CONNECTION_COMPLETE_EVENT - 1))
    await tester.command(
        legacy_parameters(
            advertising_type=AdvType.ADV_DIRECT_IND_LOW_DUTY,
            peer_address_type=peer.address_type,
            peer_address=peer,
        )
    )
    await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
    await _observe_intervals(
        tester, 37, AdvertisingPduType.ADV_DIRECT_IND, bytes(peer), 100
    )
    advertisement = await tester.expect_pdu(37, kind=AdvertisingPduType.ADV_DIRECT_IND)
    _check_advertisement(advertisement, bytes(peer))
    parameters = LinkParameters(access_address=LOWER_DATA_ACCESS_ADDRESS)
    await tester.transmit(
        37,
        request_pdu(
            AdvertisingPduType.CONNECT_IND, scanner=peer, data=parameters.encode()
        ),
        start_us=advertisement.end_us + LL_IFS_US,
    )
    assert await tester.receive(37, duration_us=ADVERTISING_INTERVAL_US) is None
    complete = await tester.event(hci.HCI_LE_Connection_Complete_Event)
    assert type(complete) is hci.HCI_LE_Connection_Complete_Event
    assert (
        complete.status,
        complete.role,
        complete.peer_address,
        complete.peer_address_type,
        complete.connection_interval,
        complete.peripheral_latency,
        complete.supervision_timeout,
    ) == (
        ErrorCode.SUCCESS,
        hci.Role.PERIPHERAL,
        peer,
        peer.address_type,
        parameters.interval,
        parameters.latency,
        parameters.timeout,
    )
    assert (
        await tester.receive(
            37,
            duration_us=(LL_CONN_ESTABLISHMENT_EVENTS + 1)
            * parameters.interval
            * LL_CONN_INTERVAL_UNIT_US,
        )
        is None
    )
    disconnected = await tester.event(hci.HCI_Disconnection_Complete_Event)
    assert (
        disconnected.status,
        disconnected.connection_handle,
        disconnected.reason,
    ) == (
        ErrorCode.SUCCESS,
        complete.connection_handle,
        ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR,
    )


# Verify low-duty directed PDU contents/timing, connection acceptance and establishment
# timeout when the Lower Tester sends no connection data.
PROCEDURES |= procedure_cases(
    low_duty_directed_advertising,
    {
        'LL/DDI/ADV/BV-19-C': (
            'Low Duty Cycle Directed Advertising Events',
            {},
        ),
    },
)


async def advertising_always_le_1m(tester, *, nonconnectable):
    """Advertising Always Using the LE 1M PHY.

    Initial Condition:
        Table 4.2-8 selects ADV_IND or ADV_NONCONN_IND with public AdvA, all
        supported channels and the minimum advertising interval.

    Test Case Configuration:
        LL/DDI/ADV/BV-111-C: ADV_NONCONN_IND.
        LL/DDI/ADV/BV-20-C: ADV_IND.

    Test Steps:
        1. Configure the Lower Tester to accept only LE 1M advertising packets;
           scan each supported primary channel for at least thirty intervals.
        2. Send LE Set Default PHY with ALL_PHYS = 0 and TX_PHYS / RX_PHYS
           both preferring LE 2M.
        3. Enable the Table 4.2-8 advertising type on all supported channels
           using the minimum advertising interval.
        4. Receive the selected advertising PDU starting an event on an
           applicable channel using LE 1M. Repeat until at least ninety packets
           are detected, with at least thirty per channel.
        5. Disable advertising with LE Set Advertising Enable; receive Command
           Complete.

    Observation Setup:
        Observe thirty complete intervals (31 packets) per channel, satisfying
        both step 1's interval count and step 4's packet-count minimum. Check
        PDU type, public AdvA, common name data, PHY and timing for every packet.

    Expected Outcome:
        Pass verdict: every advertising packet is received correctly on LE 1M,
        regardless of the LE 2M default preference; all channels are observed.

    Reference:
        LL.TS.p28.pdf, Section 4.2.3.2.4, LL/DDI/ADV/BV-20-C and LL/DDI/ADV/BV-111-C;
        PDF pages 132-134, printed pages 4.2-39 through 4.2-41, Table 4.2-8.
        Source reference [10], Section 2.3; Figure 4.2-16, steps 1-5.
    """
    await tester.command(
        hci.HCI_LE_Set_Default_PHY_Command(0, hci.PhyBit.LE_2M, hci.PhyBit.LE_2M)
    )
    await tester.command(
        legacy_parameters(
            advertising_type=(
                AdvType.ADV_NONCONN_IND if nonconnectable else AdvType.ADV_IND
            ),
            advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
        )
    )
    await tester.command(hci.HCI_LE_Set_Advertising_Data_Command(IUT_NAME_DATA))
    await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
    kind = (
        AdvertisingPduType.ADV_NONCONN_IND
        if nonconnectable
        else AdvertisingPduType.ADV_IND
    )
    for channel in LL_ADV_PRIMARY_CHANNELS:
        await _observe_intervals(tester, channel, kind, IUT_NAME_DATA, 30)
    await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(0))


# Verify legacy advertising remains on LE 1M despite Host preference for LE 2M.
PROCEDURES |= procedure_cases(
    advertising_always_le_1m,
    {
        'LL/DDI/ADV/BV-111-C': (
            'Advertising Always Using the LE 1M PHY, Non-connectable',
            {'nonconnectable': True},
        ),
        'LL/DDI/ADV/BV-20-C': (
            (
                'Advertising Always Using the LE 1M PHY, Connectable and '
                'Scannable Undirected'
            ),
            {'nonconnectable': False},
        ),
    },
)


async def extended_legacy_nonconnectable(tester):
    """Extended Advertising, Legacy PDUs, Non-Connectable.

    Initial Condition:
        Configure a public extended advertising set using legacy non-connectable
        properties, channel 37 and the minimum interval.

    Test Case Configuration:
        Table 4.2-9 selects one octet 0x01, no data, then 31 octets consisting of
        0xF8 followed by thirty zeroes. Use public AdvA and selected channel 37;
        verify PDU type, PHY, address and exact data in every packet.

    Test Steps:
        1. Configure the Lower Tester to monitor IUT advertising packets.
        2. Send LE Set Extended Advertising Parameters on the selected primary
           channel with the minimum interval and Properties = 0b00010000
           (legacy ADV_NONCONN_IND).
        Repeat steps 3-8 for every Table 4.2-9 round:
        3. Send LE Set Extended Advertising Data with the round's data; receive
           Command Complete.
        4. Enable with LE Set Extended Advertising Enable; receive Command Complete.
        5. Receive ADV_NONCONN_IND carrying the step-3 data, starting an event
           on the selected primary channel.
        6. Measure the next event relative to the first packet's start.
        7. Repeat steps 5-6 until fifty complete intervals have been detected.
        8. Disable with LE Set Extended Advertising Enable; receive Command Complete.

    Expected Outcome:
        Pass verdict: all three rounds use ADV_NONCONN_IND on LE 1M, carry the
        exact submitted data and satisfy the advertising interval bounds.

    Reference:
        LL.TS.p28.pdf, Section 4.2, LL/DDI/ADV/BV-21-C;
        PDF pages 134-136, printed pages 4.2-41 through 4.2-43, Table 4.2-9.
        Source reference [10], Section 4.4.2.6; Figure 4.2-17, steps 1-8.
    """
    await tester.command(
        extended_parameters(
            advertising_event_properties=Properties.USE_LEGACY_ADVERTISING_PDUS
        )
    )
    for data in (b'\x01', b'', LEGACY_MAXIMUM_DATA):
        await set_extended_data(tester, data)
        await tester.command(extended_enable())
        await _observe_intervals(
            tester, 37, AdvertisingPduType.ADV_NONCONN_IND, data, 50
        )
        await tester.command(extended_enable(0))


# Verify extended HCI commands configure legacy non-connectable PDUs and data.
PROCEDURES |= procedure_cases(
    extended_legacy_nonconnectable,
    {
        'LL/DDI/ADV/BV-21-C': (
            'Extended Advertising, Legacy PDUs, Non-Connectable',
            {},
        ),
    },
)


async def extended_legacy_undirected(tester, *, extended=False, connectable=False):
    """Extended Advertising, Legacy PDUs, Undirected.

    Initial Condition:
        Table 4.2-10 selects legacy-HCI ADV_IND with CSA #2, legacy-HCI
        ADV_NONCONN_IND, or extended-HCI legacy ADV_NONCONN_IND. All channels
        are enabled with public AdvA and the minimum advertising interval.

    Test Case Configuration:
        BV-22 uses legacy HCI, ADV_IND and ChSel = 1; BV-112 uses legacy HCI,
        ADV_NONCONN_IND and ChSel = 0; BV-113 uses extended HCI, legacy
        ADV_NONCONN_IND properties 0x10 and ChSel = 0. The three payload/channel
        rounds are 0x01 on 37, empty on 38, then 0xF8 plus thirty zeroes on 39.
        Enable all supported channels throughout; only the observation channel
        changes per round. Check LE 1M, public AdvA, exact data and timing.

    Test Steps:
        1. Configure the Lower Tester to monitor IUT advertising packets.
        2. Send the Table 4.2-10 Set Parameters command with its type/properties,
           all supported primary channels and the minimum advertising interval.
        Repeat steps 3-7 for every Table 4.2-11 round:
        3. Send the Table 4.2-10 Set Data command with that round's payload;
           receive Command Complete.
        4. Send the Table 4.2-10 Enable command; receive Command Complete.
           For Extended Advertising Enable, set Duration[0] = 0x0000.
        5. Listen only on the round's Table 4.2-11 channel; receive the selected
           PDU with Table 4.2-10 ChSel and the step-3 data, beginning an event.
        6. Repeat step 5 until fifty complete intervals have been detected.
        7. Disable advertising with the selected Enable command; receive
           Command Complete.

    Expected Outcome:
        Pass verdict: each round carries the exact Table 4.2-11 data on its
        specified observation channel, using ADV_IND with ChSel 1 for BV-22,
        or ADV_NONCONN_IND with ChSel 0 for BV-112 and BV-113.

    Reference:
        LL.TS.p28.pdf, Section 4.2.3.3, LL/DDI/ADV/BV-22-C,
        LL/DDI/ADV/BV-112-C and LL/DDI/ADV/BV-113-C; PDF pages 136-139,
        printed pages 4.2-43 through 4.2-46, Tables 4.2-10 and 4.2-11.
        Source reference [10], Section 4.4.2.3; Figure 4.2-18, steps 1-7.
    """
    kind = (
        AdvertisingPduType.ADV_IND
        if connectable
        else AdvertisingPduType.ADV_NONCONN_IND
    )
    if extended:
        await tester.command(
            extended_parameters(
                advertising_event_properties=Properties.USE_LEGACY_ADVERTISING_PDUS,
                primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
            )
        )
    else:
        await tester.command(
            legacy_parameters(
                advertising_type=(
                    AdvType.ADV_IND if connectable else AdvType.ADV_NONCONN_IND
                ),
                advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
            )
        )
    for channel, data in zip(
        LL_ADV_PRIMARY_CHANNELS, (b'\x01', b'', LEGACY_MAXIMUM_DATA)
    ):
        if extended:
            await set_extended_data(tester, data)
        else:
            await tester.command(hci.HCI_LE_Set_Advertising_Data_Command(data))
        await tester.command(
            extended_enable()
            if extended
            else hci.HCI_LE_Set_Advertising_Enable_Command(1)
        )
        await _observe_intervals(tester, channel, kind, data, 50, chsel=connectable)
        await tester.command(
            extended_enable(0)
            if extended
            else hci.HCI_LE_Set_Advertising_Enable_Command(0)
        )


# Verify the prescribed HCI family, legacy PDU, ChSel and data/channel rounds.
PROCEDURES |= procedure_cases(
    extended_legacy_undirected,
    {
        'LL/DDI/ADV/BV-112-C': (
            'Extended Advertising, Legacy PDUs, Undirected, Non-connectable',
            {},
        ),
        'LL/DDI/ADV/BV-113-C': (
            'Extended Advertising, Legacy PDUs, Undirected, CSA #1, Non-connectable',
            {'extended': True},
        ),
        'LL/DDI/ADV/BV-22-C': (
            (
                'Extended Advertising, Legacy PDUs, CSA #2, Connectable and'
                ' Scannable Undirected'
            ),
            {'connectable': True},
        ),
    },
)


async def extended_nonconnectable(tester, *, phy, coding_options=None):
    """Extended Advertising, Non-Connectable.

    Initial Condition:
        Advertising parameters are selected within the supported interval range,
        with a supported own-address type and all supported primary channels.
        This implementation selects a public own address and a 200-ms interval.
        Tables 4.2-12/13 select the APIs' primary/secondary PHYs. BV-47, BV-48
        and BV-49 use 1M/1M, Coded/Coded and 1M/2M, with no coding preference
        and no Host coding-selection bit. BV-81–85 use Coded/Coded and set that
        bit before configuring advertising parameters.

    Test Case Configuration:
        BV-81–85 use primary/secondary coding options (0,0), (1,2), (2,1),
        (3,4) and (4,3). Only options 3 (require S=2) and 4 (require S=8)
        constrain the lower tester's air encoding verdict.
        Table 4.2-14 rows 1-6: undirected data lengths 0, 31, 474, 711, 948,
        capacity; row 7: capacity with fragmentation preference 1. Rows 8-10:
        directed lengths 0, 251, capacity. Rows 11/12: undirected/directed, no
        data, Duration 500 (5 s). Rows 13/14: undirected/directed, no data,
        Max_Extended_Advertising_Events 50. Unspecified preferences, durations
        and event counts are zero.

    Test Steps:
        1. Read the maximum advertising-data capacity and require 31..1650 octets.
           Store the returned capacity for the subsequent rounds.
        Repeat Steps 2-13 for each of the fourteen Table 4.2-14 rows:
        2. Continue to Step 3 if the row's data length fits the capacity;
           otherwise skip this row and continue with the next row.
        3. Set the row's properties, all primary channels, selected interval and
           the case's primary/secondary PHYs and coding options. Directed rows
           use the Lower Tester's public peer address. BV-81–85 use v2 for the
           configured coding options. The other cases use v1 defaults.
        4. Submit the row's data and fragmentation preference, using seeded random
           octets 1..254. Lengths above 251 use FIRST, zero or more INTERMEDIATE,
           and LAST fragments. Round 2 uses FIRST with one octet, 29 one-octet
           INTERMEDIATE fragments, and LAST with one octet. Otherwise use COMPLETE.
        5. Enable advertising with the row's Duration and Max_Extended_Advertising_
           Events values, receiving successful Command Complete responses.
        6. Receive ADV_EXT_IND with AdvMode 00 and no SuppInfo, SyncInfo, TxPower,
           ACAD or AdvData. Nonempty data requires AuxPtr; empty data permits it.
           With AuxPtr, require ADI and the configured SID; without it, forbid ADI.
           Check the configured primary PHY. Options 0–2 permit either coding
           on Coded PHY. Options 3 and 4 require S=2 and S=8, respectively.
        7. If AuxPtr is absent, continue at Step 10.
        8. Follow AuxPtr to AUX_ADV_IND, checking pointer timing, AdvMode 00,
           matching ADI, the selected secondary PHY and no SuppInfo, SyncInfo or
           TxPower. Incomplete data requires another AuxPtr. The same coding-option
           rule as Step 6 applies to the secondary PHY.
        9. Follow each further AuxPtr to AUX_CHAIN_IND until no pointer remains
           and all data is received. Check AdvMode 00, matching ADI, secondary PHY,
           additional submitted data, and no AdvA, TargetA, SuppInfo, TxPower or
           SyncInfo. Apply the secondary coding rule to every chain PDU.
        10. If Max_Extended_Advertising_Events is nonzero, repeat Steps 6-9 until
            advertising stops. Require at least one event and no more than the
            requested count, then Advertising Set Terminated with status 0x43.
            Continue at Step 13.
        11. Otherwise, if Duration is nonzero, repeat Steps 6-9 through the duration,
            then verify no additional events start and receive Advertising Set
            Terminated with status 0x3C. Continue at Step 13.
        12. Otherwise, repeat Steps 6-9 until ten complete intervals are observed
            (eleven primary packets on one monitored primary channel).
        13. Disable using LE Set Extended Advertising Enable before the next row.

    Expected Outcome:
        Pass verdict: the capacity is in range and every executed row produces
        ADV_EXT_IND with the prescribed fields and PHY. Nonempty data has AuxPtr;
        all referenced PDUs meet T_MAFS, contain the same SID/DID, and deliver
        exactly all submitted data without prohibited fields. Directed TargetA
        matches the configured public peer and appears in exactly one of the
        primary or first auxiliary PDU; for Coded primary plus AuxPtr it appears
        in the auxiliary PDU. Address type bits agree with present public fields.
        Every chain PDU uses the configured secondary PHY. Duration-limited rows
        stop within the source timing envelope with status 0x3C; count-limited
        rows do not exceed the requested count and terminate with status 0x43.
        Completed-event counts match independently observed primary events,
        and no further advertising is received after termination.

    Timing Measurements:
        For D = Duration * 10 ms, the source Notes permit minimum duration
        D * (1 - 500 ppm) - 16 us and maximum (D + 10 ms) * (1 + 500 ppm) + 16 us.
        Bracket enable by its command submission/completion times. The latest
        primary start uses the upper enable bracket plus the maximum duration.
        The last primary start must be no earlier than the lower enable bracket
        plus the minimum duration minus one maximum allowed advertising interval.
        This last-start lower bound is a conservative RF observation, not a
        specified HCI delivery deadline; final packet completion is not equated
        to the advertising event's start. Interval limits include 500-ppm drift
        and 32-us two-endpoint jitter.

    Source Correction:
        Step 2 refers to Step 14, but the procedure ends at Step 13. Interpret
        the capacity-exceeded branch as skipping the current table row, not as
        sending commands for an unsupported length. Amber confirms this reading;
        this is a local correction, not an adopted SIG erratum.
        Step 3 assigns both PHY and both PHY Options to one table. Read primary
        fields from Table 4.2-12 and secondary fields from Table 4.2-13.

    Reference:
        LL.TS.p28.pdf, Section 4.2.3.4, LL/DDI/ADV/BV-47-C,
        LL/DDI/ADV/BV-48-C, LL/DDI/ADV/BV-49-C and LL/DDI/ADV/BV-81-C through
        LL/DDI/ADV/BV-85-C; PDF pages 139-146,
        printed pages 4.2-46 through 4.2-53, Tables 4.2-12 through 4.2-14.
    """
    if coding_options is not None:
        await tester.command(hci.HCI_LE_Set_Host_Feature_Command(41, 1))
    primary_option, secondary_option = coding_options or (0, 0)
    primary_coding = REQUIRED_ADV_CODING.get(primary_option)
    secondary_coding = REQUIRED_ADV_CODING.get(secondary_option)
    capacity = (
        await tester.command(hci.HCI_LE_Read_Maximum_Advertising_Data_Length_Command())
    ).max_advertising_data_length
    assert PDU_ADV_LEGACY_DATA_SIZE_MAX <= capacity <= hci.HCI_LE_ADV_DATA_SIZE_MAX
    rounds = [(0, length, 0, 0, 0) for length in (0, 31, 474, 711, 948, capacity)]
    rounds += [(0, capacity, 1, 0, 0)]
    rounds += [
        (Properties.DIRECTED_ADVERTISING, length, 0, 0, 0)
        for length in (0, 251, capacity)
    ]
    rounds += [
        (properties, 0, 0, duration, maximum)
        for duration, maximum in ((500, 0), (0, 50))
        for properties in (0, Properties.DIRECTED_ADVERTISING)
    ]
    generator = random.Random(0)
    interval_us = 320 * hci.HCI_LE_ADV_INTERVAL_UNIT_US
    primary_phy = Phy.LE_CODED if phy == Phy.LE_CODED else Phy.LE_1M
    forbidden = ('address', 'target', 'sync', 'power', 'acad', 'cte')
    for round_number, (properties, length, preference, duration, maximum) in enumerate(
        rounds, 1
    ):
        if length > capacity:
            continue
        await tester.command(
            extended_parameters(
                coding_options=coding_options,
                advertising_event_properties=properties,
                peer_address=LOWER_CENTRAL_PUBLIC_ADDRESS,
                peer_address_type=hci.AddressType.PUBLIC_DEVICE,
                primary_advertising_phy=primary_phy,
                secondary_advertising_phy=phy,
                primary_advertising_interval_min=320,
                primary_advertising_interval_max=320,
                primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
            )
        )
        data = bytes(generator.randint(1, 254) for _ in range(length))
        if round_number == 2:
            for index, octet in enumerate(data):
                operation = (
                    Operation.FIRST_FRAGMENT
                    if index == 0
                    else (
                        Operation.LAST_FRAGMENT
                        if index == len(data) - 1
                        else Operation.INTERMEDIATE_FRAGMENT
                    )
                )
                await tester.command(
                    hci.HCI_LE_Set_Extended_Advertising_Data_Command(
                        0, operation, preference, bytes([octet])
                    )
                )
        else:
            await set_extended_data(tester, data, preference=preference)
        before_enable = tester.radio.now_us
        await tester.command(
            hci.HCI_LE_Set_Extended_Advertising_Enable_Command(
                1, [0], [duration], [maximum]
            )
        )
        duration_us = duration * hci.HCI_LE_DURATION_UNIT_US
        tolerance = (
            duration_us * LL_SCA_PPM[LL_SCA_500_PPM] // PHY_TIME_UNITS_PER_SECOND
            + ADVERTISING_INTERVAL_JITTER_US // 2
        )
        maximum_duration = duration_us + LL_ADV_DELAY_MAX_US
        maximum_duration += (
            maximum_duration * LL_SCA_PPM[LL_SCA_500_PPM] // PHY_TIME_UNITS_PER_SECOND
            + ADVERTISING_INTERVAL_JITTER_US // 2
        )
        deadline = tester.radio.now_us + maximum_duration if duration else None
        previous_start = None
        count = 0
        while True:
            primary = await tester.receive(
                37, duration_us=2 * interval_us, phy=primary_phy
            )
            if primary is None:
                assert duration or maximum
                break
            first = _nonconnectable_extended_packet(primary, primary_phy)
            if primary_coding is not None:
                assert primary.coding == primary_coding
            assert first['data'] == b'' and all(
                field not in first for field in forbidden[2:]
            )
            if previous_start is not None:
                minimum_interval, maximum_interval = advertising_interval_limits(
                    interval_us
                )
                assert (
                    minimum_interval
                    <= primary.start_us - previous_start
                    <= maximum_interval
                )
            previous_start = primary.start_us
            if deadline is not None:
                assert primary.start_us <= deadline
            if 'aux' in first:
                assert 'adi' in first
                _, packet, fields = await follow_auxiliary(tester, primary, first)
                assert _nonconnectable_extended_packet(packet, phy) == fields
                assert fields['adi'] == first['adi']
                assert all(field not in fields for field in ('cte', 'sync', 'power'))
                assert (
                    int.from_bytes(fields['adi'], 'little') >> PDU_ADV_ADI_DID_BITS == 3
                )
                assert sum('address' in item for item in (first, fields)) == 1
                assert next(
                    item['address'] for item in (first, fields) if 'address' in item
                ) == bytes(DUT_ADDRESS)
                assert sum('target' in item for item in (first, fields)) == int(
                    bool(properties)
                )
                if properties:
                    assert fields.get('target', first.get('target')) == bytes(
                        LOWER_CENTRAL_PUBLIC_ADDRESS
                    )
                if primary_phy == Phy.LE_CODED:
                    assert 'address' not in first and 'target' not in first
                assert (
                    await chain(
                        tester,
                        packet,
                        fields,
                        forbidden=forbidden,
                        expected_phy=phy,
                        expected_coding=secondary_coding,
                    )
                    == data
                )
            else:
                assert not data and 'adi' not in first
                assert first['address'] == bytes(DUT_ADDRESS)
                assert first.get('target') == (
                    bytes(LOWER_CENTRAL_PUBLIC_ADDRESS) if properties else None
                )
            count += 1
            assert not maximum or count <= maximum
            if not duration and (not maximum) and (count == 11):
                break
        if duration or maximum:
            assert count > 0, 'No ADV_EXT_IND received in an executed round'
            event = await tester.event(hci.HCI_LE_Advertising_Set_Terminated_Event)
            assert event.advertising_handle == 0
            assert event.status == (
                ErrorCode.LIMIT_REACHED_ERROR
                if maximum
                else ErrorCode.ADVERTISING_TIMEOUT_ERROR
            )
            assert event.num_completed_extended_advertising_events == count
            if duration:
                assert (
                    previous_start is not None
                    and previous_start
                    >= before_enable
                    + duration_us
                    - advertising_interval_limits(interval_us)[1]
                    - tolerance
                )
            assert (
                await tester.receive(37, duration_us=2 * interval_us, phy=primary_phy)
                is None
            )
        await tester.command(extended_enable(0))


# Verify non-connectable extended advertising on the primary and secondary channels:
# payload fields, AuxPtr timing, complete data chains, directed and undirected events,
# and commanded duration/event-count limits.
PROCEDURES |= procedure_cases(
    extended_nonconnectable,
    {
        'LL/DDI/ADV/BV-47-C': (
            'Extended Advertising, Non-Connectable – LE 1M PHY',
            {'phy': Phy.LE_1M},
        ),
        'LL/DDI/ADV/BV-48-C': (
            'Extended Advertising, Non-Connectable – LE Coded PHY',
            {'phy': Phy.LE_CODED},
        ),
        'LL/DDI/ADV/BV-49-C': (
            'Extended Advertising, Non-Connectable – LE 2M PHY',
            {'phy': Phy.LE_2M},
        ),
        'LL/DDI/ADV/BV-81-C': (
            'Extended Advertising, Non-Connectable – LE Coded PHY, No Coding '
            'Preference',
            {'phy': Phy.LE_CODED, 'coding_options': (0, 0)},
        ),
        'LL/DDI/ADV/BV-82-C': (
            'Extended Advertising, Non-Connectable – LE Coded PHY, Recommended, '
            'Primary S=2, Secondary S=8',
            {'phy': Phy.LE_CODED, 'coding_options': (1, 2)},
        ),
        'LL/DDI/ADV/BV-83-C': (
            'Extended Advertising, Non-Connectable – LE Coded PHY, Recommended, '
            'Primary S=8, Secondary S=2',
            {'phy': Phy.LE_CODED, 'coding_options': (2, 1)},
        ),
        'LL/DDI/ADV/BV-84-C': (
            'Extended Advertising, Non-Connectable – LE Coded PHY, Required, '
            'Primary S=2, Secondary S=8',
            {'phy': Phy.LE_CODED, 'coding_options': (3, 4)},
        ),
        'LL/DDI/ADV/BV-85-C': (
            'Extended Advertising, Non-Connectable – LE Coded PHY, Required, '
            'Primary S=8, Secondary S=2',
            {'phy': Phy.LE_CODED, 'coding_options': (4, 3)},
        ),
    },
)


async def extended_advertising_change_coding(tester):
    """Extended Advertising, Non-Connectable, Advertising Coding Selection.

    Initial Condition:
        Start with the public own address, all primary channels, a 200-ms
        interval and the Advertising Coding Selection Host Support bit set.

    Test Steps:
        1. Read the supported maximum advertising data length.
        2. Configure an undirected Coded/Coded set to require S=2 on both
           channels. Set the maximum-length data and enable advertising.
        3. Follow ADV_EXT_IND to AUX_ADV_IND and any AUX_CHAIN_IND PDUs.
           Check that every PDU uses S=2 and the chain reproduces the payload.
        4. Disable advertising and reconfigure the same set to require S=8.
           Enable without resending the data. Repeat the air checks for S=8.

    Expected Outcome:
        Reconfiguration changes the coding of the primary and every auxiliary
        PDU while preserving the complete, maximum-length payload.

    Reference:
        LL.TS.p28, LL/DDI/ADV/BV-86-C, Figures 4.2-61 and 4.2-62, Steps 1-13,
        printed pages 4.2-141 through 4.2-144.
    """
    await tester.command(hci.HCI_LE_Set_Host_Feature_Command(41, 1))
    capacity = (
        await tester.command(hci.HCI_LE_Read_Maximum_Advertising_Data_Length_Command())
    ).max_advertising_data_length
    assert PDU_ADV_LEGACY_DATA_SIZE_MAX <= capacity <= hci.HCI_LE_ADV_DATA_SIZE_MAX
    data = random.Random(0).randbytes(capacity)
    for option, coding in REQUIRED_ADV_CODING.items():
        await tester.command(
            extended_parameters(
                coding_options=(option, option),
                primary_advertising_phy=Phy.LE_CODED,
                secondary_advertising_phy=Phy.LE_CODED,
                primary_advertising_interval_min=320,
                primary_advertising_interval_max=320,
                primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
            )
        )
        if coding == PHY_CODED_S2_FACTOR:
            await set_extended_data(tester, data)
        await tester.command(extended_enable())
        primary = await tester.expect_pdu(
            kind=AdvertisingPduType.ADV_EXT_IND, duration_us=400_000, phy=Phy.LE_CODED
        )
        fields = _nonconnectable_extended_packet(primary, Phy.LE_CODED)
        assert primary.coding == coding and 'aux' in fields
        _, packet, auxiliary = await follow_auxiliary(tester, primary, fields)
        assert _nonconnectable_extended_packet(packet, Phy.LE_CODED) == auxiliary
        assert auxiliary['adi'] == fields['adi']
        assert (
            await chain(
                tester,
                packet,
                auxiliary,
                expected_phy=Phy.LE_CODED,
                expected_coding=coding,
            )
            == data
        )
        await tester.command(extended_enable(0))


# Verify reconfiguring an existing set changes coding without discarding AdvData.
PROCEDURES |= procedure_cases(
    extended_advertising_change_coding,
    {
        'LL/DDI/ADV/BV-86-C': (
            'Extended Advertising, Non-Connectable, Advertising Coding Selection',
            {},
        ),
    },
)


async def extended_scan_response(tester, *, phy):
    """Extended Advertising, Scannable.

    Initial Condition:
        Scannable advertising has selected supported intervals, own-address type,
        primary channels and name/data. This implementation uses public addresses
        and all primary channels. Table 4.2-15 selects primary/secondary PHYs
        1M/1M (BV-45), 1M/2M (BV-52), or Coded/Coded (BV-54). For these selected
        cases, AUX_SCAN_RSP ADI may be absent or match AUX_ADV_IND ADI. The selected
        interval is 100 ms on uncoded PHYs and 150 ms on Coded to fit the data chain.

    Test Case Configuration:
        Table 4.2-16 rows 1-6: undirected scannable lengths 1, 31, 474, 711, 948,
        capacity. Row 7: capacity, notification enabled, fragmentation preference 1.
        Row 8: 31 octets, wrong AdvA. Rows 9-11: directed scannable lengths 1,
        251, capacity. Row 12: directed, 31 octets, wrong AdvA. All other rows
        disable notifications and use fragmentation preference 0.

    Test Steps:
        1. Read maximum advertising-data capacity and require 31..1650 octets.
           Store the capacity for subsequent rows.
        Repeat Steps 2-13 for every Table 4.2-16 row:
        2. If the row's data length fits, continue at Step 3; otherwise proceed
           to Step 13 to disable the set, then continue with the next row.
        3. Set all primary channels and selected intervals, the row's advertising
           properties and Scan_Request_Notification_Enable, and the PHYs from
           Table 4.2-15. Directed rows select the Lower Tester's public peer address.
        4. Submit seeded random response octets 1..254 and the row's fragmentation
           preference. Lengths above 251 use FIRST, zero or more INTERMEDIATE,
           then LAST. Round 2 uses FIRST with one octet, 29 one-octet INTERMEDIATE
           commands and LAST with one octet. Otherwise use COMPLETE.
        5. Enable extended advertising with Duration 0 and receive Command Complete.
        6. Receive ADV_EXT_IND with AdvMode 10, AuxPtr, and ADI with the configured
           SID. Forbid SuppInfo, SyncInfo, TxPower and ACAD; check primary PHY.
        7. Follow AuxPtr to AUX_ADV_IND on the secondary PHY with AdvMode 10,
           matching ADI, and no SuppInfo, AuxPtr, SyncInfo, TxPower or AdvData.
        8. Send AUX_SCAN_REQ on that secondary channel at T_IFS after AUX_ADV_IND,
           with public ScanA equal to the Lower Tester and the row's AdvA.
        9. For wrong AdvA, require no AUX_SCAN_RSP and continue at Step 12.
        10. Otherwise receive AUX_SCAN_RSP at T_IFS after AUX_SCAN_REQ, within
            +/-2 us, with AdvMode 00, IUT AdvA, no TargetA or SuppInfo, and ADI as
            permitted by the selected case. Incomplete response data requires AuxPtr.
        11. Follow every AuxPtr to AUX_CHAIN_IND with AdvMode 00 and the remaining
            data until the final pointer is absent and all data is received.
            Check the secondary PHY and prohibit AdvA, TargetA, SuppInfo, TxPower
            and SyncInfo. If AUX_SCAN_RSP has ADI, every chain PDU must carry
            the same ADI; otherwise every chain PDU must omit ADI.
        12. If notifications are enabled, receive Scan Request Received with the
            selected handle and the request's public scanner address/type.
        13. Disable extended advertising before the next row. Before disabling,
            observe another primary/auxiliary event to confirm continued advertising.

    Expected Outcome:
        Pass verdict: capacity is in range and every executed row produces the
        prescribed primary/auxiliary PDUs, PHYs, SID and consistent advertising ADI.
        Every AuxPtr meets T_MAFS and its encoded channel/time. Correct requests
        receive the prescribed response at T_IFS +/-2 us with exactly all submitted
        data and no extra data; wrong AdvA receives no response. Response and chain
        fields obey Steps 10/11, including conditional response-chain ADI inheritance.
        Enabled notifications identify the handle and public scanner. Advertising
        continues after the response; unexpected notifications fail the test.
        The +/-2-us response tolerance and referenced 1-us interval measurement
        accuracy require no sub-microsecond stimulus or measurement.

    Source Correction:
        Step 3 points to Table 4.2-16 for PHYs; the PHYs are in Table 4.2-15,
        while 4.2-16 supplies per-round parameters. Step 6 and the pass verdict
        list TxPower twice; enforce the prohibition once. The PDF 151 verdict's
        unconditional chain-ADI prohibition is a confirmed test-text error.
        Apply Core 6.3 Vol 6 Part B Section 2.3.1.8, Table 2.7 C.1: chain ADI
        is mandatory and matches the superior PDU when that PDU includes ADI;
        otherwise it is absent. AUX_SCAN_RSP ADI remains optional per Table 4.2-15.

    Reference:
        LL.TS.p28.pdf, Section 4.2.3.5, LL/DDI/ADV/BV-45-C,
        LL/DDI/ADV/BV-52-C and LL/DDI/ADV/BV-54-C; PDF pages 146-151,
        printed pages 4.2-53 through 4.2-58, Tables 4.2-15 and 4.2-16.
    """
    primary_phy = Phy.LE_CODED if phy == Phy.LE_CODED else Phy.LE_1M
    capacity = (
        await tester.command(hci.HCI_LE_Read_Maximum_Advertising_Data_Length_Command())
    ).max_advertising_data_length
    assert PDU_ADV_LEGACY_DATA_SIZE_MAX <= capacity <= hci.HCI_LE_ADV_DATA_SIZE_MAX
    rounds = [
        (False, length, False, 0, DUT_ADDRESS)
        for length in (1, 31, 474, 711, 948, capacity)
    ]
    rounds += [
        (False, capacity, True, 1, DUT_ADDRESS),
        (False, 31, False, 0, LOWER_CENTRAL_PUBLIC_ADDRESS),
    ]
    rounds += [(True, length, False, 0, DUT_ADDRESS) for length in (1, 251, capacity)]
    rounds += [(True, 31, False, 0, LOWER_CENTRAL_PUBLIC_ADDRESS)]
    generator = random.Random(0)
    for index, (directed, length, notify, preference, target) in enumerate(rounds):
        if length > capacity:
            await tester.command(extended_enable(0))
            continue
        await tester.command(
            extended_parameters(
                advertising_event_properties=Properties.SCANNABLE_ADVERTISING
                | (Properties.DIRECTED_ADVERTISING if directed else 0),
                peer_address=LOWER_CENTRAL_PUBLIC_ADDRESS,
                peer_address_type=LOWER_CENTRAL_PUBLIC_ADDRESS.address_type,
                scan_request_notification_enable=int(notify),
                primary_advertising_phy=primary_phy,
                secondary_advertising_phy=phy,
                primary_advertising_interval_min=240 if phy == Phy.LE_CODED else 160,
                primary_advertising_interval_max=240 if phy == Phy.LE_CODED else 160,
                primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
            )
        )
        data = bytes(generator.randint(1, 254) for _ in range(length))
        if index == 1:
            for offset, octet in enumerate(data):
                await tester.command(
                    hci.HCI_LE_Set_Extended_Scan_Response_Data_Command(
                        0,
                        (
                            Operation.FIRST_FRAGMENT
                            if offset == 0
                            else (
                                Operation.LAST_FRAGMENT
                                if offset == len(data) - 1
                                else Operation.INTERMEDIATE_FRAGMENT
                            )
                        ),
                        preference,
                        bytes([octet]),
                    )
                )
        else:
            await set_extended_data(tester, data, response=True, preference=preference)
        await tester.command(extended_enable())
        primary = await tester.expect_pdu(
            kind=AdvertisingPduType.ADV_EXT_IND, duration_us=200000, phy=primary_phy
        )
        assert primary.phy == primary_phy
        primary_fields = decode_extended(primary.pdu)
        assert (
            primary.pdu[PDU_HEADER_SIZE] >> PDU_ADV_EXT_HEADER_LENGTH_BITS
            == AdvertisingMode.SCANNABLE
        )
        assert set(primary_fields) == {'adi', 'aux', 'data'} and (
            not primary_fields['data']
        )
        channel, advertisement, fields = await follow_auxiliary(
            tester, primary, primary_fields, phy=phy
        )
        assert advertisement.phy == phy
        assert (
            advertisement.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.ADV_EXT_IND
        )
        assert (
            advertisement.pdu[PDU_HEADER_SIZE] >> PDU_ADV_EXT_HEADER_LENGTH_BITS
            == AdvertisingMode.SCANNABLE
        )
        assert set(fields) == {'address', 'adi', 'data'} | (
            {'target'} if directed else set()
        )
        assert fields['address'] == bytes(DUT_ADDRESS) and (not fields['data'])
        assert not advertisement.pdu[0] & PDU_ADV_TXADD_MASK
        assert fields['adi'] == primary_fields['adi']
        assert int.from_bytes(fields['adi'], 'little') >> PDU_ADV_ADI_DID_BITS == 3
        if directed:
            assert fields['target'] == bytes(LOWER_CENTRAL_PUBLIC_ADDRESS)
            assert not advertisement.pdu[0] & PDU_ADV_RXADD_MASK
        await tester.transmit(
            channel,
            request_pdu(
                AdvertisingPduType.SCAN_REQ,
                advertiser=target,
                scanner=LOWER_CENTRAL_PUBLIC_ADDRESS,
            ),
            start_us=advertisement.end_us + LL_IFS_US,
            phy=phy,
        )
        end = tester.radio.now_us + 1
        if target != DUT_ADDRESS:
            assert await tester.receive(channel, duration_us=10000, phy=phy) is None
        else:
            response = await tester.expect_pdu(
                channel, kind=AdvertisingPduType.ADV_EXT_IND, duration_us=1000, phy=phy
            )
            assert response.phy == phy
            assert abs(response.start_us - end - LL_IFS_US) <= RESPONSE_IFS_TOLERANCE_US
            response_fields = decode_extended(response.pdu)
            assert (
                response.pdu[PDU_HEADER_SIZE] >> PDU_ADV_EXT_HEADER_LENGTH_BITS
                == AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
            )
            assert response_fields['address'] == bytes(DUT_ADDRESS)
            assert not response.pdu[0] & PDU_ADV_TXADD_MASK
            assert response_fields.get('adi', fields['adi']) == fields['adi']
            assert all(key not in response_fields for key in ('target', 'cte', 'sync'))
            assert (
                await chain(
                    tester,
                    response,
                    response_fields,
                    expected_phy=phy,
                    forbidden=(
                        'address',
                        'target',
                        'cte',
                        'sync',
                        'power',
                        'acad',
                    ),
                )
                == data
            )
            if notify:
                event = await tester.event(hci.HCI_LE_Scan_Request_Received_Event)
                assert (
                    event.advertising_handle == 0
                    and event.scanner_address == LOWER_CENTRAL_PUBLIC_ADDRESS
                    and (
                        event.scanner_address_type
                        == LOWER_CENTRAL_PUBLIC_ADDRESS.address_type
                    )
                )
        await receive_extended(tester, phy=phy, primary_phy=primary_phy)
        assert not any(
            isinstance(event, hci.HCI_LE_Scan_Request_Received_Event)
            for event in tester.events
        ), 'unexpected scan notification'
        await tester.command(extended_enable(0))


# Verify scannable ADV_EXT_IND/AUX_ADV_IND fields, secondary-channel scan
# requests/responses, complete response data chains, directed/undirected events, request
# notifications, and continued advertising after a response.
PROCEDURES |= procedure_cases(
    extended_scan_response,
    {
        'LL/DDI/ADV/BV-45-C': (
            'Extended Advertising, Scannable – ADI allowed in scan response',
            {'phy': Phy.LE_1M},
        ),
        'LL/DDI/ADV/BV-52-C': (
            (
                'Extended Advertising, Scannable – ADI allowed in scan '
                'response – LE 2M PHY'
            ),
            {'phy': Phy.LE_2M},
        ),
        'LL/DDI/ADV/BV-54-C': (
            (
                'Extended Advertising, Scannable – ADI allowed in scan '
                'response – LE Coded PHY'
            ),
            {'phy': Phy.LE_CODED},
        ),
    },
)


async def extended_data_update(tester):
    """Extended Advertising, Host Modifying Data and ADI.

    Initial Condition:
        Advertising parameters have selected supported intervals, event type,
        own-address type and primary channels. This implementation uses public
        non-connectable/non-scannable undirected advertising on all primary
        channels, LE 1M on both PHYs and a selected interval of 100 ms.

    Test Case Configuration:
        Table 4.2-19 submits the reported capacity, one octet, and 251 octets.

    Test Steps:
        1. Read capacity, require 31..1650 octets and store the result.
        2. Configure all primary channels, a supported selected interval,
           properties zero and LE 1M on both PHYs; this also selects the SID.
        Repeat Steps 3-9 for each Table 4.2-19 row:
        3. If the row length fits the capacity, proceed to Step 4; otherwise
           proceed to Step 10 to disable advertising.
        4. Submit the row's data using seeded random octets 1..254. Lengths above
           251 use FIRST, zero or more INTERMEDIATE fragments, then LAST;
           other lengths use COMPLETE.
        5. Enable extended advertising with Duration 0 only in round 1; later
           rounds update the still-enabled set without disabling it.
        Repeat Steps 6-9 ten times per executed row:
        6. Receive ADV_EXT_IND with AdvMode 00, AuxPtr and ADI containing the SID
           selected in Step 2 and an Advertising DID.
        7. Follow AuxPtr to LE 1M AUX_ADV_IND with AdvMode 00 and matching ADI.
           If it does not contain all the data, it must include another AuxPtr.
        8. Follow every AuxPtr to AUX_CHAIN_IND with AdvMode 00 and further data
           until no pointer remains and all data has been received.
        9. Except for the very first advertisement, compare concatenated RF data
           with the preceding advertisement, ignoring PDU boundaries. Changed
           data with unchanged DID fails. A new DID must carry exactly the current
           submission; a previous DID may retain the previous payload. Require
           the submitted data to appear during the ten observations.
        10. Disable extended advertising after all executed rows or the capacity
            branch from Step 3.

    Expected Outcome:
        Pass verdict: capacity is in range. ADV_EXT_IND has the configured SID
        and AuxPtr referring to AUX_ADV_IND with matching ADI. Every AuxPtr meets
        T_MAFS and its encoded channel/time; every secondary PDU uses LE 1M.
        Data read under a new DID contains exactly the current submission and no
        further data, including all chain fragments. Every changed RF payload
        changes DID, and each executed row is actually observed. Do not impose
        an additional HCI-to-air update deadline absent from the source.

    Source Correction:
        Step 6 names Step 3 as the SID configuration step; Step 3 only checks
        capacity, while Step 2 sets advertising parameters. Use the Step 2 SID,
        consistent with the source pass verdict. Amber supports this local
        cross-reference correction; no adopted SIG erratum is claimed.

    Reference:
        LL.TS.p28.pdf, Section 4.2, LL/DDI/ADV/BV-27-C;
        PDF pages 156-159, printed pages 4.2-63 through 4.2-66,
        Table 4.2-19 and Figure 4.2-23.
    """
    capacity = (
        await tester.command(hci.HCI_LE_Read_Maximum_Advertising_Data_Length_Command())
    ).max_advertising_data_length
    assert PDU_ADV_LEGACY_DATA_SIZE_MAX <= capacity <= hci.HCI_LE_ADV_DATA_SIZE_MAX
    await tester.command(
        extended_parameters(
            primary_advertising_interval_min=160,
            primary_advertising_interval_max=160,
            primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
        )
    )
    previous = None
    generator = random.Random(0)
    for index, length in enumerate(
        (capacity, 1, hci.HCI_LE_EXT_ADV_DATA_FRAGMENT_SIZE_MAX)
    ):
        if length > capacity:
            break
        data = bytes(generator.randint(1, 254) for _ in range(length))
        await set_extended_data(tester, data)
        if index == 0:
            await tester.command(extended_enable())
        observed_current = False
        for _ in range(10):
            primary = await tester.expect_pdu(
                kind=AdvertisingPduType.ADV_EXT_IND, duration_us=200000
            )
            first_fields = _nonconnectable_extended_packet(primary, Phy.LE_1M)
            assert 'aux' in first_fields and 'adi' in first_fields
            _, packet, fields = await follow_auxiliary(tester, primary, first_fields)
            assert _nonconnectable_extended_packet(packet, Phy.LE_1M) == fields
            assert fields['adi'] == first_fields['adi']
            assert sum('address' in item for item in (first_fields, fields)) == 1
            assert next(
                item['address'] for item in (first_fields, fields) if 'address' in item
            ) == bytes(DUT_ADDRESS)
            assert all(
                'target' not in item and 'sync' not in item
                for item in (first_fields, fields)
            )
            assert int.from_bytes(fields['adi'], 'little') >> PDU_ADV_ADI_DID_BITS == 3
            observed = await chain(tester, packet, fields, expected_phy=Phy.LE_1M)
            did = int.from_bytes(fields['adi'], 'little') & PDU_ADV_ADI_DID_MASK
            previous = _check_advertising_update(previous, observed, did, data)
            observed_current |= observed == data
        assert observed_current, 'Submitted data never appeared in this round'
    await tester.command(extended_enable(0))


# Verify non-connectable, undirected primary/auxiliary advertising and complete data
# chains while the Host changes data on an enabled set. Confirm that changed RF data
# changes DID and new-DID data exactly matches the current Host submission,
# independently of fragmentation boundaries.
PROCEDURES |= procedure_cases(
    extended_data_update,
    {
        'LL/DDI/ADV/BV-27-C': (
            'Extended Advertising, Host Modifying Data and ADI',
            {},
        ),
    },
)


async def overlapping_events(tester, *, rounds=(1, 2, 3)):
    """Extended Advertising, Overlapping Extended Advertising Events.

    Initial Condition:
        Advertising parameters select supported intervals, an advertising event
        type, own-address type and primary channel map. This implementation uses
        public addresses, all primary channels and LE 1M on both PHYs. Table
        4.2-20 selects rounds 1-3 for BV-28, round 4 for BV-115, round 5 for
        BV-116 and round 6 for BV-117.

    Test Case Configuration:
        Table 4.2-21 rows (properties, Max_Skip, repeat count):
        1: (0, 1, 100); 2: (0, 15, 50); 3: (0, 255, 10);
        4: (connectable, 8, 50); 5: (directed, 8, 50);
        6: (connectable directed, 8, 50).

    Test Steps:
        Repeat Steps 1-9 for each selected Table 4.2-21 row:
        1. Set extended advertising parameters using all primary channels and a
           supported selected interval, the row's properties and Max_Skip, and
           LE 1M on both PHYs. Directed rows target the public Lower Tester.
        2. Set one advertising-data octet using a seeded random value 1..254.
        3. Enable extended advertising with Duration 0.
        Repeat Steps 4-8 using the row's repeat count:
        4. Receive primary ADV_EXT_IND with AuxPtr and AdvMode matching the row's
           properties; validate the RF envelope and required ADI.
        5. Decode Aux Offset and Offset Units to compute the target time range
           for the referred AUX_ADV_IND and retain its RF channel.
        6. Receive further primary events until that target is reached, repeating
           Steps 4-5 and recalculating the common time range. Require the same
           auxiliary channel/PHY, matching ADI, and overlapping pointer ranges
           within their Offset Units.
        7. Follow AuxPtr to LE 1M AUX_ADV_IND with the expected AdvMode. Verify
           the actual start is in every retained pointer range, meets T_MAFS,
           and follows the preceding auxiliary event without overlap.
        8. Record the number of primary events that skipped an auxiliary PDU.
           Require at most Max_Skip skips and hence at most Max_Skip+1
           overlapping extended events. Check the configured addresses and data.
        9. Disable extended advertising before the next selected row.

    Expected Outcome:
        Pass verdict: every ADV_EXT_IND AuxPtr refers to a valid AUX_ADV_IND
        meeting T_MAFS. Pointers from overlapping events identify a common time
        within one Offset Unit and the same secondary channel/PHY. Skipped events
        do not exceed Max_Skip and no more than Max_Skip+1 events overlap.
        Received modes, ADI, addresses and the one submitted data octet match
        the configured row.

    Reference:
        LL.TS.p28.pdf, Section 4.2.3.7, LL/DDI/ADV/BV-28-C,
        LL/DDI/ADV/BV-115-C, LL/DDI/ADV/BV-116-C and LL/DDI/ADV/BV-117-C;
        PDF pages 159-161, printed pages 4.2-66 through 4.2-68,
        Tables 4.2-20 and 4.2-21.
    """
    configurations = (
        (0, 1, 100),
        (0, 15, 50),
        (0, 255, 10),
        (Properties.CONNECTABLE_ADVERTISING, 8, 50),
        (Properties.DIRECTED_ADVERTISING, 8, 50),
        (Properties.CONNECTABLE_ADVERTISING | Properties.DIRECTED_ADVERTISING, 8, 50),
    )
    generator = random.Random(0)
    for round_number in rounds:
        properties, skip, count = configurations[round_number - 1]
        await tester.command(
            extended_parameters(
                advertising_event_properties=properties,
                peer_address=LOWER_CENTRAL_PUBLIC_ADDRESS,
                peer_address_type=hci.AddressType.PUBLIC_DEVICE,
                primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
                secondary_advertising_max_skip=skip,
            )
        )
        data = bytes([generator.randint(1, 254)])
        await set_extended_data(tester, data)
        await tester.command(extended_enable())
        previous_end = 0
        mode = (
            AdvertisingMode.CONNECTABLE
            if properties & Properties.CONNECTABLE_ADVERTISING
            else AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
        )
        for _ in range(count):
            primary = await tester.expect_pdu()
            fields, channel, target_min, target_max = _overlapping_advertisement(
                primary, mode
            )
            unit = target_max - target_min
            skipped = 0
            while tester.radio.now_us + 1 < target_min - unit:
                following = await tester.receive(
                    37, duration_us=target_min - unit - tester.radio.now_us - 1
                )
                if following is None:
                    break
                next_fields, next_channel, next_min, next_max = (
                    _overlapping_advertisement(following, mode)
                )
                assert next_channel == channel
                target_min = max(target_min, next_min)
                target_max = min(target_max, next_max)
                assert target_min < target_max
                assert next_fields['adi'] == fields['adi']
                primary, fields, unit = (following, next_fields, next_max - next_min)
                skipped += 1
                assert skipped <= skip
            first_fields = fields
            _, packet, fields = await follow_auxiliary(tester, primary, fields)
            assert packet.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.ADV_EXT_IND
            assert packet.start_us > previous_end
            assert target_min <= packet.start_us < target_max
            assert packet.phy == Phy.LE_1M
            assert packet.pdu[PDU_HEADER_SIZE] >> PDU_ADV_EXT_HEADER_LENGTH_BITS == mode
            assert fields['adi'] == first_fields['adi']
            assert sum('address' in item for item in (first_fields, fields)) == 1
            assert next(
                item['address'] for item in (first_fields, fields) if 'address' in item
            ) == bytes(DUT_ADDRESS)
            for item in (first_fields, fields):
                if 'target' in item:
                    assert item['target'] == bytes(LOWER_CENTRAL_PUBLIC_ADDRESS)
            assert sum('target' in item for item in (first_fields, fields)) == int(
                bool(properties & Properties.DIRECTED_ADVERTISING)
            )
            assert fields['data'] == data and 'aux' not in fields
            previous_end = packet.end_us
        await tester.command(extended_enable(0))


# Verify that primary advertising events refer to a valid secondary PDU and correctly
# implement Secondary_Advertising_Max_Skip. Observe event timing and packet contents,
# including AuxPtrs from overlapping events.
PROCEDURES |= procedure_cases(
    overlapping_events,
    {
        'LL/DDI/ADV/BV-28-C': (
            'Extended Advertising, Overlapping Extended Advertising Events',
            {},
        ),
        'LL/DDI/ADV/BV-115-C': (
            (
                'Extended Advertising, Overlapping Extended Advertising '
                'Events, Connectable'
            ),
            {'rounds': (4,)},
        ),
        'LL/DDI/ADV/BV-116-C': (
            ('Extended Advertising, Overlapping Extended Advertising Events, Directed'),
            {'rounds': (5,)},
        ),
        'LL/DDI/ADV/BV-117-C': (
            (
                'Extended Advertising, Overlapping Extended Advertising '
                'Events, Connectable Directed'
            ),
            {'rounds': (6,)},
        ),
    },
)


MULTI_EXTENDED_ROUNDS = (
    (0, 1, 0, 0),
    (1, 0, 0, 191),
    (0, 5, 191, 0),
    (0, 0, 191, 191),
    (0, 4, 251, 251),
    (0, 1, 253, 0),
    (0, 0, 474, 0),
)
MULTI_LEGACY_ROUNDS = (
    (0, 16, 0, 0),
    (19, 0, 0, 191),
    (0, 19, 191, 31),
    (19, 16, 31, 31),
    (21, 4, 0, 251),
    (0, 29, 253, 0),
    (0, 18, 474, 0),
)


async def _configure_advertising_sets(
    tester, properties, phys, lengths, *, intervals=(160, 160), skip_empty=False
):
    generator = random.Random(104)
    payloads = []
    for handle, (flags, phy, interval) in enumerate(zip(properties, phys, intervals)):
        await tester.command(
            extended_parameters(
                advertising_handle=handle,
                advertising_sid=handle,
                advertising_event_properties=flags,
                peer_address=LOWER_CENTRAL_PUBLIC_ADDRESS,
                peer_address_type=LOWER_CENTRAL_PUBLIC_ADDRESS.address_type,
                primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
                primary_advertising_interval_min=interval,
                primary_advertising_interval_max=interval,
                primary_advertising_phy=phy,
                secondary_advertising_phy=phy,
            )
        )
    for handle, length in enumerate(lengths):
        payload = bytes(generator.randrange(1, 255) for _ in range(length))
        payloads.append(payload)
        if length or not skip_empty:
            await set_extended_data(tester, payload, handle=handle)
    return payloads


def _extended_advertising_set(packet, fields, properties, phys, payloads):
    """Identify a configured set without requiring an optional auxiliary PDU."""
    mode = packet.pdu[PDU_HEADER_SIZE] >> PDU_ADV_EXT_HEADER_LENGTH_BITS
    assert not fields['data']
    if 'aux' in fields:
        assert 'adi' in fields
        handle = int.from_bytes(fields['adi'], 'little') >> PDU_ADV_ADI_DID_BITS
        assert handle in range(len(properties))
    else:
        assert 'adi' not in fields
        assert mode == AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
        matches = [
            index
            for index, flags in enumerate(properties)
            if not (
                flags
                & (
                    Properties.USE_LEGACY_ADVERTISING_PDUS
                    | Properties.CONNECTABLE_ADVERTISING
                )
            )
            and packet.phy == phys[index]
            and not payloads[index]
            and bool(flags & Properties.DIRECTED_ADVERTISING) == ('target' in fields)
        ]
        assert len(matches) == 1, 'Primary-only event does not identify a unique set'
        handle = matches[0]
    flags = properties[handle]
    assert not flags & Properties.USE_LEGACY_ADVERTISING_PDUS
    assert packet.phy == phys[handle]
    assert mode == (
        AdvertisingMode.CONNECTABLE
        if flags & Properties.CONNECTABLE_ADVERTISING
        else AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
    )
    if flags & Properties.CONNECTABLE_ADVERTISING or (
        packet.phy == Phy.LE_CODED and 'aux' in fields
    ):
        assert 'address' not in fields and 'target' not in fields
    return handle


def _advertising_set_addresses(packets, directed):
    """Check public identity once across the primary and first auxiliary PDU."""
    for name, expected, mask, count in (
        ('address', DUT_ADDRESS, PDU_ADV_TXADD_MASK, 1),
        ('target', LOWER_CENTRAL_PUBLIC_ADDRESS, PDU_ADV_RXADD_MASK, int(directed)),
    ):
        present = [
            (packet, fields[name]) for packet, fields in packets if name in fields
        ]
        assert len(present) == count
        for packet, address in present:
            assert address == bytes(expected) and not packet.pdu[0] & mask


async def _observe_advertising_sets(
    tester, properties, phys, payloads, *, repetitions=10
):
    """Observe every primary channel; check each set and complete auxiliary chain."""
    legacy_types = {
        16: AdvertisingPduType.ADV_NONCONN_IND,
        19: AdvertisingPduType.ADV_IND,
        21: AdvertisingPduType.ADV_DIRECT_IND,
        29: AdvertisingPduType.ADV_DIRECT_IND,
        18: AdvertisingPduType.ADV_SCAN_IND,
    }
    counts = [0, 0]
    channels = [set(), set()]
    # Single-observation callers still need a sample on every primary channel.
    per_channel, extra = divmod(repetitions, len(LL_ADV_PRIMARY_CHANNELS))
    for position, channel in enumerate(LL_ADV_PRIMARY_CHANNELS):
        target = [count + max(1, per_channel + (position < extra)) for count in counts]
        for attempt in range(repetitions * 200):
            phy = phys[attempt % len(phys)]
            packet = await tester.receive(channel, duration_us=200000, phy=phy)
            if packet is None:
                continue
            assert packet.valid_crc and packet.phy == phy
            kind = packet.pdu[0] & PDU_ADV_TYPE_MASK
            assert packet.pdu[1] == len(packet.pdu) - PDU_HEADER_SIZE
            if kind == AdvertisingPduType.ADV_EXT_IND:
                fields = decode_extended(packet.pdu)
                handle = _extended_advertising_set(
                    packet, fields, properties, phys, payloads
                )
                flags = properties[handle]
                addresses = [(packet, fields)]
                expected_mode = (
                    AdvertisingMode.CONNECTABLE
                    if flags & Properties.CONNECTABLE_ADVERTISING
                    else AdvertisingMode.NONCONNECTABLE_NONSCANNABLE
                )
                if 'aux' in fields:
                    adi = fields['adi']
                    _, packet, fields = await follow_auxiliary(tester, packet, fields)
                    assert fields['adi'] == adi
                    assert packet.phy == phys[handle]
                    assert (
                        packet.pdu[0] & PDU_ADV_TYPE_MASK
                        == AdvertisingPduType.ADV_EXT_IND
                    )
                    assert (
                        packet.pdu[PDU_HEADER_SIZE] >> PDU_ADV_EXT_HEADER_LENGTH_BITS
                        == expected_mode
                    )
                    addresses.append((packet, fields))
                _advertising_set_addresses(
                    addresses, bool(flags & Properties.DIRECTED_ADVERTISING)
                )
                assert (
                    await chain(tester, packet, fields, expected_phy=phys[handle])
                    == payloads[handle]
                )
            else:
                matches = [
                    handle
                    for handle, flags in enumerate(properties)
                    if legacy_types.get(flags) == kind
                ]
                matches = [
                    handle
                    for handle in matches
                    if packet.pdu[PDU_HEADER_SIZE + PDU_ADV_ADDRESS_SIZE :]
                    == (
                        bytes(LOWER_CENTRAL_PUBLIC_ADDRESS)
                        if properties[handle] & Properties.DIRECTED_ADVERTISING
                        else payloads[handle]
                    )
                ]
                assert len(matches) == 1, (kind, packet.pdu.hex(), matches)
                handle = matches[0]
                assert phy == phys[handle]
                assert not packet.pdu[0] & PDU_ADV_TXADD_MASK
                if properties[handle] & Properties.DIRECTED_ADVERTISING:
                    assert not packet.pdu[0] & PDU_ADV_RXADD_MASK
                assert packet.pdu[
                    PDU_HEADER_SIZE : PDU_HEADER_SIZE + PDU_ADV_ADDRESS_SIZE
                ] == bytes(DUT_ADDRESS)
            counts[handle] += 1
            channels[handle].add(channel)
            if all((count >= goal for count, goal in zip(counts, target))):
                break
        assert all((count >= goal for count, goal in zip(counts, target))), (
            channel,
            counts,
        )
    assert min(counts) >= repetitions and all(len(seen) == 3 for seen in channels)


async def _advertising_rounds(tester, table, rounds, phys, *, skip_empty=False):
    capacity = (
        await tester.command(hci.HCI_LE_Read_Maximum_Advertising_Data_Length_Command())
    ).max_advertising_data_length
    assert PDU_ADV_LEGACY_DATA_SIZE_MAX <= capacity <= hci.HCI_LE_ADV_DATA_SIZE_MAX
    for number in rounds:
        first, second, size0, size1 = table[number - 1]
        if max(size0, size1) > capacity:
            tester.output.write(
                f'ROUND {number} INAPPLICABLE: reported data capacity\n'
            )
            continue
        properties = (first, second)
        payloads = await _configure_advertising_sets(
            tester, properties, phys, (size0, size1), skip_empty=skip_empty
        )
        durations = [
            (
                128
                if flags & Properties.HIGH_DUTY_CYCLE_DIRECTED_CONNECTABLE_ADVERTISING
                else 0
            )
            for flags in properties
        ]
        await tester.command(
            hci.HCI_LE_Set_Extended_Advertising_Enable_Command(
                1, [0, 1], durations, [0, 0]
            )
        )
        await _observe_advertising_sets(tester, properties, phys, payloads)
        await tester.command(
            hci.HCI_LE_Set_Extended_Advertising_Enable_Command(
                0, [0, 1], [0, 0], [0, 0]
            )
        )
        await tester.command(hci.HCI_LE_Clear_Advertising_Sets_Command())


async def multiple_sets_1m(tester, *, rounds):
    """Extended Advertising, Multiple Sets, Single PHY, LE 1M PHY.

    Initial Condition:
        Advertising parameters select supported intervals, event properties,
        own-address type and primary channels. This implementation uses public
        sets 0/1, distinct SIDs 0/1, all primary channels and 100-ms intervals.
        Both primary and secondary PHYs are LE 1M. Table 4.2-22 selects which
        Table 4.2-23 rows are executed; directed sets use the public Lower Tester.

    Test Case Configuration:
        Table 4.2-23 rows (first properties, second properties, first length,
        second length): (0,1,0,0), (1,0,0,191), (0,5,191,0), (0,0,191,191),
        (0,4,251,251), (0,1,253,0), (0,0,474,0). Properties 0/1/4/5 mean
        non-connectable undirected/connectable undirected/non-connectable
        directed/connectable directed. BV-29 selects rows 4/7; BV-118 selects
        1/2/6; BV-119 selects 5; BV-120 selects 3. All HCI commands require success.

    Test Steps:
        1. Read maximum advertising-data length, require 31..1650 octets and
           store the capacity.
        Repeat Steps 2-11 for the case's Table 4.2-23 rows:
        2. If both data lengths fit the capacity, proceed to Step 3; otherwise
           skip the entire row and continue with the next row.
        3. Configure the first set using all primary channels, a supported selected
           interval, its row properties, and LE 1M on both PHYs.
        4. Configure the second set with its row properties, all primary channels,
           a supported selected interval, public Own_Address_Type and both PHYs 1M.
        5. For each set submit seeded random octets 1..254 at the row's length.
           Above 251 octets use FIRST, zero or more INTERMEDIATE, then LAST;
           otherwise use COMPLETE, including zero-length data.
        6. Enable both sets with one extended enable command and both Durations 0.
        Repeat Steps 7-9 ten times per set, monitoring channels 37/38/39:
        7. Receive interlaced ADV_EXT_IND events with each set's expected AdvMode.
           Identify an auxiliary-bearing set by SID; a primary-only empty set
           is identified by its unique configured mode, PHY and directedness.
        8. If AuxPtr is present, follow it to LE 1M AUX_ADV_IND with the same
           AdvMode and matching ADI. Incomplete data requires a further AuxPtr.
           Check encoded channel/time and at least T_MAFS after the superior PDU.
        9. Follow every further pointer to AUX_CHAIN_IND with AdvMode 00 and the
           remaining data until no pointer remains and all data is received.
           Require every chain PHY to remain LE 1M and the ADI to match.
        10. Disable both sets in one extended advertising enable command.
        11. Clear both sets using LE Clear Advertising Sets.

    Expected Outcome:
        Pass verdict: capacity is in range and both configured sets produce
        interlaced primary events with their expected modes and identities.
        AuxPtr is optional for an empty non-connectable set; when present it
        refers to a valid LE 1M auxiliary PDU and each pointer meets T_MAFS.
        Reassembled data is exactly the submission for that set, with no extra
        data, including empty rows. Every required set is observed ten times
        across the supported primary channels with its correct public addresses.

    Source Correction:
        Step 2 refers to Step 12, but the procedure ends at Step 11. Interpret
        the capacity-exceeded branch as skipping the entire row, as confirmed
        by Amber from the supplied source. This is not an adopted SIG erratum.

    Reference:
        LL.TS.p28.pdf, Section 4.2.3.8, LL/DDI/ADV/BV-29-C,
        LL/DDI/ADV/BV-118-C, LL/DDI/ADV/BV-119-C and LL/DDI/ADV/BV-120-C;
        PDF pages 162-166, printed pages 4.2-69 through 4.2-73,
        Tables 4.2-22 and 4.2-23.
    """
    await _advertising_rounds(
        tester, MULTI_EXTENDED_ROUNDS, rounds, (Phy.LE_1M, Phy.LE_1M)
    )


# Verify simultaneous LE 1M advertising sets with the configured payload fields, timing
# and channels, with and without data. Check interlaced primary events and exact
# independently submitted data through auxiliaries and chains when present.
PROCEDURES |= procedure_cases(
    multiple_sets_1m,
    {
        'LL/DDI/ADV/BV-29-C': (
            'Extended Advertising, Multiple Sets, Single PHY, LE 1M PHY',
            {'rounds': (4, 7)},
        ),
        'LL/DDI/ADV/BV-118-C': (
            'Extended Advertising, Multiple Sets, Single PHY, LE 1M PHY',
            {'rounds': (1, 2, 6)},
        ),
        'LL/DDI/ADV/BV-119-C': (
            'Extended Advertising, Multiple Sets, Single PHY, LE 1M PHY',
            {'rounds': (5,)},
        ),
        'LL/DDI/ADV/BV-120-C': (
            'Extended Advertising, Multiple Sets, Single PHY, LE 1M PHY',
            {'rounds': (3,)},
        ),
    },
)


async def multiple_sets_phys(tester, *, phys):
    """Extended Advertising, Multiple Sets.

    Initial Condition:
        Advertising parameters select supported intervals, event properties,
        own-address type and primary channels. This implementation uses public
        sets 0/1 with SIDs 0/1, all primary channels and 100-ms intervals. Both
        primary and secondary PHYs of a set use its Table 4.2-24 PHY: Coded/Coded
        across the two sets for BV-30, and 1M/Coded for BV-31. Directed sets
        target the public Lower Tester.

    Test Case Configuration:
        Table 4.2-25 rows (first properties, second properties, first length,
        second length): (0,1,0,0), (1,0,0,191), (0,5,191,0), (0,0,191,191),
        (0,4,251,251), (0,1,253,0), (0,0,474,0). The properties mean
        non-connectable undirected (0), connectable undirected (1),
        non-connectable directed (4), or connectable directed (5).
        Both cases execute every applicable row. All commands require success.

    Test Steps:
        1. Read maximum advertising-data length, require 31..1650 octets and
           retain the capacity.
        Repeat Steps 2-11 for all seven Table 4.2-25 rows:
        2. Proceed if both data lengths fit the capacity; otherwise skip the row.
        3. Configure the first set's row properties, all primary channels and a
           supported selected interval, using its assigned PHY on both channels.
        4. Configure the second set likewise with its own row properties and
           assigned PHY, explicitly selecting a public Own_Address_Type.
        5. For each set submit the specified length of seeded octets 1..254.
           Above 251 octets use FIRST, zero or more INTERMEDIATE, then LAST;
           otherwise use COMPLETE, including zero-length data.
        6. Enable both sets in one extended enable command, both Durations 0.
        Repeat Steps 7-9 ten times per set, monitoring all primary channels:
        7. Receive interlaced ADV_EXT_IND with each set's mode and assigned PHY.
           Use SID when AuxPtr is present; otherwise identify the unique empty
           primary-only set without requiring ADI or an auxiliary packet.
        8. Follow an AuxPtr, when present, to AUX_ADV_IND with the set's assigned
           PHY and expected AdvMode. Check ADI, channel, encoded timing and T_MAFS.
           If data is incomplete, require a further pointer.
        9. Follow every further pointer to AUX_CHAIN_IND with AdvMode 00 and
           additional data until the final pointer is absent and all data is
           received, retaining that set's secondary PHY and matching ADI.
        10. Disable both sets in one extended advertising enable command.
        11. Clear the advertising sets with one LE Clear Advertising Sets command.

    Expected Outcome:
        Pass verdict: capacity is in range; both sets produce interlaced primary
        events on their configured PHYs. Optional AuxPtrs refer to valid auxiliary
        packets with the same set's PHY and at least T_MAFS spacing. Each complete
        data chain equals that set's submission, without additional data; empty
        sets contain none. Both sets are observed ten times across primary channels,
        with the prescribed modes, public identities and matching ADI when present.

    Source Correction:
        Step 2 names nonexistent Step 12; skip the entire oversized row after
        checking both lengths. The last pass bullet's generic Coded wording
        does not override Table 4.2-24 or Steps 7/8: BV-31 uses LE 1M for its
        first set and Coded for its second. Amber supports both readings from
        the supplied source; these are local corrections, not adopted SIG errata.

    Reference:
        LL.TS.p28.pdf, Section 4.2.3.9, LL/DDI/ADV/BV-30-C and LL/DDI/ADV/BV-31-C;
        PDF pages 166-170, printed pages 4.2-73 through 4.2-77,
        Tables 4.2-24 and 4.2-25.
    """
    await _advertising_rounds(tester, MULTI_EXTENDED_ROUNDS, range(1, 8), phys)


# Verify simultaneous advertising sets using the assigned Coded/Coded or 1M/Coded PHY
# pair, including empty data, directed/connectable properties, interlaced primary
# events, valid auxiliary timing and complete data chains.
PROCEDURES |= procedure_cases(
    multiple_sets_phys,
    {
        'LL/DDI/ADV/BV-30-C': (
            'Extended Advertising, Multiple Sets, Single PHY, LE Coded PHY',
            {'phys': (Phy.LE_CODED, Phy.LE_CODED)},
        ),
        'LL/DDI/ADV/BV-31-C': (
            (
                'Extended Advertising, Multiple Sets, Multiple PHYs, LE 1M '
                'and LE Coded PHYs'
            ),
            {'phys': (Phy.LE_1M, Phy.LE_CODED)},
        ),
    },
)


async def multiple_legacy_extended_sets(tester, *, rounds):
    """Extended Advertising, Multiple Sets, Legacy and Extended.

    Initial Condition:
        Advertising parameters select supported intervals, event properties,
        own-address type and primary channels. Both sets use public addresses,
        all primary channels and LE 1M. This implementation selects 100-ms
        intervals for non-high-duty sets, handles/SIDs 0/1, and the public Lower
        Tester for directed advertising. Table 4.2-26 selects the rows per case.

    Test Case Configuration:
        Table 4.2-27 rows (first properties, second properties, first length,
        second length): (0x00,0x10,0,0), (0x13,0x00,0,191), (0x00,0x13,191,31),
        (0x13,0x10,31,31), (0x15,0x04,0,251), (0x00,0x1D,253,0),
        (0x00,0x12,474,0). BV-32 selects row 1; BV-121 selects 2-4;
        BV-122 selects 5; BV-123 selects 6; BV-124 selects 7.
        Require successful HCI responses throughout.

    Test Steps:
        1. Read maximum advertising-data length, require 31..1650 octets and
           store the capacity.
        Repeat Steps 2-11 for the case's Table 4.2-27 rows:
        2. If both row lengths fit the capacity, proceed to Step 3; otherwise
           skip the entire row and proceed to the next row.
        3. Set the first advertising set's row properties using all primary
           channels, a supported interval and LE 1M primary/secondary PHYs.
        4. Set the second set's row properties with the same channel/PHY choices,
           a supported interval and a public Own_Address_Type.
        5. For each nonempty set, submit seeded octets 1..254. Above 251 use
           FIRST, zero or more INTERMEDIATE, then LAST; otherwise use COMPLETE.
           For a zero-length set, send no data command.
        6. Enable both sets in one command. Duration is 0x0080 (1280 ms) only
           for properties 0x001D; otherwise it is zero.
        Repeat Steps 7-9 ten times per set, monitoring every primary channel:
        7. Receive interlaced legacy or ADV_EXT_IND events according to each
           set's properties. Check the exact legacy kind or extended AdvMode.
        8. If AuxPtr is present, follow it to LE 1M AUX_ADV_IND with the expected
           mode and matching ADI, encoded channel/time and T_MAFS spacing.
           Incomplete data requires a further AuxPtr. Empty non-connectable
           primary-only sets do not require an auxiliary packet or ADI.
        9. Follow further pointers to AUX_CHAIN_IND with AdvMode 00 and additional
           data until all data is received and no pointer remains. Require LE 1M
           on every secondary PDU, and exact set-specific reassembled data.
        10. Disable both sets using one extended advertising enable command.
        11. Clear the advertising sets using one LE Clear Advertising Sets command.

    Expected Outcome:
        Pass verdict: capacity is in range and both sets interlace the expected
        legacy or extended primary PDUs. Any AuxPtr refers to a valid LE 1M
        auxiliary packet with T_MAFS spacing. Each set's complete legacy data or
        extended chain equals the submitted data without additional bytes; empty
        rows contain no data. Both sets are observed ten times across the primary
        channels, with the configured public AdvA/TargetA and correct mode/type.

    Source Correction:
        Step 2 refers to Step 12, but the source ends at Step 11. Skip the
        entire row if either requested data length exceeds the capacity.
        Amber supports this reading from the supplied source; no adopted
        SIG erratum is claimed.

    Reference:
        LL.TS.p28.pdf, Section 4.2.3.10, LL/DDI/ADV/BV-32-C,
        LL/DDI/ADV/BV-121-C through LL/DDI/ADV/BV-124-C;
        PDF pages 170-174, printed pages 4.2-77 through 4.2-81,
        Tables 4.2-26 and 4.2-27.
    """
    await _advertising_rounds(
        tester, MULTI_LEGACY_ROUNDS, rounds, (Phy.LE_1M, Phy.LE_1M), skip_empty=True
    )


# Verify parallel legacy and extended advertising sets, including correct payload
# fields, timing and channels, empty/nonempty data and complete auxiliary chains.
# Distinguish sets by their prescribed PDU types and data.
PROCEDURES |= procedure_cases(
    multiple_legacy_extended_sets,
    {
        'LL/DDI/ADV/BV-32-C': (
            'Extended Advertising, Multiple Sets, Legacy and Extended',
            {'rounds': (1,)},
        ),
        'LL/DDI/ADV/BV-121-C': (
            'Extended Advertising, Multiple Sets, Single PHY, LE 1M PHY',
            {'rounds': (2, 3, 4)},
        ),
        'LL/DDI/ADV/BV-122-C': (
            'Extended Advertising, Multiple Sets, Single PHY, LE 1M PHY',
            {'rounds': (5,)},
        ),
        'LL/DDI/ADV/BV-123-C': (
            'Extended Advertising, Multiple Sets, Single PHY, LE 1M PHY',
            {'rounds': (6,)},
        ),
        'LL/DDI/ADV/BV-124-C': (
            'Extended Advertising, Multiple Sets, Single PHY, LE 1M PHY',
            {'rounds': (7,)},
        ),
    },
)


async def maximum_sets(tester):
    """Extended Advertising, Multiple Sets, Maximum Supported Sets.

    Initial Condition:
        Advertising parameters select supported intervals, event properties,
        own-address type and channel map. TSPX_adv_sets_max is supplied during
        preparation by Read Number of Supported Advertising Sets (eight for this
        DUT). Select public non-connectable/non-scannable undirected advertising,
        all primary channels, LE 1M on both PHYs and 100-ms intervals. Choose each
        SID in 0..15; uniqueness is not a source requirement. Independently seeded
        31-octet datasets distinguish sets that share a SID.

    Test Steps:
        1. Configure TSPX_adv_sets_max handles with all primary channels, a supported
           selected interval, properties zero, both PHYs 1M and a seeded random
           SID in 0..15 for each set. Complete all parameter commands first.
        2. Submit 31 seeded advertising-data octets, each 1..254, for every set.
        3. Enable each set using its own extended advertising enable command
           with Duration 0.
        4. Receive interlaced ADV_EXT_IND events with AuxPtr for every set.
        5. Follow AuxPtr to LE 1M AUX_ADV_IND; if all submitted data is not yet
           present, require a further AuxPtr. Check its encoded channel/time,
           minimum T_MAFS spacing and matching ADI.
        6. Follow every further pointer to AUX_CHAIN_IND containing additional
           data until no pointer remains and all data for the set is received.
           Every secondary PDU retains the configured PHY and chain ADI.
        7. Repeat Steps 4-6 ten times per configured set. Identify each complete
           observation by SID plus exact concatenated payload, not SID alone.
        8. Disable each set using its own extended advertising enable command.

    Expected Outcome:
        Pass verdict: the declared number of sets produce interlaced ADV_EXT_IND
        with AuxPtr to valid LE 1M AUX_ADV_IND. Each pointer meets T_MAFS and
        its encoded channel/time. Each set's complete data equals its submitted
        31 octets, without additional bytes, including any chain fragments.
        All sets are independently observed ten times, with correct public AdvA,
        AdvMode and matching ADI, even when some sets share a SID.

    Reference:
        LL.TS.p28.pdf, Section 4.2, LL/DDI/ADV/BV-35-C; PDF pages 180-182, printed pages
        4.2-87 through 4.2-89, Figure 4.2-34.
    """
    result = await tester.command(
        hci.HCI_LE_Read_Number_Of_Supported_Advertising_Sets_Command()
    )
    count = result.num_supported_advertising_sets
    assert count > 0
    generator = random.Random(0)
    datasets = {
        handle: (
            generator.randrange(PDU_ADV_ADI_SID_MAX + 1),
            bytes(generator.randint(1, 254) for _ in range(31)),
        )
        for handle in range(count)
    }
    for handle, (sid, _) in datasets.items():
        await tester.command(
            extended_parameters(
                advertising_handle=handle,
                advertising_sid=sid,
                primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
            )
        )
    for handle, (_, data) in datasets.items():
        await set_extended_data(tester, data, handle=handle)
    for handle in range(count):
        await tester.command(extended_enable(1, handle))
    received = dict.fromkeys(datasets, 0)
    for _ in range(20 * count):
        primary = await tester.expect_pdu(kind=AdvertisingPduType.ADV_EXT_IND)
        first = _nonconnectable_extended_packet(primary, Phy.LE_1M)
        assert 'aux' in first and 'adi' in first
        _, packet, fields = await follow_auxiliary(tester, primary, first)
        assert _nonconnectable_extended_packet(packet, Phy.LE_1M) == fields
        assert fields['adi'] == first['adi']
        _advertising_set_addresses(((primary, first), (packet, fields)), False)
        sid = int.from_bytes(fields['adi'], 'little') >> PDU_ADV_ADI_DID_BITS
        data = await chain(tester, packet, fields, expected_phy=Phy.LE_1M)
        matches = [
            handle for handle, expected in datasets.items() if expected == (sid, data)
        ]
        assert len(matches) == 1, 'Unknown or ambiguous advertising-set data'
        received[matches[0]] += 1
        if min(received.values()) >= 10:
            break
    assert min(received.values()) >= 10
    for handle in range(count):
        await tester.command(extended_enable(0, handle))


# Verify the declared maximum simultaneous advertising sets, including interlaced
# primary/auxiliary events, correct timing and channel use, and complete
# minimum-supported Host data in each set's auxiliary chain.
PROCEDURES |= procedure_cases(
    maximum_sets,
    {
        'LL/DDI/ADV/BV-35-C': (
            'Extended Advertising, Multiple Sets, Maximum Supported Sets',
            {},
        ),
    },
)


async def invalid_legacy_size(tester, *, scannable=False):
    """Disallow Extended Advertising.

    Initial Condition:
        Advertising parameters select supported intervals, event properties,
        own-address type and primary channels. Table 4.2-34 selects legacy
        non-connectable (BI-05) or legacy scannable (BI-06) PDUs through extended
        HCI commands. This implementation uses a public advertising set.

    Test Case Configuration:
        LL/DDI/ADV/BI-05-C: non-connectable legacy advertising.
        LL/DDI/ADV/BI-06-C: scannable legacy advertising.

    Test Steps:
        1. Send LE Set Extended Advertising Parameters with the Table 4.2-34
           properties: Use Legacy PDUs or Use Scannable Legacy PDUs.
        2. Send LE Set Extended Advertising Data with length 31; require Command
           Complete with status 0x00.
        3. Enable the same set with LE Set Extended Advertising Enable; require
           Command Complete with status 0x00.
        4. Send LE Set Extended Advertising Data with length 32; require Command
           Complete with status 0x12.

    Postamble:
        Disable the advertising set after the rejected command.

    Expected Outcome:
        Pass verdict: Step 4 returns Invalid HCI Command Parameters (0x12) in
        Command Complete for LE Set Extended Advertising Data. The preceding
        31-octet submission and enable succeed. Responses match their command
        opcodes and preserve valid command credit.

    Reference:
        LL.TS.p28.pdf, Section 4.2.3.13, LL/DDI/ADV/BI-05-C and LL/DDI/ADV/BI-06-C; PDF
        pages 195-196, printed pages 4.2-102 through 4.2-103, Table 4.2-34.
    """
    properties = Properties.USE_LEGACY_ADVERTISING_PDUS
    if scannable:
        properties |= Properties.SCANNABLE_ADVERTISING
    await tester.command(
        extended_parameters(
            advertising_event_properties=properties,
            primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
        )
    )
    await set_extended_data(tester, bytes(PDU_ADV_LEGACY_DATA_SIZE_MAX))
    await tester.command(extended_enable())
    await tester.command(
        hci.HCI_LE_Set_Extended_Advertising_Data_Command(
            0, Operation.COMPLETE_DATA, 0, bytes(PDU_ADV_LEGACY_DATA_SIZE_MAX + 1)
        ),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    await tester.command(extended_enable(0))


# Verify that a legacy-PDU advertising set rejects a 32-octet advertising data
# replacement while enabled, returning Invalid HCI Command Parameters.
PROCEDURES |= procedure_cases(
    invalid_legacy_size,
    {
        'LL/DDI/ADV/BI-05-C': (
            (
                'Disallow Extended Advertising PDU sizes for Legacy '
                'Advertising when advertising enabled'
            ),
            {},
        ),
        'LL/DDI/ADV/BI-06-C': (
            (
                'Disallow Extended Advertising PDU sizes for Scannable '
                'Legacy Advertising when advertising enabled'
            ),
            {'scannable': True},
        ),
    },
)


async def invalid_hop_increment(tester):
    """Connection Request Invalid Hop Increment.

    Initial Condition:
        Prepare undirected advertising with supported selected intervals, address
        type, channel and device-name/data length. Select a public AdvA, minimum
        supported interval, channel 37 and empty advertising data. No device name
        is transmitted; Section 4.1.6 requires the name IUT when one is sent.
        Allow requests from all devices. Use otherwise valid Common PDU Contents
        from Section 4.3.2 and ChSel 0 so the invalid hop is not bypassed by CSA #2.
        This DUT rejects invalid hop increments in connection-parameter decoding,
        so the executable path is the source's ignore alternative.

    Test Steps:
        1. Set legacy advertising parameters: ADV_IND (type 0), channel map 001,
           filter policy 0; require successful Command Complete.
        2. Repeat Steps 3-23 for hopIncrement 0..4 and 17..31.
        3. Prepare CONNECT_IND with that hop and otherwise valid common fields.
        4. Enable undirected advertising using the Step 1 channel map.
        5. Receive ADV_IND and send the prepared CONNECT_IND on the selected
           channel at T_IFS after the advertisement's end.
        6. The IUT either ignores the request or establishes the connection.
        7. For an ignored request, execute Steps 9-14 and omit Steps 15-23.
        8. For an accepted request, omit Steps 9-14 and execute Steps 15-23.
        9. Observe that advertising continues.
        10. Receive ADV_IND and send another CONNECT_IND with the same invalid
            hop on the selected channel at T_IFS.
        11. Send six data packets at the connection anchors/interval encoded in
            Step 10, hopping by that requested increment on the full channel map.
        12. Require no IUT response to any of those six data packets.
        13. Repeat Steps 9-12 five times.
        14. Disable advertising and receive successful Command Complete.
        Accepted-connection alternative (not taken by this DUT):
        15. Receive HCI LE Connection Complete.
        16. Execute Step 17 in parallel with Steps 18-20.
        17. After one advertising interval, require no further ADV_IND and observe
            for four advertising intervals to confirm advertising has stopped.
        18. Send data at each connection event on the channel obtained by adding
            the requested hop increment to the preceding unmapped channel.
        19. Require an acknowledgement for each data packet.
        20. Repeat Steps 18-19 for ten connection events.
        21. Send HCI Disconnect and receive successful Command Status.
        22. Receive LL_TERMINATE_IND.
        23. Receive HCI Disconnection Complete.

    Expected Outcome:
        Pass verdict: use a supported interval and the explicitly configured
        channel. For the ignore alternative, advertising continues and no data
        probe receives a response. For the accept alternative, the IUT reports
        connection completion, stops advertising and acknowledges the requested
        connection events. These are alternative outcomes, not cumulative duties.
        This DUT must satisfy the ignore outcome for all twenty hop values,
        with five six-packet probe groups per value and no connection-complete
        notification. Every observed advertisement has the configured public
        address, LE 1M PHY and empty data.

    Source Correction:
        References to Step 24 mean the actual procedure tail through Step 23;
        no Step 24 exists. Step 4 and the generic all-channel pass wording must
        retain Step 1's explicit map 001: Advertising Enable cannot change that
        map. Step 23 names a Disconnection Complete command, but it is an event.
        Amber supports the channel/numbering corrections and the alternative
        behavior reading from the supplied source. These are local corrections,
        not officially adopted SIG errata.

    Reference:
        LL.TS.p28.pdf, Section 4.2, LL/DDI/ADV/BI-07-C; PDF pages 207-210, printed pages
        4.2-114 through 4.2-117, Steps 1-23 and Figures 4.2-48/49.
        Section 4.1.6 defines common initial conditions; Section 4.3.2 defines
        otherwise valid connection-request contents.
    """
    await tester.command(legacy_parameters())
    for hop in (
        *range(LL_HOP_INCREMENT_MIN),
        *range(LL_HOP_INCREMENT_MAX + 1, PDU_ADV_CONNECT_HOP_MASK + 1),
    ):
        parameters = LinkParameters(
            access_address=LOWER_DATA_ACCESS_ADDRESS, hop_sca=hop
        )
        request = request_pdu(
            AdvertisingPduType.CONNECT_IND,
            scanner=LOWER_CENTRAL_PUBLIC_ADDRESS,
            data=parameters.encode(),
        )
        await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
        advertisement = await tester.expect_pdu(kind=AdvertisingPduType.ADV_IND)
        _check_advertisement(advertisement, b'')
        await tester.transmit(37, request, start_us=advertisement.end_us + LL_IFS_US)
        for _ in range(5):
            advertisement = await tester.expect_pdu(kind=AdvertisingPduType.ADV_IND)
            _check_advertisement(advertisement, b'')
            end = await tester.transmit(
                37, request, start_us=advertisement.end_us + LL_IFS_US
            )
            anchor = end + (1 + parameters.window_offset) * LL_CONN_INTERVAL_UNIT_US
            for counter in range(LL_CONN_ESTABLISHMENT_EVENTS):
                channel = (counter + 1) * hop % LL_DATA_CHANNEL_COUNT
                await tester.transmit(
                    channel,
                    bytes([LlId.CONTINUATION, 0]),
                    start_us=anchor
                    + counter * parameters.interval * LL_CONN_INTERVAL_UNIT_US,
                    access_address=parameters.access_address,
                    crc_init=parameters.crc_init,
                )
                assert (
                    await tester.receive(
                        channel,
                        duration_us=1000,
                        access_address=parameters.access_address,
                        crc_init=parameters.crc_init,
                    )
                    is None
                )
        advertisement = await tester.expect_pdu(kind=AdvertisingPduType.ADV_IND)
        _check_advertisement(advertisement, b'')
        assert not any(
            isinstance(
                event,
                (
                    hci.HCI_LE_Connection_Complete_Event,
                    hci.HCI_LE_Enhanced_Connection_Complete_Event,
                ),
            )
            for event in tester.events
        )
        await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(0))


# Verify that invalid-hop connection requests either leave the advertiser advertising
# without data responses, or result in a connection with stopped advertising and
# acknowledged data. Exercise every specified invalid hop increment using the applicable
# behavior of this DUT.
PROCEDURES |= procedure_cases(
    invalid_hop_increment,
    {
        'LL/DDI/ADV/BI-07-C': (
            'Connection Request Invalid Hop Increment',
            {},
        ),
    },
)


def _check_high_duty_timeout_events(events, handle):
    connections = [
        event
        for event in events
        if isinstance(
            event,
            (
                hci.HCI_LE_Connection_Complete_Event,
                hci.HCI_LE_Enhanced_Connection_Complete_Event,
            ),
        )
    ]
    terminated = [
        event
        for event in events
        if isinstance(event, hci.HCI_LE_Advertising_Set_Terminated_Event)
    ]
    assert connections or terminated, 'missing advertising timeout outcome'
    assert len(connections) <= 1, 'duplicate connection timeout outcomes'
    assert len(terminated) <= 1, 'duplicate advertising termination outcomes'
    for event in connections + terminated:
        assert event.status == ErrorCode.ADVERTISING_TIMEOUT_ERROR
    for event in terminated:
        assert event.advertising_handle == handle


async def high_duty_without_connection(tester):
    """High Duty Cycle Connectable Directed Advertising, No Connection.

    Initial Condition:
        Advertising parameters use a supported interval, event type, own address
        and channel map. TSPX_supported_advertising_channels declares used/unused
        channels. Select public own/peer addresses, the minimum interval and all
        three supported primary channels. The Lower Tester only receives packets.

    Test Steps:
        1. Configure the Lower Tester for passive scanning; send no scan or
           connection request during the procedure.
        2. Send LE Set Extended Advertising Parameters with properties 0x001D,
           all supported channels, a supported interval and Peer_Address equal
           to the Lower Tester. Require successful Command Complete.
        3. Send LE Set Extended Advertising Enable with Enable 1, Num_Sets 1,
           the Step 2 handle and Duration 0x000C. Require successful Command
           Complete. Preserve this literal; its correct duration is 120 ms.
        4. Receive ADV_DIRECT_IND from the IUT. Check the configured public
           AdvA/TargetA and LE 1M PHY on each supported primary channel.
        5. Do not attempt a connection for five seconds.
        6. Evaluate one of the following IUT-response alternatives:
           6A.1. Receive one LE Connection Complete or Enhanced Connection
                 Complete with Advertising Timeout (0x3C).
           6B.1. Receive one LE Advertising Set Terminated.
           6C.1. Execute 6C.2 and 6C.3 in either order.
           6C.2. Receive one LE Connection Complete or Enhanced Connection
                 Complete with Advertising Timeout (0x3C).
           6C.3. Receive one LE Advertising Set Terminated.

    Expected Outcome:
        Pass verdict: after Step 5, no connection completes and one of 6A, 6B or
        6C occurs. Legacy and enhanced connection-complete events are alternatives,
        not two required notifications. Reject duplicate outcomes. When duration
        expiry produces Advertising Set Terminated, additionally require status
        0x3C and the configured handle, as specified by Core 7.7.65.18; the test
        source's 6B.1/6C.3 does not itself spell out that status.

    Postamble:
        Confirm that advertising has stopped on all three primary channels, then
        disable the advertising set successfully.

    Source Correction:
        Step 3's parenthetical 1.2 seconds is inconsistent with Duration 0x000C.
        Core Vol 4, Part E, 7.8.56 defines 10-ms units, so retain the literal and
        interpret it as 120 ms. Amber confirms this arithmetic correction; do not
        invent a different literal to infer the author's intended duration. This
        is a local correction, not an adopted SIG erratum.

    Reference:
        LL.TS.p28.pdf, Section 4.2, LL/DDI/ADV/BV-80-C; PDF pages 232-234, printed pages
        4.2-139 through 4.2-141, Figure 4.2-60, all Steps 1-6 and alternatives
        6A.1/6B.1/6C.1-6C.3. This is a standalone case, not Section 4.2.3.17.
        Core Specification 6.3, Vol 4, Part E, 7.8.56 and 7.7.65.18 define the
        duration units and timeout-event behavior.
    """
    await tester.command(
        extended_parameters(
            advertising_event_properties=Properties.USE_LEGACY_ADVERTISING_PDUS
            | Properties.CONNECTABLE_ADVERTISING
            | Properties.DIRECTED_ADVERTISING
            | Properties.HIGH_DUTY_CYCLE_DIRECTED_CONNECTABLE_ADVERTISING,
            primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
            peer_address=LOWER_CENTRAL_PUBLIC_ADDRESS,
            peer_address_type=hci.AddressType.PUBLIC_DEVICE,
        )
    )
    before_enable = tester.radio.now_us
    await tester.command(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0], [0x000C], [0])
    )
    for channel in LL_ADV_PRIMARY_CHANNELS:
        packet = await tester.expect_pdu(
            channel, kind=AdvertisingPduType.ADV_DIRECT_IND
        )
        assert packet.phy == Phy.LE_1M
        assert packet.pdu[PDU_HEADER_SIZE:] == bytes(DUT_ADDRESS) + bytes(
            LOWER_CENTRAL_PUBLIC_ADDRESS
        )
        assert not packet.pdu[0] & (PDU_ADV_TXADD_MASK | PDU_ADV_RXADD_MASK)
    await tester.wait(
        before_enable + 5 * PHY_TIME_UNITS_PER_SECOND - tester.radio.now_us
    )
    _check_high_duty_timeout_events(tester.events, 0)
    for channel in LL_ADV_PRIMARY_CHANNELS:
        assert await tester.receive(channel) is None
    await tester.command(extended_enable(0))


# Verify that high-duty connectable directed advertising times out without completing a
# connection with the Lower Tester.
PROCEDURES |= procedure_cases(
    high_duty_without_connection,
    {
        'LL/DDI/ADV/BV-80-C': (
            'High Duty Cycle Connectable Directed Advertising, No Connection',
            {},
        ),
    },
)


async def _observe_selected_advertising_channel(tester, channel, enabled):
    # Observe fifty worst-case event intervals, not merely fifty received packets.
    _, maximum_interval = advertising_interval_limits(ADVERTISING_INTERVAL_US)
    deadline = tester.radio.now_us + 50 * maximum_interval
    count = 0
    while tester.radio.now_us < deadline:
        packet = await tester.receive(
            channel, duration_us=deadline - tester.radio.now_us
        )
        if packet is None:
            break
        assert packet.valid_crc
        assert packet.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.ADV_IND
        assert packet.pdu[1] == len(packet.pdu) - PDU_HEADER_SIZE
        _check_advertisement(packet, b'')
        count += 1
    assert count >= 45 if enabled else count == 0


async def selected_channels(tester):
    """Advertising on Selected Channels.

    Initial Condition:
        Undirected advertising uses a supported interval, own-address type,
        advertising channels and common device-name/data length. Select public
        ADV_IND at the minimum supported interval and no name/data. Repeat the
        complete procedure for the seven Table 4.2-48 maps 0x01 through 0x07.

    Test Steps:
        1. Send LE Set Advertising Parameters with Advertising_Type 0x00 and
           the current round's channel map; require successful Command Complete.
        2. Send LE Set Advertising Data with Advertising_Data_Length zero;
           require successful Command Complete.
        3. Send LE Set Advertising Enable with Enable 1; require successful
           Command Complete.
        4. The IUT sends ADV_IND on selected channels until Step 5 finishes.
        5. Monitor each of channels 37, 38 and 39 in turn, in any order, for at
           least fifty advertising events per channel. Observe for fifty maximum
           event intervals, including AdvDelay and common drift/jitter allowance.
           Check received PDUs' type, CRC, length, public AdvA, data and LE 1M PHY.
        6. Send LE Set Advertising Enable with Enable 0; require successful
           Command Complete before beginning the next channel-map round.

    Expected Outcome:
        Pass verdict: in Step 5, receive at least 45 ADV_IND PDUs on each selected
        channel, and no ADV_IND on each unselected channel. The fifty-event
        observation requirement is not a fifty-received-packet pass threshold.

    Reference:
        LL.TS.p28.pdf, Section 4.2, LL/DDI/ADV/BV-106-C; PDF pages 264-266, printed
        pages 4.2-171 through 4.2-173, Table 4.2-48, Figure 4.2-75, Steps 1-6
        and the per-channel pass verdict. This is a standalone case, not
        Section 4.2.3.19. Section 4.1.6 defines common selected initial conditions.
    """
    for channel_map in range(1, LL_ADV_PRIMARY_CHANNEL_MAP_ALL + 1):
        await tester.command(legacy_parameters(advertising_channel_map=channel_map))
        await tester.command(hci.HCI_LE_Set_Advertising_Data_Command(b''))
        await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
        for bit, channel in enumerate(LL_ADV_PRIMARY_CHANNELS):
            await _observe_selected_advertising_channel(
                tester, channel, bool(channel_map & 1 << bit)
            )
        await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(0))


# Verify advertising occurs on every enabled primary channel and nowhere on disabled
# primary channels.
PROCEDURES |= procedure_cases(
    selected_channels,
    {
        'LL/DDI/ADV/BV-106-C': (
            'Advertising on Selected Channels',
            {},
        ),
    },
)


# Core 6.3 Vol 6 Part B 4.4.2.4.3: maximum high-duty same-channel interval.
HIGH_DUTY_INTERVAL_MAX_US = 3750

# Section 4.2.3.15 Step 9: ten data generations, including the initial writes.
PRIMARY_DATA_ROUNDS = 10

# Core 4.4.2: consecutive primary advertising PDUs in an event are <=10 ms apart.
PRIMARY_EVENT_PDU_GAP_US = 10000

# Table 4.2-36: legacy properties, event kind, and enable duration (10 ms units).
PRIMARY_ROUNDS = {
    64: (Properties.USE_LEGACY_ADVERTISING_PDUS, AdvertisingPduType.ADV_NONCONN_IND, 0),
    65: (
        Properties.USE_LEGACY_ADVERTISING_PDUS
        | Properties.CONNECTABLE_ADVERTISING
        | Properties.SCANNABLE_ADVERTISING,
        AdvertisingPduType.ADV_IND,
        0,
    ),
    66: (
        Properties.USE_LEGACY_ADVERTISING_PDUS
        | Properties.CONNECTABLE_ADVERTISING
        | Properties.DIRECTED_ADVERTISING
        | Properties.HIGH_DUTY_CYCLE_DIRECTED_CONNECTABLE_ADVERTISING,
        AdvertisingPduType.ADV_DIRECT_IND,
        0x007D,
    ),
    67: (
        Properties.USE_LEGACY_ADVERTISING_PDUS
        | Properties.CONNECTABLE_ADVERTISING
        | Properties.DIRECTED_ADVERTISING,
        AdvertisingPduType.ADV_DIRECT_IND,
        0,
    ),
    68: (
        Properties.USE_LEGACY_ADVERTISING_PDUS | Properties.SCANNABLE_ADVERTISING,
        AdvertisingPduType.ADV_SCAN_IND,
        0,
    ),
}


async def _primary_intervals(tester, legacy_kind, *, high_duty=False):
    """Measure consecutive same-channel starts, without following auxiliary PDUs."""
    for channel in (37, 38, 39):
        starts = [[], []]
        deadline = tester.radio.now_us + 300000
        while min(map(len, starts)) < 4 and tester.radio.now_us < deadline:
            packet = await tester.expect_pdu(channel, duration_us=70000)
            kind = packet.pdu[0] & PDU_ADV_TYPE_MASK
            assert kind in (AdvertisingPduType.ADV_EXT_IND, legacy_kind)
            handle = int(kind != AdvertisingPduType.ADV_EXT_IND)
            if len(starts[handle]) < 4:
                starts[handle].append(packet.start_us)
        assert min(map(len, starts)) == 4, (channel, starts)
        for handle, times in enumerate(starts):
            for first, second in pairwise(times):
                interval = second - first
                if high_duty and handle == 1:
                    assert 0 < interval <= HIGH_DUTY_INTERVAL_MAX_US, interval
                else:
                    base = (20000, 50000)[handle]
                    minimum, maximum = advertising_interval_limits(base)
                    assert minimum <= interval <= maximum, (channel, handle, interval)
                tester.output.write(
                    f'INTERVAL channel={channel} set={handle} us={interval}\n'
                )


async def _primary_set_events(tester, legacy_kind, payloads):
    """Check complete primary events; allow other events before an auxiliary PDU."""
    directed = legacy_kind == AdvertisingPduType.ADV_DIRECT_IND
    properties = (0, Properties.USE_LEGACY_ADVERTISING_PDUS)
    maximum_gap = (
        PRIMARY_EVENT_PDU_GAP_US
        + (
            PRIMARY_EVENT_PDU_GAP_US * LL_SCA_PPM[LL_SCA_500_PPM]
            + PHY_TIME_UNITS_PER_SECOND
            - 1
        )
        // PHY_TIME_UNITS_PER_SECOND
        + ADVERTISING_INTERVAL_JITTER_US
    )
    seen = set()
    for _ in range(100):
        primaries = []
        for channel in LL_ADV_PRIMARY_CHANNELS:
            packet = await tester.expect_pdu(channel, duration_us=200000)
            assert packet.phy == Phy.LE_1M
            kind = packet.pdu[0] & PDU_ADV_TYPE_MASK
            assert kind in (AdvertisingPduType.ADV_EXT_IND, legacy_kind)
            if primaries:
                assert (
                    primaries[-1].end_us
                    <= packet.start_us
                    <= (primaries[-1].start_us + maximum_gap)
                ), 'Primary packets do not belong to one advertising event'
                assert kind == primaries[0].pdu[0] & PDU_ADV_TYPE_MASK, (
                    'Primary PDU type changed within an advertising event',
                    channel,
                )
            primaries.append(packet)
        if kind == AdvertisingPduType.ADV_EXT_IND:
            parents = [decode_extended(packet.pdu) for packet in primaries]
            for packet, fields in zip(primaries, parents):
                assert (
                    _extended_advertising_set(
                        packet, fields, properties, (Phy.LE_1M, Phy.LE_1M), payloads
                    )
                    == 0
                )
            assert all(('aux' in fields) == ('aux' in parents[0]) for fields in parents)
            packet, fields = primaries[-1], parents[-1]
            auxiliary = None
            if 'aux' in fields:
                auxiliary_channel, packet, fields = await follow_auxiliary(
                    tester, packet, fields
                )
                assert packet.phy == Phy.LE_1M
                assert (
                    packet.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.ADV_EXT_IND
                )
                assert (
                    packet.pdu[PDU_HEADER_SIZE] >> PDU_ADV_EXT_HEADER_LENGTH_BITS == 0
                )
                auxiliary = (packet, fields)
                for parent, parent_fields in zip(primaries, parents):
                    pointer = parent_fields['aux']
                    channel, offset = decode_aux(pointer)
                    unit = (
                        PDU_ADV_OFFSET_UNIT_300_US
                        if pointer[0] & PDU_ADV_AUX_OFFSET_UNITS_MASK
                        else PDU_ADV_OFFSET_UNIT_30_US
                    )
                    assert channel == auxiliary_channel and pointer[2] >> 5 == 0
                    assert (
                        parent.start_us + offset
                        <= packet.start_us
                        < (parent.start_us + offset + unit)
                    )
                    assert packet.start_us - parent.end_us >= LL_MAFS_US
                    assert fields['adi'] == parent_fields['adi']
            for parent, parent_fields in zip(primaries, parents):
                addresses = [(parent, parent_fields)]
                if auxiliary is not None:
                    addresses.append(auxiliary)
                _advertising_set_addresses(addresses, False)
            assert (
                await chain(tester, packet, fields, expected_phy=Phy.LE_1M)
                == payloads[0]
            )
            seen.add(0)
        else:
            for packet in primaries:
                assert not packet.pdu[0] & PDU_ADV_TXADD_MASK
                assert packet.pdu[
                    PDU_HEADER_SIZE : PDU_HEADER_SIZE + PDU_ADV_ADDRESS_SIZE
                ] == bytes(DUT_ADDRESS)
                assert packet.pdu[PDU_HEADER_SIZE + PDU_ADV_ADDRESS_SIZE :] == (
                    bytes(LOWER_CENTRAL_PUBLIC_ADDRESS) if directed else payloads[1]
                )
                if directed:
                    assert not packet.pdu[0] & PDU_ADV_RXADD_MASK
            seen.add(1)
        if len(seen) == 2:
            return
    raise AssertionError('Missing complete primary event for an advertising set')


async def multiple_legacy_and_extended_primary_advertising_sets(tester, *, number):
    """Extended Advertising, Multiple Sets, Legacy and Extended, Primary Advertising
    PDU.

    Initial Condition:
        Advertising parameters select supported intervals, event types, address
        type and channel map. TSPX_supported_advertising_channels declares used
        and unused channels. This implementation selects all three primary
        channels and public addresses; set 0 is extended non-connectable,
        non-scannable and undirected, while set 1 uses the case's legacy properties.
        Both sets use LE 1M and distinct SIDs; directed rows target the Lower Tester.

    Test Case Configuration:
        Table 4.2-36 set 1 properties/PDU/Duration:
        BV-64: 0x0010 / ADV_NONCONN_IND / 0;
        BV-65: 0x0013 / ADV_IND / 0;
        BV-66: 0x001D / high-duty ADV_DIRECT_IND / 0x007D (1.25 s);
        BV-67: 0x0015 / low-duty ADV_DIRECT_IND / 0;
        BV-68: 0x0012 / ADV_SCAN_IND / 0.
        Both sets use the row's Duration. All HCI commands require success.

    Test Steps:
        1. Configure set 0 with properties zero, all primary channels, both PHYs
           LE 1M and a 20-ms interval (0x20 in 625-us units).
        2. Configure set 1 with the Table 4.2-36 properties, all primary channels,
           both PHYs LE 1M, a 50-ms interval (0x50), and a different SID.
        3. For ADV_DIRECT_IND properties, omit Steps 4/5 and proceed to Step 6.
        4. Otherwise submit five seeded random data octets to set 0, identified
           by its advertising handle (see the opcode Source Correction).
        5. Submit five different seeded octets to set 1 using its handle.
        6. Enable both sets in one extended enable command with Num_Sets 2 and
           the table's Duration for each set. Send this command to the IUT.
        7. Observe interlaced events on supported primary channels. Require only
           the configured PDU types, set 0's 20-ms cadence and set 1's cadence,
           and exact set-specific data for the nondirected rows. The receiver
           samples consecutive starts using common drift/jitter tolerances.
           Receive all three primary PDUs of each sampled event and require
           one PDU type throughout that event; different sets may use different
           types. Other events may occur before an extended event's auxiliary PDU.
        8. When AuxPtr is present, follow it to LE 1M AUX_ADV_IND and verify
           pointer timing, T_MAFS, mode, ADI, addresses and complete data.
        9. For nondirected rows, repeat the data writes for a total of ten data
           generations, including the initial writes in Steps 4/5. Keep both sets
           enabled and use new, distinct five-octet payloads for each generation.
           Repeat observations 7/8 after each pair of writes. Directed rows retain
           Step 3's empty-data branch and skip the data-update loop.
        10. Disable both sets, using one extended enable command per set.
        11. Clear the advertising sets with LE Clear Advertising Sets.

    Expected Outcome:
        Pass verdict: Step 7 receives only the expected primary PDU types on the
        supported channels, with set 0 at 20 ms and set 1 at 50 ms except the
        mandatory high-duty exception below. Nondirected rows deliver the exact
        submitted data for each set in all ten generations. Each sampled primary
        event retains its configured PDU type across all three primary channels.
        Each primary AuxPtr identifies the same valid LE 1M auxiliary packet.
        Timing requirements use integer microseconds; no fractional-us boundary
        stimulus is required. Complete cleanup Steps 10/11.

    Source Correction:
        Steps 4/5 name legacy Set Advertising Data but supply a handle; use
        Set Extended Advertising Data, as also drawn in Figure 4.2-50. Step 6's
        command recipient is the IUT. Enable both sets with one Num_Sets 2 command.
        The test owner confirmed that Step 9 repeats data updates, with ten total
        generations including the initial writes; the diagram's repeat box does
        not replace these writes. Preserve Step 3 for directed rows: both sets
        carry empty data and omit the updates. Correct data are received by the
        Lower Tester, rather than the IUT named in the pass paragraph.
        BV-66 high-duty directed advertising uses the Core maximum 3.75-ms
        interval, rather than the generic 50-ms outcome, and retains the table's
        1.25-s Duration for both sets.

    Reference:
        LL.TS.p28.pdf, Section 4.2.3.15, LL/DDI/ADV/BV-64-C, LL/DDI/ADV/BV-65-C,
        LL/DDI/ADV/BV-66-C, LL/DDI/ADV/BV-67-C, LL/DDI/ADV/BV-68-C; PDF pages
        210-214, printed pages 4.2-117-4.2-121, Table 4.2-36 and Figures 4.2-50/51.
    """
    flags, _, duration = PRIMARY_ROUNDS[number]
    directed = bool(flags & Properties.DIRECTED_ADVERTISING)
    properties, phys = ((0, flags), (Phy.LE_1M, Phy.LE_1M))
    payloads = await _configure_advertising_sets(
        tester,
        properties,
        phys,
        (0, 0) if directed else (5, 5),
        intervals=(32, 80),
        skip_empty=True,
    )
    assert directed or payloads[0] != payloads[1]
    await tester.command(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(
            1, [0, 1], [duration, duration], [0, 0]
        )
    )
    await _primary_intervals(tester, PRIMARY_ROUNDS[number][1], high_duty=number == 66)
    generator = random.Random(105)
    for generation in range(1 if directed else PRIMARY_DATA_ROUNDS):
        if generation:
            previous = payloads
            payloads = [
                bytes(generator.randrange(1, 255) for _ in range(5)) for _ in range(2)
            ]
            assert payloads[0] != payloads[1]
            assert all(data != old for data, old in zip(payloads, previous))
            for handle, data in enumerate(payloads):
                await set_extended_data(tester, data, handle=handle)
        tester.output.write(f'PRIMARY DATA generation={generation + 1} {payloads!r}\n')
        await _primary_set_events(tester, PRIMARY_ROUNDS[number][1], payloads)
    for handle in range(2):
        await tester.command(
            hci.HCI_LE_Set_Extended_Advertising_Enable_Command(0, [handle], [0], [0])
        )
    await tester.command(hci.HCI_LE_Clear_Advertising_Sets_Command())


# Verify that each advertising event uses only its configured primary PDU type when
# legacy and extended sets are active together. Check interlacing, configured intervals
# and the set-specific data where the procedure loads it; directed legacy rows carry no
# advertising data.
PROCEDURES |= procedure_cases(
    multiple_legacy_and_extended_primary_advertising_sets,
    {
        'LL/DDI/ADV/BV-64-C': (
            'Non-Connectable and Non-Scannable Undirected',
            {'number': 64},
        ),
        'LL/DDI/ADV/BV-65-C': (
            'Connectable and Scannable Undirected',
            {'number': 65},
        ),
        'LL/DDI/ADV/BV-66-C': (
            'Connectable Directed – High Duty Cycle',
            {'number': 66},
        ),
        'LL/DDI/ADV/BV-67-C': (
            'Connectable Directed – Low Duty Cycle',
            {'number': 67},
        ),
        'LL/DDI/ADV/BV-68-C': (
            'Scannable Undirected',
            {'number': 68},
        ),
    },
)


async def _decision_advertisement(tester, *, phy, mode, with_address, with_adi, data):
    primary = await tester.expect_pdu(
        kind=AdvertisingPduType.ADV_DECISION_IND, phy=phy, duration_us=100_000
    )
    fields = decode_extended(primary.pdu)
    assert primary.pdu[2] >> 6 == mode
    assert fields['data'] == data and 'aux' in fields
    assert ('address' in fields) == with_address
    assert ('adi' in fields) == with_adi
    assert not {'target', 'sync', 'cte', 'acad'} & fields.keys()
    if with_address:
        assert fields['address'] == bytes(DUT_ADDRESS)
    channel, auxiliary, auxiliary_fields = await follow_auxiliary(
        tester, primary, fields
    )
    assert auxiliary.phy == phy and auxiliary.pdu[2] >> 6 == mode
    if with_adi:
        assert fields['adi'] == auxiliary_fields['adi']
    return channel, auxiliary, auxiliary_fields


async def decision_advertising_data_updates(
    tester, *, phy, mode, with_address=False, with_adi=False
):
    """Decision-Based Advertisements, Set Before and After Extended Advertising Enabled.

    Initial Condition:
        Standby; select one primary channel and a supported public own address.

    Test Case Configuration:
        LL/DDI/ADV/BV-92-C: LE 1M, connectable, AdvA absent, ADI absent.
        LL/DDI/ADV/BV-93-C: LE 1M, non-connectable/non-scannable, AdvA absent,
            ADI absent.
        LL/DDI/ADV/BV-94-C: LE 1M, scannable, AdvA absent, ADI absent.
        LL/DDI/ADV/BV-95-C: LE Coded, connectable, AdvA absent, ADI absent.
        LL/DDI/ADV/BV-96-C: LE Coded, non-connectable/non-scannable, AdvA
            absent, ADI absent.
        LL/DDI/ADV/BV-97-C: LE Coded, scannable, AdvA absent, ADI absent.
        LL/DDI/ADV/BV-98-C: LE 1M, connectable, AdvA present, ADI absent.
        LL/DDI/ADV/BV-99-C: LE 1M, non-connectable/non-scannable, AdvA present,
            ADI absent.
        LL/DDI/ADV/BV-100-C: LE 1M, scannable, AdvA present, ADI absent.
        LL/DDI/ADV/BV-101-C: LE 1M, connectable, AdvA absent, ADI present.
        LL/DDI/ADV/BV-102-C: LE 1M, non-connectable/non-scannable, AdvA absent,
            ADI present.
        LL/DDI/ADV/BV-103-C: LE 1M, scannable, AdvA absent, ADI present.

    Test Steps:
        Repeat steps 1-8 for all five Table 4.2-47 rounds:
        1. Set extended parameters with the PHY/properties in Table 4.2-46.
        2. Set Decision Type Flags/Data (tag with six/seven octets, arbitrary
           five/eight octets, and no data).
        3-4. Enable advertising and check ADV_DECISION_IND plus its AUX_ADV_IND.
        5-6. Update decision data while enabled to one arbitrary octet and check
             the very next decision PDU and its auxiliary event.
        7-8. Disable advertising and observe silence on the selected channel.

    Expected Outcome:
        Every primary PDU has the required AdvMode, AuxPtr, optional AdvA/ADI,
        flags and data. Live updates appear in the first subsequent primary PDU.

    Source Correction:
        Step 8 names AUX_DECISION_IND, which Core does not define. Observe the
        cessation of ADV_DECISION_IND, the PDU named in steps 4 and 6 and Core
        Vol 6, Part B, 2.3.1.11. This is a local name correction, not SIG errata.

    Reference:
        LL.TS.p28, Section 4.2.3.19, PDF pages 259-261 (printed 4.2-166-168),
        Tables 4.2-46/47, Figure 4.2-72; Core Vol 4, Part E, 7.8.144.
    """
    generator = random.Random(92)
    properties = (
        0x80 | mode | (0x100 if with_address else 0) | (0x200 if with_adi else 0)
    )
    for flags, size in [(1, 6), (1, 7), (0, 5), (0, 8), (0, 0)]:
        await tester.command(
            extended_parameters(
                advertising_event_properties=properties,
                primary_advertising_phy=phy,
                secondary_advertising_phy=phy,
            )
        )
        if mode == AdvertisingMode.SCANNABLE:
            await set_extended_data(tester, b'\x01', response=True)
        data = generator.randbytes(size)
        await tester.command(hci.HCI_LE_Set_Decision_Data_Command(0, flags, data))
        await tester.command(extended_enable())
        await _decision_advertisement(
            tester,
            phy=phy,
            mode=mode,
            with_address=with_address,
            with_adi=with_adi,
            data=bytes([flags]) + data,
        )
        updated = generator.randbytes(1)
        await tester.command(hci.HCI_LE_Set_Decision_Data_Command(0, 0, updated))
        await _decision_advertisement(
            tester,
            phy=phy,
            mode=mode,
            with_address=with_address,
            with_adi=with_adi,
            data=b'\0' + updated,
        )
        await tester.command(extended_enable(0))
        assert await tester.receive(37, phy=phy, duration_us=40_000) is None


# Verify decision-PDU contents when decision data is set before and after extended
# advertising is enabled, across the selected flags, address and PHY variants.
PROCEDURES |= procedure_cases(
    decision_advertising_data_updates,
    {
        f'LL/DDI/ADV/BV-{number}-C': (
            'Decision-Based Advertisements, Set Before and After '
            'Extended Advertising Enabled',
            {'phy': phy, 'mode': mode, 'with_address': address, 'with_adi': adi},
        )
        for number, phy, mode, address, adi in [
            (92, Phy.LE_1M, 1, False, False),
            (93, Phy.LE_1M, 0, False, False),
            (94, Phy.LE_1M, 2, False, False),
            (95, Phy.LE_CODED, 1, False, False),
            (96, Phy.LE_CODED, 0, False, False),
            (97, Phy.LE_CODED, 2, False, False),
            (98, Phy.LE_1M, 1, True, False),
            (99, Phy.LE_1M, 0, True, False),
            (100, Phy.LE_1M, 2, True, False),
            (101, Phy.LE_1M, 1, False, True),
            (102, Phy.LE_1M, 0, False, True),
            (103, Phy.LE_1M, 2, False, True),
        ]
    },
)


async def decision_scannable_advertising(tester):
    """Decision-Based Advertisements, Scannable.

    Initial Condition:
        The IUT supports scannable extended advertising and uses one primary channel.

    Test Steps:
        1. Configure properties 0x0082 and one primary channel.
        2. Set complete scan response data to the single octet 0x01.
        3. Set decision data to eight random octets.
        4-6. Enable, receive ADV_DECISION_IND with AdvMode 2 and AuxPtr, and
             follow that pointer to AUX_ADV_IND on the secondary channel.
        7. Send AUX_SCAN_REQ T_IFS after the auxiliary advertisement.
        8. Receive AUX_SCAN_RSP T_IFS later containing the configured response.

    Expected Outcome:
        The decision PDU carries all eight decision octets, and the scan response
        carries 0x01 with the expected advertiser address and timing.

    Reference:
        LL.TS.p28, DDI/ADV/BV-104-C, PDF pages 261-263, Figure 4.2-73;
        Core Vol 6, Part B, 2.3.1.11 and 4.4.2.5.
    """
    data = random.Random(104).randbytes(8)
    await tester.command(extended_parameters(advertising_event_properties=0x82))
    await set_extended_data(tester, b'\x01', response=True)
    await tester.command(hci.HCI_LE_Set_Decision_Data_Command(0, 0, data))
    await tester.command(extended_enable())
    channel, auxiliary, fields = await _decision_advertisement(
        tester,
        phy=Phy.LE_1M,
        mode=2,
        with_address=False,
        with_adi=False,
        data=b'\0' + data,
    )
    address = hci.Address(fields['address'], 0)
    end = await tester.transmit(
        channel,
        request_pdu(AdvertisingPduType.SCAN_REQ, advertiser=address),
        start_us=auxiliary.end_us + LL_IFS_US,
    )
    response = await tester.expect_pdu(channel, duration_us=1000)
    assert abs(response.start_us - end - LL_IFS_US) <= 2
    assert decode_extended(response.pdu)['data'] == b'\x01'
    assert decode_extended(response.pdu)['address'] == fields['address']


# Verify that scannable decision advertising leads to a scan-request exchange and the
# expected scan response on the secondary channel.
PROCEDURES |= procedure_cases(
    decision_scannable_advertising,
    {
        'LL/DDI/ADV/BV-104-C': ('Decision-Based Advertisements, Scannable', {}),
    },
)


async def decision_advertising_default_data(tester):
    """Decision-Based Advertisements With No Data, Enable Extended Advertisement.

    Initial Condition:
        No decision data has been provided for a new advertising set.

    Test Steps:
        1. Set extended advertising parameters with bit 7 and a supported event type.
        2. Enable extended advertising and receive successful Command Complete.
        3. Receive an ADV_DECISION_IND with no Decision Data.

    Expected Outcome:
        The decision payload contains only the default zero Decision Type Flags.

    Reference:
        LL.TS.p28, DDI/ADV/BV-105-C, PDF pages 263-264, Figure 4.2-74;
        Core Vol 6, Part B, 2.3.1.11; Vol 4, Part E, 7.8.144.
    """
    await tester.command(extended_parameters(advertising_event_properties=0x80))
    await tester.command(extended_enable())
    await _decision_advertisement(
        tester,
        phy=Phy.LE_1M,
        mode=0,
        with_address=False,
        with_adi=False,
        data=b'\0',
    )


# Verify that enabling extended decision advertising without setting decision data
# transmits decision PDUs with zero flags and an empty decision payload.
PROCEDURES |= procedure_cases(
    decision_advertising_default_data,
    {
        'LL/DDI/ADV/BV-105-C': (
            'Decision-Based Advertisements With No Data, Enable Extended Advertisement',
            {},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_device_discovery(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
