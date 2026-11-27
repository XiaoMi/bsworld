# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Extended scanning and initiating (Core Vol 6, Part B, 4.4.3.3-4.4.3.4,
4.4.4.2; Vol 4, Part E, 7.8.64-7.8.66, 7.7.65.13).

Matrix: {primary only, AUX_ADV_IND, AUX_CHAIN_IND} x {complete, truncated,
broken chain} x {address in primary / auxiliary, directed, anonymous} x
{passive, active AUX_SCAN, AUX_CONNECT} x {1M, 2M, Coded}.

Here the IUT scans or initiates and the Lower Tester plays the extended
advertiser. Scenarios run on the ``air/harness.py`` harness; ``send_event``
scripts an ADV_EXT_IND on a primary channel followed by AUX_ADV_IND /
AUX_CHAIN_IND / AUX_SCAN_RSP / AUX_CONNECT_RSP on the auxiliary channel, offset
by their AuxPtr. ``status_of`` reads the Data Status field of an extended report
(COMPLETE / INCOMPLETE / TRUNCATED). The advertiser side lives in
``extended_advertising_test.py``, periodic synchronization in
``periodic_test.py``.
"""

from dataclasses import replace

import pytest

from bscontroller.hci import HCI_LE_SCAN_INTERVAL_UNIT_US
from bscontroller.linklayer import LL_IFS_US, AdvertisingMode
from bscontroller.linklayer.advertising_pdu import AuxPtr, ExtendedPdu
from bscontroller.linklayer.parameters import Parameters
from bscontroller.radio.base import Phy
from tests.unit.linklayer.air.advertising import create_extended_connection
from tests.unit.linklayer.air.harness import (
    IUT_ADDRESS,
    OTHER,
    PEER,
    ErrorCode,
    PduType,
    address_of,
    hci,
)

Report = hci.HCI_LE_Extended_Advertising_Report_Event
WINDOW = 160  # 100 ms keeps a whole chain inside one scan window
WINDOW_US = WINDOW * HCI_LE_SCAN_INTERVAL_UNIT_US
AUX_CHANNEL = 12
AUX_OFFSET = 3000
CONNECTED = hci.HCI_LE_Enhanced_Connection_Complete_Event
COMPLETE, INCOMPLETE, TRUNCATED = 0, 1, 2

# Event_Type bits (Vol 4, Part E, 7.7.65.13).
CONNECTABLE, SCANNABLE, DIRECTED, SCAN_RESPONSE, LEGACY = 1, 2, 4, 8, 16


def status_of(report):
    return (report.event_type >> 5) & 3


def scan(
    iut,
    *,
    active=False,
    phys=hci.PhyBit.LE_1M,
    policy=0,
    duration=0,
    period=0,
    duplicates=0,
):
    count = bin(phys).count('1')
    iut.ut(
        hci.HCI_LE_Set_Extended_Scan_Parameters_Command(
            own_address_type=hci.OwnAddressType.PUBLIC,
            scanning_filter_policy=policy,
            scanning_phys=phys,
            scan_types=[int(active)] * count,
            scan_intervals=[WINDOW] * count,
            scan_windows=[WINDOW] * count,
        )
    )
    iut.ut(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, duplicates, duration, period))


def primary(
    mode=0, *, sid=2, did=7, phy=Phy.LE_1M, address=None, target=None, offset=AUX_OFFSET
):
    return ExtendedPdu(
        mode=mode,
        adi=sid << 12 | did,
        address=bytes(address) if address else None,
        random_address=bool(address and address.is_random),
        target=bytes(target) if target else None,
        random_target=bool(target and target.is_random),
        aux_ptr=AuxPtr(AUX_CHANNEL, offset, phy),
    )


def auxiliary(
    data=b'',
    *,
    mode=0,
    sid=2,
    did=7,
    advertiser=PEER,
    target=None,
    more=None,
    phy=Phy.LE_1M,
    **fields,
):
    return ExtendedPdu(
        mode=mode,
        adi=sid << 12 | did,
        address=bytes(advertiser) if advertiser else None,
        random_address=bool(advertiser and advertiser.is_random),
        target=bytes(target) if target else None,
        random_target=bool(target and target.is_random),
        aux_ptr=AuxPtr(AUX_CHANNEL, more, phy) if more else None,
        data=data,
        **fields,
    )


def chain(data=b'', *, sid=2, did=7, more=None, phy=Phy.LE_1M, **fields):
    return ExtendedPdu(
        adi=sid << 12 | did,
        aux_ptr=AuxPtr(AUX_CHANNEL, more, phy) if more else None,
        data=data,
        **fields,
    )


def send_event(iut, *pdus, phy=Phy.LE_1M, primary_phy=None, gap=AUX_OFFSET):
    """Primary on a primary channel, then each PDU ``gap`` after the previous."""
    first, *rest = pdus
    iut.lt_tx(first, phy=primary_phy or phy)
    for pdu in rest:
        iut.lt_tx(pdu, phy=phy, channel=AUX_CHANNEL, after=gap)


# ---------------------------------------------------------------------------
# Passive extended scanning
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('phy', list(Phy))
def test_aux_adv_ind_is_followed_and_reported(iut, phy):
    """The scanner follows an AuxPtr and reports the AUX_ADV_IND contents.

    Purpose: Verify that a passive extended scanner follows the AuxPtr from
    ADV_EXT_IND to AUX_ADV_IND and reports one complete extended advertisement
    with the advertiser address, data, primary and secondary PHYs, SID and
    TxPower, listening on the auxiliary channel at the pointed PHY (Vol 6,
    Part B, 4.4.3.3; Vol 4, Part E, 7.7.65.13).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Lower Tester scripts an ADV_EXT_IND on a primary channel pointing to an
           AUX_ADV_IND on the parametrized secondary PHY carrying data and
           TxPower -4.
        3. Run one scan window.

    Expected:
        - The Host gets one report from the peer with the data, primary PHY 1M,
          secondary PHY equal to the parametrized PHY, SID 2 and TxPower -4.
        - The report event type is 0 (complete, non-legacy) and nothing else
          reaches the Host.
        - The auxiliary listen opened on the auxiliary channel at the pointed
          PHY.

    Parameters range over every secondary PHY.
    """
    scan(iut)
    send_event(
        iut,
        primary(phy=phy),
        auxiliary(b'\x02\x01\x06', tx_power=-4),
        phy=phy,
        primary_phy=Phy.LE_1M,
    )
    iut.run(WINDOW_US)
    report = iut.ut_rx_report(
        address=PEER,
        data=b'\x02\x01\x06',
        primary_phy=Phy.LE_1M,
        secondary_phy=phy,
        advertising_sid=2,
        tx_power=-4,
    )
    assert report.event_type == 0
    iut.ut_rx_q_is_empty()
    # The auxiliary listen opens around AuxPtr's offset on its channel and PHY.
    aux_listens = [entry for entry in iut.radio.listens if entry[2] == AUX_CHANNEL]
    assert [entry[3] for entry in aux_listens] == [phy]


def test_primary_without_auxptr_is_reported_from_primary(iut):
    """An ADV_EXT_IND with no AuxPtr is reported from the primary PDU.

    Purpose: Verify that an ADV_EXT_IND carrying an address but no AuxPtr is
    reported directly, with empty data, no secondary PHY and the unknown SID
    value (Vol 6, Part B, 4.4.3.3; Vol 4, Part E, 7.7.65.13).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Lower Tester scripts an ADV_EXT_IND with the peer's random address and
           no AuxPtr.
        3. Run one scan window.

    Expected:
        - The Host gets one report from the peer with empty data, secondary PHY 0
          and SID 0xFF.
    """
    scan(iut)
    iut.lt_tx(ExtendedPdu(address=bytes(PEER), random_address=True))
    iut.run(WINDOW_US)
    iut.ut_rx_report(address=PEER, data=b'', secondary_phy=0, advertising_sid=0xFF)


@pytest.mark.parametrize('pieces', [2, 3, 7])
def test_chain_reassembles_complete_data(iut, pieces):
    """An AUX_CHAIN_IND chain reassembles into fragmented complete reports.

    Purpose: Verify that the scanner follows an AUX_CHAIN_IND chain, reassembles
    the full AdvData, and surfaces it as HCI report fragments that are
    "incomplete, more to come" until the final complete one, each within the HCI
    fragment size limit (Vol 6, Part B, 4.4.3.3; Vol 4, Part E, 7.7.65.13).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Lower Tester scripts a primary PDU, an AUX_ADV_IND and the chain
           carrying the parametrized number of 200-octet pieces.
        3. Run one scan window and collect the reports.

    Expected:
        - The joined report data equals the full concatenated pieces.
        - Every report but the last has Data Status INCOMPLETE; the last is
          COMPLETE.
        - No report carries more than 229 octets.

    Parameters range over 2, 3 and 7 chain pieces.
    """
    scan(iut)
    data = [bytes([index]) * 200 for index in range(pieces)]
    pdus = [auxiliary(data[0], more=AUX_OFFSET)]
    pdus += [chain(piece, more=AUX_OFFSET) for piece in data[1:-1]]
    pdus += [chain(data[-1])]
    send_event(iut, primary(), *pdus)
    iut.run(WINDOW_US)
    reports = iut.reports()
    assert b''.join(report.data for report in reports) == b''.join(data)
    # HCI fragments are "incomplete, more to come" until the final one.
    assert [status_of(report) for report in reports[:-1]] == [INCOMPLETE] * (
        len(reports) - 1
    )
    assert status_of(reports[-1]) == COMPLETE
    assert all(len(report.data) <= 229 for report in reports)


@pytest.mark.parametrize(
    'fault',
    ['missing', 'adi', 'mode', 'malformed', 'empty-continuation', 'phy'],
)
def test_broken_chain_is_reported_truncated(iut, fault):
    """A chain broken partway is reported as truncated with the data so far.

    Purpose: Verify that when the chain breaks (missing continuation, wrong ADI,
    wrong mode, malformed PDU, empty continuation, or a PHY change) the scanner
    reports the data received so far and marks the final fragment TRUNCATED
    (Vol 6, Part B, 4.4.3.3; Vol 4, Part E, 7.7.65.13).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Lower Tester scripts a primary PDU and an AUX_ADV_IND carrying "head"
           pointing onward, then the fault-specific continuation (or none).
        3. Run one scan window and collect the reports.

    Expected:
        - The joined report data is "head".
        - The final report's Data Status is TRUNCATED.

    Parameters range over fault kinds: missing, adi, mode, malformed,
    empty-continuation, phy.
    """
    scan(iut)
    pdus = [primary(), auxiliary(b'head', more=AUX_OFFSET)]
    if fault == 'adi':
        pdus.append(chain(b'tail', did=8))
    elif fault == 'mode':
        pdus.append(replace(chain(b'tail'), mode=AdvertisingMode.CONNECTABLE))
    elif fault == 'malformed':
        pdus.append(bytes([PduType.ADV_EXT_IND, 1, 0x3F]))
    elif fault == 'empty-continuation':
        pdus.append(chain(b'', more=AUX_OFFSET))
    elif fault == 'phy':
        pdus[1] = auxiliary(b'head', more=AUX_OFFSET, phy=Phy.LE_2M)
        pdus.append(chain(b'tail'))
    send_event(iut, *pdus)
    iut.run(WINDOW_US)
    reports = iut.reports()
    assert b''.join(report.data for report in reports) == b'head'
    assert status_of(reports[-1]) == TRUNCATED


def test_chain_longer_than_maximum_is_truncated(iut):
    """A chain beyond the maximum report length is truncated.

    Purpose: Verify that when a chain carries more AdvData than the scanner can
    report it stops at the maximum length and marks the final fragment TRUNCATED
    (Vol 6, Part B, 4.4.3.3; Vol 4, Part E, 7.7.65.13).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Lower Tester scripts a primary PDU and a chain of ten 200-octet PDUs.
        3. Run one scan window and collect the reports.

    Expected:
        - The total reported data is at most 1650 octets and a multiple of 200.
        - The final report's Data Status is TRUNCATED.
    """
    scan(iut)
    pdus = [auxiliary(bytes(200), more=AUX_OFFSET)]
    pdus += [chain(bytes(200), more=AUX_OFFSET) for _ in range(8)]
    pdus += [chain(bytes(200))]
    send_event(iut, primary(), *pdus)
    iut.run(WINDOW_US)
    reports = iut.reports()
    total = sum(len(report.data) for report in reports)
    assert total <= 1650 and total % 200 == 0
    assert status_of(reports[-1]) == TRUNCATED


def test_auxptr_that_points_before_mafs_is_not_followed(iut):
    """An AuxPtr pointing sooner than T_MAFS is not followed.

    Purpose: Verify that the scanner ignores an AuxPtr whose offset is too small
    to leave the required T_MAFS gap, reporting nothing (Vol 6, Part B, 2.3.4.5,
    4.4.3.3).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Lower Tester scripts a primary PDU with a 30 us AuxPtr offset and an
           AUX_ADV_IND 30 us later.
        3. Run one scan window.

    Expected:
        - Nothing reaches the Host.
    """
    scan(iut)
    send_event(iut, primary(offset=30), auxiliary(b'x'), gap=30)
    iut.run(WINDOW_US)
    iut.ut_rx_q_is_empty()


@pytest.mark.parametrize(
    'mismatch',
    [dict(did=9), dict(sid=3), dict(mode=AdvertisingMode.SCANNABLE)],
    ids=['did', 'sid', 'mode'],
)
def test_auxiliary_must_match_primary(iut, mismatch):
    """An AUX_ADV_IND that disagrees with the primary PDU is dropped.

    Purpose: Verify that the scanner discards the auxiliary when its DID, SID or
    advertising mode disagrees with the ADV_EXT_IND that pointed to it, reporting
    nothing (Vol 6, Part B, 4.4.3.3).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Lower Tester scripts a primary PDU and an AUX_ADV_IND with the
           mismatching field.
        3. Run one scan window.

    Expected:
        - Nothing reaches the Host.

    Parameters range over DID, SID and mode mismatches.
    """
    scan(iut)
    send_event(iut, primary(), auxiliary(b'x', **mismatch))
    iut.run(WINDOW_US)
    iut.ut_rx_q_is_empty()


@pytest.mark.parametrize('location', ['primary', 'auxiliary'])
def test_advertiser_address_from_either_pdu(iut, location):
    """The advertiser address is taken from whichever PDU carries it.

    Purpose: Verify that the scanner reports the advertiser address whether it is
    carried in the ADV_EXT_IND or in the AUX_ADV_IND (Vol 6, Part B, 4.4.3.3).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Lower Tester scripts the event with the address in the primary PDU or
           in the auxiliary PDU, per the parameter.
        3. Run one scan window.

    Expected:
        - The report carries the peer address with its address type.

    Parameters range over the address in the primary and in the auxiliary PDU.
    """
    scan(iut)
    if location == 'primary':
        send_event(iut, primary(address=PEER), auxiliary(b'x', advertiser=None))
    else:
        send_event(iut, primary(), auxiliary(b'x'))
    iut.run(WINDOW_US)
    iut.ut_rx_report(address=PEER, address_type=PEER.address_type)


def test_anonymous_advertising_is_reported_as_anonymous(iut):
    """An advertisement with no address is reported as anonymous.

    Purpose: Verify that an extended event with no AdvA in either PDU is reported
    with the anonymous address type and its data (Vol 6, Part B, 4.4.3.3; Vol 4,
    Part E, 7.7.65.13).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Lower Tester scripts a primary PDU and an AUX_ADV_IND with no
           advertiser address, carrying "anon".
        3. Run one scan window.

    Expected:
        - The report has the ANONYMOUS address type and data "anon".
    """
    scan(iut)
    send_event(iut, primary(), auxiliary(b'anon', advertiser=None))
    iut.run(WINDOW_US)
    iut.ut_rx_report(address_type=hci.AddressType.ANONYMOUS, data=b'anon')


@pytest.mark.parametrize('target', [IUT_ADDRESS, OTHER])
def test_directed_auxiliary_is_reported_only_to_its_target(iut, target):
    """A directed extended event is reported only to its target.

    Purpose: Verify that a directed AUX_ADV_IND is reported (with the directed
    bit and the TargetA as DirectAddress) when its target is the IUT, and
    ignored when it is directed at another device (Vol 6, Part B, 4.4.3.3;
    Vol 4, Part E, 7.7.65.13).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Lower Tester scripts a primary PDU and a directed AUX_ADV_IND aimed at
           the parametrized target.
        3. Run one scan window and collect the reports.

    Expected:
        - Target is the IUT: one report with the directed bit set and
          DirectAddress equal to the IUT.
        - Target is another device: no report.

    Parameters range over the IUT and another device as target.
    """
    scan(iut)
    send_event(iut, primary(), auxiliary(b'd', target=target))
    iut.run(WINDOW_US)
    reports = iut.reports()
    if target == IUT_ADDRESS:
        (report,) = reports
        assert report.event_type & DIRECTED
        assert report.direct_address == IUT_ADDRESS
    else:
        assert not reports


def test_legacy_pdu_reported_through_extended_event(iut):
    """A legacy advertisement is reported through an extended report.

    Purpose: Verify that an extended scanner reports a legacy ADV_IND via an
    extended advertising report, with the legacy, connectable and scannable bits
    set and no secondary PHY (Vol 6, Part B, 4.4.3.3; Vol 4, Part E,
    7.7.65.13).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Lower Tester scripts a legacy ADV_IND carrying "legacy".
        3. Run one scan window.

    Expected:
        - The report carries "legacy" with secondary PHY 0.
        - Its event type is LEGACY | CONNECTABLE | SCANNABLE.
    """
    from tests.unit.linklayer.air.harness import adv_ind

    scan(iut)
    iut.lt_tx(adv_ind(data=b'legacy'))
    iut.run(WINDOW_US)
    report = iut.ut_rx_report(data=b'legacy', secondary_phy=0)
    assert report.event_type == LEGACY | CONNECTABLE | SCANNABLE


@pytest.mark.parametrize('listed', [False, True])
def test_filter_policy_applies_to_auxiliary_address(iut, listed):
    """Filtered scanning matches the auxiliary advertiser address.

    Purpose: Verify that with filter policy 1 the scanner reports an extended
    event only when the advertiser address carried in the AUX_ADV_IND is on the
    Filter Accept List (Vol 4, Part E, 7.8.64).

    Steps:
        1. If listed, Upper Tester adds the peer to the Filter Accept List.
        2. Upper Tester enables scanning with filter policy 1.
        3. Lower Tester scripts a primary PDU and an AUX_ADV_IND from the peer.
        4. Run one scan window and collect the reports.

    Expected:
        - One report when listed, none otherwise.

    Parameters range over listed and unlisted.
    """
    if listed:
        iut.ut(
            hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(PEER.address_type, PEER)
        )
    scan(iut, policy=1)
    send_event(iut, primary(), auxiliary(b'x'))
    iut.run(WINDOW_US)
    assert len(iut.reports()) == int(listed)


def test_coded_primary_scanning(iut):
    """Scanning on the Coded PHY follows a Coded event.

    Purpose: Verify that a scanner configured for the Coded PHY receives an
    ADV_EXT_IND and AUX_ADV_IND on the Coded PHY and reports both primary and
    secondary PHY as Coded (Vol 6, Part B, 4.4.3.3).

    Steps:
        1. Upper Tester enables scanning on the Coded PHY.
        2. Lower Tester scripts a Coded-PHY event (primary and auxiliary).
        3. Run one scan window.

    Expected:
        - The report shows primary PHY and secondary PHY both Coded.
    """
    scan(iut, phys=hci.PhyBit.LE_CODED)
    send_event(
        iut,
        primary(phy=Phy.LE_CODED),
        auxiliary(b'c', phy=Phy.LE_CODED),
        phy=Phy.LE_CODED,
    )
    iut.run(WINDOW_US)
    iut.ut_rx_report(primary_phy=Phy.LE_CODED, secondary_phy=Phy.LE_CODED)


def test_dual_phy_scanning_alternates(iut):
    """Scanning on two PHYs listens on both over time.

    Purpose: Verify that a scanner configured for both the 1M and Coded PHYs opens
    listen windows on each (Vol 6, Part B, 4.4.3.3; Vol 4, Part E, 7.8.64).

    Steps:
        1. Upper Tester enables scanning on the 1M and Coded PHYs.
        2. Run four scan windows.
        3. Collect the PHYs of the listen windows.

    Expected:
        - Both LE 1M and LE Coded appear among the listen PHYs.
    """
    scan(iut, phys=hci.PhyBit.LE_1M | hci.PhyBit.LE_CODED)
    iut.run(4 * WINDOW_US)
    phys = {phy for _, _, _, phy in iut.radio.listens}
    assert phys == {Phy.LE_1M, Phy.LE_CODED}


# ---------------------------------------------------------------------------
# Active extended scanning: AUX_SCAN_REQ / AUX_SCAN_RSP
# ---------------------------------------------------------------------------
def scan_response(data, *, advertiser=PEER, more=None, **fields):
    return ExtendedPdu(
        address=bytes(advertiser),
        random_address=advertiser.is_random,
        aux_ptr=AuxPtr(AUX_CHANNEL, more) if more else None,
        data=data,
        **fields,
    )


def test_active_sends_aux_scan_req_and_reports_response(iut):
    """Active scanning requests a scannable set and reports the response.

    Purpose: Verify that an active extended scanner sends an AUX_SCAN_REQ on the
    auxiliary channel one T_IFS after the scannable AUX_ADV_IND, addressed from
    the IUT to the peer, reports the empty scannable advertisement then the
    AUX_SCAN_RSP data, and records a backoff success (Vol 6, Part B, 4.4.3.4).

    Steps:
        1. Upper Tester enables active scanning.
        2. Lower Tester scripts a scannable event and an AUX_SCAN_RSP carrying
           "response".
        3. Run one scan window and read the SCAN_REQ.

    Expected:
        - The SCAN_REQ is on the auxiliary channel one T_IFS after the AUX_ADV_IND,
          with AdvA the IUT and TargetA the peer.
        - The Host gets a scannable report with empty data, then a
          scannable+scan-response report with "response", and nothing else.
        - The backoff records one success.
    """
    scan(iut, active=True)
    send_event(
        iut,
        primary(AdvertisingMode.SCANNABLE),
        auxiliary(mode=AdvertisingMode.SCANNABLE),
    )
    iut.lt_tx(scan_response(b'response'), channel=AUX_CHANNEL)
    iut.run(WINDOW_US)
    request = iut.lt_rx(PduType.SCAN_REQ)
    aux = iut.radio.delivered[1]
    assert request.channel == AUX_CHANNEL
    assert request.start_us == aux.end_us + LL_IFS_US
    assert address_of(request.pdu) == IUT_ADDRESS
    assert address_of(request.pdu, 8, rx=True) == PEER
    iut.ut_rx_report(event_type=SCANNABLE, data=b'')
    iut.ut_rx_report(event_type=SCANNABLE | SCAN_RESPONSE, data=b'response')
    iut.ut_rx_q_is_empty()
    assert iut.ll.scan_parameters.backoff.successes == 1


@pytest.mark.parametrize(
    'stage', ['primary', 'auxiliary', 'request', 'response', 'response-chain']
)
def test_reenable_starts_a_new_scan_period_during_an_exchange(iut, stage):
    """Restart an extended scan at successive RF boundaries (Vol 4, Part E, 7.8.65).

    Steps:
        1. Start active scanning and script a scannable extended advertising event.
        2. Re-enable with a new duration during primary RX, auxiliary RX, request
           TX, response RX, or the first response-chain RX.
        3. Send a fresh advertising event and answer its scan request.

    Expected:
        - Previously issued radio operations finish. The old exchange submits
          no further radio work or reports and leaves the new backoff untouched.
        - The next exchange delivers its advertisement and scan response.
    """
    scan(iut, active=True)
    iut.lt_tx(primary(AdvertisingMode.SCANNABLE))
    if stage != 'primary':
        iut.lt_tx(
            auxiliary(mode=AdvertisingMode.SCANNABLE),
            channel=AUX_CHANNEL,
            after=AUX_OFFSET,
        )
    if stage in ('response', 'response-chain'):
        more = AUX_OFFSET if stage == 'response-chain' else None
        iut.lt_tx(scan_response(b'old', more=more), channel=AUX_CHANNEL)
    if stage == 'response-chain':
        iut.lt_tx(chain(b'tail'), channel=AUX_CHANNEL, after=AUX_OFFSET)
    method, occurrence = {
        'primary': ('receive', 1),
        'auxiliary': ('receive', 2),
        'request': ('transmit', 1),
        'response': ('receive', 3),
        'response-chain': ('receive', 4),
    }[stage]
    window = iut.link.roles.scanning.run(iut.link.scan, iut.now_us + WINDOW_US)
    with iut.pause_radio(window, method, occurrence=occurrence):
        if stage in ('request', 'response', 'response-chain'):
            iut.ut_rx_report(event_type=SCANNABLE, data=b'')
        iut.ut(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 20, 0))
        started = iut.now_us
        random_state = iut.link.random.getstate()
        listens = len(iut.radio.listens)
    assert len(iut.radio.listens) == listens + (method == 'receive')
    assert iut.link.random.getstate() == random_state
    assert iut.ll.scan_parameters.deadline_us == started + 200_000
    assert iut.ll.scan_parameters.backoff.successes == 0
    assert iut.ll.scan_parameters.backoff.failures == 0
    assert [tx.kind for tx in iut.lt_rx_all()] == (
        [PduType.SCAN_REQ] if stage in ('request', 'response', 'response-chain') else []
    )
    iut.lt_script_is_consumed()
    iut.ut_rx_q_is_empty()

    send_event(
        iut,
        primary(AdvertisingMode.SCANNABLE),
        auxiliary(mode=AdvertisingMode.SCANNABLE),
    )
    iut.lt_tx(scan_response(b'new'), channel=AUX_CHANNEL)
    iut.run(WINDOW_US)
    iut.lt_rx(PduType.SCAN_REQ)
    iut.ut_rx_report(event_type=SCANNABLE, data=b'')
    iut.ut_rx_report(event_type=SCANNABLE | SCAN_RESPONSE, data=b'new')
    assert iut.ll.scan_parameters.backoff.successes == 1
    iut.ut_rx_q_is_empty()


def test_duplicate_filter_keeps_scan_response_apart_from_advertisement(iut):
    """A scan response is not a duplicate of the advertisement sharing its ADI.

    Purpose: Verify that with duplicate filtering, the scannable advertisement
    and its AUX_SCAN_RSP, which share one ADI, are each reported once, and a
    repeat of both under the same ADI is filtered (Vol 6, Part B, 2.3.4.4,
    4.4.3.5).

    Steps:
        1. Upper Tester enables active scanning with duplicate filtering.
        2. Twice, Lower Tester scripts the scannable event with SID 2, DID 7 and
           an AUX_SCAN_RSP carrying "response"; run one scan window each.

    Expected:
        - The Host gets the scannable report and the scan-response report once
          each, and nothing else.
    """
    scan(iut, active=True, duplicates=1)
    for _ in range(2):
        send_event(
            iut,
            primary(AdvertisingMode.SCANNABLE),
            auxiliary(mode=AdvertisingMode.SCANNABLE),
        )
        iut.lt_tx(scan_response(b'response'), channel=AUX_CHANNEL)
        iut.run(WINDOW_US)
    iut.ut_rx_report(event_type=SCANNABLE, data=b'')
    iut.ut_rx_report(event_type=SCANNABLE | SCAN_RESPONSE, data=b'response')
    iut.ut_rx_q_is_empty()


def test_active_follows_scan_response_chain(iut):
    """Active scanning follows a chained AUX_SCAN_RSP.

    Purpose: Verify that the scanner follows an AUX_SCAN_RSP that chains onward
    and reassembles the scan-response data across the fragments (Vol 6, Part B,
    4.4.3.4).

    Steps:
        1. Upper Tester enables active scanning.
        2. Lower Tester scripts a scannable event, an AUX_SCAN_RSP "first" that
           points onward, and a continuation "second".
        3. Run one scan window and collect the reports.

    Expected:
        - The joined scan-response report data is "firstsecond".
    """
    scan(iut, active=True)
    send_event(
        iut,
        primary(AdvertisingMode.SCANNABLE),
        auxiliary(mode=AdvertisingMode.SCANNABLE),
    )
    iut.lt_tx(scan_response(b'first', more=AUX_OFFSET), channel=AUX_CHANNEL)
    # AUX_SCAN_RSP has no ADI, so its continuation carries none either.
    iut.lt_tx(ExtendedPdu(data=b'second'), channel=AUX_CHANNEL, after=AUX_OFFSET)
    iut.run(WINDOW_US)
    reports = iut.reports()
    assert (
        b''.join(r.data for r in reports if r.event_type & SCAN_RESPONSE)
        == b'firstsecond'
    )


@pytest.mark.parametrize(
    'response',
    [
        None,
        scan_response(b'x', advertiser=OTHER),
        scan_response(b'x', adi=0x2008),
        scan_response(b'x', mode=AdvertisingMode.SCANNABLE),
        bytes([PduType.ADV_EXT_IND, 1, 0x3F]),
    ],
    ids=['none', 'other-advertiser', 'other-adi', 'mode', 'malformed'],
)
def test_active_unmatched_scan_response_is_a_failure(iut, response):
    """An unmatched AUX_SCAN_RSP counts as a backoff failure.

    Purpose: Verify that after sending an AUX_SCAN_REQ the scanner treats a
    missing response, one from another advertiser, one with the wrong ADI, one
    with the wrong mode, or a malformed PDU as a failure: only the advertisement
    is reported and the backoff failure count increments (Vol 6, Part B,
    4.4.3.4).

    Steps:
        1. Upper Tester enables active scanning.
        2. Lower Tester scripts a scannable event and, unless "none", the bad
           response.
        3. Run one scan window and read the SCAN_REQ.

    Expected:
        - Exactly one report (the advertisement) is produced.
        - The backoff records one failure.

    Parameters range over no response, wrong advertiser, wrong ADI, wrong mode,
    and malformed.
    """
    scan(iut, active=True)
    send_event(
        iut,
        primary(AdvertisingMode.SCANNABLE),
        auxiliary(mode=AdvertisingMode.SCANNABLE),
    )
    if response is not None:
        iut.lt_tx(response, channel=AUX_CHANNEL)
    iut.run(WINDOW_US)
    iut.lt_rx(PduType.SCAN_REQ)
    assert len(iut.reports()) == 1
    assert iut.ll.scan_parameters.backoff.failures == 1


def test_active_does_not_request_nonscannable(iut):
    """Active extended scanning does not request a non-scannable event.

    Purpose: Verify that an active extended scanner sends no AUX_SCAN_REQ for a
    non-scannable extended event (Vol 6, Part B, 4.4.3.4).

    Steps:
        1. Upper Tester enables active scanning.
        2. Lower Tester scripts a non-scannable event (primary and AUX_ADV_IND).
        3. Run one scan window.

    Expected:
        - The scanner transmitted nothing.
    """
    scan(iut, active=True)
    send_event(iut, primary(), auxiliary(b'x'))
    iut.run(WINDOW_US)
    iut.lt_rx_q_is_empty()


def test_passive_does_not_request_scannable(iut):
    """Passive scanning reports a scannable event without requesting it.

    Purpose: Verify that a passive extended scanner reports a scannable event but
    never sends an AUX_SCAN_REQ (Vol 6, Part B, 4.4.3.3).

    Steps:
        1. Upper Tester enables passive scanning.
        2. Lower Tester scripts a scannable event (primary and AUX_ADV_IND).
        3. Run one scan window.

    Expected:
        - The scanner transmitted nothing.
        - The Host gets a scannable report.
    """
    scan(iut)
    send_event(
        iut,
        primary(AdvertisingMode.SCANNABLE),
        auxiliary(mode=AdvertisingMode.SCANNABLE),
    )
    iut.run(WINDOW_US)
    iut.lt_rx_q_is_empty()
    iut.ut_rx_report(event_type=SCANNABLE)


# ---------------------------------------------------------------------------
# Scan duration and period (7.8.65)
# ---------------------------------------------------------------------------
def test_duration_without_period_reports_scan_timeout(iut):
    """A scan duration without a period ends with a Scan Timeout.

    Purpose: Verify that extended scanning with a duration but no period stops at
    the deadline, reports an LE Scan Timeout, and opens no further listen windows
    (Vol 4, Part E, 7.8.65).

    Steps:
        1. Upper Tester enables scanning with a 200 ms duration.
        2. Run past the duration.
        3. Record the listen count, run one more window.

    Expected:
        - The Host gets an LE Scan Timeout event.
        - Scanning is no longer enabled.
        - No new listen windows open after the timeout.
    """
    scan(iut, duration=20)  # 200 ms
    iut.run(250_000)
    iut.ut_rx(hci.HCI_LE_Scan_Timeout_Event)
    assert not iut.ll.scanning_enabled
    count = len(iut.radio.listens)
    iut.run(WINDOW_US)
    assert len(iut.radio.listens) == count


def test_duration_with_period_restarts_scanning(iut):
    """A scan duration with a period restarts scanning each period.

    Purpose: Verify that extended scanning with a duration and a period scans for
    the duration, stays idle until the next period, and resumes at the period
    boundary, reporting no Scan Timeout (Vol 4, Part E, 7.8.65).

    Steps:
        1. Upper Tester enables scanning with a 100 ms duration every 1.28 s.
        2. Run 1.5 s.
        3. Inspect the listen window start times.

    Expected:
        - Nothing reaches the Host (no Scan Timeout).
        - There are listens in the first 100 ms, none between 100 ms and 1.28 s,
          and more in the window starting at 1.28 s.
    """
    scan(iut, duration=10, period=1)  # 100 ms every 1.28 s
    iut.run(1_500_000)
    iut.ut_rx_q_is_empty()
    starts = [start for start, _, _, _ in iut.radio.listens]
    assert any(start < 100_000 for start in starts)
    assert not any(100_000 <= start < 1_280_000 for start in starts)
    assert any(1_280_000 <= start < 1_380_000 for start in starts)


@pytest.mark.parametrize('duration,period', [(0, 1), (1, 0)])
def test_invalid_scan_restart_preserves_the_current_period(iut, duration, period):
    """Validate the new period before replacing the scan (Vol 4, Part E, 7.8.65).

    Steps:
        1. Enable timed scanning, then request per-period duplicate filtering
           with either Duration or Period zero.
        2. Disable scanning with reserved Filter_Duplicates and Duration > Period.

    Expected:
        - The rejected restart leaves the scanner and its deadline intact.
        - Disable succeeds and ignores all fields after Enable.
    """
    scan(iut, duration=5)
    original = iut.link.scan
    iut.ut(
        hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 2, duration, period),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    assert iut.link.scan is original and original.deadline_us == 50_000
    iut.ut(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0xFF, 65535, 1))
    assert not iut.ll.scanning_enabled


@pytest.mark.parametrize('duplicates,report_count', [(1, 1), (2, 2)])
def test_reenable_respects_the_duplicate_filter_period(iut, duplicates, report_count):
    """Re-enable scanning using the same duplicate filter (Vol 4, Part E, 7.8.65).

    Steps:
        1. Receive an advertisement, re-enable with identical settings and
           receive the same ADI again.

    Expected:
        - Filter_Duplicates 1 suppresses the second report. Filter_Duplicates 2
          starts a fresh duplicate history for the new period.
    """
    scan(iut, duplicates=duplicates, duration=2, period=1)
    send_event(iut, primary(), auxiliary(b'same'))
    iut.run(10_000)
    iut.ut(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, duplicates, 2, 1))
    send_event(iut, primary(), auxiliary(b'same'))
    iut.run(10_000)
    assert [report.data for report in iut.reports()] == [b'same'] * report_count


def test_duration_not_shorter_than_period_is_invalid(iut):
    """A scan duration not shorter than its period is rejected.

    Purpose: Verify that Set Extended Scan Enable rejects a duration that is not
    shorter than the period (Vol 4, Part E, 7.8.65).

    Steps:
        1. Upper Tester enables scanning with a duration that equals or exceeds
           the period.

    Expected:
        - The command fails with INVALID_COMMAND_PARAMETERS_ERROR.
    """
    iut.ut(
        hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 128, 1),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


@pytest.mark.parametrize('duplicates', [1, 2])
def test_duplicate_filter_resets_each_period_only_when_requested(iut, duplicates):
    """Duplicate filtering resets each period only when the reset mode is set.

    Purpose: Verify that with duplicate filtering value 2 (reset each period) the
    scanner reports the same advertisement again in the next period, while with
    plain filtering value 1 it stays suppressed (Vol 4, Part E, 7.8.65).

    Steps:
        1. Upper Tester sets extended scan parameters.
        2. Upper Tester enables scanning with the parametrized duplicate mode, a
           100 ms duration and a 1.28 s period.
        3. Lower Tester scripts the same event in the first period, run 200 ms.
        4. Lower Tester scripts it again, run through the next period.

    Expected:
        - Two reports when duplicates=2 (reset each period), one otherwise.

    Parameters range over duplicate modes 1 and 2.
    """
    iut.ut(
        hci.HCI_LE_Set_Extended_Scan_Parameters_Command(
            0, 0, hci.PhyBit.LE_1M, [0], [WINDOW], [WINDOW]
        )
    )
    iut.ut(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, duplicates, 10, 1))
    send_event(iut, primary(), auxiliary(b'dup'))
    iut.run(200_000)
    send_event(iut, primary(), auxiliary(b'dup'))
    iut.run(1_200_000)
    assert len(iut.reports()) == (2 if duplicates == 2 else 1)


def test_duplicate_filter_tracks_the_previous_did_per_advertising_set(iut):
    """A DID may recur after a different DID in the same advertising set.

    Purpose: Verify that each address and SID has its own previous DID (Vol 6,
    Part B, 4.4.2.11 and 4.4.3.5).

    Steps:
        1. Enable passive scanning with duplicate filtering.
        2. Send DID 7 from the peer on SID 2. Interleave another SID and another
           advertiser before repeating the first report.
        3. Change the first set's DID to 8 while keeping its data, then use DID
           7 again with new data.

    Expected:
        - The Host receives each set's first report and both DID changes. The
          intervening sets do not cause the repeated report to be delivered.
    """
    scan(iut, duplicates=1)
    name_a, name_b = b'\x02\x09A', b'\x02\x09B'
    for address, sid, did, data in (
        (PEER, 2, 7, name_a),
        (PEER, 3, 8, name_a),
        (OTHER, 2, 8, name_a),
        (PEER, 2, 7, name_a),
        (PEER, 2, 8, name_a),
        (PEER, 2, 7, name_b),
    ):
        send_event(
            iut,
            primary(sid=sid, did=did),
            auxiliary(data, advertiser=address, sid=sid, did=did),
        )
        iut.run(WINDOW_US)
    iut.lt_script_is_consumed()
    assert [(r.address, r.advertising_sid, r.data) for r in iut.reports()] == [
        (PEER, 2, name_a),
        (PEER, 3, name_a),
        (OTHER, 2, name_a),
        (PEER, 2, name_a),
        (PEER, 2, name_b),
    ]


def test_duplicate_filter_reports_longer_prefixes_of_the_same_chain(iut):
    """Repeated receptions can recover more data under an unchanged DID.

    Purpose: Verify that a longer truncated report and the final complete
    report reach the Host (Vol 6, Part B, 4.4.3.5).

    Steps:
        1. Enable passive scanning with duplicate filtering.
        2. Transmit the same three-fragment advertisement in each scan window.
           Deliver one fragment, then two, repeat both prefixes, then deliver
           the full chain followed by a prefix and another full reception.

    Expected:
        - The Host receives the first and longer prefixes marked TRUNCATED,
          then the whole advertisement marked COMPLETE, each once.
    """
    scan(iut, duplicates=1)
    head, middle, tail = b'\x02\x09A', b'\x02\x0a\x08', b'\x02\x01\x06'
    pdus = (
        primary(),
        auxiliary(head, more=AUX_OFFSET),
        chain(middle, more=AUX_OFFSET),
        chain(tail),
    )
    for count in (1, 2, 1, 2, 3, 1, 3):
        send_event(iut, *pdus[: 1 + count])
        iut.run(WINDOW_US)
    iut.lt_script_is_consumed()
    assert [(status_of(r), r.data) for r in iut.reports()] == [
        (TRUNCATED, head),
        (TRUNCATED, head + middle),
        (COMPLETE, head + middle + tail),
    ]


@pytest.mark.parametrize(
    'phys,status',
    [
        (0, ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (hci.PhyBit.LE_2M, ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR),
    ],
)
def test_extended_scan_parameter_validation(iut, phys, status):
    """Set Extended Scan Parameters rejects invalid and unsupported PHYs.

    Purpose: Verify that Set Extended Scan Parameters rejects an empty scanning
    PHY set and the unsupported LE 2M scanning PHY (Vol 4, Part E, 7.8.64).

    Steps:
        1. Upper Tester sends Set Extended Scan Parameters with the parametrized
           PHY set.

    Expected:
        - The command completes with the parametrized error status.

    Parameters range over no PHYs (invalid) and LE 2M (unsupported).
    """
    iut.ut(
        hci.HCI_LE_Set_Extended_Scan_Parameters_Command(
            0, 0, phys, [0], [WINDOW], [WINDOW]
        ),
        status,
    )


def test_extended_scan_bad_second_entry_commits_nothing(iut):
    """A bad per-PHY entry rejects Set Extended Scan Parameters atomically.

    Purpose: Verify that when one PHY's scan-type entry is invalid the whole Set
    Extended Scan Parameters command is rejected and no scan windows are stored
    (Vol 4, Part E, 7.8.64).

    Steps:
        1. Upper Tester sends Set Extended Scan Parameters for 1M and Coded with
           an invalid scan type for the second entry.

    Expected:
        - The command fails with INVALID_COMMAND_PARAMETERS_ERROR.
        - No scan windows are stored.
    """
    iut.ut(
        hci.HCI_LE_Set_Extended_Scan_Parameters_Command(
            0,
            0,
            hci.PhyBit.LE_1M | hci.PhyBit.LE_CODED,
            [0, 2],
            [WINDOW] * 2,
            [WINDOW] * 2,
        ),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    assert not iut.ll.scan_parameters.windows


# ---------------------------------------------------------------------------
# Extended initiating: AUX_CONNECT_REQ / AUX_CONNECT_RSP (4.4.4.2)
# ---------------------------------------------------------------------------
def connect_response(advertiser=PEER, target=IUT_ADDRESS, **changes):
    values = dict(
        kind=PduType.AUX_CONNECT_RSP,
        address=bytes(advertiser),
        random_address=advertiser.is_random,
        target=bytes(target),
        random_target=target.is_random,
    )
    return ExtendedPdu(**(values | changes))


def test_cancel_during_aux_follow_cannot_initiate_for_a_new_request(iut):
    """Cancel and reissue Create Connection while receiving AUX_ADV_IND (7.8.66).

    Steps:
        1. Follow a connectable primary advertisement. During auxiliary RX,
           cancel initiation and start another request for the same peer.
        2. Deliver the old AUX_ADV_IND, then a fresh advertisement and response.

    Expected:
        - Only the fresh advertisement elicits AUX_CONNECT_REQ and creates an ACL.
    """
    create_extended_connection(iut)
    mode = AdvertisingMode.CONNECTABLE
    send_event(iut, primary(mode), auxiliary(mode=mode))
    window = iut.link.roles.scanning.run(iut.link.initiating, iut.now_us + WINDOW_US)
    with iut.pause_radio(window, 'receive', occurrence=2):
        iut.ut(hci.HCI_LE_Create_Connection_Cancel_Command())
        create_extended_connection(iut)
    iut.ut_rx(CONNECTED, status=ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR)
    iut.ut_rx_q_is_empty()
    iut.lt_rx_q_is_empty()
    send_event(iut, primary(mode), auxiliary(mode=mode))
    iut.lt_tx(connect_response(), channel=AUX_CHANNEL)
    iut.run(10_000)
    iut.lt_rx(PduType.CONNECT_IND)
    iut.ut_rx(CONNECTED, status=0, peer_address=PEER)


@pytest.mark.parametrize('phy', list(Phy))
def test_aux_connect_creates_central_connection(iut, phy):
    """Extended initiating sends CONNECT_IND and connects as Central.

    Purpose: Verify that an extended initiator on a connectable event sends a
    CONNECT_IND on the auxiliary channel and secondary PHY, applies the per-PHY
    Host connection parameters, completes as Central using CSA #2, and raises a
    Channel Selection Algorithm event (Vol 6, Part B, 4.4.4.2; Vol 4, Part E,
    7.8.66).

    Steps:
        1. Upper Tester sends Extended Create Connection on all three PHYs.
        2. Lower Tester scripts a connectable event on the parametrized PHY and an
           AUX_CONNECT_RSP.
        3. Run until just after the exchange and read the CONNECT_IND.

    Expected:
        - The CONNECT_IND is on the auxiliary channel and the event's PHY, with
          TargetA the peer.
        - Its connection interval matches the per-PHY parameter set
          (1M:24, 2M:32, Coded:40).
        - The Host gets Connection Complete as Central with the peer and a CSA #2
          Channel Selection Algorithm event.
        - The connection uses the event PHY for TX and RX and CSA #2.

    Parameters range over every PHY, with the Coded case using a Coded primary.
    """
    phys = hci.PhyBit.LE_1M | hci.PhyBit.LE_2M | hci.PhyBit.LE_CODED
    create_extended_connection(iut, phys=phys)
    primary_phy = Phy.LE_CODED if phy == Phy.LE_CODED else Phy.LE_1M
    send_event(
        iut,
        primary(AdvertisingMode.CONNECTABLE, phy=phy),
        auxiliary(mode=AdvertisingMode.CONNECTABLE, phy=phy),
        phy=phy,
        primary_phy=primary_phy,
    )
    iut.lt_tx(connect_response(), channel=AUX_CHANNEL, phy=phy)
    # 1M and Coded primary windows alternate; the Coded event lands in the second.
    # Stop right after the exchange: the peer never joins the new connection.
    iut.run(WINDOW_US * (2 if primary_phy == Phy.LE_CODED else 1) - 50_000)
    request = iut.lt_rx(PduType.CONNECT_IND)
    assert request.channel == AUX_CHANNEL and request.phy == phy
    assert address_of(request.pdu, 8, rx=True) == PEER
    parameters = Parameters.decode(request.pdu[14:])
    # Each PHY uses its own Host parameter set (7.8.66).
    assert parameters.interval == {Phy.LE_1M: 24, Phy.LE_2M: 32, Phy.LE_CODED: 40}[phy]
    event = iut.ut_rx(CONNECTED, status=0, role=hci.Role.CENTRAL, peer_address=PEER)
    iut.ut_rx(
        hci.HCI_LE_Channel_Selection_Algorithm_Event,
        connection_handle=event.connection_handle,
        channel_selection_algorithm=1,
    )
    (connection,) = iut.link.connections.values()
    assert connection.tx_phy == connection.rx_phy == phy and connection.csa2


@pytest.mark.parametrize(
    'response',
    [
        None,
        connect_response(advertiser=OTHER),
        connect_response(target=OTHER),
        connect_response(mode=AdvertisingMode.CONNECTABLE),
        connect_response(random_target=True),
    ],
    ids=['none', 'other-advertiser', 'other-target', 'mode', 'target-type'],
)
def test_aux_connect_without_valid_response_keeps_initiating(iut, response):
    """An unmatched AUX_CONNECT_RSP leaves initiating active.

    Purpose: Verify that after sending a CONNECT_IND the initiator forms no
    connection and keeps initiating when the response is missing, from another
    advertiser, aimed at another target, the wrong mode, or has the wrong target
    address type (Vol 6, Part B, 4.4.4.2).

    Steps:
        1. Upper Tester sends Extended Create Connection.
        2. Lower Tester scripts a connectable event and, unless "none", the bad
           response.
        3. Run one scan window and read the CONNECT_IND.

    Expected:
        - No connection is created and initiating is still enabled.
        - Nothing reaches the Host.

    Parameters range over no response, wrong advertiser, wrong target, wrong
    mode, and wrong target address type.
    """
    create_extended_connection(iut)
    send_event(
        iut,
        primary(AdvertisingMode.CONNECTABLE),
        auxiliary(mode=AdvertisingMode.CONNECTABLE),
    )
    if response is not None:
        iut.lt_tx(response, channel=AUX_CHANNEL)
    iut.run(WINDOW_US)
    iut.lt_rx(PduType.CONNECT_IND)
    assert not iut.link.connections and iut.ll.initiating_enabled
    iut.ut_rx_q_is_empty()


@pytest.mark.parametrize(
    'event',
    ['nonconnectable', 'other-advertiser', 'directed-other'],
)
def test_initiating_ignores_ineligible_extended_events(iut, event):
    """Extended initiating ignores events it cannot connect to.

    Purpose: Verify that the initiator sends no CONNECT_IND for a non-connectable
    event, a connectable event from another advertiser, or a directed
    connectable event aimed at another device, and keeps initiating (Vol 6,
    Part B, 4.4.4.2).

    Steps:
        1. Upper Tester sends Extended Create Connection toward the peer.
        2. Lower Tester scripts the ineligible event.
        3. Run one scan window.

    Expected:
        - The initiator transmitted nothing and nothing reaches the Host.

    Parameters range over non-connectable, other-advertiser, and directed-to-other
    events.
    """
    create_extended_connection(iut)
    if event == 'nonconnectable':
        send_event(iut, primary(), auxiliary(b'x'))
    elif event == 'other-advertiser':
        send_event(
            iut,
            primary(AdvertisingMode.CONNECTABLE),
            auxiliary(mode=AdvertisingMode.CONNECTABLE, advertiser=OTHER),
        )
    else:
        send_event(
            iut,
            primary(AdvertisingMode.CONNECTABLE),
            auxiliary(mode=AdvertisingMode.CONNECTABLE, target=OTHER),
        )
    iut.run(WINDOW_US)
    iut.lt_rx_q_is_empty()
    iut.ut_rx_q_is_empty()


def test_extended_initiating_with_filter_list(iut):
    """Filtered extended initiating connects to a listed advertiser.

    Purpose: Verify that with initiator filter policy 1 the extended initiator
    connects to an advertiser on the Filter Accept List (Vol 4, Part E,
    7.8.66).

    Steps:
        1. Upper Tester adds the other device to the Filter Accept List.
        2. Upper Tester sends Extended Create Connection with filter policy 1.
        3. Lower Tester scripts a connectable event from the other device and an
           AUX_CONNECT_RSP.
        4. Run one scan window.

    Expected:
        - The Host gets Connection Complete with the other device.
    """
    iut.ut(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(OTHER.address_type, OTHER)
    )
    create_extended_connection(iut, policy=1)
    send_event(
        iut,
        primary(AdvertisingMode.CONNECTABLE),
        auxiliary(mode=AdvertisingMode.CONNECTABLE, advertiser=OTHER),
    )
    iut.lt_tx(connect_response(advertiser=OTHER), channel=AUX_CHANNEL)
    iut.run(WINDOW_US)
    iut.ut_rx(CONNECTED, status=0, peer_address=OTHER)


def test_legacy_advertiser_via_extended_create_connection(iut):
    """Extended initiating connects to a legacy advertiser.

    Purpose: Verify that an extended initiator connects to a legacy ADV_IND by
    sending a CONNECT_IND and completing the connection (Vol 6, Part B,
    4.4.4.2).

    Steps:
        1. Upper Tester sends Extended Create Connection toward the peer.
        2. Lower Tester scripts a legacy ADV_IND.
        3. Run one scan window and read the CONNECT_IND.

    Expected:
        - A CONNECT_IND is sent.
        - The Host gets Connection Complete.
    """
    from tests.unit.linklayer.air.harness import adv_ind

    create_extended_connection(iut)
    iut.lt_tx(adv_ind())
    iut.run(WINDOW_US)
    iut.lt_rx(PduType.CONNECT_IND)
    iut.ut_rx(CONNECTED, status=0)


@pytest.mark.parametrize(
    'phys,status',
    [
        (hci.PhyBit.LE_2M, ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (0x08, ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR),
    ],
)
def test_extended_create_connection_phy_validation(iut, phys, status):
    """Extended Create Connection rejects invalid and unsupported PHY sets.

    Purpose: Verify that Extended Create Connection rejects an initiating PHY set
    whose bits are invalid for initiating and one that names an unsupported PHY
    (Vol 4, Part E, 7.8.66).

    Steps:
        1. Upper Tester sends Extended Create Connection with the parametrized PHY
           set.

    Expected:
        - The command completes with the parametrized error status.

    Parameters range over LE 2M alone (invalid for initiating) and an unsupported
    PHY bit.
    """
    create_extended_connection(iut, phys=phys, status=status)


def test_extended_create_connection_parameter_validation(iut):
    """Extended Create Connection rejects a window larger than the interval.

    Purpose: Verify that Extended Create Connection is rejected when the scan
    window exceeds the scan interval (Vol 4, Part E, 7.8.66).

    Steps:
        1. Upper Tester sends Extended Create Connection with the scan window one
           unit larger than the interval.

    Expected:
        - The command fails with INVALID_COMMAND_PARAMETERS_ERROR.
    """
    iut.ut(
        hci.HCI_LE_Extended_Create_Connection_Command(
            0,
            0,
            PEER.address_type,
            PEER,
            hci.PhyBit.LE_1M,
            [WINDOW],
            [WINDOW + 1],
            [24],
            [40],
            [0],
            [100],
            [0],
            [0],
        ),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


@pytest.mark.parametrize('location', ['primary', 'auxiliary'])
def test_target_from_either_pdu_and_primary_wins(iut, location):
    """On 1M, non-connectable primary fields stand for the event (Table 2.5).

    Purpose: Verify that the directed TargetA is reported from either PDU, and
    that on the 1M PHY a non-connectable ADV_EXT_IND's TargetA takes precedence
    over the auxiliary's, so a primary target aimed at the IUT is reported as
    directed even when the auxiliary names a different target (Vol 6, Part B,
    4.4.3.3, Table 2.5).

    Steps:
        1. Upper Tester configures and enables passive scanning.
        2. Lower Tester scripts either a primary PDU targeting the IUT with an
           auxiliary targeting another device, or a plain primary with an
           auxiliary targeting the IUT.
        3. Run one scan window.

    Expected:
        - The report's DirectAddress is the IUT and its directed bit is set.

    Parameters range over the target carried in the primary and in the auxiliary
    PDU.
    """
    scan(iut)
    if location == 'primary':
        send_event(
            iut,
            primary(target=IUT_ADDRESS),
            auxiliary(b'd', target=OTHER),
        )
    else:
        send_event(iut, primary(), auxiliary(b'd', target=IUT_ADDRESS))
    iut.run(WINDOW_US)
    report = iut.ut_rx_report(direct_address=IUT_ADDRESS)
    assert report.event_type & DIRECTED
