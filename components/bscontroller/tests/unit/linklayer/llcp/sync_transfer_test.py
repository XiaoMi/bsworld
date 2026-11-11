# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Periodic Advertising Sync Transfer (Core Vol 6, Part B, 2.4.2.27, 5.1.13;
Vol 4, Part E, 7.8.89-7.8.92).

Matrix: sender {local set, synchronized train} x {SyncInfo timing, field
encoding, waiting for a describable train, dropping an ended one,
retransmission, HCI errors}; recipient {transfer mode, PHY validity,
connEventCount in the past/future, counter limits, duplicate train, address
resolution, length, feature, capacity, CTE_Type}. Scenarios drive the LLCP
harness (llcp/harness.py); the first-AUX_SYNC_IND outcome is covered on the
air in air/sync_transfer_test.py.
"""

import struct

import pytest

from bscontroller.address import Address, AddressType
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    LL_DATA_CHANNEL_MAP_ALL,
    LL_EVENT_COUNTER_MASK,
    LL_PERIODIC_INTERVAL_UNIT_US,
)
from bscontroller.linklayer.advertising import SyncInfo
from bscontroller.linklayer.discovery import PeriodicSync
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.privacy import address_hash
from bscontroller.radio.base import Phy
from tests.unit.helpers import extended_parameters
from tests.unit.linklayer.llcp.harness import (
    CENTRAL,
    PERIPHERAL,
    ROLES,
    ErrorCode,
    Opcode,
    hci,
    pdu,
    unknown_rsp,
)

RECIPIENT = LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_RECIPIENT
RECEIVED = hci.HCI_LE_Periodic_Advertising_Sync_Transfer_Received_Event
PERIODIC_INTERVAL = 80
PERIODIC_US = PERIODIC_INTERVAL * LL_PERIODIC_INTERVAL_UNIT_US
CONNECTION_US = 24 * LL_CONN_INTERVAL_UNIT_US
SYNC_AA = 0x8E89BED7 ^ 0x13579BDF
SYNC_CRC = 0x445566
SERVICE_DATA = 0xA5C3
ADVERTISER = Address('00:11:22:33:44:77', AddressType.PUBLIC_DEVICE)
IDENTITY = Address('C0:AA:BB:CC:DD:EE', AddressType.RANDOM_DEVICE)
PEER_IRK = bytes(range(1, 17))
# Independent LL_PERIODIC_SYNC_IND CtrData layout (Vol 6, Part B, Figure 2.48).
CTRDATA = struct.Struct('<H18sHHBB6sH')
OFFSET_UNIT_300 = 1 << 13
OFFSET_ADJUST = 1 << 14
OFFSET_ADJUST_US = 2_457_600


def rpa(key, prand=bytes.fromhex('456789')[::-1]):
    return Address(address_hash(key, prand) + prand, AddressType.RANDOM_DEVICE)


def sync_info(offset_field, interval=PERIODIC_INTERVAL, counter=0, sca=5):
    return struct.pack(
        '<HH5sI3sH',
        offset_field,
        interval,
        (LL_DATA_CHANNEL_MAP_ALL | sca << 37).to_bytes(5, 'little'),
        SYNC_AA,
        SYNC_CRC.to_bytes(3, 'little'),
        counter,
    )


def indication(
    *,
    info=None,
    event=0,
    last=0,
    sid=3,
    random=False,
    sca=0,
    phy=0x01,
    address=ADVERTISER,
    sync_event=0,
    identifier=SERVICE_DATA,
):
    info = sync_info(100) if info is None else info
    return pdu(
        Opcode.LL_PERIODIC_SYNC_IND,
        CTRDATA.pack(
            identifier,
            info,
            event,
            last,
            sid | int(random) << 4 | sca << 5,
            phy,
            bytes(address),
            sync_event,
        ),
    )


def decode(payload):
    """Decode the PDU independently and resolve its SyncInfo reference point."""
    assert payload[0] == Opcode.LL_PERIODIC_SYNC_IND and len(payload) == 35
    identifier, info, event, last, flags, phy, address, sync_event = CTRDATA.unpack(
        payload[1:]
    )
    offset, interval, channels, aa, crc, counter = struct.unpack('<HH5sI3sH', info)
    unit = 300 if offset & OFFSET_UNIT_300 else 30
    delay = (offset & 0x1FFF) * unit + (
        OFFSET_ADJUST_US if offset & OFFSET_ADJUST else 0
    )
    return {
        'id': identifier,
        'event': event,
        'last': last,
        'sid': flags & 0x0F,
        'atype': flags >> 4 & 1,
        'sca': flags >> 5,
        'phy': phy,
        'address': address,
        'sync_event': sync_event,
        'delay': delay,
        'unit': unit,
        'interval': interval,
        'channels': int.from_bytes(channels, 'little'),
        'aa': aa,
        'crc': int.from_bytes(crc, 'little'),
        'counter': counter,
    }


def anchor_of(dut, fields):
    """Tnominal of the described AUX_SYNC_IND on the IUT's connection grid."""
    distance = (fields['event'] - dut.event_counter) & LL_EVENT_COUNTER_MASK
    if distance & 0x8000:
        distance -= LL_EVENT_COUNTER_MASK + 1
    return dut.connection.anchor_us + distance * CONNECTION_US + fields['delay']


