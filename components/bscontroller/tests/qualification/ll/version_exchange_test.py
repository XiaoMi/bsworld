# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.3: Version exchange."""

import struct

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_CHANNEL_MAP_SIZE,
    LL_CONN_INTERVAL_UNIT_US,
    LL_EVENT_COUNTER_MASK,
    LL_IFS_US,
    LL_RESPONSE_TIMEOUT_US,
    LL_SUPERVISION_TIMEOUT_MAX,
    PDU_DATA_MD_MASK,
    PDU_DATA_NESN_MASK,
    PDU_DATA_SN_MASK,
    PDU_HEADER_SIZE,
    LlId,
)
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.connection import (
    PEER_VERSION,
    Opcode,
    _advertiser_connection_parameters,
    _check_completion,
    _completion,
    _quiet_events,
    _version_link,
    _version_payload,
)
from tests.qualification.link import _pending_connection

PROCEDURES = ProcedureMap()

VERSION_COLLISION_DELAY_LIMIT = 10

# Long-interval collision setup: 3 s, strictly greater than the specified 2 s.
VERSION_COLLISION_INTERVAL = 2400
VERSION_COLLISION_REPEATS_PER_DELAY = 5

# LL.TS.p28 4.3.7.41..44 and PER/BV-146: prescribed event counts and delay search.
VERSION_DUPLICATE_DELAY_EVENTS = 50
VERSION_RESPONSE_DELAY_EVENTS = 10


async def _exchange(tester, *, central=False, mode='requesting', duplicates=True):
    """Perform Version Exchange with the selected duplicate/delay stimuli."""
    local = await tester.command(hci.HCI_Read_Local_Version_Information_Command())
    expected = bytes([Opcode.LL_VERSION_IND]) + struct.pack(
        '<BHH', local.lmp_version, local.company_identifier, local.lmp_subversion
    )
    parameters = _advertiser_connection_parameters()
    if central:
        await tester.command(
            hci.HCI_LE_Set_Host_Channel_Classification_Command(
                parameters.channel_map.to_bytes(LL_CHANNEL_MAP_SIZE, 'little')
            )
        )
    link, _ = await _pending_connection(tester, central=central, parameters=parameters)
    autonomous = False

    def initial_reply(packet):
        nonlocal autonomous
        version = _version_payload(packet)
        if version is None:
            return b''
        assert version == expected
        if mode == 'responding':
            pytest.skip('Inconclusive: IUT sent Version Indication before the tester')
        autonomous = True
        if mode == 'requesting' and duplicates:
            initial.append(VERSION_IND)
        return b'' if mode == 'delayed' else VERSION_IND

    # Observe autonomous initiation before deciding whether to issue a Host command.
    initial = [initial_reply] if central else [b'', initial_reply]
    packets = await _same_event(
        link, initial, missing_version_inconclusive=mode == 'collision'
    )
    assert sum(_version_payload(packet) is not None for packet in packets) == int(
        autonomous
    )
    requesting = mode != 'responding' and not autonomous
    if requesting:
        await tester.command(
            hci.HCI_Read_Remote_Version_Information_Command(link.handle)
        )
    if not duplicates:
        assert mode in ('requesting', 'responding')
        if mode == 'responding':
            await _responding_version(tester, link, expected, duplicate=False)
        elif not autonomous:
            await _requested_version(tester, link, expected)
        await _quiet_events(link, 1)
        event = _completion(tester)
        if requesting:
            _check_completion(event, link.handle)
        else:
            assert event is None, 'unsolicited Host version completion'
        await link.disconnect()
        return
    if mode == 'delayed':
        if not autonomous:
            packets = await _same_event(link, [b''] if central else [b'', b''])
        assert [_version_payload(packet) for packet in packets].count(expected) == 1
        assert _completion(tester) is None
        due = (
            _last_transmission_start(link, 0)
            + VERSION_RESPONSE_DELAY_EVENTS
            * link.parameters.interval
            * LL_CONN_INTERVAL_UNIT_US
        )
        await _quiet_events(link, VERSION_RESPONSE_DELAY_EVENTS, no_completion=True)
        packet = await link.exchange(VERSION_IND, llid=LlId.CONTROL, check_ack=True)
        sent = _last_transmission_start(link, len(VERSION_IND))
        assert due <= sent < due + link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
        assert _version_payload(packet) is None
    elif mode == 'responding':
        await _responding_version(tester, link, expected)
    elif not autonomous:
        payloads = [VERSION_IND]
        if mode == 'requesting':
            payloads.append(VERSION_IND)
        if not central and requesting:
            payloads.insert(0, b'')
        packets = await _same_event(
            link, payloads, missing_version_inconclusive=mode == 'collision'
        )
        versions = [_version_payload(packet) for packet in packets]
        if not requesting and central and (versions[0] is not None):
            pytest.skip('Inconclusive: IUT sent Version Indication before the tester')
        assert versions.count(expected) == 1
        assert all(payload in (None, expected) for payload in versions)
    if mode != 'delayed':
        due = (
            _last_transmission_start(link, len(VERSION_IND))
            + VERSION_DUPLICATE_DELAY_EVENTS
            * link.parameters.interval
            * LL_CONN_INTERVAL_UNIT_US
        )
    await _quiet_events(link, 1)
    event = _completion(tester)
    if requesting:
        _check_completion(event, link.handle)
    else:
        assert event is None
    if mode in ('requesting', 'responding'):
        while link.anchor < due:
            await _quiet_events(link, 1, no_completion=True)
        packet = await link.exchange(VERSION_IND, llid=LlId.CONTROL, check_ack=True)
        sent = _last_transmission_start(link, len(VERSION_IND))
        assert due <= sent < due + link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
        assert _version_payload(packet) is None
    await _quiet_events(link, VERSION_RESPONSE_DELAY_EVENTS, no_completion=True)
    await link.disconnect()


