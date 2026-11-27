# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connection Subrating state at the HCI, packet, and radio boundaries."""

import struct
from dataclasses import replace

import pytest

from bscontroller.linklayer import LL_CONN_INTERVAL_UNIT_US, LL_SCA_500_PPM
from bscontroller.linklayer.cis_parameters import event_anchor
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.roles import connection_window
from tests.unit.linklayer.llcp.harness import (
    ErrorCode,
    Opcode,
    feature,
    hci,
    unknown_rsp,
)

CONTROLLER_FEATURE = LeFeatureMask.CONNECTION_SUBRATING
HOST_FEATURE = LeFeatureMask.CONNECTION_SUBRATING_HOST_SUPPORT
REQUEST = hci.HCI_LE_Subrate_Request_Command
CHANGED = hci.HCI_LE_Subrate_Change_Event


def indication(factor, base, latency=0, continuation=0, timeout=400):
    return struct.pack(
        '<B5H', Opcode.LL_SUBRATE_IND, factor, base, latency, continuation, timeout
    )


def test_subrate_discovery_serializes_with_initial_data_length(central):
    connection = central.connection
    connection.established = False
    lengths = central.controls.data_length
    lengths.local = replace(lengths.local, tx_octets=60, tx_time=592)

    (status,) = central.hci(REQUEST(central.handle, 3, 3, 0, 1, 400))
    assert status.status == ErrorCode.SUCCESS
    central.ut_rx(hci.HCI_Command_Status_Event)
    connection.establish()

    central.lt_rx(Opcode.LL_FEATURE_REQ)
    central.lt_tx(
        feature(
            Opcode.LL_FEATURE_RSP,
            CONTROLLER_FEATURE
            | HOST_FEATURE
            | LeFeatureMask.LE_DATA_PACKET_LENGTH_EXTENSION,
        )
    )
    central.lt_rx(Opcode.LL_SUBRATE_IND)
    central.ut_rx_q_is_empty()
    central.tx_ack()
    central.ut_rx(CHANGED, status=0, subrate_factor=3)

    central.event_prepare()
    central.lt_rx(Opcode.LL_LENGTH_REQ)


def test_internal_feature_discovery_does_not_emit_a_host_features_event(central):
    (status,) = central.hci(REQUEST(central.handle, 3, 3, 0, 0, 400))
    assert status.status == ErrorCode.SUCCESS
    central.ut_rx(hci.HCI_Command_Status_Event)
    central.lt_rx(Opcode.LL_FEATURE_REQ)
    central.lt_tx(feature(Opcode.LL_FEATURE_RSP, CONTROLLER_FEATURE))
    central.ut_rx(CHANGED, status=ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)
    central.ut_rx_q_is_empty()
    central.lt_rx_q_is_empty()

    (status,) = central.hci(REQUEST(central.handle, 3, 3, 0, 0, 400))
    assert status.status == ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR


def test_subrate_request_after_unknown_response_is_rejected_locally(peripheral):
    (status,) = peripheral.hci(REQUEST(peripheral.handle, 3, 3, 0, 0, 400))
    assert status.status == ErrorCode.SUCCESS
    peripheral.ut_rx(hci.HCI_Command_Status_Event)
    peripheral.lt_rx(Opcode.LL_SUBRATE_REQ)
    peripheral.lt_tx(unknown_rsp(Opcode.LL_SUBRATE_REQ))
    peripheral.ut_rx(CHANGED, status=ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)
    peripheral.ut_rx_q_is_empty()
    (status,) = peripheral.hci(REQUEST(peripheral.handle, 3, 3, 0, 0, 400))
    assert status.status == ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
    peripheral.lt_rx_q_is_empty()


