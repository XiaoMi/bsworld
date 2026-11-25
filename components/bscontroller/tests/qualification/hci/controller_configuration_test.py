# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""HCI.TS.p39, Section 4.10: Controller Configuration."""

import random
import struct

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
    LL_EVENT_COUNTER_MASK,
    LL_IFS_US,
    LL_PERIODIC_INTERVAL_MIN,
    PDU_DATA_EMPTY_TIME_CODED_S8_US,
    PDU_DATA_OCTET_TIME_CODED_S8_US,
    PDU_DATA_PAYLOAD_SIZE_MAX,
    PDU_DATA_PAYLOAD_SIZE_MIN,
    PDU_DATA_PAYLOAD_TIME_CODED_MAX_US,
    PDU_DATA_PAYLOAD_TIME_CODED_MIN_US,
    PDU_DATA_PAYLOAD_TIME_MIN_US,
    PDU_DATA_PAYLOAD_TIME_UNCODED_MAX_US,
    ControlOpcode,
    LlId,
)
from bscontroller.radio.base import Phy
from tests.qualification import pawr as pawr_peer
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.hci.power_control_test import expect_remote_power_read_error
from tests.qualification.peer import (
    PEER_FEATURES,
    change_phy,
    establish,
    exchange_feature_page,
    exchange_features,
)
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

    Test Case Configuration:
        Rounds 1-9 use the ordered Cartesian product TxOctets = 0x001B, 0x00FB,
        0x0080 and, for each, TxTime = 0x0148, 0x4290, 0x2000. Rounds 10-20
        choose each value randomly in the inclusive ranges 0x001B..0x00FB and
        0x0148..0x4290. A locally seeded generator makes those stimuli repeatable.

    Test Steps:
        The source describes an unnumbered procedure for every Table 4.33 row:
        - Send LE Write Suggested Default Data Length with that row's TxOctets
          and TxTime, and receive Command Complete.
        - Send LE Read Suggested Default Data Length and receive Command
          Complete with both stored values.

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

    Test Case Configuration:
        Steps 4 and 12 do not give a data length; send zero octets, so the
        disabled train (step 4) and the missing data (step 12) are the only
        invalid conditions. The selected extended interval is 100 ms and the
        minimum periodic interval is 0x0006 (7.5 ms).

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

    Test Case Configuration:
        BI-64 selects v1, rounds 1 and 2 only: unknown handle with other values
        valid returns 0x42; Min greater than Max with other values valid returns
        0x12. Configure valid handle zero before the rounds; use unknown handle
        one in round 1, then handle zero with Min = 41, Max = 40 in round 2.
        The unselected BI-65 uses v2 and all Table 4.57 rounds.

    Test Steps:
        Repeat steps 1 and 2 for the rounds selected by Table 4.56:
        1. Send the selected Set Periodic Advertising Parameters command with
           the parameters of the Table 4.57 round.
        2. Require Command Complete with that round's prescribed status.

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

    Test Case Configuration:
        HCI/CCO/BV-12-C: remove one resolving-list entry.
        HCI/CCO/BV-13-C: clear the resolving list.

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

    Test Case Configuration:
        Advertising uses ADV_NONCONN_IND; scanning is active; initiation uses
        explicit peer filtering; periodic acquisition uses Options zero.

    Test Steps:
        1. Send the role configuration and enable commands in Table 4.38.
        2. Send Add, Remove, Clear, Set Address Resolution Enable (both 0 and 1),
           and Set Privacy Mode for both public and random identity types.

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

    Test Case Configuration:
        HCI/CCO/BI-46-C: identical resolving-list entry.
        HCI/CCO/BI-47-C: same Peer IRK with a changed identity address type.
        HCI/CCO/BI-48-C: same Peer IRK with a changed identity address.

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

    Test Case Configuration:
        HCI/CCO/BI-51-C: LE Create Connection, Own_Address_Type 0x01,
            Initiator_Filter_Policy 0x00.
        HCI/CCO/BI-52-C: LE Create Connection, Own_Address_Type 0x03,
            Initiator_Filter_Policy 0x00.
        HCI/CCO/BI-53-C: LE Create Connection, Own_Address_Type 0x03,
            Initiator_Filter_Policy 0x01.
        HCI/CCO/BI-54-C: LE Extended Create Connection, Own_Address_Type 0x01,
            Initiator_Filter_Policy 0x00.
        HCI/CCO/BI-55-C: LE Extended Create Connection, Own_Address_Type 0x03,
            Initiator_Filter_Policy 0x00.
        HCI/CCO/BI-56-C: LE Extended Create Connection, Own_Address_Type 0x03,
            Initiator_Filter_Policy 0x01.

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


async def pawr_invalid_parameters(tester):
    """LE Set Periodic Advertising Parameters [v2], Invalid Parameters.

    Initial Condition:
        Standby with a valid non-connectable, non-scannable advertising set.

    Test Steps:
        1. Configure extended advertising set 0 on LE 1M.
        2. Send all fourteen Table 4.57 combinations, including unknown handle,
           interval ordering, subevent count/interval, response delay, spacing
           and slot count boundaries.
        3. For Num_Subevents zero, send invalid ignored PAwR fields. For one
           subevent, send Subevent_Interval below its otherwise valid range.

    Expected Outcome:
        Each Command Complete returns the table's status: 0x42 for an unknown
        set, 0x12 for invalid parameters, and success for the two ignored-field
        rounds. Every reply echoes Advertising_Handle.

    Reference:
        HCI.TS.p39, 4.10.15, HCI/CCO/BI-65-C, Tables 4.56/4.57.
        Core Vol 4, Part E, 7.8.61.
    """
    await tester.command(extended_parameters())
    baseline = dict(interval=800, timing=(10, 80, 8, 25), slots=25)
    rounds = [
        (dict(handle=1), 0x42),
        (dict(interval=800), 0x12),
        (dict(timing=(10, 81, 8, 25)), 0x12),
        (dict(timing=(10, 80, 80, 25)), 0x12),
        (dict(timing=(10, 80, 0, 25)), 0x12),
        (dict(timing=(10, 80, 8, 29)), 0x12),
        (dict(timing=(129, 6, 1, 2)), 0x12),
        (dict(timing=(10, 5, 1, 2)), 0x12),
        (dict(timing=(10, 80, 255, 25)), 0x12),
        (dict(timing=(10, 80, 8, 1)), 0x12),
        (dict(slots=0), 0x12),
        (dict(timing=(10, 24, 12, 25), slots=5), 0x12),
        (dict(timing=(0, 5, 255, 1), slots=0), 0),
        (dict(timing=(1, 5, 1, 10), slots=3), 0),
    ]
    for index, (changes, status) in enumerate(rounds, 1):
        command = pawr_peer.parameters(**(baseline | changes))
        if index == 2:
            # Construct the deliberately reversed interval pair on the wire.
            command = type(command)(0, 801, 800, 0, 10, 80, 8, 25, 25)
        await tester.command(command, status)
        assert tester.last_command_event.parameters[3:] == bytes(
            [status, command.advertising_handle]
        )


