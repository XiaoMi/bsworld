# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Procedure collisions (Core Vol 6, Part B, 5.3), Zephyr ctrl_collision style.

Same-procedure collisions are rejected with 0x23 (LL Procedure Collision);
different Instant procedures with 0x2A (Different Transaction Collision). The
Central's procedure always wins; a Peripheral defers the Central's request until
its own procedure has been rejected, then serves it.

Matrix: {PHY x PHY, PHY x Connection Parameters, Connection Parameters x
Connection Parameters / Channel Map} x {Central local, Central remote,
Peripheral local} x {same-procedure collision, different-transaction collision,
deferred-request served, deferred-request T_PRT timeout}. Scenarios run through
the Lower Tester / Upper Tester harness (llcp/harness.py): PDUs cross the real
ConnectionEvent ARQ, and Host notifications are the real HCI events.
"""

import pytest

from bscontroller.linklayer import LL_RESPONSE_TIMEOUT_US
from bscontroller.linklayer.instant import InstantUpdate
from bscontroller.radio.base import Phy
from tests.unit.linklayer.llcp.harness import (
    PHY_2M,
    PHY_CODED,
    PROCEDURE_COLLISION,
    ErrorCode,
    Opcode,
    channel_map_ind,
    connection_param_req,
    connection_update_ind,
    hci,
    instant_of,
    length,
    pdu,
    phy_req,
    phy_rsp,
    phy_update_ind,
    reject_ext_ind,
    unknown_rsp,
)

DIFFERENT = ErrorCode.DIFFERENT_TRANSACTION_COLLISION_ERROR
PHY_DONE = hci.HCI_LE_PHY_Update_Complete_Event
UPDATE_DONE = hci.HCI_LE_Connection_Update_Complete_Event
REMOTE_REQUEST = hci.HCI_LE_Remote_Connection_Parameter_Request_Event
DISCONNECTED = hci.HCI_Disconnection_Complete_Event


def request_parameters(dut, minimum, maximum=None):
    dut.host_start(
        'prepare_parameters',
        minimum,
        maximum or minimum,
        0,
        100,
        valid_ce_lengths=True,
    )


# ---------------------------------------------------------------------------
# PHY x PHY
# ---------------------------------------------------------------------------
def test_phy_central_loc_collision(central):
    """Central's PHY_REQ crosses the Peripheral's: Central rejects, keeps its own.

    Purpose: Verify that when the Central has a PHY Update outstanding and the
    peer's PHY_REQ arrives, the Central rejects the peer with LL Procedure
    Collision (0x23) and still completes its own PHY Update (5.3).

    Steps:
        1. Upper Tester starts a Central PHY Update to 2M.
        2. Lower Tester receives LL_PHY_REQ, then sends its own PHY_REQ (Coded).
        3. Lower Tester answers the Central's request with PHY_RSP.
        4. The IUT sends PHY_UPDATE_IND; both sides wait for the Instant.

    Expected:
        - The crossing peer PHY_REQ draws LL_REJECT_EXT_IND (PROCEDURE_COLLISION).
        - The Central's own PHY Update applies at its Instant.
        - Host gets LE PHY Update Complete (SUCCESS, 2M/2M) and nothing else.
    """
    central.host_start('prepare_phy', PHY_2M, PHY_2M, 0, 0)
    central.event_prepare()
    central.lt_rx(Opcode.LL_PHY_REQ)
    central.lt_tx(phy_req(PHY_CODED, PHY_CODED))
    central.lt_rx(
        Opcode.LL_REJECT_EXT_IND, reject_ext_ind(Opcode.LL_PHY_REQ, PROCEDURE_COLLISION)
    )
    central.tx_ack()
    central.event_done()
    # The peer then answers the Central's own request.
    central.event_prepare()
    central.lt_tx(phy_rsp(PHY_2M, PHY_2M), ack=False)
    update = central.lt_rx(Opcode.LL_PHY_UPDATE_IND)
    central.tx_ack()
    central.event_done()
    central.wait_instant(instant_of(update))
    central.event_prepare()
    central.ut_rx(PHY_DONE, status=0, tx_phy=Phy.LE_2M, rx_phy=Phy.LE_2M)
    central.ut_rx_q_is_empty()


def test_phy_central_rem_collision(central):
    """Peripheral's request first, then the Host asks: Host is told to wait.

    Purpose: Verify that while the Central is serving a peer-initiated PHY Update,
    a Host-initiated PHY Update is refused with Command Disallowed, and becomes
    allowed again once the remote procedure completes (5.3).

    Steps:
        1. Lower Tester sends PHY_REQ; the Central begins serving it.
        2. Upper Tester tries to start its own PHY Update.
        3. The IUT sends PHY_UPDATE_IND; both sides wait for the Instant.
        4. Upper Tester retries the PHY Update after completion.

    Expected:
        - The Host's PHY Update during the remote procedure returns
          COMMAND_DISALLOWED.
        - Host gets LE PHY Update Complete (SUCCESS) for the remote procedure.
        - The retried Host PHY Update is admitted.
    """
    central.event_prepare()
    central.lt_tx(phy_req(PHY_2M, PHY_2M))
    central.host_start(
        'prepare_phy',
        PHY_CODED,
        PHY_CODED,
        0,
        0,
        status=ErrorCode.COMMAND_DISALLOWED_ERROR,
    )
    update = central.lt_rx(Opcode.LL_PHY_UPDATE_IND)
    central.tx_ack()
    central.event_done()
    central.wait_instant(instant_of(update))
    central.event_prepare()
    central.ut_rx(PHY_DONE, status=0)
    central.host_start('prepare_phy', PHY_CODED, PHY_CODED, 2, 0)


def test_phy_periph_loc_collision(peripheral):
    """Peripheral defers the Central's PHY_REQ until its own is rejected.

    Purpose: Verify that a Peripheral with an outstanding PHY_REQ defers the
    Central's crossing PHY_REQ (does not reject it), keeps its own response
    timer, and then serves the deferred request with a fresh timer once its own
    procedure is rejected (5.3).

    Steps:
        1. Upper Tester starts a Peripheral PHY Update to 2M.
        2. Lower Tester receives LL_PHY_REQ, then sends the Central's PHY_REQ.
        3. Lower Tester sends a second Central PHY_REQ.
        4. Lower Tester rejects the Peripheral's own request (PROCEDURE_COLLISION).
        5. The IUT serves the deferred request: PHY_RSP, then PHY_UPDATE_IND.

    Expected:
        - The first deferred Central request is stored and not answered yet,
          while the local response timer is unchanged.
        - A second Central request is refused with LL_REJECT_EXT_IND and does not
          replace the deferred one.
        - Host gets LE PHY Update Complete (PROCEDURE_COLLISION) for the local
          procedure.
        - The deferred request is then served under a fresh LL_RESPONSE_TIMEOUT,
          completing with LE PHY Update Complete (SUCCESS, Coded).
    """
    peripheral.host_start('prepare_phy', PHY_2M, PHY_2M, 0, 0)
    deadline = peripheral.controls.deadline_us
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_PHY_REQ)
    central_request = phy_req(PHY_CODED, PHY_CODED)
    peripheral.lt_tx(central_request)
    # Not answered yet, and the local timer still runs.
    peripheral.lt_rx_q_is_empty()
    assert peripheral.controls.phy.remote_phy_request == central_request
    assert peripheral.controls.deadline_us == deadline
    # A second Central request is refused and does not replace the deferred one.
    peripheral.lt_tx(phy_req(PHY_2M, PHY_2M))
    assert peripheral.controls.phy.remote_phy_request == central_request
    peripheral.lt_rx(
        Opcode.LL_REJECT_EXT_IND, reject_ext_ind(Opcode.LL_PHY_REQ, PROCEDURE_COLLISION)
    )
    peripheral.tx_ack()
    peripheral.event_done()
    peripheral.elapse(1000)
    peripheral.event_prepare()
    peripheral.lt_tx(reject_ext_ind(Opcode.LL_PHY_REQ, PROCEDURE_COLLISION))
    peripheral.ut_rx(PHY_DONE, status=PROCEDURE_COLLISION)
    # Then the deferred Central request is served, with its own fresh timer.
    assert peripheral.controls.phy.phy_request
    assert not peripheral.controls.phy.phy_request_local
    assert peripheral.controls.deadline_us == peripheral.radio.now_us + (
        LL_RESPONSE_TIMEOUT_US
    )
    peripheral.lt_rx(Opcode.LL_PHY_RSP)
    peripheral.tx_ack()
    instant = peripheral.event_counter + 6
    peripheral.lt_tx(phy_update_ind(PHY_CODED, PHY_CODED, instant))
    peripheral.event_done()
    peripheral.wait_instant(instant)
    peripheral.event_prepare()
    peripheral.ut_rx(PHY_DONE, status=0, tx_phy=Phy.LE_CODED)


def test_phy_periph_loc_collision_deferred_request_times_out(peripheral):
    """A deferred Central PHY_REQ stays under the local T_PRT until it expires.

    Purpose: Verify that an unrelated rejection does not release the Peripheral's
    deferred Central PHY_REQ, so the original local response timer still governs
    and a timeout closes the link (5.3, T_PRT).

    Steps:
        1. Upper Tester starts a Peripheral PHY Update to 2M.
        2. Lower Tester receives LL_PHY_REQ, then sends a Central PHY_REQ.
        3. Lower Tester sends an unrelated LL_REJECT_EXT_IND (for CPR, DIFFERENT).
        4. Simulated time advances to the local response deadline.

    Expected:
        - The unrelated rejection leaves the local PHY request still pending.
        - At the deadline the Host gets Disconnection Complete
          (LMP_OR_LL_RESPONSE_TIMEOUT).
    """
    peripheral.host_start('prepare_phy', PHY_2M, PHY_2M, 0, 0)
    deadline = peripheral.controls.deadline_us
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_PHY_REQ)
    peripheral.lt_tx(phy_req(PHY_2M, PHY_2M))
    # An unrelated rejection does not release the deferred request.
    peripheral.lt_tx(reject_ext_ind(Opcode.LL_CONNECTION_PARAM_REQ, DIFFERENT))
    assert peripheral.controls.phy.phy_request_local
    peripheral.elapse(deadline - peripheral.radio.now_us)
    peripheral.ut_rx(DISCONNECTED, reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)


# ---------------------------------------------------------------------------
# PHY x Connection Parameters
# ---------------------------------------------------------------------------
def test_phy_conn_central_loc_collision(central):
    """Central PHY procedure in flight; Peripheral's CPR is a different collision.

    Purpose: Verify that a Central running a PHY Update rejects the peer's
    Connection Parameter Request as a different-transaction collision and does
    not store its parameters (5.3).

    Steps:
        1. Upper Tester starts a Central PHY Update to 2M.
        2. Lower Tester receives LL_PHY_REQ, then sends LL_CONNECTION_PARAM_REQ.

    Expected:
        - The CPR draws LL_REJECT_EXT_IND (PROCEDURE_COLLISION) for the
          CONNECTION_PARAM_REQ opcode.
        - No remote parameters are stored and nothing reaches the Host.
    """
    central.host_start('prepare_phy', PHY_2M, PHY_2M, 0, 0)
    central.event_prepare()
    central.lt_rx(Opcode.LL_PHY_REQ)
    central.lt_tx(connection_param_req(30))
    central.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(Opcode.LL_CONNECTION_PARAM_REQ, PROCEDURE_COLLISION),
    )
    central.tx_ack()
    assert central.controls.connection_parameters.remote_parameters is None
    central.ut_rx_q_is_empty()


@pytest.mark.parametrize('host_replied', [False, True])
def test_phy_periph_loc_with_central_cpr(peripheral, host_replied):
    """A served Central CPR keeps its own timer when the PHY is later rejected.

    Purpose: Verify that while the Peripheral's PHY Update is outstanding the
    Central's Connection Parameter Request is served (reported to the Host under
    its own response timer), and a later rejection of the PHY procedure neither
    stops nor restarts that CPR timer; the CPR then times out (5.3, T_PRT). Runs
    with and without the Host having replied to the parameter request.

    Steps:
        1. Upper Tester starts a Peripheral PHY Update to 2M.
        2. Lower Tester receives LL_PHY_REQ, then sends LL_CONNECTION_PARAM_REQ.
        3. Host is notified; optionally the Host replies to the parameters.
        4. Lower Tester rejects the PHY_REQ (DIFFERENT_TRANSACTION_COLLISION).
        5. Simulated time advances to the CPR response deadline.

    Expected:
        - Host gets LE Remote Connection Parameter Request (interval_min 30).
        - Host gets LE PHY Update Complete (DIFFERENT) for the local PHY.
        - The CPR response deadline is unchanged by the PHY rejection.
        - At the deadline the Host gets Disconnection Complete
          (LMP_OR_LL_RESPONSE_TIMEOUT).
    """
    peripheral.host_start('prepare_phy', PHY_2M, PHY_2M, 0, 0)
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_PHY_REQ)
    peripheral.lt_tx(connection_param_req(30))
    peripheral.ut_rx(REMOTE_REQUEST, interval_min=30)
    peripheral.elapse(1000)
    if host_replied:
        peripheral.host('reply_parameters', 30, 30, 0, 100)
    deadline = peripheral.controls.deadline_us
    peripheral.lt_tx(reject_ext_ind(Opcode.LL_PHY_REQ, DIFFERENT))
    peripheral.ut_rx(PHY_DONE, status=DIFFERENT)
    assert peripheral.controls.deadline_us == deadline
    peripheral.elapse(deadline - peripheral.radio.now_us)
    peripheral.ut_rx(DISCONNECTED, reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)


def test_phy_periph_loc_with_identical_cpr_keeps_cpr_timer(peripheral):
    """An auto-accepted identical CPR keeps its timer through a PHY UNKNOWN_RSP.

    Purpose: Verify that a Central CPR carrying the current parameters is accepted
    without the Host (the Peripheral still owes PARAM_RSP and awaits UPDATE_IND
    under its own timer), and an UNKNOWN_RSP ending the local PHY procedure does
    not disturb that CPR timer, which then times out (5.3, T_PRT).

    Steps:
        1. Upper Tester starts a Peripheral PHY Update to 2M.
        2. Lower Tester sends LL_CONNECTION_PARAM_REQ with identical parameters.
        3. Lower Tester sends LL_UNKNOWN_RSP for LL_PHY_REQ.
        4. Simulated time advances to the CPR response deadline.

    Expected:
        - Host gets LE PHY Update Complete (UNSUPPORTED_REMOTE_FEATURE).
        - The CPR response deadline is unchanged by the PHY completion.
        - At the deadline the Host gets Disconnection Complete
          (LMP_OR_LL_RESPONSE_TIMEOUT).
    """
    parameters = peripheral.connection.parameters
    peripheral.host_start('prepare_phy', PHY_2M, PHY_2M, 0, 0)
    # Identical parameters: accepted without Host, the Peripheral still owes
    # PARAM_RSP and then awaits UPDATE_IND under its own timer.
    peripheral.lt_tx(
        connection_param_req(
            parameters.interval, latency=parameters.latency, timeout=parameters.timeout
        )
    )
    deadline = peripheral.controls.deadline_us
    peripheral.lt_tx(unknown_rsp(Opcode.LL_PHY_REQ))
    peripheral.ut_rx(PHY_DONE, status=ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)
    assert peripheral.controls.deadline_us == deadline
    peripheral.elapse(deadline - peripheral.radio.now_us)
    peripheral.ut_rx(DISCONNECTED, reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)


def test_phy_rem_while_instant_pending_is_different_collision(central):
    """A peer PHY_REQ during a pending Instant update is a different collision.

    Purpose: Verify that when the Central already has an Instant-bearing update
    pending, a peer PHY_REQ is rejected as a different-transaction collision and
    the pending update is left intact (5.3).

    Steps:
        1. The Central has an InstantUpdate (channel map) pending.
        2. Lower Tester sends PHY_REQ.

    Expected:
        - The IUT answers LL_REJECT_EXT_IND (DIFFERENT) for LL_PHY_REQ.
        - The pending InstantUpdate is unchanged.
    """
    central.controls.pending = InstantUpdate(20, channel_map=3)
    central.lt_tx(phy_req(PHY_2M, PHY_2M))
    central.lt_rx(
        Opcode.LL_REJECT_EXT_IND, reject_ext_ind(Opcode.LL_PHY_REQ, DIFFERENT)
    )
    assert central.controls.pending == InstantUpdate(20, channel_map=3)


# ---------------------------------------------------------------------------
# Connection Parameters x Connection Parameters / Channel Map
# ---------------------------------------------------------------------------
def test_cpr_central_loc_collision(central):
    """A crossing CPR during the Central's own CPR is a same-procedure collision.

    Purpose: Verify that a Central running a Connection Parameter Request rejects
    the peer's crossing CPR with LL Procedure Collision and keeps its own request
    pending without storing the peer's parameters (5.3).

    Steps:
        1. Upper Tester starts a Central CPR (interval_min 24).
        2. Lower Tester receives LL_CONNECTION_PARAM_REQ, then sends its own CPR.

    Expected:
        - The crossing CPR draws LL_REJECT_EXT_IND (PROCEDURE_COLLISION).
        - The Central's own parameter_request stays pending with no remote
          parameters stored, and nothing reaches the Host.
    """
    request_parameters(central, 24)
    central.event_prepare()
    central.lt_rx(Opcode.LL_CONNECTION_PARAM_REQ)
    central.lt_tx(connection_param_req(30))
    central.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(Opcode.LL_CONNECTION_PARAM_REQ, PROCEDURE_COLLISION),
    )
    assert central.controls.connection_parameters.parameter_request
    assert central.controls.connection_parameters.remote_parameters is None
    central.ut_rx_q_is_empty()


@pytest.mark.parametrize('masked', [False, True])
def test_cpr_periph_loc_collision(peripheral, masked):
    """The Peripheral preserves the Central's CPR until its own is rejected.

    Purpose: Verify that a Peripheral with an outstanding CPR defers the Central's
    crossing CPR (no Host event, no reply, local timer untouched) and, once its
    own request is rejected, serves the deferred one; when the Remote Connection
    Parameter Request event is masked the deferred CPR is instead refused with
    UNSUPPORTED_REMOTE_FEATURE (5.3). Runs with the event unmasked and masked.

    Steps:
        1. Upper Tester starts a Peripheral CPR (interval_min 24).
        2. Lower Tester receives LL_CONNECTION_PARAM_REQ, then sends a Central CPR.
        3. Lower Tester sends a second Central CPR.
        4. Lower Tester rejects the Peripheral's own CPR (PROCEDURE_COLLISION).

    Expected:
        - The first deferred Central CPR is stored, draws no Host event and no
          reply, and leaves the local deadline unchanged.
        - A second Central CPR is refused with LL_REJECT_EXT_IND and does not
          replace the deferred one.
        - Host gets LE Connection Update Complete (PROCEDURE_COLLISION) for the
          local procedure.
        - Unmasked: Host gets LE Remote Connection Parameter Request (30/30), the
          deadline refreshes to LL_RESPONSE_TIMEOUT, and after the Host reply the
          IUT sends LL_CONNECTION_PARAM_RSP.
        - Masked: no Host event; the IUT answers LL_REJECT_EXT_IND
          (UNSUPPORTED_REMOTE_FEATURE) and clears the deadline.
    """
    if masked:
        peripheral.controller.le_event_mask &= ~(
            1 << (hci.HCI_LE_REMOTE_CONNECTION_PARAMETER_REQUEST_EVENT - 1)
        )
    request_parameters(peripheral, 24)
    deadline = peripheral.controls.deadline_us
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_CONNECTION_PARAM_REQ)
    request = connection_param_req(30)
    peripheral.lt_tx(request)
    # Deferred: no Host event, no reply, and the local timer is untouched.
    assert peripheral.controls.connection_parameters.remote_parameters == request[1:]
    peripheral.ut_rx_q_is_empty()
    peripheral.lt_rx_q_is_empty()
    assert peripheral.controls.deadline_us == deadline
    # A second Central request does not replace the deferred one.
    peripheral.lt_tx(connection_param_req(40))
    peripheral.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(Opcode.LL_CONNECTION_PARAM_REQ, PROCEDURE_COLLISION),
    )
    peripheral.tx_ack()
    peripheral.elapse(1000)
    peripheral.lt_tx(
        reject_ext_ind(Opcode.LL_CONNECTION_PARAM_REQ, PROCEDURE_COLLISION)
    )
    peripheral.ut_rx(UPDATE_DONE, status=PROCEDURE_COLLISION)
    if masked:
        peripheral.ut_rx_q_is_empty()
        peripheral.lt_rx(
            Opcode.LL_REJECT_EXT_IND,
            reject_ext_ind(
                Opcode.LL_CONNECTION_PARAM_REQ,
                ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR,
            ),
        )
        assert peripheral.controls.deadline_us == 0
    else:
        peripheral.ut_rx(
            REMOTE_REQUEST,
            connection_handle=peripheral.handle,
            interval_min=30,
            interval_max=30,
        )
        assert peripheral.controls.deadline_us == (
            peripheral.radio.now_us + LL_RESPONSE_TIMEOUT_US
        )
        peripheral.host('reply_parameters', 30, 30, 0, 100)
        peripheral.lt_rx(Opcode.LL_CONNECTION_PARAM_RSP)


def test_cpr_periph_loc_deferred_request_keeps_local_timeout(peripheral):
    """A deferred Central CPR stays under the Peripheral's local T_PRT.

    Purpose: Verify that deferring the Central's crossing CPR does not restart the
    Peripheral's own response timer, so the original deadline governs and a
    timeout closes the link (5.3, T_PRT).

    Steps:
        1. Upper Tester starts a Peripheral CPR (interval_min 24).
        2. Lower Tester sends a Central CPR, which is deferred.
        3. Simulated time advances to the local response deadline.

    Expected:
        - At the original deadline the Host gets Disconnection Complete
          (LMP_OR_LL_RESPONSE_TIMEOUT).
    """
    request_parameters(peripheral, 24)
    deadline = peripheral.controls.deadline_us
    peripheral.lt_tx(connection_param_req(30))
    peripheral.elapse(deadline - peripheral.radio.now_us)
    peripheral.ut_rx(DISCONNECTED, reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)


def test_cpr_periph_loc_central_answers_with_update(peripheral):
    """The Central may accept a crossing request by sending UPDATE_IND directly.

    Purpose: Verify that when the Peripheral's CPR is outstanding, the Central can
    resolve the crossing by replying with LL_CONNECTION_UPDATE_IND, which the
    Peripheral applies at the Instant and reports as a successful update (5.3).

    Steps:
        1. Upper Tester starts a Peripheral CPR (24..40).
        2. Lower Tester receives LL_CONNECTION_PARAM_REQ, then sends its own CPR.
        3. Lower Tester sends LL_CONNECTION_UPDATE_IND (interval 30) at an Instant.
        4. The Peripheral waits for the Instant and applies the update.

    Expected:
        - Host gets LE Connection Update Complete (SUCCESS, interval 30).
        - The procedure controls are idle afterwards.
    """
    request_parameters(peripheral, 24, 40)
    peripheral.lt_rx(Opcode.LL_CONNECTION_PARAM_REQ)
    peripheral.lt_tx(connection_param_req(30))
    instant = peripheral.event_counter + 6
    peripheral.lt_tx(connection_update_ind(30, instant))
    peripheral.wait_instant(instant)
    peripheral.event_prepare()
    peripheral.ut_rx(UPDATE_DONE, status=0, connection_interval=30)
    assert not peripheral.controls.busy


@pytest.mark.parametrize('iut_role', ['central', 'peripheral'])
def test_cpr_rem_while_channel_map_pending(request, iut_role):
    """A peer CPR during a pending channel map update is a different collision.

    Purpose: Verify that, for either role, a peer Connection Parameter Request
    received while an Instant-bearing channel map update is pending is rejected as
    a different-transaction collision and leaves that pending update intact (5.3).
    Runs for both roles (Central, Peripheral).

    Steps:
        1. The IUT has an InstantUpdate (channel map) pending.
        2. Lower Tester sends LL_CONNECTION_PARAM_REQ.

    Expected:
        - The IUT answers LL_REJECT_EXT_IND (DIFFERENT) for CONNECTION_PARAM_REQ.
        - The pending InstantUpdate is unchanged and no remote parameters stored.
    """
    dut = request.getfixturevalue(iut_role)
    update = InstantUpdate(20, channel_map=3)
    dut.controls.pending = update
    dut.lt_tx(connection_param_req(30))
    dut.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(Opcode.LL_CONNECTION_PARAM_REQ, DIFFERENT),
    )
    assert dut.controls.pending is update
    assert dut.controls.connection_parameters.remote_parameters is None


def test_cpr_loc_rejected_keeps_pending_channel_map(peripheral):
    """Rejecting the local CPR does not discard a concurrently pending map update.

    Purpose: Verify that when the Peripheral's CPR is rejected by the Central, a
    channel map update sent at an Instant in the same exchange still applies, so
    the two procedures are tracked independently (5.3).

    Steps:
        1. Upper Tester starts a Peripheral CPR (interval_min 24).
        2. Lower Tester receives LL_CONNECTION_PARAM_REQ and acknowledges it.
        3. Lower Tester sends LL_CHANNEL_MAP_IND at an Instant.
        4. Lower Tester rejects the CPR (DIFFERENT_TRANSACTION_COLLISION).
        5. The Peripheral waits for the Instant.

    Expected:
        - Host gets LE Connection Update Complete (DIFFERENT) for the CPR.
        - The channel map update applies (channel_map == 0x03) and the link stays
          open.
    """
    request_parameters(peripheral, 24)
    peripheral.lt_rx(Opcode.LL_CONNECTION_PARAM_REQ)
    peripheral.tx_ack()
    instant = peripheral.event_counter + 6
    peripheral.lt_tx(channel_map_ind(0x03, instant))
    peripheral.lt_tx(reject_ext_ind(Opcode.LL_CONNECTION_PARAM_REQ, DIFFERENT))
    peripheral.ut_rx(UPDATE_DONE, status=DIFFERENT)
    peripheral.wait_instant(instant)
    peripheral.event_prepare()
    assert peripheral.connection.parameters.channel_map == 0x03
    assert not peripheral.connection.closed


def test_central_host_channel_classification_while_busy(central):
    """Host channel classification is disallowed while a CPR is in flight.

    Purpose: Verify that a Host Set Host Channel Classification is refused with
    Command Disallowed while the Central already has a Connection Parameter
    Request outstanding (5.3).

    Steps:
        1. Upper Tester starts a Central CPR (interval_min 30).
        2. Upper Tester calls classify_channels while that CPR is in flight.
        3. Lower Tester receives the outstanding LL_CONNECTION_PARAM_REQ.

    Expected:
        - classify_channels returns COMMAND_DISALLOWED.
        - The CPR is still the outstanding PDU on air.
    """
    request_parameters(central, 30)
    service = central.controller.le_procedures.ll
    assert service.classify_channels(0x0F) == ErrorCode.COMMAND_DISALLOWED_ERROR
    central.lt_rx(Opcode.LL_CONNECTION_PARAM_REQ)


# ---------------------------------------------------------------------------
# Power Control x Instant procedures
# ---------------------------------------------------------------------------
def _start_power_read(dut):
    dut.host_start('prepare_read_remote_transmit_power', 1)
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_POWER_CONTROL_REQ)


def _complete_power_read(dut):
    dut.lt_tx(pdu(Opcode.LL_POWER_CONTROL_RSP, 0, 0, 0xFC, 0xFF))
    dut.ut_rx(
        hci.HCI_LE_Transmit_Power_Reporting_Event,
        status=ErrorCode.SUCCESS,
        reason=2,
        tx_power_level=-4,
    )


@pytest.mark.parametrize('iut_role', ['central', 'peripheral'])
@pytest.mark.parametrize('procedure', ['phy', 'parameters'])
def test_peer_instant_procedure_can_overlap_a_local_power_read(
    request, iut_role, procedure
):
    """A peer Instant procedure can start while Power Control awaits a response.

    Purpose: Verify compatible peer procedures can run concurrently (Vol 6,
    Part B, 5.3).

    Steps:
        1. Start a local read and receive its LL_POWER_CONTROL_REQ.
        2. Send a peer PHY or Connection Parameter Request.
        3. Answer the outstanding Power Control Request.

    Expected:
        - A peer PHY request receives the appropriate PHY response for the role.
        - Peer connection parameters reach the Host.
        - The local read completes with the peer's reported power.
    """
    dut = request.getfixturevalue(iut_role)
    _start_power_read(dut)
    if procedure == 'phy':
        dut.lt_tx(phy_req(PHY_2M, PHY_2M))
        dut.lt_rx(
            Opcode.LL_PHY_UPDATE_IND if iut_role == 'central' else Opcode.LL_PHY_RSP
        )
    else:
        dut.lt_tx(connection_param_req(30))
        dut.ut_rx(REMOTE_REQUEST, interval_min=30)
    _complete_power_read(dut)
    dut.ut_rx_q_is_empty()


# ---------------------------------------------------------------------------
# Host requests queued behind another local procedure
# ---------------------------------------------------------------------------
def _length_request(dut, octets=251, time_us=2120):
    assert dut.host('set_data_length', octets, time_us) == ErrorCode.SUCCESS


def test_phy_request_waits_for_local_data_length(central):
    """A Host PHY request during a local Data Length Update is queued.

    Purpose: Verify that LE Set PHY issued while the Controller's own Data
    Length Update is outstanding is accepted and started once that procedure
    completes. HCI defines no Command Disallowed for this case (Vol 4, Part E,
    7.8.49) and the Link Layer runs one local procedure at a time (Vol 6,
    Part B, 5).

    Steps:
        1. Upper Tester calls set_data_length; Lower Tester receives LENGTH_REQ.
        2. Upper Tester starts a PHY Update while LENGTH_REQ is unanswered.
        3. Lower Tester answers LENGTH_RSP.
        4. Run the next connection event.

    Expected:
        - The PHY request is admitted with Success but sends nothing yet.
        - PHY_REQ is sent only in the event after LENGTH_RSP completes the
          Data Length Update.
    """
    _length_request(central)
    central.event_prepare()
    central.lt_rx(Opcode.LL_LENGTH_REQ)
    central.host_start('prepare_phy', PHY_2M, PHY_2M, 0, 0)
    assert not central.controls.phy.phy_request
    central.lt_tx(length(Opcode.LL_LENGTH_RSP, 251, 2120, 251, 2120))
    central.event_done()

    central.event_prepare()
    central.lt_rx(Opcode.LL_PHY_REQ, phy_req(PHY_2M, PHY_2M))


def test_host_phy_request_waits_for_local_power_read(central):
    """A second locally initiated procedure waits for Power Control to complete."""
    _start_power_read(central)
    central.host_start('prepare_phy', PHY_2M, PHY_2M, 0, 0)
    central.tx_ack()
    central.event_done()

    central.event_prepare()
    central.lt_rx_q_is_empty()
    _complete_power_read(central)
    central.event_done()

    central.event_prepare()
    central.lt_rx(Opcode.LL_PHY_REQ, phy_req(PHY_2M, PHY_2M))


@pytest.mark.parametrize('kind', ['phy', 'parameters', 'data_length'])
def test_queued_request_of_same_kind_is_disallowed(central, kind):
    """A second request of a queued procedure kind is still refused.

    Purpose: Verify that queueing only covers a different busy procedure: a
    second Host request of the same kind, running or queued, keeps returning
    Command Disallowed so the Host cannot stack duplicates.

    Steps:
        1. Start a local Data Length Update (or PHY Update for data_length).
        2. Queue one request of the parametrized kind.
        3. Issue a second request of the same kind.

    Expected:
        - The first queued request succeeds; the duplicate returns Command
          Disallowed.
    """
    if kind == 'data_length':
        central.host_start('prepare_phy', PHY_2M, PHY_2M, 0, 0)
        _length_request(central)
        assert (
            central.host('set_data_length', 251, 2120)
            == ErrorCode.COMMAND_DISALLOWED_ERROR
        )
        return
    _length_request(central)
    if kind == 'phy':
        central.host_start('prepare_phy', PHY_2M, PHY_2M, 0, 0)
        central.host_start(
            'prepare_phy',
            PHY_CODED,
            PHY_CODED,
            0,
            0,
            status=ErrorCode.COMMAND_DISALLOWED_ERROR,
        )
    else:
        request_parameters(central, 30)
        central.host_start(
            'prepare_parameters',
            40,
            40,
            0,
            100,
            valid_ce_lengths=True,
            status=ErrorCode.COMMAND_DISALLOWED_ERROR,
        )


def test_queued_request_is_dropped_on_disconnect(central):
    """A queued Host request does not outlive its connection.

    Purpose: Verify that closing the link discards queued procedures, so no
    PHY_REQ is built for a closed connection.

    Steps:
        1. Start a local Data Length Update and queue a PHY Update behind it.
        2. Close the LLCP coordinator.

    Expected:
        - The deferred queue is empty after close.
    """
    _length_request(central)
    central.host_start('prepare_phy', PHY_2M, PHY_2M, 0, 0)
    assert central.connection.llcp.deferred
    central.connection.llcp.close(ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR)
    assert not central.connection.llcp.deferred