async def _requested_version(tester, link, expected):
    """Receive a possibly delayed request and acknowledge it with the peer version."""
    responded = False

    def reply(packet):
        nonlocal responded
        version = _version_payload(packet)
        if version is None:
            return b''
        assert version == expected
        assert not responded, 'duplicate Version Indication'
        responded = True
        if link.central:
            payloads.append(b'')
        return VERSION_IND

    deadline = tester.radio.now_us + LL_RESPONSE_TIMEOUT_US
    while tester.radio.now_us < deadline:
        payloads = [reply] if link.central else [b'', reply]
        packets = await _same_event(
            link,
            payloads,
            followup=reply,
            keep_open=lambda: not link.central and not responded,
        )
        assert sum(_version_payload(p) is not None for p in packets) == int(responded)
        if responded:
            return
    pytest.skip('Observation limit: no requested Version Indication')


async def _responding_version(tester, link, expected, *, duplicate=True):
    """Acknowledge the response, optionally sending a same-event duplicate."""
    responded = False
    sent = False

    def reply(packet):
        nonlocal responded, sent
        version = _version_payload(packet)
        if not sent:
            if link.central and version is not None:
                pytest.skip(
                    'Inconclusive: IUT sent Version Indication before the tester'
                )
            sent = True
            return VERSION_IND
        if version is None:
            return b''
        assert version == expected, 'incorrect local version information'
        assert not responded, 'duplicate local Version Indication'
        responded = True
        if not duplicate:
            return b''
        # Receive the acknowledgement of the duplicate before leaving this event.
        payloads.append(b'')
        return VERSION_IND

    # The source imposes no response deadline. This bound limits the experiment;
    # exhausting it is not evidence of a normative response-time failure.
    deadline = tester.radio.now_us + LL_RESPONSE_TIMEOUT_US
    while tester.radio.now_us < deadline:
        payloads = [reply, reply] if not sent else [reply]
        packets = await _same_event(
            link,
            payloads,
            followup=reply,
            keep_open=lambda: not link.central and not responded,
        )
        versions = [_version_payload(packet) for packet in packets]
        assert all(version in (None, expected) for version in versions)
        assert versions.count(expected) == int(responded), (
            'duplicate Version Indication'
        )
        assert _completion(tester) is None, 'unsolicited Host version completion'
        if responded:
            return
    pytest.skip('Observation limit: no Version Indication within the tester budget')


def _last_transmission_start(link, payload_size):
    return link.last_sent_end_us - link.tx_phy.airtime_us(
        PDU_HEADER_SIZE + payload_size
    )


