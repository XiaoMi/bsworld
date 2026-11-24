# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.3: Connection Subrating."""

import math
import random
import struct
from dataclasses import dataclass, replace

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    LL_EVENT_COUNTER_MASK,
    LL_IFS_US,
    PDU_DATA_LLID_MASK,
    PDU_DATA_MD_MASK,
    PDU_DATA_NESN_MASK,
    PDU_DATA_SN_MASK,
    PDU_HEADER_SIZE,
    ControlOpcode,
    LlId,
)
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.peer import (
    PeerLink,
    establish,
    exchange_features,
    received_acl,
)
from tests.qualification.tester import (
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
)

Opcode = ControlOpcode
ErrorCode = hci.HCI_ErrorCode
HOST_FEATURE = hci.LeFeature.CONNECTION_SUBRATING_HOST_SUPPORT
PEER_FEATURES = int(
    hci.LeFeatureMask.CONNECTION_SUBRATING
    | hci.LeFeatureMask.EXTENDED_REJECT_INDICATION
    | hci.LeFeatureMask.PERIPHERAL_INITIATED_FEATURE_EXCHANGE
)
SubrateChange = hci.HCI_LE_Subrate_Change_Event
ConnectionUpdate = hci.HCI_LE_Connection_Update_Complete_Event
COUNTER_PERIOD = LL_EVENT_COUNTER_MASK + 1

# Table 4.3-65. Timeout fields below and in all commands are in 10 ms.
INVALID_REQUESTS = (
    (8, 8, 1, 3, 500),
    (1, 2, 1, 3, 500),
    (4, 6, 8, 3, 1200),
    (4, 4, 1, 3, 1681),
    (4, 2, 1, 3, 500),
    (6, 6, 1, 3, 239),
)

# Table 4.3-47. Each item describes the transmitted payload at one anchor.
# None marks an unused event, and False marks an empty LL Data PDU.
CONTINUATION_PATTERN = (
    True,
    False,
    True,
    False,
    False,
    True,
    True,
    True,
    False,
    False,
    False,
    None,
    True,
    False,
    False,
    False,
    None,
    None,
    False,
    None,
    None,
    None,
    None,
    None,
    True,
)


@dataclass(frozen=True)
class Subrate:
    factor: int
    base: int = 0
    latency: int = 0
    continuation: int = 0
    timeout: int = 500

    def regular(self, counter):
        return (counter - self.base) % self.factor == 0

    def pdu(self):
        return bytes([Opcode.LL_SUBRATE_IND]) + struct.pack(
            '<HHHHH',
            self.factor,
            self.base,
            self.latency,
            self.continuation,
            self.timeout,
        )

    @classmethod
    def read(cls, pdu):
        assert pdu[0] == Opcode.LL_SUBRATE_IND
        return cls(*struct.unpack('<HHHHH', pdu[1:]))

    def after_wrap(self):
        return replace(self, base=(self.base - COUNTER_PERIOD) % self.factor)


class SubratingPeer(PeerLink):
    """Lower Tester that can drop an acknowledgment or inject a bad CRC."""

    mute = False
    bad_crc = False
    withhold_indication = False

    async def exchange(
        self,
        *args,
        mute=False,
        bad_crc=False,
        withhold_indication=False,
        watch_control=None,
        **kwargs,
    ):
        self.mute = mute
        self.bad_crc = bad_crc
        self.withhold_indication = withhold_indication
        channel = self.channel
        try:
            packet = await super().exchange(*args, **kwargs)
            if watch_control is not None and packet is not None:
                assert self.central
                packet = await self._receive_pending_control(
                    channel, packet, watch_control
                )
            return packet
        finally:
            self.mute = self.bad_crc = self.withhold_indication = False

    async def _receive_pending_control(self, channel, packet, opcode):
        # The Central can send the indication after ACL data at the same anchor.
        while packet.pdu[0] & PDU_DATA_MD_MASK and control_payload(packet)[:1] != bytes(
            [opcode]
        ):
            response = await self.tester.receive(
                channel,
                duration_us=1000,
                **(self.radio_parameters | {'phy': self.rx_phy}),
            )
            if response is None:
                break
            assert response.valid_crc
            assert response.start_us == self.last_sent_end_us + LL_IFS_US
            self._accept(response)
            await self._send(
                channel, b'', LlId.CONTINUATION, response.end_us + LL_IFS_US
            )
            packet = response
        return packet

    def _accept(self, packet):
        super()._accept(packet)
        if self.withhold_indication and control_payload(packet)[:1] == bytes(
            [Opcode.LL_SUBRATE_IND]
        ):
            self.mute = True

    async def _send(self, channel, data, llid, start):
        if self.mute:
            return
        if not self.bad_crc:
            return await super()._send(channel, data, llid, start)
        header = llid | self.sn * PDU_DATA_SN_MASK | self.nesn * PDU_DATA_NESN_MASK
        end = await self.tester.transmit(
            channel,
            bytes([header, len(data)]) + data,
            start_us=start,
            bad_crc=True,
            **self.radio_parameters,
        )
        self.last_sent_end_us = end


async def connect(
    tester, *, central, host=True, exchange=True, interval=8, timeout=500, csa2=False
):
    await tester.command(hci.HCI_LE_Set_Host_Feature_Command(HOST_FEATURE, int(host)))
    link = await establish(
        tester,
        central=central,
        interval=interval,
        timeout=timeout,
        csa2=csa2,
        peer_address=(
            LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
        ),
        peer_factory=SubratingPeer,
    )
    if exchange:
        await exchange_features(link, PEER_FEATURES | (1 << HOST_FEATURE))
    return link


def control_payload(packet):
    if packet is not None and packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL:
        return packet.pdu[PDU_HEADER_SIZE:]
    return b''


