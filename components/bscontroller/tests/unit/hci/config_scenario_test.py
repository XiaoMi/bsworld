# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Controller-defined configuration as seen by the Host (Core Vol 4, Part E).

Each ``ControllerConfig`` value must reach both the HCI command that reports it
and the behaviour that enforces it: version information (7.4.1), LE buffers and
the ACL length check (7.8.2), maximum data length and the LL_LENGTH_REQ that
follows (7.8.46; Vol 6, Part B, 4.5.10), advertising set and data limits (7.8.57,
7.8.58), list sizes (7.8.14, 7.8.73), and the connection and periodic sync
limits. Parsing, precedence and validation of the configuration itself are
covered by ``tests/unit/config_test.py``. Uses the HCI harness in
``harness.py`` through the ``configured`` fixture.
"""

from bscontroller import hci
from bscontroller.config import ControllerConfig
from bscontroller.linklayer import ControlOpcode as Opcode
from bscontroller.linklayer.discovery import PeriodicSync
from tests.unit.hci.harness import PEERS, ErrorCode
from tests.unit.helpers import (
    PEER_ADDRESS,
    create_connection,
    create_sync,
    extended_parameters,
)

Operation = hci.HCI_LE_Set_Extended_Advertising_Data_Command.Operation


def test_version_information_follows_config(configured):
    """Read Local Version Information reports the configured identity.

    Purpose: Verify that HCI and LMP version, revision, subversion and company
    identifier come from the configuration (Vol 4, Part E, 7.4.1).

    Steps:
        1. Configure Core 5.4 versions, revision 3, subversion 7, company 0x0059.
        2. Read Local Version Information.

    Expected:
        - Every returned field equals the configured value.
    """
    host = configured(
        ControllerConfig(
            hci_version=0x0D,
            hci_revision=3,
            lmp_version=0x0D,
            lmp_subversion=7,
            manufacturer=0x0059,
        )
    )
    result = host.cmd(hci.HCI_Read_Local_Version_Information_Command())
    assert (result.hci_version, result.hci_subversion) == (0x0D, 3)
    assert (result.lmp_version, result.lmp_subversion) == (0x0D, 7)
    assert result.company_identifier == 0x0059


def test_le_buffers_report_and_bound_host_acl(configured):
    """LE Read Buffer Size reports the configured buffers and ACL length is
    enforced.

    Purpose: Verify that the configured ACL packet count and length are
    reported (7.8.2) and that the length check applies to Host ACL data, so a
    configured length larger than one LL PDU is accepted and fragmented by the
    LL.

    Steps:
        1. Configure 20 ACL packets of 600 octets.
        2. Read LE Buffer Size.
        3. Establish a connection and send a 600-octet ACL packet, then a
           601-octet one.

    Expected:
        - LE Read Buffer Size returns 600 octets and 20 packets.
        - The 600-octet packet is queued; the 601-octet packet is refused.
    """
    host = configured(ControllerConfig(acl_packet_count=20, acl_packet_length=600))
    buffers = host.cmd(hci.HCI_LE_Read_Buffer_Size_Command())
    assert (
        buffers.le_acl_data_packet_length,
        buffers.total_num_le_acl_data_packets,
    ) == (
        600,
        20,
    )
    connection = host.connection()
    host.acl(connection.handle, hci.HCI_ACL_PB_FIRST_NON_FLUSHABLE, bytes(600))
    assert sum(len(item.payload) for item in connection.event.queue) == 600
    host.acl(connection.handle, hci.HCI_ACL_PB_FIRST_NON_FLUSHABLE, bytes(601))
    assert sum(len(item.payload) for item in connection.event.queue) == 600
    host.ut_rx_q_is_empty()


def test_maximum_data_length_reports_and_caps_negotiation(configured):
    """Maximum data length is reported and caps the LL_LENGTH_REQ the LL sends.

    Purpose: Verify that the supported maxima reported by LE Read Maximum Data
    Length come from the configuration (7.8.46), and that a Host request above
    them is capped to connMaxTx*/connMaxRx* supported in LL_LENGTH_REQ (Vol 6,
    Part B, 4.5.10).

    Steps:
        1. Configure TX 100 octets / 1000 us and RX 120 octets / 1100 us.
        2. Read the maximum data length.
        3. Establish a connection and Set Data Length to 251 octets / 2120 us.

    Expected:
        - The read returns the configured maxima.
        - The queued LL_LENGTH_REQ carries RX 120/1100 and TX 100/1000.
    """
    host = configured(
        ControllerConfig(
            max_tx_octets=100, max_tx_time=1000, max_rx_octets=120, max_rx_time=1100
        )
    )
    maximum = host.cmd(hci.HCI_LE_Read_Maximum_Data_Length_Command())
    assert (
        maximum.supported_max_tx_octets,
        maximum.supported_max_tx_time,
        maximum.supported_max_rx_octets,
        maximum.supported_max_rx_time,
    ) == (100, 1000, 120, 1100)
    connection = host.connection()
    host.cmd(hci.HCI_LE_Set_Data_Length_Command(connection.handle, 251, 2120))
    request = next(
        item.payload
        for item in connection.event.queue
        if item.payload[0] == Opcode.LL_LENGTH_REQ
    )
    assert request[1:] == (120).to_bytes(2, 'little') + (1100).to_bytes(2, 'little') + (
        100
    ).to_bytes(2, 'little') + (1000).to_bytes(2, 'little')


def test_advertising_set_count_and_data_length(configured):
    """Advertising set count and maximum data length follow the configuration.

    Purpose: Verify that the reported set count and maximum advertising data
    length (7.8.58, 7.8.57) are the configured ones and that both are enforced
    when configuring sets and setting extended data.

    Steps:
        1. Configure 2 sets and 300 octets of advertising data.
        2. Read both values; configure sets 0 and 1, then set 2.
        3. Set 250 octets as the first fragment, then 51 more as the last.

    Expected:
        - The reads return 2 and 300.
        - Set 2 is refused with MEMORY_CAPACITY_EXCEEDED.
        - The 301-octet total is refused with MEMORY_CAPACITY_EXCEEDED.
    """
    host = configured(
        ControllerConfig(advertising_set_count=2, max_advertising_data_length=300)
    )
    count = host.cmd(
        hci.HCI_LE_Read_Number_Of_Supported_Advertising_Sets_Command()
    ).num_supported_advertising_sets
    length = host.cmd(
        hci.HCI_LE_Read_Maximum_Advertising_Data_Length_Command()
    ).max_advertising_data_length
    assert (count, length) == (2, 300)
    host.cmd(extended_parameters(advertising_handle=0))
    host.cmd(extended_parameters(advertising_handle=1))
    host.cmd(
        extended_parameters(advertising_handle=2),
        ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR,
    )
    data = hci.HCI_LE_Set_Extended_Advertising_Data_Command
    host.cmd(data(0, Operation.FIRST_FRAGMENT, 0, bytes(250)))
    host.cmd(
        data(0, Operation.LAST_FRAGMENT, 0, bytes(51)),
        ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR,
    )


def test_list_sizes_report_and_bound_additions(configured):
    """Filter Accept List and Periodic Advertiser List sizes are configurable.

    Purpose: Verify that both list sizes are reported (7.8.14, 7.8.73) and that
    an addition beyond the configured size is refused.

    Steps:
        1. Configure a Filter Accept List of 2 and a Periodic Advertiser List of 1.
        2. Read both sizes; add 3 accept-list entries; add 2 periodic entries.

    Expected:
        - The reads return 2 and 1.
        - The third accept-list and second periodic addition return
          MEMORY_CAPACITY_EXCEEDED.
    """
    host = configured(
        ControllerConfig(filter_accept_list_size=2, periodic_advertiser_list_size=1)
    )
    assert (
        host.cmd(
            hci.HCI_LE_Read_Filter_Accept_List_Size_Command()
        ).filter_accept_list_size
        == 2
    )
    assert (
        host.cmd(
            hci.HCI_LE_Read_Periodic_Advertiser_List_Size_Command()
        ).periodic_advertiser_list_size
        == 1
    )
    accept = hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command
    for peer, status in zip(
        PEERS[:3], (0, 0, ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR), strict=True
    ):
        host.cmd(accept(peer.address_type, peer), status)
    periodic = hci.HCI_LE_Add_Device_To_Periodic_Advertiser_List_Command
    host.cmd(periodic(PEERS[0].address_type, PEERS[0], 1))
    host.cmd(
        periodic(PEERS[1].address_type, PEERS[1], 1),
        ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR,
    )


def test_connection_limit_applies_to_initiating_and_advertising(configured):
    """The configured connection limit bounds both connection roles.

    Purpose: Verify that with ``max_connections`` connections established a new
    Create Connection is refused with CONNECTION_LIMIT_EXCEEDED and the
    advertiser no longer accepts a CONNECT_IND, regardless of role.

    Steps:
        1. Configure 2 connections; establish one Central and one Peripheral.
        2. Create Connection to a third peer.
        3. Ask the connection manager whether it accepts a third peer.

    Expected:
        - Create Connection returns CONNECTION_LIMIT_EXCEEDED.
        - The manager refuses the third peer.
    """
    host = configured(ControllerConfig(max_connections=2))
    host.connection(hci.Role.CENTRAL, PEERS[0])
    host.connection(hci.Role.PERIPHERAL, PEERS[1])
    host.cmd_status(
        create_connection(peer_address=PEERS[2]),
        ErrorCode.CONNECTION_LIMIT_EXCEEDED_ERROR,
    )
    assert not host.link.roles.connections.can_accept(PEERS[2])


def test_periodic_sync_limit(configured):
    """The configured periodic sync limit bounds Create Sync.

    Purpose: Verify that with ``max_periodic_syncs`` syncs established a new
    Create Sync is refused with MEMORY_CAPACITY_EXCEEDED, and accepted again
    after one is terminated.

    Steps:
        1. Configure 1 sync and establish it.
        2. Create Sync for another SID; terminate the sync; Create Sync again.

    Expected:
        - The first Create Sync returns MEMORY_CAPACITY_EXCEEDED.
        - After Terminate Sync the Create Sync succeeds.
    """
    host = configured(ControllerConfig(max_periodic_syncs=1))
    host.link.syncs[1] = PeriodicSync(
        PEER_ADDRESS, 0, 100_000, 100_000, established=True, handle=1
    )
    host.cmd_status(
        create_sync(advertising_sid=5), ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR
    )
    host.cmd(hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(1))
    host.cmd_status(create_sync(advertising_sid=5))


def test_controllers_in_one_process_keep_separate_configs(configured):
    """Two Controllers built from one document report their own values.

    Purpose: Verify that per-Controller entries give each Controller its own
    limits, so one test topology can mix Controller capabilities.

    Steps:
        1. Load hci0 and hci1 from a document with different ``aclPacketCount``.
        2. Build a Controller for each and read LE Buffer Size.

    Expected:
        - Each Controller reports its own packet count.
    """
    document = {
        'defaults': {'aclPacketLength': 27},
        'controllers': {'hci0': {'aclPacketCount': 3}, 'hci1': {'aclPacketCount': 9}},
    }
    first = configured(ControllerConfig.from_mapping(document, 'hci0'))
    second = configured(ControllerConfig.from_mapping(document, 'hci1'))
    read = hci.HCI_LE_Read_Buffer_Size_Command
    assert first.cmd(read()).total_num_le_acl_data_packets == 3
    assert second.cmd(read()).total_num_le_acl_data_packets == 9
    assert first.cmd(read()).le_acl_data_packet_length == 27
