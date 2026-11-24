# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connection Update and Connection Parameters Request (Core Vol 6, Part B, 5.1.1,
5.1.7).

Matrix: {Central, Peripheral} x {local, remote} x {accept, Host reject, peer
reject, unsupported fallback, invalid, timeout, instant handling}. Scenarios
drive the LLCP harness (llcp/harness.py); a final section unit-tests the pure
parameter-selection policy functions. Collisions with other procedures live in
collision_test.py.
"""

import pytest

from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    LL_EVENT_COUNTER_MASK,
    LL_SUPERVISION_TIMEOUT_UNIT_US,
)
from bscontroller.linklayer.connection_parameters import (
    PARAM_REQUEST,
    UPDATE,
    anchor_offset,
    preferred_interval,
    preferred_periodicity,
)
from bscontroller.linklayer.features import LeFeatureMask
from tests.unit.linklayer.llcp.harness import (
    CENTRAL,
    PERIPHERAL,
    PROCEDURE_COLLISION,
    ErrorCode,
    Opcode,
    connection_param_req,
    connection_param_rsp,
    connection_update_ind,
    hci,
    instant_of,
    reject_ext_ind,
    reject_ind,
    unknown_rsp,
)

UPDATE_DONE = hci.HCI_LE_Connection_Update_Complete_Event
REMOTE_REQUEST = hci.HCI_LE_Remote_Connection_Parameter_Request_Event
DISCONNECTED = hci.HCI_Disconnection_Complete_Event


def request(iut, minimum, maximum=None, latency=0, timeout=100, *, status=0):
    iut.host_start(
        'prepare_parameters',
        minimum,
        minimum if maximum is None else maximum,
        latency,
        timeout,
        status=status,
        valid_ce_lengths=True,
    )


def unpack_update(payload):
    return UPDATE.unpack(payload[1:])


def unpack_param(payload):
    return PARAM_REQUEST.unpack(payload[1:])


def assert_idle(iut):
    assert not iut.controls.busy and iut.controls.deadline_us == 0
    iut.lt_rx_q_is_empty()


def complete_update_at_instant(iut, update, *, notify=True, interval=None):
    iut.wait_instant(instant_of(update))
    iut.event_prepare()
    interval = interval or unpack_update(update)[2]
    assert iut.connection.parameters.interval == interval
    if notify:
        iut.ut_rx(UPDATE_DONE, status=0, connection_interval=interval)
    iut.ut_rx_q_is_empty()
    assert_idle(iut)


# ---------------------------------------------------------------------------
# Central, local
# ---------------------------------------------------------------------------
def test_central_loc_param_req(central):
    """A Central local update negotiates via PARAM_REQ then UPDATE_IND.

    Purpose: Verify that a Host connection-update request on a Central sends
    LL_CONNECTION_PARAM_REQ, selects an interval from the intersection of the
    Host and peer ranges, and applies it via UPDATE_IND at the Instant
    (Vol 6, Part B, 5.1.7), reporting completion.

    Steps:
        1. Upper Tester requests interval 30..40, latency 1, timeout 200.
        2. Lower Tester receives PARAM_REQ and answers PARAM_RSP 32..36.
        3. Lower Tester receives UPDATE_IND and acknowledges.
        4. Idle events run to the Instant.

    Expected:
        - PARAM_REQ carries (30, 40, 1, 200, 1) with the reference event counter.
        - UPDATE_IND selects interval 32, latency 1, timeout 200, Instant +6.
        - Host gets Connection Update Complete (SUCCESS, interval 32).
    """
    request(central, 30, 40, 1, 200)
    central.event_prepare()
    sent = central.lt_rx(Opcode.LL_CONNECTION_PARAM_REQ)
    assert unpack_param(sent)[:5] == (30, 40, 1, 200, 1)
    assert unpack_param(sent)[5] == central.event_counter
    central.lt_tx(connection_param_rsp(32, 36, latency=1, timeout=200))
    central.event_done()

    central.event_prepare()
    update = central.lt_rx(Opcode.LL_CONNECTION_UPDATE_IND)
    size, offset, interval, latency, timeout, instant = unpack_update(update)
    # The Central selects from the intersection of Host and peer ranges.
    assert (interval, latency, timeout) == (32, 1, 200)
    assert instant == (central.event_counter + 6) & LL_EVENT_COUNTER_MASK
    central.tx_ack()
    central.event_done()
    complete_update_at_instant(central, update)


def test_central_loc_param_req_disjoint_peer_range(central):
    """A disjoint peer range fails the update with unacceptable parameters.

    Purpose: Verify that when the peer's PARAM_RSP range does not overlap the
    Host range, the Central reports Connection Update Complete with unacceptable
    connection parameters and no change.

    Steps:
        1. Upper Tester requests interval 30..40.
        2. Lower Tester receives PARAM_REQ and answers PARAM_RSP 50..60.

    Expected:
        - Host gets Connection Update Complete (unacceptable parameters).
        - The procedure is idle.
    """
    request(central, 30, 40)
    central.event_prepare()
    central.lt_rx(Opcode.LL_CONNECTION_PARAM_REQ)
    central.lt_tx(connection_param_rsp(50, 60))
    central.event_done()
    central.ut_rx(
        UPDATE_DONE, status=ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR
    )
    assert_idle(central)


def test_disconnect_completes_connection_update_waiting_for_subrate(central):
    """Report failure for a Host update accepted while subrating is pending.

    Purpose: Verify a successful Command Status is followed by completion
    even when the connection closes before the update can start
    (Core Vol 4, Part E, 7.8.18).

    Steps:
        1. The Host requests a subrate change and then a connection update.
        2. The connection ends before the update can run.

    Expected:
        - The Host receives both failed procedure completions for this handle.
        - The connection update did not send a PDU or change any parameters.
    """
    central.set_peer_features(LeFeatureMask.CONNECTION_SUBRATING_HOST_SUPPORT)
    for command in (
        hci.HCI_LE_Subrate_Request_Command(central.handle, 3, 3, 0, 0, 200),
        hci.HCI_LE_Connection_Update_Command(central.handle, 30, 30, 0, 200, 0, 0),
    ):
        central.hci(command)
        central.ut_rx(
            hci.HCI_Command_Status_Event,
            command_opcode=command.op_code,
            status=ErrorCode.SUCCESS,
        )
    central.event_prepare()
    central.lt_rx(Opcode.LL_SUBRATE_IND)

    reason = ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    central.connection.complete(reason)
    for event_type in (UPDATE_DONE, hci.HCI_LE_Subrate_Change_Event):
        central.ut_rx(event_type, status=reason, connection_handle=central.handle)
    central.ut_rx(DISCONNECTED, reason=reason)
    assert central.connection.parameters.interval == 24
    central.ut_rx_q_is_empty()


@pytest.mark.parametrize(
    'response',
    [
        unknown_rsp(Opcode.LL_CONNECTION_PARAM_REQ),
        reject_ext_ind(
            Opcode.LL_CONNECTION_PARAM_REQ, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
        ),
    ],
    ids=['unknown', 'reject-unsupported'],
)
@pytest.mark.parametrize('known_features', [False, True])
def test_central_loc_unsupported_falls_back_to_update(
    central, response, known_features
):
    """A Central falls back to UPDATE_IND when the peer lacks PARAM_REQ.

    Purpose: Verify that when the peer refuses PARAM_REQ as unsupported (via
    UNKNOWN_RSP or REJECT_EXT_IND), the Central falls back to UPDATE_IND with the
    Host minimum, clears only the parameter-request feature bit, and uses
    UPDATE_IND directly next time (Vol 6, Part B, 5.1.1), for each refusal kind
    and for known/unknown peer features.

    Steps:
        1. Seed peer features (known or unknown) and request interval 30..40.
        2. Lower Tester receives PARAM_REQ and replies with the refusal.
        3. Lower Tester receives the fallback UPDATE_IND and acknowledges.
        4. Issue a second Host request.

    Expected:
        - No Host event follows the refusal before the fallback.
        - UPDATE_IND carries (30, 0, 300); the update completes at its Instant.
        - The parameter-request support bit is cleared (only that bit).
        - The next request goes straight to UPDATE_IND.
    """
    features = int(
        LeFeatureMask.CONNECTION_PARAMETERS_REQUEST_PROCEDURE | LeFeatureMask.LE_2M_PHY
    )
    central.set_peer_features(features if known_features else None)
    request(central, 30, 40, 0, 300)
    central.event_prepare()
    central.lt_rx(Opcode.LL_CONNECTION_PARAM_REQ)
    central.lt_tx(response)
    central.event_done()
    central.ut_rx_q_is_empty()
    # The Central falls back to UPDATE_IND with the Host minimum (5.1.1).
    central.event_prepare()
    update = central.lt_rx(Opcode.LL_CONNECTION_UPDATE_IND)
    assert unpack_update(update)[2:5] == (30, 0, 300)
    central.tx_ack()
    central.event_done()
    complete_update_at_instant(central, update)
    # Learned: the peer lacks the procedure; only that bit is cleared.
    assert not central.controls.connection_parameters.supports_parameter_request
    assert central.connection.peer_features == (
        features & ~int(LeFeatureMask.CONNECTION_PARAMETERS_REQUEST_PROCEDURE)
        if known_features
        else None
    )
    # The next Host request goes directly to UPDATE_IND.
    request(central, 24)
    central.event_prepare()
    central.lt_rx(Opcode.LL_CONNECTION_UPDATE_IND)


def test_central_loc_known_unsupported_peer_uses_update_directly(central):
    """A Central skips PARAM_REQ for a peer known to lack the feature.

    Purpose: Verify that when the peer features are cached without the parameter
    request procedure, a Host request goes straight to UPDATE_IND with no
    response timer and completes at its Instant.

    Steps:
        1. Cache empty peer features and request interval 50..60, timeout 300.
        2. Lower Tester receives UPDATE_IND and acknowledges.
        3. Idle events run to the Instant.

    Expected:
        - UPDATE_IND carries interval 50 and no response timer is armed.
        - Host gets Connection Update Complete (SUCCESS, interval 50).
    """
    central.set_peer_features(0)
    request(central, 50, 60, 0, 300)
    central.event_prepare()
    update = central.lt_rx(Opcode.LL_CONNECTION_UPDATE_IND)
    assert unpack_update(update)[2] == 50
    assert central.controls.deadline_us == 0
    central.tx_ack()
    central.event_done()
    complete_update_at_instant(central, update)


@pytest.mark.parametrize(
    'response,status',
    [
        (
            reject_ext_ind(
                Opcode.LL_CONNECTION_PARAM_REQ,
                ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR,
            ),
            ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR,
        ),
        (
            reject_ind(ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR),
            ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR,
        ),
        (
            reject_ext_ind(Opcode.LL_CONNECTION_PARAM_REQ, PROCEDURE_COLLISION),
            PROCEDURE_COLLISION,
        ),
    ],
    ids=['reject-ext', 'reject-legacy', 'collision'],
)
@pytest.mark.parametrize('role', [CENTRAL, PERIPHERAL])
def test_loc_param_req_rejected(iut, role, response, status):
    """A rejected PARAM_REQ completes the update with the refusal's status.

    Purpose: Verify that a peer rejection of PARAM_REQ (reject-ext, legacy
    reject, or collision) completes the local update with the matching status,
    keeps the current interval and leaves no tracked exchange, for each refusal
    kind and both roles.

    Steps:
        1. Upper Tester requests interval 30.
        2. Lower Tester receives PARAM_REQ and replies with the refusal.

    Expected:
        - Host gets Connection Update Complete with the expected status,
          interval 24 (unchanged).
        - The procedure is idle; no local exchange remains tracked.
    """
    dut = iut(role)
    request(dut, 30)
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_CONNECTION_PARAM_REQ)
    dut.lt_tx(response)
    dut.event_done()
    dut.ut_rx(UPDATE_DONE, status=status, connection_interval=24)
    dut.ut_rx_q_is_empty()
    assert_idle(dut)
    assert not dut.connection.llcp.local_exchanges


def test_loc_rejection_for_other_procedure_is_not_ours(central):
    """A rejection naming a different opcode does not end our update.

    Purpose: Verify that a REJECT_EXT_IND referencing LL_PHY_REQ is not taken as
    the answer to the outstanding PARAM_REQ: the parameter request stays pending
    and its deadline is unchanged.

    Steps:
        1. Upper Tester requests interval 30 and record the deadline.
        2. Lower Tester receives PARAM_REQ.
        3. Lower Tester sends REJECT_EXT_IND(LL_PHY_REQ, ...).

    Expected:
        - No Host notification is produced.
        - The parameter request is still pending with the same deadline.
    """
    request(central, 30)
    deadline = central.controls.deadline_us
    central.event_prepare()
    central.lt_rx(Opcode.LL_CONNECTION_PARAM_REQ)
    central.lt_tx(
        reject_ext_ind(
            Opcode.LL_PHY_REQ, ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR
        )
    )
    central.event_done()
    central.ut_rx_q_is_empty()
    assert central.controls.connection_parameters.parameter_request
    assert central.controls.deadline_us == deadline


@pytest.mark.parametrize('role', [CENTRAL, PERIPHERAL])
def test_loc_param_req_response_timeout(iut, role):
    """A PARAM_REQ response timeout disconnects the link.

    Purpose: Verify that when the peer acknowledges PARAM_REQ but never answers,
    the procedure timer expiry closes the connection with the response-timeout
    reason, for both roles.

    Steps:
        1. Upper Tester requests interval 30.
        2. Lower Tester acknowledges PARAM_REQ but sends no response.
        3. Advance to just before, then past, the procedure deadline.

    Expected:
        - No timeout fires one microsecond early; it fires at the deadline.
        - Host gets Disconnection Complete (response timeout).
    """
    dut = iut(role)
    request(dut, 30)
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_CONNECTION_PARAM_REQ)
    dut.tx_ack()
    dut.event_done()
    assert not dut.expire_response_timer(early_us=1)
    assert dut.elapse(1)
    dut.ut_rx(DISCONNECTED, reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)


def test_central_loc_update_has_no_response_timer(central):
    """A Central-only UPDATE_IND path arms no response timer.

    Purpose: Verify that when the Central goes directly to UPDATE_IND (peer lacks
    the parameter request procedure), no response timer is running even after an
    intervening channel classification.

    Steps:
        1. Cache empty peer features and request interval 40.
        2. Classify channels on the Central.

    Expected:
        - The procedure deadline is zero.
    """
    central.set_peer_features(0)
    request(central, 40)
    central.controller.le_procedures.ll.classify_channels(0x0F)
    assert central.controls.deadline_us == 0


@pytest.mark.parametrize(
    'arguments,status',
    [
        ((6, 5, 0, 100), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        ((5, 5, 0, 100), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        ((24, 24, 500, 100), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        ((24, 24, 0, 9), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
    ],
)
def test_host_admission_rejects_invalid_parameters(central, arguments, status):
    """Invalid update parameters are rejected at admission.

    Purpose: Verify that out-of-range interval, min>max, excessive latency or
    too-short timeout are rejected with Invalid Command Parameters and never
    queue a PDU, across several invalid argument sets.

    Steps:
        1. Upper Tester requests an update with the invalid arguments.

    Expected:
        - The admission returns the expected error status.
        - Nothing is queued on the connection.
    """
    request(central, *arguments, status=status)
    assert not central.connection.event.queue


def test_host_admission_rejects_invalid_ce_lengths(central):
    """Invalid connection-event-length hints are rejected at admission.

    Purpose: Verify that a prepare with invalid CE length hints is refused with
    Invalid Command Parameters.

    Steps:
        1. Call prepare_parameters with valid_ce_lengths=False.

    Expected:
        - The prepared status is Invalid Command Parameters.
    """
    prepared = central.host(
        'prepare_parameters', 24, 24, 0, 100, valid_ce_lengths=False
    )
    assert prepared.status == ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR


def test_host_admission_busy(central):
    """A second update request while one is outstanding is disallowed.

    Purpose: Verify that the Host cannot start a second connection update while a
    request is still pending.

    Steps:
        1. Upper Tester requests interval 30.
        2. Upper Tester requests interval 40 before completion.

    Expected:
        - The second request returns Command Disallowed.
        - Only one PDU is queued.
    """
    request(central, 30)
    request(central, 40, status=ErrorCode.COMMAND_DISALLOWED_ERROR)
    assert len(central.connection.event.queue) == 1


def test_host_admission_unknown_handle(central):
    """Preparing an update on an unknown handle is refused.

    Purpose: Verify that preparing a connection update for a non-existent handle
    fails with Unknown Connection Identifier.

    Steps:
        1. Call prepare_parameters with handle 0x0EFF.

    Expected:
        - The prepared status is Unknown Connection Identifier.
    """
    prepared = central.controller.le_procedures.ll.prepare_parameters(
        0x0EFF, 24, 24, 0, 100, valid_ce_lengths=True
    )
    assert prepared.status == ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR


def test_periph_loc_rejected_for_known_unsupported_peer(peripheral):
    """A Peripheral cannot update a peer known to lack the feature.

    Purpose: Verify that a Peripheral, which cannot fall back to UPDATE_IND,
    refuses a Host update request when the peer is known not to support the
    parameter request procedure.

    Steps:
        1. Cache empty peer features on the Peripheral.
        2. Upper Tester requests interval 30.

    Expected:
        - The admission returns Unsupported Remote Feature.
    """
    peripheral.set_peer_features(0)
    request(peripheral, 30, status=ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)


# ---------------------------------------------------------------------------
# Peripheral, local
# ---------------------------------------------------------------------------
def test_periph_loc(peripheral):
    """A Peripheral local update completes when the Central's UPDATE_IND applies.

    Purpose: Verify that a Host update on a Peripheral sends PARAM_REQ, waits for
    the Central's UPDATE_IND, and completes only once the Instant has passed
    (Vol 6, Part B, 5.1.7.2).

    Steps:
        1. Upper Tester requests interval 30..40, timeout 200.
        2. Lower Tester receives PARAM_REQ and acknowledges.
        3. Lower Tester sends UPDATE_IND with interval 36, Instant +6.
        4. Idle events run to the Instant.

    Expected:
        - PARAM_REQ carries (30, 40, 0, 200); a response timer stays armed.
        - The interval becomes 36 at the Instant.
        - Host gets Connection Update Complete (SUCCESS, interval 36); idle after.
    """
    request(peripheral, 30, 40, 0, 200)
    peripheral.event_prepare()
    sent = peripheral.lt_rx(Opcode.LL_CONNECTION_PARAM_REQ)
    assert unpack_param(sent)[:4] == (30, 40, 0, 200)
    peripheral.tx_ack()
    peripheral.event_done()
    instant = peripheral.event_counter + 6
    peripheral.event_prepare()
    peripheral.lt_tx(connection_update_ind(36, instant, timeout=200))
    peripheral.event_done()
    # The procedure completes only once the Instant has passed (5.1.7.2).
    assert peripheral.controls.deadline_us
    peripheral.wait_instant(instant)
    peripheral.event_prepare()
    assert peripheral.connection.parameters.interval == 36
    peripheral.ut_rx(UPDATE_DONE, status=0, connection_interval=36)
    peripheral.ut_rx_q_is_empty()
    assert_idle(peripheral)


def test_periph_loc_central_rejects(peripheral):
    """A Peripheral reports failure when the Central rejects PARAM_REQ.

    Purpose: Verify that a Peripheral, unable to fall back to UPDATE_IND,
    reports the Central's rejection as the update failure and learns the peer
    lacks the feature for subsequent requests.

    Steps:
        1. Upper Tester requests interval 30.
        2. Lower Tester receives PARAM_REQ and sends REJECT_EXT_IND
           (unsupported remote feature).
        3. Upper Tester requests interval 30 again.

    Expected:
        - Host gets Connection Update Complete (unsupported remote feature).
        - The second request is refused with Unsupported Remote Feature.
    """
    request(peripheral, 30)
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_CONNECTION_PARAM_REQ)
    peripheral.lt_tx(
        reject_ext_ind(
            Opcode.LL_CONNECTION_PARAM_REQ, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
        )
    )
    peripheral.event_done()
    # A Peripheral cannot fall back; it reports the failure.
    peripheral.ut_rx(UPDATE_DONE, status=ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)
    peripheral.ut_rx_q_is_empty()
    request(peripheral, 30, status=ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)


# ---------------------------------------------------------------------------
# Central, remote
# ---------------------------------------------------------------------------
def test_central_rem_host_accepts(central):
    """A Central answers a peer PARAM_REQ the Host accepts with UPDATE_IND.

    Purpose: Verify that a peer-initiated PARAM_REQ is surfaced to the Host, and
    once the Host replies with accepted parameters the Central issues UPDATE_IND
    and completes at the Instant (Vol 6, Part B, 5.1.7).

    Steps:
        1. Lower Tester sends PARAM_REQ 30..40, latency 1, timeout 200.
        2. Host receives the Remote Connection Parameter Request event.
        3. Host replies with reply_parameters(34, 40, 1, 200).
        4. Lower Tester receives UPDATE_IND and acknowledges; run to Instant.

    Expected:
        - A response timer is armed while awaiting the Host reply.
        - UPDATE_IND carries interval 34, latency 1, timeout 200.
        - Host gets Connection Update Complete (SUCCESS, interval 34).
    """
    central.event_prepare()
    central.lt_tx(connection_param_req(30, 40, latency=1, timeout=200))
    central.event_done()
    central.ut_rx(
        REMOTE_REQUEST, interval_min=30, interval_max=40, max_latency=1, timeout=200
    )
    assert central.controls.deadline_us
    assert central.host('reply_parameters', 34, 40, 1, 200) == ErrorCode.SUCCESS
    central.event_prepare()
    update = central.lt_rx(Opcode.LL_CONNECTION_UPDATE_IND)
    assert unpack_update(update)[2:5] == (34, 1, 200)
    central.tx_ack()
    central.event_done()
    complete_update_at_instant(central, update)


def test_central_rem_host_rejects(central):
    """A Host rejection of a peer PARAM_REQ is sent as REJECT_EXT_IND.

    Purpose: Verify that when the Host rejects a peer PARAM_REQ, the Central
    sends REJECT_EXT_IND with the Host's reason, and further Host replies for the
    answered request are disallowed.

    Steps:
        1. Lower Tester sends PARAM_REQ 30.
        2. Host receives the Remote Connection Parameter Request event.
        3. Host rejects with unacceptable connection parameters.
        4. Lower Tester receives REJECT_EXT_IND and acknowledges.
        5. Host attempts another reject and another reply.

    Expected:
        - REJECT_EXT_IND carries (LL_CONNECTION_PARAM_REQ, the Host reason).
        - The procedure is idle; no further Host event.
        - Both the later reject and reply return Command Disallowed.
    """
    central.event_prepare()
    central.lt_tx(connection_param_req(30))
    central.event_done()
    central.ut_rx(REMOTE_REQUEST)
    reason = ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR
    assert central.host('reject_parameters', reason) == ErrorCode.SUCCESS
    central.event_prepare()
    central.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(Opcode.LL_CONNECTION_PARAM_REQ, reason),
    )
    central.tx_ack()
    central.event_done()
    central.ut_rx_q_is_empty()
    assert_idle(central)
    # Once answered, further Host replies are disallowed.
    assert (
        central.host('reject_parameters', reason) == ErrorCode.COMMAND_DISALLOWED_ERROR
    )
    assert (
        central.host('reply_parameters', 30, 30, 0, 100)
        == ErrorCode.COMMAND_DISALLOWED_ERROR
    )


def test_central_rem_host_reply_is_validated(central):
    """An invalid Host reply to a peer PARAM_REQ is rejected, request kept.

    Purpose: Verify that a reply_parameters call with min>max is rejected with
    Invalid Command Parameters and leaves the remote request still awaiting a
    valid answer.

    Steps:
        1. Lower Tester sends PARAM_REQ 30 and the Host sees the request event.
        2. Host replies with reply_parameters(30, 20, 0, 100).

    Expected:
        - The reply returns Invalid Command Parameters.
        - The remote parameters are still recorded (request not consumed).
    """
    central.event_prepare()
    central.lt_tx(connection_param_req(30))
    central.event_done()
    central.ut_rx(REMOTE_REQUEST)
    assert (
        central.host('reply_parameters', 30, 20, 0, 100)
        == ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
    )
    assert central.controls.connection_parameters.remote_parameters is not None


def test_central_rem_host_does_not_answer(central):
    """A Host that never answers a peer PARAM_REQ triggers a timeout.

    Purpose: Verify that if the Host never replies to a surfaced peer PARAM_REQ,
    the procedure timer expiry closes the connection with the response-timeout
    reason.

    Steps:
        1. Lower Tester sends PARAM_REQ 30 and the Host sees the request event.
        2. Expire the procedure timer with no Host reply.

    Expected:
        - Host gets Disconnection Complete (response timeout).
    """
    central.event_prepare()
    central.lt_tx(connection_param_req(30))
    central.event_done()
    central.ut_rx(REMOTE_REQUEST)
    assert central.expire_response_timer()
    central.ut_rx(DISCONNECTED, reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)


def test_central_rem_masked_event_rejects_automatically(central):
    """A masked remote-request event makes the Central reject automatically.

    Purpose: Verify that when the LE Remote Connection Parameter Request event is
    masked off, the Central does not surface the request to the Host and instead
    rejects PARAM_REQ with unsupported remote feature.

    Steps:
        1. Mask off the remote connection parameter request event.
        2. Lower Tester sends PARAM_REQ 30.
        3. Lower Tester receives the Central's response.

    Expected:
        - No Host notification is produced.
        - The IUT transmits REJECT_EXT_IND(LL_CONNECTION_PARAM_REQ, unsupported
          remote feature).
    """
    controller = central.controller
    controller.le_event_mask &= ~(
        1 << (hci.HCI_LE_REMOTE_CONNECTION_PARAMETER_REQUEST_EVENT - 1)
    )
    central.event_prepare()
    central.lt_tx(connection_param_req(30))
    central.event_done()
    central.ut_rx_q_is_empty()
    central.event_prepare()
    central.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(
            Opcode.LL_CONNECTION_PARAM_REQ, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
        ),
    )


def test_central_rem_identical_parameters_need_no_host(central):
    """A peer PARAM_REQ for the current parameters needs no Host involvement.

    Purpose: Verify that when the peer requests the parameters already in use,
    the Central issues UPDATE_IND without surfacing the request to the Host and
    reports no completion because nothing changes.

    Steps:
        1. Lower Tester sends PARAM_REQ for the current interval/latency/timeout.
        2. Lower Tester receives UPDATE_IND and acknowledges; run to Instant.

    Expected:
        - No Host notification is produced at any point.
        - The update completes at its Instant with no completion event.
    """
    parameters = central.connection.parameters
    central.event_prepare()
    central.lt_tx(
        connection_param_req(
            parameters.interval, latency=parameters.latency, timeout=parameters.timeout
        )
    )
    central.event_done()
    central.ut_rx_q_is_empty()
    central.event_prepare()
    update = central.lt_rx(Opcode.LL_CONNECTION_UPDATE_IND)
    central.tx_ack()
    central.event_done()
    # Unchanged parameters and no Host involvement: no completion event.
    complete_update_at_instant(central, update, notify=False)


@pytest.mark.parametrize(
    'periodicity,offsets,expected_interval',
    [
        (6, (0xFFFF,) * 6, 36),
        (0, (5, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF), 34),
        (0, (40, 3, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF), 34),
    ],
    ids=['periodicity', 'first-offset', 'skip-oversized-offset'],
)
def test_central_rem_preferences(central, periodicity, offsets, expected_interval):
    """The Central honours the peer's periodicity and offset preferences.

    Purpose: Verify that the interval chosen for UPDATE_IND respects the peer's
    preferred periodicity and that the window offset follows the first usable
    anchor offset, across periodicity-only, first-offset and oversized-offset
    cases.

    Steps:
        1. Lower Tester sends PARAM_REQ 34..40 with the preferences.
        2. Host accepts with reply_parameters(34, 40, 0, 100).
        3. Lower Tester receives UPDATE_IND.

    Expected:
        - The selected interval equals the expected value.
        - The window offset equals the computed anchor offset for the first
          usable offset, or 0 when none is usable.
    """
    central.event_prepare()
    central.lt_tx(
        connection_param_req(
            34, 40, periodicity=periodicity, reference=0, offsets=offsets
        )
    )
    central.event_done()
    central.ut_rx(REMOTE_REQUEST)
    central.host('reply_parameters', 34, 40, 0, 100)
    central.event_prepare()
    update = central.lt_rx(Opcode.LL_CONNECTION_UPDATE_IND)
    _, offset, interval, *_ = unpack_update(update)
    assert interval == expected_interval
    usable = [value for value in offsets if value < interval]
    if usable:
        old = central.connection.parameters.interval
        assert offset == anchor_offset(
            (0, usable[0]), instant_of(update), old, interval
        )
    else:
        assert offset == 0


@pytest.mark.parametrize(
    'fields',
    [
        dict(minimum=5),
        dict(minimum=30, maximum=20),
        dict(minimum=30, latency=500),
        dict(minimum=30, timeout=9),
        dict(minimum=3200, timeout=100),
    ],
    ids=['interval-low', 'min-gt-max', 'latency', 'timeout-low', 'timeout-short'],
)
@pytest.mark.parametrize('role', [CENTRAL, PERIPHERAL])
def test_rem_invalid_param_req(iut, role, fields):
    """An out-of-range peer PARAM_REQ is rejected as invalid parameters.

    Purpose: Verify that a peer PARAM_REQ violating interval, min<=max, latency
    or timeout bounds is answered with REJECT_EXT_IND (invalid parameters) and
    never reaches the Host, for both roles across several invalid field sets.

    Steps:
        1. Lower Tester sends PARAM_REQ with the invalid fields.
        2. Lower Tester receives the IUT's rejection.

    Expected:
        - The IUT transmits REJECT_EXT_IND(LL_CONNECTION_PARAM_REQ, invalid
          parameters).
        - No Host notification is produced.
    """
    dut = iut(role)
    fields = dict(fields)
    minimum = fields.pop('minimum')
    dut.event_prepare()
    dut.lt_tx(connection_param_req(minimum, **fields))
    dut.event_done()
    dut.event_prepare()
    dut.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(
            Opcode.LL_CONNECTION_PARAM_REQ,
            ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR,
        ),
    )
    dut.ut_rx_q_is_empty()


def test_central_invalid_param_rsp_is_rejected(central):
    """An out-of-range PARAM_RSP is rejected as invalid parameters.

    Purpose: Verify that a Central rejects a PARAM_RSP with an out-of-range
    interval using REJECT_EXT_IND referencing LL_CONNECTION_PARAM_RSP.

    Steps:
        1. Upper Tester requests interval 30.
        2. Lower Tester receives PARAM_REQ and answers PARAM_RSP 5.
        3. Lower Tester receives the IUT's rejection.

    Expected:
        - The IUT transmits REJECT_EXT_IND(LL_CONNECTION_PARAM_RSP, invalid
          parameters).
    """
    request(central, 30)
    central.event_prepare()
    central.lt_rx(Opcode.LL_CONNECTION_PARAM_REQ)
    central.lt_tx(connection_param_rsp(5))
    central.event_done()
    central.event_prepare()
    central.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(
            Opcode.LL_CONNECTION_PARAM_RSP,
            ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR,
        ),
    )


def test_central_receives_update_ind_answers_unsupported(central):
    """A Central answers a received CONNECTION_UPDATE_IND with UNKNOWN_RSP.

    Purpose: Verify that CONNECTION_UPDATE_IND is only legal from a Central, so a
    Central IUT that receives one replies UNKNOWN_RSP.

    Steps:
        1. Lower Tester sends CONNECTION_UPDATE_IND to the Central.
        2. Lower Tester receives the Central's response.

    Expected:
        - The IUT transmits UNKNOWN_RSP(LL_CONNECTION_UPDATE_IND).
    """
    central.event_prepare()
    central.lt_tx(connection_update_ind(30, 10))
    central.event_done()
    central.event_prepare()
    central.lt_rx(Opcode.LL_UNKNOWN_RSP, unknown_rsp(Opcode.LL_CONNECTION_UPDATE_IND))


def test_central_unsolicited_param_rsp_is_ignored(central):
    """A Central silently ignores an unsolicited PARAM_RSP.

    Purpose: Verify that a PARAM_RSP with no outstanding PARAM_REQ is ignored:
    no reply and no Host event.

    Steps:
        1. Lower Tester sends PARAM_RSP 30.
        2. Run two idle connection events.

    Expected:
        - No Host notification is produced.
    """
    central.event_prepare()
    central.lt_tx(connection_param_rsp(30))
    central.event_done()
    central.run_events(2)
    central.ut_rx_q_is_empty()


# ---------------------------------------------------------------------------
# Peripheral, remote
# ---------------------------------------------------------------------------
def test_periph_rem_param_req(peripheral):
    """A Peripheral answers a peer PARAM_REQ with PARAM_RSP then awaits UPDATE_IND.

    Purpose: Verify that a Peripheral surfaces a peer PARAM_REQ to the Host,
    answers with PARAM_RSP keeping the Central's periodicity preference, and
    completes when the Central's UPDATE_IND applies at the Instant.

    Steps:
        1. Lower Tester sends PARAM_REQ 30..40 with periodicity 10.
        2. Host accepts with reply_parameters(30, 40, 0, 100).
        3. Lower Tester receives PARAM_RSP and acknowledges.
        4. Lower Tester sends UPDATE_IND interval 40, Instant +6; run to Instant.

    Expected:
        - PARAM_RSP carries (30, 40, 0, 100, 10); a response timer stays armed.
        - Host gets Connection Update Complete (SUCCESS, interval 40); idle after.
    """
    peripheral.event_prepare()
    peripheral.lt_tx(connection_param_req(30, 40, periodicity=10))
    peripheral.event_done()
    peripheral.ut_rx(REMOTE_REQUEST, interval_min=30, interval_max=40)
    peripheral.host('reply_parameters', 30, 40, 0, 100)
    peripheral.event_prepare()
    response = peripheral.lt_rx(Opcode.LL_CONNECTION_PARAM_RSP)
    # The Peripheral keeps the Central's periodicity preference.
    assert unpack_param(response)[:5] == (30, 40, 0, 100, 10)
    peripheral.tx_ack()
    peripheral.event_done()
    assert peripheral.controls.deadline_us
    instant = peripheral.event_counter + 6
    peripheral.event_prepare()
    peripheral.lt_tx(connection_update_ind(40, instant))
    peripheral.event_done()
    assert peripheral.controls.deadline_us
    peripheral.wait_instant(instant)
    peripheral.event_prepare()
    peripheral.ut_rx(UPDATE_DONE, status=0, connection_interval=40)
    assert_idle(peripheral)


def test_periph_rem_param_req_no_update_ind(peripheral):
    """A missing UPDATE_IND after PARAM_RSP times out and disconnects.

    Purpose: Verify that after a Peripheral answers a peer PARAM_REQ with
    PARAM_RSP, the Central failing to send UPDATE_IND expires the procedure timer
    and closes the link with the response-timeout reason.

    Steps:
        1. Lower Tester sends PARAM_REQ 30 and the Host accepts.
        2. Lower Tester receives PARAM_RSP and acknowledges.
        3. Expire the procedure timer with no UPDATE_IND.

    Expected:
        - Host gets Disconnection Complete (response timeout).
    """
    peripheral.event_prepare()
    peripheral.lt_tx(connection_param_req(30))
    peripheral.event_done()
    peripheral.ut_rx(REMOTE_REQUEST)
    peripheral.host('reply_parameters', 30, 30, 0, 100)
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_CONNECTION_PARAM_RSP)
    peripheral.tx_ack()
    peripheral.event_done()
    assert peripheral.expire_response_timer()
    peripheral.ut_rx(DISCONNECTED, reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)


@pytest.mark.parametrize(
    'notify',
    [True, False],
    ids=['changed', 'unchanged'],
)
def test_periph_rem_update_ind(peripheral, notify):
    """A direct peer UPDATE_IND applies and notifies only on a change.

    Purpose: Verify that a Peripheral applies a Central UPDATE_IND at its Instant
    and reports completion only when the interval actually changes, for the
    changed and unchanged cases.

    Steps:
        1. Lower Tester sends UPDATE_IND with the chosen interval, Instant +6.
        2. Idle events run to the Instant.

    Expected:
        - The interval becomes the requested value at the Instant.
        - Host gets Connection Update Complete (SUCCESS) only when it changed.
        - No further Host notification.
    """
    parameters = peripheral.connection.parameters
    interval = 40 if notify else parameters.interval
    instant = peripheral.event_counter + 6
    peripheral.event_prepare()
    peripheral.lt_tx(
        connection_update_ind(
            interval, instant, latency=parameters.latency, timeout=parameters.timeout
        )
    )
    peripheral.event_done()
    peripheral.wait_instant(instant)
    peripheral.event_prepare()
    assert peripheral.connection.parameters.interval == interval
    if notify:
        peripheral.ut_rx(UPDATE_DONE, status=0, connection_interval=interval)
    peripheral.ut_rx_q_is_empty()


def test_periph_rem_update_applies_window_and_restarts_supervision(peripheral):
    """A peer UPDATE_IND shifts the anchor and restarts supervision.

    Purpose: Verify that applying a Central UPDATE_IND with a transmit-window
    offset moves the anchor by the offset, restarts the supervision timer from
    the new anchor, and opens a receive window of the window size.

    Steps:
        1. Set the anchor and counter, then send UPDATE_IND interval 40, Instant 1,
           window size 3, offset 2, timeout 200.
        2. Advance to the Instant event.

    Expected:
        - The anchor and supervision-restart time move to anchor + offset units.
        - The supervision deadline is the new anchor plus the timeout.
        - An update window is open with a receive window of 3 interval units.
    """
    peripheral.connection.anchor_us = 40_000
    peripheral.connection.event_counter = 0
    peripheral.event_prepare()
    peripheral.lt_tx(connection_update_ind(40, 1, size=3, offset=2, timeout=200))
    peripheral.connection.event_counter = 1
    peripheral.event_prepare()
    connection = peripheral.connection
    expected = 40_000 + 2 * LL_CONN_INTERVAL_UNIT_US
    assert connection.anchor_us == connection.supervision_restart_us == expected
    assert connection.supervision_deadline_us == (
        expected + 200 * LL_SUPERVISION_TIMEOUT_UNIT_US
    )
    assert connection.update_window
    assert connection.receive_window_us == 3 * LL_CONN_INTERVAL_UNIT_US


def test_periph_rem_update_at_current_instant_has_no_window_offset(peripheral):
    """A peer UPDATE_IND at the current Instant applies with no window shift.

    Purpose: Verify that a Central UPDATE_IND whose Instant equals the current
    event applies immediately, keeping the anchor and opening no update window.

    Steps:
        1. Record the current anchor.
        2. Send UPDATE_IND interval 40 at the current counter, size 1, offset 2.

    Expected:
        - The interval becomes 40 and the anchor is unchanged.
        - No update window is opened.
        - Host gets Connection Update Complete (SUCCESS).
    """
    old_anchor = peripheral.connection.anchor_us
    peripheral.event_prepare()
    peripheral.lt_tx(
        connection_update_ind(40, peripheral.event_counter, size=1, offset=2)
    )
    assert peripheral.connection.parameters.interval == 40
    assert peripheral.connection.anchor_us == old_anchor
    assert not peripheral.connection.update_window
    peripheral.ut_rx(UPDATE_DONE, status=0)


@pytest.mark.parametrize(
    'kw',
    [
        dict(interval=5),
        dict(interval=30, size=0),
        dict(interval=30, size=30),
        dict(interval=30, offset=31),
        dict(interval=30, latency=500),
        dict(interval=30, timeout=9),
    ],
    ids=['interval', 'window-zero', 'window-ge-interval', 'offset', 'latency', 'to'],
)
def test_periph_rem_invalid_update_closes_link(peripheral, kw):
    """A peer UPDATE_IND with invalid fields closes the link.

    Purpose: Verify that a Central UPDATE_IND violating interval, window size,
    window offset, latency or timeout bounds closes the connection with invalid
    parameters, across several malformed field sets.

    Steps:
        1. Lower Tester sends UPDATE_IND with the malformed fields, Instant 10.

    Expected:
        - The connection is closed.
        - Host gets Disconnection Complete (invalid parameters).
    """
    kw = dict(kw)
    interval = kw.pop('interval')
    peripheral.event_prepare()
    peripheral.lt_tx(connection_update_ind(interval, 10, **kw))
    assert peripheral.connection.closed
    peripheral.ut_rx(DISCONNECTED, reason=ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR)


@pytest.mark.parametrize('distance', [-1, 0x8000])
def test_periph_rem_update_instant_passed(peripheral, distance):
    """A peer UPDATE_IND with a past Instant closes the link.

    Purpose: Verify that a Central UPDATE_IND whose Instant has already passed
    (just behind or across the wrap boundary) closes the connection with
    instant-passed.

    Steps:
        1. Set the event counter to 10.
        2. Lower Tester sends UPDATE_IND with the past Instant.

    Expected:
        - Host gets Disconnection Complete (instant passed).
    """
    peripheral.connection.event_counter = 10
    peripheral.event_prepare()
    peripheral.lt_tx(connection_update_ind(40, (10 + distance) & 0xFFFF))
    peripheral.ut_rx(DISCONNECTED, reason=ErrorCode.INSTANT_PASSED_ERROR)


def test_periph_rem_second_instant_while_pending_closes_link(peripheral):
    """A second UPDATE_IND while one is pending closes the link.

    Purpose: Verify that a Peripheral cannot hold two pending connection updates:
    a second UPDATE_IND arriving before the first Instant is invalid and closes
    the connection.

    Steps:
        1. Lower Tester sends UPDATE_IND interval 40, Instant 20.
        2. Lower Tester sends UPDATE_IND interval 50, Instant 22.

    Expected:
        - Host gets Disconnection Complete (invalid parameters).
    """
    peripheral.event_prepare()
    peripheral.lt_tx(connection_update_ind(40, 20))
    peripheral.event_done()
    peripheral.event_prepare()
    peripheral.lt_tx(connection_update_ind(50, 22))
    peripheral.ut_rx(DISCONNECTED, reason=ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR)


def test_periph_rem_masked_event_rejects_automatically(peripheral):
    """A masked remote-request event makes the Peripheral reject automatically.

    Purpose: Verify that when the LE Remote Connection Parameter Request event is
    masked off, a Peripheral rejects a peer PARAM_REQ with unsupported remote
    feature without surfacing it to the Host.

    Steps:
        1. Mask off the remote connection parameter request event.
        2. Lower Tester sends PARAM_REQ 30.
        3. Lower Tester receives the Peripheral's response.

    Expected:
        - The IUT transmits REJECT_EXT_IND(LL_CONNECTION_PARAM_REQ, unsupported
          remote feature).
        - No Host notification is produced.
    """
    peripheral.controller.le_event_mask &= ~(
        1 << (hci.HCI_LE_REMOTE_CONNECTION_PARAMETER_REQUEST_EVENT - 1)
    )
    peripheral.event_prepare()
    peripheral.lt_tx(connection_param_req(30))
    peripheral.event_done()
    peripheral.event_prepare()
    peripheral.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(
            Opcode.LL_CONNECTION_PARAM_REQ, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
        ),
    )
    peripheral.ut_rx_q_is_empty()


# ---------------------------------------------------------------------------
# Central local update: Instant and anchor at first transmission
# ---------------------------------------------------------------------------
def test_central_loc_update_instant_assigned_at_first_transmission(central):
    """The UPDATE_IND Instant is fixed at first transmission, not at request.

    Purpose: Verify that when queued ACL data delays the UPDATE_IND, its Instant
    is computed from the event counter at the actual first transmission, and a
    retransmission keeps that value.

    Steps:
        1. Cache empty peer features and enqueue two ACL packets.
        2. Request interval 40; drain the two data events with acks.
        3. Transmit UPDATE_IND, then retransmit it without a new ack.

    Expected:
        - The Instant equals the counter at first transmission plus 6.
        - The retransmission carries the identical UPDATE_IND.
    """
    central.set_peer_features(0)
    # Older ACL data delays the control PDU by two connection events.
    central.connection.enqueue_acl(False, b'first')
    central.connection.enqueue_acl(False, b'second')
    request(central, 40)
    for _ in range(2):
        central.event_prepare()
        central.lt_rx_data()
        central.tx_ack()
        central.event_done()
    central.event_prepare()
    update = central.lt_rx(Opcode.LL_CONNECTION_UPDATE_IND)
    assert instant_of(update) == (central.event_counter + 6) & LL_EVENT_COUNTER_MASK
    # A retransmission keeps the already transmitted value.
    central.event_done()
    central.event_prepare()
    assert central.lt_rx(Opcode.LL_CONNECTION_UPDATE_IND) == update
    central.tx_ack()


def test_central_rem_offset_is_recomputed_when_indication_is_delayed(central):
    """The window offset is recomputed when the UPDATE_IND is delayed.

    Purpose: Verify that preparing a delayed UPDATE_IND recomputes the window
    offset from the reference against the new Instant, and records it on the
    pending update.

    Steps:
        1. Request an update with a reference anchor and read the queued payload.
        2. Advance the event counter by one and prepare the payload again.

    Expected:
        - The rewritten UPDATE_IND keeps interval 18.
        - The offset equals (reference - Instant * old interval) mod interval.
        - The pending update records that offset and Instant.
    """
    controls = central.controls
    controls.connection_parameters.request_update(
        18, 0, 300, reference=(central.event_counter, 1)
    )
    payload = central.connection.event.queue[-1].payload
    central.connection.event_counter += 1
    rewritten = controls.prepare(payload)
    _, offset, interval, _, _, instant = UPDATE.unpack(rewritten[1:])
    assert interval == 18
    assert offset == (1 - instant * central.connection.parameters.interval) % interval
    assert controls.pending.parameters.window_offset == offset
    assert controls.pending.instant == instant


def test_central_loc_update_unacked_at_instant_closes_link(central):
    """An unacknowledged UPDATE_IND at its Instant closes the link.

    Purpose: Verify that if the peer never acknowledges the Central's UPDATE_IND
    before the Instant, the connection closes with instant-passed.

    Steps:
        1. Cache empty peer features and request interval 40.
        2. Lower Tester receives UPDATE_IND but never acknowledges.
        3. Advance the counter to the Instant and prepare that event.

    Expected:
        - Host gets Disconnection Complete (instant passed).
    """
    central.set_peer_features(0)
    request(central, 40)
    central.event_prepare()
    update = central.lt_rx(Opcode.LL_CONNECTION_UPDATE_IND)
    central.event_done()
    central.connection.event_counter = instant_of(update)
    central.event_prepare()
    central.ut_rx(DISCONNECTED, reason=ErrorCode.INSTANT_PASSED_ERROR)


def test_central_loc_update_not_yet_sent_waits(central):
    """An Instant reached before UPDATE_IND is sent does not close the link.

    Purpose: Verify that if the connection reaches the pending update's Instant
    before the UPDATE_IND has actually been transmitted, the link stays open and
    the pending update is retained (no premature instant-passed).

    Steps:
        1. Cache empty peer features and request interval 40.
        2. Set the event counter to the pending Instant and prepare the event.

    Expected:
        - The connection is not closed.
        - The same pending update is still held.
    """
    central.set_peer_features(0)
    request(central, 40)
    pending = central.controls.pending
    central.connection.event_counter = pending.instant
    central.event_prepare()
    assert not central.connection.closed and central.controls.pending is pending


def test_central_loc_update_applies_window_offset(central):
    """A Central applies its own UPDATE_IND window offset at the Instant.

    Purpose: Verify that a Central-driven update with a window offset shifts the
    anchor by that offset at the Instant and reports completion.

    Steps:
        1. Cache empty peer features and request an update with reference (0, 5).
        2. Lower Tester receives UPDATE_IND and acknowledges; run to Instant.

    Expected:
        - The anchor moves by the UPDATE_IND offset in interval units.
        - Host gets Connection Update Complete (SUCCESS, interval 40).
    """
    central.set_peer_features(0)
    central.controls.connection_parameters.request_update(40, 0, 100, reference=(0, 5))
    central.event_prepare()
    update = central.lt_rx(Opcode.LL_CONNECTION_UPDATE_IND)
    central.tx_ack()
    central.event_done()
    _, offset, *_ = unpack_update(update)
    central.wait_instant(instant_of(update))
    anchor = central.connection.anchor_us
    central.event_prepare()
    assert central.connection.anchor_us == anchor + offset * LL_CONN_INTERVAL_UNIT_US
    central.ut_rx(UPDATE_DONE, status=0, connection_interval=40)


# ---------------------------------------------------------------------------
# Pure policy functions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'minimum,maximum,periodicity,selected',
    [(13, 19, 6, 18), (12, 18, 6, 12), (13, 17, 6, 13), (13, 19, 0, 13)],
)
def test_preferred_interval_respects_accepted_bounds(
    minimum, maximum, periodicity, selected
):
    """preferred_interval picks a value honouring bounds and periodicity.

    Purpose: Verify that the pure selection function chooses an interval inside
    the accepted bounds that respects the periodicity preference, falling back to
    the minimum when no multiple fits, across several cases.

    Steps:
        1. Call preferred_interval(minimum, maximum, periodicity) per case.

    Expected:
        - The returned interval equals the expected selected value.
    """
    assert preferred_interval(minimum, maximum, periodicity) == selected


@pytest.mark.parametrize(
    'reference,instant,old_interval,interval,expected',
    [
        ((1, 1), 7, 24, 24, 1),
        ((1, 23), 7, 24, 24, 23),
        ((1, 1), 8, 24, 18, 13),
        ((LL_EVENT_COUNTER_MASK - 2, 1), 3, 24, 18, 1),
    ],
)
def test_preferred_anchor_uses_reference_counter_and_wrap(
    reference, instant, old_interval, interval, expected
):
    """anchor_offset computes the window offset with counter wrap.

    Purpose: Verify that the pure anchor_offset function derives the window
    offset from the reference anchor, the Instant and the old/new intervals,
    including event-counter wrap, across several cases.

    Steps:
        1. Call anchor_offset(reference, instant, old_interval, interval).

    Expected:
        - The returned offset equals the expected value.
    """
    assert anchor_offset(reference, instant, old_interval, interval) == expected


@pytest.mark.parametrize(
    'minimum,maximum,preference,expected',
    [(18, 18, 6, 0), (13, 19, 0, 1), (13, 19, 6, 6), (13, 19, 20, 1)],
)
def test_periodicity_prefers_peer_value_only_for_a_range(
    minimum, maximum, preference, expected
):
    """preferred_periodicity uses the peer value only when a range allows it.

    Purpose: Verify that the pure preferred_periodicity function honours the
    peer's periodicity preference only when the accepted interval range leaves
    room, otherwise falling back to 1, across several cases.

    Steps:
        1. Call preferred_periodicity(minimum, maximum, preference) per case.

    Expected:
        - The returned periodicity equals the expected value.
    """
    assert preferred_periodicity(minimum, maximum, preference) == expected
