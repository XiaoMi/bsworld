# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Command dispatch, Controller information, Reset and event masks (Core Vol 4,
Part E, 4.5, 7.3.1-7.3.2, 7.3.69, 7.4, 7.8.1-7.8.3, 7.8.27, 3.1.1).

Matrix: {supported, unsupported, malformed} commands x {sync, async} framing;
information commands x return values; {power-on, Reset} x default state;
{event mask, page 2, LE mask} x {masked, unmasked}; legacy/extended command
families after Reset.
"""

import logging

import pytest

from bscontroller import hci
from bscontroller.config import ControllerConfig
from bscontroller.errors import InvalidPacketError
from bscontroller.hci.backend import RadioBackend
from bscontroller.hci.controller import Controller
from bscontroller.linklayer import (
    PDU_DATA_PAYLOAD_SIZE_MAX,
    PDU_DATA_PAYLOAD_TIME_CODED_MAX_US,
)
from tests.unit.hci.harness import ALL, IUT_ADDRESS, ErrorCode, HciHarness
from tests.unit.linklayer.air.harness import ScriptedRadio

# BR/EDR opcodes this LE-only Controller does not implement (Vol 4, Part E, 7.1).
INQUIRY = hci.hci_command_op_code(0x01, 0x0001)
CREATE_CONNECTION = hci.hci_command_op_code(0x01, 0x0005)
READ_LOCAL_EXTENDED_FEATURES = hci.hci_command_op_code(0x04, 0x0004)

# Core Vol 2, Part H, 7.6: the P-256 generator is the public key for scalar 1.
P256_GENERATOR_X = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
P256_GENERATOR_Y = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
P256_FIELD_PRIME = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
P256_Y_AT_ZERO_X = 0x66485C780E2F83D72433BD5D84A06BB6541C2AF31DAE871728BF856A174F93F4


# ---------------------------------------------------------------------------
# Dispatch and framing (4.5)
# ---------------------------------------------------------------------------
def test_command_complete_framing_on_the_wire(host):
    """Command Complete is framed correctly for a simple read command.

    Purpose: Verify a Host command answered by Command Complete produces one
    well-formed event on the wire with the right opcode and return parameters
    (Vol 4, Part E, 4.5, 7.4.6).

    Steps:
        1. Send Read BD_ADDR.

    Expected:
        - The single event is a Command Complete with one allowed command
          packet, the Read BD_ADDR opcode, and the IUT public address.
    """
    host.send(hci.HCI_Read_BD_ADDR_Command())
    (raw,) = host.events
    assert isinstance(raw, hci.HCI_Command_Complete_Event)
    assert raw.num_hci_command_packets == 1
    assert raw.command_opcode == hci.HCI_READ_BD_ADDR_COMMAND
    assert raw.return_parameters.bd_addr == IUT_ADDRESS


@pytest.mark.parametrize(
    'opcode,parameters,asynchronous',
    [
        (READ_LOCAL_EXTENDED_FEATURES, b'', False),
        (READ_LOCAL_EXTENDED_FEATURES, bytes(255), False),
        # Without a schema the Controller cannot know the opcode is asynchronous;
        # Command Complete with Unknown HCI Command is a valid response (4.5).
        (CREATE_CONNECTION, b'\xff', False),
        (INQUIRY, bytes(5), False),
        (hci.HCI_LE_CS_READ_LOCAL_SUPPORTED_CAPABILITIES_COMMAND, b'\xff', False),
        (hci.HCI_LE_CS_READ_REMOTE_SUPPORTED_CAPABILITIES_COMMAND, b'', True),
    ],
)
def test_unsupported_command_is_rejected_before_decoding(
    host, opcode, parameters, asynchronous
):
    """Unsupported opcodes are refused with Unknown HCI Command before decoding.

    Purpose: Verify the Controller rejects commands it does not implement,
    answering Command Status for known-async opcodes and Command Complete
    (Status = Unknown HCI Command) otherwise, without decoding parameters
    (Vol 4, Part E, 4.5).

    Steps:
        1. For each parametrized opcode and payload, send a raw HCI_Command.

    Expected:
        - One command packet is allowed; async opcodes get Command Status with
          UNKNOWN_HCI_COMMAND, others get Command Complete whose first status
          octet is UNKNOWN_HCI_COMMAND; the Host queue is then empty.
    """
    reply = host.reply(hci.HCI_Command(parameters, op_code=opcode))
    assert reply.num_hci_command_packets == 1
    if asynchronous:
        assert isinstance(reply, hci.HCI_Command_Status_Event)
        assert reply.status == ErrorCode.UNKNOWN_HCI_COMMAND_ERROR
    else:
        assert isinstance(reply, hci.HCI_Command_Complete_Event)
        # The Host decodes the reply without a schema: Status is the first octet.
        parameters = reply.return_parameters
        status = getattr(parameters, 'status', None)
        assert (status if status is not None else bytes(parameters.data)[0]) == (
            ErrorCode.UNKNOWN_HCI_COMMAND_ERROR
        )
    host.ut_rx_q_is_empty()


def test_unknown_opcode_with_registered_class_is_still_unknown(host):
    """A command the codec knows but the Controller lacks is still Unknown.

    Purpose: Verify that removing an opcode from ``supported_commands`` makes the
    Controller answer Unknown HCI Command even though the codec has a class for
    it (Vol 4, Part E, 4.5).

    Steps:
        1. Discard LE Rand from the Controller's supported commands.
        2. Send LE Rand expecting Unknown HCI Command.

    Expected:
        - The command is answered with status UNKNOWN_HCI_COMMAND.
    """
    host.controller.supported_commands.discard(hci.HCI_LE_RAND_COMMAND)
    host.cmd(hci.HCI_LE_Rand_Command(), ErrorCode.UNKNOWN_HCI_COMMAND_ERROR)


@pytest.mark.parametrize(
    'opcode,parameters',
    [
        (hci.HCI_RESET_COMMAND, b'\x01'),
        (hci.HCI_SET_EVENT_MASK_COMMAND, bytes(7)),
        (hci.HCI_SET_EVENT_MASK_COMMAND, bytes(9)),
        (hci.HCI_LE_SET_ADVERTISING_DATA_COMMAND, b'\x01x'),
        (hci.HCI_LE_SET_SCAN_RESPONSE_DATA_COMMAND, b'\x20' + bytes(31)),
        (hci.HCI_LE_SET_EXTENDED_ADVERTISING_DATA_COMMAND, b'\x00\x03\x00\x03ab'),
        (hci.HCI_LE_SET_EXTENDED_ADVERTISING_ENABLE_COMMAND, b'\x01\x02' + bytes(4)),
        (hci.HCI_LE_SET_EXTENDED_SCAN_PARAMETERS_COMMAND, b'\x00\x00\x01' + bytes(6)),
        (hci.HCI_LE_SET_PERIODIC_ADVERTISING_SUBEVENT_DATA_COMMAND, b'\x08\x01'),
        (hci.HCI_LE_SET_PERIODIC_ADVERTISING_RESPONSE_DATA_COMMAND, b'\x34\x02'),
        (hci.HCI_LE_SET_PERIODIC_SYNC_SUBEVENT_COMMAND, b'\x34\x02\x00\x00\x02\x01'),
    ],
)
def test_invalid_command_parameter_length_returns_command_complete(
    host, opcode, parameters
):
    """A complete H4 command with invalid parameters fails before execution (4.5.1).

    Steps:
        1. Send a synchronous command with a missing or extra field, an invalid
           length prefix, or a record count that disagrees with its payload.
        2. Issue Read BD_ADDR on the same transport.

    Expected:
        - One Command Complete returns 0x12 for the submitted opcode.
        - The event mask and advertising command family retain their initial values.
          The subsequent read succeeds.
    """
    host.cmd(
        hci.HCI_Command(parameters, op_code=opcode),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    assert host.controller.event_mask == ALL
    assert host.controller.le_procedures.extended_commands is None
    assert host.cmd(hci.HCI_Read_BD_ADDR_Command()).bd_addr == IUT_ADDRESS
    host.ut_rx_q_is_empty()


@pytest.mark.parametrize(
    'opcode,identifier',
    [
        (hci.HCI_LE_SET_DATA_LENGTH_COMMAND, b'\x34\x02'),
        (hci.HCI_LE_SET_PERIODIC_ADVERTISING_SUBEVENT_DATA_COMMAND, b'\x08'),
        (hci.HCI_LE_SET_PERIODIC_SYNC_SUBEVENT_COMMAND, b'\x34\x02'),
    ],
)
def test_invalid_command_echoes_the_supplied_handle(host, opcode, identifier):
    """Command Complete carries the supplied handle after Status (4.5.1).

    Steps:
        1. Supply only the connection, advertising or sync handle of the command.

    Expected:
        - Status is 0x12 and the handle octets match the command on the wire.
    """
    result = host.cmd(
        hci.HCI_Command(identifier, op_code=opcode),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    assert result.wire[7 : 7 + len(identifier)] == identifier


@pytest.mark.parametrize(
    'opcode,parameters',
    [
        (hci.HCI_DISCONNECT_COMMAND, b'\x01'),
        (hci.HCI_LE_CREATE_CIS_COMMAND, b'\x02' + bytes(4)),
        (hci.HCI_LE_EXTENDED_CREATE_CONNECTION_COMMAND, bytes(9) + b'\x01' + bytes(15)),
        (
            hci.HCI_LE_EXTENDED_CREATE_CONNECTION_V2_COMMAND,
            bytes(11) + b'\x01' + bytes(17),
        ),
    ],
)
def test_invalid_async_command_only_returns_command_status(host, opcode, parameters):
    """An asynchronous command rejected at dispatch has no completion event (4.5.1).

    Steps:
        1. Send an incomplete Disconnect, CIS array, or Extended Create
           Connection PHY record within a complete H4 frame.

    Expected:
        - Command Status returns 0x12. No initiation starts or further event arrives.
    """
    reply = host.cmd_status(
        hci.HCI_Command(parameters, op_code=opcode),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    assert reply.num_hci_command_packets == 1
    assert host.link.initiating is None
    host.ut_rx_q_is_empty()


def test_legacy_advertising_data_ignores_unused_padding(host):
    """Only the declared octets of a padded legacy field reach the advertiser.

    Steps:
        1. Send two octets of advertising data with nonzero unused padding.

    Expected:
        - The Controller stores the two declared octets.
    """
    parameters = b'\x02ab' + b'\xff' * 29
    host.cmd(
        hci.HCI_Command(parameters, op_code=hci.HCI_LE_SET_ADVERTISING_DATA_COMMAND)
    )
    assert host.controller.le_legacy_advertiser.advertising_data == b'ab'


def test_frame_length_mismatch_is_rejected(host):
    """A command whose H4 length disagrees with its body is rejected.

    Purpose: Verify the Controller raises on a malformed command packet whose
    declared parameter length does not match the payload, emitting no event.

    Steps:
        1. Corrupt the length octet of supported synchronous and asynchronous
           commands, and of an unsupported command.
        2. Feed each frame directly to ``on_packet``.

    Expected:
        - ``InvalidPacketError`` is raised and the Host queue stays empty.
    """
    for opcode in (
        hci.HCI_RESET_COMMAND,
        hci.HCI_LE_READ_LOCAL_P_256_PUBLIC_KEY_COMMAND,
        INQUIRY,
    ):
        packet = bytearray(bytes(hci.HCI_Command(b'', op_code=opcode)))
        packet[3] = 1
        with pytest.raises(InvalidPacketError):
            host.controller.on_packet(bytes(packet))
    host.ut_rx_q_is_empty()


def test_event_packet_from_host_is_ignored(host):
    """An event packet sent by the Host is silently ignored.

    Purpose: Verify the Controller drops an HCI event received from the Host
    (events flow Controller to Host only), producing nothing back.

    Steps:
        1. Send a Command Complete event as if from the Host.

    Expected:
        - The Host queue remains empty.
    """
    host.send(hci.HCI_Command_Complete_Event(1, 0, hci.HCI_StatusReturnParameters(0)))
    host.ut_rx_q_is_empty()


def test_controller_requires_a_backend():
    """A Controller cannot be constructed without a backend.

    Purpose: Verify ``Controller`` requires a backend argument, since it has no
    radio or link to operate without one.

    Steps:
        1. Construct a Controller with no backend inside an event loop.

    Expected:
        - ``TypeError`` mentioning 'backend' is raised.
    """
    import asyncio

    async def create():
        return Controller('missing')

    with pytest.raises(TypeError, match='backend'):
        asyncio.run(create())


def test_backend_belongs_to_one_controller():
    """A backend can be bound to only one Controller.

    Purpose: Verify a ``RadioBackend`` instance may be attached to a single
    Controller; a second Controller reusing it is rejected.

    Steps:
        1. Create one Controller with a backend.
        2. Create a second Controller with the same backend.

    Expected:
        - The second construction raises ``ValueError`` mentioning 'one
          Controller'.
    """
    import asyncio

    backend = RadioBackend(ScriptedRadio())

    async def create(name):
        return Controller(name, backend=backend)

    asyncio.run(create('first'))
    with pytest.raises(ValueError, match='one Controller'):
        asyncio.run(create('second'))


def test_radio_trace_formats_packet_only_when_debug_logging_is_enabled(host, caplog):
    """The radio hot path formats the H4 Controller's trace only for a debug reader."""
    calls = []

    class Pdu(bytes):
        def hex(self):
            calls.append(self)
            return super().hex()

    pdu = Pdu(b'\x02\x00')
    with caplog.at_level(logging.INFO, logger='bscontroller.hci.controller'):
        host.loop.run_until_complete(host.link.transmit(37, pdu))
    assert not calls
    with caplog.at_level(logging.DEBUG, logger='bscontroller.hci.controller'):
        host.loop.run_until_complete(host.link.transmit(37, pdu))
    assert calls == [pdu]
    assert 'RADIO TX ch=37 pdu=0200' in caplog.text


