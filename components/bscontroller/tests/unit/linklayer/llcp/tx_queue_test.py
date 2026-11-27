# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Data/control transmit queue, ARQ and Host flow control (Zephyr ctrl_tx_queue,
ctrl_tx_buffer_alloc; Core Vol 6, Part B, 4.5.9; Vol 4, Part E, 7.7.19).

Every step goes through the real ConnectionEvent: SN/NESN, retransmission of the
same snapshot, MD, data pause and completed-packet credits.

Matrix: {queue ordering and pause, ARQ, Host ACL boundaries and credits} across
data-only, control-only and mixed queues, FIFO versus immediate replies,
retransmission and duplicate handling, encryption pause/resume, packet
boundaries and completed-packet credits. Scenarios use the Lower Tester / Upper
Tester harness (llcp/harness.py); collisions and terminate cases live in
collision_test.py and invalid_terminate_test.py.
"""

import pytest

from bscontroller.linklayer import (
    PDU_DATA_NESN_MASK,
    PDU_DATA_SN_MASK,
    LlId,
)
from bscontroller.linklayer.connection_event import Outbound
from bscontroller.linklayer.encryption import State
from tests.unit.linklayer.llcp.harness import (
    LTK,
    ROLES,
    Opcode,
    hci,
    pdu,
)


# ---------------------------------------------------------------------------
# Queue ordering and pause (ctrl_tx_queue)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('role', ROLES)
def test_data_only(iut, role):
    """Three queued ACL fragments go out in order, with MD on all but the last.

    Purpose: Verify that a data-only queue is transmitted in FIFO order, that the
    More Data bit is set while further fragments remain and clear on the last, and
    that each acknowledged fragment returns one completed-packet credit (4.5.9).
    Runs for both roles.

    Steps:
        1. Host queues three ACL fragments.
        2. For each, the IUT transmits and the peer acknowledges.

    Expected:
        - The payloads arrive in order with MD True, True, False.
        - Three completed-packet credits are returned.
    """
    dut = iut(role)
    for chunk in (b'one', b'two', b'three'):
        dut.connection.enqueue_acl(False, chunk)
    sent = []
    for _ in range(3):
        packet = dut.lt_rx_data()
        sent.append((packet.payload, packet.md))
        dut.tx_ack()
    assert sent == [(b'one', True), (b'two', True), (b'three', False)]
    assert dut.completed_packets() == 3


@pytest.mark.parametrize('role', ROLES)
def test_control_only(iut, role):
    """A single queued control PDU is sent once and leaves the queue empty.

    Purpose: Verify that a lone procedure PDU (VERSION_IND) is transmitted and,
    once acknowledged, nothing more waits for the air (4.5.9). Runs for both
    roles.

    Steps:
        1. Upper Tester starts a Version Exchange.
        2. The IUT sends LL_VERSION_IND and the peer acknowledges.

    Expected:
        - LL_VERSION_IND is the PDU on air.
        - No further control PDU is queued.
    """
    dut = iut(role)
    dut.host_start('read_remote_version')
    assert dut.lt_rx(Opcode.LL_VERSION_IND)
    dut.tx_ack()
    dut.lt_rx_q_is_empty()


@pytest.mark.parametrize('role', ROLES)
def test_control_and_data_keep_fifo_order(iut, role):
    """Queued procedure PDUs do not overtake older data (ctrl_and_data_1).

    Purpose: Verify that a procedure PDU queued between two ACL fragments keeps
    FIFO order: earlier data first, then the procedure PDU, then later data
    (4.5.9). Runs for both roles.

    Steps:
        1. Host queues data 'first', starts a PHY Update, then queues 'second'.
        2. The IUT transmits each in turn with acknowledgments.

    Expected:
        - On-air order is 'first', LL_PHY_REQ, 'second'.
    """
    dut = iut(role)
    dut.connection.enqueue_acl(False, b'first')
    dut.host_start('request_phy', 2, 2, 0)
    dut.connection.enqueue_acl(False, b'second')
    assert dut.lt_rx_data().payload == b'first'
    dut.tx_ack()
    dut.lt_rx(Opcode.LL_PHY_REQ)
    dut.tx_ack()
    assert dut.lt_rx_data().payload == b'second'


@pytest.mark.parametrize('role', ROLES)
def test_version_indication_is_prioritized(iut, role):
    """A started Version Exchange is sent ahead of already-queued data.

    Purpose: Verify that LL_VERSION_IND, as an autonomous indication, is
    transmitted before earlier-queued ACL data (4.5.9). Runs for both roles.

    Steps:
        1. Host queues ACL data, then starts a Version Exchange.

    Expected:
        - The next PDU on air is LL_VERSION_IND.
    """
    dut = iut(role)
    dut.connection.enqueue_acl(False, b'data')
    dut.host_start('read_remote_version')
    dut.lt_rx(Opcode.LL_VERSION_IND)


@pytest.mark.parametrize('role', ROLES)
def test_responses_are_prioritized(iut, role):
    """Immediate replies jump ahead of queued data (ctrl_and_data_2); procedure
    replies such as PING_RSP keep FIFO order.

    Purpose: Verify that an immediate reply (LL_UNKNOWN_RSP to an unknown opcode)
    is sent before queued ACL data, while a procedure reply such as LL_PING_RSP
    stays in FIFO order behind that data (4.5.9). Runs for both roles.

    Steps:
        1. Host queues ACL data.
        2. Lower Tester sends LL_PING_REQ, then an undefined opcode (0xFF).
        3. The IUT replies and the peer acknowledges between PDUs.

    Expected:
        - LL_UNKNOWN_RSP goes out before the queued data.
        - The queued data goes out next, then LL_PING_RSP.
    """
    dut = iut(role)
    dut.connection.enqueue_acl(False, b'data')
    dut.lt_tx(pdu(Opcode.LL_PING_REQ))
    dut.lt_tx(bytes([0xFF]))
    dut.lt_rx(Opcode.LL_UNKNOWN_RSP)
    dut.tx_ack()
    assert dut.lt_rx_data().payload == b'data'
    dut.tx_ack()
    dut.lt_rx(Opcode.LL_PING_RSP)


@pytest.mark.parametrize('role', ROLES)
def test_pending_data_is_not_preempted(iut, role):
    """A sent-but-unacked data PDU is retransmitted before any new control PDU.

    Purpose: Verify that a data PDU already on air but not yet acknowledged is
    retransmitted unchanged before a newly queued control reply is sent, and the
    retransmission keeps MD set because that reply is waiting (4.5.9). Runs for
    both roles.

    Steps:
        1. Host queues ACL data; the IUT transmits it (unacknowledged).
        2. Lower Tester sends LL_PING_REQ without acknowledging the data.
        3. The IUT retransmits; the peer then acknowledges.

    Expected:
        - The retransmission has the same LLID and payload as the first PDU.
        - Its MD bit is set (the queued PING_RSP keeps the event open).
        - LL_PING_RSP follows the acknowledgment.
    """
    dut = iut(role)
    dut.connection.enqueue_acl(False, b'data')
    first = dut.lt_rx_data()
    dut.lt_tx(pdu(Opcode.LL_PING_REQ), ack=False)
    again = dut.lt_rx_data()
    assert (again.llid, again.payload) == (first.llid, first.payload)
    assert again.md, 'the queued PING_RSP keeps the event open'
    dut.tx_ack()
    dut.lt_rx(Opcode.LL_PING_RSP)


def test_data_paused_during_encryption_resumes_after(central):
    """Pause and resume around a procedure (ctrl_and_data_3/4/5).

    Purpose: Verify that while encryption start is in progress, ACL data queued
    before and after the ENC_REQ is held (only Empty PDUs go out) and then resumes
    in order once encryption leaves the pausing state (5.1.3.1, 4.5.9).

    Steps:
        1. Host queues 'before', starts encryption, then queues 'after'.
        2. The IUT sends LL_ENC_REQ; the peer acknowledges.
        3. Two paused events each send an Empty PDU.
        4. The encryption state is forced back to IDLE to release data.

    Expected:
        - During the pause the queue still holds 'before' and 'after'.
        - After release the data goes out in order: 'before' then 'after'.
    """
    central.connection.enqueue_acl(False, b'before')
    central.host_start('enable_encryption', bytes(8), 0, LTK)
    central.connection.enqueue_acl(False, b'after')
    central.lt_rx(Opcode.LL_ENC_REQ)
    central.tx_ack()
    for _ in range(2):
        central.lt_rx_empty()
    assert [item.payload for item in central.connection.event.queue] == [
        b'before',
        b'after',
    ]
    central.encryption.state = State.IDLE
    assert central.lt_rx_data().payload == b'before'
    central.tx_ack()
    assert central.lt_rx_data().payload == b'after'


def test_paused_queue_sends_empty_without_md(peripheral):
    """A paused data queue emits an Empty PDU with no payload and no MD.

    Purpose: Verify that while data is paused (encryption in the KEY state), a
    queued ACL fragment does not go out; the IUT sends an Empty PDU with MD clear
    (5.1.3.1).

    Steps:
        1. Force the encryption state to KEY (data paused).
        2. Host queues an ACL fragment.
        3. The IUT prepares its next transmission.

    Expected:
        - The sent PDU is an Empty PDU (CONTINUATION, no payload, MD clear).
    """
    peripheral.encryption.state = State.KEY
    peripheral.connection.enqueue_acl(False, b'must wait')
    packet = peripheral._send()
    assert packet.llid == LlId.CONTINUATION and not packet.payload and not packet.md


@pytest.mark.parametrize(
    'opcode', [Opcode.LL_VERSION_IND, Opcode.LL_FEATURE_RSP, Opcode.LL_PING_RSP]
)
def test_non_encryption_control_waits_while_paused(central, opcode):
    """Non-encryption control PDUs wait while the data pause gate is closed.

    Purpose: Verify that while encryption is in a pausing state (ENC_RSP), a
    queued non-encryption control PDU is held back (the next outbound is empty)
    and is released only once the gate reopens (5.1.3.1). Runs for VERSION_IND,
    FEATURE_RSP and PING_RSP.

    Steps:
        1. Force the encryption state to ENC_RSP (gate closed).
        2. Queue the control PDU as outbound.
        3. Reopen the gate by forcing the state to IDLE.

    Expected:
        - While closed, the next outbound payload is empty.
        - After reopening, the next outbound payload is the control PDU.
    """
    central.encryption.state = State.ENC_RSP
    central.connection.event.queue.append(Outbound(LlId.CONTROL, bytes([opcode])))
    assert central.connection.event._next_outbound().payload == b''
    central.encryption.state = State.IDLE
    assert central.connection.event._next_outbound().payload == bytes([opcode])


def test_multiple_pause_resume(central):
    """Repeated refreshes never release paused data in plaintext.

    Purpose: Verify that across two back-to-back Encryption Pause/refresh cycles,
    data queued before the refreshes stays paused and is only released after both
    refreshes complete, never going out in plaintext (5.1.3.1).

    Steps:
        1. Start on an encrypted link and queue ACL data 'kept'.
        2. Twice: start encryption, exchange PAUSE_ENC_REQ/RSP, ENC_REQ/RSP,
           START_ENC_REQ/RSP, and observe Key Refresh Complete.
        3. Read the data after both cycles.

    Expected:
        - Each cycle reports Encryption Key Refresh Complete (SUCCESS).
        - The queued 'kept' data is delivered only after the cycles, encrypted.
    """
    central.enable_encryption()
    central.connection.enqueue_acl(False, b'kept')
    for _ in range(2):
        central.host_start('enable_encryption', bytes(8), 0, LTK)
        central.lt_rx(Opcode.LL_PAUSE_ENC_REQ)
        central.lt_tx(pdu(Opcode.LL_PAUSE_ENC_RSP))
        central.lt_rx(Opcode.LL_PAUSE_ENC_RSP)
        central.tx_ack()
        central.lt_rx(Opcode.LL_ENC_REQ)
        central.lt_tx(pdu(Opcode.LL_ENC_RSP) + bytes(12))
        central.install_peer_cipher()
        central.lt_rx_empty()
        central.lt_tx(pdu(Opcode.LL_START_ENC_REQ), encrypt=False)
        central.lt_rx(Opcode.LL_START_ENC_RSP)
        central.lt_tx(pdu(Opcode.LL_START_ENC_RSP))
        central.ut_rx(hci.HCI_Encryption_Key_Refresh_Complete_Event, status=0)
    assert central.lt_rx_data().payload == b'kept'


# ---------------------------------------------------------------------------
# ARQ (4.5.9)
# ---------------------------------------------------------------------------
def test_retransmit_keeps_sn_and_payload_until_ack(central):
    """An unacknowledged data PDU is retransmitted with the same SN and bytes.

    Purpose: Verify ARQ retransmission (4.5.9): until the peer acknowledges, the
    IUT resends the identical PDU with an unchanged SN, advancing only NESN to
    acknowledge the peer; the completed-packet credit is returned only on ack.

    Steps:
        1. Host queues ACL data; the IUT transmits it.
        2. Lower Tester sends a non-acknowledging Empty PDU.
        3. The IUT retransmits; the peer then acknowledges.

    Expected:
        - The retransmission equals the first PDU and retries is 1.
        - SN and payload bytes are identical; NESN flips to acknowledge the peer.
        - No credit before the ack; tx_sn advances to 1 and one credit after.
    """
    central.connection.enqueue_acl(False, b'payload')
    first = central.lt_rx_data()
    central.lt_tx_raw(LlId.CONTINUATION, b'', ack=False)
    again = central.lt_rx_data()
    assert again == first and central.connection.event.retries == 1
    # Same SN and bytes; NESN acknowledges the peer's new empty PDU.
    old, new = central.radio.sent[0][2], central.radio.sent[1][2]
    assert old[1:] == new[1:]
    assert old[0] & PDU_DATA_SN_MASK == new[0] & PDU_DATA_SN_MASK
    assert not old[0] & PDU_DATA_NESN_MASK and new[0] & PDU_DATA_NESN_MASK
    assert central.completed_packets() == 0
    central.tx_ack()
    assert central.connection.event.tx_sn == 1
    assert central.completed_packets() == 1


def test_crc_error_cannot_acknowledge(central):
    """A CRC-failed PDU never acknowledges the IUT's outstanding transmission.

    Purpose: Verify that a reception with a bad CRC cannot acknowledge a pending
    data PDU, so it remains pending and the SN does not advance (4.5.9).

    Steps:
        1. Host queues ACL data; the IUT transmits it.
        2. Lower Tester sends an acknowledging Empty PDU with an invalid CRC.

    Expected:
        - The pending PDU is still pending.
        - tx_sn is unchanged (0).
    """
    central.connection.enqueue_acl(False, b'test')
    central.lt_rx_data()
    central.lt_tx_raw(LlId.CONTINUATION, b'', ack=True, valid_crc=False)
    assert central.connection.event.pending is not None
    assert central.connection.event.tx_sn == 0


def test_duplicate_rx_is_acked_but_not_redelivered(central):
    """A retransmitted peer PDU (old SN) is acknowledged but not re-delivered.

    Purpose: Verify ARQ de-duplication (4.5.9): a peer PDU resent with the
    previous SN (because the peer missed our ACK) is acknowledged but its payload
    is not delivered to the Host a second time.

    Steps:
        1. Lower Tester sends ACL data; it is delivered to the Host.
        2. Lower Tester retransmits the same data with the old SN.

    Expected:
        - The first reception is delivered once.
        - The duplicate delivers nothing and rx_nesn stays at 1.
    """
    central.lt_tx_data(b'abc')
    assert central.acl_received() == [b'abc']
    # The peer did not see our ACK and retransmits with the old SN.
    central.lt_tx_raw(LlId.START, b'abc', sn=0)
    assert central.acl_received() == []
    assert central.connection.event.rx_nesn == 1


def test_duplicate_can_still_carry_a_new_ack(central):
    """A duplicate peer PDU still carries a fresh acknowledgment of our data.

    Purpose: Verify that a peer PDU which is a duplicate in the receive direction
    (old SN) can nevertheless acknowledge the IUT's own outstanding data via its
    NESN, clearing the pending transmission while delivering nothing new (4.5.9).

    Steps:
        1. Lower Tester sends ACL data; it is delivered and drained.
        2. Host queues the IUT's own ACL data; the IUT transmits it.
        3. Lower Tester resends the old data (old SN) but with an acknowledgment.

    Expected:
        - The IUT's pending transmission is cleared.
        - No new ACL data is delivered to the Host.
    """
    central.lt_tx_data(b'abc')
    central.acl_received()
    central.connection.enqueue_acl(False, b'ours')
    central.lt_rx_data()
    central.lt_tx_raw(LlId.START, b'abc', sn=0, ack=True)
    assert central.connection.event.pending is None
    assert central.acl_received() == []


@pytest.mark.parametrize(
    'pdu_bytes', [b'', b'\x01', b'\x02\x02x', b'\x02\x1c' + bytes(28)]
)
def test_invalid_data_pdu_is_ignored(central, pdu_bytes):
    """A malformed data PDU neither establishes the link nor reaches the Host.

    Purpose: Verify that a structurally invalid data-channel PDU (too short header,
    length/payload mismatch, over-length) is dropped: it does not establish the
    connection and nothing is delivered to the Host (4.5.9). Runs over an empty
    PDU, a one-byte PDU, a length/payload mismatch and an over-length PDU.

    Steps:
        1. Mark the connection as not yet established.
        2. Feed the malformed PDU into Connection.accept with a valid CRC.

    Expected:
        - The connection is still not established.
        - Nothing reaches the Host.
    """
    central.connection.established = False
    central.connection.accept(pdu_bytes, True)
    assert not central.connection.established
    central.ut_rx_q_is_empty()


def test_bad_crc_reception_establishes_without_payload(central):
    """A first reception with a bad CRC establishes the link but delivers nothing.

    Purpose: Verify that the first packet of a connection, even with a failing
    CRC, marks the connection established without acknowledging or delivering its
    payload (4.5.5, 4.5.9).

    Steps:
        1. Mark the connection as not yet established.
        2. Queue a bad-CRC START PDU carrying 'abc' and run one receive window.

    Expected:
        - The connection becomes established with rx_nesn still 0.
        - No ACL data is delivered to the Host.
    """
    from bscontroller.radio.base import Received

    central.connection.established = False
    central.radio.incoming.append(
        Received(bytes([LlId.START, 3]) + b'abc', -45, 1300, 1404, False)
    )
    central.loop.run_until_complete(central.connection.receive(7, 2000))
    assert central.connection.established and central.connection.event.rx_nesn == 0
    assert central.acl_received() == []


def test_failed_receive_observes_ack_without_consuming_pending(central):
    """A control reply that fails procedure processing still records the ack but
    keeps the pending PDU.

    Purpose: Verify that when a received control PDU acknowledges the IUT's
    outstanding control PDU but the procedure layer declines to consume it, the
    acknowledgment context is observed (not a duplicate) while the pending PDU and
    its wire snapshot remain intact for retransmission (4.5.9).

    Steps:
        1. Upper Tester starts a PHY Update; the IUT sends LL_PHY_REQ.
        2. Patch the procedure dispatch to record context and return None.
        3. Feed an acknowledging LL_PHY_RSP into Connection.accept.

    Expected:
        - The observed context's acknowledged_control equals the pending payload
          and is not a duplicate.
        - The pending PDU and its wire bytes are unchanged; tx_sn stays 0 and the
          PDU is still marked as sent.
    """
    from types import SimpleNamespace

    central.host_start('request_phy', 1, 1, 0)
    central.lt_rx(Opcode.LL_PHY_REQ)
    event = central.connection.event
    pending, wire = event.pending, event.pending_wire
    observed = []

    def reject(context):
        observed.append(context)
        return None

    event.control = SimpleNamespace(receive=reject)
    response = bytes([Opcode.LL_PHY_RSP, 1, 1])
    central.connection.accept(
        bytes([LlId.CONTROL | PDU_DATA_NESN_MASK, len(response)]) + response, True
    )
    assert observed[0].acknowledged_control == pending.payload
    assert not observed[0].duplicate
    assert event.pending is pending and event.pending_wire == wire
    assert event.tx_sn == 0 and event.sent_pending


# ---------------------------------------------------------------------------
# Host ACL boundaries and credits (ctrl_tx_buffer_alloc)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'continuation,llid', [(False, LlId.START), (True, LlId.CONTINUATION)]
)
def test_acl_packet_boundary(central, continuation, llid):
    """The Host packet boundary flag maps to the LLID of the transmitted PDU.

    Purpose: Verify that a first-fragment Host ACL packet is sent as LLID=START
    and a continuation as LLID=CONTINUATION (Vol 4, Part E, 7.7.19; 4.5.9). Runs
    for first-fragment and continuation.

    Steps:
        1. Host queues an ACL fragment with the given boundary flag.
        2. The IUT transmits it.

    Expected:
        - The on-air LLID is START or CONTINUATION to match the boundary.
    """
    central.connection.enqueue_acl(continuation, b'abc')
    assert central.lt_rx_data().llid == llid


@pytest.mark.parametrize('continuation', [False, True])
def test_empty_host_fragment(central, continuation):
    """An empty Host fragment returns its credit at once and never goes on air.

    Purpose: Verify that an empty ACL fragment is never sent as LLID=START with
    length 0: its credit is returned immediately, and following non-empty
    fragments keep their own boundary (4.5.9). Runs for first-fragment and
    continuation.

    Steps:
        1. Host queues an empty fragment with the given boundary flag.
        2. Host queues a non-empty continuation fragment; the IUT sends it.
        3. Host queues a further continuation; the IUT sends it.

    Expected:
        - The empty fragment leaves the queue empty and returns one credit.
        - The next fragment's LLID matches the empty fragment's boundary.
        - The subsequent fragment is a CONTINUATION.
    """
    central.connection.enqueue_acl(continuation, b'')
    # Never LLID=start with Length=0 on air; the credit returns immediately.
    assert not central.connection.event.queue
    assert central.completed_packets() == 1
    central.connection.enqueue_acl(True, b'first bytes')
    first = central.lt_rx_data()
    assert first.llid == (LlId.CONTINUATION if continuation else LlId.START)
    central.tx_ack()
    central.connection.enqueue_acl(True, b'next')
    assert central.lt_rx_data().llid == LlId.CONTINUATION


def test_empty_start_does_not_relabel_queued_or_control(central):
    """An empty start fragment does not re-label earlier data or control PDUs.

    Purpose: Verify that discarding an empty first fragment does not change the
    LLID of previously queued data or an intervening control PDU; the queue keeps
    its original START / CONTROL / START labelling (4.5.9).

    Steps:
        1. Queue data 'older' (start), an empty fragment, a PING_REQ control PDU,
           then data 'new' (continuation).

    Expected:
        - The queue is exactly [(START, 'older'), (CONTROL, PING_REQ),
          (START, 'new')].
    """
    connection = central.connection
    connection.enqueue_acl(False, b'older')
    connection.enqueue_acl(False, b'')
    connection.llcp.respond(bytes([Opcode.LL_PING_REQ]))
    connection.enqueue_acl(True, b'new')
    assert [(item.llid, item.payload) for item in connection.event.queue] == [
        (LlId.START, b'older'),
        (LlId.CONTROL, bytes([Opcode.LL_PING_REQ])),
        (LlId.START, b'new'),
    ]


def test_received_continuation_is_delivered_with_boundary(central):
    """A received continuation PDU is delivered to the Host with pb_flag=1.

    Purpose: Verify that a received LLID=CONTINUATION data PDU is surfaced to the
    Host as an ACL data packet carrying the continuation packet-boundary flag
    (Vol 4, Part E, 7.7.19).

    Steps:
        1. Lower Tester sends ACL data with start=False (a continuation).

    Expected:
        - The Host receives one ACL data packet with the payload and pb_flag 1.
    """
    central.lt_tx_data(b'part', start=False)
    events = [
        event
        for event in central.controller.events
        if isinstance(event, hci.HCI_AclDataPacket)
    ]
    assert events and events[0].data == b'part'
    assert events[0].pb_flag == 1


def test_reserved_llid_is_arq_only(central):
    """A reserved-LLID data PDU takes part in ARQ only and is never delivered.

    Purpose: Verify that a PDU with a reserved LLID is acknowledged (rx_nesn
    advances) but its payload is never delivered to the Host as ACL data (4.5.9).

    Steps:
        1. Lower Tester sends a raw reserved-LLID PDU carrying 'xyz'.

    Expected:
        - rx_nesn advances to 1.
        - No ACL data reaches the Host.
    """
    central.lt_tx_raw(LlId.RESERVED, b'xyz')
    assert central.connection.event.rx_nesn == 1
    assert central.acl_received() == []


def test_header_bits(central):
    """SN and NESN track transmission acknowledgment and peer reception.

    Purpose: Verify the SN/NESN header bookkeeping across a short exchange: the
    first transmission has SN=0, NESN=0; after an ack the next data PDU advances
    SN and, once the peer has sent a PDU, sets NESN to acknowledge it (4.5.9).

    Steps:
        1. Host queues two ACL fragments; the IUT sends the first.
        2. The peer acknowledges; the IUT sends the second.
        3. The peer sends its own ACL data.

    Expected:
        - The first header has SN and NESN clear.
        - The second header has SN set (after the ack) and NESN set
          (acknowledging the peer's first PDU).
        - The peer's data is delivered to the Host.
    """
    central.connection.enqueue_acl(False, b'a')
    central.connection.enqueue_acl(False, b'b')
    central.lt_rx_data()
    header = central.radio.sent[-1][2][0]
    assert not header & PDU_DATA_SN_MASK and not header & PDU_DATA_NESN_MASK
    central.tx_ack()
    header = central.radio.sent[-1][2][0]
    central.lt_rx_data()
    header = central.radio.sent[-1][2][0]
    # SN advanced after the ACK; NESN acknowledges the peer's first PDU.
    assert header & PDU_DATA_SN_MASK and header & PDU_DATA_NESN_MASK
    central.lt_tx_data(b'peer')
    assert central.acl_received() == [b'peer']


def test_cancel_with_identical_control_pdus(central):
    """Only the owner's unsent queue entry can be withdrawn (Vol 6, Part B, 5.2).

    Steps:
        Queue two identical Ping Responses and withdraw the second entry.
        Send the first, try to withdraw it during retransmission, then ACK it.

    Expected:
        The first entry stays queued, transmits twice and is released by its ACK.
    """
    connection = central.connection
    payload = bytes([Opcode.LL_PING_RSP])
    first = connection.llcp.respond(payload)
    second = connection.llcp.respond(payload)
    assert connection.event.cancel_queued_control(second)
    assert list(connection.event.queue) == [first]
    central.lt_rx(Opcode.LL_PING_RSP)
    assert not connection.event.cancel_queued_control(first)
    central.lt_rx(Opcode.LL_PING_RSP)
    central.tx_ack()
    assert connection.event.pending is None
