# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Periodic advertising and synchronization (Core Vol 6, Part B, 4.4.2.12,
4.4.5; Vol 4, Part E, 7.8.61-7.8.71, 7.8.88).

Matrix: advertiser {train timing, SyncInfo, chain, CSA #2, data update, enable
rules}; synchronizer {acquire via SyncInfo, establish, report, skip, loss,
6-event failure, cancel, terminate, list, channel-map ACAD, receive enable}.

Scenarios run on the ``air/harness.py`` harness and reuse ``configure``,
``enable`` and ``split`` from ``extended_advertising_test.py``. The advertiser
half drives the IUT as a periodic advertiser and reads AUX_SYNC_IND trains on the
periodic access address. The synchronizer half uses the ``Train`` helper as a
Lower Tester periodic advertiser: it announces via ADV_EXT_IND + AUX_ADV_IND
SyncInfo, then schedules AUX_SYNC_IND events on CSA #2 channels. Extended
advertising itself is in ``extended_advertising_test.py``; ownership races around
syncs are in ``lifetime_test.py``.
"""

import pytest

from bscontroller.hci import HCI_LE_SCAN_INTERVAL_UNIT_US
from bscontroller.linklayer import (
    LL_DATA_CHANNEL_MAP_ALL,
    LL_EVENT_COUNTER_MASK,
    LL_PERIODIC_INTERVAL_UNIT_US,
    LL_PERIODIC_SYNC_ESTABLISHMENT_EVENTS,
)
from bscontroller.linklayer.advertising import AuxPtr, ExtendedPdu, SyncInfo
from bscontroller.linklayer.channel_selection import channel_selection_2
from bscontroller.linklayer.pdu import PDU_ADV_ADI_DID_BITS, PDU_ADV_ADI_DID_MASK
from bscontroller.radio.base import Phy
from tests.support import AD_CHANNEL_MAP_UPDATE_INDICATION, advertising_data
from tests.unit.linklayer.air.extended_advertising_test import (
    configure as configure_extended,
)
from tests.unit.linklayer.air.extended_advertising_test import (
    enable as enable_extended,
)
from tests.unit.linklayer.air.extended_advertising_test import split
from tests.unit.linklayer.air.harness import OTHER, PEER, ErrorCode, hci

Operation = hci.HCI_LE_Set_Extended_Advertising_Data_Command.Operation
Options = hci.HCI_LE_Periodic_Advertising_Create_Sync_Command.Options
PERIODIC_INTERVAL = 80  # 100 ms
PERIODIC_US = PERIODIC_INTERVAL * LL_PERIODIC_INTERVAL_UNIT_US
SYNC_AA = 0x8E89BED7 ^ 0x13579BDF
SYNC_CRC = 0x445566
ESTABLISHED = hci.HCI_LE_Periodic_Advertising_Sync_Established_Event
PERIODIC_REPORT = hci.HCI_LE_Periodic_Advertising_Report_Event
LOST = hci.HCI_LE_Periodic_Advertising_Sync_Lost_Event
WINDOW = 160
WINDOW_US = WINDOW * HCI_LE_SCAN_INTERVAL_UNIT_US
AUX_CHANNEL = 12


# ---------------------------------------------------------------------------
# Periodic advertiser
# ---------------------------------------------------------------------------
def periodic(iut, data=b'', *, enable=True):
    configure_extended(iut)
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(
            0, PERIODIC_INTERVAL, PERIODIC_INTERVAL, 0
        )
    )
    if data:
        iut.ut(
            hci.HCI_LE_Set_Periodic_Advertising_Data_Command(
                0, Operation.COMPLETE_DATA, data
            )
        )
    if enable:
        iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))


def trains(packets, item):
    return [packet for packet in packets if packet.access_address == item.periodic_aa]


def test_periodic_train_interval_and_csa2_channels(iut):
    """The periodic train runs at a fixed interval on CSA #2 channels.

    Purpose: Verify that an enabled periodic advertiser transmits AUX_SYNC_IND at
    the configured interval, hopping its data channels by CSA #2 keyed on the
    periodic access address, with each PDU carrying the periodic data and no
    address (Vol 6, Part B, 4.4.2.12, 4.5.8).

    Steps:
        1. Configure a periodic advertiser with data and enable it.
        2. Run five periodic intervals and take the periodic train.

    Expected:
        - At least four train PDUs were sent.
        - Consecutive PDUs are spaced by exactly the periodic interval.
        - Each PDU's channel is the CSA #2 channel for its event counter, carries
          the periodic data and no address.
    """
    periodic(iut, b'periodic')
    item = iut.link.advertisers[0]
    iut.run(5 * PERIODIC_US)
    train = trains(iut.lt_rx_all(), item)
    assert len(train) >= 4
    starts = [packet.start_us for packet in train]
    assert all(b - a == PERIODIC_US for a, b in zip(starts, starts[1:]))
    for counter, packet in enumerate(train):
        assert packet.channel == channel_selection_2(
            counter, item.periodic_aa, LL_DATA_CHANNEL_MAP_ALL
        )
        assert packet.ext.data == b'periodic' and packet.ext.address is None


def test_periodic_chain_for_long_data(iut):
    """Long periodic data is sent as an AUX_CHAIN_IND sequence.

    Purpose: Verify that periodic data too large for one PDU is transmitted as a
    chained AUX_SYNC_IND / AUX_CHAIN_IND sequence whose payloads reassemble to the
    input, with only the last PDU lacking an AuxPtr (Vol 6, Part B, 2.3.4,
    4.4.2.12).

    Steps:
        1. Configure extended parameters and periodic parameters with interval
           400.
        2. Load 500 octets of periodic data in two fragments and enable the train.
        3. Run one periodic interval and take the train PDUs.

    Expected:
        - The joined train payloads equal the full data.
        - The first PDU carries an AuxPtr and the last does not.
    """
    data = bytes(range(250)) * 2
    configure_extended(iut)
    iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 400, 400, 0))
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Data_Command(
            0, Operation.FIRST_FRAGMENT, data[:250]
        )
    )
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Data_Command(
            0, Operation.LAST_FRAGMENT, data[250:]
        )
    )
    iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    item = iut.link.advertisers[0]
    iut.run(400 * LL_PERIODIC_INTERVAL_UNIT_US + 10_000)
    train = trains(iut.lt_rx_all(), item)
    assert b''.join(packet.ext.data for packet in train) == data
    assert train[0].ext.aux_ptr is not None and train[-1].ext.aux_ptr is None


def test_aux_adv_ind_carries_syncinfo_to_the_train(iut):
    """The AUX_ADV_IND SyncInfo points correctly to the periodic train.

    Purpose: Verify that when extended advertising is enabled alongside a periodic
    train the AUX_ADV_IND carries a SyncInfo whose access address, CRC init,
    interval, offset and event counter line up with the next train event (Vol 6,
    Part B, 2.3.4.6, 4.4.2.12).

    Steps:
        1. Configure a periodic advertiser with data and enable it.
        2. Enable extended advertising and run three periodic intervals.
        3. Split the auxiliary PDUs from the periodic train and read the first
           AUX_ADV_IND's SyncInfo.

    Expected:
        - The SyncInfo access address and CRC init match the train's.
        - Its interval matches the periodic interval.
        - The SyncInfo offset places its anchor at or before the next train PDU,
          within one SyncInfo offset unit.
        - Its event counter matches that train PDU's index.
    """
    periodic(iut, b'p')
    enable_extended(iut)
    item = iut.link.advertisers[0]
    iut.run(3 * PERIODIC_US)
    packets = iut.lt_rx_all()
    _, auxiliary = split(
        [packet for packet in packets if packet.access_address is None]
    )
    train = trains(packets, item)
    aux = auxiliary[0]
    info = aux.ext.sync_info
    assert (
        info.access_address == item.periodic_aa and info.crc_init == item.periodic_crc
    )
    assert info.interval == PERIODIC_INTERVAL
    anchor = aux.start_us + info.offset_us
    following = next(packet for packet in train if packet.start_us >= aux.start_us)
    assert anchor <= following.start_us < anchor + info.unit_us
    assert (info.event_counter - train.index(following)) & LL_EVENT_COUNTER_MASK == 0


def test_periodic_data_update_while_enabled(iut):
    """Updating periodic data while enabled takes effect on a later event.

    Purpose: Verify that Set Periodic Advertising Data accepted during an active
    train replaces the data carried in subsequent AUX_SYNC_IND events (Vol 4,
    Part E, 7.8.62).

    Steps:
        1. Configure a periodic advertiser with data "old" and enable it.
        2. Run one interval, then set periodic data to "new".
        3. Run two more intervals and take the train.

    Expected:
        - The last train PDU carries "new".
    """
    periodic(iut, b'old')
    item = iut.link.advertisers[0]
    iut.run(PERIODIC_US + 1)
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Data_Command(
            0, Operation.COMPLETE_DATA, b'new'
        )
    )
    iut.run(2 * PERIODIC_US)
    assert trains(iut.lt_rx_all(), item)[-1].ext.data == b'new'


def test_periodic_disable_stops_train(iut):
    """Disabling periodic advertising stops the train.

    Purpose: Verify that Set Periodic Advertising Enable with disable halts the
    AUX_SYNC_IND train (Vol 4, Part E, 7.8.63).

    Steps:
        1. Configure and enable a periodic advertiser.
        2. Disable periodic advertising.
        3. Run three intervals and take the train.

    Expected:
        - No train PDUs are transmitted.
    """
    periodic(iut)
    item = iut.link.advertisers[0]
    iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0, 0))
    iut.run(3 * PERIODIC_US)
    assert not trains(iut.lt_rx_all(), item)


@pytest.mark.parametrize(
    'properties',
    [
        hci.HCI_LE_Set_Extended_Advertising_Parameters_Command.AdvertisingProperties.CONNECTABLE_ADVERTISING,
        hci.HCI_LE_Set_Extended_Advertising_Parameters_Command.AdvertisingProperties.SCANNABLE_ADVERTISING,
        hci.HCI_LE_Set_Extended_Advertising_Parameters_Command.AdvertisingProperties.ANONYMOUS_ADVERTISING,
    ],
)
def test_periodic_requires_nonconnectable_nonscannable_identified_set(iut, properties):
    """Periodic parameters require a non-connectable, non-scannable, identified set.

    Purpose: Verify that Set Periodic Advertising Parameters is rejected when the
    extended set is connectable, scannable, or anonymous, since periodic
    advertising requires a non-connectable, non-scannable, non-anonymous set
    (Vol 4, Part E, 7.8.61).

    Steps:
        1. Configure an extended set with the parametrized property.
        2. Send Set Periodic Advertising Parameters.

    Expected:
        - The command fails with INVALID_COMMAND_PARAMETERS_ERROR.

    Parameters range over connectable, scannable and anonymous properties.
    """
    configure_extended(iut, properties)
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 80, 80, 0),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


def test_periodic_commands_require_existing_set(iut):
    """Periodic commands reject an unknown advertising set.

    Purpose: Verify that Set Periodic Advertising Parameters, Data and Enable each
    reject a handle for which no advertising set exists (Vol 4, Part E,
    7.8.61-7.8.63).

    Steps:
        1. Send Set Periodic Advertising Parameters for handle 7.
        2. Send Set Periodic Advertising Data for handle 7.
        3. Send Set Periodic Advertising Enable for handle 7.

    Expected:
        - Each command fails with UNKNOWN_ADVERTISING_IDENTIFIER_ERROR.
    """
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(7, 80, 80, 0),
        ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR,
    )
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Data_Command(
            7, Operation.COMPLETE_DATA, b''
        ),
        ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR,
    )
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 7),
        ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR,
    )


def test_periodic_enable_without_parameters_is_disallowed(iut):
    """Enabling periodic advertising without parameters is disallowed.

    Purpose: Verify that Set Periodic Advertising Enable is rejected for a set
    that has no periodic parameters configured (Vol 4, Part E, 7.8.63).

    Steps:
        1. Configure an extended set with no periodic parameters.
        2. Send Set Periodic Advertising Enable.

    Expected:
        - The command fails with COMMAND_DISALLOWED_ERROR.
    """
    configure_extended(iut)
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )


def test_periodic_parameters_disallowed_while_enabled(iut):
    """Periodic parameters and set removal are disallowed while the train runs.

    Purpose: Verify that while a periodic train is enabled both Set Periodic
    Advertising Parameters and Remove Advertising Set are rejected (Vol 4,
    Part E, 7.8.61, 7.8.59).

    Steps:
        1. Configure and enable a periodic advertiser.
        2. Send Set Periodic Advertising Parameters.
        3. Send Remove Advertising Set for the same handle.

    Expected:
        - Both commands fail with COMMAND_DISALLOWED_ERROR.
    """
    periodic(iut)
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 80, 80, 0),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )
    # A periodic set cannot be removed while its train is running.
    iut.ut(
        hci.HCI_LE_Remove_Advertising_Set_Command(0), ErrorCode.COMMAND_DISALLOWED_ERROR
    )


@pytest.mark.parametrize('interval', [(5, 5), (80, 40)])
def test_periodic_interval_validation(iut, interval):
    """Set Periodic Advertising Parameters rejects invalid interval ranges.

    Purpose: Verify that Set Periodic Advertising Parameters rejects a below
    minimum interval and an inverted interval range (Vol 4, Part E, 7.8.61).

    Steps:
        1. Configure an extended set.
        2. Send Set Periodic Advertising Parameters with the parametrized
           interval range.

    Expected:
        - The command fails with INVALID_COMMAND_PARAMETERS_ERROR.

    Parameters range over a below-minimum interval and an inverted range.
    """
    configure_extended(iut)
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, *interval, 0),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


def test_periodic_data_too_long_for_interval(iut):
    """Periodic data exceeding the train budget is rejected and discarded.

    Purpose: Verify that each periodic data fragment is checked against the
    interval's airtime budget, so a sequence that overflows is rejected with
    Packet Too Long, all buffered data is discarded, and the aborted sequence
    cannot be resumed (Vol 4, Part E, 7.8.62).

    Steps:
        1. Configure an extended set and periodic parameters with a 7.5 ms
           interval.
        2. Load three 250-octet fragments (first plus two intermediate).
        3. Load a fourth 250-octet last fragment, overflowing the budget.
        4. Try to resume with a one-octet last fragment.

    Expected:
        - The overflowing fragment fails with PACKET_TOO_LONG_ERROR.
        - The buffered periodic data is empty.
        - The resume attempt fails with COMMAND_DISALLOWED_ERROR.
    """
    configure_extended(iut)
    # 7.5 ms: 750 octets (6.9 ms of chain + T_MAFS) fit, 1000 do not.
    iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 6, 6, 0))
    command = hci.HCI_LE_Set_Periodic_Advertising_Data_Command
    iut.ut(command(0, Operation.FIRST_FRAGMENT, bytes(250)))
    iut.ut(command(0, Operation.INTERMEDIATE_FRAGMENT, bytes(250)))
    iut.ut(command(0, Operation.INTERMEDIATE_FRAGMENT, bytes(250)))
    iut.ut(
        command(0, Operation.LAST_FRAGMENT, bytes(250)),
        ErrorCode.PACKET_TOO_LONG_ERROR,
    )
    # All data is discarded, and the sequence cannot be resumed.
    assert bytes(iut.link.advertisers[0].periodic_data) == b''
    iut.ut(
        command(0, Operation.LAST_FRAGMENT, bytes(1)),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )


def test_periodic_adi_in_sync_and_chain(iut):
    """With Enable bit 1 every train PDU carries the periodic ADI.

    Purpose: Verify that a train enabled with Include ADI puts an ADI field with
    the set's SID and the periodic DID in AUX_SYNC_IND and in every
    AUX_CHAIN_IND, and that the data still reassembles (Vol 6, Part B, 2.3.1.7,
    2.3.1.8, 4.6.34; Vol 4, Part E, 7.8.63).

    Steps:
        1. Configure a 500-ms periodic set with 500 octets of data, which needs
           AUX_CHAIN_IND PDUs, and enable it with Enable 0x03.
        2. Run one periodic interval and take the train PDUs.

    Expected:
        - The train has more than one PDU and reassembles to the data.
        - Every PDU's ADI has the set's SID and the periodic DID.
    """
    data = bytes(range(250)) * 2
    configure_extended(iut)
    iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 400, 400, 0))
    command = hci.HCI_LE_Set_Periodic_Advertising_Data_Command
    iut.ut(command(0, Operation.FIRST_FRAGMENT, data[:250]))
    iut.ut(command(0, Operation.LAST_FRAGMENT, data[250:]))
    iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0b11, 0))
    item = iut.link.advertisers[0]
    iut.run(400 * LL_PERIODIC_INTERVAL_UNIT_US + 10_000)
    train = trains(iut.lt_rx_all(), item)
    assert len(train) > 1
    assert b''.join(packet.ext.data for packet in train) == data
    adi = item.sid << PDU_ADV_ADI_DID_BITS | item.periodic_did
    assert all(packet.ext.adi == adi for packet in train)


def test_periodic_did_follows_periodic_data_only(iut):
    """The AUX_SYNC_IND DID follows the periodic data, not the advertising set.

    Purpose: Verify on air that Operation 0x04 and new periodic data each give
    AUX_SYNC_IND a new DID with the same SID, while the set's AUX_ADV_IND keeps
    its ADI (Vol 6, Part B, 4.4.2.11; Vol 4, Part E, 7.8.62).

    Steps:
        1. Configure a periodic set with data, enable it with Enable 0x03 and
           enable extended advertising; run one periodic interval.
        2. Send Operation 0x04 and run one interval.
        3. Store the same data again with Operation 0x03 and run one interval.

    Expected:
        - Each step's last AUX_SYNC_IND carries the same data, the set's SID and
          a DID that differs from the previous step's.
        - Every AUX_ADV_IND carries the same ADI.
    """
    periodic(iut, b'periodic', enable=False)
    iut.ut(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0b11, 0))
    enable_extended(iut)
    item = iut.link.advertisers[0]
    command = hci.HCI_LE_Set_Periodic_Advertising_Data_Command
    dids, auxiliary_adis = [], set()
    for update in (
        None,
        command(0, Operation.UNCHANGED_DATA, b''),
        command(0, Operation.COMPLETE_DATA, b'periodic'),
    ):
        if update is not None:
            iut.ut(update)
        iut.run(PERIODIC_US)
        packets = iut.lt_rx_all()
        last = trains(packets, item)[-1]
        assert last.ext.data == b'periodic'
        assert last.ext.adi >> PDU_ADV_ADI_DID_BITS == item.sid
        dids.append(last.ext.adi & PDU_ADV_ADI_DID_MASK)
        _, auxiliary = split(
            [packet for packet in packets if packet.access_address is None]
        )
        auxiliary_adis |= {packet.ext.adi for packet in auxiliary}
    assert dids[0] != dids[1] and dids[1] != dids[2]
    assert len(auxiliary_adis) == 1


# ---------------------------------------------------------------------------
# Synchronizer: Lower Tester advertises a periodic train
# ---------------------------------------------------------------------------
class Train:
    """A Lower Tester periodic advertiser: SyncInfo announcements and AUX_SYNC_IND."""

    def __init__(
        self,
        iut,
        *,
        address=PEER,
        sid=3,
        interval=PERIODIC_INTERVAL,
        counter=0,
        phy=Phy.LE_1M,
    ):
        self.iut, self.address, self.sid = iut, address, sid
        self.interval, self.counter, self.phy = interval, counter, phy
        self.channel_map = LL_DATA_CHANNEL_MAP_ALL
        self.anchor_us = None

    @property
    def interval_us(self):
        return self.interval * LL_PERIODIC_INTERVAL_UNIT_US

    def announce(self, offset_us=30_000):
        """ADV_EXT_IND + AUX_ADV_IND with SyncInfo; the train starts offset later."""
        primary = ExtendedPdu(
            adi=self.sid << 12 | 1, aux_ptr=AuxPtr(AUX_CHANNEL, 3000, self.phy)
        )
        info = SyncInfo(
            offset_us, self.interval, self.channel_map, SYNC_AA, SYNC_CRC, self.counter
        )
        auxiliary = ExtendedPdu(
            address=bytes(self.address),
            random_address=self.address.is_random,
            adi=self.sid << 12 | 1,
            sync_info=info,
        )
        self.iut.lt_tx(primary, phy=Phy.LE_1M)
        self.iut.lt_tx(auxiliary, phy=self.phy, channel=AUX_CHANNEL, after=3000)
        self._offset_us = offset_us

    def first_anchor(self):
        """Anchor of the event SyncInfo described, once the announcement arrived."""
        aux = self.iut.radio.delivered[-1]
        self.anchor_us = aux.start_us + self._offset_us
        return self.anchor_us

    def send(self, data=b'', *, skip=False, **fields):
        """Schedule this train event (or let it pass silently) and step the anchor."""
        if not skip:
            self.iut.lt_tx(
                ExtendedPdu(data=data, **fields),
                phy=self.phy,
                at=self.anchor_us,
                access_address=SYNC_AA,
                channel=channel_selection_2(self.counter, SYNC_AA, self.channel_map),
            )
        self.anchor_us += self.interval_us
        self.counter = (self.counter + 1) & LL_EVENT_COUNTER_MASK

    def run_through(self):
        self.iut.run_until(self.anchor_us - self.interval_us + 2000)


def create_sync(
    iut,
    *,
    address=PEER,
    sid=3,
    skip=0,
    timeout=100,
    options=0,
    status=ErrorCode.SUCCESS,
):
    iut.ut(
        hci.HCI_LE_Periodic_Advertising_Create_Sync_Command(
            options, sid, address.address_type, address, skip, timeout, 0
        ),
        status,
    )


def drain_periodic(iut):
    found = [event for event in iut.events if isinstance(event, PERIODIC_REPORT)]
    for event in found:
        iut.events.remove(event)
    return [event.data for event in found]


def acquire(iut, train, **sync):
    """Deliver SyncInfo and stop before the first train event it announced."""
    create_sync(iut, **sync)
    train.announce()
    iut.run(10_000)
    assert iut.link.sync is not None and iut.link.sync.info is not None
    return train.first_anchor()


def establish(iut, train, data=b'first', **sync):
    acquire(iut, train, **sync)
    train.send(data)
    train.run_through()
    return iut.ut_rx(
        ESTABLISHED,
        status=0,
        advertiser_address=train.address,
        advertising_sid=train.sid,
    )


def test_sync_established_and_reports(iut):
    """A sync establishes and reports each periodic event.

    Purpose: Verify that after acquiring SyncInfo and receiving the first train
    event the synchronizer reports Sync Established with the train's interval and
    PHY, and reports each subsequent AUX_SYNC_IND payload (Vol 6, Part B, 4.4.5;
    Vol 4, Part E, 7.7.65.14-7.7.65.15).

    Steps:
        1. Establish a sync against the Lower Tester train with first data.
        2. For "second" and "third", schedule a train event and run through it.

    Expected:
        - Sync Established reports the periodic interval and the LE 1M PHY.
        - A Periodic Advertising Report is produced for "first", then "second"
          and "third" in order, each complete.
        - Nothing else reaches the Host.
    """
    train = Train(iut)
    event = establish(iut, train)
    assert event.periodic_advertising_interval == PERIODIC_INTERVAL
    assert event.advertiser_phy == Phy.LE_1M
    iut.ut_rx(
        PERIODIC_REPORT, sync_handle=event.sync_handle, data=b'first', data_status=0
    )
    for data in (b'second', b'third'):
        train.send(data)
        train.run_through()
        iut.ut_rx(PERIODIC_REPORT, data=data)
    iut.ut_rx_q_is_empty()


def test_sync_listens_on_csa2_channel_with_train_access_address(iut):
    """The synchronizer listens on the train's CSA #2 channels.

    Purpose: Verify that after establishment the synchronizer opens its periodic
    listen windows on the CSA #2 channels derived from the train's event counter
    and access address, including across the event-counter wrap (Vol 6, Part B,
    4.4.5, 4.5.8).

    Steps:
        1. Establish a sync whose train counter starts near the wrap boundary.
        2. Drain the establishment reports.
        3. Schedule and run through three more train events.
        4. Collect the periodic listen windows (excluding primary and auxiliary
           channels).

    Expected:
        - The first four periodic listens use the CSA #2 channels for the
          consecutive wrapping event counters and the train access address.
    """
    train = Train(iut, counter=LL_EVENT_COUNTER_MASK - 1)
    establish(iut, train)
    drain_periodic(iut)
    for _ in range(3):
        train.send(b'x')
        train.run_through()
    listens = [
        entry
        for entry in iut.radio.listens
        if entry[2] not in (37, 38, 39, AUX_CHANNEL)
    ]
    expected = [
        channel_selection_2(
            (LL_EVENT_COUNTER_MASK - 1 + index) & LL_EVENT_COUNTER_MASK,
            SYNC_AA,
            LL_DATA_CHANNEL_MAP_ALL,
        )
        for index in range(4)
    ]
    assert [entry[2] for entry in listens[:4]] == expected


def test_sync_reports_chain_in_fragments(iut):
    """A chained periodic event is reported in ordered fragments.

    Purpose: Verify that the synchronizer follows an AUX_CHAIN_IND off a periodic
    event and reports the head as incomplete then the tail as complete (Vol 6,
    Part B, 4.4.5; Vol 4, Part E, 7.7.65.15).

    Steps:
        1. Establish a sync with empty first data and clear events.
        2. Schedule a periodic event "head" that points to a chain.
        3. Script the chain continuation "tail" on the auxiliary channel.
        4. Run through the event.

    Expected:
        - A Periodic Advertising Report carries "head" with data status 1
          (incomplete, more to come).
        - A following report carries "tail" with data status 0 (complete).
    """
    train = Train(iut)
    establish(iut, train, data=b'')
    iut.events.clear()
    train.send(b'head', aux_ptr=AuxPtr(AUX_CHANNEL, 3000))
    iut.lt_tx(
        ExtendedPdu(data=b'tail'),
        channel=AUX_CHANNEL,
        after=3000,
        access_address=SYNC_AA,
    )
    train.run_through()
    iut.ut_rx(PERIODIC_REPORT, data=b'head', data_status=1)
    iut.ut_rx(PERIODIC_REPORT, data=b'tail', data_status=0)


@pytest.mark.parametrize('skip', [0, 2])
def test_skip_is_applied_only_after_establishment(iut, skip):
    """Periodic skip reduces listens only after the sync is established.

    Purpose: Verify that the requested periodic skip takes effect only after
    establishment, so the synchronizer then listens once per (skip + 1) events
    (Vol 6, Part B, 4.4.5; Vol 4, Part E, 7.8.67).

    Steps:
        1. Establish a sync with the parametrized skip.
        2. Drain establishment reports and record the listen count.
        3. Schedule 3*(skip + 1) train events and run through them.
        4. Count the periodic listens opened since establishment.

    Expected:
        - Exactly three periodic listens occur over the 3*(skip + 1) events.

    Parameters range over skip 0 and skip 2.
    """
    train = Train(iut)
    establish(iut, train, skip=skip)
    drain_periodic(iut)
    before = len(iut.radio.listens)
    for _ in range(3 * (skip + 1)):
        train.send(b'x')
    train.run_through()
    sync_listens = [
        entry for entry in iut.radio.listens[before:] if entry[2] != AUX_CHANNEL
    ]
    assert len(sync_listens) == 3


def test_six_missed_events_fail_establishment(iut):
    """Six missed events fail sync establishment.

    Purpose: Verify that if the synchronizer misses the first
    LL_PERIODIC_SYNC_ESTABLISHMENT_EVENTS consecutive train events it fails
    establishment, reporting Sync Established with Connection Failed To Be
    Established and no handle, and stops synchronizing (Vol 6, Part B, 4.4.5).

    Steps:
        1. Acquire SyncInfo for the Lower Tester train.
        2. Let the establishment-event count of train events pass with no PDU.
        3. Run through and one more interval.

    Expected:
        - Sync Established reports
          CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR with sync handle 0.
        - Nothing else reaches the Host.
        - The synchronizer is no longer synchronizing.
    """
    train = Train(iut)
    acquire(iut, train)
    for _ in range(LL_PERIODIC_SYNC_ESTABLISHMENT_EVENTS):
        train.send(skip=True)
    train.run_through()
    iut.run(train.interval_us)
    iut.ut_rx(
        ESTABLISHED,
        status=ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR,
        sync_handle=0,
    )
    iut.ut_rx_q_is_empty()
    assert not iut.ll.synchronizing


def test_late_first_packet_still_establishes(iut):
    """A first packet within the establishment window still establishes.

    Purpose: Verify that if the first received train event arrives on the last
    allowed establishment event the sync still establishes successfully (Vol 6,
    Part B, 4.4.5).

    Steps:
        1. Acquire SyncInfo for the Lower Tester train.
        2. Let establishment-event minus one events pass with no PDU.
        3. Send a "late" train event and run through it.

    Expected:
        - Sync Established reports success.
    """
    train = Train(iut)
    acquire(iut, train)
    for _ in range(LL_PERIODIC_SYNC_ESTABLISHMENT_EVENTS - 1):
        train.send(skip=True)
    train.send(b'late')
    train.run_through()
    iut.ut_rx(ESTABLISHED, status=0)


def test_sync_lost_after_timeout(iut):
    """A sync is lost after the sync timeout with no reception.

    Purpose: Verify that an established sync reports Sync Lost once no periodic
    event is received for the sync timeout, and that the synchronization is then
    gone (Vol 6, Part B, 4.4.5; Vol 4, Part E, 7.7.65.16).

    Steps:
        1. Establish a sync with a 200 ms timeout and drain reports.
        2. Let four train events pass with no PDU and run through them.

    Expected:
        - The Host gets a Periodic Advertising Sync Lost event.
        - The controller no longer holds that synchronization.
    """
    train = Train(iut)
    establish(iut, train, timeout=20)  # 200 ms
    drain_periodic(iut)
    for _ in range(4):
        train.send(skip=True)
    train.run_through()
    event = iut.ut_rx(LOST)
    assert event.sync_handle not in iut.link.syncs


def test_reception_refreshes_sync_timeout(iut):
    """Receiving periodic events keeps the sync alive past its timeout.

    Purpose: Verify that each received periodic event refreshes the sync timeout,
    so a train that keeps transmitting is not lost even past the nominal timeout
    (Vol 6, Part B, 4.4.5).

    Steps:
        1. Establish a sync with a 200 ms timeout.
        2. Send six train events and run through them.

    Expected:
        - No Sync Lost event is produced.
    """
    train = Train(iut)
    establish(iut, train, timeout=20)
    for _ in range(6):
        train.send(b'x')
    train.run_through()
    assert not [event for event in iut.events if isinstance(event, LOST)]


@pytest.mark.parametrize('use_list', [False, True])
def test_other_advertiser_or_sid_is_not_acquired(iut, use_list):
    """SyncInfo from the wrong advertiser or SID is not acquired.

    Purpose: Verify that a Create Sync targeting one advertiser/SID does not
    acquire SyncInfo announced by a different SID, whether the target is given
    directly or via the Periodic Advertiser List (Vol 6, Part B, 4.4.5; Vol 4,
    Part E, 7.8.67).

    Steps:
        1. If using the list, add a different advertiser/SID to the Periodic
           Advertiser List.
        2. Create a sync (by parameter or via the list) for the target.
        3. Lower Tester announces a train with SID 4.
        4. Run one scan window.

    Expected:
        - No SyncInfo is acquired (the pending sync still has no info).

    Parameters range over targeting by address and via the Periodic Advertiser
    List.
    """
    if use_list:
        iut.ut(
            hci.HCI_LE_Add_Device_To_Periodic_Advertiser_List_Command(
                OTHER.address_type, OTHER, 3
            )
        )
    train = Train(iut, address=PEER, sid=4)
    create_sync(iut, options=Options.USE_PERIODIC_ADVERTISER_LIST if use_list else 0)
    train.announce()
    iut.run(WINDOW_US)
    assert iut.link.sync.info is None


def test_periodic_advertiser_list_acquisition(iut):
    """A sync created from the Periodic Advertiser List acquires a listed train.

    Purpose: Verify that a Create Sync using the Periodic Advertiser List acquires
    and establishes against the advertiser/SID on the list, ignoring the command's
    own address and SID fields (Vol 4, Part E, 7.8.67, 7.8.70).

    Steps:
        1. Add the peer with SID 3 to the Periodic Advertiser List.
        2. Acquire using the list (command carries a different address and SID).
        3. Send a train event and run through it.

    Expected:
        - Sync Established reports success with the listed advertiser and SID 3.
    """
    iut.ut(
        hci.HCI_LE_Add_Device_To_Periodic_Advertiser_List_Command(
            PEER.address_type, PEER, 3
        )
    )
    train = Train(iut)
    acquire(
        iut, train, address=OTHER, sid=9, options=Options.USE_PERIODIC_ADVERTISER_LIST
    )
    train.send(b'x')
    train.run_through()
    iut.ut_rx(ESTABLISHED, status=0, advertiser_address=PEER, advertising_sid=3)


def test_reports_disabled_at_creation_and_reenabled(iut):
    """Reporting can be disabled at creation and toggled afterward.

    Purpose: Verify that a sync created with reporting initially disabled produces
    no reports, that Set Periodic Advertising Receive Enable with reporting on
    starts them, and that disabling again stops them (Vol 4, Part E, 7.8.67,
    7.8.88).

    Steps:
        1. Establish a sync with reporting initially disabled.
        2. Confirm no report reached the Host.
        3. Enable reporting; send "now"; run through the event.
        4. Disable reporting; send "quiet"; run through the event.

    Expected:
        - No report while reporting is disabled at creation.
        - A Periodic Advertising Report with "now" after reporting is enabled.
        - No report after reporting is disabled again.
    """
    train = Train(iut)
    event = establish(iut, train, options=Options.REPORTING_INITIALLY_DISABLED)
    iut.ut_rx_q_is_empty()
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command(event.sync_handle, 1)
    )
    train.send(b'now')
    train.run_through()
    iut.ut_rx(PERIODIC_REPORT, data=b'now')
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command(event.sync_handle, 0)
    )
    train.send(b'quiet')
    train.run_through()
    iut.ut_rx_q_is_empty()


def adi(did, sid=3):
    return sid << PDU_ADV_ADI_DID_BITS | did


def test_duplicate_filtering_initially_enabled(iut):
    """Create Sync bit 2 filters reports that repeat the last reported ADI.

    Purpose: Verify that with duplicate filtering initially enabled an event is
    reported only when its ADI differs from that of the previous report with an
    ADI, and that events without ADI are always reported (Vol 4, Part E, 7.8.67;
    Vol 6, Part B, 4.4.5.1.1).

    Steps:
        1. Create a sync with Options bit 2 and establish it on an AUX_SYNC_IND
           with DID 5.
        2. Send DID 5 twice, DID 6 with new data twice, then two PDUs without
           ADI; run through them.

    Expected:
        - Reports carry "a" (the first DID 5 event), "b" (the first DID 6
          event) and "c" twice; nothing else is reported.
    """
    train = Train(iut)
    acquire(iut, train, options=Options.DUPLICATE_FILTERING_INITIALLY_ENABLED)
    train.send(b'a', adi=adi(5))
    train.run_through()
    iut.ut_rx(ESTABLISHED, status=0)
    for data, did in ((b'a', 5), (b'a', 5), (b'b', 6), (b'b', 6)):
        train.send(data, adi=adi(did))
    train.send(b'c')
    train.send(b'c')
    train.run_through()
    assert drain_periodic(iut) == [b'a', b'b', b'c', b'c']
    iut.ut_rx_q_is_empty()


def test_reporting_initially_disabled_ignores_duplicate_filtering(iut):
    """Create Sync ignores bit 2 when bit 1 disables reporting.

    Purpose: Verify that a sync created with reporting initially disabled and
    duplicate filtering initially enabled starts with both reporting and
    filtering off (Vol 4, Part E, 7.8.67).

    Steps:
        1. Establish a sync with Options bits 1 and 2.

    Expected:
        - No report reaches the Host; the sync reports and filters nothing.
    """
    train = Train(iut)
    event = establish(
        iut,
        train,
        options=Options.REPORTING_INITIALLY_DISABLED
        | Options.DUPLICATE_FILTERING_INITIALLY_ENABLED,
    )
    iut.ut_rx_q_is_empty()
    sync = iut.link.syncs[event.sync_handle]
    assert not sync.reports_enabled and not sync.filter_duplicates


def test_receive_enable_toggles_duplicate_filtering(iut):
    """Receive Enable bit 1 starts and stops periodic duplicate filtering.

    Purpose: Verify that enabling filtering on a running sync suppresses data
    that was already reported, reports a new DID once, and that disabling it
    reports every event again (Vol 4, Part E, 7.8.88; Vol 6, Part B, 4.4.5.1.1).

    Steps:
        1. Establish a sync without filtering on DID 5 and send DID 5 again.
        2. Send Receive Enable 0x03; send DID 5, then DID 6 twice.
        3. Send Receive Enable 0x01; send DID 6 twice.

    Expected:
        - Step 1 reports both events.
        - Step 2 reports only the first DID 6 event.
        - Step 3 reports both events.
    """
    train = Train(iut)
    acquire(iut, train)
    train.send(b'a', adi=adi(5))
    train.run_through()
    handle = iut.ut_rx(ESTABLISHED, status=0).sync_handle
    train.send(b'a', adi=adi(5))
    train.run_through()
    assert drain_periodic(iut) == [b'a', b'a']
    receive_enable = hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command
    iut.ut(receive_enable(handle, 0b11))
    for data, did in ((b'a', 5), (b'b', 6), (b'b', 6)):
        train.send(data, adi=adi(did))
    train.run_through()
    assert drain_periodic(iut) == [b'b']
    iut.ut(receive_enable(handle, 0b01))
    for _ in range(2):
        train.send(b'b', adi=adi(6))
    train.run_through()
    assert drain_periodic(iut) == [b'b', b'b']


def test_truncated_report_clears_the_duplicate_record(iut):
    """A truncated report makes no ADI a duplicate.

    Purpose: Verify that a truncated report becomes the train's previous report
    without filtering anything: the next copy of its data is reported, and so is
    the DID reported before it. Complete data is then filtered as usual (Vol 6,
    Part B, 4.4.5.1.1 and 4.4.3.5; Vol 4, Part E, 7.7.65.15).

    Steps:
        1. Create a sync with duplicate filtering initially enabled and
           establish it on an AUX_SYNC_IND with DID 5.
        2. Send DID 7 whose AuxPtr leads to no AUX_CHAIN_IND, then DID 7 with
           complete data twice.
        3. Send DID 9 whose AuxPtr leads to no AUX_CHAIN_IND, then DID 7 with
           complete data.

    Expected:
        - Step 1 reports "a".
        - Step 2 reports "head" as incomplete and an empty truncated report,
          then "full" once, as complete.
        - Step 3 reports "head" and an empty truncated report, then "full".
    """

    def truncated(did):
        train.send(b'head', adi=adi(did), aux_ptr=AuxPtr(AUX_CHANNEL, 3000))

    train = Train(iut)
    acquire(iut, train, options=Options.DUPLICATE_FILTERING_INITIALLY_ENABLED)
    train.send(b'a', adi=adi(5))
    train.run_through()
    iut.ut_rx(ESTABLISHED, status=0)
    iut.ut_rx(PERIODIC_REPORT, data=b'a', data_status=0)
    truncated(7)
    for _ in range(2):
        train.send(b'full', adi=adi(7))
    truncated(9)
    train.send(b'full', adi=adi(7))
    train.run_through()
    for _ in range(2):
        iut.ut_rx(PERIODIC_REPORT, data=b'head', data_status=1)
        iut.ut_rx(PERIODIC_REPORT, data=b'', data_status=2)
        iut.ut_rx(PERIODIC_REPORT, data=b'full', data_status=0)
    iut.ut_rx_q_is_empty()


def test_reporting_disabled_skips_the_chain(iut):
    """A sync that reports nothing does not follow AUX_CHAIN_IND.

    Purpose: Verify that while reporting is disabled the synchronizer receives
    only AUX_SYNC_IND, which keeps it synchronized, and does not listen for the
    chain, whose data it would not report (Vol 6, Part B, 4.4.5.1.1).

    Steps:
        1. Establish a sync with reporting initially disabled.
        2. Send an AUX_SYNC_IND whose AuxPtr points 3 ms later and run past it.
        3. Enable reporting and repeat step 2.

    Expected:
        - In step 2 the synchronizer opens no window after the AUX_SYNC_IND
          and reports nothing.
        - In step 3 it listens for the AUX_CHAIN_IND and reports "head" as
          incomplete, then an empty truncated report.
    """
    train = Train(iut)
    event = establish(iut, train, options=Options.REPORTING_INITIALLY_DISABLED)

    def chain_listens():
        mark, anchor = len(iut.radio.listens), train.anchor_us
        train.send(b'head', aux_ptr=AuxPtr(AUX_CHANNEL, 3000))
        iut.run_until(anchor + 10_000)
        return [entry for entry in iut.radio.listens[mark:] if entry[0] > anchor]

    assert chain_listens() == []
    iut.ut_rx_q_is_empty()
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command(event.sync_handle, 1)
    )
    assert [channel for _, _, channel, _ in chain_listens()] == [AUX_CHANNEL]
    iut.ut_rx(PERIODIC_REPORT, data=b'head', data_status=1)
    iut.ut_rx(PERIODIC_REPORT, data=b'', data_status=2)
    iut.ut_rx_q_is_empty()


def test_receive_enable_validation(iut):
    """Set Periodic Advertising Receive Enable validates handle and enable.

    Purpose: Verify that Set Periodic Advertising Receive Enable reports Unknown
    Advertising Identifier for an unknown sync handle, accepts the reporting and
    duplicate-filtering bits, and reports Unsupported Feature for a reserved bit
    (Vol 4, Part E, 7.8.88).

    Steps:
        1. Send Receive Enable for a nonexistent sync handle.
        2. Establish a sync.
        3. Send Receive Enable with each value 0x00 to 0x03, then with 0x04.

    Expected:
        - The unknown-handle command fails with
          UNKNOWN_ADVERTISING_IDENTIFIER_ERROR.
        - Values 0x00 to 0x03 succeed and set both flags of the sync.
        - The reserved bit fails with
          UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR.
    """
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command(5, 1),
        ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR,
    )
    train = Train(iut)
    event = establish(iut, train)
    sync = iut.link.syncs[event.sync_handle]
    for enable in range(4):
        iut.ut(
            hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command(
                event.sync_handle, enable
            )
        )
        assert (sync.reports_enabled, sync.filter_duplicates) == (
            bool(enable & 1),
            bool(enable & 2),
        )
    iut.ut(
        hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command(
            event.sync_handle, 0b100
        ),
        ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
    )


def test_terminate_sync(iut):
    """Terminating a sync stops reports and frees the handle.

    Purpose: Verify that Periodic Advertising Terminate Sync stops further
    reports and that terminating the same handle again reports Unknown
    Advertising Identifier (Vol 4, Part E, 7.8.69).

    Steps:
        1. Establish a sync and drain reports.
        2. Terminate the sync.
        3. Send a train event "after" and run through it.
        4. Terminate the same handle again.

    Expected:
        - No report reaches the Host after termination.
        - The second terminate fails with UNKNOWN_ADVERTISING_IDENTIFIER_ERROR.
    """
    train = Train(iut)
    event = establish(iut, train)
    drain_periodic(iut)
    iut.ut(hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(event.sync_handle))
    train.send(b'after')
    train.run_through()
    iut.ut_rx_q_is_empty()
    iut.ut(
        hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(event.sync_handle),
        ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR,
    )


def test_cancel_pending_sync(iut):
    """Cancelling a pending Create Sync reports it and locks a second cancel.

    Purpose: Verify that Periodic Advertising Create Sync Cancel aborts a pending
    acquisition, reporting Sync Established with Operation Cancelled By Host, and
    that a second cancel with nothing pending is disallowed (Vol 4, Part E,
    7.8.68).

    Steps:
        1. Create a sync.
        2. Send Create Sync Cancel and run so the event is delivered.
        3. Send Create Sync Cancel again.

    Expected:
        - Sync Established reports OPERATION_CANCELLED_BY_HOST_ERROR.
        - The second cancel fails with COMMAND_DISALLOWED_ERROR.
    """
    create_sync(iut)
    iut.ut(hci.HCI_LE_Periodic_Advertising_Create_Sync_Cancel_Command())
    iut.run(1)
    iut.ut_rx(ESTABLISHED, status=ErrorCode.OPERATION_CANCELLED_BY_HOST_ERROR)
    iut.ut(
        hci.HCI_LE_Periodic_Advertising_Create_Sync_Cancel_Command(),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )


def test_create_sync_rules(iut):
    """Create Sync enforces one pending request and no duplicate sync.

    Purpose: Verify that a second Create Sync while one is pending is disallowed,
    and that after a sync is established a Create Sync for the same train reports
    Connection Already Exists (Vol 4, Part E, 7.8.67).

    Steps:
        1. Create a sync, then create another while it is pending.
        2. Cancel the pending sync and clear events.
        3. Establish a sync against the train.
        4. Create a sync for the same train again.

    Expected:
        - The second concurrent Create Sync fails with
          COMMAND_DISALLOWED_ERROR.
        - The post-establishment Create Sync fails with
          CONNECTION_ALREADY_EXISTS_ERROR.
    """
    create_sync(iut)
    create_sync(iut, status=ErrorCode.COMMAND_DISALLOWED_ERROR)
    iut.ut(hci.HCI_LE_Periodic_Advertising_Create_Sync_Cancel_Command())
    iut.run(1)
    iut.events.clear()
    train = Train(iut)
    establish(iut, train)
    create_sync(iut, status=ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR)


@pytest.mark.parametrize(
    'changes,status',
    [
        (dict(timeout=9), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (dict(skip=0x01F4), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (dict(sid=16), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (dict(options=1 << 3), ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR),
    ],
)
def test_create_sync_validation(iut, changes, status):
    """Create Sync rejects invalid and unsupported parameters.

    Purpose: Verify that Create Sync rejects a too-small timeout, an out-of-range
    skip, an out-of-range SID, and a reserved Options bit, and that synchronizing
    does not start (Vol 4, Part E, 7.8.67).

    Steps:
        1. Send Create Sync with the invalid field set.

    Expected:
        - The command completes with the parametrized error status.
        - The controller is not synchronizing.

    Parameters range over too-small timeout, out-of-range skip, out-of-range SID,
    and reserved Options bit 3.
    """
    create_sync(iut, status=status, **changes)
    assert not iut.ll.synchronizing


def test_periodic_list_edits_and_lock(iut):
    """The Periodic Advertiser List enforces capacity, edits and a lock.

    Purpose: Verify that the Periodic Advertiser List fills to its reported size,
    rejects a duplicate add, rejects an add past capacity, supports remove with
    an unknown-remove error, rejects an invalid address type, and is locked for
    add and clear while a Create Sync is pending (Vol 4, Part E, 7.8.70-7.8.72).

    Steps:
        1. Read the list size and fill it with distinct SIDs for the peer.
        2. Add a duplicate, then one past capacity.
        3. Remove an entry, then remove it again.
        4. Add with an invalid address type.
        5. Create a sync, then try to add and to clear the list.

    Expected:
        - The duplicate add fails with INVALID_COMMAND_PARAMETERS_ERROR.
        - The over-capacity add fails with MEMORY_CAPACITY_EXCEEDED_ERROR.
        - The repeat remove fails with UNKNOWN_ADVERTISING_IDENTIFIER_ERROR.
        - The invalid address type add fails with
          INVALID_COMMAND_PARAMETERS_ERROR.
        - The add and clear while syncing fail with COMMAND_DISALLOWED_ERROR.
    """
    add = hci.HCI_LE_Add_Device_To_Periodic_Advertiser_List_Command
    remove = hci.HCI_LE_Remove_Device_From_Periodic_Advertiser_List_Command
    size = iut.ut(
        hci.HCI_LE_Read_Periodic_Advertiser_List_Size_Command()
    ).periodic_advertiser_list_size
    for sid in range(size):
        iut.ut(add(PEER.address_type, PEER, sid))
    iut.ut(add(PEER.address_type, PEER, 0), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR)
    iut.ut(add(OTHER.address_type, OTHER, 0), ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR)
    iut.ut(remove(PEER.address_type, PEER, 0))
    iut.ut(
        remove(PEER.address_type, PEER, 0),
        ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR,
    )
    iut.ut(add(2, PEER, 0), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR)
    create_sync(iut)
    iut.ut(add(OTHER.address_type, OTHER, 1), ErrorCode.COMMAND_DISALLOWED_ERROR)
    iut.ut(
        hci.HCI_LE_Clear_Periodic_Advertiser_List_Command(),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )


# ---------------------------------------------------------------------------
# Periodic channel map update via ACAD (2.3.4.8)
# ---------------------------------------------------------------------------
def channel_map_acad(channels, instant):
    return bytes(
        advertising_data(
            [
                (
                    AD_CHANNEL_MAP_UPDATE_INDICATION,
                    channels.to_bytes(5, 'little') + instant.to_bytes(2, 'little'),
                )
            ]
        )
    )


def test_acad_channel_map_applies_at_instant(iut):
    """An ACAD channel-map update takes effect at its instant.

    Purpose: Verify that a Channel Map Update Indication carried in the periodic
    event's ACAD switches the synchronizer's data channels to the new map
    starting at the indicated instant, without missing reports across the switch
    (Vol 6, Part B, 2.3.4.8, 4.4.5).

    Steps:
        1. Establish a sync and drain reports.
        2. Send event "a" carrying an ACAD update to a narrow map at instant
           counter + 2, then "b".
        3. Switch the Lower Tester train's channel map to the narrow map and send
           "c".
        4. Run through the events.

    Expected:
        - Reports "a", "b" and "c" are all received in order.
        - The sync's stored channel map is the narrow map.
    """
    train = Train(iut)
    establish(iut, train)
    drain_periodic(iut)
    instant = (train.counter + 2) & LL_EVENT_COUNTER_MASK
    narrow = 0b11111 << 10
    train.send(b'a', acad=channel_map_acad(narrow, instant))
    train.send(b'b')
    train.run_through()
    train.channel_map = narrow
    train.send(b'c')
    train.run_through()
    assert [r.data for r in iut.events if isinstance(r, PERIODIC_REPORT)] == [
        b'a',
        b'b',
        b'c',
    ]
    assert iut.link.syncs[1].info.channel_map == narrow


@pytest.mark.parametrize(
    'channels,instant_delta,length',
    [(1, 2, 7), (LL_DATA_CHANNEL_MAP_ALL + 1, 2, 7), (3, -1, 7), (3, 2, 6)],
    ids=['one-channel', 'rfu', 'past', 'short'],
)
def test_invalid_acad_channel_map_is_ignored(iut, channels, instant_delta, length):
    """An invalid ACAD channel-map update is ignored.

    Purpose: Verify that the synchronizer ignores a Channel Map Update Indication
    that names a single channel, sets reserved bits, uses a past instant, or is
    too short, so no channel-map update is pending (Vol 6, Part B, 2.3.4.8).

    Steps:
        1. Establish a sync.
        2. Send an event whose ACAD carries the parametrized invalid update.
        3. Run through the event.

    Expected:
        - No channel-map update is pending on the sync.

    Parameters range over one-channel, reserved-bit, past-instant and too-short
    updates.
    """
    train = Train(iut)
    establish(iut, train)
    instant = (train.counter + instant_delta) & LL_EVENT_COUNTER_MASK
    value = (channels.to_bytes(5, 'little') + instant.to_bytes(2, 'little') + b'\0')[
        :length
    ]
    acad = bytes(advertising_data([(AD_CHANNEL_MAP_UPDATE_INDICATION, value)]))
    train.send(b'x', acad=acad)
    train.run_through()
    assert iut.link.syncs[1].channel_map_update is None


def test_malformed_acad_stops_parsing(iut):
    """Malformed ACAD entries are skipped without losing a valid update.

    Purpose: Verify, against a PeriodicSync directly, that a zero-length ACAD
    entry and an entry claiming an over-long length both leave no channel-map
    update pending, while a well-formed entry is parsed and recorded (Vol 6,
    Part B, 2.3.4.8).

    Steps:
        1. Build a PeriodicSync and a valid channel-map ACAD payload.
        2. Feed it preceded by a zero-length entry.
        3. Feed it preceded by an over-long-length entry.
        4. Feed two concatenated valid entries.

    Expected:
        - The first two cases leave no channel-map update pending.
        - The final case records the update as (instant 20, map 3).
    """
    from bscontroller.linklayer.discovery import PeriodicSync

    sync = PeriodicSync(PEER, 3, 100_000, 0, counter=10)
    good = channel_map_acad(3, 20)
    sync.receive_channel_map(bytes([0]) + good)
    assert sync.channel_map_update is None
    sync.receive_channel_map(bytes([40, 1]) + good)
    assert sync.channel_map_update is None
    sync.receive_channel_map(good + good)
    assert sync.channel_map_update == (20, 3)


@pytest.mark.parametrize('counter', [10, LL_EVENT_COUNTER_MASK - 2])
@pytest.mark.parametrize('skip', [1, 4])
def test_acad_instant_applies_across_skipped_events(counter, skip):
    """A channel-map instant applies even when events are skipped over it.

    Purpose: Verify, against a PeriodicSync directly, that a channel-map update
    scheduled for a future instant is not applied before the instant but is
    applied once the event counter advances to or past it, even across a skip,
    and across the counter wrap (Vol 6, Part B, 2.3.4.8).

    Steps:
        1. Build a PeriodicSync with SyncInfo at the parametrized counter.
        2. Receive a channel-map update for instant counter + 3.
        3. Advance the counter to counter + 2 and apply; the map is unchanged.
        4. Advance by the parametrized skip and apply again.

    Expected:
        - Before the instant the channel map is still the all-channels map.
        - After advancing to or past the instant the map is the new value and no
          update stays pending.

    Parameters range over counters {10, near the wrap} crossed with skips {1, 4}.
    """
    from bscontroller.linklayer.discovery import PeriodicSync

    info = SyncInfo(9000, 80, LL_DATA_CHANNEL_MAP_ALL, SYNC_AA, SYNC_CRC, counter)
    sync = PeriodicSync(PEER, 3, 100_000, 0, info=info, counter=counter)
    instant = (counter + 3) & LL_EVENT_COUNTER_MASK
    sync.receive_channel_map(channel_map_acad(0b10101, instant))
    sync.counter = (counter + 2) & LL_EVENT_COUNTER_MASK
    sync.apply_channel_map()
    assert sync.info.channel_map == LL_DATA_CHANNEL_MAP_ALL
    sync.counter = (sync.counter + skip) & LL_EVENT_COUNTER_MASK
    sync.apply_channel_map()
    assert sync.info.channel_map == 0b10101 and sync.channel_map_update is None
