# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Monitoring Advertisers through scanning PDUs and HCI (Core Vol 6, B, 4.4.3.8)."""

import pytest

from bscontroller import hci
from bscontroller.linklayer.advertising_pdu import AuxPtr, ExtendedPdu
from bscontroller.radio.base import Phy
from tests.unit.linklayer.air.harness import PEER, adv_ind, scan_rsp

REPORT = hci.HCI_LE_Monitored_Advertisers_Report_Event
SECOND = 1_000_000


def _start(iut, *, extended=False, policy=0, duplicates=0):
    iut.controller.le_event_mask = (1 << 64) - 1
    if extended:
        iut.ut(
            hci.HCI_LE_Set_Extended_Scan_Parameters_Command(
                0, policy, 1, [0], [16], [16]
            )
        )
        iut.ut(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, duplicates, 0, 0))
    else:
        iut.ut(hci.HCI_LE_Set_Scan_Parameters_Command(0, 16, 16, 0, policy))
        iut.ut(hci.HCI_LE_Set_Scan_Enable_Command(1, duplicates))
    iut.ut(
        hci.HCI_LE_Add_Device_To_Monitored_Advertisers_List_Command(
            PEER.address_type, PEER, -70, -40, 1
        )
    )
    iut.ut(hci.HCI_LE_Enable_Monitoring_Advertisers_Command(1))


def _advertise(iut, rssi, *, extended=False, address=PEER):
    pdu = (
        ExtendedPdu(address=bytes(address), random_address=address.is_random)
        if extended
        else adv_ind(address)
    )
    iut.lt_tx(pdu, rssi=rssi)
    iut.run(2_000)
    return iut.reports()


@pytest.mark.parametrize('extended', [False, True])
@pytest.mark.parametrize('policy', [0, 1])
def test_monitoring_thresholds_and_filter_accept_list(iut, extended, policy):
    """Low is strict, high is inclusive; an empty accept list permits monitoring.

    Purpose: Exercise RSSI hysteresis independently of the Filter Accept List.

    Steps:
        Receive at low, expire once, receive below and at high, then refresh with
        intermediate RSSI before allowing a second timeout.

    Expected:
        Low is strict and high inclusive; each loss/high transition reports once
        under either policy and either advertising format.
    """
    _start(iut, extended=extended, policy=policy)
    assert bool(_advertise(iut, -70, extended=extended)) == (policy == 0)
    iut.run_until(SECOND)
    iut.ut_rx(REPORT, address=PEER, condition=0)
    _advertise(iut, -41, extended=extended)
    iut.ut_rx_q_is_empty()
    _advertise(iut, -40, extended=extended)
    iut.ut_rx(REPORT, address=PEER, condition=1)
    for _ in range(3):
        iut.run(600_000)
        _advertise(iut, -60, extended=extended)
        iut.ut_rx_q_is_empty()
    iut.run(SECOND)
    iut.ut_rx(REPORT, condition=0)
    iut.run(2 * SECOND)
    iut.ut_rx_q_is_empty()


@pytest.mark.parametrize('extended', [False, True])
def test_monitoring_an_extended_only_advertiser(iut, extended):
    """Only an extended scanner follows the monitored peer's ADV_EXT_IND.

    Steps:
        Receive an extended advertisement after 600 ms, cross the original
        loss deadline, then let supervision expire and receive another one.

    Expected:
        Extended scanning refreshes the timer and reports recovery. Legacy
        scanning reports loss at the original deadline (Vol 4, Part E, 7.8.11).
    """
    _start(iut, extended=extended)
    iut.run_until(600_000)
    _advertise(iut, -40, extended=True)
    iut.run_until(SECOND)
    if extended:
        iut.ut_rx_q_is_empty()
        iut.run(SECOND)
    iut.ut_rx(REPORT, address=PEER, condition=0)
    _advertise(iut, -40, extended=True)
    if extended:
        iut.ut_rx(REPORT, address=PEER, condition=1)
    iut.ut_rx_q_is_empty()