def command(dut, packet):
    """Send an HCI command; pop and return its Command Complete parameters."""
    (event,) = dut.hci(packet)
    assert isinstance(event, hci.HCI_Command_Complete_Event)
    dut.controller.events.remove(event)
    return event.return_parameters


def command_status(dut, packet):
    return command(dut, packet).status


def periodic_set(dut, interval=PERIODIC_INTERVAL, *, enable=True):
    assert command_status(dut, extended_parameters()) == ErrorCode.SUCCESS
    parameters = hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(
        0, interval, interval, 0
    )
    assert command_status(dut, parameters) == ErrorCode.SUCCESS
    if enable:
        enable_command = hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0)
        assert command_status(dut, enable_command) == ErrorCode.SUCCESS
    return dut.link.advertisers[0]


def transmitted(dut, item):
    """The train transmits its scheduled AUX_SYNC_IND, as periodic_transmit does."""
    item.periodic_last_counter = item.periodic_counter
    dut.link.roles.advertising.periodic_completed(item)


def set_info_transfer(dut, handle=None, advertising_handle=0):
    return command_status(
        dut,
        hci.HCI_LE_Periodic_Advertising_Set_Info_Transfer_Command(
            dut.handle if handle is None else handle, SERVICE_DATA, advertising_handle
        ),
    )


def sync_transfer(dut, handle=None, sync_handle=1):
    return command(
        dut,
        hci.HCI_LE_Periodic_Advertising_Sync_Transfer_Command(
            dut.handle if handle is None else handle, SERVICE_DATA, sync_handle
        ),
    )


def synchronized(dut, *, last_rx_us, counter=7, address=ADVERTISER, phy=Phy.LE_1M):
    sync = PeriodicSync(
        address,
        2,
        10_000_000,
        10**12,
        info=SyncInfo(0, PERIODIC_INTERVAL, 0x1F00FF, SYNC_AA, SYNC_CRC, 0, sca=2),
        established=True,
        handle=1,
        last_rx_us=last_rx_us,
        last_counter=counter,
        phy=phy,
        advertiser_address=address,
    )
    dut.link.syncs[1] = sync
    return sync


def transfer_parameters(dut, mode=2, skip=0, timeout=100, cte=0, handle=None):
    return command(
        dut,
        hci.HCI_LE_Set_Periodic_Advertising_Sync_Transfer_Parameters_Command(
            dut.handle if handle is None else handle, mode, skip, timeout, cte
        ),
    )


def default_parameters(dut, mode=2, skip=0, timeout=100, cte=0):
    return command(
        dut,
        hci.HCI_LE_Set_Default_Periodic_Advertising_Sync_Transfer_Parameters_Command(
            mode, skip, timeout, cte
        ),
    )


def receive_mode(dut, mode=2, skip=0, timeout=100):
    return transfer_parameters(dut, mode, skip, timeout).status


def deliver(dut, payload):
    """The peer sends the PDU; the IUT acknowledges it and replies nothing."""
    dut.event_prepare()
    dut.lt_tx(payload)
    dut.lt_rx_q_is_empty()
    dut.event_done()


def transfers(dut):
    return dut.link.roles.synchronization.transfers


# ---------------------------------------------------------------------------
# Sender: local advertising set
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('role', ROLES)
def test_set_info_transfer_waits_for_the_first_aux_sync_ind(iut, role):
    """Set Info Transfer is sent once the train has transmitted an event.

    Purpose: Verify that the Advertising IUT holds LL_PERIODIC_SYNC_IND until
    its new train has transmitted an AUX_SYNC_IND, which lastPaEventCounter
    must name, then sends the Host Service_Data, its SID, address type,
    address, SCA, PHY bit and a SyncInfo that, relative to connEventCount,
    points at the next scheduled event within one offset unit (Vol 6, Part B,
    2.4.2.27; Vol 4, Part E, 7.8.90). Runs for both roles.

    Steps:
        1. Enable a periodic train; the peer has the Recipient feature.
        2. Upper Tester sends Set Info Transfer; connection events run past
           the train's first scheduled event without it being transmitted.
        3. The train transmits event 0; Lower Tester receives
           LL_PERIODIC_SYNC_IND in the next connection event and acknowledges.

    Expected:
        - Command Complete succeeds; no PDU is sent before step 3.
        - connEventCount names the transmitting event; the SyncInfo anchor is
          at or before the scheduled event 1, within one unit;
          lastPaEventCounter is 0 and paEventCounter 1.
        - ID, SID 3, public AType, SCA 50 ppm, PHY 0x01, AdvA and train
          identity match the set; no response timer runs; nothing reaches the Host.
    """
    dut = iut(role)
    item = periodic_set(dut)
    dut.set_peer_features(RECIPIENT)
    assert set_info_transfer(dut) == ErrorCode.SUCCESS
    assert dut.controls.deadline_us == 0
    first_us = item.periodic_next_us
    while dut.connection.anchor_us <= first_us:
        dut.run_events(1)
    transmitted(dut, item)
    dut.event_prepare()
    fields = decode(dut.lt_rx(Opcode.LL_PERIODIC_SYNC_IND))
    dut.tx_ack()
    assert fields['event'] == dut.event_counter
    target = anchor_of(dut, fields)
    dut.event_done()
    assert target <= item.periodic_next_us < target + fields['unit']
    assert (fields['last'], fields['counter']) == (0, 1) == (0, item.periodic_counter)
    assert (fields['id'], fields['sid'], fields['atype']) == (SERVICE_DATA, 3, 0)
    assert (fields['sca'], fields['phy']) == (5, 0x01)
    assert fields['address'] == bytes(dut.controller.public_address)
    assert (fields['aa'], fields['crc']) == (item.periodic_aa, item.periodic_crc)
    assert fields['interval'] == PERIODIC_INTERVAL
    assert fields['channels'] & LL_DATA_CHANNEL_MAP_ALL == LL_DATA_CHANNEL_MAP_ALL
    dut.ut_rx_q_is_empty()
    assert not dut.controls.busy