async def pawr_invalid_connection(tester):
    """LE Extended Create Connection [v2], Invalid Parameters.

    Initial Condition:
        PAwR advertising set 0 is enabled on LE 1M.

    Test Steps:
        1. Send the four Table 4.55 rounds: unknown handle, Subevent 0x80,
           mismatched 0xFF handle/subevent, and Min_CE_Length > Max_CE_Length.
        2. Observe Command Status for every asynchronous command.

    Expected Outcome:
        Unknown handle returns 0x42. The three invalid combinations return
        0x12. No connection procedure replaces a periodic subevent.

    Reference:
        HCI.TS.p39, HCI/CCO/BI-63-C, Table 4.55; Core 7.8.66.
    """
    await pawr_peer.configure(tester)
    for handle, subevent, minimum, maximum, status in (
        (1, 1, 0, 0, 0x42),
        (0, 128, 0, 0, 0x12),
        (255, 1, 0, 0, 0x12),
        (0, 1, 1, 0, 0x12),
    ):
        await tester.command(
            hci.HCI_LE_Extended_Create_Connection_V2_Command(
                handle,
                subevent,
                0,
                0,
                PEER_ADDRESS.address_type,
                PEER_ADDRESS,
                1,
                [16],
                [16],
                [24],
                [24],
                [0],
                [100],
                [minimum],
                [maximum],
            ),
            status,
        )
    await pawr_peer.stop_advertising(tester)


async def pawr_invalid_subevent_data(tester):
    """LE Set Periodic Advertising Subevent Data, Invalid Parameters.

    Initial Condition:
        Extended set 0 has ten PAwR subevents, SI 318.75 ms, RSD 1.25 ms,
        RSS 1.25 ms and five response slots. No data request exists yet.

    Test Steps:
        1. Before enabling the train, send subevent data and require rejection.
        2. Enable periodic and extended advertising; wait for a data request.
        3. Execute Table 4.60: handle range/existence, excessive airtime, zero
           or excessive entry count, subevent range/request window, truncated
           data, invalid response-slot range and duplicate subevent entries.
        4. On a fresh request, publish valid data, then publish the same
           requested instance again.

    Expected Outcome:
        Commands return the specified 0x42, 0x12, 0x45 or 0x0C status. A valid
        instance is accepted once; its second publication is rejected.

    Reference:
        HCI.TS.p39, 4.10.15.1, HCI/CCO/BI-124-C, Tables 4.59/4.60.
        Core Vol 4, Part E, 7.8.125.
    """
    await tester.command(extended_parameters())
    await tester.command(
        pawr_peer.parameters(interval=2550, timing=(10, 255, 1, 10), slots=5)
    )
    await tester.command(pawr_peer.SET_DATA(0, [0], [0], [1], [b'x']), 0x0C)
    await tester.masks(le=(1 << 64) - 1)
    await tester.command(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    await tester.command(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0], [0], [0])
    )
    for index in range(10):
        event = await pawr_peer.next_request(tester)
        sub = event.subevent_start
        command = pawr_peer.SET_DATA(0, [sub], [0], [1], [b'x'])
        status = 0x12
        if index == 0:
            command = pawr_peer.SET_DATA(1, [sub], [0], [1], [b'x'])
            status = 0x42
        if index == 1:
            command = pawr_peer.SET_DATA(240, [sub], [0], [1], [b'x'])
        if index == 2:
            command = pawr_peer.SET_DATA(0, [sub], [0], [1], [bytes(127)])
            status = 0x45
        if index == 3:
            command = pawr_peer.SET_DATA(0, [], [], [], [])
        if index == 4:
            command = pawr_peer.SET_DATA(
                0, list(range(16)), [0] * 16, [1] * 16, [b'x'] * 16
            )
        if index == 5:
            command = pawr_peer.SET_DATA(0, [128], [0], [1], [b'x'])
        if index == 6:
            outside = next(
                value
                for value in range(128)
                if not sub <= value < sub + event.subevent_data_count
            )
            command = pawr_peer.SET_DATA(0, [outside], [0], [1], [b'x'])
            status = 0x0C
        if index == 7:
            command = pawr_peer.SET_DATA.from_parameters(
                bytes([0, 1, sub, 0, 1, 255]) + bytes(249)
            )
        if index == 8:
            command = pawr_peer.SET_DATA(0, [sub], [6], [1], [b'x'])
        if index == 9:
            command = pawr_peer.SET_DATA(
                0, [sub, sub], [0, 0], [1, 1], [b'\x01', b'\x01']
            )
        await tester.command(command, status)
    event = await pawr_peer.next_request(tester)
    command = pawr_peer.SET_DATA(0, [event.subevent_start], [0], [1], [b'x'])
    await tester.command(command)
    await tester.command(command, 0x0C)
    await pawr_peer.stop_advertising(tester)


async def pawr_invalid_response_data(tester):
    """LE Set Periodic Advertising Response Data, Invalid Parameters.

    Initial Condition:
        Synchronize to the Lower Tester's five-subevent train and select 0.

    Test Steps:
        1. Before each Table 4.58 round, receive a fresh subevent-0 report;
           send the selected combination:
           invalid handle, excessive airtime, past slot, truncated payload,
           subevent outside the train and a subevent not synchronized with.
        2. For the passed-slot row, wait past that current event's slot.
           Observe no response after each rejected command.
        3. Terminate synchronization successfully.

    Expected Outcome:
        Status is respectively 0x12, 0x45, 0x46, 0x12, 0x0C and 0x0C.
        Invalid data never produces a response PDU.

    Reference:
        HCI.TS.p39, HCI/CCO/BI-66-C, Table 4.58; Core 7.8.126.
    """
    train, handle = await pawr_peer.scanner(tester)
    await tester.command(pawr_peer.SET_SUBEVENTS(handle, 0, [0]))
    for row in range(6):
        # Table 4.58 repeats Steps 2-4, including a fresh subevent-0 report.
        train.next_event()
        await pawr_peer.transmit_indication(tester, train, data=b'round')
        report = await tester.expect(
            lambda event: isinstance(event, pawr_peer.REPORT)
            and event.sync_handle == handle
            and event.periodic_event_counter == train.counter
            and event.subevent == 0
            and event.data == b'round'
        )
        assert report.data_status == 0
        counter = report.periodic_event_counter
        command = pawr_peer.SET_RESPONSE(handle, counter, 0, 0, 0, b'x')
        if row == 0:
            command = pawr_peer.SET_RESPONSE(0xF00, counter, 0, 0, 0, b'x')
        elif row == 1:
            command = pawr_peer.SET_RESPONSE(handle, counter, 0, 0, 0, bytes(127))
        elif row == 2:
            due = train.anchor + train.pawr[2] * 1250
            await tester.wait(max(0, due + 200 - tester.radio.now_us))
        elif row == 3:
            command = pawr_peer.SET_RESPONSE.from_parameters(
                handle.to_bytes(2, 'little')
                + counter.to_bytes(2, 'little')
                + bytes([0, 0, 0, 255])
                + bytes(247)
            )
        elif row == 4:
            command = pawr_peer.SET_RESPONSE(handle, counter, 0, 5, 0, b'x')
        else:
            command = pawr_peer.SET_RESPONSE(handle, counter, 0, 2, 0, b'x')
        await tester.command(command, (0x12, 0x45, 0x46, 0x12, 0x0C, 0x0C)[row])
        # Observe the remainder of this event's response window. An error
        # status alone does not prove that invalid response data was discarded.
        end = train.anchor + train.pawr[2] * 1250 + 10 * train.pawr[3] * 125
        packet = await tester.receive(
            train.channel,
            duration_us=max(1, end - tester.radio.now_us),
            access_address=train.rsp_aa,
            crc_init=train.crc_init,
        )
        assert packet is None, 'response emitted after an invalid command'
    await tester.command(hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle))


