# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.3.7: Periodic Advertising Sync Transfer procedures."""

import pytest

from bscontroller import hci
from bscontroller.hci import HCI_LE_PERIODIC_SYNC_TIMEOUT_MIN
from bscontroller.linklayer import (
    LL_EVENT_COUNTER_MASK,
    LL_PERIODIC_INTERVAL_UNIT_US,
    PDU_ADV_ADI_DID_BITS,
    PDU_ADV_TXADD_MASK,
    PDU_ADV_TYPE_MASK,
    PDU_DATA_LLID_MASK,
    AdvertisingPduType,
    ControlOpcode,
    LlId,
)
from bscontroller.radio.base import Phy
from tests.qualification import privacy as privacy_peer
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.ll.connection_test import _change_phy
from tests.qualification.peer import establish, exchange_features
from tests.qualification.periodic import PeriodicTrain
from tests.qualification.tester import (
    DUT_ADDRESS,
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
    decode_extended,
    extended_enable,
    extended_parameters,
    follow_auxiliary,
    scan_parameters,
)
from tests.qualification.transfer import (
    COUNTER_SPAN_US,
    PREPARATION_US,
    RECEIVED,
    SERVICE_DATA,
    TRANSFER_OFFSET_MAX_US,
    SyncIndication,
    TransferSession,
    check_received,
    lower_train,
    phy_bit,
    signed,
    train_anchor,
    transferred_anchor,
)

Opcode = ControlOpcode
ErrorCode = hci.HCI_ErrorCode
TransferParameters = (
    hci.HCI_LE_Set_Periodic_Advertising_Sync_Transfer_Parameters_Command
)
DefaultParameters = (
    hci.HCI_LE_Set_Default_Periodic_Advertising_Sync_Transfer_Parameters_Command
)
# The Lower Tester's periodic advertiser, distinct from its ACL addresses.
ADVERTISER = hci.Address('00:5A:A5:00:5A:A5', hci.AddressType.PUBLIC_DEVICE)
# The advertiser's public identity in the IUT resolving list, and its IRKs.
IDENTITY = hci.Address('00:A5:5A:00:A5:5A', hci.AddressType.PUBLIC_DEVICE)
PEER_IRK = privacy_peer.PEER_IRK
LOCAL_IRK = privacy_peer.LOCAL_IRK
OTHER_IRK = bytes.fromhex('0f1e2d3c4b5a69788796a5b4c3d2e1f0')
# An IRK the IUT does not hold: its RPAs stay unresolved.
UNKNOWN_IRK = bytes.fromhex('a0a1a2a3a4a5a6a7a8a9aaabacadaeaf')
STATIC_ADVERTISER = hci.Address('C3:5A:A5:C3:5A:A5', hci.AddressType.RANDOM_DEVICE)
# The Lower Tester's random static identity in LL.TS 4.3.7.48.
STATIC_IDENTITY = hci.Address('D5:A5:5A:D5:A5:5A', hci.AddressType.RANDOM_DEVICE)
# Optional procedures of the independent Lower Tester (LL.TS 4.1.6.7.3).
PEER_FEATURES = (
    hci.LeFeatureMask.LE_ENCRYPTION
    | hci.LeFeatureMask.EXTENDED_REJECT_INDICATION
    | hci.LeFeatureMask.PERIPHERAL_INITIATED_FEATURE_EXCHANGE
    | hci.LeFeatureMask.LE_DATA_PACKET_LENGTH_EXTENSION
    | hci.LeFeatureMask.LE_2M_PHY
    | hci.LeFeatureMask.LE_CODED_PHY
    | hci.LeFeatureMask.LE_EXTENDED_ADVERTISING
    | hci.LeFeatureMask.LE_PERIODIC_ADVERTISING
    | hci.LeFeatureMask.CHANNEL_SELECTION_ALGORITHM_2
    | hci.LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_SENDER
    | hci.LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_RECIPIENT
    | hci.LeFeatureMask.PERIODIC_ADVERTISING_ADI_SUPPORT
)
# Selected observation lengths: the source's report counts, in train events.
REPORTS = 10
# A selected bound on connection events before an expected event or PDU.
RESPONSE_EVENTS = 64