@pytest.mark.parametrize('order', ['disable', 'disable-enable'])
def test_set_info_transfer_dropped_when_the_train_ends(central, order):
    """A transfer still waiting when its train stops is dropped.

    Purpose: Verify that disabling the set's periodic advertising before its
    first AUX_SYNC_IND discards the waiting LL_PERIODIC_SYNC_IND, also when
    the Host enables a new train before the next connection event, and that a
    later transfer is not held behind it (Vol 6, Part B, 2.4.2.27; Vol 4,
    Part E, 7.8.90).

    Steps:
        1. Enable a periodic train; send Set Info Transfer before it transmits.
        2. Disable periodic advertising (variant: and enable it again); run a
           connection event.
        3. Enable it if disabled; the train transmits event 0; send Set Info
           Transfer again and run a connection event.

    Expected:
        - No PDU in step 2, and no waiting transfer afterwards.
        - Exactly one LL_PERIODIC_SYNC_IND in step 3, for the new train.
    """
    item = periodic_set(central)
    central.set_peer_features(RECIPIENT)
    assert set_info_transfer(central) == ErrorCode.SUCCESS
    toggle = hci.HCI_LE_Set_Periodic_Advertising_Enable_Command
    assert command_status(central, toggle(0, 0)) == ErrorCode.SUCCESS
    if order == 'disable-enable':
        assert command_status(central, toggle(1, 0)) == ErrorCode.SUCCESS
    central.run_events(1)
    assert not central.controls.transfers
    if order == 'disable':
        assert command_status(central, toggle(1, 0)) == ErrorCode.SUCCESS
    transmitted(central, item)
    assert set_info_transfer(central) == ErrorCode.SUCCESS
    central.event_prepare()
    fields = decode(central.lt_rx(Opcode.LL_PERIODIC_SYNC_IND))
    assert fields['aa'] == item.periodic_aa
    central.tx_ack()
    central.event_done()
    central.run_events(1)
    assert not central.controls.transfers and not central.controls.released


def test_set_info_transfer_names_last_transmitted_event(central):
    """lastPaEventCounter names an AUX_SYNC_IND the advertiser transmitted.

    Purpose: Verify that once the train has transmitted events, the PDU's
    lastPaEventCounter is the last transmitted paEventCounter, adjacent to the
    described event (Vol 6, Part B, 2.4.2.27).

    Steps:
        1. Enable a periodic train and mark event 4 transmitted, event 5 next.
        2. Send Set Info Transfer and receive the PDU.

    Expected:
        - lastPaEventCounter is 4 and paEventCounter is 5.
    """
    item = periodic_set(central)
    item.periodic_counter, item.periodic_last_counter = 5, 4
    central.set_peer_features(RECIPIENT)
    assert set_info_transfer(central) == ErrorCode.SUCCESS
    central.event_prepare()
    fields = decode(central.lt_rx(Opcode.LL_PERIODIC_SYNC_IND))
    assert (fields['last'], fields['counter']) == (4, 5)


def test_long_offset_moves_conn_event_count(central):
    """A train event beyond the largest offset moves connEventCount forward.

    Purpose: Verify that when the next train event is more than the largest
    syncPacketWindowOffset from this event, the sender names the latest earlier
    connection event, keeping the offset below one connection interval
    (Vol 6, Part B, 2.4.2.27, 2.3.4.6).

    Steps:
        1. Enable a train with a 5-s interval; it transmitted event 0, and
           event 1 is 5 s away.
        2. Send Set Info Transfer and receive the PDU.

    Expected:
        - connEventCount is ahead of this event by fewer than 2^14 events.
        - The offset is below one connection interval; the anchor lies within
          one unit before the scheduled event.
    """
    item = periodic_set(central, 4000)
    item.periodic_counter, item.periodic_last_counter = 1, 0
    central.set_peer_features(RECIPIENT)
    assert set_info_transfer(central) == ErrorCode.SUCCESS
    central.event_prepare()
    fields = decode(central.lt_rx(Opcode.LL_PERIODIC_SYNC_IND))
    distance = (fields['event'] - central.event_counter) & LL_EVENT_COUNTER_MASK
    assert 0 < distance < 1 << 14
    assert fields['delay'] < CONNECTION_US
    target = anchor_of(central, fields)
    assert target <= item.periodic_next_us < target + fields['unit']


