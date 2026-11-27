# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Feature Exchange and Version Exchange (Core Vol 6, Part B, 5.1.4, 5.1.5).

Matrix: {Central, Peripheral} x {local, remote} x {answered, cached, unknown,
rejected, timeout, disconnect, collision}. Scenarios drive the LLCP harness
(llcp/harness.py); collisions with other procedures live in collision_test.py.
"""

import struct
from dataclasses import replace

import pytest

from bscontroller.linklayer.features import LeFeatureMask
from tests.unit.linklayer.llcp.harness import (
    CENTRAL,
    PERIPHERAL,
    PROCEDURE_COLLISION,
    ROLES,
    ErrorCode,
    Opcode,
    feature,
    hci,
    reject_ext_ind,
    reject_ind,
    unknown_rsp,
    version_ind,
)

FEATURES_DONE = hci.HCI_LE_Read_Remote_Features_Complete_Event
VERSION_DONE = hci.HCI_Read_Remote_Version_Information_Complete_Event
DISCONNECTED = hci.HCI_Disconnection_Complete_Event
PEER_FEATURES = int(
    LeFeatureMask.LE_ENCRYPTION
    | LeFeatureMask.LE_2M_PHY
    | LeFeatureMask.CONNECTION_PARAMETERS_REQUEST_PROCEDURE
    | LeFeatureMask.LE_DATA_PACKET_LENGTH_EXTENSION
)


# Core Vol 6, Part B, 5.1.4 and Table 4.9: legacy feature PDUs
# carry eight octets, with HCI-only page-zero bits cleared.
FEATURE_PAGE_ZERO_SIZE = 8
FEATURE_PAGE_ZERO_MASK = (1 << (8 * FEATURE_PAGE_ZERO_SIZE)) - 1
HCI_ONLY_PAGE_ZERO_FEATURES = (1 << 27) | (1 << 42) | (1 << 48)


def request_opcode(role):
    return (
        Opcode.LL_FEATURE_REQ if role == CENTRAL else Opcode.LL_PERIPHERAL_FEATURE_REQ
    )


def le_features(event):
    return int.from_bytes(event.le_features, 'little')


def local_page_zero(dut):
    return dut.link.features & FEATURE_PAGE_ZERO_MASK & ~HCI_ONLY_PAGE_ZERO_FEATURES


# ---------------------------------------------------------------------------
# Feature Exchange, local
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('role', ROLES)
def test_extended_feature_wrong_page_keeps_exchange(iut, role):
    """A wrong-page response leaves the sent exchange available for its real response.

    Purpose: Preserve LLCP response ownership when ignoring a mismatched page
    (Core Vol 6, Part B, 5.1.4.3).

    Steps:
        1. Start Read All Remote Features and exchange legacy page 0.
        2. Receive the IUT's page-1 request, then send a page-2 response.
        3. Send the matching page-1 response through the same LLCP/ARQ path.

    Expected:
        The wrong page generates no Host completion or outgoing rejection.
        The matching page completes successfully with exactly the supplied data.
    """
    dut = iut(role)
    dut.host_start('read_remote_features', pages=1)
    dut.event_prepare()
    dut.lt_rx(request_opcode(role))
    dut.lt_tx(feature(Opcode.LL_FEATURE_RSP, 1 << 63))
    dut.lt_rx(Opcode.LL_FEATURE_EXT_REQ)
    dut.lt_tx(bytes([Opcode.LL_FEATURE_EXT_RSP, 2, 2]) + bytes(24))
    dut.ut_rx_q_is_empty()
    dut.lt_rx_q_is_empty()
    dut.lt_tx(bytes([Opcode.LL_FEATURE_EXT_RSP, 1, 1]) + b'\x01' + bytes(23))
    dut.ut_rx(
        hci.HCI_LE_Read_All_Remote_Features_Complete_Event,
        status=ErrorCode.SUCCESS,
        max_remote_page=1,
        max_valid_page=1,
        le_features=(1 << 63).to_bytes(8, 'little') + b'\x01' + bytes(239),
    )
    assert not dut.controls.feature_request


@pytest.mark.parametrize('role', ROLES)
def test_feature_loc(iut, role):
    """Local Feature Exchange completes and a repeat is served from the cache.

    Purpose: Verify that a Host-initiated Feature Exchange sends the role's
    request carrying the local features, reports the peer features to the Host,
    and answers a second request from the cache without new air traffic
    (Vol 6, Part B, 5.1.4), for both roles.

    Steps:
        1. Upper Tester starts Read Remote Features.
        2. Lower Tester receives the role's FEATURE_REQ with local features.
        3. Lower Tester answers with LL_FEATURE_RSP carrying PEER_FEATURES.
        4. Upper Tester starts Read Remote Features again.

    Expected:
        - Host gets LE Read Remote Features Complete (SUCCESS, peer features).
        - No further Host notification and no procedure timer remains.
        - The second request completes from the cache with no PDU on air.
    """
    dut = iut(role)
    dut.host_start('read_remote_features')
    dut.event_prepare()
    sent = dut.lt_rx(request_opcode(role))
    assert sent[1:] == local_page_zero(dut).to_bytes(FEATURE_PAGE_ZERO_SIZE, 'little')
    dut.lt_tx(feature(Opcode.LL_FEATURE_RSP, PEER_FEATURES))
    dut.event_done()
    event = dut.ut_rx(FEATURES_DONE, status=0, connection_handle=dut.handle)
    assert le_features(event) == PEER_FEATURES
    dut.ut_rx_q_is_empty()
    assert (
        not dut.controls.feature_request
        and dut.connection.llcp.response_deadline_us is None
    )
    # The second request is served from the cache, without air traffic.
    dut.host_start('read_remote_features')
    dut.ut_rx(FEATURES_DONE, status=0)
    dut.lt_rx_q_is_empty()


@pytest.mark.parametrize('role', ROLES)
def test_feature_loc_busy(iut, role):
    """A second Feature Exchange while one is outstanding is disallowed.

    Purpose: Verify that the Host cannot start a second Feature Exchange while a
    request is still pending, for both roles.

    Steps:
        1. Upper Tester starts Read Remote Features.
        2. Upper Tester starts Read Remote Features again before completion.

    Expected:
        - The second admission returns Command Disallowed.
        - Only one request PDU is queued.
    """
    dut = iut(role)
    dut.host_start('read_remote_features')
    dut.host_start('read_remote_features', status=ErrorCode.COMMAND_DISALLOWED_ERROR)
    assert len(dut.connection.event.queue) == 1


@pytest.mark.parametrize(
    'response,status',
    [
        (None, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR),
        ('reject', ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR),
        ('reject-ext', PROCEDURE_COLLISION),
    ],
    ids=['unknown', 'reject', 'reject-ext'],
)
@pytest.mark.parametrize('role', ROLES)
def test_feature_loc_refused(iut, role, response, status):
    """A refused local Feature Exchange completes with the refusal's status.

    Purpose: Verify that each refusal of the role's FEATURE_REQ completes the
    procedure with the matching status and never answers an UNKNOWN_RSP with
    another UNKNOWN_RSP, for each refusal kind (unknown, reject, reject-ext) and
    both roles.

    Steps:
        1. Upper Tester starts Read Remote Features.
        2. Lower Tester receives the role's request.
        3. Lower Tester replies with UNKNOWN_RSP, REJECT_IND or REJECT_EXT_IND.

    Expected:
        - Host gets Read Remote Features Complete with the expected status.
        - No further Host notification and nothing is queued for the air.
        - The feature request state is cleared.
    """
    dut = iut(role)
    opcode = request_opcode(role)
    dut.host_start('read_remote_features')
    dut.event_prepare()
    dut.lt_rx(opcode)
    if response is None:
        dut.lt_tx(unknown_rsp(opcode))
    elif response == 'reject':
        dut.lt_tx(reject_ind(status))
    else:
        dut.lt_tx(reject_ext_ind(opcode, status))
    dut.event_done()
    dut.ut_rx(FEATURES_DONE, status=status)
    dut.ut_rx_q_is_empty()
    # UNKNOWN_RSP is never answered with another UNKNOWN_RSP.
    dut.lt_rx_q_is_empty()
    assert not dut.controls.feature_request


@pytest.mark.parametrize('role', ROLES)
def test_feature_loc_timeout(iut, role):
    """A response timeout completes the exchange, then disconnects, exactly once.

    Purpose: Verify that when the peer acknowledges the request but never
    answers, the procedure timer (T_PRT) expiry reports the completion with the
    timeout status before the disconnection, each exactly once, for both roles.

    Steps:
        1. Upper Tester starts Read Remote Features.
        2. Lower Tester acknowledges the request but sends no response.
        3. Advance time to just before, then past, the procedure deadline.

    Expected:
        - No timeout fires one microsecond early; it fires at the deadline.
        - Host gets Read Remote Features Complete (response timeout) first.
        - Host then gets Disconnection Complete (response timeout); nothing else.
    """
    dut = iut(role)
    dut.host_start('read_remote_features')
    dut.event_prepare()
    dut.lt_rx(request_opcode(role))
    dut.tx_ack()
    dut.event_done()
    assert not dut.expire_response_timer(early_us=1)
    assert dut.elapse(1)
    # Completion precedes the disconnection, and is reported exactly once.
    dut.ut_rx(FEATURES_DONE, status=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)
    dut.ut_rx(DISCONNECTED, reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)
    dut.ut_rx_q_is_empty()


@pytest.mark.parametrize('role', ROLES)
def test_feature_loc_disconnect(iut, role):
    """A disconnection during the exchange completes it then disconnects.

    Purpose: Verify that completing the connection while a Feature Exchange is
    outstanding reports the pending completion with the disconnect reason before
    the disconnection event, for both roles.

    Steps:
        1. Upper Tester starts Read Remote Features.
        2. The connection completes with connection-timeout.

    Expected:
        - Host gets Read Remote Features Complete (connection timeout).
        - Host then gets Disconnection Complete (connection timeout).
        - No further Host notification.
    """
    dut = iut(role)
    dut.host_start('read_remote_features')
    dut.connection.complete(ErrorCode.CONNECTION_TIMEOUT_ERROR)
    dut.ut_rx(FEATURES_DONE, status=ErrorCode.CONNECTION_TIMEOUT_ERROR)
    dut.ut_rx(DISCONNECTED, reason=ErrorCode.CONNECTION_TIMEOUT_ERROR)
    dut.ut_rx_q_is_empty()


@pytest.mark.parametrize('role', ROLES)
def test_feature_loc_wrong_length_response_is_unknown(iut, role):
    """A wrong-length FEATURE_RSP is answered with UNKNOWN_RSP.

    Purpose: Verify that a FEATURE_RSP whose payload is the wrong length is not
    accepted as the answer; the IUT emits LL_UNKNOWN_RSP and keeps the request
    outstanding, for both roles.

    Steps:
        1. Upper Tester starts Read Remote Features.
        2. Lower Tester receives the role's request.
        3. Lower Tester sends a FEATURE_RSP with a 4-octet body.

    Expected:
        - The IUT transmits UNKNOWN_RSP(LL_FEATURE_RSP).
        - The feature request is still pending.
        - No Host notification is produced.
    """
    dut = iut(role)
    dut.host_start('read_remote_features')
    dut.event_prepare()
    dut.lt_rx(request_opcode(role))
    dut.lt_tx(bytes([Opcode.LL_FEATURE_RSP]) + bytes(4))
    dut.event_done()
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_UNKNOWN_RSP, unknown_rsp(Opcode.LL_FEATURE_RSP))
    assert dut.controls.feature_request
    dut.ut_rx_q_is_empty()


def test_feature_loc_after_peer_already_exchanged(central):
    """A Host request after a peer exchange is served from the cache.

    Purpose: Verify that once the peer's own Feature Exchange has populated the
    cache, a later Host-initiated request completes from that cache without a new
    request on air.

    Steps:
        1. Lower Tester sends LL_PERIPHERAL_FEATURE_REQ with PEER_FEATURES.
        2. The Central answers with LL_FEATURE_RSP and the peer acknowledges.
        3. Upper Tester starts Read Remote Features.

    Expected:
        - Host gets Read Remote Features Complete (SUCCESS) with peer features.
        - No request PDU is queued for the air.
    """
    central.event_prepare()
    central.lt_tx(feature(Opcode.LL_PERIPHERAL_FEATURE_REQ, PEER_FEATURES))
    central.event_done()
    central.event_prepare()
    central.lt_rx(Opcode.LL_FEATURE_RSP)
    central.tx_ack()
    central.event_done()
    central.host_start('read_remote_features')
    event = central.ut_rx(FEATURES_DONE, status=0)
    assert le_features(event) == PEER_FEATURES
    central.lt_rx_q_is_empty()


# ---------------------------------------------------------------------------
# Feature Exchange, remote
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('role', ROLES)
def test_feature_rem(iut, role):
    """A peer Feature Exchange is answered with the role-correct intersection.

    Purpose: Verify that a peer-initiated request is answered with a FEATURE_RSP
    whose octet 0 is the intersection of local and peer features while higher
    octets stay local, and that the full peer feature set is cached
    (Vol 6, Part B, 5.1.4.1, 5.1.4.2), for both roles.

    Steps:
        1. Lower Tester sends the request legal for its own role, with features
           that set bits in a higher octet.
        2. Lower Tester receives the IUT's LL_FEATURE_RSP and acknowledges.

    Expected:
        - Response octet 0 equals local AND peer AND 0xFF.
        - Response octets 1 through 7 equal the local page-zero feature octets.
        - The connection caches the full peer features, nothing reaches the Host.
    """
    dut = iut(role)
    # The peer sends the request that is legal for its own role.
    opcode = request_opcode(PERIPHERAL if role == CENTRAL else CENTRAL)
    peer = PEER_FEATURES | 0xFF00_0000_0000_0000
    dut.event_prepare()
    dut.lt_tx(feature(opcode, peer))
    dut.event_done()
    dut.event_prepare()
    response = dut.lt_rx(Opcode.LL_FEATURE_RSP)
    assert len(response[1:]) == FEATURE_PAGE_ZERO_SIZE
    features = int.from_bytes(response[1:], 'little')
    # Octet 0 is the intersection; higher octets are local (5.1.4.1, 5.1.4.2).
    assert features & 0xFF == local_page_zero(dut) & peer & 0xFF
    assert features & ~0xFF == local_page_zero(dut) & ~0xFF
    dut.tx_ack()
    dut.event_done()
    assert dut.connection.peer_features == peer
    dut.ut_rx_q_is_empty()


@pytest.mark.parametrize('role', ROLES)
def test_feature_rem_wrong_role_request_is_unknown(iut, role):
    """A request opcode illegal for the peer's role is answered UNKNOWN_RSP.

    Purpose: Verify that a feature request carrying the opcode reserved for the
    IUT's own role (and thus illegal from the peer) is rejected with
    UNKNOWN_RSP and does not cache any peer features, for both roles.

    Steps:
        1. Lower Tester sends the request opcode that belongs to the IUT's role.

    Expected:
        - The IUT transmits UNKNOWN_RSP for that opcode.
        - No peer features are cached.
    """
    dut = iut(role)
    opcode = request_opcode(role)
    dut.event_prepare()
    dut.lt_tx(feature(opcode, PEER_FEATURES))
    dut.event_done()
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_UNKNOWN_RSP, unknown_rsp(opcode))
    assert dut.connection.peer_features is None


@pytest.mark.parametrize('role', ROLES)
def test_feature_unsolicited_response_is_cached_silently(iut, role):
    """An unsolicited FEATURE_RSP is cached without any reply or Host event.

    Purpose: Verify that a FEATURE_RSP that answers no outstanding request is
    accepted silently: the peer features are cached, the Host is not notified
    and nothing is sent back, for both roles.

    Steps:
        1. Lower Tester sends LL_FEATURE_RSP with PEER_FEATURES.
        2. Run one idle connection event.

    Expected:
        - No Host notification is produced.
        - The connection caches PEER_FEATURES.
    """
    dut = iut(role)
    dut.event_prepare()
    dut.lt_tx(feature(Opcode.LL_FEATURE_RSP, PEER_FEATURES))
    dut.event_done()
    dut.run_events(1)
    dut.ut_rx_q_is_empty()
    assert dut.connection.peer_features == PEER_FEATURES


@pytest.mark.parametrize('role', ROLES)
def test_feature_collision_loc_and_rem(iut, role):
    """A colliding peer request does not short-circuit the local exchange.

    Purpose: Verify that when the peer's own feature request collides with the
    IUT's outstanding one, the IUT answers the peer first yet still keeps and
    completes its own request on the following event, for both roles.

    Steps:
        1. Upper Tester starts Read Remote Features.
        2. Lower Tester sends its own request (without acknowledging ours).
        3. Lower Tester receives the IUT's FEATURE_RSP and acknowledges.
        4. Lower Tester receives the IUT's own request and answers it.

    Expected:
        - The response is prioritized ahead of our queued request.
        - Our feature request stays outstanding until its own answer arrives.
        - Host finally gets Read Remote Features Complete (SUCCESS), nothing else.
    """
    dut = iut(role)
    dut.host_start('read_remote_features')
    # The peer's own request arrives before ours is answered.
    peer_opcode = request_opcode(PERIPHERAL if role == CENTRAL else CENTRAL)
    dut.event_prepare()
    dut.lt_tx(feature(peer_opcode, PEER_FEATURES), ack=False)
    # Both the response and our request are queued; the response is prioritized.
    dut.lt_rx(Opcode.LL_FEATURE_RSP)
    dut.tx_ack()
    dut.event_done()
    # Peer features learned from its request do not finish our own request.
    assert dut.controls.feature_request
    dut.event_prepare()
    dut.lt_rx(request_opcode(role))
    dut.lt_tx(feature(Opcode.LL_FEATURE_RSP, PEER_FEATURES))
    dut.event_done()
    dut.ut_rx(FEATURES_DONE, status=0)
    dut.ut_rx_q_is_empty()


# ---------------------------------------------------------------------------
# Version Exchange
# ---------------------------------------------------------------------------
def configure_version(dut):
    controller = dut.controller
    controller.config = replace(
        controller.config,
        lmp_version=hci.SpecificationVersion.BLUETOOTH_CORE_5_4,
        manufacturer=0x0059,
        lmp_subversion=0x1234,
    )
    return struct.pack('<BHH', controller.config.lmp_version, 0x0059, 0x1234)


@pytest.mark.parametrize('role', ROLES)
def test_version_loc(iut, role):
    """Local Version Exchange reports the peer version, then uses the cache.

    Purpose: Verify that a Host-initiated Version Exchange sends the local
    VERSION_IND once, reports the peer's version tuple, refuses a concurrent
    second request, and later answers from the cache (Vol 6, Part B, 5.1.5), for
    both roles.

    Steps:
        1. Upper Tester starts Read Remote Version Information (twice).
        2. Lower Tester receives the IUT's VERSION_IND and replies with its own.
        3. Run an idle event, then Upper Tester requests the version again.

    Expected:
        - The concurrent second admission returns Command Disallowed.
        - Host gets Read Remote Version Complete (SUCCESS) with the peer tuple.
        - The repeat completes from the cache with no VERSION_IND on air.
    """
    dut = iut(role)
    local = configure_version(dut)
    dut.host_start('read_remote_version')
    dut.host_start('read_remote_version', status=ErrorCode.COMMAND_DISALLOWED_ERROR)
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_VERSION_IND, bytes([Opcode.LL_VERSION_IND]) + local)
    dut.lt_tx(version_ind(9, 0x1234, 0x5678))
    dut.event_done()
    dut.ut_rx(
        VERSION_DONE, status=0, version=9, manufacturer_name=0x1234, subversion=0x5678
    )
    dut.ut_rx_q_is_empty()
    # Our VERSION_IND is sent at most once per connection.
    dut.run_events(1)
    # A later request completes from the cache.
    dut.host_start('read_remote_version')
    dut.ut_rx(VERSION_DONE, status=0, version=9)
    dut.lt_rx_q_is_empty()


@pytest.mark.parametrize('role', ROLES)
def test_version_rem(iut, role):
    """A peer Version Exchange is answered once and never reported to the Host.

    Purpose: Verify that a peer-initiated VERSION_IND is answered with exactly
    one local VERSION_IND, is not reported to the Host, and that a repeated peer
    indication is ignored while the first tuple is kept, for both roles.

    Steps:
        1. Lower Tester sends VERSION_IND (9, 1, 1).
        2. Lower Tester receives the IUT's VERSION_IND and acknowledges.
        3. Lower Tester sends a second VERSION_IND and an idle event runs.

    Expected:
        - No Host notification is produced for either indication.
        - The cached peer version stays (9, 1, 1).
        - No local exchange is tracked as awaiting an answer.
    """
    dut = iut(role)
    local = configure_version(dut)
    dut.event_prepare()
    dut.lt_tx(version_ind(9, 1, 1))
    dut.event_done()
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_VERSION_IND, bytes([Opcode.LL_VERSION_IND]) + local)
    dut.tx_ack()
    dut.event_done()
    # A remote exchange is not reported to the Host.
    dut.ut_rx_q_is_empty()
    # A repeated peer indication is ignored: no second reply, first tuple kept.
    dut.event_prepare()
    dut.lt_tx(version_ind(0, 0, 0))
    dut.event_done()
    dut.run_events(1)
    assert dut.controls.peer_version == (9, 1, 1)
    # The reply was not tracked as a local request awaiting an answer.
    assert not dut.connection.llcp.local_procedure_busy


@pytest.mark.parametrize('role', ROLES)
def test_version_collision_loc_and_rem(iut, role):
    """A single local VERSION_IND answers a collision in both directions.

    Purpose: Verify that when the peer's indication arrives before the IUT sends
    its own, the Host's request is completed from the peer tuple and the single
    local VERSION_IND serves both directions, for both roles.

    Steps:
        1. Upper Tester starts Read Remote Version Information.
        2. Lower Tester sends VERSION_IND (9, 2, 3) before the IUT transmits.
        3. Lower Tester receives the IUT's one VERSION_IND and acknowledges.
        4. Run one idle event.

    Expected:
        - Host gets Read Remote Version Complete (SUCCESS) with manufacturer 2,
          subversion 3.
        - Only one VERSION_IND is transmitted.
        - No further Host notification.
    """
    dut = iut(role)
    configure_version(dut)
    dut.host_start('read_remote_version')
    # The peer's indication arrives before ours is transmitted.
    dut.event_prepare()
    dut.lt_tx(version_ind(9, 2, 3))
    dut.ut_rx(VERSION_DONE, status=0, manufacturer_name=2, subversion=3)
    # Only one local indication exists, which answers both directions.
    dut.lt_rx(Opcode.LL_VERSION_IND)
    dut.tx_ack()
    dut.event_done()
    dut.run_events(1)
    dut.ut_rx_q_is_empty()


@pytest.mark.parametrize('role', ROLES)
def test_version_loc_timeout(iut, role):
    """A Version Exchange response timeout completes then disconnects.

    Purpose: Verify that when the peer acknowledges the VERSION_IND but never
    answers, the procedure timer expiry reports the completion with the timeout
    status before the disconnection, for both roles.

    Steps:
        1. Upper Tester starts Read Remote Version Information.
        2. Lower Tester acknowledges the VERSION_IND but sends no reply.
        3. Expire the procedure timer.

    Expected:
        - Host gets Read Remote Version Complete (response timeout).
        - Host then gets Disconnection Complete (response timeout); nothing else.
    """
    dut = iut(role)
    dut.host_start('read_remote_version')
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_VERSION_IND)
    dut.tx_ack()
    dut.event_done()
    assert dut.expire_response_timer()
    dut.ut_rx(VERSION_DONE, status=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)
    dut.ut_rx(DISCONNECTED, reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)
    dut.ut_rx_q_is_empty()


@pytest.mark.parametrize('role', ROLES)
def test_version_loc_disconnect_reports_only_disconnection(iut, role):
    """A disconnection during a Version Exchange reports only the disconnection.

    Purpose: Verify that completing the connection while a local Version
    Exchange is outstanding reports only the disconnection (version read has no
    Command Complete carrying a failure) and clears the request, for both roles.

    Steps:
        1. Upper Tester starts Read Remote Version Information.
        2. The connection completes with connection-timeout.

    Expected:
        - Host gets Disconnection Complete (connection timeout) and nothing else.
        - The version request state is cleared.
    """
    dut = iut(role)
    dut.host_start('read_remote_version')
    dut.connection.complete(ErrorCode.CONNECTION_TIMEOUT_ERROR)
    dut.ut_rx(DISCONNECTED, reason=ErrorCode.CONNECTION_TIMEOUT_ERROR)
    dut.ut_rx_q_is_empty()
    assert not dut.controls.version_request


def test_version_timer_starts_after_feature_exchange(central):
    """A queued Version Exchange gets a full timeout after Feature Exchange."""
    central.host_start('read_remote_features')
    first = central.controls.features.deadline_us
    central.elapse(1000)
    central.host_start('read_remote_version')
    assert central.controls.version_deadline_us is None
    assert central.connection.llcp.response_deadline_us == first
    central.controls.features.finish(ErrorCode.SUCCESS)
    central.elapse(1000)
    central.event_prepare()
    assert central.controls.version_deadline_us == first + 2000
    assert central.connection.llcp.response_deadline_us == first + 2000


@pytest.mark.parametrize('role', ROLES)
def test_version_wrong_length_is_unknown(iut, role):
    """A wrong-length VERSION_IND is answered with UNKNOWN_RSP.

    Purpose: Verify that a VERSION_IND whose payload is the wrong length is
    rejected with UNKNOWN_RSP and caches no peer version, for both roles.

    Steps:
        1. Lower Tester sends VERSION_IND with a truncated (1-octet) body.
        2. Lower Tester receives the IUT's response.

    Expected:
        - The IUT transmits UNKNOWN_RSP(LL_VERSION_IND).
        - No peer version is cached.
    """
    dut = iut(role)
    dut.event_prepare()
    dut.lt_tx(bytes([Opcode.LL_VERSION_IND, 9]))
    dut.event_done()
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_UNKNOWN_RSP, unknown_rsp(Opcode.LL_VERSION_IND))
    assert dut.controls.peer_version is None


@pytest.mark.parametrize('operation', ['features', 'version'])
def test_cached_completion_follows_command_status(central, operation):
    """A cached read completes right after its Command Status.

    Purpose: Verify that when the value is already cached, the raw HCI read
    command yields Command Status (SUCCESS) immediately followed by the matching
    completion event, for both the features and the version read.

    Steps:
        1. Pre-populate the cache (peer features, or peer version).
        2. Send the raw Read Remote Features / Version command over HCI.

    Expected:
        - The first event is Command Status with SUCCESS.
        - The second event is the operation's completion event.
    """
    if operation == 'features':
        central.set_peer_features(0)
        command = hci.HCI_LE_Read_Remote_Features_Command(central.handle)
        event_type = FEATURES_DONE
    else:
        central.controls.peer_version = (9, 1, 1)
        command = hci.HCI_Read_Remote_Version_Information_Command(central.handle)
        event_type = VERSION_DONE
    events = central.hci(command)
    assert isinstance(events[0], hci.HCI_Command_Status_Event)
    assert events[0].status == ErrorCode.SUCCESS
    assert isinstance(events[1], event_type)


@pytest.mark.parametrize('method', ['read_remote_features', 'read_remote_version'])
def test_unknown_handle(central, method):
    """A remote features or version read on an unknown handle is refused.

    Purpose: Verify that requesting either exchange for a connection handle that
    does not exist fails with Unknown Connection Identifier, for both the
    features and the version entry point.

    Steps:
        1. Call the read method with handle 0x0EFF.

    Expected:
        - The status is Unknown Connection Identifier.
    """
    status = getattr(central.controller.le_procedures.ll, method)(0x0EFF)
    assert status == ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR


@pytest.mark.parametrize('role', ROLES)
@pytest.mark.parametrize('local', [True, False])
def test_hci_only_features_are_never_sent_in_acl_exchange(iut, role, local):
    """Page-zero features marked Send to Peer = N stay clear on the air.

    Purpose: Verify that feature bits 27, 42 and 48, which have Send to Peer
    = N, stay zero in both the initiating request and the LL_FEATURE_RSP,
    for both roles (Vol 6, Part B, Table 4.9).

    Steps:
        1. Set the three page-zero HCI-only bits in the IUT's local features.
        2. Either Upper Tester starts a local Feature Exchange, or Lower Tester
           sends the peer role's feature request with an empty feature set.
        3. Lower Tester receives the IUT's request or LL_FEATURE_RSP.

    Expected:
        - None of bits 27, 42 or 48 is set in the sent FeatureSet.
        - A request carries the remaining local features; a response also
          clears the first octet, which reflects the peer's empty set.
    """
    dut = iut(role)
    excluded = HCI_ONLY_PAGE_ZERO_FEATURES
    baseline = local_page_zero(dut)
    dut.link.features |= excluded
    if local:
        dut.host_start('read_remote_features')
    else:
        dut.lt_tx(
            feature(request_opcode(PERIPHERAL if role == CENTRAL else CENTRAL), 0)
        )
    dut.event_prepare()
    sent = dut.lt_rx(request_opcode(role) if local else Opcode.LL_FEATURE_RSP)
    expected = baseline if local else baseline & ~0xFF
    assert sent[1:] == expected.to_bytes(FEATURE_PAGE_ZERO_SIZE, 'little')
