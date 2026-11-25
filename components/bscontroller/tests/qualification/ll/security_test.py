# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.8."""

import random

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    LL_ENCRYPTION_IV_HALF_SIZE,
    LL_ENCRYPTION_SKD_HALF_SIZE,
    LL_EVENT_COUNTER_MASK,
    LL_IFS_US,
    LL_RESPONSE_TIMEOUT_US,
    LL_SUPERVISION_TIMEOUT_UNIT_US,
    PDU_CONTROL_ENC_RSP_SIZE,
    PDU_CONTROL_FEATURE_SET_SIZE,
    PDU_CONTROL_FEATURE_SIZE,
    PDU_CONTROL_VERSION_SIZE,
    PDU_DATA_LLID_MASK,
    PDU_DATA_MD_MASK,
    PDU_DATA_NESN_MASK,
    PDU_DATA_SN_MASK,
    PDU_HEADER_SIZE,
    AdvertisingPduType,
    LlId,
)
from tests.qualification import privacy as privacy_peer
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.connection import (
    check_duplicate_advertiser_request,
    check_duplicate_initiator_request,
)
from tests.qualification.link import (
    check_connection_silent,
)
from tests.qualification.peer import (
    PeerLink,
    host_send,
    peer_send,
    received_acl,
)
from tests.qualification.security import (
    MODE_SETUP_DATA_SIZE,
    MODE_SETUP_TOTAL_OCTETS,
    ROOT,
    ErrorCode,
    Opcode,
    aes,
    begin_encryption,
    establish_security_link,
    exchange_security_features,
    expect_no_encryption_result,
    finish_encryption,
)
from tests.qualification.tester import (
    DUT_ADDRESS,
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    PEER_ADDRESS,
    LinkParameters,
    decode_extended,
    extended_enable,
    extended_parameters,
    follow_auxiliary,
    legacy_parameters,
    request_pdu,
)
from tests.support import (
    AUTHENTICATED_PAYLOAD_TIMEOUT_DEFAULT,
    AUTHENTICATED_PAYLOAD_TIMEOUT_UNIT_US,
    CONTROLLER_COMPANY_IDENTIFIER,
)

# Receiving Encrypted Data sends 1000 independent ten-octet LL start PDUs.
RECEIVE_PACKET_COUNT = 1000
RECEIVE_DATA_SIZE = 10
# The selected authenticated-payload-timeout IXIT is one second, below five seconds.
AUTHENTICATED_TIMEOUT_IXIT = 100
# Figure 4.8-42 observes two seconds of empty traffic before the pause response.
PAUSE_DATA_GUARD_US = 2_000_000


async def _authenticated_data(tester, link, data):
    await link.exchange(data, llid=LlId.START)
    # Packet bounds are half-open; PHY RX acknowledges the final sample at end-1.
    timestamp = link.last_sent_end_us - 1
    if link.central:
        await link.exchange()
    await received_acl(tester, link, data, continuation=False)
    return timestamp


async def _encrypted_link(tester, central):
    link = await establish_security_link(tester, central)
    await exchange_security_features(link, ping=False)
    await begin_encryption(tester, link)
    await finish_encryption(tester, link)
    return link


async def _mic_failure(tester, *, central, header_error):
    """Corrupt authentication with a valid CRC, then check full-timeout RF silence."""
    link = await _encrypted_link(tester, central)
    await peer_send(tester, link, b'valid prior data')
    await host_send(tester, link, b'valid DUT data')
    channel = link.channel
    if central:
        packet = await tester.expect_pdu(
            channel,
            start_us=link.anchor - 50,
            duration_us=link.anchor - tester.radio.now_us + 3000,
            **link.radio_parameters,
        )
        assert packet.pdu[0] & PDU_DATA_LLID_MASK in (LlId.CONTINUATION, LlId.START)
        link._accept(link.decode(packet))
        start = packet.end_us + LL_IFS_US
    else:
        start = link.anchor
    corrupted = _corrupted_authenticated_data(link, header_error=header_error)
    await tester.transmit(channel, corrupted, start_us=start, **link.radio_parameters)
    fault_end = tester.radio.now_us + 1
    event = await tester.event(hci.HCI_Disconnection_Complete_Event)
    assert (
        event.status == ErrorCode.SUCCESS
        and event.connection_handle == link.handle
        and event.reason == ErrorCode.CONNECTION_TERMINATED_DUE_TO_MIC_FAILURE_ERROR
    )
    assert not any(isinstance(event, hci.HCI_AclDataPacket) for event in tester.events)
    until = fault_end + link.parameters.timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
    await _repeat_corrupted_data(tester, link, corrupted, until)
    check_connection_silent(tester, link.parameters.access_address, fault_end, until)
    assert not any(isinstance(event, hci.HCI_AclDataPacket) for event in tester.events)


def _corrupted_authenticated_data(link, *, header_error):
    """Apply exactly one source-prescribed fault after encrypting ten data octets."""
    header = LlId.START | link.sn * PDU_DATA_SN_MASK | link.nesn * PDU_DATA_NESN_MASK
    payload = bytearray(link.encode(header, bytes(range(RECEIVE_DATA_SIZE))))
    if header_error:
        # Flip only the low LLID bit, without recalculating the MIC.
        header ^= 1
    else:
        payload[-1] ^= 1
    return bytes([header, len(payload)]) + payload


async def _repeat_corrupted_data(tester, link, corrupted, until):
    """Retain event phase, including delayed Host notification and counter wrap."""
    interval = link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
    reply_offset = (
        link.rx_phy.airtime_us(PDU_HEADER_SIZE) + LL_IFS_US if link.central else 0
    )
    while link.anchor < until:
        if link.anchor <= tester.radio.now_us:
            link.anchor += interval
            link.counter = (link.counter + 1) & LL_EVENT_COUNTER_MASK
            continue
        await tester.transmit(
            link.channel,
            corrupted,
            start_us=link.anchor + reply_offset,
            **link.radio_parameters,
        )
        assert (
            await tester.receive(
                link.channel,
                duration_us=min(LL_IFS_US + 1000, until - tester.radio.now_us),
                **link.radio_parameters,
            )
            is None
        )
    if tester.radio.now_us < until:
        await tester.wait(until - tester.radio.now_us)


