# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Independent Lower Tester for Periodic Advertising Sync Transfer.

The Lower Tester serves one ACL connection and one periodic train on its single
radio, in time order. LL_PERIODIC_SYNC_IND is encoded and decoded here from
Core Vol 6, Part B, 2.4.2.27 and 2.3.4.6, without Controller codecs.
"""

import struct
from dataclasses import dataclass

from bscontroller import hci
from bscontroller.hci import HCI_LE_PERIODIC_SYNC_TIMEOUT_MIN
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    LL_DATA_CHANNEL_MAP_ALL,
    LL_EVENT_COUNTER_MASK,
    LL_PERIODIC_INTERVAL_UNIT_US,
    PDU_ADV_OFFSET_UNIT_30_US,
    PDU_ADV_OFFSET_UNIT_300_US,
    PDU_ADV_OFFSET_VALUE_MASK,
    PDU_ADV_SYNC_OFFSET_ADJUST_MASK,
    PDU_ADV_SYNC_OFFSET_ADJUST_US,
    PDU_ADV_SYNC_OFFSET_UNITS_MASK,
    PDU_DATA_LLID_MASK,
    PDU_HEADER_SIZE,
    ControlOpcode,
    LlId,
)
from bscontroller.radio.base import Phy
from tests.qualification.peer import (
    PEER_FEATURES,
    change_phy,
    establish,
    exchange_features,
)
from tests.qualification.periodic import PeriodicTrain
from tests.qualification.tester import (
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
)

Opcode = ControlOpcode
ErrorCode = hci.HCI_ErrorCode
RECEIVED = hci.HCI_LE_Periodic_Advertising_Sync_Transfer_Received_Event
RECEIVED_V2 = hci.HCI_LE_Periodic_Advertising_Sync_Transfer_Received_V2_Event
REPORT = hci.HCI_LE_Periodic_Advertising_Report_Event
TransferParameters = (
    hci.HCI_LE_Set_Periodic_Advertising_Sync_Transfer_Parameters_Command
)

# The Lower Tester advertiser, distinct from its ACL addresses.
ADVERTISER = hci.Address('00:5A:A5:00:5A:A5', hci.AddressType.PUBLIC_DEVICE)

# Selected bound on connection events before an expected event or PDU.
RESPONSE_EVENTS = 64


# Figure 2.48: ID, SyncInfo, connEventCount, lastPaEventCounter, SID/AType/SCA,
# PHY, AdvA, syncConnEventCount. The opcode precedes these 34 octets.
CTRDATA = struct.Struct('<H18sHHBB6sH')
SYNC_INFO = struct.Struct('<HH5sI3sH')
PERIODIC_SYNC_SIZE = 1 + CTRDATA.size
# 2.3.4.6: the largest syncPacketWindowOffset, with Offset Adjust.
TRANSFER_OFFSET_MAX_US = (
    PDU_ADV_SYNC_OFFSET_ADJUST_US
    + PDU_ADV_OFFSET_VALUE_MASK * PDU_ADV_OFFSET_UNIT_300_US
)
# 2.4.2.27: a lastPaEventCounter more than one event away must be within 5 s.
COUNTER_SPAN_US = 5_000_000
# A data channel Access Address distinct from the Lower Tester ACL.
TRAIN_ACCESS_ADDRESS = 0x9DDE2508
TRAIN_CRC_INIT = 0x445566
# The advertiser SCA carried in the Lower Tester's SyncInfo: 50 ppm.
TRAIN_SCA = 5
SERVICE_DATA = 0x5AA5
# Free radio time kept before each connection anchor, and after now.
GUARD_US = 1500
PREPARATION_US = 100


def signed(value):
    """The nearest signed distance of a 16-bit counter difference."""
    value &= LL_EVENT_COUNTER_MASK
    return value - (LL_EVENT_COUNTER_MASK + 1) if value & 0x8000 else value


def phy_bit(phy):
    return 1 << (Phy(phy) - 1)


@dataclass(frozen=True)
class SyncIndication:
    identifier: int
    sync_info: bytes
    event: int
    last: int
    sid: int
    random: bool
    sca: int
    phy: int
    address: bytes
    sync_event: int
    pawr: tuple[int, int, int, int] | None = None
    rsp_aa: int = 0

    def encode(self):
        return (
            bytes(
                [
                    Opcode.LL_PERIODIC_SYNC_WR_IND
                    if self.pawr
                    else Opcode.LL_PERIODIC_SYNC_IND
                ]
            )
            + CTRDATA.pack(
                self.identifier,
                self.sync_info,
                self.event,
                self.last,
                self.sid | int(self.random) << 4 | self.sca << 5,
                self.phy,
                self.address,
                self.sync_event,
            )
            + (struct.pack('<IBBBB', self.rsp_aa, *self.pawr) if self.pawr else b'')
        )

    @classmethod
    def decode(cls, payload):
        responses = payload[0] == Opcode.LL_PERIODIC_SYNC_WR_IND
        assert payload[0] in (
            Opcode.LL_PERIODIC_SYNC_IND,
            Opcode.LL_PERIODIC_SYNC_WR_IND,
        )
        assert len(payload) == PERIODIC_SYNC_SIZE + (8 if responses else 0)
        identifier, info, event, last, flags, phy, address, sync_event = CTRDATA.unpack(
            payload[1:PERIODIC_SYNC_SIZE]
        )
        return cls(
            identifier,
            info,
            event,
            last,
            flags & 0x0F,
            bool(flags & 0x10),
            flags >> 5,
            phy,
            address,
            sync_event,
            tuple(payload[-4:]) if responses else None,
            int.from_bytes(payload[-8:-4], 'little') if responses else 0,
        )


@dataclass(frozen=True)
class TransferredInfo:
    """A decoded SyncInfo of LL_PERIODIC_SYNC_IND (2.3.4.6, 2.4.2.27)."""

    delay_us: int
    unit_us: int
    interval: int
    channel_map: int
    sca: int
    access_address: int
    crc_init: int
    counter: int

    @classmethod
    def decode(cls, data):
        offset, interval, channels, aa, crc, counter = SYNC_INFO.unpack(data)
        unit = (
            PDU_ADV_OFFSET_UNIT_300_US
            if offset & PDU_ADV_SYNC_OFFSET_UNITS_MASK
            else PDU_ADV_OFFSET_UNIT_30_US
        )
        delay = (offset & PDU_ADV_OFFSET_VALUE_MASK) * unit
        if offset & PDU_ADV_SYNC_OFFSET_ADJUST_MASK:
            assert unit == PDU_ADV_OFFSET_UNIT_300_US, 'Offset Adjust with 30-us units'
            delay += PDU_ADV_SYNC_OFFSET_ADJUST_US
        channels = int.from_bytes(channels, 'little')
        return cls(
            delay,
            unit,
            interval,
            channels & LL_DATA_CHANNEL_MAP_ALL,
            channels >> 37,
            aa,
            int.from_bytes(crc, 'little'),
            counter,
        )


def train_anchor(train, counter):
    """Anchor of any event of ``train``, on its own interval grid."""
    return (
        train.anchor
        + signed(counter - train.counter)
        * train.interval
        * LL_PERIODIC_INTERVAL_UNIT_US
    )


def encode_sync_info(train, counter, reference_us, *, sca=TRAIN_SCA):
    """SyncInfo for event ``counter``, relative to ``reference_us`` (2.3.4.6)."""
    delay = train_anchor(train, counter) - reference_us
    assert 0 <= delay <= TRANSFER_OFFSET_MAX_US, delay
    flags = 0
    if delay >= PDU_ADV_SYNC_OFFSET_ADJUST_US:
        delay -= PDU_ADV_SYNC_OFFSET_ADJUST_US
        flags |= PDU_ADV_SYNC_OFFSET_ADJUST_MASK | PDU_ADV_SYNC_OFFSET_UNITS_MASK
        unit = PDU_ADV_OFFSET_UNIT_300_US
    elif delay >= (PDU_ADV_OFFSET_VALUE_MASK + 1) * PDU_ADV_OFFSET_UNIT_30_US:
        flags |= PDU_ADV_SYNC_OFFSET_UNITS_MASK
        unit = PDU_ADV_OFFSET_UNIT_300_US
    else:
        unit = PDU_ADV_OFFSET_UNIT_30_US
    return SYNC_INFO.pack(
        delay // unit | flags,
        train.interval,
        (train.channel_map | sca << 37).to_bytes(5, 'little'),
        train.access_address,
        train.crc_init.to_bytes(3, 'little'),
        counter,
    )


def lower_train(
    link, *, interval=96, phy=Phy.LE_1M, counter=0x1234, events=2, pawr=False
):
    """A Lower Tester train phased between connection events.

    The anchor sits half the shorter of the two intervals after a connection
    anchor. With one interval a multiple of the other, the train and the
    connection never contend for the single Lower Tester radio.
    """
    connection_us = link.parameters.interval * LL_CONN_INTERVAL_UNIT_US
    interval_us = interval * LL_PERIODIC_INTERVAL_UNIT_US
    assert interval_us % connection_us == 0 or connection_us % interval_us == 0
    return PeriodicTrain(
        link.anchor + events * connection_us + min(connection_us, interval_us) // 2,
        interval,
        counter,
        TRAIN_ACCESS_ADDRESS,
        TRAIN_CRC_INIT,
        phy=phy,
        pawr=(2, 6, 1, 10) if pawr else None,
    )


def event_data(counter):
    """Periodic data naming its paEventCounter (LL.TS 4.3.7.5 Step 2)."""
    return b'event' + counter.to_bytes(2, 'little')


class TransferSession:
    """Serve one connection and one train on the Lower Tester's single radio.

    ``mode`` is 'transmit' (Lower Tester train), 'receive' (IUT train),
    'silent' (the train's events pass without a packet) or 'off'. Optional
    ``announce`` sends ADV_EXT_IND/AUX_ADV_IND SyncInfo once per interval.
    """

    def __init__(self, tester, link, train=None, *, mode='transmit', adi=None):
        self.tester, self.link, self.train = tester, link, train
        self.mode = mode
        self.adi = adi
        self.data = event_data
        self.announce = None
        self.announcements = 0
        self.sent = []
        self.received = []

    @property
    def interval_us(self):
        return self.link.parameters.interval * LL_CONN_INTERVAL_UNIT_US

    @property
    def now_us(self):
        return self.tester.radio.now_us

    def catch_up(self):
        """Skip connection events whose anchor already passed (Core 4.5.1)."""
        while self.link.anchor < self.now_us + GUARD_US:
            self.link.anchor += self.interval_us
            self.link.counter = (self.link.counter + 1) & LL_EVENT_COUNTER_MASK

    def _budget(self):
        airtime = self.train.phy.airtime_us(PDU_HEADER_SIZE + 64)
        if self.train.pawr is not None and self.mode == 'transmit':
            airtime += (self.train.pawr[0] - 1) * self.train.pawr[1] * 1250
        return airtime + (3000 if self.mode == 'receive' else 300)

    async def _fill(self):
        limit = self.link.anchor - GUARD_US
        if self.announce is not None and self.train is not None:
            # After the previous connection event; the varying phase keeps a
            # long Coded preamble from always meeting the same IUT receive-window
            # boundary.
            self.announcements += 1
            start = (
                self.link.anchor
                - self.interval_us
                + self.interval_us // 10
                + self.announcements * 337 % 1000
            )
            if start > self.now_us + PREPARATION_US:
                await self.train.announce(self.tester, start=start, **self.announce)
        while self.train is not None and self.mode != 'off':
            anchor = self.train.anchor
            if anchor + self._budget() >= limit:
                break
            if anchor <= self.now_us + PREPARATION_US or self.mode == 'silent':
                self.train.next_event()
                continue
            counter = self.train.counter
            if self.mode == 'transmit':
                adi = self.adi(counter) if self.adi is not None else None
                await self.train.transmit(self.tester, self.data(counter), adi=adi)
                self.sent.append(counter)
            else:
                payload = await self.train.receive(self.tester, required=False)
                if payload is not None:
                    self.received.append((counter, payload))

    async def step(self, payload=b'', *, llid=LlId.CONTINUATION):
        """Serve train events before the next connection event, then that event."""
        self.catch_up()
        await self._fill()
        self.catch_up()
        return await self.link.exchange(
            payload, llid=llid if payload else LlId.CONTINUATION
        )

    async def run(self, events):
        for _ in range(events):
            await self.step()

    async def run_for(self, duration_us):
        end = self.now_us + duration_us
        while self.link.anchor < end:
            await self.step()

    async def send_control(self, payload):
        """Send a control PDU and require its acknowledgement.

        A Lower Tester Peripheral transmits after accepting the IUT packet, so
        its SN is known only after the exchange; the IUT acknowledges in its
        next packet. A Lower Tester Central receives the acknowledgement in the
        same exchange.
        """
        sent = self.link.sn
        await self.step(payload, llid=LlId.CONTROL)
        if self.link.central:
            sent = self.link.sn
            await self.step()
        assert self.link.sn != sent, 'LL Control PDU not acknowledged'

    async def expect_control(self, opcode, *, events=32):
        """Return an IUT control PDU with its connection event counter and anchor."""
        for _ in range(events):
            packet = await self.step()
            if (
                packet is not None
                and packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
                and len(packet.pdu) > PDU_HEADER_SIZE
                and packet.pdu[PDU_HEADER_SIZE] == opcode
            ):
                return (
                    packet.pdu[PDU_HEADER_SIZE:],
                    (self.link.counter - 1) & LL_EVENT_COUNTER_MASK,
                    self.link.anchor - self.interval_us,
                )
        raise AssertionError(f'LL control {Opcode(opcode).name} not received')

    async def until(self, predicate, *, events):
        """Serve connection events until an HCI event matches; None otherwise."""
        for _ in range(events):
            if (event := self.tester.take(predicate)) is not None:
                return event
            await self.step()
        return self.tester.take(predicate)

    async def command(self, command, status=ErrorCode.SUCCESS):
        """An HCI command between connection events, then keep the link grid."""
        result = await self.tester.command(command, status)
        self.catch_up()
        return result

    def take_all(self, kind):
        found = []
        while (
            event := self.tester.take(lambda item: isinstance(item, kind))
        ) is not None:
            found.append(event)
        return found

    def reports(self, handle=None):
        reports = self.take_all(
            (REPORT, hci.HCI_LE_Periodic_Advertising_Report_V2_Event)
        )
        reports = [report for report in reports if report.data_status != 0xFF]
        assert handle is None or all(report.sync_handle == handle for report in reports)
        return reports

    def transfers(self):
        events = self.take_all((RECEIVED, RECEIVED_V2))
        assert all(type(event) is self.received_type for event in events)
        return events

    @property
    def received_type(self):
        return (
            RECEIVED_V2
            if self.train is not None and self.train.pawr is not None
            else RECEIVED
        )

    def indication(
        self,
        *,
        event_offset=0,
        counter=None,
        last=None,
        sid=5,
        address,
        phy=None,
        identifier=SERVICE_DATA,
        sca=0,
    ):
        """The Lower Tester's PDU for its train, sent in the next connection event.

        connEventCount is that event plus ``event_offset``; SyncInfo points at
        ``counter``, by default the train's next event. lastPaEventCounter
        defaults to the event before it. SCA 0 matches the source's RFU 000.
        """
        self.catch_up()
        train = self.train
        counter = train.counter if counter is None else counter & LL_EVENT_COUNTER_MASK
        reference = self.link.anchor + event_offset * self.interval_us
        return SyncIndication(
            identifier,
            encode_sync_info(train, counter, reference),
            (self.link.counter + event_offset) & LL_EVENT_COUNTER_MASK,
            ((counter - 1) if last is None else last) & LL_EVENT_COUNTER_MASK,
            sid,
            address.is_random,
            sca,
            phy_bit(train.phy) if phy is None else phy,
            bytes(address),
            (self.link.counter - 1) & LL_EVENT_COUNTER_MASK,
            train.pawr,
            train.rsp_aa if train.pawr is not None else 0,
        ).encode()


def transferred_anchor(indication, counter, anchor, interval_us):
    """Tnominal of the described event on the receiving connection grid."""
    info = TransferredInfo.decode(indication.sync_info)
    return (
        anchor + signed(indication.event - counter) * interval_us + info.delay_us,
        info,
    )


def check_received(event, link, train, *, sid, address, address_type=None, status=0):
    """Transfer Received [v1] fields (Vol 4, Part E, 7.7.65.24)."""
    assert type(event) is (RECEIVED_V2 if train.pawr else RECEIVED)
    if train.pawr:
        assert (
            event.num_subevents,
            event.subevent_interval,
            event.response_slot_delay,
            event.response_slot_spacing,
        ) == train.pawr
    assert event.status == status
    assert event.connection_handle == link.handle
    assert event.service_data == SERVICE_DATA
    assert event.advertising_sid == sid
    assert event.advertiser_address == address
    assert event.advertiser_address_type == (
        address.address_type if address_type is None else address_type
    )
    assert event.advertiser_phy == train.phy
    assert event.periodic_advertising_interval == train.interval
    assert event.advertiser_clock_accuracy == TRAIN_SCA
    if status == ErrorCode.SUCCESS:
        assert 0 <= event.sync_handle <= 0x0EFF


def sync_timeout(interval, factor):
    """Sync_Timeout of ``factor`` periodic intervals, in 10-ms units.

    A selected floor applies the HCI minimum of 100 ms to short trains
    (Core Vol 4, Part E, 7.8.91).
    """
    return max(
        HCI_LE_PERIODIC_SYNC_TIMEOUT_MIN,
        -(-factor * interval * LL_PERIODIC_INTERVAL_UNIT_US // 10_000),
    )


async def transfer_link(
    tester, *, central, phy=Phy.LE_1M, interval=24, timeout=100, pawr=False
):
    """Initial Condition: connected on the selected PHY, LE 1M establishment.

    Extended commands establish the link: after a legacy advertising command,
    the transfer-mode commands are Command Disallowed (Core Vol 4, Part E,
    3.1.1).
    """
    link = await establish(
        tester,
        central=central,
        extended=True,
        interval=interval,
        timeout=timeout,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
    )
    if phy != Phy.LE_1M:
        await exchange_features(link, PEER_FEATURES)
        # A Central IUT answers LL_PHY_REQ with LL_PHY_UPDATE_IND, not a response
        # the remote form expects; its Host starts the procedure instead.
        await change_phy(tester, link, phy, local=central)
    if pawr:
        from tests.support import LE_EVENT_MASK_V1

        mask = LE_EVENT_MASK_V1 | sum(1 << (code - 1) for code in (0x24, 0x25, 0x26))
        await tester.masks(le=mask)
    return link


async def disconnect(tester, link):
    """The Lower Tester terminates; the IUT acknowledges and reports it.

    The termination is served on the connection grid, so a long connection
    interval cannot delay the acknowledgement past the HCI observation.
    """
    reason = ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    session = TransferSession(tester, link)
    await session.send_control(bytes([Opcode.LL_TERMINATE_IND, reason]))
    event = await tester.event(hci.HCI_Disconnection_Complete_Event)
    assert (event.connection_handle, event.reason) == (link.handle, reason)


async def set_transfer_mode(session, mode, *, skip=0, timeout=None):
    train = session.train
    await session.command(
        TransferParameters(
            session.link.handle,
            mode,
            skip,
            sync_timeout(train.interval, 3) if timeout is None else timeout,
            0,
        )
    )


async def receive_transfer(session, *, sid, address=ADVERTISER, **expected):
    event = await session.until(
        lambda item: isinstance(item, session.received_type), events=RESPONSE_EVENTS
    )
    assert event is not None, 'no Periodic Advertising Sync Transfer Received'
    check_received(
        event, session.link, session.train, sid=sid, address=address, **expected
    )
    if session.train.pawr and event.status == 0 and session.train.pawr[0] > 1:
        await session.command(
            hci.HCI_LE_Set_Periodic_Sync_Subevent_Command(event.sync_handle, 0, [1])
        )
    return event