def test_controller_without_host_sink_drops_events():
    """With no Host sink the Controller handles commands but emits nothing.

    Purpose: Verify a Controller whose Host sink is ``None`` processes a command
    without error and produces no outgoing events.

    Steps:
        1. Build a harness, clear the Controller's host sink.
        2. Feed a Reset command and drain.

    Expected:
        - No events are produced and the host sink stays ``None``.
    """
    harness = HciHarness(unmask=False)
    harness.controller.host = None
    harness.controller.on_packet(bytes(hci.HCI_Reset_Command()))
    harness._drain()
    assert not harness.events and harness.controller.host is None
    harness.close()


# ---------------------------------------------------------------------------
# Controller information (7.4, 7.8.2, 7.8.3, 7.8.27, 7.8.46)
# ---------------------------------------------------------------------------
def test_read_local_version(host):
    """Read Local Version returns the Controller's own version fields.

    Purpose: Verify Read Local Version Information reports the Controller's HCI
    and LMP versions and manufacturer identifier (Vol 4, Part E, 7.4.1).

    Steps:
        1. Send Read Local Version Information.

    Expected:
        - The returned HCI/LMP versions and company identifier match the
          Controller's configuration.
    """
    result = host.cmd(hci.HCI_Read_Local_Version_Information_Command())
    config = host.controller.config
    assert (result.hci_version, result.lmp_version) == (
        config.hci_version,
        config.lmp_version,
    )
    assert result.company_identifier == config.manufacturer


