# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.3: Feature exchange and long LL Control PDUs."""

import random
import struct
from functools import partial

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    PDU_CONTROL_FEATURE_SET_SIZE,
    PDU_DATA_LLID_MASK,
    PDU_DATA_NESN_MASK,
    PDU_HEADER_SIZE,
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
from tests.qualification.connection import (
    LENGTH_FIELDS,
    MAXIMUM,
    MINIMUM,
    ErrorCode,
    Opcode,
    _check_feature_pdu,
    _check_parameters,
    _initiate_auxiliary,
)
from tests.qualification.peer import (
    LONG_CONTROL_FEATURES,
    LongControlPeer,
    establish,
    exchange_features,
)
from tests.qualification.periodic import PeriodicTrain
from tests.qualification.tester import (
    DUT_ADDRESS,
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
    PEER_ADDRESS,
)
from tests.qualification.transfer import (
    SERVICE_DATA,
    SyncIndication,
    TransferSession,
    lower_train,
    transferred_anchor,
)

PROCEDURES = ProcedureMap()


async def _feature_or_version(tester, *, central, version=False):
    """The peer initiates Feature/Version Exchange; check the LL response and finish
    the connection."""
    declared = None
    if not version:
        result = await tester.command(
            hci.HCI_LE_Read_Local_Supported_Features_Command()
        )
        declared = int.from_bytes(result.le_features, 'little')
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    if version:
        payload = bytes(
            [Opcode.LL_VERSION_IND, hci.SpecificationVersion.BLUETOOTH_CORE_5_4]
        ) + struct.pack('<HH', 4660, 0)
        response = await link.control(Opcode.LL_VERSION_IND, payload)
        assert len(response) == 6
    else:
        opcode = Opcode.LL_PERIPHERAL_FEATURE_REQ if central else Opcode.LL_FEATURE_REQ
        response = await link.control(
            Opcode.LL_FEATURE_RSP, bytes([opcode]) + bytes(PDU_CONTROL_FEATURE_SET_SIZE)
        )
        assert declared is not None
        _check_feature_pdu(declared, response)
    await link.disconnect(local=True)


