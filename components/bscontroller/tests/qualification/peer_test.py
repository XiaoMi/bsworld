# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Lower Tester regressions for autonomous Feature Exchange and DLU.

These exercise the shared peer against the Controller on the real PHY; the
selected LL.TS procedures remain registered in their normative chapter files.
"""

from functools import partial

import pytest

from bscontroller import hci
from bscontroller.linklayer import ControlOpcode
from tests.qualification.peer import LongControlPeer, establish
from tests.qualification.tester import (
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
)

MINIMUM = (27, 328, 27, 328)
MAXIMUM = (251, 17040, 251, 17040)


async def long_control_link(tester, *, central, delay=0, lengths=MINIMUM):
    return await establish(
        tester,
        central=central,
        extended=True,
        timeout=200,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
        peer_factory=partial(
            LongControlPeer,
            reject=True,
            features=None,
            feature_delay_us=delay,
            lengths=lengths,
        ),
    )


@pytest.mark.parametrize('delay', [0, 1_000_000])
async def test_central_feature_exchange_completes_before_inconclusive(air_dut, delay):
    """Complete the autonomous Central exchange before ending Inconclusive.

    Purpose:
        Verify both immediate completion and the one-second delay selected by
        LL.TS 4.3.7.12, with a valid Feature Response and completion event.

    Steps:
        1. Connect without Feature Exchange and request remote features by HCI
           to exercise the same LL request as an autonomous Central exchange.
        2. Serve the exchange and observe the peer's Inconclusive result.

    Expected:
        - The selected delay elapses and the IUT completes Feature Exchange
          successfully before the peer ends Inconclusive.
    """
    link = await long_control_link(air_dut, central=True, delay=delay)
    await air_dut.command(hci.HCI_LE_Read_Remote_Features_Command(link.handle))
    with pytest.raises(pytest.skip.Exception, match='Inconclusive'):
        await link.exchange()
    assert link.feature_request is not None
    assert air_dut.radio.now_us - link.feature_request >= delay
    event = await air_dut.event(hci.HCI_LE_Read_Remote_Features_Complete_Event)
    assert event.status == 0 and event.connection_handle == link.handle


async def test_peripheral_feature_exchange_is_rejected(air_dut):
    """Reject a Peripheral Feature Request when peer features are unavailable.

    Purpose:
        Verify that a Peripheral request receives LL_UNKNOWN_RSP rather than
        taking the Central's Inconclusive path.

    Steps:
        1. Connect a Peripheral IUT and trigger its Feature Request by HCI.
        2. Serve three connection events and read the completion status.

    Expected:
        - Feature Exchange fails; the ACL remains usable for disconnection.
    """
    link = await long_control_link(air_dut, central=False)
    await air_dut.command(hci.HCI_LE_Read_Remote_Features_Command(link.handle))
    for _ in range(3):
        await link.exchange()
    event = await air_dut.event(hci.HCI_LE_Read_Remote_Features_Complete_Event)
    assert event.status != 0 and link.feature_request is None
    await link.disconnect(local=True)


async def test_data_length_request_uses_current_peer_limits(air_dut):
    """Answer autonomous DLU with the configured maximum peer limits.

    Purpose:
        Verify that the peer uses its current lengths when answering an IUT
        LL_LENGTH_REQ, rather than capturing the initial minimum data limits.

    Steps:
        1. Connect with minimum limits, then select the maximum peer limits.
        2. Trigger IUT DLU with LE Set Data Length and serve its request.

    Expected:
        - Effective transmit and receive lengths both become 251 octets.
    """
    link = await long_control_link(air_dut, central=True)
    link.lengths = MAXIMUM
    await air_dut.command(hci.HCI_LE_Set_Data_Length_Command(link.handle, 251, 17040))
    await link.control(ControlOpcode.LL_LENGTH_REQ)
    for _ in range(2):
        await link.exchange()
    event = await air_dut.event(hci.HCI_LE_Data_Length_Change_Event)
    assert event.max_tx_octets == 251 and event.max_rx_octets == 251
    await link.disconnect(local=True)