@pytest.mark.parametrize('role', ROLES)
def test_sync_conn_event_count_names_an_earlier_reception(iut, role):
    """syncConnEventCount names an earlier event that received a packet.

    Purpose: Verify that the sender names the latest event with a received
    packet, and an earlier one once this event has received, so that it never
    equals the transmitting event (Vol 6, Part B, 2.4.2.27). Runs for both
    roles.

    Steps:
        1. Receive packets in events 3 and 4; run to event 6; the train has
           transmitted.
        2. Send Set Info Transfer; receive in event 6 before transmitting.

    Expected:
        - Before reception in event 6 the PDU would name event 4; after it,
          the transmitted PDU still names event 4.
    """
    dut = iut(role)
    transmitted(dut, periodic_set(dut))
    dut.set_peer_features(RECIPIENT)
    connection = dut.connection
    for _ in range(3):
        dut.run_events(1)
    for _ in range(2):
        connection.received_in_event()
        dut.run_events(1)
    dut.run_events(1)
    assert connection.event_counter == 6 and connection.sync_event_counter == 4
    connection.received_in_event()
    assert set_info_transfer(dut) == ErrorCode.SUCCESS
    dut.event_prepare()
    assert decode(dut.lt_rx(Opcode.LL_PERIODIC_SYNC_IND))['sync_event'] == 4


def test_sync_conn_event_count_without_an_earlier_reception(peripheral):
    """With only this event received, syncConnEventCount names this event.

    Purpose: Verify that before any reception, and when only the current event
    has received a packet, the sender names an event it actually used rather
    than an event without reception (Vol 6, Part B, 2.4.2.27).

    Steps:
        1. Run to event 2 without receptions; receive in event 2.

    Expected:
        - syncConnEventCount is event 2.
    """
    connection = peripheral.connection
    peripheral.run_events(2)
    connection.received_in_event()
    assert connection.event_counter == connection.sync_event_counter == 2


def test_retransmission_keeps_the_prepared_pdu(central):
    """A retransmitted LL_PERIODIC_SYNC_IND keeps its first-transmission fields.

    Purpose: Verify that the fields are fixed at first transmission, so an
    unacknowledged PDU is retransmitted unchanged in a later event; its
    connEventCount still lies within 2^14 events of that event (2.4.2.27).

    Steps:
        1. Send Set Info Transfer; Lower Tester receives the PDU and does not
           acknowledge.
        2. In the next event, Lower Tester receives the retransmission.

    Expected:
        - Both transmissions carry identical octets.
    """
    transmitted(central, periodic_set(central))
    central.set_peer_features(RECIPIENT)
    assert set_info_transfer(central) == ErrorCode.SUCCESS
    central.event_prepare()
    first = central.lt_rx(Opcode.LL_PERIODIC_SYNC_IND)
    central.event_done()
    central.event_prepare()
    assert central.lt_rx(Opcode.LL_PERIODIC_SYNC_IND) == first
    central.tx_ack()


@pytest.mark.parametrize(
    'features,handle,advertising,enable,status',
    [
        (RECIPIENT, None, 1, True, ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR),
        (RECIPIENT, None, 0, False, ErrorCode.COMMAND_DISALLOWED_ERROR),
        (RECIPIENT, 0x0EFF, 0, True, ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR),
        (None, None, 0, True, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR),
        (0, None, 0, True, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR),
    ],
    ids=[
        'unknown-set',
        'periodic-disabled',
        'unknown-connection',
        'no-features',
        'no-recipient',
    ],
)
def test_set_info_transfer_errors(
    central, features, handle, advertising, enable, status
):
    """Set Info Transfer rejects an unusable set, connection or peer.

    Purpose: Verify Unknown Advertising Identifier for a missing set, Command
    Disallowed while its periodic advertising is disabled, Unknown Connection
    Identifier, and Unsupported Remote Feature when the peer has not indicated
    the Recipient feature, including before Feature Exchange (Vol 4, Part E,
    7.8.90; Vol 6, Part B, 4.5.11).

    Steps:
        1. Configure the set and the peer features for the variant.
        2. Send Set Info Transfer.

    Expected:
        - Command Complete carries the variant status; nothing is queued.
    """
    periodic_set(central, enable=enable)
    central.set_peer_features(features)
    assert set_info_transfer(central, handle, advertising) == status
    central.event_prepare()
    central.lt_rx_q_is_empty()
    assert not central.controls.transfers


