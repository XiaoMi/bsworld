# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""PAwR radio scheduling and lifecycle (Core Vol 6, Part B, 4.4.2.12.2).

Exercise an indication, its timed response, missed subevents, Host skip, counter
wrap and stopping a train while response slots are pending.
"""

from dataclasses import replace

import pytest

from bscontroller import hci
from bscontroller.linklayer import LL_IFS_US
from bscontroller.linklayer.advertising_pdu import (
    ExtendedPdu,
    SyncInfo,
    connect_request,
    connect_response,
)
from bscontroller.linklayer.channel_selection import channel_selection_2
from bscontroller.linklayer.discovery import Initiating, PeriodicSync
from bscontroller.linklayer.initiating_radio import pawr_connect
from bscontroller.linklayer.pawr import PawrSync, PawrTiming
from bscontroller.radio.base import Phy
from tests.unit.linklayer.air.advertising import configure_extended, enable_extended
from tests.unit.linklayer.air.harness import (
    CONNECT_PARAMETERS,
    IUT_ADDRESS,
    PEER,
    PduType,
)

TIMING = PawrTiming(3, 16, 8, 10)
INFO = SyncInfo(0, 80, (1 << 37) - 1, 0x12345678, 0x123456, 0)
RSP_AA = 0x87654321
REPORT = hci.HCI_LE_Periodic_Advertising_Report_V2_Event


def sync_setup(iut, *, skip=0, counter=0, subevents=(0,)):
    iut.ut(hci.HCI_LE_Set_Event_Mask_Command(((1 << 64) - 1).to_bytes(8, 'little')))
    sync = PeriodicSync(
        PEER,
        1,
        10_000_000,
        10_000_000,
        info=replace(INFO, event_counter=counter),
        handle=1,
        established=True,
        next_us=10_000,
        skip=skip,
        counter=counter,
        pawr=PawrSync(TIMING, RSP_AA, subevents=subevents),
    )
    iut.link.syncs[1] = sync
    return sync


def indication(iut, sync, *, subevent=0, data=b'indication'):
    at = 10_000 + subevent * 20_000
    channel = channel_selection_2(
        sync.counter ^ subevent, INFO.access_address, INFO.channel_map
    )
    iut.lt_tx(
        ExtendedPdu(data=data),
        at=at,
        channel=channel,
        access_address=INFO.access_address,
    )
    return at, channel


def test_pawr_receive_window_follows_the_current_sleep_clock_accuracy(iut):
    """Receive an early PAwR subevent after switching to 500 ppm
    (Vol 6, Part B, 4.2.4 and 4.4.5.1.2).
    """
    sync = sync_setup(iut)
    sync.next_us = 100_000
    iut.ut(hci.HCI_LE_Modify_Sleep_Clock_Accuracy_Command(1))
    iut.lt_tx(
        ExtendedPdu(data=b'early'),
        at=sync.next_us - 50,
        channel=channel_selection_2(
            sync.counter, INFO.access_address, INFO.channel_map
        ),
        access_address=INFO.access_address,
    )
    iut.run_until(102_000)

    iut.ut_rx(REPORT, subevent=0, data=b'early')
    assert iut.radio.listens[-1][0] == 100_000 - 71


@pytest.mark.parametrize('skip', [0, 1, 19])
@pytest.mark.parametrize('counter', [0, 0xFFFF])
def test_response_remains_due_after_skip_advances_anchor(iut, skip, counter):
    """Receive, let Skip advance the next anchor, then respond in the current slot.

    Purpose:
        Receive, let Skip advance the next anchor, then respond in the current slot.

    Steps:
        1. Receive an IND, advance by Skip, then submit current-slot response data.

    Expected:
        - Transmit at that received IND plus RSD, including counter wrap and Skip 19.
    """
    sync = sync_setup(iut, skip=skip, counter=counter)
    start, channel = indication(iut, sync)
    iut.run_until(start + 1_000)
    iut.ut_rx(REPORT, periodic_event_counter=counter, subevent=0, data=b'indication')
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Response_Data_Command(
            1, counter, 0, 0, 0, b'echo'
        )
    )
    iut.run_until(start + 12_000)
    packet = iut.lt_rx()
    assert packet.start_us == start + 10_000
    assert packet.channel == channel and packet.access_address == RSP_AA
    assert packet.ext.data == b'echo'
    assert sync.counter == (counter + skip + 1) & 0xFFFF
    iut.lt_rx_q_is_empty()


def test_response_reflects_rf_compensation_written_after_scheduling(iut):
    """Update the scheduled response's TxPower (Core Vol 4, Part E, 7.8.76).

    Steps:
        1. Enable IncludeTxPower, receive an indication and schedule its response.
        2. Write +5 dB of TX path compensation before the response slot.

    Expected:
        - The response's TxPower field and actual RF power are both +5 dBm.
    """
    sync = sync_setup(iut)
    properties = hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command.Properties
    iut.ut(
        hci.HCI_LE_Set_Periodic_Sync_Subevent_Command(
            1, properties.INCLUDE_TX_POWER, [0]
        )
    )
    start, _ = indication(iut, sync)
    iut.run_until(start + 1_000)
    iut.ut_rx(REPORT)
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Response_Data_Command(
            1, 0, 0, 0, 0, b'echo'
        )
    )
    assert sync.pawr is not None and sync.pawr.scheduled
    iut.ut(hci.HCI_LE_Write_RF_Path_Compensation_Command(50, 0))
    iut.run_until(start + 12_000)
    packet = iut.lt_rx()
    assert packet.start_us == start + TIMING.slot_offset_us(0)
    assert packet.ext.tx_power == packet.power_dbm == 5


def test_missing_indication_does_not_transmit_queued_response(iut):
    """Queue data before its IND, miss that IND and verify only the failure report.

    Purpose:
        Queue data before its IND, miss that IND and verify only the failure report.

    Steps:
        1. Queue a response and withhold the indication it depends on.

    Expected:
        - Publish the failed-reception report and transmit nothing.
    """
    sync_setup(iut)
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Response_Data_Command(
            1, 0, 0, 0, 0, b'echo'
        )
    )
    iut.run_until(25_000)
    iut.ut_rx(REPORT, subevent=0, data_status=0xFF, data=b'')
    iut.lt_rx_q_is_empty()


def test_termination_cancels_scheduled_response(iut):
    """Terminate after a valid indication and response command; no slot TX follows.

    Purpose:
        Terminate after a valid indication and response command; no slot TX follows.

    Steps:
        1. Receive an IND, queue its response, then terminate synchronization.

    Expected:
        - No transmission survives the terminated context.
    """
    sync = sync_setup(iut)
    start, _ = indication(iut, sync)
    iut.run_until(start + 1_000)
    iut.ut_rx(REPORT)
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Response_Data_Command(
            1, 0, 0, 0, 0, b'echo'
        )
    )
    iut.ut(hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(1))
    iut.run_until(start + 12_000)
    iut.lt_rx_q_is_empty()


@pytest.mark.parametrize('request_number', [1, 2])
@pytest.mark.parametrize('action', ['disable', 'remove', 'reset', 'restart'])
def test_data_request_callback_can_release_train(iut, request_number, action):
    """Release a PAwR train while delivering its data request.

    Purpose:
        Verify that the first and later Host callbacks can release or replace
        the train without a stale transmission or state update.

    Steps:
        1. Enable a three-subevent train and unmask its data requests.
        2. In the selected request callback, disable, remove, reset or restart it.
        3. Advance past the old first anchor and check the replacement's schedule.

    Expected:
        - Released trains transmit nothing and raise no exception.
        - A restarted train retains its own anchor and subevent state.
    """
    configure_extended(iut)
    iut.ut(hci.HCI_LE_Set_Event_Mask_Command(((1 << 64) - 1).to_bytes(8, 'little')))
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Parameters_V2_Command(
            0, 80, 80, 0, 3, 16, 8, 10, 2
        )
    )
    requests = []
    restarted = []

    def emit(event):
        iut.events.append(event)
        if not isinstance(
            event, hci.HCI_LE_Periodic_Advertising_Subevent_Data_Request_Event
        ):
            return
        requests.append(event)
        if len(requests) != request_number:
            return
        iut.controller.on_packet(
            bytes(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0, 0))
        )
        if action == 'remove':
            iut.controller.on_packet(
                bytes(hci.HCI_LE_Remove_Advertising_Set_Command(0))
            )
        elif action == 'reset':
            iut.controller.on_packet(bytes(hci.HCI_Reset_Command()))
        elif action == 'restart':
            iut.controller.on_packet(
                bytes(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
            )
            item = iut.link.advertisers[0]
            restarted.append((item.pawr_state, item.periodic_next_us))

    iut.controller.send_hci_packet = emit
    iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    iut.run_until(110_000)
    assert len(requests) >= request_number
    for event in iut.events:
        if isinstance(event, hci.HCI_Command_Complete_Event):
            assert event.return_parameters.status == hci.HCI_ErrorCode.SUCCESS
    if action != 'restart':
        iut.lt_rx_q_is_empty()
    else:
        state, anchor = restarted[0]
        item = iut.link.advertisers[0]
        assert item.pawr_state is state and item.periodic_next_us == anchor
        packets = iut.lt_rx_all()
        if request_number == 1:
            assert len(packets) == 1 and packets[0].start_us == anchor
            assert state.subevent == 1
        else:
            assert not packets and state.subevent == 0
            assert iut.now_us == 110_000


@pytest.mark.parametrize('action', ['continue', 'restart', 'reset-reuse'])
def test_response_slot_completes_only_on_its_original_train(iut, action):
    """Receive a PAwR response across a Host command (Vol 4, Part E, 7.7.65.37).

    Steps:
        1. Configure subevent zero with one response slot and transmit its indication.
        2. While receiving the response, keep the train, restart it, or reset the
           Controller and configure another train at the same handle.

    Expected:
        - The uninterrupted train reports the slot data. A released train
          produces no response report and leaves its replacement at subevent zero.
    """

    def configure_train():
        configure_extended(iut)
        iut.ut(
            hci.HCI_LE_Set_Periodic_Advertising_Parameters_V2_Command(
                0, 80, 80, 0, 3, 16, 8, 10, 2
            )
        )
        iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))

    configure_train()
    item = iut.link.advertisers[0]
    iut.run(1)
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Subevent_Data_Command(
            0, [0], [0], [1], [b'ind']
        )
    )
    iut.run_until(item.periodic_next_us)
    indication = iut.lt_rx()
    slot = indication.start_us + TIMING.slot_offset_us(0)
    iut.lt_tx(
        ExtendedPdu(data=b'echo'),
        channel=indication.channel,
        at=slot,
        access_address=item.pawr_state.rsp_aa,
    )
    with iut.pause_radio(iut.link.advance_to_us(slot + 1), 'receive'):
        if action == 'reset-reuse':
            iut.ut(hci.HCI_Reset_Command())
            iut.ut(hci.HCI_Set_Event_Mask_Command(b'\xff' * 8))
            iut.ut(hci.HCI_LE_Set_Event_Mask_Command(b'\xff' * 8))
            configure_train()
        elif action == 'restart':
            iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0, 0))
            iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    iut.lt_script_is_consumed()
    reports = [
        event
        for event in iut.events
        if isinstance(event, hci.HCI_LE_Periodic_Advertising_Response_Report_Event)
    ]
    if action == 'continue':
        (report,) = reports
        assert report.advertising_handle == report.subevent == report.tx_status == 0
        (response,) = report.responses
        assert response.response_slot == 0 and response.data == b'echo'
    else:
        assert not reports
        current = iut.link.advertisers[0]
        assert current.periodic_counter == current.pawr_state.subevent == 0


def test_restarted_train_ignores_old_subevent_completion(iut):
    """A completion from the old train cannot advance its replacement.

    Purpose:
        Verify state identity when the Host restarts periodic advertising
        while a subevent operation is in flight.

    Steps:
        1. Prepare a subevent, then disable and re-enable the same set.
        2. Deliver the old subevent's successful completion.

    Expected:
        - The replacement stays at subevent zero, before its first transmission.
    """
    configure_extended(iut)
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Parameters_V2_Command(
            0, 80, 80, 0, 3, 16, 8, 10, 2
        )
    )
    iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    manager = iut.link.roles.advertising
    item = iut.link.advertisers[0]
    current = manager.begin_subevent(item)
    assert current is not None
    iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0, 0))
    iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    manager.end_subevent(item, current, transmitted=True)
    assert item.pawr_state.subevent == 0
    assert not item.pawr_state.transmitted and item.periodic_last_counter is None


def test_failed_response_report_callback_can_restart_train(iut):
    """A failed subevent report may restart its periodic train.

    Purpose:
        Verify that a Host restart in a failure report callback cannot advance
        the replacement's event counter or anchor.

    Steps:
        1. Supply data and a response slot for the last subevent of an event.
        2. Complete that subevent untransmitted, restarting in its failure report.

    Expected:
        - The replacement retains its initial anchor, counter and subevent zero.
    """
    configure_extended(iut)
    iut.ut(hci.HCI_LE_Set_Event_Mask_Command(((1 << 64) - 1).to_bytes(8, 'little')))
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Parameters_V2_Command(
            0, 80, 80, 0, 3, 16, 8, 10, 2
        )
    )
    iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    manager = iut.link.roles.advertising
    manager.prepare()
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Subevent_Data_Command(
            0, [2], [0], [1], [b'data']
        )
    )
    item = iut.link.advertisers[0]
    item.pawr_state.subevent = 2
    current = manager.begin_subevent(item)
    assert current is not None
    restarted = []

    def emit(event):
        iut.events.append(event)
        if isinstance(event, hci.HCI_LE_Periodic_Advertising_Response_Report_Event):
            assert event.tx_status != 0
            iut.controller.on_packet(
                bytes(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0, 0))
            )
            iut.controller.on_packet(
                bytes(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
            )
            restarted.append(item.periodic_next_us)

    iut.controller.send_hci_packet = emit
    manager.end_subevent(item, current, transmitted=False)
    assert restarted == [item.periodic_next_us]
    assert item.periodic_counter == item.pawr_state.subevent == 0
    assert not item.pawr_state.transmitted and item.periodic_last_counter is None


def test_selected_subevents_share_event_counter(iut):
    """Listen to subevents 0 and 2 and preserve one counter across both reports.

    Purpose:
        Listen to subevents 0 and 2 and preserve one counter across both reports.

    Steps:
        1. Receive subevents 0 and 2 in one event on their CSA#2 channels.

    Expected:
        - Both reports use the same event counter, which then advances once.
    """
    sync = sync_setup(iut, subevents=(0, 2))
    indication(iut, sync, subevent=0)
    indication(iut, sync, subevent=2, data=b'second')
    iut.run_until(52_000)
    iut.ut_rx(REPORT, periodic_event_counter=0, subevent=0, data=b'indication')
    iut.ut_rx(REPORT, periodic_event_counter=0, subevent=2, data=b'second')
    assert sync.counter == 1
    iut.lt_script_is_consumed()


def test_subevent_train_uses_each_payload_once(iut):
    """Provide data for subevent 0, then observe empty PDUs when no new data arrives.

    Purpose:
        Provide data for subevent 0, then observe empty PDUs when no new data arrives.

    Steps:
        1. Supply one payload and observe subevents across two periodic events.

    Expected:
        - Only the requested instance contains data; later PDUs are empty and unchained.
    """
    configure_extended(iut)
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Parameters_V2_Command(
            0, 80, 80, 0, 3, 16, 8, 10, 2
        )
    )
    iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(3, 0))
    iut.link.roles.advertising.prepare()
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Subevent_Data_Command(
            0, [0], [0], [0], [b'once']
        )
    )
    item = iut.link.advertisers[0]
    iut.run_until(240_000)
    train = [p for p in iut.lt_rx_all() if p.access_address == item.periodic_aa]
    assert [p.start_us for p in train[:4]] == [100_000, 120_000, 140_000, 200_000]
    assert [p.ext.data for p in train[:4]] == [b'once', b'', b'', b'']
    assert all(p.ext.aux_ptr is None for p in train)
    assert all(p.ext.address is None for p in train)
    assert len({p.ext.adi for p in train}) <= 2


def test_long_interval_announces_only_encodable_syncinfo(iut):
    """A 25.6-second train waits for its SyncInfo offset to become encodable.

    Purpose:
        Verify that ordinary extended advertising keeps running while a PAwR
        anchor is farther away than the 13-bit SyncInfo offset can represent.

    Steps:
        1. Enable 128 subevents at 200 ms and a 25.6-second periodic interval.
        2. Observe advertising during the first second, then up to 24 seconds.

    Expected:
        - Early advertisements have no SyncInfo, without raising an exception.
        - Later advertisements point to the first periodic anchor using a
          nonzero, representable offset and the unchanged event counter.
    """
    configure_extended(iut)
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Parameters_V2_Command(
            0, 0x5000, 0x5000, 0, 128, 160, 8, 10, 1
        )
    )
    iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    enable_extended(iut)
    iut.run_until(1_000_000)
    early = [packet.ext for packet in iut.lt_rx_all() if packet.ext.address]
    assert early and all(pdu.sync_info is None for pdu in early)
    iut.run_until(24_000_000)
    later = [packet.ext.sync_info for packet in iut.lt_rx_all() if packet.ext.sync_info]
    assert later and all(info.event_counter == 0 for info in later)
    assert all(30 <= info.offset_us < 8192 * 300 for info in later)


def test_mid_event_syncinfo_wraps_counter(iut):
    """A mid-event announcement points to subevent zero across counter wrap.

    Purpose:
        Verify the successor of periodic counter 0xFFFF is encoded as zero
        when an announcement must refer to the following PAwR event.

    Steps:
        1. Enable a three-subevent train and place it midway through event FFFF.
        2. Transmit an extended advertising event before the next subevent.

    Expected:
        - The AUX_ADV_IND SyncInfo names counter zero without overflow.
        - Its offset points to the following event's subevent-zero anchor.
    """
    configure_extended(iut)
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Parameters_V2_Command(
            0, 80, 80, 0, 3, 16, 8, 10, 2
        )
    )
    iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    item = iut.link.advertisers[0]
    item.periodic_next_us = 0
    item.periodic_counter = 0xFFFF
    item.pawr_state.subevent = 1
    enable_extended(iut)
    iut.run_until(5_000)
    advertised = [p for p in iut.lt_rx_all() if p.ext.sync_info]
    assert advertised
    for packet in advertised:
        info = packet.ext.sync_info
        assert info.event_counter == 0
        assert packet.start_us + info.offset_us <= 100_000
        assert packet.start_us + info.offset_us > 100_000 - info.unit_us


def test_radio_conflict_reports_failed_subevent(iut, monkeypatch):
    """A selected subevent lost to radio contention reports failed reception.

    Purpose:
        Preserve the Host's v2 observation of every selected subevent even
        when a competing connection prevents the radio from listening.

    Steps:
        1. Install an established PAwR sync and deny radio admission.
        2. Execute its next subevent receive operation.

    Expected:
        - Report v2 names the selected subevent with Data_Status 0xFF.
        - No packet is transmitted and the periodic event counter advances.
    """
    sync = sync_setup(iut)
    monkeypatch.setattr(iut.link.resource, 'admit', lambda *args, **kwargs: False)
    iut.call(iut.link.roles.synchronization.run_subevent(sync))
    iut.ut_rx(REPORT, periodic_event_counter=0, subevent=0, data_status=255)
    assert sync.counter == 1
    iut.lt_rx_q_is_empty()


@pytest.mark.parametrize('wrong_target_type', [False, True])
def test_pawr_aux_connect_response_checks_target_address_type(iut, wrong_target_type):
    """Require the InitA address and type when the synchronized device answers."""
    configure_extended(iut)
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Parameters_V2_Command(
            0, 80, 80, 0, 3, 16, 8, 10, 2
        )
    )
    iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    item = iut.link.advertisers[0]
    initiating = Initiating(
        IUT_ADDRESS,
        PEER,
        30_000,
        30_000,
        CONNECT_PARAMETERS,
        iut.now_us,
        pawr_advertising=item,
    )
    iut.link.roles.scanning.start_initiating(initiating)
    channel = 12
    iut.lt_tx(
        ExtendedPdu(
            kind=PduType.AUX_CONNECT_RSP,
            address=bytes(PEER),
            random_address=PEER.is_random,
            target=bytes(IUT_ADDRESS),
            random_target=wrong_target_type,
        ),
        channel=channel,
        access_address=item.periodic_aa,
    )
    iut.call(pawr_connect(iut.link, item, initiating, channel, item.periodic_next_us))

    request = iut.lt_rx(PduType.AUX_CONNECT_REQ)
    assert request.access_address == item.periodic_aa
    assert iut.link.initiating is None and item.periodic_enabled
    status = hci.HCI_ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR
    iut.ut_rx(
        hci.HCI_LE_Enhanced_Connection_Complete_Event,
        status=status if wrong_target_type else 0,
    )
    assert len(iut.link.connections) == int(not wrong_target_type)


@pytest.mark.parametrize('spare_us', [-1, 0], ids=['overlap', 'fits'])
def test_pawr_connect_reply_requires_reserved_airtime(iut, spare_us):
    """Answer the request only when the complete response fits before the grant ends."""
    sync = sync_setup(iut)
    request = connect_request(PEER, IUT_ADDRESS, CONNECT_PARAMETERS)
    response = connect_response(IUT_ADDRESS, PEER)
    request_end = sync.next_us + Phy.LE_1M.airtime_us(len(request))
    response_start = request_end + LL_IFS_US
    iut.link.resource.deadline_us = (
        response_start + Phy.LE_1M.airtime_us(len(response)) + spare_us
    )
    channel = channel_selection_2(sync.counter, INFO.access_address, INFO.channel_map)
    iut.lt_tx(
        request, at=sync.next_us, channel=channel, access_address=INFO.access_address
    )
    iut.call(iut.link.roles.synchronization.run_subevent(sync))
    iut.lt_script_is_consumed()
    if spare_us < 0:
        assert not iut.link.connections
        iut.lt_rx_q_is_empty()
    else:
        sent = iut.lt_rx(PduType.AUX_CONNECT_RSP)
        assert (
            sent.start_us == response_start
            and sent.access_address == INFO.access_address
        )
        assert len(iut.link.connections) == 1


@pytest.mark.parametrize('source', ['sync', 'advertiser'])
@pytest.mark.parametrize('replacement', [False, True])
def test_released_train_cannot_commit_delayed_connection(iut, source, replacement):
    """A released PAwR owner cannot create a connection after its RF reply.

    Purpose:
        Apply the existing advertising/initiation lifetime invariant to PAwR
        connection exchanges that complete after a train was removed or replaced.

    Steps:
        1. Retain a connection-acceptance result naming a PAwR train.
        2. Remove that owner, optionally replacing it at the same handle.
        3. Commit the delayed acceptance result.

    Expected:
        - The result is rejected, without creating a connection or disturbing
          a replacement owner that reused the handle.
    """
    from bscontroller.linklayer.results import ConnectionAccepted
    from tests.unit.linklayer.air.harness import CONNECT_PARAMETERS, IUT_ADDRESS

    if source == 'sync':
        train = sync_setup(iut)
        contexts = iut.link.syncs
        handle = 1
        result = dict(pawr_sync=train)
        if replacement:
            contexts[handle] = replace(train)
        else:
            del contexts[handle]
    else:
        configure_extended(iut)
        iut.ut(
            hci.HCI_LE_Set_Periodic_Advertising_Parameters_V2_Command(
                0, 80, 80, 0, 3, 16, 8, 10, 2
            )
        )
        iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
        contexts = iut.link.advertisers
        handle = 0
        train = contexts[handle]
        result = dict(pawr_advertiser=train)
        advertising = iut.link.roles.advertising
        advertising.remove(handle)
        if replacement:
            advertising.publish(replace(train))
    result = ConnectionAccepted(
        CONNECT_PARAMETERS, PEER, hci.Role.PERIPHERAL, 0, IUT_ADDRESS, True, **result
    )
    assert iut.link.roles.accepted(result) is None
    assert not iut.link.connections
    assert (handle in contexts) == replacement
