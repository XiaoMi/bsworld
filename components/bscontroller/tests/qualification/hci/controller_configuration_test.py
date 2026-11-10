# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""HCI.TS.p39, Section 4.10: Controller Configuration."""

import random
import struct

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
    LL_PERIODIC_INTERVAL_MIN,
    PDU_DATA_PAYLOAD_SIZE_MAX,
    PDU_DATA_PAYLOAD_SIZE_MIN,
    PDU_DATA_PAYLOAD_TIME_CODED_MAX_US,
    PDU_DATA_PAYLOAD_TIME_MIN_US,
    ControlOpcode,
    LlId,
)
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.peer import establish
from tests.qualification.tester import (
    PEER_ADDRESS,
    create_connection,
    create_sync,
    extended_enable,
    extended_parameters,
    le_features,
    legacy_parameters,
    scan_parameters,
)

ErrorCode = hci.HCI_ErrorCode
Opcode = ControlOpcode
Operation = hci.HCI_LE_Set_Extended_Advertising_Data_Command.Operation

# Capacity declared for this DUT configuration, not read from its implementation.
EXPECTED_PERIODIC_ADVERTISER_LIST_SIZE = 8
# HCI/CCO/BI-33-C: one hundred random periodic data octets.
PERIODIC_DATA_LENGTH = 100
# Selected 100-ms extended advertising interval for HCI/CCO/BI-33-C.
SELECTED_ADVERTISING_INTERVAL = 160


def _periodic_parameters(**changes):
    values = {
        'advertising_handle': 0,
        'periodic_advertising_interval_min': 40,
        'periodic_advertising_interval_max': 40,
        'periodic_advertising_properties': 0,
    }
    return hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(**(values | changes))


async def bredr_not_supported(tester):
    """BR/EDR Not Supported.

    Initial Condition:
        The LE-only IUT is in standby.

    Test Steps:
        The source describes an unnumbered procedure: the Upper Tester sends
        HCI Inquiry and receives either Command Complete or Command Status
        with Status = Unknown HCI Command (0x01). Check the corresponding
        opcode and one command credit in either event alternative.

    Expected Outcome:
        Pass verdict: either permitted event reports status 0x01 for Inquiry.

    Reference:
        HCI.TS.p39.pdf, Section 4.10, HCI/CCO/BV-07-C;
        PDF/printed pages 154-155, Figure 4.83.
        Source reference [1], Section 3.2.
    """
    # Vol 4, Part E, 7.1.1: LAP 0x9E8B00, Inquiry_Length 1, Num_Responses 1.
    command = hci.HCI_Command(
        bytes.fromhex('008b9e') + bytes([1, 1]),
        op_code=hci.hci_command_op_code(0x01, 0x0001),
    )
    tester.send(bytes(command))
    event = await tester.expect(
        lambda event: (
            isinstance(
                event, (hci.HCI_Command_Complete_Event, hci.HCI_Command_Status_Event)
            )
            and event.command_opcode == command.op_code
        )
    )
    assert event.num_hci_command_packets == 1
    if isinstance(event, hci.HCI_Command_Status_Event):
        status = event.status
    else:
        # The status is the first return-parameter octet, also for unknown commands.
        status = event.parameters[3]
    assert status == ErrorCode.UNKNOWN_HCI_COMMAND_ERROR


async def set_data_length(tester):
    """LE Set Data Length.

    Initial Condition:
        Establish an ACL with the IUT as Central and a DLE-capable Lower Tester.
        The initial effective data length is 27 octets.

    Test Steps:
        The source describes an unnumbered procedure:
        - The Upper Tester sends LE Set Data Length with the current connection
          handle and permissible TxOctets/TxTime values.
        - The IUT returns Command Complete for that command.
        - If the command changes the maximum transmission packet size or time,
          the IUT sends LE Data Length Change with the updated values.

    Observation Setup:
        Establish the ACL and exchange DLE support before the source procedure.
        Select TxOctets = 60 and TxTime = 592 us. Answer LL_LENGTH_REQ with peer
        receive/transmit limits of 60 octets and 592 us, then check the resulting
        change event against both endpoints' negotiated limits. This vector
        changes the initial effective length, so the conditional event is
        required here. Disconnect in a separate postamble.

    Expected Outcome:
        Pass verdict: the command succeeds for the selected handle. Because the
        selected vector changes effective limits, Data Length Change must report
        60 octets and 592 us in both directions for that same connection.

    Reference:
        HCI.TS.p39.pdf, Section 4.10, HCI/CCO/BV-09-C;
        PDF/printed page 156, Figure 4.84.
        Source reference [2], Section 7.8.33.
    """
    link = await establish(tester, central=True)
    await link.control(
        Opcode.LL_FEATURE_RSP,
        bytes([Opcode.LL_PERIPHERAL_FEATURE_REQ])
        + int(hci.LeFeatureMask.LE_DATA_PACKET_LENGTH_EXTENSION).to_bytes(8, 'little'),
    )
    octets, time_us = 60, 592
    result = await tester.command(
        hci.HCI_LE_Set_Data_Length_Command(link.handle, octets, time_us)
    )
    assert result.connection_handle == link.handle
    request = await link.control(Opcode.LL_LENGTH_REQ)
    max_rx_octets, max_rx_time, max_tx_octets, max_tx_time = struct.unpack(
        '<HHHH', request[1:]
    )
    assert max_rx_octets >= PDU_DATA_PAYLOAD_SIZE_MIN
    assert max_rx_time >= PDU_DATA_PAYLOAD_TIME_MIN_US
    assert (max_tx_octets, max_tx_time) == (octets, time_us)
    await link.exchange(
        bytes([Opcode.LL_LENGTH_RSP])
        + struct.pack('<HHHH', octets, time_us, octets, time_us),
        llid=LlId.CONTROL,
    )
    event = await tester.event(hci.HCI_LE_Data_Length_Change_Event)
    assert event.connection_handle == link.handle
    assert (
        event.max_tx_octets,
        event.max_tx_time,
        event.max_rx_octets,
        event.max_rx_time,
    ) == (
        min(octets, max_tx_octets),
        min(time_us, max_tx_time),
        min(octets, max_rx_octets),
        min(time_us, max_rx_time),
    )
    await link.disconnect(local=True)