async def pawr_invalid_sync_subevents(tester):
    """LE Set Periodic Sync Subevent, Invalid Parameters.

    Initial Condition:
        An established five-subevent Lower Tester PAwR train on LE 1M.

    Test Steps:
        1. Execute Table 4.61 with handle > 0x0EFF, zero/129 entries, more
           entries than the train's subevents and a subevent > 0x7F.
        2. Terminate the established synchronization.

    Expected Outcome:
        Every invalid command returns 0x12; synchronization remains usable.

    Reference:
        HCI.TS.p39, HCI/CCO/BI-68-C, Table 4.61; Core 7.8.127.
    """
    train, handle = await pawr_peer.scanner(tester)
    for selected, subevents in (
        (0xF00, [0]),
        (handle, []),
        (handle, [0] * 129),
        (handle, [0] * 6),
        (handle, [128]),
    ):
        await tester.command(pawr_peer.SET_SUBEVENTS(selected, 0, subevents), 0x12)
    train.next_event()
    await pawr_peer.transmit_indication(tester, train, data=b'still synchronized')
    report = await tester.expect(
        lambda event: isinstance(event, pawr_peer.REPORT)
        and event.sync_handle == handle
        and event.data == b'still synchronized'
    )
    assert report.periodic_event_counter == train.counter and report.data_status == 0
    await tester.command(hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle))


async def write_rf_path_compensation(tester):
    """LE Write RF Path Compensation Command.

    Initial Condition:
        The IUT is in standby.

    Test Steps:
        Send LE Write RF Path Compensation with both compensation values set
        to 0x0001 (0.1 dB). Receive Command Complete.

    Expected Outcome:
        The command completes successfully.

    Reference:
        HCI.TS.p39, Section 4.10, HCI/CCO/BV-19-C, Figure 4.94,
        PDF pages 166-167; Core Vol 4, Part E, Section 7.8.76.
    """
    await tester.command(hci.HCI_LE_Write_RF_Path_Compensation_Command(1, 1))


async def read_rf_path_compensation(tester):
    """LE Read RF Path Compensation Command.

    Initial Condition:
        The IUT is in standby.

    Test Steps:
        Send LE Read RF Path Compensation and receive Command Complete with
        the current transmit and receive compensation values.

    Expected Outcome:
        The command succeeds and returns both signed compensation values.

    Reference:
        HCI.TS.p39, Section 4.10, HCI/CCO/BV-20-C, Figure 4.95,
        PDF page 167; Core Vol 4, Part E, Section 7.8.75.
    """
    result = await tester.command(hci.HCI_LE_Read_RF_Path_Compensation_Command())
    assert -1280 <= result.rf_tx_path_compensation_value <= 1280
    assert -1280 <= result.rf_rx_path_compensation_value <= 1280


async def invalid_power_control_parameters(tester, *, operation, invalid_phy=False):
    """Invalid LE Power Control HCI Parameters.

    Initial Condition:
        Establish a LE 1M ACL with the IUT as Peripheral.

    Test Case Configuration:
        BI-06 and BI-07 select Enhanced Read Transmit Power Level with an
        unused handle and PHY 0xF0, respectively. BI-08 and BI-09 select the
        same invalid parameters for Read Remote Transmit Power Level.
        BI-10 selects Set Path Loss Reporting Parameters, BI-11 selects Set
        Path Loss Reporting Enable, and BI-12 selects Set Transmit Power
        Reporting Enable, each with an unused handle.

    Test Steps:
        1. Send the command and invalid parameter selected in Table 4.39.
        2. Receive the event and error specified in the same row. For remote
           reads, accept the error in Command Status or in LE Transmit Power
           Reporting after a successful Command Status.

    Expected Outcome:
        An unused ACL handle results in 0x02. A PHY of 0xF0 results in 0x11.

    Reference:
        HCI.TS.p39, Section 4.10.3, HCI/CCO/BI-06-C through BI-12-C,
        Figure 4.99 and Table 4.39, PDF pages 175-177;
        Core Vol 4, Part E, Sections 7.8.117-7.8.121.
    """
    link = await establish(tester)
    handle, phy, error = (
        (link.handle, 0xF0, ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR)
        if invalid_phy
        else (link.handle + 1, 1, ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR)
    )
    if operation == 'remote_read':
        await expect_remote_power_read_error(tester, handle, phy, error)
    else:
        command, parameters = {
            'local_read': (
                hci.HCI_LE_Enhanced_Read_Transmit_Power_Level_Command,
                (handle, phy),
            ),
            'path_loss_parameters': (
                hci.HCI_LE_Set_Path_Loss_Reporting_Parameters_Command,
                (handle, 80, 0, 40, 0, 0),
            ),
            'path_loss_enable': (
                hci.HCI_LE_Set_Path_Loss_Reporting_Enable_Command,
                (handle, 1),
            ),
            'power_reporting_enable': (
                hci.HCI_LE_Set_Transmit_Power_Reporting_Enable_Command,
                (handle, 1, 1),
            ),
        }[operation]
        await tester.command(command(*parameters), error)
    await link.disconnect()


