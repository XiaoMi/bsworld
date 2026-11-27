# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connection Rate negotiation, exact anchor timing, and legacy interactions."""

import struct

import pytest

from bscontroller.linklayer import LL_RESPONSE_TIMEOUT_US
from bscontroller.linklayer.connection_rate import ConnectionRateParameters
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.subrating import SubrateRequest
from bscontroller.radio.base import Phy
from tests.unit.linklayer.llcp.harness import (
    CENTRAL,
    PHY_CODED,
    ErrorCode,
    Opcode,
    connection_param_req,
    connection_update_ind,
    feature,
    hci,
    phy_rsp,
    reject_ext_ind,
    version_ind,
)

RateChange = hci.HCI_LE_Connection_Rate_Change_Event
RateDefaults = hci.HCI_LE_Set_Default_Rate_Parameters_Command
RateRequest = hci.HCI_LE_Connection_Rate_Request_Command
HOST_BIT = LeFeatureMask.SHORTER_CONNECTION_INTERVALS_HOST_SUPPORT
SUBRATING = (
    LeFeatureMask.CONNECTION_SUBRATING | LeFeatureMask.CONNECTION_SUBRATING_HOST_SUPPORT
)


def host_parameters(minimum=11, maximum=None):
    return minimum, minimum if maximum is None else maximum, 1, 1, 0, 0, 100, 1, 100


def feature_extension(opcode):
    return bytes((opcode, 1, 1)) + (0x300).to_bytes(24, 'little')


def enable_rate(iut):
    iut.link.features |= HOST_BIT | SUBRATING
    iut.set_peer_features(
        LeFeatureMask.LL_EXTENDED_FEATURE_SET
        | LeFeatureMask.CONNECTION_PARAMETERS_REQUEST_PROCEDURE
        | SUBRATING
    )
    iut.lt_tx(feature_extension(Opcode.LL_FEATURE_EXT_REQ))
    iut.lt_rx(Opcode.LL_FEATURE_EXT_RSP)
    iut.tx_ack()


def request_rate(iut, interval):
    iut.hci(RateRequest(iut.handle, *host_parameters(interval)))
    iut.ut_rx(hci.HCI_Command_Status_Event, status=ErrorCode.SUCCESS)


def rate_indication(interval, instant):
    return struct.pack(
        '<B7H', Opcode.LL_CONNECTION_RATE_IND, 0, interval, instant, 1, 0, 0, 100
    )


def test_central_discovers_the_peer_host_bit_before_scheduling_rate(central):
    central.link.features |= HOST_BIT
    connection = central.connection
    old_interval = connection.parameters.interval_us

    central.hci(RateRequest(central.handle, *host_parameters()))
    central.ut_rx(hci.HCI_Command_Status_Event, status=ErrorCode.SUCCESS)
    central.lt_rx(Opcode.LL_FEATURE_REQ)
    central.lt_tx(feature(Opcode.LL_FEATURE_RSP, LeFeatureMask.LL_EXTENDED_FEATURE_SET))
    central.lt_rx(Opcode.LL_FEATURE_EXT_REQ)
    central.lt_tx(feature_extension(Opcode.LL_FEATURE_EXT_RSP))
    payload = central.lt_rx(Opcode.LL_CONNECTION_RATE_IND)
    offset, interval, instant, factor, latency, continuation, timeout = struct.unpack(
        '<7H', payload[1:]
    )
    assert (offset, interval, factor, latency, continuation, timeout) == (
        0,
        11,
        1,
        0,
        0,
        100,
    )
    central.tx_ack()

    central.wait_instant(instant)
    assert connection.parameters.interval_us == old_interval
    anchor = connection.anchor_us
    central.event_prepare()
    central.ut_rx(RateChange, status=ErrorCode.SUCCESS, connection_interval=11)
    assert connection.parameters.interval_us == 1375
    assert connection.anchor_us == anchor
    assert connection.next_anchor_us == anchor + 1375


@pytest.mark.parametrize('peer_features_known', [True, False])
def test_peer_rate_request_waits_for_local_version(central, peer_features_known):
    """Queue the rate update and prerequisite feature discovery (6.B.5.1.33)."""
    if peer_features_known:
        enable_rate(central)
    else:
        central.link.features |= HOST_BIT | SUBRATING
        central.set_peer_features(LeFeatureMask.LL_EXTENDED_FEATURE_SET)

    central.host_start('read_remote_version')
    central.lt_rx(Opcode.LL_VERSION_IND)
    central.tx_ack()
    request = ConnectionRateParameters(60, 70)
    central.lt_tx(request.request(central.event_counter))
    central.event_prepare()
    central.lt_rx_q_is_empty()

    central.lt_tx(version_ind())
    central.event_prepare()
    if not peer_features_known:
        central.lt_rx(Opcode.LL_FEATURE_EXT_REQ)
        central.lt_tx(feature_extension(Opcode.LL_FEATURE_EXT_RSP))
    central.lt_rx(Opcode.LL_CONNECTION_RATE_IND)


