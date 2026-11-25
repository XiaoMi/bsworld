# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""HCI.TS.p39, Section 4.15.1: Connected Isochronous Streams."""

import pytest

from bscontroller import hci
from bscontroller.linklayer import ControlOpcode, LlId
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.cis import (
    CIS_REQ,
    cis_request,
    connect_cis_acl,
    establish_configured_cis,
    establish_peer_cis,
)
from tests.qualification.iso import data_path, framed_sdu, iso_packet
from tests.support import LE_EVENT_MASK_ALL

ErrorCode = hci.HCI_ErrorCode
INVALID = ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
UNSUPPORTED = ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR
DISALLOWED = ErrorCode.COMMAND_DISALLOWED_ERROR
RESOURCE_ERRORS = (
    ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR,
    ErrorCode.CONNECTION_LIMIT_EXCEEDED_ERROR,
    ErrorCode.CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR,
)

DEFAULT_CIG = {
    'cig_id': 1,
    'sdu_interval_c_to_p': 20_000,
    'sdu_interval_p_to_c': 20_000,
    'worst_case_sca': 0,
    'packing': 0,
    'framing': 0,
    'max_transport_latency_c_to_p': 40,
    'max_transport_latency_p_to_c': 40,
}


def _set_cig(*, count=1, **changes):
    defaults = DEFAULT_CIG | {
        'cis_id': tuple(range(count)),
        'max_sdu_c_to_p': (10,) * count,
        'max_sdu_p_to_c': (0,) * count,
        'phy_c_to_p': (hci.PhyBit.LE_1M,) * count,
        'phy_p_to_c': (hci.PhyBit.LE_1M,) * count,
        'rtn_c_to_p': (2,) * count,
        'rtn_p_to_c': (2,) * count,
    }
    return hci.HCI_LE_Set_CIG_Parameters_Command(**(defaults | changes))


async def _expect_cig_error(tester, link, command, allowed):
    result = await tester.command(command, (ErrorCode.SUCCESS, *allowed))
    if result.status != ErrorCode.SUCCESS:
        return
    response = await tester.command(
        hci.HCI_LE_Create_CIS_Command(
            result.connection_handle, (link.handle,) * len(result.connection_handle)
        ),
        (ErrorCode.SUCCESS, *allowed),
    )
    if response.status == ErrorCode.SUCCESS:
        event = await tester.event(hci.HCI_LE_CIS_Established_Event)
        assert event.connection_handle in result.connection_handle
        assert event.status in allowed


async def central_cis_non_test_command(tester):
    """Create a framed CIS with non-test parameters and exercise its errors.

    Initial Condition:
        The IUT is Central on an ACL to the Lower Tester. Table 4.91 selects
        Steps 1–8, 11–13, 15–33 and Alternative 21A for a Controller with
        all three PHYs, asymmetric PHYs and segmented framing. Both CIS
        Established event versions are enabled.

    Test Steps:
        1. Configure 16 CISes, try to add 16 distinct CIS IDs to the same
           CIG, and remove the CIG. Exercise zero and RFU PHY masks and
           too little transport latency for the framed 20 ms SDU interval.
        2. Configure one framed CIS and try to disconnect it before creation.
        3. Start creation, try to change the CIG, and cancel while the Lower
           Tester is withholding LL_CIS_RSP. Supply a late LL_CIS_RSP.
        4. Repeat the cancellation with a late LL_REJECT_EXT_IND instead.
        5. Create the CIS again, configure its input data path and send an
           SDU. Exercise duplicate data path and creation commands, unknown
           identifiers, and Peripheral-only commands on the Central CIS.

    Expected Outcome:
        The second group configuration exceeds the CIS limit. Invalid
        commands return the prescribed statuses. Each cancellation notifies
        the Host once and rejects a late LL_CIS_RSP with 0x44. The same
        handle then establishes successfully and carries framed data.

    Reference:
        HCI.TS.p39, 4.15.1.1, HCI/CIS/BV-19-C, Table 4.91.
        Core Vol 4, Part E, 7.1.6, 7.8.97 and 7.8.101.
    """
    link = await connect_cis_acl(tester, central=True)
    await tester.masks(le=LE_EVENT_MASK_ALL)
    unknown = ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
    cancelled = ErrorCode.OPERATION_CANCELLED_BY_HOST_ERROR
    reason = ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    local_reason = ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR
    timing = {'sdu_interval_c_to_p': 100_000, 'sdu_interval_p_to_c': 100_000}
    capacity = await tester.command(_set_cig(count=16, **timing))
    assert capacity.cig_id == 1 and len(set(capacity.connection_handle)) == 16
    await tester.command(
        _set_cig(count=16, cis_id=tuple(range(16, 32)), **timing),
        ErrorCode.CONNECTION_LIMIT_EXCEEDED_ERROR,
    )
    assert (await tester.command(hci.HCI_LE_Remove_CIG_Command(1))).cig_id == 1
    for direction in ('phy_c_to_p', 'phy_p_to_c'):
        await tester.command(_set_cig(**{direction: (0,)}), (INVALID, UNSUPPORTED))
        await tester.command(_set_cig(**{direction: (0xF8,)}), UNSUPPORTED)
    await tester.command(
        _set_cig(
            framing=1, max_transport_latency_c_to_p=10, max_transport_latency_p_to_c=10
        ),
        (UNSUPPORTED, INVALID),
    )
    configure = _set_cig(
        framing=1,
        max_transport_latency_c_to_p=50,
        max_transport_latency_p_to_c=50,
        max_sdu_p_to_c=(10,),
    )
    configured = await tester.command(configure)
    assert configured.cig_id == 1
    (handle,) = configured.connection_handle
    create = hci.HCI_LE_Create_CIS_Command((handle,), (link.handle,))
    disconnect = hci.HCI_Disconnect_Command(handle, reason)
    lifecycle_events = (
        hci.HCI_LE_CIS_Established_Event,
        hci.HCI_LE_CIS_Established_V2_Event,
        hci.HCI_Disconnection_Complete_Event,
    )
    result = await tester.command(disconnect, (ErrorCode.SUCCESS, DISALLOWED))
    if result.status == ErrorCode.SUCCESS:
        event = await tester.event(hci.HCI_Disconnection_Complete_Event)
        assert event.status == DISALLOWED

    for reject in (False, True):
        await tester.command(create)
        request = await link.control(ControlOpcode.LL_CIS_REQ)
        if not reject:
            await tester.command(configure, DISALLOWED)
        await tester.command(disconnect)
        if reject:
            late_pdu = bytes(
                (
                    ControlOpcode.LL_REJECT_EXT_IND,
                    ControlOpcode.LL_CIS_REQ,
                    ErrorCode.CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR,
                )
            )
            await link.exchange(late_pdu, llid=LlId.CONTROL)
        else:
            late_pdu = bytes((ControlOpcode.LL_CIS_RSP,)) + request[-8:]
            rejected = await link.control(ControlOpcode.LL_REJECT_EXT_IND, late_pdu)
            assert rejected == bytes(
                (ControlOpcode.LL_REJECT_EXT_IND, ControlOpcode.LL_CIS_RSP, cancelled)
            )
        established = await tester.event(lifecycle_events[:2])
        assert (established.status, established.connection_handle) == (
            cancelled,
            handle,
        )
        disconnected = await tester.event(hci.HCI_Disconnection_Complete_Event)
        assert (
            disconnected.status,
            disconnected.connection_handle,
            disconnected.reason,
        ) == (ErrorCode.SUCCESS, handle, local_reason)
        await link.exchange()
        assert tester.take(lambda event: isinstance(event, lifecycle_events)) is None

    requests = []
    _, air, established = await establish_configured_cis(
        tester, link, handle, check_request=requests.append
    )
    assert isinstance(established, hci.HCI_LE_CIS_Established_V2_Event)
    (request,) = requests
    assert (
        established.sub_interval,
        established.max_sdu_c_to_p,
        established.max_sdu_p_to_c,
        established.sdu_interval_c_to_p,
        established.sdu_interval_p_to_c,
        established.framing,
    ) == (
        air.subinterval_us,
        int.from_bytes(request[5:7], 'little') & 0x0FFF,
        int.from_bytes(request[7:9], 'little') & 0x0FFF,
        int.from_bytes(request[9:12], 'little'),
        int.from_bytes(request[12:15], 'little'),
        request[6] >> 7,
    )
    await data_path(tester, handle, 0)
    payload = bytes(range(10))
    tester.send(bytes(iso_packet(handle, 0, payload)))
    air.skip_elapsed()
    for _ in range(2 * air.nse):
        packet = await air.exchange(expect_rx=None)
        if packet is not None and packet.pdu[2:]:
            break
    else:
        raise AssertionError('CIS did not transmit the framed SDU')
    assert packet.pdu[0] & 0x43 == 2
    assert packet.pdu[2:4] == bytes((2, len(payload) + 3))
    assert packet.pdu[7:] == payload

    setup = hci.HCI_LE_Setup_ISO_Data_Path_Command
    path = (0, 0, hci.CodingFormat(hci.CodecID.TRANSPARENT), 0, b'')
    await tester.command(setup(handle, *path), DISALLOWED)
    await tester.command(setup(0x0EFF, *path), unknown)
    await tester.command(create, ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR)
    await tester.command(hci.HCI_LE_Remove_CIG_Command(0xEF), unknown)
    await tester.command(hci.HCI_LE_Accept_CIS_Request_Command(handle), DISALLOWED)
    await tester.command(
        hci.HCI_LE_Reject_CIS_Request_Command(handle, reason), DISALLOWED
    )