def _timeout(interval, factor):
    """Sync_Timeout of ``factor`` periodic intervals, in 10-ms units.

    A selected floor applies the HCI minimum of 100 ms to short trains
    (Core Vol 4, Part E, 7.8.91).
    """
    return max(
        HCI_LE_PERIODIC_SYNC_TIMEOUT_MIN,
        -(-factor * interval * LL_PERIODIC_INTERVAL_UNIT_US // 10_000),
    )


async def _link(tester, *, central, phy=Phy.LE_1M, interval=24, timeout=100):
    """Initial Condition: connected on the selected PHY, LE 1M establishment.

    Extended commands establish the link: after a legacy advertising command,
    the transfer-mode commands are Command Disallowed (Core Vol 4, Part E,
    3.1.1).
    """
    link = await establish(
        tester,
        central=central,
        extended=True,
        interval=interval,
        timeout=timeout,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    if phy != Phy.LE_1M:
        await exchange_features(link, PEER_FEATURES)
        # A Central IUT answers LL_PHY_REQ with LL_PHY_UPDATE_IND, not a response
        # the remote form expects; its Host starts the procedure instead.
        await _change_phy(tester, link, phy, local=central)
    return link


async def _disconnect(tester, link):
    """The Lower Tester terminates; the IUT acknowledges and reports it.

    The termination is served on the connection grid, so a long connection
    interval cannot delay the acknowledgement past the HCI observation.
    """
    reason = ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    session = TransferSession(tester, link)
    await session.send_control(bytes([Opcode.LL_TERMINATE_IND, reason]))
    event = await tester.event(hci.HCI_Disconnection_Complete_Event)
    assert (event.connection_handle, event.reason) == (link.handle, reason)


async def _transfer_mode(session, mode, *, skip=0, timeout=None):
    train = session.train
    await session.command(
        TransferParameters(
            session.link.handle,
            mode,
            skip,
            _timeout(train.interval, 3) if timeout is None else timeout,
            0,
        )
    )


async def _received(session, *, sid, address=ADVERTISER, **expected):
    event = await session.until(
        lambda item: isinstance(item, RECEIVED), events=RESPONSE_EVENTS
    )
    assert event is not None, 'no Periodic Advertising Sync Transfer Received'
    check_received(
        event, session.link, session.train, sid=sid, address=address, **expected
    )
    return event


async def _reports(session, handle, count=REPORTS, *, events=None):
    """Serve the link until ``count`` reports with ``handle``; return their data."""
    data = []
    limit = events or (count + 4) * max(
        1, session.train.interval * LL_PERIODIC_INTERVAL_UNIT_US // session.interval_us
    )
    for _ in range(limit):
        data.extend(report.data for report in session.reports(handle))
        if len(data) >= count:
            return data
        await session.step()
    data.extend(report.data for report in session.reports(handle))
    assert len(data) >= count, f'{len(data)} periodic reports, expected {count}'
    return data


async def _no_reports(session, intervals=REPORTS):
    await session.run_for(
        intervals * session.train.interval * LL_PERIODIC_INTERVAL_UNIT_US
    )
    assert not session.reports(), 'unexpected Periodic Advertising Report'


async def accepting_different_phys(
    tester, *, central, connection_phy, primary_phy, secondary_phy, advertiser_list
):
    """Periodic Advertising Sync Transfer Procedure, Accepting – Different PHYs.

    Initial Condition:
        CEN/PER: The IUT is not scanning, synchronized or connected. Table
        4.3-75 selects the role, connection PHY and periodic PHY.

    Test Case Configuration:
        Public advertiser address, 30-ms connection interval and a 120-ms
        periodic interval; the train lies between connection events.

    Test Steps:
        1. Connect on LE 1M; for an LE 2M or LE Coded connection PHY, complete
           the Lower Tester's feature exchange and PHY Update procedure.
        2. Start periodic advertising on the Secondary ADV PHY. The optional
           extended advertising is not started, so the Primary ADV PHY has
           no transmissions.
        3. Set the transfer mode: Mode 0x02, Skip 0, Sync_Timeout three
           periodic intervals.
        4. Send LL_PERIODIC_SYNC_IND with nonzero ID, connEventCount of its
           event, SyncInfo for the next train event, SID 5, public AType, RFU
           000, the periodic PHY bit and the advertiser address.
        5. Alternative 5A: receive Sync Transfer Received with success and the
           PDU's fields, then at least ten periodic reports.
        6. Terminate the sync and require success.
        7. Stop the train.
        8. Without the advertiser-list row, skip to Step 12.
        9. Add a different advertiser to the Periodic Advertiser List.
        10. Repeat Steps 2-7: the list is ignored.
        11. Clear the Periodic Advertiser List.
        12. Disconnect.

    Expected Outcome:
        Pass verdict:
            CEN/PER: In each case, the IUT accepts the transfer, synchronizes on
            the periodic PHY and reports. No v2 event is generated.

    Reference:
        LL.TS.p28.pdf, 4.3.7.4, Table 4.3-75, Steps 1-12; PDF 908-911,
        printed 4.3-435..438. Source [13] 4.6.24.
    """
    link = await _link(tester, central=central, phy=connection_phy)
    del primary_phy
    rounds = (False, True) if advertiser_list else (False,)
    for listed in rounds:
        if listed:
            await tester.command(
                hci.HCI_LE_Add_Device_To_Periodic_Advertiser_List_Command(
                    hci.AddressType.PUBLIC_DEVICE, LOWER_PERIPHERAL_PUBLIC_ADDRESS, 5
                )
            )
        session = TransferSession(tester, link, lower_train(link, phy=secondary_phy))
        await _transfer_mode(session, 0x02)
        await session.send_control(session.indication(address=ADVERTISER))
        event = await _received(session, sid=5)
        await _reports(session, event.sync_handle)
        assert not session.transfers()
        await session.command(
            hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(event.sync_handle)
        )
        session.mode = 'off'
        if listed:
            await tester.command(hci.HCI_LE_Clear_Periodic_Advertiser_List_Command())
    await _disconnect(tester, link)


async def _wait_until(session, predicate, *, events=RESPONSE_EVENTS * 4):
    """Serve connection events until ``predicate(session)`` holds at an event."""
    for _ in range(events):
        session.catch_up()
        if predicate(session):
            return
        await session.step()
    raise AssertionError('selected transfer timing did not occur')


def _until_train(session, minimum_us, maximum_us):
    """The next train event lies this far after the next connection anchor."""
    return lambda current: (
        minimum_us <= current.train.anchor - current.link.anchor <= maximum_us
    )


def _nearest_event(session, counter=None):
    """The nearest connection event whose offset can reach the train event."""
    train = session.train
    counter = train.counter if counter is None else counter
    delay = train_anchor(train, counter) - session.link.anchor
    return max(0, -(-(delay - TRANSFER_OFFSET_MAX_US) // session.interval_us))


def _check_spacing(counters, step, sets, *, omissions=2):
    """Reported events are at most ``step`` apart, with selected omissions."""
    gaps = [later - earlier for earlier, later in zip(counters, counters[1:])]
    assert all(gap > 0 for gap in gaps), counters
    assert sum(gap > step for gap in gaps) <= omissions, counters
    assert counters[-1] - counters[0] >= (sets - 1) * step, counters


async def accepting_skipping_events(tester, *, central):
    """Periodic Advertising Sync Transfer Procedure, Accepting – Skipping Events.

    Initial Condition:
        CEN/PER: The IUT is not scanning, synchronized or connected.

    Test Case Configuration:
        Each round connects on LE 1M with a 500-ms interval and 6-s
        supervision timeout. The 6-s periodic interval exceeds the 4.9149-s
        largest SyncInfo offset plus two connection intervals.

    Test Steps:
        1. Connect on LE 1M. Repeat Steps 2-8 for the rounds of Table 4.3-77:
           Skip 0x0000 for 30 sets, 0x0001 for 30 and 0x0013 for 3.
        2. Start a train whose data carries its paEventCounter.
        3. Set Mode 0x02, the round's Skip and Sync_Timeout (Skip + 3) periodic
           intervals.
        4. Send LL_PERIODIC_SYNC_IND with connEventCount the nearest event whose
           offset reaches the next train event, public AType, LE 1M PHY.
        5. Alternative 5A: receive Sync Transfer Received with success; then
           reports at least once per (1 + Skip) events for the selected sets.
        6. Terminate the sync.
        7. Stop the train.
        8. Disconnect, and reconnect for the next round.

    Expected Outcome:
        Pass verdict:
            CEN/PER: Reported event numbers are no more than (1 + Skip) apart,
            with at most two omissions in the round.

    Reference:
        LL.TS.p28.pdf, 4.3.7.5, Tables 4.3-76/77, Steps 1-8; PDF 911-914,
        printed 4.3-438..441. Source [13] 4.6.24.
    """
    for skip, sets in ((0x0000, 30), (0x0001, 30), (0x0013, 3)):
        link = await _link(tester, central=central, interval=400, timeout=600)
        session = TransferSession(tester, link, lower_train(link, interval=4800))
        await _transfer_mode(session, 0x02, skip=skip, timeout=_timeout(4800, skip + 3))
        offset = _nearest_event(session)
        await session.send_control(
            session.indication(event_offset=offset, address=ADVERTISER)
        )
        event = await _received(session, sid=5)
        counters = []
        span = sets * (skip + 1)
        while not counters or counters[-1] - counters[0] < span - 1:
            await session.step()
            counters.extend(
                int.from_bytes(report.data[-2:], 'little')
                for report in session.reports(event.sync_handle)
            )
            assert session.train.counter - session.sent[0] <= span + 3 * (skip + 1), (
                'reports stopped'
            )
        _check_spacing(counters, skip + 1, sets)
        await session.command(
            hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(event.sync_handle)
        )
        await _disconnect(tester, link)


async def accepting_already_synchronized(tester, *, central):
    """Periodic Advertising Sync Transfer Procedure, Accepting – Already
    Synchronized.

    Initial Condition:
        CEN/PER: The IUT is not scanning, synchronized or connected.

    Test Steps:
        1. Connect on LE 1M with a 30-ms interval.
        2. Start a 120-ms periodic train on LE 1M.
        3. Set Mode 0x02, Skip 0 and Sync_Timeout three periodic intervals.
        4. Send LL_PERIODIC_SYNC_IND for the train, LE 1M PHY.
        5. Alternative 5A: receive Sync Transfer Received with success, then
           at least ten periodic reports.
        6. Repeat Step 4 twice: no further transfer event.
        7. Receive at least ten more reports with the same Sync_Handle.
        8. Terminate the sync.
        9. Stop the train.
        10. Disconnect.

    Expected Outcome:
        Pass verdict:
            CEN/PER: No second Sync Transfer Received event for the same
            train; reports continue with the same Sync_Handle.

    Source Correction:
        Step 7A.1 again expects a Sync Transfer Received event, contradicting
        Step 6 and the verdict. Follow the verdict: Step 7 checks only reports.

    Reference:
        LL.TS.p28.pdf, 4.3.7.6, Table 4.3-78, Steps 1-10; PDF 914-918,
        printed 4.3-441..445. Source [13] 4.6.24; Core Vol 4, Part E,
        7.7.65.24 Note.
    """
    link = await _link(tester, central=central)
    session = TransferSession(tester, link, lower_train(link))
    await _transfer_mode(session, 0x02)
    await session.send_control(session.indication(address=ADVERTISER))
    event = await _received(session, sid=5)
    await _reports(session, event.sync_handle)
    for _ in range(2):
        await session.send_control(session.indication(address=ADVERTISER))
        await session.run(4)
        assert not session.transfers(), 'second Sync Transfer Received event'
    await _reports(session, event.sync_handle)
    assert not session.transfers()
    await session.command(
        hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(event.sync_handle)
    )
    session.mode = 'off'
    await _disconnect(tester, link)


async def _extreme_round(session, distance):
    """Choose connEventCount, SyncInfo event and lastPaEventCounter (4.3-80)."""
    train, interval_us = session.train, session.interval_us
    if distance == 'future-minimum':
        if train.interval * LL_PERIODIC_INTERVAL_UNIT_US > interval_us:
            await _wait_until(
                session,
                _until_train(
                    session, interval_us, interval_us + TRANSFER_OFFSET_MAX_US // 2
                ),
            )
        session.catch_up()
        start = session.link.anchor + interval_us
        events = -(
            -(start - train.anchor) // (train.interval * LL_PERIODIC_INTERVAL_UNIT_US)
        )
        counter = (train.counter + max(0, events)) & LL_EVENT_COUNTER_MASK
        return 1, counter, None
    if distance == 'future-maximum':
        period = train.interval * LL_PERIODIC_INTERVAL_UNIT_US
        await _wait_until(
            session, _until_train(session, period - 2 * interval_us, period)
        )
        delay = train.anchor - session.link.anchor
        return delay // interval_us, train.counter, None
    if distance == 'past-minimum':
        await _wait_until(
            session,
            _until_train(session, interval_us, TRANSFER_OFFSET_MAX_US - interval_us),
        )
        return -1, train.counter, None
    # Maximum distance in the past: the earliest SyncInfo event lastPaEventCounter
    # allows, and the earliest connection event whose offset reaches it.
    await _wait_until(session, lambda current: len(current.sent) >= 2)
    last = session.sent[-1]
    period = train.interval * LL_PERIODIC_INTERVAL_UNIT_US
    back = max(1, COUNTER_SPAN_US // period)
    counter = (last - back) & LL_EVENT_COUNTER_MASK
    session.catch_up()
    target = train_anchor(train, counter)
    events = -(-(target - TRANSFER_OFFSET_MAX_US - session.link.anchor) // interval_us)
    return events, counter, last


async def accepting_extreme_timings(tester, *, central):
    """Periodic Advertising Sync Transfer Procedure, Accepting – Extreme Timings.

    Initial Condition:
        CEN/PER: The IUT is not scanning, synchronized or connected.

    Test Case Configuration:
        Rounds 1-4 use a 500-ms connection interval and a 6-s periodic
        interval, above the 4.9149-s largest offset plus two connection
        intervals. Rounds 5-6 use a 30-ms connection interval and a 7.5-ms
        periodic interval, below half the connection interval.

    Test Steps:
        1. Connect on LE 1M. Repeat Steps 2-8 for each round of Table 4.3-80.
        2. Start the train with an advertiser address different from the ACL
           addresses.
        3. Set Mode 0x02, Skip 0 and Sync_Timeout three periodic intervals,
           with the HCI minimum for the short train.
        4. Send LL_PERIODIC_SYNC_IND with connEventCount and SyncInfo at the
           round's distance: the nearest or furthest event in the future or
           past whose offset reaches a train event. The maximum past uses the
           earliest event lastPaEventCounter permits: one before the last
           transmitted event, or 5 s before it for the short train.
        5. Alternative 5A: receive Sync Transfer Received with success, then
           at least ten periodic reports.
        6. Terminate the sync.
        7. Stop the train.
        8. Disconnect, and reconnect for the next round.

    Expected Outcome:
        Pass verdict:
            CEN/PER: In each round, the IUT synchronizes and reports.

    Reference:
        LL.TS.p28.pdf, 4.3.7.7, Tables 4.3-79/80, Steps 1-8 and the
        connEventCount note; PDF 918-921, printed 4.3-445..448. Source [13]
        4.6.24; Core Vol 6, Part B, 2.4.2.27.
    """
    for interval, period, distance in (
        (400, 4800, 'future-minimum'),
        (400, 4800, 'future-maximum'),
        (400, 4800, 'past-minimum'),
        (400, 4800, 'past-maximum'),
        (24, 6, 'past-maximum'),
        (24, 6, 'future-minimum'),
    ):
        tester.output.write(f'ROUND interval={interval} period={period} {distance}\n')
        link = await _link(tester, central=central, interval=interval, timeout=600)
        session = TransferSession(tester, link, lower_train(link, interval=period))
        await _transfer_mode(session, 0x02)
        events, counter, last = await _extreme_round(session, distance)
        await session.send_control(
            session.indication(
                event_offset=events, counter=counter, last=last, address=ADVERTISER
            )
        )
        event = await _received(session, sid=5)
        await _reports(session, event.sync_handle)
        await session.command(
            hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(event.sync_handle)
        )
        session.mode = 'off'
        await _disconnect(tester, link)


async def accepting_synchronization_failure(tester, *, central):
    """Periodic Advertising Sync Transfer Procedure, Accepting – Synchronization
    Failure.

    Initial Condition:
        CEN/PER: The IUT is not scanning, synchronized or connected.

    Test Steps:
        1. Connect on LE 1M with a 30-ms interval; the periodic interval is
           three connection intervals.
        2. Set Mode 0x02, Skip 0 and Sync_Timeout twelve periodic intervals.
        3. Send LL_PERIODIC_SYNC_IND with valid SyncInfo and connEventCount,
           public AType, LE 1M PHY and a public AdvA.
        4. After six periodic intervals from the first expected event, receive
           Sync Transfer Received with status 0x3E.
        5. From the seventh event, send AUX_SYNC_IND as the PDU describes.

    Expected Outcome:
        Pass verdict:
            CEN/PER: The failure follows six missed periodic events.
        Fail verdict:
            CEN/PER: The IUT sends a Periodic Advertising Report.

    Coverage Note:
        The event is required after the sixth window opens and before the
        seventh event plus a selected two connection intervals. Reports are
        then observed for ten periodic intervals.

    Reference:
        LL.TS.p28.pdf, 4.3.7.8, Table 4.3-81, Steps 1-5; PDF 921-923,
        printed 4.3-448..450. Source [13] 4.6.24; Core Vol 6, Part B, 4.4.5.1.
    """
    link = await _link(tester, central=central)
    session = TransferSession(
        tester, link, lower_train(link, interval=72), mode='silent'
    )
    await _transfer_mode(session, 0x02, timeout=_timeout(72, 12))
    await session.send_control(session.indication(address=ADVERTISER))
    session.catch_up()
    period = 72 * LL_PERIODIC_INTERVAL_UNIT_US
    first = session.train.anchor
    while session.train.anchor < session.now_us:
        first += period
    event = await _received(
        session, sid=5, status=ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR
    )
    assert first + 5 * period <= session.now_us, 'failure before six periodic events'
    assert session.now_us <= first + 6 * period + 2 * session.interval_us
    del event
    session.mode = 'transmit'
    await _no_reports(session)
    assert session.sent and not session.transfers()
    session.mode = 'off'
    await _disconnect(tester, link)


async def accepting_mode_change(tester, *, central):
    """Periodic Advertising Sync Transfer Procedure, Accepting, Changing Transfer
    Mode During Synchronization.

    Initial Condition:
        CEN/PER: The IUT is not scanning, synchronized or connected.

    Test Steps:
        1. Start a 600-ms periodic train on LE 1M.
        2. Connect on LE 1M with a 30-ms interval.
        3. Set Mode 0x01 (reports initially disabled), Skip 0 and Sync_Timeout
           three periodic intervals.
        4. Just after a train event, send LL_PERIODIC_SYNC_IND for the train
           and require its acknowledgement.
        5. Before the next train event, set Mode 0x02.
        6. Receive Sync Transfer Received with success.
        7. Receive no Periodic Advertising Report for ten periodic intervals.

    Expected Outcome:
        Pass verdict:
            CEN/PER: The IUT synchronizes with the mode in force when it
            received the PDU.

    Coverage Note:
        The train starts after the connection; its timing satisfies the
        Step 4-6 requirement either way.

    Reference:
        LL.TS.p28.pdf, 4.3.7.10, Table 4.3-84, Steps 1-7; PDF 931-933,
        printed 4.3-458..460. Source [13] 4.6.24; Core Vol 4, Part E, 7.8.91.
    """
    link = await _link(tester, central=central)
    session = TransferSession(tester, link, lower_train(link, interval=480))
    await _transfer_mode(session, 0x01)
    period = 480 * LL_PERIODIC_INTERVAL_UNIT_US
    await _wait_until(
        session, _until_train(session, period - 4 * session.interval_us, period)
    )
    await session.send_control(session.indication(address=ADVERTISER))
    await _transfer_mode(session, 0x02)
    assert session.train.anchor > session.now_us, 'mode changed after the train'
    await _received(session, sid=5)
    await _no_reports(session)
    session.mode = 'off'
    await _disconnect(tester, link)


async def _receive_enable(session, handle, enable):
    await session.command(
        hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command(handle, enable)
    )
    # Reports completed before the command took effect are not under test.
    session.reports()


async def _toggle_reports(session, handle):
    """Steps 16-20: disable, observe, enable, observe; twice."""
    for _ in range(2):
        await _receive_enable(session, handle, 0)
        await _no_reports(session)
        await _receive_enable(session, handle, 1)
        await _reports(session, handle)


async def accepting_modes_and_addresses(tester, *, central):
    """Periodic Advertising Sync Transfer Procedure, Accepting – Different Modes
    and Addresses.

    Initial Condition:
        CEN/PER: The IUT is not scanning, synchronized or connected. Its
        resolving list holds the IRK of a public identity for the advertiser's
        RPA. The default transfer parameters have not been set since reset.

    Test Steps:
        1. Connect on LE 1M. Repeat Steps 2-22 for each round of Table 4.3-83.
        2. Start a 120-ms train with the round's SID.
        3. Set the round's Default Mode, Skip 0 and Sync_Timeout 30 intervals.
        4. For a New connection round, disconnect and reconnect.
        5. Set the round's Specific Mode for the connection, Skip 0 and
           Sync_Timeout 30 intervals.
        6. Send LL_PERIODIC_SYNC_IND with the round's SID and public AdvA.
        7. For Mode 0x00, receive no transfer event or report for ten
           intervals, then skip to Step 22.
        8. Receive Sync Transfer Received [v1] with success.
        9. For Mode 0x01, skip to Step 13.
        10. Alternative 10A: receive at least ten reports.
        11. Set Mode 0x01 for the connection.
        12. Alternative 12A: receive at least ten more reports; skip to 16.
        13. Receive no report for ten intervals.
        14. Set Mode 0x02 for the connection.
        15. Receive no report for ten intervals.
        16. Disable reports with Receive Enable 0x00.
        17. Receive no report for ten intervals.
        18. Enable reports with Receive Enable 0x01.
        19. Alternative 19A: receive at least ten reports.
        20. Repeat Steps 16-19.
        21. Terminate the sync.
        22. Stop the train.
        23. Repeat Steps 2-10 with round 6 values and a resolvable AdvA:
            the event reports type 0x02 and the public identity.
        24. Terminate the sync.
        25. Repeat Steps 2-10 with round 6 values and an unresolvable RPA:
            the event reports type 0x01 and that RPA.
        26. Terminate the sync.
        27. Disconnect.

    Expected Outcome:
        Pass verdict:
            CEN/PER: The IUT synchronizes only in modes 0x01-0x02 of the
            connection, uses the default only for a later connection, and
            reports only when the mode or Receive Enable enables reports.

    Source Correction:
        Step 6 names the SID of Step 3; the SID is selected in Step 2. Steps
        12A.1 and 19A.1 again expect Sync Transfer Received for a train
        already synchronized, contradicting 7.7.65.24 and the verdict; they
        check reports only. Step 11's "values specified in Step 5" use Skip 0
        and the 30-interval timeout in round 3, which has no Step 5.

    Reference:
        LL.TS.p28.pdf, 4.3.7.9, Tables 4.3-82/83, Steps 1-27; PDF 923-931,
        printed 4.3-450..458. Source [13] 4.6.24; Core Vol 4, Part E,
        7.8.88, 7.8.91, 7.8.92.
    """
    timeout = _timeout(96, 30)
    await privacy_peer.add_peer(
        tester, identity=IDENTITY, peer_irk=PEER_IRK, local_irk=bytes(16)
    )
    link = await _link(tester, central=central)
    rounds = (
        (None, False, None, 0x00),
        (0x02, False, None, 0x00),
        (None, True, None, 0x02),
        (None, False, 0x00, 0x00),
        (None, False, 0x01, 0x01),
        (None, False, 0x02, 0x02),
    )
    for sid, (default, new, specific, mode) in enumerate(rounds, 1):
        tester.output.write(f'ROUND {sid} mode=0x{mode:02x}\n')
        if default is not None:
            await tester.command(DefaultParameters(default, 0, timeout, 0))
        if new:
            await _disconnect(tester, link)
            link = await _link(tester, central=central)
        session = TransferSession(tester, link, lower_train(link))
        if specific is not None:
            await session.command(
                TransferParameters(link.handle, specific, 0, timeout, 0)
            )
        await session.send_control(session.indication(sid=sid, address=ADVERTISER))
        if mode == 0x00:
            await _no_reports(session)
            assert not session.transfers(), 'transfer event in Mode 0x00'
            continue
        event = await _received(session, sid=sid)
        handle = event.sync_handle
        if mode == 0x02:
            await _reports(session, handle)
            await session.command(TransferParameters(link.handle, 0x01, 0, timeout, 0))
            await _reports(session, handle)
        else:
            await _no_reports(session)
            await session.command(TransferParameters(link.handle, 0x02, 0, timeout, 0))
            await _no_reports(session)
        await _toggle_reports(session, handle)
        assert not session.transfers(), 'second transfer event for the train'
        await session.command(
            hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle)
        )
        session.mode = 'off'
    await session.command(TransferParameters(link.handle, 0x02, 0, timeout, 0))
    for sid, address, expected, kind in (
        (7, privacy_peer.private_address(PEER_IRK), IDENTITY, 0x02),
        (8, privacy_peer.private_address(OTHER_IRK), None, 0x01),
    ):
        tester.output.write(f'ROUND privacy sid={sid}\n')
        session = TransferSession(tester, link, lower_train(link))
        await session.send_control(session.indication(sid=sid, address=address))
        event = await _received(
            session, sid=sid, address=expected or address, address_type=kind
        )
        await _reports(session, event.sync_handle)
        await session.command(
            hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(event.sync_handle)
        )
        session.mode = 'off'
    await _disconnect(tester, link)


def _did_session(session, sid):
    """Carry ADI SID/DID in every AUX_SYNC_IND; ``did`` changes the data identity."""
    session.did = 0x123
    session.adi = lambda counter: sid << PDU_ADV_ADI_DID_BITS | session.did
    # Identical data in every event: only the ADI tells duplicates apart.
    session.data = lambda counter: b'duplicate'
    return session


async def _single_report(session, handle, *, warn):
    """One report, then ten intervals in which repeats are warned or forbidden."""
    reports = await _reports(session, handle, 1)
    await session.run_for(10 * session.train.interval * LL_PERIODIC_INTERVAL_UNIT_US)
    extra = len(reports) - 1 + len(session.reports(handle))
    if extra and warn:
        session.tester.output.write(f'WARNING {extra} duplicate periodic reports\n')
    assert warn or not extra, f'{extra} duplicate periodic reports'


async def accepting_duplicate_filter(tester, *, central):
    """Periodic Advertising Sync Transfer Procedure, Accepting, Enabling
    Duplicate Filter when already synchronized.

    Initial Condition:
        The IUT is not scanning, synchronized or connected; the default
        transfer parameters have not been set since reset.

    Test Steps:
        1. Connect on LE 1M.
        2. Start a 120-ms train whose AUX_SYNC_IND carries an ADI with a fixed
           DID and the same data.
        3. Set Mode 0x02 (duplicate filtering disabled).
        4. Send LL_PERIODIC_SYNC_IND; the IUT acknowledges it.
        5. Receive Sync Transfer Received with success.
        6. Alternative 6A: receive a report for each AUX_SYNC_IND for ten
           intervals.
        7. Terminate the sync.
        8. Set Mode 0x03 (duplicate filtering enabled).
        9. Send LL_PERIODIC_SYNC_IND; the IUT acknowledges it.
        10. Receive Sync Transfer Received with success.
        11. Alternative 11A: receive a report.
        12-13. For nine more intervals, a repeated report is a warning.
        14. Change the DID.
        15. Receive a report.
        16-17. For nine more intervals, a repeated report is a warning.

    Expected Outcome:
        Pass verdict:
            Steps 5 and 10 report the transfer; Step 6A.1 reports every event;
            Step 15 reports the changed data.

    Reference:
        LL.TS.p28.pdf, 4.3.7.38, Table 4.3-120, Steps 1-17; PDF 987-992,
        printed 4.3-514..519. Source [13] 4.4.5.1; Core Vol 4, Part E, 7.8.91.
    """
    link = await _link(tester, central=central)
    session = _did_session(TransferSession(tester, link, lower_train(link)), 5)
    await _transfer_mode(session, 0x02)
    await session.send_control(session.indication(address=ADVERTISER))
    event = await _received(session, sid=5)
    start = len(session.sent)
    await session.run_for(
        REPORTS * session.train.interval * LL_PERIODIC_INTERVAL_UNIT_US
    )
    count = len(session.reports(event.sync_handle))
    assert count >= len(session.sent) - start >= REPORTS - 1, 'unreported event'
    await session.command(
        hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(event.sync_handle)
    )
    await _transfer_mode(session, 0x03)
    await session.send_control(session.indication(address=ADVERTISER))
    event = await _received(session, sid=5)
    await _single_report(session, event.sync_handle, warn=True)
    session.did ^= 0x5A5
    await _single_report(session, event.sync_handle, warn=True)
    session.mode = 'off'
    await _disconnect(tester, link)


async def accepting_default_duplicate_filter(tester, *, central):
    """Periodic Advertising Sync Transfer Procedure, Accepting, Enabling Default
    Duplicate Filter.

    Initial Condition:
        The IUT is not scanning, synchronized or connected; the default
        transfer parameters have not been set since reset.

    Test Steps:
        1. Set the default transfer parameters: Mode 0x03.
        2. Connect on LE 1M.
        3. Start a 120-ms train whose AUX_SYNC_IND carries an ADI with a fixed
           DID and the same data.
        4. Send LL_PERIODIC_SYNC_IND; the IUT acknowledges it.
        5. Alternative 5A: receive Sync Transfer Received with success.
        6. Alternative 6A: receive one Periodic Advertising Report [v1].
        7-8. Receive no further report for nine intervals.
        9. Change the DID.
        10. Receive one Periodic Advertising Report.
        11-12. Receive no further report for nine intervals.

    Expected Outcome:
        Pass verdict:
            Step 5 reports the transfer; Steps 6A.1 and 10 send a single
            report and no report follows until the DID changes.

    Source Correction:
        Step 5A.1 names Num_Subevents, a field of the v2 event only; the v1
        event of a Controller without PAwR has none. Step 9 compares the DID
        with Step 1, which sets no DID; compare with the train's DID.

    Reference:
        LL.TS.p28.pdf, 4.3.7.39, Table 4.3-121, Steps 1-12; PDF 992-996,
        printed 4.3-519..523. Source [13] 4.4.5.1; Core Vol 4, Part E, 7.8.92.
    """
    await tester.command(DefaultParameters(0x03, 0, _timeout(96, 3), 0))
    link = await _link(tester, central=central)
    session = _did_session(TransferSession(tester, link, lower_train(link)), 5)
    await session.send_control(session.indication(address=ADVERTISER))
    event = await _received(session, sid=5)
    await _single_report(session, event.sync_handle, warn=False)
    session.did ^= 0x5A5
    await _single_report(session, event.sync_handle, warn=False)
    session.mode = 'off'
    await _disconnect(tester, link)


async def _iut_features(tester, link):
    """LL.TS 4.1.6.7.3: CEN IUT initiates Feature Exchange; PER Lower Tester."""
    if not link.central:
        await exchange_features(link, PEER_FEATURES)
        return
    await tester.command(hci.HCI_LE_Read_Remote_Features_Command(link.handle))
    await link.control(Opcode.LL_FEATURE_REQ)
    await link.exchange(
        bytes([Opcode.LL_FEATURE_RSP]) + int(PEER_FEATURES).to_bytes(8, 'little'),
        llid=LlId.CONTROL,
    )
    event = await tester.event(hci.HCI_LE_Read_Remote_Features_Complete_Event)
    assert event.status == ErrorCode.SUCCESS


async def _sender_link(tester, *, central, phy, peer=None, air_address=None):
    """Connect, complete the role's Feature Exchange, then select the PHY."""
    peer = peer or (
        LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
    )
    link = await establish(
        tester,
        central=central,
        extended=True,
        peer_address=peer,
        air_address=air_address,
    )
    await _iut_features(tester, link)
    if phy != Phy.LE_1M:
        await _change_phy(tester, link, phy, local=central)
    return link


async def _sent_transfer(session, *, unknown=False):
    """Steps 9-10: the IUT's LL_PERIODIC_SYNC_IND and its receiving event."""
    payload, counter, anchor = await session.expect_control(Opcode.LL_PERIODIC_SYNC_IND)
    if unknown:
        await session.send_control(
            bytes([Opcode.LL_UNKNOWN_RSP, Opcode.LL_PERIODIC_SYNC_IND])
        )
    indication = SyncIndication.decode(payload)
    assert indication.identifier == SERVICE_DATA
    assert abs(signed(indication.event - counter)) < 1 << 14, 'connEventCount range'
    # The sender received a packet in syncConnEventCount: this event for a
    # Peripheral, which receives first, or an earlier one (2.4.2.27). One event
    # back is a selected bound for this continuously served link.
    assert 0 <= signed(counter - indication.sync_event) <= 1, 'syncConnEventCount'
    target, info = transferred_anchor(indication, counter, anchor, session.interval_us)
    return indication, target, info


def _check_train(indication, target, info, train, *, phy):
    """The SyncInfo and connEventCount describe ``train`` (2.4.2.27, 2.3.4.6)."""
    actual = train_anchor(train, info.counter)
    assert target <= actual < target + info.unit_us, (target, actual)
    assert (info.interval, info.access_address, info.crc_init) == (
        train.interval,
        train.access_address,
        train.crc_init,
    )
    assert info.channel_map == train.channel_map
    distance = abs(signed(indication.last - info.counter))
    assert (
        distance <= 1
        or distance * train.interval * LL_PERIODIC_INTERVAL_UNIT_US <= COUNTER_SPAN_US
    ), 'lastPaEventCounter too far from the SyncInfo event'
    assert indication.phy == phy_bit(phy)
    # Core 6.3 assigns the source's RFU bits to the sender's SCA (2.4.2.27).
    assert 0 <= indication.sca <= 7


async def _synchronize_described(session, indication, target, info, phy):
    """Step 11: listen with only the transferred SyncInfo and receive its train."""
    train = PeriodicTrain(
        target,
        info.interval,
        info.counter,
        info.access_address,
        info.crc_init,
        info.channel_map,
        phy,
        info.unit_us,
    )
    while train.anchor <= session.now_us + PREPARATION_US:
        train.next_event()
    session.train, session.mode, session.received = train, 'receive', []
    await _wait_until(session, lambda current: current.received, events=64)


async def advertising_iut_initiated(
    tester, *, central, connection_phy, primary_phy, secondary_phy, privacy
):
    """Periodic Advertising Sync Transfer Procedure, Advertising IUT Initiated.

    Initial Condition:
        CEN/PER: Connected on the Table 4.3-73 PHY with public addresses; the
        IUT is not advertising. CEN: the IUT initiates Feature Exchange; PER:
        the Lower Tester does. For privacy, the resolving list holds a local
        IRK for the Lower Tester.

    Test Case Configuration:
        30-ms connection interval; the periodic interval is four connection
        intervals. Periodic advertising is enabled just after a connection
        event, so the train stays between connection events.

    Test Steps:
        1. Set extended parameters: properties 0x0000, public own address and
           the Primary/Secondary ADV PHYs.
        2. Set periodic parameters with properties 0x0000.
        3. Enable periodic advertising.
        4. Enable extended advertising.
        5. Receive ADV_EXT_IND with AuxPtr.
        6. Follow it to AUX_ADV_IND with SyncInfo.
        7. Synchronize with AUX_SYNC_IND.
        8. Send Set Info Transfer with nonzero Service_Data; require success.
        9. Receive LL_PERIODIC_SYNC_IND: ID, SID 3, AType of the advertising,
           the secondary PHY bit and the advertiser address.
        10. Maintain the connection.
        11. Receive the train using only that SyncInfo and connEventCount.
        12. Disable extended advertising.
        13. Disable periodic advertising.
        14. Without the privacy row, skip to Step 17.
        15. Repeat Steps 1-13 with own address type 0x02 and the Lower Tester
            as peer: AdvA is the unresolved RPA.
        16. Repeat Steps 1-10 and 12-13, answering with LL_UNKNOWN_RSP; the IUT
            maintains the connection.
        17. Disconnect.

    Expected Outcome:
        Pass verdict:
            CEN/PER: The IUT transfers synchronization information matching its
            periodic advertising and maintains the connection.

    Source Correction:
        Step 9's RFU 000 is the SCA field in Core 6.3; the IUT sends its
        sleep clock accuracy there.

    Reference:
        LL.TS.p28.pdf, 4.3.7.2, Table 4.3-73, Steps 1-17; PDF 899-903,
        printed 4.3-426..430. Source [13] 4.6.23; Core Vol 6, Part B, 2.4.2.27.
    """
    link = await _sender_link(tester, central=central, phy=connection_phy)
    rounds = [(hci.OwnAddressType.PUBLIC, False)]
    if privacy:
        await privacy_peer.add_peer(
            tester, identity=link_peer(link), peer_irk=bytes(16), local_irk=LOCAL_IRK
        )
        rounds += [
            (hci.OwnAddressType.RESOLVABLE_OR_PUBLIC, False),
            (hci.OwnAddressType.PUBLIC, True),
        ]
    for own, unknown in rounds:
        tester.output.write(f'ROUND own={own} unknown={unknown}\n')
        await tester.command(
            extended_parameters(
                own_address_type=own,
                peer_address_type=link_peer(link).address_type,
                peer_address=link_peer(link),
                primary_advertising_phy=primary_phy,
                secondary_advertising_phy=secondary_phy,
            )
        )
        await tester.command(
            hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 96, 96, 0)
        )
        session = TransferSession(tester, link)
        await session.step()
        await session.command(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
        await session.command(extended_enable())
        auxiliary, fields = await _receive_announcement(
            tester, primary_phy, secondary_phy
        )
        assert 'sync' in fields, 'AUX_ADV_IND without SyncInfo'
        session.train = PeriodicTrain.from_advertisement(auxiliary, fields)
        session.mode = 'receive'
        await _wait_until(session, lambda current: current.received, events=64)
        address = hci.Address(fields['address'])
        if own == hci.OwnAddressType.RESOLVABLE_OR_PUBLIC:
            privacy_peer.check_private_address(address, LOCAL_IRK)
        else:
            assert fields['address'] == bytes(DUT_ADDRESS)
        await session.command(
            hci.HCI_LE_Periodic_Advertising_Set_Info_Transfer_Command(
                link.handle, SERVICE_DATA, 0
            )
        )
        indication, target, info = await _sent_transfer(session, unknown=unknown)
        assert (indication.sid, indication.address) == (3, fields['address'])
        assert indication.random == bool(auxiliary.pdu[0] & PDU_ADV_TXADD_MASK)
        _check_train(indication, target, info, session.train, phy=secondary_phy)
        await session.run(4)
        if not unknown:
            await _synchronize_described(
                session, indication, target, info, secondary_phy
            )
        await session.command(extended_enable(0))
        await session.command(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0, 0))
        session.mode = 'off'
        await session.run(2)
    await _disconnect(tester, link)


async def _receive_announcement(tester, primary_phy, secondary_phy, *, attempts=8):
    """Steps 5-6: ADV_EXT_IND, then the AUX_ADV_IND its AuxPtr points to.

    Listening can start inside an advertising event the IUT began during the
    preceding HCI command; that partial packet is not an observation, so wait
    for the next event.
    """
    for _ in range(attempts):
        packet = await tester.receive(37, duration_us=100_000, phy=primary_phy)
        if (
            packet is None
            or not packet.valid_crc
            or packet.pdu[0] & PDU_ADV_TYPE_MASK != AdvertisingPduType.ADV_EXT_IND
        ):
            continue
        fields = decode_extended(packet.pdu)
        assert 'aux' in fields, 'ADV_EXT_IND without AuxPtr'
        _, auxiliary, aux_fields = await follow_auxiliary(tester, packet, fields)
        assert auxiliary.phy == secondary_phy
        return auxiliary, aux_fields
    raise AssertionError('no ADV_EXT_IND from the IUT')


def link_peer(link):
    return (
        LOWER_PERIPHERAL_PUBLIC_ADDRESS
        if link.central
        else LOWER_CENTRAL_PUBLIC_ADDRESS
    )


async def _synchronize_lower(
    session, *, sid, scanning_phy, address, report_type, check_data=None
):
    """Steps 3-8: the IUT scans, reports and synchronizes with the Lower train."""
    tester = session.tester
    # Reports of an earlier round are not evidence for this one.
    session.take_all(hci.HCI_LE_Extended_Advertising_Report_Event)
    session.reports()
    await session.command(
        scan_parameters(
            extended=True,
            scanning_phys=phy_bit(scanning_phy),
            scan_types=[0],
            scan_intervals=[48],
            scan_windows=[48],
        )
    )
    await session.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0))

    def periodic_report(event):
        return isinstance(event, hci.HCI_LE_Extended_Advertising_Report_Event) and any(
            report.periodic_advertising_interval for report in event.reports
        )

    event = await session.until(periodic_report, events=RESPONSE_EVENTS * 4)
    assert event is not None, 'no Extended Advertising Report with a periodic interval'
    report = next(item for item in event.reports if item.periodic_advertising_interval)
    assert report.advertising_sid == sid
    assert report.periodic_advertising_interval == session.train.interval
    assert report.address_type == report_type and report.address == address
    if check_data is not None:
        assert report.data == check_data
    await session.command(
        hci.HCI_LE_Periodic_Advertising_Create_Sync_Command(
            0,
            sid,
            hci.AddressType.RANDOM_DEVICE
            if report.address.is_random
            else hci.AddressType.PUBLIC_DEVICE,
            report.address,
            0,
            _timeout(session.train.interval, 6),
            0,
        )
    )
    established = await session.until(
        lambda item: isinstance(
            item, hci.HCI_LE_Periodic_Advertising_Sync_Established_Event
        ),
        events=RESPONSE_EVENTS * 4,
    )
    assert established is not None and established.status == ErrorCode.SUCCESS
    assert established.advertising_sid == sid
    assert established.advertiser_address == address
    assert established.periodic_advertising_interval == session.train.interval
    await _reports(session, established.sync_handle, 1)
    tester.output.write(f'SYNCHRONIZED handle={established.sync_handle}\n')
    return established


async def _transfer_lower(session, handle, *, sid, address, phy, resolvable=None):
    """Steps 9-12: Sync Transfer of the Lower Tester's own train."""
    await session.command(
        hci.HCI_LE_Periodic_Advertising_Sync_Transfer_Command(
            session.link.handle, SERVICE_DATA, handle
        )
    )
    indication, target, info = await _sent_transfer(session)
    assert indication.sid == sid
    assert indication.random == address.is_random
    if resolvable is not None:
        # 2.4.2.27: any RPA generated from the advertiser's IRK.
        privacy_peer.check_private_address(hci.Address(indication.address), resolvable)
    else:
        assert indication.address == bytes(address)
    _check_train(indication, target, info, session.train, phy=phy)
    assert indication.last in session.sent, 'lastPaEventCounter was not received'
    await session.run(4)
    return indication


async def _stop_lower(session, handle):
    await session.command(
        hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(handle)
    )
    await session.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))
    session.mode, session.announce = 'off', None
    session.take_all(hci.HCI_LE_Extended_Advertising_Report_Event)
    session.reports()


