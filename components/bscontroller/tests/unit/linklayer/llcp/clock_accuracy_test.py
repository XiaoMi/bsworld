# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Sleep Clock Accuracy Update request, response, and timing state."""

from bscontroller.linklayer import LL_CONN_INTERVAL_UNIT_US
from bscontroller.linklayer.features import LeFeatureMask
from tests.unit.linklayer.llcp.harness import (
    CENTRAL,
    ErrorCode,
    Opcode,
    feature,
    hci,
    pdu,
    unknown_rsp,
    version_ind,
)

REQUEST = hci.HCI_LE_Request_Peer_SCA_Command
COMPLETE = hci.HCI_LE_Request_Peer_SCA_Complete_Event


def modify(iut, action, status=ErrorCode.SUCCESS):
    (event,) = iut.hci(hci.HCI_LE_Modify_Sleep_Clock_Accuracy_Command(action))
    assert event.return_parameters.status == status


def test_local_clock_improves_before_request_and_degrades_after_response(central):
    nominal_us = central.connection.parameters.interval * LL_CONN_INTERVAL_UNIT_US
    modify(central, 0)
    central.event_prepare()
    assert central.link.sleep_clock_accuracy == 7
    central.lt_rx(Opcode.LL_CLOCK_ACCURACY_REQ, pdu(Opcode.LL_CLOCK_ACCURACY_REQ, 7))
    central.lt_tx(pdu(Opcode.LL_CLOCK_ACCURACY_RSP, 5))
    modify(central, 0, ErrorCode.LIMIT_REACHED_ERROR)

    modify(central, 1)
    central.event_prepare()
    central.lt_rx(Opcode.LL_CLOCK_ACCURACY_REQ, pdu(Opcode.LL_CLOCK_ACCURACY_REQ, 0))
    now = central.connection.anchor_us
    central.connection.step_event()
    assert central.link.sleep_clock_accuracy == 7
    assert central.connection.anchor_us - now == nominal_us

    central.lt_tx(pdu(Opcode.LL_CLOCK_ACCURACY_RSP, 5))
    now = central.connection.anchor_us
    central.connection.step_event()
    assert central.link.sleep_clock_accuracy == 0
    assert central.connection.anchor_us - now > nominal_us


def test_peer_request_updates_receive_window_and_keeps_local_accuracy(peripheral):
    peripheral.connection.anchor_us = 400_000
    peripheral.connection.last_rx_us = 0
    peripheral.lt_tx(pdu(Opcode.LL_CLOCK_ACCURACY_REQ, 7))
    peripheral.lt_rx(Opcode.LL_CLOCK_ACCURACY_RSP, pdu(Opcode.LL_CLOCK_ACCURACY_RSP, 5))
    peripheral.tx_ack()
    peripheral.connection.last_rx_us = 0
    narrow = peripheral.connection.receive_widening_us

    peripheral.lt_tx(pdu(Opcode.LL_CLOCK_ACCURACY_REQ, 0))
    peripheral.lt_rx(Opcode.LL_CLOCK_ACCURACY_RSP, pdu(Opcode.LL_CLOCK_ACCURACY_RSP, 5))
    peripheral.connection.last_rx_us = 0
    assert peripheral.connection.receive_widening_us - narrow == 192
    assert peripheral.link.sleep_clock_accuracy == 5


def test_peer_query_reports_accuracy_from_the_response(central):
    (status,) = central.hci(REQUEST(central.handle))
    assert status.status == ErrorCode.SUCCESS
    central.event_prepare()
    central.lt_rx(Opcode.LL_CLOCK_ACCURACY_REQ, pdu(Opcode.LL_CLOCK_ACCURACY_REQ, 5))
    central.lt_tx(pdu(Opcode.LL_CLOCK_ACCURACY_RSP, 7))
    central.ut_rx(hci.HCI_Command_Status_Event)
    central.ut_rx(
        COMPLETE, status=0, connection_handle=central.handle, peer_clock_accuracy=7
    )


def test_peer_query_checks_advertised_and_unknown_support(central):
    central.set_peer_features(0)
    (status,) = central.hci(REQUEST(central.handle))
    assert status.status == ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
    central.ut_rx(hci.HCI_Command_Status_Event)
    central.lt_rx_q_is_empty()

    central.set_peer_features(None)
    central.hci(REQUEST(central.handle))
    central.event_prepare()
    central.lt_rx(Opcode.LL_CLOCK_ACCURACY_REQ)
    central.lt_tx(unknown_rsp(Opcode.LL_CLOCK_ACCURACY_REQ))
    central.ut_rx(hci.HCI_Command_Status_Event)
    central.ut_rx(
        COMPLETE, status=ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR
    )

    (status,) = central.hci(REQUEST(central.handle))
    assert status.status == ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
    modify(central, 1, ErrorCode.COMMAND_DISALLOWED_ERROR)
    central.event_prepare()
    central.lt_rx_q_is_empty()