async def invalid_cig_parameters(tester):
    """Reject invalid non-test CIG parameters.

    Initial Condition:
        The IUT is Central on an ACL to the Lower Tester. Use Table 4.99
        parameters as the baseline.

    Test Steps:
        1. Vary one parameter according to Table 4.98. For the invalid count
           use 26 valid CIS records with a declared count of 32.
        2. Check Command Complete after each request.
        3. Request a 255 µs SDU interval with Max_SDU of 28. Check Set CIG
           Parameters and, if accepted, Create CIS.
        4. Repeat Step 3 with 16 CISes and Max_SDU of 32.

    Expected Outcome:
        The PHY rows return 0x11 and remaining Table 4.98 rows return 0x12.
        Both short-interval configurations fail in the command or CIS event
        with a case-appropriate code.

    Reference:
        HCI.TS.p39, 4.15.1, HCI/CIS/BI-05-C, Tables 4.98–99.
    """
    link = await connect_cis_acl(tester, central=True)
    for changes in (
        {'cig_id': 0xFF},
        {'sdu_interval_c_to_p': 0xF00000},
        {'worst_case_sca': 0xF0},
        {'count': 2, 'packing': 0xF0},
        {'framing': 0xF0},
        {'max_transport_latency_c_to_p': 0xF000},
        {'cis_id': (0xFF,)},
        {'max_sdu_c_to_p': (0xF000,)},
        {'max_sdu_p_to_c': (0xF000,)},
    ):
        await tester.command(_set_cig(**changes), INVALID)
    for phy in ('phy_c_to_p', 'phy_p_to_c'):
        await tester.command(_set_cig(**{phy: (0xF0,)}), UNSUPPORTED)

    parameters = bytearray(_set_cig(count=26).parameters)
    parameters[14] = 0x20
    await tester.command(
        hci.HCI_Command(
            bytes(parameters), op_code=hci.HCI_LE_SET_CIG_PARAMETERS_COMMAND
        ),
        INVALID,
    )
    await _expect_cig_error(
        tester,
        link,
        _set_cig(sdu_interval_c_to_p=255, max_sdu_c_to_p=(28,)),
        (UNSUPPORTED, INVALID),
    )
    await _expect_cig_error(
        tester,
        link,
        _set_cig(count=16, sdu_interval_c_to_p=255, max_sdu_c_to_p=(32,) * 16),
        (UNSUPPORTED, INVALID, *RESOURCE_ERRORS),
    )


async def invalid_transport_latency(tester):
    """Reject a CIS with an infeasible maximum transport latency.

    Initial Condition:
        CIS Host Support is set and the IUT is Central on an existing ACL.
        The Lower Tester is Peripheral.

    Test Steps:
        1. Set a framed CIG with 100 ms SDU intervals, Max_SDU 384 in
           both directions, and both maximum transport latencies set to 5 ms.
        2. If Set CIG Parameters succeeds, issue Create CIS for its handles
           and check Command Status or the subsequent CIS Established event.

    Expected Outcome:
        The Controller reports 0x11 or 0x12 in one of the allowed responses.

    Reference:
        HCI.TS.p39, 4.15.1, HCI/CIS/BI-12-C, Figure 4.159;
        LL.TS.p28, 4.10.2.3.
    """
    link = await connect_cis_acl(tester, central=True)
    await _expect_cig_error(
        tester,
        link,
        _set_cig(
            framing=1,
            sdu_interval_c_to_p=100_000,
            sdu_interval_p_to_c=100_000,
            max_sdu_c_to_p=(384,),
            max_sdu_p_to_c=(384,),
            max_transport_latency_c_to_p=5,
            max_transport_latency_p_to_c=5,
            rtn_c_to_p=(3,),
            rtn_p_to_c=(3,),
        ),
        (UNSUPPORTED, INVALID),
    )