@pytest.mark.parametrize(
    'sent,acknowledged,peer_factor',
    [
        pytest.param(False, False, 5, id='request-still-queued'),
        pytest.param(True, False, 3, id='request-unacknowledged'),
        pytest.param(True, True, 5, id='update-outside-requested-range'),
    ],
)
def test_independent_peer_subrate_update_preserves_host_request(
    peripheral, sent, acknowledged, peer_factor
):
    peripheral.set_peer_features(CONTROLLER_FEATURE)
    (status,) = peripheral.hci(REQUEST(peripheral.handle, 3, 3, 0, 0, 400))
    assert status.status == ErrorCode.SUCCESS
    peripheral.ut_rx(hci.HCI_Command_Status_Event)
    if sent:
        peripheral.lt_rx(Opcode.LL_SUBRATE_REQ)

    peripheral.lt_tx(indication(peer_factor, 0), ack=acknowledged)
    if acknowledged:
        peripheral.lt_rx_empty(ack=False)
    else:
        peripheral.lt_rx(Opcode.LL_SUBRATE_REQ)
    peripheral.ut_rx(CHANGED, status=0, subrate_factor=peer_factor)

    reason = ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR
    peripheral.lt_tx(bytes((Opcode.LL_REJECT_EXT_IND, Opcode.LL_SUBRATE_REQ, reason)))
    peripheral.ut_rx(CHANGED, status=reason)
    peripheral.ut_rx_q_is_empty()


def test_unchanged_indication_completes_matching_host_request_after_ack(peripheral):
    existing = indication(3, 0)
    peripheral.lt_tx(existing)
    peripheral.lt_rx_empty()
    peripheral.ut_rx(CHANGED, status=0, subrate_factor=3)

    (status,) = peripheral.hci(REQUEST(peripheral.handle, 3, 3, 0, 0, 400))
    assert status.status == ErrorCode.SUCCESS
    peripheral.ut_rx(hci.HCI_Command_Status_Event)
    peripheral.lt_rx(Opcode.LL_SUBRATE_REQ)
    peripheral.lt_tx(existing)
    peripheral.ut_rx_q_is_empty()
    peripheral.lt_rx_empty()
    peripheral.ut_rx(CHANGED, status=0, subrate_factor=3)
    peripheral.ut_rx_q_is_empty()


def test_supervision_and_schedule_switch_at_the_indication_ack(central):
    central.set_peer_features(HOST_FEATURE)
    connection = central.connection
    central.set_parameters(interval=6, timeout=400)
    connection.event_counter = 0xFFFC
    central.hci(REQUEST(central.handle, 5, 5, 0, 0, 200))
    central.ut_rx(hci.HCI_Command_Status_Event)
    assert connection.parameters.timeout == 400
    central.lt_rx(Opcode.LL_SUBRATE_IND, indication(5, 0xFFFC, timeout=200))

    connection.step_event(5)
    assert (connection.event_index, connection.event_counter) == (0x10001, 1)
    assert connection.next_scheduled_event == 0x10001
    central.lt_rx(Opcode.LL_SUBRATE_IND, indication(5, 0xFFFC, timeout=200))
    assert connection.parameters.timeout == 400
    central.ut_rx_q_is_empty()

    central.tx_ack()
    central.ut_rx(CHANGED, status=0, subrate_factor=5, supervision_timeout=200)
    assert connection.parameters.timeout == 200
    connection.step_event()
    assert connection.next_scheduled_event == 0x10006


def test_reservation_skips_idle_anchors_and_carries_fraction_across_wrap(central):
    central.set_peer_features(HOST_FEATURE)
    central.link.sleep_clock_accuracy = LL_SCA_500_PPM
    connection = central.connection
    central.set_parameters(interval=6)
    connection.event_counter = 0xFFFE
    connection.clock_remainder = 625_000
    central.hci(REQUEST(central.handle, 7, 7, 0, 0, 400))
    central.lt_rx(Opcode.LL_SUBRATE_IND)
    central.tx_ack()
    anchor = connection.anchor_us
    connection.step_event()
    next_window = connection_window(connection)
    interval = 7 * 6 * LL_CONN_INTERVAL_UNIT_US
    expected_offset = interval + (interval * 250 + 625_000) // 1_000_000
    # LL_CIS_REQ names ACL anchors by their wrapped 16-bit event counter.
    assert event_anchor(connection, 5) == anchor + expected_offset
    connection.skip_missed_events()
    assert event_anchor(connection, 0xFFFE) == anchor
    assert connection.event_counter == 5
    assert connection.anchor_us == anchor + expected_offset
    assert next_window == connection_window(connection)
    assert (
        next_window.max_interval_us
        == interval + (interval * 250 + 999_999) // 1_000_000
    )