async def read_suggested_data_length(tester):
    """LE Read Suggested Default Data Length Command.

    Initial Condition:
        The IUT has just been reset and is in standby.

    Test Steps:
        The source describes an unnumbered procedure: the Upper Tester sends
        LE Read Suggested Default Data Length and receives Command Complete.

    Observation Setup:
        Reset before the procedure to establish the initial condition. Check
        Success, the matching opcode, command credit and both returned values.

    Expected Outcome:
        Pass verdict: SuggestedMaxTxOctets is 27 and SuggestedMaxTxTime is 328 us.

    Reference:
        HCI.TS.p39.pdf, Section 4.10, HCI/CCO/BV-10-C;
        PDF/printed page 157, Figure 4.85.
        Source reference [8], Section 7.8.34.
    """
    await tester.command(hci.HCI_Reset_Command())
    result = await tester.command(
        hci.HCI_LE_Read_Suggested_Default_Data_Length_Command()
    )
    assert result.suggested_max_tx_octets == PDU_DATA_PAYLOAD_SIZE_MIN
    assert result.suggested_max_tx_time == PDU_DATA_PAYLOAD_TIME_MIN_US


async def write_suggested_data_length(tester):
    """LE Write Suggested Default Data Length Command.

    Initial Condition:
        The IUT is in standby.

    Test Steps:
        The source describes an unnumbered procedure for every Table 4.33 row:
        - Send LE Write Suggested Default Data Length with that row's TxOctets
          and TxTime, and receive Command Complete.
        - Send LE Read Suggested Default Data Length and receive Command
          Complete with both stored values.

    Test Case Configuration:
        Rounds 1-9 use the ordered Cartesian product TxOctets = 0x001B, 0x00FB,
        0x0080 and, for each, TxTime = 0x0148, 0x4290, 0x2000. Rounds 10-20
        choose each value randomly in the inclusive ranges 0x001B..0x00FB and
        0x0148..0x4290. A locally seeded generator makes those stimuli repeatable.

    Expected Outcome:
        Pass verdict: all twenty writes and reads succeed with matching opcode
        and credit; each read returns the exact pair most recently written.

    Reference:
        HCI.TS.p39.pdf, Section 4.10, HCI/CCO/BV-11-C;
        PDF/printed pages 157-159, Table 4.33 and Figure 4.86.
        Source reference [8], Section 7.8.35.
    """
    pairs = [
        (octets, time_us)
        for octets in (PDU_DATA_PAYLOAD_SIZE_MIN, PDU_DATA_PAYLOAD_SIZE_MAX, 128)
        for time_us in (
            PDU_DATA_PAYLOAD_TIME_MIN_US,
            PDU_DATA_PAYLOAD_TIME_CODED_MAX_US,
            0x2000,
        )
    ]
    generator = random.Random(0)
    pairs += [
        (
            generator.randint(PDU_DATA_PAYLOAD_SIZE_MIN, PDU_DATA_PAYLOAD_SIZE_MAX),
            generator.randint(
                PDU_DATA_PAYLOAD_TIME_MIN_US, PDU_DATA_PAYLOAD_TIME_CODED_MAX_US
            ),
        )
        for _ in range(11)
    ]
    for octets, time_us in pairs:
        await tester.command(
            hci.HCI_LE_Write_Suggested_Default_Data_Length_Command(octets, time_us)
        )
        result = await tester.command(
            hci.HCI_LE_Read_Suggested_Default_Data_Length_Command()
        )
        assert (result.suggested_max_tx_octets, result.suggested_max_tx_time) == (
            octets,
            time_us,
        )


async def set_default_phy(tester):
    """LE Set Default PHY Command.

    Initial Condition:
        The IUT is in standby.

    Test Steps:
        The source describes an unnumbered procedure: the Upper Tester sends
        LE Set Default PHY with ALL_PHYS = 0x03 and TX_PHYS = RX_PHYS = 0
        (no preferences), then receives Command Complete.
        Check Success, the matching opcode and one command credit.

    Expected Outcome:
        Pass verdict: Command Complete returns Success.

    Reference:
        HCI.TS.p39.pdf, Section 4.10, HCI/CCO/BV-15-C;
        PDF/printed pages 161-162, Figure 4.90.
        Source reference [8], Section 7.8.48.
    """
    await tester.command(
        hci.HCI_LE_Set_Default_PHY_Command(
            hci.HCI_LE_ALL_PHYS_TX_MASK | hci.HCI_LE_ALL_PHYS_RX_MASK, 0, 0
        )
    )