# ---------------------------------------------------------------------------
# Sender: synchronized train
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('role', ROLES)
def test_sync_transfer_describes_received_train(iut, role):
    """Sync Transfer describes the train from its last received AUX_SYNC_IND.

    Purpose: Verify that a synchronized IUT names the last received event in
    lastPaEventCounter, points SyncInfo at the following event, keeps the
    advertiser's SCA, channel map and PHY, and sends the over-air RPA with a
    random AType (Vol 6, Part B, 2.4.2.27; Vol 4, Part E, 7.8.89).

    Steps:
        1. Run ten events; the sync last received event 7 30 ms ago on LE 2M.
        2. Upper Tester sends Sync Transfer for that sync.
        3. Lower Tester receives LL_PERIODIC_SYNC_IND.

    Expected:
        - lastPaEventCounter 7, paEventCounter 8, anchor 70 ms after this event
          within one unit, connEventCount this event.
        - SyncInfo SCA 2 and channel map from the train; PDU SCA 50 ppm; PHY
          0x02; random AType and the RPA as AdvA; SID 2.
    """
    dut = iut(role)
    dut.run_events(10)
    address = rpa(PEER_IRK)
    synchronized(
        dut,
        last_rx_us=dut.connection.anchor_us - 30_000,
        address=address,
        phy=Phy.LE_2M,
    )
    dut.set_peer_features(RECIPIENT)
    assert sync_transfer(dut).status == ErrorCode.SUCCESS
    dut.event_prepare()
    payload = dut.lt_rx(Opcode.LL_PERIODIC_SYNC_IND)
    fields = decode(payload)
    assert fields['event'] == dut.event_counter
    target = anchor_of(dut, fields)
    assert target <= dut.connection.anchor_us + 70_000 < target + fields['unit']
    assert (fields['last'], fields['counter']) == (7, 8)
    info = SyncInfo.decode(payload[3:21], transfer=True)
    assert info.sca == 2 and info.channel_map == 0x1F00FF
    assert (fields['sca'], fields['phy'], fields['atype'], fields['sid']) == (
        5,
        0x02,
        1,
        2,
    )
    assert fields['address'] == bytes(address)


def test_counter_rule_falls_back_to_the_next_received_event(central):
    """An old reception makes SyncInfo name the event after it.

    Purpose: Verify that when the next train event is more than one event and
    5 s after lastPaEventCounter, the SyncInfo names lastPaEventCounter + 1 in
    the past, with connEventCount in the past (Vol 6, Part B, 2.4.2.27).

    Steps:
        1. Run 300 events; the sync last received event 7 about 6 s ago.
        2. Send Sync Transfer and receive the PDU.

    Expected:
        - paEventCounter is 8 and its anchor is 100 ms after that reception.
        - connEventCount is earlier than this event.
    """
    central.run_events(300)
    last = central.connection.anchor_us - 6_000_000
    synchronized(central, last_rx_us=last)
    central.set_peer_features(RECIPIENT)
    assert sync_transfer(central).status == ErrorCode.SUCCESS
    central.event_prepare()
    fields = decode(central.lt_rx(Opcode.LL_PERIODIC_SYNC_IND))
    assert (fields['last'], fields['counter']) == (7, 8)
    target = anchor_of(central, fields)
    assert target <= last + PERIODIC_US < target + fields['unit']
    assert (central.event_counter - fields['event']) & LL_EVENT_COUNTER_MASK < 1 << 14
    assert fields['event'] != central.event_counter


def test_old_reception_waits_for_a_fresh_one(central):
    """A train last received 2^14 connection events ago waits for a reception.

    Purpose: Verify that when the lastPaEventCounter rule forces a SyncInfo
    event so far back that no connEventCount within 2^14 events of the
    transmitting event can reference it, the sender holds the PDU until the
    train is received again (Vol 6, Part B, 2.4.2.27).

    Steps:
        1. The sync last received event 7 so that event 8 is 2^14 connection
           intervals before this event.
        2. Send Sync Transfer; run a connection event.
        3. The sync receives event 9 30 ms before the next connection event.

    Expected:
        - Command Complete succeeds; no PDU in step 2.
        - The PDU then names lastPaEventCounter 9 and paEventCounter 10.
    """
    anchor = central.connection.anchor_us
    sync = synchronized(
        central, last_rx_us=anchor - (1 << 14) * CONNECTION_US - PERIODIC_US
    )
    central.set_peer_features(RECIPIENT)
    assert sync_transfer(central).status == ErrorCode.SUCCESS
    central.run_events(1)
    sync.last_rx_us, sync.last_counter = central.connection.anchor_us - 30_000, 9
    central.event_prepare()
    fields = decode(central.lt_rx(Opcode.LL_PERIODIC_SYNC_IND))
    assert (fields['last'], fields['counter']) == (9, 10)
    assert fields['event'] == central.event_counter


def test_sync_transfer_unknown_sync(central):
    """Sync Transfer for an unknown Sync_Handle is rejected.

    Purpose: Verify Unknown Advertising Identifier when Sync_Handle names no
    synchronized train, before the connection is checked (Vol 4, Part E,
    7.8.89).

    Steps:
        1. Send Sync Transfer with Sync_Handle 1 and no synchronized train.

    Expected:
        - Command Complete with 0x42 and the connection handle.
    """
    central.set_peer_features(RECIPIENT)
    result = sync_transfer(central, 0x0EFF)
    assert result.status == ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR
    assert result.connection_handle == 0x0EFF


# ---------------------------------------------------------------------------
# Recipient
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('role', ROLES)
def test_default_mode_ignores_transfer(iut, role):
    """The reset transfer mode ignores LL_PERIODIC_SYNC_IND.

    Purpose: Verify that with Mode 0x00, the default after reset, a valid PDU is
    acknowledged without LL_UNKNOWN_RSP, synchronization or a Host event
    (Vol 4, Part E, 7.8.92; Vol 6, Part B, 5.1.13). Runs for both roles.

    Steps:
        1. Lower Tester sends a valid LL_PERIODIC_SYNC_IND.

    Expected:
        - No reply PDU, no transfer context and no Host event.
    """
    dut = iut(role)
    deliver(dut, indication(event=dut.event_counter))
    assert not transfers(dut)
    dut.ut_rx_q_is_empty()


