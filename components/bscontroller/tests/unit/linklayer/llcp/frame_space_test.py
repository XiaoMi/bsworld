# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Frame Space negotiation and the packet executor's timing view."""

import struct

import pytest

from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.radio.base import Phy
from tests.unit.linklayer.llcp.harness import (
    CENTRAL,
    PERIPHERAL,
    ErrorCode,
    Opcode,
    connection_param_rsp,
    connection_update_ind,
    hci,
    instant_of,
    reject_ext_ind,
    unknown_rsp,
)

UPDATE = hci.HCI_LE_Frame_Space_Update_Command
COMPLETE = hci.HCI_LE_Frame_Space_Update_Complete_Event
CP = UPDATE.SpacingType.T_IFS_ACL_CP
PC = UPDATE.SpacingType.T_IFS_ACL_PC
ONE_MBPS = 1
TWO_MBPS = 2


def request(minimum, maximum, phys, spacing):
    return struct.pack(
        '<BHHBH', Opcode.LL_FRAME_SPACE_REQ, minimum, maximum, phys, spacing
    )


def response(space, phys, spacing):
    return struct.pack('<BHBH', Opcode.LL_FRAME_SPACE_RSP, space, phys, spacing)


def test_current_phy_timing_only_changes_when_selected_phy_becomes_active(central):
    connection = central.connection
    (status,) = central.hci(UPDATE(central.handle, 200, 200, TWO_MBPS, CP))
    assert status.status == ErrorCode.SUCCESS
    central.ut_rx(hci.HCI_Command_Status_Event)
    central.lt_rx(Opcode.LL_FRAME_SPACE_REQ, request(200, 200, TWO_MBPS, CP))
    assert connection.llcp.rx_spacing_window_us() == (150, 150)
    central.lt_tx(response(200, TWO_MBPS, int(CP) | 0x8000))
    central.ut_rx(COMPLETE, status=0, frame_space=200, phys=TWO_MBPS, spacing_types=CP)
    assert connection.llcp.rx_spacing_window_us() == (150, 150)
    connection.rx_phy = Phy.LE_2M
    assert connection.llcp.rx_spacing_window_us() == (200, 200)
    assert connection.llcp.tx_spacing_us(None) == 150


def test_request_to_reduce_spacing_preserves_the_old_value_in_fs_max(central):
    llcp = central.connection.llcp
    central.lt_tx(request(200, 200, ONE_MBPS, CP))
    central.lt_rx(Opcode.LL_FRAME_SPACE_RSP, response(200, ONE_MBPS, CP))
    central.tx_ack()
    central.ut_rx(COMPLETE, status=0, initiator=2, frame_space=200)
    llcp.response_received(200)

    (status,) = central.hci(UPDATE(central.handle, 120, 120, ONE_MBPS, CP))
    assert status.status == ErrorCode.SUCCESS
    central.ut_rx(hci.HCI_Command_Status_Event)
    assert llcp.rx_spacing_window_us() == (200, 200)
    assert llcp.rx_spacing_window_us(Opcode.LL_FRAME_SPACE_REQ) == (120, 200)
    central.lt_rx(Opcode.LL_FRAME_SPACE_REQ, request(120, 200, ONE_MBPS, CP))
    assert llcp.rx_spacing_window_us() == (120, 200)
    central.lt_tx(response(120, ONE_MBPS, CP))
    assert llcp.rx_spacing_window_us() == (120, 120)
    central.ut_rx(COMPLETE, status=0, initiator=0, frame_space=120)


def test_responder_uses_new_tx_spacing_and_keeps_old_rx_window_until_switch(
    peripheral,
):
    llcp = peripheral.connection.llcp
    peripheral.lt_tx(request(225, 225, ONE_MBPS, CP | PC))
    assert llcp.tx_spacing_us(Opcode.LL_FRAME_SPACE_RSP) == 225
    assert llcp.tx_spacing_us(None) == 150
    peripheral.lt_rx(Opcode.LL_FRAME_SPACE_RSP, response(225, ONE_MBPS, CP | PC))
    peripheral.ut_rx(COMPLETE, status=0, initiator=2, frame_space=225)

    assert llcp.tx_spacing_us(None) == 225
    assert llcp.rx_spacing_window_us() == (150, 225)
    peripheral.tx_ack()
    llcp.response_received(150)
    assert llcp.rx_spacing_window_us() == (150, 225)
    llcp.response_received(225)
    assert llcp.rx_spacing_window_us() == (225, 225)