def test_duplicate_filter_does_not_suppress_monitoring(iut):
    """Repeated identical advertisements still refresh the monitor timer.

    Purpose: Keep supervision active even when duplicate Host reports are suppressed.

    Steps:
        Enable duplicate filtering and receive the same PDU every 600 ms.

    Expected:
        Only the first advertisement is reported and no loss report is emitted.
    """
    _start(iut, duplicates=1)
    assert _advertise(iut, -50)
    for _ in range(4):
        iut.run(600_000)
        assert not _advertise(iut, -50)
        iut.ut_rx_q_is_empty()


@pytest.mark.parametrize('operation', ['disable', 'clear', 'remove', 'reset'])
def test_monitoring_cancellation(iut, operation):
    """Disable, clear, remove and Reset cancel pending supervision and high reports.

    Purpose: Cancel pending supervision through every list/lifecycle operation.

    Steps:
        Enable monitoring, apply the selected operation, advance two seconds and
        receive a high-RSSI advertisement.

    Expected:
        No pending loss or high report survives disable, clear, remove or Reset.
    """
    _start(iut)
    command = {
        'disable': hci.HCI_LE_Enable_Monitoring_Advertisers_Command(0),
        'clear': hci.HCI_LE_Clear_Monitored_Advertisers_List_Command(),
        'remove': hci.HCI_LE_Remove_Device_From_Monitored_Advertisers_List_Command(
            PEER.address_type, PEER
        ),
        'reset': hci.HCI_Reset_Command(),
    }[operation]
    iut.ut(command)
    iut.run(2 * SECOND)
    _advertise(iut, -20)
    iut.ut_rx_q_is_empty()


def test_restart_and_duplicate_add_preserve_configuration(iut):
    """Enable restarts supervision; duplicate adds preserve the timeout.

    Purpose: Distinguish restarting monitoring from adding a duplicate device.

    Steps:
        Restart at 900 ms, add the existing device with different thresholds and
        timeout, then cross the original one-second deadline.

    Expected:
        The restart resets supervision, duplicate parameters are ignored and
        the original high threshold remains effective.
    """
    _start(iut)
    iut.run(900_000)
    iut.ut(hci.HCI_LE_Enable_Monitoring_Advertisers_Command(1))
    iut.ut(
        hci.HCI_LE_Add_Device_To_Monitored_Advertisers_List_Command(
            PEER.address_type, PEER, -127, 20, 255
        )
    )
    iut.run(999_999)
    iut.ut_rx_q_is_empty()
    iut.run(1)
    iut.ut_rx(REPORT, condition=0)
    _advertise(iut, -40)
    iut.ut_rx(REPORT, condition=1)


def test_add_while_enabled_and_event_mask(iut):
    """Adding after clear starts supervision; masking only suppresses delivery.

    Purpose: Start newly added supervision and retain state when events are masked.

    Steps:
        Clear while enabled, add a device, mask its loss event, expire, unmask
        and receive at the high threshold.

    Expected:
        The hidden loss changes state; exactly one high event is then delivered.
    """
    _start(iut)
    iut.ut(hci.HCI_LE_Clear_Monitored_Advertisers_List_Command())
    iut.run(SECOND)
    iut.ut(
        hci.HCI_LE_Add_Device_To_Monitored_Advertisers_List_Command(
            PEER.address_type, PEER, -70, -40, 1
        )
    )
    iut.controller.le_event_mask &= ~(1 << (REPORT.subevent_code - 1))
    iut.run(SECOND)
    iut.ut_rx_q_is_empty()
    iut.controller.le_event_mask = (1 << 64) - 1
    _advertise(iut, -40)
    iut.ut_rx(REPORT, condition=1)


