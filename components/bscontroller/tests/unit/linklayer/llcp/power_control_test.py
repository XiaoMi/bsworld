# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Power level changes on ACL and path loss zone timing."""

import pytest

from bscontroller.linklayer.power_control import (
    PathLossMonitoring,
    PathLossParameters,
    PathLossZone,
)
from bscontroller.radio.base import Phy
from tests.unit.linklayer.llcp.harness import Opcode, hci, pdu


def test_power_request_changes_the_response_transmit_level(central):
    central.event_prepare()
    central.lt_tx(pdu(Opcode.LL_POWER_CONTROL_REQ, 1, 0xF6, 0))
    central.lt_rx(
        Opcode.LL_POWER_CONTROL_RSP,
        pdu(Opcode.LL_POWER_CONTROL_RSP, 0, 0xF8, 0xF8, 0xFF),
    )
    assert central.radio.sent[-1][3]['power_dbm'] == -8
    central.tx_ack()
    central.lt_tx(pdu(Opcode.LL_POWER_CONTROL_REQ, 2, 0, 0x7F))
    central.lt_rx(
        Opcode.LL_POWER_CONTROL_RSP,
        pdu(Opcode.LL_POWER_CONTROL_RSP, 0, 0, 0x7E, 0xFF),
    )
    assert central.radio.sent[-1][3]['power_dbm'] == -8


def test_coded_power_levels_are_managed_together_but_adjusted_separately(central):
    central.connection.tx_phy = Phy.LE_CODED
    central.event_prepare()
    central.lt_tx(pdu(Opcode.LL_POWER_CONTROL_REQ, 8, 0xF8, 0))
    central.lt_rx(
        Opcode.LL_POWER_CONTROL_RSP,
        pdu(Opcode.LL_POWER_CONTROL_RSP, 0, 0xF8, 0xF8, 0xFF),
    )
    assert central.radio.sent[-1][3]['power_dbm'] == 0
    central.tx_ack()
    assert central.host('read_local_transmit_power', 3) == (0, 0, 4)
    assert central.host('read_local_transmit_power', 4) == (0, -8, 4)


def test_queued_power_indication_and_retry_use_their_reported_power(central):
    """Keep the advertised power through queueing and ACL retransmission."""
    central.event_prepare()
    central.lt_tx(pdu(Opcode.LL_POWER_CONTROL_REQ, 1, 0, 0))
    central.lt_rx(Opcode.LL_POWER_CONTROL_RSP)
    central.tx_ack()
    central.lt_rx_empty(ack=False)

    central.hci(hci.HCI_LE_Write_RF_Path_Compensation_Command(50, 0))
    central.event_done()
    central.event_prepare()
    central.lt_rx_empty(ack=False)
    central.hci(hci.HCI_LE_Write_RF_Path_Compensation_Command(-50, 0))
    central.event_done()
    central.event_prepare()
    central.tx_ack()

    initial = pdu(Opcode.LL_POWER_CHANGE_IND, 1, 0, 5, 5)
    central.lt_rx(Opcode.LL_POWER_CHANGE_IND, initial)
    assert central.radio.sent[-1][3]['power_dbm'] == 5

    central.hci(hci.HCI_LE_Write_RF_Path_Compensation_Command(90, 0))
    central.event_done()
    central.event_prepare()
    central.lt_rx(Opcode.LL_POWER_CHANGE_IND, initial)
    assert central.radio.sent[-1][3]['power_dbm'] == 5
    central.tx_ack()

    central.lt_rx(
        Opcode.LL_POWER_CHANGE_IND,
        pdu(Opcode.LL_POWER_CHANGE_IND, 1, 0, 0xF6, 0xFB),
    )
    assert central.radio.sent[-1][3]['power_dbm'] == -5
    central.tx_ack()
    central.lt_rx(
        Opcode.LL_POWER_CHANGE_IND, pdu(Opcode.LL_POWER_CHANGE_IND, 1, 0, 14, 9)
    )
    assert central.radio.sent[-1][3]['power_dbm'] == 9


def test_path_loss_requires_hysteresis_and_dwell_across_counter_wrap():
    monitoring = PathLossMonitoring()
    monitoring.configure(PathLossParameters(60, 5, 30, 5, 2))
    monitoring.set_enabled(True)

    assert monitoring.observe(24, 0xFFFC) == PathLossZone.LOW
    assert monitoring.observe(34, 0xFFFD) is None
    assert monitoring.observe(35, 0xFFFE) is None
    assert monitoring.observe(34, 0xFFFF) is None
    assert monitoring.observe(35, 0xFFFF) is None
    assert monitoring.observe(40, 0) is None
    assert monitoring.observe(40, 1) == PathLossZone.MIDDLE
    assert monitoring.observe(65, 2) is None
    assert monitoring.observe(65, 4) == PathLossZone.HIGH
    assert monitoring.observe(56, 5) is None
    assert monitoring.observe(55, 6) is None
    assert monitoring.observe(55, 8) == PathLossZone.MIDDLE


def test_path_loss_reconfiguration_applies_without_dwell():
    monitoring = PathLossMonitoring()
    monitoring.configure(PathLossParameters(60, 5, 30, 5, 100))
    monitoring.set_enabled(True)
    assert monitoring.observe(70, 0) == PathLossZone.HIGH
    monitoring.configure(PathLossParameters(255, 0, 30, 5, 100))
    assert monitoring.observe(70, 1) == PathLossZone.MIDDLE
    assert monitoring.observe(70, 2) is None
    monitoring.set_enabled(False)
    assert monitoring.observe(20, 3) is None
    monitoring.set_enabled(True)
    assert monitoring.observe(20, 4) == PathLossZone.LOW


@pytest.mark.parametrize(
    'boundary, middle, outside, zone',
    [(30, 31, 29, PathLossZone.LOW), (60, 59, 61, PathLossZone.HIGH)],
)
def test_zero_hysteresis_crosses_at_the_boundary_without_repeating_reports(
    boundary, middle, outside, zone
):
    monitoring = PathLossMonitoring()
    monitoring.configure(PathLossParameters(60, 0, 30, 0, 1))
    monitoring.set_enabled(True)

    assert monitoring.observe(middle, 0) == PathLossZone.MIDDLE
    assert monitoring.observe(boundary, 1) is None
    assert monitoring.observe(boundary, 2) == zone
    assert monitoring.observe(boundary, 3) is None
    assert monitoring.observe(boundary, 4) is None
    assert monitoring.observe(outside, 5) is None
    assert monitoring.observe(boundary, 6) is None
    assert monitoring.observe(boundary, 7) == PathLossZone.MIDDLE
    assert monitoring.observe(boundary, 8) is None
    assert monitoring.observe(boundary, 9) is None


def test_maximum_path_loss_dwell_with_every_other_event_observed():
    monitoring = PathLossMonitoring()
    monitoring.configure(PathLossParameters(60, 5, 30, 5, 0xFFFF))
    monitoring.set_enabled(True)

    assert monitoring.observe(24, 0) == PathLossZone.LOW
    for counter in range(2, 0x10002, 2):
        assert monitoring.observe(70, counter & 0xFFFF) is None
    assert monitoring.observe(70, 2) == PathLossZone.HIGH