async def _same_event(
    link,
    payloads,
    *,
    followup=None,
    missing_version_inconclusive=False,
    keep_open=None,
):
    """Exchange an MD-linked sequence; payload callbacks inspect the preceding RX."""
    tester, channel = (link.tester, link.channel)
    packets = []
    anchor = start = link.anchor

    async def receive(expected_start):
        packet = await tester.receive(
            channel,
            start_us=max(tester.radio.now_us + 1, expected_start - 1),
            duration_us=max(1000, expected_start - tester.radio.now_us + 1000),
            **link.radio_parameters | {'phy': link.rx_phy},
        )
        if packet is None:
            if missing_version_inconclusive and not link.central and sent_version:
                pytest.skip('Inconclusive: Peripheral did not listen to Step 4 packet')
            raise AssertionError('missing DATA PDU in same-event exchange')
        assert packet.valid_crc
        assert len(packet.pdu) >= PDU_HEADER_SIZE
        assert packet.pdu[1] == len(packet.pdu) - PDU_HEADER_SIZE
        assert packet.start_us == expected_start
        assert int(bool(packet.pdu[0] & PDU_DATA_SN_MASK)) == link.nesn
        link._accept(packet)
        link.last_received = packet
        packets.append(packet)
        return packet

    sent_sn = link.sn
    sent_version = False
    for index, payload in enumerate(payloads):
        if link.central:
            packet = await receive(start)
            if index:
                assert int(bool(packet.pdu[0] & PDU_DATA_NESN_MASK)) != sent_sn
            start = packet.end_us + LL_IFS_US
        if callable(payload):
            payload = payload(link.last_received)
        header = (
            (LlId.CONTROL if payload else LlId.CONTINUATION)
            | link.sn * PDU_DATA_SN_MASK
            | link.nesn * PDU_DATA_NESN_MASK
            | (
                PDU_DATA_MD_MASK
                if index + 1 < len(payloads) or (keep_open is not None and keep_open())
                else 0
            )
        )
        sent_sn = link.sn
        sent_version = payload == VERSION_IND
        await tester.transmit(
            channel,
            bytes([header, len(payload)]) + payload,
            start_us=start,
            **link.radio_parameters,
        )
        link.last_sent_end_us = tester.radio.now_us + 1
        start = link.last_sent_end_us + LL_IFS_US
        if not link.central:
            packet = await receive(start)
            assert int(bool(packet.pdu[0] & PDU_DATA_NESN_MASK)) != sent_sn
            start = packet.end_us + LL_IFS_US
            if (
                followup is not None
                and index + 1 == len(payloads)
                and _version_payload(packet) is not None
            ):
                payloads.append(followup)
        assert (
            tester.radio.now_us
            < anchor + link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
        )
    link.anchor += link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
    link.counter = link.counter + 1 & LL_EVENT_COUNTER_MASK
    return packets


VERSION_IND = bytes([Opcode.LL_VERSION_IND]) + struct.pack('<BHH', *PEER_VERSION)