@pytest.mark.parametrize('phy', [0x00, 0x06, 0x07, 0x08, 0x80], ids=hex)
def test_unusable_phy_is_ignored(peripheral, phy):
    """A PHY field without exactly one supported bit is ignored.

    Purpose: Verify that the recipient ignores LL_PERIODIC_SYNC_IND whose PHY
    field has no bit, several bits or an RFU bit: no reply, no synchronization
    and no Host event (Vol 6, Part B, 5.1.13).

    Steps:
        1. Enable receipt with Mode 0x02.
        2. Lower Tester sends the PDU with the variant PHY value.

    Expected:
        - No reply PDU, no transfer context and no Host event.
    """
    assert receive_mode(peripheral) == ErrorCode.SUCCESS
    deliver(peripheral, indication(event=peripheral.event_counter, phy=phy))
    assert not transfers(peripheral)
    peripheral.ut_rx_q_is_empty()


@pytest.mark.parametrize(
    'bit,phy', [(0x01, Phy.LE_1M), (0x02, Phy.LE_2M), (0x04, Phy.LE_CODED)]
)
@pytest.mark.parametrize('role', ROLES)
def test_transfer_starts_synchronizing(iut, role, bit, phy):
    """A valid transfer starts synchronizing at the described anchor.

    Purpose: Verify that with receipt enabled the recipient starts a transfer
    synchronization on the indicated PHY at Tnominal = anchor(connEventCount) +
    offset, keeping SID, address, Service_Data and the mode's reporting, skip
    and timeout (Vol 6, Part B, 5.1.13, 5.1.13.1; Vol 4, Part E, 7.8.91).

    Steps:
        1. Enable receipt with Mode 0x03, Skip 2 and a 1-s timeout.
        2. Lower Tester sends the PDU naming the event three ahead, offset
           3000 us, paEventCounter 9.

    Expected:
        - One transfer context, not yet active and with no Host event.
        - Its anchor is three intervals plus 3000 us after this event's anchor;
          its counter is 9; its PHY, address, SID and origin match.
        - Reports and duplicate filtering are enabled; skip 2; timeout 1 s.
    """
    dut = iut(role)
    assert receive_mode(dut, mode=3, skip=2, timeout=100) == ErrorCode.SUCCESS
    anchor = dut.connection.anchor_us
    counter = dut.event_counter
    deliver(
        dut,
        indication(info=sync_info(100, counter=9), event=counter + 3, phy=bit, sid=4),
    )
    (sync,) = transfers(dut).values()
    assert not dut.link.syncs
    assert sync.next_us == anchor + 3 * CONNECTION_US + 3000
    assert sync.counter == 9 and sync.phy == phy
    assert (sync.address, sync.sid) == (ADVERTISER, 4)
    assert sync.transfer.connection_handle == dut.handle
    assert sync.transfer.service_data == SERVICE_DATA
    assert sync.reports_enabled and sync.filter_duplicates
    assert (sync.skip, sync.timeout_us) == (2, 1_000_000)
    dut.ut_rx_q_is_empty()


def test_past_reference_listens_for_the_next_event(central):
    """A past connEventCount resolves to the train's next event after now.

    Purpose: Verify that a connEventCount in the past is the nearest event with
    that counter, and that the recipient listens first for the train's next
    event after the PDU, advancing paEventCounter accordingly (Vol 6, Part B,
    2.4.2.27, 5.1.13.1).

    Steps:
        1. Enable receipt; run twenty events.
        2. Lower Tester sends the PDU naming the event ten earlier, offset 3000
           us, a 7.5-ms interval and paEventCounter 0xFFFE.

    Expected:
        - The anchor is on that grid, after now and within one interval of it.
        - The counter advanced by the number of elapsed train events, wrapping.
    """
    assert receive_mode(central) == ErrorCode.SUCCESS
    central.run_events(20)
    central.radio.now_us = central.connection.anchor_us + 500
    reference = central.connection.anchor_us - 10 * CONNECTION_US + 3000
    deliver(
        central,
        indication(
            info=sync_info(100, interval=6, counter=0xFFFE),
            event=(central.event_counter - 10) & LL_EVENT_COUNTER_MASK,
        ),
    )
    (sync,) = transfers(central).values()
    events, remainder = divmod(sync.next_us - reference, 7500)
    assert remainder == 0 and events > 0
    assert sync.next_us > central.radio.now_us >= sync.next_us - 7500
    assert sync.counter == (0xFFFE + events) & LL_EVENT_COUNTER_MASK