def request_pdu(minimum, maximum, latency, continuation, timeout):
    return bytes([Opcode.LL_SUBRATE_REQ]) + struct.pack(
        '<HHHHH', minimum, maximum, latency, continuation, timeout
    )


async def wait_control(link, opcode, *, factor=1, forbidden=(), **options):
    for _ in range(8 * factor):
        counter = link.counter
        packet = await link.exchange(required=False, **options)
        payload = control_payload(packet)
        assert not payload or payload[0] not in forbidden
        if payload and payload[0] == opcode:
            return counter, payload
    raise AssertionError(f'{opcode.name} was not received')


def queue_data(tester, link, data):
    tester.send(
        bytes(
            hci.HCI_AclDataPacket(
                link.handle, hci.HCI_ACL_PB_FIRST_NON_FLUSHABLE, 0, len(data), data
            )
        )
    )


def no_event(tester, event_type=SubrateChange):
    assert not any(isinstance(event, event_type) for event in tester.events)


async def subrate_event(tester, link, rate=None, status=ErrorCode.SUCCESS):
    event = await tester.event(SubrateChange)
    assert event.connection_handle == link.handle and event.status == status
    if rate is not None:
        assert (
            event.subrate_factor,
            event.peripheral_latency,
            event.continuation_number,
            event.supervision_timeout,
        ) == (rate.factor, rate.latency, rate.continuation, rate.timeout)


async def central_request(
    tester, link, values, *, old=Subrate(1), withhold=False, same_allowed=False
):
    await tester.command(hci.HCI_LE_Subrate_Request_Command(link.handle, *values))
    for _ in range(8 * old.factor):
        counter = link.counter
        packet = await link.exchange(
            required=False,
            withhold_indication=withhold,
            watch_control=Opcode.LL_SUBRATE_IND,
        )
        payload = control_payload(packet)
        if payload and payload[0] == Opcode.LL_SUBRATE_IND:
            assert old.regular(counter)
            rate = Subrate.read(payload)
            minimum, maximum, latency, continuation, timeout = values
            assert minimum <= rate.factor <= maximum
            assert rate.latency <= latency
            assert rate.continuation == min(continuation, rate.factor - 1)
            assert rate.timeout == timeout
            assert rate.base >> 14 == counter >> 14
            return rate
        if same_allowed and any(
            isinstance(event, SubrateChange) for event in tester.events
        ):
            return old
    raise AssertionError('LL_SUBRATE_IND was not received')


async def set_central_rate(tester, link, values, *, old=Subrate(1)):
    rate = await central_request(tester, link, values, old=old)
    await subrate_event(tester, link, rate)
    return rate


async def set_peripheral_rate(tester, link, rate, *, report=True):
    packet = await link.exchange(rate.pdu(), llid=LlId.CONTROL, check_ack=True)
    assert control_payload(packet)[:1] != bytes([Opcode.LL_REJECT_EXT_IND])
    if report:
        await subrate_event(tester, link, rate)
    else:
        no_event(tester)
    return rate


async def to_regular(link, rate):
    while not rate.regular(link.counter):
        await link.exchange(required=False)


async def check_slots(link, rate, count, *, latency=0, host_data=False, empty=False):
    skipped = 0
    received = 0
    for _ in range(count):
        expected = rate.regular(link.counter)
        if expected and host_data:
            queue_data(link.tester, link, b'\x01\x02')
        packet = await link.exchange(required=False)
        if not expected:
            assert packet is None, 'PDU seen between subrated events'
        elif packet is None:
            skipped += 1
            assert skipped <= latency, 'missed too many subrated events'
        else:
            skipped = 0
            received += 1
            if empty:
                assert packet.pdu[1] == 0
    return received


async def disconnect(tester, link, rate=Subrate(1)):
    reason = ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    await tester.command(hci.HCI_Disconnect_Command(link.handle, reason))
    _, payload = await wait_control(link, Opcode.LL_TERMINATE_IND, factor=rate.factor)
    assert payload == bytes([Opcode.LL_TERMINATE_IND, reason])
    if not link.central:
        await to_regular(link, rate)
        await link.exchange(required=False)
    event = await tester.event(hci.HCI_Disconnection_Complete_Event)
    assert event.connection_handle == link.handle
    assert event.reason == ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR


async def apply_interval(tester, link, rate, payload, *, completion):
    size, offset, interval, latency, timeout, instant = struct.unpack(
        '<BHHHHH', payload[1:]
    )
    assert size > 0 and offset <= interval
    while link.counter != instant:
        before = (instant - link.counter) & LL_EVENT_COUNTER_MASK
        assert 0 < before <= LL_EVENT_COUNTER_MASK // 2
        packet = await link.exchange(required=False)
        if before == 1:
            assert packet is not None, 'missing event before connection update Instant'
        no_event(tester, ConnectionUpdate)
    interval_changed = interval != link.parameters.interval
    link.anchor += offset * LL_CONN_INTERVAL_UNIT_US
    link.parameters = replace(
        link.parameters,
        interval=interval,
        timeout=timeout,
        latency=latency,
        window_size=size,
        window_offset=offset,
    )
    packet = await link.exchange(
        required=False, window_us=size * LL_CONN_INTERVAL_UNIT_US if link.central else 0
    )
    assert packet is not None, 'missing event at connection update Instant'
    if completion:
        event = await tester.event(ConnectionUpdate)
        assert (
            event.status == ErrorCode.SUCCESS and event.connection_handle == link.handle
        )
        assert (
            event.connection_interval,
            event.peripheral_latency,
            event.supervision_timeout,
        ) == (interval, latency, timeout)
    else:
        no_event(tester, ConnectionUpdate)
    no_event(tester)
    if interval_changed:
        return Subrate(1, timeout=timeout, latency=latency)
    return replace(rate, timeout=timeout, latency=latency)