async def version_exchange_iut_requesting_collision_peripheral(tester):
    """Version Exchange, IUT Requesting, Collision, Peripheral.

    Initial Condition:
        An unencrypted connected Peripheral with public addresses, common
        channel parameters and latency, selected SCA and an interval >2 s.
        Select a 3-s interval, zero latency and 32-s supervision timeout. Read
        local version/manufacturer/subversion to validate the IUT wire payload.

    Test Steps:
        1. Establish the connection. If the IUT autonomously sends its version
           in three consecutive repeats, end with Inconclusive.
        2. Send HCI Read Remote Version Information as close as possible
           after a connection event; require successful Command Status.
        3. Wait DELAY connection events, then send the Lower Tester's
           LL_VERSION_IND containing its selected version information.
        4. Require the IUT's own LL_VERSION_IND as its response in the same
           event, also acknowledging the Lower Tester's indication.
        5. Acknowledge the IUT version indication.
        6. Require Read Remote Version Information Complete with the saved
           handle and the Lower Tester version fields from Step 3.
        7. Observe at least ten consecutive further connection events without
           another IUT LL_VERSION_IND.

        Repeat complete trials, disconnecting and reconnecting with the Initial
        Condition until Steps 3/4 occur in the same event. Start DELAY=0 and
        increment it after every fifth trial. DELAY=10 is Inconclusive.

    Expected Outcome:
        Pass verdict:
            The same-event response in Step 4 is the IUT version, correctly
            acknowledges the peer, and produces the exact Host result in Step 6.
        Fail verdict:
            More than one LL_VERSION_IND in a trial, or incorrect version data.
        Inconclusive verdict:
            Three consecutive autonomous initializations, or DELAY reaches ten.

    Source Correction:
        The abbreviated outcome omits Step 1's three-consecutive qualifier.
        Follow that explicit executable condition, not immediate Inconclusive
        after one autonomous trial. Amber corroborates this local interpretation;
        no separate adopted SIG erratum is claimed.

    Coverage Note:
        Count indications during DELAY and the collision exchange, and validate
        their exact local version fields. A missing same-event response retries
        the trial; an incorrect or duplicate version is a failure, not a retry
        or Inconclusive. Observe acknowledgements and prohibit duplicates during
        the ten-event post-collision window; reject extra Host completions.
        Disconnect the successful trial after all checks.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-146-C procedure in
        Section 4.3.5, Figure 4.3-122; PDF 728-729,
        printed 4.3-255 through 4.3-256. Steps 1-7, repeat preamble and outcome;
        referenced Core Vol 6, Part B, Section 5.1.5.
    """
    local = await tester.command(hci.HCI_Read_Local_Version_Information_Command())
    expected = bytes([Opcode.LL_VERSION_IND]) + struct.pack(
        '<BHH', local.lmp_version, local.company_identifier, local.lmp_subversion
    )
    autonomous = 0
    for attempt in range(
        VERSION_COLLISION_DELAY_LIMIT * VERSION_COLLISION_REPEATS_PER_DELAY
    ):
        delay = attempt // VERSION_COLLISION_REPEATS_PER_DELAY
        link = await _version_link(
            tester,
            interval=VERSION_COLLISION_INTERVAL,
            timeout=LL_SUPERVISION_TIMEOUT_MAX,
        )
        initial = _version_payload(link.last_received)
        if initial is not None:
            assert initial == expected
            autonomous += 1
            await link.disconnect()
            if autonomous == 3:
                pytest.skip(
                    'Inconclusive: three autonomous Version Exchange initializations'
                )
            continue
        autonomous = 0
        before = tester.radio.now_us
        await tester.command(
            hci.HCI_Read_Remote_Version_Information_Command(link.handle)
        )
        assert (
            tester.radio.now_us - before
            < VERSION_COLLISION_INTERVAL * LL_CONN_INTERVAL_UNIT_US
        )
        versions = []
        for _ in range(delay):
            value = _version_payload(await link.exchange())
            if value is not None:
                versions.append(value)
        packets = await _same_event(link, [VERSION_IND])
        assert len(packets) == 1
        value = _version_payload(packets[0])
        if value is not None:
            versions.append(value)
        assert len(versions) <= 1, 'duplicate VERSION_IND in one collision trial'
        assert all(version == expected for version in versions), 'wrong IUT version'
        if value is None:
            await link.disconnect()
            _completion(tester)
            continue
        await _quiet_events(link, VERSION_RESPONSE_DELAY_EVENTS)
        _check_completion(_completion(tester), link.handle)
        assert _completion(tester) is None
        await link.disconnect()
        return
    pytest.skip(
        'Inconclusive: no same-event version collision before DELAY reached ten'
    )


# Verify that a connected Peripheral handles simultaneous Version Exchange initiation,
# returns its version in the same connection event as the peer indication, and reports
# the peer's version to its Host.
PROCEDURES |= procedure_cases(
    version_exchange_iut_requesting_collision_peripheral,
    {
        'LL/CON/PER/BV-146-C': (
            'Version Exchange, IUT Requesting, Collision, Peripheral',
            {},
        ),
    },
)