def test_read_supported_commands_matches_dispatch(host):
    """The Supported Commands mask matches the dispatch table.

    Purpose: Verify Read Local Supported Commands sets the bit for every opcode
    the Controller actually dispatches and leaves clear the bit for an
    unsupported one (Vol 4, Part E, 7.4.2).

    Steps:
        1. Read the supported-commands mask.
        2. Resolve the command class, handler and mask bit for each supported
           opcode. Inspect one unsupported opcode's bit.

    Expected:
        - Each supported opcode has a decoder and a dedicated handler. Its defined
          mask bit is set. The LE CS Read Local Supported Capabilities bit is clear.
    """
    result = host.cmd(hci.HCI_Read_Local_Supported_Commands_Command())
    mask = int.from_bytes(result.supported_commands, 'little')
    for opcode in host.controller.supported_commands:
        command = hci.HCI_Command.command_classes[opcode]
        name = f'on_{command.name.lower()}'
        handler = getattr(
            host.controller.le_procedures, name, getattr(host.controller, name, None)
        )
        assert callable(handler), name
        # Read Local Supported Commands itself has no bit in Section 6.27.
        if opcode != hci.HCI_READ_LOCAL_SUPPORTED_COMMANDS_COMMAND:
            assert mask & hci.HCI_SUPPORTED_COMMANDS_MASKS[opcode], hex(opcode)
    unsupported = hci.HCI_SUPPORTED_COMMANDS_MASKS[
        hci.HCI_LE_CS_READ_LOCAL_SUPPORTED_CAPABILITIES_COMMAND
    ]
    assert not mask & unsupported