async def invalid_path_loss_monitoring_parameters(tester):
    """Invalid Path Loss Monitoring Parameters.

    Initial Condition:
        Establish a LE 1M ACL with the IUT as Peripheral. The fixture starts
        with 45 dB path attenuation and path loss reporting is unconfigured.

    Test Steps:
        1. Exchange empty ACL data packets at a 7.5 ms connection interval.
        2. Enable path loss reporting before setting its parameters.
        3. Set high threshold 0xF0 and high hysteresis 0xF0.
        4. Set low threshold 0x10 and low hysteresis 0x20.
        5. Set high threshold 0xE0 and low threshold 0xF0.
        6. Set high threshold 0x50, high hysteresis 0x03, low threshold 0x4F,
           and low hysteresis 0x05.

    Expected Outcome:
        Command Complete reports 0x0C in step 2 and 0x12 in each of steps 3-6.

    Source Correction:
        The figure gives 0xE0 as the last high threshold. The numbered step 6
        gives 0x50, which exercises the overlapping hysteresis boundaries in
        Core Section 7.8.119.

    Reference:
        HCI.TS.p39, Section 4.10, HCI/CCO/BI-13-C, Figure 4.100,
        PDF pages 177-179; Core Vol 4, Part E, Section 7.8.119 and 7.8.120.
    """
    link = await establish(tester, interval=6)
    await link.exchange()
    await tester.command(
        hci.HCI_LE_Set_Path_Loss_Reporting_Enable_Command(link.handle, 1),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )
    for thresholds in (
        (0xF0, 0xF0, 0, 0),
        (0x50, 0, 0x10, 0x20),
        (0xE0, 0, 0xF0, 0),
        (0x50, 0x03, 0x4F, 0x05),
    ):
        await link.exchange()
        await tester.command(
            hci.HCI_LE_Set_Path_Loss_Reporting_Parameters_Command(
                link.handle, *thresholds, 0
            ),
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        )
    await link.disconnect()


PROCEDURES: ProcedureMap = {}

# Verify that a valid RF path compensation write completes in standby.
PROCEDURES |= procedure_cases(
    write_rf_path_compensation,
    {'HCI/CCO/BV-19-C': ('LE Write RF Path Compensation Command', {})},
)

# Verify that the standby read returns both signed compensation values.
PROCEDURES |= procedure_cases(
    read_rf_path_compensation,
    {'HCI/CCO/BV-20-C': ('LE Read RF Path Compensation Command', {})},
)

# Table 4.39 covers invalid ACL handles and RFU PHY values for the power commands.
PROCEDURES |= procedure_cases(
    invalid_power_control_parameters,
    {
        'HCI/CCO/BI-06-C': (
            'LE Enhanced Read Transmit Power Level – Invalid Connection Handle',
            {'operation': 'local_read'},
        ),
        'HCI/CCO/BI-07-C': (
            'LE Enhanced Read Transmit Power Level – Invalid PHY',
            {'operation': 'local_read', 'invalid_phy': True},
        ),
        'HCI/CCO/BI-08-C': (
            'LE Read Remote Transmit Power Level – Invalid Connection Handle',
            {'operation': 'remote_read'},
        ),
        'HCI/CCO/BI-09-C': (
            'LE Read Remote Transmit Power Level – Invalid PHY',
            {'operation': 'remote_read', 'invalid_phy': True},
        ),
        'HCI/CCO/BI-10-C': (
            'LE Set Path Loss Reporting Parameters – Invalid Connection Handle',
            {'operation': 'path_loss_parameters'},
        ),
        'HCI/CCO/BI-11-C': (
            'LE Set Path Loss Reporting Enable – Invalid Connection Handle',
            {'operation': 'path_loss_enable'},
        ),
        'HCI/CCO/BI-12-C': (
            'LE Set Transmit Power Reporting Enable – Invalid Connection Handle',
            {'operation': 'power_reporting_enable'},
        ),
    },
)

# Figure 4.100 verifies enable-before-configuration and four invalid thresholds.
PROCEDURES |= procedure_cases(
    invalid_path_loss_monitoring_parameters,
    {'HCI/CCO/BI-13-C': ('Invalid Path Loss Monitoring Parameters', {})},
)

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

RADIO_CASES = (
    'HCI/CCO/BV-09-C',
    'HCI/CCO/BI-36-C',
    'HCI/CCO/BI-37-C',
    'HCI/CCO/BI-39-C',
    'HCI/CCO/BI-75-C',
    'HCI/CCO/BI-40-C',
    'HCI/CCO/BI-43-C',
    'HCI/CCO/BI-63-C',
    'HCI/CCO/BI-124-C',
    'HCI/CCO/BI-66-C',
    'HCI/CCO/BI-68-C',
    'HCI/CCO/BI-06-C',
    'HCI/CCO/BI-07-C',
    'HCI/CCO/BI-08-C',
    'HCI/CCO/BI-09-C',
    'HCI/CCO/BI-10-C',
    'HCI/CCO/BI-11-C',
    'HCI/CCO/BI-12-C',
    'HCI/CCO/BI-13-C',
    *(f'HCI/CCO/BI-{number}-C' for number in (*range(135, 141), *range(143, 148))),
)


# Verify LE Extended Create Connection [v2], Invalid Parameters.
PROCEDURES |= procedure_cases(
    pawr_invalid_connection,
    {
        'HCI/CCO/BI-63-C': (
            'LE Extended Create Connection [v2], Invalid Parameters.',
            {},
        ),
    },
)

# Verify LE Set Periodic Advertising Parameters [v2], Invalid Parameters.
PROCEDURES |= procedure_cases(
    pawr_invalid_parameters,
    {
        'HCI/CCO/BI-65-C': (
            'LE Set Periodic Advertising Parameters [v2], Invalid Parameters.',
            {},
        ),
    },
)

# Verify LE Set Periodic Advertising Subevent Data, Invalid Parameters.
PROCEDURES |= procedure_cases(
    pawr_invalid_subevent_data,
    {
        'HCI/CCO/BI-124-C': (
            'LE Set Periodic Advertising Subevent Data, Invalid Parameters.',
            {},
        ),
    },
)

# Verify LE Set Periodic Advertising Response Data, Invalid Parameters.
PROCEDURES |= procedure_cases(
    pawr_invalid_response_data,
    {
        'HCI/CCO/BI-66-C': (
            'LE Set Periodic Advertising Response Data, Invalid Parameters.',
            {},
        ),
    },
)

# Verify LE Set Periodic Sync Subevent, Invalid Parameters.
PROCEDURES |= procedure_cases(
    pawr_invalid_sync_subevents,
    {
        'HCI/CCO/BI-68-C': (
            'LE Set Periodic Sync Subevent, Invalid Parameters.',
            {},
        ),
    },
)


async def monitored_advertisers_capacity(tester):
    """LE Monitoring Advertisers RSSI command, Memory Capacity Exceeded.

    Initial Condition:
        The IUT is in standby with an empty Monitored Advertisers List.

    Test Steps:
        1. Add devices with distinct public addresses, low threshold -70 dBm,
           high threshold -40 dBm and timeout 10 seconds; require success.
        2. Repeat until Command Complete reports Memory Capacity Exceeded (0x07).
        3. Send LE Read Monitored Advertisers List Size with no parameters.
        4. Require success and Number equal to the number of successful additions.

    Expected Outcome:
        Pass verdict: a full list rejects another address with 0x07, and the
        reported Number is the independently declared capacity of eight devices.

    Reference:
        HCI.TS.p39, Section 4.10, HCI/CCO/BV-24-C, PDF pages 212-213;
        Core Vol 4, Part E, 7.8.146 and 7.8.150.
    """
    for index in range(9):
        address = hci.Address(
            index.to_bytes(6, 'little'), hci.AddressType.PUBLIC_DEVICE
        )
        await tester.command(
            hci.HCI_LE_Add_Device_To_Monitored_Advertisers_List_Command(
                0, address, -70, -40, 10
            ),
            ErrorCode.SUCCESS
            if index < 8
            else ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR,
        )
    result = await tester.command(
        hci.HCI_LE_Read_Monitored_Advertisers_List_Size_Command()
    )
    assert result.number == 8