async def peripheral_request_version(tester):
    """Peripheral Request Version.

    Initial Condition:
        Connected Peripheral, public address, arbitrary advertising interval,
        advertising channel map and SCA. Use the common connection interval,
        Peripheral latency, timeout and channel map.

    Test Steps:
        1. If the IUT initiates Version Exchange autonomously after connection,
           acknowledge its LL_VERSION_IND and send the Lower Tester's version.
        2. Otherwise send HCI_Read_Remote_Version_Information and require a
           successful Command Status event.
        3. Receive LL_VERSION_IND containing the IUT's version information.
        4. Acknowledge that indication and send the Lower Tester's version.
        5. For Host initiation, require Remote Version Information Complete
           containing the Lower Tester's version information.

    Expected Outcome:
        Pass verdict:
            The IUT initiates Version Exchange, reports its own version and
            acknowledges the peer's reply. When initiated by the Upper Tester,
            it reports the requested peer version in a successful HCI event.

    Coverage Note:
        Check the local version against Read Local Version Information and
        validate ACK sequence bits on the wire. The initial autonomous branch
        does not require an unsolicited Host completion. A finite observation
        budget bounds waiting for the indication; its exhaustion is diagnostic,
        not a normative response-time verdict.

    Reference:
        LL.TS.p28.pdf, LL/CON/PER/BV-19-C, Figure 4.3-61, Steps 1-5;
        PDF 613-614, printed 4.3-140..141. Source reference [3] 5.1.5.
    """
    await _exchange(tester, central=False, duplicates=False)


# Verify that a connected Peripheral initiates and completes Version Exchange with the
# Lower Tester acting as Central.
PROCEDURES |= procedure_cases(
    peripheral_request_version,
    {
        'LL/CON/PER/BV-19-C': (
            'Peripheral Request Version',
            {},
        ),
    },
)


async def peripheral_respond_version(tester):
    """Peripheral Respond Version.

    Initial Condition:
        Connected Peripheral, public address, arbitrary advertising interval,
        advertising channel map and SCA. Use the common connection interval,
        Peripheral latency, timeout and channel map.

    Test Steps:
        1. Configure the Lower Tester to send LL_VERSION_IND.
        2. Send LL_VERSION_IND containing the Lower Tester's version.
        3. Require the IUT to acknowledge the indication and return its own
           LL_VERSION_IND containing the IUT's version information.

    Expected Outcome:
        Pass verdict:
            The IUT correctly responds to the peer's Version Exchange.
        Inconclusive verdict:
            The IUT sends LL_VERSION_IND before the Lower Tester does.

    Coverage Note:
        Check the local version against Read Local Version Information and
        validate ACK sequence bits. No Host version request or duplicate peer
        indication is injected. A finite observation budget bounds waiting for
        the response; exhaustion is diagnostic, not a timing failure.

    Reference:
        LL.TS.p28.pdf, LL/CON/PER/BV-20-C, Figure 4.3-62, Steps 1-3;
        PDF 614-615, printed 4.3-141..142. Source reference [3] 5.1.5.
    """
    await _exchange(tester, central=False, mode='responding', duplicates=False)


# Verify that a connected Peripheral responds to Version Exchange initiated by the Lower
# Tester acting as Central.
PROCEDURES |= procedure_cases(
    peripheral_respond_version,
    {
        'LL/CON/PER/BV-20-C': (
            'Peripheral Respond Version',
            {},
        ),
    },
)


async def central_request_version(tester):
    """Central Request Version.

    Initial Condition:
        Connected Central, arbitrary scan interval and window, the Lower
        Tester's public peer address and a supported own address type.
        Use the common Peripheral latency and timeout with a legal connection
        interval.

    Test Steps:
        1. If the IUT initiates Version Exchange autonomously after connection,
           acknowledge its LL_VERSION_IND and send the Lower Tester's version.
        2. Otherwise send HCI_Read_Remote_Version_Information and require a
           successful Command Status event.
        3. Receive LL_VERSION_IND containing the IUT's version information.
        4. Acknowledge that indication and send the Lower Tester's version.
        5. For Host initiation, require Remote Version Information Complete
           containing the Lower Tester's version information.

    Expected Outcome:
        Pass verdict:
            The IUT initiates Version Exchange, reports its own version and
            acknowledges the peer's reply. When initiated by the Upper Tester,
            it reports the requested peer version in a successful HCI event.

    Coverage Note:
        Check the local version against Read Local Version Information and
        validate ACK sequence bits on the wire. The initial autonomous branch
        does not require an unsolicited Host completion. A finite observation
        budget bounds waiting for the indication; exhaustion is diagnostic,
        not a normative response-time verdict.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-20-C, Figure 4.3-149, Steps 1-5;
        PDF 771-772, printed 4.3-298..299. Source reference [3] 5.1.5.
    """
    await _exchange(tester, central=True, duplicates=False)