async def central_interval(tester, link, rate, interval, timeout):
    unchanged = (interval, timeout) == (link.parameters.interval, rate.timeout)
    await tester.command(
        hci.HCI_LE_Connection_Update_Command(
            link.handle, interval, interval, rate.latency, timeout, 0, 0
        )
    )
    for _ in range(8 * rate.factor):
        packet = await link.exchange(required=False)
        payload = control_payload(packet)
        if payload and payload[0] == Opcode.LL_CONNECTION_UPDATE_IND:
            assert struct.unpack('<BHHHHH', payload[1:])[2:5] == (
                interval,
                rate.latency,
                timeout,
            )
            return await apply_interval(tester, link, rate, payload, completion=True)
        if unchanged and any(
            isinstance(event, ConnectionUpdate) for event in tester.events
        ):
            event = await tester.event(ConnectionUpdate)
            assert (
                event.status == ErrorCode.SUCCESS
                and event.connection_handle == link.handle
            )
            return rate
    raise AssertionError('LL_CONNECTION_UPDATE_IND was not received')


async def continuation_slots(link, rate, count, *, host_data=False):
    """Derive allowed events from the non-empty PDUs actually seen on air."""
    await to_regular(link, rate)
    last_data = None
    for counter in range(count):
        regular = rate.regular(link.counter)
        if regular:
            last_data = None
            if host_data:
                queue_data(link.tester, link, b'\x01\x02')
        eligible = regular or (
            last_data is not None and counter - last_data <= rate.continuation
        )
        packet = await link.exchange(required=False)
        assert (packet is not None) == eligible
        if packet is not None and packet.pdu[1]:
            last_data = counter


async def central_host_request(tester):
    """Change the subrate of an existing Central connection.

    Initial Condition:
        Central IUT at 10 ms, factor 2, continuation zero. Both Hosts
        advertise subrating support. The Lower Tester provides its 20 ppm SCA.

    Test Steps:
        1. For each round in Table 4.3-64, request the selected connection interval.
        2. Supply ACL data and request the specified Subrate range, latency,
           and timeout. Receive the indication on a current subrated event.
        3. Acknowledge the indication, check the HCI values, and observe the
           following five subrated cycles.

    Expected Outcome:
        The indication uses valid parameters and the Host is notified after
        acknowledgment. Transmissions subsequently follow the new phase.

    Reference:
        LL.TS.p28, pages 4.3-387 through 4.3-390, Figure 4.3-194, Table 4.3-64.
    """
    await tester.command(hci.HCI_LE_Modify_Sleep_Clock_Accuracy_Command(0))
    link = await connect(tester, central=True, timeout=3200)
    await link.control(
        Opcode.LL_CLOCK_ACCURACY_RSP, bytes([Opcode.LL_CLOCK_ACCURACY_REQ, 7])
    )
    rate = await set_central_rate(tester, link, (2, 2, 0, 0, 1500))
    rounds = (
        (3, 6, 1, 1500, 80),
        (6, 10, 2, 1500, 80),
        (2, 5, 1, 1500, 80),
        (4, 6, 1, 1500, 80),
        (5, 5, 2, 1500, 80),
        (207, 207, 1, 3200, 8),
        (456, 456, 0, 3200, 8),
        (500, 500, 0, 3200, 8),
    )
    for minimum, maximum, latency, timeout, interval in rounds:
        rate = await central_interval(tester, link, rate, interval, timeout)
        queue_data(tester, link, b'\x01\x02')
        rate = await set_central_rate(
            tester, link, (minimum, maximum, latency, 0, timeout), old=rate
        )
        await check_slots(link, rate, 5 * rate.factor, host_data=True)
    await disconnect(tester, link, rate)


async def central_peer_request(tester):
    """Accept a Peripheral's request and suppress a duplicate Host notification.

    Initial Condition:
        Both Hosts support subrating. The Central defaults allow factor 1–500,
        latency and continuation zero, and supervision timeout up to 20 s.

    Test Steps:
        1. Establish an ACL and supply ACL data. The Lower Tester requests
           factor 2–4, acknowledges the indication, and checks five cycles.
        2. Repeat the request with the applied factor and unchanged parameters.

    Expected Outcome:
        Each request produces an indication with valid parameters. Only the
        changed parameters produce a Host event. RF uses the new phase.

    Reference:
        LL.TS.p28, pages 4.3-390 through 4.3-392, Figure 4.3-195.
    """
    await tester.command(hci.HCI_LE_Set_Default_Subrate_Command(1, 500, 0, 0, 2000))
    link = await connect(tester, central=True)
    queue_data(tester, link, b'\x01\x02')
    await link.exchange(request_pdu(2, 4, 0, 0, 2000), llid=LlId.CONTROL)
    _, payload = await wait_control(link, Opcode.LL_SUBRATE_IND)
    rate = Subrate.read(payload)
    assert 2 <= rate.factor <= 4 and rate.continuation == rate.latency == 0
    assert rate.timeout == 2000
    await subrate_event(tester, link, rate)
    await check_slots(link, rate, 5 * rate.factor, host_data=True)
    await to_regular(link, rate)
    await link.exchange(
        request_pdu(rate.factor, rate.factor, 0, 0, rate.timeout), llid=LlId.CONTROL
    )
    _, payload = await wait_control(link, Opcode.LL_SUBRATE_IND, factor=rate.factor)
    assert replace(Subrate.read(payload), base=rate.base) == rate
    await tester.wait(0)
    no_event(tester)
    await disconnect(tester, link, Subrate.read(payload))


async def reject_requests(link, entries, rate=Subrate(1)):
    for values in entries:
        await to_regular(link, rate)
        await link.exchange(request_pdu(*values), llid=LlId.CONTROL)
        _, payload = await wait_control(
            link, Opcode.LL_REJECT_EXT_IND, factor=rate.factor
        )
        assert len(payload) == 3 and payload[1] == Opcode.LL_SUBRATE_REQ
        no_event(link.tester)


