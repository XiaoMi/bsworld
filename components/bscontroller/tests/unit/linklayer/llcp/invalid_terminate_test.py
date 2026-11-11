# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unknown, unsupported, role-illegal and malformed control PDUs; ACL
termination. Mirrors Zephyr's ctrl_invalid, ctrl_unsupported, ctrl_undefined
and ctrl_terminate suites (Core Vol 6, Part B, 2.4.2, 5.1.6).

Invariants exercised here:
    - An unsupported or undefined opcode draws LL_UNKNOWN_RSP and keeps the link.
    - Our own LL_UNKNOWN_RSP is never answered (no ping-pong between two LLs).
    - A role-illegal PDU (one only the other role may send) is treated as unknown,
      and the role check precedes procedure-state dispatch.
    - A wrong-length control PDU is unknown; an empty control PDU and a reserved
      LLID take part in ARQ only and are never interpreted.
    - ACL termination (5.1.6): remote and local, timer handling, interaction with
      queued traffic, outstanding procedures, MD and HCI records.

Most scenarios run through the Lower Tester / Upper Tester harness
(llcp/harness.py) and parametrize ``role`` over ROLES where both roles behave the
same; a few drive the real ConnectionEvent loop directly.
"""

import pytest

from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    LL_SUPERVISION_TIMEOUT_UNIT_US,
    PDU_DATA_MD_MASK,
    PDU_DATA_NESN_MASK,
    LlId,
)
from bscontroller.linklayer.llcp import LL_CONTROL_PDU_SENDER_ROLES
from bscontroller.radio.base import Received
from tests.unit.linklayer.llcp.harness import (
    CENTRAL,
    PERIPHERAL,
    ROLES,
    ErrorCode,
    Opcode,
    hci,
    pdu,
    terminate_ind,
    unknown_rsp,
)

DISCONNECTED = hci.HCI_Disconnection_Complete_Event
REMOTE = ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
LOCAL = ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR

# Opcodes this Controller does not implement (CTE, ISO, PAwR, power control, ...).
UNSUPPORTED = [
    Opcode.LL_CTE_REQ,
    Opcode.LL_CTE_RSP,
    Opcode.LL_PERIODIC_SYNC_WR_IND,
    Opcode.LL_CLOCK_ACCURACY_REQ,
    Opcode.LL_CLOCK_ACCURACY_RSP,
    Opcode.LL_CIS_REQ,
    Opcode.LL_CIS_IND,
    Opcode.LL_POWER_CONTROL_REQ,
    Opcode.LL_SUBRATE_REQ,
    Opcode.LL_FEATURE_EXT_REQ,
    Opcode.LL_CS_REQ,
    Opcode.LL_CONNECTION_RATE_REQ,
]
# Reserved for future use or for specification development (Table 2.22).
UNDEFINED = [0x40, 0x7F, 0xEF, 0xF0, 0xFB, 0xFC, 0xFF]


# ---------------------------------------------------------------------------
# Unsupported and undefined opcodes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('opcode', UNSUPPORTED + UNDEFINED, ids=hex)
@pytest.mark.parametrize('role', ROLES)
def test_unsupported_and_undefined_rem(iut, role, opcode):
    """An unsupported or undefined opcode is answered with LL_UNKNOWN_RSP.

    Purpose: Verify that receiving an opcode this Controller does not implement
    (CTE, ISO, PAwR, power control, ...) or a reserved/undefined opcode draws
    LL_UNKNOWN_RSP and leaves the link up and idle (2.4.2). Runs for every
    unsupported and undefined opcode and for both roles.

    Steps:
        1. Lower Tester transmits the opcode in a control PDU.
        2. The event is closed and reopened.
        3. Lower Tester receives LL_UNKNOWN_RSP echoing the offending opcode and
           acknowledges it.

    Expected:
        - The on-air reply is LL_UNKNOWN_RSP for that opcode.
        - Nothing reaches the Host; the link is not closed and controls are idle.
    """
    dut = iut(role)
    dut.event_prepare()
    dut.lt_tx(bytes([opcode]) + bytes(4))
    dut.event_done()
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_UNKNOWN_RSP, unknown_rsp(opcode))
    dut.tx_ack()
    dut.event_done()
    dut.ut_rx_q_is_empty()
    assert not dut.connection.closed and not dut.controls.busy


@pytest.mark.parametrize('role', ROLES)
def test_unknown_rsp_is_never_answered(iut, role):
    """Answering our own UNKNOWN_RSP would loop forever between two LLs.

    Purpose: Verify that a received LL_UNKNOWN_RSP is never itself answered with
    another LL_UNKNOWN_RSP, which would create an endless exchange between two
    Link Layers (2.4.2). Runs for both roles.

    Steps:
        1. Lower Tester sends a bare undefined opcode (0xFF).
        2. Lower Tester receives the IUT's LL_UNKNOWN_RSP and acknowledges it.
        3. Lower Tester echoes that LL_UNKNOWN_RSP back, then an LL_UNKNOWN_RSP
           for opcode 0x00.

    Expected:
        - No further control PDU is queued for the air.
        - The link stays open.
    """
    dut = iut(role)
    dut.lt_tx(bytes([0xFF]))
    response = dut.lt_rx(Opcode.LL_UNKNOWN_RSP)
    dut.tx_ack()
    dut.lt_tx(response)
    dut.lt_tx(unknown_rsp(0x00))
    dut.lt_rx_q_is_empty()
    assert not dut.connection.closed


@pytest.mark.parametrize(
    'opcode,sender', LL_CONTROL_PDU_SENDER_ROLES.items(), ids=lambda v: str(v)
)
def test_role_illegal_pdu_is_unknown(iut, opcode, sender):
    """A PDU only the other role may send is unknown when received from the peer.

    Purpose: Verify that a control PDU whose only legitimate sender is a given
    role is treated as an unknown opcode when it arrives from the peer, drawing
    LL_UNKNOWN_RSP without closing the link (2.4.2). Runs over every opcode in
    LL_CONTROL_PDU_SENDER_ROLES with the IUT taking the sender's own role, so the
    PDU is illegal in the receive direction.

    Steps:
        1. The IUT is the role that is allowed to send this opcode.
        2. Lower Tester sends that opcode (illegal direction).

    Expected:
        - The IUT answers LL_UNKNOWN_RSP for the opcode.
        - The link is not closed.
    """
    dut = iut(sender)
    dut.lt_tx(bytes([opcode]))
    dut.lt_rx(Opcode.LL_UNKNOWN_RSP, unknown_rsp(opcode))
    assert not dut.connection.closed


@pytest.mark.parametrize(
    'opcode,sender', LL_CONTROL_PDU_SENDER_ROLES.items(), ids=lambda v: str(v)
)
def test_role_check_precedes_procedure_state(iut, monkeypatch, opcode, sender):
    """Even a truncated legal-role PDU reaches procedure dispatch; an illegal-role
    one never does.

    Purpose: Verify that the sender-role check happens before procedure-state
    dispatch: a legal-role PDU is handed to the procedure layer even when its body
    is truncated, while an illegal-role PDU is diverted to LL_UNKNOWN_RSP and never
    reaches dispatch (2.4.2). Runs over every opcode, exercising both the
    legal-role and illegal-role IUT for each.

    Steps:
        1. For the legal and then the illegal IUT role, patch the procedure
           dispatch (encryption.control) to record what it receives.
        2. Feed a bare (truncated) PDU of the opcode into Connection.control.

    Expected:
        - The legal-role IUT forwards the PDU to dispatch and queues nothing.
        - The illegal-role IUT dispatches nothing and queues a reply (UNKNOWN_RSP).
    """
    for legal in (False, True):
        dut = iut(CENTRAL if (sender == PERIPHERAL) == legal else PERIPHERAL)
        seen = []
        monkeypatch.setattr(
            dut.connection.encryption,
            'control',
            lambda p, seen=seen: seen.append(p) or True,
        )
        dut.connection.control(bytes([opcode]))
        assert bool(seen) == legal
        assert bool(dut.connection.event.queue) == (not legal)


# ---------------------------------------------------------------------------
# Malformed control PDUs (2.4.2 "wrong length")
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'payload',
    [
        bytes([Opcode.LL_PHY_REQ, 2]),
        bytes([Opcode.LL_PHY_REQ, 2, 2, 0]),
        bytes([Opcode.LL_LENGTH_REQ]) + bytes(7),
        bytes([Opcode.LL_CONNECTION_PARAM_REQ]) + bytes(22),
        bytes([Opcode.LL_PING_REQ, 0]),
        bytes([Opcode.LL_TERMINATE_IND]),
    ],
    ids=['phy-short', 'phy-long', 'length-short', 'cpr-short', 'ping-long', 'term'],
)
def test_wrong_length_is_unknown_and_keeps_link(central, payload):
    """A control PDU of the wrong length is answered with LL_UNKNOWN_RSP.

    Purpose: Verify that a known opcode carried with an incorrect length is
    rejected as unknown (LL_UNKNOWN_RSP for that opcode) and leaves the link up
    and idle (2.4.2 "wrong length"). Runs over short/long PHY_REQ, short
    LENGTH_REQ, short CONNECTION_PARAM_REQ, long PING_REQ and short TERMINATE_IND.

    Steps:
        1. Lower Tester sends the malformed control PDU.
        2. Lower Tester receives LL_UNKNOWN_RSP echoing the opcode.

    Expected:
        - The reply is LL_UNKNOWN_RSP for the PDU's opcode.
        - The link is not closed and controls are idle.
    """
    central.lt_tx(payload)
    central.lt_rx(Opcode.LL_UNKNOWN_RSP, unknown_rsp(payload[0]))
    assert not central.connection.closed and not central.controls.busy


def test_empty_control_pdu_is_dropped(central):
    """An empty control PDU (LLID=control, Length=0) is dropped after ARQ.

    Purpose: Verify that a control PDU with zero length is invalid and is handled
    by ARQ only, with no dispatch and no reply on air (2.4.2).

    Steps:
        1. Lower Tester sends a raw LLID=control PDU with an empty payload.

    Expected:
        - The IUT emits only an Empty PDU (no control reply).
        - Nothing reaches the Host.
    """
    # LLID=control with Length=0 is invalid (2.4.2): ARQ only, no dispatch.
    central.lt_tx_raw(LlId.CONTROL, b'')
    central.lt_rx_empty()
    central.ut_rx_q_is_empty()


def test_reserved_llid_participates_in_arq_only(central):
    """A reserved-LLID PDU is acknowledged but its payload is never interpreted.

    Purpose: Verify that a PDU with a reserved LLID takes part in ARQ (it is
    acknowledged) but its payload is never dispatched, even when it carries bytes
    that would otherwise be a TERMINATE_IND (2.4.2).

    Steps:
        1. Upper Tester starts a Feature Exchange; the IUT sends FEATURE_REQ.
        2. Lower Tester sends a reserved-LLID PDU whose body looks like
           TERMINATE_IND.

    Expected:
        - No pending event, no remote termination is recorded.
        - No control PDU is queued and no ACL data is delivered to the Host.
    """
    central.host_start('prepare_features')
    central.lt_rx(Opcode.LL_FEATURE_REQ)
    # A reserved-LLID PDU acknowledges, but its payload is never interpreted.
    central.lt_tx_raw(LlId.RESERVED, terminate_ind(REMOTE))
    assert central.connection.event.pending is None
    assert central.connection.remote_termination is None
    central.lt_rx_q_is_empty()
    assert central.acl_received() == []


# ---------------------------------------------------------------------------
# ACL Termination (5.1.6)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('role', ROLES)
def test_terminate_rem(iut, role):
    """A peer TERMINATE_IND closes the link only after the IUT acknowledges it.

    Purpose: Verify remote ACL termination (5.1.6): on receiving TERMINATE_IND
    the IUT keeps the link up until it has acknowledged the indication, then
    reports Disconnection Complete with the peer's reason and drops the
    connection. Runs for both roles.

    Steps:
        1. Lower Tester sends TERMINATE_IND (REMOTE_USER_TERMINATED).
        2. The IUT transmits its acknowledging Empty PDU.

    Expected:
        - The link is not closed before the acknowledgment.
        - Host gets Disconnection Complete (REMOTE_USER_TERMINATED) for the handle
          and nothing else; the handle is removed from the link's connections.
    """
    dut = iut(role)
    dut.event_prepare()
    dut.lt_tx(terminate_ind(REMOTE))
    # The link stays up until the IUT has acknowledged the indication.
    assert not dut.connection.closed
    dut.lt_rx_empty(ack=False)
    dut.ut_rx(DISCONNECTED, reason=REMOTE, connection_handle=dut.handle)
    dut.ut_rx_q_is_empty()
    assert dut.handle not in dut.link.connections


@pytest.mark.parametrize('role', ROLES)
def test_terminate_loc(iut, role):
    """A Host disconnect sends TERMINATE_IND and completes on the peer's ack.

    Purpose: Verify local ACL termination (5.1.6): a Host disconnect arms the
    supervision-timeout-based T_terminate, sends TERMINATE_IND, and completes with
    Disconnection Complete (local reason) only once the peer acknowledges. Runs
    for both roles.

    Steps:
        1. Upper Tester issues disconnect(REMOTE_USER_TERMINATED).
        2. The IUT sends LL_TERMINATE_IND.
        3. Lower Tester sends an unacknowledging PDU, then acknowledges.

    Expected:
        - The termination deadline equals the supervision timeout.
        - The link does not close until the acknowledgment arrives.
        - Host gets Disconnection Complete (CONNECTION_TERMINATED_BY_LOCAL_HOST)
          and nothing else.
    """
    dut = iut(role)
    assert dut.host('disconnect', REMOTE) == ErrorCode.SUCCESS
    deadline = dut.connection.termination_deadline_us
    assert deadline == dut.connection.parameters.timeout * (
        LL_SUPERVISION_TIMEOUT_UNIT_US
    )
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_TERMINATE_IND, terminate_ind(REMOTE))
    # Not yet: termination completes on the peer's acknowledgment.
    dut.lt_tx_raw(LlId.CONTINUATION, b'', ack=False)
    assert not dut.connection.closed
    dut.tx_ack()
    dut.ut_rx(DISCONNECTED, reason=LOCAL)
    dut.ut_rx_q_is_empty()


@pytest.mark.parametrize('role', ROLES)
def test_terminate_loc_unacknowledged_uses_terminate_timer(iut, role):
    """An unacknowledged local TERMINATE_IND closes the link at T_terminate.

    Purpose: Verify that when the peer never acknowledges TERMINATE_IND, valid
    receptions keep supervision alive but never restart T_terminate, so the link
    closes exactly at the original termination deadline (5.1.6). Runs for both
    roles.

    Steps:
        1. Upper Tester issues disconnect; the IUT sends LL_TERMINATE_IND.
        2. Time advances and the Host issues disconnect again; the peer sends a
           non-acknowledging valid PDU.
        3. Simulated time advances up to and then past the deadline.

    Expected:
        - The termination deadline is unchanged by the second disconnect and the
          valid reception.
        - The next operation is at the deadline; the link does not close one
          microsecond early but does at the deadline.
        - Host gets Disconnection Complete (CONNECTION_TERMINATED_BY_LOCAL_HOST).
    """
    dut = iut(role)
    dut.host('disconnect', REMOTE)
    deadline = dut.connection.termination_deadline_us
    dut.lt_rx(Opcode.LL_TERMINATE_IND)
    # Valid receptions keep supervision alive but never restart T_terminate.
    dut.elapse(100)
    dut.host('disconnect', REMOTE)
    dut.lt_tx_raw(LlId.CONTINUATION, b'', ack=False)
    assert dut.connection.termination_deadline_us == deadline
    dut.connection.anchor_us = deadline + 1_000_000
    assert dut.connection.next_operation_us == deadline
    assert not dut.elapse(deadline - 1 - dut.radio.now_us)
    assert dut.elapse(1)
    dut.ut_rx(DISCONNECTED, reason=LOCAL)


@pytest.mark.parametrize('role', ROLES)
def test_terminate_loc_preempts_queued_traffic(iut, role):
    """A local disconnect sends TERMINATE_IND ahead of queued data and procedures.

    Purpose: Verify that a Host disconnect takes priority: LL_TERMINATE_IND is the
    next PDU on air even when ACL data is queued and another procedure has been
    started (5.1.6). Runs for both roles.

    Steps:
        1. Host queues ACL data and starts a Version Exchange.
        2. Upper Tester issues disconnect(REMOTE_USER_TERMINATED).

    Expected:
        - The next control PDU on air is LL_TERMINATE_IND.
    """
    dut = iut(role)
    dut.connection.enqueue_acl(False, b'queued data')
    dut.host_start('prepare_version')
    dut.host('disconnect', REMOTE)
    dut.lt_rx(Opcode.LL_TERMINATE_IND)


@pytest.mark.parametrize('role', ROLES)
def test_terminate_rem_during_procedure(iut, role):
    """Zephyr ctrl_terminate: an outstanding local procedure ends with the link.

    Purpose: Verify that when a peer TERMINATE_IND arrives while a local Feature
    Exchange is outstanding, that procedure is completed with the termination
    reason and the link is torn down cleanly (5.1.6). Runs for both roles.

    Steps:
        1. Upper Tester starts a Feature Exchange.
        2. The IUT sends the role's FEATURE_REQ / PERIPHERAL_FEATURE_REQ.
        3. Lower Tester sends TERMINATE_IND (REMOTE_USER_TERMINATED).

    Expected:
        - Host gets LE Read Remote Features Complete (REMOTE_USER_TERMINATED).
        - Host gets Disconnection Complete (REMOTE_USER_TERMINATED).
        - The feature request is cleared, no procedure timer is armed, and no
          local exchange remains.
    """
    dut = iut(role)
    dut.host_start('prepare_features')
    dut.lt_rx(
        Opcode.LL_FEATURE_REQ if role == CENTRAL else Opcode.LL_PERIPHERAL_FEATURE_REQ
    )
    dut.lt_tx(terminate_ind(REMOTE))
    dut.lt_rx_empty(ack=False)
    dut.ut_rx(hci.HCI_LE_Read_Remote_Features_Complete_Event, status=REMOTE)
    dut.ut_rx(DISCONNECTED, reason=REMOTE)
    assert not dut.controls.feature_request
    assert dut.controls.next_deadline_us == 0
    assert not dut.connection.llcp.local_exchanges


@pytest.mark.parametrize('more', [False, True])
def test_central_terminate_ack_respects_md(central, more):
    """Through the real event loop: the Central acknowledges TERMINATE_IND in the
    same event only when MD keeps that event open; otherwise at the next anchor.

    Purpose: Verify, by driving the real connection event loop, that the Central's
    acknowledgment of a peer TERMINATE_IND is sent in the same event when the
    peer's More Data (MD) bit holds the event open, and otherwise at the next
    anchor; the link closes once that ack goes out (5.1.6). Runs with MD clear and
    set.

    Steps:
        1. The peer's TERMINATE_IND is queued with MD set or clear.
        2. The event loop advances one connection interval.
        3. With MD clear, the loop advances again to the next anchor.

    Expected:
        - With MD set the link closes within the same event (two PDUs sent);
          with MD clear it closes at the next anchor (one PDU, then the ack).
        - The final transmitted header acknowledges the peer (NESN set).
        - Host gets Disconnection Complete (REMOTE_USER_TERMINATED).
    """
    connection = central.connection
    packet = bytes([LlId.CONTROL | (PDU_DATA_MD_MASK if more else 0), 2])
    central.radio.incoming.append(
        Received(packet + terminate_ind(REMOTE), -45, 1480, 1592, True)
    )
    central.loop.run_until_complete(connection.advance_to_us(LL_CONN_INTERVAL_UNIT_US))
    assert connection.closed == more
    assert len(central.radio.sent) == (2 if more else 1)
    if not more:
        anchor = connection.anchor_us
        central.loop.run_until_complete(connection.advance_to_us(anchor))
        assert connection.closed and central.radio.sent[-1][0] == anchor
    assert central.radio.sent[-1][2][0] & PDU_DATA_NESN_MASK
    central.ut_rx(DISCONNECTED, reason=REMOTE)


def test_disconnect_unknown_handle(central):
    """Disconnecting an unknown handle returns Unknown Connection Identifier.

    Purpose: Verify that the LL disconnect entry point rejects a handle that does
    not exist with UNKNOWN_CONNECTION_IDENTIFIER (5.1.6).

    Steps:
        1. Upper Tester calls disconnect on a non-existent handle (0x0EFF).

    Expected:
        - The call returns UNKNOWN_CONNECTION_IDENTIFIER.
    """
    service = central.controller.le_procedures.ll
    assert (
        service.disconnect(0x0EFF, REMOTE)
        == ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
    )


def test_ping_req_wrong_length_regression(central):
    """A PING_REQ carrying an (empty) length is still answered with PING_RSP.

    Purpose: Regression guard that an LL_PING_REQ built through ``pdu`` is accepted
    and answered with LL_PING_RSP rather than mis-classified as wrong-length
    (2.4.2).

    Steps:
        1. Lower Tester sends LL_PING_REQ.

    Expected:
        - The IUT answers LL_PING_RSP.
    """
    central.lt_tx(pdu(Opcode.LL_PING_REQ))
    central.lt_rx(Opcode.LL_PING_RSP)


def test_disconnect_of_connection_with_hci_record(central):
    """HCI connection records are released with the LL connection.

    Purpose: Verify that tearing down an LL connection also releases the
    associated HCI ConnectionRecord, so no stale record survives the peer's
    disconnection (5.1.6).

    Steps:
        1. Attach an HCI ConnectionRecord for the connection's peer.
        2. Lower Tester sends TERMINATE_IND; the IUT acknowledges it.

    Expected:
        - Host gets Disconnection Complete (REMOTE_USER_TERMINATED).
        - The record's disconnected callback fires and the peer is removed from
          le_connections.
    """
    from bscontroller.hci.records import ConnectionRecord

    controller = central.controller
    record = ConnectionRecord.__new__(ConnectionRecord)
    released = []
    record.disconnected = released.append
    controller.le_connections[central.connection.peer] = record
    central.lt_tx(terminate_ind(REMOTE))
    central.lt_rx_empty(ack=False)
    central.ut_rx(DISCONNECTED, reason=REMOTE)
    assert released and central.connection.peer not in controller.le_connections