# Verify that a connected Central initiates and completes Version Exchange with the
# Lower Tester acting as Peripheral.
PROCEDURES |= procedure_cases(
    central_request_version,
    {
        'LL/CON/CEN/BV-20-C': (
            'Central Request Version',
            {},
        ),
    },
)


async def central_respond_version(tester):
    """Central Respond Version.

    Initial Condition:
        Connected Central, arbitrary scan interval and window, the Lower
        Tester's public peer address and a supported own address type.
        Use the common Peripheral latency and timeout with a legal connection
        interval.

    Test Steps:
        1. Configure the Lower Tester to send LL_VERSION_IND.
        2. Send LL_VERSION_IND containing the Lower Tester's version.
        3. Require the IUT to acknowledge the indication and return its own
           LL_VERSION_IND containing the IUT's version information.

    Expected Outcome:
        Pass verdict:
            The IUT correctly responds to the peer's Version Exchange.
        Inconclusive verdict:
            The IUT sends LL_VERSION_IND before the Lower Tester does.

    Coverage Note:
        Check the local version against Read Local Version Information and
        validate ACK sequence bits. No Host version request or duplicate peer
        indication is injected. A finite observation budget bounds waiting for
        the response; exhaustion is diagnostic, not a timing failure.

    Reference:
        LL.TS.p28.pdf, LL/CON/CEN/BV-21-C, Figure 4.3-150, Steps 1-3;
        PDF 772-773, printed 4.3-299..300. Source reference [3] 5.1.5.
    """
    await _exchange(tester, central=True, mode='responding', duplicates=False)


# Verify that a connected Central responds to Version Exchange initiated by the Lower
# Tester acting as Peripheral.
PROCEDURES |= procedure_cases(
    central_respond_version,
    {
        'LL/CON/CEN/BV-21-C': (
            'Central Respond Version',
            {},
        ),
    },
)


async def version_exchange_iut_requesting(tester, *, central):
    """Version Exchange, IUT Requesting.

    Initial Condition:
        Connected in the selected role using public addresses and common
        connection interval, Peripheral latency, timeout and channel map.
        Any advertising interval, advertising channel map and SCA are allowed.
        The selected common interval is 10 ms, latency zero and timeout 300 ms.

    Test Case Configuration:
        LL/CON/CEN/BI-11-C: Central.
        LL/CON/PER/BI-14-C: Peripheral.

    Test Steps:
        1. If the IUT autonomously sends LL_VERSION_IND after connection,
           containing its local version information, proceed to Step 4.
        2. Otherwise send HCI_Read_Remote_Version_Information and require
           successful HCI_Command_Status.
        3. Receive LL_VERSION_IND containing the IUT's version information.
        4. Send the Lower Tester's LL_VERSION_IND in the packet that
           acknowledges the IUT's indication.
        5. If Step 2 was performed, require Remote Version Information Complete
           containing the Lower Tester's version information.
        6. Within the same connection event, T_IFS after the preceding IUT PDU,
           send another LL_VERSION_IND. Require the IUT to ignore its content.
        7. After 50 connection intervals, send another LL_VERSION_IND.
           Require the IUT to ignore its content.

    Expected Outcome:
        Pass verdict:
            The IUT initiates Version Exchange and acknowledges the reply,
            without responding to either duplicate indication. If its Host
            initiated the procedure, exactly one successful completion reports
            the Lower Tester's version information.

    Coverage Note:
        Observe the first DATA before issuing the optional Host command.
        Read Local Version Information supplies the independent expected LMP
        version, manufacturer and LMP subversion. Use MD to keep the same-event
        sequence open, maintain ARQ and verify no extra indication or Host
        completion. The 50-interval delay begins at the same-event duplicate's
        RF timestamp; use the first legal event after that delay. Disconnect
        through HCI after ten additional quiet connection events.

    Reference:
        LL.TS.p28.pdf, Section 4.3.7.41; Table 4.3-123, Figure 4.3-253,
        Steps 1-7 and pass verdict; PDF 999-1001, printed 4.3-526..528.
        Source reference [3] 5.1.5.
    """
    await _exchange(tester, mode='requesting', central=central)


# Verify that an initiating IUT completes Version Exchange and ignores further Version
# Indications from the same peer in the same connection.
PROCEDURES |= procedure_cases(
    version_exchange_iut_requesting,
    {
        'LL/CON/CEN/BI-11-C': (
            'Version Exchange, IUT Requesting',
            {'central': True},
        ),
        'LL/CON/PER/BI-14-C': (
            'Version Exchange, IUT Requesting',
            {'central': False},
        ),
    },
)