def test_negative_feature_page_prevents_starting_a_frame_space_request(central):
    central.set_peer_features(LeFeatureMask.LL_EXTENDED_FEATURE_SET)
    central.lt_tx(bytes((Opcode.LL_FEATURE_EXT_REQ, 1, 1)) + bytes(24))
    central.lt_rx(Opcode.LL_FEATURE_EXT_RSP)
    central.tx_ack()
    central.ut_rx_q_is_empty()

    (status,) = central.hci(UPDATE(central.handle, 200, 200, ONE_MBPS, CP))
    assert status.status == ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
    central.lt_rx_q_is_empty()


def test_frame_space_over_budget_rejects_peer_and_host_requests(central):
    central.set_parameters(interval=6)
    (status,) = central.hci(UPDATE(central.handle, 6800, 6800, ONE_MBPS, CP))
    assert status.status == ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
    central.lt_rx_q_is_empty()
    central.lt_tx(request(6800, 6800, ONE_MBPS, CP))
    central.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        bytes(
            (
                Opcode.LL_REJECT_EXT_IND,
                Opcode.LL_FRAME_SPACE_REQ,
                ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR,
            )
        ),
    )
    assert central.connection.llcp.rx_spacing_window_us() == (150, 150)


def test_frame_space_change_applies_the_negotiated_event_guard(central):
    connection = central.connection
    spacing = UPDATE.SpacingType.T_MCES
    (status,) = central.hci(UPDATE(central.handle, 1000, 1000, ONE_MBPS, spacing))
    assert status.status == ErrorCode.SUCCESS
    central.ut_rx(hci.HCI_Command_Status_Event)
    central.lt_rx(Opcode.LL_FRAME_SPACE_REQ)
    central.lt_tx(response(1000, ONE_MBPS, spacing))
    central.ut_rx(COMPLETE, status=0, frame_space=1000)
    assert connection.event_guard_us == 1000


def test_phy_update_keeps_current_pair_if_programmed_spacing_cannot_fit(central):
    central.set_parameters(interval=6)
    (status,) = central.hci(UPDATE(central.handle, 9000, 9000, TWO_MBPS, CP))
    assert status.status == ErrorCode.SUCCESS
    central.ut_rx(hci.HCI_Command_Status_Event)
    central.lt_rx(Opcode.LL_FRAME_SPACE_REQ)
    central.lt_tx(response(9000, TWO_MBPS, CP))
    central.ut_rx(COMPLETE, status=0, frame_space=9000)

    (status,) = central.hci(
        hci.HCI_LE_Set_PHY_Command(central.handle, 0, TWO_MBPS, TWO_MBPS, 0)
    )
    assert status.status == ErrorCode.SUCCESS
    central.ut_rx(hci.HCI_Command_Status_Event)
    central.lt_rx(Opcode.LL_PHY_REQ)
    central.lt_tx(bytes((Opcode.LL_PHY_RSP, TWO_MBPS, TWO_MBPS)))
    central.lt_rx(Opcode.LL_PHY_UPDATE_IND, b'\x18\x00\x00\x00\x00')
    central.ut_rx(
        hci.HCI_LE_PHY_Update_Complete_Event,
        status=0,
        tx_phy=hci.Phy.LE_1M,
        rx_phy=hci.Phy.LE_1M,
    )