async def _mode_setup(tester, *, central):
    """Verify complete encryption setup and both address rounds with 1000 octets."""
    for random_address in (False, True):
        link = await establish_security_link(
            tester, central, random_address=random_address
        )
        await exchange_security_features(link, ping=False)
        for _ in range(MODE_SETUP_TOTAL_OCTETS // MODE_SETUP_DATA_SIZE):
            await host_send(tester, link, bytes(MODE_SETUP_DATA_SIZE))
        await begin_encryption(tester, link)
        await finish_encryption(tester, link)
        for _ in range(MODE_SETUP_TOTAL_OCTETS // MODE_SETUP_DATA_SIZE):
            await host_send(tester, link, bytes(MODE_SETUP_DATA_SIZE))
        await link.disconnect()


def _no_disconnect(tester, link=None):
    assert not any(
        isinstance(event, hci.HCI_Disconnection_Complete_Event)
        for event in tester.events
    )
    if link is not None:
        assert not link.termination_received, 'IUT sent LL_TERMINATE_IND'


async def _overlapping(tester, *, features=False, version_exchanged=False):
    """Observe deferral across the whole handshake, including prior-version state."""
    link = await establish_security_link(tester, True)
    peer_features = int(hci.LeFeatureMask.LE_ENCRYPTION)
    if features:
        declared = await tester.command(
            hci.HCI_LE_Read_Local_Supported_Features_Command()
        )
        if not int.from_bytes(declared.le_features, 'little') & (
            hci.LeFeatureMask.PERIPHERAL_INITIATED_FEATURE_EXCHANGE
        ):
            pytest.skip('IUT does not declare the required Peripheral Feature Exchange')
        version_response = None
    else:
        version = await tester.command(hci.HCI_Read_Local_Version_Information_Command())
        version_response = (
            bytes([Opcode.LL_VERSION_IND, version.lmp_version])
            + version.company_identifier.to_bytes(2, 'little')
            + version.lmp_subversion.to_bytes(2, 'little')
        )
    expected = Opcode.LL_FEATURE_RSP if features else Opcode.LL_VERSION_IND
    payload = (
        bytes([Opcode.LL_PERIPHERAL_FEATURE_REQ])
        + peer_features.to_bytes(PDU_CONTROL_FEATURE_SET_SIZE, 'little')
        if features
        else bytes([Opcode.LL_VERSION_IND, hci.SpecificationVersion.BLUETOOTH_CORE_5_0])
        + CONTROLLER_COMPANY_IDENTIFIER.to_bytes(2, 'little')
        + bytes(2)
    )
    if version_exchanged:
        assert await link.control(Opcode.LL_VERSION_IND, payload) == version_response
        await link.exchange()
    first_control = len(link.received_controls)
    await begin_encryption(tester, link)
    await link.exchange(payload, llid=LlId.CONTROL)
    packet = await link.exchange()
    assert packet.pdu[PDU_HEADER_SIZE:] == b'', (
        'overlapping response sent before encryption completes'
    )
    await finish_encryption(tester, link)
    completed_us = link.last_new_mic_tx_us
    assert completed_us is not None
    if version_exchanged:
        until = tester.radio.now_us + (
            link.parameters.timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
        )
        while tester.radio.now_us < until:
            await link.exchange()
            _no_disconnect(tester, link)
    else:
        if not any(
            packet.pdu[PDU_HEADER_SIZE : PDU_HEADER_SIZE + 1] == bytes([expected])
            for packet, _ in link.received_controls[first_control:]
        ):
            await link.control(expected)
    responses = _check_overlapping_responses(
        link.received_controls[first_control:],
        expected,
        completed_us,
        ignored=version_exchanged,
    )
    for result in responses:
        if features:
            assert len(result) == PDU_CONTROL_FEATURE_SIZE
            assert int.from_bytes(result[1:], 'little') & peer_features == peer_features
        else:
            assert len(result) == PDU_CONTROL_VERSION_SIZE
            assert result == version_response
    _no_disconnect(tester, link)
    expect_no_encryption_result(tester)
    await link.disconnect()


def _check_overlapping_responses(controls, opcode, completed_us, *, ignored=False):
    """A response consumed by handshake polling must still affect the verdict."""
    responses = []
    for packet, encrypted in controls:
        payload = packet.pdu[PDU_HEADER_SIZE:]
        if payload and payload[0] == opcode:
            assert not ignored, 'IUT answered an already exchanged version'
            assert encrypted and packet.start_us > completed_us, (
                'overlapping response sent before encryption completes'
            )
            responses.append(payload)
    assert ignored or responses, 'deferred control response not received'
    return responses


async def _pause(tester, link, *, send_data=False):
    if link.central:
        await tester.command(
            hci.HCI_LE_Enable_Encryption_Command(
                link.handle, link.rand, link.ediv, link.ltk
            )
        )
        assert await link.control(Opcode.LL_PAUSE_ENC_REQ) == bytes(
            [Opcode.LL_PAUSE_ENC_REQ]
        )
        if send_data:
            await peer_send(tester, link, b'\xff')
            expect_no_encryption_result(tester)
            _no_disconnect(tester, link)
            guard = tester.radio.now_us + PAUSE_DATA_GUARD_US
            while link.anchor < guard:
                packet = await link.exchange()
                assert packet.pdu[PDU_HEADER_SIZE:] == b''
                _no_disconnect(tester, link)
                expect_no_encryption_result(tester)
        await link.exchange(bytes([Opcode.LL_PAUSE_ENC_RSP]), llid=LlId.CONTROL)
        link.rx_enabled = False
        assert await link.control(Opcode.LL_PAUSE_ENC_RSP) == bytes(
            [Opcode.LL_PAUSE_ENC_RSP]
        )
        link.tx_enabled = False
    else:
        assert await link.control(
            Opcode.LL_PAUSE_ENC_RSP,
            bytes([Opcode.LL_PAUSE_ENC_REQ]),
            check_ack=True,
        ) == bytes([Opcode.LL_PAUSE_ENC_RSP])
        link.tx_enabled = link.rx_enabled = False
        await link.exchange(bytes([Opcode.LL_PAUSE_ENC_RSP]), llid=LlId.CONTROL)


async def _pause_procedure(tester, *, central, send_data=False):
    """Pause/restart with role-specific data placement and a new session key."""
    link = await _encrypted_link(tester, central)
    old_iv = link.iv
    old_key = link.session_key
    await _pause(tester, link, send_data=send_data)
    queued_before = len(link.received_data)
    if send_data and (not central):
        await tester.wait(0)
        tester.send(
            bytes(
                hci.HCI_AclDataPacket(
                    link.handle, hci.HCI_ACL_PB_FIRST_NON_FLUSHABLE, 0, 1, b'\xff'
                )
            )
        )
    await begin_encryption(tester, link, refresh=True)
    assert len(link.received_data) == queued_before, (
        'user data sent during the unencrypted restart handshake'
    )
    assert link.iv != old_iv
    assert link.session_key != old_key, 'session key was reused during refresh'
    expect_no_encryption_result(tester)
    await finish_encryption(tester, link, refresh=True)
    if send_data:
        if central:
            await peer_send(tester, link, b'\x11')
        else:
            packet = link.last_received
            for _ in range(16):
                if packet.pdu[PDU_HEADER_SIZE:] == b'\xff':
                    break
                assert packet.pdu[PDU_HEADER_SIZE:] == b''
                packet = await link.exchange()
            assert packet.pdu[PDU_HEADER_SIZE:] == b'\xff'
            assert link.rx_enabled
            await link.exchange()
            event = await tester.event(hci.HCI_Number_Of_Completed_Packets_Event)
            assert event.connection_handles == [link.handle]
            assert event.num_completed_packets == [1]
    _no_disconnect(tester, link)
    expect_no_encryption_result(tester)
    await link.disconnect()


async def _ping(tester, *, central, mode):
    """Check encrypted Ping, MIC timer reset, response and expiry in each role."""
    link = await establish_security_link(tester, central)
    await exchange_security_features(link, ping=mode != 'unknown')
    await begin_encryption(tester, link)
    await finish_encryption(tester, link)
    if mode == 'respond':
        assert await link.control(
            Opcode.LL_PING_RSP, bytes([Opcode.LL_PING_REQ])
        ) == bytes([Opcode.LL_PING_RSP])
        await link.disconnect()
        return
    timeout = AUTHENTICATED_PAYLOAD_TIMEOUT_DEFAULT
    if mode in ('modified', 'expire'):
        timeout = AUTHENTICATED_TIMEOUT_IXIT
        result = await tester.command(
            hci.HCI_Write_Authenticated_Payload_Timeout_Command(link.handle, timeout)
        )
        assert result.connection_handle == link.handle
    result = await tester.command(
        hci.HCI_Read_Authenticated_Payload_Timeout_Command(link.handle)
    )
    assert (
        result.connection_handle == link.handle
        and result.authenticated_payload_timeout == timeout
    )
    mask = 1 << (
        hci.HCI_AUTHENTICATED_PAYLOAD_TIMEOUT_EXPIRED_EVENT
        - hci.HCI_EVENT_MASK_PAGE_2_CODE_BASE
    )
    await tester.command(
        hci.HCI_Set_Event_Mask_Page_2_Command(mask.to_bytes(8, 'little'))
    )
    timeout_us = timeout * AUTHENTICATED_PAYLOAD_TIMEOUT_UNIT_US
    if central and mode == 'unknown':
        # This source case measures its first request from encrypted establishment,
        # not from an extra DATA packet that would restart the authentication timer.
        baseline = link.last_new_mic_tx_us
        assert baseline is not None
    else:
        baseline = await _authenticated_data(tester, link, b'authenticated baseline')
    interval = link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
    idle_start = baseline
    rounds = 0
    minimum_rounds = 1 if mode == 'expire' else 2
    while rounds < minimum_rounds or (
        mode == 'modified' and tester.radio.now_us <= idle_start + timeout_us
    ):
        saw_ping = False
        response = (
            bytes([Opcode.LL_UNKNOWN_RSP, Opcode.LL_PING_REQ])
            if mode == 'unknown'
            else bytes([Opcode.LL_PING_RSP])
        )
        while link.anchor < baseline + timeout_us + interval:

            def reply(packet, response=response):
                return (
                    (response, LlId.CONTROL)
                    if packet.pdu[PDU_HEADER_SIZE:] == bytes([Opcode.LL_PING_REQ])
                    and mode != 'expire'
                    else (b'', LlId.CONTINUATION)
                )

            packet = await link.exchange(reply=reply if central else None)
            payload = packet.pdu[PDU_HEADER_SIZE:]
            if payload:
                assert packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
                assert payload == bytes([Opcode.LL_PING_REQ])
                saw_ping = True
                assert packet.start_us < baseline + timeout_us
                break
        assert saw_ping, 'no PING_REQ before authenticated payload deadline'
        if mode != 'expire':
            if not central:
                await link.exchange(response, llid=LlId.CONTROL)
            received_at = link.last_sent_end_us - 1
            if mode == 'modified':
                assert received_at - baseline <= timeout_us
            baseline = received_at
            _no_disconnect(tester)
        else:
            await _observe_authentication_expiry(tester, link, baseline + timeout_us)
        rounds += 1
    await link.disconnect()


def _no_authentication_expiry(tester):
    assert not any(
        isinstance(event, hci.HCI_Authenticated_Payload_Timeout_Expired_Event)
        for event in tester.events
    ), 'authenticated payload timeout reported before its condition was met'


async def _observe_authentication_expiry(tester, link, deadline):
    """Observe both sides of the timer boundary without an HCI latency requirement."""
    interval = link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
    # Leave one interval free so an RF receive cannot straddle the boundary.
    while link.anchor + interval < deadline:
        await link.exchange()
        _no_authentication_expiry(tester)
    if tester.radio.now_us >= deadline:
        pytest.skip('Expiry observation crossed its pre-deadline sampling point')
    await tester.wait(deadline - 1 - tester.radio.now_us)
    _no_authentication_expiry(tester)
    await tester.wait(1)
    event = await tester.event(hci.HCI_Authenticated_Payload_Timeout_Expired_Event)
    assert event.connection_handle == link.handle
    _no_disconnect(tester)
    # Boundary observation deliberately omitted RF events; retain the original phase
    # and channel sequence when returning to the connection postamble.
    while link.anchor <= tester.radio.now_us:
        link.anchor += interval
        link.counter = (link.counter + 1) & LL_EVENT_COUNTER_MASK


async def _reject(tester, *, central, extended):
    """Check the specified rejection stage, reason, packet and HCI result."""
    link = await establish_security_link(
        tester, central, random_address=not central or not extended, random_peer=False
    )
    await exchange_security_features(link, ping=False, extended_reject=extended)
    if not central and not extended:
        plaintext = (2).to_bytes(16, 'little')
        result = await tester.command(hci.HCI_LE_Encrypt_Command(ROOT, plaintext))
        assert result.encrypted_data == aes(ROOT, plaintext)
    await begin_encryption(tester, link)
    reason = (
        ErrorCode.PIN_OR_KEY_MISSING_ERROR
        if extended or not central
        else ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
    )
    opcode = Opcode.LL_REJECT_EXT_IND if extended else Opcode.LL_REJECT_IND
    payload = (
        bytes([opcode])
        + (bytes([Opcode.LL_ENC_REQ]) if extended else b'')
        + bytes([reason])
    )
    if central:
        expect_no_encryption_result(tester)
        if extended:
            await link.exchange(
                bytes([Opcode.LL_ENC_RSP]) + link.skd_component + link.iv_component,
                llid=LlId.CONTROL,
            )
            expect_no_encryption_result(tester)
        await link.exchange(payload, llid=LlId.CONTROL)
        event = await tester.event(hci.HCI_Encryption_Change_Event)
        assert (
            event.status == reason
            and event.connection_handle == link.handle
            and (not event.encryption_enabled)
        )
    else:
        result = await tester.command(
            hci.HCI_LE_Long_Term_Key_Request_Negative_Reply_Command(link.handle)
        )
        assert result.connection_handle == link.handle
        await _receive_encryption_rejection(link, extended=extended)
    await peer_send(tester, link, b'after rejection')
    await link.disconnect()


async def _receive_encryption_rejection(link, *, extended):
    """Accept either source-permitted form, checking its exact wire fields."""
    # A local observation watchdog, not an HCI delivery-latency requirement.
    deadline = link.tester.radio.now_us + LL_RESPONSE_TIMEOUT_US
    while link.tester.radio.now_us < deadline:
        packet = await link.exchange()
        if packet.pdu[0] & PDU_DATA_LLID_MASK != LlId.CONTROL:
            continue
        payload = packet.pdu[PDU_HEADER_SIZE:]
        if payload and payload[0] in (Opcode.LL_REJECT_IND, Opcode.LL_REJECT_EXT_IND):
            expected = bytes([Opcode.LL_REJECT_IND, ErrorCode.PIN_OR_KEY_MISSING_ERROR])
            if payload[0] == Opcode.LL_REJECT_EXT_IND:
                assert extended, 'extended rejection without negotiated support'
                expected = bytes(
                    [
                        Opcode.LL_REJECT_EXT_IND,
                        Opcode.LL_ENC_REQ,
                        ErrorCode.PIN_OR_KEY_MISSING_ERROR,
                    ]
                )
            assert payload == expected, 'incorrect encryption rejection fields'
            return
    raise AssertionError('encryption rejection not received')


async def _security_timeout(tester, *, central, stage, data=False):
    """Withhold a particular handshake PDU while ACKs prevent supervision expiry."""
    if not central:
        await _peripheral_missing_start_response(tester)
        return
    link = await establish_security_link(tester, central)
    earliest_reset = tester.radio.now_us
    await begin_encryption(tester, link)
    latest_reset = link.last_received.start_us
    if stage != 'response':
        await link.exchange(
            bytes([Opcode.LL_ENC_RSP]) + link.skd_component + link.iv_component,
            llid=LlId.CONTROL,
        )
        if stage == 'final':
            link.rx_enabled = True
            packet = await link.exchange(
                bytes([Opcode.LL_START_ENC_REQ]), llid=LlId.CONTROL
            )
            assert packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
            assert packet.pdu[PDU_HEADER_SIZE:] == b''
            earliest_reset = link.last_sent_end_us - 1
            assert await link.control(Opcode.LL_START_ENC_RSP, check_ack=True) == bytes(
                [Opcode.LL_START_ENC_RSP]
            )
            latest_reset = link.last_received.start_us
            await link.exchange(check_ack=True)
    if data:
        await peer_send(tester, link, b'\xff')
    expect_no_encryption_result(tester)
    await _observe_security_timeout(tester, link, earliest_reset, latest_reset)


async def _peripheral_missing_start_response(tester):
    """Bound T_PRT by observable events without inventing an air-bit origin."""
    link = await establish_security_link(
        tester, False, random_address=True, random_peer=False
    )
    await begin_encryption(tester, link)
    earliest_reset = tester.radio.now_us
    result = await tester.command(
        hci.HCI_LE_Long_Term_Key_Request_Reply_Command(link.handle, link.ltk)
    )
    assert result.connection_handle == link.handle
    assert await link.control(Opcode.LL_START_ENC_REQ) == bytes(
        [Opcode.LL_START_ENC_REQ]
    )
    latest_reset = link.last_received.start_us
    await link.exchange(check_ack=True)
    await _observe_security_timeout(tester, link, earliest_reset, latest_reset)


async def _observe_security_timeout(tester, link, earliest_reset, latest_reset):
    """Keep RF cessation independent of the Host notification delivery time."""
    assert earliest_reset <= latest_reset
    interval = link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
    earliest_expiry = earliest_reset + LL_RESPONSE_TIMEOUT_US
    latest_expiry = latest_reset + LL_RESPONSE_TIMEOUT_US
    while link.anchor + interval < earliest_expiry:
        packet = await link.exchange()
        assert packet.pdu[PDU_HEADER_SIZE:] == b''
        _no_disconnect(tester)
    assert tester.radio.now_us < earliest_expiry
    await tester.wait(earliest_expiry - 1 - tester.radio.now_us)
    _no_disconnect(tester)
    await tester.wait(latest_expiry + 1 - tester.radio.now_us)
    # Keep the original anchor/channel sequence after the boundary observation.
    while link.anchor <= tester.radio.now_us:
        link.anchor += interval
        link.counter = (link.counter + 1) & LL_EVENT_COUNTER_MASK
    end = latest_expiry + link.parameters.timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
    if link.central:
        assert (
            await tester.receive(
                link.channel,
                duration_us=end - tester.radio.now_us,
                **link.radio_parameters,
            )
            is None
        )
    else:
        while link.anchor < end:
            assert await link.exchange(required=False) is None
    if tester.radio.now_us < end:
        await tester.wait(end - tester.radio.now_us)
    check_connection_silent(tester, link.parameters.access_address, latest_expiry, end)
    event = await tester.event(hci.HCI_Disconnection_Complete_Event)
    assert (
        event.status == ErrorCode.SUCCESS
        and event.connection_handle == link.handle
        and event.reason == ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR
    )


async def _unexpected(tester, *, data=False, refresh=False):
    """Send ENC_RSP then the forbidden PDU in the same Central connection event."""
    link = await establish_security_link(tester, True)
    if data:
        version = await tester.command(hci.HCI_Read_Local_Version_Information_Command())
        if version.lmp_version < hci.SpecificationVersion.BLUETOOTH_CORE_5_0:
            pytest.skip(
                'Legacy pre-5.0 MIC-failure termination path is not implemented'
            )
    if refresh:
        await begin_encryption(tester, link)
        await finish_encryption(tester, link)
        old_key, old_iv = link.session_key, link.iv
        await _pause(tester, link)
    await begin_encryption(tester, link, refresh=refresh)
    if refresh:
        assert link.session_key != old_key, 'session key was reused during refresh'
        assert link.iv != old_iv, 'initialization vector was reused during refresh'
    expect_no_encryption_result(tester)
    channel = link.channel
    packet = await tester.expect_pdu(
        channel,
        start_us=max(tester.radio.now_us + 1, link.anchor - 50),
        duration_us=link.anchor - tester.radio.now_us + 3000,
        **link.radio_parameters,
    )
    assert packet.start_us == link.anchor
    link._accept(link.decode(packet))
    response = bytes([Opcode.LL_ENC_RSP]) + link.skd_component + link.iv_component
    header = (
        LlId.CONTROL
        | link.sn * PDU_DATA_SN_MASK
        | link.nesn * PDU_DATA_NESN_MASK
        | PDU_DATA_MD_MASK
    )
    await tester.transmit(
        channel,
        bytes([header, len(response)]) + response,
        start_us=packet.end_us + LL_IFS_US,
        **link.radio_parameters,
    )
    response_end = tester.radio.now_us + 1
    link.last_sent_end_us = response_end
    acknowledgement = await tester.expect_pdu(
        channel, duration_us=1000, **link.radio_parameters
    )
    assert acknowledgement.start_us == response_end + LL_IFS_US
    assert acknowledgement.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
    assert acknowledgement.pdu[PDU_HEADER_SIZE:] == b''
    assert int(bool(acknowledgement.pdu[0] & PDU_DATA_NESN_MASK)) != link.sn
    link._accept(link.decode(acknowledgement))
    expect_no_encryption_result(tester)
    payload = (
        b'\xff'
        if data
        else bytes([Opcode.LL_VERSION_IND, hci.SpecificationVersion.BLUETOOTH_CORE_5_0])
        + CONTROLLER_COMPANY_IDENTIFIER.to_bytes(2, 'little')
        + bytes(2)
    )
    await link._send(
        channel,
        payload,
        LlId.START if data else LlId.CONTROL,
        acknowledgement.end_us + LL_IFS_US,
    )
    fault_end = tester.radio.now_us + 1
    event = await tester.event(hci.HCI_Disconnection_Complete_Event)
    assert (
        event.status == ErrorCode.SUCCESS
        and event.connection_handle == link.handle
        and event.reason == ErrorCode.CONNECTION_TERMINATED_DUE_TO_MIC_FAILURE_ERROR
    )
    await tester.wait(link.parameters.interval * LL_CONN_INTERVAL_UNIT_US)
    check_connection_silent(
        tester, link.parameters.access_address, fault_end, tester.radio.now_us
    )
    assert not any(isinstance(event, hci.HCI_AclDataPacket) for event in tester.events)


async def _peripheral_unexpected_start_pdu(tester, *, data):
    """Inject in the first event, without waiting for ENC_RSP or a Host LTK event."""
    link = await establish_security_link(tester, False)
    request = (
        bytes([Opcode.LL_ENC_REQ])
        + link.rand
        + link.ediv.to_bytes(2, 'little')
        + link.generator.randbytes(LL_ENCRYPTION_SKD_HALF_SIZE)
        + link.generator.randbytes(LL_ENCRYPTION_IV_HALF_SIZE)
    )
    header = (
        LlId.CONTROL
        | link.sn * PDU_DATA_SN_MASK
        | link.nesn * PDU_DATA_NESN_MASK
        | PDU_DATA_MD_MASK
    )
    channel = link.channel
    await tester.transmit(
        channel,
        bytes([header, len(request)]) + request,
        start_us=link.anchor,
        **link.radio_parameters,
    )
    request_end = tester.radio.now_us + 1
    response = await tester.expect_pdu(
        channel, duration_us=1000, **link.radio_parameters
    )
    assert response.start_us == request_end + LL_IFS_US
    assert int(bool(response.pdu[0] & PDU_DATA_NESN_MASK)) != link.sn
    payload = response.pdu[PDU_HEADER_SIZE:]
    if payload:
        assert response.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
        assert len(payload) == PDU_CONTROL_ENC_RSP_SIZE
        assert payload[0] == Opcode.LL_ENC_RSP
    link._accept(response)
    forbidden = (
        b'\xff'
        if data
        else bytes([Opcode.LL_VERSION_IND, hci.SpecificationVersion.BLUETOOTH_CORE_5_0])
        + CONTROLLER_COMPANY_IDENTIFIER.to_bytes(2, 'little')
        + bytes(2)
    )
    await link._send(
        channel,
        forbidden,
        LlId.START if data else LlId.CONTROL,
        response.end_us + LL_IFS_US,
    )
    fault_end = tester.radio.now_us + 1
    event = await tester.event(hci.HCI_Disconnection_Complete_Event)
    assert (
        event.status == ErrorCode.SUCCESS
        and event.connection_handle == link.handle
        and event.reason == ErrorCode.CONNECTION_TERMINATED_DUE_TO_MIC_FAILURE_ERROR
    )
    await tester.wait(link.parameters.interval * LL_CONN_INTERVAL_UNIT_US)
    check_connection_silent(
        tester,
        link.parameters.access_address,
        fault_end,
        tester.radio.now_us,
        allowed_control={Opcode.LL_ENC_RSP: PDU_CONTROL_ENC_RSP_SIZE},
    )
    assert not any(isinstance(event, hci.HCI_AclDataPacket) for event in tester.events)


async def peripheral_encryption_mode_setup(tester):
    """Peripheral Encryption Mode Setup.

    Initial Condition:
        Connected Peripheral with any advertising interval, supported own-address
        type and advertising channel map, common connection interval/timeout and
        any SCA. Select 10-ms interval, zero latency and 300-ms timeout.
        For the random-address round, calculate an RPA from the common IR before
        connecting. Use common IR/ER 0x112233445566778899AABBCCDDEEFF00.
        Complete the Section 4.1.6 Feature Exchange preamble for LE Encryption.
        The common key preamble calculates DHK, EDIV and LTK through HCI Encrypt
        and Rand, independently verifies AES and DIV recovery, and sets the
        calculated LTK when requested by the IUT.

    Test Steps:
        1. Establish the connection with public Lower Tester address and a
           supported IUT address type; select public IUT address for this round.
        2. Configure the Upper Tester to submit ten-octet HCI ACL packets of
           0x00 using the connection handle and a packet-start PB flag until
           1000 octets have been successfully submitted.
        3. Send empty DATA once per connection interval and receive all data
           submitted in Step 2, maintaining the acknowledgement scheme.
           Interleave submission and reception to respect HCI buffer credits.
        4. Calculate the Lower Tester's Central SKD and IV components and random
           number. Select reproducible PRNG-generated SKDc/IVc and retain the
           Rand associated with the common key preamble's EDIV.
        5. Send LL_ENC_REQ containing Rand, EDIV, SKDc and IVc. Receive
           LL_ENC_RSP with the Peripheral's SKD and IV components; continue
           Central transmissions and independently derive the session key/IV.
        6. Receive HCI LE Long Term Key Request with the same handle, Rand and
           EDIV as the request in Step 5.
        7. Reply through HCI with the calculated LTK and require successful
           Command Complete for the same connection handle.
        8. Receive LL_START_ENC_REQ and acknowledge it.
        9. Send encrypted LL_START_ENC_RSP and receive an encrypted
           LL_START_ENC_RSP in response. Verify its MIC and acknowledgement.
        10. Receive successful HCI Encryption Change for the same connection
            handle with encryption enabled.
        11. Configure submission of another 1000 zero octets as ten-octet
            packet-start HCI ACL packets on the same handle.
        12. Send empty DATA each interval and receive all Step 11 data as
            encrypted payloads with valid MICs. Check byte values, ordering
            and start boundaries without prescribing LL fragmentation.
        13. Terminate the connection and check HCI Disconnection Complete.
        14. Establish another connection with random Lower Tester and IUT
            addresses; the IUT uses the RPA calculated from the common IR.
        15. Repeat Steps 2-13 on that connection.

    Expected Outcome:
        Pass verdict:
            Both address rounds transmit all data correctly. The IUT supplies
            its IV/SKD components in LL_ENC_RSP and reports the setup request
            through HCI. It sends LL_START_ENC_REQ until acknowledged,
            acknowledges the encrypted LL_START_ENC_RSP, responds with its
            own encrypted LL_START_ENC_RSP, and reports encryption enabled.
            Independent AES-CCM processing verifies encrypted data and MICs.

    Source Correction:
        Figure 4.1-27 prints the final Encrypt operands as (DIV, ER). Use ER
        as the AES key and the zero-padded DIV block as plaintext, consistent
        with Core Vol 3 Part H Appendix B.2.1/B.2.2:
        LTK=d1(ER,DIV,0)=e(ER,padding||0||DIV).
        Step 13's un-encrypted termination annotation is stale: Step 12 has
        verified encrypted traffic and no pause/disable step intervenes.
        Terminate the actually encrypted connection without inventing a pause.
        Amber supports these Core-backed local corrections; they are not
        adopted SIG errata.

    Reference:
        LL.TS.p28, Section 4.8.6, LL/SEC/PER/BV-01-C; PDF 1244-1246,
        printed 4.8-57..59; Figure 4.8-28. Common preambles: Section 4.1.6.6,
        PDF 76-77, printed 4.1-28..29, Figures 4.1-26/27.
        Core reference [3], Vol 6 Part B Section 5.1.3.
    """
    await _mode_setup(tester, central=False)


async def peripheral_pause_encryption(tester, *, send_data):
    """Peripheral Pause Encryption.

    Initial Condition:
        Connected Peripheral with any advertising interval, supported address
        type and advertising channel map, common interval/timeout and any SCA.
        A random-address execution would first calculate the address from the
        common IR. Select a public-address connection, 10-ms interval, zero
        latency and 300-ms timeout, with LE Encryption feature exchange.
        Table 4.8-8 selects no queued data for BV-02 and queued data for BV-03.

    Test Case Configuration:
        LL/SEC/PER/BV-02-C: no queued data.
        LL/SEC/PER/BV-03-C: queued data variant.

    Test Steps:
        1. Establish an encrypted connection with the IUT as Peripheral.
        2. Send encrypted LL_PAUSE_ENC_REQ and receive encrypted
           LL_PAUSE_ENC_RSP.
        3. Respond with an unencrypted LL_PAUSE_ENC_RSP. Encryption is paused.
        4. For BV-03 only, submit one 0xFF octet through HCI ACL using the
           connection handle and a packet-start PB flag.
        5. Send unencrypted LL_ENC_REQ using new session-key material; receive
           LL_ENC_RSP. Verify that queued user data has not escaped during
           this unencrypted restart and that the derived key and IV changed.
        6. Receive HCI LE Long Term Key Request matching the handle, Rand and
           EDIV sent in Step 5.
        7. Reply with the LTK through HCI and require successful Command
           Complete for the same connection handle.
        8. Receive LL_START_ENC_REQ and acknowledge it.
        9. Send encrypted LL_START_ENC_RSP and receive an encrypted
           LL_START_ENC_RSP, independently checking its MIC and acknowledgement.
        10. Receive successful HCI Encryption Key Refresh Complete for this
            connection. Confirm resumed encryption through encrypted packets,
            not a nonexistent Encryption_Enabled field in this event.
        11. Execute the Table 4.8-8 alternative:
            11A.1. BV-02: terminate the connection and check HCI completion.
            11B.1. BV-03: send empty DATA each interval until the IUT sends the
                   queued encrypted 0xFF with valid MIC. Acknowledge it and
                   verify HCI completed-packet credits on the same handle.
            11B.2. Terminate the connection and check HCI completion.

    Expected Outcome:
        Pass verdict:
            The IUT pauses encryption and sends LL_START_ENC_REQ during restart.
            It reports Encryption Key Refresh Complete after resuming.
            For BV-03, send the queued data only after encryption has resumed.
            No queued user data may be transmitted during pause or restart.

    Source Correction:
        Step 10 describes encryption enabled in Key Refresh Complete, but that
        event contains only Status and Connection_Handle (Core Vol 4 Part E
        Section 7.7.39). Check those fields and the valid encrypted handshake
        and DATA instead. Alternatives 11A.1/11B.2 describe un-encrypted
        termination even though encryption has resumed with no later pause;
        terminate the actual encrypted link. The common-key MSC correction
        uses ER as key in LTK=d1(ER,DIV,0), not DIV.
        Amber supports these local readings; none is adopted SIG errata.

    Reference:
        LL.TS.p28, Section 4.8.6.1, LL/SEC/PER/BV-02-C and LL/SEC/PER/BV-03-C;
        PDF 1246-1248, printed 4.8-59..61; Table 4.8-8, Figure 4.8-29.
        Core reference [3], Vol 6 Part B Section 5.1.3.2; common key preamble
        Section 4.1.6.6, Figure 4.1-27.
    """
    await _pause_procedure(tester, central=False, send_data=send_data)


async def peripheral_sending_ll_reject_ind(tester):
    """Peripheral Sending LL_REJECT_IND.

    Initial Condition:
        Connected Peripheral with a private address, any advertising interval
        and advertising channel map, common connection interval/timeout and
        any SCA. Select an RPA derived from the common IR, 10-ms interval,
        zero latency and 300-ms timeout.
        The Lower Tester has a public address and does not support extended
        rejection. Exchange features with Extended Reject Indication clear.
        Use the common IR/ER and encryption-key preamble for Rand and EDIV.

    Test Steps:
        1. Establish the connection using a public Lower Tester address and a
           supported IUT address type; select a private IUT RPA to satisfy the
           initial condition.
        2. Send HCI LE Encrypt with key IR and plaintext 2; receive successful
           HCI Command Complete containing DHK. Independently check the AES result.
        3. Send LL_ENC_REQ with Rand, EDIV, Central SKD and IV components.
           Receive LL_ENC_RSP and continue Central transmissions.
        4. Receive HCI LE Long Term Key Request with the same handle, Rand
           and EDIV as Step 3.
        5. Send HCI LE Long Term Key Request Negative Reply for that handle.
           Receive successful HCI Command Complete for the same handle.
        6. Receive LL_REJECT_IND with ErrorCode PIN or Key Missing.

    Expected Outcome:
        Pass verdict:
            Receive LL_ENC_RSP in response to LL_ENC_REQ, successful Command
            Complete for the negative LTK reply, and LL_REJECT_IND with
            PIN or Key Missing. An extended reject does not meet this case.

    Postamble:
        Acknowledge rejection, verify that the unencrypted ACL is still usable,
        then terminate it and check HCI Disconnection Complete.

    Source Correction:
        The common-key Figure 4.1-27 labels the final Encrypt operands (DIV, ER).
        Retain Core LTK=d1(ER,DIV,0), with ER as key and padded DIV as plaintext.
        Amber confirms that operand order; this is local, not adopted SIG errata.

    Reference:
        LL.TS.p28, Section 4.8.6, LL/SEC/PER/BV-04-C; PDF 1248-1249,
        printed 4.8-61..62; Figure 4.8-30. Core reference [3],
        Vol 6 Part B Section 5.1.3.1; common preamble Section 4.1.6.6,
        Figures 4.1-26/27.
    """
    await _reject(tester, central=False, extended=False)


async def peripheral_receiving_encrypted_data(tester):
    """Peripheral Receiving Encrypted Data.

    Initial Condition:
        Connected Peripheral with any supported own-address type, advertising
        interval and channel map, common interval/timeout and any SCA.
        Random-address execution requires an address calculated from common IR.
        Select public addresses, 10-ms interval, zero latency and 300-ms timeout.
        Establish encryption with independent key/IV derivation, Feature Exchange
        and valid encrypted START_ENC_RSP before the numbered procedure.

    Test Steps:
        1. Prepare 1000 encrypted DATA packets with LLID=0x02 and ten-octet
           plaintext payloads containing random octets. Use a fixed PRNG seed
           for reproducible tester data, not as a requirement on the IUT.
        2. Send one packet each interval using the acknowledgement scheme and
           the independently selected data channel. Receive an empty DATA
           response and verify its acknowledgement and MIC as applicable.
        3. Receive the corresponding HCI ACL data on the connection handle.
           Require packet-start PB=0x02, followed by PB=0x01 continuations if
           the IUT fragments HCI delivery; check BC, lengths and exact bytes.
        4. Repeat Steps 2-3 until every packet from Step 1 has been reported.

    Expected Outcome:
        Pass verdict:
            Acknowledge all transmitted data and deliver all of it correctly to
            the Upper Tester through HCI. Preserve byte order and start-boundary
            positions as required by Section 4.1.7; do not require a particular
            HCI fragmentation. Independent AES-CCM creates the transmitted
            ciphertext/MIC and validates encrypted responses.

    Postamble:
        Reject extra HCI ACL data or premature disconnection, then terminate
        the encrypted connection and verify HCI Disconnection Complete.

    Source Correction:
        The encryption preamble uses Core LTK=d1(ER,DIV,0), correcting
        Figure 4.1-27's reversed final Encrypt operand labels. Amber confirms
        the Core equation; this is not adopted SIG errata.

    Reference:
        LL.TS.p28, Section 4.8.6, LL/SEC/PER/BV-05-C; PDF 1250,
        printed 4.8-63; Figure 4.8-31. Core reference [3],
        Vol 6 Part B Section 5.1.3; HCI fragmentation: LL.TS Section 4.1.7.
    """
    link = await _encrypted_link(tester, False)
    generator = random.Random(104)
    for _ in range(RECEIVE_PACKET_COUNT):
        data = generator.randbytes(RECEIVE_DATA_SIZE)
        packet = await link.exchange(data, llid=LlId.START, check_ack=True)
        assert packet.pdu[PDU_HEADER_SIZE:] == b''
        await received_acl(tester, link, data, continuation=False)
    assert not any(isinstance(event, hci.HCI_AclDataPacket) for event in tester.events)
    _no_disconnect(tester)
    await link.disconnect()


async def initiate_le_ping_procedure_when_encryption_is_enabled(tester):
    """Initiate LE Ping procedure when encryption is enabled.

    Initial Condition:
        Connected Peripheral, any advertising interval, supported own-address
        type, any advertising channel map, common connection interval, selected
        supervision timeout and any SCA. The connection is encrypted and idle:
        no background LE-U or LE-C traffic.
        Select public addresses, 10-ms interval and 300-ms supervision timeout.
        Exchange LE Encryption/LE Ping features and establish encryption with
        the common, independently verified key preamble. Read back the unchanged
        default authenticated payload timeout of 30 seconds.
        A preliminary valid-MIC DATA packet starts observation. Acquire one
        ping/response exchange, then verify the next request relative to that
        response, as required by Step 1.

    Test Steps:
        1. Receive LL_PING_REQ transmitted less than 30 seconds after the IUT
           received the preceding LL_PING_RSP.
        2. Respond with encrypted LL_PING_RSP; only empty, MIC-less DATA packets
           maintain the connection between ping exchanges.

    Expected Outcome:
        Pass verdict:
            The IUT transmits LL_PING_REQ before the 30-second authenticated
            payload timeout expires, triggering the Lower Tester's reply.
            Do not impose the Modified Timeout case's stronger completed-reply
            deadline on this case.

    Notes:
        The Lower Tester must avoid incidental MIC-bearing packets during the
        idle interval. If one is transmitted, start another full 30-second
        observation from it; this peer sends only the intended encrypted reply.
        The reference timestamp is the PHY receive grant for the last sample.
        Packet end bounds are half-open and are converted to that grant time.

    Postamble:
        Keep the ACL established through both exchanges, then terminate it.

    Reference:
        LL.TS.p28, Section 4.8.6, LL/SEC/PER/BV-06-C; PDF 1251-1252,
        printed 4.8-64..65; Figure 4.8-32. Core reference [3],
        Vol 6 Part B Section 5.1.8 and Section 5.4.
        Common key preamble: Section 4.1.6.6, with the Figure 4.1-27 operand
        correction documented in Peripheral Encryption Mode Setup.
    """
    await _ping(tester, central=False, mode='default')


async def responding_to_ll_ping_req(tester):
    """Responding to LL_PING_REQ.

    Initial Condition:
        Connected Peripheral, any advertising interval, supported own-address
        type, any advertising channel map, common interval, selected supervision
        timeout and any SCA. Encryption is enabled, with no background LE-U or
        LE-C traffic. Select public addresses, 10-ms interval and 300-ms
        supervision timeout. Complete LE Encryption/LE Ping feature exchange
        and the independently checked encryption handshake before Step 1.

    Test Steps:
        1. Send encrypted LL_PING_REQ from the Central Lower Tester.
        2. Receive the IUT's encrypted LL_PING_RSP.

    Expected Outcome:
        Pass verdict:
            Respond with LL_PING_RSP. Verify the exact opcode and independent
            AES-CCM authentication, not merely reception of any control packet.

    Postamble:
        Terminate the still-established connection and check HCI completion.

    Reference:
        LL.TS.p28, Section 4.8.6, LL/SEC/PER/BV-07-C; PDF 1252,
        printed 4.8-65; Figure 4.8-33. Core reference [3],
        Vol 6 Part B Section 5.1.8.
        Common key preamble: Section 4.1.6.6; Figure 4.1-27 operand correction
        is documented in Peripheral Encryption Mode Setup.
    """
    await _ping(tester, central=False, mode='respond')


async def no_response_to_ll_ping_req(tester):
    """No response to LL_PING_REQ.

    Initial Condition:
        Connected Peripheral, any advertising interval, supported own-address
        type and advertising channel map, common connection interval, selected
        supervision timeout and any SCA. Encryption is enabled.
        Select public addresses, 10-ms interval, 300-ms supervision timeout,
        LE Encryption/LE Ping feature exchange and a verified encrypted link.
        The authenticated payload timeout IXIT is one second (100 units of 10 ms).

    Test Steps:
        1. Write the authenticated payload timeout IXIT through HCI; require
           successful completion on this handle and verify the readback value.
        2. Unmask HCI Authenticated Payload Timeout Expired through event-mask
           page 2.
        3. Send one DATA packet with valid MIC and record the corresponding
           PHY receive-grant time; verify delivery to the Host.
        4. Keep the link idle without LE-U or LE-C traffic; maintain only
           empty DATA acknowledgements, which carry no MIC.
        5. Receive the IUT's LL_PING_REQ.
        6. Do not send LL_PING_RSP or another MIC-bearing packet.
        7. At the authenticated timeout measured from Step 3, receive HCI
           Authenticated Payload Timeout Expired for this connection handle.

    Expected Outcome:
        Pass verdict:
            Transmit LL_PING_REQ and notify the Host of authenticated-payload
            expiry when the Lower Tester withholds the response.
            At the last representable PHY time before the deadline, no expiry
            event may have been observed; after reaching the deadline, require
            the event with the correct handle.

    Observation:
        Use the PHY grant time, not the half-open packet end or an invented
        one-microsecond tolerance. Stop RF exchanges before the boundary and
        synchronize exactly before/at it. The HCI event reader's local delivery
        watchdog is not a normative transport-latency deadline; neither Core
        nor this case supplies one. Preserve missed event counters and anchor
        phase when returning to the connection postamble.

    Postamble:
        Verify that the ACL remains established and terminate it.

    Reference:
        LL.TS.p28, Section 4.8.6, LL/SEC/PER/BV-08-C; PDF 1252-1253,
        printed 4.8-65..66; Figure 4.8-34. Core reference [3],
        Vol 6 Part B Sections 5.1.8 and 5.4; HCI event: Vol 4 Part E
        Section 7.7.75. Common key preamble: Section 4.1.6.6, with the
        Figure 4.1-27 operand correction in Peripheral Encryption Mode Setup.
    """
    await _ping(tester, central=False, mode='expire')


async def modified_authentication_payload_timeout(tester):
    """Modified Authentication Payload Timeout.

    Initial Condition:
        Connected Peripheral, any advertising interval, supported own-address
        type and advertising channel map, common connection interval, selected
        supervision timeout and any SCA.
        Select public addresses, 10-ms interval and 300-ms supervision timeout.
        Complete LE Encryption/LE Ping feature exchange and a verified encrypted
        connection, as required for the MIC-bearing packets in the procedure.
        Select an authenticated payload timeout IXIT of one second.

    Test Steps:
        1. Write the authenticated payload timeout IXIT less than five seconds
           through HCI, checking completion handle and readback.
        2. Send one valid-MIC DATA packet and record its PHY reception time.
        3. Keep the link idle without background LE-U or LE-C traffic for an
           overall observation longer than the selected timeout. Execute
           Steps 4-5 within that continuing idle interval, not after first
           waiting past the authenticated deadline.
        4. Receive LL_PING_REQ transmitted before the currently active
           authenticated payload timeout.
        5. Respond with encrypted LL_PING_RSP. The interval between reception
           of the two MIC-bearing Lower Tester packets must not exceed the
           configured timeout. Repeat observation as needed to satisfy Step 3.

    Expected Outcome:
        Pass verdict:
            Transmit LL_PING_REQ and receive the resulting LL_PING_RSP within
            the configured authenticated payload timeout measured from the
            previous valid-MIC packet. Unlike the default/unsupported-peer
            cases, this verdict explicitly bounds completed response reception.

    Notes:
        Avoid incidental MIC-bearing packets; if one occurs, restart the
        observation from it. Empty DATA maintains the link without resetting
        the authenticated timer.

    Source Correction:
        Step 3's idle interval greater than the timeout cannot be a completed
        sequential delay before Steps 4-5, which require a request and response
        within that timeout. Interpret it as the ongoing background-idle
        observation and interleave the ping exchanges. Amber supports this
        Core-consistent reading; it is local, not adopted SIG errata.

    Postamble:
        Keep the connection established, then terminate it.

    Reference:
        LL.TS.p28, Section 4.8.6, LL/SEC/PER/BV-09-C; PDF 1253-1254,
        printed 4.8-66..67; Figure 4.8-35. Core reference [3],
        Vol 6 Part B Sections 5.1.8 and 5.4. Common key preamble:
        Section 4.1.6.6; its operand correction is documented in
        Peripheral Encryption Mode Setup.
    """
    await _ping(tester, central=False, mode='modified')


async def initiate_le_ping_procedure_when_the_other_side_does_not_support_the_procedure(
    tester,
):
    """Initiate LE Ping procedure when the other side does not support the procedure.

    Initial Condition:
        Connected Peripheral, any advertising interval, supported own-address
        type and advertising channel map, common connection interval, selected
        supervision timeout and any SCA. Encryption is enabled and the link is
        idle, without background LE-U or LE-C traffic.
        Select public addresses, 10-ms interval and 300-ms supervision timeout.
        Exchange LE Encryption support with LE Ping clear in the peer features;
        establish encryption and read back the unchanged 30-second timeout.
        Send preliminary valid-MIC data and acquire one ping/unknown-response
        exchange before checking the subsequent request.

    Test Steps:
        1. Receive LL_PING_REQ transmitted less than 30 seconds after the
           preceding LL_UNKNOWN_RSP was received by the IUT.
        2. Send encrypted LL_UNKNOWN_RSP with UnknownType=LL_PING_REQ.
           Empty, MIC-less DATA maintains the connection between exchanges.

    Expected Outcome:
        Pass verdict:
            Transmit LL_PING_REQ before the default authenticated payload
            timeout, triggering the Lower Tester's LL_UNKNOWN_RSP.
            The completed-reply deadline belongs to Modified Timeout, not
            this case's request-transmission verdict.

    Notes:
        Avoid incidental MIC-bearing traffic; if it occurs, observe another
        full 30 seconds from that packet. The intended unknown response itself
        contains a valid MIC and becomes the next interval's reference point.

    Source Correction:
        The final verdict names LL_PING_RSP, although the purpose, initial
        condition and Step 2 require an unsupported peer's LL_UNKNOWN_RSP.
        Use LL_UNKNOWN_RSP(PING_REQ): Core Vol 6 Part B Section 5.1.8 allows
        Ping to an unsupporting peer, and Sections 2.4.2 and 5 define that
        LL_UNKNOWN_RSP reply (Part D Section 6.13 is informative). Amber
        confirms this local correction; it is not adopted SIG errata.

    Postamble:
        Maintain the connection through both exchanges, then terminate it.

    Reference:
        LL.TS.p28, Section 4.8.6, LL/SEC/PER/BV-10-C; PDF 1255-1256,
        printed 4.8-68..69; Figure 4.8-36. Core reference [3],
        Vol 6 Part B Section 5.1.8. Common key preamble: Section 4.1.6.6,
        with the Figure 4.1-27 operand correction in Peripheral Encryption
        Mode Setup.
    """
    await _ping(tester, central=False, mode='unknown')


async def peripheral_sending_ll_reject_ext_ind(tester):
    """Peripheral Sending LL_REJECT_EXT_IND.

    Initial Condition:
        Connected Peripheral with any advertising interval, supported own-address
        type, advertising channel map and SCA; common connection interval and
        selected timeout. Select a private IUT address, public Lower Tester
        address, 10-ms interval, zero latency and 300-ms timeout. Use common IR/ER
        0x112233445566778899AABBCCDDEEFF00 and the common key preamble.

    Test Steps:
        1. Establish the connection using public Lower Tester address and a
           supported IUT address. The Lower Tester initiates Feature Exchange,
           declaring LE Encryption and Extended Reject Indication support.
        2. Send LL_ENC_REQ containing Rand, EDIV, SKDc and IVc; receive a correctly
           sized LL_ENC_RSP and continue Central transmissions.
        3. Receive HCI LE Long Term Key Request with the same connection handle,
           Rand and EDIV as Step 2.
        4. Send HCI LE Long Term Key Request Negative Reply and receive successful
           Command Complete for the same handle.
        5. Receive LL_REJECT_IND or LL_REJECT_EXT_IND with PIN or Key Missing.
           For LL_REJECT_EXT_IND, also require RejectOpcode = LL_ENC_REQ.

    Postamble:
        Acknowledge the rejection, verify unencrypted ACL remains usable, and
        terminate the connection. This is not an additional source step.

    Expected Outcome:
        Pass verdict:
            Receive LL_ENC_RSP, successful negative-LTK Command Complete, and
            either permitted rejection form with the correct reason and fields.

    Source Correction:
        Step 3 refers to its own random-number/EDIV parameters; these were sent
        in Step 2. Step 5 and the verdict mention RejectOpcode for both forms,
        but only LL_REJECT_EXT_IND contains that field. Accept either form as
        expressly permitted by the purpose, Step 5 and verdict, despite the
        extended-only title. Amber confirms Core Vol 6 Part B Section 5.1.3.1;
        this interpretation is not an adopted SIG erratum.

    Reference:
        LL.TS.p28, Section 4.8.6.1, LL/SEC/PER/BV-11-C; PDF 1256-1257,
        printed 4.8-69..70; Figure 4.8-37. Core reference [3], Vol 6 Part B
        Section 5.1.3.1. Common keys: Section 4.1.6.6, with the Figure 4.1-27
        operand correction documented in Peripheral Encryption Mode Setup.
    """
    await _reject(tester, central=False, extended=True)


async def peripheral_encryption_setup_missing_response(tester):
    """Peripheral Encryption Setup: Missing Response.

    Initial Condition:
        Connected Peripheral using a private address, any advertising interval,
        advertising channel map and SCA, common interval and timeout. Select
        a resolvable private IUT address, public Lower Tester address, 10-ms
        interval, zero latency and 300-ms timeout. Use common IR/ER and keys.

    Test Steps:
        1. Establish the connection using public Lower Tester address and the
           supported private IUT address required by the initial condition.
        2. Calculate SKDc, IVc and Rand using the reproducible Lower Tester PRNG
           and the Rand/EDIV retained by the common key preamble.
        3. Send LL_ENC_REQ containing Rand, EDIV, SKDc and IVc; receive a correctly
           sized LL_ENC_RSP and continue Central transmissions.
        4. Receive HCI LE Long Term Key Request with the same handle, Rand and
           EDIV as Step 3.
        5. Reply through HCI with the calculated LTK and require successful
           Command Complete for the same handle.
        6. Receive LL_START_ENC_REQ and acknowledge it using the next NESN.
        7. Continue Central empty DATA transmissions without LL_START_ENC_RSP;
           acknowledge Peripheral packets to avoid supervision termination.
        8. Verify that Peripheral transmissions stop on the LL response timeout.
           The last reset occurs when LL_START_ENC_REQ is queued: bracket it
           between Host LTK Reply submission and its observed air transmission.
           Require no premature termination before the earlier bound plus 40 s
           and no RF transmission after the later bound plus 40 s. Continue
           Central probes and inspect all channels through one supervision
           timeout after that upper bound.
        9. Interleave with Step 8: receive successful HCI Disconnection Complete
           for the same handle with LL Response Timeout (0x22). Do not require
           HCI delivery to coincide with the RF boundary.

    Expected Outcome:
        Pass verdict:
            The Peripheral stops maintaining the connection when the procedure
            response timer expires and reports termination to its Host. Empty
            DATA traffic must not turn this into a supervision timeout or reset
            the LL procedure timeout.

    Observation Boundary:
        Core Section 5.2 resets T_PRT when an LL Control PDU is queued, not at a
        specified air-bit boundary. Amber confirms the observable reset bracket
        above. It does not justify using the initial ENC_REQ as the final timer
        origin, or inventing an HCI delivery-latency allowance.

    Reference:
        LL.TS.p28, Section 4.8.6.1, LL/SEC/PER/BI-01-C; PDF 1257-1259,
        printed 4.8-70..72; Figure 4.8-38. Core reference [3], Vol 6 Part B
        Section 5.1.3; timer definition: Core 6.3 Vol 6 Part B Section 5.2.
        Common key preamble: Section 4.1.6.6, with the Figure 4.1-27 operand
        correction documented in Peripheral Encryption Mode Setup.
    """
    await _security_timeout(tester, central=False, stage='final', data=False)


async def peripheral_mic_failure_corrupted_mic(tester, *, header_error):
    """Peripheral MIC Failure: Corrupted MIC.

    Initial Condition:
        Encrypted Peripheral connection using common IR/ER, connection interval
        and timeout; any advertising interval, supported own-address type,
        advertising channel map and SCA. Select public addresses, 10-ms interval,
        zero latency and 300-ms timeout. Table 4.8-9 selects MIC corruption for
        BI-03-C and authenticated-header corruption for BI-04-C.

    Test Case Configuration:
        LL/SEC/PER/BI-03-C: corrupted MIC.
        LL/SEC/PER/BI-04-C: corrupted header.

    Test Steps:
        1. Complete encrypted connection setup with the common connection and
           encryption-key variables. Exchange valid data in both directions.
        2. Execute the alternative selected by Table 4.8-9:
           2A.1. BI-03-C: encrypt a ten-octet DATA payload, then flip exactly one
                 MIC bit. Recompute the correct CRC for the invalid ciphertext.
           2B.1. BI-04-C: encrypt a ten-octet DATA payload, then flip exactly one
                 authenticated LLID header bit. Recompute the correct CRC for
                 the altered header and unchanged ciphertext/MIC.
        3. Receive successful HCI Disconnection Complete for the same connection
           handle with MIC Failure (0x3D).
        4. Repeat Step 2 through one full supervision timeout with no response.
           Retransmit the corrupted packet on the connection's event channels,
           retaining event phase and wrapping its event counter. Observe the
           complete final interval, including gaps between receive windows.

    Expected Outcome:
        Pass verdict:
            The IUT terminates after attempting to authenticate the corrupted
            packet, reports termination through HCI, and does not deliver any
            corrupted payload to its Host. No connection transmission is present
            in the all-channel PHY trace during the full observation interval.

    Reference:
        LL.TS.p28, Section 4.8.6.2, LL/SEC/PER/BI-03-C and LL/SEC/PER/BI-04-C;
        PDF 1259-1260, printed 4.8-72..73; Table 4.8-9 and Figure 4.8-39.
        Core reference [3], Vol 6 Part B Sections 3.1 and 5.1.3. Common key
        preamble: Section 4.1.6.6, with the Figure 4.1-27 operand correction
        documented in Peripheral Encryption Mode Setup.
    """
    await _mic_failure(tester, central=False, header_error=header_error)


async def peripheral_receiving_unexpected_pdu_during_encryption_start(tester, *, data):
    """Peripheral Receiving unexpected PDU during encryption start.

    Initial Condition:
        Connected Peripheral with any advertising interval, supported own-address
        type, advertising channel map and Worst_Case_SCA; common connection
        interval and timeout, common IR/ER and encryption keys. Select public
        addresses, 10-ms interval, zero latency and 300-ms timeout. Table 4.8-10
        selects LL_VERSION_IND for BI-05-C and unencrypted DATA for BI-07-C.

    Test Case Configuration:
        LL/SEC/PER/BI-05-C: LL_VERSION_IND.
        LL/SEC/PER/BI-07-C: unencrypted Data PDU.

    Test Steps:
        1. Establish the connection using public Lower Tester address and a
           supported IUT address type.
        2. Calculate SKDc, IVc and Rand from the reproducible Lower Tester PRNG
           and the common key preamble's Rand/EDIV.
        3. Send LL_ENC_REQ containing Rand, EDIV, SKDc and IVc. Set MD so that
           the Central can continue in the same connection event.
        4. Immediately follow with the Table 4.8-10 PDU, in the same event if
           possible. After the IUT acknowledges ENC_REQ, send the unexpected
           plaintext PDU at T_IFS; do not wait for a Host LTK request or reply.
        5. [Optional] Accept LL_ENC_RSP if sent by the IUT. An empty ACK is also
           sufficient to continue the exchange; ENC_RSP is not a prerequisite.
        6. Verify that the IUT terminates rather than normally responding to the
           unexpected packet. Do not treat optional ENC_RSP as a normal reply
           to LL_VERSION_IND or as an encryption-completion requirement.
        7. Receive successful HCI Disconnection Complete for the same handle
           with Connection Terminated Due to MIC Failure (0x3D).

    Expected Outcome:
        Pass verdict:
            The IUT terminates on the unexpected PDU. It does not send a normal
            procedure response or deliver the unencrypted DATA to its Host.

    Reference:
        LL.TS.p28, Section 4.8.6.3, LL/SEC/PER/BI-05-C and LL/SEC/PER/BI-07-C;
        PDF 1260-1261, printed 4.8-73..74; Table 4.8-10 and Figure 4.8-40.
        Core reference [3], Vol 6 Part B Section 5.1.3.1. Common keys:
        Section 4.1.6.6, with the Figure 4.1-27 operand correction documented
        in Peripheral Encryption Mode Setup.
    """
    await _peripheral_unexpected_start_pdu(tester, data=data)


async def central_encryption_mode_setup(tester):
    """Central Encryption Mode Setup.

    Initial Condition:
        Encryption Keys Calculated using common IR/ER, and Connected Central
        with any scan interval/window, selected peer and own-address types,
        Lower Tester address, common connection parameters, Peripheral latency
        and timeout. Select 10-ms interval, zero latency and 300-ms timeout.
        Use common IR/ER 0x112233445566778899AABBCCDDEEFF00. Calculate DHK,
        Rand, EDIV, DIV and LTK through the common key preamble, independently
        verifying AES and DIV recovery. Complete LE Encryption Feature Exchange.

    Test Steps:
        1. Establish the connection using public Lower Tester address and a
           supported IUT address; select public IUT address for this round.
        2. Submit ten-octet zero-filled HCI ACL packets with the connection handle
           and a packet-start PB flag until 1000 octets are successfully submitted.
        3. Receive the corresponding DATA packets and respond with empty DATA
           until all Step 2 octets are received. Interleave Steps 2/3 to respect
           HCI buffer credits while preserving all bytes and packet boundaries.
        4. Calculate the Peripheral SKD and IV components using the reproducible
           Lower Tester PRNG and retain the common key preamble's Rand.
        5. Send HCI LE Enable Encryption with the connection handle, Rand,
           EDIV and LTK; receive successful Command Status.
        6. Receive LL_ENC_REQ, verify Rand/EDIV against Step 5 and obtain the
           Central SKD/IV components. Acknowledge it and send LL_ENC_RSP with
           the Peripheral components.
        7. Combine SKD/IV halves in Central/Peripheral order. Recover DIV using
           DHK and received Rand/EDIV, derive LTK from ER and compare it with
           the preamble LTK; independently derive the session key from SKD.
        8. Receive empty DATA from the Central and respond with LL_START_ENC_REQ.
        9. Receive and authenticate encrypted LL_START_ENC_RSP; respond with
           encrypted LL_START_ENC_RSP using the correct direction and counter.
        10. Receive successful HCI Encryption Change for the same handle with
            encryption enabled.
        11. Submit another 1000 zero octets as ten-octet HCI ACL packets with
            the same handle and a packet-start PB flag.
        12. Receive all Step 11 data encrypted, verify each MIC and plaintext,
            and respond with empty DATA until all 1000 octets are received.
        13. Terminate the actually encrypted Central connection and verify HCI
            disconnection for the same handle.
        14. Establish another connection with random Lower Tester and IUT
            addresses; calculate the IUT RPA from the common IR.
        15. Repeat Steps 2-13 on that connection.

    Expected Outcome:
        Pass verdict:
            Both address rounds transfer all plaintext and encrypted data
            correctly. The IUT supplies its IV/SKD halves in LL_ENC_REQ, sends
            encrypted LL_START_ENC_RSP until acknowledged, and reports encryption
            setup through HCI. Independent AES-CCM checks enforce the session
            key, IV, counters, direction, encrypted control payload and DATA MICs.

    Source Correction:
        Step 12 refers to data sent in Step 2, but the post-encryption submission
        is Step 11. Step 13's un-encrypted annotation is stale: no pause/disable
        operation follows encrypted Step 12. Terminate the encrypted connection
        without inventing a pause. For the common preamble, use ER as the AES
        key and padded DIV as plaintext, LTK=d1(ER,DIV,0), rather than the swapped
        operands printed in Figure 4.1-27. Amber supports these Core-backed local
        corrections; they are not claimed to be adopted SIG errata.

    Reference:
        LL.TS.p28, Section 4.8.7, LL/SEC/CEN/BV-01-C; PDF 1261-1263,
        printed 4.8-74..76; Figure 4.8-41. Core reference [3], Vol 6 Part B
        Section 5.1.3. Common key preamble: Section 4.1.6.6, PDF 76-77,
        printed 4.1-28..29; Figures 4.1-26/27. Key derivation: Core Vol 3
        Part H Appendix B.2.1/B.2.2.
    """
    await _mode_setup(tester, central=True)


async def central_pause_encryption(tester, *, send_data):
    """Central Pause Encryption.

    Initial Condition:
        Encryption Keys Calculated using common IR/ER, and Connected Central
        with any scan interval/window, selected peer and own-address types,
        Lower Tester address, common interval, Peripheral latency and timeout.
        Establish encryption through the common preamble. Select public
        addresses, 10-ms interval, zero latency and 300-ms timeout; retain the
        independently derived key and IV. Table 4.8-11 selects no data for
        LL/SEC/CEN/BV-02-C and data for LL/SEC/CEN/BV-05-C.

    Test Case Configuration:
        LL/SEC/CEN/BV-02-C: no data variant.
        LL/SEC/CEN/BV-05-C: data before LL_PAUSE_ENC_RSP.

    Test Steps:
        1. Send HCI LE Enable Encryption for the existing encrypted handle and
           require successful Command Status.
        2. Receive and authenticate encrypted LL_PAUSE_ENC_REQ.
        3. If Table 4.8-11 selects data:
           3A. Send encrypted DATA containing 0xFF.
           3B. Receive the corresponding HCI ACL data with the same handle,
               correct packet-start boundary and exact payload. Require no
               premature encryption result. Maintain empty traffic during the
               Figure 4.8-42 two-second observation guard, without termination.
        4. Send encrypted LL_PAUSE_ENC_RSP.
        5. Receive the Central's unencrypted LL_PAUSE_ENC_RSP; encryption is now
           paused in both directions.
        6. Receive LL_ENC_REQ to re-enable encryption and send LL_ENC_RSP.
           Check Rand/EDIV, independently derive the replacement session key/IV
           and verify that they differ from the previous session. No user data
           may escape during the unencrypted restart.
        7. Respond to Central transmissions with LL_START_ENC_REQ and verify
           its acknowledgement.
        8. Receive and authenticate encrypted LL_START_ENC_RSP; respond with
           encrypted LL_START_ENC_RSP. A refresh result must not precede this
           required final handshake.
        9. Execute the Table 4.8-11 alternative:
           9A.1. No data: receive successful HCI Encryption Key Refresh Complete
                 for the same handle after encryption resumes.
           9A.2. Terminate the actually encrypted Central connection and verify
                 the matching HCI disconnection.
           9B.1. Data: receive successful HCI Encryption Key Refresh Complete
                 for the same handle after encryption resumes.
           9B.2. Send encrypted DATA containing 0x11.
           9B.3. Receive the corresponding HCI ACL data with correct handle,
                 packet boundary and exact payload. No LL_TERMINATE_IND or HCI
                 Disconnection Complete may occur before the postamble.

    Postamble:
        For the data variant, terminate only after completing its no-termination
        verdict. This cleanup is not an extra numbered source step.

    Expected Outcome:
        Pass verdict:
            Enable Encryption starts pause/restart and the IUT sends LL_ENC_REQ.
            The matching successful Refresh Complete follows resumed encryption.
            For the data variant, HCI delivery of 0xFF precedes completion and
            delivery of 0x11 follows it, without connection termination.

    Source Correction:
        The verdict's Encryption Change references mean Key Refresh Complete,
        as the numbered steps and MSC specify. Core Vol 4 Part E Sections
        7.7.8, 7.7.39 and 7.8.24 distinguish initial encryption from pause/resume.
        The 9B.2/9B.3 wording describes Lower Tester RF transmission followed by
        Upper Tester HCI reception, not an extra IUT RF transmission. Step
        9A.2's un-encrypted annotation is stale after a successful restart;
        terminate the encrypted connection without inventing another pause.
        Amber supports these logical corrections; they are not adopted errata.
        The MSC's two-second guard is retained as an observation window, not a
        Core-mandated encryption timeout.

    Reference:
        LL.TS.p28, Section 4.8.7.1, LL/SEC/CEN/BV-02-C and LL/SEC/CEN/BV-05-C;
        PDF 1264-1266, printed 4.8-77..79; Table 4.8-11 and Figure 4.8-42.
        Core reference [3], Vol 6 Part B Section 5.1.3.2. Common key preamble:
        Section 4.1.6.6, with the Figure 4.1-27 operand correction documented
        in Central Encryption Mode Setup.
    """
    await _pause_procedure(tester, central=True, send_data=send_data)


async def central_receiving_ll_reject_ind(tester):
    """Central Receiving LL_REJECT_IND.

    Initial Condition:
        Connected Central using a private address, common connection interval
        and timeout, any advertising interval/channel map and SCA. Use common
        IR/ER and key variables. Select a resolvable private IUT address, public
        Lower Tester address, 10-ms interval, zero latency and 300-ms timeout.
        The feature preamble leaves Extended Reject Indication support clear.

    Test Steps:
        1. Establish the connection using public Lower Tester address and a
           supported private IUT address as required by the initial condition.
        2. Send HCI LE Enable Encryption with the common key parameters and
           require successful Command Status.
        3. Receive LL_ENC_REQ containing matching Rand/EDIV and the Central
           SKD/IV components. Check its exact size and independently recover
           the preamble LTK from the received parameters.
        4. Send LL_REJECT_IND with Unsupported Remote Feature (0x1A), without
           first sending LL_ENC_RSP.
        5. Receive HCI Encryption Change for the same handle with Unsupported
           Remote Feature status and encryption disabled.

    Postamble:
        Verify the unencrypted ACL remains usable and terminate the connection.
        This is not an extra numbered source step.

    Expected Outcome:
        Pass verdict:
            The received LL_ENC_REQ contains the requested Rand/EDIV, and the
            Upper Tester receives matching HCI Encryption Change with
            Unsupported Remote Feature status.

    Reference:
        LL.TS.p28, Section 4.8.7, LL/SEC/CEN/BV-03-C; PDF 1266-1267,
        printed 4.8-79..80; Figure 4.8-43. Core reference [3], Vol 6 Part B
        Section 5.1.3.1. Common key preamble: Section 4.1.6.6, with the
        Figure 4.1-27 operand correction documented in Central Encryption
        Mode Setup.
    """
    await _reject(tester, central=True, extended=False)


async def central_encryption_sending_data_before_ll_enc_rsp(tester):
    """Central Encryption: Sending Data before LL_ENC_RSP.

    Initial Condition:
        Encryption Keys Calculated using common IR/ER, and Connected Central
        with public IUT address, selected peer-address type and Lower Tester
        address, any scan interval/window, common connection interval,
        Peripheral latency and timeout. Select public Lower Tester address,
        10-ms interval, zero latency and 300-ms timeout. Independently verify
        the common key calculations before enabling encryption.

    Test Steps:
        1. Send HCI LE Enable Encryption with the connection handle, Rand,
           EDIV and LTK from the common preamble. Receive successful Command
           Status.
        2. Receive LL_ENC_REQ containing the matching parameters from Step 1
           and the Central SKD/IV components.
        3. Send unencrypted DATA containing 0xFF before sending LL_ENC_RSP.
        4. Receive the corresponding HCI ACL data with the same handle, correct
           packet-start boundary and exact payload. No encryption result or
           connection termination may have occurred.
        5. Send LL_ENC_RSP containing the Peripheral SKD/IV components and
           independently derive the session key and IV.
        6. Respond to the Central's empty DATA with LL_START_ENC_REQ and verify
           its acknowledgement.
        7. Receive and authenticate encrypted LL_START_ENC_RSP; respond with
           encrypted LL_START_ENC_RSP.
        8. Receive successful HCI Encryption Change for the same handle with
           encryption enabled.
        9. Send encrypted DATA containing 0x11.
        10. Receive the corresponding HCI ACL data with the same handle, correct
            packet boundary and exact payload.

    Postamble:
        Verify the no-termination verdict before explicitly disconnecting.
        This cleanup is not an additional source step.

    Expected Outcome:
        Pass verdict:
            The Host receives 0xFF before successful Encryption Change and 0x11
            after that event. Encrypted traffic has valid MICs and no premature
            result event, LL_TERMINATE_IND or HCI Disconnection Complete occurs.

    Reference:
        LL.TS.p28, Section 4.8.7, LL/SEC/CEN/BV-04-C; PDF 1268-1270,
        printed 4.8-81..83; Figure 4.8-44. Core reference [3], Vol 6 Part B
        Section 5.1.3. Common key preamble: Section 4.1.6.6, with the
        Figure 4.1-27 operand correction documented in Central Encryption
        Mode Setup.
    """
    link = await establish_security_link(tester, True)
    await begin_encryption(tester, link)
    await peer_send(tester, link, b'\xff')
    expect_no_encryption_result(tester)
    _no_disconnect(tester, link)
    await finish_encryption(tester, link)
    await peer_send(tester, link, b'\x11')
    _no_disconnect(tester, link)
    expect_no_encryption_result(tester)
    await link.disconnect()


async def initiate_le_ping_procedure_when_encryption_is_enabled_ll_sec_cen_bv_06_c(
    tester,
):
    """Initiate LE Ping procedure when encryption is enabled.

    Initial Condition:
        Encrypted Connected Central, any advertising interval/channel map,
        supported own-address type, common interval, selected supervision timeout
        and any SCA.
        The link is idle, with no background LE-U or LE-C traffic. Select public
        addresses, 10-ms interval, zero latency and 300-ms supervision timeout.
        Complete LE Encryption/LE Ping Feature Exchange and common-key setup.
        Read back the unchanged default authenticated payload timeout of 30 s.
        A preliminary valid-MIC DATA packet starts observation. Acquire one
        ping/response exchange, then measure the next request from that response.

    Test Steps:
        1. Receive LL_PING_REQ transmitted less than 30 s after the IUT received
           the preceding LL_PING_RSP.
        2. Respond with encrypted LL_PING_RSP; maintain only empty, MIC-less
           DATA acknowledgements between ping exchanges.

    Expected Outcome:
        Pass verdict:
            The request triggers a reply before the default authenticated-payload
            timeout expires. This case bounds the request, not the completed
            reply as in the Modified Timeout case.

    Notes:
        Avoid incidental MIC-bearing packets during idle observation. If one is
        sent, restart the full 30-second observation from it. This peer sends
        only the intended encrypted response, timestamped at its PHY RX grant.

    Source Correction:
        The purpose calls the Lower Tester Central, but the IUT is Central.
        The Lower Tester is Peripheral, as required by the connection roles.

    Postamble:
        Verify the connection remains established and terminate it.

    Reference:
        LL.TS.p28, Section 4.8.7, LL/SEC/CEN/BV-06-C; PDF 1270-1271,
        printed 4.8-83..84; Figure 4.8-45. Core reference [3], Vol 6 Part B
        Section 5.1.8. Common keys: Section 4.1.6.6, with the Figure 4.1-27
        operand correction documented in Central Encryption Mode Setup.
    """
    await _ping(tester, central=True, mode='default')


async def responding_to_ll_ping_req_ll_sec_cen_bv_07_c(tester):
    """Responding to LL_PING_REQ.

    Initial Condition:
        Encrypted Connected Central with any advertising interval/channel map,
        supported own-address type, common interval, selected supervision timeout
        and any SCA. No background LE-U or LE-C traffic. Select public addresses,
        10-ms interval, zero latency and 300-ms supervision timeout. Complete
        LE Encryption/LE Ping Feature Exchange and independently checked setup.

    Test Steps:
        1. Send encrypted LL_PING_REQ from the Peripheral Lower Tester.
        2. Receive the IUT's encrypted LL_PING_RSP.

    Expected Outcome:
        Pass verdict:
            Respond with LL_PING_RSP. Check the exact opcode and independently
            authenticate its AES-CCM payload, not merely any control response.

    Postamble:
        Terminate the still-established connection and check HCI completion.

    Reference:
        LL.TS.p28, Section 4.8.7, LL/SEC/CEN/BV-07-C; PDF 1271-1272,
        printed 4.8-84..85; Figure 4.8-46. Core reference [3], Vol 6 Part B
        Section 5.1.8. Common keys: Section 4.1.6.6, with the Figure 4.1-27
        operand correction documented in Central Encryption Mode Setup.
    """
    await _ping(tester, central=True, mode='respond')


async def no_response_to_ll_ping_req_ll_sec_cen_bv_08_c(tester):
    """No response to LL_PING_REQ.

    Initial Condition:
        Encrypted Connected Central with any advertising interval/channel map,
        supported own-address type, common interval, selected supervision timeout
        and any SCA. Select public addresses, 10-ms interval, zero latency and
        300-ms supervision timeout. Complete LE Encryption/LE Ping Feature
        Exchange and common-key setup. The authenticated timeout IXIT is one
        second, expressed as 100 units of 10 ms.

    Test Steps:
        1. Set the authenticated payload timeout IXIT through HCI; verify
           successful completion, matching handle and readback value.
        2. Unmask HCI Authenticated Payload Timeout Expired using event-mask
           page 2.
        3. Send DATA containing a valid MIC, record the IUT's PHY receive-grant
           time and verify Host delivery.
        4. Keep the connection idle, with no LE-U or LE-C traffic; maintain only
           empty acknowledgements without a MIC.
        5. Receive the IUT's LL_PING_REQ.
        6. Do not respond with LL_PING_RSP or another MIC-bearing packet.
        7. Receive HCI Authenticated Payload Timeout Expired for the correct
           handle at the configured interval after the last valid-MIC packet.

    Expected Outcome:
        Pass verdict:
            Send LL_PING_REQ and notify the Host of authenticated-payload expiry
            when the response is withheld. No expiry may be observed at the last
            representable PHY time before the deadline; after reaching it,
            require the event for this connection handle.

    Observation:
        Use the last-sample PHY receive grant, not a half-open packet end plus
        an invented tolerance. Stop RF exchanges before the boundary, synchronize
        before/at it, and preserve event phase afterwards. The HCI reader's local
        watchdog is not a normative transport-latency requirement.

    Postamble:
        Verify the connection remains established and terminate it.

    Reference:
        LL.TS.p28, Section 4.8.7, LL/SEC/CEN/BV-08-C; PDF 1272-1273,
        printed 4.8-85..86; Figure 4.8-47. Core reference [3], Vol 6 Part B
        Sections 5.1.8 and 5.4; HCI event: Vol 4 Part E Section 7.7.75.
        Common keys: Section 4.1.6.6, with the Figure 4.1-27 operand correction
        documented in Central Encryption Mode Setup.
    """
    await _ping(tester, central=True, mode='expire')


async def modified_authentication_payload_timeout_ll_sec_cen_bv_09_c(tester):
    """Modified Authentication Payload Timeout.

    Initial Condition:
        Connected Central with any advertising interval/channel map, supported
        own-address type, common interval, selected supervision timeout and any SCA.
        Select public addresses, 10-ms interval, zero latency and 300-ms
        supervision timeout. Complete LE Encryption/LE Ping Feature Exchange
        and encrypted setup, as required for MIC-bearing stimuli. Select a
        one-second authenticated timeout IXIT.

    Test Steps:
        1. Write the authenticated timeout IXIT less than 5 s; check HCI
           completion handle and readback value.
        2. Send DATA containing a valid MIC and record its PHY receive-grant time.
        3. Keep the link idle, with no background LE-U or LE-C traffic, for an
           observation lasting longer than the IXIT. Interleave Steps 4/5 with
           this observation rather than waiting past the first deadline first.
        4. Receive LL_PING_REQ transmitted strictly before the IXIT expires.
        5. Respond with encrypted LL_PING_RSP. The interval between valid-MIC
           packets received by the IUT must not exceed the IXIT.

    Expected Outcome:
        Pass verdict:
            Request transmission and receipt of the resulting valid-MIC reply
            both occur within the IXIT from the last valid-MIC reception.
            Continue observation past the initial IXIT even if requests recur
            much faster than the configured timeout.

    Notes:
        Avoid incidental MIC-bearing traffic; otherwise restart the observation
        from that packet. Only intended replies reset the reference here.

    Source Correction:
        Step 3 describes an observation interleaved with Steps 4/5. Reading it
        as a completed wait before Step 4 contradicts that step and the verdict.
        The encrypted preamble is required by Steps 2/5 although omitted from
        the initial-condition list. These are local Core-backed interpretations,
        not adopted SIG errata.

    Postamble:
        Keep the connection established and terminate after the observation.

    Reference:
        LL.TS.p28, Section 4.8.7, LL/SEC/CEN/BV-09-C; PDF 1273-1274,
        printed 4.8-86..87; Figure 4.8-48. Core reference [3], Vol 6 Part B
        Section 5.1.8. Common keys: Section 4.1.6.6, with the Figure 4.1-27
        operand correction documented in Central Encryption Mode Setup.
    """
    await _ping(tester, central=True, mode='modified')


async def initiate_le_ping_when_peer_does_not_support_it_cen_bv_10(
    tester,
):
    """Initiate LE Ping procedure when the other side does not support the procedure.

    Initial Condition:
        Encrypted Connected Central with any advertising interval/channel map,
        supported own-address type, common interval, selected supervision timeout
        and any SCA. Select public addresses, 10-ms interval, zero latency and
        300-ms supervision timeout. The Lower Tester declares LE Encryption
        support with LE Ping clear. Complete the common-key encrypted handshake
        and read back the unchanged default authenticated timeout of 30 s.
        Keep the connection idle without background LE-U or LE-C traffic.

    Test Steps:
        1. Receive LL_PING_REQ less than 30 s after establishment of the encrypted
           connection. Use the last-sample PHY grant for the final encrypted
           Lower Tester LL_START_ENC_RSP as the reference. Do not inject extra
           MIC-bearing DATA before this first request.
        2. Respond with encrypted LL_UNKNOWN_RSP identifying LL_PING_REQ.
        3. Receive another LL_PING_REQ less than 30 s after the IUT received
           the LL_UNKNOWN_RSP from Step 2.
        4. Respond again with encrypted LL_UNKNOWN_RSP identifying LL_PING_REQ.

    Expected Outcome:
        Pass verdict:
            The Central sends both requests within their respective 30-second
            windows, triggering authenticated unknown-opcode responses rather
            than depending on peer support for Ping.

    Notes:
        Avoid incidental MIC-bearing packets. If one is sent, repeat observation
        from that packet as the source notes require; this peer sends only the
        intended encrypted handshake and unknown-opcode replies. Empty DATA and
        retransmissions do not restart the valid-MIC reference.

    Source Correction:
        The purpose calls the Lower Tester Central while the IUT is Central;
        the Lower Tester is Peripheral. Amber supports the role correction and
        the valid-MIC timer rule in Core Vol 6 Part B Section 5.4. The local PDF
        explicitly supplies all four steps and both timing references; no
        adopted erratum is claimed.

    Postamble:
        Verify the connection remains established and terminate it.

    Reference:
        LL.TS.p28, Section 4.8.7, LL/SEC/CEN/BV-10-C; PDF 1274-1276,
        printed 4.8-87..89; Figure 4.8-49. Core reference [3], Vol 6 Part B
        Sections 5.1.8 and 5.4. Common keys: Section 4.1.6.6, with the
        Figure 4.1-27 operand correction in Central Encryption Mode Setup.
    """
    await _ping(tester, central=True, mode='unknown')


async def central_receiving_ll_reject_ext_ind(tester):
    """Central Receiving LL_REJECT_EXT_IND.

    Initial Condition:
        Connected Central with any scan interval/window, public peer address,
        Lower Tester address, supported own-address type, common connection
        interval, Peripheral latency and timeout. Select public addresses,
        10-ms interval, zero latency and 300-ms timeout. Use common IR/ER and key
        variables. The feature preamble declares LE Encryption and Extended
        Reject Indication support.

    Test Steps:
        1. Send HCI LE Enable Encryption with the connection handle and common
           key parameters; receive successful Command Status.
        2. Receive correctly sized LL_ENC_REQ with matching Rand/EDIV and the
           Central SKD/IV components. Independently recover the preamble LTK
           and send LL_ENC_RSP with the Peripheral components.
        3. Send LL_REJECT_EXT_IND with RejectOpcode = LL_ENC_REQ and PIN or Key
           Missing (0x06). Require no premature encryption result before this
           rejection is injected.
        4. Receive HCI Encryption Change for the same handle with PIN or Key
           Missing status and encryption disabled.

    Postamble:
        Verify that unencrypted ACL remains usable, then terminate. This is not
        an additional numbered source step.

    Expected Outcome:
        Pass verdict:
            Receive LL_ENC_REQ with the requested parameters and matching
            HCI Encryption Change indicating PIN or Key Missing after rejection.

    Reference:
        LL.TS.p28, Section 4.8.7, LL/SEC/CEN/BV-11-C; PDF 1276-1277,
        printed 4.8-89..90; Figure 4.8-50. Core reference [3], Vol 6 Part B
        Section 5.1.3.1. Common keys: Section 4.1.6.6, with the Figure 4.1-27
        operand correction documented in Central Encryption Mode Setup.
    """
    await _reject(tester, central=True, extended=True)


async def central_start_encryption_overlapping_procedure(tester):
    """Central Start Encryption: Overlapping Procedure.

    Initial Condition:
        Encryption Keys Calculated using common IR/ER, and Connected Central
        with any scan interval/window and peer-address type, Lower Tester
        address, public IUT address, common interval, Peripheral latency and
        timeout. Select public Lower Tester address, 10-ms interval, zero latency
        and 300-ms timeout. Read the IUT's local version for response validation.
        Execute both alternatives on separate connections: leave version
        information unexchanged for Alternative 1; complete and acknowledge
        Version Exchange before encryption start for Alternative 2.

    Test Steps:
        1. Send HCI LE Enable Encryption with the connection handle and the
           preamble's Rand, EDIV and LTK; receive successful Command Status.
        2. Receive LL_ENC_REQ and verify its matching key parameters and exact
           format; independently recover the preamble LTK and derive the
           session key from the exchanged SKD/IV components.
        3. Send LL_VERSION_IND, followed by LL_ENC_RSP.
        Alternative 1 (version not previously exchanged):
            1. Verify that the IUT defers its response to LL_VERSION_IND.
            2. Complete encryption start normally, including acknowledged
               LL_START_ENC_REQ and authenticated encrypted LL_START_ENC_RSP
               exchange; receive successful HCI Encryption Change for the
               correct handle with encryption enabled.
            3. Receive the queued LL_VERSION_IND only after the final encrypted
               Lower Tester LL_START_ENC_RSP reaches the IUT. Authenticate the
               response and check all version fields against HCI local version.
        Alternative 2 (version already exchanged in the preamble):
            1. Verify that the IUT ignores the additional LL_VERSION_IND.
            2. Complete encryption start normally and require matching
               successful HCI Encryption Change with encryption enabled.

    Expected Outcome:
        Pass verdict:
            Alternative 1 returns the deferred version response after encryption
            completes. Alternative 2 sends no further version response because
            the version exchange was already completed. Both complete encryption
            and notify the Host successfully. Retain every decoded control PDU
            so handshake polling cannot discard an early response unnoticed.

    Observation:
        In Alternative 2, continue empty traffic for one selected supervision
        timeout after encryption, checking the retained control history for a
        second version response. This is a finite observation guard, not a Core
        response-latency requirement. Distinguish air completion from the time
        the Upper Tester reads an already-delivered HCI notification.

    Postamble:
        Verify no premature termination or extra encryption result, then
        terminate each connection.

    Reference:
        LL.TS.p28, Section 4.8.7, LL/SEC/CEN/BV-12-C; PDF 1277-1279,
        printed 4.8-90..92; Figure 4.8-51. Core reference [3], Vol 6 Part B
        Section 5.1.3.1. Common key preamble: Section 4.1.6.6, with the
        Figure 4.1-27 operand correction in Central Encryption Mode Setup.
    """
    for version_exchanged in (False, True):
        await _overlapping(tester, version_exchanged=version_exchanged)


async def central_start_encryption_overlapping_procedure_with_ll_peripheral_feature_req(
    tester,
):
    """Central Start Encryption: Overlapping Procedure with LL_PERIPHERAL_FEATURE_REQ.

    Initial Condition:
        Encryption Keys Calculated using common IR/ER, and Connected Central
        with any scan interval/window and peer-address type, Lower Tester
        address, public IUT address, common interval, Peripheral latency and
        timeout. Select public Lower Tester address, 10-ms interval, zero latency
        and 300-ms timeout. The IUT must support Peripheral-Initiated Feature
        Exchange; verify its declaration through HCI before running the case.
        The Lower Tester declares LE Encryption; do not require unrelated
        optional LE Ping support.

    Test Steps:
        1. Send HCI LE Enable Encryption with the connection handle and the
           preamble's Rand, EDIV and LTK; receive successful Command Status.
        2. Receive LL_ENC_REQ with matching parameters; verify its format and
           independently derive the session key and IV.
        3. Send LL_PERIPHERAL_FEATURE_REQ followed by LL_ENC_RSP.
        4. Verify that the IUT defers the feature response while encryption
           start is incomplete. Retain every control PDU, including responses
           consumed by encryption-handshake polling.
        5. Complete encryption start normally and receive matching successful
           HCI Encryption Change with encryption enabled.
        6. Receive and authenticate the queued LL_FEATURE_RSP only after the
           final encrypted Lower Tester LL_START_ENC_RSP reaches the IUT.
           Check its exact size and preservation of mutually supported
           LE Encryption in the feature set.

    Expected Outcome:
        Pass verdict:
            Return LL_FEATURE_RSP after encryption completes and report successful
            Encryption Change to the Host. A premature feature response, even if
            consumed by another helper, cannot satisfy this verdict.

    Postamble:
        Verify no premature termination or extra encryption result and terminate.

    Reference:
        LL.TS.p28, Section 4.8.7, LL/SEC/CEN/BV-13-C; PDF 1279-1280,
        printed 4.8-92..93; Figure 4.8-52. Core reference [3], Vol 6 Part B
        Section 5.1.3.1. Common key preamble: Section 4.1.6.6, with the
        Figure 4.1-27 operand correction in Central Encryption Mode Setup.
    """
    await _overlapping(tester, features=True)


async def central_receiving_unexpected_pdu_during_encryption_start(tester):
    """Central Receiving unexpected PDU during encryption start.

    Initial Condition:
        Connected Central with any advertising interval/channel map, supported
        address type, common connection interval and timeout, and any SCA. Use
        common IR/ER and encryption-key variables. Select public IUT and Lower
        Tester addresses, 10-ms interval, zero latency and 300-ms timeout.

    Test Steps:
        1. Establish the connection with public IUT address and a supported
           Lower Tester address type; select public Lower Tester address.
        2. Prepare the test Rand and common-key parameters. The IUT's Central
           Link Layer generates its own SKDc and IVc for LL_ENC_REQ.
        3. Send HCI LE Enable Encryption to the IUT with connection handle,
           Rand, EDIV and LTK. Require successful Command Status and receive
           LL_ENC_REQ with matching parameters; retain its SKDc and IVc.
        4. Send LL_ENC_RSP containing the Lower Tester's Peripheral SKD/IV
           components. Set MD to permit continuation of this connection event.
        5. Immediately follow with unencrypted LL_VERSION_IND in the same event
           if possible. Receive the Central's empty acknowledgement of ENC_RSP,
           check NESN, and send VERSION_IND at T_IFS after that empty packet.
           Do not substitute a normal response to LL_START_ENC_REQ.
        6. Verify that the IUT terminates rather than replying normally. Inspect
           all-channel PHY transmissions after the offending packet, including
           the interval following HCI notification, for an unexpected response.
        7. Receive successful HCI Disconnection Complete for the same handle
           with Connection Terminated Due to MIC Failure (0x3D).

    Expected Outcome:
        Pass verdict:
            Terminate on the unexpected LL_VERSION_IND, report MIC Failure
            through HCI and do not send a normal version response.

    Source Correction:
        An Upper Tester has no connection-establishment radio address: Step 1's
        public-address wording refers to the IUT controlled through HCI. Steps
        2/3 mix Host and Link Layer responsibilities. HCI LE Enable Encryption
        targets the local IUT, not the Lower Tester, and carries handle, Rand,
        EDIV and LTK, not SKD/IV. The IUT generates Central SKD/IV; the peer
        supplies Peripheral components over LL_ENC_RSP. Amber confirms these
        corrections against Core Vol 4 Part E Section 7.8.24 and Vol 6 Part B
        Section 5.1.3.1; they are not adopted SIG errata.

    Reference:
        LL.TS.p28, Section 4.8.7, LL/SEC/CEN/BV-14-C; PDF 1281-1282,
        printed 4.8-94..95; Figure 4.8-53. Core reference [3], Vol 6 Part B
        Section 5.1.3.1. Common keys: Section 4.1.6.6, with the Figure 4.1-27
        operand correction documented in Central Encryption Mode Setup.
    """
    await _unexpected(tester, data=False, refresh=False)


async def central_encryption_setup_missing_response(tester):
    """Central Encryption Setup: Missing Response.

    Initial Condition:
        Encryption Keys Calculated using common IR/ER, and Connected Central
        with any scan interval/window and peer-address type, Lower Tester
        address, public IUT address, common interval, Peripheral latency and
        timeout. Select public Lower Tester address, 10-ms interval, zero latency
        and 300-ms supervision timeout. Independently verify the common key
        preamble's Rand, EDIV and LTK.

    Test Steps:
        1. Send HCI LE Enable Encryption with the connection handle and the
           preamble's Rand, EDIV and LTK; receive successful Command Status.
        2. Receive LL_ENC_REQ with matching parameters; check its exact format
           and recover the preamble LTK independently from the received fields.
        3. Acknowledge LL_ENC_REQ through NESN, but do not send LL_ENC_RSP.
        4. Receive Central empty DATA until the procedure response timer expires,
           returning empty acknowledgements to prevent supervision expiry.
           Bracket the initial control-queue reset between Host command
           submission and observed LL_ENC_REQ transmission. Require no premature
           termination before the earlier bound plus 40 s and no connection
           transmission after the later bound plus 40 s.
        5. Receive successful HCI Disconnection Complete for the same handle
           with LL Response Timeout (0x22), separately from RF cessation.

    Expected Outcome:
        Pass verdict:
            The IUT stops maintaining the connection on LL response timeout,
            not the shorter supervision timeout, and notifies the Host.

    Observation:
        Control queueing, not an exact air-bit boundary, resets T_PRT under Core
        Section 5.2. Check all-channel PHY silence for one supervision timeout
        after the upper expiry bound. Do not impose an HCI delivery-latency
        requirement or treat empty DATA as a timer reset.

    Reference:
        LL.TS.p28, Section 4.8.7, LL/SEC/CEN/BI-01-C; PDF 1282-1283,
        printed 4.8-95..96; Figure 4.8-54. Core reference [3], Vol 6 Part B
        Section 5.1.3; timer: Core 6.3 Vol 6 Part B Section 5.2. Common keys:
        Section 4.1.6.6, with the Figure 4.1-27 operand correction documented
        in Central Encryption Mode Setup.
    """
    await _security_timeout(tester, central=True, stage='response', data=False)


async def central_encryption_setup_missing_request(tester):
    """Central Encryption Setup: Missing Request.

    Initial Condition:
        Encryption Keys Calculated using common IR/ER, and Connected Central
        with any scan interval/window and peer-address type, Lower Tester
        address, public IUT address, common interval, Peripheral latency and
        timeout. Select public Lower Tester address, 10-ms interval, zero latency
        and 300-ms supervision timeout; use the common LTK/IR key variables.

    Test Steps:
        1. Send HCI LE Enable Encryption with the connection handle and the
           preamble's Rand, EDIV and LTK; receive successful Command Status.
        2. Receive LL_ENC_REQ with matching Rand/EDIV and Central SKD/IV
           components; verify its format and independently recover the LTK.
        3. Acknowledge the request and send LL_ENC_RSP with the Peripheral
           SKD/IV components.
        4. Do not send LL_START_ENC_REQ. Respond to Central transmissions using
           the acknowledgement scheme. Continue empty traffic without a new
           control-timer reset, checking the 40-second expiry bracket from Host
           command submission through observed LL_ENC_REQ transmission.
        5. Receive successful HCI Disconnection Complete for the same handle
           with LL Response Timeout (0x22).

    Expected Outcome:
        Pass verdict:
            The IUT stops maintaining the connection on LL response timeout and
            notifies the Host, despite acknowledged empty traffic keeping the
            supervision timer alive.

    Observation:
        Observe both sides of the externally bounded control-queue deadline;
        check all-channel PHY silence for a full supervision timeout after its
        upper bound. HCI notification is separate from the RF cessation boundary.

    Reference:
        LL.TS.p28, Section 4.8.7, LL/SEC/CEN/BI-03-C; PDF 1283-1284,
        printed 4.8-96..97; Figure 4.8-55. Core reference [3], Vol 6 Part B
        Section 5.1.3; timer: Core 6.3 Vol 6 Part B Section 5.2. Common keys:
        Section 4.1.6.6, with the Figure 4.1-27 operand correction documented
        in Central Encryption Mode Setup.
    """
    await _security_timeout(tester, central=True, stage='start', data=False)


async def central_encryption_setup_missing_acknowledgement(tester):
    """Central Encryption Setup: Missing Acknowledgement.

    Initial Condition:
        Encryption Keys Calculated using common IR/ER, and Connected Central
        with any scan interval/window and peer-address type, Lower Tester
        address, public IUT address, common interval, Peripheral latency and
        timeout. Select public Lower Tester address, 10-ms interval, zero latency
        and 300-ms supervision timeout; retain the common LTK/IR key variables.

    Test Steps:
        1. Send HCI LE Enable Encryption with the connection handle and the
           preamble's Rand, EDIV and LTK; receive successful Command Status.
        2. Receive LL_ENC_REQ with matching parameters and Central SKD/IV.
           Acknowledge it and send LL_ENC_RSP with the Peripheral components.
        3. Combine SKD/IV halves; recover DIV using DHK and the received Rand/EDIV,
           derive LTK using ER and compare it to the preamble value. Independently
           derive the session key for authenticating the Central's response.
        4. Receive empty Central DATA and respond with LL_START_ENC_REQ.
        5. Receive and authenticate encrypted LL_START_ENC_RSP, verifying its
           acknowledgement of LL_START_ENC_REQ. Acknowledge that response but
           do not send the final Lower Tester LL_START_ENC_RSP.
        6. Continue empty Peripheral replies to the Central's transmissions,
           maintaining acknowledgements until the control timer expires. Bracket
           its last reset between reception of the Lower Tester START_ENC_REQ
           and transmission of the IUT's encrypted START_ENC_RSP.
        7. Receive successful HCI Disconnection Complete for the same handle
           with LL Response Timeout (0x22).

    Expected Outcome:
        Pass verdict:
            The IUT stops maintaining the connection and reports LL response
            timeout without completing encryption or expiring supervision.

    Source Correction:
        Step 5 explicitly retains link-layer ACKs: the omitted acknowledgement
        in the title is the final LL_START_ENC_RSP, not NESN acknowledgement.
        Step 3 conflates DIV recovery with LTK derivation. DHK recovers DIV;
        use the common preamble's LTK=d1(ER,DIV,0), not DHK as the LTK root.
        Step 6's Peripheral-IUT wording is inverted: the IUT is Central and
        initiates connection events; the Lower Tester sends Peripheral replies.
        Amber confirms the omission and role/DHK wording defects; the ER-keyed
        derivation is also supported by the common preamble and its Core-backed
        correction. These are logical source corrections, not adopted errata.

    Observation:
        Apply the Core Section 5.2 control-queue reset rule, not an invented
        exact air-bit origin. Check no premature disconnection, full all-channel
        PHY silence after the upper expiry bound, and the separate Host event.

    Reference:
        LL.TS.p28, Section 4.8.7, LL/SEC/CEN/BI-04-C; PDF 1284-1286,
        printed 4.8-97..99; Figure 4.8-56. Core reference [3], Vol 6 Part B
        Section 5.1.3; timer: Core 6.3 Vol 6 Part B Section 5.2. Common keys:
        Section 4.1.6.6, Figures 4.1-26/27; Core Vol 3 Part H Appendix B.2.1/
        B.2.2, with the operand correction in Central Encryption Mode Setup.
    """
    await _security_timeout(tester, central=True, stage='final', data=False)


async def central_mic_failure(tester, *, header_error):
    """Central MIC Failure.

    Initial Condition:
        Encrypted Central connection using common IR/ER, any scan interval/window
        and peer-address type, Lower Tester address, public IUT address, common
        interval, Peripheral latency and timeout. Select public Lower Tester
        address, 10-ms interval, zero latency and 300-ms supervision timeout.
        Complete the common-key encrypted preamble and exchange valid data in
        both directions. Table 4.8-12 selects calculated-MIC corruption for
        LL/SEC/CEN/BI-05-C and authenticated-header corruption for BI-06-C.

    Test Case Configuration:
        LL/SEC/CEN/BI-05-C: corrupted MIC.
        LL/SEC/CEN/BI-06-C: corrupted header.

    Test Steps:
        1. Receive a Central DATA packet and respond at T_IFS with independently
           encrypted DATA containing ten plaintext octets. After encryption,
           flip exactly one calculated-MIC bit for BI-05-C or one authenticated
           LLID header bit for BI-06-C. Recompute a valid CRC for the altered PDU.
        2. Receive successful HCI Disconnection Complete for the same handle
           with Connection Terminated Due to MIC Failure (0x3D).
        3. Continue Peripheral stimuli in the predicted response slots: after
           each original event anchor, allow the airtime of an empty Central
           DATA packet and T_IFS before transmitting the unacknowledged corrupt
           packet again. Preserve channel sequence, event phase and counter wrap.
        4. Repeat Step 3 through one full supervision timeout, including the
           final partial interval, verifying that Central transmissions stopped.

    Expected Outcome:
        Pass verdict:
            Terminate the connection and report MIC Failure to the Host. No
            corrupted payload may reach HCI. Check all-channel PHY records,
            not only the Lower Tester's receive windows, for post-failure TX.

    Observation:
        The current IUT declares Core 5.0. Vol 6 Part E Section 1 requires local
        connection loss and no further connection packets after authentication
        failure. The supervision timeout bounds observation of the peer's loss;
        it is not permission for the IUT to keep transmitting after bad MIC.
        Amber confirms this distinction and the event's separate Status/Reason.

    Reference:
        LL.TS.p28, Section 4.8.7.2, LL/SEC/CEN/BI-05-C and LL/SEC/CEN/BI-06-C;
        PDF 1286-1287, printed 4.8-99..100; Table 4.8-12 and Figure 4.8-57.
        Core reference [3], Vol 6 Part B Sections 3.1 and 5.1.3; Core 5.0
        Vol 6 Part E Section 1. Common keys: Section 4.1.6.6, with the
        Figure 4.1-27 operand correction in Central Encryption Mode Setup.
    """
    await _mic_failure(tester, central=True, header_error=header_error)


async def central_pause_encryption_sending_data(tester):
    """Central Pause Encryption Sending Data.

    Initial Condition:
        Encryption Keys Calculated using common IR/ER, and Connected Central
        with any scan interval/window, selected peer and own-address types,
        Lower Tester address, common interval, Peripheral latency and timeout.
        Select public addresses, 10-ms interval, zero latency and 300-ms timeout.
        The selected IUT declares Core 5.0 or later.

    Test Steps:
        1. Establish the encrypted connection with the IUT as Central using
           the common key preamble and independently checked AES-CCM handshake.
        2. Send HCI LE Enable Encryption for this encrypted handle and receive
           successful Command Status.
        3. Receive and authenticate encrypted LL_PAUSE_ENC_REQ; reply with
           encrypted LL_PAUSE_ENC_RSP.
        4. Receive unencrypted LL_PAUSE_ENC_RSP from the Central. Encryption
           is now paused in both directions.
        5. Receive LL_ENC_REQ to restart encryption with a new session key.
           Verify Rand/EDIV and derive the replacement key/IV independently;
           require that the old key and IV were not reused.
        6. Send LL_ENC_RSP with the Peripheral SKD/IV components.
        7. Send unencrypted DATA containing 0xFF instead of LL_START_ENC_REQ.
           Keep the event open with MD and send at T_IFS after the Central's
           acknowledgement of ENC_RSP. No refresh result may precede this fault.
        8. Receive successful HCI Disconnection Complete for the same handle
           with MIC Failure (0x3D). Require no forbidden HCI ACL delivery and
           no further connection transmissions after the offending packet.

    Expected Outcome:
        Pass verdict:
            Report MIC Failure through HCI and terminate the connection without
            delivering the forbidden DATA or completing the encryption restart.

    Source Correction:
        Step 8 and the verdict put MIC Failure in Status. The event's successful
        Status is 0x00; Reason is 0x3D. Amber confirms the distinction against
        Core Vol 4 Part E Section 7.7.5. This is not an adopted SIG erratum.

    Version Scope:
        The source permits Core 4.0 IUTs to send LL_TERMINATE_IND(0x3D) between
        Steps 7/8. That legacy path is not exercised by this Core 5.0-or-later
        IUT; an older declaration is explicitly skipped rather than silently
        applying the modern no-transmission verdict to it.

    Reference:
        LL.TS.p28, Section 4.8.7.2, LL/SEC/CEN/BI-07-C; PDF 1287-1289,
        printed 4.8-100..102; Figure 4.8-58. Core reference [3], Vol 6 Part B
        Section 5.1.3. Common keys: Section 4.1.6.6, with the Figure 4.1-27
        operand correction in Central Encryption Mode Setup.
    """
    await _unexpected(tester, data=True, refresh=True)


async def central_encryption_sending_data_and_not_response(tester):
    """Central Encryption: Sending Data and Not Response.

    Initial Condition:
        Encryption Keys Calculated using common IR/ER, and Connected Central
        with any scan interval/window, selected peer-address type, Lower Tester
        address, public IUT address, common interval, Peripheral latency and
        timeout. Select public Lower Tester address, 10-ms interval, zero latency
        and 300-ms supervision timeout; retain common Rand/EDIV/LTK.

    Test Steps:
        1. Send HCI LE Enable Encryption with connection handle, Rand, EDIV
           and LTK; receive successful Command Status.
        2. Receive LL_ENC_REQ with matching parameters and verify its format.
        3. Acknowledge the request but do not send LL_ENC_RSP at any later time.
        4. Send unencrypted DATA containing 0xFF. Check its legal HCI delivery
           before ENC_RSP, then maintain only empty acknowledgements without
           resetting the procedure timer or triggering supervision expiry.
        5. Receive successful HCI Disconnection Complete for the same handle
           with LL Response Timeout (0x22). Check no premature termination before
           the lower queue-reset bound plus 40 s and full all-channel RF silence
           after the upper bound plus 40 s, independently of HCI delivery time.

    Expected Outcome:
        Pass verdict:
            The missing control response causes LL response timeout despite
            receipt of the legal plaintext DATA and continued empty traffic.

    Source Correction:
        Step 5 and the verdict call the disconnect reason Status. Require
        Status=Success and Reason=LL Response Timeout, as defined by Core Vol 4
        Part E Section 7.7.5 and confirmed by Amber; no adopted erratum is claimed.

    Observation:
        The initial IUT control-queue reset lies between Host command submission
        and observed LL_ENC_REQ transmission. Use that external bracket rather
        than assigning an exact air-bit queueing time. Empty DATA cannot reset
        T_PRT; inspect all-channel silence for a full supervision timeout after
        its upper expiry bound, then separately require the matching Host event.

    Reference:
        LL.TS.p28, Section 4.8.7.2, LL/SEC/CEN/BI-08-C; PDF 1289-1290,
        printed 4.8-102..103; Figure 4.8-59. Core reference [3], Vol 6 Part B
        Section 5.1.3; timer: Core 6.3 Vol 6 Part B Section 5.2. Common keys:
        Section 4.1.6.6, with the Figure 4.1-27 operand correction documented
        in Central Encryption Mode Setup.
    """
    await _security_timeout(tester, central=True, stage='response', data=True)


async def central_encryption_sending_data_and_not_request(tester):
    """Central Encryption: Sending Data and Not Request.

    Initial Condition:
        Encryption Keys Calculated using common IR/ER, and Connected Central
        with any scan interval/window, selected peer-address type, Lower Tester
        address, public IUT address, common interval, Peripheral latency and
        timeout. Select public Lower Tester address, 10-ms interval, zero latency
        and 300-ms timeout. The selected IUT declares Core 5.0 or later.

    Test Steps:
        1. Send HCI LE Enable Encryption with connection handle and common
           Rand/EDIV/LTK; receive successful Command Status.
        2. Receive LL_ENC_REQ with matching parameters, verify its exact format
           and derive the session key/IV independently.
        3. Acknowledge LL_ENC_REQ and send LL_ENC_RSP with Peripheral SKD/IV.
        4. Send unencrypted DATA containing 0xFF instead of LL_START_ENC_REQ.
           Keep the connection event open and send at T_IFS after the Central's
           empty acknowledgement; do not complete the encryption handshake.
        5. Receive successful HCI Disconnection Complete for the same handle
           with MIC Failure (0x3D). Require no forbidden HCI ACL delivery and no
           further connection transmissions after the offending packet.

    Expected Outcome:
        Pass verdict:
            Report MIC Failure through HCI and terminate on the unexpected DATA,
            rather than delivering it or proceeding with encryption setup.

    Source Correction:
        Step 5 and the verdict put MIC Failure in Status. Check successful
        Status and Reason=0x3D instead, per Core Vol 4 Part E Section 7.7.5
        and Amber's confirmation. This is not an adopted SIG erratum.

    Version Scope:
        The source's Core 4.0-only optional LL_TERMINATE_IND(0x3D) between Steps
        4/5 is not exercised by this Core 5.0-or-later IUT. Explicitly skip an
        older version declaration instead of claiming that legacy path passed.

    Reference:
        LL.TS.p28, Section 4.8.7.2, LL/SEC/CEN/BI-09-C; PDF 1290-1291,
        printed 4.8-103..104; Figure 4.8-60. Core reference [3], Vol 6 Part B
        Section 5.1.3. Common keys: Section 4.1.6.6, with the Figure 4.1-27
        operand correction documented in Central Encryption Mode Setup.
    """
    await _unexpected(tester, data=True, refresh=False)


async def _privacy_advertising_parameters(
    tester,
    *,
    peer,
    own_type=2,
    directed=False,
    scannable=False,
    nonconnectable=False,
    extended=False,
    policy=3,
):
    if extended:
        await tester.command(
            extended_parameters(
                own_address_type=own_type,
                peer_address_type=peer.address_type,
                peer_address=peer,
                advertising_event_properties=1 | (4 if directed else 0),
                primary_advertising_channel_map=7,
                advertising_filter_policy=policy,
            )
        )
    else:
        await tester.command(
            legacy_parameters(
                own_address_type=own_type,
                peer_address_type=peer.address_type,
                peer_address=peer,
                advertising_type=1
                if directed
                else 2
                if scannable
                else 3
                if nonconnectable
                else 0,
                advertising_channel_map=7,
                advertising_filter_policy=policy,
            )
        )


async def _privacy_advertising_enable(tester, enable=True, *, extended=False):
    await tester.command(
        extended_enable(int(enable))
        if extended
        else hci.HCI_LE_Set_Advertising_Enable_Command(int(enable))
    )


async def _privacy_advertisement(
    tester, *, extended=False, scannable=False, directed=False, nonconnectable=False
):
    packet = await tester.expect_pdu(37, duration_us=100_000)
    if extended:
        fields = decode_extended(packet.pdu)
        channel, packet, fields = await follow_auxiliary(tester, packet, fields)
        address = hci.Address(fields['address'], int(bool(packet.pdu[0] & 0x40)))
        target = (
            hci.Address(fields['target'], int(bool(packet.pdu[0] & 0x80)))
            if 'target' in fields
            else None
        )
    else:
        kind = (
            AdvertisingPduType.ADV_DIRECT_IND
            if directed
            else AdvertisingPduType.ADV_SCAN_IND
            if scannable
            else AdvertisingPduType.ADV_NONCONN_IND
            if nonconnectable
            else AdvertisingPduType.ADV_IND
        )
        assert packet.pdu[0] & 15 == kind
        address = hci.Address(packet.pdu[2:8], int(bool(packet.pdu[0] & 0x40)))
        target = (
            hci.Address(packet.pdu[8:14], int(bool(packet.pdu[0] & 0x80)))
            if directed
            else None
        )
        channel = 37
    return channel, packet, address, target


def _no_privacy_connection(tester):
    assert not any(
        isinstance(
            event,
            (
                hci.HCI_LE_Connection_Complete_Event,
                hci.HCI_LE_Enhanced_Connection_Complete_Event,
            ),
        )
        and event.status == 0
        for event in tester.events
    )


async def _privacy_scan_request(tester, packet, address, scanner, *, expected):
    end = await tester.transmit(
        37,
        request_pdu(AdvertisingPduType.SCAN_REQ, advertiser=address, scanner=scanner),
        start_us=packet.end_us + LL_IFS_US,
    )
    response = await tester.receive(37, duration_us=1000)
    # A high-duty advertisement is never used in these scan-response checks.
    assert bool(response) == expected
    if response is not None:
        assert response.pdu[0] & 15 == AdvertisingPduType.SCAN_RSP
        assert response.pdu[2:8] == bytes(address)
        assert abs(response.start_us - end - LL_IFS_US) <= 2
    return response


async def _privacy_incoming_connection(
    tester,
    *,
    peer,
    wire_peer,
    directed=False,
    extended=False,
    local_irk=True,
    expect=True,
    own_type=2,
    use_target=False,
    target_key=privacy_peer.PEER_IRK,
):
    channel, packet, address, target = await _privacy_advertisement(
        tester,
        directed=directed,
        extended=extended,
    )
    if local_irk:
        privacy_peer.check_private_address(address, privacy_peer.LOCAL_IRK)
    else:
        assert address == (privacy_peer.FALLBACK if own_type & 1 else DUT_ADDRESS)
    if use_target:
        assert target is not None
        wire_peer = target
    if directed and target is not None and target.is_resolvable:
        privacy_peer.check_private_address(target, target_key)
    parameters = LinkParameters()
    end = await tester.transmit(
        channel,
        request_pdu(
            AdvertisingPduType.CONNECT_IND,
            advertiser=address,
            scanner=wire_peer,
            data=parameters.encode(),
        ),
        start_us=packet.end_us + LL_IFS_US,
    )
    if extended:
        response = await tester.receive(channel, duration_us=500)
        assert bool(response) == expect
        if response is not None:
            fields = decode_extended(response.pdu)
            assert response.pdu[0] & 15 == AdvertisingPduType.AUX_CONNECT_RSP
            assert fields['address'] == bytes(address) and fields['target'] == bytes(
                wire_peer
            )
            assert abs(response.start_us - end - LL_IFS_US) <= 2
    link = PeerLink(tester, parameters, end, csa2=extended)
    if extended:
        link.anchor = end + 2500
    if not expect:
        assert await link.exchange(required=False) is None
        _no_privacy_connection(tester)
        return None, address, target
    event = await tester.event(hci.HCI_LE_Enhanced_Connection_Complete_Event)
    assert event.status == 0 and event.role == hci.Role.PERIPHERAL
    assert event.peer_address == peer
    expected_peer_type = (
        2 + int(peer.is_random) if wire_peer.is_resolvable else peer.address_type
    )
    assert event.peer_address_type == expected_peer_type
    assert bytes(event.peer_resolvable_private_address) == (
        bytes(wire_peer) if wire_peer.is_resolvable else bytes(6)
    )
    assert bytes(event.local_resolvable_private_address) == (
        bytes(address) if local_irk and own_type >= 2 else bytes(6)
    )
    link.handle = event.connection_handle
    if extended:
        selection = await tester.event(hci.HCI_LE_Channel_Selection_Algorithm_Event)
        assert (
            selection.connection_handle == link.handle
            and selection.channel_selection_algorithm == 1
        )
        stopped = await tester.event(hci.HCI_LE_Advertising_Set_Terminated_Event)
        assert stopped.status == 0 and stopped.connection_handle == link.handle
    await link.exchange()
    return link, address, target


async def privacy_nonconnectable_advertising(tester, *, resolvable=False):
    """Privacy: Non-connectable Undirected Advertising.

    Initial Condition:
        The Host supplies an NRPA, or a nonzero Local IRK and v2 RPA timeout
        range one to two seconds; all primary advertising channels are enabled.

    Test Case Configuration:
        LL/SEC/ADV/BV-02-C: non-resolvable private address.
        LL/SEC/ADV/BV-27-C: resolvable private address.

    Test Steps:
        1-3. Monitor the radio, provide the address/IRK and enable nonconnectable
             advertising at a supported interval.
        4-5. NRPA branch: observe 100 advertising events and compare AdvA exactly.
             RPA branch: resolve every observed AdvA until it changes within the
             selected timeout, and resolve the new AdvA with the same IRK.
        6. Disable advertising.

    Expected Outcome:
        NRPA remains equal to the configured value. Both RPAs resolve with the
        assigned Local IRK, and renewal occurs within the maximum plus one event.

    Reference:
        LL.TS.p28, Section 4.8.3.1, PDF pages 1186-1189, Table 4.8-1,
        Figure 4.8-2; Core Vol 6, Part B, 1.3.2 and 6.2.1;
        Vol 4, Part E, 7.8.45.
    """
    peer = LOWER_CENTRAL_PUBLIC_ADDRESS
    if resolvable:
        await privacy_peer.add_peer(tester, identity=peer, peer_irk=bytes(16))
        await tester.command(
            hci.HCI_LE_Set_Resolvable_Private_Address_Timeout_V2_Command(1, 2)
        )
    else:
        await tester.command(
            hci.HCI_LE_Set_Random_Address_Command(privacy_peer.FALLBACK)
        )
    await _privacy_advertising_parameters(
        tester,
        peer=peer,
        own_type=2 if resolvable else 1,
        nonconnectable=True,
        policy=0,
    )
    await _privacy_advertising_enable(tester)
    first = None
    for _ in range(110):
        _, packet, address, _ = await _privacy_advertisement(
            tester, nonconnectable=True
        )
        if resolvable:
            privacy_peer.check_private_address(address, privacy_peer.LOCAL_IRK)
            if first is None:
                first = address, packet.start_us
            elif address != first[0]:
                assert 970_000 <= packet.start_us - first[1] <= 2_030_000
                break
        else:
            assert address == privacy_peer.FALLBACK
    else:
        assert not resolvable, 'RPA did not renew before its declared maximum'
    await _privacy_advertising_enable(tester, False)


async def privacy_scannable_nonresolvable(tester):
    """Network Privacy: Scannable Advertising, non-resolvable private address.

    Initial Condition:
        One Host-provided NRPA; all primary advertising channels are enabled.

    Test Steps:
        1-3. Configure scannable advertising and assign the NRPA before enabling.
        4-6. For 100 advertising events, verify AdvA and send SCAN_REQ with a
             scanner NRPA at T_IFS; verify matching-address SCAN_RSP at T_IFS.
        7. Disable advertising.

    Expected Outcome:
        Every request receives a valid response with the Host-provided AdvA.

    Source Correction:
        The source assigns the random address after enabling advertising. Core
        Vol 4, Part E, 7.8.4 prohibits that command while advertising is enabled;
        assign it before enable. This is a local sequencing correction, not SIG errata.

    Reference:
        LL.TS.p28, SEC/ADV/BV-04-C, PDF pages 1189-1191, Figure 4.8-3;
        Core Vol 6, Part B, 4.4.2.5.
    """
    await tester.command(hci.HCI_LE_Set_Random_Address_Command(privacy_peer.FALLBACK))
    await _privacy_advertising_parameters(
        tester, peer=LOWER_CENTRAL_PUBLIC_ADDRESS, own_type=1, scannable=True, policy=0
    )
    await _privacy_advertising_enable(tester)
    for _ in range(100):
        _, packet, address, _ = await _privacy_advertisement(tester, scannable=True)
        assert address == privacy_peer.FALLBACK
        await _privacy_scan_request(
            tester, packet, address, privacy_peer.FALLBACK, expected=True
        )
    await _privacy_advertising_enable(tester, False)


async def privacy_scannable_rpa_timeout(tester):
    """Network Privacy: Scannable Advertising, Random RPA Timeout.

    Initial Condition:
        Shared peer/local IRKs, Filter Accept List identity, timeout range 1-2 s.

    Test Steps:
        1-5. Populate resolving/accept lists, set the timeout and enable scannable
             advertising on all primary channels with scan filtering.
        6-9. Resolve each AdvA, send peer-RPA SCAN_REQ and verify SCAN_RSP until
             AdvA refreshes; verify the new AdvA resolves with the same key.
        10-12. Stop advertising, disable resolution, restart the same advertiser.
        13-17. Repeat twenty RPA requests; verify no responses, then disable.

    Expected Outcome:
        RPA generation/renewal operates independently of resolver enable, while
        disabled resolution prevents accept-list matching of peer RPAs.

    Source Correction:
        Step 9 says ADV_NONCONN_IND and step 10 says nonconnectable disable;
        the configured operation and all exchanges are scannable ADV_SCAN_IND.
        Use that PDU/operation. Populate lists before enabling as required by
        Core Vol 4, Part E, 7.8.5 and 7.8.44, which disallow the filter-policy
        change and resolution enable while advertising is enabled. These are
        local corrections, not SIG errata.

    Reference:
        LL.TS.p28, Section 4.8.3.2, PDF pages 1191-1194, Table 4.8-2;
        Core Vol 6, Part B, 4.7 and 6.2.3; Vol 4, Part E, 7.8.45.
    """
    peer = LOWER_CENTRAL_PUBLIC_ADDRESS
    await privacy_peer.add_peer(tester, identity=peer)
    await tester.command(hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(0, peer))
    await tester.command(
        hci.HCI_LE_Set_Resolvable_Private_Address_Timeout_V2_Command(1, 2)
    )
    await _privacy_advertising_parameters(tester, peer=peer, scannable=True, policy=1)
    await _privacy_advertising_enable(tester)
    scanner = privacy_peer.private_address(privacy_peer.PEER_IRK)
    first = None
    for _ in range(110):
        _, packet, address, _ = await _privacy_advertisement(tester, scannable=True)
        privacy_peer.check_private_address(address, privacy_peer.LOCAL_IRK)
        await _privacy_scan_request(tester, packet, address, scanner, expected=True)
        if first is None:
            first = address, packet.start_us
        elif address != first[0]:
            assert 970_000 <= packet.start_us - first[1] <= 2_030_000
            break
    else:
        raise AssertionError('scannable advertiser did not renew its RPA')
    await _privacy_advertising_enable(tester, False)
    await tester.command(hci.HCI_LE_Set_Address_Resolution_Enable_Command(0))
    await _privacy_advertising_enable(tester)
    for _ in range(20):
        _, packet, address, _ = await _privacy_advertisement(tester, scannable=True)
        privacy_peer.check_private_address(address, privacy_peer.LOCAL_IRK)
        await _privacy_scan_request(tester, packet, address, scanner, expected=False)
    await _privacy_advertising_enable(tester, False)


async def privacy_scannable_identity_policy(
    tester, *, device=False, enabled=True, policy=3
):
    """Network/device privacy filtering of identity or unresolved scanner addresses.

    Initial Condition:
        Shared IRKs and a sole accept-list identity; device mode only for BV-18.
        BV-21 keeps address resolution disabled and sends a peer RPA.

    Test Case Configuration:
        LL/SEC/ADV/BV-15-C: Network Privacy Mode, address resolution enabled,
            Advertising_Filter_Policy 0x01.
        LL/SEC/ADV/BV-18-C: Device Privacy Mode, address resolution enabled,
            Advertising_Filter_Policy 0x03.
        LL/SEC/ADV/BV-21-C: Network Privacy Mode, address resolution disabled,
            Advertising_Filter_Policy 0x03.

    Test Steps:
        Populate both lists before enabling, configure scannable advertising with
        accept-list filtering, observe and resolve AdvA, send SCAN_REQ at T_IFS
        with the identity (BV-15/18) or RPA (BV-21), verify response presence or
        absence, and disable advertising. Repeat twenty observed events.

    Expected Outcome:
        Device mode answers identity requests; network mode ignores identity
        requests, and disabled resolution prevents RPA accept-list matching.

    Source Correction:
        BV-15-C and BV-18-C enable advertising in Step 2, before the Filter
        Accept List and resolving-list Steps 3-4, then enable it again in Step
        5. Core Vol 4, Part E, 7.8.5, 7.8.16 and 7.8.44 disallow those commands
        while advertising is enabled, so configure the lists first; BV-21-C
        already orders its setup this way. Advertising_Filter_Policy follows
        each source: 0x01 for BV-15-C, 0x03 for BV-21-C; BV-18-C names no
        value and uses 0x03 for its accept-list entry. This is a local
        sequencing correction, not SIG errata.

    Reference:
        LL.TS.p28, SEC/ADV/BV-15/18/21-C, PDF pages 1217-1219, 1223-1225,
        1231-1233, Figures 4.8-15/18/21;
        Core Vol 6, Part B, 4.7 and 6.2.3.
    """
    peer = LOWER_CENTRAL_PUBLIC_ADDRESS
    await privacy_peer.add_peer(tester, identity=peer, device=device, enabled=enabled)
    await tester.command(hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(0, peer))
    await _privacy_advertising_parameters(
        tester, peer=peer, scannable=True, policy=policy
    )
    await _privacy_advertising_enable(tester)
    scanner = peer if enabled else privacy_peer.private_address(privacy_peer.PEER_IRK)
    for _ in range(20):
        _, packet, address, _ = await _privacy_advertisement(tester, scannable=True)
        privacy_peer.check_private_address(address, privacy_peer.LOCAL_IRK)
        await _privacy_scan_request(tester, packet, address, scanner, expected=device)
    await _privacy_advertising_enable(tester, False)


async def _privacy_advertiser_scenario(tester, *, scenario, extended=False):
    peer = LOWER_CENTRAL_PUBLIC_ADDRESS
    directed = scenario in (
        'directed',
        'directed_local',
        'directed_peer',
        'directed_unknown',
        'ignore_directed',
        'device_directed',
        'target_as_initiator',
    )
    local_key = scenario not in ('none', 'peer', 'directed_peer', 'target_as_initiator')
    peer_key = scenario not in ('none', 'local', 'directed_local')
    device = scenario in ('device', 'device_directed')
    unknown = scenario in ('unknown', 'directed_unknown')
    ignored = scenario in ('ignore', 'ignore_directed') or unknown
    identity_request = not peer_key or device or ignored and not unknown
    own_type = 1 if scenario == 'none' else 2
    config_peer = hci.Address('C0:11:22:33:44:66') if unknown else peer
    await tester.command(hci.HCI_LE_Set_Resolvable_Private_Address_Timeout_Command(1))
    if scenario == 'none':
        await tester.command(
            hci.HCI_LE_Set_Random_Address_Command(privacy_peer.FALLBACK)
        )
    else:
        await privacy_peer.add_peer(
            tester,
            identity=config_peer,
            peer_irk=(bytes(range(16)) if unknown else privacy_peer.PEER_IRK)
            if peer_key
            else bytes(16),
            local_irk=privacy_peer.LOCAL_IRK if local_key else bytes(16),
            device=device,
        )
        await tester.command(
            hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(peer.address_type, peer)
        )
    await _privacy_advertising_parameters(
        tester,
        peer=config_peer,
        own_type=own_type,
        directed=directed,
        extended=extended,
        policy=0 if scenario == 'none' else 3,
    )
    await _privacy_advertising_enable(tester, extended=extended)
    wire_peer = (
        peer
        if identity_request
        else privacy_peer.private_address(privacy_peer.PEER_IRK)
    )
    # An absent record must fail resolution even if its identity is on the FAL.
    if unknown:
        wire_peer = privacy_peer.private_address(privacy_peer.PEER_IRK)
    link, address, target = await _privacy_incoming_connection(
        tester,
        peer=peer,
        wire_peer=wire_peer,
        directed=directed,
        extended=extended,
        local_irk=local_key,
        own_type=own_type,
        expect=not ignored,
        use_target=scenario == 'target_as_initiator',
        target_key=bytes(range(16)) if unknown else privacy_peer.PEER_IRK,
    )
    if ignored:
        if scenario == 'unknown':
            for _ in range(9):
                await _privacy_incoming_connection(
                    tester,
                    peer=peer,
                    wire_peer=wire_peer,
                    local_irk=local_key,
                    expect=False,
                )
        await _privacy_advertising_enable(tester, False, extended=extended)
        return
    # Device privacy's source requires maintaining the ACL for at least RPA timeout.
    if scenario == 'device':
        for _ in range(35):
            await link.exchange()
    await link.disconnect(local=True)
    if scenario in ('mutual', 'directed'):
        await tester.command(hci.HCI_LE_Set_Address_Resolution_Enable_Command(0))
        if scenario == 'mutual':
            await _privacy_advertising_enable(tester, extended=extended)
            for _ in range(20):
                await _privacy_incoming_connection(
                    tester,
                    peer=peer,
                    wire_peer=wire_peer,
                    extended=extended,
                    local_irk=True,
                    expect=False,
                )
            await _privacy_advertising_enable(tester, False, extended=extended)
        else:
            # High-duty advertising has a fresh lifetime for each source round.
            for index in range(20):
                await _privacy_advertising_enable(tester)
                await _privacy_incoming_connection(
                    tester,
                    peer=peer,
                    wire_peer=privacy_peer.private_address(
                        privacy_peer.PEER_IRK, 0x500000 + index
                    ),
                    directed=True,
                    local_irk=True,
                    expect=False,
                )
                await tester.wait(1_280_001)
                event = await tester.event(hci.HCI_LE_Connection_Complete_Event)
                assert event.status == ErrorCode.ADVERTISING_TIMEOUT_ERROR
    if scenario in ('directed_local', 'directed_peer'):
        # Establish the source's "new RPA has been generated" condition by
        # waiting for the explicitly configured one-second address lifetime.
        await tester.wait(1_000_001)
        await _privacy_advertising_enable(tester, extended=extended)
        _, _, renewed, renewed_target = await _privacy_advertisement(
            tester, directed=True, extended=extended
        )
        if local_key:
            assert renewed != address
            privacy_peer.check_private_address(renewed, privacy_peer.LOCAL_IRK)
        else:
            assert renewed == address
            assert renewed_target != target
            privacy_peer.check_private_address(renewed_target, privacy_peer.PEER_IRK)
        await _privacy_advertising_enable(tester, False, extended=extended)


async def privacy_connectable_nonresolvable(tester):
    """Network Privacy: Undirected Connectable Advertising, no IRKs.

    Initial Condition:
        A Host-assigned NRPA, accept-all connectable advertising on all channels.

    Test Steps:
        1-6. Configure the Lower Tester public address and local NRPA, advertise,
             send CONNECT_IND using observed AdvA, exchange empty data starting
             in the requested first window, and terminate through HCI.
        7. Repeat with Lower Tester static and non-resolvable private addresses.

    Expected Outcome:
        All three peer address classes connect while AdvA remains the assigned NRPA.

    Reference:
        LL.TS.p28, SEC/ADV/BV-06-C, PDF pages 1195-1196, Figure 4.8-5;
        Core Vol 6, Part B, 4.4.2.3 and 6.2.4.
    """
    for peer in (
        LOWER_CENTRAL_PUBLIC_ADDRESS,
        PEER_ADDRESS,
        hci.Address('01:AA:BB:CC:DD:EE'),
    ):
        await tester.command(
            hci.HCI_LE_Set_Random_Address_Command(privacy_peer.FALLBACK)
        )
        await _privacy_advertising_parameters(tester, peer=peer, own_type=1, policy=0)
        await _privacy_advertising_enable(tester)
        link, _, _ = await _privacy_incoming_connection(
            tester, peer=peer, wire_peer=peer, local_irk=False, own_type=1
        )
        await link.disconnect(local=True)


async def privacy_connectable_local_only(tester, *, extended=False):
    """Privacy connectable local only.

    Initial Condition:
        Standby with shared identities/IRKs configured as specified below.
        Use all primary advertising channels and a one-second RPA lifetime.

    Test Case Configuration:
        LL/SEC/ADV/BV-07-C: legacy advertising.
        LL/SEC/ADV/BV-23-C: extended advertising.

    Test Steps:
        Steps 1-4: use a peer identity, zero Peer IRK, valid Local IRK and
        identity in the Filter Accept List; enable connectable advertising.
        Steps 5-10: resolve AdvA, connect using peer identity via CONNECT_IND or
        AUX_CONNECT_REQ, verify Enhanced Connection Complete, exchange data and
        disconnect.

    Expected Outcome:
        The advertised address types, resolution and privacy-mode decisions match
        the specified vectors; successful ACLs exchange data, rejected ones do not.

    Source Correction:
        Step 8 places an identity in Peer_Resolvable_Private_Address. Core Vol
        4, Part E, 7.7.65.10 requires zero when no peer RPA was used; check zero
        and keep the identity in Peer_Address. This is a local correction, not
        SIG errata.

    Reference:
        LL.TS.p28, 4.8.3.3, PDF pages 1196-1198, Table 4.8-3, Figure 4.8-6;
        Core Vol 6, Part B, 4.7 and 6.2.2/4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_advertiser_scenario(tester, scenario='local', extended=extended)


async def privacy_connectable_mutual_irks(tester, *, extended=False):
    """Privacy connectable mutual irks.

    Initial Condition:
        Standby with shared identities/IRKs configured as specified below.
        Use all primary advertising channels and a one-second RPA lifetime.

    Test Case Configuration:
        LL/SEC/ADV/BV-08-C: legacy advertising.
        LL/SEC/ADV/BV-24-C: extended advertising.

    Test Steps:
        Steps 1-8: configure mutual IRKs and accept-list filtering, advertise a
        local RPA, connect using a peer RPA and check both actual RPAs plus
        identity in Enhanced Connection Complete. Steps 9-10: exchange data and
        disconnect. Steps 11-16: disable resolution, re-enable advertising,
        repeat twenty RPA connection requests and require neither connection nor
        data responses before disabling.

    Expected Outcome:
        The advertised address types, resolution and privacy-mode decisions match
        the specified vectors; successful ACLs exchange data, rejected ones do not.

    Reference:
        LL.TS.p28, 4.8.3.4, PDF pages 1199-1202, Table 4.8-4, Figures 4.8-7/8;
        Core Vol 6, Part B, 4.7 and 6.2.2/4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_advertiser_scenario(tester, scenario='mutual', extended=extended)


async def privacy_connectable_peer_only(tester, *, extended=False):
    """Privacy connectable peer only.

    Initial Condition:
        Standby with shared identities/IRKs configured as specified below.
        Use all primary advertising channels and a one-second RPA lifetime.

    Test Steps:
        Steps 1-4: install Peer IRK with zero Local IRK and the peer identity in
        the accept list; advertise with own type 2. Steps 5-8: require public
        AdvA, resolve peer-RPA InitA in CONNECT_IND, establish the ACL, exchange
        empty data and disconnect.

    Expected Outcome:
        The advertised address types, resolution and privacy-mode decisions match
        the specified vectors; successful ACLs exchange data, rejected ones do not.

    Reference:
        LL.TS.p28, SEC/ADV/BV-09-C, PDF pages 1203-1205, Figure 4.8-9;
        Core Vol 6, Part B, 4.7 and 6.2.2/4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_advertiser_scenario(tester, scenario='peer', extended=extended)


async def privacy_connectable_unknown_peer(tester, *, extended=False):
    """Privacy connectable unknown peer.

    Initial Condition:
        Standby with shared identities/IRKs configured as specified below.
        Use all primary advertising channels and a one-second RPA lifetime.

    Test Steps:
        Steps 1-6: configure the local IRK with an unrelated resolving-list
        identity and put the actual peer identity only in the accept list;
        enable local-RPA advertising. Steps 7-9: repeat ten received
        advertisements and peer-RPA connection requests. Verify every attempt is
        rejected without a connection event or data response.

    Expected Outcome:
        The advertised address types, resolution and privacy-mode decisions match
        the specified vectors; successful ACLs exchange data, rejected ones do not.

    Reference:
        LL.TS.p28, SEC/ADV/BV-10-C, PDF pages 1205-1207, Figure 4.8-10;
        Core Vol 6, Part B, 4.7 and 6.2.2/4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_advertiser_scenario(tester, scenario='unknown', extended=extended)


async def privacy_directed_mutual_irks(tester, *, extended=False):
    """Privacy directed mutual irks.

    Initial Condition:
        Standby with shared identities/IRKs configured as specified below.
        Use all primary advertising channels and a one-second RPA lifetime.

    Test Steps:
        Steps 1-8: install mutual IRKs, enable resolution and high-duty directed
        advertising, verify local-RPA AdvA and peer-RPA TargetA, connect with a
        separately generated peer RPA, exchange data and disconnect. Steps 9-14:
        disable resolution and perform twenty high-duty advertising sessions,
        send a different peer RPA in each request, require no accepted ACL/data
        response and Advertising Timeout status 0x3C for each session.

    Expected Outcome:
        The advertised address types, resolution and privacy-mode decisions match
        the specified vectors; successful ACLs exchange data, rejected ones do not.

    Source Correction:
        The source calls the directed target InitA; Core Vol 6, Part B, 2.3.1.2
        calls it TargetA. Use that name while retaining InitA for CONNECT_IND.
        This is a local correction, not SIG errata.

    Reference:
        LL.TS.p28, 4.8.3.4, SEC/ADV/BV-11-C, PDF pages 1207-1209, Figure 4.8-11;
        Core Vol 6, Part B, 4.7 and 6.2.2/4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_advertiser_scenario(tester, scenario='directed', extended=extended)


async def privacy_directed_local_only(tester, *, extended=False):
    """Privacy directed local only.

    Initial Condition:
        Standby with shared identities/IRKs configured as specified below.
        Use all primary advertising channels and a one-second RPA lifetime.

    Test Case Configuration:
        LL/SEC/ADV/BV-12-C: legacy advertising.
        LL/SEC/ADV/BV-25-C: extended advertising.

    Test Steps:
        Steps 1-8: install zero Peer IRK and nonzero Local IRK, enable directed
        advertising, require local-RPA AdvA and peer identity TargetA, connect
        using that identity and verify Enhanced Connection Complete. Steps 9-10:
        exchange data and disconnect. Steps 11-13: wait for the configured RPA
        lifetime, re-enable and require renewed AdvA.

    Expected Outcome:
        The advertised address types, resolution and privacy-mode decisions match
        the specified vectors; successful ACLs exchange data, rejected ones do not.

    Source Correction:
        Step 8 puts the peer identity in Peer_Resolvable_Private_Address; Core
        Vol 4, Part E, 7.7.65.10 requires zero for an identity-only peer. Check
        zero, retaining Peer_Address as identity. This is a local correction,
        not SIG errata.

    Reference:
        LL.TS.p28, 4.8.3.5, PDF pages 1209-1212, Table 4.8-5, Figure 4.8-12;
        Core Vol 6, Part B, 4.7 and 6.2.2/4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_advertiser_scenario(
        tester, scenario='directed_local', extended=extended
    )


async def privacy_directed_peer_only(tester, *, extended=False):
    """Privacy directed peer only.

    Initial Condition:
        Standby with shared identities/IRKs configured as specified below.
        Use all primary advertising channels and a one-second RPA lifetime.

    Test Steps:
        Steps 1-6: install nonzero Peer IRK and zero Local IRK, enable directed
        advertising with own type 2, require public AdvA and peer-RPA TargetA,
        accept a peer-RPA request and exchange data. Steps 7-10: disconnect,
        wait for timeout, re-enable and require a new resolvable TargetA.

    Expected Outcome:
        The advertised address types, resolution and privacy-mode decisions match
        the specified vectors; successful ACLs exchange data, rejected ones do not.

    Source Correction:
        Step 8 says both addresses renew, while Local IRK is zero and the
        expected outcome keeps AdvA as identity. Core Vol 6, Part B, 4.7 uses
        the local Identity Address when the Local IRK is all zeros, and 6.2.2
        requires a Peer-IRK RPA in TargetA; require only TargetA to renew.
        This is a local correction, not SIG errata.

    Reference:
        LL.TS.p28, SEC/ADV/BV-13-C, PDF pages 1213-1215, Figure 4.8-13;
        Core Vol 6, Part B, 4.7 and 6.2.2; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_advertiser_scenario(
        tester, scenario='directed_peer', extended=extended
    )


async def privacy_directed_unknown_peer(tester, *, extended=False):
    """Privacy directed unknown peer.

    Initial Condition:
        Standby with shared identities/IRKs configured as specified below.
        Use all primary advertising channels and a one-second RPA lifetime.

    Test Steps:
        Steps 1-4: configure an unrelated identity/Peer IRK with the local IRK
        and enable directed advertising towards that record. Steps 5-6: resolve
        local AdvA and submit a connection request from an RPA generated by the
        actual peer, whose IRK is absent. Require no accepted connection or data
        response.

    Expected Outcome:
        The advertised address types, resolution and privacy-mode decisions match
        the specified vectors; successful ACLs exchange data, rejected ones do not.

    Reference:
        LL.TS.p28, SEC/ADV/BV-14-C, PDF pages 1215-1217, Figure 4.8-14;
        Core Vol 6, Part B, 4.7 and 6.2.2/4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_advertiser_scenario(
        tester, scenario='directed_unknown', extended=extended
    )


async def privacy_connectable_identity_rejected(tester, *, extended=False):
    """Privacy connectable identity rejected.

    Initial Condition:
        Standby with shared identities/IRKs configured as specified below.
        Use all primary advertising channels and a one-second RPA lifetime.

    Test Steps:
        Steps 1-4: install mutual IRKs in network privacy mode, add peer
        identity to the accept list and enable connectable advertising. Steps
        5-8: resolve local AdvA, send CONNECT_IND with identity InitA, transmit
        candidate data in the requested first window and require no response or
        accepted connection. Step 9: disable advertising.

    Expected Outcome:
        The advertised address types, resolution and privacy-mode decisions match
        the specified vectors; successful ACLs exchange data, rejected ones do not.

    Reference:
        LL.TS.p28, SEC/ADV/BV-16-C, PDF pages 1219-1221, Figure 4.8-16;
        Core Vol 6, Part B, 4.7 and 6.2.2/4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_advertiser_scenario(tester, scenario='ignore', extended=extended)


async def privacy_directed_identity_rejected(tester, *, extended=False):
    """Privacy directed identity rejected.

    Initial Condition:
        Standby with shared identities/IRKs configured as specified below.
        Use all primary advertising channels and a one-second RPA lifetime.

    Test Steps:
        Steps 1-4: configure mutual IRKs in network privacy mode and enable
        directed advertising. Steps 5-8: resolve local AdvA, send identity InitA
        in CONNECT_IND and candidate data in the first window; require no
        connection event or data response. Step 9: disable advertising.

    Expected Outcome:
        The advertised address types, resolution and privacy-mode decisions match
        the specified vectors; successful ACLs exchange data, rejected ones do not.

    Reference:
        LL.TS.p28, SEC/ADV/BV-17-C, PDF pages 1221-1223, Figure 4.8-17;
        Core Vol 6, Part B, 4.7 and 6.2.2/4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_advertiser_scenario(
        tester, scenario='ignore_directed', extended=extended
    )


async def privacy_connectable_device_mode(tester, *, extended=False):
    """Privacy connectable device mode.

    Initial Condition:
        Standby with shared identities/IRKs configured as specified below.
        Use all primary advertising channels and a one-second RPA lifetime.

    Test Case Configuration:
        LL/SEC/ADV/BV-19-C: legacy advertising.
        LL/SEC/ADV/BV-26-C: extended advertising.

    Test Steps:
        Steps 1-8: install mutual IRKs in device mode and accept-list filtering,
        enable advertising, connect using the peer identity and verify local RPA
        plus zero peer RPA in Enhanced Connection Complete. Steps 9-11: exchange
        empty data for at least the one-second address lifetime, then terminate
        through HCI.

    Expected Outcome:
        The advertised address types, resolution and privacy-mode decisions match
        the specified vectors; successful ACLs exchange data, rejected ones do not.

    Reference:
        LL.TS.p28, 4.8.3.6, PDF pages 1225-1228, Table 4.8-6, Figure 4.8-19;
        Core Vol 6, Part B, 4.7 and 6.2.2/4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_advertiser_scenario(tester, scenario='device', extended=extended)


async def privacy_directed_device_mode(tester, *, extended=False):
    """Privacy directed device mode.

    Initial Condition:
        Standby with shared identities/IRKs configured as specified below.
        Use all primary advertising channels and a one-second RPA lifetime.

    Test Steps:
        Steps 1-6: install mutual IRKs in device mode, enable directed
        advertising with local-RPA AdvA and peer-RPA TargetA, then connect using
        peer identity InitA. Steps 7-8: verify the accepted ACL, exchange empty
        data and terminate through HCI.

    Expected Outcome:
        The advertised address types, resolution and privacy-mode decisions match
        the specified vectors; successful ACLs exchange data, rejected ones do not.

    Reference:
        LL.TS.p28, SEC/ADV/BV-20-C, PDF pages 1229-1231, Figure 4.8-20;
        Core Vol 6, Part B, 4.7 and 6.2.2/4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_advertiser_scenario(
        tester, scenario='device_directed', extended=extended
    )


async def privacy_directed_target_as_initiator(tester, *, extended=False):
    """Privacy directed target as initiator.

    Initial Condition:
        Standby with shared identities/IRKs configured as specified below.
        Use all primary advertising channels and a one-second RPA lifetime.

    Test Steps:
        Steps 1-4: install Peer IRK with zero Local IRK, enable directed
        advertising using own type 2 and verify public AdvA plus peer-RPA
        TargetA. Step 5: copy the exact received TargetA into CONNECT_IND InitA
        and retain observed AdvA. Steps 6-7: require successful connection,
        exchange empty data and terminate.

    Expected Outcome:
        The advertised address types, resolution and privacy-mode decisions match
        the specified vectors; successful ACLs exchange data, rejected ones do not.

    Reference:
        LL.TS.p28, SEC/ADV/BV-22-C, PDF pages 1233-1235, Figure 4.8-22;
        Core Vol 6, Part B, 4.7 and 6.2.2/4; Vol 4, Part E, 7.7.65.10.
    """
    await _privacy_advertiser_scenario(
        tester, scenario='target_as_initiator', extended=extended
    )


async def privacy_duplicate_advertiser(tester):
    """Reject Existing Connection Request, Different Peer RPA.

    Initial Condition:
        The Lower Tester and IUT are not bonded. A public peer identity will be
        installed after the first ACL, matching the source's key-distribution stage.

    Test Steps:
        1-5. Advertise on channel 37, accept a peer-RPA CONNECT_IND with a 50-ms
             interval and verify the first Connection Complete.
        6-9. Maintain acknowledged empty data, add the peer identity/IRK, enable
             resolution and re-enable advertising with identity accept-list filtering.
        10-13. Receive continued advertising and send CONNECT_IND using a new
               RPA of the same peer and a different Access Address. Require no
               successful completion and continued advertising.
        14-16. Independently schedule both candidate ACLs for five seconds;
               require acknowledged replies only for the original connection.

    Expected Outcome:
        The newly learned IRK identifies the earlier unresolved RPA as the same
        peer, so a different peer RPA cannot create another connection.

    Source Correction:
        The initial condition requests accept-list filtering before any IRK is
        available, which would prevent the initial peer RPA from matching its
        identity. Allow the unbonded first connection, then enable the specified
        filtering after steps 7-8. Core Vol 6, Part B, 4.7 requires resolution
        before identity filtering. This is a local setup correction, not SIG errata.

    Reference:
        LL.TS.p28, SEC/ADV/BI-01-C, PDF pages 1235-1238, Figures 4.8-23/24;
        Core Vol 6, Part B, 4.5 and 4.7.
    """
    await check_duplicate_advertiser_request(tester, privacy=True)


async def privacy_duplicate_initiator(tester):
    """Duplicate Connection Request, Different Peer RPA.

    Initial Condition:
        Unbonded peer advertising with an RPA; no Filter Accept List is used.

    Test Steps:
        1-5. Initiate towards the received raw RPA, send CONNECT_IND at T_IFS and
             verify Connection Complete and the initial ACL parameters.
        6-8. Maintain empty-data acknowledgements, add the peer identity/IRK and
             enable address resolution.
        9-11. Advertise a different RPA on all primary channels, send a second
              Create Connection for it, and maintain the original ACL for five seconds.
        12. Accept status 0x0B, a completion with 0x0B after successful status,
            or successful status followed by no completion and later successful
            cancellation with completion status 0x02.

    Expected Outcome:
        No second CONNECT_IND is transmitted to the resolved existing peer;
        the original ACL continues acknowledged data for the entire observation.

    Reference:
        LL.TS.p28, SEC/INI/BI-01-C, PDF pages 1240-1243, Figures 4.8-26/27;
        Core Vol 6, Part B, 4.5 and 4.7.
    """
    await check_duplicate_initiator_request(tester, privacy=True)


async def privacy_random_address_scanning(tester, *, encrypt):
    """Random Address Scanning, with optional LE Encrypt verification.

    Initial Condition:
        The common IR is used to derive IRK with d1(IR, 1, 0); the IUT actively
        scans with one Host-generated random address.

    Test Case Configuration:
        LL/SEC/SCN/BV-01-C: include LE Encrypt verification of the RPA hash.
        LL/SEC/SCN/BV-02-C: omit the optional LE Encrypt verification.

    Test Steps:
        1-3. Set the local random address, enable active scanning using own type 1,
             and configure the Lower Tester with an IRK-generated random address.
        4-8. Advertise ADV_IND, observe SCAN_REQ with exact local/advertiser
             addresses, respond at T_IFS and verify both Host reports.
        9-10. Attempt Set Random Address while scanning and require Command
              Disallowed; disable scanning successfully.
        11. For BV-01, use LE Encrypt with IRK and padded prand and verify the
            truncated ah result equals the advertiser address's hash.

    Expected Outcome:
        ScanA equals the assigned random address, reporting remains functional,
        and a live address change is rejected. BV-01 also verifies the AES hash.

    Source Correction:
        Step 11 swaps the address halves: Core Vol 6, Part B, 1.3.2.2 puts prand
        in the most significant three address octets and hash in the least
        significant three. HCI octet strings are little endian, so encrypt
        address[3:6] plus thirteen zero octets and compare result[:3] to
        address[:3]. This is a local correction, not adopted SIG errata.

    Reference:
        LL.TS.p28, Section 4.8.4.1, PDF pages 1238-1240, Table 4.8-7,
        Figure 4.8-25; Core Vol 3, Part H, 2.2.2; Vol 6, Part B, 4.4.3.2.
    """
    irk = aes(ROOT, (1).to_bytes(16, 'little'))
    address = privacy_peer.private_address(irk)
    await tester.command(hci.HCI_LE_Set_Random_Address_Command(privacy_peer.FALLBACK))
    await privacy_peer.scanning(tester, active=True, own_type=1)
    reports, requests = await privacy_peer.observe_advertising(
        tester,
        active=True,
        address=address,
        kind=AdvertisingPduType.ADV_IND,
        duration_us=400_000,
    )
    assert reports and requests
    assert all(
        request.pdu[2:8] == bytes(privacy_peer.FALLBACK)
        and request.pdu[8:14] == bytes(address)
        for request in requests
    )
    assert any(report.event_type == 4 for report in reports)
    assert all(report.address == address for report in reports)
    await tester.command(
        hci.HCI_LE_Set_Random_Address_Command(PEER_ADDRESS),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )
    await privacy_peer.scan_enable(tester, False)
    if encrypt:
        result = await tester.command(
            hci.HCI_LE_Encrypt_Command(irk, bytes(address)[3:] + bytes(13))
        )
        assert result.encrypted_data[:3] == bytes(address)[:3]


PROCEDURES = ProcedureMap()

# Verify that a Peripheral performs an encryption-mode change using the correct
# encryption. The Lower Tester maintains a Central connection and initiates encryption
# setup.
PROCEDURES |= procedure_cases(
    peripheral_encryption_mode_setup,
    {
        'LL/SEC/PER/BV-01-C': (
            'Peripheral Encryption Mode Setup',
            {},
        ),
    },
)

# Verify that a Peripheral performs encryption pause without transmitting user data
# during the pause. The Lower Tester maintains the Central connection, pauses encryption
# and restarts it with a new session key.
PROCEDURES |= procedure_cases(
    peripheral_pause_encryption,
    {
        'LL/SEC/PER/BV-02-C': (
            'Peripheral Pause Encryption',
            {'send_data': False},
        ),
        'LL/SEC/PER/BV-03-C': (
            'Peripheral Pause Encryption Sending Data',
            {'send_data': True},
        ),
    },
)

# Verify that a Peripheral sends the correct LL_REJECT_IND during encryption start when
# its Host supplies no LTK. The Lower Tester acts as Central, maintains the connection
# and initiates encryption.
PROCEDURES |= procedure_cases(
    peripheral_sending_ll_reject_ind,
    {
        'LL/SEC/PER/BV-04-C': (
            'Peripheral Sending LL_REJECT_IND',
            {},
        ),
    },
)

# Verify that a Peripheral receives encrypted data. The Lower Tester maintains the
# encrypted Central connection and sends unfragmented LL payloads, while the Upper
# Tester verifies the delivered data.
PROCEDURES |= procedure_cases(
    peripheral_receiving_encrypted_data,
    {
        'LL/SEC/PER/BV-05-C': (
            'Peripheral Receiving Encrypted Data',
            {},
        ),
    },
)

# Verify that an encrypted Peripheral initiates LE Ping before the default 30-second
# authenticated payload timeout when no valid-MIC packet arrives. The Central Lower
# Tester responds, preventing forged acknowledgements from keeping an unauthenticated
# link alive indefinitely.
PROCEDURES |= procedure_cases(
    initiate_le_ping_procedure_when_encryption_is_enabled,
    {
        'LL/SEC/PER/BV-06-C': (
            'Initiate LE Ping procedure when encryption is enabled',
            {},
        ),
    },
)

# Verify that an encrypted Peripheral responds to the Lower Tester's LL_PING_REQ with
# LL_PING_RSP.
PROCEDURES |= procedure_cases(
    responding_to_ll_ping_req,
    {
        'LL/SEC/PER/BV-07-C': (
            'Responding to LL_PING_REQ',
            {},
        ),
    },
)

# Verify that an encrypted Peripheral reports HCI Authenticated Payload Timeout Expired
# when the Lower Tester supplies no valid-MIC packet within the configured authenticated
# payload timeout.
PROCEDURES |= procedure_cases(
    no_response_to_ll_ping_req,
    {
        'LL/SEC/PER/BV-08-C': (
            'No response to LL_PING_REQ',
            {},
        ),
    },
)

# Verify use of a Host-configured authenticated payload timeout outside the default
# range, described as greater than 100 seconds or less than five seconds. Execute the
# less-than-five-second branch specified by this procedure's Step 1.
PROCEDURES |= procedure_cases(
    modified_authentication_payload_timeout,
    {
        'LL/SEC/PER/BV-09-C': (
            'Modified Authentication Payload Timeout',
            {},
        ),
    },
)

# Verify that an encrypted Peripheral initiates Ping before its default 30-second
# authenticated payload timeout even when its Central peer does not support LE Ping. The
# Lower Tester returns an authenticated unknown response, preventing unauthenticated
# acknowledgements from sustaining the connection indefinitely.
PROCEDURES |= procedure_cases(
    initiate_le_ping_procedure_when_the_other_side_does_not_support_the_procedure,
    {
        'LL/SEC/PER/BV-10-C': (
            (
                'Initiate LE Ping procedure when the other side does not '
                'support the procedure'
            ),
            {},
        ),
    },
)

# Verify that a Peripheral rejects encryption with a correctly formed LL_REJECT_IND or
# LL_REJECT_EXT_IND when its Host cannot provide the LTK. The Lower Tester maintains a
# Central connection and initiates encryption; the Upper Tester supplies a negative LTK
# reply.
PROCEDURES |= procedure_cases(
    peripheral_sending_ll_reject_ext_ind,
    {
        'LL/SEC/PER/BV-11-C': (
            'Peripheral Sending LL_REJECT_EXT_IND',
            {},
        ),
    },
)

# Verify that a Peripheral recovers from an incomplete encryption setup when the Central
# omits LL_START_ENC_RSP. The Lower Tester maintains the Central connection but
# deliberately leaves encryption setup incomplete.
PROCEDURES |= procedure_cases(
    peripheral_encryption_setup_missing_response,
    {
        'LL/SEC/PER/BI-01-C': (
            'Peripheral Encryption Setup: Missing Response',
            {},
        ),
    },
)

# Verify that a Peripheral terminates an encrypted connection on a MIC or
# authenticated-header failure. The Central Lower Tester first transfers valid encrypted
# data, then injects corrupted encrypted DATA packets.
PROCEDURES |= procedure_cases(
    peripheral_mic_failure_corrupted_mic,
    {
        'LL/SEC/PER/BI-03-C': (
            'Peripheral MIC Failure: Corrupted MIC',
            {'header_error': False},
        ),
        'LL/SEC/PER/BI-04-C': (
            'Peripheral MIC Failure: Corrupted Header',
            {'header_error': True},
        ),
    },
)

# Verify that a Peripheral drops the link instead of normally responding to the
# unexpected PDU selected by Table 4.8-10 during encryption start. The Lower Tester acts
# as Central.
PROCEDURES |= procedure_cases(
    peripheral_receiving_unexpected_pdu_during_encryption_start,
    {
        'LL/SEC/PER/BI-05-C': (
            (
                'Peripheral Receiving unexpected PDU during encryption '
                'start - LL_VERSION_IND PDU'
            ),
            {'data': False},
        ),
        'LL/SEC/PER/BI-07-C': (
            (
                'Peripheral Receiving unexpected PDU during encryption '
                'start - Unencrypted Data PDU'
            ),
            {'data': True},
        ),
    },
)

# Verify that a Central requests and completes encryption-mode setup with correct
# encryption. The Peripheral Lower Tester accepts the encryption request, independently
# derives the session key and verifies encrypted control/data traffic.
PROCEDURES |= procedure_cases(
    central_encryption_mode_setup,
    {
        'LL/SEC/CEN/BV-01-C': (
            'Central Encryption Mode Setup',
            {},
        ),
    },
)

# Verify that a Central performs encryption pause and restart. The Peripheral Lower
# Tester accepts the pause request and, for the data variant, checks delivery before and
# after encryption resumes.
PROCEDURES |= procedure_cases(
    central_pause_encryption,
    {
        'LL/SEC/CEN/BV-02-C': (
            'Central Pause Encryption',
            {'send_data': False},
        ),
        'LL/SEC/CEN/BV-05-C': (
            'Central Pause Encryption: Sending Data before LL_PAUSE_ENC_RSP',
            {'send_data': True},
        ),
    },
)

# Verify that a Central handles LL_REJECT_IND during encryption start and notifies its
# Host. The Peripheral Lower Tester maintains the connection and rejects the encryption
# request.
PROCEDURES |= procedure_cases(
    central_receiving_ll_reject_ind,
    {
        'LL/SEC/CEN/BV-03-C': (
            'Central Receiving LL_REJECT_IND',
            {},
        ),
    },
)

# Verify that a Central delivers DATA received after LL_ENC_REQ and before LL_ENC_RSP
# without terminating the connection. The Peripheral Lower Tester sends plaintext before
# its encryption response and encrypted data after setup completes.
PROCEDURES |= procedure_cases(
    central_encryption_sending_data_before_ll_enc_rsp,
    {
        'LL/SEC/CEN/BV-04-C': (
            'Central Encryption: Sending Data before LL_ENC_RSP',
            {},
        ),
    },
)

# Verify that an encrypted Central initiates LE Ping before the default 30-second
# authenticated payload timeout when no valid-MIC packet arrives. The Peripheral Lower
# Tester responds so forged empty acknowledgements cannot maintain an unauthenticated
# connection indefinitely.
PROCEDURES |= procedure_cases(
    initiate_le_ping_procedure_when_encryption_is_enabled_ll_sec_cen_bv_06_c,
    {
        'LL/SEC/CEN/BV-06-C': (
            'Initiate LE Ping procedure when encryption is enabled',
            {},
        ),
    },
)

# Verify that an encrypted Central responds to the Peripheral Lower Tester's LL_PING_REQ
# with LL_PING_RSP.
PROCEDURES |= procedure_cases(
    responding_to_ll_ping_req_ll_sec_cen_bv_07_c,
    {
        'LL/SEC/CEN/BV-07-C': (
            'Responding to LL_PING_REQ',
            {},
        ),
    },
)

# Verify that an encrypted Central reports HCI Authenticated Payload Timeout Expired
# when no valid-MIC packet arrives from the Lower Tester within the configured timeout.
PROCEDURES |= procedure_cases(
    no_response_to_ll_ping_req_ll_sec_cen_bv_08_c,
    {
        'LL/SEC/CEN/BV-08-C': (
            'No response to LL_PING_REQ',
            {},
        ),
    },
)

# Verify a Central's use of a Host-configured authenticated payload timeout greater than
# 100 s or less than 5 s. Execute the less-than-five-second branch explicitly specified
# by Step 1.
PROCEDURES |= procedure_cases(
    modified_authentication_payload_timeout_ll_sec_cen_bv_09_c,
    {
        'LL/SEC/CEN/BV-09-C': (
            'Modified Authentication Payload Timeout',
            {},
        ),
    },
)

# Verify that an encrypted Central initiates LE Ping before the default 30-second
# authenticated timeout even when its peer does not support Ping. The Peripheral Lower
# Tester responds with encrypted LL_UNKNOWN_RSP, providing a fresh authenticated packet
# despite lacking LE Ping support.
PROCEDURES |= procedure_cases(
    initiate_le_ping_when_peer_does_not_support_it_cen_bv_10,
    {
        'LL/SEC/CEN/BV-10-C': (
            (
                'Initiate LE Ping procedure when the other side does not '
                'support the procedure'
            ),
            {},
        ),
    },
)

# Verify that a Central handles LL_REJECT_EXT_IND during encryption start and notifies
# its Host. The Peripheral Lower Tester first responds to LL_ENC_REQ, then rejects the
# procedure with PIN or Key Missing.
PROCEDURES |= procedure_cases(
    central_receiving_ll_reject_ext_ind,
    {
        'LL/SEC/CEN/BV-11-C': (
            'Central Receiving LL_REJECT_EXT_IND',
            {},
        ),
    },
)

# Verify that a Central completes encryption start correctly when it receives an
# overlapping LL_VERSION_IND before LL_ENC_RSP.
PROCEDURES |= procedure_cases(
    central_start_encryption_overlapping_procedure,
    {
        'LL/SEC/CEN/BV-12-C': (
            'Central Start Encryption: Overlapping Procedure',
            {},
        ),
    },
)

# Verify that a Central completes encryption start correctly when an
# LL_PERIPHERAL_FEATURE_REQ overlaps it, returning the deferred feature response only
# after encryption completes.
PROCEDURES |= procedure_cases(
    central_start_encryption_overlapping_procedure_with_ll_peripheral_feature_req,
    {
        'LL/SEC/CEN/BV-13-C': (
            (
                'Central Start Encryption: Overlapping Procedure with '
                'LL_PERIPHERAL_FEATURE_REQ'
            ),
            {},
        ),
    },
)

# Verify that a Central drops the link instead of normally responding to LL_VERSION_IND
# received after LL_ENC_RSP during encryption start. The Lower Tester acts as
# Peripheral.
PROCEDURES |= procedure_cases(
    central_receiving_unexpected_pdu_during_encryption_start,
    {
        'LL/SEC/CEN/BV-14-C': (
            'Central Receiving unexpected PDU during encryption start',
            {},
        ),
    },
)

# Verify that a Central recovers from a missing LL_ENC_RSP during encryption setup. The
# Peripheral Lower Tester acknowledges the request while deliberately leaving the
# control procedure incomplete.
PROCEDURES |= procedure_cases(
    central_encryption_setup_missing_response,
    {
        'LL/SEC/CEN/BI-01-C': (
            'Central Encryption Setup: Missing Response',
            {},
        ),
    },
)

# Verify that a Central recovers when the Peripheral Lower Tester sends LL_ENC_RSP but
# omits LL_START_ENC_REQ, while maintaining valid empty acknowledgements until the
# procedure times out.
PROCEDURES |= procedure_cases(
    central_encryption_setup_missing_request,
    {
        'LL/SEC/CEN/BI-03-C': (
            'Central Encryption Setup: Missing Request',
            {},
        ),
    },
)

# Verify that a Central recovers when the Peripheral Lower Tester omits the final
# LL_START_ENC_RSP. It acknowledges received packets normally but leaves encryption
# start incomplete until the LL response timeout.
PROCEDURES |= procedure_cases(
    central_encryption_setup_missing_acknowledgement,
    {
        'LL/SEC/CEN/BI-04-C': (
            'Central Encryption Setup: Missing Acknowledgement',
            {},
        ),
    },
)

# Verify that a Central terminates an encrypted connection on an authentication failure
# in a Peripheral DATA packet. The Lower Tester exchanges valid data before corrupting
# the selected MIC or header field.
PROCEDURES |= procedure_cases(
    central_mic_failure,
    {
        'LL/SEC/CEN/BI-05-C': (
            'Central MIC Failure: Corrupted MIC',
            {'header_error': False},
        ),
        'LL/SEC/CEN/BI-06-C': (
            'Central MIC Failure: Corrupted Header',
            {'header_error': True},
        ),
    },
)

# Verify that a Central terminates when the Peripheral Lower Tester sends DATA during
# the sensitive restart phase of encryption pause. Establish encryption, accept pause,
# then send DATA instead of START_ENC_REQ.
PROCEDURES |= procedure_cases(
    central_pause_encryption_sending_data,
    {
        'LL/SEC/CEN/BI-07-C': (
            'Central Pause Encryption Sending Data',
            {},
        ),
    },
)

# Verify that a Central terminates on procedure response timeout when the Peripheral
# sends DATA before LL_ENC_RSP and then withholds that response indefinitely.
PROCEDURES |= procedure_cases(
    central_encryption_sending_data_and_not_response,
    {
        'LL/SEC/CEN/BI-08-C': (
            'Central Encryption: Sending Data and Not Response',
            {},
        ),
    },
)

# Verify that a Central terminates when the Peripheral sends DATA instead of
# LL_START_ENC_REQ after accepting the encryption setup request.
PROCEDURES |= procedure_cases(
    central_encryption_sending_data_and_not_request,
    {
        'LL/SEC/CEN/BI-09-C': (
            'Central Encryption: Sending Data and Not Request',
            {},
        ),
    },
)


# Verify that nonconnectable advertising contains the Host-assigned private address.
# The Lower Tester observes the advertised packet contents.
PROCEDURES |= procedure_cases(
    privacy_nonconnectable_advertising,
    {
        'LL/SEC/ADV/BV-02-C': (
            'Privacy – Non-connectable Undirected Advertising, '
            'non-resolvable private address',
            {},
        ),
        'LL/SEC/ADV/BV-27-C': (
            'Privacy – Non-connectable Undirected Advertising, Resolvable '
            'Private Address, Random RPA Timeout',
            {'resolvable': True},
        ),
    },
)

# Verify scannable undirected advertising with a Host-assigned non-resolvable private
# address and a successful scan-request/response exchange.
PROCEDURES |= procedure_cases(
    privacy_scannable_nonresolvable,
    {
        'LL/SEC/ADV/BV-04-C': (
            'Network Privacy - Scannable Advertising, non-resolvable private address',
            {},
        ),
    },
)

# Verify scannable advertising with resolvable private addresses, address renewal after
# timeout, and suppression of unresolved scan requests when resolution is disabled.
PROCEDURES |= procedure_cases(
    privacy_scannable_rpa_timeout,
    {
        'LL/SEC/ADV/BV-28-C': (
            'Network Privacy – Scannable Advertising, resolvable private '
            'address, Random RPA Timeout',
            {},
        ),
    },
)

# Verify scanner identity rejection in network privacy, acceptance in device privacy,
# and rejection of peer RPAs when disabled resolution prevents accept-list matching.
PROCEDURES |= procedure_cases(
    privacy_scannable_identity_policy,
    {
        'LL/SEC/ADV/BV-15-C': (
            'Network Privacy - Scannable Advertising, resolvable private '
            'address, Ignore Identity Address',
            {'policy': 1},
        ),
        'LL/SEC/ADV/BV-18-C': (
            'Device Privacy - Scannable Advertising, resolvable private '
            'address, Accept Identity Address',
            {'device': True},
        ),
        'LL/SEC/ADV/BV-21-C': (
            'Network Privacy - Scannable Advertising, resolvable private '
            'address, Ignore scanner RPA',
            {'enabled': False},
        ),
    },
)

# Verify that an advertiser using a non-resolvable private AdvA accepts a connection
# and exchanges data as a Peripheral.
PROCEDURES |= procedure_cases(
    privacy_connectable_nonresolvable,
    {
        'LL/SEC/ADV/BV-06-C': (
            'Network Privacy - Undirected Connectable Advertising no Local '
            'IRK, no peer IRK',
            {},
        ),
    },
)

# Verify connection with a local-RPA AdvA and peer-identity InitA when only the Local
# IRK is configured and the peer identity is in the Filter Accept List.
PROCEDURES |= procedure_cases(
    privacy_connectable_local_only,
    {
        'LL/SEC/ADV/BV-07-C': (
            'Network Privacy - Undirected Connectable Advertising with '
            'Local IRK, no peer IRK, CONNECT_IND',
            {},
        ),
    },
)

# Verify connection with local and peer RPAs when both IRKs are configured, and
# rejection of the peer RPA after address resolution is disabled.
PROCEDURES |= procedure_cases(
    privacy_connectable_mutual_irks,
    {
        'LL/SEC/ADV/BV-08-C': (
            'Network Privacy - Undirected Connectable Advertising with '
            'Local IRK and Peer IRK, CONNECT_IND',
            {},
        ),
    },
)

# Verify connection with identity AdvA and peer-RPA InitA when the resolving list
# contains a Peer IRK and a zero Local IRK.
PROCEDURES |= procedure_cases(
    privacy_connectable_peer_only,
    {
        'LL/SEC/ADV/BV-09-C': (
            'Network Privacy - Undirected Connectable Advertising without '
            'Local IRK and with peer IRK',
            {},
        ),
    },
)

# Verify that accept-list filtering rejects connection requests from a peer whose RPA
# cannot be resolved with the configured Peer IRK.
PROCEDURES |= procedure_cases(
    privacy_connectable_unknown_peer,
    {
        'LL/SEC/ADV/BV-10-C': (
            'Network Privacy - Undirected Connectable Advertising using '
            'Resolving List and Peer Device Identity not in the List',
            {},
        ),
    },
)

# Verify directed connection establishment with local and peer RPAs, address renewal,
# and rejection of the peer RPA after resolution is disabled.
PROCEDURES |= procedure_cases(
    privacy_directed_mutual_irks,
    {
        'LL/SEC/ADV/BV-11-C': (
            'Network Privacy - Directed Connectable Advertising using local '
            'and remote IRK',
            {},
        ),
    },
)

# Verify directed connection establishment using local-RPA AdvA and peer-identity
# TargetA with only a Local IRK, and renewal of AdvA after timeout.
PROCEDURES |= procedure_cases(
    privacy_directed_local_only,
    {
        'LL/SEC/ADV/BV-12-C': (
            'Network Privacy - Directed Connectable Advertising with local '
            'IRK but without remote IRK, CONNECT_IND',
            {},
        ),
    },
)

# Verify directed connection establishment using identity AdvA and peer-RPA TargetA
# with only a Peer IRK, and renewal of TargetA after timeout.
PROCEDURES |= procedure_cases(
    privacy_directed_peer_only,
    {
        'LL/SEC/ADV/BV-13-C': (
            'Network Privacy - Directed Connectable Advertising without '
            'local IRK but with remote IRK',
            {},
        ),
    },
)

# Verify that directed advertising rejects a connection request from a peer whose RPA
# does not match the configured resolving record.
PROCEDURES |= procedure_cases(
    privacy_directed_unknown_peer,
    {
        'LL/SEC/ADV/BV-14-C': (
            'Network Privacy - Directed Connectable Advertising using '
            'Resolving List and Peer Device Identity not in the List',
            {},
        ),
    },
)

# Verify that network privacy rejects an identity-address connection request when the
# peer identity has an associated IRK in the resolving list.
PROCEDURES |= procedure_cases(
    privacy_connectable_identity_rejected,
    {
        'LL/SEC/ADV/BV-16-C': (
            'Network Privacy - Undirected Connectable Advertising with '
            'Local IRK and Peer IRK, Ignore Identity Address',
            {},
        ),
    },
)

# Verify that directed advertising in network privacy rejects a peer identity InitA
# when the peer has an associated IRK in the resolving list.
PROCEDURES |= procedure_cases(
    privacy_directed_identity_rejected,
    {
        'LL/SEC/ADV/BV-17-C': (
            'Network Privacy - Directed Connectable Advertising using local '
            'and remote IRK, Ignore Identity Address',
            {},
        ),
    },
)

# Verify that undirected connectable advertising in device privacy accepts a peer
# identity InitA despite an associated Peer IRK and maintains the resulting connection.
PROCEDURES |= procedure_cases(
    privacy_connectable_device_mode,
    {
        'LL/SEC/ADV/BV-19-C': (
            'Device Privacy - Undirected Connectable Advertising with Local '
            'IRK and Peer IRK, Accept Identity Address, CONNECT_IND',
            {},
        ),
    },
)

# Verify that directed advertising in device privacy accepts a peer identity InitA
# despite an associated Peer IRK and exchanges connection data.
PROCEDURES |= procedure_cases(
    privacy_directed_device_mode,
    {
        'LL/SEC/ADV/BV-20-C': (
            'Device Privacy - Directed Connectable Advertising using local '
            'and remote IRK, Accept Identity Address',
            {},
        ),
    },
)

# Verify connection acceptance when the Lower Tester copies the exact peer-RPA TargetA
# from directed advertising into CONNECT_IND InitA.
PROCEDURES |= procedure_cases(
    privacy_directed_target_as_initiator,
    {
        'LL/SEC/ADV/BV-22-C': (
            'Network Privacy – Directed Connectable Advertising using '
            'Target RPA as InitA',
            {},
        ),
    },
)

# Verify connection with a local-RPA AdvA and peer-identity InitA when only the Local
# IRK is configured and the peer identity is in the Filter Accept List.
PROCEDURES |= procedure_cases(
    privacy_connectable_local_only,
    {
        'LL/SEC/ADV/BV-23-C': (
            'Network Privacy - Undirected Connectable Advertising with '
            'Local IRK, no peer IRK, AUX_CONNECT_REQ',
            {'extended': True},
        ),
    },
)

# Verify connection with local and peer RPAs when both IRKs are configured, and
# rejection of the peer RPA after address resolution is disabled.
PROCEDURES |= procedure_cases(
    privacy_connectable_mutual_irks,
    {
        'LL/SEC/ADV/BV-24-C': (
            'Network Privacy - Undirected Connectable Advertising with '
            'Local IRK and Peer IRK, AUX_CONNECT_REQ',
            {'extended': True},
        ),
    },
)

# Verify directed connection establishment using local-RPA AdvA and peer-identity
# TargetA with only a Local IRK, and renewal of AdvA after timeout.
PROCEDURES |= procedure_cases(
    privacy_directed_local_only,
    {
        'LL/SEC/ADV/BV-25-C': (
            'Network Privacy - Directed Connectable Advertising with local '
            'IRK but without remote IRK, AUX_CONNECT_REQ',
            {'extended': True},
        ),
    },
)

# Verify that undirected connectable advertising in device privacy accepts a peer
# identity InitA despite an associated Peer IRK and maintains the resulting connection.
PROCEDURES |= procedure_cases(
    privacy_connectable_device_mode,
    {
        'LL/SEC/ADV/BV-26-C': (
            'Device Privacy - Undirected Connectable Advertising with Local '
            'IRK and Peer IRK, Accept Identity Address, AUX_CONNECT_REQ',
            {'extended': True},
        ),
    },
)


# Verify that an advertiser rejects another connection from an already connected peer
# after learning its IRK and receiving a different RPA. The Lower Tester maintains the
# original Central connection.
PROCEDURES |= procedure_cases(
    privacy_duplicate_advertiser,
    {
        'LL/SEC/ADV/BI-01-C': (
            'Reject Existing Connection Request, Different Peer RPA',
            {},
        ),
    },
)

# Verify that an initiator sends no second connection request to an already connected
# peer after learning its IRK and observing a different RPA, while maintaining the
# original ACL.
PROCEDURES |= procedure_cases(
    privacy_duplicate_initiator,
    {
        'LL/SEC/INI/BI-01-C': ('Duplicate Connection Request, Different Peer RPA', {}),
    },
)

# Verify active scanning with a Host-assigned random address and rejection of Set
# Random Address while scanning. The selected LE Encrypt variant also checks the
# address hash.
PROCEDURES |= procedure_cases(
    privacy_random_address_scanning,
    {
        'LL/SEC/SCN/BV-01-C': (
            'Random Address Scanning, LE Encryption',
            {'encrypt': True},
        ),
        'LL/SEC/SCN/BV-02-C': (
            'Random Address Scanning, No LE Encryption',
            {'encrypt': False},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_security(air_dut, identifier, description, procedure, parameters, marks):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