async def invalid_monitored_advertisers_parameters(tester):
    """Invalid LE Monitoring Advertisers Parameters.

    Initial Condition:
        The IUT is in standby; the Monitored Advertisers List is empty. All
        unchanged add parameters are public address, -70/-40 dBm and 10 seconds.

    Test Steps:
        1. Perform all eight Table 4.62 rounds: add with address type 2; low
           threshold 21; high threshold 21; high below low; timeout zero;
           remove with address type 2; remove a valid absent address; enable 2.
        2. In every round require Command Complete for the transmitted opcode
           with Invalid HCI Command Parameters (0x12).

    Expected Outcome:
        Pass verdict: all eight invalid commands return 0x12.

    Reference:
        HCI.TS.p39, Section 4.10, HCI/CCO/BI-71-C, Table 4.62,
        PDF pages 213-214; Core Vol 4, Part E, 7.8.146, 7.8.147 and 7.8.149.
    """
    add = hci.HCI_LE_Add_Device_To_Monitored_Advertisers_List_Command
    remove = hci.HCI_LE_Remove_Device_From_Monitored_Advertisers_List_Command
    for command in (
        add(2, PEER_ADDRESS, -70, -40, 10),
        add(0, PEER_ADDRESS, 21, -40, 10),
        add(0, PEER_ADDRESS, -70, 21, 10),
        add(0, PEER_ADDRESS, -40, -70, 10),
        add(0, PEER_ADDRESS, -70, -40, 0),
        remove(2, PEER_ADDRESS),
        remove(0, PEER_ADDRESS),
        hci.HCI_LE_Enable_Monitoring_Advertisers_Command(2),
    ):
        await tester.command(command, ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR)


# Verify monitoring-list capacity and every invalid-parameter round in Table 4.62.
PROCEDURES |= procedure_cases(
    monitored_advertisers_capacity,
    {
        'HCI/CCO/BV-24-C': (
            'LE Monitoring Advertisers RSSI command, Memory Capacity Exceeded',
            {},
        )
    },
)
PROCEDURES |= procedure_cases(
    invalid_monitored_advertisers_parameters,
    {'HCI/CCO/BI-71-C': ('Invalid LE Monitoring Advertisers Parameters', {})},
)


async def invalid_subrate_parameters(tester, *, existing):
    """Invalid Subrate Parameters.

    Initial Condition:
        The IUT has a Central ACL with a 10 ms connection interval. The
        Lower Tester supports Connection Subrating.

    Test Case Configuration:
        BI-36 uses LE Subrate Request and checks Command Status. BI-37 uses
        LE Set Default Subrate and checks Command Complete.

    Test Steps:
        1. For BI-36, send LE Subrate Request with an unused handle and
           otherwise valid parameters.
        2. Require Command Status with Unknown Connection Identifier (0x02).
        3. Execute the rounds in Table 4.44 using the selected command.
           Only BI-36 runs round 9, which depends on the current interval.
        4. Require Invalid HCI Command Parameters (0x12) for each round.

    Expected Outcome:
        The command response has the selected event type and the specified
        status for each invalid handle or parameter set.

    Reference:
        HCI.TS.p39, 4.10.7, Tables 4.43 and 4.44, PDF pages 186-188;
        Core Vol 4, Part E, 7.8.123 and 7.8.124.
    """
    link = await establish(tester, central=True, interval=8)
    if existing:
        await exchange_features(
            link,
            int(
                hci.LeFeatureMask.CONNECTION_SUBRATING
                | hci.LeFeatureMask.CONNECTION_SUBRATING_HOST_SUPPORT
            ),
        )
        await tester.command(
            hci.HCI_LE_Subrate_Request_Command(0x0EFF, 1, 2, 0, 0, 50),
            ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR,
        )

    rounds = (
        (0, 2, 2, 0, 50),
        (4, 3, 2, 0, 50),
        (501, 500, 500, 0, 50),
        (2, 501, 2, 0, 50),
        (2, 3, 0x01F4, 0, 50),
        (2, 0x01F4, 2, 0x01F4, 50),
        (2, 3, 3, 0, 0x0009),
        (2, 3, 3, 0, 0x0C81),
        (25, 25, 5, 0, 0x00C8),
        (3, 5, 2, 5, 50),
    )
    for number, values in enumerate(rounds, start=1):
        if not existing and number == 9:
            continue
        command = (
            hci.HCI_LE_Subrate_Request_Command(link.handle, *values)
            if existing
            else hci.HCI_LE_Set_Default_Subrate_Command(*values)
        )
        await tester.command(command, ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR)


async def invalid_default_subrate_in_standby(tester):
    """LE Set Default Subrate, Invalid Parameters.

    Initial Condition:
        The IUT is in standby. Unspecified fields use zero latency or
        continuation and a 500 ms supervision timeout.

    Test Steps:
        1. Set Subrate_Min = 1, Subrate_Max = 3 and Max_Latency = 166.
        2. Set Subrate_Min = 2 and Subrate_Max = 1.
        3. Set Subrate_Min = 1, Subrate_Max = 2 and Continuation_Number = 3.
        After each command, require Command Complete with Invalid HCI
        Command Parameters (0x12).

    Expected Outcome:
        All three invalid commands return status 0x12.

    Reference:
        HCI.TS.p39, 4.10, HCI/CCO/BI-120-C, PDF pages 246-247;
        Core Vol 4, Part E, 7.8.123.
    """
    for values in ((1, 3, 166, 0), (2, 1, 0, 0), (1, 2, 0, 3)):
        await tester.command(
            hci.HCI_LE_Set_Default_Subrate_Command(*values, 50),
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        )


