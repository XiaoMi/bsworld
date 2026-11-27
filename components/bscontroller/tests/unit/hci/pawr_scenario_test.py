# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""PAwR H4 commands, airtime validation and event versions (Core Vol 4, Part E,
7.8.61, 7.8.125-127 and 7.7.65.14-15/.24/.36-37)."""

from dataclasses import replace

import pytest

from bscontroller import hci
from bscontroller.linklayer.advertising_pdu import SyncInfo
from bscontroller.linklayer.discovery import PeriodicSync, TransferOrigin
from bscontroller.linklayer.pawr import PawrSync, PawrTiming, SlotResponse
from tests.unit.hci.harness import PEERS
from tests.unit.helpers import extended_parameters

PARAMETERS = hci.HCI_LE_Set_Periodic_Advertising_Parameters_V2_Command
SUBEVENT_DATA = hci.HCI_LE_Set_Periodic_Advertising_Subevent_Data_Command
RESPONSE_DATA = hci.HCI_LE_Set_Periodic_Advertising_Response_Data_Command
SUBEVENTS = hci.HCI_LE_Set_Periodic_Sync_Subevent_Command
TIMING = PawrTiming(5, 48, 8, 80)
INFO = SyncInfo(0, 255, (1 << 37) - 1, 0x12345678, 0x123456, 0)


def parameters(**changes):
    values = dict(
        advertising_handle=0,
        periodic_advertising_interval_min=255,
        periodic_advertising_interval_max=255,
        periodic_advertising_properties=0,
        num_subevents=5,
        subevent_interval=48,
        response_slot_delay=8,
        response_slot_spacing=80,
        num_response_slots=5,
    )
    return PARAMETERS(**(values | changes))


def advertise(host, **changes):
    host.cmd(extended_parameters())
    host.cmd(parameters(**changes))
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    host.in_loop(host.link.roles.advertising.prepare)
    host.events.clear()
    return host.link.advertisers[0]


def synchronized(host):
    sync = PeriodicSync(
        PEERS[0],
        1,
        10_000_000,
        10_000_000,
        info=INFO,
        handle=1,
        established=True,
        next_us=100_000,
        pawr=PawrSync(TIMING, 0x87654321),
    )
    host.link.syncs[1] = sync
    return sync


@pytest.mark.parametrize(
    'changes,status',
    [
        ({'num_subevents': 129}, 0x12),
        ({'subevent_interval': 5}, 0x12),
        ({'subevent_interval': 52}, 0x12),
        ({'response_slot_delay': 0}, 0x12),
        ({'response_slot_delay': 48}, 0x12),
        ({'response_slot_delay': 255}, 0x12),
        ({'response_slot_spacing': 1}, 0x12),
        ({'num_response_slots': 0}, 0x12),
        (
            {
                'subevent_interval': 24,
                'response_slot_delay': 12,
                'response_slot_spacing': 25,
            },
            0x12,
        ),
        (
            {
                'num_subevents': 0,
                'subevent_interval': 5,
                'response_slot_delay': 255,
                'response_slot_spacing': 1,
                'num_response_slots': 0,
            },
            0,
        ),
        ({'num_subevents': 1, 'subevent_interval': 0}, 0),
    ],
)
def test_parameter_ranges(host, changes, status):
    """Send invalid boundary combinations and check the reply's handle and status.

    Purpose:
        Send invalid boundary combinations and check the reply's handle and status.

    Steps:
        1. Configure a set and send each boundary parameter combination over H4.

    Expected:
        - The status and echoed handle match; ignored fields do not create PAwR state.
    """
    host.cmd(extended_parameters())
    result = host.cmd(parameters(**changes), status)
    assert result.wire[7] == 0
    if not status:
        assert (host.link.advertisers[0].pawr is None) == (
            changes.get('num_subevents') == 0
        )


@pytest.mark.parametrize(
    'subevents,starts,counts,data,status',
    [
        ([], [], [], [], 0x12),
        ([128], [0], [1], [b'x'], 0x12),
        ([0, 0], [0, 0], [1, 1], [b'x', b'y'], 0x12),
        ([0], [4], [2], [b'x'], 0x12),
        ([5], [0], [1], [b'x'], 0x0C),
    ],
)
def test_subevent_data_validation(host, subevents, starts, counts, data, status):
    """Reject invalid entries atomically, leaving the request unfilled.

    Purpose:
        Reject invalid entries atomically, leaving the request unfilled.

    Steps:
        1. Enable PAwR, then submit an invalid subevent-data array.

    Expected:
        - Reject the whole command without storing any entry.
    """
    item = advertise(host)
    host.cmd(SUBEVENT_DATA(0, subevents, starts, counts, data), status)
    assert not item.pawr_state.data


def test_subevent_data_is_single_use(host):
    """Fill a request, reject duplicate publication and consume its payload once.

    Purpose:
        Fill a request, reject duplicate publication and consume its payload once.

    Steps:
        1. Fill a window, reject its duplicate, consume it and fill the next window.

    Expected:
        - Each requested instance receives its data once; old windows are rejected.
    """
    item = advertise(
        host,
        num_subevents=10,
        periodic_advertising_interval_min=480,
        periodic_advertising_interval_max=480,
    )
    host.cmd(SUBEVENT_DATA(0, [0], [0], [1], [b'first']))
    host.cmd(SUBEVENT_DATA(0, [0], [0], [1], [b'changed']), 0x0C)
    current = host.in_loop(host.link.roles.advertising.begin_subevent, item)
    assert current.data.data == b'first'
    assert 0 not in item.pawr_state.data
    host.cmd(SUBEVENT_DATA(0, [1], [0], [1], [b'old window']), 0x0C)
    host.cmd(SUBEVENT_DATA(0, [8], [0], [1], [b'next window']))
    assert item.pawr_state.data[8].data == b'next window'


@pytest.mark.parametrize('length,status', [(126, 0), (127, 0x45)])
def test_indication_airtime_limit(host, length, status):
    """Check the 1M payload boundary before 1.25 ms minus 150 us T_IFS.

    Purpose:
        Check the 1M payload boundary before 1.25 ms minus 150 us T_IFS.

    Steps:
        1. Configure 1M with RSD 1.25 ms; submit 126 and 127 payload octets.

    Expected:
        - 126 octets succeed and 127 return Packet Too Long.
    """
    advertise(
        host,
        num_subevents=1,
        periodic_advertising_interval_min=16,
        periodic_advertising_interval_max=16,
        response_slot_delay=1,
        response_slot_spacing=10,
        num_response_slots=3,
    )
    host.cmd(SUBEVENT_DATA(0, [0], [0], [3], [bytes(length)]), status)


@pytest.mark.parametrize(
    'factory,raw',
    [
        (SUBEVENT_DATA, bytes([0, 1, 0, 0, 1, 255]) + bytes(240)),
        (RESPONSE_DATA, bytes([1, 0, 0, 0, 0, 0, 0, 255]) + bytes(240)),
        (SUBEVENTS, bytes([1, 0, 0, 0, 3, 0])),
    ],
)
def test_truncated_variable_parameters_return_status(host, factory, raw):
    """Send malformed H4 arrays; expect 0x12 and a usable command channel.

    Purpose:
        Send malformed H4 arrays; expect 0x12 and a usable command channel.

    Steps:
        1. Send an H4 command whose declared array or payload is truncated.

    Expected:
        - Return Invalid Parameters and preserve the command channel.
    """
    advertise(host)
    synchronized(host)
    command = factory.from_parameters(raw)
    assert not command.complete
    host.cmd(command, 0x12)


@pytest.mark.parametrize(
    'subevents,status', [([], 0x12), ([128], 0x12), ([5], 0x12), ([0, 2, 4], 0)]
)
def test_sync_subevents_replace_selection(host, subevents, status):
    """Replace the synchronized subevents; reject values outside the train.

    Purpose:
        Replace the synchronized subevents; reject values outside the train.

    Steps:
        1. Select a new subevent list on a five-subevent synchronized train.

    Expected:
        - Valid values replace the list; invalid values leave it unchanged.
    """
    sync = synchronized(host)
    host.cmd(SUBEVENTS(1, 0, subevents), status)
    assert sync.pawr.subevents == (tuple(subevents) if not status else (0,))


@pytest.mark.parametrize('length,status', [(126, 0), (127, 0x45)])
def test_response_airtime_limit(host, length, status):
    """Check response airtime independently of the optional AdvA.

    Purpose:
        Check response airtime independently of the optional AdvA.

    Steps:
        1. Submit 126 and 127 response octets for RSS 1.25 ms.

    Expected:
        - Only the shorter encoded response fits and is accepted.
    """
    sync = synchronized(host)
    sync.pawr.timing = replace(TIMING, response_slot_spacing=10)
    host.cmd(RESPONSE_DATA(1, 0, 0, 0, 0, bytes(length)), status)


@pytest.mark.parametrize(
    'event,request_subevent,response,status',
    [
        (0, 0, 2, 0x0C),
        (0, 0, 5, 0x0C),
        (0xFFFF, 0, 0, 0x46),
    ],
)
def test_response_target_validation(host, event, request_subevent, response, status):
    """Reject unsynchronized subevents and slots whose request is already past.

    Purpose:
        Reject unsynchronized subevents and slots whose request is already past.

    Steps:
        1. Request responses for an unselected, nonexistent or already past target.

    Expected:
        - Return Command Disallowed or Too Late as appropriate.
    """
    synchronized(host)
    host.cmd(RESPONSE_DATA(1, event, request_subevent, response, 0, b'x'), status)


@pytest.mark.parametrize('transfer', [False, True])
@pytest.mark.parametrize('version', [0, 1, 2])
def test_sync_event_versions(host, transfer, version):
    """Enable both, v1 only or neither; emit only the newest enabled version.

    Purpose:
        Enable both, v1 only or neither; emit only the newest enabled version.

    Steps:
        1. Enable both versions, v1 only or neither; emit the sync notification.

    Expected:
        - Deliver exactly the newest enabled event with the configured PAwR timing.
    """
    family = (
        (
            hci.HCI_LE_Periodic_Advertising_Sync_Transfer_Received_Event,
            hci.HCI_LE_Periodic_Advertising_Sync_Transfer_Received_V2_Event,
        )
        if transfer
        else (
            hci.HCI_LE_Periodic_Advertising_Sync_Established_Event,
            hci.HCI_LE_Periodic_Advertising_Sync_Established_V2_Event,
        )
    )
    mask = sum(1 << (event.subevent_code - 1) for event in family[:version])
    host.cmd(hci.HCI_LE_Set_Event_Mask_Command(mask.to_bytes(8, 'little')))
    sync = synchronized(host)
    sync.transfer = TransferOrigin(1, 0x1234) if transfer else None
    host.notify(
        'on_sync_transfer_received' if transfer else 'on_sync_established', sync, 0
    )
    if version:
        event = host.ut_rx(family[version - 1], sync_handle=1)
        if version == 2:
            assert (
                event.num_subevents,
                event.subevent_interval,
                event.response_slot_delay,
                event.response_slot_spacing,
            ) == (5, 48, 8, 80)
    host.ut_rx_q_is_empty()


def test_response_reports_fragment_and_preserve_slot_order(host):
    """Report received/missing slots; split long responses below 255 HCI octets.

    Purpose:
        Report received/missing slots; split long responses below 255 HCI octets.

    Steps:
        1. Report a 249-octet response, a missing slot and a final short response.

    Expected:
        - Fragments fit HCI, retain slot order and reconstruct the exact data.
    """
    advertise(host)
    source = [
        SlotResponse(0, bytes(range(249)), -42, -5),
        SlotResponse(1, None),
        SlotResponse(2, b'tail', -43),
    ]
    host.notify('on_response_report', host.link.advertisers[0], 3, True, source)
    entries = []
    while host.events:
        event = host.ut_rx(
            hci.HCI_LE_Periodic_Advertising_Response_Report_Event,
            advertising_handle=0,
            subevent=3,
            tx_status=0,
        )
        assert event.wire[2] <= 255
        entries.extend(event.responses)
    assert [entry.response_slot for entry in entries] == [0, 0, 1, 2]
    assert [entry.data_status for entry in entries] == [1, 0, 255, 0]
    assert b''.join(entry.data for entry in entries[:2]) == bytes(range(249))
