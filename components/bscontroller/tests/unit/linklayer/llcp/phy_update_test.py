# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""PHY Update procedure (Core Vol 6, Part B, 5.1.10).

Matrix: {Central, Peripheral} x {local, remote} x {changed, no change, rejected,
unknown, invalid, timeout, disconnect, instant}. Scenarios drive the LLCP
harness (llcp/harness.py); collisions with other procedures are in
collision_test.py.
"""

import pytest

from bscontroller.linklayer import LL_RESPONSE_TIMEOUT_US
from bscontroller.linklayer import phy as phy_procedure
from bscontroller.radio.base import PHY_CODED_S8_FACTOR, Phy
from tests.unit.linklayer.llcp.harness import (
    CENTRAL,
    PERIPHERAL,
    PHY_1M,
    PHY_2M,
    PHY_CODED,
    PROCEDURE_COLLISION,
    ErrorCode,
    Opcode,
    feature,
    hci,
    instant_of,
    length,
    phy_req,
    phy_rsp,
    phy_update_ind,
    reject_ext_ind,
    reject_ind,
    unknown_rsp,
)

PHY_DONE = hci.HCI_LE_PHY_Update_Complete_Event
ALL_PHYS = PHY_1M | PHY_2M | PHY_CODED


def assert_phys(iut, tx, rx):
    assert (iut.connection.tx_phy, iut.connection.rx_phy) == (tx, rx)


def assert_idle(iut):
    assert not iut.controls.busy and not iut.controls.phy.phy_request
    assert iut.controls.next_deadline_us == 0
    iut.lt_rx_q_is_empty()


# ---------------------------------------------------------------------------
# Central, local (Host-initiated)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'host,peer,ind,result',
    [
        ((PHY_2M, PHY_2M), (PHY_2M, PHY_2M), (PHY_2M, PHY_2M), (Phy.LE_2M, Phy.LE_2M)),
        (
            (PHY_CODED, PHY_CODED),
            (ALL_PHYS, ALL_PHYS),
            (PHY_CODED, PHY_CODED),
            (Phy.LE_CODED, Phy.LE_CODED),
        ),
        # Asymmetric: Central->Peripheral 2M, Peripheral->Central stays on 1M.
        (
            (PHY_2M, PHY_1M),
            (PHY_1M | PHY_2M, PHY_2M),
            (PHY_2M, 0),
            (Phy.LE_2M, Phy.LE_1M),
        ),
    ],
    ids=['2m', 'coded', 'asymmetric'],
)
def test_central_loc(central, host, peer, ind, result):
    """A Host-initiated Central PHY Update applies the negotiated PHYs.

    Purpose: Verify that the Central sends PHY_REQ with the Host preferences,
    chooses the PHYs from the intersection with the peer's PHY_RSP, applies them
    at the Instant and reports completion (Vol 6, Part B, 5.1.10), for the 2M,
    coded and asymmetric parameter sets.

    Steps:
        1. Upper Tester starts a PHY Update with its preferences.
        2. Lower Tester receives PHY_REQ and answers with PHY_RSP.
        3. Lower Tester receives PHY_UPDATE_IND and acknowledges it.
        4. Idle events run up to the Instant, where the PHYs change.

    Expected:
        - UPDATE_IND carries the expected per-direction PHY fields.
        - The response timer stops when UPDATE_IND is sent.
        - Host gets PHY Update Complete (SUCCESS) with the resulting PHYs, plus a
          Data Length Change when a coded PHY raises the effective times.
        - The procedure is idle afterwards.
    """
    central.host_start('prepare_phy', *host, PHY_CODED_S8_FACTOR)
    central.event_prepare()
    central.lt_rx(Opcode.LL_PHY_REQ, phy_req(*host))
    central.lt_tx(phy_rsp(*peer))
    central.event_done()

    central.event_prepare()
    update = central.lt_rx(Opcode.LL_PHY_UPDATE_IND)
    assert update[1:3] == bytes(ind)
    central.tx_ack()
    # The response timer stops when UPDATE_IND is sent (5.1.10, 5.2).
    assert central.controls.next_deadline_us == 0
    central.event_done()

    central.wait_instant(instant_of(update))
    assert_phys(central, Phy.LE_1M, Phy.LE_1M)
    central.event_prepare()
    assert_phys(central, *result)
    central.ut_rx(PHY_DONE, status=0, tx_phy=result[0], rx_phy=result[1])
    if Phy.LE_CODED in result:
        # Coded packet times raise connEffectiveMax*Time (4.5.10).
        central.ut_rx(
            hci.HCI_LE_Data_Length_Change_Event, max_tx_time=2704, max_rx_time=2704
        )
    central.ut_rx_q_is_empty()
    assert_idle(central)


def test_phy_update_at_event_counter_wrap(central):
    """Apply the changed PHYs when the six-event lead wraps to Instant zero."""
    central.connection.event_counter = 0xFFFA
    central.host_start('prepare_phy', PHY_2M, PHY_2M, 0)
    central.lt_rx(Opcode.LL_PHY_REQ)
    central.lt_tx(phy_rsp(PHY_2M, PHY_2M))
    update = central.lt_rx(Opcode.LL_PHY_UPDATE_IND, phy_update_ind(PHY_2M, PHY_2M, 0))
    central.lt_rx(Opcode.LL_PHY_UPDATE_IND, update)
    central.tx_ack()
    central.wait_instant(0)
    assert_phys(central, Phy.LE_1M, Phy.LE_1M)
    central.event_prepare()
    assert_phys(central, Phy.LE_2M, Phy.LE_2M)


def test_central_loc_no_actual_change(central):
    """A Central PHY Update with no change completes on transmission.

    Purpose: Verify that when the negotiation keeps the current 1M PHY, the
    Central sends UPDATE_IND with zero PHY fields and zero Instant and reports
    the Host-initiated completion at transmission, before the acknowledgement.

    Steps:
        1. Upper Tester starts a PHY Update preferring 1M.
        2. Lower Tester receives PHY_REQ and answers PHY_RSP accepting all PHYs.
        3. Lower Tester receives UPDATE_IND (0, 0, 0) and then acknowledges.

    Expected:
        - UPDATE_IND carries zero PHY fields and a zero Instant.
        - Host gets PHY Update Complete (SUCCESS, 1M/1M) before the ACK.
        - The procedure is idle and nothing else reaches the Host.
    """
    central.host_start('prepare_phy', PHY_1M, PHY_1M, 0)
    central.event_prepare()
    central.lt_rx(Opcode.LL_PHY_REQ)
    central.lt_tx(phy_rsp(ALL_PHYS, ALL_PHYS))
    central.event_done()
    central.event_prepare()
    # No change: both PHY fields and the Instant are zero.
    central.lt_rx(Opcode.LL_PHY_UPDATE_IND, phy_update_ind(0, 0, 0))
    # Host-initiated: complete on transmission, before the ACK.
    central.ut_rx(PHY_DONE, status=0, tx_phy=Phy.LE_1M, rx_phy=Phy.LE_1M)
    central.tx_ack()
    central.event_done()
    central.ut_rx_q_is_empty()
    assert_idle(central)


@pytest.mark.parametrize(
    'response,status',
    [
        (unknown_rsp(Opcode.LL_PHY_REQ), ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR),
        (
            reject_ext_ind(Opcode.LL_PHY_REQ, PROCEDURE_COLLISION),
            PROCEDURE_COLLISION,
        ),
        (
            reject_ext_ind(
                Opcode.LL_PHY_REQ, ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR
            ),
            ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR,
        ),
    ],
    ids=['unknown', 'reject-collision', 'reject-unsupported'],
)
@pytest.mark.parametrize('role', [CENTRAL, PERIPHERAL])
def test_loc_peer_refuses(iut, role, response, status):
    """A refused local PHY Update completes with the refusal's status and no PHY
    change.

    Purpose: Verify that a peer refusal of PHY_REQ completes the Host-initiated
    procedure with the matching status, keeps the PHYs on 1M and leaves no stale
    response timer, for each refusal kind (unknown, reject-collision,
    reject-unsupported) and both roles.

    Steps:
        1. Upper Tester starts a PHY Update preferring 2M.
        2. Lower Tester receives PHY_REQ and replies with the refusal PDU.
        3. Advance far past the response timeout.

    Expected:
        - Host gets PHY Update Complete with the expected status, PHYs 1M/1M.
        - The procedure is idle; no stale timer later closes the link.
    """
    dut = iut(role)
    dut.host_start('prepare_phy', PHY_2M, PHY_2M, 0)
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_PHY_REQ)
    dut.lt_tx(response)
    dut.event_done()
    dut.ut_rx(PHY_DONE, status=status, tx_phy=Phy.LE_1M, rx_phy=Phy.LE_1M)
    dut.ut_rx_q_is_empty()
    assert_idle(dut)
    # No stale response timer remains to close the link later.
    assert not dut.elapse(10 * LL_RESPONSE_TIMEOUT_US)
    assert not dut.connection.closed


def test_loc_legacy_reject_does_not_end_phy_request(central):
    """A legacy LL_REJECT_IND is not a valid answer to PHY_REQ.

    Purpose: Verify that LL_REJECT_IND, which carries no opcode, is not accepted
    as a PHY_REQ response (Vol 6, Part B, 2.4.2.14); the PHY request stays
    outstanding and no completion is reported.

    Steps:
        1. Upper Tester starts a PHY Update preferring 2M.
        2. Lower Tester receives PHY_REQ and replies with LL_REJECT_IND.

    Expected:
        - No Host notification is produced.
        - The PHY request is still pending.
    """
    # LL_REJECT_IND carries no opcode; it is not a valid PHY_REQ answer (2.4.2.14).
    central.host_start('prepare_phy', PHY_2M, PHY_2M, 0)
    central.event_prepare()
    central.lt_rx(Opcode.LL_PHY_REQ)
    central.lt_tx(reject_ind(PROCEDURE_COLLISION))
    central.event_done()
    central.ut_rx_q_is_empty()
    assert central.controls.phy.phy_request


@pytest.mark.parametrize('role', [CENTRAL, PERIPHERAL])
def test_loc_invalid_response_preferences(iut, role):
    """Zero preferences in the peer's answer are rejected, request kept.

    Purpose: Verify that a PHY_RSP (or PHY_REQ, by role) with a zero preference
    field is invalid (Vol 6, Part B, 2.4.2.22); the IUT answers REJECT_EXT_IND
    with invalid-parameters and keeps its own request outstanding, for both
    roles.

    Steps:
        1. Upper Tester starts a PHY Update preferring 2M.
        2. Lower Tester receives PHY_REQ.
        3. Lower Tester sends the role-appropriate answer with a zero TX field.
        4. Lower Tester receives the IUT's rejection and acknowledges.

    Expected:
        - The IUT transmits REJECT_EXT_IND(answer opcode, invalid parameters).
        - The PHY request is still pending; no Host notification is produced.
    """
    dut = iut(role)
    dut.host_start('prepare_phy', PHY_2M, PHY_2M, 0)
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_PHY_REQ)
    # Zero preferences are invalid in either direction (2.4.2.22).
    answer = phy_rsp(0, PHY_2M) if role == CENTRAL else phy_req(0, PHY_2M)
    dut.lt_tx(answer)
    dut.event_done()
    dut.event_prepare()
    dut.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(answer[0], ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR),
    )
    dut.tx_ack()
    dut.event_done()
    assert dut.controls.phy.phy_request
    dut.ut_rx_q_is_empty()


@pytest.mark.parametrize('role', [CENTRAL, PERIPHERAL])
def test_loc_response_timeout(iut, role):
    """A PHY Update response timeout disconnects the link.

    Purpose: Verify that when the peer acknowledges PHY_REQ but never answers,
    the procedure timer expiry closes the connection with the response-timeout
    reason, for both roles.

    Steps:
        1. Upper Tester starts a PHY Update preferring 2M.
        2. Lower Tester acknowledges PHY_REQ but sends no response.
        3. Advance to just before, then past, the procedure deadline.

    Expected:
        - No timeout fires one microsecond early; it fires at the deadline.
        - Host gets Disconnection Complete (response timeout); nothing else.
    """
    dut = iut(role)
    dut.host_start('prepare_phy', PHY_2M, PHY_2M, 0)
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_PHY_REQ)
    dut.tx_ack()
    dut.event_done()
    assert not dut.expire_response_timer(early_us=1)
    assert dut.elapse(1)
    dut.ut_rx(
        hci.HCI_Disconnection_Complete_Event,
        reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR,
    )
    dut.ut_rx_q_is_empty()


@pytest.mark.parametrize(
    'peer_request,response',
    [
        (feature(Opcode.LL_PERIPHERAL_FEATURE_REQ, 1), Opcode.LL_FEATURE_RSP),
        (length(Opcode.LL_LENGTH_REQ, 27, 328, 27, 328), Opcode.LL_LENGTH_RSP),
    ],
    ids=['feature', 'length'],
)
def test_peer_request_restarts_phy_response_timer(central, peer_request, response):
    """A queued response renews the running PHY procedure timer (Core 6.B.5.2).

    Steps:
        1. Start a PHY Update. While waiting for PHY_RSP, receive a peer request.
        2. Transmit the response twice without a link-layer acknowledgment.
        3. Keep the ACL alive through the old deadline and let the new one expire.

    Expected:
        - The 40 seconds run from queuing the response, including both transmissions.
        - Only the new deadline disconnects the ACL with LL Response Timeout.
    """
    central.host_start('prepare_phy', PHY_2M, PHY_2M, 0)
    central.lt_rx(Opcode.LL_PHY_REQ)
    old_deadline = central.controls.next_deadline_us
    central.elapse(1_000_000)
    central.lt_tx(peer_request)
    deadline = central.radio.now_us + LL_RESPONSE_TIMEOUT_US
    packet = central.lt_rx(response)
    central.lt_rx(response, packet)
    assert not central.elapse(old_deadline - central.radio.now_us)
    assert not central.elapse(deadline - 1 - central.radio.now_us)
    assert central.elapse(1)
    central.ut_rx(
        hci.HCI_Disconnection_Complete_Event,
        reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR,
    )


@pytest.mark.parametrize('role', [CENTRAL, PERIPHERAL])
def test_loc_disconnect_during_procedure(iut, role):
    """A peer TERMINATE_IND during a PHY Update disconnects the link.

    Purpose: Verify that receiving LL_TERMINATE_IND while a PHY Update is
    outstanding tears the link down with the remote-user-terminated reason and
    clears the procedure timer, for both roles.

    Steps:
        1. Upper Tester starts a PHY Update preferring 2M.
        2. Lower Tester receives PHY_REQ, then sends LL_TERMINATE_IND.
        3. The IUT sends its final Empty PDU acknowledging the termination.

    Expected:
        - Host gets Disconnection Complete (remote user terminated); nothing else.
        - The procedure deadline is cleared.
    """
    dut = iut(role)
    dut.host_start('prepare_phy', PHY_2M, PHY_2M, 0)
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_PHY_REQ)
    dut.lt_tx(pdu_terminate())
    dut.lt_rx_empty()
    dut.ut_rx(
        hci.HCI_Disconnection_Complete_Event,
        reason=ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR,
    )
    dut.ut_rx_q_is_empty()
    assert dut.controls.next_deadline_us == 0


def pdu_terminate():
    return bytes(
        [Opcode.LL_TERMINATE_IND, ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR]
    )


def test_host_admission(central):
    """Host PHY Update admission validates arguments and rejects a second start.

    Purpose: Verify that the Host entry point rejects unsupported PHY masks,
    admits a valid request, and disallows a concurrent second request while one
    is outstanding.

    Steps:
        1. Start a PHY Update with an unsupported PHY mask.
        2. Start a valid PHY Update.
        3. Start another PHY Update while the first is still outstanding.

    Expected:
        - The first start returns Unsupported Feature Or Parameter Value.
        - The third start returns Command Disallowed.
        - Only one request PDU is queued.
    """
    central.hci(hci.HCI_LE_Set_PHY_Command(central.handle, 0, 0x80, PHY_2M, 0))
    central.ut_rx(
        hci.HCI_Command_Status_Event,
        status=ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
    )
    central.host_start('prepare_phy', PHY_2M, PHY_2M, 0)
    # A second Host request while one is outstanding is disallowed.
    central.host_start(
        'prepare_phy', PHY_2M, PHY_2M, 0, status=ErrorCode.COMMAND_DISALLOWED_ERROR
    )
    assert len(central.connection.event.queue) == 1


# ---------------------------------------------------------------------------
# Central, remote (Peripheral-initiated)
# ---------------------------------------------------------------------------
def test_central_rem(central):
    """A peer PHY_REQ drives the Central straight to UPDATE_IND.

    Purpose: Verify that a Central never answers a peer PHY_REQ with PHY_RSP but
    issues UPDATE_IND directly, applies the chosen PHYs at the Instant and, when
    a PHY changes, reports completion to the Host (Vol 6, Part B, 5.1.10).

    Steps:
        1. Lower Tester sends PHY_REQ preferring 2M.
        2. Lower Tester receives the Central's UPDATE_IND and acknowledges.
        3. Idle events run up to the Instant, where the PHYs change.

    Expected:
        - UPDATE_IND carries 2M for both directions; no PHY_RSP is sent.
        - PHYs become 2M/2M at the Instant.
        - Host gets PHY Update Complete (SUCCESS, 2M/2M); the procedure is idle.
    """
    central.event_prepare()
    central.lt_tx(phy_req(PHY_2M, PHY_2M))
    central.event_done()
    # The Central never answers PHY_REQ with PHY_RSP (5.1.10).
    central.event_prepare()
    update = central.lt_rx(Opcode.LL_PHY_UPDATE_IND)
    assert update[1:3] == bytes([PHY_2M, PHY_2M])
    central.tx_ack()
    central.event_done()
    central.wait_instant(instant_of(update))
    central.event_prepare()
    assert_phys(central, Phy.LE_2M, Phy.LE_2M)
    # A remote procedure that changes a PHY is reported to the Host.
    central.ut_rx(PHY_DONE, status=0, tx_phy=Phy.LE_2M, rx_phy=Phy.LE_2M)
    central.ut_rx_q_is_empty()
    assert_idle(central)


def test_central_rem_no_actual_change(central):
    """A peer PHY_REQ that changes nothing produces no Host notification.

    Purpose: Verify that when a peer-initiated PHY Update keeps the current 1M
    PHY and the Host never asked, the Central sends UPDATE_IND (0, 0, 0) and does
    not notify the Host.

    Steps:
        1. Lower Tester sends PHY_REQ preferring 1M.
        2. Lower Tester receives UPDATE_IND (0, 0, 0) and acknowledges.

    Expected:
        - UPDATE_IND carries zero PHY fields and a zero Instant.
        - No Host notification is produced; the procedure is idle.
    """
    central.event_prepare()
    central.lt_tx(phy_req(PHY_1M, PHY_1M))
    central.event_done()
    central.event_prepare()
    central.lt_rx(Opcode.LL_PHY_UPDATE_IND, phy_update_ind(0, 0, 0))
    central.tx_ack()
    central.event_done()
    # Nothing changed and the Host did not ask: no notification.
    central.ut_rx_q_is_empty()
    assert_idle(central)


def test_central_rem_honours_default_preferences(central):
    """A Central uses the Host's default PHY preferences when answering a peer.

    Purpose: Verify that when the link's default TX/RX preference is 1M, a fresh
    Central answers a peer PHY_REQ for 2M with UPDATE_IND (0, 0, 0), keeping 1M.

    Steps:
        1. Set the link default TX and RX preferences to 1M.
        2. Build a fresh Central connection and feed it a PHY_REQ for 2M.

    Expected:
        - The queued UPDATE_IND carries zero PHY fields and a zero Instant.
    """
    central.link.default_tx_phys = central.link.default_rx_phys = PHY_1M
    fresh = central.connection.__class__(
        central.link, central.connection.parameters, central.connection.peer, CENTRAL, 0
    )
    fresh.control(phy_req(PHY_2M, PHY_2M))
    assert fresh.event.queue[-1].payload == phy_update_ind(0, 0, 0)


@pytest.mark.parametrize(
    'peer_tx,peer_rx,expected',
    [
        # RFU bits in a preference are ignored, not rejected.
        # The Central keeps its current PHY whenever the intersection allows it.
        (0xFF, 0xFF, (0, 0)),
        (0x82, 0x82, (PHY_2M, PHY_2M)),
        # A single identical preference forces a symmetric choice.
        (PHY_CODED, PHY_CODED, (PHY_CODED, PHY_CODED)),
        (PHY_2M, PHY_1M | PHY_2M, (0, PHY_2M)),
        (PHY_1M, PHY_1M, (0, 0)),
    ],
)
def test_central_rem_selection(central, peer_tx, peer_rx, expected):
    """The Central's UPDATE_IND PHY fields follow its selection rules.

    Purpose: Verify that the Central ignores RFU bits, keeps its current PHY
    whenever the intersection allows, and makes a symmetric choice for a single
    shared preference, across several peer PHY_REQ preference pairs.

    Steps:
        1. Lower Tester sends PHY_REQ with the parametrized preferences.
        2. Lower Tester receives the Central's UPDATE_IND.

    Expected:
        - UPDATE_IND's two PHY fields equal the expected pair.
    """
    central.event_prepare()
    central.lt_tx(phy_req(peer_tx, peer_rx))
    central.event_done()
    central.event_prepare()
    update = central.lt_rx(Opcode.LL_PHY_UPDATE_IND)
    assert update[1:3] == bytes(expected)


@pytest.mark.parametrize('role', [CENTRAL, PERIPHERAL])
@pytest.mark.parametrize('prefs', [(0, PHY_2M), (PHY_2M, 0)])
def test_rem_invalid_preferences(iut, role, prefs):
    """A peer PHY_REQ with a zero preference field is rejected.

    Purpose: Verify that a peer-initiated PHY_REQ carrying a zero TX or RX
    preference is answered with REJECT_EXT_IND (invalid parameters) and leaves
    the procedure idle, for both roles and both zero-field placements.

    Steps:
        1. Lower Tester sends PHY_REQ with a zero preference field.
        2. Lower Tester receives the IUT's rejection and acknowledges.

    Expected:
        - The IUT transmits REJECT_EXT_IND(LL_PHY_REQ, invalid parameters).
        - No Host notification is produced; the procedure is idle.
    """
    dut = iut(role)
    dut.event_prepare()
    dut.lt_tx(phy_req(*prefs))
    dut.event_done()
    dut.event_prepare()
    dut.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(Opcode.LL_PHY_REQ, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR),
    )
    dut.tx_ack()
    dut.event_done()
    dut.ut_rx_q_is_empty()
    assert_idle(dut)


def test_central_rem_update_must_be_acked_by_instant(central):
    """An unacknowledged UPDATE_IND at its Instant closes the link.

    Purpose: Verify that if the peer never acknowledges the Central's UPDATE_IND
    before the Instant event, the connection closes with the instant-passed
    reason.

    Steps:
        1. Lower Tester sends PHY_REQ preferring 2M.
        2. Lower Tester receives UPDATE_IND but never acknowledges it.
        3. Advance the event counter to the Instant and prepare that event.

    Expected:
        - The connection is closed.
        - Host gets Disconnection Complete (instant passed).
    """
    central.event_prepare()
    central.lt_tx(phy_req(PHY_2M, PHY_2M))
    central.event_done()
    central.event_prepare()
    update = central.lt_rx(Opcode.LL_PHY_UPDATE_IND)
    # The peer never acknowledges; the Instant arrives with UPDATE_IND unacked.
    central.event_done()
    central.connection.event_counter = instant_of(update)
    central.event_prepare()
    assert central.connection.closed
    central.ut_rx(
        hci.HCI_Disconnection_Complete_Event, reason=ErrorCode.INSTANT_PASSED_ERROR
    )


# ---------------------------------------------------------------------------
# Peripheral, local
# ---------------------------------------------------------------------------
def test_periph_loc(peripheral):
    """A Host-initiated Peripheral PHY Update applies the Central's UPDATE_IND.

    Purpose: Verify that a Peripheral sends PHY_REQ for the Host preferences and
    applies the Central's UPDATE_IND PHYs at the Instant, reporting completion
    (Vol 6, Part B, 5.1.10).

    Steps:
        1. Upper Tester starts a PHY Update preferring 2M.
        2. Lower Tester receives PHY_REQ and acknowledges it.
        3. Lower Tester sends UPDATE_IND with an Instant six events ahead.
        4. Idle events run up to the Instant, where the PHYs change.

    Expected:
        - The response timer stops when UPDATE_IND is received.
        - PHYs become 2M/2M at the Instant.
        - Host gets PHY Update Complete (SUCCESS, 2M/2M); the procedure is idle.
    """
    peripheral.host_start('prepare_phy', PHY_2M, PHY_2M, 0)
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_PHY_REQ, phy_req(PHY_2M, PHY_2M))
    peripheral.tx_ack()
    peripheral.event_done()
    instant = peripheral.event_counter + 6
    peripheral.event_prepare()
    peripheral.lt_tx(phy_update_ind(PHY_2M, PHY_2M, instant))
    assert peripheral.controls.next_deadline_us == 0
    peripheral.event_done()
    peripheral.wait_instant(instant)
    assert_phys(peripheral, Phy.LE_1M, Phy.LE_1M)
    peripheral.event_prepare()
    assert_phys(peripheral, Phy.LE_2M, Phy.LE_2M)
    peripheral.ut_rx(PHY_DONE, status=0, tx_phy=Phy.LE_2M, rx_phy=Phy.LE_2M)
    peripheral.ut_rx_q_is_empty()
    assert_idle(peripheral)


def test_periph_loc_asymmetric_wire_fields_are_role_relative(peripheral):
    """A Peripheral maps the UPDATE_IND C_TO_P/P_TO_C fields to its own TX/RX.

    Purpose: Verify that the wire fields of UPDATE_IND are interpreted relative
    to the Peripheral's role: C_TO_P is its RX PHY and P_TO_C is its TX PHY, so an
    asymmetric update sets TX 2M and RX coded.

    Steps:
        1. Upper Tester starts a PHY Update allowing all PHYs.
        2. Lower Tester receives PHY_REQ.
        3. Lower Tester sends UPDATE_IND with C_TO_P=coded, P_TO_C=2M.
        4. Idle events run up to the Instant.

    Expected:
        - PHYs become TX 2M, RX coded at the Instant.
        - Host gets PHY Update Complete with TX 2M, RX coded.
    """
    peripheral.host_start('prepare_phy', ALL_PHYS, ALL_PHYS, 0)
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_PHY_REQ)
    instant = peripheral.event_counter + 6
    # C_TO_P = Coded, P_TO_C = 2M: this Peripheral transmits 2M, receives Coded.
    peripheral.lt_tx(phy_update_ind(PHY_CODED, PHY_2M, instant))
    peripheral.event_done()
    peripheral.wait_instant(instant)
    peripheral.event_prepare()
    assert_phys(peripheral, Phy.LE_2M, Phy.LE_CODED)
    peripheral.ut_rx(PHY_DONE, tx_phy=Phy.LE_2M, rx_phy=Phy.LE_CODED)


def test_periph_loc_no_actual_change(peripheral):
    """A Peripheral PHY Update with no change completes immediately.

    Purpose: Verify that when the Central answers with UPDATE_IND (0, 0, 0) the
    Host-initiated Peripheral procedure reports completion keeping 1M, with no
    Instant to wait for.

    Steps:
        1. Upper Tester starts a PHY Update preferring 1M.
        2. Lower Tester receives PHY_REQ.
        3. Lower Tester sends UPDATE_IND (0, 0, 0).

    Expected:
        - Host gets PHY Update Complete (SUCCESS, 1M/1M); nothing else.
        - The procedure is idle.
    """
    peripheral.host_start('prepare_phy', PHY_1M, PHY_1M, 0)
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_PHY_REQ)
    peripheral.lt_tx(phy_update_ind(0, 0, 0))
    peripheral.event_done()
    peripheral.ut_rx(PHY_DONE, status=0, tx_phy=Phy.LE_1M, rx_phy=Phy.LE_1M)
    peripheral.ut_rx_q_is_empty()
    assert_idle(peripheral)


@pytest.mark.parametrize(
    'host_initiated', [True, False], ids=['host-initiated', 'peer-initiated']
)
@pytest.mark.parametrize(
    'supported_phys,central_phy,peripheral_phy,expected',
    [
        (ALL_PHYS, PHY_1M | PHY_2M, PHY_2M, (Phy.LE_2M, Phy.LE_1M)),
        (ALL_PHYS, PHY_2M, PHY_2M | 0x80, (Phy.LE_1M, Phy.LE_2M)),
        (PHY_1M | PHY_2M, PHY_2M, PHY_CODED, (Phy.LE_1M, Phy.LE_2M)),
        (ALL_PHYS, PHY_1M, 0x80, (Phy.LE_1M, Phy.LE_1M)),
        (ALL_PHYS, 0x80, PHY_1M | PHY_2M, (Phy.LE_1M, Phy.LE_1M)),
    ],
    ids=['multiple-bits', 'reserved-and-valid-bits', 'unsupported', 'current', 'both'],
)
def test_periph_invalid_update_keeps_phy_in_affected_direction(
    peripheral,
    monkeypatch,
    host_initiated,
    supported_phys,
    central_phy,
    peripheral_phy,
    expected,
):
    """Invalid UPDATE_IND fields leave their directions unchanged (Core 6.B.5.1.10).

    Purpose: Verify directional handling of multiple bits, reserved bits and a
    defined PHY outside the Controller's supported mask.

    Steps:
        1. Start a PHY Update from the Host or the Lower Tester.
        2. Exchange the PHY request or response and send UPDATE_IND with an
           invalid field and a future Instant.
        3. If a PHY will change, advance to the Instant.

    Expected:
        - The connection stays open and each invalid direction keeps its PHY.
        - A valid change applies at the Instant and reports the resulting PHYs.
        - With no change, only a Host-initiated procedure reports completion.
    """
    monkeypatch.setattr(phy_procedure, 'CONTROLLER_SUPPORTED_PHY_MASK', supported_phys)
    if host_initiated:
        peripheral.host_start(
            'prepare_phy', supported_phys, supported_phys, PHY_CODED_S8_FACTOR
        )
        peripheral.event_prepare()
        peripheral.lt_rx(Opcode.LL_PHY_REQ)
    else:
        peripheral.event_prepare()
        peripheral.lt_tx(phy_req(supported_phys, supported_phys))
        peripheral.event_done()
        peripheral.event_prepare()
        peripheral.lt_rx(Opcode.LL_PHY_RSP)
    peripheral.tx_ack()
    peripheral.event_done()
    instant = peripheral.event_counter + 6
    peripheral.event_prepare()
    peripheral.lt_tx(phy_update_ind(central_phy, peripheral_phy, instant))
    assert not peripheral.connection.closed
    assert peripheral.controls.next_deadline_us == 0
    current = (Phy.LE_1M, Phy.LE_1M)
    assert_phys(peripheral, *current)
    if expected != current:
        peripheral.event_done()
        peripheral.wait_instant(instant)
        assert_phys(peripheral, *current)
        peripheral.event_prepare()
    assert_phys(peripheral, *expected)
    if expected != current or host_initiated:
        peripheral.ut_rx(PHY_DONE, status=0, tx_phy=expected[0], rx_phy=expected[1])
    peripheral.ut_rx_q_is_empty()
    assert_idle(peripheral)


@pytest.mark.parametrize('counter', [0, 0xFFFF])
def test_periph_update_at_current_instant_applies_immediately(peripheral, counter):
    """An UPDATE_IND whose Instant is the current event applies at once.

    Purpose: Verify that a Peripheral applies an UPDATE_IND whose Instant equals
    the current connection event immediately and reports completion, including
    the counter wrap values 0 and 0xFFFF.

    Steps:
        1. Set the event counter to the parametrized value.
        2. Upper Tester starts a PHY Update preferring 2M.
        3. Lower Tester receives PHY_REQ.
        4. Lower Tester sends UPDATE_IND with the Instant equal to the counter.

    Expected:
        - PHYs become 2M/2M immediately.
        - Host gets PHY Update Complete (SUCCESS); the procedure is idle.
        - The PHY procedure context is in the COMPLETE phase.
    """
    peripheral.connection.event_counter = counter
    peripheral.host_start('prepare_phy', PHY_2M, PHY_2M, 0)
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_PHY_REQ)
    peripheral.lt_tx(phy_update_ind(PHY_2M, PHY_2M, counter))
    assert_phys(peripheral, Phy.LE_2M, Phy.LE_2M)
    peripheral.ut_rx(PHY_DONE, status=0)
    assert_idle(peripheral)
    assert peripheral.controls.phy.context.phase.name == 'COMPLETE'


@pytest.mark.parametrize('distance', [-1, -100, 0x8000])
def test_periph_instant_in_the_past_closes_link(peripheral, distance):
    """An UPDATE_IND with an Instant in the past closes the link.

    Purpose: Verify that a Peripheral rejects an UPDATE_IND whose Instant has
    already passed (just behind, far behind, or across the wrap boundary),
    closing the connection with instant-passed.

    Steps:
        1. Set the event counter to 50.
        2. Lower Tester sends UPDATE_IND with an Instant offset by ``distance``.

    Expected:
        - Host gets Disconnection Complete (instant passed).
    """
    peripheral.connection.event_counter = 50
    peripheral.event_prepare()
    peripheral.lt_tx(phy_update_ind(PHY_2M, PHY_2M, (50 + distance) & 0xFFFF))
    peripheral.ut_rx(
        hci.HCI_Disconnection_Complete_Event, reason=ErrorCode.INSTANT_PASSED_ERROR
    )


def test_periph_update_ind_from_central_role_is_unknown(central):
    """A Central answers a received UPDATE_IND with UNKNOWN_RSP.

    Purpose: Verify that UPDATE_IND is only legal from a Central, so a Central
    IUT that receives one replies UNKNOWN_RSP and keeps the link open.

    Steps:
        1. Lower Tester sends UPDATE_IND to the Central.
        2. Lower Tester receives the Central's response.

    Expected:
        - The IUT transmits UNKNOWN_RSP(LL_PHY_UPDATE_IND).
        - The connection stays open.
    """
    # UPDATE_IND is only sent by a Central; a Central IUT answers UNKNOWN_RSP.
    central.event_prepare()
    central.lt_tx(phy_update_ind(PHY_2M, PHY_2M, 10))
    central.event_done()
    central.event_prepare()
    central.lt_rx(Opcode.LL_UNKNOWN_RSP, unknown_rsp(Opcode.LL_PHY_UPDATE_IND))
    assert not central.connection.closed


def test_phy_rsp_to_peripheral_is_unknown(peripheral):
    """A Peripheral answers an unexpected PHY_RSP with UNKNOWN_RSP.

    Purpose: Verify that a PHY_RSP received by a Peripheral with no outstanding
    request is illegal and answered with UNKNOWN_RSP.

    Steps:
        1. Lower Tester sends PHY_RSP to the Peripheral.
        2. Lower Tester receives the Peripheral's response.

    Expected:
        - The IUT transmits UNKNOWN_RSP(LL_PHY_RSP).
    """
    peripheral.event_prepare()
    peripheral.lt_tx(phy_rsp(PHY_2M, PHY_2M))
    peripheral.event_done()
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_UNKNOWN_RSP, unknown_rsp(Opcode.LL_PHY_RSP))


def test_central_unsolicited_phy_rsp_is_ignored(central):
    """A Central silently ignores an unsolicited PHY_RSP.

    Purpose: Verify that a Central that receives a PHY_RSP with no outstanding
    PHY_REQ ignores it: no reply, no Host event and no procedure started.

    Steps:
        1. Lower Tester sends PHY_RSP to the Central.
        2. Run two idle connection events.

    Expected:
        - No Host notification is produced.
        - No PHY procedure is busy.
    """
    central.event_prepare()
    central.lt_tx(phy_rsp(PHY_2M, PHY_2M))
    central.event_done()
    central.run_events(2)
    central.ut_rx_q_is_empty()
    assert not central.controls.busy


# ---------------------------------------------------------------------------
# Peripheral, remote
# ---------------------------------------------------------------------------
def test_periph_rem(peripheral):
    """A peer PHY_REQ to a Peripheral is answered PHY_RSP then applied.

    Purpose: Verify that a peer-initiated PHY Update on a Peripheral is answered
    with PHY_RSP, then the Central's UPDATE_IND is applied at the Instant and
    completion is reported (Vol 6, Part B, 5.1.10).

    Steps:
        1. Lower Tester sends PHY_REQ preferring 2M.
        2. Lower Tester receives the Peripheral's PHY_RSP and acknowledges.
        3. Lower Tester sends UPDATE_IND with an Instant six events ahead.
        4. Idle events run up to the Instant.

    Expected:
        - A response timer is armed after PHY_RSP and stops at UPDATE_IND.
        - PHYs become 2M/2M at the Instant.
        - Host gets PHY Update Complete (SUCCESS, 2M/2M); the procedure is idle.
    """
    peripheral.event_prepare()
    peripheral.lt_tx(phy_req(PHY_2M, PHY_2M))
    peripheral.event_done()
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_PHY_RSP, phy_rsp(ALL_PHYS, ALL_PHYS))
    peripheral.tx_ack()
    assert peripheral.controls.next_deadline_us
    peripheral.event_done()
    instant = peripheral.event_counter + 6
    peripheral.event_prepare()
    peripheral.lt_tx(phy_update_ind(PHY_2M, PHY_2M, instant))
    assert peripheral.controls.next_deadline_us == 0
    peripheral.event_done()
    peripheral.wait_instant(instant)
    peripheral.event_prepare()
    assert_phys(peripheral, Phy.LE_2M, Phy.LE_2M)
    peripheral.ut_rx(PHY_DONE, status=0, tx_phy=Phy.LE_2M, rx_phy=Phy.LE_2M)
    peripheral.ut_rx_q_is_empty()
    assert_idle(peripheral)


def test_periph_rem_no_actual_change(peripheral):
    """A peer PHY_REQ that changes nothing yields no Host notification.

    Purpose: Verify that a Peripheral answers a peer PHY_REQ with PHY_RSP, and
    when the Central's UPDATE_IND keeps the current 1M PHY the Host is not
    notified.

    Steps:
        1. Lower Tester sends PHY_REQ preferring 1M.
        2. Lower Tester receives the Peripheral's PHY_RSP.
        3. Lower Tester sends UPDATE_IND (0, 0, 0).

    Expected:
        - No Host notification is produced; the procedure is idle.
    """
    peripheral.event_prepare()
    peripheral.lt_tx(phy_req(PHY_1M, PHY_1M))
    peripheral.event_done()
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_PHY_RSP)
    peripheral.lt_tx(phy_update_ind(0, 0, 0))
    peripheral.event_done()
    peripheral.ut_rx_q_is_empty()
    assert_idle(peripheral)


def test_periph_rem_response_timeout_without_update(peripheral):
    """A missing UPDATE_IND after PHY_RSP times out and disconnects.

    Purpose: Verify that after a Peripheral answers a peer PHY_REQ with PHY_RSP,
    the Central failing to send UPDATE_IND expires the procedure timer and closes
    the link with the response-timeout reason.

    Steps:
        1. Lower Tester sends PHY_REQ preferring 2M.
        2. Lower Tester receives PHY_RSP and acknowledges.
        3. Expire the procedure timer with no UPDATE_IND sent.

    Expected:
        - Host gets Disconnection Complete (response timeout).
    """
    peripheral.event_prepare()
    peripheral.lt_tx(phy_req(PHY_2M, PHY_2M))
    peripheral.event_done()
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_PHY_RSP)
    peripheral.tx_ack()
    peripheral.event_done()
    assert peripheral.expire_response_timer()
    peripheral.ut_rx(
        hci.HCI_Disconnection_Complete_Event,
        reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR,
    )


@pytest.mark.parametrize('origin', ['loc', 'rem'])
def test_periph_unexpected_pdu_awaiting_instant(peripheral, origin):
    """A new PHY_REQ while awaiting the Instant is a same-procedure collision.

    Purpose: Verify that once an UPDATE_IND is pending its Instant, a further
    peer PHY_REQ is rejected as a procedure collision while the original update
    still completes at its Instant, for both a local and a remote origin of the
    pending update.

    Steps:
        1. Reach the pending-Instant state via a local or remote PHY Update.
        2. Lower Tester sends UPDATE_IND with an Instant six events ahead.
        3. Lower Tester sends a new PHY_REQ before the Instant.
        4. Lower Tester receives the IUT's rejection and acknowledges.
        5. Idle events run up to the original Instant.

    Expected:
        - The IUT transmits REJECT_EXT_IND(LL_PHY_REQ, procedure collision).
        - PHYs become 2M/2M at the original Instant.
        - Host gets PHY Update Complete (SUCCESS).
    """
    if origin == 'loc':
        peripheral.host_start('prepare_phy', PHY_2M, PHY_2M, 0)
        peripheral.event_prepare()
        peripheral.lt_rx(Opcode.LL_PHY_REQ)
    else:
        peripheral.event_prepare()
        peripheral.lt_tx(phy_req(PHY_2M, PHY_2M))
        peripheral.event_done()
        peripheral.event_prepare()
        peripheral.lt_rx(Opcode.LL_PHY_RSP)
    instant = peripheral.event_counter + 6
    peripheral.lt_tx(phy_update_ind(PHY_2M, PHY_2M, instant))
    peripheral.event_done()
    # A new PHY request while waiting for the Instant is a same-procedure collision.
    peripheral.event_prepare()
    peripheral.lt_tx(phy_req(PHY_1M, PHY_1M))
    peripheral.event_done()
    peripheral.event_prepare()
    peripheral.lt_rx(
        Opcode.LL_REJECT_EXT_IND, reject_ext_ind(Opcode.LL_PHY_REQ, PROCEDURE_COLLISION)
    )
    peripheral.tx_ack()
    peripheral.event_done()
    # The original update still completes at its Instant.
    peripheral.wait_instant(instant)
    peripheral.event_prepare()
    assert_phys(peripheral, Phy.LE_2M, Phy.LE_2M)
    peripheral.ut_rx(PHY_DONE, status=0)