async def version_exchange_iut_responding(tester, *, central):
    """Version Exchange, IUT Responding.

    Initial Condition:
        Connected in the selected role using public addresses and common
        connection interval, Peripheral latency, timeout and channel map.
        Any advertising interval, advertising channel map and SCA are allowed.
        The selected common interval is 10 ms, latency zero and timeout 300 ms.

    Test Case Configuration:
        LL/CON/CEN/BI-12-C: Central.
        LL/CON/PER/BI-15-C: Peripheral.

    Test Steps:
        1. Send LL_VERSION_IND containing the Lower Tester's version
           information. Require the IUT to acknowledge the PDU.
        2. Receive LL_VERSION_IND containing the IUT's version information;
           the source permits this response at any later time.
        3. Within that same connection event, T_IFS after the IUT PDU, send
           another LL_VERSION_IND in its acknowledgement. Require the IUT
           to process the acknowledgement but ignore the indication content.
        4. After 50 connection intervals, send another LL_VERSION_IND and
           require the IUT to ignore its content.

    Expected Outcome:
        Pass verdict:
            The IUT correctly responds to the first indication, but sends no
            further Version Indication in response to Steps 3 or 4.
        Inconclusive verdict:
            The IUT sends LL_VERSION_IND before the Lower Tester does.

    Coverage Note:
        No Host version command is issued, so no unsolicited Host completion
        is permitted. Read Local Version Information supplies the expected
        local LMP tuple. Observe autonomous initiation during connection
        setup and immediately before the first peer indication. Use MD for
        the same-event duplicate and time the later duplicate from its RF
        timestamp. Maintain the connection while waiting across later events
        for the initial response. The duplicate still acknowledges that response
        at T_IFS within its connection event.
        Maintain ten additional quiet events and disconnect through HCI.

    Observation Limit:
        The source specifies no maximum response delay. A finite tester budget
        of LL_RESPONSE_TIMEOUT_US bounds the experiment; absence of a response
        within it yields a diagnostic skip, not a normative timing failure.

    Reference:
        LL.TS.p28.pdf, Section 4.3.7.42; Table 4.3-124, Figure 4.3-254,
        Steps 1-4 and pass/inconclusive verdicts; PDF 1001-1002,
        printed 4.3-528..529. Source reference [3] 5.1.5.
    """
    await _exchange(tester, mode='responding', central=central)


# Verify that a responding IUT completes Version Exchange and ignores further Version
# Indications from the same peer in the same connection.
PROCEDURES |= procedure_cases(
    version_exchange_iut_responding,
    {
        'LL/CON/CEN/BI-12-C': (
            'Version Exchange, IUT Responding',
            {'central': True},
        ),
        'LL/CON/PER/BI-15-C': (
            'Version Exchange, IUT Responding',
            {'central': False},
        ),
    },
)


async def version_exchange_iut_requesting_collision(tester, *, central):
    """Version Exchange, IUT Requesting, Collision.

    Initial Condition:
        Connected in the selected role using public addresses and common
        connection interval, Peripheral latency, timeout and channel map.
        Any advertising interval, advertising channel map and SCA are allowed.
        The selected common interval is 10 ms, latency zero and timeout 300 ms.

    Test Case Configuration:
        LL/CON/CEN/BV-146-C: Central.
        LL/CON/PER/BV-145-C: Peripheral.

    Test Steps:
        1. If the IUT autonomously sends LL_VERSION_IND after connection,
           containing its local version information, proceed to Step 4.
        2. Otherwise send HCI_Read_Remote_Version_Information and require
           successful HCI_Command_Status.
        3. Receive LL_VERSION_IND containing the IUT's version information.
        4. Within that same connection event, T_IFS after the IUT PDU, send the
           Lower Tester's LL_VERSION_IND in the packet acknowledging it.
        5. Require the IUT to acknowledge that PDU without another
           LL_VERSION_IND.
        6. If Step 2 was performed, require successful Remote Version
           Information Complete containing the Lower Tester's version tuple.

    Expected Outcome:
        Pass verdict:
            The IUT initiates Version Exchange and acknowledges the peer's
            indication without sending another local indication. If the Host
            initiated the procedure, completion follows receipt of the peer's
            indication and reports its exact version information.
        Inconclusive verdict:
            A Peripheral IUT does not listen to the Step 4 peer packet.

    Coverage Note:
        Read Local Version Information supplies the expected local LMP tuple.
        MD preserves the collision's same-event exchange. Require exactly one
        local indication, no unsolicited Host completion for an autonomous
        initiation and no duplicates during subsequent quiet events. Disconnect
        through HCI after observation.
        If no Peripheral response is received after the Step 4 transmission,
        use the source's not-listening Inconclusive verdict. Missing responses
        before Step 4, incorrect acknowledgements, invalid CRC/length or version
        information and duplicate indications remain assertion failures.

    Reference:
        LL.TS.p28.pdf, Section 4.3.7.43; Table 4.3-125, Figure 4.3-255,
        Steps 1-6 and pass/inconclusive verdicts; PDF 1002-1004,
        printed 4.3-529..531. Source reference [3] 5.1.5.
    """
    await _exchange(tester, mode='collision', central=central)