@pytest.mark.parametrize(
    'event,last,accepted',
    [
        (0x3FFF, 9, True),
        (-0x3FFF, 9, True),
        (0x4000, 9, False),
        (-0x4000, 9, False),
        (0, 10, True),
        (0, 9 + 50, True),
        (0, 9 + 51, False),
        (0, 9 - 51, False),
    ],
    ids=[
        'event-ahead-limit',
        'event-behind-limit',
        'event-ahead',
        'event-behind',
        'last-adjacent',
        'last-5s',
        'last-after-5s',
        'last-before-5s',
    ],
)
def test_counters_beyond_their_limits_are_ignored(central, event, last, accepted):
    """Counters outside the LL_PERIODIC_SYNC_IND limits are ignored.

    Purpose: Verify that the recipient ignores a PDU whose connEventCount is
    2^14 or more events from the receiving event, or whose lastPaEventCounter
    is neither equal nor adjacent to PeriodicEventCounter and more than 5 s
    from it, before taking a sync handle or radio time, and accepts both
    limits (Vol 6, Part B, 2.4.2.27).

    Steps:
        1. Enable receipt; the SyncInfo names paEventCounter 9 with a 100-ms
           interval.
        2. Lower Tester sends the PDU with the variant connEventCount offset
           and lastPaEventCounter.

    Expected:
        - A transfer context exists only for the accepted variants; no Host
          event and no reply PDU.
    """
    assert receive_mode(central) == ErrorCode.SUCCESS
    deliver(
        central,
        indication(
            info=sync_info(100, counter=9),
            event=(central.event_counter + event) & LL_EVENT_COUNTER_MASK,
            last=last & LL_EVENT_COUNTER_MASK,
        ),
    )
    assert len(transfers(central)) == int(accepted)
    central.ut_rx_q_is_empty()


def test_already_synchronized_train_is_ignored(central):
    """A transfer for a synchronized train is ignored.

    Purpose: Verify that the recipient ignores a PDU describing a train it is
    already synchronized with, generating no Host event (Vol 6, Part B,
    5.1.13; Vol 4, Part E, 7.7.65.24 Note).

    Steps:
        1. Enable receipt; the IUT is synchronized with SID 2 of the advertiser.
        2. Lower Tester sends the PDU for that train, then for SID 3.

    Expected:
        - Only the second starts a transfer context; no Host event.
    """
    assert receive_mode(central) == ErrorCode.SUCCESS
    synchronized(central, last_rx_us=0)
    deliver(central, indication(event=central.event_counter, sid=2))
    assert not transfers(central)
    deliver(central, indication(event=central.event_counter, sid=3))
    assert len(transfers(central)) == 1
    central.ut_rx_q_is_empty()


def test_transfer_in_progress_is_not_restarted(central):
    """A second transfer for a train being synchronized is ignored.

    Purpose: Verify that a PDU for a train whose transfer synchronization is
    in progress neither restarts nor duplicates it (Vol 6, Part B, 5.1.13).

    Steps:
        1. Enable receipt; Lower Tester sends the same PDU twice.

    Expected:
        - One transfer context, unchanged by the second PDU.
    """
    assert receive_mode(central) == ErrorCode.SUCCESS
    deliver(central, indication(event=central.event_counter))
    (first,) = transfers(central).values()
    deliver(central, indication(event=central.event_counter))
    assert list(transfers(central).values()) == [first]


def test_resolvable_advertiser_reports_identity(central):
    """A resolvable AdvA is resolved to the identity in the resolving list.

    Purpose: Verify that the recipient resolves an RPA AdvA with the resolving
    list, so the sync is identified by the random identity while retaining the
    over-air RPA (Vol 6, Part B, 2.4.2.27; Vol 4, Part E, 7.7.65.24).

    Steps:
        1. Add the advertiser's identity and IRK; enable address resolution.
        2. Enable receipt; Lower Tester sends the PDU with an RPA from that IRK.

    Expected:
        - The sync address is the random identity (type 0x03); the over-air
          address is the RPA.
    """
    privacy = central.link.privacy
    assert privacy.add(IDENTITY, PEER_IRK, bytes(16)) == ErrorCode.SUCCESS
    assert privacy.set_enabled(True) == ErrorCode.SUCCESS
    assert receive_mode(central) == ErrorCode.SUCCESS
    address = rpa(PEER_IRK)
    deliver(
        central, indication(event=central.event_counter, random=True, address=address)
    )
    (sync,) = transfers(central).values()
    assert sync.address == Address(bytes(IDENTITY), AddressType.RANDOM_IDENTITY)
    assert sync.address.address_type == AddressType.RANDOM_IDENTITY
    assert sync.advertiser_address == address


@pytest.mark.parametrize('role', ROLES)
def test_wrong_length_draws_unknown_rsp(iut, role):
    """A truncated LL_PERIODIC_SYNC_IND draws LL_UNKNOWN_RSP.

    Purpose: Verify that a PDU without the 34-octet CtrData is not interpreted
    and is answered with LL_UNKNOWN_RSP (Vol 6, Part B, 2.4.2). Runs for both
    roles.

    Steps:
        1. Enable receipt; Lower Tester sends the PDU without its last octet.

    Expected:
        - Lower Tester receives LL_UNKNOWN_RSP for opcode 0x1C; no context.
    """
    dut = iut(role)
    assert receive_mode(dut) == ErrorCode.SUCCESS
    dut.event_prepare()
    dut.lt_tx(indication(event=dut.event_counter)[:-1])
    dut.event_done()
    dut.event_prepare()
    dut.lt_rx(Opcode.LL_UNKNOWN_RSP, unknown_rsp(Opcode.LL_PERIODIC_SYNC_IND))
    dut.tx_ack()
    assert not transfers(dut)