async def synchronized_iut_initiated(
    tester, *, central, rpa_peer, advertiser, connection_phy, primary_phy, secondary_phy
):
    """Periodic Advertising Sync Transfer Procedure, Synchronized IUT Initiated.

    Initial Condition:
        CEN/PER: Connected on the Table 4.3-74 PHY; the IUT is not advertising.
        The resolving list holds the peer IRKs of the Lower Tester's RPAs:
        its public identity and the advertiser's public identity. CEN: the
        IUT initiates Feature Exchange; PER: the Lower Tester does. The Lower
        Tester connects with the Table 4.3-74 address type.

    Test Case Configuration:
        30-ms connection interval; a 120-ms train between connection events,
        announced once per connection interval on channel 37.

    Test Steps:
        1. Start extended advertising on the Primary/Secondary ADV PHYs with
           the Step 1 AdvA.
        2. Start its periodic advertising.
        3. Set extended scan parameters: the Primary ADV PHY, passive.
        4. Enable scanning.
        5. Receive an Extended Advertising Report with a nonzero periodic
           interval and the advertising SID.
        6. Send Create Sync for the advertiser.
        7. Receive a successful Sync Established with matching fields.
        8. Receive periodic reports.
        9. Send Sync Transfer with nonzero Service_Data; require success.
        10. Receive LL_PERIODIC_SYNC_IND: ID, SID, AType of the AdvA, the
            secondary PHY bit and AdvA; an RPA AdvA may be any RPA from its IRK.
        11. Maintain the connection.
        12. Check that SyncInfo and connEventCount describe the train.
        13. Terminate the sync.
        14. Disable scanning.
        15. Stop advertising.
        16. Disconnect.

    Expected Outcome:
        Pass verdict:
            CEN/PER: The IUT transfers synchronization information matching
            the periodic advertising; Step 5 reports the advertised SID.

    Source Correction:
        Step 10's RFU 000 is the SCA field in Core 6.3.

    Coverage Note:
        lastPaEventCounter must name an event the Lower Tester transmitted.

    Reference:
        LL.TS.p28.pdf, 4.3.7.3, Table 4.3-74, Steps 1-16; PDF 903-907,
        printed 4.3-430..434. Source [13] 4.6.23; Core Vol 6, Part B, 2.4.2.27.
    """
    identity = (
        LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
    )
    # In network privacy mode an identity with an IRK must use its RPAs, so the
    # Lower Tester's IRK is held only when it connects with an RPA (Core Vol 6,
    # Part B, 4.7).
    if rpa_peer:
        await privacy_peer.add_peer(
            tester, identity=identity, peer_irk=PEER_IRK, local_irk=bytes(16)
        )
    await privacy_peer.add_peer(
        tester, identity=IDENTITY, peer_irk=OTHER_IRK, local_irk=bytes(16)
    )
    link = await _sender_link(
        tester,
        central=central,
        phy=connection_phy,
        peer=identity,
        air_address=privacy_peer.private_address(PEER_IRK) if rpa_peer else None,
    )
    resolvable = None
    report_address, report_type = None, None
    if advertiser == 'other':
        address = ADVERTISER
    elif advertiser == 'same':
        address = identity
    elif advertiser == 'resolvable':
        address = privacy_peer.private_address(OTHER_IRK, 0x5A5A5A)
        resolvable = OTHER_IRK
        report_address, report_type = IDENTITY, hci.AddressType.PUBLIC_IDENTITY
    elif advertiser == 'rpa':
        address = privacy_peer.private_address(UNKNOWN_IRK)
    else:
        address = STATIC_ADVERTISER
    if report_address is None:
        report_address, report_type = address, address.address_type
    session = TransferSession(tester, link, lower_train(link, phy=secondary_phy))
    session.announce = {
        'sid': 5,
        'address': address,
        'primary_phy': primary_phy,
        'auxiliary_offset': 3000 if primary_phy == Phy.LE_CODED else 600,
    }
    established = await _synchronize_lower(
        session,
        sid=5,
        scanning_phy=primary_phy,
        address=report_address,
        report_type=report_type,
    )
    await _transfer_lower(
        session,
        established.sync_handle,
        sid=5,
        address=address,
        phy=secondary_phy,
        resolvable=resolvable,
    )
    await _stop_lower(session, established.sync_handle)
    await _disconnect(tester, link)