async def invalid_auth_timeout_for_subrate(tester):
    """Invalid Write Authenticated Payload Timeout Parameters.

    Initial Condition:
        The IUT is Peripheral on a 10 ms ACL. The Lower Tester sets the
        subrate factor to 3, continuation number to 0 and latency to 2.

    Test Steps:
        1. Write Authenticated Payload Timeout = 0x0008 (80 ms).
        2. Verify the returned error status. The source warns unless the
           status is Command Disallowed (0x0C), which this DUT returns.
        3. Write 0x0009 (90 ms) on the same connection.
        4. Require a successful Command Complete.

    Expected Outcome:
        An 80 ms timeout is rejected and a 90 ms timeout is accepted.

    Reference:
        HCI.TS.p39, 4.10, HCI/CCO/BI-39-C, PDF page 189;
        Core Vol 4, Part E, 7.3.94.
    """
    await tester.command(
        hci.HCI_LE_Set_Host_Feature_Command(
            hci.LeFeature.CONNECTION_SUBRATING_HOST_SUPPORT, 1
        )
    )
    link = await establish(tester, interval=8)
    await exchange_features(
        link,
        int(
            hci.LeFeatureMask.CONNECTION_SUBRATING
            | hci.LeFeatureMask.CONNECTION_SUBRATING_HOST_SUPPORT
        ),
    )
    await link.exchange(
        struct.pack('<B5H', Opcode.LL_SUBRATE_IND, 3, link.counter, 2, 0, 50),
        llid=LlId.CONTROL,
        check_ack=True,
    )
    event = await tester.event(hci.HCI_LE_Subrate_Change_Event)
    assert (
        event.status,
        event.connection_handle,
        event.subrate_factor,
        event.peripheral_latency,
        event.continuation_number,
        event.supervision_timeout,
    ) == (ErrorCode.SUCCESS, link.handle, 3, 2, 0, 50)

    await tester.command(
        hci.HCI_Write_Authenticated_Payload_Timeout_Command(link.handle, 8),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )
    result = await tester.command(
        hci.HCI_Write_Authenticated_Payload_Timeout_Command(link.handle, 9)
    )
    assert result.connection_handle == link.handle


async def unsupported_host_feature(tester):
    """Reject Setting Host Controlled FeatureSet Bit, Unsupported Feature on Controller.

    Initial Condition:
        The IUT is in standby and Channel Sounding Host Support is clear.

    Test Steps:
        1. Send LE Set Host Feature with bit 47 and value 1.
        2. Require Command Complete with Unsupported Feature or Parameter
           Value (0x11).

    Expected Outcome:
        The command returns status 0x11.

    Reference:
        HCI.TS.p39, 4.10.8, Table 4.47 and Figure 4.107, PDF pages 192-193;
        Core Vol 4, Part E, 7.8.115 and Vol 6, Part B, 4.6.
    """
    await tester.command(
        hci.HCI_LE_Set_Host_Feature_Command(
            hci.LeFeature.CHANNEL_SOUNDING_HOST_SUPPORT, 1
        ),
        ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
    )


# Validate all Table 4.44 rounds applicable to each of the two subrate commands.
PROCEDURES |= procedure_cases(
    invalid_subrate_parameters,
    {
        'HCI/CCO/BI-36-C': (
            'Invalid Subrate Parameters – LE Subrate Request',
            {'existing': True},
        ),
        'HCI/CCO/BI-37-C': (
            'Invalid Subrate Parameters – LE Set Default Subrate',
            {'existing': False},
        ),
    },
)

# Verify the authenticated payload timeout bound on a subrated connection.
PROCEDURES |= procedure_cases(
    invalid_auth_timeout_for_subrate,
    {'HCI/CCO/BI-39-C': ('Invalid Write Authenticated Payload Timeout Parameters', {})},
)

# Reject LE Set Default Subrate with invalid latency, factor or continuation.
PROCEDURES |= procedure_cases(
    invalid_default_subrate_in_standby,
    {'HCI/CCO/BI-120-C': ('LE Set Default Subrate, Invalid Parameters', {})},
)

# Reject the unsupported Channel Sounding Host bit selected in Table 4.47.
PROCEDURES |= procedure_cases(
    unsupported_host_feature,
    {'HCI/CCO/BI-122-C': ('Reject Unsupported Channel Sounding Host Feature', {})},
)


async def _expect_frame_space_update_error(tester, command, status):
    response = await tester.command(command, (ErrorCode.SUCCESS, status))
    if response.status == ErrorCode.SUCCESS:
        complete = await tester.event(hci.HCI_LE_Frame_Space_Update_Complete_Event)
        assert complete.connection_handle == command.connection_handle
        assert complete.status == status


async def invalid_frame_space_parameters(tester):
    """LE Frame Space Update, Invalid Frame Space Parameters.

    Initial Condition:
        The IUT is Peripheral on an established LE 1M ACL with 150 µs
        inter-frame space in both directions.

    Test Steps:
        1. Send LE Frame Space Update for each round of Table 4.34: Min above
           0x2710, Max above 0x2710, unknown ACL handle, zero PHYs, zero
           Spacing_Types, and Min = 1 with Max = 0.
        2. In each round observe Command Status or, if successful, the
           subsequent LE Frame Space Update Complete.

    Expected Outcome:
        The observed failure status is Unknown Connection Identifier (0x02)
        for round 3 and Invalid HCI Command Parameters (0x12) otherwise.

    Reference:
        HCI.TS.p39, 4.10, HCI/CCO/BI-75-C, Table 4.34, PDF page 171;
        Core Vol 4, Part E, 7.8.151 and 7.7.65.48.
    """
    link = await establish(tester)
    command = hci.HCI_LE_Frame_Space_Update_Command
    defaults = {
        'connection_handle': link.handle,
        'frame_space_min': 150,
        'frame_space_max': 200,
        'phys': hci.PhyBit.LE_1M,
        'spacing_types': command.SpacingType.T_IFS_ACL_CP,
    }
    invalid = ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
    for changes, status in (
        ({'frame_space_min': 0x2711}, invalid),
        ({'frame_space_max': 0x2711}, invalid),
        (
            {'connection_handle': 0x0EFF},
            ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR,
        ),
        ({'phys': 0}, invalid),
        ({'spacing_types': 0}, invalid),
        ({'frame_space_min': 1, 'frame_space_max': 0}, invalid),
    ):
        await _expect_frame_space_update_error(
            tester, command(**(defaults | changes)), status
        )


# Validate Table 4.34, including the unknown connection handle.
PROCEDURES |= procedure_cases(
    invalid_frame_space_parameters,
    {'HCI/CCO/BI-75-C': ('LE Frame Space Update, Invalid Frame Space Parameters', {})},
)

RATE_HOST_FEATURE = hci.LeFeature.SHORTER_CONNECTION_INTERVALS_HOST_SUPPORT
RATE_CHANGE = hci.HCI_LE_Connection_Rate_Change_Event
# The test preamble uses a 10 ms ACL, 6 s supervision timeout and no subrating.
RATE_PARAMETERS = {
    'connection_interval_min': 80,
    'connection_interval_max': 80,
    'subrate_min': 1,
    'subrate_max': 1,
    'max_latency': 0,
    'continuation_number': 0,
    'supervision_timeout': 600,
    'min_ce_length': 1,
    'max_ce_length': 80,
}


def _rate_request(link, **changes):
    return hci.HCI_LE_Connection_Rate_Request_Command(
        link.handle, **(RATE_PARAMETERS | changes)
    )


