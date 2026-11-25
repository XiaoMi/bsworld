# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Privacy address selection and HCI/RF boundaries (Core Vol 4, Part E)."""

import pytest

from bscontroller import hci
from tests.unit.helpers import extended_parameters
from tests.unit.linklayer.air.advertising import (
    configure_legacy,
    enable_legacy,
    scan_legacy,
)
from tests.unit.linklayer.air.harness import PEER, adv_direct_ind, connect_ind

HOST_RPA = hci.Address('70:81:94:0D:FB:AA')


def test_host_supplied_rpa_is_not_a_controller_generated_local_rpa(iut):
    """A Host-supplied random address is not reported as a local RPA.

    Purpose: Verify that a connection accepted with Own_Address_Type 1 reports
    an all-zero Local_Resolvable_Private_Address in the Enhanced Connection
    Complete event, even when the Host's random address looks resolvable
    (Vol 4, Part E, 7.7.65.10).

    Steps:
        1. Upper Tester sets the random address to a resolvable-looking value.
        2. Upper Tester configures legacy advertising with Own_Address_Type 1
           and enables it.
        3. Lower Tester sends CONNECT_IND to that address on channel 37.
        4. Run one advertising event.

    Expected:
        - LE Enhanced Connection Complete reports an all-zero local RPA and
          the Lower Tester address as the peer address.
        - The new connection uses the Host-supplied address as its own address.
    """
    iut.ut(hci.HCI_LE_Set_Random_Address_Command(HOST_RPA))
    configure_legacy(iut, own=1)
    enable_legacy(iut)
    iut.lt_tx(connect_ind(advertiser=HOST_RPA), channel=37)
    iut.run(1)
    event = iut.ut_rx(hci.HCI_LE_Enhanced_Connection_Complete_Event)
    assert bytes(event.local_resolvable_private_address) == bytes(6)
    assert event.peer_address == PEER
    assert next(iter(iut.link.connections.values())).self_address == HOST_RPA


def test_random_address_set_after_scan_parameters_applies_on_enable(iut):
    """The random address set before scan enable is the one the scanner uses.

    Purpose: Verify that a random address set after LE Set Scan Parameters
    applies when scanning is enabled, and that it cannot be changed while
    scanning (Vol 4, Part E, 7.8.4).

    Steps:
        1. Upper Tester sets active scan parameters with Own_Address_Type 1.
        2. Upper Tester sets the random address.
        3. Upper Tester enables scanning.
        4. Upper Tester sets a different random address while scanning.

    Expected:
        - The scanner uses the random address set in step 2.
        - Step 4 returns Command Disallowed and the scanner address is
          unchanged.
    """
    iut.ut(hci.HCI_LE_Set_Scan_Parameters_Command(1, 16, 16, 1, 0))
    iut.ut(hci.HCI_LE_Set_Random_Address_Command(HOST_RPA))
    iut.ut(hci.HCI_LE_Set_Scan_Enable_Command(1, 0))
    assert iut.link.scan.address == HOST_RPA
    iut.ut(hci.HCI_LE_Set_Random_Address_Command(PEER), 0x0C)
    assert iut.link.scan.address == HOST_RPA


def test_failed_multi_set_enable_does_not_generate_private_addresses(iut):
    """A rejected multi-set enable leaves every set disabled and no RPA.

    Purpose: Verify that LE Set Extended Advertising Enable checks every set
    before changing any advertising or RPA state, so a failure on one set
    does not generate a local RPA for another (Vol 4, Part E, 7.8.56).

    Steps:
        1. Upper Tester adds the Lower Tester to the resolving list with a
           local IRK.
        2. Upper Tester configures set 0 with Own_Address_Type 2 directed at
           the Lower Tester, which could generate an RPA.
        3. Upper Tester configures set 1 with Own_Address_Type 1 and no
           random address.
        4. Upper Tester enables sets 0 and 1 in one command.
        5. Upper Tester reads the local resolvable address for the peer.

    Expected:
        - The enable returns Invalid HCI Command Parameters.
        - Neither set is enabled.
        - The local resolvable address read back is all zeros.
    """
    iut.ut(
        hci.HCI_LE_Add_Device_To_Resolving_List_Command(
            1,
            PEER,
            bytes(16),
            bytes(range(16)),
        )
    )
    iut.ut(
        extended_parameters(
            advertising_handle=0,
            own_address_type=2,
            peer_address=PEER,
            peer_address_type=1,
        )
    )
    iut.ut(extended_parameters(advertising_handle=1, own_address_type=1))
    iut.ut(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0, 1], [0, 0], [0, 0]),
        0x12,
    )
    assert not any(item.enabled for item in iut.link.advertisers.values())
    result = iut.ut(hci.HCI_LE_Read_Local_Resolvable_Address_Command(1, PEER))
    assert bytes(result.local_resolvable_address) == bytes(6)