def test_without_recipient_feature_draws_unknown_rsp(central):
    """Without the Recipient feature, LL_PERIODIC_SYNC_IND is unknown.

    Purpose: Verify that a Link Layer not supporting the Recipient feature
    answers the 35-octet PDU with LL_UNKNOWN_RSP rather than dropping it
    (Vol 6, Part B, 2.4.2, 4.6.24).

    Steps:
        1. Remove the Recipient feature; Lower Tester sends the PDU.

    Expected:
        - Lower Tester receives LL_UNKNOWN_RSP for opcode 0x1C.
    """
    central.link.features &= ~RECIPIENT
    central.event_prepare()
    central.lt_tx(indication(event=central.event_counter))
    central.event_done()
    central.event_prepare()
    central.lt_rx(Opcode.LL_UNKNOWN_RSP, unknown_rsp(Opcode.LL_PERIODIC_SYNC_IND))


def test_no_free_sync_reports_capacity_failure(central):
    """A transfer with no free sync handle reports Memory Capacity Exceeded.

    Purpose: Verify that when every sync handle is in use, the recipient notifies
    the Host of the failed synchronization with status 0x07 and Sync_Handle
    zero (Vol 6, Part B, 5.1.13; Vol 4, Part E, 7.7.65.24).

    Steps:
        1. Fill every sync handle with an unrelated synchronized train.
        2. Enable receipt; Lower Tester sends the PDU.

    Expected:
        - Upper Tester receives the transfer event with 0x07, Sync_Handle 0 and
          the PDU's Service_Data, SID and address; no context remains.
    """
    for handle in range(1, central.link.config.max_periodic_syncs + 1):
        central.link.syncs[handle] = PeriodicSync(
            IDENTITY, handle % 16, 10**6, 10**12, established=True, handle=handle
        )
    assert receive_mode(central) == ErrorCode.SUCCESS
    deliver(central, indication(event=central.event_counter))
    central.ut_rx(
        RECEIVED,
        status=ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR,
        connection_handle=central.handle,
        service_data=SERVICE_DATA,
        sync_handle=0,
        advertising_sid=3,
        advertiser_address=ADVERTISER,
        periodic_advertising_interval=PERIODIC_INTERVAL,
    )
    assert not transfers(central)


# ---------------------------------------------------------------------------
# Transfer mode commands
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'mode,skip,timeout,cte,status',
    [
        (4, 0, 100, 0, ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR),
        (2, 0x01F4, 100, 0, ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (2, 0, 0x0009, 0, ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (2, 0, 0x4001, 0, ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (2, 0, 100, 0x17, ErrorCode.COMMAND_DISALLOWED_ERROR),
        (2, 0, 100, 0x1F, ErrorCode.COMMAND_DISALLOWED_ERROR),
        (2, 0, 100, 0x08, ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR),
        (2, 0, 100, 0x01, ErrorCode.SUCCESS),
        (2, 0, 100, 0x16, ErrorCode.SUCCESS),
        (3, 0x01F3, 0x4000, 0, ErrorCode.SUCCESS),
    ],
    ids=[
        'mode-rfu',
        'skip',
        'timeout-low',
        'timeout-high',
        'cte-all',
        'cte-all-and-reserved',
        'cte-reserved',
        'cte-aoa',
        'cte-aod-and-none',
        'mode3-limits',
    ],
)
def test_transfer_parameter_checks(central, mode, skip, timeout, cte, status):
    """Transfer mode parameters are validated for both commands.

    Purpose: Verify the per-connection and default commands reject an RFU Mode,
    out-of-range Skip or Sync_Timeout, all defined CTE_Type bits (Command
    Disallowed) and a reserved CTE_Type bit, and accept defined CTE
    restrictions and Mode 0x03 with the ADI feature at the range limits
    (Vol 4, Part E, 7.8.91, 7.8.92).

    Steps:
        1. Send the per-connection command with the variant parameters.
        2. Send the default command with the same parameters.

    Expected:
        - Both return the variant status; the per-connection one also returns
          the connection handle.
    """
    result = transfer_parameters(central, mode, skip, timeout, cte)
    assert (result.status, result.connection_handle) == (status, central.handle)
    assert default_parameters(central, mode, skip, timeout, cte).status == status


def test_transfer_parameters_unknown_connection(central):
    """The per-connection mode needs a current connection.

    Purpose: Verify Unknown Connection Identifier for a handle without a
    connection (Vol 4, Part E, 7.8.91).

    Steps:
        1. Send the per-connection command for handle 0x0EFF.

    Expected:
        - Command Complete with 0x02 and handle 0x0EFF.
    """
    result = transfer_parameters(central, handle=0x0EFF)
    assert result.status == ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
    assert result.connection_handle == 0x0EFF


def test_default_mode_applies_to_later_connections(iut):
    """The default mode applies to connections created after the command.

    Purpose: Verify that Set Default Periodic Advertising Sync Transfer
    Parameters leaves existing connections unchanged and seeds later ones
    (Vol 4, Part E, 7.8.92).

    Steps:
        1. Create a connection; set the default mode to 0x02.
        2. Create a second connection; Lower Tester sends a valid PDU on each.

    Expected:
        - Only the second connection starts a transfer synchronization.
    """
    first = iut(CENTRAL)
    assert default_parameters(first).status == ErrorCode.SUCCESS
    second = iut(PERIPHERAL)
    deliver(first, indication(event=first.event_counter, sid=1))
    assert not transfers(first)
    deliver(second, indication(event=second.event_counter, sid=2))
    (sync,) = transfers(second).values()
    assert sync.transfer.connection_handle == second.handle
