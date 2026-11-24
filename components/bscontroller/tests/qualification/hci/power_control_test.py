# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""HCI.TS.p39, Section 4.14: LE Power Control."""

import pytest

from bscontroller import hci
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.peer import establish

ErrorCode = hci.HCI_ErrorCode


async def expect_remote_power_read_error(tester, handle, phy, error):
    """Require either permitted placement of the remote power-read error."""
    status = await tester.command(
        hci.HCI_LE_Read_Remote_Transmit_Power_Level_Command(handle, phy),
        (error, ErrorCode.SUCCESS),
    )
    if status.status == ErrorCode.SUCCESS:
        report = await tester.event(hci.HCI_LE_Transmit_Power_Reporting_Event)
        assert report.status == error


async def enhanced_read_transmit_power(tester):
    """LE Enhanced Read Transmit Power Level.

    Initial Condition:
        Establish an ACL with the IUT as Central or Peripheral. Select a
        Peripheral IUT and LE 1M.

    Test Steps:
        1. Send LE Enhanced Read Transmit Power Level with the current ACL
           handle and LE 1M PHY.
        2. Receive Command Complete with Current_TX_Power_Level and
           Max_TX_Power_Level for the selected ACL and PHY.

    Expected Outcome:
        Require success, the current connection handle and active PHY, and
        power levels within -127 to 20 dBm with current no higher than maximum.

    Reference:
        HCI.TS.p39, Section 4.14, HCI/PCL/BV-01-C, Figure 4.143,
        PDF pages 292-293; Core Vol 4, Part E, Section 7.8.117.
    """
    link = await establish(tester)
    result = await tester.command(
        hci.HCI_LE_Enhanced_Read_Transmit_Power_Level_Command(link.handle, 1)
    )
    assert result.connection_handle == link.handle
    assert result.phy == 1
    assert -127 <= result.current_tx_power_level <= result.max_tx_power_level <= 20
    await link.disconnect()


async def enhanced_read_power_unknown_handle(tester):
    """LE Enhanced Read Transmit Power Level with an invalid connection handle.

    Initial Condition:
        Establish a LE 1M ACL with the IUT as Peripheral. Select an unused ACL
        handle in the valid handle range.

    Test Steps:
        1. Send LE Enhanced Read Transmit Power Level with the unused handle.
        2. Receive Command Complete with Unknown Connection Identifier (0x02).

    Expected Outcome:
        The command returns the status in the selected row of Table 4.89.

    Reference:
        HCI.TS.p39, Section 4.14.1, HCI/PCL/BI-04-C, Table 4.89,
        PDF page 293; Core Vol 4, Part E, Section 7.8.117.
    """
    link = await establish(tester)
    await tester.command(
        hci.HCI_LE_Enhanced_Read_Transmit_Power_Level_Command(link.handle + 1, 1),
        ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR,
    )
    await link.disconnect()


async def read_remote_power_unknown_handle(tester):
    """LE Read Remote Transmit Power Level with an invalid connection handle.

    Initial Condition:
        Establish a LE 1M ACL with the IUT as Peripheral. Select an unused ACL
        handle in the valid range and unmask LE Transmit Power Reporting.

    Test Steps:
        1. Send LE Read Remote Transmit Power Level with the unused handle.
        2. Receive either Command Status with error (2A), or successful
           Command Status followed by LE Transmit Power Reporting with error (2B).

    Expected Outcome:
        Require Unknown Connection Identifier (0x02) in the selected event.

    Reference:
        HCI.TS.p39, Section 4.14.2, HCI/PCL/BI-08-C, Table 4.90,
        PDF page 294; Core Vol 4, Part E, Section 7.8.118.
    """
    link = await establish(tester)
    await expect_remote_power_read_error(
        tester, link.handle + 1, 1, ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
    )
    await link.disconnect()


PROCEDURES: ProcedureMap = {}

# Check the local power levels and active PHY on an established ACL.
PROCEDURES |= procedure_cases(
    enhanced_read_transmit_power,
    {'HCI/PCL/BV-01-C': ('LE Enhanced Read Transmit Power Level', {})},
)

# Section 4.14.1 selects an unknown ACL handle for the synchronous read.
PROCEDURES |= procedure_cases(
    enhanced_read_power_unknown_handle,
    {
        'HCI/PCL/BI-04-C': (
            'Invalid LE Enhanced Read Transmit Power Level Parameters, '
            'Connection_Handle, Not the current ACL, 0x02',
            {},
        ),
    },
)

# Section 4.14.2 selects an unknown ACL handle for the asynchronous read.
PROCEDURES |= procedure_cases(
    read_remote_power_unknown_handle,
    {
        'HCI/PCL/BI-08-C': (
            'Invalid LE Read Remote Transmit Power Level Parameters, '
            'Connection_Handle, Not the current ACL, 0x02',
            {},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_power_control(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