async def central_invalid_requests(tester):
    """Reject out-of-range subrate requests without changing the schedule.

    Initial Condition:
        Central IUT on a 100 ms ACL. Both Hosts support connection subrating.

    Test Steps:
        1. Set per-link limits to 4–7, latency 7, continuation 3, timeout
           16.8 s and accept the Controller's resulting indication.
        2. Send the six LL_SUBRATE_REQ variants of Table 4.3-65.

    Expected Outcome:
        Each invalid request receives LL_REJECT_EXT_IND and produces no
        HCI LE Subrate Change. The negotiated phase remains in use.

    Reference:
        LL.TS.p28, pages 4.3-392 through 4.3-394, Figure 4.3-196, Table 4.3-65.
    """
    link = await connect(tester, central=True, interval=80)
    rate = await set_central_rate(tester, link, (4, 7, 7, 3, 1680))
    await continuation_slots(link, rate, 2 * rate.factor, host_data=True)
    await reject_requests(link, INVALID_REQUESTS, rate)
    await continuation_slots(link, rate, 2 * rate.factor, host_data=True)
    await disconnect(tester, link, rate)


async def central_feature_exchange(tester, *, peer_supports):
    """Determine Peripheral Host support before sending LL_SUBRATE_IND.

    Initial Condition:
        Central IUT on an ACL without prior Feature Exchange. The Central
        Host enables Connection Subrating.

    Test Case Configuration:
        LL/CON/CEN/BV-136-C: the Peripheral Host feature is set.
        LL/CON/CEN/BV-143-C: the Peripheral Host feature is clear.

    Test Steps:
        1. The Central Host requests subrating.
        2. Require LL_FEATURE_REQ before any indication and reply with the
           selected Peripheral FeatureSet.
        3. For the supported peer, acknowledge LL_SUBRATE_IND and check the
           successful HCI result. Otherwise require Unsupported Remote Feature.

    Expected Outcome:
        The exchange completes before a Central indication. An unsupported
        Peripheral produces status 0x1A and receives no indication.

    Reference:
        LL.TS.p28, pages 4.3-394 through 4.3-395 and 4.3-410 through
        4.3-411, Figures 4.3-197 and 4.3-205.
    """
    link = await connect(tester, central=True, exchange=False)
    values = (3, 5, 1, 2, 500)
    await tester.command(hci.HCI_LE_Subrate_Request_Command(link.handle, *values))
    _, request = await wait_control(
        link, Opcode.LL_FEATURE_REQ, forbidden=(Opcode.LL_SUBRATE_IND,)
    )
    advertised = int.from_bytes(request[1:], 'little')
    assert advertised & (1 << HOST_FEATURE)
    mask = PEER_FEATURES | (int(peer_supports) << HOST_FEATURE)
    await link.exchange(
        bytes([Opcode.LL_FEATURE_RSP]) + mask.to_bytes(8, 'little'), llid=LlId.CONTROL
    )
    rate = Subrate(1)
    if peer_supports:
        _, payload = await wait_control(link, Opcode.LL_SUBRATE_IND)
        rate = Subrate.read(payload)
        assert 3 <= rate.factor <= 5 and rate.latency <= 1
        assert rate.continuation == min(2, rate.factor - 1)
        assert rate.timeout == 500
        await subrate_event(tester, link, rate)
    else:
        await subrate_event(
            tester, link, status=ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
        )
        for _ in range(5):
            assert control_payload(await link.exchange())[:1] != bytes(
                [Opcode.LL_SUBRATE_IND]
            )
    await disconnect(tester, link, rate)


async def central_defaults(tester):
    """Use host defaults to accept only compatible future peer requests.

    Initial Condition:
        Standby. Both Hosts enable Connection Subrating.

    Test Steps:
        1. For each row of Table 4.3-66, set the Central defaults and
           establish a 100 ms ACL. Check for no unsolicited indication.
        2. Send the invalid requests in Table 4.3-65, followed by the valid
           request from Table 4.3-66. Acknowledge the indication.
        3. Check the Host event, observe twenty cycles and disconnect.

    Expected Outcome:
        Invalid requests are rejected without notifying the Host. The
        negotiated factor is inside both ranges and continuation is clamped.

    Reference:
        LL.TS.p28, pages 4.3-396 through 4.3-399, Figure 4.3-198,
        Tables 4.3-65 and 4.3-66.
    """
    rounds = (
        (3, 6, 3, 3, 6, 4),
        (4, 8, 3, 6, 10, 4),
        (7, 12, 3, 4, 10, 4),
        (4, 7, 6, 5, 10, 9),
        (4, 10, 3, 6, 8, 4),
        (5, 12, 7, 11, 15, 4),
        (5, 12, 7, 11, 15, 9),
        (5, 12, 7, 3, 15, 2),
    )
    for local_min, local_max, local_cont, peer_min, peer_max, peer_cont in rounds:
        await tester.command(
            hci.HCI_LE_Set_Default_Subrate_Command(
                local_min, local_max, 1, local_cont, 400
            )
        )
        link = await connect(tester, central=True, interval=80)
        queue_data(tester, link, b'\x01\x02')
        for _ in range(3):
            assert control_payload(await link.exchange())[:1] != bytes(
                [Opcode.LL_SUBRATE_IND]
            )
        await reject_requests(link, INVALID_REQUESTS)
        await link.exchange(
            request_pdu(peer_min, peer_max, 0, peer_cont, 400), llid=LlId.CONTROL
        )
        _, payload = await wait_control(link, Opcode.LL_SUBRATE_IND)
        rate = Subrate.read(payload)
        assert max(local_min, peer_min) <= rate.factor <= min(local_max, peer_max)
        assert rate.latency == 0 and rate.timeout == 400
        assert rate.continuation == min(max(local_cont, peer_cont), rate.factor - 1)
        await subrate_event(tester, link, rate)
        await continuation_slots(link, rate, 20 * rate.factor, host_data=True)
        await disconnect(tester, link, rate)