async def _connect_for_rate(tester, *, central=False, peer_host=True, dle=True):
    await tester.command(hci.HCI_LE_Set_Host_Feature_V2_Command(RATE_HOST_FEATURE, 1))
    await tester.command(
        hci.HCI_LE_Set_Host_Feature_Command(
            hci.LeFeature.CONNECTION_SUBRATING_HOST_SUPPORT, 1
        )
    )
    link = await establish(tester, central=central, interval=8, timeout=600)
    features = (
        PEER_FEATURES
        | hci.LeFeatureMask.LL_EXTENDED_FEATURE_SET
        | hci.LeFeatureMask.CONNECTION_PARAMETERS_REQUEST_PROCEDURE
        | hci.LeFeatureMask.CONNECTION_SUBRATING
        | hci.LeFeatureMask.CONNECTION_SUBRATING_HOST_SUPPORT
    )
    if not dle:
        features &= ~hci.LeFeatureMask.LE_DATA_PACKET_LENGTH_EXTENSION
    await exchange_features(link, int(features))
    controller_bit = int(hci.LeFeatureMask.SHORTER_CONNECTION_INTERVALS) >> 64
    host_bit = int(hci.LeFeatureMask.SHORTER_CONNECTION_INTERVALS_HOST_SUPPORT) >> 64
    remote_bits = controller_bit | (host_bit if peer_host else 0)
    local_bits = await exchange_feature_page(link, remote_bits)
    assert local_bits & (controller_bit | host_bit) == controller_bit | host_bit
    return link


async def _expect_rate_error(tester, command, status):
    result = await tester.command(command, (ErrorCode.SUCCESS, status))
    if result.status == ErrorCode.SUCCESS:
        event = await tester.event(RATE_CHANGE)
        assert (event.connection_handle, event.status) == (
            command.connection_handle,
            status,
        )


async def rate_request_requires_peer_host_support(tester, *, central):
    """Reject a Connection Rate Request when the peer Host has not enabled SCI.

    Initial Condition:
        The ACL is established and the Lower Tester publishes the SCI Controller
        bit with the SCI Host Support bit clear in feature page 1.

    Test Case Configuration:
        BI-135 selects the Peripheral IUT and BI-136 selects the Central IUT.

    Test Steps:
        1. Send LE Connection Rate Request with valid timing and subrate values.
        2. Check Command Status or the subsequent Connection Rate Change event.

    Expected Outcome:
        The request fails with Unsupported Remote Feature (0x1A).

    Reference:
        HCI.TS.p39, 4.10.26, Table 4.80; Core Vol 4, Part E, 7.8.154.
    """
    link = await _connect_for_rate(tester, central=central, peer_host=False)
    await _expect_rate_error(
        tester, _rate_request(link), ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
    )
    await link.disconnect()


async def invalid_rate_parameters(tester, *, default):
    """Validate the interval, subrate, latency, timeout and event length fields.

    Initial Condition:
        The ACL is established and both Hosts enable SCI.

    Test Case Configuration:
        BI-137 sends LE Connection Rate Request. BI-138 sends LE Set Default
        Rate Parameters.

    Test Steps:
        1. Send the command with each invalid parameter from Table 4.82.
        2. Read the controller's minimum supported interval. If it exceeds
           375 µs, request the exact interval one step below it.

    Expected Outcome:
        Malformed values fail with 0x12. A valid but wholly unsupported range
        fails with 0x20.

    Source Correction:
        Step 4 refers to itself and omits the subtraction sign. Step 3 supplies
        the lower limit, so use that limit minus 125 µs for both interval bounds.

    Reference:
        HCI.TS.p39, 4.10.27, Tables 4.81–82;
        Core Vol 4, Part E, 7.8.154–7.8.156.
    """
    link = await _connect_for_rate(tester)

    async def reject(status, **changes):
        if default:
            await tester.command(
                hci.HCI_LE_Set_Default_Rate_Parameters_Command(
                    **(RATE_PARAMETERS | changes)
                ),
                status,
            )
        else:
            await _expect_rate_error(tester, _rate_request(link, **changes), status)
        await link.exchange()

    for changes in (
        {'connection_interval_min': 0x7D01},
        {'connection_interval_max': 2},
        {'connection_interval_max': 0x7D01},
        {'connection_interval_min': 4, 'connection_interval_max': 3},
        {'subrate_min': 0},
        {'subrate_min': 0x01F5},
        {'subrate_max': 0},
        {'subrate_max': 0x01F5},
        {'subrate_min': 2, 'subrate_max': 1},
        {'max_latency': 0x01F4},
        {'continuation_number': 0x01F4},
        {'supervision_timeout': 9},
        {'supervision_timeout': 0x0C81},
        {'min_ce_length': 2, 'max_ce_length': 1},
        {'min_ce_length': 0x7D00},
        {'max_ce_length': 0x7D00},
    ):
        await reject(ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR, **changes)

    rates = await tester.command(
        hci.HCI_LE_Read_Minimum_Supported_Connection_Interval_Command()
    )
    minimum = rates.minimum_supported_connection_interval
    assert 3 <= minimum <= 10
    if minimum > 3:
        await reject(
            ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR,
            connection_interval_min=minimum - 1,
            connection_interval_max=minimum - 1,
        )
    await link.disconnect()


async def rate_request_during_legacy_procedure(tester, *, subrate):
    """Reject a Connection Rate Request during a conflicting legacy procedure.

    Initial Condition:
        The IUT is Peripheral and both Hosts enable SCI on a 10 ms ACL.

    Test Case Configuration:
        BI-139 starts the Connection Parameters Request procedure. BI-140
        starts the Connection Subrate Request procedure.

    Test Steps:
        1. Start the legacy procedure with Max_Latency = 0x0100.
        2. Observe its LL request and withhold the LL response.
        3. Send LE Connection Rate Request with Max_Latency = 1.

    Expected Outcome:
        The new command fails with Command Disallowed (0x0C).

    Reference:
        HCI.TS.p39, 4.10.27, BI-139-C and BI-140-C, pages 250–251;
        Core Vol 4, Part E, 7.8.154.
    """
    link = await _connect_for_rate(tester)
    latency = 0x0100
    if subrate:
        await tester.command(
            hci.HCI_LE_Subrate_Request_Command(link.handle, 1, 1, latency, 0, 600)
        )
        parameters = await link.control(Opcode.LL_SUBRATE_REQ)
    else:
        await tester.command(
            hci.HCI_LE_Connection_Update_Command(link.handle, 8, 8, latency, 600, 0, 0)
        )
        parameters = await link.control(Opcode.LL_CONNECTION_PARAM_REQ)
    assert struct.unpack_from('<H', parameters, 5)[0] == latency
    await _expect_rate_error(
        tester, _rate_request(link, max_latency=1), ErrorCode.COMMAND_DISALLOWED_ERROR
    )
    await link.disconnect()