async def read_periodic_list_size(tester):
    """LE Read Periodic Advertiser List Size Command.

    Initial Condition:
        The IUT is in standby. This DUT declares an eight-entry list.

    Test Steps:
        The source describes an unnumbered procedure: the Upper Tester sends
        LE Read Periodic Advertiser List Size and receives Command Complete
        containing the list size. Check Success, the corresponding opcode,
        command credit and the independently declared capacity of eight.

    Expected Outcome:
        Pass verdict: the command succeeds and reports eight entries. No minimum
        capacity is inferred from the needs of a different test procedure.

    Reference:
        HCI.TS.p39.pdf, Section 4.10, HCI/CCO/BV-16-C;
        PDF/printed pages 162-163, Figure 4.91.
        Source reference [9], Section 7.8.73.
    """
    result = await tester.command(
        hci.HCI_LE_Read_Periodic_Advertiser_List_Size_Command()
    )
    assert (
        result.periodic_advertiser_list_size == EXPECTED_PERIODIC_ADVERTISER_LIST_SIZE
    )


async def periodic_list_commands(tester):
    """LE Add/Remove/Clear Periodic Advertiser List Commands.

    Initial Condition:
        The IUT is in standby with an empty Periodic Advertiser List.

    Test Steps:
        1. Send LE Clear Periodic Advertiser List; require Command Complete
           with Status = Success (0x00).
        2. Add an arbitrarily chosen valid address, address type and SID;
           require successful Command Complete.
        3. Remove the step-2 identity; require successful Command Complete.
        4. Remove it again; require Command Complete with Unknown Advertising
           Identifier (0x42).
        5. Add the step-2 identity again; require successful Command Complete.
        6. Clear the list; require successful Command Complete.
        7. Add the step-2 identity again; require successful Command Complete.
        8. Add the identical entry again; require Command Complete with Invalid
           HCI Command Parameters (0x12).
        9. Add the same address and type but a different SID; require successful
           Command Complete.
        10. Add the same address and SID as step 2 but a different address type;
            require successful Command Complete.

    Expected Outcome:
        Pass verdict: all ten ordered commands succeed except the second remove
        (Unknown Advertising Identifier, 0x42) and duplicate add (0x12).
        Each completion has the matching opcode and one command credit.

    Reference:
        HCI.TS.p39.pdf, Section 4.10, HCI/CCO/BV-17-C;
        PDF/printed pages 163-165, steps 1-10 and Figure 4.92.
        Source reference [9], Sections 7.8.70, 7.8.71 and 7.8.72.
    """
    address = hci.Address('C0:34:56:78:9A:BC', hci.AddressType.PUBLIC_DEVICE)
    add = hci.HCI_LE_Add_Device_To_Periodic_Advertiser_List_Command
    remove = hci.HCI_LE_Remove_Device_From_Periodic_Advertiser_List_Command
    clear = hci.HCI_LE_Clear_Periodic_Advertiser_List_Command
    await tester.command(clear())
    await tester.command(add(address.address_type, address, 3))
    await tester.command(remove(address.address_type, address, 3))
    await tester.command(
        remove(address.address_type, address, 3),
        ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR,
    )
    await tester.command(add(address.address_type, address, 3))
    await tester.command(clear())
    await tester.command(add(address.address_type, address, 3))
    await tester.command(
        add(address.address_type, address, 3),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    await tester.command(add(address.address_type, address, 4))
    random_address = hci.Address(bytes(address), hci.AddressType.RANDOM_DEVICE)
    await tester.command(add(random_address.address_type, random_address, 3))


async def invalid_periodic_data_parameters(tester):
    """Invalid LE Set Periodic Advertising Data Parameters.

    Initial Condition:
        The IUT is in standby. Its feature mask declares Periodic Advertising ADI
        Support.

    Test Steps:
        1. Send LE Set Extended Advertising Parameters on all advertising
           channels with a selected supported interval, properties 0x0000,
           public own address and LE 1M on both PHYs; require Success.
        2. Send LE Set Periodic Advertising Parameters on that handle with the
           minimum periodic interval; require Success.
        3. Send LE Set Periodic Advertising Data with Operation 0x03 and 100
           random octets in 1..254; require Success.
        4. Send LE Set Periodic Advertising Data with Operation 0x04.
        5. Require Command Complete with Invalid HCI Command Parameters (0x12).
        6. Enable periodic advertising with Enable bit 0 set; require Success.
        7. Enable extended advertising with Duration[0] 0x0000; require Success.
        8. Send LE Set Periodic Advertising Data with Operation 0x04 and 100
           random octets in 1..254.
        9. Require Command Complete with Invalid HCI Command Parameters (0x12).
        10. Send Operation 0x04 with Advertising_Data_Length 0; require Success.
        11. Send Operation 0x03 with Advertising_Data_Length 0; require Success.
        12. Send LE Set Periodic Advertising Data with Operation 0x04.
        13. Require Command Complete with Invalid HCI Command Parameters (0x12).

    Test Case Configuration:
        Steps 4 and 12 do not give a data length; send zero octets, so the
        disabled train (step 4) and the missing data (step 12) are the only
        invalid conditions. The selected extended interval is 100 ms and the
        minimum periodic interval is 0x0006 (7.5 ms).

    Observation Setup:
        Read the local features before step 1 to establish the ADI initial
        condition.

    Expected Outcome:
        Pass verdict: steps 5, 9 and 13 return 0x12 and step 10 returns Success,
        each with the matching opcode and a command credit.

    Postamble:
        Disable periodic and extended advertising.

    Reference:
        HCI.TS.p39.pdf, Section 4.10, HCI/CCO/BI-33-C;
        PDF/printed pages 183-184, steps 1-13.
        Source reference [13], Section 7.8.62.
    """
    assert await le_features(tester) & (
        hci.LeFeatureMask.PERIODIC_ADVERTISING_ADI_SUPPORT
    ), 'this source requires Periodic Advertising ADI Support'
    generator = random.Random(0)
    data = bytes(generator.randint(1, 254) for _ in range(PERIODIC_DATA_LENGTH))
    command = hci.HCI_LE_Set_Periodic_Advertising_Data_Command
    await tester.command(
        extended_parameters(
            primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
            primary_advertising_interval_min=SELECTED_ADVERTISING_INTERVAL,
            primary_advertising_interval_max=SELECTED_ADVERTISING_INTERVAL,
        )
    )
    await tester.command(
        _periodic_parameters(
            periodic_advertising_interval_min=LL_PERIODIC_INTERVAL_MIN,
            periodic_advertising_interval_max=LL_PERIODIC_INTERVAL_MIN,
        )
    )
    await tester.command(command(0, Operation.COMPLETE_DATA, data))
    await tester.command(
        command(0, Operation.UNCHANGED_DATA, b''),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    await tester.command(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    await tester.command(extended_enable())
    await tester.command(
        command(
            0,
            Operation.UNCHANGED_DATA,
            bytes(generator.randint(1, 254) for _ in range(PERIODIC_DATA_LENGTH)),
        ),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    await tester.command(command(0, Operation.UNCHANGED_DATA, b''))
    await tester.command(command(0, Operation.COMPLETE_DATA, b''))
    await tester.command(
        command(0, Operation.UNCHANGED_DATA, b''),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    await tester.command(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0, 0))
    await tester.command(extended_enable(0))


async def invalid_data_length(tester):
    """LE Set Data Length, Invalid Parameters.

    Initial Condition:
        An ACL exists with the IUT in either role; select Central and a valid handle.

    Test Steps:
        The source describes an unnumbered procedure:
        - The Upper Tester sends LE Set Data Length with TxTime = 17041 us.
        - The IUT returns Command Complete with Invalid Parameters (0x12).

    Observation Setup:
        Establish the ACL before the procedure and use its valid handle and
        permissible TxOctets = 27. Check the opcode, command credit, error
        status and returned handle shown in Figure 4.105, then disconnect
        in a separate postamble.

    Expected Outcome:
        Pass verdict: Command Complete returns 0x12, the submitted handle,
        matching command opcode and one command credit.

    Reference:
        HCI.TS.p39.pdf, Section 4.10, HCI/CCO/BI-40-C;
        PDF/printed page 190, Figure 4.105.
        Source reference [2], Section 7.8.33.
    """
    link = await establish(tester, central=True)
    command = hci.HCI_LE_Set_Data_Length_Command(
        link.handle, PDU_DATA_PAYLOAD_SIZE_MIN, PDU_DATA_PAYLOAD_TIME_CODED_MAX_US + 1
    )
    await tester.wait(0)
    tester.send(bytes(command))
    event = await tester.expect(
        lambda event: (
            isinstance(event, hci.HCI_Command_Complete_Event)
            and event.command_opcode == command.op_code
        )
    )
    assert event.num_hci_command_packets == 1
    # The generic HCI decoder exposes only status for errors; inspect the wire handle.
    assert event.parameters[3:] == struct.pack(
        '<BH', ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR, link.handle
    )
    await link.disconnect(local=True)


async def invalid_channel_map_handle(tester):
    """LE Read Channel Map \u2013 Reject Invalid Handle.

    Initial Condition:
        Establish one valid ACL; either IUT role is permitted. Select Central.

    Test Steps:
        1. Send LE Read Channel Map with a Connection_Handle different from
           the established connection's handle.
        2. Require Command Complete with Unknown Connection Identifier (0x02).

    Observation Setup:
        Establish one ACL and retain its handle before step 1; select an unused
        handle for the command. Disconnect the valid ACL after step 2, not as
        part of the source procedure.

    Expected Outcome:
        Pass verdict: Command Complete returns 0x02, matching opcode and credit.

    Reference:
        HCI.TS.p39.pdf, Section 4.10, HCI/CCO/BI-43-C;
        PDF/printed pages 191-192, Figure 4.106.
        Source reference [13], Section 7.8.20.
    """
    link = await establish(tester, central=True)
    await tester.command(
        hci.HCI_LE_Read_Channel_Map_Command(link.handle + 1),
        ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR,
    )
    await link.disconnect(local=True)


async def invalid_periodic_parameters(tester):
    """LE Set Periodic Advertising Parameters, Invalid Parameters.

    Initial Condition:
        A valid advertising set is configured, without periodic advertising enabled.

    Test Steps:
        Repeat steps 1 and 2 for the rounds selected by Table 4.56:
        1. Send the selected Set Periodic Advertising Parameters command with
           the parameters of the Table 4.57 round.
        2. Require Command Complete with that round's prescribed status.

    Test Case Configuration:
        BI-64 selects v1, rounds 1 and 2 only: unknown handle with other values
        valid returns 0x42; Min greater than Max with other values valid returns
        0x12. Configure valid handle zero before the rounds; use unknown handle
        one in round 1, then handle zero with Min = 41, Max = 40 in round 2.
        The unselected BI-65 uses v2 and all Table 4.57 rounds.

    Expected Outcome:
        Pass verdict: Table 4.57 rounds 1 and 2 return 0x42 and 0x12 respectively,
        with matching opcode and credit. Table 4.56 assigns only these rows to v1.

    Source Correction:
        The shared Test Purpose mentions only v2, but Table 4.56 explicitly
        assigns BI-64 to v1 rounds 1-2. Follow that case-specific selection.
        Amber confirms the table/prose mismatch; no adopted erratum is claimed.

    Reference:
        HCI.TS.p39.pdf, Section 4.10.15, HCI/CCO/BI-64-C;
        PDF/printed pages 207-208, Tables 4.56 and 4.57.
        Source reference [17], Section 7.8.61.
    """
    await tester.command(extended_parameters())
    await tester.command(
        _periodic_parameters(advertising_handle=1),
        ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR,
    )
    await tester.command(
        _periodic_parameters(periodic_advertising_interval_min=41),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


async def invalid_connection_event_lengths(tester):
    """LE Create Connection, Invalid Parameters.

    Initial Condition:
        The IUT is already initiating. No advertiser is present to complete it.

    Test Steps:
        1. Send LE Create Connection with Min_CE_Length greater than
           Max_CE_Length and all other parameters valid.
        2. Require Command Status with Invalid HCI Command Parameters (0x12).

    Observation Setup:
        Start a valid pending attempt before step 1 to establish the required
        Initiating state. For the invalid command, select Min_CE_Length = 1 and
        Max_CE_Length = 0. Cancel the original attempt after step 2 as a separate
        postamble, not as an additional source step.

    Expected Outcome:
        Pass verdict: the second command returns Command Status with 0x12,
        matching opcode and one available command credit.

    Reference:
        HCI.TS.p39.pdf, Section 4.10, HCI/CCO/BI-134-C;
        PDF/printed pages 247-248, steps 1-2.
        Source reference [17], Section 7.8.12.
    """
    await tester.command(create_connection())
    await tester.command(
        create_connection(min_ce_length=1), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
    )
    await tester.command(hci.HCI_LE_Create_Connection_Cancel_Command())


# Public IXIT values for this Controller configuration.
EXPECTED_RESOLVING_LIST_SIZE = 16
EXPECTED_DECISION_TESTS = 8
PEER_IRK = bytes.fromhex('00112233445566778899aabbccddeeff')
LOCAL_IRK = bytes.fromhex('ffeeddccbbaa99887766554433221100')


def _resolving_add(address=PEER_ADDRESS, *, address_type=None):
    return hci.HCI_LE_Add_Device_To_Resolving_List_Command(
        address.address_type if address_type is None else address_type,
        address,
        PEER_IRK,
        LOCAL_IRK,
    )


async def resolving_list_remove(tester, *, clear=False):
    """Remove or clear resolving records in standby.

    Initial Condition:
        The IUT is in standby with address resolution disabled.

    Test Steps:
        The source describes an unnumbered procedure: add a valid peer identity,
        receive successful Command Complete, then remove that identity or clear
        the list and receive successful Command Complete.

    Expected Outcome:
        Both commands succeed with matching opcodes and one command credit.

    Reference:
        HCI.TS.p39, Section 4.10, BV-12-C/BV-13-C, pages 159-160,
        Figures 4.87-4.88; Core Vol 4, Part E, 7.8.38-40.
    """
    await tester.command(_resolving_add())
    await tester.command(
        hci.HCI_LE_Clear_Resolving_List_Command()
        if clear
        else hci.HCI_LE_Remove_Device_From_Resolving_List_Command(1, PEER_ADDRESS)
    )
    # Observe removal through the public API, never by reading LL internals.
    await tester.command(
        hci.HCI_LE_Remove_Device_From_Resolving_List_Command(1, PEER_ADDRESS),
        ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR,
    )


async def resolving_list_size(tester):
    """Read resolving-list capacity.

    Initial Condition:
        The IUT is in standby.

    Test Steps:
        Send LE Read Resolving List Size and receive Command Complete.

    Expected Outcome:
        Success and Resolving_List_Size equals the declared IXIT capacity 16.

    Reference:
        HCI.TS.p39, Section 4.10, BV-14-C, page 161, Figure 4.89;
        Core Vol 4, Part E, 7.8.41.
    """
    result = await tester.command(hci.HCI_LE_Read_Resolving_List_Size_Command())
    assert result.resolving_list_size == EXPECTED_RESOLVING_LIST_SIZE


async def resolving_list_busy(tester, *, role):
    """Resolving List Commands fail when list in use.

    Initial Condition:
        Standby; add one valid peer and enable address resolution.

    Test Steps:
        1. Send the role configuration and enable commands in Table 4.38.
        2. Send Add, Remove, Clear, Set Address Resolution Enable (both 0 and 1),
           and Set Privacy Mode for both public and random identity types.

    Test Case Configuration:
        Advertising uses ADV_NONCONN_IND; scanning is active; initiation uses
        explicit peer filtering; periodic acquisition uses Options zero.

    Expected Outcome:
        Role setup succeeds. Each of the seven list commands returns nonzero
        status in Command Complete, as specified by the source verdict.

    Reference:
        HCI.TS.p39, Section 4.10.2, pages 173-175, Table 4.38, Figure 4.98;
        Core Vol 4, Part E, 7.8.38-40, 7.8.44 and 7.8.77.
    """
    await tester.command(_resolving_add())
    await tester.command(hci.HCI_LE_Set_Address_Resolution_Enable_Command(1))
    if role == 'advertising':
        await tester.command(legacy_parameters(advertising_type=3))
        await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
    elif role == 'scanning':
        await tester.command(scan_parameters(le_scan_type=1))
        await tester.command(hci.HCI_LE_Set_Scan_Enable_Command(1, 0))
    elif role == 'sync':
        await tester.command(create_sync())
    else:
        await tester.command(create_connection(extended=role == 'extended'))
    commands = [
        _resolving_add(),
        hci.HCI_LE_Remove_Device_From_Resolving_List_Command(1, PEER_ADDRESS),
        hci.HCI_LE_Clear_Resolving_List_Command(),
        hci.HCI_LE_Set_Address_Resolution_Enable_Command(0),
        hci.HCI_LE_Set_Address_Resolution_Enable_Command(1),
        hci.HCI_LE_Set_Privacy_Mode_Command(0, PEER_ADDRESS, 1),
        hci.HCI_LE_Set_Privacy_Mode_Command(1, PEER_ADDRESS, 1),
    ]
    for command in commands:
        await tester.command(command, tuple(range(1, 256)))


async def resolving_list_duplicate(tester, *, change):
    """LE Add Device To Resolving List: duplicate identity or Peer IRK.

    Initial Condition:
        The resolving list is empty and not in use.

    Test Steps:
        1-2. Add a valid identity and receive Success.
        3-4. Repeat with identical values, a changed identity type, or a changed
             identity address, retaining the same nonzero Peer IRK.
        5-6. For changed identities, remove the proposed second identity; the
             result is Unknown Connection Identifier after rejected addition,
             or Success after an accepted addition (source alternatives 6A/6B).

    Expected Outcome:
        The repeated addition returns Success or a valid error. Removal follows
        the corresponding source alternative.

    Source Correction:
        The Expected Outcome on page 194 says the second record is never added,
        whereas step 6B explicitly permits successful removal after accepted
        addition. Preserve both numbered alternatives; this DUT rejects and
        takes 6A. Core Vol 4, Part E, 7.8.38 lets an identical four-parameter
        repeat succeed or fail (O), and makes rejection of a duplicate identity
        or non-zero Peer_IRK recommended with 0x12 if rejected (RC).
        This is a local interpretation, not an adopted SIG erratum.

    Reference:
        HCI.TS.p39, Section 4.10.9, pages 193-194, Table 4.48, Figure 4.108;
        Core Vol 4, Part E, 7.8.38-39.
    """
    await tester.command(_resolving_add())
    address = (
        hci.Address(bytes(PEER_ADDRESS), 0)
        if change == 'type'
        else (
            hci.Address('C0:11:22:33:44:66', 1) if change == 'address' else PEER_ADDRESS
        )
    )
    result = await tester.command(_resolving_add(address), tuple(range(256)))
    if change != 'none':
        await tester.command(
            hci.HCI_LE_Remove_Device_From_Resolving_List_Command(
                address.address_type, address
            ),
            ErrorCode.SUCCESS
            if result.status == 0
            else ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR,
        )


async def create_without_random_address(tester, *, extended, own_type, policy):
    """Reject connection creation with an unset random fallback address.

    Initial Condition:
        Reset IUT, no random address and no usable Local IRK.

    Test Steps:
        1. Send Create Connection or Extended Create Connection with own address
           type and filter policy from Table 4.50 or 4.51.
        2. Accept Command Status 0x12, or successful Command Status followed by
           Connection Complete (Enhanced for extended initiation) with 0x12.

    Expected Outcome:
        Invalid HCI Command Parameters is reported through either permitted path.

    Source Correction:
        The initial condition says Initiating State, although no earlier creation
        command is specified. Start in standby so the command under test creates
        the attempted initiation; otherwise Command Disallowed would mask the
        random-address check. Core Vol 4, Part E, 7.8.12/66 defines this validation.
        For BI-54/55/56, the Section 4.10.12 Expected Outcome on page 202 names
        HCI_LE_Connection_Complete, whereas step 2B.2 names
        HCI_LE_Enhanced_Connection_Complete. Core Vol 4, Part E, 7.8.66 generates
        only the Enhanced event for Extended Create Connection, so ALT 2B follows
        step 2B.2. These are local clarifications, not adopted SIG errata.

    Reference:
        HCI.TS.p39, Sections 4.10.11-12, pages 200-202, Tables 4.50-51;
        Core Vol 4, Part E, 7.8.12 and 7.8.66.
    """
    await tester.command(hci.HCI_Reset_Command())
    await tester.masks()
    result = await tester.command(
        create_connection(
            extended=extended, own_address_type=own_type, initiator_filter_policy=policy
        ),
        (ErrorCode.SUCCESS, ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
    )
    if result.status == ErrorCode.SUCCESS:
        event = await tester.event(
            hci.HCI_LE_Enhanced_Connection_Complete_Event
            if extended
            else hci.HCI_LE_Connection_Complete_Event
        )
        assert event.status == ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR


async def invalid_decision_data(tester):
    """LE Set Decision Data, Invalid Parameters.

    Initial Condition:
        The IUT is not scanning.

    Test Steps:
        Repeat steps 1-3 for all five Table 4.63 rounds: configure decision
        advertising on LE 1M, send Decision Data with the specified handle,
        flags and length, and receive Command Complete with the listed status.

    Expected Outcome:
        Unknown handle: 0x42; tag shorter than six: 0x12; five arbitrary octets:
        Success; nine octets: 0x12; empty data without a tag: Success.

    Reference:
        HCI.TS.p39, Section 4.10, BI-73-C, pages 214-215, Table 4.63;
        Core Vol 4, Part E, 7.8.144.
    """
    for handle, flags, size, status in [
        (1, 0, 0, 0x42),
        (0, 1, 5, 0x12),
        (0, 0, 5, 0),
        (0, 0, 9, 0x12),
        (0, 0, 0, 0),
    ]:
        await tester.command(extended_parameters(advertising_event_properties=0x80))
        await tester.command(
            hci.HCI_LE_Set_Decision_Data_Command(handle, flags, bytes(size)), status
        )


async def invalid_decision_instructions(tester):
    """LE Set Decision Instructions, Invalid Parameters.

    Initial Condition:
        IXIT TSPX_max_decision_tests is eight.

    Test Steps:
        Repeat steps 1-2 for Table 4.64: zero tests, one more than IXIT capacity,
        and one test with Test_Flags[0] bit zero cleared.

    Expected Outcome:
        Command Complete statuses are respectively 0x12, 0x43 and 0x12.

    Reference:
        HCI.TS.p39, Section 4.10, BI-74-C, pages 215-216, Table 4.64;
        Core Vol 4, Part E, 7.8.145.
    """
    for count, flags, status in [
        (0, 1, 0x12),
        (EXPECTED_DECISION_TESTS + 1, 1, 0x43),
        (1, 0, 0x12),
    ]:
        await tester.command(
            hci.HCI_LE_Set_Decision_Instructions_Command(
                [flags] * count, [6] * count, [bytes([7]) + bytes(15)] * count
            ),
            status,
        )


async def eight_decision_tests(tester):
    """LE Set Decision Instructions, Support for 8 Tests.

    Initial Condition:
        The IUT is in standby.

    Test Steps:
        1. Send eight valid AdvMode tests with the first test starting a group.
        2. Receive successful Command Complete.

    Expected Outcome:
        The controller accepts all eight tests in one command.

    Source Correction:
        Page 216 step 1 mentions General_Flags. Core Vol 4, Part E, 7.8.145
        defines no such command field and requires Test_Flags[0] bit 0 to be one.
        Send the Core-defined arrays, with first flags 0x03 and remaining 0x02.
        This local wire-format correction is not an adopted SIG erratum.

    Reference:
        HCI.TS.p39, Section 4.10, BV-25-C, page 216;
        Core Vol 4, Part E, 7.8.145.
    """
    await tester.command(
        hci.HCI_LE_Set_Decision_Instructions_Command(
            [3] + [2] * 7, [6] * 8, [bytes([7]) + bytes(15)] * 8
        )
    )


PROCEDURES: ProcedureMap = {}

# Verify that an LE-only IUT rejects a BR/EDR Inquiry command.
PROCEDURES |= procedure_cases(
    bredr_not_supported,
    {
        'HCI/CCO/BV-07-C': (
            'BR/EDR Not Supported',
            {},
        ),
    },
)

# Verify successful Set Data Length and notification of changed limits.
PROCEDURES |= procedure_cases(
    set_data_length,
    {
        'HCI/CCO/BV-09-C': (
            'LE Set Data Length',
            {},
        ),
    },
)

# Verify the reset values reported by Read Suggested Default Data Length.
PROCEDURES |= procedure_cases(
    read_suggested_data_length,
    {
        'HCI/CCO/BV-10-C': (
            'LE Read Suggested Default Data Length Command',
            {},
        ),
    },
)

# Verify storage and readback of all prescribed default-data-length pairs.
PROCEDURES |= procedure_cases(
    write_suggested_data_length,
    {
        'HCI/CCO/BV-11-C': (
            'LE Write Suggested Default Data Length Command',
            {},
        ),
    },
)

# Verify that Set Default PHY accepts no-preference selections.
PROCEDURES |= procedure_cases(
    set_default_phy,
    {
        'HCI/CCO/BV-15-C': (
            'LE Set Default PHY Command',
            {},
        ),
    },
)

# Verify that Read Periodic Advertiser List Size returns the declared capacity.
PROCEDURES |= procedure_cases(
    read_periodic_list_size,
    {
        'HCI/CCO/BV-16-C': (
            'LE Read Periodic Advertiser List Size Command',
            {},
        ),
    },
)

# Verify add, remove and clear operations and address-type/SID identity.
PROCEDURES |= procedure_cases(
    periodic_list_commands,
    {
        'HCI/CCO/BV-17-C': (
            'LE Add/Remove/Clear Periodic Advertiser List Commands',
            {},
        ),
    },
)

# Verify the Unchanged Data operation's conditions for periodic advertising data with
# Periodic Advertising ADI support.
PROCEDURES |= procedure_cases(
    invalid_periodic_data_parameters,
    {
        'HCI/CCO/BI-33-C': (
            'Invalid LE Set Periodic Advertising Data Parameters',
            {},
        ),
    },
)

# Verify rejection of TxTime one microsecond above its maximum.
PROCEDURES |= procedure_cases(
    invalid_data_length,
    {
        'HCI/CCO/BI-40-C': (
            'LE Set Data Length, Invalid Parameters',
            {},
        ),
    },
)

# Verify rejection of Read Channel Map for an unassigned ACL handle.
PROCEDURES |= procedure_cases(
    invalid_channel_map_handle,
    {
        'HCI/CCO/BI-43-C': (
            'LE Read Channel Map – Reject Invalid Handle',
            {},
        ),
    },
)

# Verify invalid-handle and reversed-interval errors for the selected v1 command.
PROCEDURES |= procedure_cases(
    invalid_periodic_parameters,
    {
        'HCI/CCO/BI-64-C': (
            (
                'LE Set Periodic Advertising Parameters, Invalid '
                'Parameters, HCI_LE_Set_Periodic_Advertising_Parameters '
                '[v1]'
            ),
            {},
        ),
    },
)

# Verify rejection of Min_CE_Length greater than Max_CE_Length.
PROCEDURES |= procedure_cases(
    invalid_connection_event_lengths,
    {
        'HCI/CCO/BI-134-C': (
            'LE Create Connection, Invalid Parameters',
            {},
        ),
    },
)

# Verify successful removal of one resolving-list entry or clearing of the list after
# adding a valid peer identity.
PROCEDURES |= procedure_cases(
    resolving_list_remove,
    {
        'HCI/CCO/BV-12-C': ('LE Remove Device From Resolving List Command', {}),
        'HCI/CCO/BV-13-C': ('LE Clear Resolving List Command', {'clear': True}),
    },
)

# Verify that Read Resolving List Size reports the declared total capacity.
PROCEDURES |= procedure_cases(
    resolving_list_size,
    {
        'HCI/CCO/BV-14-C': ('LE Read Resolving List Size Command', {}),
    },
)

# Verify that resolving-list commands fail while the enabled resolver is in use by
# advertising, scanning, initiating or periodic sync acquisition.
PROCEDURES |= procedure_cases(
    resolving_list_busy,
    {
        f'HCI/CCO/BI-{number:02}-C': (
            'Resolving List Commands fail when list in use',
            {'role': role},
        )
        for number, role in enumerate(
            ('advertising', 'scanning', 'legacy', 'extended', 'sync'), 1
        )
    },
)

# Verify handling of a repeated identity or nonzero Peer IRK, including removal of the
# proposed second identity after the duplicate addition.
PROCEDURES |= procedure_cases(
    resolving_list_duplicate,
    {
        f'HCI/CCO/BI-{number}-C': (
            'LE Add Device To Resolving List – Duplicate Entry'
            if number == 46
            else 'LE Add Device To Resolving List – Existing Peer IRK Entry',
            {'change': change},
        )
        for number, change in [(46, 'none'), (47, 'type'), (48, 'address')]
    },
)

# Verify rejection of legacy and extended connection creation when the selected own-
# address type requires an unset random fallback address.
PROCEDURES |= procedure_cases(
    create_without_random_address,
    {
        f'HCI/CCO/BI-{number}-C': (
            f'Reject Invalid {"Extended " if number >= 54 else ""}'
            'Create Connection Command, '
            f'0x{own_type:02X}, {"N/A" if own_type == 1 else f"0x{policy:02X}"}',
            {'extended': number >= 54, 'own_type': own_type, 'policy': policy},
        )
        for number, own_type, policy in [
            (51, 1, 0),
            (52, 3, 0),
            (53, 3, 1),
            (54, 1, 0),
            (55, 3, 0),
            (56, 3, 1),
        ]
    },
)

# Verify the specified statuses for unknown advertising handles and decision-data
# lengths, including insufficient tag data and valid empty data.
PROCEDURES |= procedure_cases(
    invalid_decision_data,
    {
        'HCI/CCO/BI-73-C': ('LE Set Decision Data, Invalid Parameters', {}),
    },
)

# Verify rejection of zero decision tests, a test count above the declared limit, and a
# first test that does not start a group.
PROCEDURES |= procedure_cases(
    invalid_decision_instructions,
    {
        'HCI/CCO/BI-74-C': ('LE Set Decision Instructions, Invalid Parameters', {}),
    },
)

# Verify that Set Decision Instructions accepts at least eight valid tests in one
# command.
PROCEDURES |= procedure_cases(
    eight_decision_tests,
    {
        'HCI/CCO/BV-25-C': ('LE Set Decision Instructions, Support for 8 Tests', {}),
    },
)

RADIO_CASES = ('HCI/CCO/BV-09-C', 'HCI/CCO/BI-40-C', 'HCI/CCO/BI-43-C')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES, radio_cases=RADIO_CASES),
)
async def test_controller_configuration_commands(
    hci_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(hci_dut, identifier, description, procedure, parameters, marks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES, radio_cases=RADIO_CASES, radio=True),
)
async def test_controller_configuration_radio(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