async def unsupported_test_burst_or_flush(tester, *, bn, ft):
    """Reject CIS burst or flush settings outside the declared capability.

    Initial Condition:
        The IUT is Central on an ACL to the Lower Tester.

    Test Case Configuration:
        BI-14 sets both BN values to 2. BI-15 sets both FT values to 2.
        Keep the other variant parameter at 1.

    Test Steps:
        1. Send LE Set CIG Parameters Test with the selected configuration
           and otherwise valid bidirectional parameters.

    Expected Outcome:
        Command Complete returns Unsupported Feature or Parameter Value
        (0x11) for the selected unsupported setting.

    Reference:
        HCI.TS.p39, 4.15.1.6, HCI/CIS/BI-14-C and BI-15-C, Table 4.102.
    """
    await connect_cis_acl(tester, central=True)
    await tester.command(
        hci.HCI_LE_Set_CIG_Parameters_Test_Command(
            cig_id=1,
            sdu_interval_c_to_p=20_000,
            sdu_interval_p_to_c=20_000,
            ft_c_to_p=ft,
            ft_p_to_c=ft,
            iso_interval=16,
            worst_case_sca=0,
            packing=0,
            framing=0,
            cis_id=(1,),
            nse=(3,),
            max_sdu_c_to_p=(32,),
            max_sdu_p_to_c=(32,),
            max_pdu_c_to_p=(32,),
            max_pdu_p_to_c=(32,),
            phy_c_to_p=(1,),
            phy_p_to_c=(1,),
            bn_c_to_p=(bn,),
            bn_p_to_c=(bn,),
        ),
        UNSUPPORTED,
    )


async def unsegmented_framing_unsupported(tester):
    """Reject the unsegmented framing option in LE Set CIG Parameters.

    Initial Condition:
        The IUT has an ACL connection and declares no CIS unsegmented
        framing support.

    Test Steps:
        1. Set Framing to 0x02 in an otherwise valid CIG configuration.

    Expected Outcome:
        Command Complete reports Unsupported Feature or Parameter Value
        (0x11).

    Reference:
        HCI.TS.p39, 4.15.1, HCI/CIS/BI-18-C.
    """
    await connect_cis_acl(tester, central=True)
    await tester.command(_set_cig(framing=2), UNSUPPORTED)


async def cis_request_timeout(tester):
    """Report Connection Accept Timeout for an unanswered CIS Request.

    Initial Condition:
        The IUT is Peripheral with the CIS Host feature enabled. Set the
        Connection Accept Timeout to 100 ms before the CIS Request.

    Test Steps:
        1. Send a valid LL_CIS_REQ and observe the CIS Request event.
        2. Continue exchanging ACL packets without accepting or rejecting
           the CIS.
        3. Observe CIS Established after the Connection Accept Timeout.

    Expected Outcome:
        Until the timeout, no establishment event is reported for the
        request. At expiry the event uses its handle and status 0x10.

    Reference:
        HCI.TS.p39, 4.15.1, HCI/CIS/BI-07-C; Core Vol 4, Part E, 6.7.
    """
    link = await connect_cis_acl(tester)
    await tester.command(hci.HCI_Write_Connection_Accept_Timeout_Command(0x00A0))
    await link.exchange(cis_request((link.counter + 8) & 0xFFFF), llid=LlId.CONTROL)
    request = await tester.event(hci.HCI_LE_CIS_Request_Event)
    assert request.acl_connection_handle == link.handle
    # The lower tester stores the exclusive end; the PHY's receive callback
    # sees the last occupied microsecond.
    deadline_us = link.last_sent_end_us - 1 + 100_000
    while link.anchor + 2_000 < deadline_us:
        await link.exchange()
    await tester.wait(deadline_us - tester.radio.now_us - 1)
    assert (
        tester.take(lambda event: isinstance(event, hci.HCI_LE_CIS_Established_Event))
        is None
    )
    await tester.wait(2_000)
    result = tester.take(
        lambda event: isinstance(event, hci.HCI_LE_CIS_Established_Event)
    )
    assert result is not None
    assert (result.connection_handle, result.status) == (
        request.cis_connection_handle,
        ErrorCode.CONNECTION_ACCEPT_TIMEOUT_ERROR,
    )


async def reject_max_sdu_in_wrong_direction(tester):
    """Reject a Max_SDU change that contradicts a configured ISO data path.

    Initial Condition:
        The IUT is Central on an ACL connection to the Lower Tester.

    Test Steps:
        1. Configure a receive-only CIS and its Controller-to-Host data path.
        2. Try to reconfigure that CIS to transmit-only.
        3. Remove its output data path and retry the reconfiguration.
        4. Set up the Host-to-Controller input path and request a receive-only
           configuration.

    Expected Outcome:
        Each change that disables a configured path fails with 0x0C.
        Removing the conflicting path allows reconfiguring the CIS.

    Reference:
        HCI.TS.p39, 4.15.1, HCI/CIS/BI-10-C, Figure 4.158.
    """
    await connect_cis_acl(tester, central=True)
    receive = _set_cig(cig_id=0, max_sdu_c_to_p=(0,), max_sdu_p_to_c=(42,))
    send = _set_cig(cig_id=0, max_sdu_c_to_p=(42,), max_sdu_p_to_c=(0,))
    result = await tester.command(receive)
    (handle,) = result.connection_handle
    await data_path(tester, handle, 1)
    await tester.command(send, DISALLOWED)
    removed = await tester.command(hci.HCI_LE_Remove_ISO_Data_Path_Command(handle, 2))
    assert removed.connection_handle == handle
    result = await tester.command(send)
    (handle,) = result.connection_handle
    await data_path(tester, handle, 0)
    await tester.command(receive, DISALLOWED)


