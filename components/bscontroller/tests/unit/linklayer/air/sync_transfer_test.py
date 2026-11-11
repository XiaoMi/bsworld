# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Synchronizing from a Periodic Advertising Sync Transfer, on the air
(Core Vol 6, Part B, 5.1.13; Vol 4, Part E, 7.7.65.24).

Matrix: {first AUX_SYNC_IND, six missed events} x {reports enabled, disabled,
masked event} plus CTE_Type at the first AUX_SYNC_IND and reset. The
LL_PERIODIC_SYNC_IND is delivered on an unscheduled connection context, so the
scripted radio serves only the train from ``periodic_test.py``. PDU encoding
and recipient rules are in llcp/sync_transfer_test.py.
"""

from dataclasses import replace

import pytest

from bscontroller.linklayer import LL_PERIODIC_SYNC_ESTABLISHMENT_EVENTS
from bscontroller.linklayer.api import SyncTransferConfig
from bscontroller.linklayer.connection import Connection
from tests.unit.linklayer.air.harness import (
    CONNECT_PARAMETERS,
    PUBLIC_PEER,
    ErrorCode,
    hci,
)
from tests.unit.linklayer.air.periodic_test import (
    PERIODIC_INTERVAL,
    PERIODIC_REPORT,
    Train,
    drain_periodic,
)
from tests.unit.linklayer.llcp.sync_transfer_test import indication, sync_info

RECEIVED = hci.HCI_LE_Periodic_Advertising_Sync_Transfer_Received_Event
RECEIVED_V2 = hci.HCI_LE_Periodic_Advertising_Sync_Transfer_Received_V2_Event
SERVICE_DATA = 0x1234
OFFSET_US = 30_000
REPORTING = SyncTransferConfig(True, True, False, 0, 1_000_000)


def transfer(iut, train, mode=REPORTING):
    """Deliver LL_PERIODIC_SYNC_IND naming this connection event, offset 30 ms."""
    connection = Connection(
        iut.link, CONNECT_PARAMETERS, hci.Address('F0:F1:F2:F3:F4:02'), 1, iut.now_us
    )
    connection.sync_transfer = mode
    connection.control(
        indication(
            info=sync_info(OFFSET_US // 30, counter=train.counter),
            event=connection.event_counter,
            last=train.counter,
            sid=train.sid,
            address=train.address,
            identifier=SERVICE_DATA,
        )
    )
    train.anchor_us = connection.anchor_us + OFFSET_US
    return connection


def test_first_aux_sync_ind_completes_transfer(iut):
    """The first AUX_SYNC_IND establishes the sync and reports the transfer.

    Purpose: Verify that the recipient listens at the transferred anchor, and on
    the first AUX_SYNC_IND reports Sync Transfer Received [v1] with the
    connection handle, Service_Data, a new Sync_Handle and the train's SID,
    address, PHY, interval and SCA, then reports each event's data (Vol 6,
    Part B, 5.1.13; Vol 4, Part E, 7.7.65.24, 7.7.65.15).

    Steps:
        1. Deliver the PDU with receipt enabled and reports on.
        2. The Lower Tester train sends "first", then "second".

    Expected:
        - One v1 event with success and the transfer fields; no v2 event.
        - Periodic reports carry "first" and "second" on that Sync_Handle.
    """
    train = Train(iut, address=PUBLIC_PEER)
    connection = transfer(iut, train)
    train.send(b'first')
    train.run_through()
    event = iut.ut_rx(
        RECEIVED,
        status=ErrorCode.SUCCESS,
        connection_handle=connection.handle,
        service_data=SERVICE_DATA,
        sync_handle=1,
        advertising_sid=train.sid,
        advertiser_address_type=hci.AddressType.PUBLIC_DEVICE,
        advertiser_address=PUBLIC_PEER,
        advertiser_phy=1,
        periodic_advertising_interval=PERIODIC_INTERVAL,
        advertiser_clock_accuracy=5,
    )
    iut.ut_rx(PERIODIC_REPORT, sync_handle=event.sync_handle, data=b'first')
    train.send(b'second')
    train.run_through()
    iut.ut_rx(PERIODIC_REPORT, sync_handle=event.sync_handle, data=b'second')
    assert not any(isinstance(item, RECEIVED_V2) for item in iut.events)
    assert iut.link.syncs[1].established


def test_six_missed_events_fail_the_transfer(iut):
    """Six events without an AUX_SYNC_IND fail the transfer with 0x3E.

    Purpose: Verify that the recipient reports Sync Transfer Received with
    Connection Failed To Be Established and Sync_Handle zero after six events
    from the first one expected, and then ignores the train (Vol 6, Part B,
    4.4.5.1, 5.1.13; LL.TS 4.3.7.8).

    Steps:
        1. Deliver the PDU; the train is silent for six events.
        2. The train then sends an AUX_SYNC_IND.

    Expected:
        - One failure event with 0x3E, Sync_Handle 0 and the transfer fields.
        - No periodic report and no synchronization remains.
    """
    train = Train(iut, address=PUBLIC_PEER)
    connection = transfer(iut, train)
    for _ in range(LL_PERIODIC_SYNC_ESTABLISHMENT_EVENTS):
        train.send(skip=True)
    train.run_through()
    iut.ut_rx(
        RECEIVED,
        status=ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR,
        connection_handle=connection.handle,
        service_data=SERVICE_DATA,
        sync_handle=0,
    )
    train.send(b'late')
    train.run_through()
    iut.ut_rx_q_is_empty()
    assert not iut.link.syncs and not iut.link.roles.synchronization.transfers


# CTEInfo octets: CTETime 2 (16 us) with CTEType in bits 6-7 (Vol 6, Part B,
# 2.5.2).
AOA = 0x02
AOD_1US = 0x40 | 0x02


@pytest.mark.parametrize(
    'cte_type,cte_info,status',
    [
        (0x10, None, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR),
        (0x02, AOD_1US, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR),
        (0x01, AOA, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR),
        (0x05, AOD_1US, ErrorCode.SUCCESS),
        (0x07, None, ErrorCode.SUCCESS),
        (0x10, AOA, ErrorCode.SUCCESS),
    ],
    ids=[
        'without-cte',
        'aod-1us',
        'aoa',
        'other-types',
        'no-cte-allowed',
        'cte-required',
    ],
)
def test_cte_type_filters_the_first_aux_sync_ind(iut, cte_type, cte_info, status):
    """CTE_Type decides at the first AUX_SYNC_IND, and only there.

    Purpose: Verify that a transfer mode's CTE_Type fails synchronization when
    the first AUX_SYNC_IND has an excluded CTE type, or no CTE while bit 4 is
    set, reporting Unsupported Remote Feature as Create Sync does; that other
    packets establish the sync; and that a later CTE change keeps it (Vol 6,
    Part B, 4.4.3.4; Vol 4, Part E, 7.8.67, 7.8.91).

    Steps:
        1. Deliver the PDU with reports on and the variant CTE_Type.
        2. The train sends "first" with the variant CTEInfo, then "second"
           with the other CTE presence.

    Expected:
        - One transfer event with the variant status.
        - On success, reports for "first" and "second"; on failure, no report,
          sync or transfer context.
    """
    train = Train(iut, address=PUBLIC_PEER)
    connection = transfer(iut, train, replace(REPORTING, cte_type=cte_type))
    train.send(b'first', cte=cte_info)
    train.send(b'second', cte=AOA if cte_info is None else None)
    train.run_through()
    iut.ut_rx(
        RECEIVED,
        status=status,
        connection_handle=connection.handle,
        service_data=SERVICE_DATA,
        sync_handle=1 if status == ErrorCode.SUCCESS else 0,
    )
    reports = drain_periodic(iut)
    if status == ErrorCode.SUCCESS:
        assert reports == [b'first', b'second']
        assert iut.link.syncs[1].established
    else:
        assert reports == []
        assert not iut.link.syncs and not iut.link.roles.synchronization.transfers


def test_reports_disabled_mode(iut):
    """Mode 0x01 synchronizes with reports disabled until Receive Enable.

    Purpose: Verify that a transfer received with reports disabled establishes
    and notifies the Host without periodic reports, and that Set Periodic
    Advertising Receive Enable turns them on (Vol 4, Part E, 7.8.91, 7.8.88).

    Steps:
        1. Deliver the PDU in Mode 0x01; the train sends "first".
        2. Enable reports; the train sends "second".

    Expected:
        - The success event and no report for "first"; a report for "second".
    """
    train = Train(iut, address=PUBLIC_PEER)
    transfer(iut, train, SyncTransferConfig(True, False, False, 0, 1_000_000))
    train.send(b'first')
    train.run_through()
    event = iut.ut_rx(RECEIVED, status=ErrorCode.SUCCESS)
    iut.ut_rx_q_is_empty()
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command(event.sync_handle, 1)
    )
    train.send(b'second')
    train.run_through()
    assert drain_periodic(iut) == [b'second']


def test_masked_event_still_synchronizes(iut):
    """A masked transfer event is not sent; the v2 bit does not select it.

    Purpose: Verify that with only the v2 Sync Transfer Received bit enabled
    the Controller, which supports only v1, sends neither version, while the
    sync itself is established (Vol 4, Part E, 7.7.65.24, 7.8.1; HCI.TS
    4.17.5 alternative A).

    Steps:
        1. Mask the v1 event and enable v2; deliver the PDU; train sends "first".

    Expected:
        - No transfer event of either version; the sync is active.
    """
    iut.controller.le_event_mask &= ~(1 << (RECEIVED.subevent_code - 1))
    train = Train(iut, address=PUBLIC_PEER)
    transfer(iut, train)
    train.send(b'first')
    train.run_through()
    assert not any(isinstance(item, (RECEIVED, RECEIVED_V2)) for item in iut.events)
    assert 1 in iut.link.syncs


def test_reset_drops_transfer_in_progress(iut):
    """HCI Reset discards a transfer synchronization in progress.

    Purpose: Verify that Reset clears transfer synchronization state, so no
    event follows for information received before it (Vol 4, Part E, 7.3.2).

    Steps:
        1. Deliver the PDU; send Reset before the first train event.
        2. The train sends "first".

    Expected:
        - No transfer context, sync or Host event.
    """
    train = Train(iut, address=PUBLIC_PEER)
    transfer(iut, train)
    assert iut.link.roles.synchronization.transfers
    iut.ut(hci.HCI_Reset_Command())
    iut.controller.event_mask = iut.controller.le_event_mask = (1 << 64) - 1
    train.send(b'first')
    train.run_through()
    assert not iut.link.roles.synchronization.transfers and not iut.link.syncs
    iut.ut_rx_q_is_empty()
