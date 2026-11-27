# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Legacy state combinations advertised by LE Read Supported States."""

import pytest

from bscontroller import hci
from bscontroller.linklayer.connection import Connection
from bscontroller.radio.base import RADIO_SWITCH_TIME_US, Phy
from tests.unit.linklayer.air.advertising import (
    AdvType,
    configure_legacy,
    create_connection,
    enable_legacy,
    scan_legacy,
)
from tests.unit.linklayer.air.harness import (
    CONNECT_PARAMETERS,
    OTHER,
    PEER,
    PUBLIC_PEER,
    PduType,
    address_of,
    adv_ind,
    adv_scan_ind,
    scan_rsp,
)

# Core Vol 4, Part E, 7.8.27. The columns select advertising alone, or with
# a passive scanner, active scanner, initiator, Central or Peripheral.
ADV_STATES = {
    AdvType.ADV_NONCONN_IND: (0, 8, 12, 16, 18, 20),
    AdvType.ADV_SCAN_IND: (1, 9, 13, 17, 19, 21),
    AdvType.ADV_IND: (2, 10, 14, 32, 35, 38),
    AdvType.ADV_DIRECT_IND: (3, 11, 15, 33, 36, 39),
    AdvType.ADV_DIRECT_IND_LOW_DUTY: (29, 30, 31, 34, 37, 40),
}
COLUMNS = (
    (),
    ('passive',),
    ('active',),
    ('initiating',),
    ('central',),
    ('peripheral',),
)
OTHER_STATES = {
    4: ('passive',),
    5: ('active',),
    6: ('initiating',),
    7: ('peripheral',),
    22: ('passive', 'initiating'),
    23: ('active', 'initiating'),
    24: ('passive', 'central'),
    25: ('active', 'central'),
    26: ('passive', 'peripheral'),
    27: ('active', 'peripheral'),
    28: ('initiating', 'central'),
    41: ('initiating', 'peripheral'),
}
STATES = {
    bit: (kind, roles)
    for kind, bits in ADV_STATES.items()
    for bit, roles in zip(bits, COLUMNS, strict=True)
} | {bit: (None, roles) for bit, roles in OTHER_STATES.items()}


@pytest.mark.parametrize('bit', sorted(STATES), ids=lambda bit: f'bit-{bit}')
def test_advertised_legacy_states_share_the_radio(iut, bit):
    """Check each advertised role through its HCI or over-the-air behavior.

    Steps:
        1. Read LE Supported States. Prepare an existing ACL for combinations
           with a Central or Peripheral, then enable the indicated legacy roles.
        2. Answer an ACL event, send a scannable advertisement and a connectable
           advertisement for the enabled roles. Use separate peer addresses.

    Expected:
        The ACL exchanges a data-channel PDU, the advertiser sends its configured
        PDU, and the scanner reports the peer or its active scan response.
        The initiator sends CONNECT_IND to its selected advertiser.
    """
    kind, roles = STATES[bit]
    states = iut.ut(hci.HCI_LE_Read_Supported_States_Command()).le_states
    assert int.from_bytes(states, 'little') == sum(1 << state for state in STATES)
    acl = None
    if 'central' in roles or 'peripheral' in roles:
        role = hci.Role.CENTRAL if 'central' in roles else hci.Role.PERIPHERAL
        acl = Connection(iut.link, CONNECT_PARAMETERS, OTHER, role, iut.now_us)
        iut.link.connections[acl.handle] = acl
        acl.establish()
    if kind is not None:
        configure_legacy(iut, kind, data=b'local', channel_map=1)
        enable_legacy(iut)
    active = 'active' in roles
    scanning = active or 'passive' in roles
    if scanning:
        scan_legacy(iut, active=active)
    if 'initiating' in roles:
        create_connection(iut, PUBLIC_PEER)
    if acl is not None:
        iut.lt_tx(bytes((1, 0)), access_address=acl.parameters.access_address)
    iut.run(8_000)
    if acl is not None:
        assert any(
            p.access_address == acl.parameters.access_address for p in iut.radio.sent
        )
        iut.lt_script_is_consumed()
    if kind is not None:
        expected = (
            PduType.ADV_DIRECT_IND
            if kind == AdvType.ADV_DIRECT_IND_LOW_DUTY
            else PduType[kind.name]
        )
        local = next(
            p for p in iut.radio.sent if p.channel == 37 and p.kind == expected
        )
        if expected != PduType.ADV_DIRECT_IND:
            assert local.pdu[8:] == b'local'
    # Advertise on 38 and 39, away from the local advertiser's response window.
    # Scanning and initiating can use different windows of the same scan interval.
    if scanning:
        for channel in (38, 39, 38):
            iut.lt_tx(adv_scan_ind(PEER, b'peer'), channel=channel)
            if active:
                iut.lt_tx(scan_rsp(PEER, b'reply'), channel=channel)
            iut.run(31_000)
        reports = iut.reports()
        report_type = hci.HCI_LE_Advertising_Report_Event.EventType
        assert any(
            r.address == PEER
            and r.event_type == report_type.ADV_SCAN_IND
            and r.data == b'peer'
            for r in reports
        )
        requests = [
            p for p in iut.radio.sent if p.channel >= 37 and p.kind == PduType.SCAN_REQ
        ]
        assert bool(requests) == active
        if active:
            assert all(address_of(p.pdu, 8, rx=True) == PEER for p in requests)
            assert any(
                r.address == PEER
                and r.event_type == report_type.SCAN_RSP
                and r.data == b'reply'
                for r in reports
            )
    if 'initiating' in roles:
        for channel in (38, 39, 38):
            iut.lt_tx(adv_ind(PUBLIC_PEER), channel=channel)
            iut.run(31_000)
        assert any(
            p.channel in (38, 39)
            and p.kind == PduType.CONNECT_IND
            and p.pdu[8:14] == bytes(PUBLIC_PEER)
            for p in iut.radio.sent
        )


@pytest.mark.parametrize(
    ('remaining_us', 'reported', 'requested'),
    [(854, True, True), (853, True, False), (-1, False, False)],
    ids=['full-response-fits', 'one-us-short', 'advertisement-crosses-reservation'],
)
def test_active_scan_before_acl_anchor(iut, remaining_us, reported, requested):
    """Place ADV_SCAN_IND at the end of a gap before a Central ACL reservation.

    A maximum-length response needs 854 us after the advertisement on LE 1M,
    including both T_IFS and the response start tolerance. Check both HCI reports
    and the first bit of the next ACL packet for each boundary.
    """
    acl = Connection(iut.link, CONNECT_PARAMETERS, OTHER, hci.Role.CENTRAL, iut.now_us)
    iut.link.connections[acl.handle] = acl
    acl.establish()
    acl.anchor_us = anchor = 10_000
    scan_legacy(iut, active=True)
    adv = adv_scan_ind(PEER, b'adv')
    end = anchor - RADIO_SWITCH_TIME_US - remaining_us
    iut.lt_tx(adv, channel=37, at=end - Phy.LE_1M.airtime_us(len(adv)))
    if requested:
        iut.lt_tx(scan_rsp(PEER, b'r' * 31), channel=37)
    iut.run_until(anchor + 500)
    assert [
        p.start_us
        for p in iut.radio.sent
        if p.access_address == acl.parameters.access_address
    ] == [anchor]
    requests = [
        p for p in iut.radio.sent if p.channel == 37 and p.kind == PduType.SCAN_REQ
    ]
    assert bool(requests) == requested
    expected = [b'adv'] if reported else []
    if requested:
        expected.append(b'r' * 31)
    assert [r.data for r in iut.reports()] == expected