async def _remote_features(tester, *, central, unsupported=False, all_features=False):
    """Verify the Host command, role-specific on-air request and exact completion
    features."""
    link = await establish(
        tester,
        central=central,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    declared = int.from_bytes(
        (
            await tester.command(hci.HCI_LE_Read_Local_Supported_Features_Command())
        ).le_features,
        'little',
    )
    await tester.command(
        hci.HCI_LE_Read_All_Remote_Features_Command(link.handle, 1)
        if all_features
        else hci.HCI_LE_Read_Remote_Features_Command(link.handle)
    )
    opcode = Opcode.LL_FEATURE_REQ if central else Opcode.LL_PERIPHERAL_FEATURE_REQ
    initial = link.last_received.pdu[PDU_HEADER_SIZE:]
    request = (
        initial
        if link.last_received.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
        and initial[:1] == bytes([opcode])
        else await link.control(opcode)
    )
    _check_feature_pdu(declared, request, opcode=opcode)
    features = int(
        hci.LeFeatureMask.CONNECTION_PARAMETERS_REQUEST_PROCEDURE
        | hci.LeFeatureMask.LE_2M_PHY
    )
    response = (
        bytes([Opcode.LL_UNKNOWN_RSP, opcode])
        if unsupported
        else bytes([Opcode.LL_FEATURE_RSP])
        + features.to_bytes(PDU_CONTROL_FEATURE_SET_SIZE, 'little')
    )
    sent_sn = link.sn
    packet = await link.exchange(response, llid=LlId.CONTROL, check_ack=True)
    if central:
        sent_sn = link.sn
        packet = await link.exchange()
    assert bool(packet.pdu[0] & PDU_DATA_NESN_MASK) != bool(sent_sn)
    event = await tester.event(
        hci.HCI_LE_Read_All_Remote_Features_Complete_Event
        if all_features
        else hci.HCI_LE_Read_Remote_Features_Complete_Event
    )
    assert event.connection_handle == link.handle
    assert event.status == (
        ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR if unsupported else ErrorCode.SUCCESS
    )
    if not unsupported:
        assert event.le_features == response[1:] + (bytes(240) if all_features else b'')
    await link.disconnect()


async def feature_setup_response(tester):
    """Feature Setup Response.

    Initial Condition:
        An unencrypted connected public Peripheral with selected interval,
        latency and timeout, and common channel parameters. The Lower Tester
        acts as Central. Read the IUT local LE features for the declared ICS
        comparison and select an all-zero Lower Tester FeatureSet.

    Test Steps:
        1. Send LL_FEATURE_REQ with the Lower Tester FeatureSet and wait for
           LL_FEATURE_RSP. Check opcode, full FeatureSet length, cleared
           Masked-to-Peer bits, required local features and zero RFU bits.
        2. Execute Peripheral Connection Terminated for the established handle
           and parameters; require successful HCI termination completion.

    Expected Outcome:
        Pass verdict:
            Every Masked-to-Peer feature is clear. The response contains all
            required supported features matching the declared ICS mapping,
            and its RFU bits are zero.

    Coverage Note:
        Apply Core 6.3 Send-to-Peer feature rules to the local declaration:
        optional peer-report bits need not be set, forbidden bits must be clear,
        and no undeclared feature may appear. Validate DATA timing and ACKs
        through the independent Central peer while awaiting the response.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-14-C procedure in
        Section 4.3.5, Figure 4.3-56; PDF 607-608, printed 4.3-134 through 4.3-135.
    """
    await _feature_or_version(tester, central=False, version=False)


# Verify that a connected Peripheral responds to Feature Exchange with the supported
# feature information and correct peer-report masking.
PROCEDURES |= procedure_cases(
    feature_setup_response,
    {
        'LL/CON/PER/BV-14-C': (
            'Feature Setup Response',
            {},
        ),
    },
)


async def initiate_feature_exchange(tester):
    """Initiate Feature Exchange.

    Initial Condition:
        An unencrypted connected Peripheral with supported own address type,
        common interval/channel parameters, and selected timeout. Select public
        addresses; the Lower Tester acts as Central and retains the ACL handle.

    Test Steps:
        1. Send LE Read Local Supported Features Page 0 and require successful
           Command Complete with the local feature bitmap for ICS comparison.
        2. Send LE Read Remote Features Page 0 for the connection handle and
           require successful Command Status.
        3. If the IUT already sent LL_PERIPHERAL_FEATURE_REQ autonomously, use
           that request; otherwise receive its request after Step 2. Check opcode,
           FeatureSet length, supported required bits, Send-to-Peer masking and
           zero RFU bits. Send LL_FEATURE_RSP with a selected valid Central
           FeatureSet and require the IUT to acknowledge the response.
        4. Require successful LE Read Remote Features Page 0 Complete for the
           same handle with the exact feature bytes from LL_FEATURE_RSP.

    Expected Outcome:
        Pass verdict:
            The IUT requests Feature Exchange, acknowledges the reply and reports
            completion. Local HCI features match its declared support; the request
            contains supported peer-visible features with forbidden/RFU bits
            clear, and the HCI peer feature result matches the received response.

    Coverage Note:
        Core 6.3 Vol 6, Part B, Sections 4.6 and 5.1.4 distinguish Send-to-Peer
        masking from FeatureSet_USED in responses. The initial request retains
        required locally supported first-octet bits; it is neither all zero nor
        necessarily equal to the raw HCI local bitmap. Optional peer-report bits
        may be clear. Amber corroborated these request/response rules.
        Disconnect through HCI after the normative four-step procedure.

    Reference:
        LL.TS.p28, independently titled LL/CON/PER/BV-22-C procedure in
        Section 4.3.5, Figure 4.3-64; PDF 616-618, printed 4.3-143 through 4.3-145.
    """
    await _remote_features(tester, central=False, unsupported=False)


# Verify Peripheral-initiated Feature Exchange, correct peer-visible supported features,
# acknowledgement, and reporting of the peer features.
PROCEDURES |= procedure_cases(
    initiate_feature_exchange,
    {
        'LL/CON/PER/BV-22-C': (
            'Initiate Feature Exchange',
            {},
        ),
    },
)


async def initiate_feature_exchange_central_does_not_support(
    tester, *, all_features=False
):
    """Initiate Feature Exchange – Central does not support.

    Initial Condition:
        Connected, unencrypted Peripheral; public addresses, common connection
        parameters. The Lower Central must not initiate Feature Exchange.

    Test Case Configuration:
        LL/CON/PER/BV-23-C: LE Read Remote Features (page 0).
        LL/CON/PER/BV-165-C: LE Read All Remote Features, Pages_Requested = 1.

    Test Steps:
        1. Send the selected Host feature command for the established handle;
           require successful Command Status.
        2. Receive LL_PERIPHERAL_FEATURE_REQ and validate its page 0 features
           against the independent local declaration and Send-to-Peer rules.
        3. Send and acknowledge LL_UNKNOWN_RSP identifying that request opcode.
        4. Require the selected feature-completion event, matching handle and
           Unsupported Remote Feature (0x1A), then terminate the connection.

    Expected Outcome:
        Pass verdict: the selected completion reports 0x1A without a Central
        feature request.

    Reference:
        LL.TS.p28, Section 4.3.5.1, Table 4.3-39, Figure 4.3-65;
        printed pages 4.3-145 through 4.3-146.
    """
    await _remote_features(
        tester, central=False, unsupported=True, all_features=all_features
    )


# Verify Peripheral-initiated Feature Exchange when the Central does not support the
# procedure and answers with LL_UNKNOWN_RSP.
PROCEDURES |= procedure_cases(
    initiate_feature_exchange_central_does_not_support,
    {
        'LL/CON/PER/BV-23-C': (
            'Initiate Feature Exchange – Central does not support',
            {},
        ),
        'LL/CON/PER/BV-165-C': (
            'Initiate Feature Exchange – Central does not support',
            {'all_features': True},
        ),
    },
)


async def feature_setup_request(tester):
    """Feature Setup Request.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and selected interval, latency, timeout and
        common channel map. Retain the established connection handle.

    Test Steps:
        1. Send HCI LE Read Local Supported Features Page 0 and require
           successful Command Complete containing the local feature set.
        2. Send HCI LE Read Remote Features Page 0 for the established handle
           and require successful Command Status.
        3. Require LL_FEATURE_REQ with the configured feature set. Accept
           an autonomous request sent before Step 2, provided it is still
           waiting for the Lower Tester's LL_FEATURE_RSP. Send that response
           with the selected valid peer feature set and require its ACK.
        4. Require successful HCI LE Read Remote Features Page 0 Complete
           for the same handle, with features matching LL_FEATURE_RSP.

    Expected Outcome:
        Pass verdict:
            The LL request contains the declared supported features under
            the Send-to-Peer rules; Masked-to-Peer and RFU bits are clear.
            The HCI local feature set matches the declared capabilities.
            The Central requests and acknowledges Feature Exchange, and its
            HCI remote-feature completion matches the Lower Tester's response.

    Coverage Note:
        The Lower Tester offers Connection Parameters Request and LE 2M PHY.
        Check the complete eight-octet feature field and HCI handle/status,
        then disconnect after the four numbered source steps.
        The pre-5.0 name "Read Remote Used Features" identifies the same
        HCI operation, not a different test procedure.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-13-C procedure in
        Section 4.3.6, Figure 4.3-142; PDF 761-763, printed 4.3-288 through
        4.3-290. Steps 1-4 and Expected Outcome; Core Vol 6, Part B, 4.6/5.1.4.
    """
    await _remote_features(tester, central=True, unsupported=False)


# Verify that a connected Central initiates Feature Exchange with its declared feature
# set and reports the Peripheral response to its Host.
PROCEDURES |= procedure_cases(
    feature_setup_request,
    {
        'LL/CON/CEN/BV-13-C': (
            'Feature Setup Request',
            {},
        ),
    },
)


async def responding_to_feature_exchange(tester):
    """Responding to Feature Exchange.

    Initial Condition:
        An unencrypted connected Central with a public Lower Tester Peripheral,
        supported own address type and common interval, latency, timeout and
        channel map. Read the declared local features for response validation.

    Test Steps:
        1. Send LL_PERIPHERAL_FEATURE_REQ with the Lower Tester's selected
           feature set and wait for LL_FEATURE_RSP.
        2. Require the Central to respond with LL_FEATURE_RSP.

    Expected Outcome:
        Pass verdict:
            The Central sends the feature response. All feature bits marked
            Masked to Peer are clear.

    Coverage Note:
        Select a zero peer FeatureSet; the response's FeatureSet_USED first
        octet must therefore be zero. Check the fixed eight-octet field,
        required local features and optional Send-to-Peer reporting rules,
        and reject forbidden, undeclared or RFU bits. Disconnect after the
        two numbered source steps.

    Reference:
        LL.TS.p28, independently titled LL/CON/CEN/BV-23-C procedure in
        Section 4.3.6, Figure 4.3-152; PDF 775, printed 4.3-302.
        Steps 1-2 and Expected Outcome; referenced Core procedure 5.1.4.2.
    """
    await _feature_or_version(tester, central=True, version=False)


# Verify that a connected Central responds to Peripheral-initiated Feature Exchange
# using the correct peer-visible feature set.
PROCEDURES |= procedure_cases(
    responding_to_feature_exchange,
    {
        'LL/CON/CEN/BV-23-C': (
            'Responding to Feature Exchange',
            {},
        ),
    },
)


# Table 4.3-1: WR_IND is the longest supported control PDU (42 + 1 octets).
LONG_CONTROL_PAYLOAD_SIZE = 43
LONG_CONTROL_EVENTS = 10


async def _long_control_link(
    tester,
    *,
    central,
    phy,
    reject=False,
    features=LONG_CONTROL_FEATURES,
    feature_delay_us=0,
    lengths=MINIMUM,
):
    """Connect on the target PHY without an intervening Feature Exchange."""
    peer_factory = partial(
        LongControlPeer,
        features=features,
        lengths=lengths,
        reject=reject,
        feature_delay_us=feature_delay_us,
    )
    if central and phy != Phy.LE_1M:
        link = await _initiate_auxiliary(
            tester, phy=phy, retry=False, timeout=200, peer_factory=peer_factory
        )
    else:
        link = await establish(
            tester,
            central=central,
            extended=True,
            phy=phy,
            timeout=200,
            peer_address=(
                LOWER_PERIPHERAL_PUBLIC_ADDRESS
                if central
                else LOWER_CENTRAL_PUBLIC_ADDRESS
            ),
            peer_factory=peer_factory,
        )
    assert link.tx_phy == link.rx_phy == phy
    if central:
        assert link.parameters.timeout >= 200
    return link


async def _long_control_data_length(tester, link):
    """Complete maximum DLE without performing Feature Exchange."""
    link.lengths = MAXIMUM
    payload = await link.control(
        Opcode.LL_LENGTH_RSP,
        bytes([Opcode.LL_LENGTH_REQ]) + LENGTH_FIELDS.pack(*MAXIMUM),
    )
    dut = _check_parameters(payload, Opcode.LL_LENGTH_RSP, MAXIMUM)
    assert dut[0] >= LONG_CONTROL_PAYLOAD_SIZE
    await link.exchange()
    changed = await tester.event(hci.HCI_LE_Data_Length_Change_Event)
    assert changed.connection_handle == link.handle
    assert changed.max_rx_octets == min(dut[0], MAXIMUM[2])
    assert changed.max_tx_octets == min(dut[2], MAXIMUM[0])
    assert changed.max_rx_octets >= LONG_CONTROL_PAYLOAD_SIZE
    assert changed.max_tx_octets >= LONG_CONTROL_PAYLOAD_SIZE
    tester.output.write(f'LONG CONTROL DLE peer={MAXIMUM} effective={changed}\n')


async def acknowledging_long_control_pdus(tester, *, central, phy):
    """Acknowledging Long Control PDUs.

    Initial Condition:
        No connections or advertising; the selected role and connection PHY.
        The declared longest supported received control is WR_IND, with
        42-octet CtrData. Initial ACL data limits are 27 octets.

    Test Case Configuration:
        Table 4.3-85 selects Central or Peripheral and LE 1M, LE 2M or LE Coded.

    Test Steps:
        1. Establish directly on the selected PHY without a PHY Update.
        2. Answer any autonomous Data Length Update with the minimum limits.
        3. Send an independently encoded 43-octet LL_PERIODIC_SYNC_WR_IND.
        4. Require its sequence-number acknowledgement and maintain at least
           ten more connection events with valid packets on the selected PHY.
        5. Terminate the connection.

    Expected Outcome:
        The IUT acknowledges the longest supported control PDU despite the
        27-octet data limit, and the connection remains active.

    Source Correction:
        The source Expected Outcome refers to transferring synchronization
        information, although its purpose and Steps 3-4 require receiving and
        acknowledging it. Follow those explicit purpose and procedure steps.
        Transfer reception remains disabled; no new periodic sync is required.

    Reference:
        LL.TS.p28, 4.3.7.11, Table 4.3-85, Steps 1-5;
        LL/CON/CEN/BV-105-C..107-C and PER/BV-109-C..111-C.
        Core Vol 6, Part B, 4.5.11 and 2.4.2.40.
    """
    link = await _long_control_link(tester, central=central, phy=phy)
    session = TransferSession(tester, link, lower_train(link, pawr=True), mode='off')
    payload = session.indication(address=PEER_ADDRESS)
    assert len(payload) == LONG_CONTROL_PAYLOAD_SIZE and payload[0] == 0x2A
    await session.send_control(payload)
    await session.run(LONG_CONTROL_EVENTS)
    assert not session.transfers()
    await link.disconnect(local=True)


# Verify acknowledging long control pdus on each supported connection PHY.
PROCEDURES |= procedure_cases(
    acknowledging_long_control_pdus,
    {
        'LL/CON/CEN/BV-105-C': (
            'Acknowledging Long Control PDUs',
            {'central': True, 'phy': Phy.LE_1M},
        ),
        'LL/CON/CEN/BV-106-C': (
            'Acknowledging Long Control PDUs',
            {'central': True, 'phy': Phy.LE_2M},
        ),
        'LL/CON/CEN/BV-107-C': (
            'Acknowledging Long Control PDUs',
            {'central': True, 'phy': Phy.LE_CODED},
        ),
        'LL/CON/PER/BV-109-C': (
            'Acknowledging Long Control PDUs',
            {'central': False, 'phy': Phy.LE_1M},
        ),
        'LL/CON/PER/BV-110-C': (
            'Acknowledging Long Control PDUs',
            {'central': False, 'phy': Phy.LE_2M},
        ),
        'LL/CON/PER/BV-111-C': (
            'Acknowledging Long Control PDUs',
            {'central': False, 'phy': Phy.LE_CODED},
        ),
    },
)


async def _reject_long_control(tester, *, central, phy, features, data_length):
    if data_length or features:
        await tester.command(
            hci.HCI_LE_Write_Suggested_Default_Data_Length_Command(251, 17040)
        )
    unsupported = LONG_CONTROL_FEATURES & ~(
        hci.LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_RECIPIENT
        | hci.LeFeatureMask.PERIODIC_ADVERTISING_WITH_RESPONSES_SCANNER
    )
    link = await _long_control_link(
        tester,
        central=central,
        phy=phy,
        reject=True,
        features=unsupported if features else None,
        feature_delay_us=1_000_000 if not features and not data_length else 0,
        lengths=MAXIMUM if data_length else MINIMUM,
    )
    if features:
        await exchange_features(link, unsupported)
    if data_length:
        await _long_control_data_length(tester, link)
    await pawr_peer.configure(tester, interval=96, timing=(2, 6, 1, 10), slots=1)
    session = TransferSession(tester, link, mode='off')
    # Transmit real subevents first, so rejection cannot be caused by a source
    # which has never advertised. Observe every intervening ACL control PDU.
    await session.run_for(3 * 96 * 1250)
    for maximum in (False, True) if features else (False,):
        if maximum:
            await _long_control_data_length(tester, link)
        await session.command(
            hci.HCI_LE_Periodic_Advertising_Set_Info_Transfer_Command(
                link.handle, SERVICE_DATA, 0
            ),
            ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR,
        )
        await session.run(LONG_CONTROL_EVENTS)
    await pawr_peer.stop_advertising(tester)
    await link.disconnect(local=True)


async def rejecting_long_control_before_feature_exchange(tester, *, central, phy):
    """Rejecting Request to Send Long Control PDUs before Feature Exchange and DLU.

    Initial Condition:
        No connections; a selected target PHY, initial 27-octet data limits and
        no completed Feature Exchange. Central supervision timeout is 2 s.

    Test Case Configuration:
        LL/CON/CEN/BV-108-C: Central IUT, LE 1M.
        LL/CON/CEN/BV-109-C: Central IUT, LE 2M.
        LL/CON/CEN/BV-110-C: Central IUT, LE Coded.
        LL/CON/PER/BV-112-C: Peripheral IUT, LE 1M.
        LL/CON/PER/BV-113-C: Peripheral IUT, LE 2M.
        LL/CON/PER/BV-114-C: Peripheral IUT, LE Coded.

    Test Steps:
        1. Establish directly on the selected PHY, without Feature Exchange.
        2. Reject autonomous Peripheral Feature Exchange; detect an autonomous
           Central exchange, wait one second and complete it before ending
           Inconclusive, as required by source alternative 2A.
        3. Answer any autonomous Data Length Update with minimum values.
        4. Enable a PAwR advertiser and command Set Info Transfer on the ACL.
        5. Require a nonzero command status and observe ten connection events.
        6. Disable advertising and terminate the connection.

    Expected Outcome:
        Set Info Transfer is rejected. No control with CtrData longer than
        26 octets is sent before, during or after the command.

    Reference:
        LL.TS.p28, 4.3.7.12, Table 4.3-86, Steps 1-6;
        LL/CON/CEN/BV-108-C..110-C and PER/BV-112-C..114-C.
        Core Vol 6, Part B, 4.5.11.
    """
    await _reject_long_control(
        tester, central=central, phy=phy, features=False, data_length=False
    )


# Verify rejection before Feature Exchange on each supported connection PHY.
PROCEDURES |= procedure_cases(
    rejecting_long_control_before_feature_exchange,
    {
        'LL/CON/CEN/BV-108-C': (
            'Rejecting Long Control PDUs before Feature Exchange',
            {'central': True, 'phy': Phy.LE_1M},
        ),
        'LL/CON/CEN/BV-109-C': (
            'Rejecting Long Control PDUs before Feature Exchange',
            {'central': True, 'phy': Phy.LE_2M},
        ),
        'LL/CON/CEN/BV-110-C': (
            'Rejecting Long Control PDUs before Feature Exchange',
            {'central': True, 'phy': Phy.LE_CODED},
        ),
        'LL/CON/PER/BV-112-C': (
            'Rejecting Long Control PDUs before Feature Exchange',
            {'central': False, 'phy': Phy.LE_1M},
        ),
        'LL/CON/PER/BV-113-C': (
            'Rejecting Long Control PDUs before Feature Exchange',
            {'central': False, 'phy': Phy.LE_2M},
        ),
        'LL/CON/PER/BV-114-C': (
            'Rejecting Long Control PDUs before Feature Exchange',
            {'central': False, 'phy': Phy.LE_CODED},
        ),
    },
)


async def rejecting_long_control_after_data_length_update(tester, *, central, phy):
    """Rejecting Request to Send Long Control PDUs before Feature Exchange after DLU.

    Initial Condition:
        No connections; the selected role/PHY and no completed Feature Exchange.

    Test Case Configuration:
        LL/CON/CEN/BV-161-C: Central IUT, LE 1M.
        LL/CON/CEN/BV-162-C: Central IUT, LE 2M.
        LL/CON/CEN/BV-163-C: Central IUT, LE Coded.
        LL/CON/PER/BV-162-C: Peripheral IUT, LE 1M.
        LL/CON/PER/BV-163-C: Peripheral IUT, LE 2M.
        LL/CON/PER/BV-164-C: Peripheral IUT, LE Coded.

    Test Steps:
        1. Establish directly on the selected PHY without Feature Exchange.
        2. Reject autonomous Peripheral Feature Exchange; detect an autonomous
           Central exchange, complete it and end Inconclusive.
        3. Initiate Data Length Update with 251 octets and maximum packet time;
           check the response and the effective Data Length Change event.
        4. Command Set Info Transfer from a running PAwR advertising set.
        5. Require a nonzero command status and observe ten connection events.
        6. Disable advertising and terminate the connection.

    Expected Outcome:
        Data Length Update completes and permits reception of a 43-octet data
        payload by the Lower Tester. Set Info Transfer is still rejected;
        no control with CtrData longer than 26 octets is transmitted.

    Reference:
        LL.TS.p28, 4.3.7.13, Table 4.3-87, Steps 1-6;
        LL/CON/CEN/BV-161-C..163-C and PER/BV-162-C..164-C.
        Core Vol 6, Part B, 4.5.11 and 5.1.9.
    """
    await _reject_long_control(
        tester, central=central, phy=phy, features=False, data_length=True
    )


# Verify that maximum DLU does not authorize long controls before Feature Exchange.
PROCEDURES |= procedure_cases(
    rejecting_long_control_after_data_length_update,
    {
        'LL/CON/CEN/BV-161-C': (
            'Rejecting Long Control PDUs before Feature Exchange after DLU',
            {'central': True, 'phy': Phy.LE_1M},
        ),
        'LL/CON/CEN/BV-162-C': (
            'Rejecting Long Control PDUs before Feature Exchange after DLU',
            {'central': True, 'phy': Phy.LE_2M},
        ),
        'LL/CON/CEN/BV-163-C': (
            'Rejecting Long Control PDUs before Feature Exchange after DLU',
            {'central': True, 'phy': Phy.LE_CODED},
        ),
        'LL/CON/PER/BV-162-C': (
            'Rejecting Long Control PDUs before Feature Exchange after DLU',
            {'central': False, 'phy': Phy.LE_1M},
        ),
        'LL/CON/PER/BV-163-C': (
            'Rejecting Long Control PDUs before Feature Exchange after DLU',
            {'central': False, 'phy': Phy.LE_2M},
        ),
        'LL/CON/PER/BV-164-C': (
            'Rejecting Long Control PDUs before Feature Exchange after DLU',
            {'central': False, 'phy': Phy.LE_CODED},
        ),
    },
)


async def rejecting_long_control_after_feature_exchange(tester, *, central, phy):
    """Rejecting Request to Send Long Control PDUs after Feature Exchange.

    Initial Condition:
        No connections; the selected role and connection PHY.

    Test Case Configuration:
        LL/CON/CEN/BV-111-C: Central IUT, LE 1M.
        LL/CON/CEN/BV-112-C: Central IUT, LE 2M.
        LL/CON/CEN/BV-113-C: Central IUT, LE Coded.
        LL/CON/PER/BV-115-C: Peripheral IUT, LE 1M.
        LL/CON/PER/BV-116-C: Peripheral IUT, LE 2M.
        LL/CON/PER/BV-117-C: Peripheral IUT, LE Coded.

    Test Steps:
        1. Establish directly on the selected PHY.
        2. Complete Feature Exchange without PAST Recipient or PAwR Scanner
           support, retaining only features that use short control PDUs.
        3. Keep minimum data limits; answer autonomous DLU with 27 octets.
        4-5. Command Set Info Transfer from a running PAwR set and require
             rejection; verify that ten further events contain no long control.
        6-9. Complete maximum DLU and repeat the command, rejection and
             observation despite the expanded data-length limits.
        10. Disable advertising and terminate the connection.

    Expected Outcome:
        Set Info Transfer returns nonzero status in both data-length states.
        Feature Exchange and DLU do not authorize a long control procedure
        whose required features the Lower Tester did not advertise.

    Reference:
        LL.TS.p28, 4.3.7.14, Table 4.3-88, Steps 1-10;
        LL/CON/CEN/BV-111-C..113-C and PER/BV-115-C..117-C.
        Core Vol 6, Part B, 4.5.11.
    """
    await _reject_long_control(
        tester, central=central, phy=phy, features=True, data_length=False
    )


# Verify rejecting long control after feature exchange on each supported connection PHY.
PROCEDURES |= procedure_cases(
    rejecting_long_control_after_feature_exchange,
    {
        'LL/CON/CEN/BV-111-C': (
            'Rejecting Long Control PDUs after Feature Exchange',
            {'central': True, 'phy': Phy.LE_1M},
        ),
        'LL/CON/CEN/BV-112-C': (
            'Rejecting Long Control PDUs after Feature Exchange',
            {'central': True, 'phy': Phy.LE_2M},
        ),
        'LL/CON/CEN/BV-113-C': (
            'Rejecting Long Control PDUs after Feature Exchange',
            {'central': True, 'phy': Phy.LE_CODED},
        ),
        'LL/CON/PER/BV-115-C': (
            'Rejecting Long Control PDUs after Feature Exchange',
            {'central': False, 'phy': Phy.LE_1M},
        ),
        'LL/CON/PER/BV-116-C': (
            'Rejecting Long Control PDUs after Feature Exchange',
            {'central': False, 'phy': Phy.LE_2M},
        ),
        'LL/CON/PER/BV-117-C': (
            'Rejecting Long Control PDUs after Feature Exchange',
            {'central': False, 'phy': Phy.LE_CODED},
        ),
    },
)


async def sending_long_control_after_feature_exchange(tester, *, central, phy):
    """Sending Long Control PDUs after Feature Exchange.

    Initial Condition:
        No connections; the selected role/PHY and initial 27-octet data limits.
        WR_IND is the longest supported transmitted LL Control PDU.

    Test Case Configuration:
        LL/CON/CEN/BV-114-C: Central IUT, LE 1M.
        LL/CON/CEN/BV-115-C: Central IUT, LE 2M.
        LL/CON/CEN/BV-116-C: Central IUT, LE Coded.
        LL/CON/PER/BV-118-C: Peripheral IUT, LE 1M.
        LL/CON/PER/BV-119-C: Peripheral IUT, LE 2M.
        LL/CON/PER/BV-120-C: Peripheral IUT, LE Coded.

    Test Steps:
        1. Establish directly on the selected PHY.
        2. Complete Feature Exchange with PAST Recipient and PAwR Scanner.
        3. Retain the minimum ACL data limits, answering autonomous DLU with
           27 octets and the minimum packet time.
        4. Start a PAwR train and command Set Info Transfer on this connection.
        5-6. Receive and acknowledge the 43-octet WR_IND, decode its fields
             independently and derive the transferred periodic anchor.
        7. Receive the described subevent-0 train while maintaining at least
           ten more ACL events, then disable advertising and disconnect.

    Expected Outcome:
        The IUT transmits the longest supported control despite the 27-octet
        ACL data limit. Service_Data, identity, PHY, SyncInfo, PAwR timing and
        RspAA are valid; the transferred train can be received at its anchor.

    Reference:
        LL.TS.p28, 4.3.7.15, Table 4.3-89, Steps 1-7;
        LL/CON/CEN/BV-114-C..116-C and PER/BV-118-C..120-C.
        Core Vol 6, Part B, 4.5.11, 5.1.13 and 2.4.2.40.
    """
    link = await _long_control_link(tester, central=central, phy=phy)
    await exchange_features(link, LONG_CONTROL_FEATURES)
    await pawr_peer.configure(tester, interval=96, timing=(2, 6, 1, 10), slots=1)
    session = TransferSession(tester, link, mode='off')
    await session.run_for(3 * 96 * 1250)
    await session.command(
        hci.HCI_LE_Periodic_Advertising_Set_Info_Transfer_Command(
            link.handle, SERVICE_DATA, 0
        )
    )
    payload, counter, anchor = await session.expect_control(
        Opcode.LL_PERIODIC_SYNC_WR_IND
    )
    assert len(payload) == LONG_CONTROL_PAYLOAD_SIZE
    indication = SyncIndication.decode(payload)
    assert indication.identifier == SERVICE_DATA
    assert indication.address == bytes(DUT_ADDRESS) and not indication.random
    assert indication.phy == 1 and indication.pawr == (2, 6, 1, 10)
    target, info = transferred_anchor(indication, counter, anchor, session.interval_us)
    assert info.interval == 96 and info.access_address != indication.rsp_aa
    assert indication.rsp_aa
    session.train = PeriodicTrain(
        target,
        info.interval,
        info.counter,
        info.access_address,
        info.crc_init,
        info.channel_map,
        pawr=indication.pawr,
        rsp_aa=indication.rsp_aa,
    )
    session.mode = 'receive'
    await session.run(2 * LONG_CONTROL_EVENTS)
    assert session.received, 'train absent at the transferred subevent-0 anchor'
    assert not any(
        isinstance(event, hci.HCI_LE_Data_Length_Change_Event)
        and (event.max_tx_octets > 27 or event.max_rx_octets > 27)
        for event in tester.events
    ), 'long control transmission depended on expanded data limits'
    session.mode = 'off'
    await pawr_peer.stop_advertising(tester)
    await link.disconnect(local=True)


# Verify sending long control after feature exchange on each supported connection PHY.
PROCEDURES |= procedure_cases(
    sending_long_control_after_feature_exchange,
    {
        'LL/CON/CEN/BV-114-C': (
            'Sending Long Control PDUs after Feature Exchange',
            {'central': True, 'phy': Phy.LE_1M},
        ),
        'LL/CON/CEN/BV-115-C': (
            'Sending Long Control PDUs after Feature Exchange',
            {'central': True, 'phy': Phy.LE_2M},
        ),
        'LL/CON/CEN/BV-116-C': (
            'Sending Long Control PDUs after Feature Exchange',
            {'central': True, 'phy': Phy.LE_CODED},
        ),
        'LL/CON/PER/BV-118-C': (
            'Sending Long Control PDUs after Feature Exchange',
            {'central': False, 'phy': Phy.LE_1M},
        ),
        'LL/CON/PER/BV-119-C': (
            'Sending Long Control PDUs after Feature Exchange',
            {'central': False, 'phy': Phy.LE_2M},
        ),
        'LL/CON/PER/BV-120-C': (
            'Sending Long Control PDUs after Feature Exchange',
            {'central': False, 'phy': Phy.LE_CODED},
        ),
    },
)


def _extended_feature_pdu(pdu, opcode, page):
    """Check the independent ICS expectation for peer-visible page 1 features."""
    assert len(pdu) == 27
    assert pdu[:3] == bytes([opcode, 1, page])
    page_1 = (
        hci.LeFeatureMask.FRAME_SPACE_UPDATE
        | hci.LeFeatureMask.SHORTER_CONNECTION_INTERVALS
    ) >> 64
    assert pdu[3:] == (page_1 if page == 1 else 0).to_bytes(24, 'little')


async def extended_feature_response(tester, *, central):
    """LL Extended Feature Set Response.

    Initial Condition:
        Unencrypted ACL, public addresses and common connection parameters.
        The Lower Tester declares MaxPage 10 and random 192-bit feature pages.

    Test Case Configuration:
        LL/CON/CEN/BV-164-C: Central IUT.
        LL/CON/PER/BV-168-C: Peripheral IUT.

    Test Steps:
        Repeat for pages 1 through 10 in a reproducibly shuffled random order:
        1. Send LL_FEATURE_EXT_REQ, MaxPage 10, the selected PageNumber and
           random 24-octet FeaturePage; ensure page 10 is nonzero.
        2. Require LL_FEATURE_EXT_RSP with the same PageNumber, constant MaxPage
           and peer-visible FeaturePage matching the independent ICS declaration.

    Expected Outcome:
        Pass verdict: every page has the correct response opcode, length and
        page number. MaxPage is 1. Page 1 reports Frame Space Update and Shorter
        Connection Intervals. Monitoring Advertisers and LE Flushable ACL Data
        have Send to Peer = N. The SCI Host Support bit remains cleared.

    Reference:
        LL.TS.p28, Section 4.3.7.50, Table 4.3-133, Figure 4.3-262,
        printed page 4.3-545; Core Vol 6, Part B, 5.1.4.3.
    """
    link = await establish(tester, central=central)
    rng = random.Random(164)
    pages = list(range(1, 11))
    rng.shuffle(pages)
    for page in pages:
        features = (rng.getrandbits(192) | 1).to_bytes(24, 'little')
        response = await link.control(
            Opcode.LL_FEATURE_EXT_RSP,
            bytes([Opcode.LL_FEATURE_EXT_REQ, 10, page]) + features,
        )
        _extended_feature_pdu(response, Opcode.LL_FEATURE_EXT_RSP, page)
    await link.disconnect(local=False)


# Verify both roles for extended page responses/requests, and legacy-only peers.
PROCEDURES |= procedure_cases(
    extended_feature_response,
    {
        'LL/CON/CEN/BV-164-C': (
            'LL Extended Feature Set Response',
            {'central': True},
        ),
        'LL/CON/PER/BV-168-C': (
            'LL Extended Feature Set Response',
            {'central': False},
        ),
    },
)


async def extended_feature_request(tester, *, central):
    """LL Extended Feature Set Request.

    Initial Condition:
        Unencrypted ACL, public addresses and common connection parameters.
        Lower Tester feature bit 63 is set. Pages 1, 3 and MaxPage contain
        reproducible random nonzero bits. Each round uses a different LT address.

    Test Case Configuration:
        LL/CON/CEN/BV-165-C: Central IUT.
        LL/CON/PER/BV-169-C: Peripheral IUT.

    Test Steps:
        Repeat for (MaxPageLT, MaxRequested) = (10,10), (10,5), (6,10), (6,3), (3,1):
        1. Establish a new ACL using a distinct public LT address.
        2-3. Send Read All Remote Features and require successful Command Status
             followed by the role-specific legacy feature request.
        4-5. Send a duplicate Host command while the first is pending; require
             Command Disallowed (0x0C).
        6. Respond with LL_FEATURE_RSP, feature bit 63 set.
        7-8. Receive every extended page request through min(MaxPageLT,
             MaxRequested), validate its local FeaturePage and constant MaxPage,
             and respond with the same PageNumber and the LT's MaxPage/data.
        9. Require successful Read All Remote Features Complete: correct handle,
           Max_Remote_Page, highest fetched Max_Valid_Page, exact page 0 and
           fetched pages, and zero-filled remaining pages.
        10. Terminate the ACL from the Lower Tester.

    Expected Outcome:
        Pass verdict: all five rounds reject the duplicate command, obtain all
        required pages, and report the exact remote features and page limits.

    Source Correction:
        Step 9's reference to data sent in Steps 6 and 9 means Steps 6 and 8.

    Reference:
        LL.TS.p28, Section 4.3.7.51, Tables 4.3-134/135, Figure 4.3-263,
        printed pages 4.3-546 through 4.3-548; Core Vol 6, Part B, 5.1.4.3.
    """
    rng = random.Random(165)
    for index, (maximum, requested) in enumerate(
        ((10, 10), (10, 5), (6, 10), (6, 3), (3, 1))
    ):
        peer = hci.Address(f'12:34:56:78:90:{index:02X}', hci.AddressType.PUBLIC_DEVICE)
        link = await establish(tester, central=central, peer_address=peer)
        command = hci.HCI_LE_Read_All_Remote_Features_Command(link.handle, requested)
        await tester.command(command)
        opcode = Opcode.LL_FEATURE_REQ if central else Opcode.LL_PERIPHERAL_FEATURE_REQ
        initial = link.last_received.pdu[PDU_HEADER_SIZE:]
        request = (
            initial
            if link.last_received.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
            and initial[:1] == bytes([opcode])
            else await link.control(opcode)
        )
        assert request[0] == opcode and len(request) == 9
        assert int.from_bytes(request[1:], 'little') & (1 << 63)
        await tester.command(command, ErrorCode.COMMAND_DISALLOWED_ERROR)
        page0 = (1 << 63).to_bytes(8, 'little')
        packet = await link.exchange(
            bytes([Opcode.LL_FEATURE_RSP]) + page0, llid=LlId.CONTROL
        )
        needed = min(maximum, requested)
        expected = bytearray(page0 + bytes(240))
        received = set()
        for _ in range(needed):
            payload = packet.pdu[PDU_HEADER_SIZE:]
            if (
                packet.pdu[0] & PDU_DATA_LLID_MASK != LlId.CONTROL
                or payload[0] != Opcode.LL_FEATURE_EXT_REQ
            ):
                payload = await link.control(Opcode.LL_FEATURE_EXT_REQ)
            page = payload[2]
            assert 1 <= page <= needed and page not in received
            _extended_feature_pdu(payload, Opcode.LL_FEATURE_EXT_REQ, page)
            received.add(page)
            bits = (
                (rng.getrandbits(192) | 1).to_bytes(24, 'little')
                if page in (1, 3, maximum)
                else bytes(24)
            )
            offset = 8 + (page - 1) * 24
            expected[offset : offset + 24] = bits
            packet = await link.exchange(
                bytes([Opcode.LL_FEATURE_EXT_RSP, maximum, page]) + bits,
                llid=LlId.CONTROL,
            )
        assert received == set(range(1, needed + 1))
        # Grant the last Peripheral transmission so the completion can reach HCI.
        await link.exchange()
        event = await tester.event(hci.HCI_LE_Read_All_Remote_Features_Complete_Event)
        assert event.status == 0 and event.connection_handle == link.handle
        assert (event.max_remote_page, event.max_valid_page) == (maximum, needed)
        assert event.le_features == bytes(expected)
        await link.disconnect(local=False)


PROCEDURES |= procedure_cases(
    extended_feature_request,
    {
        'LL/CON/CEN/BV-165-C': (
            'LL Extended Feature Set Request',
            {'central': True},
        ),
        'LL/CON/PER/BV-169-C': (
            'LL Extended Feature Set Request',
            {'central': False},
        ),
    },
)


async def extended_feature_peer_unsupported(tester, *, central, peer_initiates=False):
    """LL Extended Feature Set Not Supported by the Peer.

    Initial Condition:
        Unencrypted ACL, public addresses and common connection parameters.
        The Lower Tester clears feature bit 63; select page-0 features 0x0101.

    Test Case Configuration:
        LL/CON/CEN/BV-166-C and LL/CON/PER/BV-170-C: Central/Peripheral IUT;
            the IUT initiates the legacy feature request after the Host command.
        LL/CON/PER/BV-166-C: Peripheral IUT; the Lower Tester initiates first.
        LL/CON/PER/BV-167-C: Peripheral IUT; the Lower Tester does not initiate.

    Test Steps:
        1. Establish the ACL. For PER/BV-166-C, send LL_FEATURE_REQ first and
           validate the IUT's legacy response including its feature bit 63.
        2. Send Read All Remote Features, Pages_Requested 1; require Command
           Status success. For other cases receive the role-specific legacy
           request and respond with LL_FEATURE_RSP, bit 63 clear.
        3. Require successful Read All Remote Features Complete for the handle:
           Max_Remote_Page 0, Max_Valid_Page 0, matching page 0, other pages zero.

    Expected Outcome:
        Pass verdict: the cached or freshly received legacy features complete
        the Host request without an extended feature-page exchange.

    Source Correction:
        PER/BV-166-C reverses recipients in Steps 1 and 2: the LT sends its
        request to the IUT, and the IUT responds to the LT.

    Reference:
        LL.TS.p28, Section 4.3.7.52, Table 4.3-136, Figure 4.3-264,
        printed pages 4.3-549 through 4.3-550; PER/BV-166-C and PER/BV-167-C,
        printed pages 4.3-265 through 4.3-267; Core Vol 6, Part B, 5.1.4.
    """
    link = await establish(tester, central=central)
    page0 = (0x0101).to_bytes(8, 'little')
    if peer_initiates:
        response = await link.control(
            Opcode.LL_FEATURE_RSP, bytes([Opcode.LL_FEATURE_REQ]) + page0
        )
        assert len(response) == 9
        assert int.from_bytes(response[1:], 'little') & (1 << 63)
    await tester.command(hci.HCI_LE_Read_All_Remote_Features_Command(link.handle, 1))
    if not peer_initiates:
        opcode = Opcode.LL_FEATURE_REQ if central else Opcode.LL_PERIPHERAL_FEATURE_REQ
        await link.control(opcode)
        await link.exchange(bytes([Opcode.LL_FEATURE_RSP]) + page0, llid=LlId.CONTROL)
        await link.exchange()
    event = await tester.event(hci.HCI_LE_Read_All_Remote_Features_Complete_Event)
    assert event.status == 0 and event.connection_handle == link.handle
    assert event.max_remote_page == event.max_valid_page == 0
    assert event.le_features == page0 + bytes(240)
    await link.disconnect(local=False)


PROCEDURES |= procedure_cases(
    extended_feature_peer_unsupported,
    {
        'LL/CON/CEN/BV-166-C': (
            'LL Extended Feature Set Not Supported by the Peer',
            {'central': True, 'peer_initiates': False},
        ),
        'LL/CON/PER/BV-170-C': (
            'LL Extended Feature Set Not Supported by the Peer',
            {'central': False, 'peer_initiates': False},
        ),
        'LL/CON/PER/BV-166-C': (
            'LL Extended Feature Set Not Supported by the Peer',
            {'central': False, 'peer_initiates': True},
        ),
        'LL/CON/PER/BV-167-C': (
            'LL Extended Feature Set Not Supported by the Peer',
            {'central': False, 'peer_initiates': False},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_feature_exchange(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
