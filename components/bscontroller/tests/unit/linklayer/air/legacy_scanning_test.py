# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Legacy scanning and initiating states (Core Vol 6, Part B, 4.4.3, 4.4.4;
Vol 4, Part E, 7.8.10-7.8.13).

Matrix: {passive, active} x {ADV_IND, ADV_SCAN_IND, ADV_NONCONN_IND,
ADV_DIRECT_IND, SCAN_RSP} x {reported, filtered, malformed, duplicate}, the
active-scan backoff, and initiating {undirected, directed, filtered, cancel}.

Here the IUT scans or initiates and the Lower Tester plays the advertiser.
Scenarios run on the ``air/harness.py`` harness: ``ut`` drives HCI, ``lt_tx``
scripts the advertiser's PDU for the IUT's next listen window, ``lt_rx`` reads
the IUT's SCAN_REQ or CONNECT_IND, ``ut_rx_report`` pops advertising reports, and
``run`` advances the clock. The advertiser side lives in
``legacy_advertising_test.py``; the extended equivalents in
``extended_scanning_test.py``.
"""

import pytest

from bscontroller.hci import HCI_LE_SCAN_INTERVAL_UNIT_US
from bscontroller.linklayer import LL_IFS_US
from bscontroller.linklayer.connection import Connection
from bscontroller.linklayer.parameters import Parameters
from bscontroller.linklayer.state import Role
from tests.unit.linklayer.air.advertising import (
    SCAN_WINDOW,
    AdvType,
    configure_legacy,
    enable_legacy,
    scan_legacy,
)
from tests.unit.linklayer.air.harness import (
    CONNECT_PARAMETERS,
    IUT_ADDRESS,
    OTHER,
    PEER,
    PUBLIC_PEER,
    ErrorCode,
    PduType,
    address_of,
    adv_direct_ind,
    adv_ind,
    adv_nonconn_ind,
    adv_scan_ind,
    hci,
    scan_rsp,
)

ReportType = hci.HCI_LE_Advertising_Report_Event.EventType
WINDOW_US = SCAN_WINDOW * HCI_LE_SCAN_INTERVAL_UNIT_US
CONNECTED = hci.HCI_LE_Enhanced_Connection_Complete_Event
FLUSH_TIMEOUT_US = 625  # One HCI automatic flush timeout slot.


def stop(iut):
    iut.ut(hci.HCI_LE_Set_Scan_Enable_Command(0, 0))


def accept(iut, address):
    iut.ut(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(
            address.address_type, address
        )
    )


@pytest.mark.parametrize('active,state_bit', [(False, 8), (True, 12)])
def test_nonconnectable_advertising_and_scanning_share_the_radio(
    iut, active, state_bit
):
    """Exercise the advertised legacy state combination (Vol 4, Part E, 7.8.27).

    Steps:
        1. Read LE Supported States and enable non-connectable advertising on
           channel 37 together with passive or active scanning.
        2. Transmit a scannable advertisement while both roles are enabled.
           In the active case, answer the scan request.

    Expected:
        - The matching state bit is set. The IUT sends its own advertising data
          and reports the peer. Active scanning also produces SCAN_REQ and a
          scan response report for that peer.
    """
    states = iut.ut(hci.HCI_LE_Read_Supported_States_Command()).le_states
    assert int.from_bytes(states, 'little') & (1 << state_bit)
    configure_legacy(iut, AdvType.ADV_NONCONN_IND, data=b'local', channel_map=1)
    enable_legacy(iut)
    scan_legacy(iut, active=active)
    iut.lt_tx(adv_scan_ind(data=b'peer'), channel=37, at=3000)
    if active:
        iut.lt_tx(scan_rsp(data=b'response'), channel=37)
    iut.run(WINDOW_US)
    advertisement = iut.lt_rx(PduType.ADV_NONCONN_IND)
    assert advertisement.channel == 37 and advertisement.pdu[8:] == b'local'
    iut.ut_rx_report(event_type=ReportType.ADV_SCAN_IND, address=PEER, data=b'peer')
    if active:
        request = iut.lt_rx(PduType.SCAN_REQ)
        assert address_of(request.pdu, 8, rx=True) == PEER
        iut.ut_rx_report(event_type=ReportType.SCAN_RSP, address=PEER, data=b'response')
    iut.ut_rx_q_is_empty()
    iut.lt_rx_q_is_empty()


# ---------------------------------------------------------------------------
# Passive scanning
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'pdu,report_type',
    [
        (adv_ind(data=b'\x02\x01\x06'), ReportType.ADV_IND),
        (adv_scan_ind(data=b'\x02\x01\x06'), ReportType.ADV_SCAN_IND),
        (adv_nonconn_ind(data=b'\x02\x01\x06'), ReportType.ADV_NONCONN_IND),
    ],
)
def test_passive_reports_undirected(iut, pdu, report_type):
    """Passive scanning reports each undirected PDU and never transmits.

    Purpose: Verify that a passive scanner reports an undirected advertisement
    with the matching report event type, advertiser address, AdvData and rounded
    RSSI, and sends nothing on air (Vol 6, Part B, 4.4.3.1; Vol 4, Part E,
    7.7.65.2).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Lower Tester scripts the advertisement with RSSI -55.4.
        3. Run one scan window.

    Expected:
        - The Host gets one advertising report of the matching type, from the
          peer, with the AdvData and RSSI -55.
        - Nothing else reaches the Host.
        - The scanner transmitted nothing.

    Parameters range over ADV_IND, ADV_SCAN_IND and ADV_NONCONN_IND.
    """
    scan_legacy(iut)
    iut.lt_tx(pdu, rssi=-55.4)
    iut.run(WINDOW_US)
    iut.ut_rx_report(
        event_type=report_type, address=PEER, data=b'\x02\x01\x06', rssi=-55
    )
    iut.ut_rx_q_is_empty()
    # Passive scanning never transmits.
    iut.lt_rx_q_is_empty()


def queue_flushable_acl(iut):
    connection = Connection(
        iut.link, CONNECT_PARAMETERS, OTHER, Role.CENTRAL, iut.now_us
    )
    iut.link.connections[connection.handle] = connection
    connection.establish()
    iut.ut_rx(CONNECTED)
    iut.ut_rx(hci.HCI_LE_Channel_Selection_Algorithm_Event)
    iut.ut(hci.HCI_Write_Automatic_Flush_Timeout_Command(connection.handle, 1))
    data = b'\x01\x00\x04\x00\x1e'
    iut.controller.on_packet(
        bytes(
            hci.HCI_AclDataPacket(
                connection.handle, hci.HCI_ACL_PB_FIRST_FLUSHABLE, 0, len(data), data
            )
        )
    )
    return connection


def test_acl_flush_expiry_does_not_interrupt_passive_scanning(iut):
    """Catch a broadcast whose access address spans the software deadline."""
    scan_legacy(iut)
    connection = queue_flushable_acl(iut)
    iut.lt_tx(adv_nonconn_ind(), channel=37, at=FLUSH_TIMEOUT_US - 25)

    iut.run(FLUSH_TIMEOUT_US + 200)

    iut.ut_rx_report(event_type=ReportType.ADV_NONCONN_IND, address=PEER)
    (completed,) = iut.events
    assert isinstance(completed, hci.HCI_Number_Of_Completed_Packets_Event)
    assert completed.connection_handles == [connection.handle]
    assert completed.num_completed_packets == [1]
    iut.lt_rx_q_is_empty()


@pytest.mark.parametrize('scanning', [False, True], ids=['idle', 'scan-gap'])
def test_idle_phy_releases_acl_credit_at_flush_deadline(iut, scanning):
    if scanning:
        scan_legacy(iut, window=4)
    # A 2.5-ms scan window has closed when the ACL packet is queued.
    iut.run(3000)
    connection = queue_flushable_acl(iut)
    deadline = iut.now_us + FLUSH_TIMEOUT_US
    completed_at = []

    def record_completion(packet):
        assert isinstance(packet, hci.HCI_Number_Of_Completed_Packets_Event)
        assert packet.connection_handles == [connection.handle]
        assert packet.num_completed_packets == [1]
        completed_at.append(iut.now_us)

    iut.controller.send_hci_packet = record_completion
    iut.run(FLUSH_TIMEOUT_US + 100)

    assert completed_at == [deadline]
    iut.lt_rx_q_is_empty()


def test_passive_reports_directed_to_us_without_data(iut):
    """Passive scanning reports a directed PDU aimed at us, but not at others.

    Purpose: Verify that a passive scanner reports an ADV_DIRECT_IND whose TargetA
    is the IUT with an empty AdvData, and ignores one directed at another device
    (Vol 6, Part B, 4.4.3.1).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Lower Tester scripts an ADV_DIRECT_IND targeting the IUT.
        3. Lower Tester scripts an ADV_DIRECT_IND targeting another device.
        4. Run two scan windows.

    Expected:
        - The Host gets one ADV_DIRECT_IND report from the peer with empty data.
        - Nothing else reaches the Host.
    """
    scan_legacy(iut)
    iut.lt_tx(adv_direct_ind())
    iut.lt_tx(adv_direct_ind(target=OTHER))
    iut.run(2 * WINDOW_US)
    iut.ut_rx_report(event_type=ReportType.ADV_DIRECT_IND, address=PEER, data=b'')
    iut.ut_rx_q_is_empty()


@pytest.mark.parametrize(
    'pdu',
    [
        adv_ind()[:7],  # shorter than an address
        bytes([PduType.ADV_IND, 40]) + bytes(40),  # longer than 37 octets
        bytes([PduType.ADV_IND, 10]) + bytes(6),  # length mismatch
        bytes([PduType.CONNECT_IND, 6]) + bytes(6),  # not an advertising PDU
        bytes([PduType.ADV_DIRECT_IND, 6]) + bytes(6),  # direct without target
    ],
    ids=['short', 'long', 'length', 'type', 'direct-short'],
)
def test_malformed_pdu_is_not_reported(iut, pdu):
    """A malformed advertising PDU is dropped without a report.

    Purpose: Verify that a scanner drops advertising PDUs that are shorter than an
    address, longer than the legacy maximum, length-mismatched, not an
    advertising type, or directed without a target, reporting none of them
    (Vol 6, Part B, 2.3.1).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Lower Tester scripts the malformed PDU.
        3. Run one scan window.

    Expected:
        - Nothing reaches the Host.

    Parameters range over short, over-long, length-mismatched, wrong-type, and
    directed-without-target PDUs.
    """
    scan_legacy(iut)
    iut.lt_tx(pdu)
    iut.run(WINDOW_US)
    iut.ut_rx_q_is_empty()


def test_bad_crc_is_not_reported(iut):
    """A CRC-failed advertisement is not reported.

    Purpose: Verify that a scanner discards an advertisement received with a
    failing CRC, reporting nothing (Vol 6, Part B, 4.4.3.1).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Lower Tester scripts an ADV_IND with an invalid CRC.
        3. Run one scan window.

    Expected:
        - Nothing reaches the Host.
    """
    scan_legacy(iut)
    iut.lt_tx(adv_ind(), valid_crc=False)
    iut.run(WINDOW_US)
    iut.ut_rx_q_is_empty()


@pytest.mark.parametrize('listed', [False, True])
def test_filter_accept_list_policy(iut, listed):
    """Filtered scanning reports only Filter Accept List advertisers.

    Purpose: Verify that with scanning filter policy 1 the scanner reports an
    advertisement only when its advertiser is on the Filter Accept List (Vol 4,
    Part E, 7.8.10).

    Steps:
        1. If listed, Upper Tester adds the peer to the Filter Accept List.
        2. Upper Tester configures and enables scanning with filter policy 1.
        3. Lower Tester scripts an ADV_IND from the peer and one from another
           device.
        4. Run two scan windows.

    Expected:
        - The reports are exactly [peer] when listed and empty otherwise.

    Parameters range over listed and unlisted.
    """
    if listed:
        accept(iut, PEER)
    scan_legacy(iut, policy=1)
    iut.lt_tx(adv_ind())
    iut.lt_tx(adv_ind(OTHER))
    iut.run(2 * WINDOW_US)
    reports = iut.reports()
    assert [report.address for report in reports] == ([PEER] if listed else [])


def test_filter_matches_address_type(iut):
    """Filter Accept List matching distinguishes the address type.

    Purpose: Verify that a Filter Accept List entry matches only an advertiser of
    the same address type, so a public-typed entry does not match the peer's
    random address (Vol 4, Part E, 7.8.10).

    Steps:
        1. Upper Tester adds the peer address with the PUBLIC type to the list.
        2. Upper Tester configures and enables scanning with filter policy 1.
        3. Lower Tester scripts an ADV_IND from the peer (random address).
        4. Run one scan window.

    Expected:
        - Nothing reaches the Host.
    """
    accept(iut, hci.Address(bytes(PEER), hci.AddressType.PUBLIC_DEVICE))
    scan_legacy(iut, policy=1)
    iut.lt_tx(adv_ind())
    iut.run(WINDOW_US)
    iut.ut_rx_q_is_empty()


@pytest.mark.parametrize('duplicates', [0, 1])
def test_duplicate_filtering(iut, duplicates):
    """Duplicate filtering suppresses repeats of identical advertisements.

    Purpose: Verify that with duplicate filtering enabled the scanner reports an
    identical advertisement once and still reports changed data, while with
    filtering disabled every advertisement is reported (Vol 4, Part E, 7.8.11).

    Steps:
        1. Upper Tester enables scanning with the parametrized duplicate setting.
        2. Lower Tester scripts the same advertisement three times, then one with
           changed data.
        3. Run four scan windows.

    Expected:
        - Filtered: data is ["same", "changed"].
        - Unfiltered: data is ["same", "same", "same", "changed"].

    Parameters range over filtering disabled and enabled.
    """
    scan_legacy(iut, duplicates=duplicates)
    for _ in range(3):
        iut.lt_tx(adv_ind(data=b'same'))
    iut.lt_tx(adv_ind(data=b'changed'))
    iut.run(4 * WINDOW_US)
    data = [report.data for report in iut.reports()]
    assert data == (
        [b'same', b'changed'] if duplicates else [b'same'] * 3 + [b'changed']
    )


def test_duplicate_filter_resets_on_reenable(iut):
    """Re-enabling scanning clears the duplicate filter.

    Purpose: Verify that disabling then re-enabling scanning with duplicate
    filtering resets the seen set, so an advertisement reported before the toggle
    is reported again afterward (Vol 4, Part E, 7.8.11).

    Steps:
        1. Upper Tester enables scanning with duplicate filtering.
        2. Lower Tester scripts an ADV_IND; run one window.
        3. Disable with Filter_Duplicates set to 0xFF (ignored on disable),
           then re-enable scanning with filtering.
        4. Lower Tester scripts the same ADV_IND; run one window.

    Expected:
        - Two reports are produced across the two windows.
    """
    scan_legacy(iut, duplicates=1)
    iut.lt_tx(adv_ind())
    iut.run(WINDOW_US)
    iut.ut(hci.HCI_LE_Set_Scan_Enable_Command(0, 0xFF))
    iut.ut(hci.HCI_LE_Set_Scan_Enable_Command(1, 1))
    iut.lt_tx(adv_ind())
    iut.run(WINDOW_US)
    assert len(iut.reports()) == 2


@pytest.mark.parametrize('duplicates', [0, 1])
def test_repeated_scan_enable_preserves_pending_scan_response(iut, duplicates):
    """Change Filter_Duplicates while the scanner waits for SCAN_RSP (7.8.11).

    Steps:
        Send ADV_SCAN_IND, re-enable after SCAN_REQ, then deliver its SCAN_RSP.
        Receive two identical non-scannable advertisements.

    Expected:
        The response reaches the Host, counts as one backoff success, and
        subsequent reports follow the new duplicate-filter setting.
    """
    scan_legacy(iut, active=True, duplicates=1 - duplicates)
    iut.lt_tx(adv_scan_ind(data=b'ad'))
    iut.lt_tx(scan_rsp(data=b'rsp'))
    window = iut.link.roles.scanning.run(iut.link.scan, iut.now_us + WINDOW_US)
    with iut.pause_radio(window, 'receive', occurrence=2):
        iut.ut_rx_report(event_type=ReportType.ADV_SCAN_IND, data=b'ad')
        iut.lt_rx(PduType.SCAN_REQ)
        iut.ut(hci.HCI_LE_Set_Scan_Enable_Command(1, duplicates))
    iut.ut_rx_report(event_type=ReportType.SCAN_RSP, data=b'rsp')
    assert iut.ll.scan_parameters.backoff.successes == 1
    for _ in range(2):
        iut.lt_tx(adv_nonconn_ind(data=b'repeat'))
        iut.run(WINDOW_US)
    assert [entry.data for entry in iut.reports()] == [b'repeat'] * (2 - duplicates)


@pytest.mark.parametrize('action', ['disable', 'restart', 'reset'])
def test_scan_response_belongs_to_the_scan_that_sent_the_request(iut, action):
    """Release an active scanner while waiting for SCAN_RSP (Vol 6, Part B, 4.4.3.2).

    Steps:
        1. Receive an ADV_SCAN_IND, transmit SCAN_REQ and pause its response window.
        2. Disable scanning, restart it, or reset and configure it again.
        3. Deliver the old response, then run a fresh active-scan exchange.

    Expected:
        - The first advertisement was reported before the Host command. The old
          response produces no report and consumes no backoff randomness.
        - The fresh scanner reports its own response and records one success.
    """
    scan_legacy(iut, active=True)
    iut.lt_tx(adv_scan_ind(data=b'first'))
    iut.lt_tx(scan_rsp(data=b'old'))
    window = iut.link.roles.scanning.run(iut.link.scan, iut.now_us + WINDOW_US)
    with iut.pause_radio(window, 'receive', occurrence=2):
        iut.ut_rx_report(event_type=ReportType.ADV_SCAN_IND, data=b'first')
        iut.lt_rx(PduType.SCAN_REQ)
        if action == 'reset':
            iut.ut(hci.HCI_Reset_Command())
            iut.ut(hci.HCI_Set_Event_Mask_Command(b'\xff' * 8))
            iut.ut(hci.HCI_LE_Set_Event_Mask_Command(b'\xff' * 8))
            scan_legacy(iut, active=True)
        else:
            stop(iut)
            if action == 'restart':
                iut.ut(hci.HCI_LE_Set_Scan_Enable_Command(1, 0))
        random_state = iut.link.random.getstate()
    assert iut.link.random.getstate() == random_state
    iut.ut_rx_q_is_empty()
    iut.lt_script_is_consumed()

    if action == 'disable':
        iut.ut(hci.HCI_LE_Set_Scan_Enable_Command(1, 0))
    assert iut.ll.scan_parameters.backoff.successes == 0
    iut.lt_tx(adv_scan_ind(data=b'second'))
    iut.lt_tx(scan_rsp(data=b'new'))
    iut.run(WINDOW_US)
    iut.lt_rx(PduType.SCAN_REQ)
    iut.ut_rx_report(event_type=ReportType.ADV_SCAN_IND, data=b'second')
    iut.ut_rx_report(event_type=ReportType.SCAN_RSP, data=b'new')
    assert iut.ll.scan_parameters.backoff.successes == 1
    iut.ut_rx_q_is_empty()


def test_scan_rotates_primary_channels_per_window(iut):
    """The scanner rotates across the primary channels window by window.

    Purpose: Verify that a scanner listens on channels 37, 38 and 39 in turn
    across successive scan windows (Vol 6, Part B, 4.4.3).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Run three scan windows.
        3. Read the channels of the listen windows.

    Expected:
        - The first three listen windows use channels 37, 38, 39 in order.
    """
    scan_legacy(iut)
    iut.run(3 * WINDOW_US)
    channels = [channel for _, _, channel, _ in iut.radio.listens]
    assert channels[:3] == [37, 38, 39]


def test_scan_window_gaps_do_not_listen(iut):
    """The scanner only listens within its window, not across the interval.

    Purpose: Verify that when the scan interval is larger than the window the
    scanner listens only during each window and stays idle in the gap (Vol 6,
    Part B, 4.4.3; Vol 4, Part E, 7.8.10).

    Steps:
        1. Upper Tester enables scanning with interval four times the window.
        2. Run four windows worth of time.
        3. Inspect each listen window's start and length.

    Expected:
        - Each listen starts within the first window of its interval.
        - No listen is longer than one window.
    """
    scan_legacy(iut, interval=4 * SCAN_WINDOW, window=SCAN_WINDOW)
    iut.run(4 * WINDOW_US)
    for start, end, _, _ in iut.radio.listens:
        assert start % (4 * WINDOW_US) < WINDOW_US
        assert end - start <= WINDOW_US


def test_disabled_scanner_does_not_listen_or_report(iut):
    """A disabled scanner neither listens nor reports.

    Purpose: Verify that after Set Scan Enable disable the scanner opens no
    listen windows and reports nothing even when an advertisement is on air
    (Vol 4, Part E, 7.8.11).

    Steps:
        1. Upper Tester enables then disables scanning.
        2. Lower Tester scripts an ADV_IND.
        3. Run one scan window.

    Expected:
        - The radio recorded no listen windows.
        - Nothing reaches the Host.
    """
    scan_legacy(iut)
    stop(iut)
    iut.lt_tx(adv_ind())
    iut.run(WINDOW_US)
    assert not iut.radio.listens
    iut.ut_rx_q_is_empty()


# ---------------------------------------------------------------------------
# Active scanning (4.4.3.2)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('pdu', [adv_ind(data=b'adv'), adv_scan_ind(data=b'adv')])
def test_active_sends_scan_request_and_reports_response(iut, pdu):
    """Active scanning requests a scannable advertiser and reports the response.

    Purpose: Verify that an active scanner sends a SCAN_REQ one T_IFS after a
    scannable advertisement, addressed from the IUT to the peer, and reports both
    the advertisement and the SCAN_RSP (Vol 6, Part B, 4.4.3.2).

    Steps:
        1. Upper Tester configures and enables active scanning.
        2. Lower Tester scripts the scannable advertisement, then a SCAN_RSP.
        3. Run one scan window.
        4. Read the IUT's SCAN_REQ.

    Expected:
        - The SCAN_REQ starts T_IFS after the advertisement, with AdvA the IUT
          and TargetA the peer.
        - The Host gets the advertisement report, then a SCAN_RSP report from the
          peer.
        - Nothing else reaches the Host.

    Parameters range over ADV_IND and ADV_SCAN_IND advertisements.
    """
    scan_legacy(iut, active=True)
    iut.lt_tx(pdu)
    iut.lt_tx(scan_rsp(data=b'rsp'))
    iut.run(WINDOW_US)
    request = iut.lt_rx(PduType.SCAN_REQ)
    advertisement = iut.radio.delivered[0]
    assert request.start_us == advertisement.end_us + LL_IFS_US
    assert address_of(request.pdu) == IUT_ADDRESS
    assert address_of(request.pdu, 8, rx=True) == PEER
    iut.ut_rx_report(data=b'adv')
    iut.ut_rx_report(event_type=ReportType.SCAN_RSP, address=PEER, data=b'rsp')
    iut.ut_rx_q_is_empty()


@pytest.mark.parametrize('pdu', [adv_nonconn_ind(), adv_direct_ind()])
def test_active_does_not_request_nonscannable(iut, pdu):
    """Active scanning does not request a non-scannable advertiser.

    Purpose: Verify that an active scanner does not send a SCAN_REQ for an
    ADV_NONCONN_IND or ADV_DIRECT_IND, and still reports the advertisement
    (Vol 6, Part B, 4.4.3.2).

    Steps:
        1. Upper Tester configures and enables active scanning.
        2. Lower Tester scripts the non-scannable advertisement.
        3. Run one scan window.

    Expected:
        - The scanner transmitted nothing.
        - Exactly one report is produced.

    Parameters range over ADV_NONCONN_IND and ADV_DIRECT_IND.
    """
    scan_legacy(iut, active=True)
    iut.lt_tx(pdu)
    iut.run(WINDOW_US)
    iut.lt_rx_q_is_empty()
    assert len(iut.reports()) == 1


@pytest.mark.parametrize(
    'response',
    [
        None,
        scan_rsp(OTHER, b'rsp'),
        scan_rsp(hci.Address(bytes(PEER), hci.AddressType.PUBLIC_DEVICE), b'rsp'),
        adv_ind(data=b'rsp'),
        bytes([PduType.SCAN_RSP | 0x40, 10]) + bytes(PEER) + b'rsp',
    ],
    ids=['none', 'other-advertiser', 'address-type', 'not-scan-rsp', 'length'],
)
def test_active_unmatched_response_is_a_failure(iut, response):
    """An unmatched or missing SCAN_RSP counts as a backoff failure.

    Purpose: Verify that after sending a SCAN_REQ the scanner treats a missing
    response, a response from another advertiser, a wrong address type, a
    non-SCAN_RSP PDU, or a length-mismatched one as a failure: only the
    advertisement is reported and the backoff failure count increments (Vol 6,
    Part B, 4.4.3.2, 4.4.3.3).

    Steps:
        1. Upper Tester configures and enables active scanning.
        2. Lower Tester scripts an ADV_IND and, unless the case is "none", the
           bad response.
        3. Run one scan window and read the SCAN_REQ.

    Expected:
        - Exactly one report (the advertisement) is produced.
        - Backoff shows one failure and no successes.

    Parameters range over no response, wrong advertiser, wrong address type, a
    non-SCAN_RSP PDU, and a length mismatch.
    """
    scan_legacy(iut, active=True)
    iut.lt_tx(adv_ind())
    if response is not None:
        iut.lt_tx(response)
    iut.run(WINDOW_US)
    iut.lt_rx(PduType.SCAN_REQ)
    # Only the advertisement is reported; the attempt counts as a failure.
    assert len(iut.reports()) == 1
    backoff = iut.ll.scan_parameters.backoff
    assert backoff.failures == 1 and backoff.successes == 0


def test_active_backoff_doubles_after_two_failures(iut):
    """Two scan-request failures double the backoff upper limit.

    Purpose: Verify that after two consecutive active-scan failures the backoff
    upper limit doubles to two and the backoff count falls within it (Vol 6,
    Part B, 4.4.3.3).

    Steps:
        1. Upper Tester configures and enables active scanning.
        2. Twice: Lower Tester scripts an ADV_IND with no response; run a window.
        3. Read the IUT's transmissions.

    Expected:
        - Two SCAN_REQs were sent.
        - The backoff upper limit is 2 and the count is 1 or 2.
    """
    scan_legacy(iut, active=True)
    for _ in range(2):
        iut.lt_tx(adv_ind())
        iut.run(WINDOW_US)
    assert len(iut.lt_rx_all()) == 2
    backoff = iut.ll.scan_parameters.backoff
    assert backoff.upper_limit == 2 and 1 <= backoff.count <= 2


def test_active_backoff_suppresses_requests_until_count_reaches_zero(iut):
    """Backoff suppresses SCAN_REQs until the backoff count reaches zero.

    Purpose: Verify that with a backoff count of three the scanner withholds the
    SCAN_REQ until the count counts down to zero, sending it only on the third
    advertisement while still reporting all three (Vol 6, Part B, 4.4.3.3).

    Steps:
        1. Upper Tester configures and enables active scanning.
        2. Set the backoff count to 3.
        3. Three times: Lower Tester scripts an ADV_IND; run a window.
        4. Read the IUT's transmissions.

    Expected:
        - Exactly one SCAN_REQ was sent (on the third advertisement).
        - All three advertisements are reported.
    """
    scan_legacy(iut, active=True)
    iut.link.scan.backoff.count = 3
    for _ in range(3):
        iut.lt_tx(adv_ind())
        iut.run(WINDOW_US)
    # backoffCount 3 -> 2 -> 1 -> 0: only the third advertisement is requested.
    assert len(iut.lt_rx_all()) == 1
    assert len(iut.reports()) == 3


def test_active_scan_respects_filter_policy(iut):
    """Active scanning filters SCAN_REQ by the scanning filter policy.

    Purpose: Verify that with filter policy 1 an active scanner neither requests
    nor reports an advertiser absent from the Filter Accept List (Vol 4, Part E,
    7.8.10).

    Steps:
        1. Upper Tester enables active scanning with filter policy 1 and no list
           entries.
        2. Lower Tester scripts an ADV_IND from the peer.
        3. Run one scan window.

    Expected:
        - The scanner transmitted nothing.
        - Nothing reaches the Host.
    """
    scan_legacy(iut, active=True, policy=1)
    iut.lt_tx(adv_ind())
    iut.run(WINDOW_US)
    iut.lt_rx_q_is_empty()
    iut.ut_rx_q_is_empty()


# ---------------------------------------------------------------------------
# Scan command validation (7.8.10, 7.8.11)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'fields,status',
    [
        (dict(le_scan_type=2), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (
            dict(le_scan_interval=3, le_scan_window=3),
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
        (dict(le_scan_interval=0x4001), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (
            dict(le_scan_window=SCAN_WINDOW + 1),
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
        (
            dict(own_address_type=4),
            ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
        ),
        (
            dict(scanning_filter_policy=4),
            ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
        ),
    ],
)
def test_scan_parameter_validation(iut, fields, status):
    """Set Scan Parameters rejects invalid and unsupported fields.

    Purpose: Verify that Set Scan Parameters returns the right error for a
    reserved scan type, an out-of-range interval or window, a window larger than
    the interval, and an unsupported own address type or filter policy (Vol 4,
    Part E, 7.8.10).

    Steps:
        1. Upper Tester sends Set Scan Parameters with one invalid field.

    Expected:
        - The command completes with the parametrized error status.

    Parameters range over bad scan type, below-minimum interval/window,
    over-range interval, window larger than interval, unsupported own address
    type, and unsupported filter policy.
    """
    values = dict(
        le_scan_type=0,
        le_scan_interval=SCAN_WINDOW,
        le_scan_window=SCAN_WINDOW,
        own_address_type=0,
        scanning_filter_policy=0,
    )
    iut.ut(hci.HCI_LE_Set_Scan_Parameters_Command(**(values | fields)), status)


def test_scan_parameters_disallowed_while_scanning(iut):
    """Set Scan Parameters is disallowed while scanning.

    Purpose: Verify that Set Scan Parameters is rejected with Command Disallowed
    while scanning is enabled (Vol 4, Part E, 7.8.10).

    Steps:
        1. Upper Tester configures and enables scanning.
        2. Upper Tester sends Set Scan Parameters again.

    Expected:
        - The command completes with COMMAND_DISALLOWED_ERROR.
    """
    scan_legacy(iut)
    iut.ut(
        hci.HCI_LE_Set_Scan_Parameters_Command(0, SCAN_WINDOW, SCAN_WINDOW, 0, 0),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )


@pytest.mark.parametrize('enable,duplicates', [(2, 0), (1, 2)])
def test_scan_enable_validation(iut, enable, duplicates):
    """Set Scan Enable rejects bad enable and duplicate-filter values.

    Purpose: Verify that Set Scan Enable rejects an out-of-range enable flag and
    an out-of-range duplicate-filter value (Vol 4, Part E, 7.8.11).

    Steps:
        1. Upper Tester sends Set Scan Enable with the parametrized values.

    Expected:
        - The command completes with INVALID_COMMAND_PARAMETERS_ERROR.

    Parameters range over enable=2 and duplicate-filter=2.
    """
    iut.ut(
        hci.HCI_LE_Set_Scan_Enable_Command(enable, duplicates),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


def test_filter_list_is_locked_while_in_use(iut):
    """The Filter Accept List cannot be edited while in use.

    Purpose: Verify that adding to the Filter Accept List is disallowed while a
    scanner using it is enabled, and is allowed again once scanning stops (Vol 4,
    Part E, 7.8.15).

    Steps:
        1. Upper Tester enables scanning with filter policy 1.
        2. Upper Tester tries to add the peer to the list.
        3. Upper Tester disables scanning.
        4. Upper Tester adds the peer to the list.

    Expected:
        - The add during scanning fails with COMMAND_DISALLOWED_ERROR.
        - The add after stopping succeeds.
    """
    scan_legacy(iut, policy=1)
    iut.ut(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(PEER.address_type, PEER),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )
    stop(iut)
    accept(iut, PEER)


def test_filter_list_capacity_and_edits(iut):
    """The Filter Accept List enforces capacity and supports edits.

    Purpose: Verify that the Filter Accept List fills to its reported size,
    treats a re-add of an existing entry as no new entry, rejects an add past
    capacity, and supports remove, clear and re-add, while rejecting an invalid
    address type (Vol 4, Part E, 7.8.14-7.8.17).

    Steps:
        1. Read the list size and fill it with distinct addresses.
        2. Re-add an existing entry.
        3. Add one more entry past capacity.
        4. Remove an entry, clear the list, and re-add the peer.
        5. Add with an invalid address type.

    Expected:
        - The re-add does not consume capacity.
        - The over-capacity add fails with MEMORY_CAPACITY_EXCEEDED_ERROR.
        - Remove, clear and the following add succeed.
        - The invalid address type add fails with
          INVALID_COMMAND_PARAMETERS_ERROR.
    """
    size = iut.ut(
        hci.HCI_LE_Read_Filter_Accept_List_Size_Command()
    ).filter_accept_list_size
    for index in range(size):
        accept(
            iut,
            hci.Address(f'C0:00:00:00:00:{index:02X}', hci.AddressType.RANDOM_DEVICE),
        )
    # Re-adding an existing entry is not a new entry.
    accept(iut, hci.Address('C0:00:00:00:00:00', hci.AddressType.RANDOM_DEVICE))
    iut.ut(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(PEER.address_type, PEER),
        ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR,
    )
    iut.ut(
        hci.HCI_LE_Remove_Device_From_Filter_Accept_List_Command(
            PEER.address_type, PEER
        )
    )
    iut.ut(hci.HCI_LE_Clear_Filter_Accept_List_Command())
    accept(iut, PEER)
    iut.ut(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(3, PEER),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


def test_anonymous_filter_entry_ignores_address_bytes(iut):
    """An anonymous Filter Accept List entry is stored with a zero address.

    Purpose: Verify that adding an anonymous-typed entry stores it with the
    canonical all-zero anonymous address, ignoring the supplied address bytes
    (Vol 4, Part E, 7.8.16).

    Steps:
        1. Upper Tester adds the peer with the ANONYMOUS address type.

    Expected:
        - The list contains an all-zero anonymous address entry.
    """
    iut.ut(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(
            hci.AddressType.ANONYMOUS, PEER
        )
    )
    assert hci.Address(bytes(6), hci.AddressType.ANONYMOUS) in iut.link.accept_list


# ---------------------------------------------------------------------------
# Initiating (4.4.4)
# ---------------------------------------------------------------------------
def create_connection(iut, peer=PEER, *, policy=0, status=ErrorCode.SUCCESS, **changes):
    values = dict(
        le_scan_interval=SCAN_WINDOW,
        le_scan_window=SCAN_WINDOW,
        initiator_filter_policy=policy,
        peer_address_type=peer.address_type,
        peer_address=peer,
        own_address_type=hci.OwnAddressType.PUBLIC,
        connection_interval_min=24,
        connection_interval_max=40,
        max_latency=0,
        supervision_timeout=100,
        min_ce_length=0,
        max_ce_length=0,
    )
    iut.ut(hci.HCI_LE_Create_Connection_Command(**(values | changes)), status)


@pytest.mark.parametrize(
    'pdu', [adv_ind(), adv_direct_ind()], ids=['undirected', 'directed']
)
def test_initiate_sends_connect_ind_and_becomes_central(iut, pdu):
    """Initiating sends CONNECT_IND to an eligible advertiser and connects.

    Purpose: Verify that an initiator sends a CONNECT_IND one T_IFS after an
    eligible advertisement, carrying the Host connection parameters and a
    one-unit transmit window, picks CSA #2 when the advertiser's ChSel bit is
    set, and completes as Central with a Channel Selection Algorithm event
    (Vol 6, Part B, 4.4.4; Vol 4, Part E, 7.8.12).

    Steps:
        1. Upper Tester sends Create Connection.
        2. Lower Tester scripts the eligible advertisement.
        3. Run one scan window and read the CONNECT_IND.

    Expected:
        - The CONNECT_IND starts T_IFS after the advertisement, with AdvA the IUT
          and TargetA the peer, interval 24, timeout 100 and window size 1.
        - Its ChSel bit matches the advertisement's.
        - The Host gets Connection Complete as Central with the peer, then a
          Channel Selection Algorithm event matching the ChSel bit, and nothing
          else.
        - Initiating is no longer enabled.

    Parameters range over an undirected and a directed advertisement.
    """
    create_connection(iut)
    iut.lt_tx(pdu)
    iut.run(WINDOW_US)
    request = iut.lt_rx(PduType.CONNECT_IND)
    advertisement = iut.radio.delivered[0]
    assert request.start_us == advertisement.end_us + LL_IFS_US
    assert address_of(request.pdu) == IUT_ADDRESS
    assert address_of(request.pdu, 8, rx=True) == PEER
    parameters = Parameters.decode(request.pdu[14:])
    assert parameters.interval == 24 and parameters.timeout == 100
    assert parameters.window_size == 1
    # The peer's ChSel bit selects CSA #2 for the new connection.
    assert bool(request.pdu[0] & 0x20) == bool(pdu[0] & 0x20)
    event = iut.ut_rx(CONNECTED, status=0, role=hci.Role.CENTRAL, peer_address=PEER)
    iut.ut_rx(
        hci.HCI_LE_Channel_Selection_Algorithm_Event,
        connection_handle=event.connection_handle,
        channel_selection_algorithm=int(bool(pdu[0] & 0x20)),
    )
    iut.ut_rx_q_is_empty()
    assert not iut.ll.initiating_enabled


@pytest.mark.parametrize(
    'pdu',
    [
        adv_ind(OTHER),
        adv_scan_ind(),
        adv_nonconn_ind(),
        adv_direct_ind(target=OTHER),
        adv_ind(PUBLIC_PEER),
        adv_ind()[:7],
    ],
    ids=['other', 'scannable', 'nonconn', 'direct-other', 'type', 'malformed'],
)
def test_initiate_ignores_ineligible_advertisements(iut, pdu):
    """Initiating ignores advertisements it cannot connect to.

    Purpose: Verify that an initiator sends no CONNECT_IND for an advertisement
    from another device, a scannable or non-connectable type, a directed PDU
    aimed elsewhere, the wrong peer address, or a malformed PDU, and keeps
    initiating (Vol 6, Part B, 4.4.4).

    Steps:
        1. Upper Tester sends Create Connection toward the peer.
        2. Lower Tester scripts the ineligible advertisement.
        3. Run one scan window.

    Expected:
        - The initiator transmitted nothing and nothing reaches the Host.
        - Initiating is still enabled.

    Parameters range over other advertiser, scannable, non-connectable,
    directed-to-other, wrong address type, and malformed.
    """
    create_connection(iut)
    iut.lt_tx(pdu)
    iut.run(WINDOW_US)
    iut.lt_rx_q_is_empty()
    iut.ut_rx_q_is_empty()
    assert iut.ll.initiating_enabled


@pytest.mark.parametrize('listed', [False, True])
def test_initiate_with_filter_accept_list(iut, listed):
    """Filtered initiating connects only to a listed advertiser.

    Purpose: Verify that with initiator filter policy 1 the initiator connects to
    an advertiser on the Filter Accept List and ignores one that is absent
    (Vol 4, Part E, 7.8.12).

    Steps:
        1. If listed, Upper Tester adds the other device to the list.
        2. Upper Tester sends Create Connection with filter policy 1.
        3. Lower Tester scripts an ADV_IND from the other device.
        4. Run one scan window.

    Expected:
        - Listed: a CONNECT_IND is sent and the Host gets Connection Complete
          with the other device.
        - Unlisted: the initiator transmitted nothing.

    Parameters range over listed and unlisted.
    """
    if listed:
        accept(iut, OTHER)
    create_connection(iut, policy=1)
    iut.lt_tx(adv_ind(OTHER))
    iut.run(WINDOW_US)
    if listed:
        iut.lt_rx(PduType.CONNECT_IND)
        iut.ut_rx(CONNECTED, status=0, peer_address=OTHER)
    else:
        iut.lt_rx_q_is_empty()


def test_initiate_cancel(iut):
    """Create Connection Cancel aborts initiating and reports it.

    Purpose: Verify that Create Connection Cancel stops an outstanding initiation,
    reports Connection Complete with Unknown Connection Identifier, leaves no
    CONNECT_IND on air for later advertisements, and that a second cancel with
    nothing pending is disallowed (Vol 4, Part E, 7.8.13).

    Steps:
        1. Upper Tester sends Create Connection, then Create Connection Cancel.
        2. Run so the cancellation event is delivered.
        3. Lower Tester scripts an ADV_IND; run one window.
        4. Upper Tester sends Create Connection Cancel again.

    Expected:
        - The Host gets Connection Complete with
          UNKNOWN_CONNECTION_IDENTIFIER_ERROR.
        - No CONNECT_IND is sent afterward.
        - The second cancel fails with COMMAND_DISALLOWED_ERROR.
    """
    create_connection(iut)
    iut.ut(hci.HCI_LE_Create_Connection_Cancel_Command())
    iut.run(1)  # The cancellation event is delivered after Command Complete.
    iut.ut_rx(CONNECTED, status=ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR)
    iut.lt_tx(adv_ind())
    iut.run(WINDOW_US)
    iut.lt_rx_q_is_empty()
    iut.ut(
        hci.HCI_LE_Create_Connection_Cancel_Command(),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )


def test_initiate_busy_and_existing_connection(iut):
    """Create Connection is disallowed while busy or already connected.

    Purpose: Verify that a second Create Connection while one is pending is
    disallowed, and that after a connection forms another Create Connection to
    the same peer reports Connection Already Exists (Vol 4, Part E, 7.8.12).

    Steps:
        1. Upper Tester sends Create Connection.
        2. Upper Tester sends a second Create Connection to another device.
        3. Lower Tester scripts an ADV_IND; run one window; the connection forms.
        4. Upper Tester sends Create Connection to the same peer again.

    Expected:
        - The second concurrent request fails with COMMAND_DISALLOWED_ERROR.
        - The Host gets Connection Complete and a Channel Selection Algorithm
          event.
        - The final request fails with CONNECTION_ALREADY_EXISTS_ERROR.
    """
    create_connection(iut)
    # A second request while one is pending is disallowed (7.8.12).
    create_connection(iut, OTHER, status=ErrorCode.COMMAND_DISALLOWED_ERROR)
    iut.lt_tx(adv_ind())
    iut.run(WINDOW_US)
    iut.ut_rx(CONNECTED, status=0)
    iut.ut_rx(hci.HCI_LE_Channel_Selection_Algorithm_Event)
    create_connection(iut, status=ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR)


@pytest.mark.parametrize(
    'changes',
    [
        dict(le_scan_window=SCAN_WINDOW + 1),
        dict(connection_interval_min=5),
        dict(connection_interval_min=50, connection_interval_max=40),
        dict(supervision_timeout=5),
        dict(min_ce_length=2, max_ce_length=1),
    ],
)
def test_create_connection_validation(iut, changes):
    """Create Connection rejects invalid parameter sets.

    Purpose: Verify that Create Connection is rejected when the scan window
    exceeds the interval, the connection interval is out of range or inverted,
    the supervision timeout is too small, or the CE length range is inverted, and
    that initiating does not start (Vol 4, Part E, 7.8.12).

    Steps:
        1. Upper Tester sends Create Connection with the invalid field set.

    Expected:
        - The command completes with INVALID_COMMAND_PARAMETERS_ERROR.
        - Initiating is not enabled.

    Parameters range over window over interval, interval too small, inverted
    interval range, timeout too small, and inverted CE length range.
    """
    create_connection(iut, status=ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR, **changes)
    assert not iut.ll.initiating_enabled


def test_scanning_and_initiating_share_the_radio(iut):
    """Scanning and initiating alternate on the shared radio.

    Purpose: Verify that enabling both scanning and initiating lets the two roles
    share one radio, each getting listen windows over time (Vol 6, Part B,
    4.4.3-4.4.4).

    Steps:
        1. Upper Tester enables scanning.
        2. Upper Tester sends Create Connection.
        3. Run four scan windows.

    Expected:
        - The radio recorded at least two listen windows.
    """
    scan_legacy(iut)
    create_connection(iut)
    iut.run(4 * WINDOW_US)
    # The scanner context alternates between the two roles.
    assert len(iut.radio.listens) >= 2