# Verify that an IUT handles simultaneous Version Exchange initiation without sending a
# second local Version Indication.
PROCEDURES |= procedure_cases(
    version_exchange_iut_requesting_collision,
    {
        'LL/CON/CEN/BV-146-C': (
            'Version Exchange, IUT Requesting, Collision',
            {'central': True},
        ),
        'LL/CON/PER/BV-145-C': (
            'Version Exchange, IUT Requesting, Collision',
            {'central': False},
        ),
    },
)


async def version_exchange_iut_requesting_delayed_response(tester, *, central):
    """Version Exchange, IUT Requesting, Delayed Response.

    Initial Condition:
        Connected in the selected role using public addresses and common
        connection interval, Peripheral latency, timeout and channel map.
        Any advertising interval, advertising channel map and SCA are allowed.
        The selected common interval is 10 ms, latency zero and timeout 300 ms.

    Test Case Configuration:
        LL/CON/CEN/BV-147-C: Central.
        LL/CON/PER/BV-147-C: Peripheral.

    Test Steps:
        1. If the IUT autonomously sends LL_VERSION_IND after connection,
           containing its local version information, proceed to Step 4.
        2. Otherwise send HCI_Read_Remote_Version_Information and require
           successful HCI_Command_Status.
        3. Receive LL_VERSION_IND containing the IUT's version information.
        4. Acknowledge the IUT indication without sending the peer version.
        5. After ten connection events, send LL_VERSION_IND containing the
           Lower Tester's version information.
        6. Require the IUT to acknowledge it without another LL_VERSION_IND.
        7. If Step 2 was performed, require successful Remote Version
           Information Complete containing the Lower Tester's version tuple.

    Expected Outcome:
        Pass verdict:
            The IUT initiates Version Exchange and acknowledges the delayed
            reply without sending another Version Indication. If its Host
            initiated the procedure, completion occurs after the peer
            indication and reports the exact peer version tuple.
        Fail verdict:
            The IUT drops the connection during the Step 5 delay.

    Coverage Note:
        Read Local Version Information supplies the expected local LMP tuple.
        Maintain empty DATA during the ten-event delay and reject premature
        Host completion or disconnection. Time the delay from the RF packet
        acknowledging the IUT indication, then use the first legal event after
        that delay. Autonomous initiation must not cause unsolicited Host
        completion. Maintain quiet events and disconnect through HCI afterward.

    Reference:
        LL.TS.p28.pdf, Section 4.3.7.44; Table 4.3-126, Figure 4.3-256,
        Steps 1-7 and pass/fail verdicts; PDF 1004-1006,
        printed 4.3-531..533. Source reference [3] 5.1.5.
    """
    await _exchange(tester, mode='delayed', central=central)


# Verify that an initiating IUT tolerates a delayed peer Version Indication without
# disconnecting or repeating its own indication.
PROCEDURES |= procedure_cases(
    version_exchange_iut_requesting_delayed_response,
    {
        'LL/CON/CEN/BV-147-C': (
            'Version Exchange, IUT Requesting, Delayed Response',
            {'central': True},
        ),
        'LL/CON/PER/BV-147-C': (
            'Version Exchange, IUT Requesting, Delayed Response',
            {'central': False},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_version_exchange(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