async def _verify_two_configured_streams(
    tester, link, handles, *, sizes, interval, framed
):
    for cis_id, (handle, size) in enumerate(zip(handles, sizes, strict=True), start=1):

        def check_request(request):
            assert request[:5] == bytes((CIS_REQ, 1, cis_id, 1, 1))
            max_c_to_p = int.from_bytes(request[5:7], 'little')
            max_p_to_c = int.from_bytes(request[7:9], 'little')
            assert bool(max_c_to_p & 0x8000) == framed
            assert (max_c_to_p & 0x0FFF, max_p_to_c & 0x0FFF) == (size, size)
            assert int.from_bytes(request[9:12], 'little') == interval
            assert int.from_bytes(request[12:15], 'little') == interval

        await establish_configured_cis(
            tester, link, handle, check_request=check_request
        )


async def reject_duplicate_and_modify_cis(tester):
    """Add and modify CISes in a configurable CIG and establish both.

    Initial Condition:
        The IUT is Central on an ACL to the Lower Tester and has enabled
        the CIS Host feature.

    Test Steps:
        1. Configure CIS ID 1, then add CIS ID 2 to the same CIG with
           the default non-test parameters.
        2. Modify CIS ID 2 using Table 4.97 and check its handle is stable.
        3. Send Create CIS with the first handle twice.
        4. Create each stream separately, inspect the LL_CIS_REQ, and
           complete the procedures on air.

    Expected Outcome:
        The duplicate command reports 0x12. The two configured handles
        correspond to CIS IDs 1 and 2 and establish with their respective
        original and modified SDU sizes.

    Reference:
        HCI.TS.p39, 4.15.1.3, HCI/CIS/BV-05-C, Table 4.97 and Figures 4.151–52;
        LL.TS.p28, 4.10.2.3.
    """
    link = await connect_cis_acl(tester, central=True)
    handles = []
    for cis_id, size, rtn in ((1, 251, 3), (2, 251, 3), (2, 100, 4)):
        result = await tester.command(
            _set_cig(
                cis_id=(cis_id,),
                max_sdu_c_to_p=(size,),
                max_sdu_p_to_c=(size,),
                rtn_c_to_p=(rtn,),
                rtn_p_to_c=(rtn,),
            )
        )
        assert result.cig_id == 1
        (handle,) = result.connection_handle
        if size == 100:
            assert handle == handles[-1]
        else:
            assert handle not in handles
            handles.append(handle)
    duplicate = await tester.command(
        hci.HCI_LE_Create_CIS_Command((handles[0],) * 2, (link.handle,) * 2),
        (ErrorCode.SUCCESS, INVALID),
    )
    if duplicate.status == ErrorCode.SUCCESS:
        result = await tester.event(hci.HCI_LE_CIS_Established_Event)
        assert (result.connection_handle, result.status) == (handles[0], INVALID)
    await _verify_two_configured_streams(
        tester, link, handles, sizes=(251, 100), interval=20_000, framed=False
    )


async def cigs_preserved_after_parameter_failure(tester):
    """Reject invalid CIG changes without modifying either configured CIS.

    Initial Condition:
        The IUT is Central on an ACL to the Lower Tester and has enabled
        the CIS Host feature.

    Test Steps:
        1. Set both transmit PHY masks to zero in the first Table 4.92
           configuration. Observe command failure.
        2. Apply the valid first configuration. Verify it returns two handles.
        3. Send Table 4.92's invalid second configuration for the same CIG.
        4. Create each CIS and check the control PDUs and establishment events
           against the unchanged first configuration.

    Expected Outcome:
        Both invalid commands fail. CIS IDs 1 and 2 still establish with
        framed 16-byte SDUs and 50 ms SDU intervals.

    Reference:
        HCI.TS.p39, 4.15.1.1, HCI/CIS/BI-11-C, Table 4.92 and Figure 4.148.
    """
    link = await connect_cis_acl(tester, central=True)
    initial = {
        'count': 2,
        'cis_id': (1, 2),
        'sdu_interval_c_to_p': 50_000,
        'sdu_interval_p_to_c': 50_000,
        'framing': 1,
        'max_sdu_c_to_p': (16, 16),
        'max_sdu_p_to_c': (16, 16),
        'max_transport_latency_c_to_p': 200,
        'max_transport_latency_p_to_c': 200,
        'rtn_c_to_p': (4, 4),
        'rtn_p_to_c': (4, 4),
    }
    await tester.command(_set_cig(**initial, phy_c_to_p=(0, 0)), (INVALID, UNSUPPORTED))
    result = await tester.command(_set_cig(**initial))
    assert result.cig_id == 1
    assert len(result.connection_handle) == 2
    await tester.command(
        _set_cig(
            count=3,
            cis_id=(1, 2, 3),
            sdu_interval_c_to_p=60_000,
            sdu_interval_p_to_c=60_000,
            worst_case_sca=1,
            packing=1,
            framing=0,
            max_sdu_c_to_p=(4096,) * 3,
            max_sdu_p_to_c=(4096,) * 3,
            max_transport_latency_c_to_p=250,
            max_transport_latency_p_to_c=250,
            rtn_c_to_p=(5,) * 3,
            rtn_p_to_c=(5,) * 3,
            phy_p_to_c=(0, 1, 1),
        ),
        (INVALID, UNSUPPORTED),
    )
    await _verify_two_configured_streams(
        tester,
        link,
        result.connection_handle,
        sizes=(16, 16),
        interval=50_000,
        framed=True,
    )


async def disconnect_after_rejected_cis(tester):
    """Disallow Disconnect on a CIS after the peer rejects creation.

    Initial Condition:
        The IUT is Central on an ACL connection to the Lower Tester.
        Configure the CIG with the values in Table 4.103.

    Test Steps:
        1. Set CIG Parameters and send Create CIS for the returned handle.
        2. Observe LL_CIS_REQ and send LL_REJECT_EXT_IND. Observe failure
           in LE CIS Established.
        3. Immediately send Disconnect for the unsuccessful CIS handle.

    Expected Outcome:
        Command Status, or Disconnection Complete after successful Status,
        reports 0x0C.

    Reference:
        HCI.TS.p39, 4.15.1, HCI/CIS/BI-16-C, Figure 4.162, Table 4.103.
    """
    link = await connect_cis_acl(tester, central=True)
    result = await tester.command(
        _set_cig(
            cis_id=(1,),
            framing=1,
            sdu_interval_c_to_p=50_000,
            sdu_interval_p_to_c=50_000,
            max_sdu_c_to_p=(16,),
            max_sdu_p_to_c=(16,),
            max_transport_latency_c_to_p=200,
            max_transport_latency_p_to_c=200,
            rtn_c_to_p=(4,),
            rtn_p_to_c=(4,),
        )
    )
    assert result.cig_id == 1
    (handle,) = result.connection_handle
    await tester.command(hci.HCI_LE_Create_CIS_Command((handle,), (link.handle,)))
    request = await link.control(ControlOpcode.LL_CIS_REQ)
    assert request[2] == 1
    reason = ErrorCode.CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR
    await link.exchange(
        bytes((ControlOpcode.LL_REJECT_EXT_IND, CIS_REQ, reason)), llid=LlId.CONTROL
    )
    result = await tester.event(hci.HCI_LE_CIS_Established_Event)
    assert (result.connection_handle, result.status) == (handle, reason)
    response = await tester.command(
        hci.HCI_Disconnect_Command(
            handle, ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
        ),
        (ErrorCode.SUCCESS, DISALLOWED),
    )
    if response.status == ErrorCode.SUCCESS:
        event = await tester.event(hci.HCI_Disconnection_Complete_Event)
        assert (event.connection_handle, event.status) == (handle, DISALLOWED)


