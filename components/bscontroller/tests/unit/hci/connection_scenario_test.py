# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connection-scoped HCI commands, ACL flow and connection events (Core Vol 4,
Part E, 5.4, 7.1.6, 7.3.93-7.3.94, 7.5.4, 7.7.5, 7.7.19, 7.7.65, 7.8.12-7.8.50).

Matrix: every connection-scoped command x {valid handle, unknown handle, other
handle, invalid parameters}; ACL {start, continuation, flags, size} in both
directions with completed-packet credits; event content published to the Host.
LL procedure behaviour behind these commands is specified in linklayer/llcp/.
"""

import pytest

from bscontroller import hci
from bscontroller.config import ControllerConfig
from bscontroller.hci.capabilities import HCI_RSSI_NOT_AVAILABLE
from bscontroller.linklayer import LlId
from bscontroller.linklayer.instant import InstantUpdate
from bscontroller.radio.base import Phy
from tests.unit.hci.harness import IUT_ADDRESS, PEERS, UNKNOWN_HANDLE, ErrorCode
from tests.unit.helpers import create_connection
from tests.unit.linklayer.air.harness import Air

UNKNOWN = ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
FIRST = hci.HCI_ACL_PB_FIRST_FLUSHABLE
FIRST_NON_FLUSHABLE = hci.HCI_ACL_PB_FIRST_NON_FLUSHABLE
CONTINUATION = hci.HCI_ACL_PB_CONTINUATION
DISCONNECTED = hci.HCI_Disconnection_Complete_Event


# ---------------------------------------------------------------------------
# Unknown handle: every connection-scoped command (Vol 1, Part F, 2.2)
# ---------------------------------------------------------------------------
SYNC_COMMANDS = [
    hci.HCI_Read_RSSI_Command(UNKNOWN_HANDLE),
    hci.HCI_LE_Read_Channel_Map_Command(UNKNOWN_HANDLE),
    hci.HCI_LE_Read_PHY_Command(UNKNOWN_HANDLE),
    hci.HCI_LE_Set_Data_Length_Command(UNKNOWN_HANDLE, 251, 2120),
    hci.HCI_Read_Authenticated_Payload_Timeout_Command(UNKNOWN_HANDLE),
    hci.HCI_Write_Authenticated_Payload_Timeout_Command(UNKNOWN_HANDLE, 100),
    hci.HCI_LE_Long_Term_Key_Request_Reply_Command(UNKNOWN_HANDLE, bytes(16)),
    hci.HCI_LE_Long_Term_Key_Request_Negative_Reply_Command(UNKNOWN_HANDLE),
    hci.HCI_LE_Remote_Connection_Parameter_Request_Reply_Command(
        UNKNOWN_HANDLE, 24, 24, 0, 100, 0, 0
    ),
    hci.HCI_LE_Remote_Connection_Parameter_Request_Negative_Reply_Command(
        UNKNOWN_HANDLE, ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR
    ),
]
ASYNC_COMMANDS = [
    hci.HCI_Disconnect_Command(UNKNOWN_HANDLE, 0x13),
    hci.HCI_LE_Read_Remote_Features_Command(UNKNOWN_HANDLE),
    hci.HCI_Read_Remote_Version_Information_Command(UNKNOWN_HANDLE),
    hci.HCI_LE_Connection_Update_Command(UNKNOWN_HANDLE, 24, 24, 0, 100, 0, 0),
    hci.HCI_LE_Set_PHY_Command(UNKNOWN_HANDLE, 0, 2, 2, 0),
    hci.HCI_LE_Enable_Encryption_Command(UNKNOWN_HANDLE, bytes(8), 0, bytes(16)),
]


@pytest.mark.parametrize('command', SYNC_COMMANDS, ids=lambda c: type(c).__name__)
def test_sync_command_with_unknown_handle(host, command):
    """A sync connection command with an unknown handle is refused.

    Purpose: Verify every synchronous connection-scoped command answers Command
    Complete with Unknown Connection Identifier when the handle is not known,
    echoing the handle on the wire where present (Vol 1, Part F, 2.2).

    Steps:
        1. Establish one connection so a valid handle exists.
        2. Send the parametrized sync command carrying the unknown handle.

    Expected:
        - The command returns UNKNOWN_CONNECTION_IDENTIFIER; where the reply has
          a handle, it echoes the unknown handle; the Host queue is empty.
    """
    host.connection()
    result = host.cmd(command, UNKNOWN)
    if len(result.wire) >= 9:
        assert host.wire_handle(result) == UNKNOWN_HANDLE
    host.ut_rx_q_is_empty()


@pytest.mark.parametrize('command', ASYNC_COMMANDS, ids=lambda c: type(c).__name__)
def test_async_command_with_unknown_handle(host, command):
    """An async connection command with an unknown handle is refused.

    Purpose: Verify every asynchronous connection-scoped command answers Command
    Status with Unknown Connection Identifier for an unknown handle and starts
    no procedure (Vol 1, Part F, 2.2).

    Steps:
        1. Establish one connection so a valid handle exists.
        2. Send the parametrized async command carrying the unknown handle.

    Expected:
        - Command Status is UNKNOWN_CONNECTION_IDENTIFIER and the Host queue is
          empty.
    """
    host.connection()
    host.cmd_status(command, UNKNOWN)
    host.ut_rx_q_is_empty()


def test_unestablished_connection_is_not_addressable(host):
    """LL owns the handle before HCI publishes it; the Host cannot use it yet.

    Purpose: Verify a connection object that the LL has created but not yet
    reported is not addressable over HCI: the Host sees Unknown Connection
    Identifier and the handle is not found.

    Steps:
        1. Create an unreported Connection and register it in the link.
        2. Send LE Read PHY for its handle.

    Expected:
        - The command returns UNKNOWN_CONNECTION_IDENTIFIER and the handle is
          not found by the Controller.
    """
    from bscontroller.linklayer.connection import Connection
    from tests.unit.helpers import PARAMETERS

    connection = Connection(host.link, PARAMETERS, PEERS[0], hci.Role.CENTRAL, 0)
    host.link.connections[connection.handle] = connection
    host.cmd(hci.HCI_LE_Read_PHY_Command(connection.handle), UNKNOWN)
    assert host.controller.find_connection_by_handle(connection.handle) is None


# ---------------------------------------------------------------------------
# Query commands
# ---------------------------------------------------------------------------
def test_read_rssi_before_any_reception(host):
    """Read RSSI reports not-available before any packet is received.

    Purpose: Verify Read RSSI returns the not-available sentinel for a fresh
    connection that has received nothing (Vol 4, Part E, 7.5.4).

    Steps:
        1. Establish a connection and send Read RSSI.

    Expected:
        - The reply carries the connection handle and RSSI_NOT_AVAILABLE.
    """
    connection = host.connection()
    result = host.cmd(hci.HCI_Read_RSSI_Command(connection.handle))
    assert (result.handle, result.rssi) == (connection.handle, HCI_RSSI_NOT_AVAILABLE)


def test_read_rssi_tracks_valid_receptions_only(host):
    """Read RSSI reflects only valid-CRC receptions.

    Purpose: Verify the RSSI reported by Read RSSI updates from a valid-CRC
    reception but ignores a later bad-CRC one (Vol 4, Part E, 7.5.4).

    Steps:
        1. Establish a connection.
        2. Script one valid reception at -53.4 dBm and one invalid at -80 dBm
           and run the connection's receive for each.
        3. Read RSSI.

    Expected:
        - The reported RSSI is -53 (from the valid reception only).
    """
    connection = host.connection()
    for rssi, valid, at in ((-53.4, True, 1300), (-80, False, 2300)):
        host.radio.script.append(Air(b'\x01\x00', rssi=rssi, valid_crc=valid, at=at))
        host.loop.run_until_complete(connection.receive(7, at + 200))
    assert host.cmd(hci.HCI_Read_RSSI_Command(connection.handle)).rssi == -53


def test_read_channel_map(host):
    """LE Read Channel Map returns the connection's channel map.

    Purpose: Verify LE Read Channel Map reports the connection parameters'
    channel map (Vol 4, Part E, 7.8.20).

    Steps:
        1. Establish a connection and send LE Read Channel Map.

    Expected:
        - The returned channel map equals ``connection.parameters.channel_map``.
    """
    connection = host.connection()
    result = host.cmd(hci.HCI_LE_Read_Channel_Map_Command(connection.handle))
    assert (
        int.from_bytes(result.channel_map, 'little')
        == connection.parameters.channel_map
    )


@pytest.mark.parametrize('phys', [(Phy.LE_1M, Phy.LE_1M), (Phy.LE_2M, Phy.LE_CODED)])
def test_read_phy(host, phys):
    """LE Read PHY reports the connection's current TX and RX PHYs.

    Purpose: Verify LE Read PHY returns the connection's TX and RX PHYs
    (Vol 4, Part E, 7.8.47).

    Steps:
        1. Establish a connection and set its TX/RX PHYs to the parametrized
           pair.
        2. Send LE Read PHY.

    Expected:
        - The returned TX and RX PHYs equal the configured pair.
    """
    connection = host.connection()
    connection.tx_phy, connection.rx_phy = phys
    result = host.cmd(hci.HCI_LE_Read_PHY_Command(connection.handle))
    assert (result.tx_phy, result.rx_phy) == phys


def test_authenticated_payload_timeout_round_trip(host):
    """Authenticated Payload Timeout writes, reads back and rejects zero.

    Purpose: Verify Write/Read Authenticated Payload Timeout round-trip the
    value (in units of 10 ms) and that writing zero is rejected (Vol 4, Part E,
    7.3.93, 7.3.94).

    Steps:
        1. Establish a connection and write a timeout of 300.
        2. Read the timeout back.
        3. Write zero.

    Expected:
        - The stored auth timeout is 3_000_000 us; the read returns 300; writing
          zero returns INVALID_COMMAND_PARAMETERS.
    """
    connection = host.connection()
    host.cmd(
        hci.HCI_Write_Authenticated_Payload_Timeout_Command(connection.handle, 300)
    )
    assert connection.encryption.auth_timeout_us == 3_000_000
    result = host.cmd(
        hci.HCI_Read_Authenticated_Payload_Timeout_Command(connection.handle)
    )
    assert result.authenticated_payload_timeout == 300
    host.cmd(
        hci.HCI_Write_Authenticated_Payload_Timeout_Command(connection.handle, 0),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


# ---------------------------------------------------------------------------
# Connection-scoped commands reach only their own handle
# ---------------------------------------------------------------------------
def test_commands_route_to_the_selected_handle(host):
    """A connection command affects only its own handle.

    Purpose: Verify Disconnect and queued ACL act only on the addressed
    connection: the other connection's queue is untouched, and the terminate
    and data land on the selected one, which is then removed on completion
    (Vol 4, Part E, 7.1.6, 7.7.5).

    Steps:
        1. Establish two connections; queue ACL on the second then Disconnect
           it.
        2. Complete the disconnection and consume its event.

    Expected:
        - The first queue stays empty; the second queue holds TERMINATE_IND then
          the ACL payload; a Disconnection Complete (reason 0x16) is delivered;
          the first handle is still found, the second is gone.
    """
    first, second = host.connection(), host.connection()
    host.acl(second.handle, FIRST, b'second')
    host.cmd_status(hci.HCI_Disconnect_Command(second.handle, 0x13))
    assert not first.event.queue
    assert [item.payload for item in second.event.queue][0][0] == 0x02  # TERMINATE_IND
    assert second.event.queue[1].payload == b'second'
    host.in_loop(second.complete, ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR)
    host.ut_rx(DISCONNECTED, connection_handle=second.handle, reason=0x16)
    assert host.controller.find_connection_by_handle(first.handle) is not None
    assert host.controller.find_connection_by_handle(second.handle) is None


@pytest.mark.parametrize(
    'command,check',
    [
        (lambda h: hci.HCI_LE_Read_Remote_Features_Command(h), 'feature_request'),
        (
            lambda h: hci.HCI_Read_Remote_Version_Information_Command(h),
            'version_request',
        ),
        (lambda h: hci.HCI_LE_Set_PHY_Command(h, 0, 2, 2, 0), 'busy'),
        (
            lambda h: hci.HCI_LE_Connection_Update_Command(h, 30, 30, 0, 100, 0, 0),
            'busy',
        ),
    ],
    ids=['features', 'version', 'phy', 'update'],
)
def test_async_procedure_commands_start_on_the_selected_connection(
    host, command, check
):
    """An async procedure command starts the procedure on its own connection.

    Purpose: Verify each async procedure command (features, version, PHY,
    connection update) marks only the addressed connection's control flag and
    leaves the other connection idle (Vol 4, Part E, 7.8.x).

    Steps:
        1. Establish two connections.
        2. Send the parametrized command on the second connection.

    Expected:
        - The second connection's checked control flag is set; the first
          connection's is not and its event queue is empty.
    """
    first, second = host.connection(), host.connection()
    host.cmd_status(command(second.handle))
    assert getattr(second.controls, check)
    assert not getattr(first.controls, check) and not first.event.queue


def test_cached_completion_follows_command_status(host):
    """A cached feature set completes immediately after Command Status.

    Purpose: Verify LE Read Remote Features, when the peer features are already
    cached, returns Command Status then an immediate Read Remote Features
    Complete carrying them (Vol 4, Part E, 7.8.21).

    Steps:
        1. Establish a connection and preset its cached peer features to 0x1F.
        2. Send LE Read Remote Features.

    Expected:
        - A Read Remote Features Complete (status 0, this handle) reports
          features 0x1F.
    """
    connection = host.connection()
    connection.peer_features = 0x1F
    host.cmd_status(hci.HCI_LE_Read_Remote_Features_Command(connection.handle))
    event = host.ut_rx(
        hci.HCI_LE_Read_Remote_Features_Complete_Event,
        status=0,
        connection_handle=connection.handle,
    )
    assert int.from_bytes(event.le_features, 'little') == 0x1F


def test_remote_parameter_replies(host):
    """Remote parameter request replies are disallowed without a request.

    Purpose: Verify both the positive and negative Remote Connection Parameter
    Request replies are refused with COMMAND_DISALLOWED when no request is
    outstanding (Vol 4, Part E, 7.8.31, 7.8.32).

    Steps:
        1. Establish a connection.
        2. Send the positive then the negative reply command.

    Expected:
        - Both replies return COMMAND_DISALLOWED.
    """
    connection = host.connection()
    host.cmd(
        hci.HCI_LE_Remote_Connection_Parameter_Request_Reply_Command(
            connection.handle, 24, 24, 0, 100, 0, 0
        ),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )
    host.cmd(
        hci.HCI_LE_Remote_Connection_Parameter_Request_Negative_Reply_Command(
            connection.handle, ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR
        ),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )


def test_long_term_key_replies_are_disallowed_without_request(host):
    """Long Term Key replies are disallowed without an LTK request.

    Purpose: Verify both the positive and negative LTK Request replies are
    refused with COMMAND_DISALLOWED on a peripheral connection with no
    outstanding request, echoing the handle (Vol 4, Part E, 7.8.25, 7.8.26).

    Steps:
        1. Establish a peripheral connection.
        2. Send the positive then the negative LTK reply.

    Expected:
        - The positive reply returns COMMAND_DISALLOWED and echoes the handle;
          the negative reply returns COMMAND_DISALLOWED.
    """
    connection = host.connection(hci.Role.PERIPHERAL)
    result = host.cmd(
        hci.HCI_LE_Long_Term_Key_Request_Reply_Command(connection.handle, bytes(16)),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )
    assert host.wire_handle(result) == connection.handle
    host.cmd(
        hci.HCI_LE_Long_Term_Key_Request_Negative_Reply_Command(connection.handle),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )


@pytest.mark.parametrize(
    'arguments,status',
    [
        ((0, 0x08, 2, 0), ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR),
        ((0, 2, 0, 0), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        ((0x04, 2, 2, 0), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        ((0, 2, 2, 3), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
    ],
    ids=['rfu-phy', 'no-rx', 'rfu-all-phys', 'options'],
)
def test_set_phy_parameter_validation(host, arguments, status):
    """LE Set PHY rejects invalid PHY and option parameters.

    Purpose: Verify LE Set PHY returns the correct error for a reserved PHY, a
    missing RX direction, reserved all-PHYs bits and an invalid options value,
    starting no procedure (Vol 4, Part E, 7.8.49).

    Steps:
        1. Establish a connection.
        2. Send LE Set PHY with the parametrized arguments.

    Expected:
        - Command Status matches the expected error and the connection is not
          marked busy.
    """
    connection = host.connection()
    host.cmd_status(hci.HCI_LE_Set_PHY_Command(connection.handle, *arguments), status)
    assert not connection.controls.busy


@pytest.mark.parametrize('options,coding', [(0, 8), (1, 2), (2, 8)])
def test_set_phy_coding_preference(host, options, coding):
    """LE Set PHY options select the Coded PHY S coding preference.

    Purpose: Verify the PHY options field maps to the connection's TX coding
    factor (S=8 or S=2) when requesting the Coded PHY (Vol 4, Part E, 7.8.49).

    Steps:
        1. Establish a connection.
        2. Send LE Set PHY requesting Coded with the parametrized options.

    Expected:
        - The connection's ``tx_coding`` equals the expected S factor.
    """
    connection = host.connection()
    host.cmd_status(hci.HCI_LE_Set_PHY_Command(connection.handle, 0, 4, 4, options))
    assert connection.tx_coding == coding


@pytest.mark.parametrize(
    'all_phys,tx,rx,status,expected',
    [
        (0, 2, 2, 0, (2, 2)),
        (3, 0, 0, 0, (7, 7)),
        (1, 0, 4, 0, (7, 4)),
        (0, 0, 2, ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR, None),
        (0, 8, 2, ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR, None),
    ],
)
def test_set_default_phy(host, all_phys, tx, rx, status, expected):
    """LE Set Default PHY stores valid preferences and rejects invalid ones.

    Purpose: Verify LE Set Default PHY applies the default TX/RX PHY masks when
    valid (including the all-PHYs wildcard) and rejects invalid combinations
    (Vol 4, Part E, 7.8.48).

    Steps:
        1. Send LE Set Default PHY with the parametrized masks.
        2. When valid, read back the link's default TX/RX PHYs.

    Expected:
        - The command returns the expected status; for valid cases the stored
          default PHYs equal the expected pair.
    """
    host.cmd(hci.HCI_LE_Set_Default_PHY_Command(all_phys, tx, rx), status)
    if expected:
        assert (host.link.default_tx_phys, host.link.default_rx_phys) == expected


def test_connection_update_ce_length_validation(host):
    """Connection Update rejects a min CE length above the max.

    Purpose: Verify LE Connection Update rejects parameters where the minimum
    connection-event length exceeds the maximum (Vol 4, Part E, 7.8.18).

    Steps:
        1. Establish a connection.
        2. Send LE Connection Update with min CE length 2 and max 1.

    Expected:
        - Command Status is INVALID_COMMAND_PARAMETERS.
    """
    connection = host.connection()
    host.cmd_status(
        hci.HCI_LE_Connection_Update_Command(connection.handle, 30, 30, 0, 100, 2, 1),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


def test_suggested_default_data_length(host):
    """Suggested Default Data Length round-trips and validates its range.

    Purpose: Verify Write/Read Suggested Default Data Length round-trip a valid
    pair and that writing an out-of-range octet count is rejected (Vol 4,
    Part E, 7.8.33, 7.8.34).

    Steps:
        1. Write 251 octets / 2120 us and read it back.
        2. Write an invalid 26 octets.

    Expected:
        - The read returns (251, 2120); the invalid write returns
          INVALID_COMMAND_PARAMETERS.
    """
    host.cmd(hci.HCI_LE_Write_Suggested_Default_Data_Length_Command(251, 2120))
    result = host.cmd(hci.HCI_LE_Read_Suggested_Default_Data_Length_Command())
    assert (result.suggested_max_tx_octets, result.suggested_max_tx_time) == (251, 2120)
    host.cmd(
        hci.HCI_LE_Write_Suggested_Default_Data_Length_Command(26, 2120),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


def test_set_data_length_admission(host):
    """LE Set Data Length admits only after DLE feature and rejects repeats.

    Purpose: Verify LE Set Data Length requires the peer's Data Length Extension
    feature, succeeds once, refuses an identical repeat, and validates the octet
    range (Vol 4, Part E, 7.8.33).

    Steps:
        1. Establish a connection; with no peer DLE feature send Set Data
           Length.
        2. Grant the DLE feature and send it again, then a third time, then with
           an invalid octet count.

    Expected:
        - Without the feature it returns UNSUPPORTED_REMOTE_FEATURE and echoes
          the handle; with it the first succeeds; a repeat returns
          COMMAND_DISALLOWED; an invalid size returns INVALID_COMMAND_PARAMETERS.
    """
    connection = host.connection()
    command = hci.HCI_LE_Set_Data_Length_Command(connection.handle, 251, 2120)
    connection.peer_features = 0
    result = host.cmd(command, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)
    assert host.wire_handle(result) == connection.handle
    connection.peer_features = int(hci.LeFeatureMask.LE_DATA_PACKET_LENGTH_EXTENSION)
    host.cmd(command)
    host.cmd(command, ErrorCode.COMMAND_DISALLOWED_ERROR)
    host.cmd(
        hci.HCI_LE_Set_Data_Length_Command(connection.handle, 26, 2120),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


# ---------------------------------------------------------------------------
# Host channel classification (7.8.19)
# ---------------------------------------------------------------------------
def test_channel_classification_targets_only_centrals_and_is_atomic(host):
    """Host channel classification applies atomically to Central connections.

    Purpose: Verify Set Host Channel Classification is refused while any Central
    has a pending channel-map update (atomicity), applies to all Centrals once
    clear, and never touches Peripheral connections (Vol 4, Part E, 7.8.19).

    Steps:
        1. Establish two Central connections and one Peripheral.
        2. With a pending update on one Central, send the command; then clear it
           and resend.

    Expected:
        - With a pending update the command is COMMAND_DISALLOWED and no Central
          gains the pending map; once clear both Centrals get the map and the
          Peripheral stays unaffected.
    """
    first = host.connection(hci.Role.CENTRAL)
    second = host.connection(hci.Role.CENTRAL)
    peripheral = host.connection(hci.Role.PERIPHERAL)
    channel_map = (1 << 2) | (1 << 8)
    command = hci.HCI_LE_Set_Host_Channel_Classification_Command(
        channel_map.to_bytes(5, 'little')
    )
    second.controls.pending = InstantUpdate(6, channel_map=channel_map)
    host.cmd(command, ErrorCode.COMMAND_DISALLOWED_ERROR)
    assert first.controls.pending is None
    second.controls.pending = None
    host.cmd(command)
    assert (
        first.controls.pending.channel_map
        == second.controls.pending.channel_map
        == channel_map
    )
    assert peripheral.controls.pending is None


@pytest.mark.parametrize('channel_map', [0, 1, 1 << 37], ids=['none', 'one', 'rfu'])
def test_channel_classification_validation(host, channel_map):
    """Host channel classification rejects invalid channel maps.

    Purpose: Verify Set Host Channel Classification rejects maps with no used
    channel, a single used channel, or a reserved bit set (Vol 4, Part E,
    7.8.19).

    Steps:
        1. Send the command with the parametrized invalid map.

    Expected:
        - Each returns INVALID_COMMAND_PARAMETERS.
    """
    host.cmd(
        hci.HCI_LE_Set_Host_Channel_Classification_Command(
            channel_map.to_bytes(5, 'little')
        ),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


def test_channel_classification_is_used_for_new_connections(host):
    """A host channel classification applies to a new initiation.

    Purpose: Verify a channel map set by Set Host Channel Classification is
    carried into a subsequent Create Connection's initiating parameters
    (Vol 4, Part E, 7.8.19, 7.8.12).

    Steps:
        1. Set a host channel classification excluding two channels.
        2. Start Create Connection.

    Expected:
        - The initiating parameters' channel map equals the configured map.
    """
    channel_map = (1 << 37) - 1 - 3
    host.cmd(
        hci.HCI_LE_Set_Host_Channel_Classification_Command(
            channel_map.to_bytes(5, 'little')
        )
    )
    host.cmd_status(create_connection())
    assert host.link.initiating.parameters.channel_map == channel_map


# ---------------------------------------------------------------------------
# ACL data, Host -> Controller (5.4.2)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'pb,llid',
    [
        (FIRST, LlId.START),
        (FIRST_NON_FLUSHABLE, LlId.START),
        (CONTINUATION, LlId.CONTINUATION),
    ],
)
def test_host_acl_boundary_selects_llid(host, pb, llid):
    """The Host ACL packet-boundary flag maps to the LL data LLID.

    Purpose: Verify the HCI ACL PB flag selects the LL data channel LLID (START
    vs CONTINUATION) for queued payloads (Vol 4, Part E, 5.4.2).

    Steps:
        1. Establish a connection.
        2. Send Host ACL with the parametrized PB flag.

    Expected:
        - The queued fragment's LLID matches the expected value and carries the
          payload.
    """
    connection = host.connection()
    host.acl(connection.handle, pb, b'abc')
    assert (connection.event.queue[-1].llid, connection.event.queue[-1].payload) == (
        llid,
        b'abc',
    )


@pytest.mark.parametrize(
    'handle,pb,bc',
    [(UNKNOWN_HANDLE, FIRST, 0), (None, 3, 0), (None, FIRST, 1)],
    ids=['unknown-handle', 'reserved-pb', 'broadcast'],
)
def test_invalid_host_acl_is_dropped(host, handle, pb, bc):
    """Invalid Host ACL packets are silently dropped.

    Purpose: Verify the Controller drops Host ACL data with an unknown handle, a
    reserved PB flag, or a non-zero broadcast flag, queuing nothing and emitting
    no event (Vol 4, Part E, 5.4.2).

    Steps:
        1. Establish a connection.
        2. Send Host ACL with the parametrized invalid handle/PB/BC.

    Expected:
        - The connection queue stays empty and the Host queue is empty.
    """
    connection = host.connection()
    host.acl(connection.handle if handle is None else handle, pb, b'abc', bc=bc)
    assert not connection.event.queue
    host.ut_rx_q_is_empty()


def test_oversize_host_acl_is_rejected(host):
    """Host ACL larger than the buffer is rejected.

    Purpose: Verify the Controller rejects a Host ACL payload exceeding the LE
    ACL buffer length, queuing nothing (Vol 4, Part E, 5.4.2).

    Steps:
        1. Establish a connection.
        2. Submit an ACL packet one octet over the buffer size.

    Expected:
        - ``ValueError`` mentioning 'HCI buffer' is raised; the queue is empty.
    """
    connection = host.connection()
    data = bytes(host.controller.config.acl_packet_length + 1)
    with pytest.raises(ValueError, match='HCI buffer'):
        host.controller.on_hci_acl_data_packet(
            hci.HCI_AclDataPacket(connection.handle, FIRST, 0, len(data), data)
        )
    assert not connection.event.queue


def test_empty_host_acl_returns_its_credit_at_once(host):
    """An empty Host ACL packet returns its flow-control credit immediately.

    Purpose: Verify enqueuing an empty ACL payload returns one completed-packet
    credit to the Host at once (Vol 4, Part E, 5.4.2, 7.7.19).

    Steps:
        1. Establish a connection and enqueue an empty ACL payload.

    Expected:
        - A Number Of Completed Packets event reports one completed packet for
          this handle.
    """
    connection = host.connection()
    host.in_loop(connection.enqueue_acl, False, b'')
    event = host.ut_rx(hci.HCI_Number_Of_Completed_Packets_Event)
    assert event.connection_handles == [connection.handle]
    assert event.num_completed_packets == [1]


# ---------------------------------------------------------------------------
# ACL data, Controller -> Host (5.4.2, 7.7.19)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'llid,pb', [(LlId.START, FIRST), (LlId.CONTINUATION, CONTINUATION)]
)
def test_received_acl_boundary(host, llid, pb):
    """A received LL fragment's LLID maps to the Host ACL PB flag.

    Purpose: Verify an inbound LL data fragment is delivered to the Host as ACL
    with the PB flag matching its LLID (START vs CONTINUATION) (Vol 4, Part E,
    5.4.2, 7.7.19).

    Steps:
        1. Establish a connection.
        2. Accept an LL fragment with the parametrized LLID.

    Expected:
        - The Host receives an ACL packet for this handle with the expected PB
          flag and the payload 'abc'.
    """
    connection = host.connection()
    host.in_loop(connection.accept, bytes([llid, 3]) + b'abc', True)
    packet = host.ut_rx(
        hci.HCI_AclDataPacket, connection_handle=connection.handle, pb_flag=pb
    )
    assert packet.data == b'abc'


def test_completed_packet_credit_after_peer_ack(host):
    """A completed-packet credit is returned only after the peer acknowledges.

    Purpose: Verify a transmitted ACL payload yields its completed-packet credit
    to the Host only once the peer acknowledges it, not at send time (Vol 4,
    Part E, 7.7.19).

    Steps:
        1. Establish a connection, queue ACL and send it.
        2. Confirm no credit yet, then accept the peer's acknowledgment.

    Expected:
        - No event is produced at send; after the ack a Number Of Completed
          Packets event reports one packet for this handle.
    """
    connection = host.connection()
    host.acl(connection.handle, FIRST, b'payload')
    host.loop.run_until_complete(connection.send(0, 100))
    host.ut_rx_q_is_empty()  # No credit until the peer acknowledges.
    host.in_loop(connection.accept, bytes([LlId.CONTINUATION | 0x04, 0]), True)
    event = host.ut_rx(hci.HCI_Number_Of_Completed_Packets_Event)
    assert (event.connection_handles, event.num_completed_packets) == (
        [connection.handle],
        [1],
    )


# ---------------------------------------------------------------------------
# Connection lifecycle events published to the Host (7.7.5, 7.7.65)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('role', [hci.Role.CENTRAL, hci.Role.PERIPHERAL])
def test_connection_complete_content(host, role):
    """Enhanced Connection Complete carries the full connection parameters.

    Purpose: Verify a reported connection produces an Enhanced Connection
    Complete with the correct role, peer, interval, timeout and clock accuracy,
    a Channel Selection Algorithm event for a Central, and a matching public
    record (Vol 4, Part E, 7.7.65, 7.7.65.6), for both roles.

    Steps:
        1. Create a CSA2 connection with a self address and register it.
        2. Report it created and read the events.

    Expected:
        - The Enhanced Connection Complete fields match the parameters; a
          Central also gets a Channel Selection Algorithm event; the record
          reflects the peer, self address and role.
    """
    from bscontroller.linklayer.connection import Connection
    from tests.unit.helpers import PARAMETERS

    connection = Connection(
        host.link, PARAMETERS, PEERS[3], role, 0, csa2=True, self_address=IUT_ADDRESS
    )
    host.link.connections[connection.handle] = connection
    host.in_loop(connection.report_created)
    event = host.ut_rx(
        hci.HCI_LE_Enhanced_Connection_Complete_Event,
        status=0,
        connection_handle=connection.handle,
        role=role,
        peer_address=PEERS[3],
        connection_interval=PARAMETERS.interval,
        supervision_timeout=PARAMETERS.timeout,
        central_clock_accuracy=PARAMETERS.sca,
    )
    if role == hci.Role.CENTRAL:
        host.ut_rx(
            hci.HCI_LE_Channel_Selection_Algorithm_Event, channel_selection_algorithm=1
        )
    host.ut_rx_q_is_empty()
    record = host.controller.find_connection_by_handle(event.connection_handle)
    assert record.peer_address == PEERS[3] and record.self_address == IUT_ADDRESS
    assert record.role == role


def test_legacy_connection_complete_when_enhanced_is_masked(host):
    """A masked Enhanced event falls back to legacy Connection Complete.

    Purpose: Verify that when the Enhanced Connection Complete event is masked,
    a new connection is reported with the legacy Connection Complete event
    (Vol 4, Part E, 7.8.1, 7.7.65).

    Steps:
        1. Mask the Enhanced Connection Complete bit in the LE mask.
        2. Create and report a peripheral connection.

    Expected:
        - A legacy LE Connection Complete (status 0, this handle) is delivered.
    """
    mask = ((1 << 64) - 1) & ~sum(
        1 << (code - 1)
        for code in (
            hci.HCI_LE_ENHANCED_CONNECTION_COMPLETE_EVENT,
            hci.HCI_LE_ENHANCED_CONNECTION_COMPLETE_V2_EVENT,
        )
    )
    host.cmd(hci.HCI_LE_Set_Event_Mask_Command(mask.to_bytes(8, 'little')))
    from bscontroller.linklayer.connection import Connection
    from tests.unit.helpers import PARAMETERS

    connection = Connection(host.link, PARAMETERS, PEERS[0], hci.Role.PERIPHERAL, 0)
    host.link.connections[connection.handle] = connection
    host.in_loop(connection.report_created)
    host.ut_rx(
        hci.HCI_LE_Connection_Complete_Event,
        status=0,
        connection_handle=connection.handle,
    )


def test_disconnection_releases_the_published_record(host):
    """Disconnection removes the handle but the record keeps its final identity.

    Purpose: Verify completing a connection delivers Disconnection Complete,
    removes the handle from lookup, and that the retained record keeps the final
    peer and self identity even after a new connection reuses the handle
    (Vol 4, Part E, 7.7.5).

    Steps:
        1. Establish a connection, fetch its record and set a self address.
        2. Complete it (remote user terminated) and consume the event.
        3. Establish a replacement connection on a new peer.

    Expected:
        - Disconnection Complete reports reason 0x13; the handle is gone; the
          replacement reuses the handle; the old record keeps its peer and the
          self address it had at completion.
    """
    connection = host.connection()
    record = host.controller.find_connection_by_handle(connection.handle)
    connection.self_address = hci.Address('C0:12:34:56:78:9A')
    host.in_loop(connection.complete, ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR)
    host.ut_rx(DISCONNECTED, status=0, connection_handle=connection.handle, reason=0x13)
    assert host.controller.find_connection_by_handle(connection.handle) is None
    # The retained record keeps the final identity, not a later handle owner.
    replacement = host.connection(peer=PEERS[5])
    assert replacement.handle == connection.handle
    assert record.peer_address == connection.peer
    assert record.self_address == hci.Address('C0:12:34:56:78:9A')


def test_record_follows_live_identity_and_is_read_only(host):
    """A live connection record tracks identity changes but is read-only.

    Purpose: Verify a connection record reflects later changes to the live
    connection's self address while remaining immutable to callers.

    Steps:
        1. Establish a connection and fetch its record.
        2. Change the live connection's self address.
        3. Attempt to set the record's self address.

    Expected:
        - The record reports the updated self address; assigning it raises
          ``AttributeError``.
    """
    connection = host.connection()
    record = host.controller.find_connection_by_handle(connection.handle)
    updated = hci.Address('C0:12:34:56:78:9A')
    connection.self_address = updated
    assert record.self_address == updated
    with pytest.raises(AttributeError):
        record.self_address = IUT_ADDRESS


def test_record_requires_a_snapshot(host):
    """Constructing a connection record requires a snapshot, not a live object.

    Purpose: Verify ``ConnectionRecord`` rejects construction from a live
    connection object, enforcing that records wrap a detached snapshot.

    Steps:
        1. Construct ``ConnectionRecord`` with a live connection.

    Expected:
        - ``TypeError`` mentioning 'snapshot' is raised.
    """
    from bscontroller.hci.records import ConnectionRecord

    with pytest.raises(TypeError, match='snapshot'):
        ConnectionRecord(host.controller, host.connection())


def test_record_without_self_address_reports_public_address(host):
    """A record with no self address falls back to the public address.

    Purpose: Verify that when a connection has no explicit self address, its
    record reports the Controller's public address.

    Steps:
        1. Establish a connection with no self address and fetch its record.

    Expected:
        - ``connection.self_address`` is ``None`` and the record's self address
          equals the Controller's public address.
    """
    connection = host.connection()
    record = host.controller.find_connection_by_handle(connection.handle)
    assert connection.self_address is None
    assert record.self_address == host.controller.public_address


# ---------------------------------------------------------------------------
# Initiation admission (7.8.12, 7.8.13, 7.8.66)
# ---------------------------------------------------------------------------
def test_create_connection_capacity_and_duplicates(host):
    """Create Connection rejects duplicates and respects the connection limit.

    Purpose: Verify Create Connection refuses a peer that already has a
    connection and refuses a new attempt once the Controller's connection
    capacity is reached (Vol 4, Part E, 7.8.12).

    Steps:
        1. Establish one connection, then attempt Create Connection to the same
           peer.
        2. Fill the remaining capacity, then attempt one more.

    Expected:
        - The duplicate returns CONNECTION_ALREADY_EXISTS; the over-capacity
          attempt returns CONNECTION_LIMIT_EXCEEDED.
    """
    first = host.connection()
    host.cmd_status(
        create_connection(peer_address=first.peer),
        ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR,
    )
    template = host.link.connections[first.handle]
    for handle in range(2, ControllerConfig().max_connections + 1):
        host.link.connections[handle] = template
    host.cmd_status(create_connection(), ErrorCode.CONNECTION_LIMIT_EXCEEDED_ERROR)


@pytest.mark.parametrize('extended', [False, True])
def test_rejected_create_connection_preserves_pending_attempt(host, extended):
    """A rejected second Create Connection leaves the pending attempt intact.

    Purpose: Verify that a Create Connection refused because one is already
    pending does not disturb the existing initiating attempt or the link's
    random state, for legacy and extended forms (Vol 4, Part E, 7.8.12,
    7.8.66).

    Steps:
        1. Start Create Connection and snapshot the attempt and random state.
        2. Send a second Create Connection expecting COMMAND_DISALLOWED.

    Expected:
        - The second returns COMMAND_DISALLOWED; the initiating object, its peer
          /start/windows and the random state are unchanged.
    """
    host.cmd_status(create_connection(extended=extended))
    attempt = host.link.initiating
    snapshot = (attempt.peer, attempt.start_us, attempt.windows)
    state = host.link.random.getstate()
    host.cmd_status(
        create_connection(extended=extended), ErrorCode.COMMAND_DISALLOWED_ERROR
    )
    assert host.link.initiating is attempt
    assert (attempt.peer, attempt.start_us, attempt.windows) == snapshot
    assert host.link.random.getstate() == state


@pytest.mark.parametrize('extended', [False, True])
def test_initiating_templates_allocate_no_wire_identity(host, extended):
    """Initiating templates reserve no Access Address until a peer responds.

    Purpose: Verify Create Connection builds per-PHY templates with zeroed
    wire-identity fields (Access Address, CRCInit, hop) and consumes no
    randomness, so no identity is allocated before a connectable response
    (Vol 4, Part E, 7.8.12, 7.8.66).

    Steps:
        1. Snapshot the link's random state.
        2. Start Create Connection and inspect each PHY template.

    Expected:
        - Every template has zero Access Address, CRCInit and hop; the random
          state is unchanged.
    """
    state = host.link.random.getstate()
    host.cmd_status(create_connection(extended=extended))
    for parameters in host.link.initiating.phy_parameters.values():
        assert (parameters.access_address, parameters.crc_init, parameters.hop) == (
            0,
            0,
            0,
        )
    assert host.link.random.getstate() == state


@pytest.mark.parametrize(
    'changes',
    [
        dict(initiator_filter_policy=2),
        dict(own_address_type=4),
        dict(peer_address_type=2),
    ],
)
def test_create_connection_unsupported_values(host, changes):
    """Create Connection rejects unsupported parameter values.

    Purpose: Verify Create Connection refuses unsupported filter policy, own
    address type and peer address type values, leaving initiating disabled
    (Vol 4, Part E, 7.8.12).

    Steps:
        1. Send Create Connection with the parametrized unsupported change.

    Expected:
        - Command Status is UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE and
          initiating is not enabled.
    """
    host.cmd_status(
        create_connection(**changes),
        ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
    )
    assert not host.ll.initiating_enabled


def test_create_connection_cancel_reports_unknown_connection(host):
    """Create Connection Cancel reports an unknown-connection Complete.

    Purpose: Verify cancelling a pending Create Connection produces an Enhanced
    Connection Complete with Unknown Connection Identifier for the intended
    peer, and that a second cancel is disallowed (Vol 4, Part E, 7.8.13).

    Steps:
        1. Start Create Connection, then send Create Connection Cancel.
        2. Send Create Connection Cancel again.

    Expected:
        - The event status is UNKNOWN_CONNECTION_IDENTIFIER for the intended
          peer; the second cancel returns COMMAND_DISALLOWED.
    """
    host.cmd_status(create_connection())
    host.cmd(hci.HCI_LE_Create_Connection_Cancel_Command())
    event = host.ut_rx(hci.HCI_LE_Enhanced_Connection_Complete_Event)
    assert (
        event.status == UNKNOWN
        and event.peer_address == create_connection().peer_address
    )
    host.cmd(
        hci.HCI_LE_Create_Connection_Cancel_Command(),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )
