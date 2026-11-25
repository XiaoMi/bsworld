# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.10: Connected Isochronous Stream."""

from random import Random

import pytest

from bscontroller import hci
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    PDU_DATA_LLID_MASK,
    PDU_DATA_SN_MASK,
    PDU_HEADER_SIZE,
    ControlOpcode,
    LlId,
)
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.cis import (
    CIS_REQ,
    PEER_CIS_FEATURES,
    channels,
    cis_request,
    connect_cis_acl,
    disconnect_cis,
    enable_cis,
    establish_configured_cis,
    establish_peer_cis,
)
from tests.qualification.iso import data_path
from tests.qualification.peer import establish
from tests.support import LE_EVENT_MASK_V1

REJECT = ControlOpcode.LL_REJECT_EXT_IND


async def configure_default_cig(tester):
    configured = await tester.command(
        hci.HCI_LE_Set_CIG_Parameters_Command(
            1,
            20000,
            20000,
            0,
            0,
            0,
            40,
            40,
            [1],
            [251],
            [251],
            [1],
            [1],
            [3],
            [3],
        )
    )
    (handle,) = configured.connection_handle
    return handle


async def central_rejected(tester):
    """CIS Setup Procedure, Central Initiated, Rejected.

    Initial Condition:
        CIS Host Support is enabled. The IUT is ACL Central and the peer
        advertises CIS support. Use the default non-test CIG parameters.

    Test Steps:
        1. Set CIG Parameters and create a CIS on the ACL.
        2. Observe LL_CIS_REQ and validate its configuration.
        3. Reject the request with LL_REJECT_EXT_IND.
        4. Observe a CIS Established event with the same failure status.

    Reference:
        LL.TS.p28, 4.10.3.1, LL/CIS/CEN/BV-03-C.
    """
    link = await connect_cis_acl(tester, central=True)
    handle = await configure_default_cig(tester)
    await tester.command(hci.HCI_LE_Create_CIS_Command([handle], [link.handle]))
    request = await link.control(ControlOpcode.LL_CIS_REQ)
    assert len(request) == 36 and request[:5] == bytes([CIS_REQ, 1, 1, 1, 1])
    assert int.from_bytes(request[5:7], 'little') == 251
    assert int.from_bytes(request[7:9], 'little') == 251
    assert int.from_bytes(request[9:12], 'little') == 20000
    assert int.from_bytes(request[12:15], 'little') == 20000
    assert 1 <= request[19] <= 31
    minimum = int.from_bytes(request[28:31], 'little')
    maximum = int.from_bytes(request[31:34], 'little')
    assert 500 <= minimum <= maximum < 40000
    reason = ErrorCode.CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR
    await link.exchange(bytes([REJECT, CIS_REQ, reason]), llid=LlId.CONTROL)
    event = await tester.event(hci.HCI_LE_CIS_Established_Event)
    assert (event.connection_handle, event.status) == (handle, reason)


async def peripheral_rejected(tester):
    """CIS Setup Response Procedure, Peripheral, Reject Response.

    Initial Condition:
        The IUT is the ACL Peripheral with CIS Host Support and the request
        event enabled.

    Test Steps:
        1. Send a valid LL_CIS_REQ using the default CIG parameters.
        2. Observe LE CIS Request and reject it with a valid HCI reason.
        3. Require the corresponding LL_REJECT_EXT_IND and reason.

    Reference:
        LL.TS.p28, 4.10.4.1, LL/CIS/PER/BV-02-C.
    """
    link = await connect_cis_acl(tester)
    await link.exchange(cis_request(link.counter + 6), llid=LlId.CONTROL)
    event = await tester.event(hci.HCI_LE_CIS_Request_Event)
    assert (event.acl_connection_handle, event.cig_id, event.cis_id) == (
        link.handle,
        1,
        1,
    )
    reason = ErrorCode.CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR
    await tester.command(
        hci.HCI_LE_Reject_CIS_Request_Command(event.cis_connection_handle, reason)
    )
    assert await link.control(REJECT) == bytes([REJECT, CIS_REQ, reason])


async def invalid_phy(tester, *, bad_phy):
    """CIS Setup Response Procedure, Peripheral, Invalid or Unsupported PHY.

    Initial Condition:
        The IUT is an ACL Peripheral with the CIS Request event enabled.

    Test Case Configuration:
        BI-04 uses 0x03 (multiple PHYs), BI-05 uses 0x80 (RFU), and BI-06
        uses 0x00 (no PHY).

    Test Steps:
        1. Repeat with the invalid value in both directions, C to P only,
           and P to C only, as in Table 4.10-38.
        2. For each request require an extended reject with a valid reason.
        3. Check that the IUT did not generate an HCI LE CIS Request.

    Reference:
        LL.TS.p28, 4.10.4.4, Table 4.10-38, Figure 4.10-42.
    """
    link = await connect_cis_acl(tester)
    for c_to_p, p_to_c in ((bad_phy, bad_phy), (bad_phy, 1), (1, bad_phy)):
        reply = await link.control(
            REJECT, cis_request(link.counter + 6, c_to_p=c_to_p, p_to_c=p_to_c)
        )
        assert reply[:2] == bytes([REJECT, CIS_REQ])
        assert ErrorCode(reply[2]) != ErrorCode.SUCCESS
        assert (
            tester.take(lambda event: isinstance(event, hci.HCI_LE_CIS_Request_Event))
            is None
        )


async def request_masked(tester):
    """CIS Request Event Not Set.

    Initial Condition:
        The IUT acts as ACL Peripheral with the CIS Host Support bit clear.

    Test Steps:
        1. Send the default LL_CIS_REQ and require an extended reject with
           Unsupported Remote Feature (0x1A).
        2. Disconnect the ACL, enable CIS Host Support and reconnect.
        3. Mask only the LE CIS Request event and send the same procedure.
           Require an extended reject and monitor HCI for at least five seconds.
        4. Unmask the event and repeat the request. Require LE CIS Request.

    Reference:
        LL.TS.p28, 4.10.4.3, LL/CIS/PER/BV-22-C, Figure 4.10-30.
    """
    link = await connect_cis_acl(tester, enable=False)
    reply = await link.control(REJECT, cis_request(link.counter + 6))
    assert reply == bytes([REJECT, CIS_REQ, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR])
    await link.disconnect()
    link = await connect_cis_acl(tester)
    await tester.masks(le=LE_EVENT_MASK_V1 & ~(1 << (hci.HCI_LE_CIS_REQUEST_EVENT - 1)))
    reply = await link.control(REJECT, cis_request(link.counter + 6))
    assert reply[:2] == bytes([REJECT, CIS_REQ])
    assert ErrorCode(reply[2]) != ErrorCode.SUCCESS
    deadline = tester.radio.now_us + 5_000_000
    while link.anchor < deadline:
        await link.exchange()
    if tester.radio.now_us < deadline:
        await tester.wait(deadline - tester.radio.now_us)
    assert (
        tester.take(lambda event: isinstance(event, hci.HCI_LE_CIS_Request_Event))
        is None
    )
    await tester.masks(le=LE_EVENT_MASK_V1)
    await link.exchange(cis_request(link.counter + 6), llid=LlId.CONTROL)
    event = await tester.event(hci.HCI_LE_CIS_Request_Event)
    assert (event.acl_connection_handle, event.cig_id, event.cis_id) == (
        link.handle,
        1,
        1,
    )


async def invalid_parameters(tester):
    """CIS Request, Invalid Parameters.

    Initial Condition:
        ACL Peripheral with CIS Host Support enabled and an LE 1M ACL.

    Test Steps:
        1. Send one request for each invalid combination in Table 4.10-41.
        2. Send the two out-of-range CIS offset requests.
        3. Require LL_REJECT_EXT_IND with Invalid LL Parameters (0x1E)
           in each round and verify no LE CIS Request reached the Host.

    Reference:
        LL.TS.p28, 4.10.4.6, LL/CIS/PER/BI-07-C, Table 4.10-41.
    """
    link = await connect_cis_acl(tester)
    for fields in (
        ((9, 3, 254),),  # C-to-P SDU interval.
        ((12, 3, 254),),
        ((15, 2, 252),),  # C-to-P maximum PDU.
        ((17, 2, 252),),
        ((15, 2, 1), (23, 1, 0x10)),  # PDU with no C-to-P burst.
        ((17, 2, 1), (23, 1, 0x01)),
        ((19, 1, 0),),  # NSE.
        ((19, 1, 32),),
        ((20, 3, 399),),  # Sub_Interval below minimum or at ISO_Interval.
        ((20, 3, 20000),),
        ((20, 3, 400), (19, 1, 1)),
        ((24, 1, 0),),  # Flush timeout.
        ((25, 1, 0),),
        ((26, 2, 3),),  # ISO_Interval.
        ((26, 2, 3201),),
        ((28, 3, 499),),  # CIS_Offset_Min.
        ((28, 3, 500), (31, 3, 40000)),  # CIS_Offset_Max equals connInterval.
    ):
        request = bytearray(cis_request(link.counter + 6))
        for offset, size, value in fields:
            request[offset : offset + size] = value.to_bytes(size, 'little')
        assert await link.control(REJECT, bytes(request)) == bytes(
            [REJECT, CIS_REQ, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR]
        )
    assert not any(
        isinstance(event, hci.HCI_LE_CIS_Request_Event) for event in tester.events
    )


async def out_of_range_cis_capacity(tester):
    """Reject LL_CIS_REQ with Out of Range BN, FT, NSE, Peripheral.

    Initial Condition:
        ACL Peripheral configured with a maximum BN and FT of one.

    Test Steps:
        1. Set BN to two in each direction independently, then FT to two
           in each direction independently. NSE already supports the wire
           maximum of 31, so the test specifies no NSE round.
        2. Require each extended reject with status 0x30.

    Reference:
        LL.TS.p28, 4.10.5.16, LL/CIS/PER/BV-52-C, Table 4.10-75.
    """
    link = await connect_cis_acl(tester)
    for offset, value in ((23, 0x12), (23, 0x21), (24, 2), (25, 2)):
        request = bytearray(cis_request(link.counter + 6))
        request[offset] = value
        assert await link.control(REJECT, bytes(request)) == bytes(
            [REJECT, CIS_REQ, ErrorCode.PARAMETER_OUT_OF_MANDATORY_RANGE_ERROR]
        )


async def reject_unsegmented_framing(tester):
    """Reject Request when Framed, Unsegmented Mode is not supported.

    Initial Condition:
        IUT is ACL Peripheral with CIS Host Support enabled.

    Test Steps:
        1. Send LL_CIS_REQ with Framed and Framing_Mode both set to one.
        2. Require an extended reject for LL_CIS_REQ with a valid error.

    Reference:
        LL.TS.p28, LL/CIS/PER/BV-50-C; Core Vol 6, Part B, 5.1.15.
    """
    link = await connect_cis_acl(tester)
    request = bytearray(
        cis_request(link.counter + 6, c_sdu=240, p_sdu=240, framed=True)
    )
    request[6] |= 0x40  # Unsegmented framing mode.
    reply = await link.control(REJECT, bytes(request))
    assert reply[:2] == bytes([REJECT, CIS_REQ])
    assert ErrorCode(reply[2]) != ErrorCode.SUCCESS


async def update_peer_clock_accuracy(tester, *, central):
    """CIS Updating Peer Clock Accuracy.

    Initial Condition:
        An ACL with the Lower Tester supporting SCA Updates. Run once as
        Central and once as Peripheral per Table 4.10-57.

    Test Steps:
        1. Send LE Request Peer SCA with the ACL handle.
        2. Observe LL_CLOCK_ACCURACY_REQ and return a valid peer SCA.
        3. Check the completion status, ACL handle and reported SCA.
        4. Repeat with a valid but unassigned handle and require 0x02.

    Reference:
        LL.TS.p28, 4.10.5.7, Table 4.10-57 and Figure 4.10-54.
    """
    link = await connect_cis_acl(
        tester,
        central=central,
        features=PEER_CIS_FEATURES | hci.LeFeatureMask.SLEEP_CLOCK_ACCURACY_UPDATES,
    )
    command = hci.HCI_LE_Request_Peer_SCA_Command
    await tester.command(command(link.handle))
    request = await link.control(ControlOpcode.LL_CLOCK_ACCURACY_REQ)
    assert len(request) == 2 and request[1] <= 7
    sca = 6
    await link.exchange(
        bytes([ControlOpcode.LL_CLOCK_ACCURACY_RSP, sca]), llid=LlId.CONTROL
    )
    complete = await tester.event(hci.HCI_LE_Request_Peer_SCA_Complete_Event)
    assert (
        complete.status,
        complete.connection_handle,
        complete.peer_clock_accuracy,
    ) == (0, link.handle, sca)
    await tester.command(command(0x0EFF), ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR)


async def unsupported_peer_clock_accuracy(tester, *, central):
    """Error Updating Peer Clock Accuracy.

    Initial Condition:
        An ACL to a peer without SCA Updates. Run both roles in Table 4.10-58.

    Test Steps:
        1. Request Peer SCA before feature exchange. Reply to the observed
           LL_CLOCK_ACCURACY_REQ with LL_UNKNOWN_RSP (alternative 3B).
        2. Require the completion status Unsupported Feature or Parameter
           Value (0x11).
        3. Repeat the HCI command and require Unsupported Remote Feature
           (0x1A) in Command Status (alternative 6A).

    Reference:
        LL.TS.p28, 4.10.5.8, Table 4.10-58 and Figure 4.10-55.
    """
    link = await establish(tester, central=central)
    command = hci.HCI_LE_Request_Peer_SCA_Command(link.handle)
    await tester.command(command)
    await link.control(ControlOpcode.LL_CLOCK_ACCURACY_REQ)
    await link.exchange(
        bytes([ControlOpcode.LL_UNKNOWN_RSP, ControlOpcode.LL_CLOCK_ACCURACY_REQ]),
        llid=LlId.CONTROL,
    )
    complete = await tester.event(hci.HCI_LE_Request_Peer_SCA_Complete_Event)
    assert (complete.status, complete.connection_handle) == (
        ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
        link.handle,
    )
    await tester.command(command, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)


async def setup_hci_paths(tester, handle):
    for direction in (0, 1):
        await tester.command(
            hci.HCI_LE_Setup_ISO_Data_Path_Command(
                handle, direction, 0, hci.CodingFormat(hci.CodecID.TRANSPARENT), 0, b''
            )
        )


async def exchange_cis_data(tester, peer, handle, random):
    counter = peer.counter
    outbound = random.randbytes(32)
    inbound = random.randbytes(32)
    tester.send(
        bytes(
            hci.HCI_IsoDataPacket(
                handle,
                len(outbound) + 4,
                outbound,
                2,
                packet_sequence_number=counter,
                iso_sdu_length=len(outbound),
                packet_status_flag=0,
            )
        )
    )
    # Acknowledge the previous TX sequence and send a new SDU at this event.
    header = ((counter & 1) << 3) | ((counter & 1) << 2)
    observed = await peer.exchange(inbound, header=header)
    assert observed.pdu[0] & 0b11 == 0
    assert observed.pdu[2:] == outbound
    received = await tester.expect(
        lambda event: isinstance(event, hci.HCI_IsoDataPacket)
        and event.connection_handle == handle
    )
    assert received.packet_status_flag == 0
    assert received.iso_sdu_fragment == inbound


