# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Extended advertising state (Core Vol 6, Part B, 4.4.2.2.2, 4.4.2.5.2,
4.4.2.7; Vol 4, Part E, 7.8.53-7.8.61).

Matrix: {non-connectable/non-scannable, scannable, connectable, anonymous,
directed, legacy PDUs} x {AUX chain layout, AUX_SCAN_REQ, AUX_CONNECT_REQ,
filtered, wrong target} x {duration, max events, removal, sets}.

The IUT advertises with extended sets; the Lower Tester plays the scanner or
initiator. Scenarios run on the ``air/harness.py`` harness and read ADV_EXT_IND
on primary channels plus AUX_ADV_IND / AUX_CHAIN_IND / AUX_SCAN_RSP /
AUX_CONNECT_RSP on the secondary channel. Fragmentation and airtime are covered
in ``extended_codec_test.py``, scanning and initiation in
``extended_scanning_test.py``, and periodic advertising in ``periodic_test.py``.
"""

from itertools import pairwise

import pytest

from bscontroller.hci import HCI_LE_ADV_INTERVAL_UNIT_US
from bscontroller.linklayer import (
    LL_IFS_US,
    LL_MAFS_US,
    AdvertisingMode,
)
from bscontroller.linklayer.advertising import ExtendedPdu
from bscontroller.radio.base import RADIO_SWITCH_TIME_US, Phy
from tests.unit.linklayer.air.advertising import (
    EXTENDED_INTERVAL,
    Operation,
    configure_extended,
    enable_extended,
    extended_parameters,
    split_primary_auxiliary,
)
from tests.unit.linklayer.air.harness import (
    CONNECT_PARAMETERS,
    IUT_ADDRESS,
    OTHER,
    PEER,
    ErrorCode,
    PduType,
    connect_ind,
    hci,
    scan_req,
)

Properties = (
    hci.HCI_LE_Set_Extended_Advertising_Parameters_Command.AdvertisingProperties
)
INTERVAL_US = EXTENDED_INTERVAL * HCI_LE_ADV_INTERVAL_UNIT_US
TERMINATED = hci.HCI_LE_Advertising_Set_Terminated_Event
CONNECTED = hci.HCI_LE_Enhanced_Connection_Complete_Event
CONNECTABLE = Properties.CONNECTABLE_ADVERTISING
SCANNABLE = Properties.SCANNABLE_ADVERTISING
DIRECTED = Properties.DIRECTED_ADVERTISING


def one_event(iut):
    iut.run(1)
    return iut.lt_rx_all()


# ---------------------------------------------------------------------------
# Event layout (4.4.2.2.2)
# ---------------------------------------------------------------------------
def test_tx_path_compensation_changes_the_radiated_power_and_header(iut):
    """A TX path gain applies to the radio and the advertising TxPower field.

    Purpose: Verify that writing RF path compensation updates the radiated
    power of an already configured set (Core Vol 4, Part E, 7.8.76).

    Steps:
        1. Configure an extended set with TxPower and a -8 dBm limit.
        2. Add 5 dB of TX path gain, then enable the set.
        3. Observe one advertising event.

    Expected:
        - The primary and auxiliary packets are transmitted at -3 dBm.
        - The AUX_ADV_IND TxPower field reports -3 dBm.
    """
    configure_extended(iut, Properties.INCLUDE_TX_POWER, advertising_tx_power=-8)
    iut.ut(hci.HCI_LE_Write_RF_Path_Compensation_Command(50, 0))
    enable_extended(iut)
    primary, auxiliary = split_primary_auxiliary(one_event(iut))
    assert primary
    (advertisement,) = auxiliary
    assert all(packet.power_dbm == -3 for packet in (*primary, advertisement))
    assert ExtendedPdu.decode(advertisement.pdu).tx_power == -3


@pytest.mark.parametrize('primary_phy', [Phy.LE_1M, Phy.LE_CODED])
@pytest.mark.parametrize('secondary_phy', list(Phy))
@pytest.mark.parametrize('channel_map', [1, 5, 7])
def test_primary_points_every_channel_to_one_auxiliary(
    iut, primary_phy, secondary_phy, channel_map
):
    """Every primary ADV_EXT_IND points to the single AUX_ADV_IND.

    Purpose: Verify that an extended event sends one ADV_EXT_IND per mapped
    primary channel, each carrying only ADI and an AuxPtr (no address, no data)
    on the primary PHY, all pointing to one AUX_ADV_IND on the secondary channel
    and PHY at least T_MAFS later, and that the AUX_ADV_IND carries the address,
    data and matching ADI (Vol 6, Part B, 2.3.4, 4.4.2.2.2).

    Steps:
        1. Upper Tester configures an extended set with AdvData, the parametrized
           primary PHY, secondary PHY and channel map.
        2. Upper Tester enables advertising.
        3. Run one event and split primary from auxiliary transmissions.

    Expected:
        - One ADV_EXT_IND per mapped channel, spaced by the radio switch time.
        - Each ADV_EXT_IND is on the primary PHY, carries ADI (SID 5) with no
          address or data, and an AuxPtr on the secondary PHY and the auxiliary
          channel whose offset window contains the AUX_ADV_IND, at least T_MAFS
          after the primary PDU.
        - The one AUX_ADV_IND is on the secondary PHY, carries the IUT address,
          the AdvData and the shared ADI.

    Parameters range over primary PHY {1M, Coded}, every secondary PHY, and
    channel maps {37}, {37, 39}, {37, 38, 39}.
    """
    configure_extended(
        iut,
        data=b'\x02\x01\x06',
        primary_advertising_phy=primary_phy,
        secondary_advertising_phy=secondary_phy,
        primary_advertising_channel_map=channel_map,
    )
    enable_extended(iut)
    primary, (auxiliary,) = split_primary_auxiliary(one_event(iut))
    assert len(primary) == channel_map.bit_count()
    for previous, current in pairwise(primary):
        assert current.start_us - previous.end_us == RADIO_SWITCH_TIME_US
    for packet in primary:
        ext = packet.ext
        assert packet.kind == PduType.ADV_EXT_IND and packet.phy == primary_phy
        # ADV_EXT_IND carries only ADI and AuxPtr: no address, no data.
        assert ext.address is None and ext.data == b'' and ext.adi >> 12 == 5
        assert ext.aux_ptr.phy == secondary_phy
        assert ext.aux_ptr.channel == auxiliary.channel
        start = packet.start_us + ext.aux_ptr.offset_us
        assert start <= auxiliary.start_us < start + ext.aux_ptr.unit_us
        assert auxiliary.start_us - packet.end_us >= LL_MAFS_US
    aux = auxiliary.ext
    assert auxiliary.phy == secondary_phy
    assert aux.address == bytes(IUT_ADDRESS) and aux.data == b'\x02\x01\x06'
    assert aux.adi == primary[0].ext.adi


@pytest.mark.parametrize('size', [0, 245, 246, 1000, 1650])
def test_long_data_uses_aux_chain(iut, size):
    """Long AdvData is carried as an AUX_CHAIN_IND sequence.

    Purpose: Verify that AdvData too large for one PDU is split across an
    AUX_ADV_IND and AUX_CHAIN_IND chain whose payloads reassemble to the input,
    with each continuation spaced by its AuxPtr offset (at least T_MAFS after the
    previous PDU), keeping the ADI but dropping the address, and the last PDU
    carrying no AuxPtr (Vol 6, Part B, 2.3.4, 4.4.2.2.2).

    Steps:
        1. Upper Tester sets extended parameters.
        2. Upper Tester loads the AdvData in fragments of up to 251 octets.
        3. Upper Tester enables advertising.
        4. Run one event and take the auxiliary chain.

    Expected:
        - The joined chain payloads equal the original data.
        - Each continuation starts at the previous AuxPtr offset, at least
          T_MAFS after the previous PDU, keeps the shared ADI and omits the
          address.
        - The final chain PDU has no AuxPtr.

    Parameters range over data sizes {0, 245, 246, 1000, 1650}.
    """
    data = bytes(index % 251 for index in range(size))
    iut.ut(extended_parameters())
    for offset in range(0, max(1, size), 251):
        chunk = data[offset : offset + 251]
        operation = (
            Operation.COMPLETE_DATA
            if size <= 251
            else Operation.FIRST_FRAGMENT
            if offset == 0
            else Operation.LAST_FRAGMENT
            if offset + 251 >= size
            else Operation.INTERMEDIATE_FRAGMENT
        )
        iut.ut(hci.HCI_LE_Set_Extended_Advertising_Data_Command(0, operation, 0, chunk))
    enable_extended(iut)
    _, chain = split_primary_auxiliary(one_event(iut))
    assert b''.join(packet.ext.data for packet in chain) == data
    for previous, current in pairwise(chain):
        pointer = previous.ext.aux_ptr
        assert current.start_us == previous.start_us + pointer.offset_us
        assert current.start_us - previous.end_us >= LL_MAFS_US
        # Continuations keep the ADI but not the address (2.3.4).
        assert current.ext.address is None and current.ext.adi == chain[0].ext.adi
    assert chain[-1].ext.aux_ptr is None


def test_tx_power_and_anonymous_fields(iut):
    """Anonymous advertising omits the address and reports TxPower.

    Purpose: Verify that an anonymous set with Include TxPower sends an
    AUX_ADV_IND with no AdvA, carries the configured TxPower field, and transmits
    every PDU at that power level (Vol 6, Part B, 2.3.4.1, 4.4.2.2.2).

    Steps:
        1. Upper Tester configures an anonymous set with Include TxPower, AdvData
           and TxPower -8.
        2. Upper Tester enables advertising.
        3. Run one event and split primary from auxiliary transmissions.

    Expected:
        - The AUX_ADV_IND carries no address.
        - Its TxPower field is -8.
        - Every primary and auxiliary PDU is transmitted at -8 dBm.
    """
    configure_extended(
        iut,
        Properties.ANONYMOUS_ADVERTISING | Properties.INCLUDE_TX_POWER,
        data=b'x',
        advertising_tx_power=-8,
    )
    enable_extended(iut)
    primary, (auxiliary,) = split_primary_auxiliary(one_event(iut))
    assert auxiliary.ext.address is None
    assert auxiliary.ext.tx_power == -8
    assert all(packet.power_dbm == -8 for packet in primary + [auxiliary])


def test_did_changes_when_data_changes(iut):
    """Changing AdvData renews the DID but keeps the SID.

    Purpose: Verify that loading new AdvData changes the Data ID in the ADI while
    leaving the Set ID unchanged, so scanners can detect the update (Vol 6,
    Part B, 2.3.4.1).

    Steps:
        1. Upper Tester configures a set with AdvData "old" and enables it.
        2. Run one event and take the AUX_ADV_IND.
        3. Upper Tester loads AdvData "new".
        4. Run past one interval plus the maximum advDelay and take the new
           AUX_ADV_IND.

    Expected:
        - The second PDU carries "new".
        - Its DID differs from the first, but its SID is the same.
    """
    configure_extended(iut, data=b'old')
    enable_extended(iut)
    _, (first,) = split_primary_auxiliary(one_event(iut))
    iut.ut(
        hci.HCI_LE_Set_Extended_Advertising_Data_Command(
            0, Operation.COMPLETE_DATA, 0, b'new'
        )
    )
    iut.run(INTERVAL_US + 10_001)
    _, (second,) = split_primary_auxiliary(iut.lt_rx_all())
    assert second.ext.data == b'new'
    assert second.ext.adi & 0xFFF != first.ext.adi & 0xFFF
    assert second.ext.adi >> 12 == first.ext.adi >> 12


def test_unchanged_data_operation_renews_did_only(iut):
    """The Unchanged Data operation renews the DID without touching the data.

    Purpose: Verify that Set Extended Advertising Data with the Unchanged Data
    operation keeps the existing AdvData but renews the Data ID in the ADI
    (Vol 4, Part E, 7.8.54).

    Steps:
        1. Upper Tester configures a set with AdvData "same" and enables it.
        2. Run one event and take the AUX_ADV_IND.
        3. Upper Tester sends the Unchanged Data operation.
        4. Run past one interval plus the maximum advDelay and take the next
           AUX_ADV_IND.

    Expected:
        - The data is still "same".
        - The ADI differs from the first.
    """
    configure_extended(iut, data=b'same')
    enable_extended(iut)
    _, (first,) = split_primary_auxiliary(one_event(iut))
    iut.ut(
        hci.HCI_LE_Set_Extended_Advertising_Data_Command(
            0, Operation.UNCHANGED_DATA, 0, b''
        )
    )
    iut.run(INTERVAL_US + 10_001)
    _, (second,) = split_primary_auxiliary(iut.lt_rx_all())
    assert second.ext.data == b'same' and second.ext.adi != first.ext.adi


@pytest.mark.parametrize(
    'properties,pdu_type',
    [
        (Properties.USE_LEGACY_ADVERTISING_PDUS, PduType.ADV_NONCONN_IND),
        (Properties.USE_LEGACY_ADVERTISING_PDUS | SCANNABLE, PduType.ADV_SCAN_IND),
        (
            Properties.USE_LEGACY_ADVERTISING_PDUS | SCANNABLE | CONNECTABLE,
            PduType.ADV_IND,
        ),
    ],
)
def test_legacy_pdus_through_extended_commands(iut, properties, pdu_type):
    """Legacy-PDU extended sets emit legacy advertising on all channels.

    Purpose: Verify that an extended set with the Use Legacy PDUs property emits
    the corresponding legacy PDU type on all three primary channels, carrying the
    AdvData, rather than an ADV_EXT_IND / auxiliary layout (Vol 6, Part B,
    4.4.2.2.2; Vol 4, Part E, 7.8.53).

    Steps:
        1. Upper Tester configures the legacy-PDU set with AdvData "legacy".
        2. Upper Tester enables advertising.
        3. Run one event and read all transmissions.

    Expected:
        - All three PDUs are the mapped legacy type.
        - The first PDU carries "legacy".

    Parameters range over non-connectable (ADV_NONCONN_IND), scannable
    (ADV_SCAN_IND), and connectable+scannable (ADV_IND).
    """
    configure_extended(iut, properties, data=b'legacy')
    enable_extended(iut)
    packets = one_event(iut)
    assert [packet.kind for packet in packets] == [pdu_type] * 3
    assert packets[0].pdu[8:] == b'legacy'


@pytest.mark.parametrize('notify', [0, 1])
@pytest.mark.parametrize(
    'properties,pdu_type',
    [
        (Properties.USE_LEGACY_ADVERTISING_PDUS | SCANNABLE, PduType.ADV_SCAN_IND),
        (
            Properties.USE_LEGACY_ADVERTISING_PDUS | SCANNABLE | CONNECTABLE,
            PduType.ADV_IND,
        ),
    ],
)
def test_legacy_pdu_scan_request_notification(iut, properties, pdu_type, notify):
    """A legacy-PDU extended set reports SCAN_REQ when notifications are enabled.

    Purpose: Verify that an extended set using legacy PDUs answers SCAN_REQ with
    SCAN_RSP and raises a Scan Request Received event only when notifications
    are enabled, as for AUX_SCAN_REQ (Vol 4, Part E, 7.7.65.19; Vol 6, Part B,
    4.4.2.3).

    Steps:
        1. Upper Tester configures the scannable legacy-PDU set with scan-response
           data and the parametrized notification setting.
        2. Upper Tester enables advertising.
        3. Lower Tester scripts a SCAN_REQ on channel 37.
        4. Run one event and read all transmissions.

    Expected:
        - Channel 37 carries the legacy PDU, then SCAN_RSP one T_IFS after the
          request with the IUT address and "response".
        - A Scan Request Received event is raised iff notifications are enabled.
        - Nothing else reaches the Host.

    Parameters range over ADV_SCAN_IND and ADV_IND, each with notifications
    disabled and enabled.
    """
    configure_extended(
        iut,
        properties,
        scan_response=b'response',
        scan_request_notification_enable=notify,
    )
    enable_extended(iut)
    iut.lt_tx(scan_req(), channel=37)
    packets = one_event(iut)
    assert [packet.kind for packet in packets] == [
        pdu_type,
        PduType.SCAN_RSP,
        pdu_type,
        pdu_type,
    ]
    response = packets[1]
    request = iut.radio.delivered[0]
    assert response.channel == 37
    assert response.start_us == request.end_us + LL_IFS_US
    assert response.pdu[2:8] == bytes(IUT_ADDRESS)
    assert response.pdu[8:] == b'response'
    if notify:
        iut.ut_rx(
            hci.HCI_LE_Scan_Request_Received_Event,
            advertising_handle=0,
            scanner_address=PEER,
        )
    iut.ut_rx_q_is_empty()


@pytest.mark.parametrize('legacy', [True, False], ids=['legacy', 'extended'])
@pytest.mark.parametrize('action', ['disable', 'restart', 'reuse-handle', 'reset'])
def test_scan_request_for_a_released_advertising_set(iut, legacy, action):
    """An outstanding scan request belongs to the advertising event that opened RX.

    Purpose: Verify the lifetime of SCAN_REQ and AUX_SCAN_REQ notifications across
    advertising disable and Controller reset (Vol 4, Part E, 7.3.2, 7.8.56, 7.7.65.19).

    Steps:
        1. Enable a scannable set with notifications and pause its request RX.
        2. Disable or restart the set, replace it at the same handle,
           or reset the Controller and recreate the set with notifications enabled.
        3. Give the new enable a one-event limit and deliver both the old request
           and a fresh one, enabling the set first if it was left disabled.

    Expected:
        - The old event produces no further transmission or Host notification.
        - The fresh request receives the new scan-response data and one Host
          notification for handle 0. Termination counts one completed event.
    """
    properties = (
        SCANNABLE | Properties.USE_LEGACY_ADVERTISING_PDUS if legacy else SCANNABLE
    )
    configure_extended(
        iut, properties, scan_response=b'old', scan_request_notification_enable=1
    )
    enable_extended(iut)
    iut.lt_tx(scan_req())
    event = iut.link.advance_to_us(iut.now_us + 1)
    with iut.pause_radio(event, 'receive'):
        assert iut.lt_rx_all()
        if action == 'reset':
            iut.ut(hci.HCI_Reset_Command())
            iut.ut(hci.HCI_Set_Event_Mask_Command(b'\xff' * 8))
            iut.ut(hci.HCI_LE_Set_Event_Mask_Command(b'\xff' * 8))
        else:
            enable_extended(iut, on=0)
            if action == 'reuse-handle':
                iut.ut(hci.HCI_LE_Remove_Advertising_Set_Command(0))
        if action in ('reuse-handle', 'reset'):
            iut.ut(
                extended_parameters(
                    properties=properties, scan_request_notification_enable=1
                )
            )
        iut.ut(
            hci.HCI_LE_Set_Extended_Scan_Response_Data_Command(
                0, Operation.COMPLETE_DATA, 0, b'new'
            )
        )
        if action != 'disable':
            enable_extended(iut, max_events=1)
    iut.lt_script_is_consumed()
    iut.lt_rx_q_is_empty()
    iut.ut_rx_q_is_empty()

    if action == 'disable':
        enable_extended(iut, max_events=1)
    iut.lt_tx(scan_req())
    packets = one_event(iut)
    if legacy:
        (response,) = [packet for packet in packets if packet.kind == PduType.SCAN_RSP]
        assert response.pdu[8:] == b'new'
    else:
        _, auxiliary = split_primary_auxiliary(packets)
        advertisement, response = auxiliary
        assert advertisement.ext.mode == AdvertisingMode.SCANNABLE
        assert response.ext.data == b'new'
    iut.ut_rx(
        hci.HCI_LE_Scan_Request_Received_Event,
        advertising_handle=0,
        scanner_address=PEER,
    )
    iut.run(1)
    iut.ut_rx(
        TERMINATED,
        status=ErrorCode.LIMIT_REACHED_ERROR,
        advertising_handle=0,
        num_completed_extended_advertising_events=1,
    )
    iut.ut_rx_q_is_empty()


@pytest.mark.parametrize('legacy', [True, False], ids=['legacy', 'extended'])
def test_reenable_preserves_scan_exchange_and_starts_new_event_count(iut, legacy):
    """An in-flight scan exchange keeps its address across a repeated enable.

    Steps:
        1. Open a scannable event using a random address.
        2. While waiting for the scan request, change the address and re-enable
           with a one-event limit (Vol 4, Part E, 7.8.52 and 7.8.56).
        3. Send one request to each address in its respective event.

    Expected:
        Both requests receive a response from the address they saw. The new
        enable keeps its anchor and counts only the event using the new address.
    """
    old, new = hci.Address('C2:00:00:00:00:01'), OTHER
    properties = SCANNABLE | (Properties.USE_LEGACY_ADVERTISING_PDUS if legacy else 0)
    configure_extended(
        iut,
        properties,
        scan_response=b'rsp',
        own_address_type=hci.OwnAddressType.RANDOM,
        scan_request_notification_enable=1,
    )
    iut.ut(hci.HCI_LE_Set_Advertising_Set_Random_Address_Command(0, old))
    enable_extended(iut)
    iut.lt_tx(scan_req(advertiser=old))
    with iut.pause_radio(iut.link.advance_to_us(iut.now_us + 1), 'receive'):
        iut.ut(hci.HCI_LE_Set_Advertising_Set_Random_Address_Command(0, new))
        enable_extended(iut, max_events=1)
        anchor = iut.link.advertisers[0].next_us
        random_state = iut.link.random.getstate()
    assert iut.link.advertisers[0].next_us == anchor
    assert iut.link.random.getstate() == random_state
    for address in (old, new):
        if address == new:
            iut.lt_tx(scan_req(advertiser=new))
            iut.run(1)
        packets = iut.lt_rx_all()
        if legacy:
            (response,) = [
                packet for packet in packets if packet.kind == PduType.SCAN_RSP
            ]
            assert response.pdu[2:] == bytes(address) + b'rsp'
        else:
            _, (_, response) = split_primary_auxiliary(packets)
            assert (
                response.ext.address == bytes(address) and response.ext.data == b'rsp'
            )
        iut.ut_rx(hci.HCI_LE_Scan_Request_Received_Event, scanner_address=PEER)
    iut.run(1)
    iut.ut_rx(
        TERMINATED,
        status=ErrorCode.LIMIT_REACHED_ERROR,
        num_completed_extended_advertising_events=1,
    )
    iut.ut_rx_q_is_empty()


# ---------------------------------------------------------------------------
# Scannable (4.4.2.5.2): AUX_SCAN_REQ / AUX_SCAN_RSP
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('notify', [0, 1])
def test_aux_scan_request_answered_with_scan_response(iut, notify):
    """A scannable set answers AUX_SCAN_REQ with AUX_SCAN_RSP.

    Purpose: Verify that a scannable extended set carries no AdvData in its
    AUX_ADV_IND, answers an AUX_SCAN_REQ with an AUX_SCAN_RSP on the same channel
    one T_IFS later carrying the IUT address and scan-response data, and raises a
    Scan Request Received event only when notifications are enabled (Vol 6,
    Part B, 4.4.2.5.2; Vol 4, Part E, 7.7.70).

    Steps:
        1. Upper Tester configures a scannable set with scan-response data and the
           parametrized notification setting.
        2. Upper Tester enables advertising.
        3. Lower Tester scripts an AUX_SCAN_REQ.
        4. Run one event and split primary from auxiliary transmissions.

    Expected:
        - The AUX_ADV_IND is scannable with empty AdvData.
        - The AUX_SCAN_RSP is on the AUX_ADV_IND's channel, one T_IFS after the
          request, carrying the IUT address and "response".
        - A Scan Request Received event is raised iff notifications are enabled.
        - Nothing else reaches the Host.

    Parameters range over scan-request notification disabled and enabled.
    """
    configure_extended(
        iut,
        SCANNABLE,
        scan_response=b'response',
        scan_request_notification_enable=notify,
    )
    enable_extended(iut)
    iut.lt_tx(scan_req(), phy=Phy.LE_1M)
    primary, auxiliary = split_primary_auxiliary(one_event(iut))
    aux_adv, response = auxiliary
    request = iut.radio.delivered[0]
    # AUX_ADV_IND of a scannable set carries no AdvData.
    assert aux_adv.ext.mode == AdvertisingMode.SCANNABLE and aux_adv.ext.data == b''
    assert response.channel == aux_adv.channel
    assert response.start_us == request.end_us + LL_IFS_US
    assert response.ext.address == bytes(IUT_ADDRESS)
    assert response.ext.data == b'response'
    if notify:
        iut.ut_rx(
            hci.HCI_LE_Scan_Request_Received_Event,
            advertising_handle=0,
            scanner_address=PEER,
        )
    iut.ut_rx_q_is_empty()


def test_disabling_during_scan_response_stops_the_auxiliary_chain(iut, monkeypatch):
    """Disabling a set lets its in-flight PDU finish and cancels the remaining chain.

    Purpose: Verify that an extended scan response stops at the next PDU boundary
    when the Host disables the set (Vol 4, Part E, 7.8.56).

    Steps:
        1. Enable a scannable set with a 300-byte scan response and notifications.
        2. Send an AUX_SCAN_REQ and disable the set as its first response PDU
           finishes transmitting.

    Expected:
        - The Host receives the scan request notification and successful command
          completion. The response carries an AuxPtr and no continuation is sent.
    """
    configure_extended(iut, SCANNABLE, scan_request_notification_enable=1)
    set_data = hci.HCI_LE_Set_Extended_Scan_Response_Data_Command
    iut.ut(set_data(0, Operation.FIRST_FRAGMENT, 0, b'a' * 150))
    iut.ut(set_data(0, Operation.LAST_FRAGMENT, 0, b'b' * 150))
    enable_extended(iut)
    transmit = iut.radio.transmit

    async def disable_after_response(*args, **kwargs):
        end = await transmit(*args, **kwargs)
        packet = iut.radio.sent[-1]
        if (
            packet.channel not in (37, 38, 39)
            and packet.ext.data
            and packet.ext.aux_ptr
        ):
            iut.controller.on_packet(
                bytes(
                    hci.HCI_LE_Set_Extended_Advertising_Enable_Command(0, [0], [0], [0])
                )
            )
        return end

    monkeypatch.setattr(iut.radio, 'transmit', disable_after_response)
    iut.lt_tx(scan_req())
    _, auxiliary = split_primary_auxiliary(one_event(iut))
    advertisement, response = auxiliary
    assert advertisement.ext.mode == AdvertisingMode.SCANNABLE
    assert response.ext.aux_ptr is not None
    assert (b'a' * 150 + b'b' * 150).startswith(response.ext.data)
    iut.ut_rx(
        hci.HCI_LE_Scan_Request_Received_Event,
        advertising_handle=0,
        scanner_address=PEER,
    )
    (complete,) = iut.events
    assert isinstance(complete, hci.HCI_Command_Complete_Event)
    assert complete.return_parameters.status == ErrorCode.SUCCESS


def test_aux_scan_request_for_other_advertiser_is_ignored(iut):
    """An AUX_SCAN_REQ for another advertiser draws no response.

    Purpose: Verify that a scannable set ignores an AUX_SCAN_REQ whose AdvA names
    a different advertiser, so only the AUX_ADV_IND is transmitted (Vol 6,
    Part B, 4.4.2.5.2).

    Steps:
        1. Upper Tester configures a scannable set with scan-response data.
        2. Upper Tester enables advertising.
        3. Lower Tester scripts an AUX_SCAN_REQ addressed to another advertiser.
        4. Run one event and split out the auxiliary transmissions.

    Expected:
        - Only one auxiliary PDU (the AUX_ADV_IND) is transmitted.
    """
    configure_extended(iut, SCANNABLE, scan_response=b'response')
    enable_extended(iut)
    iut.lt_tx(scan_req(advertiser=OTHER))
    _, auxiliary = split_primary_auxiliary(one_event(iut))
    assert len(auxiliary) == 1


@pytest.mark.parametrize('listed', [False, True])
def test_aux_scan_request_filter_policy(iut, listed):
    """AUX_SCAN_REQ filtering follows the advertising filter policy.

    Purpose: Verify that with filter policy 1 a scannable set answers an
    AUX_SCAN_REQ only when the scanner is on the Filter Accept List (Vol 4,
    Part E, 7.8.53).

    Steps:
        1. If listed, Upper Tester adds the peer to the Filter Accept List.
        2. Upper Tester configures a scannable set with scan-response data and
           filter policy 1.
        3. Upper Tester enables advertising.
        4. Lower Tester scripts an AUX_SCAN_REQ.
        5. Run one event and split out the auxiliary transmissions.

    Expected:
        - Two auxiliary PDUs (AUX_ADV_IND and AUX_SCAN_RSP) when listed, one
          otherwise.

    Parameters range over listed and unlisted.
    """
    if listed:
        iut.ut(
            hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(PEER.address_type, PEER)
        )
    configure_extended(iut, SCANNABLE, scan_response=b'r', advertising_filter_policy=1)
    enable_extended(iut)
    iut.lt_tx(scan_req())
    _, auxiliary = split_primary_auxiliary(one_event(iut))
    assert len(auxiliary) == (2 if listed else 1)


def test_scannable_enable_requires_scan_response_data(iut):
    """Enabling a scannable set without scan-response data is disallowed.

    Purpose: Verify that enabling a scannable extended set is rejected with
    Command Disallowed when no scan-response data has been set (Vol 4, Part E,
    7.8.56).

    Steps:
        1. Upper Tester configures a scannable set with no scan-response data.
        2. Upper Tester enables advertising.

    Expected:
        - The enable fails with COMMAND_DISALLOWED_ERROR.
    """
    configure_extended(iut, SCANNABLE)
    enable_extended(iut, status=ErrorCode.COMMAND_DISALLOWED_ERROR)


# ---------------------------------------------------------------------------
# Connectable (4.4.2.7): AUX_CONNECT_REQ / AUX_CONNECT_RSP
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('secondary_phy', list(Phy))
def test_aux_connect_request_creates_connection(iut, secondary_phy):
    """An AUX_CONNECT_REQ makes the IUT a Peripheral with AUX_CONNECT_RSP.

    Purpose: Verify that a connectable extended set answers an AUX_CONNECT_REQ
    with an AUX_CONNECT_RSP one T_IFS later carrying the IUT and peer addresses,
    forms a Peripheral connection on the secondary PHY using CSA #2, and reports
    Connection Complete, a Channel Selection Algorithm event and Advertising Set
    Terminated (Vol 6, Part B, 4.4.2.7, 4.5.8; Vol 4, Part E, 7.7.65.10,
    7.7.65.20, 7.7.65.18).

    Steps:
        1. Upper Tester configures a connectable set on the parametrized secondary
           PHY with AdvData.
        2. Upper Tester enables advertising.
        3. Lower Tester scripts an AUX_CONNECT_REQ on the secondary PHY.
        4. Run one event and split out the auxiliary transmissions.

    Expected:
        - The AUX_CONNECT_RSP is one T_IFS after the request, carrying the IUT
          address and the peer as target.
        - The Host gets Connection Complete as Peripheral, a Channel Selection
          Algorithm event with CSA #2, and Advertising Set Terminated with one
          completed event, and nothing else.
        - The connection uses the secondary PHY for TX and RX, with the request's
          interval.

    Parameters range over every secondary PHY.
    """
    configure_extended(
        iut, CONNECTABLE, data=b'c', secondary_advertising_phy=secondary_phy
    )
    enable_extended(iut)
    iut.lt_tx(connect_ind(), phy=secondary_phy)
    _, auxiliary = split_primary_auxiliary(one_event(iut))
    aux_adv, response = auxiliary
    request = iut.radio.delivered[0]
    assert response.kind == PduType.AUX_CONNECT_RSP
    assert response.start_us == request.end_us + LL_IFS_US
    assert response.ext.address == bytes(IUT_ADDRESS)
    assert response.ext.target == bytes(PEER)
    event = iut.ut_rx(CONNECTED, status=0, role=hci.Role.PERIPHERAL, peer_address=PEER)
    # Extended advertising connections always use CSA #2 (4.5.8).
    iut.ut_rx(
        hci.HCI_LE_Channel_Selection_Algorithm_Event,
        connection_handle=event.connection_handle,
        channel_selection_algorithm=1,
    )
    iut.ut_rx(
        TERMINATED,
        status=0,
        advertising_handle=0,
        connection_handle=event.connection_handle,
        num_completed_extended_advertising_events=1,
    )
    iut.ut_rx_q_is_empty()
    (connection,) = iut.link.connections.values()
    assert connection.tx_phy == connection.rx_phy == secondary_phy
    assert connection.parameters.interval == CONNECT_PARAMETERS.interval


@pytest.mark.parametrize('phase', ['request-rx', 'response-tx'])
@pytest.mark.parametrize(
    'action', ['disable', 'reenable', 'restart', 'reuse-handle', 'reset']
)
def test_host_command_during_aux_connection_exchange(iut, phase, action):
    """An AUX_CONNECT_RSP creates the Peripheral connection (Vol 6, Part B, 4.5).

    Steps:
        1. Enable connectable advertising and send AUX_CONNECT_REQ.
        2. While receiving the request or transmitting the response, disable,
           repeat enable, stop and restart, reuse the handle, or issue HCI Reset.

    Expected:
        Re-enable preserves the exchange. Stopping the set before the request
        is accepted suppresses the response. Once transmitting, the response
        creates a connection unless Reset releases it. The completion cannot
        terminate a newer enable or a replacement set.
    """
    configure_extended(iut, CONNECTABLE, primary_advertising_channel_map=1)
    enable_extended(iut)
    iut.lt_tx(connect_ind())
    method, occurrence = ('receive', 1) if phase == 'request-rx' else ('transmit', 3)
    with iut.pause_radio(
        iut.link.advance_to_us(iut.now_us + 1), method, occurrence=occurrence
    ):
        if action == 'reset':
            iut.ut(hci.HCI_Reset_Command())
            iut.ut(hci.HCI_Set_Event_Mask_Command(b'\xff' * 8))
            iut.ut(hci.HCI_LE_Set_Event_Mask_Command(b'\xff' * 8))
        elif action == 'reenable':
            enable_extended(iut)
        else:
            enable_extended(iut, on=0)
            if action == 'reuse-handle':
                iut.ut(hci.HCI_LE_Remove_Advertising_Set_Command(0))
                configure_extended(iut, CONNECTABLE)
            if action != 'disable':
                enable_extended(iut)

    responses = [
        packet for packet in iut.lt_rx_all() if packet.kind == PduType.AUX_CONNECT_RSP
    ]
    sent = phase == 'response-tx' or action == 'reenable'
    assert len(responses) == int(sent)
    assert bool(iut.link.connections) == (sent and action != 'reset')
    assert iut.ll.advertising.any_enabled() == (action in ('restart', 'reuse-handle'))
    if sent:
        assert responses[0].ext.target == bytes(PEER)
    if sent and action != 'reset':
        handle = iut.ut_rx(
            CONNECTED, status=0, role=hci.Role.PERIPHERAL, peer_address=PEER
        ).connection_handle
        assert handle in iut.link.connections
        iut.ut_rx(
            hci.HCI_LE_Channel_Selection_Algorithm_Event, connection_handle=handle
        )
        if action == 'reenable':
            iut.ut_rx(
                TERMINATED,
                status=0,
                connection_handle=handle,
                advertising_handle=0,
                num_completed_extended_advertising_events=0,
            )
    iut.ut_rx_q_is_empty()


def test_connectable_data_must_fit_one_pdu(iut):
    """Connectable set data larger than one PDU is rejected.

    Purpose: Verify that Set Extended Advertising Data rejects a payload that
    cannot fit a single connectable AUX_ADV_IND (Vol 4, Part E, 7.8.54).

    Steps:
        1. Upper Tester sets connectable extended parameters.
        2. Upper Tester loads 250 octets of AdvData.

    Expected:
        - The data command fails with PACKET_TOO_LONG_ERROR.
    """
    iut.ut(extended_parameters(properties=CONNECTABLE))
    iut.ut(
        hci.HCI_LE_Set_Extended_Advertising_Data_Command(
            0, Operation.COMPLETE_DATA, 0, bytes(250)
        ),
        ErrorCode.PACKET_TOO_LONG_ERROR,
    )


@pytest.mark.parametrize('initiator', [PEER, OTHER])
def test_directed_connectable_accepts_only_target(iut, initiator):
    """A directed connectable set connects only to its target.

    Purpose: Verify that a directed connectable extended set advertises its
    TargetA and forms a connection only when the AUX_CONNECT_REQ comes from that
    target, not from another device (Vol 6, Part B, 4.4.2.7).

    Steps:
        1. Upper Tester configures a directed connectable set and enables it.
        2. Confirm the AUX_ADV_IND carries the peer as TargetA.
        3. Clear the script and let the Lower Tester send an AUX_CONNECT_REQ from
           the parametrized initiator.
        4. Run past one interval plus the maximum advDelay.

    Expected:
        - A connection exists iff the initiator is the peer.

    Parameters range over the peer and another device as initiator.
    """
    configure_extended(iut, CONNECTABLE | DIRECTED)
    enable_extended(iut)
    _, (aux,) = split_primary_auxiliary(one_event(iut)[:4])
    assert aux.ext.target == bytes(PEER)
    iut.radio.script.clear()
    iut.lt_tx(connect_ind(initiator=initiator))
    iut.run(INTERVAL_US + 10_001)
    assert bool(iut.link.connections) == (initiator == PEER)


def test_connect_request_with_malformed_parameters_is_ignored(iut):
    """An AUX_CONNECT_REQ with truncated LLData is ignored.

    Purpose: Verify that a connectable set drops an AUX_CONNECT_REQ whose LLData
    is a byte short, forming no connection and sending no AUX_CONNECT_RSP (Vol 6,
    Part B, 2.3.3.1, 4.4.2.7).

    Steps:
        1. Upper Tester configures a connectable set and enables it.
        2. Lower Tester scripts an AUX_CONNECT_REQ with truncated LLData.
        3. Run one event and split out the auxiliary transmissions.

    Expected:
        - Only one auxiliary PDU (the AUX_ADV_IND) was sent.
        - No connection is created.
    """
    configure_extended(iut, CONNECTABLE)
    enable_extended(iut)
    iut.lt_tx(connect_ind(parameters=CONNECT_PARAMETERS.encode()[:-1]))
    _, auxiliary = split_primary_auxiliary(one_event(iut))
    assert len(auxiliary) == 1 and not iut.link.connections


# ---------------------------------------------------------------------------
# Lifetime: duration, max events, disable, remove (7.8.56-7.8.60)
# ---------------------------------------------------------------------------
def test_duration_expiry_terminates_with_timeout(iut):
    """An advertising duration expiry terminates the set with a timeout.

    Purpose: Verify that an extended set enabled with a duration stops at the
    deadline, reports Advertising Set Terminated with an advertising timeout and
    the number of completed events, and transmits nothing afterward (Vol 6,
    Part B, 4.4.2.6; Vol 4, Part E, 7.8.56).

    Steps:
        1. Upper Tester configures a set and enables it with a 300 ms duration.
        2. Run past the duration.
        3. Drain transmissions, then run three more intervals.

    Expected:
        - The Host gets Advertising Set Terminated with
          ADVERTISING_TIMEOUT_ERROR and at least two completed events, and
          nothing else.
        - No PDU is transmitted after expiry.
    """
    configure_extended(iut)
    enable_extended(iut, duration=30)  # 300 ms
    iut.run(310_000)
    event = iut.ut_rx(TERMINATED, status=ErrorCode.ADVERTISING_TIMEOUT_ERROR)
    assert event.num_completed_extended_advertising_events >= 2
    iut.ut_rx_q_is_empty()
    iut.lt_rx_all()
    iut.run(INTERVAL_US * 3)
    iut.lt_rx_q_is_empty()


def test_max_events_terminates_with_limit_reached(iut):
    """A max-events limit terminates the set after exactly that many events.

    Purpose: Verify that an extended set enabled with a max-events limit stops
    after that many advertising events, reports Advertising Set Terminated with
    Limit Reached and the completed count, and sends no further events (Vol 6,
    Part B, 4.4.2.6; Vol 4, Part E, 7.8.56).

    Steps:
        1. Upper Tester configures a set and enables it with max events 3.
        2. Run ten intervals.
        3. Read all transmissions.

    Expected:
        - The Host gets Advertising Set Terminated with LIMIT_REACHED_ERROR and
          three completed events.
        - Exactly three events were sent (three primary PDUs each).
    """
    configure_extended(iut)
    enable_extended(iut, max_events=3)
    iut.run(10 * INTERVAL_US)
    iut.ut_rx(
        TERMINATED,
        status=ErrorCode.LIMIT_REACHED_ERROR,
        num_completed_extended_advertising_events=3,
    )
    primary, _ = split_primary_auxiliary(iut.lt_rx_all())
    assert len(primary) == 3 * 3


def test_multiple_sets_advertise_independently(iut):
    """Two enabled sets advertise their own data independently.

    Purpose: Verify that two extended advertising sets enabled together each
    transmit their own AdvData (Vol 6, Part B, 4.4.2.2.2; Vol 4, Part E,
    7.8.56).

    Steps:
        1. Upper Tester configures set 0 with "zero" and set 1 with "one".
        2. Upper Tester enables both sets.
        3. Run three intervals and split out the auxiliary transmissions.

    Expected:
        - The auxiliary payloads seen are exactly {"zero", "one"}.
    """
    configure_extended(iut, data=b'zero', handle=0)
    configure_extended(iut, data=b'one', handle=1, advertising_sid=6)
    iut.ut(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0, 1], [0, 0], [0, 0])
    )
    iut.run(3 * INTERVAL_US)
    _, auxiliary = split_primary_auxiliary(iut.lt_rx_all())
    assert {packet.ext.data for packet in auxiliary} == {b'zero', b'one'}


def test_disable_all_sets(iut):
    """Disabling with an empty handle list stops every set.

    Purpose: Verify that Set Extended Advertising Enable with disable and empty
    lists stops all advertising sets at once (Vol 4, Part E, 7.8.56).

    Steps:
        1. Upper Tester configures sets 0 and 1.
        2. Upper Tester enables both sets.
        3. Upper Tester disables with empty handle, duration and max-events lists.

    Expected:
        - No advertising set is enabled.
    """
    configure_extended(iut, handle=0)
    configure_extended(iut, handle=1)
    iut.ut(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0, 1], [0, 0], [0, 0])
    )
    iut.ut(hci.HCI_LE_Set_Extended_Advertising_Enable_Command(0, [], [], []))
    assert not iut.ll.advertising.any_enabled()


def test_enable_unknown_set_changes_nothing(iut):
    """Enabling a batch that names an unknown set changes nothing.

    Purpose: Verify that a Set Extended Advertising Enable listing an unknown
    handle alongside a known one is rejected with Unknown Advertising Identifier
    and leaves all sets disabled (Vol 4, Part E, 7.8.56).

    Steps:
        1. Upper Tester configures set 0.
        2. Upper Tester enables handles [0, 9].

    Expected:
        - The command fails with UNKNOWN_ADVERTISING_IDENTIFIER_ERROR.
        - No advertising set is enabled.
    """
    configure_extended(iut, handle=0)
    iut.ut(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0, 9], [0, 0], [0, 0]),
        ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR,
    )
    assert not iut.ll.advertising.any_enabled()


def test_remove_and_clear(iut):
    """Advertising sets can be removed and cleared only while disabled.

    Purpose: Verify that Remove Advertising Set and Clear Advertising Sets are
    disallowed while a set is enabled, succeed once it is disabled, that removing
    a nonexistent set reports Unknown Advertising Identifier, and that Clear
    removes all sets (Vol 4, Part E, 7.8.59, 7.8.60).

    Steps:
        1. Upper Tester configures set 3 and enables it.
        2. Upper Tester tries to remove set 3 and to clear all sets.
        3. Upper Tester disables set 3 and removes it.
        4. Upper Tester removes set 3 again.
        5. Upper Tester configures set 4 and clears all sets.

    Expected:
        - Remove and Clear while enabled fail with COMMAND_DISALLOWED_ERROR.
        - Remove after disable succeeds.
        - The second remove fails with UNKNOWN_ADVERTISING_IDENTIFIER_ERROR.
        - After Clear no advertisers remain.
    """
    configure_extended(iut, handle=3)
    enable_extended(iut, handle=3)
    iut.ut(
        hci.HCI_LE_Remove_Advertising_Set_Command(3), ErrorCode.COMMAND_DISALLOWED_ERROR
    )
    iut.ut(
        hci.HCI_LE_Clear_Advertising_Sets_Command(), ErrorCode.COMMAND_DISALLOWED_ERROR
    )
    enable_extended(iut, handle=3, on=0)
    iut.ut(hci.HCI_LE_Remove_Advertising_Set_Command(3))
    iut.ut(
        hci.HCI_LE_Remove_Advertising_Set_Command(3),
        ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR,
    )
    configure_extended(iut, handle=4)
    iut.ut(hci.HCI_LE_Clear_Advertising_Sets_Command())
    assert not iut.link.advertisers


def test_set_capacity(iut):
    """The controller enforces its advertising-set capacity.

    Purpose: Verify that the controller accepts as many advertising sets as it
    reports supporting, rejects one more with Memory Capacity Exceeded, and
    reports a maximum advertising data length of at least 1650 octets (Vol 4,
    Part E, 7.8.57, 7.8.58).

    Steps:
        1. Read the number of supported advertising sets.
        2. Configure that many sets.
        3. Configure one more set.
        4. Read the maximum advertising data length.

    Expected:
        - The extra set fails with MEMORY_CAPACITY_EXCEEDED_ERROR.
        - The maximum advertising data length is at least 1650.
    """
    count = iut.ut(
        hci.HCI_LE_Read_Number_Of_Supported_Advertising_Sets_Command()
    ).num_supported_advertising_sets
    for handle in range(count):
        iut.ut(extended_parameters(handle))
    iut.ut(
        extended_parameters(count),
        ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR,
    )
    length = iut.ut(
        hci.HCI_LE_Read_Maximum_Advertising_Data_Length_Command()
    ).max_advertising_data_length
    assert length >= 1650


def test_set_random_address(iut):
    """A connectable set's random address is advertised and locked while enabled.

    Purpose: Verify that Set Advertising Set Random Address applies the address to
    a set's AUX_ADV_IND with the random flag, is disallowed while the set is
    enabled, and reports Unknown Advertising Identifier for an unknown set (Vol 4,
    Part E, 7.8.52).

    Steps:
        1. Upper Tester configures set 0 with own address type RANDOM.
        2. Upper Tester sets the set's random address and enables it.
        3. Run one event and take the AUX_ADV_IND.
        4. Upper Tester sets the random address again while enabled.
        5. Upper Tester sets a random address for an unknown set.

    Expected:
        - The AUX_ADV_IND carries the random address with the random flag set.
        - The address update while enabled fails with COMMAND_DISALLOWED_ERROR.
        - The unknown-set update fails with
          UNKNOWN_ADVERTISING_IDENTIFIER_ERROR.
    """
    random = hci.Address('C2:00:00:00:00:01', hci.AddressType.RANDOM_DEVICE)
    configure_extended(
        iut, CONNECTABLE, data=b'r', own_address_type=hci.OwnAddressType.RANDOM
    )
    iut.ut(hci.HCI_LE_Set_Advertising_Set_Random_Address_Command(0, random))
    enable_extended(iut)
    _, (aux,) = split_primary_auxiliary(one_event(iut))
    assert aux.ext.address == bytes(random) and aux.ext.random_address
    iut.ut(
        hci.HCI_LE_Set_Advertising_Set_Random_Address_Command(0, random),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )
    iut.ut(
        hci.HCI_LE_Set_Advertising_Set_Random_Address_Command(9, random),
        ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR,
    )


@pytest.mark.parametrize(
    'changes,status',
    [
        (
            dict(primary_advertising_channel_map=0),
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
        (
            dict(
                primary_advertising_interval_min=0x20,
                primary_advertising_interval_max=0x10,
            ),
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
        # LE 2M cannot carry primary advertising: reported as unsupported.
        (
            dict(primary_advertising_phy=Phy.LE_2M),
            ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
        ),
        (dict(advertising_sid=16), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (dict(advertising_handle=0xF0), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (
            dict(advertising_event_properties=CONNECTABLE | SCANNABLE),
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
    ],
)
def test_parameter_validation(iut, changes, status):
    """Set Extended Advertising Parameters rejects invalid and unsupported fields.

    Purpose: Verify that Set Extended Advertising Parameters returns the right
    error for an empty channel map, an inverted interval range, an LE 2M primary
    PHY, an out-of-range SID, an out-of-range handle, and the illegal
    connectable+scannable combination (Vol 4, Part E, 7.8.53).

    Steps:
        1. Upper Tester sends Set Extended Advertising Parameters with one invalid
           field.

    Expected:
        - The command completes with the parametrized error status.

    Parameters range over empty channel map, inverted interval range, LE 2M
    primary PHY (unsupported), SID 16, handle 0xF0, and connectable+scannable.
    """
    iut.ut(extended_parameters(**changes), status)


def test_parameters_disallowed_while_enabled(iut):
    """Set Extended Advertising Parameters is disallowed while enabled.

    Purpose: Verify that Set Extended Advertising Parameters is rejected with
    Command Disallowed while the set is advertising (Vol 4, Part E, 7.8.53).

    Steps:
        1. Upper Tester configures a set and enables it.
        2. Upper Tester sends Set Extended Advertising Parameters again.

    Expected:
        - The command completes with COMMAND_DISALLOWED_ERROR.
    """
    configure_extended(iut)
    enable_extended(iut)
    iut.ut(extended_parameters(), ErrorCode.COMMAND_DISALLOWED_ERROR)


def test_data_fragment_sequence_rules(iut):
    """Extended advertising data fragments must follow the operation sequence.

    Purpose: Verify that an intermediate fragment without a preceding first
    fragment is disallowed, that a first/intermediate/last sequence assembles the
    data, that data for an unknown set reports Unknown Advertising Identifier, and
    that an invalid operation is rejected (Vol 4, Part E, 7.8.54).

    Steps:
        1. Upper Tester sets extended parameters for set 0.
        2. Upper Tester sends an intermediate fragment first.
        3. Upper Tester sends first, intermediate and last fragments.
        4. Upper Tester sends data for unknown set 9.
        5. Upper Tester sends an invalid operation value.

    Expected:
        - The orphaned intermediate fragment fails with
          COMMAND_DISALLOWED_ERROR.
        - The assembled data is "abc".
        - The unknown-set command fails with
          UNKNOWN_ADVERTISING_IDENTIFIER_ERROR.
        - The invalid operation fails with INVALID_COMMAND_PARAMETERS_ERROR.
    """
    iut.ut(extended_parameters())
    command = hci.HCI_LE_Set_Extended_Advertising_Data_Command
    # 7.8.54 leaves an orphaned continuation's status open; it never joins data.
    iut.ut(
        command(0, Operation.INTERMEDIATE_FRAGMENT, 0, b'a'),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )
    iut.ut(command(0, Operation.FIRST_FRAGMENT, 0, b'a'))
    iut.ut(command(0, Operation.INTERMEDIATE_FRAGMENT, 0, b'b'))
    iut.ut(command(0, Operation.LAST_FRAGMENT, 0, b'c'))
    assert bytes(iut.link.advertisers[0].data) == b'abc'
    iut.ut(
        command(9, Operation.COMPLETE_DATA, 0, b''),
        ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR,
    )
    iut.ut(command(0, 5, 0, b''), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR)


def test_extended_report_of_own_event_round_trips(iut):
    """The transmitted PDUs decode with the same codec the scanner uses.

    Purpose: Verify that an AUX_ADV_IND the IUT transmits re-encodes to the same
    bytes after decoding, so the extended-PDU codec round-trips the controller's
    own output (Vol 6, Part B, 2.3.4).

    Steps:
        1. Upper Tester configures a set with AdvData "roundtrip" and enables it.
        2. Run one event and take the AUX_ADV_IND.
        3. Decode and re-encode the transmitted PDU.

    Expected:
        - The re-encoded bytes equal the transmitted PDU.
    """
    configure_extended(iut, data=b'roundtrip')
    enable_extended(iut)
    _, (aux,) = split_primary_auxiliary(one_event(iut))
    assert ExtendedPdu.decode(aux.pdu).encode() == aux.pdu
