# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Encryption Start/Pause, LE Ping and Authenticated Payload Timeout (Core Vol 6,
Part B, 5.1.3, 5.1.8, 5.4).

Matrix: {Central local, Peripheral remote} x {start, refresh} x {success, key
missing, rejected, unsupported, timeout, MIC failure, unexpected PDU}, plus the
data-pause rules that surround every step. All encrypted PDUs are produced and
checked by an independent peer cipher derived from the on-air SKD/IV values.

Scenarios use the Lower Tester / Upper Tester harness (llcp/harness.py); the
peer-side session key is installed with install_peer_cipher / enable_encryption so
that AES-CCM, MIC failures and plaintext/encrypted RX transitions are exercised on
real traffic.
"""

import pytest

from bscontroller.linklayer import LL_CONN_INTERVAL_UNIT_US
from bscontroller.linklayer.encryption import State
from bscontroller.linklayer.features import LeFeatureMask
from tests.unit.linklayer.llcp.harness import (
    LTK,
    PHY_1M,
    PROCEDURE_COLLISION,
    ErrorCode,
    LlId,
    Opcode,
    enc_req,
    enc_rsp,
    hci,
    pdu,
    phy_rsp,
    phy_update_ind,
    reject_ext_ind,
    reject_ind,
    unknown_rsp,
    version_ind,
)

RAND = bytes(range(1, 9))
EDIV = 0x1234
CHANGED = hci.HCI_Encryption_Change_Event
REFRESHED = hci.HCI_Encryption_Key_Refresh_Complete_Event
LTK_REQUEST = hci.HCI_LE_Long_Term_Key_Request_Event
DISCONNECTED = hci.HCI_Disconnection_Complete_Event
MIC_FAILURE = ErrorCode.CONNECTION_TERMINATED_DUE_TO_MIC_FAILURE_ERROR
KEY_MISSING = ErrorCode.PIN_OR_KEY_MISSING_ERROR
ON = hci.HCI_Encryption_Change_Event.Enabled.E0_OR_AES_CCM
OFF = hci.HCI_Encryption_Change_Event.Enabled.OFF


def encrypted(dut):
    security = dut.connection.packet_security
    return security.tx_enabled, security.rx_enabled


def assert_data_paused(dut):
    """Queued Host data stays queued; only an Empty PDU goes out (5.1.3.1)."""
    dut.connection.enqueue_acl(False, b'paused')
    dut.event_prepare()
    dut.lt_rx_empty()
    dut.event_done()
    assert dut.connection.event.queue[-1].payload == b'paused'


# ---------------------------------------------------------------------------
# Central, local start
# ---------------------------------------------------------------------------
def central_start(dut, *, refresh=False):
    """Drive the Central through ENC_REQ ... START_ENC_RSP; stop before the end."""
    dut.host_start('prepare_encryption', RAND, EDIV, LTK)
    dut.event_prepare()
    request = dut.lt_rx(Opcode.LL_ENC_REQ)
    assert request[1:9] == RAND and request[9:11] == EDIV.to_bytes(2, 'little')
    assert dut.data_paused() and dut.encryption.deadline_us
    dut.lt_tx(enc_rsp())
    dut.event_done()
    assert encrypted(dut) == (False, False)
    dut.install_peer_cipher()
    # The Peripheral's START_ENC_REQ is sent unencrypted.
    dut.event_prepare()
    dut.lt_rx_empty()
    dut.lt_tx(pdu(Opcode.LL_START_ENC_REQ), encrypt=False)
    assert encrypted(dut) == (True, True)
    dut.lt_rx(Opcode.LL_START_ENC_RSP)
    dut.event_done()


def test_central_loc_start(central):
    """A Central-initiated Encryption Start pauses data until encryption is on.

    Purpose: Verify the full Central local Encryption Start (5.1.3): data queued
    before the start stays paused through ENC_REQ/RSP and START_ENC_REQ, is
    released only after the encrypted START_ENC_RSP is acknowledged, and then
    flows encrypted both ways with the deadline cleared.

    Steps:
        1. Host queues ACL data 'before', then the Central runs ENC_REQ ...
           START_ENC_RSP via central_start().
        2. Lower Tester sends the encrypted START_ENC_RSP.
        3. The IUT's data is read and the peer replies with encrypted data.

    Expected:
        - Data stays paused until START_ENC_RSP arrives.
        - Host gets Encryption Change (SUCCESS, enabled) and nothing else.
        - Data no longer paused, encryption enabled, deadline cleared.
        - 'before' is delivered encrypted and the peer's 'reply' is received.
    """
    central.connection.enqueue_acl(False, b'before')
    central_start(central)
    # User data is still paused until the encrypted START_ENC_RSP arrives.
    assert central.data_paused()
    central.event_prepare()
    central.lt_tx(pdu(Opcode.LL_START_ENC_RSP))
    central.ut_rx(
        CHANGED, status=0, connection_handle=central.handle, encryption_enabled=ON
    )
    central.ut_rx_q_is_empty()
    assert not central.data_paused() and central.encryption.enabled
    assert central.encryption.deadline_us == 0
    # Data now flows, encrypted, and the peer can read it.
    assert central.lt_rx_data().payload == b'before'
    central.tx_ack()
    central.lt_tx_data(b'reply')
    assert central.acl_received() == [b'reply']


@pytest.mark.parametrize(
    'response,status',
    [
        (reject_ind(KEY_MISSING), KEY_MISSING),
        (reject_ext_ind(Opcode.LL_ENC_REQ, KEY_MISSING), KEY_MISSING),
        (unknown_rsp(Opcode.LL_ENC_REQ), ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR),
    ],
    ids=['reject', 'reject-ext', 'unknown'],
)
def test_central_loc_start_refused(central, response, status):
    """A peer refusal of the Central's ENC_REQ aborts the start cleanly.

    Purpose: Verify that when the peer refuses the Central's ENC_REQ, encryption
    is not enabled, data resumes unpaused and the link stays open, with the Host
    told the corresponding failure status (5.1.3). Runs for each refusal kind:
    LL_REJECT_IND, LL_REJECT_EXT_IND (both key missing) and LL_UNKNOWN_RSP
    (unsupported remote feature).

    Steps:
        1. Host starts Encryption Start; the IUT sends LL_ENC_REQ.
        2. Lower Tester sends the refusal PDU.

    Expected:
        - Host gets Encryption Change with the expected failure status, disabled.
        - Data is not paused, the link is not closed, and encryption stays off.
    """
    central.host_start('prepare_encryption', RAND, EDIV, LTK)
    central.event_prepare()
    central.lt_rx(Opcode.LL_ENC_REQ)
    central.lt_tx(response)
    central.event_done()
    central.ut_rx(CHANGED, status=status, encryption_enabled=OFF)
    central.ut_rx_q_is_empty()
    assert not central.data_paused() and not central.connection.closed
    assert encrypted(central) == (False, False)


def test_central_loc_start_refused_after_enc_rsp(central):
    """A late key-missing rejection (after ENC_RSP) aborts the start.

    Purpose: Verify that when the peer answers ENC_RSP but then rejects with key
    missing instead of START_ENC_REQ, the Central reports the failure and does not
    enable encryption (5.1.3).

    Steps:
        1. Host starts Encryption Start; the IUT sends LL_ENC_REQ.
        2. Lower Tester sends LL_ENC_RSP, then in the next event sends
           LL_REJECT_EXT_IND (ENC_REQ, key missing).

    Expected:
        - Host gets Encryption Change (PIN_OR_KEY_MISSING).
        - Encryption stays off in both directions.
    """
    central.host_start('prepare_encryption', RAND, EDIV, LTK)
    central.event_prepare()
    central.lt_rx(Opcode.LL_ENC_REQ)
    central.lt_tx(enc_rsp())
    central.event_done()
    # The Peripheral Host has no key: it rejects instead of START_ENC_REQ.
    central.event_prepare()
    central.lt_rx_empty()
    central.lt_tx(reject_ext_ind(Opcode.LL_ENC_REQ, KEY_MISSING))
    central.ut_rx(CHANGED, status=KEY_MISSING)
    assert encrypted(central) == (False, False)


def test_central_loc_start_timeout(central):
    """An unanswered ENC_REQ times out and closes the link.

    Purpose: Verify that if the peer acknowledges LL_ENC_REQ but never responds,
    the procedure response timer (T_PRT) expires and the link is torn down
    (5.1.3, T_PRT).

    Steps:
        1. Host starts Encryption Start; the IUT sends LL_ENC_REQ.
        2. The peer acknowledges but sends no response.
        3. Simulated time advances to just before, then to, the deadline.

    Expected:
        - The timer does not fire one microsecond early but fires at the deadline.
        - Host gets Disconnection Complete (LMP_OR_LL_RESPONSE_TIMEOUT).
    """
    central.host_start('prepare_encryption', RAND, EDIV, LTK)
    central.event_prepare()
    central.lt_rx(Opcode.LL_ENC_REQ)
    central.tx_ack()
    central.event_done()
    assert not central.expire_response_timer(early_us=1)
    assert central.elapse(1)
    central.ut_rx(DISCONNECTED, reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)


@pytest.mark.parametrize('opcode', [Opcode.LL_VERSION_IND, Opcode.LL_LENGTH_REQ])
def test_central_unexpected_pdu_during_start_closes_link(central, opcode):
    """An unexpected PDU during the START_REQ wait closes the link as MIC failure.

    Purpose: Verify that once ENC_RSP is received, only START_ENC_REQ,
    TERMINATE_IND or rejects are legal; any other control PDU received in the
    START_REQ state terminates the link with a MIC failure reason (5.1.3). Runs
    for an unexpected LL_VERSION_IND and LL_LENGTH_REQ.

    Steps:
        1. Host starts Encryption Start; the IUT sends LL_ENC_REQ.
        2. Lower Tester sends LL_ENC_RSP.
        3. Lower Tester sends the unexpected PDU in the next event.

    Expected:
        - Host gets Disconnection Complete
          (CONNECTION_TERMINATED_DUE_TO_MIC_FAILURE).
    """
    central.host_start('prepare_encryption', RAND, EDIV, LTK)
    central.event_prepare()
    central.lt_rx(Opcode.LL_ENC_REQ)
    central.lt_tx(enc_rsp())
    central.event_done()
    # START_REQ state: only START_ENC_REQ, TERMINATE_IND or rejects may follow.
    central.event_prepare()
    central.lt_tx(version_ind() if opcode == Opcode.LL_VERSION_IND else pdu(opcode))
    central.ut_rx(DISCONNECTED, reason=MIC_FAILURE)


def test_central_mic_failure_closes_link(central):
    """A received PDU with a bad MIC closes the encrypted link.

    Purpose: Verify that on an encrypted link, a received PDU whose MIC fails
    verification terminates the connection with a MIC failure and delivers nothing
    (5.1.3.2, Part E).

    Steps:
        1. Start on an encrypted link.
        2. Encrypt a PDU with the peer cipher, corrupt its last byte, and accept
           it with the correct header NESN.

    Expected:
        - rx_nesn does not advance (the bad PDU is not acknowledged).
        - Host gets Disconnection Complete (MIC failure).
        - No ACL data is delivered.
    """
    central.enable_encryption()
    wire = bytearray(central.peer_cipher.encrypt(LlId.START, b'tampered'))
    wire[-1] ^= 1
    header = LlId.START | (0x04 if central.connection.event.rx_nesn else 0)
    central.connection.accept(bytes([header, len(wire)]) + wire, True)
    assert central.connection.event.rx_nesn == 0
    central.ut_rx(DISCONNECTED, reason=MIC_FAILURE)
    assert central.acl_received() == []


def test_central_plaintext_on_encrypted_link_closes(central):
    """Plaintext data on an encrypted link closes the link as MIC failure.

    Purpose: Verify that on an encrypted link a received plaintext data PDU is
    rejected as a MIC failure and the connection is terminated (5.1.3.2).

    Steps:
        1. Start on an encrypted link.
        2. Lower Tester sends data with encrypt=False.

    Expected:
        - Host gets Disconnection Complete (MIC failure).
    """
    central.enable_encryption()
    central.lt_tx_data(b'plaintext', encrypt=False)
    central.ut_rx(DISCONNECTED, reason=MIC_FAILURE)


def test_central_host_admission(central):
    """Only one Encryption Start may be admitted at a time on the Central.

    Purpose: Verify Host admission control: a second prepare_encryption while one
    is outstanding, and a supply_key on the Central, are both refused with Command
    Disallowed (5.1.3).

    Steps:
        1. Host starts Encryption Start successfully.
        2. Host tries prepare_encryption again.
        3. Host calls supply_key.

    Expected:
        - The second prepare_encryption returns COMMAND_DISALLOWED.
        - supply_key returns COMMAND_DISALLOWED.
    """
    central.host_start('prepare_encryption', RAND, EDIV, LTK)
    central.host_start(
        'prepare_encryption', RAND, EDIV, LTK, status=ErrorCode.COMMAND_DISALLOWED_ERROR
    )
    assert central.host('supply_key', LTK) == ErrorCode.COMMAND_DISALLOWED_ERROR


def test_peripheral_cannot_start_encryption(peripheral):
    """A Peripheral cannot initiate Encryption Start.

    Purpose: Verify that a Host prepare_encryption on a Peripheral is refused with
    Command Disallowed, since only the Central may start encryption (5.1.3).

    Steps:
        1. Host calls prepare_encryption on the Peripheral.

    Expected:
        - The call returns COMMAND_DISALLOWED.
    """
    peripheral.host_start(
        'prepare_encryption', RAND, EDIV, LTK, status=ErrorCode.COMMAND_DISALLOWED_ERROR
    )


def test_encryption_state_is_per_connection(central):
    """Encryption state and LTK are kept per connection, not shared.

    Purpose: Verify that enabling encryption on two connections stores each LTK
    independently and uses distinct encryption state objects, so one connection's
    key never leaks into another (5.1.3).

    Steps:
        1. Add a second connection alongside the fixture's.
        2. Issue LE Enable Encryption with a different key for each.

    Expected:
        - Each connection's stored LTK matches the key it was given.
        - The two connections' encryption objects are distinct.
        - Each event queue holds exactly one entry.
    """
    from tests.unit.helpers import add_connection

    second = add_connection(central.connection, 1)
    sink = central.link.listener.sink
    for value, key in ((central.connection, b'1' * 16), (second, b'2' * 16)):
        sink.on_hci_le_enable_encryption_command(
            hci.HCI_LE_Enable_Encryption_Command(value.handle, bytes(8), 0, key)
        )
        assert value.encryption.ltk == key
    assert central.encryption is not second.encryption
    assert len(central.connection.event.queue) == len(second.event.queue) == 1


# ---------------------------------------------------------------------------
# Central, local refresh (Encryption Pause)
# ---------------------------------------------------------------------------
def test_central_loc_refresh(central):
    """A Central key refresh pauses data and reports Key Refresh Complete.

    Purpose: Verify Encryption Pause then restart (key refresh) on the Central
    (5.1.3): the PAUSE_ENC_REQ/RSP exchange turns encryption off on both sides,
    the restart is a full Encryption Start reported as Key Refresh Complete, and
    queued data is held until the refresh completes.

    Steps:
        1. Start on an encrypted link and queue ACL data 'queued'.
        2. Host starts Encryption Start (refresh); exchange PAUSE_ENC_REQ/RSP.
        3. Run the restart start sequence via central_start_after_pause().
        4. Read the queued data.

    Expected:
        - After PAUSE_ENC_RSP encryption is off in both directions.
        - Host gets Encryption Key Refresh Complete (SUCCESS) and nothing else.
        - 'queued' is delivered after the refresh.
    """
    central.enable_encryption()
    central.connection.enqueue_acl(False, b'queued')
    central.host_start('prepare_encryption', RAND, EDIV, LTK)
    central.event_prepare()
    central.lt_rx(Opcode.LL_PAUSE_ENC_REQ)
    # Encrypted pause response from the peer turns encryption off on both sides.
    central.lt_tx(pdu(Opcode.LL_PAUSE_ENC_RSP))
    assert encrypted(central) == (False, False)
    central.lt_rx(Opcode.LL_PAUSE_ENC_RSP)
    central.tx_ack()
    central.event_done()
    # The restart is a full Encryption Start, reported as Key Refresh Complete.
    central_start_after_pause(central)
    central.ut_rx(REFRESHED, status=0, connection_handle=central.handle)
    central.ut_rx_q_is_empty()
    assert central.lt_rx_data().payload == b'queued'


def central_start_after_pause(dut):
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_ENC_REQ)
    dut.lt_tx(enc_rsp(bytes(8), bytes(4)))
    dut.install_peer_cipher()
    dut.lt_rx_empty()
    dut.lt_tx(pdu(Opcode.LL_START_ENC_REQ), encrypt=False)
    dut.lt_rx(Opcode.LL_START_ENC_RSP)
    dut.lt_tx(pdu(Opcode.LL_START_ENC_RSP))
    dut.event_done()


def test_central_loc_refresh_failure_closes_link(central):
    """A failed key refresh never resumes paused data in plaintext; it closes.

    Purpose: Verify that when the restart after Encryption Pause is rejected with
    key missing, the Central reports the refresh failure and closes the link
    rather than resuming paused user data unencrypted (5.1.3).

    Steps:
        1. Start on an encrypted link; Host starts Encryption Start (refresh).
        2. Exchange PAUSE_ENC_REQ/RSP, then the IUT sends LL_ENC_REQ.
        3. Lower Tester sends LL_REJECT_IND (key missing).

    Expected:
        - Host gets Encryption Key Refresh Complete (PIN_OR_KEY_MISSING).
        - Host gets Disconnection Complete (PIN_OR_KEY_MISSING).
        - Encryption is off in both directions.
    """
    central.enable_encryption()
    central.host_start('prepare_encryption', RAND, EDIV, LTK)
    central.event_prepare()
    central.lt_rx(Opcode.LL_PAUSE_ENC_REQ)
    central.lt_tx(pdu(Opcode.LL_PAUSE_ENC_RSP))
    central.lt_rx(Opcode.LL_PAUSE_ENC_RSP)
    central.tx_ack()
    central.lt_rx(Opcode.LL_ENC_REQ)
    central.lt_tx(reject_ind(KEY_MISSING))
    # Paused user data must never resume in plaintext after a failed refresh.
    central.ut_rx(REFRESHED, status=KEY_MISSING)
    central.ut_rx(DISCONNECTED, reason=KEY_MISSING)
    assert encrypted(central) == (False, False)


# ---------------------------------------------------------------------------
# Peripheral, remote start
# ---------------------------------------------------------------------------
def peripheral_key_request(dut, *, extended=False):
    dut.set_peer_features(
        int(LeFeatureMask.EXTENDED_REJECT_INDICATION) if extended else 0
    )
    dut.event_prepare()
    dut.lt_tx(enc_req(RAND, EDIV, bytes(range(8)), bytes(range(4))))
    dut.ut_rx(
        LTK_REQUEST,
        connection_handle=dut.handle,
        random_number=RAND,
        encryption_diversifier=EDIV,
    )
    dut.lt_rx(Opcode.LL_ENC_RSP)
    dut.tx_ack()
    dut.event_done()
    assert dut.encryption.state == State.KEY and dut.encryption.deadline_us


def test_periph_rem_start(peripheral):
    """A peer-initiated Encryption Start on the Peripheral completes with the key.

    Purpose: Verify the Peripheral remote Encryption Start (5.1.3): on ENC_REQ the
    Host is asked for the LTK, data is paused, and after the key is supplied the
    Peripheral sends START_ENC_REQ (RX becomes encrypted) and START_ENC_RSP,
    enabling encryption and resuming data.

    Steps:
        1. The peer sends ENC_REQ via peripheral_key_request(); the Host is asked
           for the key and data is confirmed paused.
        2. Host supplies the LTK; the IUT sends LL_START_ENC_REQ.
        3. Lower Tester sends START_ENC_RSP; the IUT sends its START_ENC_RSP.

    Expected:
        - From START_ENC_REQ the Peripheral receives encrypted; after
          START_ENC_RSP both directions are encrypted.
        - Host gets Encryption Change (SUCCESS, enabled) and nothing else.
        - The queued 'before' data is delivered afterwards.
    """
    peripheral.connection.enqueue_acl(False, b'before')
    peripheral_key_request(peripheral)
    assert_data_paused(peripheral)
    assert peripheral.host('supply_key', LTK) == ErrorCode.SUCCESS
    peripheral.install_peer_cipher()
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_START_ENC_REQ)
    # From START_ENC_REQ transmission the Peripheral receives encrypted.
    assert encrypted(peripheral) == (False, True)
    peripheral.lt_tx(pdu(Opcode.LL_START_ENC_RSP))
    assert encrypted(peripheral) == (True, True)
    peripheral.lt_rx(Opcode.LL_START_ENC_RSP)
    peripheral.ut_rx_q_is_empty()
    peripheral.tx_ack()
    peripheral.event_done()
    peripheral.ut_rx(CHANGED, status=0, encryption_enabled=ON)
    peripheral.ut_rx_q_is_empty()
    assert peripheral.lt_rx_data().payload == b'before'


@pytest.mark.parametrize('extended', [False, True], ids=['reject', 'reject-ext'])
def test_periph_rem_start_key_missing(peripheral, extended):
    """A missing key makes the Peripheral reject the peer's Encryption Start.

    Purpose: Verify that when the Host has no key for a peer-initiated start, the
    Peripheral rejects with key missing; ACL data stays paused until the rejection
    is acknowledged, then resumes with the link open and the response timer
    cleared (5.1.3.1). Runs for the plain and extended rejection forms.

    Steps:
        1. The peer sends ENC_REQ via peripheral_key_request().
        2. Host queues data 'queued' and supplies no key (None).
        3. The IUT sends the rejection; the peer acknowledges it.

    Expected:
        - ACL data stays paused until the rejection is acknowledged.
        - After the ack data is not paused, the link is open, deadline is 0.
        - The queued 'queued' data is delivered and nothing reaches the Host.
    """
    peripheral_key_request(peripheral, extended=extended)
    peripheral.connection.enqueue_acl(False, b'queued')
    assert peripheral.host('supply_key', None) == ErrorCode.SUCCESS
    rejection = (
        reject_ext_ind(Opcode.LL_ENC_REQ, KEY_MISSING)
        if extended
        else reject_ind(KEY_MISSING)
    )
    peripheral.event_prepare()
    peripheral.lt_rx(rejection[0], rejection)
    # ACL stays paused until the rejection is acknowledged (5.1.3.1).
    assert peripheral.data_paused()
    peripheral.tx_ack()
    peripheral.event_done()
    assert not peripheral.data_paused() and not peripheral.connection.closed
    assert peripheral.encryption.deadline_us == 0
    assert peripheral.lt_rx_data().payload == b'queued'
    peripheral.ut_rx_q_is_empty()


@pytest.mark.parametrize('peer_data', [b'', b'peer data'], ids=['empty', 'data'])
def test_periph_rem_key_missing_ack_may_carry_peer_data(peripheral, peer_data):
    """Only a valid acknowledging PDU releases the paused data after a rejection.

    Purpose: Verify that after a key-missing rejection, paused data is released
    only when a valid PDU actually acknowledges the rejection; a bad-CRC ack or
    non-acknowledging valid PDUs release nothing, and the releasing PDU's own
    payload is delivered to the Host (5.1.3.1). Runs with an empty and a non-empty
    releasing payload.

    Steps:
        1. The peer sends ENC_REQ; Host supplies no key; the IUT sends REJECT_IND.
        2. Lower Tester sends a bad-CRC ack, then a valid-but-non-acking PDU, then
           a non-acknowledging continuation.
        3. The IUT retransmits REJECT_IND; the peer sends the releasing PDU.

    Expected:
        - Data stays paused through the non-releasing PDUs.
        - After the releasing PDU data is not paused and the link is open.
        - Any non-empty releasing payload is delivered to the Host.
    """
    peripheral_key_request(peripheral)
    peripheral.host('supply_key', None)
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_REJECT_IND)
    # A bad-CRC ACK, or valid packets that do not acknowledge, release nothing.
    peripheral.lt_tx_raw(LlId.CONTINUATION, b'', ack=True, valid_crc=False)
    peripheral.lt_tx_raw(LlId.START, bytes(28), ack=True)
    peripheral.lt_tx_raw(LlId.CONTINUATION, b'', ack=False)
    assert peripheral.data_paused()
    peripheral.lt_rx(Opcode.LL_REJECT_IND)
    peripheral.lt_tx_raw(LlId.START if peer_data else LlId.CONTINUATION, peer_data)
    assert not peripheral.data_paused() and not peripheral.connection.closed
    assert peripheral.acl_received() == ([peer_data] if peer_data else [])


@pytest.mark.parametrize('sent', [False, True], ids=['not-sent', 'not-acked'])
def test_periph_rem_key_missing_data_before_rejection_ack_closes(peripheral, sent):
    """New peer data before the rejection is acknowledged closes the link.

    Purpose: Verify that while a key-missing rejection is still unacknowledged, a
    non-acknowledging data PDU from the peer is treated as a MIC failure and
    closes the link (5.1.3.1). Runs with the rejection not yet sent and sent but
    not acknowledged.

    Steps:
        1. The peer sends ENC_REQ; Host supplies no key.
        2. Optionally the IUT transmits REJECT_IND first.
        3. Lower Tester sends data without acknowledging.

    Expected:
        - Host gets Disconnection Complete (MIC failure).
    """
    peripheral_key_request(peripheral)
    peripheral.host('supply_key', None)
    if sent:
        peripheral.lt_rx(Opcode.LL_REJECT_IND)
    peripheral.lt_tx_data(b'data', ack=False)
    peripheral.ut_rx(DISCONNECTED, reason=MIC_FAILURE)


def test_periph_rem_key_missing_rejection_keeps_response_timer(peripheral):
    """Sending the key-missing rejection keeps the original response timer.

    Purpose: Verify that transmitting the key-missing REJECT_IND does not restart
    the procedure response timer, so T_PRT still expires on the original schedule
    and closes the link if the rejection goes unacknowledged (5.1.3.1, T_PRT).

    Steps:
        1. The peer sends ENC_REQ; advance 1 ms; Host supplies no key.
        2. Record the deadline and send REJECT_IND.
        3. Advance to just before, then to, the deadline.

    Expected:
        - The deadline is unchanged by sending the rejection.
        - The timer fires at the deadline, not one microsecond early.
        - Host gets Disconnection Complete (LMP_OR_LL_RESPONSE_TIMEOUT).
    """
    peripheral_key_request(peripheral)
    peripheral.elapse(1000)
    peripheral.host('supply_key', None)
    deadline = peripheral.encryption.deadline_us
    peripheral.lt_rx(Opcode.LL_REJECT_IND)
    assert peripheral.encryption.deadline_us == deadline
    assert not peripheral.expire_response_timer(early_us=1)
    assert peripheral.elapse(1)
    peripheral.ut_rx(DISCONNECTED, reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)


def test_periph_rem_host_never_answers(peripheral):
    """If the Host never supplies a key, the procedure times out.

    Purpose: Verify that when the Host never answers the LTK request for a
    peer-initiated start, the procedure response timer expires and closes the link
    (5.1.3, T_PRT).

    Steps:
        1. The peer sends ENC_REQ via peripheral_key_request().
        2. Simulated time advances to the response deadline.

    Expected:
        - Host gets Disconnection Complete (LMP_OR_LL_RESPONSE_TIMEOUT).
    """
    peripheral_key_request(peripheral)
    assert peripheral.expire_response_timer()
    peripheral.ut_rx(DISCONNECTED, reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)


@pytest.mark.parametrize(
    'llid,payload',
    [(LlId.START, b'data'), (LlId.CONTROL, version_ind())],
    ids=['data', 'control'],
)
def test_periph_rem_no_new_traffic_while_key_pending(peripheral, llid, payload):
    """New traffic while the key is pending closes the link and is not acked.

    Purpose: Verify that while the Peripheral is waiting for the Host's key, a new
    data or control PDU from the peer is a protocol violation that closes the link
    as a MIC failure and is never acknowledged (5.1.3.1). Runs for new data and a
    new control PDU (VERSION_IND).

    Steps:
        1. The peer sends ENC_REQ via peripheral_key_request().
        2. Record rx_nesn and have the peer send the new PDU.

    Expected:
        - Host gets Disconnection Complete (MIC failure).
        - rx_nesn is unchanged (the offending PDU is not acknowledged).
    """
    peripheral_key_request(peripheral)
    rx_nesn = peripheral.connection.event.rx_nesn
    peripheral.lt_tx_raw(llid, payload)
    peripheral.ut_rx(DISCONNECTED, reason=MIC_FAILURE)
    # The offending PDU is never acknowledged.
    assert peripheral.connection.event.rx_nesn == rx_nesn


def test_periph_rem_terminate_allowed_while_key_pending(peripheral):
    """TERMINATE_IND is still accepted while the Host key is pending.

    Purpose: Verify that, unlike other new traffic, a peer TERMINATE_IND received
    while awaiting the key is honoured and tears down the link with the peer's
    reason (5.1.3.1, 5.1.6).

    Steps:
        1. The peer sends ENC_REQ via peripheral_key_request().
        2. Lower Tester sends TERMINATE_IND (REMOTE_USER_TERMINATED).

    Expected:
        - The IUT emits only an Empty PDU (acknowledgment).
        - Host gets Disconnection Complete (REMOTE_USER_TERMINATED).
    """
    peripheral_key_request(peripheral)
    peripheral.lt_tx(
        pdu(Opcode.LL_TERMINATE_IND, ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR)
    )
    peripheral.lt_rx_empty()
    peripheral.ut_rx(
        DISCONNECTED, reason=ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    )


def test_periph_rem_second_enc_req_after_start_is_a_collision(peripheral):
    """A new ENC_REQ in the START_RSP state is a same-procedure collision.

    Purpose: Verify that once the Peripheral has sent START_ENC_REQ, a fresh
    ENC_REQ from the peer is refused as a same-procedure collision without leaving
    the START_RSP state (5.1.3, 5.3).

    Steps:
        1. The peer sends ENC_REQ; Host supplies the key; the IUT sends
           START_ENC_REQ.
        2. Feed a new ENC_REQ into the encryption procedure.

    Expected:
        - The queued reply is LL_REJECT_EXT_IND (ENC_REQ, PROCEDURE_COLLISION).
        - The encryption state stays START_RSP.
    """
    peripheral_key_request(peripheral)
    peripheral.host('supply_key', LTK)
    peripheral.install_peer_cipher()
    peripheral.lt_rx(Opcode.LL_START_ENC_REQ)
    # START_RSP state: a new ENC_REQ is refused as a same-procedure collision.
    peripheral.encryption.control(enc_req())
    assert peripheral.connection.event.queue[-1].payload == reject_ext_ind(
        Opcode.LL_ENC_REQ, PROCEDURE_COLLISION
    )
    assert peripheral.encryption.state == State.START_RSP


def test_periph_rem_enc_req_on_encrypted_link_closes(peripheral):
    """A plaintext ENC_REQ on an already encrypted link closes the link.

    Purpose: Verify that an ENC_REQ arriving in plaintext on an already encrypted
    link is a MIC failure and terminates the connection (5.1.3.2).

    Steps:
        1. Start on an encrypted link.
        2. Lower Tester sends ENC_REQ (plaintext).

    Expected:
        - Host gets Disconnection Complete (MIC failure).
    """
    peripheral.enable_encryption()
    peripheral.lt_tx(enc_req())
    peripheral.ut_rx(DISCONNECTED, reason=MIC_FAILURE)


def test_periph_host_admission(peripheral):
    """supply_key is disallowed when idle and rejects an unknown handle.

    Purpose: Verify Host admission for supply_key on the Peripheral: it is refused
    with Command Disallowed when no key request is pending, and an unknown handle
    returns Unknown Connection Identifier (5.1.3).

    Steps:
        1. Host calls supply_key with no key request pending.
        2. Call supply_key on an unknown handle (0x0EFF).

    Expected:
        - The idle supply_key returns COMMAND_DISALLOWED.
        - The unknown-handle call returns UNKNOWN_CONNECTION_IDENTIFIER.
    """
    assert peripheral.host('supply_key', LTK) == ErrorCode.COMMAND_DISALLOWED_ERROR
    unknown = peripheral.controller.le_procedures.ll.supply_key(0x0EFF, LTK)
    assert unknown == ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR


# ---------------------------------------------------------------------------
# Peripheral, remote refresh
# ---------------------------------------------------------------------------
def peripheral_pause(dut):
    dut.enable_encryption()
    dut.event_prepare()
    dut.lt_tx(pdu(Opcode.LL_PAUSE_ENC_REQ))
    dut.lt_rx(Opcode.LL_PAUSE_ENC_RSP)
    # After sending its (encrypted) response the Peripheral receives plaintext.
    assert encrypted(dut) == (True, False)
    dut.lt_tx(pdu(Opcode.LL_PAUSE_ENC_RSP))
    assert encrypted(dut) == (False, False)
    assert dut.encryption.state == State.RESTART
    dut.event_done()


def test_periph_rem_refresh(peripheral):
    """A peer-initiated key refresh completes and reports Key Refresh Complete.

    Purpose: Verify the Peripheral remote key refresh (5.1.3): after the
    PAUSE_ENC_REQ/RSP pause, a new ENC_REQ drives a fresh key request and start,
    data is paused throughout, and the result is Key Refresh Complete.

    Steps:
        1. Run peripheral_pause() to pause via PAUSE_ENC_REQ/RSP; data is paused.
        2. The peer sends ENC_REQ; the Host supplies the key.
        3. Exchange ENC_RSP, START_ENC_REQ and START_ENC_RSP.

    Expected:
        - Host gets Encryption Key Refresh Complete (SUCCESS) and nothing else.
        - Data is no longer paused afterwards.
    """
    peripheral_pause(peripheral)
    assert peripheral.data_paused()
    peripheral.event_prepare()
    peripheral.lt_tx(enc_req(RAND, EDIV))
    peripheral.ut_rx(LTK_REQUEST)
    peripheral.lt_rx(Opcode.LL_ENC_RSP)
    peripheral.host('supply_key', LTK)
    peripheral.install_peer_cipher()
    peripheral.tx_ack()
    peripheral.lt_rx(Opcode.LL_START_ENC_REQ)
    peripheral.lt_tx(pdu(Opcode.LL_START_ENC_RSP))
    peripheral.lt_rx(Opcode.LL_START_ENC_RSP)
    peripheral.tx_ack()
    peripheral.event_done()
    peripheral.ut_rx(REFRESHED, status=0)
    peripheral.ut_rx_q_is_empty()
    assert not peripheral.data_paused()


@pytest.mark.parametrize('acknowledged', [True, False], ids=['ack', 'timeout'])
def test_periph_rem_refresh_key_missing_terminates(peripheral, acknowledged):
    """A missing key during a refresh ends the link via ACL Termination.

    Purpose: Verify that after an Encryption Pause, a missing key for the refresh
    ends the link by sending TERMINATE_IND (not resuming plaintext), that the
    aborted encryption timer cannot pre-empt T_terminate, and that paused data
    never escapes (5.1.3, 5.1.6). Runs with the termination acknowledged and with
    it timing out.

    Steps:
        1. Run peripheral_pause(); the peer sends ENC_REQ; the IUT sends ENC_RSP.
        2. Answer late so the old T_PRT would nearly expire; queue data that must
           not escape; Host supplies no key.
        3. Advance past the aborted encryption deadline, then either acknowledge
           the TERMINATE_IND or let T_terminate expire.

    Expected:
        - The IUT sends LL_TERMINATE_IND (key missing) and clears the deadline.
        - The link does not close at the stale encryption deadline.
        - Host gets Disconnection Complete (PIN_OR_KEY_MISSING).
    """
    peripheral_pause(peripheral)
    peripheral.event_prepare()
    peripheral.lt_tx(enc_req())
    peripheral.ut_rx(LTK_REQUEST)
    peripheral.lt_rx(Opcode.LL_ENC_RSP)
    peripheral.tx_ack()
    procedure_deadline = peripheral.encryption.deadline_us
    # Answer late, so the old T_PRT would expire before T_terminate does.
    peripheral.elapse(procedure_deadline - 500_000 - peripheral.radio.now_us)
    peripheral.connection.enqueue_acl(False, b'must not escape')
    peripheral.host('supply_key', None)
    # After a pause, a missing key ends the link via ACL Termination.
    termination = pdu(Opcode.LL_TERMINATE_IND, KEY_MISSING)
    peripheral.lt_rx(Opcode.LL_TERMINATE_IND, termination)
    assert peripheral.encryption.deadline_us == 0
    # The aborted encryption timer cannot win against T_terminate.
    peripheral.elapse(procedure_deadline - peripheral.radio.now_us)
    assert not peripheral.connection.closed
    if acknowledged:
        peripheral.tx_ack()
    else:
        peripheral.elapse(
            peripheral.connection.termination_deadline_us - peripheral.radio.now_us
        )
    peripheral.ut_rx(DISCONNECTED, reason=KEY_MISSING)


# ---------------------------------------------------------------------------
# Delayed UNKNOWN_RSP for a Peripheral procedure sent before ENC_REQ (5.1.3.1)
# ---------------------------------------------------------------------------
def phy_then_enc_req(dut, *, extended=False):
    dut.set_peer_features(
        int(LeFeatureMask.EXTENDED_REJECT_INDICATION) if extended else 0
    )
    dut.host_start('prepare_phy', PHY_1M, PHY_1M, 0, 0)
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_PHY_REQ)
    dut.lt_tx(enc_req())
    dut.ut_rx(LTK_REQUEST)


@pytest.mark.parametrize('wrong_page', [False, True])
def test_delayed_feature_unknown_rsp_survives_ignored_page(peripheral, wrong_page):
    """An ignored feature page cannot turn a delayed UNKNOWN_RSP into a MIC failure.

    Purpose: Retain the outstanding feature exchange during Encryption Start
    (Core Vol 6, Part B, 5.1.3.1 and 5.1.4.3).

    Steps:
        1. Start Read All Remote Features and receive the IUT's page-1 request.
        2. Optionally send a wrong-page response, then send ENC_REQ.
        3. Acknowledge ENC_RSP with UNKNOWN_RSP for the feature-page request.
        4. Supply no key, receive the key-missing rejection and acknowledge it.

    Expected:
        The Host feature request completes as Unsupported Remote Feature (0x1A).
        The connection stays open, Encryption Start retains its own deadline,
        and rejecting the missing key resumes the paused data path.
    """
    peripheral.host_start('prepare_features', pages=1)
    peripheral.event_prepare()
    peripheral.lt_rx(Opcode.LL_PERIPHERAL_FEATURE_REQ)
    peripheral.lt_tx(pdu(Opcode.LL_FEATURE_RSP, (1 << 63).to_bytes(8, 'little')))
    peripheral.lt_rx(Opcode.LL_FEATURE_EXT_REQ)
    if wrong_page:
        peripheral.lt_tx(pdu(Opcode.LL_FEATURE_EXT_RSP, 2, 2, bytes(24)))
        peripheral.ut_rx_q_is_empty()
    peripheral.lt_tx(enc_req())
    peripheral.ut_rx(LTK_REQUEST)
    peripheral.lt_rx(Opcode.LL_ENC_RSP)
    deadline = peripheral.encryption.deadline_us
    peripheral.lt_tx(unknown_rsp(Opcode.LL_FEATURE_EXT_REQ))
    peripheral.ut_rx(
        hci.HCI_LE_Read_All_Remote_Features_Complete_Event,
        status=ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR,
    )
    assert not peripheral.connection.closed
    assert peripheral.encryption.deadline_us == deadline
    peripheral.connection.enqueue_acl(False, b'queued')
    peripheral.host('supply_key', None)
    peripheral.lt_rx(Opcode.LL_REJECT_IND)
    peripheral.tx_ack()
    assert not peripheral.encryption.busy
    assert peripheral.lt_rx_data().payload == b'queued'


@pytest.mark.parametrize('extended', [False, True], ids=['reject', 'reject-ext'])
@pytest.mark.parametrize('when', ['before-enc-rsp', 'after-enc-rsp', 'after-unknown'])
def test_delayed_unknown_rsp_survives_key_rejection(peripheral, extended, when):
    """A delayed UNKNOWN_RSP for a pre-ENC_REQ PHY procedure survives key rejection.

    Purpose: Verify that a PHY procedure started before ENC_REQ may be completed by
    a late UNKNOWN_RSP while encryption is being rejected for a missing key: the
    PHY completes as unsupported, the encryption response timer is untouched, the
    rejection is still sent, and paused data resumes without a stale PHY timer
    (5.1.3.1). Runs for plain/extended rejection and for supplying no key before
    ENC_RSP, after ENC_RSP, or after the UNKNOWN_RSP.

    Steps:
        1. Start PHY then receive ENC_REQ via phy_then_enc_req(); queue data.
        2. Depending on 'when', Host supplies no key before/after ENC_RSP.
        3. Lower Tester sends UNKNOWN_RSP(PHY_REQ) while acking ENC_RSP.
        4. The IUT sends the key-missing rejection; the peer acknowledges it.

    Expected:
        - The link stays open and encryption stays busy with an unchanged deadline.
        - Host gets LE PHY Update Complete (UNSUPPORTED_REMOTE_FEATURE) and the
          PHY request is cleared.
        - The rejection (REJECT_IND or REJECT_EXT_IND) is sent and, once acked,
          encryption is no longer busy and the queued data is delivered.
        - No stale PHY timer fires afterwards.
    """
    phy_then_enc_req(peripheral, extended=extended)
    peripheral.connection.enqueue_acl(False, b'queued')
    if when == 'before-enc-rsp':
        peripheral.host('supply_key', None)
    peripheral.lt_rx(Opcode.LL_ENC_RSP)
    if when == 'after-enc-rsp':
        peripheral.host('supply_key', None)
    deadline = peripheral.encryption.deadline_us
    # The Central acknowledges ENC_RSP while answering the earlier PHY_REQ.
    peripheral.lt_tx(unknown_rsp(Opcode.LL_PHY_REQ))
    assert not peripheral.connection.closed and peripheral.encryption.busy
    assert peripheral.encryption.deadline_us == deadline
    peripheral.ut_rx(
        hci.HCI_LE_PHY_Update_Complete_Event,
        status=ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR,
    )
    assert not peripheral.controls.phy.phy_request
    if when == 'after-unknown':
        peripheral.host('supply_key', None)
    peripheral.lt_rx(Opcode.LL_REJECT_EXT_IND if extended else Opcode.LL_REJECT_IND)
    peripheral.tx_ack()
    assert not peripheral.encryption.busy
    assert peripheral.lt_rx_data().payload == b'queued'
    # No stale PHY timer survives.
    assert not peripheral.elapse(deadline + 1 - peripheral.radio.now_us)


@pytest.mark.parametrize(
    'invalid', ['not-sent', 'wrong-opcode', 'wrong-length', 'wrong-llid']
)
def test_delayed_unknown_rsp_exception_is_narrow(peripheral, invalid):
    """The delayed-UNKNOWN_RSP exception admits only the exact expected PDU.

    Purpose: Verify that the narrow exception allowing a plaintext UNKNOWN_RSP
    after ENC_RSP is tightly scoped: anything but the exact UNKNOWN_RSP(PHY_REQ)
    on a control LLID after ENC_RSP is a MIC failure that closes the link
    (5.1.3.1). Runs for not-sent (no ENC_RSP yet), wrong opcode, wrong length and
    wrong LLID.

    Steps:
        1. Start PHY then receive ENC_REQ; Host supplies no key.
        2. Unless 'not-sent', the IUT sends ENC_RSP first.
        3. Lower Tester sends the malformed/early response (bad opcode, length or
           LLID) without acknowledging.

    Expected:
        - Host gets Disconnection Complete (MIC failure).
    """
    phy_then_enc_req(peripheral)
    peripheral.host('supply_key', None)
    if invalid != 'not-sent':
        # The exception only opens once ENC_RSP has been transmitted.
        peripheral.lt_rx(Opcode.LL_ENC_RSP)
    response = unknown_rsp(Opcode.LL_PHY_REQ)
    if invalid == 'wrong-opcode':
        response = unknown_rsp(Opcode.LL_VERSION_IND)
    elif invalid == 'wrong-length':
        response += b'\x00'
    llid = LlId.START if invalid == 'wrong-llid' else LlId.CONTROL
    peripheral.lt_tx_raw(llid, response, ack=False)
    peripheral.ut_rx(DISCONNECTED, reason=MIC_FAILURE)


@pytest.mark.parametrize('mode', ['plaintext', 'encrypted', 'bad-mic'])
def test_delayed_unknown_rsp_with_key_keeps_handshake(peripheral, mode):
    """A delayed UNKNOWN_RSP with a key present does not break the start handshake.

    Purpose: Verify that, with the key supplied, a late UNKNOWN_RSP for the earlier
    PHY procedure completes that procedure without disturbing the encryption start
    handshake: plaintext before START_ENC_REQ and encrypted after both complete
    the PHY, while a bad-MIC copy closes the link and preserves the PHY request
    (5.1.3.1). Runs for plaintext, encrypted and bad-MIC delivery.

    Steps:
        1. Start PHY then receive ENC_REQ; Host supplies the key; IUT sends
           ENC_RSP.
        2. For plaintext send the UNKNOWN_RSP before START_ENC_REQ; otherwise ack
           and let the IUT send START_ENC_REQ, then deliver the UNKNOWN_RSP
           encrypted or with a corrupted MIC.

    Expected:
        - Bad-MIC: Host gets Disconnection Complete (MIC failure) and the PHY
          request is preserved.
        - Otherwise: the encryption state is START_RSP, Host gets LE PHY Update
          Complete, the PHY request is cleared and data is paused.
    """
    phy_then_enc_req(peripheral)
    peripheral.host('supply_key', LTK)
    peripheral.install_peer_cipher()
    peripheral.lt_rx(Opcode.LL_ENC_RSP)
    response = unknown_rsp(Opcode.LL_PHY_REQ)
    if mode == 'plaintext':
        peripheral.lt_tx(response, encrypt=False)
    else:
        peripheral.tx_ack()
        peripheral.lt_rx(Opcode.LL_START_ENC_REQ)
        if mode == 'encrypted':
            peripheral.lt_tx(response)
        else:
            header = LlId.CONTROL | 0x04
            wire = bytearray(peripheral.peer_cipher.encrypt(header, response))
            wire[-1] ^= 1
            peripheral.connection.accept(bytes([header, len(wire)]) + wire, True)
            peripheral.ut_rx(DISCONNECTED, reason=MIC_FAILURE)
            assert peripheral.controls.phy.phy_request
            return
    assert peripheral.encryption.state == State.START_RSP
    peripheral.ut_rx(hci.HCI_LE_PHY_Update_Complete_Event)
    assert not peripheral.controls.phy.phy_request
    assert peripheral.data_paused()


# ---------------------------------------------------------------------------
# Procedures queued during encryption
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'payload',
    [version_ind(), pdu(Opcode.LL_FEATURE_RSP, bytes(8)), pdu(Opcode.LL_PING_RSP)],
    ids=['version', 'feature', 'ping'],
)
def test_other_control_waits_for_encryption(central, payload):
    """Other control PDUs queued during an Encryption Start wait for it to finish.

    Purpose: Verify that a non-encryption control PDU queued while Encryption Start
    is in progress is held back until the encryption state leaves the pausing
    phase, then sent (5.1.3.1). Runs for VERSION_IND, FEATURE_RSP and PING_RSP.

    Steps:
        1. Host starts Encryption Start and queues the control PDU.
        2. The IUT sends LL_ENC_REQ; the peer acknowledges; only Empty PDUs go out.
        3. Force the encryption state to IDLE.

    Expected:
        - While encrypting, only an Empty PDU is sent and no control PDU waits.
        - After IDLE the queued control PDU is transmitted.
    """
    central.host_start('prepare_encryption', RAND, EDIV, LTK)
    central.connection.enqueue_control(payload)
    central.event_prepare()
    central.lt_rx(Opcode.LL_ENC_REQ)
    central.tx_ack()
    central.lt_rx_empty()
    central.lt_rx_q_is_empty()
    central.encryption.state = State.IDLE
    assert central.lt_rx() == payload


def test_phy_update_completes_before_encryption(central):
    """A completed PHY Update leaves no exchange that could authorize a late RSP.

    Purpose: Verify that a fully completed PHY Update leaves no outstanding local
    exchange, so it cannot later authorize a delayed UNKNOWN_RSP exception against
    an encryption start (5.1.3.1).

    Steps:
        1. Host starts a PHY Update; the IUT sends LL_PHY_REQ.
        2. Lower Tester sends PHY_RSP; the IUT sends PHY_UPDATE_IND; the peer acks.

    Expected:
        - Host gets LE PHY Update Complete (SUCCESS).
        - No local exchange remains recorded.
    """
    central.host_start('prepare_phy', PHY_1M, PHY_1M, 0, 0)
    central.event_prepare()
    central.lt_rx(Opcode.LL_PHY_REQ)
    central.lt_tx(phy_rsp(PHY_1M, PHY_1M))
    central.lt_rx(Opcode.LL_PHY_UPDATE_IND, phy_update_ind(0, 0, 0))
    central.tx_ack()
    central.ut_rx(hci.HCI_LE_PHY_Update_Complete_Event, status=0)
    # A completed exchange cannot later authorize a delayed UNKNOWN_RSP.
    assert not central.connection.llcp.local_exchanges


@pytest.mark.parametrize(
    'state,opcode',
    [
        (State.ENC_RSP, Opcode.LL_ENC_REQ),
        (State.KEY, Opcode.LL_ENC_RSP),
        (State.START_RSP, Opcode.LL_START_ENC_REQ),
        (State.START_RSP, Opcode.LL_START_ENC_RSP),
        (State.PAUSE_RSP, Opcode.LL_PAUSE_ENC_REQ),
        (State.PAUSE_ACK, Opcode.LL_PAUSE_ENC_RSP),
    ],
)
def test_queued_pdu_restarts_timer_but_retransmission_does_not(central, state, opcode):
    """Queuing an encryption PDU (re)arms the timer; retransmission does not.

    Purpose: Verify that enqueuing an encryption-handshake PDU sets the procedure
    response deadline to now + T_PRT, while resending the same PDU does not move
    that deadline (5.1.3, T_PRT). Runs over each (state, opcode) pair of the
    encryption handshake.

    Steps:
        1. Force the encryption state and advance 100 us.
        2. Enqueue the opcode and record the deadline.
        3. Advance 100 us and mark the PDU sent twice.

    Expected:
        - The deadline equals now + 40 s when the PDU is enqueued.
        - The two retransmissions leave the deadline unchanged.
    """
    encryption = central.encryption
    encryption.state = state
    central.elapse(100)
    encryption.enqueue(opcode)
    deadline = encryption.deadline_us
    assert deadline == central.radio.now_us + 40_000_000
    central.elapse(100)
    encryption.sent(bytes([opcode]))
    encryption.sent(bytes([opcode]))
    assert encryption.deadline_us == deadline


# ---------------------------------------------------------------------------
# LE Ping and Authenticated Payload Timeout
# ---------------------------------------------------------------------------
def ping_reserve(dut):
    parameters = dut.connection.parameters
    return 2 * parameters.interval * LL_CONN_INTERVAL_UNIT_US * (parameters.latency + 1)


def test_ping_loc_before_authenticated_payload_timeout(central):
    """LE Ping fires before the authenticated payload timeout and then restarts it.

    Purpose: Verify that on an encrypted link an LE Ping is scheduled one ping
    reserve before the Authenticated Payload Timeout, the ping itself carries no
    response timer, and any authenticated packet restarts the payload timer and
    clears the pending ping (5.1.8, 5.4).

    Steps:
        1. Start on an encrypted link; read the authenticated timeout.
        2. Advance to just before, then to, the scheduled ping time.
        3. The IUT sends LL_PING_REQ; the peer answers LL_PING_RSP.

    Expected:
        - Nothing goes out before the scheduled time.
        - The ping has no response timer of its own (deadline 0).
        - After the response the ping is no longer pending and the payload timer
          is rescheduled one ping reserve before the next timeout; the Host sees
          nothing.
    """
    central.enable_encryption()
    timeout = central.encryption.auth_timeout_us
    assert central.encryption.next_deadline_us == timeout - ping_reserve(central)
    assert not central.elapse(timeout - ping_reserve(central) - 1)
    central.lt_rx_q_is_empty()
    central.elapse(1)
    # The ping request has no response timer of its own.
    assert central.encryption.deadline_us == 0
    central.event_prepare()
    central.lt_rx(Opcode.LL_PING_REQ)
    central.lt_tx(pdu(Opcode.LL_PING_RSP))
    central.event_done()
    # Any authenticated packet restarts the payload timer and ends the ping.
    assert not central.encryption.ping_pending
    assert central.encryption.next_deadline_us == (
        central.radio.now_us + timeout - ping_reserve(central)
    )
    central.ut_rx_q_is_empty()


def test_ping_unanswered_reports_timeout_and_keeps_link(central):
    """An unanswered LE Ping reports the Authenticated Payload Timeout, link up.

    Purpose: Verify that when a sent LL_PING_REQ is not answered, the Authenticated
    Payload Timeout Expired event is reported to the Host but the link is kept open
    (5.1.8, 5.4).

    Steps:
        1. Start on an encrypted link; expire the response timer to trigger a ping.
        2. The IUT sends LL_PING_REQ; the peer acknowledges but does not answer.
        3. Expire the response timer again.

    Expected:
        - Host gets Authenticated Payload Timeout Expired for the handle.
        - The link is not closed and no ping stays pending.
    """
    central.enable_encryption()
    central.expire_response_timer()
    central.event_prepare()
    central.lt_rx(Opcode.LL_PING_REQ)
    central.tx_ack()
    central.event_done()
    central.expire_response_timer()
    central.ut_rx(
        hci.HCI_Authenticated_Payload_Timeout_Expired_Event,
        connection_handle=central.handle,
    )
    assert not central.connection.closed and not central.encryption.ping_pending


@pytest.mark.parametrize('encrypt', [True, False], ids=['encrypted', 'plain'])
def test_ping_rem(peripheral, encrypt):
    """A peer LL_PING_REQ is answered with LL_PING_RSP, encrypted or plaintext.

    Purpose: Verify that the Peripheral answers a received LL_PING_REQ with
    LL_PING_RSP and surfaces nothing to the Host (5.1.8). Runs on an encrypted and
    an unencrypted link.

    Steps:
        1. Optionally start on an encrypted link.
        2. The peer sends LL_PING_REQ; the IUT answers LL_PING_RSP; the peer acks.

    Expected:
        - LL_PING_RSP is sent and nothing reaches the Host.
    """
    if encrypt:
        peripheral.enable_encryption()
    peripheral.event_prepare()
    peripheral.lt_tx(pdu(Opcode.LL_PING_REQ))
    peripheral.lt_rx(Opcode.LL_PING_RSP)
    peripheral.tx_ack()
    peripheral.event_done()
    peripheral.ut_rx_q_is_empty()


def test_unencrypted_link_has_no_ping(central):
    """An unencrypted link never schedules an LE Ping.

    Purpose: Verify that on an unencrypted link there is no Authenticated Payload
    Timeout, so no ping is ever scheduled regardless of elapsed time (5.1.8, 5.4).

    Steps:
        1. On the default (unencrypted) link, check the next deadline.
        2. Advance far past many authenticated-timeout periods.

    Expected:
        - The next deadline is 0 and no timer fires.
    """
    assert central.encryption.next_deadline_us == 0
    assert not central.elapse(10 * central.encryption.auth_timeout_us)


def test_authenticated_timeout_host_command(central):
    """Set Authenticated Payload Timeout validates value and handle.

    Purpose: Verify the Host command to set the Authenticated Payload Timeout: a
    valid value is stored, zero is rejected as an invalid parameter, and an
    unknown handle is rejected (5.4, Vol 4, Part E, 7.3.94).

    Steps:
        1. Host sets the authenticated timeout to 2 s.
        2. Host sets it to 0.
        3. Call set_authenticated_timeout on an unknown handle (0x0EFF).

    Expected:
        - The 2 s value is accepted and stored.
        - Zero returns INVALID_COMMAND_PARAMETERS.
        - The unknown handle returns UNKNOWN_CONNECTION_IDENTIFIER.
    """
    assert central.host('set_authenticated_timeout', 2_000_000) == ErrorCode.SUCCESS
    assert central.encryption.auth_timeout_us == 2_000_000
    assert (
        central.host('set_authenticated_timeout', 0)
        == ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
    )
    assert (
        central.controller.le_procedures.ll.set_authenticated_timeout(0x0EFF, 1)
        == ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
    )


def test_unsolicited_ping_rsp_is_ignored(central):
    """An unsolicited LL_PING_RSP is silently ignored.

    Purpose: Verify that a PING_RSP received without a pending ping is dropped
    without affecting the Host (5.1.8).

    Steps:
        1. Lower Tester sends LL_PING_RSP with no ping outstanding.
        2. Run one connection event.

    Expected:
        - Nothing reaches the Host.
    """
    central.lt_tx(pdu(Opcode.LL_PING_RSP))
    central.run_events(1)
    central.ut_rx_q_is_empty()


def test_unexpected_encryption_reject_is_answered_unknown(central):
    """An unexpected encryption reject is answered with LL_UNKNOWN_RSP.

    Purpose: Verify that a REJECT_EXT_IND for ENC_REQ arriving when no encryption
    procedure is outstanding is treated as an unexpected PDU and answered with
    LL_UNKNOWN_RSP, like Zephyr's ctrl_invalid (2.4.2).

    Steps:
        1. Lower Tester sends REJECT_EXT_IND(ENC_REQ, PROCEDURE_COLLISION) with no
           encryption procedure in flight.

    Expected:
        - The IUT answers LL_UNKNOWN_RSP for LL_REJECT_EXT_IND.
        - Nothing reaches the Host and encryption is not busy.
    """
    # Like Zephyr's ctrl_invalid: an unexpected PDU gets LL_UNKNOWN_RSP.
    central.lt_tx(reject_ext_ind(Opcode.LL_ENC_REQ, PROCEDURE_COLLISION))
    central.lt_rx(Opcode.LL_UNKNOWN_RSP, unknown_rsp(Opcode.LL_REJECT_EXT_IND))
    central.ut_rx_q_is_empty()
    assert not central.encryption.busy


# ---------------------------------------------------------------------------
# Retransmissions across RX security transitions (Part E, 2; 5.1.3.2)
# ---------------------------------------------------------------------------
def test_duplicate_pause_req_survives_rx_mode_switch(peripheral):
    """The Central retries its encrypted PAUSE_ENC_REQ after our RX went plain.

    Purpose: Verify that when a retransmitted PAUSE_ENC_REQ arrives after the
    Peripheral's RX has switched to plaintext, the duplicate is recognised from
    the cached snapshot and decrypted without advancing the RX counter or closing
    the link (5.1.3.2, Part E).

    Steps:
        1. Start on an encrypted link; the peer sends PAUSE_ENC_REQ; the IUT
           answers PAUSE_ENC_RSP so TX stays encrypted but RX goes plaintext.
        2. Replay the same received bytes as a duplicate through the cipher.

    Expected:
        - The duplicate decrypts to LL_PAUSE_ENC_REQ.
        - The link stays open and the RX counter is unchanged.
    """
    peripheral.enable_encryption()
    peripheral.lt_tx(pdu(Opcode.LL_PAUSE_ENC_REQ))
    peripheral.lt_rx(Opcode.LL_PAUSE_ENC_RSP)
    assert encrypted(peripheral) == (True, False)
    counter = peripheral.connection.packet_security.cipher.rx_counter
    # Same bytes retried: recognised from the cached snapshot, no new counter.
    header, wire, plaintext, _ = peripheral.connection.packet_security.last_received
    assert peripheral.encryption.receive(header, wire, duplicate=True) == plaintext
    assert not peripheral.connection.closed
    assert peripheral.connection.packet_security.cipher.rx_counter == counter
    assert plaintext == pdu(Opcode.LL_PAUSE_ENC_REQ)


def test_duplicate_authenticated_pdu_refreshes_payload_timer(central):
    """A duplicate authenticated PDU refreshes the payload timer, not delivery.

    Purpose: Verify that re-accepting a previously received authenticated data PDU
    updates the last-authenticated time (keeping the Authenticated Payload Timeout
    fresh) without re-delivering its payload to the Host (5.4, Part E).

    Steps:
        1. Start on an encrypted link; the peer sends 'first'; it is drained.
        2. Advance 1 ms, then replay the same received bytes.

    Expected:
        - last_authenticated_us becomes the current time.
        - No ACL data is delivered a second time.
    """
    central.enable_encryption()
    central.lt_tx_data(b'first')
    central.acl_received()
    central.elapse(1000)
    header, wire, _, _ = central.connection.packet_security.last_received
    central.connection.accept(bytes([header, len(wire)]) + wire, True)
    assert central.encryption.last_authenticated_us == central.radio.now_us
    assert central.acl_received() == []


def test_plaintext_duplicate_cannot_pass_as_encrypted(central):
    """A pre-encryption PDU retried after encryption starts is a MIC failure.

    Purpose: Verify that a plaintext PDU received before encryption, if replayed
    after encryption has been enabled, cannot be accepted as encrypted: it fails
    the MIC check and closes the link (5.1.3.2).

    Steps:
        1. The peer sends plaintext 'plain'; it is drained and its snapshot kept.
        2. Start encryption, restore the plaintext snapshot, and replay the bytes.

    Expected:
        - Host gets Disconnection Complete (MIC failure).
    """
    central.lt_tx_data(b'plain')
    central.acl_received()
    snapshot = central.connection.packet_security.last_received
    central.enable_encryption()
    # The key switch happened mid-retry: the plaintext snapshot is still cached.
    central.connection.packet_security.last_received = snapshot
    header, wire, _, _ = snapshot
    central.connection.accept(bytes([header, len(wire)]) + wire, True)
    central.ut_rx(DISCONNECTED, reason=MIC_FAILURE)


@pytest.mark.parametrize('fatal', [False, True])
def test_resolved_peer_encryption_failure_discards_private_addresses(central, fatal):
    """Encryption failure on a resolved peer rotates both cached RPAs.

    Purpose: Verify that when encryption fails on a connection whose peer was
    resolved, the Controller discards the local and peer RPAs for that
    resolving-list entry, because a wrong IRK can resolve a peer by accident,
    while the connection keeps its original address metadata (Vol 6, Part B,
    4.7). Covers ordinary key rejection and fatal MIC termination.

    Steps:
        1. Add the connected peer's identity to the resolving list.
        2. Generate the local RPA, read the peer RPA, and record both on the
           connection.
        3. Fail encryption with Key Missing, or terminate with MIC Failure.

    Expected:
        - The resolving list's local and peer RPAs both differ from the
          originals.
        - The connection still records the original local and peer RPAs.
    """
    privacy = central.link.privacy
    identity = central.connection.peer
    assert privacy.add(identity, bytes(range(16)), bytes(range(16, 32))) == 0
    old_local = privacy.local_address(2, hci.Address.ANY, identity)
    old_peer = privacy.read_rpa(identity, local=False)[1]
    central.connection.local_rpa = old_local
    central.connection.peer_rpa = old_peer
    if fatal:
        central.connection.complete(MIC_FAILURE)
    else:
        central.connection.encryption.finish(KEY_MISSING)
    assert privacy.read_rpa(identity, local=True)[1] != old_local
    assert privacy.read_rpa(identity, local=False)[1] != old_peer
    assert central.connection.local_rpa == old_local
    assert central.connection.peer_rpa == old_peer
