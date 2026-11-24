# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""HCI.TS.p39, Section 4.11: Controller Setup."""

import pytest

from bscontroller import hci
from tests.qualification.cases import collect_cases, procedure_cases, verify_case
from tests.qualification.peer import establish
from tests.qualification.tester import (
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
)


async def host_feature_during_connection(tester, *, central):
    """LE Set Host Feature Command During Connection.

    Initial Condition:
        The IUT and Lower Tester use public device addresses.

    Test Case Configuration:
        BV-08 uses the IUT as initiator and BV-09 uses it as advertiser.

    Test Steps:
        1. For BV-08, send LE Create Connection. The Lower Tester transmits
           ADV_IND and verifies CONNECT_IND uses the IUT public address.
        2. For BV-09, configure legacy undirected connectable advertising
           with empty advertising data. Enable advertising and send CONNECT_IND
           with the Lower Tester's public address in response to ADV_IND.
        3. Require LE Connection Complete for the established ACL.
        4. Send LE Set Host Feature with the supported Connection Subrating
           Host Support bit number and Bit_Value = 1.
        5. Require Command Complete with Command Disallowed (0x0C).

    Expected Outcome:
        The IUT rejects changing the Host feature while the ACL exists.

    Reference:
        HCI.TS.p39, 4.11, HCI/CSE/BV-08-C and BV-09-C, Figures 4.126
        and 4.127, PDF pages 265-267; Core Vol 4, Part E, 7.8.115.
    """
    await tester.masks(le=1 << (hci.HCI_LE_CONNECTION_COMPLETE_EVENT - 1))
    await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
        legacy_advertising_data=None if central else b'',
        connection_complete_event=hci.HCI_LE_Connection_Complete_Event,
    )
    await tester.command(
        hci.HCI_LE_Set_Host_Feature_Command(
            hci.LeFeature.CONNECTION_SUBRATING_HOST_SUPPORT, 1
        ),
        hci.HCI_ErrorCode.COMMAND_DISALLOWED_ERROR,
    )


# Reject a supported Host feature once the initiator or advertiser is connected.
PROCEDURES = procedure_cases(
    host_feature_during_connection,
    {
        'HCI/CSE/BV-08-C': (
            'LE Set Host Feature Command During Connection, Initiator',
            {'central': True},
        ),
        'HCI/CSE/BV-09-C': (
            'LE Set Host Feature Command During Connection, Advertiser',
            {'central': False},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES, radio_cases=PROCEDURES, radio=True),
)
async def test_controller_setup(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