async def reject_early_tx_sync(tester, *, central):
    """Reject LE Read ISO TX Sync before transmitting an SDU.

    Initial Condition:
        Establish an unframed CIS with the Section 4.10.2.3 default
        parameters. Set up the HCI input path with no SDUs queued.

    Test Case Configuration:
        BI-03 selects a Central IUT and BI-04 selects a Peripheral IUT.

    Test Steps:
        1. Exchange a CIS subevent using the Lower Tester's NULL PDU.
        2. Observe the IUT's empty PDU and issue LE Read ISO TX Sync.

    Expected Outcome:
        With no SDU on air, the command returns 0x0C. The empty packet may
        be a CIS NULL PDU or ISOAL padding.

    Source Correction:
        Core Vol 4, Part E, 7.8.96 requires 0x0C before an SDU is sent.
        A padding PDU from ISOAL has no associated SDU or sequence number
        (Vol 6, Part B, 4.5.13.3).

    Reference:
        HCI.TS.p39, 4.15.1.3, HCI/CIS/BI-03-C and BI-04-C, Table 4.96.
    """
    _, air, event = await establish_peer_cis(tester, central=central)
    handle = event.connection_handle
    await data_path(tester, handle, 0)
    packet = await air.exchange()
    assert packet.pdu[1] == 0
    assert packet.pdu[0] & 0x40 or packet.pdu[0] & 0x03 == 1
    await tester.command(hci.HCI_LE_Read_ISO_TX_Sync_Command(handle), DISALLOWED)


async def _receive_unframed_iso_data(air, data):
    for _ in range(2 * air.nse):
        packet = await air.exchange()
        if packet.pdu[1]:
            assert not packet.pdu[0] & 0x40
            assert packet.pdu[0] & 0x03 == 0
            assert packet.pdu[1:] == bytes((len(data),)) + data
            return
    raise AssertionError('CIS carried no SDU after HCI ISO input')


async def ignore_iso_reserved_header_bits(tester, *, central):
    """Ignore all RFU bits in an incoming HCI ISO Data packet.

    Initial Condition:
        Establish an unframed, transmit-only CIS with 100 ms SDU and
        ISO intervals, NSE=3, BN=FT=1, and 8-byte Max_SDU and Max_PDU
        in the active direction. Use an HCI input path.

    Test Case Configuration:
        BI-01 transmits Central to Peripheral. BI-02 transmits Peripheral
        to Central. The opposite direction has zero BN and maximum sizes.

    Test Steps:
        1. Send one complete ISO SDU with all RFU bits set in the HCI
           handle, data length and SDU length words.
        2. Receive the ISO data at the Lower Tester.

    Expected Outcome:
        The CIS carries the exact eight SDU bytes.

    Reference:
        HCI.TS.p39, 4.15.1.2, HCI/CIS/BI-01-C and BI-02-C, Tables 4.93–95.
    """
    _, air, event = await establish_peer_cis(
        tester,
        central=central,
        interval_us=100_000,
        test_nse=3,
        sdu_sizes=(8, 0) if central else (0, 8),
    )
    await data_path(tester, event.connection_handle, 0)
    data = b'ISO data'
    wire = bytearray(bytes(iso_packet(event.connection_handle, 17, data)))
    wire[2] |= 0x80
    wire[4] |= 0xC0
    wire[8] |= 0x30
    tester.send(bytes(wire))
    air.skip_elapsed()
    await _receive_unframed_iso_data(air, data)


async def unframed_tx_sync_time_offset(tester):
    """Report Time_Offset zero after unframed ISO transmission.

    Initial Condition:
        Enable the CIS Host feature. The IUT is Central on an ACL to the
        Lower Tester.

    Test Steps:
        1. Send LE Set CIG Parameters Test with the defaults of LL.TS
           4.10.2.3. Create the CIS, exchange the LL control PDUs and
           acknowledge the IUT's first CIS PDU.
        2. Read Buffer Size [v2], set up an input data path, and send an
           HCI ISO packet within the advertised size.
        3. Observe the unframed data at the Lower Tester and issue
           LE Read ISO TX Sync.

    Expected Outcome:
        The CIG setup and establishment succeed, and the transmitted SDU
        produces a LE Read ISO TX Sync result with Time_Offset zero.

    Reference:
        HCI.TS.p39, 4.15.1, HCI/CIS/BV-06-C, Figure 4.154;
        LL.TS.p28, 4.10.2.3.
    """
    _, air, event = await establish_peer_cis(tester, central=True, test_nse=3)
    handle = event.connection_handle
    buffers = await tester.command(hci.HCI_LE_Read_Buffer_Size_V2_Command())
    await data_path(tester, handle, 0)
    data = b'iso tx sync'
    wire = iso_packet(handle, 23, data)
    assert wire.data_total_length <= buffers.iso_data_packet_length
    tester.send(bytes(wire))
    await _receive_unframed_iso_data(air, data)
    sync = await tester.command(hci.HCI_LE_Read_ISO_TX_Sync_Command(handle))
    assert (sync.connection_handle, sync.packet_sequence_number, sync.time_offset) == (
        handle,
        23,
        0,
    )


async def read_tx_sync_on_receive_only_cis(tester, *, central):
    """Reject LE Read ISO TX Sync for the CIS receive-only direction.

    Initial Condition:
        Establish a unidirectional CIS. Max_SDU, Max_PDU and BN are
        zero in the IUT's transmit direction.

    Test Case Configuration:
        BV-07 selects a Central IUT and BV-08 selects a Peripheral IUT.

    Test Steps:
        1. Send LE Read ISO TX Sync for the established CIS.

    Expected Outcome:
        Command Complete returns 0x0C.

    Reference:
        HCI.TS.p39, 4.15.1.4, HCI/CIS/BV-07-C and BV-08-C, Table 4.100.
    """
    _, _, event = await establish_peer_cis(tester, central=central, receive_only=True)
    assert (event.bn_c_to_p if central else event.bn_p_to_c) == 0
    await tester.command(
        hci.HCI_LE_Read_ISO_TX_Sync_Command(event.connection_handle), DISALLOWED
    )


