# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LE flushable ACL data (Core Vol 6, Part B, 4.5.17.1)."""

from bscontroller import hci
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import LlId

READ_TIMEOUT = hci.HCI_Read_Automatic_Flush_Timeout_Command
WRITE_TIMEOUT = hci.HCI_Write_Automatic_Flush_Timeout_Command
NON_FLUSHABLE = hci.HCI_ACL_PB_FIRST_NON_FLUSHABLE
FLUSHABLE = hci.HCI_ACL_PB_FIRST_FLUSHABLE
CONTINUATION = hci.HCI_ACL_PB_CONTINUATION


def set_timeout(iut, timeout):
    (event,) = iut.hci(WRITE_TIMEOUT(iut.handle, timeout))
    assert event.return_parameters.status == ErrorCode.SUCCESS
    assert event.return_parameters.connection_handle == iut.handle
    iut.ut_rx(hci.HCI_Command_Complete_Event)


def enqueue(iut, boundary, data):
    iut.hci(hci.HCI_AclDataPacket(iut.handle, boundary, 0, len(data), data))


def test_hci_read_and_write_automatic_flush_timeout(central):
    def read():
        (event,) = central.hci(READ_TIMEOUT(central.handle))
        central.ut_rx(hci.HCI_Command_Complete_Event)
        parameters = event.return_parameters
        assert parameters.status == ErrorCode.SUCCESS
        assert parameters.connection_handle == central.handle
        return parameters.flush_timeout

    assert read() == 0
    set_timeout(central, 0x07FF)
    assert read() == 0x07FF
    (event,) = central.hci(WRITE_TIMEOUT(central.handle, 0x0800))
    assert event.return_parameters.status == ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
    central.ut_rx(hci.HCI_Command_Complete_Event)
    assert read() == 0x07FF
    (event,) = central.hci(READ_TIMEOUT(central.handle + 1))
    assert (
        event.return_parameters.status == ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
    )
    central.ut_rx(hci.HCI_Command_Complete_Event)


def test_expiry_drops_all_queued_and_late_hci_fragments(central):
    set_timeout(central, 0x10)
    enqueue(central, NON_FLUSHABLE, b'first')
    enqueue(central, FLUSHABLE, b'flushable')
    enqueue(central, CONTINUATION, b'continuation')
    first = central.lt_rx_data()
    assert first.payload == b'first'

    central.elapse(10_000 - central.radio.now_us - 1)
    assert central.completed_packets() == 0
    central.elapse(1)
    assert central.completed_packets() == 2
    enqueue(central, CONTINUATION, b'late continuation')
    assert central.completed_packets() == 1

    enqueue(central, NON_FLUSHABLE, b'last')
    assert central.lt_rx_data().payload == b'first'
    central.tx_ack()
    assert central.lt_rx_data().payload == b'last'
    central.tx_ack()
    assert central.completed_packets() == 2


def test_first_encrypted_fragment_cancels_its_whole_pdu_timeout(central):
    central.enable_encryption()
    set_timeout(central, 0x10)
    payload = bytes(range(40))
    enqueue(central, FLUSHABLE, payload)
    first = central.lt_rx_data()
    assert 0 < len(first.payload) < len(payload)
    assert first.llid == LlId.START
    wire = central.radio.sent[-1][2][1:]

    central.elapse(20_000)
    enqueue(central, CONTINUATION, b'late continuation')
    retry = central.lt_rx_data()
    assert retry.payload == first.payload and retry.llid == first.llid
    assert central.radio.sent[-1][2][1:] == wire
    assert central.completed_packets() == 0
    central.tx_ack()

    rest = central.lt_rx_data()
    assert rest.llid == LlId.CONTINUATION
    assert first.payload + rest.payload == payload
    central.tx_ack()
    assert central.completed_packets() == 1
    assert central.lt_rx_data().payload == b'late continuation'
    central.tx_ack()
    assert central.completed_packets() == 1


def test_timeout_change_only_applies_to_new_pdus(central):
    set_timeout(central, 0x10)
    enqueue(central, FLUSHABLE, b'old')
    set_timeout(central, 0)
    enqueue(central, FLUSHABLE, b'new')
    central.elapse(10_000)
    assert central.completed_packets() == 1
    assert central.lt_rx_data().payload == b'new'
    central.tx_ack()
    assert central.completed_packets() == 1


def test_start_time_after_deadline_does_not_transmit_expired_packet(central):
    set_timeout(central, 1)
    enqueue(central, FLUSHABLE, b'drop')
    enqueue(central, NON_FLUSHABLE, b'next')
    connection = central.connection
    assert connection.next_operation_us == connection.anchor_us
    assert connection.next_acl_flush_us == 625
    central.loop.run_until_complete(connection.send(0, 625))
    assert central.radio.sent[-1][2][2:] == b'next'
    assert central.completed_packets() == 1
    central.tx_ack()
    assert central.completed_packets() == 1