@pytest.mark.parametrize('extended', [False, True])
@pytest.mark.parametrize('identity_type', [0, 1])
def test_monitoring_resolved_identity(iut, extended, identity_type):
    """Resolved RPAs match the monitored identity and report its identity address type.

    Purpose: Exercise identity resolution before monitoring, including loss
    reports that retain the last resolved address (Vol 6, Part B, 4.4.3.8).

    Steps:
        1. Add a public/random identity and the Core ah sample's Peer IRK.
        2. Receive its known RPA above low, then expire supervision.
        3. Receive that RPA at high RSSI.

    Expected:
        Both reports contain the identity address and type 2/3, never the RPA.
    """
    identity = hci.Address(bytes(PEER), hci.AddressType(identity_type))
    key = bytes.fromhex('ec0234a357c8ad05341010a60a397d9b')[::-1]
    rpa = hci.Address('70:81:94:0D:FB:AA')
    iut.ut(
        hci.HCI_LE_Add_Device_To_Resolving_List_Command(
            identity_type, identity, key, bytes(16)
        )
    )
    iut.ut(hci.HCI_LE_Set_Address_Resolution_Enable_Command(1))
    _start(iut, extended=extended)
    iut.ut(hci.HCI_LE_Clear_Monitored_Advertisers_List_Command())
    iut.ut(
        hci.HCI_LE_Add_Device_To_Monitored_Advertisers_List_Command(
            identity_type, identity, -70, -40, 1
        )
    )
    _advertise(iut, -50, extended=extended, address=rpa)
    iut.run(SECOND)
    reported = hci.Address(bytes(identity), hci.AddressType(identity_type + 2))
    iut.ut_rx(REPORT, address=reported, address_type=identity_type + 2, condition=0)
    _advertise(iut, -40, extended=extended, address=rpa)
    iut.ut_rx(REPORT, address=reported, address_type=identity_type + 2, condition=1)


def test_monitoring_primary_survives_missing_auxiliary(iut):
    """An addressed primary refreshes monitoring even if the auxiliary is missed.

    Purpose: Monitor received advertisements independently from completion of
    the extended advertising report (Vol 6, Part B, 4.4.3.8).

    Steps:
        Enable extended scanning and supervision. Every 600 ms transmit a
        primary with AdvA and AuxPtr, but omit the auxiliary transmission.

    Expected:
        No loss report appears while addressed primary packets keep arriving.
    """
    _start(iut, extended=True)
    for _ in range(4):
        iut.run(600_000)
        iut.lt_tx(
            ExtendedPdu(
                address=bytes(PEER),
                random_address=PEER.is_random,
                adi=0x1000,
                aux_ptr=AuxPtr(12, 3000, Phy.LE_1M),
            ),
            rssi=-50,
        )
        iut.run(5_000)
        iut.reports()
        iut.ut_rx_q_is_empty()


def test_monitoring_independent_devices_and_exact_refresh_deadline(iut):
    """Timers expire independently and refresh only strictly above low.

    Purpose: Verify per-device deadlines at one-microsecond boundaries.

    Steps:
        1. Enable a one-second monitor and add a second device with two seconds.
        2. Receive the first device above low; stop one tick before its deadline.
        3. Cross its deadline, then the second device's deadline.

    Expected:
        Each device emits exactly one loss report at its own deadline.
    """
    _start(iut)
    other = hci.Address('C0:11:22:33:44:56')
    iut.ut(
        hci.HCI_LE_Add_Device_To_Monitored_Advertisers_List_Command(
            other.address_type, other, -70, -40, 2
        )
    )
    iut.run(400_000)
    _advertise(iut, -69)
    received_at = iut.radio.delivered[-1].end_us - 1
    iut.run_until(received_at + SECOND - 1)
    iut.ut_rx_q_is_empty()
    iut.run(1)
    iut.ut_rx(REPORT, address=PEER, condition=0)
    iut.run_until(2 * SECOND)
    iut.ut_rx(REPORT, address=other, condition=0)


@pytest.mark.parametrize(
    'packet,valid_crc',
    [
        (adv_ind(PEER), False),
        (bytes([1, 8]) + bytes(PEER) + bytes(2), True),
        (scan_rsp(PEER), True),
        (adv_ind(hci.Address('C0:11:22:33:44:56')), True),
    ],
)
def test_monitoring_ignores_invalid_or_unrelated_pdus(iut, packet, valid_crc):
    """Invalid packets, scan responses and unrelated devices cannot refresh supervision.

    Purpose: Accept only valid advertisements from a monitored device.

    Steps:
        Start supervision, then receive the selected packet at high RSSI
        halfway through the timeout and advance to the original deadline.

    Expected:
        One loss report is delivered at the original one-second deadline.
    """
    _start(iut)
    iut.run(500_000)
    iut.lt_tx(packet, valid_crc=valid_crc, rssi=-20)
    iut.run_until(SECOND)
    iut.reports()
    iut.ut_rx(REPORT, address=PEER, condition=0)