def test_rate_update_recalculates_offset_at_first_transmission_across_wrap(central):
    """Rebase the peer's preferred anchor after a delayed first transmission."""
    enable_rate(central)
    central.set_parameters(interval=8)
    central.connection.event_counter = 0xFFFC
    central.lt_tx(
        struct.pack(
            '<B13H',
            Opcode.LL_CONNECTION_RATE_REQ,
            61,
            61,
            1,
            1,
            0,
            0,
            100,
            0,
            0xFFFC,
            7,
            0xFFFF,
            0xFFFF,
            0xFFFF,
        )
    )
    assert central.controls.pending.instant == 2

    central.connection.step_event(3)
    payload = central.lt_rx(Opcode.LL_CONNECTION_RATE_IND)
    # Nine old intervals of 80 ticks separate the reference and Instant.
    assert struct.unpack_from('<3H', payload, 1) == (19, 61, 5)


def test_rate_offset_and_legacy_instant_use_the_correct_time_units(peripheral):
    peripheral.link.features |= HOST_BIT
    connection = peripheral.connection
    rate_instant = (peripheral.event_counter + 6) & 0xFFFF
    peripheral.lt_tx(
        bytes((Opcode.LL_CONNECTION_RATE_IND,))
        + struct.pack(
            '<7H',
            2,
            11,
            rate_instant,
            1,
            0,
            0,
            100,
        )
    )
    peripheral.wait_instant(rate_instant)
    anchor = connection.anchor_us
    peripheral.event_prepare()
    peripheral.ut_rx(RateChange, status=ErrorCode.SUCCESS, connection_interval=11)
    assert connection.anchor_us == anchor + 250
    assert connection.next_anchor_us == anchor + 250 + 1375

    legacy_instant = (peripheral.event_counter + 6) & 0xFFFF
    peripheral.lt_tx(connection_update_ind(9, legacy_instant, offset=1))
    for _ in range(6):
        connection.step_event()
    anchor = connection.anchor_us
    peripheral.event_prepare()
    peripheral.ut_rx(
        hci.HCI_LE_Connection_Update_Complete_Event,
        status=ErrorCode.SUCCESS,
        connection_interval=9,
    )
    assert connection.parameters.rate_interval_us is None
    assert connection.anchor_us == anchor + 1250
    assert connection.next_anchor_us == anchor + 1250 + 11250


def test_default_rate_parameters_only_apply_to_new_connections(iut):
    existing = iut(CENTRAL)
    existing.hci(RateDefaults(*host_parameters(20, 60)))
    existing.ut_rx(hci.HCI_Command_Complete_Event)

    new = iut(CENTRAL)
    assert existing.controls.rate.limits.connection_interval_min == 60
    assert existing.controls.rate.limits.connection_interval_max == 32000
    assert new.controls.rate.limits.connection_interval_min == 20
    assert new.controls.rate.limits.connection_interval_max == 60


def test_central_rejects_peer_latency_outside_the_host_limits(central):
    central.link.features |= HOST_BIT
    central.set_peer_features(LeFeatureMask.LL_EXTENDED_FEATURE_SET)
    central.lt_tx(feature_extension(Opcode.LL_FEATURE_EXT_REQ))
    central.lt_rx(Opcode.LL_FEATURE_EXT_RSP)
    central.tx_ack()

    central.lt_tx(
        struct.pack(
            '<B13H',
            Opcode.LL_CONNECTION_RATE_REQ,
            60,
            60,
            1,
            1,
            1,
            0,
            100,
            0,
            0,
            *(0xFFFF,) * 4,
        )
    )
    central.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(
            Opcode.LL_CONNECTION_RATE_REQ,
            ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR,
        ),
    )
    central.ut_rx_q_is_empty()


def test_central_retains_a_working_phy_after_shortening_the_interval(central):
    enable_rate(central)
    request_rate(central, 10)
    indication = central.lt_rx(Opcode.LL_CONNECTION_RATE_IND)
    central.tx_ack()
    central.wait_instant(int.from_bytes(indication[5:7], 'little'))
    central.event_prepare()
    central.ut_rx(RateChange, status=0, connection_interval=10)

    central.hci(hci.HCI_LE_Set_PHY_Command(central.handle, 0, PHY_CODED, PHY_CODED, 2))
    central.ut_rx(hci.HCI_Command_Status_Event, status=0)
    central.lt_rx(Opcode.LL_PHY_REQ)
    central.lt_tx(phy_rsp(PHY_CODED, PHY_CODED))
    central.lt_rx(Opcode.LL_PHY_UPDATE_IND, b'\x18\x00\x00\x00\x00')
    central.ut_rx(
        hci.HCI_LE_PHY_Update_Complete_Event,
        status=0,
        tx_phy=Phy.LE_1M,
        rx_phy=Phy.LE_1M,
    )

    connection = central.connection
    anchor = connection.anchor_us
    central.loop.run_until_complete(connection.advance_to_us(anchor))
    assert central.radio.sent[-1][0] == anchor