async def multiple_resolvable_private_addresses(tester, *, central):
    """Periodic Advertising Sync Transfer Procedure, Synchronized IUT Initiated –
    Multiple Resolvable Private Addresses.

    Initial Condition:
        CEN/PER: The Lower Tester connects with its public identity. The IUT
        holds the peer IRK of its random static identity.

    Test Steps:
        1. Enable address resolution.
        2. Set extended scan parameters: policy 0x00, LE 1M, passive.
        Repeat the remaining steps for each round of Table 4.3-131.
        3. Enable scanning.
        4. Send ADV_EXT_IND without AdvA.
        5. Send AUX_ADV_IND with a new RPA, resolvable or not per the round,
           no data, SyncInfo and ADI.
        6. Receive an Extended Advertising Report: nonzero interval, the SID,
           type 0x03 with the identity when resolved, otherwise 0x01 with the
           RPA, and no data.
        7. Send Create Sync with type 0x01 and the reported address.
        8. Send AUX_SYNC_IND.
        9. Receive a successful Sync Established with matching fields.
        10. Receive a periodic report for the Sync_Handle.
        11. Send Sync Transfer; require success.
        12. Receive LL_PERIODIC_SYNC_IND: 12A, an AdvA resolvable with the
            same IRK; 12B, the round's RPA, which no earlier round used.
        13. Terminate the sync.
        14. Disable scanning.

    Expected Outcome:
        Pass verdict:
            12A.1 AdvA resolves with the peer IRK; 12B.1 AdvA is new and does
            not resolve with it.

    Source Correction:
        Step 9 compares Sync Established with Step 7; for a resolved RPA,
        7.7.65.14 reports the random identity with type 0x03.

    Reference:
        LL.TS.p28.pdf, 4.3.7.48, Tables 4.3-130/131, Steps 1-14; PDF
        1010-1014, printed 4.3-537..541. Source [19] 2.4.2.27.
    """
    await privacy_peer.add_peer(
        tester, identity=STATIC_IDENTITY, peer_irk=OTHER_IRK, local_irk=bytes(16)
    )
    link = await _sender_link(tester, central=central, phy=Phy.LE_1M)
    used = []
    for index, resolved in enumerate((True, True, False, True)):
        tester.output.write(f'ROUND {index + 1} resolved={resolved}\n')
        key = OTHER_IRK if resolved else UNKNOWN_IRK
        address = privacy_peer.private_address(key, 0x400000 + index)
        session = TransferSession(tester, link, lower_train(link))
        session.announce = {'sid': 5, 'address': address, 'data': b''}
        established = await _synchronize_lower(
            session,
            sid=5,
            scanning_phy=Phy.LE_1M,
            address=STATIC_IDENTITY if resolved else address,
            report_type=hci.AddressType.RANDOM_IDENTITY
            if resolved
            else hci.AddressType.RANDOM_DEVICE,
            check_data=b'',
        )
        indication = await _transfer_lower(
            session,
            established.sync_handle,
            sid=5,
            address=address,
            phy=Phy.LE_1M,
            resolvable=OTHER_IRK if resolved else None,
        )
        if not resolved:
            assert indication.address not in used
            with pytest.raises(AssertionError):
                privacy_peer.check_private_address(
                    hci.Address(indication.address), OTHER_IRK
                )
        used.append(indication.address)
        await _stop_lower(session, established.sync_handle)
        await session.run(2)
    await _disconnect(tester, link)