def test_le_features_advertise_their_required_commands(host):
    """Implemented LE features advertise their commands (Vol 4, Part E, 3 and 6.27).

    Steps:
        1. Read All Local Supported Features and Read Local Supported Commands.
        2. Check the feature bit and the corresponding command bits assigned by Core.

    Expected:
        - Both CIS roles, Path Loss Monitoring, Subrating, Monitoring Advertisers
          and Frame Space Update advertise their feature-specific commands.
    """
    features = int.from_bytes(
        host.cmd(hci.HCI_LE_Read_All_Local_Supported_Features_Command()).le_features,
        'little',
    )
    commands = host.cmd(
        hci.HCI_Read_Local_Supported_Commands_Command()
    ).supported_commands
    # Command bits (octet, mask) for each Core condition in Section 6.27.
    required = {
        hci.LeFeature.CONNECTED_ISOCHRONOUS_STREAM_CENTRAL: (
            (41, 0x80),
            (42, 0x07),
        ),  # C.39
        hci.LeFeature.CONNECTED_ISOCHRONOUS_STREAM_PERIPHERAL: ((42, 0x18),),  # C.40
        hci.LeFeature.LE_PATH_LOSS_MONITORING: ((44, 0x60),),  # C.52
        hci.LeFeature.CONNECTION_SUBRATING: ((46, 0x03),),  # C.57
        hci.LeFeature.MONITORING_ADVERTISERS: (
            (28, 0x80),
            (47, 0xE0),
            (48, 0x01),
        ),  # C.78
        hci.LeFeature.FRAME_SPACE_UPDATE: ((48, 0x02),),  # C.79
    }
    for feature, fields in required.items():
        assert features & (1 << feature), feature.name
        for octet, mask in fields:
            assert commands[octet] & mask == mask, (feature.name, octet)


def test_read_local_supported_features_is_le_only(host):
    """Local Supported Features advertise LE-only, BR/EDR not supported.

    Purpose: Verify Read Local Supported Features reports the LE-supported and
    BR/EDR-not-supported LMP feature bits (Vol 4, Part E, 7.4.3).

    Steps:
        1. Read the LMP feature mask.

    Expected:
        - Both LE_SUPPORTED_CONTROLLER and BR_EDR_NOT_SUPPORTED bits are set.
    """
    result = host.cmd(hci.HCI_Read_Local_Supported_Features_Command())
    features = int.from_bytes(result.lmp_features, 'little')
    assert features & hci.LmpFeatureMask.LE_SUPPORTED_CONTROLLER
    assert features & hci.LmpFeatureMask.BR_EDR_NOT_SUPPORTED


def test_read_le_features_and_states(host):
    """LE features and supported states report the expected capabilities.

    Purpose: Verify LE Read Local Supported Features advertises the expected LE
    feature bits and LE Read Supported States returns a non-empty states mask
    (Vol 4, Part E, 7.8.3, 7.8.27).

    Steps:
        1. Read the LE feature mask and check the expected bits.
        2. Read the supported states.

    Expected:
        - Encryption, extended/periodic advertising, Periodic Advertising ADI
          Support, both Periodic Advertising Sync Transfer roles, 2M/Coded PHY
          and the minimum-used-channels feature are set; the states field is 8
          octets and not all zero.
    """
    features = int.from_bytes(
        host.cmd(hci.HCI_LE_Read_Local_Supported_Features_Command()).le_features,
        'little',
    )
    for bit in (
        hci.LeFeatureMask.LE_ENCRYPTION,
        hci.LeFeatureMask.LE_EXTENDED_ADVERTISING,
        hci.LeFeatureMask.LE_PERIODIC_ADVERTISING,
        hci.LeFeatureMask.PERIODIC_ADVERTISING_ADI_SUPPORT,
        hci.LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_SENDER,
        hci.LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_RECIPIENT,
        hci.LeFeatureMask.LE_2M_PHY,
        hci.LeFeatureMask.LE_CODED_PHY,
        hci.LeFeatureMask.MINIMUM_NUMBER_OF_USED_CHANNELS_PROCEDURE,
        hci.LeFeatureMask.REMOTE_PUBLIC_KEY_VALIDATION,
        hci.LeFeatureMask.LE_POWER_CONTROL_REQUEST,
        hci.LeFeatureMask.LE_POWER_CONTROL_REQUEST_DUP,
        hci.LeFeatureMask.LE_PATH_LOSS_MONITORING,
    ):
        assert features & bit, bit
    states = host.cmd(hci.HCI_LE_Read_Supported_States_Command()).le_states
    assert len(states) == 8 and any(states)