@pytest.mark.parametrize('policy', [0, 2, 3])
def test_unresolved_directed_rpa_uses_legacy_directed_report(iut, policy):
    """An unresolved directed RPA is reported only under policies 2 and 3.

    Purpose: Verify that a legacy scanner reports an ADV_DIRECT_IND whose
    TargetA is an unresolvable RPA through the LE Directed Advertising Report
    event only with scanning filter policy 2 or 3, carrying a random direct
    address type (Vol 4, Part E, 7.7.65.11).

    Steps:
        1. For policy 3, Upper Tester adds the Lower Tester to the Filter
           Accept List.
        2. Upper Tester starts scanning with the parametrized filter policy.
        3. Lower Tester sends ADV_DIRECT_IND with an RPA TargetA on channel 37.
        4. Run the scanner.

    Expected:
        - Policy 0: no event reaches the Host.
        - Policies 2 and 3: one LE Directed Advertising Report with event type
          ADV_DIRECT_IND, the Lower Tester address, direct address type
          Random and the RPA as the direct address, and no other event.
    """
    if policy == 3:
        iut.ut(hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(1, PEER))
    scan_legacy(iut, policy=policy)
    iut.lt_tx(adv_direct_ind(target=HOST_RPA), channel=37)
    iut.run(1000)
    if policy == 0:
        iut.ut_rx_q_is_empty()
    else:
        event = iut.ut_rx(hci.HCI_LE_Directed_Advertising_Report_Event)
        (report,) = event.reports
        assert report.event_type == 1 and report.address == PEER
        assert report.direct_address_type == 1 and report.direct_address == HOST_RPA
        iut.ut_rx_q_is_empty()


def test_extended_set_does_not_inherit_the_global_random_address(iut):
    """An extended set does not fall back to the global random address.

    Purpose: Verify that an extended advertising set with Own_Address_Type 1
    cannot be enabled using the address from LE Set Random Address and needs
    its own LE Set Advertising Set Random Address (Vol 4, Part E, 7.8.52,
    7.8.53).

    Steps:
        1. Upper Tester sets the global random address.
        2. Upper Tester configures set 0 with Own_Address_Type 1.
        3. Upper Tester enables set 0.
        4. Upper Tester sets the random address of set 0.
        5. Upper Tester enables set 0 again and runs one event.

    Expected:
        - Step 3 returns Invalid HCI Command Parameters.
        - Step 5 succeeds and set 0 advertises with its per-set address.
    """
    iut.ut(hci.HCI_LE_Set_Random_Address_Command(HOST_RPA))
    iut.ut(extended_parameters(own_address_type=1))
    enable_sets = hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0], [0], [0])
    iut.ut(enable_sets, 0x12)
    iut.ut(hci.HCI_LE_Set_Advertising_Set_Random_Address_Command(0, HOST_RPA))
    iut.ut(enable_sets)
    iut.run(1)
    assert iut.link.advertisers[0].address == HOST_RPA


def _add_local_irk(iut):
    iut.ut(
        hci.HCI_LE_Add_Device_To_Resolving_List_Command(
            1, PEER, bytes(16), bytes(range(16))
        )
    )


@pytest.mark.parametrize('extended', [False, True])
def test_scan_enable_requires_random_address_despite_local_irk(iut, extended):
    """Scanning with own type 0x03 needs a random address even with a Local IRK.

    Purpose: Verify that scan enable rejects Own_Address_Type 0x03 while the
    random address is unset, although the resolving list holds a Local IRK;
    scanning has no peer that could supply a matching entry (Vol 4, Part E,
    7.8.11 and 7.8.65).

    Steps:
        1. Upper Tester adds the Lower Tester with a nonzero Local IRK.
        2. Upper Tester sets legacy or extended scan parameters with
           Own_Address_Type 0x03 and no random address.
        3. Upper Tester enables scanning.
        4. Upper Tester sets the random address and enables scanning again.

    Expected:
        - Step 3 returns Invalid HCI Command Parameters and leaves scanning off.
        - Step 4 succeeds.
    """
    _add_local_irk(iut)
    if extended:
        iut.ut(
            hci.HCI_LE_Set_Extended_Scan_Parameters_Command(3, 0, 1, [0], [16], [16])
        )
        enable_scan = hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0)
    else:
        iut.ut(hci.HCI_LE_Set_Scan_Parameters_Command(0, 16, 16, 3, 0))
        enable_scan = hci.HCI_LE_Set_Scan_Enable_Command(1, 0)
    iut.ut(enable_scan, 0x12)
    assert iut.link.scan is None
    iut.ut(hci.HCI_LE_Set_Random_Address_Command(HOST_RPA))
    iut.ut(enable_scan)
    assert iut.link.scan is not None


@pytest.mark.parametrize(
    'policy,status', [(0, 0x00), (1, 0x12)], ids=['peer-entry', 'accept-list']
)
def test_initiating_own_type_3_uses_peer_entry_only_for_policy_0(iut, policy, status):
    """Only policy 0x00 lets a peer's Local IRK replace the random address.

    Purpose: Verify that Create Connection with Own_Address_Type 0x03 and an
    unset random address succeeds when Peer_Address has a resolving-list entry
    with a Local IRK under policy 0x00, but fails under the Filter Accept List
    policy, where Peer_Address is ignored (Vol 4, Part E, 7.8.12).

    Steps:
        1. Upper Tester adds the Lower Tester with a nonzero Local IRK.
        2. Upper Tester sends Create Connection to the Lower Tester with
           Own_Address_Type 0x03 and the parametrized filter policy.

    Expected:
        - Policy 0x00: Command Status Success and initiation is pending.
        - Policy 0x01: Command Status Invalid HCI Command Parameters and no
          initiation starts.
    """
    _add_local_irk(iut)
    iut.ut(
        hci.HCI_LE_Create_Connection_Command(
            le_scan_interval=16,
            le_scan_window=16,
            initiator_filter_policy=policy,
            peer_address_type=1,
            peer_address=PEER,
            own_address_type=3,
            connection_interval_min=24,
            connection_interval_max=24,
            max_latency=0,
            supervision_timeout=100,
            min_ce_length=0,
            max_ce_length=0,
        ),
        status,
    )
    assert (iut.link.initiating is not None) == (status == 0)