async def invalid_or_unsupported_phy(tester, *, central, phy):
    """Periodic Advertising Sync Transfer Procedure, Invalid or Unsupported PHY.

    Initial Condition:
        The IUT is not scanning, synchronized or connected; it has the role of
        Table 4.3-128.

    Test Steps:
        1. Connect on LE 1M.
        2. Start a 120-ms periodic train.
        3. Set Mode 0x01 for the connection.
        4. Send LL_PERIODIC_SYNC_IND with the PHY field of Table 4.3-128.
        5. Receive no Sync Transfer Received event.
        6. Five seconds after Step 4, send it with PHY 0x01.
        7. Receive a successful Sync Transfer Received event.

    Expected Outcome:
        Pass verdict:
            Step 5 has no transfer event; Step 7 has one. The ignored PDU is
            acknowledged and draws no LL_UNKNOWN_RSP.

    Reference:
        LL.TS.p28.pdf, 4.3.7.46, Table 4.3-128, Steps 1-7; PDF 1007-1008,
        printed 4.3-534..535. Source [19] 5.1.13.
    """
    link = await _link(tester, central=central)
    session = TransferSession(tester, link, lower_train(link))
    await _transfer_mode(session, 0x01)
    await session.send_control(session.indication(address=ADVERTISER, phy=phy))
    start = session.now_us
    while session.now_us < start + 5_000_000:
        packet = await session.step()
        assert packet.pdu[0] & PDU_DATA_LLID_MASK != LlId.CONTROL, (
            'reply to an ignored PDU'
        )
    assert not session.transfers(), 'transfer event for an unusable PHY'
    await session.send_control(session.indication(address=ADVERTISER))
    await _received(session, sid=5)
    session.mode = 'off'
    await _disconnect(tester, link)