async def rate_request_interval_too_short(tester, *, phy, dle):
    """Reject a proposed interval shorter than connIntervalRequired.

    Initial Condition:
        The IUT is Peripheral on a 10 ms ACL. Both Hosts enable SCI and
        the inter-frame and minimum connection event spacing are 150 µs.

    Test Case Configuration:
        BI-143 and BI-144 use LE 1M. BI-145 and BI-146 use LE Coded.
        BI-144 and BI-146 establish the maximum Data Length parameters.

    Test Steps:
        1. If selected, exchange LL_LENGTH_REQ/RSP with the maximum values
           for the PHY and observe changed effective limits.
        2. Request an interval equal to the greatest 125 µs multiple strictly
           below connIntervalRequired.

    Expected Outcome:
        The IUT rejects the command with 0x11. Without DLE, 0x20 also applies
        if the interval is below the IUT's globally supported minimum.

    Reference:
        HCI.TS.p39, 4.10.29, Table 4.84; Core Vol 4, Part E, 7.8.154 and
        Vol 6, Part B, 4.5.10.
    """
    link = await _connect_for_rate(tester, dle=dle)
    if phy != Phy.LE_1M:
        await change_phy(tester, link, phy, local=True)
    coded = phy == Phy.LE_CODED
    octets = PDU_DATA_PAYLOAD_SIZE_MIN
    time = PDU_DATA_PAYLOAD_TIME_CODED_MIN_US if coded else PDU_DATA_PAYLOAD_TIME_MIN_US
    if dle:
        maximum = PDU_DATA_PAYLOAD_SIZE_MAX
        max_time = (
            PDU_DATA_PAYLOAD_TIME_CODED_MAX_US
            if coded
            else PDU_DATA_PAYLOAD_TIME_UNCODED_MAX_US
        )
        request = struct.pack(
            '<BHHHH', Opcode.LL_LENGTH_REQ, maximum, max_time, maximum, max_time
        )
        received = await link.control(Opcode.LL_LENGTH_RSP, request)
        rx_octets, rx_time, _, _ = struct.unpack('<HHHH', received[1:])
        octets, time = min(maximum, rx_octets), min(max_time, rx_time)
        event = await tester.expect(
            lambda event: isinstance(event, hci.HCI_LE_Data_Length_Change_Event)
            and (event.connection_handle, event.max_rx_octets, event.max_rx_time)
            == (link.handle, octets, time)
        )
        assert (event.connection_handle, event.max_rx_octets, event.max_rx_time) == (
            link.handle,
            octets,
            time,
        )

    required = 2 * LL_IFS_US + min(
        time, octets * PDU_DATA_OCTET_TIME_CODED_S8_US + PDU_DATA_EMPTY_TIME_CODED_S8_US
    )
    interval = (required - 1) // 125
    reason = ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR
    await tester.command(
        _rate_request(
            link, connection_interval_min=interval, connection_interval_max=interval
        ),
        reason
        if dle
        else (reason, ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR),
    )
    await link.disconnect()


async def legacy_update_during_rate_request(tester):
    """Reject LE Connection Update while a Connection Rate Request is pending.

    Initial Condition:
        The IUT is Peripheral and both Hosts enable SCI on an established ACL.

    Test Steps:
        1. Send LE Connection Rate Request with Max_Latency = 0x0100 and
           observe the LL_CONNECTION_RATE_REQ.
        2. Withhold LL_CONNECTION_RATE_IND and send LE Connection Update.
        3. Send a valid LL_CONNECTION_RATE_IND and run to its Instant.

    Expected Outcome:
        LE Connection Update returns 0x0C. The original rate request completes
        with a successful Connection Rate Change event.

    Source Correction:
        The title names a Connection Parameters Request. Steps 1–3 instead
        start the Connection Rate Request, as required by Core 7.8.18.

    Reference:
        HCI.TS.p39, BI-147-C, page 255; Core Vol 4, Part E, 7.8.18 and
        Vol 6, Part B, 5.1.32–5.1.33.
    """
    link = await _connect_for_rate(tester)
    await tester.command(_rate_request(link, max_latency=0x0100))
    request = await link.control(Opcode.LL_CONNECTION_RATE_REQ)
    assert struct.unpack_from('<H', request, 9)[0] == 0x0100
    await tester.command(
        hci.HCI_LE_Connection_Update_Command(link.handle, 8, 8, 0x0101, 600, 0, 0),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )
    instant = (link.counter + 7) & LL_EVENT_COUNTER_MASK
    indication = struct.pack(
        '<B7H', Opcode.LL_CONNECTION_RATE_IND, 0, 80, instant, 1, 0, 0, 600
    )
    await link.exchange(indication, llid=LlId.CONTROL)
    while link.counter != (instant + 1) & LL_EVENT_COUNTER_MASK:
        await link.exchange()
    result = await tester.event(RATE_CHANGE)
    assert (result.connection_handle, result.status, result.connection_interval) == (
        link.handle,
        ErrorCode.SUCCESS,
        80,
    )
    await link.disconnect()


# Verify local and peer SCI support gating in both connection roles.
PROCEDURES |= procedure_cases(
    rate_request_requires_peer_host_support,
    {
        f'HCI/CCO/BI-{number}-C': (
            f'Reject LE Connection Rate Request without peer SCI Host Support, {role}',
            {'central': role == 'Central'},
        )
        for number, role in ((135, 'Peripheral'), (136, 'Central'))
    },
)

# Apply the invalid parameter matrix to the active and default rate commands.
PROCEDURES |= procedure_cases(
    invalid_rate_parameters,
    {
        'HCI/CCO/BI-137-C': (
            'LE Connection Rate Request, Invalid Parameters',
            {'default': False},
        ),
        'HCI/CCO/BI-138-C': (
            'LE Set Default Rate Parameters, Invalid Parameters',
            {'default': True},
        ),
    },
)

# Keep the legacy request pending while the Host submits the SCI command.
PROCEDURES |= procedure_cases(
    rate_request_during_legacy_procedure,
    {
        'HCI/CCO/BI-139-C': (
            'Reject LE Connection Rate Request during Connection Parameters Request',
            {'subrate': False},
        ),
        'HCI/CCO/BI-140-C': (
            'Reject LE Connection Rate Request during Connection Subrate Request',
            {'subrate': True},
        ),
    },
)

# Check the active PHY packet budget with and without Data Length Update.
PROCEDURES |= procedure_cases(
    rate_request_interval_too_short,
    {
        f'HCI/CCO/BI-{number}-C': (
            f'Reject LE Connection Rate Request below the packet budget, {phy.name}',
            {'phy': phy, 'dle': dle},
        )
        for number, phy, dle in (
            (143, Phy.LE_1M, False),
            (144, Phy.LE_1M, True),
            (145, Phy.LE_CODED, False),
            (146, Phy.LE_CODED, True),
        )
    },
)

# Reject the legacy update and allow the pending rate request to finish.
PROCEDURES |= procedure_cases(
    legacy_update_during_rate_request,
    {
        'HCI/CCO/BI-147-C': (
            'Reject LE Connection Update during Connection Rate Request',
            {},
        )
    },
)


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