@pytest.mark.parametrize(
    'pdu',
    [
        bytes((Opcode.LL_SUBRATE_REQ,)) + SubrateRequest(1, 1, 0, 0, 100).pack(),
        connection_param_req(8),
    ],
    ids=['subrate', 'connection-parameters'],
)
def test_central_rejects_an_incompatible_peer_request_during_rate_update(central, pdu):
    enable_rate(central)
    request_rate(central, 10)
    rate = central.lt_rx(Opcode.LL_CONNECTION_RATE_IND)
    central.lt_tx(pdu, ack=False)
    central.tx_ack()
    central.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(pdu[0], ErrorCode.DIFFERENT_TRANSACTION_COLLISION_ERROR),
    )
    central.tx_ack()
    central.wait_instant(int.from_bytes(rate[5:7], 'little'))
    central.event_prepare()
    central.ut_rx(RateChange, status=0, connection_interval=10)


@pytest.mark.parametrize('already_sent', [False, True], ids=['queued', 'acknowledged'])
def test_peripheral_distinguishes_peer_rate_update_from_local_request(
    peripheral, already_sent
):
    enable_rate(peripheral)
    request_rate(peripheral, 10)
    if already_sent:
        peripheral.lt_rx(Opcode.LL_CONNECTION_RATE_REQ)
    peripheral.lt_tx(rate_indication(40, 6))
    if not already_sent:
        peripheral.lt_rx_empty()
    peripheral.wait_instant(6)
    peripheral.event_prepare()
    peripheral.ut_rx(RateChange, status=0, connection_interval=40)
    assert peripheral.controls.rate.pending

    if not already_sent:
        peripheral.lt_rx(Opcode.LL_CONNECTION_RATE_REQ)
    peripheral.lt_tx(rate_indication(10, 12))
    peripheral.wait_instant(12)
    peripheral.event_prepare()
    peripheral.ut_rx(RateChange, status=0, connection_interval=10)
    assert not peripheral.controls.rate.pending


def test_legacy_hci_commands_are_disallowed_during_a_peer_rate_update(peripheral):
    enable_rate(peripheral)
    peripheral.lt_tx(rate_indication(40, 6))
    for command in (
        hci.HCI_LE_Connection_Update_Command(peripheral.handle, 8, 8, 0, 100, 0, 0),
        hci.HCI_LE_Subrate_Request_Command(peripheral.handle, 1, 1, 0, 0, 100),
    ):
        peripheral.hci(command)
        peripheral.ut_rx(
            hci.HCI_Command_Status_Event, status=ErrorCode.COMMAND_DISALLOWED_ERROR
        )


def test_rate_hci_command_is_disallowed_while_peer_connection_update_is_pending(
    peripheral,
):
    enable_rate(peripheral)
    peripheral.lt_tx(connection_param_req(24))
    peripheral.lt_rx(Opcode.LL_CONNECTION_PARAM_RSP)
    peripheral.tx_ack()
    peripheral.hci(RateRequest(peripheral.handle, *host_parameters(10)))
    peripheral.ut_rx(
        hci.HCI_Command_Status_Event, status=ErrorCode.COMMAND_DISALLOWED_ERROR
    )


@pytest.mark.parametrize('rate_first', [False, True])
def test_host_timing_update_commands_exclude_each_other_while_queued(
    central, rate_first
):
    enable_rate(central)
    # Hold the local slot so the first timing command is queued without a PDU.
    central.host_start('read_remote_version')
    legacy = hci.HCI_LE_Connection_Update_Command(central.handle, 8, 8, 0, 100, 0, 0)
    rate = RateRequest(central.handle, *host_parameters(10))
    subrate = hci.HCI_LE_Subrate_Request_Command(central.handle, 1, 1, 0, 0, 100)
    first, second = (rate, legacy) if rate_first else (legacy, rate)
    for command, status in (
        (first, ErrorCode.SUCCESS),
        (second, ErrorCode.COMMAND_DISALLOWED_ERROR),
        (subrate, ErrorCode.COMMAND_DISALLOWED_ERROR),
    ):
        central.hci(command)
        central.ut_rx(hci.HCI_Command_Status_Event, status=status)


def test_rate_request_response_timer_starts_at_enqueue(peripheral):
    enable_rate(peripheral)
    request_rate(peripheral, 10)
    deadline = peripheral.radio.now_us + LL_RESPONSE_TIMEOUT_US
    assert peripheral.connection.llcp.response_deadline_us == deadline
    peripheral.elapse(2000)
    request = peripheral.lt_rx(Opcode.LL_CONNECTION_RATE_REQ)
    assert peripheral.connection.llcp.response_deadline_us == deadline
    peripheral.lt_rx(Opcode.LL_CONNECTION_RATE_REQ, request)
    assert peripheral.connection.llcp.response_deadline_us == deadline

    peripheral.loop.run_until_complete(peripheral.radio.advance_to_us(deadline))
    reason = ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR
    assert peripheral.connection.llcp.check_timeout()
    peripheral.ut_rx(RateChange, status=reason)
    peripheral.ut_rx(hci.HCI_Disconnection_Complete_Event, reason=reason)
    peripheral.ut_rx_q_is_empty()