def test_peripheral_update_applies_before_ack_and_reports_after_ack(peripheral):
    connection = peripheral.connection
    connection.event_counter = 100
    peripheral.lt_tx(indication(5, 100, continuation=2, timeout=200))
    assert connection.parameters.timeout == 200
    peripheral.ut_rx_q_is_empty()
    peripheral.lt_rx_empty()
    peripheral.ut_rx(CHANGED, status=0, subrate_factor=5, continuation_number=2)

    connection.step_event(2)
    assert connection.next_scheduled_event == 102
    peripheral.lt_tx_data(b'corrupt', valid_crc=False)
    connection.step_event()
    assert connection.next_scheduled_event == 105


def test_host_interval_update_checks_supervision_against_active_subrate(central):
    central.set_peer_features(HOST_FEATURE)
    central.set_parameters(interval=8)
    central.hci(REQUEST(central.handle, 5, 5, 0, 0, 400))
    central.lt_rx(Opcode.LL_SUBRATE_IND)
    central.tx_ack()

    command = hci.HCI_LE_Connection_Update_Command(central.handle, 8, 8, 0, 10, 0, 0)
    (status,) = central.hci(command)
    # 100 ms is exactly twice 10 ms times five, and therefore too short.
    assert status.status == ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
    (status,) = central.hci(
        hci.HCI_LE_Connection_Update_Command(central.handle, 8, 8, 0, 11, 0, 0)
    )
    assert status.status == ErrorCode.SUCCESS


def test_unchanged_peripheral_interval_checks_active_subrate(peripheral):
    connection = peripheral.connection
    peripheral.set_parameters(interval=8)
    peripheral.lt_tx(indication(5, 0, continuation=1, timeout=200))
    peripheral.lt_rx_empty()
    peripheral.ut_rx(CHANGED, status=0, subrate_factor=5, continuation_number=1)

    # The 10 ms interval stays fixed. A supervision timeout of 100 ms is
    # invalid even at latency zero, since the factor remains five.
    update = struct.pack('<BB5H', Opcode.LL_CONNECTION_UPDATE_IND, 1, 0, 8, 0, 10, 30)
    peripheral.lt_tx(update)
    assert connection.closed


@pytest.mark.parametrize('interval', [8, 12], ids=['unchanged', 'changed'])
def test_connection_update_only_resets_continuation_when_interval_changes(
    peripheral, interval
):
    connection = peripheral.connection
    peripheral.set_parameters(interval=8)
    peripheral.lt_tx(indication(5, 0, continuation=2, timeout=200))
    peripheral.lt_rx_empty()
    peripheral.ut_rx(CHANGED, status=0, subrate_factor=5, continuation_number=2)

    instant = 6
    update = struct.pack(
        '<BB5H', Opcode.LL_CONNECTION_UPDATE_IND, 1, 0, interval, 0, 210, instant
    )
    peripheral.lt_tx(update)
    peripheral.lt_rx_empty()
    connection.step_event(instant)
    peripheral.event_prepare()
    schedule = peripheral.controls.subrating.schedule
    peripheral.ut_rx(
        hci.HCI_LE_Connection_Update_Complete_Event,
        status=0,
        connection_interval=interval,
    )
    if interval == 8:
        assert (schedule.factor, schedule.continuation) == (5, 2)
    else:
        assert (schedule.factor, schedule.continuation) == (1, 0)
    peripheral.ut_rx_q_is_empty()