def test_frame_space_update_cannot_invalidate_an_announced_phy(central):
    central.set_parameters(interval=6)
    central.hci(hci.HCI_LE_Set_PHY_Command(central.handle, 0, TWO_MBPS, TWO_MBPS, 0))
    central.ut_rx(hci.HCI_Command_Status_Event, status=0)
    central.lt_rx(Opcode.LL_PHY_REQ)
    central.lt_tx(bytes((Opcode.LL_PHY_RSP, TWO_MBPS, TWO_MBPS)))
    update = central.lt_rx(Opcode.LL_PHY_UPDATE_IND)
    central.tx_ack()

    central.lt_tx(request(9000, 9000, TWO_MBPS, CP))
    central.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(
            Opcode.LL_FRAME_SPACE_REQ,
            ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR,
        ),
    )
    central.tx_ack()
    central.wait_instant(instant_of(update))
    central.event_prepare()
    central.ut_rx(hci.HCI_LE_PHY_Update_Complete_Event, status=0, rx_phy=Phy.LE_2M)
    assert central.connection.llcp.rx_spacing_window_us() == (150, 150)


def test_frame_space_update_cannot_invalidate_an_announced_interval(central):
    central.set_parameters(interval=24)
    central.set_peer_features(LeFeatureMask.LL_EXTENDED_FEATURE_SET)
    central.host_start('prepare_parameters', 6, 6, 0, 100, valid_ce_lengths=True)
    update = central.lt_rx(Opcode.LL_CONNECTION_UPDATE_IND)
    central.tx_ack()

    central.lt_tx(request(9000, 9000, ONE_MBPS, CP))
    central.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(
            Opcode.LL_FRAME_SPACE_REQ,
            ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR,
        ),
    )
    central.tx_ack()
    central.wait_instant(instant_of(update))
    central.event_prepare()
    central.ut_rx(hci.HCI_LE_Connection_Update_Complete_Event, status=0)
    assert central.connection.parameters.interval == 6
    assert central.connection.llcp.rx_spacing_window_us() == (150, 150)


def test_queued_frame_space_reply_revalidates_a_newly_announced_interval(peripheral):
    peripheral.set_parameters(interval=24)
    llcp = peripheral.connection.llcp
    peripheral.lt_tx(request(9000, 9000, ONE_MBPS, CP))
    assert llcp.tx_spacing_us(Opcode.LL_FRAME_SPACE_RSP) == 9000

    peripheral.lt_tx(connection_update_ind(6, instant=6))
    assert llcp.tx_spacing_us(Opcode.LL_FRAME_SPACE_RSP) == 150
    peripheral.lt_rx(
        Opcode.LL_REJECT_EXT_IND,
        reject_ext_ind(
            Opcode.LL_FRAME_SPACE_REQ,
            ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR,
        ),
    )
    peripheral.tx_ack()
    peripheral.wait_instant(6)
    peripheral.event_prepare()
    peripheral.ut_rx(hci.HCI_LE_Connection_Update_Complete_Event, status=0)
    assert peripheral.connection.parameters.interval == 6
    assert llcp.tx_spacing_us(None) == 150


@pytest.mark.parametrize(
    'peer_reply',
    [connection_param_rsp(6, 10), unknown_rsp(Opcode.LL_CONNECTION_PARAM_REQ)],
    ids=['negotiated', 'fallback'],
)
def test_frame_space_update_keeps_selected_interval_in_host_range(central, peer_reply):
    central.set_parameters(interval=24)
    central.host_start('prepare_parameters', 6, 6, 0, 100, valid_ce_lengths=True)
    central.lt_rx(Opcode.LL_CONNECTION_PARAM_REQ)

    central.lt_tx(request(9000, 9000, ONE_MBPS, CP))
    central.lt_rx(Opcode.LL_FRAME_SPACE_RSP, response(9000, ONE_MBPS, CP))
    central.ut_rx(COMPLETE, status=0, initiator=2, frame_space=9000)
    central.lt_tx(peer_reply)

    central.ut_rx(
        hci.HCI_LE_Connection_Update_Complete_Event,
        status=ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR,
    )
    central.lt_rx_q_is_empty()
    assert central.connection.parameters.interval == 24