async def _receive_timestamped_sdu(tester, expected):
    received = bytearray()
    timestamp = None
    while len(received) < len(expected):
        packet = await tester.event(hci.HCI_IsoDataPacket)
        first = not received
        received.extend(packet.iso_sdu_fragment)
        last = len(received) == len(expected)
        assert packet.pb_flag == ((2 if last else 0) if first else (3 if last else 1))
        if first:
            assert packet.packet_sequence_number is not None
            assert packet.iso_sdu_length == len(expected)
            assert packet.packet_status_flag == 0
            assert bool(packet.ts_flag) == (packet.time_stamp is not None)
            timestamp = packet.time_stamp
        else:
            assert not packet.ts_flag
            assert packet.time_stamp is None
            assert packet.packet_sequence_number is None
            assert packet.iso_sdu_length is None
            assert packet.packet_status_flag is None
        assert expected.startswith(received)
    return timestamp


async def optional_iso_timestamps(tester, *, central):
    """Transfer a framed ISO sequence with optional HCI timestamps.

    Initial Condition:
        Establish a framed bidirectional CIS and set up both HCI paths.
        Use 246-byte SDUs, which fit in one framed CIS PDU and can
        require multiple HCI packets when timestamps are present.

    Test Case Configuration:
        BV-09 selects a Central IUT and BV-10 selects a Peripheral IUT.
        Both permit optional timestamps.

    Test Steps:
        1. Send two framed SDUs on adjacent CIS events, with an increasing
           octet count that continues across the SDU boundary.
        2. Reassemble the HCI packets and check their flags and timestamps.
        3. Send two SDUs with HCI timestamps and the same octet progression.
           Observe the framed PDUs on air.

    Expected Outcome:
        Each direction delivers all SDU bytes in order. Timestamps and
        SDU metadata are consistent with the first and later HCI packet
        boundaries. When both received SDUs are timestamped, the timestamps
        are one SDU interval apart within clock and timing tolerance.

    Reference:
        HCI.TS.p39, 4.15.1.5, HCI/CIS/BV-09-C and BV-10-C, Table 4.101;
        Core Vol 4, Part E, 5.4.5.
    """
    _, air, event = await establish_peer_cis(
        tester, central=central, test_nse=3, framed=True
    )
    handle = event.connection_handle
    await data_path(tester, handle, 0)
    await data_path(tester, handle, 1)
    buffers = await tester.command(hci.HCI_LE_Read_Buffer_Size_V2_Command())
    capacity = buffers.iso_data_packet_length - 8
    sdus = tuple(
        bytes((sequence * 246 + offset) % 256 for offset in range(246))
        for sequence in range(2)
    )
    # Drive both peer SDUs before polling the HCI queue. Polling advances the
    # same PHY clock and can otherwise jump across the next CIS anchor.
    for sdu in sdus:
        await air.exchange(framed_sdu(sdu), header=0x02 | ((air.counter & 1) << 3))
        while air.subevent:
            air.step()
    stamps = [await _receive_timestamped_sdu(tester, sdu) for sdu in sdus]
    if all(stamp is not None for stamp in stamps):
        # Each of the two advertised SCA values is at most 500 ppm.
        elapsed = (stamps[1] - stamps[0]) & 0xFFFFFFFF
        assert abs(elapsed - air.interval_us) <= air.interval_us // 1000 + 2

    nesn = 0
    previous = None
    for sequence, sdu in enumerate(sdus):
        timestamp = tester.radio.now_us & 0xFFFFFFFF
        # Leave eight bytes for the timestamp and SDU header on the first PDU.
        first, last = sdu[:capacity], sdu[capacity:]
        packet = iso_packet(handle, sequence, first, timestamp=timestamp)
        if last:
            packet.pb_flag = 0
            packet.iso_sdu_length = len(sdu)
        tester.send(bytes(packet))
        if last:
            tester.send(bytes(hci.HCI_IsoDataPacket(handle, len(last), last, 3)))
        air.skip_elapsed()
        for _ in range(3 * air.nse):
            pdu = (await air.exchange(header=0x40 | nesn)).pdu
            if not pdu[1]:
                continue
            assert pdu[0] & 0x03 == 2
            assert pdu[2:4] == bytes((2, len(sdu) + 3))
            nesn = 0 if pdu[0] & 0x08 else 0x04
            if pdu[7:] == sdu:
                break
            assert pdu[7:] == previous
        else:
            raise AssertionError('CIS did not transmit the timestamped SDU')
        previous = sdu
        while air.subevent:
            air.step()


async def reject_invalid_peripheral_commands(tester):
    """Reject invalid data path setup, Create CIS and Accept CIS at Peripheral.

    Initial Condition:
        The IUT is Peripheral on an established CIS. Configure its
        Controller-to-Host ISO data path.

    Test Steps:
        1. Repeat Setup ISO Data Path with the active CIS handle and with
           an unassigned handle.
        2. Attempt Create CIS with the active CIS and ACL handles.
        3. Attempt Accept CIS Request for the unassigned handle.

    Expected Outcome:
        Duplicate Setup ISO Data Path returns 0x0C, and the unknown-handle
        commands return 0x02. Create CIS returns one of 0x01, 0x02, 0x0B,
        or 0x0C.

    Reference:
        HCI.TS.p39, 4.15.1, HCI/CIS/BI-08-C, Figure 4.156.
    """
    link, _, event = await establish_peer_cis(tester, central=False)
    handle = event.connection_handle
    unknown_handle = 0x0EFF
    unknown = ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
    await data_path(tester, handle, 1)
    for candidate, status in ((handle, DISALLOWED), (unknown_handle, unknown)):
        await tester.command(
            hci.HCI_LE_Setup_ISO_Data_Path_Command(
                candidate, 1, 0, hci.CodingFormat(hci.CodecID.TRANSPARENT), 0, b''
            ),
            status,
        )
    await tester.command(
        hci.HCI_LE_Create_CIS_Command((handle,), (link.handle,)),
        (
            ErrorCode.UNKNOWN_HCI_COMMAND_ERROR,
            unknown,
            ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR,
            DISALLOWED,
        ),
    )
    await tester.command(hci.HCI_LE_Accept_CIS_Request_Command(unknown_handle), unknown)