async def central_transition(tester):
    """Retransmit on old and new subrated events until acknowledgment.

    Initial Condition:
        Central IUT at 100 ms, factor 10, continuation zero and timeout 25 s.
        Both Hosts support connection subrating.

    Test Steps:
        1. Request each set of parameters in Table 4.3-67.
        2. Discard the acknowledgment of LL_SUBRATE_IND and observe the
           union of old and new events for three least common multiples.
        3. Acknowledge an identical retransmission and check the Host event.
           Observe at least five cycles using the new schedule.

    Expected Outcome:
        Retransmissions preserve the indication on both sets of events.
        No successful HCI event appears before the acknowledgment, and
        ordinary traffic then uses only the new schedule.

    Reference:
        LL.TS.p28, pages 4.3-399 through 4.3-402, Figure 4.3-199, Table 4.3-67.
    """
    link = await connect(tester, central=True, interval=80, timeout=2500)
    old = await set_central_rate(tester, link, (10, 10, 0, 0, 2500))
    rounds = ((7, 0, 0), (9, 0, 0), (3, 3, 1), (6, 4, 0), (6, 4, 0), (4, 2, 3))
    for index, (factor, latency, continuation) in enumerate(rounds):
        queue_data(tester, link, b'\x01\x02')
        rate = await central_request(
            tester,
            link,
            (factor, factor, latency, continuation, 2500),
            old=old,
            withhold=True,
            same_allowed=index == 4,
        )
        if rate is old:
            await subrate_event(tester, link, rate)
            continue
        for _ in range(3 * math.lcm(old.factor, rate.factor) - 1):
            counter = link.counter
            packet = await link.exchange(required=False, mute=True)
            if old.regular(counter) or rate.regular(counter):
                assert control_payload(packet) == rate.pdu()
            elif packet is not None:
                assert rate.continuation and control_payload(packet) == rate.pdu()
            no_event(tester)
        await wait_control(link, Opcode.LL_SUBRATE_IND, factor=rate.factor)
        await subrate_event(tester, link, rate)
        await continuation_slots(link, rate, 5 * rate.factor, host_data=True)
        old = rate
    await disconnect(tester, link, old)


async def connection_interval_change(tester, *, central):
    """Keep the subrate on an anchor/timeout change and reset it on a new interval.

    Initial Condition:
        100 ms ACL. Start with factor 4 and continuation 2 for Central;
        Peripheral uses the factor and continuation defined by the Lower Tester.

    Test Case Configuration:
        LL/CON/CEN/BV-139-C: Central IUT, connection updates from the Host.
        LL/CON/PER/BV-139-C: Peripheral IUT, connection updates from the peer.

    Test Steps:
        1. Update while retaining the original connection interval.
           Queue Host ACL data at subrated anchors to verify that
           the RF phase and continuation events persist after the Instant.
        2. Update to 7.5 ms (Central) or 75 ms (Peripheral).
           Observe participation in the event before the Instant, the
           event at the Instant, and the following full-rate events.

    Expected Outcome:
        Same-interval updates preserve the RF phase and continuation.
        A real interval change restores factor 1 and continuation zero
        without an HCI LE Subrate Change event. The Peripheral attends
        both special events even if they fall between subrated events.

    Reference:
        LL.TS.p28, pages 4.3-241 through 4.3-245 and 4.3-402 through
        4.3-405, Figures 4.3-114/115 and 4.3-200/201; Core 6, Part B, §5.1.1.
    """
    link = await connect(tester, central=central, interval=80)
    if central:
        rate = await set_central_rate(tester, link, (4, 4, 0, 2, 500))
        await continuation_slots(link, rate, 8, host_data=True)
        rate = await central_interval(tester, link, rate, 80, 750)
        await continuation_slots(link, rate, 16, host_data=True)
        rate = await central_interval(tester, link, rate, 6, 300)
        assert await check_slots(link, rate, 100, latency=5, empty=True) >= 95
    else:
        rate = await set_peripheral_rate(
            tester, link, Subrate(4, link.counter, continuation=2)
        )
        for interval, completion in ((80, False), (60, True)):
            await continuation_slots(link, rate, 2 * rate.factor, host_data=True)
            await to_regular(link, rate)
            instant = (link.counter + 7) & LL_EVENT_COUNTER_MASK
            assert not rate.regular(instant) and not rate.regular(instant - 1)
            payload = bytes([Opcode.LL_CONNECTION_UPDATE_IND]) + struct.pack(
                '<BHHHHH', 1, 0, interval, rate.latency, rate.timeout, instant
            )
            await link.exchange(payload, llid=LlId.CONTROL, check_ack=True)
            rate = await apply_interval(
                tester, link, rate, payload, completion=completion
            )
        await check_slots(link, rate, 20)
    await disconnect(tester, link, rate)


async def central_host_feature_clear(tester):
    """Reject the peer's request when the local Host Support bit is clear.

    Initial Condition:
        Central ACL. The Upper Tester clears the Connection Subrating
        Host Support bit.

    Test Steps:
        1. Send LL_SUBRATE_REQ from the Lower Tester.
        2. Receive LL_REJECT_EXT_IND for that opcode.

    Expected Outcome:
        The request is rejected without a subrate change.

    Reference:
        LL.TS.p28, page 4.3-406, Figure 4.3-202.
    """
    link = await connect(tester, central=True, host=False)
    await reject_requests(link, ((1, 1, 0, 0, 500),))
    await disconnect(tester, link)


async def run_to_wrap(link, rate):
    await check_slots(link, rate, COUNTER_PERIOD - link.counter, empty=True)