@pytest.mark.parametrize(
    'radio_until_us', [3500, None], ids=['shared-radio', 'next-anchor']
)
def test_open_frame_space_window_stops_at_radio_deadline(central, radio_until_us):
    connection = central.connection
    central.set_parameters(interval=6)
    (status,) = central.hci(UPDATE(central.handle, 150, 10000, ONE_MBPS, CP))
    assert status.status == ErrorCode.SUCCESS

    anchor = connection.next_anchor_us
    deadline = radio_until_us or anchor - connection.event_guard_us
    reached = central.loop.run_until_complete(
        connection.advance_to_us(connection.anchor_us, radio_until_us=radio_until_us)
    )

    assert reached == deadline
    assert connection.anchor_us == anchor
    assert central.controls.frame_space.pending


@pytest.mark.parametrize('role', [CENTRAL, PERIPHERAL], ids=['central', 'peripheral'])
def test_peer_request_precedes_unsent_local_frame_space_request(iut, role):
    link = iut(role)
    (status,) = link.hci(UPDATE(link.handle, 200, 200, ONE_MBPS, CP))
    assert status.status == ErrorCode.SUCCESS
    link.ut_rx(hci.HCI_Command_Status_Event)

    link.lt_tx(request(225, 225, ONE_MBPS, CP))
    link.lt_rx(Opcode.LL_FRAME_SPACE_RSP, response(225, ONE_MBPS, CP))
    link.ut_rx(COMPLETE, status=0, initiator=2, frame_space=225)
    link.tx_ack()

    link.lt_rx(Opcode.LL_FRAME_SPACE_REQ, request(200, 225, ONE_MBPS, CP))
    link.lt_tx(response(200, ONE_MBPS, CP))
    link.ut_rx(COMPLETE, status=0, initiator=0, frame_space=200)


@pytest.mark.parametrize(
    'phys, spacing',
    [(ONE_MBPS, CP), (TWO_MBPS, PC), (ONE_MBPS | TWO_MBPS, CP), (TWO_MBPS, CP | PC)],
)
def test_peer_unsupported_rejection_only_caches_the_selected_pair(
    central, phys, spacing
):
    unsupported = ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
    (status,) = central.hci(UPDATE(central.handle, 200, 200, TWO_MBPS, CP))
    assert status.status == ErrorCode.SUCCESS
    central.ut_rx(hci.HCI_Command_Status_Event)
    central.lt_rx(Opcode.LL_FRAME_SPACE_REQ)
    central.lt_tx(reject_ext_ind(Opcode.LL_FRAME_SPACE_REQ, unsupported))
    central.ut_rx(COMPLETE, status=unsupported, initiator=0)

    (status,) = central.hci(UPDATE(central.handle, 225, 225, TWO_MBPS, CP))
    assert status.status == unsupported
    central.ut_rx(hci.HCI_Command_Status_Event)
    central.lt_rx_q_is_empty()

    (status,) = central.hci(UPDATE(central.handle, 225, 225, phys, spacing))
    assert status.status == ErrorCode.SUCCESS
    central.lt_rx(Opcode.LL_FRAME_SPACE_REQ, request(225, 225, phys, spacing))


def test_peer_unsupported_rejection_does_not_infer_unsupported_pairs(central):
    unsupported = ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
    (status,) = central.hci(UPDATE(central.handle, 200, 200, TWO_MBPS, CP | PC))
    assert status.status == ErrorCode.SUCCESS
    central.ut_rx(hci.HCI_Command_Status_Event)
    central.lt_rx(Opcode.LL_FRAME_SPACE_REQ)
    central.lt_tx(reject_ext_ind(Opcode.LL_FRAME_SPACE_REQ, unsupported))
    central.ut_rx(COMPLETE, status=unsupported, initiator=0)

    (status,) = central.hci(UPDATE(central.handle, 225, 225, TWO_MBPS, CP))
    assert status.status == ErrorCode.SUCCESS
    central.lt_rx(Opcode.LL_FRAME_SPACE_REQ, request(225, 225, TWO_MBPS, CP))
