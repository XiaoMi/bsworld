# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Frame Space Update at connection-event scheduling boundaries."""

from bscontroller.linklayer import (
    PDU_DATA_LLID_MASK,
    PDU_DATA_MD_MASK,
    PDU_DATA_NESN_MASK,
    PDU_DATA_SN_MASK,
    LlId,
)
from bscontroller.linklayer.data_length import DataLengths
from bscontroller.radio.base import Phy, Received
from tests.unit.linklayer.llcp.frame_space import (
    COMPLETE,
    CP,
    ONE_MBPS,
    PC,
    UPDATE,
    request,
    response,
)
from tests.unit.linklayer.llcp.harness import ErrorCode, Opcode, hci


def negotiate(dut, spacing, space_us):
    (status,) = dut.hci(UPDATE(dut.handle, space_us, space_us, ONE_MBPS, spacing))
    assert status.status == ErrorCode.SUCCESS
    dut.ut_rx(hci.HCI_Command_Status_Event)
    dut.lt_rx(Opcode.LL_FRAME_SPACE_REQ)
    dut.lt_tx(response(space_us, ONE_MBPS, spacing))
    dut.ut_rx(COMPLETE, status=0, frame_space=space_us, spacing_types=spacing)
    dut.radio.sent.clear()


def advance(dut, **kwargs):
    connection = dut.connection
    return dut.loop.run_until_complete(
        connection.advance_to_us(connection.anchor_us, **kwargs)
    )


def receive_at(dut, start_us, header, payload=b''):
    pdu = bytes((header, len(payload))) + payload
    end_us = start_us + Phy.LE_1M.airtime_us(len(pdu))
    dut.radio.incoming.append(Received(pdu, -45, start_us, end_us, True))
    return end_us


def test_central_uses_anchor_when_pc_spacing_exceeds_interval(central):
    central.set_parameters(interval=6)
    negotiate(central, PC, 9000)

    anchors = []
    for _ in range(3):
        anchors.append(central.connection.anchor_us)
        advance(central)

    assert [start for start, *_ in central.radio.sent] == anchors


def test_central_counts_pc_spacing_once_for_the_next_tx(central):
    central.set_parameters(interval=6)
    negotiate(central, PC, 6000)
    anchor_us = central.connection.anchor_us
    header = int(LlId.CONTINUATION) | PDU_DATA_MD_MASK | PDU_DATA_SN_MASK
    reply_end_us = receive_at(central, anchor_us + 80 + 150, header)

    advance(central)

    assert [start for start, *_ in central.radio.sent] == [
        anchor_us,
        reply_end_us + 6000,
    ]


def test_long_cp_sends_acl_fragments_with_one_completion_credit(central):
    central.set_parameters(interval=6)
    lengths = central.controls.data_length
    lengths.local = DataLengths(27, 328, 251, 2120)
    lengths.remote = DataLengths(251, 2120, 27, 328)
    lengths.refresh()
    central.ut_rx(hci.HCI_LE_Data_Length_Change_Event)
    negotiate(central, CP, 6500)
    payload = bytes(range(251))
    central.connection.enqueue_acl(False, payload)

    advance(central)
    _, _, first, _ = central.radio.sent[-1]
    assert first[0] & PDU_DATA_LLID_MASK == LlId.START
    assert 0 < len(first[2:]) < len(payload)
    # Unacknowledged fragments retry with the same sequence and bytes.
    advance(central)
    assert central.radio.sent[-1][2] == first
    central.tx_ack()
    central.ut_rx_q_is_empty()

    reassembled = first[2:]
    while len(reassembled) < len(payload):
        advance(central)
        _, _, pdu, _ = central.radio.sent[-1]
        assert pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
        assert pdu[1] > 0
        reassembled += pdu[2:]
        central.tx_ack()
        if len(reassembled) < len(payload):
            central.ut_rx_q_is_empty()

    assert reassembled == payload
    (completion,) = central.controller.events
    assert isinstance(completion, hci.HCI_Number_Of_Completed_Packets_Event)
    assert completion.num_completed_packets == [1]


def test_peripheral_acks_and_defers_long_response_to_the_next_event(peripheral):
    peripheral.set_parameters(interval=80)
    connection = peripheral.connection
    anchor_us = connection.anchor_us
    requested_us = 10000
    first_end_us = receive_at(
        peripheral,
        anchor_us,
        LlId.CONTROL,
        request(requested_us, requested_us, ONE_MBPS, CP),
    )

    deadline_us = anchor_us + 5000
    assert advance(peripheral, radio_until_us=deadline_us) <= deadline_us
    [(tx_us, _, pdu, _)] = peripheral.radio.sent
    assert tx_us == first_end_us + 150
    assert pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
    assert pdu[0] & PDU_DATA_NESN_MASK
    assert pdu[1] == 0
    peripheral.ut_rx_q_is_empty()

    second_end_us = receive_at(
        peripheral,
        connection.anchor_us,
        LlId.CONTINUATION | PDU_DATA_NESN_MASK | PDU_DATA_SN_MASK,
    )
    advance(peripheral)
    assert len(peripheral.radio.sent) == 2
    tx_us, _, pdu, _ = peripheral.radio.sent[-1]
    assert tx_us == second_end_us + requested_us
    assert pdu[2:] == response(requested_us, ONE_MBPS, CP)
    peripheral.ut_rx(COMPLETE, status=0, initiator=2, frame_space=requested_us)


def test_central_budgets_new_mces_before_sending_response(central):
    central.set_parameters(interval=6)
    connection = central.connection
    anchor_us = connection.anchor_us
    spacing = UPDATE.SpacingType.T_MCES
    requested_us = 6500
    receive_at(
        central,
        anchor_us + 80 + 150,
        LlId.CONTROL | PDU_DATA_MD_MASK | PDU_DATA_NESN_MASK,
        request(requested_us, requested_us, ONE_MBPS, spacing),
    )

    advance(central)
    assert len(central.radio.sent) == 1
    assert central.radio.sent[0][0] == anchor_us
    central.ut_rx_q_is_empty()

    next_anchor_us = connection.anchor_us
    advance(central)
    assert len(central.radio.sent) == 2
    tx_us, _, pdu, _ = central.radio.sent[-1]
    assert tx_us == next_anchor_us
    assert pdu[2:] == response(requested_us, ONE_MBPS, spacing)
    central.ut_rx(COMPLETE, status=0, initiator=2, frame_space=requested_us)
