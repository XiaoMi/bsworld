# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""HCI.TS.p39, Section 4.6: Controller Information."""

import pytest

from bscontroller import hci
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.hci.capabilities import COMMAND_BITS
from tests.qualification.peer import establish
from tests.qualification.tester import LOWER_PERIPHERAL_PUBLIC_ADDRESS

ErrorCode = hci.HCI_ErrorCode
RADIO_CASES = {'HCI/CIN/BV-14-C'}

EXPECTED_LOCAL_VERSION = (
    hci.SpecificationVersion.BLUETOOTH_CORE_6_1,
    0,
    hci.SpecificationVersion.BLUETOOTH_CORE_6_1,
    0xFFFF,
    0,
)


# Core Vol 6, Part B, 4.6, Table 4.9. Host-controlled bits are zero at reset.
EXPECTED_LE_FEATURES = bytes.fromhex('ff 79 01 ff 3e 1d 00 80')
EXPECTED_LE_FEATURES_PAGE_1 = bytes.fromhex('03 05') + bytes(22)


async def read_local_supported_commands(tester):
    """Read Local Supported Commands command.

    Initial Condition:
        No LL connection exists. The ICS declares this LE Controller's
        supported commands independently of the returned bitmap or backend.

    Test Steps:
        1. Send Read Local Supported Commands [v1], as selected by Table 4.10.
        2. Receive successful Command Complete with the matching opcode,
           one command credit and Supported_Commands.

    Expected Outcome:
        Pass verdict:
            All 64 returned octets exactly match the independently declared
            command set mapped through the standard Supported Commands bit table.
            Unsupported and undefined command bits are zero.

    Reference:
        HCI.TS.p39.pdf, Section 4.6.1, HCI/CIN/BV-03-C, Table 4.10 and Figure 4.17;
        PDF/printed pages 49-50; source [1] 7.4.2 and [2] LE Command Tables.
    """
    result = await tester.command(hci.HCI_Read_Local_Supported_Commands_Command())
    expected = bytearray(64)
    for octet, bit in COMMAND_BITS.values():
        expected[octet] |= 1 << bit
    assert result.supported_commands == expected


async def read_local_version_information(tester):
    """Read Local Version Information Command.

    Initial Condition:
        No LL connection exists. EXPECTED_LOCAL_VERSION declares HCI 6.1,
        revision zero, LMP 6.1, manufacturer 0xFFFF and subversion zero.

    Test Steps:
        The source procedure is the unnumbered MSC in Figure 4.18:
        - Send Read Local Version Information.
        - Receive successful Command Complete with the matching opcode and
          one command credit. Read all five version/manufacturer fields.

    Expected Outcome:
        Pass verdict:
            HCI Version and LMP Version use the declared Assigned Numbers values.
            HCI Revision, Manufacturer Name and LMP Subversion exactly match
            the independent declaration.

    Reference:
        HCI.TS.p39.pdf, standalone HCI/CIN/BV-04-C, Figure 4.18;
        PDF/printed pages 50-51; source [1] HCI 7.4.1.
    """
    result = await tester.command(hci.HCI_Read_Local_Version_Information_Command())
    assert (
        result.hci_version,
        result.hci_subversion,
        result.lmp_version,
        result.company_identifier,
        result.lmp_subversion,
    ) == EXPECTED_LOCAL_VERSION


async def read_public_key_validation_feature(tester):
    """Read the Remote Public Key Validation feature bit.

    Initial Condition:
        The IUT has no connection to the Lower Tester.

    Test Steps:
        Send LE Read Local Supported Features Page 0 and receive the
        successful Command Complete event.

    Expected Outcome:
        Bit 27 of the returned FeatureSet is set.

    Reference:
        HCI.TS.p39, HCI/CIN/BV-09-C, 4.6.1, Figure 4.21.
        Core Vol 4, Part E, 7.8.3 and Vol 6, Part B, 4.6.26.
    """
    result = await tester.command(hci.HCI_LE_Read_Local_Supported_Features_Command())
    assert int.from_bytes(result.le_features, 'little') & (1 << 27)