async def bidirectional_bn_one(tester, *, central, encrypted=False):
    """Sending and Receiving Data in Bidirectional CIS, BN = 1.

    Initial Condition:
        CIS on LE 1M, one subevent, FT=1, BN=1 in both directions and 100 ms
        ISO/SDU intervals. Select role and encryption from Table 4.10-67.

    Test Steps:
        1. Enable both HCI ISO data paths.
        2. For 50 intervals inject different payloads on HCI and over the air.
        3. Check each IUT Data PDU's LLID, length and payload, and each
           received HCI SDU's status and payload. For encrypted cases the
           Lower Tester authenticates each MIC as it decrypts.

    Source Correction:
        Use 0x50 for the stated 100 ms ISO interval. The 0xF0 encoding in
        Table 4.10-66 represents 300 ms in 1.25 ms units.

    Reference:
        LL.TS.p28, 4.10.5.12, Tables 4.10-66 and 4.10-67, Figure 4.10-59.
    """
    link = await connect_cis_acl(tester, central=central, encrypted=encrypted)
    _, peer, event = await establish_peer_cis(
        tester, central=central, link=link, interval_us=100000, test_nse=1
    )
    handle = event.connection_handle
    await setup_hci_paths(tester, handle)
    random = Random(41067)
    for _ in range(50):
        await exchange_cis_data(tester, peer, handle, random)