async def premature_cis_request_commands(tester):
    """Reject premature data path setup and duplicate CIS decisions.

    Initial Condition:
        The IUT is Peripheral. Both versions of LE CIS Established are
        unmasked before accepting the Lower Tester's request.

    Test Steps:
        1. Send LL_CIS_REQ. Try to set up an output data path for its
           handle before accepting it.
        2. Accept the request and observe LL_CIS_RSP.
        3. Repeat Accept and send Reject before the Lower Tester sends
           LL_CIS_IND.
        4. Establish the CIS and repeat the Accept and Reject commands.

    Expected Outcome:
        The premature setup and all duplicate decisions return 0x0C.
        If CIS Established [v2] is emitted, its additional fields match
        the corresponding LL_CIS_REQ parameters.

    Source Correction:
        The Core defines Command Complete for LE Reject CIS Request,
        including errors (Vol 4, Part E, 7.8.102).

    Reference:
        HCI.TS.p39, 4.15.1, HCI/CIS/BI-06-C, Figure 4.155.
    """

    async def before_accept(handle):
        await tester.masks(le=LE_EVENT_MASK_ALL)
        await tester.command(
            hci.HCI_LE_Setup_ISO_Data_Path_Command(
                handle, 1, 0, hci.CodingFormat(hci.CodecID.TRANSPARENT), 0, b''
            ),
            DISALLOWED,
        )

    async def repeat_decision(handle):
        await tester.command(hci.HCI_LE_Accept_CIS_Request_Command(handle), DISALLOWED)
        await tester.command(
            hci.HCI_LE_Reject_CIS_Request_Command(
                handle, ErrorCode.CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR
            ),
            DISALLOWED,
        )

    _, _, event = await establish_peer_cis(
        tester,
        central=False,
        before_accept=before_accept,
        after_response=repeat_decision,
    )
    if isinstance(event, hci.HCI_LE_CIS_Established_V2_Event):
        assert (
            event.sub_interval,
            event.max_sdu_c_to_p,
            event.max_sdu_p_to_c,
            event.sdu_interval_c_to_p,
            event.sdu_interval_p_to_c,
            event.framing,
        ) == (5000, 251, 251, 20_000, 20_000, 0)
    await repeat_decision(event.connection_handle)


async def reject_authenticated_payload_timeout(tester, *, central):
    """Reject authenticated payload timeout operations on an encrypted CIS.

    Initial Condition:
        Establish an encrypted ACL and a CIS using the default Set CIG
        Parameters Test settings. The Upper Tester provides no ISO SDU.

    Test Case Configuration:
        BI-19 selects a Central IUT and BI-20 selects a Peripheral IUT.

    Test Steps:
        1. Send Read Authenticated Payload Timeout for the CIS handle.
        2. Send Write Authenticated Payload Timeout for the same handle.

    Expected Outcome:
        Both commands respond with Command Complete and status 0x0C.

    Reference:
        HCI.TS.p39, 4.15.1.7, HCI/CIS/BI-19-C and BI-20-C, Table 4.104;
        Core Vol 4, Part E, 7.3.93–94.
    """
    link = await connect_cis_acl(tester, central=central, encrypted=True)
    _, _, event = await establish_peer_cis(
        tester, central=central, link=link, test_nse=3
    )
    handle = event.connection_handle
    await tester.command(
        hci.HCI_Read_Authenticated_Payload_Timeout_Command(handle), DISALLOWED
    )
    await tester.command(
        hci.HCI_Write_Authenticated_Payload_Timeout_Command(handle, 3000), DISALLOWED
    )


async def disconnect_before_cis_establishment(tester):
    """Reject Disconnect before the Peripheral CIS is established.

    Initial Condition:
        The IUT is Peripheral with CIS Host Support enabled and CIS events
        unmasked. The Lower Tester is Central on the ACL.

    Test Steps:
        1. Send a valid LL_CIS_REQ, obtain its HCI connection handle, and
           accept the request.
        2. Observe LL_CIS_RSP and send LL_CIS_IND without sending the first
           CIS data PDU.
        3. Issue Disconnect for the pending CIS handle before the timeout.

    Expected Outcome:
        Command Status reports 0x0C.

    Reference:
        HCI.TS.p39, 4.15.1, HCI/CIS/BI-09-C, Figure 4.157.
    """

    async def disconnect(handle):
        await tester.command(
            hci.HCI_Disconnect_Command(
                handle, ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
            ),
            DISALLOWED,
        )

    await establish_peer_cis(tester, central=False, before_first_packet=disconnect)


async def inactive_cig_commands(tester, *, remove):
    """Check removal and parameter lifetime after a CIG becomes inactive.

    Initial Condition:
        The IUT is Central with one established CIS using the default CIG
        parameters.

    Test Case Configuration:
        BV-13 removes an inactive and a configurable CIG. BI-13 tries to
        change an inactive CIG's configuration.

    Test Steps:
        1. Disconnect the CIS. Require LL_CIS_TERMINATE_IND, acknowledge it,
           and observe Disconnection Complete for the CIS.
        2. For BV-13, remove the inactive CIG. Configure the same CIG ID
           and remove it before creating another CIS.
        3. For BI-13, issue Set CIG Parameters for the inactive CIG.

    Expected Outcome:
        The CIS terminates successfully. BV-13 accepts removal in both
        states. BI-13 rejects the parameter change with 0x0C.

    Reference:
        HCI.TS.p39, 4.15.1, HCI/CIS/BV-13-C and BI-13-C, Figures 4.160–161.
    """
    link, _, event = await establish_peer_cis(tester, central=True)
    handle = event.connection_handle
    reason = ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    await tester.command(hci.HCI_Disconnect_Command(handle, reason))
    terminate = await link.control(ControlOpcode.LL_CIS_TERMINATE_IND)
    assert terminate == bytes((ControlOpcode.LL_CIS_TERMINATE_IND, 1, 1, reason))
    await link.exchange()
    event = await tester.event(hci.HCI_Disconnection_Complete_Event)
    assert (event.status, event.connection_handle) == (ErrorCode.SUCCESS, handle)
    configure = _set_cig(
        cis_id=(1,),
        max_sdu_c_to_p=(251,),
        max_sdu_p_to_c=(251,),
        rtn_c_to_p=(3,),
        rtn_p_to_c=(3,),
    )
    if not remove:
        await tester.command(configure, DISALLOWED)
        return
    removed = await tester.command(hci.HCI_LE_Remove_CIG_Command(1))
    assert removed.cig_id == 1
    configured = await tester.command(configure)
    assert configured.cig_id == 1
    removed = await tester.command(hci.HCI_LE_Remove_CIG_Command(1))
    assert removed.cig_id == 1