def test_le_p256_key_generation_and_reset(host):
    """Exercise the generated key lifecycle and both HCI DHKey command versions.

    Core Vol 4, Part E, 7.8.36–37 and Vol 3, Part H, 2.3.5.6.1. The peer's
    scalar of 1 makes the expected secret the Controller's public X coordinate.
    The debug-key command must use the independently specified debug key.
    """
    peer = (
        P256_GENERATOR_X.to_bytes(32, 'little'),
        P256_GENERATOR_Y.to_bytes(32, 'little'),
    )
    read = hci.HCI_LE_Read_Local_P_256_Public_Key_Command()
    public_event = hci.HCI_LE_Read_Local_P_256_Public_Key_Complete_Event
    dhkey_event = hci.HCI_LE_Generate_DHKey_Complete_Event
    v1 = hci.HCI_LE_Generate_DHKey_Command(*peer)
    v2_debug = hci.HCI_LE_Generate_DHKey_V2_Command(*peer, 1)
    debug_x = 0x20B003D2F297BE2C5E2C83A7E9F9A5B9EFF49111ACF4FDDBCC0301480E359DE6

    host.cmd_status(v1, ErrorCode.COMMAND_DISALLOWED_ERROR)
    host.ut_rx_q_is_empty()
    host.cmd_status(v2_debug)
    host.ut_rx(dhkey_event, status=0, dh_key=debug_x.to_bytes(32, 'little'))

    host.cmd_status(read)
    first = host.ut_rx(public_event, status=0)
    host.cmd_status(v1)
    host.ut_rx(dhkey_event, status=0, dh_key=first.key_x_coordinate)

    host.cmd_status(read)
    second = host.ut_rx(public_event, status=0)
    assert second.key_x_coordinate != first.key_x_coordinate
    host.cmd_status(hci.HCI_LE_Generate_DHKey_V2_Command(*peer, 0))
    host.ut_rx(dhkey_event, status=0, dh_key=second.key_x_coordinate)
    host.cmd_status(
        hci.HCI_LE_Generate_DHKey_V2_Command(*peer, 2),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    host.ut_rx_q_is_empty()

    host.cmd(hci.HCI_Reset_Command())
    host.cmd_status(v1, ErrorCode.COMMAND_DISALLOWED_ERROR)
    host.ut_rx_q_is_empty()


@pytest.mark.parametrize(
    'x,y',
    [
        pytest.param(0, 0, id='zero-point'),
        pytest.param(P256_GENERATOR_X, P256_GENERATOR_Y ^ 1, id='off-curve'),
        pytest.param(P256_FIELD_PRIME, P256_Y_AT_ZERO_X, id='x-out-of-range'),
    ],
)
def test_le_dhkey_rejects_invalid_remote_key(host, x, y):
    """Reject invalid P-256 public keys and return the all-ones DHKey sentinel.

    Core Vol 3, Part H, 2.3.5.6.1 and Vol 4, Part E, 7.8.37/7.7.65.9.
    The range vector would be valid if its X coordinate were reduced modulo p.
    """
    coordinates = (x.to_bytes(32, 'little'), y.to_bytes(32, 'little'))
    host.cmd_status(hci.HCI_LE_Read_Local_P_256_Public_Key_Command())
    host.ut_rx(hci.HCI_LE_Read_Local_P_256_Public_Key_Complete_Event, status=0)
    for command in (
        hci.HCI_LE_Generate_DHKey_Command(*coordinates),
        hci.HCI_LE_Generate_DHKey_V2_Command(*coordinates, 1),
    ):
        host.cmd_status(command)
        host.ut_rx(
            hci.HCI_LE_Generate_DHKey_Complete_Event,
            status=ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
            dh_key=b'\xff' * 32,
        )
    host.ut_rx_q_is_empty()


def test_le_p256_rejects_incorrect_parameter_lengths(host):
    """Reject malformed key commands before starting P-256 work (7.8.36–37)."""
    for opcode, parameters in (
        (hci.HCI_LE_READ_LOCAL_P_256_PUBLIC_KEY_COMMAND, b'\x00'),
        (hci.HCI_LE_GENERATE_DHKEY_COMMAND, bytes(63)),
        (hci.HCI_LE_GENERATE_DHKEY_V2_COMMAND, bytes(64)),
    ):
        reply = host.reply(hci.HCI_Command(parameters, op_code=opcode))
        assert isinstance(reply, hci.HCI_Command_Status_Event)
        assert reply.status == ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
    host.ut_rx_q_is_empty()


def test_host_subrating_feature_updates_both_hci_feature_reads(host):
    """Report the Host's Subrating opt-in in Page 0 and the full FeatureSet.

    Purpose: Keep the Controller capability independent of Host opt-in
    across both reads and Reset (Vol 4, Part E, 7.8.115; Vol 6, Part B, 4.6.33).

    Steps:
        1. Read both feature bitmaps at power-on.
        2. Set Host Support and read both bitmaps again.
        3. Reset and read both bitmaps.

    Expected:
        - Both reads always report Controller Subrating support.
        - Both reads report Host Support only after the successful command.
    """
    controller = hci.LeFeatureMask.CONNECTION_SUBRATING
    host_support = hci.LeFeatureMask.CONNECTION_SUBRATING_HOST_SUPPORT
    reads = (
        hci.HCI_LE_Read_Local_Supported_Features_Command,
        hci.HCI_LE_Read_All_Local_Supported_Features_Command,
    )

    def feature_masks():
        return [
            int.from_bytes(host.cmd(read()).le_features, 'little')
            & (controller | host_support)
            for read in reads
        ]

    assert feature_masks() == [controller] * 2
    host.cmd(
        hci.HCI_LE_Set_Host_Feature_Command(
            hci.LeFeature.CONNECTION_SUBRATING_HOST_SUPPORT, 1
        )
    )
    assert feature_masks() == [controller | host_support] * 2
    host.cmd(hci.HCI_Reset_Command())
    assert feature_masks() == [controller] * 2


def test_buffer_and_data_length_capabilities(host):
    """LE buffer size and maximum data length report the capability constants.

    Purpose: Verify LE Read Buffer Size and LE Read Maximum Data Length report
    the Controller's ACL buffer size/count and maximum TX octets/time
    (Vol 4, Part E, 7.8.2, 7.8.46).

    Steps:
        1. Read the LE buffer size.
        2. Read the maximum data length.

    Expected:
        - The buffer length and count equal the ``ControllerConfig`` defaults; the
          maximum TX octets/time equal the payload-size and coded-time maxima.
    """
    buffers = host.cmd(hci.HCI_LE_Read_Buffer_Size_Command())
    assert buffers.le_acl_data_packet_length == ControllerConfig().acl_packet_length
    assert buffers.total_num_le_acl_data_packets == 8
    maximum = host.cmd(hci.HCI_LE_Read_Maximum_Data_Length_Command())
    assert (maximum.supported_max_tx_octets, maximum.supported_max_tx_time) == (
        PDU_DATA_PAYLOAD_SIZE_MAX,
        PDU_DATA_PAYLOAD_TIME_CODED_MAX_US,
    )


def test_capabilities_are_per_controller():
    """Each Controller instance owns its own supported-commands set.

    Purpose: Verify mutating one Controller's supported commands does not affect
    another's, so capabilities are per instance.

    Steps:
        1. Build two harnesses and discard LE Rand from the first's commands.

    Expected:
        - LE Rand is still supported by the second Controller.
    """
    first, second = HciHarness(unmask=False), HciHarness(unmask=False)
    first.controller.supported_commands.discard(hci.HCI_LE_RAND_COMMAND)
    assert hci.HCI_LE_RAND_COMMAND in second.controller.supported_commands
    first.close()
    second.close()


def test_le_rand_and_encrypt(host):
    """LE Rand yields fresh values and LE Encrypt matches the spec e() vector.

    Purpose: Verify LE Rand returns distinct 8-octet values and LE Encrypt
    computes AES-128 e() matching the Core sample data (Vol 4, Part E, 7.8.23,
    7.8.22; Vol 6, Part C, 1.1).

    Steps:
        1. Read two random numbers and compare.
        2. Encrypt the sample plaintext with the sample key.

    Expected:
        - The two random numbers are 8 octets and differ; the ciphertext equals
          the spec's expected e() output.
    """
    first = host.cmd(hci.HCI_LE_Rand_Command()).random_number
    assert (
        len(first) == 8 and first != host.cmd(hci.HCI_LE_Rand_Command()).random_number
    )
    # Vol 6, Part C, 1.1: e() sample data.
    key = bytes.fromhex('4C68384139F574D836BCF34E9DFB01BF')[::-1]
    plaintext = bytes.fromhex('0213243546576879acbdcedfe0f10213')[::-1]
    result = host.cmd(hci.HCI_LE_Encrypt_Command(key, plaintext))
    assert result.encrypted_data[::-1] == bytes.fromhex(
        '99ad1b5226a37e3e058e3b8e27c2c666'
    )


# ---------------------------------------------------------------------------
# Power-on and Reset defaults (7.3.1, 7.3.2, 7.8.1, 7.8.5, 7.8.10)
# ---------------------------------------------------------------------------
def assert_defaults(harness):
    controller = harness.controller
    assert controller.event_mask == hci.HCI_EVENT_MASK_DEFAULT
    assert controller.event_mask_page_2 == 0
    assert controller.le_event_mask == hci.HCI_LE_EVENT_MASK_DEFAULT
    advertiser = controller.le_legacy_advertiser
    assert (
        advertiser.advertising_interval_min
        == advertiser.advertising_interval_max
        == 0x0800
    )
    assert advertiser.advertising_channel_map == 0x07
    assert not advertiser.enabled and advertiser.advertising_data == b''
    scan = harness.ll.scan_parameters
    assert scan.interval_us == scan.window_us == 0x10 * hci.HCI_LE_SCAN_INTERVAL_UNIT_US
    assert harness.ll.default_data_length() == (27, 328)


def test_power_on_defaults(fresh):
    """A freshly powered Controller exposes the specified defaults.

    Purpose: Verify a Controller that has received no commands holds the
    power-on default event masks, advertising, scan and data-length values
    (Vol 4, Part E, 7.3.1, 7.8.1, 7.8.5, 7.8.10).

    Steps:
        1. Run ``assert_defaults`` against a fresh harness.

    Expected:
        - Every default checked by ``assert_defaults`` holds.
    """
    assert_defaults(fresh)


def test_reset_restores_defaults_and_drops_all_state(host):
    """Reset returns the Controller to defaults and discards all state.

    Purpose: Verify HCI Reset clears connections, accept list, advertisers and
    configured parameters back to power-on defaults and raises no Disconnection
    Complete (Vol 4, Part E, 7.3.2).

    Steps:
        1. Establish a connection and set advertising, data length and a filter
           entry.
        2. Send Reset.

    Expected:
        - Defaults hold again; connections, accept list and advertisers are
          empty; the prior handle is no longer found; the Host queue is empty.
    """
    connection = host.connection()
    host.cmd(
        hci.HCI_LE_Set_Advertising_Parameters_Command(
            0x20, 0x30, 0, 0, 0, IUT_ADDRESS, 1, 0
        )
    )
    host.cmd(hci.HCI_LE_Set_Advertising_Data_Command(b'data'))
    host.cmd(hci.HCI_LE_Write_Suggested_Default_Data_Length_Command(251, 2120))
    host.cmd(hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(0, IUT_ADDRESS))
    host.cmd(hci.HCI_Reset_Command())
    assert_defaults(host)
    assert not host.link.connections and not host.controller.le_connections
    assert not host.link.accept_list and not host.link.advertisers
    assert host.controller.find_connection_by_handle(connection.handle) is None
    # Reset itself raises no Disconnection Complete (7.3.2).
    host.ut_rx_q_is_empty()


def test_reset_replays_seeded_randomness(host):
    """Reset reseeds randomness so LE Rand replays its first value.

    Purpose: Verify that after Reset the Controller's random source is reseeded,
    so the first LE Rand value after Reset equals the first value before it,
    keeping scenarios reproducible.

    Steps:
        1. Read an LE Rand value.
        2. Reset and read LE Rand again.

    Expected:
        - The post-Reset random number equals the first one.
    """
    first = host.cmd(hci.HCI_LE_Rand_Command()).random_number
    host.cmd(hci.HCI_Reset_Command())
    assert host.cmd(hci.HCI_LE_Rand_Command()).random_number == first


# ---------------------------------------------------------------------------
# Event masks (7.3.1, 7.3.69, 7.8.1)
# ---------------------------------------------------------------------------
def disconnection(handle=1):
    return hci.HCI_Disconnection_Complete_Event(0, handle, 0x13)


def test_frame_space_update_event_fields_on_the_wire(host):
    """Encode the negotiated frame space and affected PHYs in LE Meta 0x35."""
    connection = host.connection()
    host.notify('on_frame_space_updated', connection, 0, 2, 165, 2, 4)
    event = host.ut_rx(
        hci.HCI_LE_Frame_Space_Update_Complete_Event,
        status=0,
        connection_handle=connection.handle,
        initiator=2,
        frame_space=165,
        phys=2,
        spacing_types=4,
    )
    assert (
        event.wire
        == b'\x04\x3e\x0a\x35\x00'
        + connection.handle.to_bytes(2, 'little')
        + b'\x02\xa5\x00\x02\x04\x00'
    )


@pytest.mark.parametrize(
    'event,command,bit',
    [
        (hci.HCI_LE_Scan_Timeout_Event(), 'event', hci.HCI_LE_META_EVENT - 1),
        (hci.HCI_LE_Scan_Timeout_Event(), 'le', hci.HCI_LE_SCAN_TIMEOUT_EVENT - 1),
        (
            hci.HCI_LE_Path_Loss_Threshold_Event(1, 60, 1),
            'le',
            hci.HCI_LE_PATH_LOSS_THRESHOLD_EVENT - 1,
        ),
        (
            hci.HCI_LE_Transmit_Power_Reporting_Event(0, 1, 2, 1, -5, 0, 0),
            'le',
            hci.HCI_LE_TRANSMIT_POWER_REPORTING_EVENT - 1,
        ),
        (
            hci.HCI_LE_Frame_Space_Update_Complete_Event(0, 1, 2, 165, 2, 4),
            'le',
            hci.HCI_LE_FRAME_SPACE_UPDATE_COMPLETE_EVENT - 1,
        ),
        (disconnection(), 'event', hci.HCI_DISCONNECTION_COMPLETE_EVENT - 1),
        (
            hci.HCI_Encryption_Change_Event(0, 1, 1),
            'event',
            hci.HCI_ENCRYPTION_CHANGE_EVENT - 1,
        ),
        (
            hci.HCI_Encryption_Key_Refresh_Complete_Event(0, 1),
            'event',
            hci.HCI_ENCRYPTION_KEY_REFRESH_COMPLETE_EVENT - 1,
        ),
        (
            hci.HCI_Read_Remote_Version_Information_Complete_Event(0, 1, 9, 1, 1),
            'event',
            hci.HCI_READ_REMOTE_VERSION_INFORMATION_COMPLETE_EVENT - 1,
        ),
        (
            hci.HCI_Authenticated_Payload_Timeout_Expired_Event(1),
            'page2',
            hci.HCI_AUTHENTICATED_PAYLOAD_TIMEOUT_EXPIRED_EVENT
            - hci.HCI_EVENT_MASK_PAGE_2_CODE_BASE,
        ),
    ],
)
@pytest.mark.parametrize('enabled', [False, True])
def test_each_event_obeys_its_own_mask(host, event, command, bit, enabled):
    """Each event is gated by the bit in its own mask register.

    Purpose: Verify an event reaches the Host only when its controlling bit is
    set in the matching mask (event mask, page 2, or LE mask), and is suppressed
    otherwise (Vol 4, Part E, 7.3.1, 7.3.69, 7.8.1).

    Steps:
        1. Build the three mask values, enabling or clearing only the bit under
           test in the relevant register.
        2. Apply all three masks and emit the parametrized event.

    Expected:
        - Exactly one event reaches the Host when the bit is enabled, none when
          it is cleared, for each event/mask/bit case and both enabled values.
    """
    masks = {'event': ALL, 'page2': ALL, 'le': ALL}
    masks[command] = (1 << bit) if enabled else ALL & ~(1 << bit)
    if command != 'event':
        masks['event'] = ALL
    host.cmd(hci.HCI_Set_Event_Mask_Command(masks['event'].to_bytes(8, 'little')))
    host.cmd(
        hci.HCI_Set_Event_Mask_Page_2_Command(masks['page2'].to_bytes(8, 'little'))
    )
    host.cmd(hci.HCI_LE_Set_Event_Mask_Command(masks['le'].to_bytes(8, 'little')))
    host.emit(event)
    assert len(host.events) == (1 if enabled else 0)


def test_completed_packets_ignore_every_mask(host):
    """Number Of Completed Packets is never gated by any mask.

    Purpose: Verify the Number Of Completed Packets event is delivered even when
    all event masks are cleared, because flow-control events are not maskable
    (Vol 4, Part E, 7.7.19).

    Steps:
        1. Clear the event mask, page 2 mask and LE mask.
        2. Emit a Number Of Completed Packets event.

    Expected:
        - The event still reaches the Host.
    """
    for command in (
        hci.HCI_Set_Event_Mask_Command(bytes(8)),
        hci.HCI_Set_Event_Mask_Page_2_Command(bytes(8)),
        hci.HCI_LE_Set_Event_Mask_Command(bytes(8)),
    ):
        host.cmd(command)
    host.emit(hci.HCI_Number_Of_Completed_Packets_Event([1], [1]))
    host.ut_rx(hci.HCI_Number_Of_Completed_Packets_Event)


def test_default_le_mask_hides_enhanced_and_extended_events(fresh):
    """The default LE mask hides enhanced events, using legacy Connection Complete.

    Purpose: Verify the power-on LE mask (bits 0-4 only) suppresses the Enhanced
    Connection Complete event, so a new connection is reported with the legacy
    event (Vol 4, Part E, 7.8.1).

    Steps:
        1. On a fresh harness establish a connection and drain events.

    Expected:
        - A valid handle is assigned and no Enhanced Connection Complete event
          was delivered.
    """
    connection = fresh.connection()
    fresh._drain()
    assert connection.handle
    enhanced = [
        e
        for e in fresh.events
        if isinstance(e, hci.HCI_LE_Enhanced_Connection_Complete_Event)
    ]
    assert not enhanced


# ---------------------------------------------------------------------------
# Advertising command families (3.1.1)
# ---------------------------------------------------------------------------
def legacy_parameters():
    return hci.HCI_LE_Set_Advertising_Parameters_Command(
        0x20, 0x20, 0, 0, 0, IUT_ADDRESS, 7, 0
    )


def extended_parameters():
    from tests.unit.helpers import extended_parameters as make

    return make()


@pytest.mark.parametrize('first', ['legacy', 'extended'])
def test_first_family_locks_the_other_until_reset(host, first):
    """The first advertising family used locks out the other until Reset.

    Purpose: Verify that once legacy or extended advertising commands are used,
    the other family (and its family-specific reads) is disallowed until Reset
    clears the choice (Vol 4, Part E, 3.1.1).

    Steps:
        1. Use the parametrized first family's parameters command.
        2. Attempt the other family's parameters and a family read.
        3. Reset and use the previously locked family.

    Expected:
        - The other family's command is COMMAND_DISALLOWED; the legacy Tx-power
          read is allowed only after legacy and disallowed after extended; after
          Reset the formerly locked family succeeds.
    """
    legacy, extended = legacy_parameters(), extended_parameters()
    if first == 'legacy':
        host.cmd(legacy)
        host.cmd(extended, ErrorCode.COMMAND_DISALLOWED_ERROR)
        host.cmd(hci.HCI_LE_Read_Advertising_Physical_Channel_Tx_Power_Command())
    else:
        host.cmd(extended)
        host.cmd(legacy, ErrorCode.COMMAND_DISALLOWED_ERROR)
        host.cmd(
            hci.HCI_LE_Read_Advertising_Physical_Channel_Tx_Power_Command(),
            ErrorCode.COMMAND_DISALLOWED_ERROR,
        )
    host.cmd(hci.HCI_Reset_Command())
    host.cmd(extended if first == 'legacy' else legacy)


def test_async_command_of_the_other_family_gets_command_status(host):
    """An async command of the locked-out family is refused by Command Status.

    Purpose: Verify that after extended advertising is chosen, a legacy-family
    async command (Create Connection) is refused with Command Status
    COMMAND_DISALLOWED (Vol 4, Part E, 3.1.1).

    Steps:
        1. Use extended advertising parameters.
        2. Send legacy Create Connection.

    Expected:
        - Create Connection returns Command Status COMMAND_DISALLOWED.
    """
    host.cmd(extended_parameters())
    from tests.unit.helpers import create_connection

    host.cmd_status(create_connection(), ErrorCode.COMMAND_DISALLOWED_ERROR)


def test_family_neutral_commands_are_always_allowed(host):
    """Family-neutral commands remain allowed after a family is chosen.

    Purpose: Verify commands that belong to neither advertising family (filter
    list edits, random address) stay allowed after extended advertising is
    selected (Vol 4, Part E, 3.1.1).

    Steps:
        1. Use extended advertising parameters.
        2. Send Add Device To Filter Accept List and Set Random Address.

    Expected:
        - Both family-neutral commands succeed.
    """
    host.cmd(extended_parameters())
    host.cmd(hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(0, IUT_ADDRESS))
    host.cmd(hci.HCI_LE_Set_Random_Address_Command(hci.Address('C0:00:00:00:00:01')))


def test_set_random_address_is_disallowed_while_initiating(host):
    """Set Random Address is refused while an initiation is pending.

    Purpose: Verify Set Random Address is disallowed while Create Connection is
    in progress and allowed again once the initiation is cancelled (Vol 4,
    Part E, 7.8.4).

    Steps:
        1. Start Create Connection.
        2. Attempt Set Random Address, then cancel the initiation and consume
           its Enhanced Connection Complete.
        3. Set Random Address again.

    Expected:
        - The first Set Random Address is COMMAND_DISALLOWED and the address is
          unchanged; after cancel the second succeeds and the address updates.
    """
    from tests.unit.helpers import create_connection

    random = hci.Address('C0:12:34:56:78:9A')
    host.cmd_status(create_connection())
    host.cmd(
        hci.HCI_LE_Set_Random_Address_Command(random),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )
    assert host.controller.random_address != random
    host.cmd(hci.HCI_LE_Create_Connection_Cancel_Command())
    host.ut_rx(hci.HCI_LE_Enhanced_Connection_Complete_Event)
    host.cmd(hci.HCI_LE_Set_Random_Address_Command(random))
    assert host.controller.random_address == random


def test_public_and_random_address_setters_accept_strings(fresh):
    """The public and random address setters accept string addresses.

    Purpose: Verify assigning string values to ``public_address`` and
    ``random_address`` stores them as addresses.

    Steps:
        1. Assign string public and random addresses on a fresh Controller.

    Expected:
        - Both addresses stringify to the assigned values. Active scanning uses
          the same random address exposed by the LL.
    """
    controller = fresh.controller
    controller.public_address = 'F0:F1:F2:F3:F4:09'
    controller.random_address = 'C0:F1:F2:F3:F4:09'
    assert str(controller.public_address).startswith('F0:F1:F2:F3:F4:09')
    assert str(controller.random_address).startswith('C0:F1:F2:F3:F4:09')
    fresh.cmd(hci.HCI_LE_Set_Scan_Parameters_Command(1, 16, 16, 1, 0))
    fresh.cmd(hci.HCI_LE_Set_Scan_Enable_Command(1, 0))
    assert (
        fresh.link.scan.address
        == fresh.link.random_address
        == controller.random_address
    )


def test_public_address_setter_updates_ll_and_subsequent_scans(fresh):
    """HCI and LL share the configured public address across HCI Reset.

    Scan parameters may predate the assignment. An active scan keeps its
    address until it is restarted, even if the Controller address changes.
    """
    first = hci.Address('F0:F1:F2:F3:F4:09', hci.AddressType.PUBLIC_DEVICE)
    second = hci.Address('F0:F1:F2:F3:F4:0A', hci.AddressType.PUBLIC_DEVICE)
    fresh.cmd(hci.HCI_LE_Set_Scan_Parameters_Command(1, 16, 16, 0, 0))
    fresh.controller.public_address = str(first).split('/')[0]
    assert fresh.cmd(hci.HCI_Read_BD_ADDR_Command()).bd_addr == first
    assert fresh.link.public_address == first
    fresh.cmd(hci.HCI_LE_Set_Scan_Enable_Command(1, 0))
    assert fresh.link.scan.address == first

    fresh.controller.public_address = second
    assert fresh.cmd(hci.HCI_Read_BD_ADDR_Command()).bd_addr == second
    assert fresh.link.public_address == second
    assert fresh.link.scan.address == first
    fresh.cmd(hci.HCI_LE_Set_Scan_Enable_Command(0, 0))
    fresh.cmd(hci.HCI_LE_Set_Scan_Enable_Command(1, 0))
    assert fresh.link.scan.address == second

    fresh.cmd(hci.HCI_Reset_Command())
    assert fresh.cmd(hci.HCI_Read_BD_ADDR_Command()).bd_addr == second
    fresh.cmd(hci.HCI_LE_Set_Scan_Enable_Command(1, 0))
    assert fresh.link.scan.address == fresh.link.public_address == second