async def read_local_le_features(tester):
    """LE Read Local Supported Features Page 0 Command.

    Initial Condition:
        The IUT has no Lower Tester connection. EXPECTED_LE_FEATURES declares
        the selected DUT capabilities independently of the Controller's code.

    Test Steps:
        The source uses the unnumbered MSC in Figure 4.22:
        - The Upper Tester sends LE Read Local Supported Features Page 0.
        - The IUT returns Command Complete with Status = Success (0x00),
          the corresponding opcode, a command credit and LE_Features.
        Compare the feature field against EXPECTED_LE_FEATURES, the independent
        declaration of this DUT's supported LL capabilities.

    Expected Outcome:
        Pass verdict: the Features field contains exactly the supported features
        mapped from LL ICS by the source's referenced Table 2.1.

    Reference:
        HCI.TS.p39.pdf, Section 4.6, independently titled procedure
        HCI/CIN/BV-12-C; PDF/printed page 56, Figure 4.22.
        Source reference [1], Section 7.8.3; [20], Table 2.1 (LL ICS mapping).
    """
    result = await tester.command(hci.HCI_LE_Read_Local_Supported_Features_Command())
    assert result.le_features == EXPECTED_LE_FEATURES


async def accept_list_capacity(tester):
    """Filter Accept List Size.

    Initial Condition:
        The IUT is not connected to the Lower Tester.

    Test Steps:
        The source describes an unnumbered procedure:
        - The Upper Tester sends LE Clear Filter Accept List.
        - Read Filter Accept List Size; require successful Command Complete
          and a size of at least one.
        - Add distinct addresses until the reported number of entries is filled;
          require successful Command Complete for each addition.
        - Add one more address and require Command Complete with Status =
          Memory Capacity Exceeded (0x07).
        - Remove one inserted address and require successful Command Complete.
        - Add another address and require successful Command Complete.
        This implementation uses distinct public addresses for all additions.

    Expected Outcome:
        Pass verdict: the reported capacity is at least one; all commands return
        matching Command Complete events with one command credit. Insertions
        succeed while space exists, fail with 0x07 when full, and succeed again
        after a successful removal.

    Reference:
        HCI.TS.p39.pdf, Section 4.6, independently titled procedure
        HCI/CIN/BV-06-C, Figure 4.19; PDF/printed pages 51-52.
        Source reference [8], Section 7.8.17.
    """
    await tester.command(hci.HCI_LE_Clear_Filter_Accept_List_Command())
    size = (
        await tester.command(hci.HCI_LE_Read_Filter_Accept_List_Size_Command())
    ).filter_accept_list_size
    assert size > 0
    for index in range(size + 1):
        address = hci.Address((index + 1).to_bytes(6, 'little'), 0)
        await tester.command(
            hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(0, address),
            (
                ErrorCode.SUCCESS
                if index < size
                else ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR
            ),
        )
    await tester.command(
        hci.HCI_LE_Remove_Device_From_Filter_Accept_List_Command(
            0, hci.Address(size.to_bytes(6, 'little'), 0)
        )
    )
    await tester.command(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(
            0, hci.Address((size + 2).to_bytes(6, 'little'), 0)
        )
    )


async def read_rssi(tester):
    """Read RSSI Value.

    Initial Condition:
        An ACL is established with the independent Lower Tester; either role is
        permitted. This implementation selects the Peripheral role and LE 1M.

    Test Steps:
        1. The Upper Tester sends Read RSSI with Handle equal to the current
           ACL's Connection_Handle.
        2. The IUT returns successful Command Complete with Handle equal to
           the value from step 1 and a valid RSSI.

    Observation Setup:
        Establish the ACL to satisfy the initial condition before step 1.
        Check the opcode, command credit and LE RSSI range as well as Status
        and Handle. Disconnect after step 2; teardown is not a source step.

    Expected Outcome:
        Pass verdict: status is Success, Handle equals the submitted connection
        handle and RSSI is within the LE measurement range -127 through +20 dBm,
        not the unavailable sentinel 127.

    Reference:
        HCI.TS.p39.pdf, Section 4.6.3, HCI/CIN/BV-14-C;
        PDF/printed pages 57-58, steps 1-2 and Figure 4.24.
        Source reference [12], Section 7.5.4; Table 4.13 selects LE PHY.
    """
    link = await establish(tester, peer_address=LOWER_PERIPHERAL_PUBLIC_ADDRESS)
    result = await tester.command(hci.HCI_Read_RSSI_Command(link.handle))
    assert result.handle == link.handle and -127 <= result.rssi <= 20
    await link.disconnect()