async def terminate_cis(tester, *, central, initiated):
    """Terminate a live CIS and verify its remaining ACL state.

    Initial Condition:
        Establish an LE 1M CIS with one subevent, BN=FT=1, 20 ms ISO/SDU
        intervals, and 130 octet SDUs and PDUs in both directions.

    Test Case Configuration:
        CEN/BV-15 and PER/BV-12 initiate termination. CEN/BV-16 and
        PER/BV-13 accept termination from the Lower Tester.

    Test Steps:
        1. Exchange a CIS data PDU and acknowledge it.
        2. For initiated termination as Central, reject Remove CIG while
           the stream is active. Disconnect via HCI, check LL_CIS_TERMINATE_IND
           and acknowledge it.
        3. For accepted termination, send LL_CIS_TERMINATE_IND and require
           an ACL ACK before Disconnection Complete for the CIS handle.
        4. Recreate a Central-initiated CIS on its original handle and
           exchange data in both directions. In the accepting cases, check
           that the old CIS no longer carries data.

    Expected Outcome:
        Termination reason matches the initiating side's request. The ACL
        survives. The recreated CIS carries fresh data, and an accepted
        termination stops the old CIS.

    Reference:
        LL.TS.p28, 4.10.5.5 and 4.10.5.6, Figures 4.10-52 and 4.10-53.
    """
    link, peer, event = await establish_peer_cis(
        tester, central=central, test_nse=1, sdu_sizes=(130, 130)
    )
    handle = event.connection_handle
    random = Random(41054)
    await setup_hci_paths(tester, handle)
    await exchange_cis_data(tester, peer, handle, random)
    if initiated:
        if central:
            await tester.command(
                hci.HCI_LE_Remove_CIG_Command(1), ErrorCode.COMMAND_DISALLOWED_ERROR
            )
        await disconnect_cis(tester, link, handle)
        if central:
            _, peer, _ = await establish_configured_cis(tester, link, handle)
            # The retained output path may report the empty establishment
            # slot before the Upper Tester starts supplying SDUs again.
            await tester.wait(peer.interval_us // 2)
            tester.take(
                lambda packet: isinstance(packet, hci.HCI_IsoDataPacket)
                and packet.connection_handle == handle
                and packet.packet_status_flag == 2
            )
            await exchange_cis_data(tester, peer, handle, random)
        return

    reason = ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    await link.exchange(
        bytes([ControlOpcode.LL_CIS_TERMINATE_IND, 1, 1, reason]),
        llid=LlId.CONTROL,
        check_ack=not central,
    )
    if central:
        assert not any(
            isinstance(item, hci.HCI_Disconnection_Complete_Event)
            and item.connection_handle == handle
            for item in tester.events
        )
        await link.exchange(check_ack=True)
    complete = await tester.event(hci.HCI_Disconnection_Complete_Event)
    assert (complete.status, complete.connection_handle, complete.reason) == (
        ErrorCode.SUCCESS,
        handle,
        reason,
    )
    peer.skip_elapsed()
    while link.anchor + 1000 < peer.nominal_us:
        await link.exchange()
    channel = channels(
        peer.counter, peer.access_address, link.parameters.channel_map, peer.nse
    )[peer.subevent]
    radio_parameters = {
        'access_address': peer.access_address,
        'crc_init': link.parameters.crc_init,
    }
    if not central:
        await tester.transmit(
            channel, b'\x40\x00', start_us=peer.nominal_us, **radio_parameters
        )
        duration_us = 3000
    else:
        duration_us = peer.nominal_us - tester.radio.now_us + 3000
    assert (
        await tester.receive(channel, duration_us=duration_us, **radio_parameters)
        is None
    )


async def feature_exchange_before_request(tester):
    """Feature Exchange Before a CIS Connect Request.

    Initial Condition:
        ACL Central and a Lower Tester supporting CIS Peripheral. Enable the
        ISO Host Support bit before establishing the ACL.

    Test Steps:
        1. Configure the CIG and issue Create CIS on the ACL.
        2. Wait for the IUT's LL_FEATURE_REQ and send LL_FEATURE_RSP. Check
           that no LL_CIS_REQ was sent before the feature request.
        3. Observe the setup request, send CIS Response and observe CIS
           Indication. Complete establishment on the air.

    Source Correction:
        Use bit 32 as defined by Core Vol 6, Part B, 4.6.29 for Host Support.
        CIS Central is a Controller bit and is independent of this command.

    Reference:
        LL.TS.p28, LL/CIS/CEN/BV-56-C, Figure 4.10-23.
    """
    await enable_cis(tester)
    link = await establish(tester, central=True, csa2=True, interval=32)
    if link.last_received.pdu[:3] == bytes([0x03, 9, ControlOpcode.LL_FEATURE_REQ]):
        pytest.skip('Inconclusive: autonomous Feature Exchange at ACL establishment')
    handle = await configure_default_cig(tester)
    features_exchanged = False

    def respond(packet):
        nonlocal features_exchanged
        control = packet.pdu[2:]
        if packet.pdu[0] & 3 != LlId.CONTROL:
            return None
        if control[0] == ControlOpcode.LL_FEATURE_REQ:
            features_exchanged = True
            return bytes([ControlOpcode.LL_FEATURE_RSP]) + PEER_CIS_FEATURES.to_bytes(
                8, 'little'
            )
        if control[0] == CIS_REQ:
            assert features_exchanged
        return None

    link.on_receive = respond
    _, _, event = await establish_configured_cis(tester, link, handle)
    assert event.status == ErrorCode.SUCCESS and features_exchanged


async def cis_host_support(tester):
    """Isochronous Channels Host Support Feature Bit.

    Initial Condition:
        IUT Central in standby, with ISO Host Support at its reset value.

    Test Steps:
        1. Reject Host configuration of the Controller-owned Broadcaster bit.
        2. Establish an ACL and require Command Disallowed when setting bit 32.
        3. Run the three rounds in Table 4.10-9. Retain the reset value,
           set the bit, then clear the bit, before making each ACL.
        4. Set default CIG parameters. Require a CIS only for the second round.
        5. Disconnect each ACL and verify no CIS termination PDU precedes
           ACL termination. Remove the CIG after the successful round.

    Reference:
        LL.TS.p28, LL/CIS/CEN/BV-30-C, Table 4.10-9 and Figure 4.10-9.
    """
    feature = hci.HCI_LE_Set_Host_Feature_Command
    await tester.command(
        feature(30, 1), ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR
    )
    link = await connect_cis_acl(tester, central=True, enable=False)
    await tester.command(feature(32, 1), ErrorCode.COMMAND_DISALLOWED_ERROR)
    await link.disconnect()
    for value in (None, 1, 0):
        if value is not None:
            await tester.command(feature(32, value))
        link = await connect_cis_acl(tester, central=True, enable=False)
        handle = await configure_default_cig(tester)
        if value:
            _, _, event = await establish_configured_cis(tester, link, handle)
            assert event.status == ErrorCode.SUCCESS
        else:
            await tester.command(
                hci.HCI_LE_Create_CIS_Command([handle], [link.handle]),
                ErrorCode.COMMAND_DISALLOWED_ERROR,
            )

        def observe(packet):
            if packet.pdu[0] & 3 == LlId.CONTROL:
                assert packet.pdu[2] != ControlOpcode.LL_CIS_TERMINATE_IND

        link.on_receive = observe
        await link.disconnect()
        if value:
            await tester.command(hci.HCI_LE_Remove_CIG_Command(1))


PROCEDURES: ProcedureMap = {}

# Verify rejection from the Lower Tester and from the IUT Host.
PROCEDURES |= procedure_cases(
    central_rejected, {'LL/CIS/CEN/BV-03-C': ('CIS Central setup rejected', {})}
)
PROCEDURES |= procedure_cases(
    peripheral_rejected, {'LL/CIS/PER/BV-02-C': ('CIS Peripheral rejects setup', {})}
)

# Check host-feature gating, a masked request and the event after unmasking.
PROCEDURES |= procedure_cases(
    request_masked, {'LL/CIS/PER/BV-22-C': ('CIS Request event masked', {})}
)

# Check every directional permutation of each invalid PHY selection.
PROCEDURES |= procedure_cases(
    invalid_phy,
    {
        f'LL/CIS/PER/BI-{number:02}-C': (title, {'bad_phy': phy})
        for number, title, phy in (
            (4, 'CIS Request with multiple PHYs', 0x03),
            (5, 'CIS Request with reserved PHY bits', 0x80),
            (6, 'CIS Request with no PHY', 0x00),
        )
    },
)


# Reject malformed fields and optional transport configurations outside the profile.
PROCEDURES |= procedure_cases(
    invalid_parameters, {'LL/CIS/PER/BI-07-C': ('CIS request invalid parameters', {})}
)
PROCEDURES |= procedure_cases(
    out_of_range_cis_capacity,
    {'LL/CIS/PER/BV-52-C': ('Reject CIS beyond advertised BN or FT', {})},
)
PROCEDURES |= procedure_cases(
    reject_unsegmented_framing,
    {'LL/CIS/PER/BV-50-C': ('Reject unsegmented CIS framing', {})},
)

# Validate the SCA procedure and wrong-handle status in both ACL roles.
PROCEDURES |= procedure_cases(
    update_peer_clock_accuracy,
    {
        'LL/CIS/CEN/BV-24-C': ('CIS Central updates Peer SCA', {'central': True}),
        'LL/CIS/PER/BV-18-C': ('CIS Peripheral updates Peer SCA', {'central': False}),
    },
)
PROCEDURES |= procedure_cases(
    unsupported_peer_clock_accuracy,
    {
        'LL/CIS/CEN/BI-01-C': ('CIS Central peer lacks SCA Updates', {'central': True}),
        'LL/CIS/PER/BI-01-C': (
            'CIS Peripheral peer lacks SCA Updates',
            {'central': False},
        ),
    },
)

# Check the four combinations of IUT role and termination side.
PROCEDURES |= procedure_cases(
    terminate_cis,
    {
        'LL/CIS/CEN/BV-15-C': (
            'CIS Terminate Procedure, Initiated, Central',
            {'central': True, 'initiated': True},
        ),
        'LL/CIS/PER/BV-12-C': (
            'CIS Terminate Procedure, Initiated, Peripheral',
            {'central': False, 'initiated': True},
        ),
        'LL/CIS/CEN/BV-16-C': (
            'CIS Terminate Procedure, Accepting, Central',
            {'central': True, 'initiated': False},
        ),
        'LL/CIS/PER/BV-13-C': (
            'CIS Terminate Procedure, Accepting, Peripheral',
            {'central': False, 'initiated': False},
        ),
    },
)


# Check bidirectional HCI and air data over 50 independent CIS events.
PROCEDURES |= procedure_cases(
    bidirectional_bn_one,
    {
        'LL/CIS/CEN/BV-47-C': (
            'CIS BN=1 bidirectional data, Central',
            {'central': True},
        ),
        'LL/CIS/PER/BV-35-C': (
            'CIS BN=1 bidirectional data, Peripheral',
            {'central': False},
        ),
        'LL/CIS/CEN/BV-48-C': (
            'CIS BN=1 encrypted data, Central',
            {'central': True, 'encrypted': True},
        ),
        'LL/CIS/PER/BV-36-C': (
            'CIS BN=1 encrypted data, Peripheral',
            {'central': False, 'encrypted': True},
        ),
    },
)


# Gate CIS creation on both local Host Support and known peer capabilities.
PROCEDURES |= procedure_cases(
    cis_host_support, {'LL/CIS/CEN/BV-30-C': ('CIS Host Support feature bit', {})}
)
PROCEDURES |= procedure_cases(
    feature_exchange_before_request,
    {'LL/CIS/CEN/BV-56-C': ('Feature Exchange before CIS setup', {})},
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks', collect_cases(PROCEDURES)
)
async def test_connected_isochronous(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)


@pytest.mark.asyncio
async def test_cis_runs_before_indication_acknowledgment(air_dut):
    """Central starts CIS on transmission and starts supervision on ACL ACK.

    Core Vol 6, Part B, 5.1.15 and 4.5.2. The Lower Tester receives
    LL_CIS_IND while withholding its acknowledgment. Observe seven CIS
    intervals without responding, then establish the stream while the ACL
    still awaits that acknowledgment.
    """
    tester = air_dut
    link = await connect_cis_acl(tester, central=True)

    def withhold_acknowledgment(packet):
        header = packet.pdu[0]
        if (
            header & PDU_DATA_LLID_MASK == LlId.CONTROL
            and packet.pdu[PDU_HEADER_SIZE] == ControlOpcode.LL_CIS_IND
        ):
            link.nesn = int(bool(header & PDU_DATA_SN_MASK))

    link.on_receive = withhold_acknowledgment
    _, peer, handle = await establish_peer_cis(
        tester, central=True, link=link, test_nse=1, exchange_first=False
    )
    await tester.command(
        hci.HCI_LE_Enable_Encryption_Command(link.handle, bytes(8), 0, bytes(16)),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )
    for _ in range(7):
        await peer.exchange(transmit=False)
    assert (
        tester.take(
            lambda event: isinstance(event, hci.HCI_LE_CIS_Established_Event)
            and event.connection_handle == handle
        )
        is None
    )
    await peer.exchange()
    event = await tester.event(hci.HCI_LE_CIS_Established_Event)
    assert (event.status, event.connection_handle) == (ErrorCode.SUCCESS, handle)

    link.on_receive = None
    await link.exchange()
    assert (
        tester.take(
            lambda event: isinstance(event, hci.HCI_Disconnection_Complete_Event)
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('central', [True, False], ids=['central', 'peripheral'])
async def test_cis_established_precedes_first_iso_sdu(air_dut, central):
    """With a preconfigured HCI path, deliver the first SDU after CIS Established."""
    tester = air_dut
    _, peer, handle = await establish_peer_cis(
        tester,
        central=central,
        test_nse=1,
        exchange_first=False,
        before_first_packet=lambda cis_handle: data_path(tester, cis_handle, 1),
    )
    # A missed first CIS event has no established HCI stream to report to.
    await peer.exchange(transmit=False, expect_rx=central)
    await tester.wait(6000)
    kinds = (hci.HCI_LE_CIS_Established_Event, hci.HCI_IsoDataPacket)

    def for_stream(event):
        return isinstance(event, kinds) and event.connection_handle == handle

    assert tester.take(for_stream) is None
    await peer.exchange(b'first', header=PDU_DATA_SN_MASK)
    established = await tester.expect(for_stream)
    assert isinstance(established, hci.HCI_LE_CIS_Established_Event)
    assert established.status == ErrorCode.SUCCESS
    packet = await tester.expect(for_stream)
    assert isinstance(packet, hci.HCI_IsoDataPacket)
    assert (packet.packet_status_flag, packet.iso_sdu_fragment) == (0, b'first')