async def counter_wrap(tester, *, central):
    """Maintain the subrate phase when the 16-bit link counter wraps.

    Initial Condition:
        10 ms ACL with Channel Selection Algorithm #2. The Lower Tester
        participates in every suitable event.

    Test Case Configuration:
        LL/CON/CEN/BV-141-C: Central chooses the base for factor 29.
        LL/CON/PER/BV-140-C: Peripheral receives base 12345 for factor 29.

    Test Steps:
        1. Enable factor 29 and check the successful Host notification.
        2. Watch each event until the event counter wraps from 65535 to 0.
        3. Continue for ten subrated events after the wrap.

    Expected Outcome:
        The regular-event phase is continuous across the wrap. Active
        events carry empty data PDUs; unused events carry no DUT traffic.

    Reference:
        LL.TS.p28, pages 4.3-245 through 4.3-247 and 4.3-406 through
        4.3-408, Figures 4.3-116 and 4.3-203.
    """
    link = await connect(tester, central=central, csa2=True)
    if central:
        rate = await set_central_rate(tester, link, (29, 29, 0, 0, 500))
    else:
        rate = await set_peripheral_rate(tester, link, Subrate(29, 12345))
    await run_to_wrap(link, rate)
    rate = rate.after_wrap()
    count = await check_slots(
        link, rate, 10 * rate.factor, latency=2 if central else 0, empty=True
    )
    if central:
        assert count >= 8
    else:
        assert count == 10 and rate.base == 24
    await disconnect(tester, link, rate)


async def delayed_wrap_update(tester):
    """Apply a pre-wrap indication first received after the counter wraps.

    Initial Condition:
        Peripheral IUT at 10 ms with supervision timeout 5 s, factor 10,
        continuation zero, a base divisible by ten and Channel Selection
        Algorithm #2.

    Test Steps:
        1. Send empty Lower Tester PDUs on each event and observe the
           scheduled Peripheral responses while maintaining ACL data.
        2. Starting at e65531, send and retry LL_SUBRATE_IND for factor 3,
           base 65500, until the IUT acknowledges.
        3. Check the Host notification and inspect events on the new phase.

    Expected Outcome:
        The first acknowledgment follows the wrap. The resulting active
        events have counters divisible by three.

    Reference:
        LL.TS.p28, pages 4.3-250 through 4.3-253, Figures 4.3-119/120.
    """
    link = await connect(tester, central=False, csa2=True)
    old = await set_peripheral_rate(tester, link, Subrate(10))
    await check_slots(link, old, 65531 - link.counter, host_data=True)
    rate = Subrate(3, 65500)
    assert link.counter == 65531
    for attempt in range(10):
        counter = link.counter
        packet = await link.exchange(rate.pdu(), llid=LlId.CONTROL, required=False)
        if packet is not None:
            assert attempt == 9 and counter == 4
            break
        no_event(tester)
    else:
        raise AssertionError('pre-wrap indication was not acknowledged')
    await subrate_event(tester, link, rate)
    rate = rate.after_wrap()
    assert rate.base == 0
    await check_slots(link, rate, 5 * rate.factor, host_data=True)
    await disconnect(tester, link, rate)