PROCEDURES: ProcedureMap = {}


def _roles(variants):
    """Expand {(peripheral, central): (description, parameters)} by IUT role."""
    cases = {}
    for (peripheral, central), (description, parameters) in variants.items():
        cases[peripheral] = (description, parameters | {'central': False})
        cases[central] = (description, parameters | {'central': True})
    return cases


# Verify acceptance on each combination of connection and periodic PHYs, and that
# the Periodic Advertiser List does not restrict a transfer.
PROCEDURES |= procedure_cases(
    accepting_different_phys,
    _roles(
        {
            (f'LL/CON/PER/BV-{peripheral}-C', f'LL/CON/CEN/BV-{central}-C'): (
                (
                    'Periodic Advertising Sync Transfer Procedure, Accepting – '
                    'Different PHYs'
                ),
                {
                    'connection_phy': connection,
                    'primary_phy': primary,
                    'secondary_phy': secondary,
                    'advertiser_list': listed,
                },
            )
            for peripheral, central, connection, primary, secondary, listed in (
                (98, 94, Phy.LE_1M, Phy.LE_1M, Phy.LE_1M, True),
                (99, 95, Phy.LE_1M, Phy.LE_CODED, Phy.LE_CODED, False),
                (100, 96, Phy.LE_1M, Phy.LE_1M, Phy.LE_2M, False),
                (101, 97, Phy.LE_CODED, Phy.LE_1M, Phy.LE_1M, False),
                (102, 98, Phy.LE_2M, Phy.LE_1M, Phy.LE_1M, False),
            )
        }
    ),
)