PROCEDURES = ProcedureMap()

# Verify the v1 Supported_Commands bitmap against the independently declared commands,
# including zero bits for unsupported or undefined commands.
PROCEDURES |= procedure_cases(
    read_local_supported_commands,
    {
        'HCI/CIN/BV-03-C': (
            'Read Local Supported Commands [v1] command',
            {},
        ),
    },
)

# Verify every returned version and manufacturer field against the declared IXIT values,
# rather than comparing two Controller self-reports.
PROCEDURES |= procedure_cases(
    read_local_version_information,
    {
        'HCI/CIN/BV-04-C': (
            'Read Local Version Information Command',
            {},
        ),
    },
)

# Verify that the reported Filter Accept List size equals the available address-list
# entries; check insertion, overflow and reuse after removal.
PROCEDURES |= procedure_cases(
    accept_list_capacity,
    {
        'HCI/CIN/BV-06-C': (
            'Filter Accept List Size',
            {},
        ),
    },
)

# The public key validation capability is reported in LE FeatureSet page 0.
PROCEDURES |= procedure_cases(
    read_public_key_validation_feature,
    {'HCI/CIN/BV-09-C': ('Read LE Public Key Validation Feature Bit', {})},
)

# Verify that the returned LE feature bits match the declared LL capabilities.
PROCEDURES |= procedure_cases(
    read_local_le_features,
    {
        'HCI/CIN/BV-12-C': (
            'LE Read Local Supported Features Page 0 Command',
            {},
        ),
    },
)

# Verify that Read RSSI returns a valid measurement for the selected ACL.
PROCEDURES |= procedure_cases(
    read_rssi,
    {
        'HCI/CIN/BV-14-C': (
            'Read RSSI Value, LE PHY',
            {},
        ),
    },
)


async def read_all_local_supported_features(tester):
    """LE Read All Local Supported Features Command.

    Initial Condition:
        The IUT has no LL connection. The declared page 0 features are those of
        EXPECTED_LE_FEATURES; page 1 declares Monitoring Advertisers (bit 64)
        and Frame Space Update (bit 65), plus Shorter Connection Intervals and
        LE Flushable ACL Data (bits 72 and 74).

    Test Steps:
        1. Send HCI LE Read All Local Supported Features.
        2. Require successful Command Complete, Max_Page = 1 and the complete
           248-octet LE_Features field matching the independent ICS declaration.

    Expected Outcome:
        Pass verdict: pages 0 and 1 match the declared features, and pages 2
        through 10 are zero.

    Source Correction:
        The source cites 7.8.129; this command is Core Vol 4, Part E, 7.8.128.

    Reference:
        HCI.TS.p39, Section 4.6, HCI/CIN/BV-15-C, PDF pages 56-57.
    """
    result = await tester.command(
        hci.HCI_LE_Read_All_Local_Supported_Features_Command()
    )
    assert result.max_page == 1
    assert (
        result.le_features
        == EXPECTED_LE_FEATURES + EXPECTED_LE_FEATURES_PAGE_1 + bytes(216)
    )


# Verify all local feature pages against the independent ICS declaration.
PROCEDURES |= procedure_cases(
    read_all_local_supported_features,
    {'HCI/CIN/BV-15-C': ('LE Read All Local Supported Features Command', {})},
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES, radio_cases=RADIO_CASES),
)
async def test_controller_information_commands(
    hci_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(hci_dut, identifier, description, procedure, parameters, marks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES, radio_cases=RADIO_CASES, radio=True),
)
async def test_controller_information_radio(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