PROCEDURES: ProcedureMap = {}

# Reuse the configured CIS after cancellation while the peer still has its request.
PROCEDURES |= procedure_cases(
    central_cis_non_test_command,
    {'HCI/CIS/BV-19-C': ('Connected Isochronous Stream Using Non-Test Command', {})},
)

# Check invalid wire parameters as well as valid fields that cannot fit a CIG.
PROCEDURES |= procedure_cases(
    invalid_cig_parameters,
    {'HCI/CIS/BI-05-C': ('LE Set CIG Parameters, Reject Invalid Parameters', {})},
)

# Keep the ACL alive while the Upper Tester leaves the CIS request unanswered.
PROCEDURES |= procedure_cases(
    cis_request_timeout,
    {'HCI/CIS/BI-07-C': ('LE CIS Request Timeout', {})},
)

# Allow the parameter error at Set CIG Parameters, Create CIS or establishment.
PROCEDURES |= procedure_cases(
    invalid_transport_latency,
    {'HCI/CIS/BI-12-C': ('CIS Setup Procedure, Invalid Transport Latency', {})},
)

# Check the optional CIS settings declared as unsupported in this selection.
PROCEDURES |= procedure_cases(
    unsupported_test_burst_or_flush,
    {
        'HCI/CIS/BI-14-C': ('Unsupported Burst Number', {'bn': 2, 'ft': 1}),
        'HCI/CIS/BI-15-C': ('Unsupported Flush Timeout', {'bn': 1, 'ft': 2}),
    },
)
PROCEDURES |= procedure_cases(
    unsegmented_framing_unsupported,
    {'HCI/CIS/BI-18-C': ('Framed Unsegmented Mode Unsupported', {})},
)

# Check CIG's configurable-state data paths and failed-creation handle lifetime.
PROCEDURES |= procedure_cases(
    reject_max_sdu_in_wrong_direction,
    {'HCI/CIS/BI-10-C': ('Reject Max_SDU in Wrong Direction', {})},
)
PROCEDURES |= procedure_cases(
    disconnect_after_rejected_cis,
    {'HCI/CIS/BI-16-C': ('Disconnect Immediately After Failed Create CIS', {})},
)

# Check handles and configurations across successful and rejected CIG updates.
PROCEDURES |= procedure_cases(
    reject_duplicate_and_modify_cis,
    {'HCI/CIS/BV-05-C': ('Add or Modify CIS', {})},
)
PROCEDURES |= procedure_cases(
    cigs_preserved_after_parameter_failure,
    {'HCI/CIS/BI-11-C': ('CIG Parameters Failure Behavior', {})},
)

# Exercise the two independent Read ISO TX Sync preconditions in each role.
PROCEDURES |= procedure_cases(
    reject_early_tx_sync,
    {
        'HCI/CIS/BI-03-C': (
            'Reject Early Read ISO TX Sync, Central',
            {'central': True},
        ),
        'HCI/CIS/BI-04-C': (
            'Reject Early Read ISO TX Sync, Peripheral',
            {'central': False},
        ),
    },
)
PROCEDURES |= procedure_cases(
    read_tx_sync_on_receive_only_cis,
    {
        'HCI/CIS/BV-07-C': (
            'Reject Read ISO TX Sync for Receive Only, Central',
            {'central': True},
        ),
        'HCI/CIS/BV-08-C': (
            'Reject Read ISO TX Sync for Receive Only, Peripheral',
            {'central': False},
        ),
    },
)

# Check RFU masking at H4, then compare the actual CIS payload in both roles.
PROCEDURES |= procedure_cases(
    ignore_iso_reserved_header_bits,
    {
        'HCI/CIS/BI-01-C': ('Ignore HCI ISO RFU Bits, Central', {'central': True}),
        'HCI/CIS/BI-02-C': ('Ignore HCI ISO RFU Bits, Peripheral', {'central': False}),
    },
)

# Read ISO TX Sync after an observed unframed transmission.
PROCEDURES |= procedure_cases(
    unframed_tx_sync_time_offset,
    {'HCI/CIS/BV-06-C': ('Unframed CIS Time_Offset', {})},
)

# Check repeated and unknown-handle operations on a Peripheral CIS.
PROCEDURES |= procedure_cases(
    reject_invalid_peripheral_commands,
    {'HCI/CIS/BI-08-C': ('Peripheral CIS, Reject Invalid Commands', {})},
)

# Verify optional timestamps and the metadata on both HCI ISO boundaries.
PROCEDURES |= procedure_cases(
    optional_iso_timestamps,
    {
        'HCI/CIS/BV-09-C': ('ISO Time_Stamp Optional, Central', {'central': True}),
        'HCI/CIS/BV-10-C': ('ISO Time_Stamp Optional, Peripheral', {'central': False}),
    },
)

# Check the pending state before and after the Lower Tester's indication.
PROCEDURES |= procedure_cases(
    premature_cis_request_commands,
    {'HCI/CIS/BI-06-C': ('Premature CIS Data Path and Duplicate Decisions', {})},
)
PROCEDURES |= procedure_cases(
    disconnect_before_cis_establishment,
    {'HCI/CIS/BI-09-C': ('Reject Disconnect Before CIS Establishment', {})},
)

# Verify removal of inactive/configurable CIGs and reject inactive changes.
PROCEDURES |= procedure_cases(
    inactive_cig_commands,
    {
        'HCI/CIS/BV-13-C': (
            'Removal of Configurable and Inactive CIG',
            {'remove': True},
        ),
        'HCI/CIS/BI-13-C': (
            'Reject Parameter Change of Inactive CIG',
            {'remove': False},
        ),
    },
)

# Exercise the ACL-only HCI setting against an encrypted CIS in either role.
PROCEDURES |= procedure_cases(
    reject_authenticated_payload_timeout,
    {
        'HCI/CIS/BI-19-C': (
            'CIS Authenticated Payload Timeout, Central',
            {'central': True},
        ),
        'HCI/CIS/BI-20-C': (
            'CIS Authenticated Payload Timeout, Peripheral',
            {'central': False},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES, radio_cases=PROCEDURES, radio=True),
)
async def test_connected_isochronous(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