def _register():
    """Recipient registrations; kept after the procedures they name."""
    simple = {
        accepting_skipping_events: (
            ('LL/CON/PER/BV-103-C', 'LL/CON/CEN/BV-99-C'),
            'Periodic Advertising Sync Transfer Procedure, Accepting – Skipping Events',
            (),
        ),
        accepting_already_synchronized: (
            ('LL/CON/PER/BV-104-C', 'LL/CON/CEN/BV-100-C'),
            'Periodic Advertising Sync Transfer Procedure, Accepting – Already '
            'Synchronized',
            (pytest.mark.source_conflict,),
        ),
        accepting_extreme_timings: (
            ('LL/CON/PER/BV-105-C', 'LL/CON/CEN/BV-101-C'),
            'Periodic Advertising Sync Transfer Procedure, Accepting – Extreme Timings',
            (),
        ),
        accepting_synchronization_failure: (
            ('LL/CON/PER/BV-106-C', 'LL/CON/CEN/BV-102-C'),
            'Periodic Advertising Sync Transfer Procedure, Accepting – '
            'Synchronization Failure',
            (),
        ),
        accepting_mode_change: (
            ('LL/CON/PER/BV-108-C', 'LL/CON/CEN/BV-104-C'),
            'Periodic Advertising Sync Transfer Procedure, Accepting, Changing '
            'Transfer Mode During Synchronization',
            (),
        ),
    }
    cases = {}
    for procedure, ((peripheral, central), description, marks) in simple.items():
        cases |= procedure_cases(
            procedure,
            {
                identifier: pytest.param(
                    description, {'central': role}, marks=list(marks)
                )
                for identifier, role in ((peripheral, False), (central, True))
            },
        )
    for procedure, peripheral, central, description in (
        (
            accepting_modes_and_addresses,
            'LL/CON/PER/BV-107-C',
            'LL/CON/CEN/BV-103-C',
            'Periodic Advertising Sync Transfer Procedure, Accepting – Different '
            'Modes and Addresses',
        ),
        (
            accepting_duplicate_filter,
            'LL/CON/PER/BV-135-C',
            'LL/CON/CEN/BV-132-C',
            'Periodic Advertising Sync Transfer Procedure, Accepting, Enabling '
            'Duplicate Filter when already synchronized',
        ),
        (
            accepting_default_duplicate_filter,
            'LL/CON/PER/BV-136-C',
            'LL/CON/CEN/BV-133-C',
            'Periodic Advertising Sync Transfer Procedure, Accepting, Enabling '
            'Default Duplicate Filter',
        ),
    ):
        marks = (
            [pytest.mark.source_conflict]
            if procedure is accepting_modes_and_addresses
            else []
        )
        cases |= procedure_cases(
            procedure,
            {
                peripheral: pytest.param(description, {'central': False}, marks=marks),
                central: pytest.param(description, {'central': True}, marks=marks),
            },
        )
    cases |= procedure_cases(
        advertising_iut_initiated,
        _roles(
            {
                (f'LL/CON/PER/BV-{peripheral}-C', f'LL/CON/CEN/BV-{central}-C'): (
                    'Periodic Advertising Sync Transfer Procedure, Advertising IUT '
                    'Initiated',
                    {
                        'connection_phy': connection,
                        'primary_phy': primary,
                        'secondary_phy': secondary,
                        'privacy': privacy,
                    },
                )
                for peripheral, central, connection, primary, secondary, privacy in (
                    (88, 84, Phy.LE_1M, Phy.LE_1M, Phy.LE_1M, True),
                    (89, 85, Phy.LE_1M, Phy.LE_CODED, Phy.LE_CODED, False),
                    (90, 86, Phy.LE_1M, Phy.LE_1M, Phy.LE_2M, False),
                    (91, 87, Phy.LE_CODED, Phy.LE_1M, Phy.LE_1M, False),
                    (92, 88, Phy.LE_2M, Phy.LE_1M, Phy.LE_1M, False),
                )
            }
        ),
    )
    cases |= procedure_cases(
        synchronized_iut_initiated,
        _roles(
            {
                (f'LL/CON/PER/BV-{peripheral}-C', f'LL/CON/CEN/BV-{central}-C'): (
                    'Periodic Advertising Sync Transfer Procedure, Synchronized '
                    'IUT Initiated',
                    {
                        'rpa_peer': rpa_peer,
                        'advertiser': advertiser,
                        'connection_phy': connection,
                        'primary_phy': primary,
                        'secondary_phy': secondary,
                    },
                )
                for (
                    peripheral,
                    central,
                    rpa_peer,
                    advertiser,
                    connection,
                    primary,
                    secondary,
                ) in (
                    (93, 89, False, 'other', Phy.LE_1M, Phy.LE_1M, Phy.LE_1M),
                    (153, 153, False, 'same', Phy.LE_1M, Phy.LE_1M, Phy.LE_1M),
                    (154, 154, True, 'resolvable', Phy.LE_1M, Phy.LE_1M, Phy.LE_1M),
                    (155, 155, False, 'rpa', Phy.LE_1M, Phy.LE_1M, Phy.LE_1M),
                    (156, 156, True, 'other', Phy.LE_1M, Phy.LE_1M, Phy.LE_1M),
                    (157, 157, False, 'static', Phy.LE_1M, Phy.LE_1M, Phy.LE_1M),
                    (94, 90, False, 'other', Phy.LE_1M, Phy.LE_CODED, Phy.LE_CODED),
                    (95, 91, False, 'other', Phy.LE_1M, Phy.LE_1M, Phy.LE_2M),
                    (96, 92, False, 'other', Phy.LE_CODED, Phy.LE_1M, Phy.LE_1M),
                    (97, 93, False, 'other', Phy.LE_2M, Phy.LE_1M, Phy.LE_1M),
                )
            }
        ),
    )
    cases |= procedure_cases(
        multiple_resolvable_private_addresses,
        _roles(
            {
                ('LL/CON/PER/BV-152-C', 'LL/CON/CEN/BV-152-C'): (
                    'Periodic Advertising Sync Transfer Procedure, Synchronized '
                    'IUT Initiated – Multiple Resolvable Private Addresses',
                    {},
                )
            }
        ),
    )
    cases |= procedure_cases(
        invalid_or_unsupported_phy,
        {
            identifier: (
                'Periodic Advertising Sync Transfer Procedure, Invalid or '
                'Unsupported PHY',
                {'central': central, 'phy': phy},
            )
            for identifier, central, phy in (
                ('LL/CON/PER/BI-22-C', False, 0x00),
                ('LL/CON/CEN/BI-15-C', True, 0x00),
                ('LL/CON/PER/BI-25-C', False, 0x06),
                ('LL/CON/CEN/BI-18-C', True, 0x06),
                ('LL/CON/PER/BI-26-C', False, 0x80),
                ('LL/CON/CEN/BI-19-C', True, 0x80),
            )
        },
    )
    return cases


# Verify skip spacing, a repeated transfer for a synchronized train, extreme
# connEventCount distances, establishment failure, the transfer mode in force at
# reception, and ignoring unusable PHY fields.
PROCEDURES |= _register()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_sync_transfer(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