def test_feature_exchange_waits_for_sca_with_peer_request_allowed(central):
    central.hci(REQUEST(central.handle))
    central.event_prepare()
    central.lt_rx(Opcode.LL_CLOCK_ACCURACY_REQ)
    central.tx_ack()

    (status,) = central.hci(hci.HCI_LE_Read_Remote_Features_Command(central.handle))
    assert status.status == ErrorCode.SUCCESS
    central.event_prepare()
    central.lt_rx_q_is_empty()
    central.lt_tx(pdu(Opcode.LL_CLOCK_ACCURACY_REQ, 7))
    central.lt_rx(Opcode.LL_CLOCK_ACCURACY_RSP, pdu(Opcode.LL_CLOCK_ACCURACY_RSP, 5))
    central.lt_tx(pdu(Opcode.LL_CLOCK_ACCURACY_RSP, 7))

    central.event_prepare()
    central.lt_rx(Opcode.LL_FEATURE_REQ)
    central.lt_tx(
        feature(Opcode.LL_FEATURE_RSP, LeFeatureMask.SLEEP_CLOCK_ACCURACY_UPDATES)
    )
    central.ut_rx(hci.HCI_Command_Status_Event)
    central.ut_rx(hci.HCI_Command_Status_Event)
    central.ut_rx(COMPLETE, status=0, peer_clock_accuracy=7)
    central.ut_rx(hci.HCI_LE_Read_Remote_Features_Complete_Event, status=0)


def test_sca_waits_for_version_exchange(central):
    central.hci(hci.HCI_Read_Remote_Version_Information_Command(central.handle))
    central.lt_rx(Opcode.LL_VERSION_IND)
    central.tx_ack()
    (status,) = central.hci(REQUEST(central.handle))
    assert status.status == ErrorCode.SUCCESS
    central.event_prepare()
    central.lt_rx_q_is_empty()

    central.lt_tx(version_ind())
    central.event_prepare()
    central.lt_rx(Opcode.LL_CLOCK_ACCURACY_REQ)
    central.lt_tx(pdu(Opcode.LL_CLOCK_ACCURACY_RSP, 7))
    central.ut_rx(hci.HCI_Command_Status_Event)
    central.ut_rx(hci.HCI_Command_Status_Event)
    central.ut_rx(hci.HCI_Read_Remote_Version_Information_Complete_Event, status=0)
    central.ut_rx(COMPLETE, status=0, peer_clock_accuracy=7)


def test_local_encryption_does_not_interrupt_sca(central):
    central.hci(REQUEST(central.handle))
    central.event_prepare()
    central.lt_rx(Opcode.LL_CLOCK_ACCURACY_REQ)
    central.tx_ack()
    (status,) = central.hci(
        hci.HCI_LE_Enable_Encryption_Command(central.handle, bytes(8), 0, bytes(16))
    )
    assert status.status == ErrorCode.SUCCESS
    central.event_prepare()
    central.lt_rx_q_is_empty()
    central.lt_tx(pdu(Opcode.LL_CLOCK_ACCURACY_RSP, 7))
    central.event_prepare()
    central.lt_rx(Opcode.LL_ENC_REQ)


def test_crossed_request_reaffirms_accuracy_before_downgrade(central):
    modify(central, 1)
    central.event_prepare()
    downgrade = pdu(Opcode.LL_CLOCK_ACCURACY_REQ, 0)
    central.lt_rx(Opcode.LL_CLOCK_ACCURACY_REQ, downgrade)
    central.lt_tx(pdu(Opcode.LL_CLOCK_ACCURACY_REQ, 7))
    central.lt_rx(Opcode.LL_CLOCK_ACCURACY_RSP, pdu(Opcode.LL_CLOCK_ACCURACY_RSP, 5))
    central.lt_tx(pdu(Opcode.LL_CLOCK_ACCURACY_RSP, 7))
    assert central.link.sleep_clock_accuracy == 5
    central.event_prepare()
    central.lt_rx(Opcode.LL_CLOCK_ACCURACY_REQ, downgrade)
    central.lt_tx(pdu(Opcode.LL_CLOCK_ACCURACY_RSP, 7))
    assert central.link.sleep_clock_accuracy == 0


def test_new_link_delays_pending_global_clock_downgrade(central, iut):
    modify(central, 1)
    central.event_prepare()
    request = pdu(Opcode.LL_CLOCK_ACCURACY_REQ, 0)
    response = pdu(Opcode.LL_CLOCK_ACCURACY_RSP, 5)
    central.lt_rx(Opcode.LL_CLOCK_ACCURACY_REQ, request)
    other = iut(CENTRAL)
    central.lt_tx(response)

    assert central.link.sleep_clock_accuracy == 5
    central.event_prepare()
    central.lt_rx_q_is_empty()

    other.connection.complete(ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR)
    central.event_prepare()
    central.lt_rx(Opcode.LL_CLOCK_ACCURACY_REQ, request)
    central.lt_tx(response)
    assert central.link.sleep_clock_accuracy == 0
