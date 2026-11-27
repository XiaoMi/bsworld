# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Data Length Update procedure and packet limits (Core Vol 6, Part B, 4.5.10,
5.1.9).

Matrix: {Central, Peripheral} x {local, remote} x {accepted, unknown, rejected,
invalid, timeout, collision} plus effective-limit rules across PHYs/intervals.
Scenarios drive the LLCP harness (llcp/harness.py); pure bound checks use
``valid_data_length`` and ``DataLengths`` directly.
"""

import struct
from dataclasses import replace

import pytest

from bscontroller.linklayer import (
    LL_RESPONSE_TIMEOUT_US,
    PDU_DATA_MIC_SIZE,
    PDU_DATA_PAYLOAD_SIZE_MAX,
    PDU_DATA_PAYLOAD_SIZE_MIN,
    PDU_DATA_PAYLOAD_TIME_CODED_MAX_US,
    PDU_DATA_PAYLOAD_TIME_CODED_MIN_US,
    PDU_DATA_PAYLOAD_TIME_MIN_US,
    PDU_DATA_PAYLOAD_TIME_UNCODED_MAX_US,
    PDU_HEADER_SIZE,
    LlId,
)
from bscontroller.linklayer.data_length import (
    DataLengthProcedure,
    DataLengths,
    valid_data_length,
)
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.instant import InstantUpdate
from bscontroller.radio.base import (
    PHY_CODED_S2_FACTOR,
    PHY_CODED_S8_FACTOR,
    Phy,
    Received,
)
from tests.unit.linklayer.llcp.harness import (
    CENTRAL,
    PHY_1M,
    ROLES,
    ErrorCode,
    Opcode,
    hci,
    length,
    phy_req,
    phy_update_ind,
    reject_ext_ind,
    unknown_rsp,
)

CHANGED = hci.HCI_LE_Data_Length_Change_Event
DISCONNECTED = hci.HCI_Disconnection_Complete_Event
MAX, MAX_TIME = PDU_DATA_PAYLOAD_SIZE_MAX, PDU_DATA_PAYLOAD_TIME_CODED_MAX_US
MIN, MIN_TIME = PDU_DATA_PAYLOAD_SIZE_MIN, PDU_DATA_PAYLOAD_TIME_MIN_US
INITIAL = DataLengths(MIN, MIN_TIME, MIN, MIN_TIME)


def lengths(dut):
    return dut.controls.data_length


def negotiate(dut, *, rx=MAX, rx_time=MAX_TIME, tx=MAX, tx_time=MAX_TIME):
    """Local request answered by the peer; returns the procedure object."""
    assert dut.host('set_data_length', MAX, MAX_TIME) == ErrorCode.SUCCESS
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_LENGTH_REQ)
    dut.lt_tx(length(Opcode.LL_LENGTH_RSP, rx, rx_time, tx, tx_time))
    dut.event_done()
    return lengths(dut)


@pytest.mark.parametrize('encrypted', [False, True])
@pytest.mark.parametrize(
    'phy,coding',
    [
        (Phy.LE_1M, PHY_CODED_S8_FACTOR),
        (Phy.LE_2M, PHY_CODED_S8_FACTOR),
        (Phy.LE_CODED, PHY_CODED_S2_FACTOR),
        (Phy.LE_CODED, PHY_CODED_S8_FACTOR),
    ],
)
def test_packet_limit_matches_on_air_byte_boundaries(central, phy, coding, encrypted):
    """Check both sides of payload boundaries, including the MIC on nonempty PDUs."""
    procedure = lengths(central)
    airtimes = [
        phy.airtime_us(
            PDU_HEADER_SIZE + count + (PDU_DATA_MIC_SIZE if encrypted and count else 0),
            coding,
        )
        for count in range(MAX + 1)
    ]
    for cap in (0, 1, MIN, MAX):
        for count in (0, 1, MIN, MAX - 1, MAX):
            for limit_us in (airtimes[count] - 1, airtimes[count], airtimes[count] + 1):
                if limit_us < airtimes[0]:
                    with pytest.raises(StopIteration):
                        procedure.packet_limit(
                            phy, limit_us, cap, coding, encrypted=encrypted
                        )
                else:
                    expected = max(
                        size for size in range(cap + 1) if airtimes[size] <= limit_us
                    )
                    assert (
                        procedure.packet_limit(
                            phy, limit_us, cap, coding, encrypted=encrypted
                        )
                        == expected
                    )


# ---------------------------------------------------------------------------
# Local
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('role', ROLES)
def test_loc(iut, role):
    """A local Data Length Update sets per-direction effective limits.

    Purpose: Verify that a Host set_data_length sends LENGTH_REQ with the local
    maximums, applies each direction's sender/receiver minimum from the peer's
    LENGTH_RSP, reports the change once and does not re-report when nothing
    changes (Vol 6, Part B, 4.5.10, 5.1.9), for both roles.

    Steps:
        1. Upper Tester calls set_data_length(200, 1700).
        2. Lower Tester receives LENGTH_REQ and answers with asymmetric limits.
        3. Call refresh() once the exchange has completed.

    Expected:
        - LENGTH_REQ carries (MAX, MAX_TIME, 200, 1700); the response timer is set.
        - Effective limits become the per-direction minima.
        - Host gets one Data Length Change event; no event is emitted on refresh.
    """
    dut = iut(role)
    assert lengths(dut).effective() == INITIAL
    assert dut.host('set_data_length', 200, 1700) == ErrorCode.SUCCESS
    assert (
        dut.connection.llcp.deadline_for(lengths(dut).context) == LL_RESPONSE_TIMEOUT_US
    )
    dut.event_prepare()
    sent = dut.lt_rx(Opcode.LL_LENGTH_REQ)
    assert struct.unpack('<HHHH', sent[1:]) == (MAX, MAX_TIME, 200, 1700)
    # Asymmetric peer limits: each direction uses the sender/receiver minimum.
    dut.lt_tx(length(Opcode.LL_LENGTH_RSP, 100, 1000, 220, 1500))
    dut.event_done()
    assert lengths(dut).effective() == DataLengths(220, 1500, 100, 1000)
    dut.ut_rx(
        CHANGED,
        max_tx_octets=100,
        max_tx_time=1000,
        max_rx_octets=220,
        max_rx_time=1500,
    )
    dut.ut_rx_q_is_empty()
    assert (
        not lengths(dut).pending
        and dut.connection.llcp.deadline_for(lengths(dut).context) is None
    )
    # No new event when nothing changes.
    lengths(dut).refresh()
    dut.ut_rx_q_is_empty()


@pytest.mark.parametrize('role', ROLES)
def test_loc_without_effective_change_has_no_event(iut, role):
    """A completed exchange that changes no effective limit emits no event.

    Purpose: Verify that when the negotiated limits leave the effective minimums
    unchanged, no Data Length Change event is produced, for both roles.

    Steps:
        1. Negotiate a Data Length Update whose RSP keeps the minimum limits.

    Expected:
        - No Host notification is produced.
    """
    dut = iut(role)
    negotiate(dut, rx=MIN, rx_time=MIN_TIME, tx=MIN, tx_time=MIN_TIME)
    dut.ut_rx_q_is_empty()


@pytest.mark.parametrize(
    'response',
    [
        unknown_rsp(Opcode.LL_LENGTH_REQ),
        reject_ext_ind(
            Opcode.LL_LENGTH_REQ, ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR
        ),
    ],
    ids=['unknown', 'reject'],
)
@pytest.mark.parametrize('role', ROLES)
def test_loc_refused_falls_back_to_initial(iut, role, response):
    """A refused Data Length Update falls back to the initial limits.

    Purpose: Verify that when the peer answers LENGTH_REQ with UNKNOWN_RSP or
    REJECT_EXT_IND, the procedure falls back to the initial limits with no Host
    event, yet a later request can still time out and close the link, for each
    refusal kind and both roles.

    Steps:
        1. Upper Tester calls set_data_length(MAX, MAX_TIME).
        2. Lower Tester receives LENGTH_REQ and replies with the refusal PDU.
        3. Start a second request and expire its response timer.

    Expected:
        - No request stays pending; effective limits equal the initial values.
        - No Host event and nothing queued after the refusal.
        - The second request's timeout yields Disconnection Complete.
    """
    dut = iut(role)
    assert dut.host('set_data_length', MAX, MAX_TIME) == ErrorCode.SUCCESS
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_LENGTH_REQ)
    dut.lt_tx(response)
    dut.event_done()
    assert not lengths(dut).pending and lengths(dut).effective() == INITIAL
    dut.ut_rx_q_is_empty()
    dut.lt_rx_q_is_empty()
    # A later timeout of a new request still closes the link.
    assert dut.host('set_data_length', MAX, MAX_TIME) == ErrorCode.SUCCESS
    assert dut.expire_response_timer()
    dut.ut_rx(DISCONNECTED, reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)


def test_loc_unrelated_rejection_is_ignored(central):
    """A rejection for another procedure does not end the Data Length Update.

    Purpose: Verify that an UNKNOWN_RSP naming a different opcode (LL_PHY_REQ) is
    not taken as the answer to the outstanding LENGTH_REQ, which stays pending.

    Steps:
        1. Upper Tester calls set_data_length(MAX, MAX_TIME).
        2. Lower Tester receives LENGTH_REQ.
        3. Lower Tester sends UNKNOWN_RSP(LL_PHY_REQ).

    Expected:
        - The Data Length request is still pending.
    """
    assert central.host('set_data_length', MAX, MAX_TIME) == ErrorCode.SUCCESS
    central.event_prepare()
    central.lt_rx(Opcode.LL_LENGTH_REQ)
    central.lt_tx(unknown_rsp(Opcode.LL_PHY_REQ))
    central.event_done()
    assert lengths(central).pending


@pytest.mark.parametrize('role', ROLES)
def test_loc_response_timeout(iut, role):
    """A Data Length Update response timeout disconnects the link.

    Purpose: Verify that when the peer acknowledges LENGTH_REQ but never answers,
    the procedure timer expiry closes the connection with the response-timeout
    reason, for both roles.

    Steps:
        1. Upper Tester calls set_data_length(MAX, MAX_TIME).
        2. Lower Tester acknowledges LENGTH_REQ but sends no response.
        3. Advance to just before, then past, the procedure deadline.

    Expected:
        - No timeout fires one microsecond early; it fires at the deadline.
        - Host gets Disconnection Complete (response timeout).
    """
    dut = iut(role)
    assert dut.host('set_data_length', MAX, MAX_TIME) == ErrorCode.SUCCESS
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_LENGTH_REQ)
    dut.tx_ack()
    dut.event_done()
    assert not dut.expire_response_timer(early_us=1)
    assert dut.elapse(1)
    dut.ut_rx(DISCONNECTED, reason=ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)


@pytest.mark.parametrize(
    'arguments,status',
    [
        ((26, 328), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        ((252, 328), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        ((27, 327), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        ((27, 17041), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
    ],
)
def test_host_admission_invalid(central, arguments, status):
    """Invalid set_data_length arguments are rejected with no PDU on air.

    Purpose: Verify that out-of-range octet/time arguments are rejected at
    admission with Invalid Command Parameters and never produce a LENGTH_REQ,
    across several boundary-violating argument pairs.

    Steps:
        1. Upper Tester calls set_data_length with the invalid arguments.

    Expected:
        - The call returns the expected error status.
        - No control PDU is queued for the air.
    """
    assert central.host('set_data_length', *arguments) == status
    central.lt_rx_q_is_empty()


def test_host_admission_busy_and_unsupported(central):
    """A concurrent or unknown-handle Data Length request is refused.

    Purpose: Verify that a second set_data_length while one is outstanding is
    disallowed, and that the call on a non-existent handle fails with Unknown
    Connection Identifier.

    Steps:
        1. Upper Tester calls set_data_length(MAX, MAX_TIME).
        2. Upper Tester calls set_data_length again on the same handle.
        3. Call set_data_length on handle 0x0EFF.

    Expected:
        - The second call returns Command Disallowed.
        - The unknown-handle call returns Unknown Connection Identifier.
    """
    assert central.host('set_data_length', MAX, MAX_TIME) == ErrorCode.SUCCESS
    assert (
        central.host('set_data_length', MAX, MAX_TIME)
        == ErrorCode.COMMAND_DISALLOWED_ERROR
    )
    other = central.controller.le_procedures.ll
    assert (
        other.set_data_length(0x0EFF, MAX, MAX_TIME)
        == ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
    )


def test_host_admission_known_unsupported_peer(central):
    """A request to a peer known to lack the feature is refused at admission.

    Purpose: Verify that when the peer's features are cached and lack Data
    Length Extension, set_data_length is refused with Unsupported Remote Feature.

    Steps:
        1. Cache empty peer features (feature unsupported).
        2. Upper Tester calls set_data_length(MAX, MAX_TIME).

    Expected:
        - The call returns Unsupported Remote Feature.
    """
    central.set_peer_features(0)
    assert (
        central.host('set_data_length', MAX, MAX_TIME)
        == ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
    )


@pytest.mark.parametrize('peer_coded', [False, True])
def test_loc_request_times_respect_known_peer_phy_features(central, peer_coded):
    """LENGTH_REQ times are capped by the peer's known PHY support.

    Purpose: Verify that the requested max times reflect the peer's cached PHY
    features: the coded maximum only when the peer supports coded PHY, otherwise
    the uncoded maximum (Vol 6, Part B, 4.5.10), for peer coded and non-coded.

    Steps:
        1. Cache peer features with Data Length Extension, with or without coded.
        2. Upper Tester calls set_data_length(MAX, MAX_TIME).
        3. Lower Tester receives LENGTH_REQ.

    Expected:
        - The request times equal the coded or uncoded maximum per the feature.
    """
    central.set_peer_features(
        int(LeFeatureMask.LE_DATA_PACKET_LENGTH_EXTENSION)
        | (int(LeFeatureMask.LE_CODED_PHY) if peer_coded else 0)
    )
    central.host('set_data_length', MAX, MAX_TIME)
    central.event_prepare()
    sent = central.lt_rx(Opcode.LL_LENGTH_REQ)
    time = MAX_TIME if peer_coded else PDU_DATA_PAYLOAD_TIME_UNCODED_MAX_US
    assert struct.unpack('<HHHH', sent[1:]) == (MAX, time, MAX, time)


# ---------------------------------------------------------------------------
# Remote
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('role', ROLES)
def test_rem(iut, role):
    """A peer LENGTH_REQ is answered with the full receive capability.

    Purpose: Verify that a peer-initiated Data Length Update is answered with
    LENGTH_RSP advertising the IUT's full receive capability, updates the
    effective receive limit and reports the change, for both roles.

    Steps:
        1. Lower Tester sends LENGTH_REQ with MAX octets.
        2. Lower Tester receives the IUT's LENGTH_RSP and acknowledges.

    Expected:
        - LENGTH_RSP's receive fields are (MAX, MAX_TIME).
        - The effective receive octets become MAX.
        - Host gets Data Length Change (rx MAX, tx MIN); no timer remains.
    """
    dut = iut(role)
    dut.event_prepare()
    dut.lt_tx(length(Opcode.LL_LENGTH_REQ, MAX, 2120, MAX, 2120))
    dut.event_done()
    dut.event_prepare()
    response = dut.lt_rx(Opcode.LL_LENGTH_RSP)
    # The responder advertises its full receive capability.
    assert struct.unpack('<HHHH', response[1:])[:2] == (MAX, MAX_TIME)
    dut.tx_ack()
    dut.event_done()
    assert lengths(dut).effective().rx_octets == MAX
    dut.ut_rx(CHANGED, max_rx_octets=MAX, max_tx_octets=MIN)
    dut.ut_rx_q_is_empty()
    assert dut.connection.llcp.deadline_for(lengths(dut).context) is None


@pytest.mark.parametrize(
    'fields',
    [(26, 328, 27, 328), (27, 327, 27, 328), (27, 328, 252, 328), (0, 0, 0, 0)],
)
@pytest.mark.parametrize('opcode', [Opcode.LL_LENGTH_REQ, Opcode.LL_LENGTH_RSP])
def test_rem_invalid_values_are_rejected(central, opcode, fields):
    """An out-of-range LENGTH_REQ or LENGTH_RSP is rejected, state unchanged.

    Purpose: Verify that a LENGTH_REQ or LENGTH_RSP carrying values outside the
    allowed ranges is answered with REJECT_EXT_IND (invalid parameters) and does
    not alter the stored remote limits, across several malformed field sets.

    Steps:
        1. Record the current remote limits.
        2. Lower Tester sends the malformed PDU with the given opcode.
        3. Lower Tester receives the IUT's rejection.

    Expected:
        - The IUT transmits REJECT_EXT_IND(opcode, invalid parameters).
        - The remote limits are unchanged.
    """
    before = lengths(central).remote
    central.event_prepare()
    central.lt_tx(length(opcode, *fields))
    central.event_done()
    central.event_prepare()
    central.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(opcode, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR),
    )
    assert lengths(central).remote == before


@pytest.mark.parametrize('opcode', [Opcode.LL_LENGTH_REQ, Opcode.LL_LENGTH_RSP])
@pytest.mark.parametrize('pending', [False, True])
@pytest.mark.parametrize('size', [0, 9])
def test_wrong_length_is_unknown_and_preserves_state(central, opcode, pending, size):
    """A wrong-length LENGTH PDU is answered UNKNOWN_RSP, state preserved.

    Purpose: Verify that a LENGTH_REQ or LENGTH_RSP whose body has the wrong size
    is answered with UNKNOWN_RSP without changing any local/remote/reported
    state, whether or not a local request is pending, across both opcodes and
    both body sizes.

    Steps:
        1. Optionally start a local request and clear its queued PDU.
        2. Capture the full procedure state.
        3. Lower Tester sends the opcode with a wrong-length body.
        4. Lower Tester receives the IUT's response.

    Expected:
        - The IUT transmits UNKNOWN_RSP for the opcode.
        - The captured procedure state is unchanged.
        - No Host notification is produced.
    """
    procedure = lengths(central)
    if pending:
        central.host('set_data_length', MAX, MAX_TIME)
        central.connection.event.queue.clear()
    before = (
        procedure.local,
        procedure.remote,
        procedure.reported,
        procedure.pending,
        central.connection.llcp.deadline_for(procedure.context),
    )
    central.event_prepare()
    central.lt_tx(bytes([opcode]) + bytes(size))
    central.event_done()
    central.event_prepare()
    central.lt_rx(Opcode.LL_UNKNOWN_RSP, unknown_rsp(opcode))
    assert (
        procedure.local,
        procedure.remote,
        procedure.reported,
        procedure.pending,
        central.connection.llcp.deadline_for(procedure.context),
    ) == before
    central.ut_rx_q_is_empty()


def test_unsolicited_response_is_ignored(central):
    """An unsolicited LENGTH_RSP is ignored.

    Purpose: Verify that a LENGTH_RSP that answers no outstanding request leaves
    the effective limits at their initial values and produces no Host event.

    Steps:
        1. Lower Tester sends LENGTH_RSP with the maximum limits.

    Expected:
        - Effective limits remain the initial values.
        - No Host notification is produced.
    """
    central.event_prepare()
    central.lt_tx(length(Opcode.LL_LENGTH_RSP, MAX, MAX_TIME, MAX, MAX_TIME))
    central.event_done()
    assert lengths(central).effective() == INITIAL
    central.ut_rx_q_is_empty()


# ---------------------------------------------------------------------------
# Collision
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('role', ROLES)
def test_collision_loc_and_rem(iut, role):
    """Both sides request at once: each answers the other and both complete.

    Purpose: Verify that when the local and a peer Data Length Update collide,
    the IUT prioritizes its response ahead of its queued request, then completes
    its own request on the following event, for both roles.

    Steps:
        1. Upper Tester calls set_data_length(MAX, MAX_TIME).
        2. Lower Tester sends LENGTH_REQ without acknowledging ours.
        3. Lower Tester receives the IUT's LENGTH_RSP and acknowledges.
        4. Lower Tester receives the IUT's own LENGTH_REQ and answers it.

    Expected:
        - The response is prioritized ahead of the queued request.
        - A Data Length Change event is reported.
        - After the answer, nothing stays pending and no timer remains.
    """
    dut = iut(role)
    dut.host('set_data_length', MAX, MAX_TIME)
    dut.event_prepare()
    dut.lt_tx(length(Opcode.LL_LENGTH_REQ, MAX, MAX_TIME, MAX, MAX_TIME), ack=False)
    # The response is prioritized ahead of the queued request.
    dut.lt_rx(Opcode.LL_LENGTH_RSP)
    dut.tx_ack()
    dut.event_done()
    assert lengths(dut).pending
    dut.ut_rx(CHANGED)
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_LENGTH_REQ)
    dut.lt_tx(length(Opcode.LL_LENGTH_RSP, MAX, MAX_TIME, MAX, MAX_TIME))
    dut.event_done()
    assert (
        not lengths(dut).pending
        and dut.connection.llcp.deadline_for(lengths(dut).context) is None
    )


# ---------------------------------------------------------------------------
# Initial procedure on connection establishment (4.5.10)
# ---------------------------------------------------------------------------
def test_suggested_defaults_are_captured_at_connection_creation(central):
    """A connection captures the link's suggested defaults at creation.

    Purpose: Verify that the Host's suggested default TX octets/time are read
    into a connection's local limits when its DataLengthProcedure is constructed
    (Vol 6, Part B, 4.5.10), independent of any already running connection.

    Steps:
        1. Set the link's default TX octets and time to the maximum.
        2. Construct a fresh DataLengthProcedure for the connection.

    Expected:
        - The existing procedure's local TX octets remain MIN.
        - The fresh procedure's local TX octets/time equal the defaults.
    """
    link = central.link
    link.default_tx_octets, link.default_tx_time = MAX, MAX_TIME
    assert lengths(central).local.tx_octets == MIN
    fresh = DataLengthProcedure(central.connection, central.controls.frame_space)
    assert (fresh.local.tx_octets, fresh.local.tx_time) == (MAX, MAX_TIME)


def test_nondefault_preference_starts_on_establishment(iut):
    """A non-default length preference auto-starts the procedure on establish.

    Purpose: Verify that when the link's default TX preference differs from the
    minimum, establishing a connection queues an initial LENGTH_REQ advertising
    the full receive capability (Vol 6, Part B, 5.1.9).

    Steps:
        1. Set the link's default TX octets and time to the maximum.
        2. Build and establish a fresh Central connection.

    Expected:
        - The data-length procedure is pending.
        - The first queued PDU is LENGTH_REQ(MAX, MAX_TIME, MAX, MAX_TIME).
    """
    dut = iut(CENTRAL)
    dut.link.default_tx_octets, dut.link.default_tx_time = MAX, MAX_TIME
    connection = dut.connection.__class__(
        dut.link, dut.connection.parameters, dut.connection.peer, CENTRAL, 0
    )
    connection.establish()
    assert connection.controls.data_length.pending
    # A request advertises the full receive capability (5.1.9).
    assert connection.event.queue[0].payload == length(
        Opcode.LL_LENGTH_REQ, MAX, MAX_TIME, MAX, MAX_TIME
    )


# ---------------------------------------------------------------------------
# Effective limits (4.5.10) as seen by packet execution
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('phy,expected', [(Phy.LE_1M, 31), (Phy.LE_2M, 71)])
def test_time_limit_can_be_tighter_than_octet_limit(central, phy, expected):
    """The TX payload limit can be bounded by time rather than octets.

    Purpose: Verify that when the receive time is at the minimum, the TX payload
    limit is derived from the air time on the current PHY, giving a per-PHY cap
    tighter than the octet limit (Vol 6, Part B, 4.5.10), for 1M and 2M.

    Steps:
        1. Negotiate a Data Length Update with the minimum receive time.
        2. Set the connection TX PHY.

    Expected:
        - tx_payload_limit() equals the expected octet count for the PHY.
        - That count is the largest whose air time fits the minimum time.
    """
    procedure = negotiate(central, rx_time=MIN_TIME)
    central.connection.tx_phy = phy
    assert procedure.tx_payload_limit() == expected
    assert (
        phy.airtime_us(expected + PDU_HEADER_SIZE)
        <= MIN_TIME
        < phy.airtime_us(expected + PDU_HEADER_SIZE + 1)
    )


def test_coded_limits_follow_interval_and_pending_update(central):
    """Coded TX limits track the interval and any pending Instant update.

    Purpose: Verify that on the coded PHY the effective TX time is bounded by the
    connection interval, and that a pending connection or PHY update changes the
    prospective TX payload limit accordingly (Vol 6, Part B, 4.5.10).

    Steps:
        1. Negotiate a Data Length Update and set both PHYs to coded.
        2. Widen the interval, then stage a pending interval-change update.
        3. Stage a pending remote PHY update to 1M with current TX on 2M.

    Expected:
        - The coded TX time is capped by the short interval, then reaches MAX_TIME
          once the interval widens.
        - A pending interval update yields tx_payload_limit() 186.
        - A pending remote 1M update with reduced remote rx_time yields 31.
    """
    procedure = negotiate(central)
    connection = central.connection
    connection.tx_phy = connection.rx_phy = Phy.LE_CODED
    assert procedure.effective().tx_time == 12660
    central.set_parameters(interval=48)
    assert procedure.effective().tx_time == MAX_TIME
    connection.controls.schedule_instant(
        InstantUpdate(6, parameters=replace(connection.parameters, interval=24))
    )
    assert procedure.tx_payload_limit() == 186
    # Received (remote) update: this Central will transmit on 1M from the Instant.
    connection.controls.schedule_instant(InstantUpdate(6, central_tx_phy=Phy.LE_1M))
    connection.tx_phy = Phy.LE_2M
    procedure.remote = replace(procedure.remote, rx_time=MIN_TIME)
    assert procedure.tx_payload_limit() == 31


def test_coded_receive_time_never_below_coded_minimum(central):
    """Coded receive time is floored at the coded minimum.

    Purpose: Verify that on the coded PHY the effective receive time never drops
    below the coded minimum even when the negotiated TX time is the (uncoded)
    minimum (Vol 6, Part B, 4.5.10).

    Steps:
        1. Negotiate a Data Length Update with the minimum TX time.
        2. Set the connection RX PHY to coded.

    Expected:
        - The effective RX time equals the coded minimum payload time.
    """
    procedure = negotiate(central, tx_time=MIN_TIME)
    central.connection.rx_phy = Phy.LE_CODED
    assert procedure.effective().rx_time == PDU_DATA_PAYLOAD_TIME_CODED_MIN_US


def test_central_prospective_phy_limit_starts_at_indication(central):
    """A Central's prospective TX limit tightens once UPDATE_IND is prepared.

    Purpose: Verify that a staged local PHY update does not reduce the TX
    payload limit until the UPDATE_IND is actually prepared, after which the
    slower PHY's tighter limit applies (Vol 6, Part B, 4.5.10).

    Steps:
        1. Negotiate a Data Length Update and set both PHYs to 2M.
        2. Stage a local pending PHY update to 1M.
        3. Prepare the UPDATE_IND for 1M.

    Expected:
        - Before preparing, tx_payload_limit() stays at 32.
        - After preparing the UPDATE_IND, it tightens to 31.
    """
    procedure = negotiate(central, rx=32, rx_time=MIN_TIME)
    connection = central.connection
    connection.tx_phy = connection.rx_phy = Phy.LE_2M
    connection.controls.schedule_instant(
        InstantUpdate(6, central_tx_phy=Phy.LE_1M, local=True)
    )
    assert procedure.tx_payload_limit() == 32
    connection.controls.prepare(phy_update_ind(PHY_1M, PHY_1M, 6))
    assert procedure.tx_payload_limit() == 31


@pytest.mark.parametrize('local', [False, True])
def test_peripheral_prospective_limit_tracks_request_response(peripheral, local):
    """A Peripheral's prospective TX limit tracks a PHY request/response.

    Purpose: Verify that a Peripheral's TX payload limit tightens from the moment
    it transmits a PHY_REQ (local) or PHY_RSP (remote) that offers a slower PHY,
    and relaxes again when the update ends with no change, for both origins.

    Steps:
        1. Negotiate a Data Length Update and set both PHYs to 2M.
        2. Start a local PHY Update or receive a peer PHY_REQ.
        3. Transmit the PHY_REQ or PHY_RSP, then receive UPDATE_IND (0, 0, 0).

    Expected:
        - Before transmission, tx_payload_limit() stays at 32.
        - After the request/response is sent, it tightens to 31.
        - After the no-change update, it relaxes back to 32.
    """
    procedure = negotiate(peripheral, rx=32, rx_time=MIN_TIME)
    connection = peripheral.connection
    connection.tx_phy = connection.rx_phy = Phy.LE_2M
    if local:
        peripheral.host_start('request_phy', PHY_1M, PHY_1M, 0)
        opcode = Opcode.LL_PHY_REQ
    else:
        peripheral.event_prepare()
        peripheral.lt_tx(phy_req(PHY_1M, PHY_1M))
        opcode = Opcode.LL_PHY_RSP
    assert procedure.tx_payload_limit() == 32
    peripheral.lt_rx(opcode)
    # From the request/response transmission, a slower offered PHY may follow.
    assert procedure.tx_payload_limit() == 31
    peripheral.lt_tx(phy_update_ind(0, 0, 0))
    assert procedure.tx_payload_limit() == 32


def test_acl_fragments_complete_only_after_final_ack(central):
    """A fragmented ACL packet yields one credit only after the last ACK.

    Purpose: Verify that a Host ACL packet larger than the TX octet limit is
    fragmented at that limit with MD set on all but the last fragment, and that
    exactly one completed-packet credit is reported after the final ACK
    (Vol 4, Part E, 7.7.19).

    Steps:
        1. Negotiate a 100-octet TX limit.
        2. Enqueue one ACL packet of MAX bytes.
        3. Receive and acknowledge each fragment in turn.

    Expected:
        - No credit is reported until all fragments are acknowledged.
        - Fragment sizes are [100, 100, 51] with START/CONTINUATION LLIDs.
        - MD is set on all but the last fragment; the bytes reassemble to data.
        - Exactly one completed-packet credit is reported at the end.
    """
    negotiate(central, rx=100)
    data = bytes(range(MAX))
    central.connection.enqueue_acl(False, data)
    fragments = []
    for _ in range(3):
        fragments.append(central.lt_rx_data())
        assert central.completed_packets() == 0
        central.tx_ack()
    assert [len(item.payload) for item in fragments] == [100, 100, 51]
    assert [item.llid for item in fragments] == [
        LlId.START,
        LlId.CONTINUATION,
        LlId.CONTINUATION,
    ]
    assert [item.md for item in fragments] == [True, True, False]
    assert b''.join(item.payload for item in fragments) == data
    # One HCI packet, one credit (Vol 4, Part E, 7.7.19).
    assert central.completed_packets() == 1


def test_retransmission_is_not_resized_after_length_reduction(central):
    """A retransmitted fragment keeps its original size after a length cut.

    Purpose: Verify that reducing the data length mid-flight does not resize or
    re-split an already transmitted fragment on retransmission; only the MD bit
    may change because a LENGTH_REQ is now queued.

    Steps:
        1. Negotiate a Data Length Update and enqueue an ACL packet of MAX bytes.
        2. Transmit the first fragment.
        3. Request a reduction to the minimum length.
        4. Transmit again (a retransmission).

    Expected:
        - The retransmitted fragment has the same LLID and payload bytes.
        - Its length stays MAX and its MD bit is set.
    """
    negotiate(central)
    central.connection.enqueue_acl(False, bytes(MAX))
    first = central.lt_rx_data()
    lengths(central).request(MIN, MIN_TIME)
    again = central.lt_rx_data()
    # Same SN, same bytes; only MD may change because LENGTH_REQ is now queued.
    assert (again.llid, again.payload) == (first.llid, first.payload)
    assert len(again.payload) == MAX and again.md


def test_receive_time_limit_rejects_oversized_packet(central):
    """A received packet exceeding the receive time limit is dropped.

    Purpose: Verify that a packet whose air time exceeds the effective receive
    time limit is not acknowledged and is not delivered to the Host
    (Vol 6, Part B, 4.5.10).

    Steps:
        1. Set the local receive limit to the minimum time.
        2. Inject an incoming ACL packet longer than that time allows.
        3. Run one receive window.

    Expected:
        - The receive NESN is not advanced (the packet is not acknowledged).
        - No ACL data is delivered to the Host.
    """
    lengths(central).local = DataLengths(MAX, MIN_TIME, MIN, MIN_TIME)
    packet = Received(b'\x02\x20' + bytes(32), -45, 1, 337, True)
    central.radio.incoming.append(packet)
    central.loop.run_until_complete(central.connection.receive(7, 1000))
    assert central.connection.event.rx_nesn == 0
    assert central.acl_received() == []


def test_receive_octet_limit_rejects_oversized_payload(central):
    """A received payload exceeding the receive octet limit is dropped.

    Purpose: Verify that a data PDU longer than the effective receive octet
    limit is not acknowledged and is not delivered to the Host
    (Vol 6, Part B, 4.5.10).

    Steps:
        1. Lower Tester transmits a data payload of MIN+1 octets.

    Expected:
        - The receive NESN is not advanced (the packet is not acknowledged).
        - No ACL data is delivered to the Host.
    """
    central.lt_tx_data(bytes(MIN + 1))
    assert central.connection.event.rx_nesn == 0
    assert central.acl_received() == []


@pytest.mark.parametrize(
    'octets,time_us,valid',
    [
        (27, 328, True),
        (251, 17040, True),
        (26, 328, False),
        (252, 328, False),
        (27, 327, False),
        (27, 17041, False),
    ],
)
def test_bounds_do_not_trade_octets_for_time(octets, time_us, valid):
    """valid_data_length enforces octet and time bounds independently.

    Purpose: Verify that valid_data_length accepts only pairs where both the
    octet count and the time are within their own ranges, rejecting any pair that
    violates either bound, across several boundary pairs.

    Steps:
        1. Call valid_data_length(octets, time_us) for each parametrized pair.

    Expected:
        - The result matches the expected validity flag.
    """
    assert valid_data_length(octets, time_us) is valid