async def peripheral_invalid_updates(tester):
    """Reject invalid incoming LL_SUBRATE_IND without changing subrating.

    Initial Condition:
        Peripheral IUT at 10 ms and factor 1; choose the LL_REJECT_EXT_IND
        branch from the valid source alternatives.

    Test Steps:
        1. Send all five invalid indications from Table 4.3-48.
        2. Check that the Controller rejects each indication without
           reporting an HCI LE Subrate Change.
        3. Check full-rate packet exchange and send an invalid HCI
           connection interval request to confirm the live link.

    Expected Outcome:
        Invalid indications do not affect the active parameters. At least
        18 of 20 full-rate events have a response and the Host command fails.

    Reference:
        LL.TS.p28, pages 4.3-253 through 4.3-254, Figure 4.3-121, Table 4.3-48.
    """
    link = await connect(tester, central=False)
    for rate in (
        Subrate(0),
        Subrate(501),
        Subrate(10, continuation=10),
        Subrate(1, latency=500),
        Subrate(5, latency=10, timeout=10),
    ):
        packet = await link.exchange(rate.pdu(), llid=LlId.CONTROL)
        payload = control_payload(packet)
        if payload[:1] != bytes([Opcode.LL_REJECT_EXT_IND]):
            _, payload = await wait_control(link, Opcode.LL_REJECT_EXT_IND)
        assert len(payload) == 3 and payload[1] == Opcode.LL_SUBRATE_IND
        no_event(tester)
    assert await check_slots(link, Subrate(1), 20, latency=2) >= 18
    result = await tester.command(
        hci.HCI_LE_Connection_Update_Command(link.handle, 0x10, 0x08, 0, 500, 0, 0),
        (ErrorCode.SUCCESS, ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
    )
    if result.status == ErrorCode.SUCCESS:
        assert (await tester.event(ConnectionUpdate)).status != ErrorCode.SUCCESS
    await disconnect(tester, link)


async def peripheral_responds(tester):
    """Apply peer indications and acknowledge the exact subrated anchors.

    Initial Condition:
        Peripheral IUT with an unencrypted ACL. Use a 10 ms interval and
        32 s timeout for all of Table 4.3-45, including factor 500.

    Test Steps:
        1. Queue Host ACL data and send every indication of Table 4.3-45.
           Select random bases with a fixed seed and match the current
           counter's two upper bits.
        2. Require the HCI notification in rounds 1–7 and no event
           when only the phase changes in rounds 8–13.
        3. Send one empty PDU on each anchor for five subrated cycles.

    Expected Outcome:
        Every indication is acknowledged. The IUT exchanges data at
        the new subrated anchors and never at unrelated anchors.

    Reference:
        LL.TS.p28, pages 4.3-237 through 4.3-239, Figure 4.3-112, Table 4.3-45.
    """
    link = await connect(tester, central=False, timeout=3200)
    random_bases = random.Random(137)
    rate = Subrate(1)
    rounds = (
        *((factor, None) for factor in (3, 7, 10, 207, 456, 500)),
        *((12, phase) for phase in (0, 3, 2, 8, 7)),
        (12, 12 * random_bases.randrange(1, 600)),
        (12, 12 * random_bases.randrange(601, 1200)),
    )
    for index, (factor, phase) in enumerate(rounds):
        await to_regular(link, rate)
        quarter = link.counter & 0xC000
        base = (
            quarter + random_bases.randrange(0x4000)
            if phase is None
            else phase + max(0, math.ceil((quarter - phase) / factor)) * factor
        )
        assert base >> 14 == link.counter >> 14
        queue_data(tester, link, b'\x01\x02')
        rate = await set_peripheral_rate(
            tester, link, Subrate(factor, base, timeout=3200), report=index < 7
        )
        await check_slots(link, rate, 5 * factor, host_data=True)
        no_event(tester)
    await disconnect(tester, link, rate)


async def peripheral_host_request(tester):
    """Make a Peripheral Host request and apply the Central's accepted indication.

    Initial Condition:
        Peripheral IUT with a supporting Central peer and an unencrypted ACL.

    Test Steps:
        1. Issue all seven HCI requests in Table 4.3-46 and check
           LL_SUBRATE_REQ against each requested range.
        2. Monitor the old schedule, then send LL_SUBRATE_IND with valid
           Central-selected parameters and check HCI LE Subrate Change.
        3. Probe five cycles on the newly selected phase.

    Expected Outcome:
        Valid requests reach the Central. The Host event matches the
        acknowledged indication and the scheduled responses follow its phase,
        allowing the negotiated Peripheral latency.

    Reference:
        LL.TS.p28, pages 4.3-239 through 4.3-241, Figure 4.3-113, Table 4.3-46.
    """
    link = await connect(tester, central=False)
    rate = Subrate(1)
    random_choice = random.Random(138)
    rounds = (
        (2, 7, 2),
        (4, 9, 2),
        (1, 3, 4),
        (3, 4, 3),
        (5, 5, 3),
        (3, 10, 1),
        (3, 10, 1),
    )
    for index, (minimum, maximum, latency) in enumerate(rounds):
        queue_data(tester, link, b'\x01\x02')
        await tester.command(
            hci.HCI_LE_Subrate_Request_Command(
                link.handle, minimum, maximum, latency, 0, 500
            )
        )
        _, payload = await wait_control(link, Opcode.LL_SUBRATE_REQ, factor=rate.factor)
        req_min, req_max, req_latency, req_cont, req_timeout = struct.unpack(
            '<HHHHH', payload[1:]
        )
        assert minimum <= req_min <= req_max <= maximum
        assert req_latency <= latency and req_cont == 0 and req_timeout == 500
        await check_slots(
            link, rate, 5 * rate.factor, latency=rate.latency, host_data=True
        )
        possible = list(range(req_min, req_max + 1))
        if index == 3:
            if rate.factor in possible:
                possible.remove(rate.factor)
        factor = rate.factor if index == 6 else random_choice.choice(possible)
        assert factor in possible
        await to_regular(link, rate)
        base = (link.counter & 0xC000) + random_choice.randrange(0x4000)
        rate = await set_peripheral_rate(
            tester, link, Subrate(factor, base, req_latency, timeout=500)
        )
        await check_slots(
            link, rate, 5 * rate.factor, latency=rate.latency, host_data=True
        )
    await disconnect(tester, link, rate)


async def continuation_data(tester, *, central, receiving):
    """Use continuation anchors for data and keep inactive anchors silent.

    Initial Condition:
        10 ms ACL with subrate factor 6, continuation 3 and latency zero.
        Central transitions from factor 2. Peripheral accepts the peer's
        indication. Both roles check a successful HCI LE Subrate Change.

    Test Case Configuration:
        LL/CON/CEN/BV-142-C and LL/CON/PER/BV-141-C: IUT sends the data.
        LL/CON/CEN/BV-145-C and LL/CON/PER/BV-143-C: IUT receives the data.

    Test Steps:
        1. Establish the subrate and align to a subrated anchor.
        2. Execute all four Table 4.3-47 patterns. For receive tests the
           Lower Tester injects a non-empty packet with bad CRC at n+9.
        3. Check data bytes and ensure silence in the unused events.

    Expected Outcome:
        Valid non-empty PDUs extend the active continuation window.
        Empty PDUs and the corrupt packet do not extend it or deliver data
        to the Host.

    Reference:
        LL.TS.p28, Table 4.3-47, Figure 4.3-118, pages 4.3-247 through
        4.3-249, 4.3-408 through 4.3-410, and §4.3.7.40,
        pages 4.3-524 through 4.3-525.
    """
    link = await connect(tester, central=central)
    if central:
        initial = await set_central_rate(tester, link, (2, 2, 0, 0, 500))
        rate = await set_central_rate(tester, link, (6, 6, 0, 3, 500), old=initial)
        if not receiving:
            await continuation_slots(link, rate, 20 * rate.factor, host_data=True)
    else:
        rate = await set_peripheral_rate(
            tester, link, Subrate(6, link.counter, continuation=3)
        )
    await to_regular(link, rate)
    for index, kind in enumerate(CONTINUATION_PATTERN):
        data = index.to_bytes(2, 'little')
        if kind and not receiving:
            queue_data(tester, link, data)
        bad_crc = receiving and index == 9
        packet = await link.exchange(
            data if receiving and (kind or bad_crc) else b'',
            llid=LlId.START if receiving and (kind or bad_crc) else LlId.CONTINUATION,
            required=False,
            mute=receiving and central and kind is None,
            bad_crc=bad_crc,
        )
        assert (packet is not None) == (kind is not None), index
        if receiving:
            if kind:
                await received_acl(tester, link, data, False)
            elif bad_crc:
                assert not any(
                    isinstance(event, hci.HCI_AclDataPacket) for event in tester.events
                )
        elif packet is not None:
            assert packet.pdu[PDU_HEADER_SIZE:] == (data if kind else b''), index
    if receiving:
        assert not any(
            isinstance(event, hci.HCI_AclDataPacket) for event in tester.events
        )
    await disconnect(tester, link, rate)


async def peripheral_active_receive(tester):
    """Receive ACL data only on every fourth connection anchor.

    Initial Condition:
        Peripheral IUT on a 10 ms ACL, factor 4, continuation zero and
        latency zero.

    Test Steps:
        1. Send a two-byte sequence value on each of 60 events.
        2. On subrated events verify the IUT acknowledges the data and
           sends the corresponding HCI ACL packet.

    Expected Outcome:
        The Host receives 15 payloads, numbered in steps of four.
        Other anchors produce no response and no Host data.

    Reference:
        LL.TS.p28, pages 4.3-257 through 4.3-258, Figure 4.3-124.
    """
    link = await connect(tester, central=False)
    rate = await set_peripheral_rate(tester, link, Subrate(4, link.counter))
    await to_regular(link, rate)
    for index in range(15 * rate.factor):
        data = index.to_bytes(2, 'little')
        packet = await link.exchange(
            data, llid=LlId.START, required=False, check_ack=True
        )
        if index % rate.factor:
            assert packet is None
        else:
            assert packet is not None
            await received_acl(tester, link, data, False)
    assert not any(isinstance(event, hci.HCI_AclDataPacket) for event in tester.events)
    await disconnect(tester, link, rate)


# Central-originated and peer-originated Connection Subrate Update procedures.
PROCEDURES: ProcedureMap = (
    procedure_cases(
        central_host_request,
        {'LL/CON/CEN/BV-134-C': ('Change the Subrate of an Existing Connection', {})},
    )
    | procedure_cases(
        central_peer_request,
        {'LL/CON/CEN/BV-135-C': ('Accept an Incoming Subrate Change Request', {})},
    )
    | procedure_cases(
        central_invalid_requests,
        {
            'LL/CON/CEN/BI-10-C': (
                'Reject an Invalid Incoming Subrate Change Request',
                {},
            )
        },
    )
    | procedure_cases(
        central_feature_exchange,
        {
            'LL/CON/CEN/BV-136-C': (
                'Feature Exchange Before a Subrate Request',
                {'peer_supports': True},
            ),
            'LL/CON/CEN/BV-143-C': (
                'Reject Subrate Request when Peripheral Feature Bit is not set',
                {'peer_supports': False},
            ),
        },
    )
    | procedure_cases(
        central_defaults,
        {'LL/CON/CEN/BV-137-C': ('Set the Default Subrate Factor', {})},
    )
    | procedure_cases(
        central_transition,
        {
            'LL/CON/CEN/BV-138-C': (
                'Central Retransmission on Old and New Subrated Connection Events '
                'when Connection Subrate Update Not Acknowledged',
                {},
            )
        },
    )
    | procedure_cases(
        central_host_feature_clear,
        {
            'LL/CON/CEN/BV-140-C': (
                'Reject Subrate Request When Feature Bit is not set',
                {},
            )
        },
    )
)

# Connection interval changes and event counter wrap in each role.
PROCEDURES |= procedure_cases(
    connection_interval_change,
    {
        'LL/CON/CEN/BV-139-C': (
            'Subrate Set to 1 After Requesting Connection Interval Change',
            {'central': True},
        ),
        'LL/CON/PER/BV-139-C': (
            'Subrate Factor set to 1 and Continuation Number set to 0 on '
            'Connection Interval change',
            {'central': False},
        ),
    },
)
PROCEDURES |= procedure_cases(
    counter_wrap,
    {
        'LL/CON/CEN/BV-141-C': (
            'Subrate Factor, Event Counter wrapping',
            {'central': True},
        ),
        'LL/CON/PER/BV-140-C': (
            'SubrateFactor, Event Counter wrapping',
            {'central': False},
        ),
    },
)
PROCEDURES |= procedure_cases(
    delayed_wrap_update,
    {
        'LL/CON/PER/BV-144-C': (
            'Receiving a Delayed Connection Change Update After the '
            'Event Counter Wraps',
            {},
        )
    },
)

# Peripheral validation and transmission/reception at the negotiated anchors.
PROCEDURES |= procedure_cases(
    peripheral_invalid_updates,
    {'LL/CON/PER/BI-13-C': ('Reject Invalid Subrate Updates', {})},
)
PROCEDURES |= procedure_cases(
    peripheral_responds,
    {'LL/CON/PER/BV-137-C': ('Respond to a Subrate Change', {})},
)
PROCEDURES |= procedure_cases(
    peripheral_host_request,
    {'LL/CON/PER/BV-138-C': ('Send a Request for a Subrate Change, Accepted', {})},
)
PROCEDURES |= procedure_cases(
    continuation_data,
    {
        'LL/CON/CEN/BV-142-C': (
            'Change the Subrate of an Existing Connection, Verify Sending Data, '
            'Continuation Number events',
            {'central': True, 'receiving': False},
        ),
        'LL/CON/PER/BV-141-C': (
            'Subrate Change, Verify Sending Data, Continuation Number events',
            {'central': False, 'receiving': False},
        ),
        'LL/CON/CEN/BV-145-C': (
            'Receive Data on Continuation events',
            {'central': True, 'receiving': True},
        ),
        'LL/CON/PER/BV-143-C': (
            'Receive Data on Continuation events',
            {'central': False, 'receiving': True},
        ),
    },
)
PROCEDURES |= procedure_cases(
    peripheral_active_receive,
    {'LL/CON/PER/BV-142-C': ('Listen for Data on Subrated events', {})},
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_subrating(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
