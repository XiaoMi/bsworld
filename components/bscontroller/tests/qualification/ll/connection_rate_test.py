# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.3: Shorter Connection Intervals."""

import struct
from dataclasses import replace

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_MIN,
    LL_CONN_INTERVAL_UNIT_US,
    LL_EVENT_COUNTER_MASK,
    PDU_DATA_LLID_MASK,
    ControlOpcode,
    LlId,
)
from bscontroller.radio.base import Phy
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.peer import (
    PEER_FEATURES,
    PeerLink,
    establish,
    exchange_feature_page,
    exchange_features,
    phy_bit,
    received_acl,
)

Opcode = ControlOpcode
ErrorCode = hci.HCI_ErrorCode
RATE_REQ, RATE_IND = Opcode.LL_CONNECTION_RATE_REQ, Opcode.LL_CONNECTION_RATE_IND
RateRequest = hci.HCI_LE_Connection_Rate_Request_Command
RateDefaults = hci.HCI_LE_Set_Default_Rate_Parameters_Command
RateChange = hci.HCI_LE_Connection_Rate_Change_Event
SetHostFeature = hci.HCI_LE_Set_Host_Feature_V2_Command
RATE_UNIT_US = 125
NO_OFFSET = 0xFFFF
INITIAL_INTERVAL = 8
TIMEOUT = 20
PAGE_BASE = 64
SCI = 1 << (hci.LeFeature.SHORTER_CONNECTION_INTERVALS - PAGE_BASE)
SCI_HOST = 1 << (hci.LeFeature.SHORTER_CONNECTION_INTERVALS_HOST_SUPPORT - PAGE_BASE)
PACKET = bytes(range(10))
PROCEDURES = ProcedureMap()


def host_parameters(low, high, factor, latency, continuation, timeout=TIMEOUT):
    return low, high, factor, factor, latency, continuation, timeout, 1, 0x3E7F


def request(
    low=40,
    high=40,
    *,
    factor=1,
    factor_max=None,
    latency=0,
    continuation=0,
    timeout=TIMEOUT,
    periodicity=0,
    reference=0,
    offsets=(NO_OFFSET,) * 4,
):
    return struct.pack(
        '<B13H',
        RATE_REQ,
        low,
        high,
        factor,
        factor if factor_max is None else factor_max,
        latency,
        continuation,
        timeout,
        periodicity,
        reference,
        *offsets,
    )


def indication(
    interval, instant, *, factor=1, latency=0, continuation=0, timeout=TIMEOUT
):
    return struct.pack(
        '<B7H', RATE_IND, 0, interval, instant, factor, latency, continuation, timeout
    )


class RatePeer(PeerLink):
    """Lower Tester's active schedule, decoded from the over-the-air indication."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.interval_us = self.parameters.interval * LL_CONN_INTERVAL_UNIT_US
        self.factor = 1
        self.base = 0

    def skip(self):
        self.anchor += self.interval_us
        self.counter = (self.counter + 1) & LL_EVENT_COUNTER_MASK

    async def exchange(self, *args, **kwargs):
        packet = await super().exchange(*args, **kwargs)
        self.anchor += (
            self.interval_us - self.parameters.interval * LL_CONN_INTERVAL_UNIT_US
        )
        return packet

    def regular(self):
        return ((self.counter - self.base) & LL_EVENT_COUNTER_MASK) % self.factor == 0

    async def data(self):
        while not self.regular():
            self.skip()
        packet = await self.exchange(PACKET, llid=LlId.START)
        assert packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
        assert packet.pdu[1] == 0
        await received_acl(self.tester, self, PACKET, False)

    async def apply_rate(self, payload, *, monitor=True):
        opcode, offset, interval, instant, factor, latency, continuation, timeout = (
            struct.unpack('<B7H', payload)
        )
        assert opcode == RATE_IND and 0 <= offset <= interval
        distance = (instant - self.counter) & LL_EVENT_COUNTER_MASK
        assert 0 < distance < 0x8000
        while (instant - self.counter) & LL_EVENT_COUNTER_MASK > 1:
            if monitor and self.regular():
                await self.data()
            else:
                self.skip()
        if monitor:
            assert not any(
                isinstance(event, RateChange) and event.status == ErrorCode.SUCCESS
                for event in self.tester.events
            )
        # The pre-Instant event must be used regardless of the previous subrate.
        await self.exchange()
        assert self.counter == instant
        self.interval_us = interval * RATE_UNIT_US
        self.anchor += offset * RATE_UNIT_US
        self.factor, self.base = factor, instant
        if monitor:
            await self.data()
            event = await self.tester.event(RateChange)
            assert (
                event.status,
                event.connection_handle,
                event.connection_interval,
                event.subrate_factor,
                event.peripheral_latency,
                event.continuation_number,
                event.supervision_timeout,
            ) == (
                ErrorCode.SUCCESS,
                self.handle,
                interval,
                factor,
                latency,
                continuation,
                timeout,
            )

    async def finish(self):
        while not self.regular():
            self.skip()
        await self.disconnect(local=False)


async def connect(
    tester, *, central, interval=INITIAL_INTERVAL, local_host=True, peer_host=True
):
    await tester.command(
        SetHostFeature(
            hci.LeFeature.SHORTER_CONNECTION_INTERVALS_HOST_SUPPORT, int(local_host)
        )
    )
    link = await establish(
        tester,
        central=central,
        interval=interval,
        timeout=TIMEOUT,
        peer_factory=RatePeer,
    )
    await exchange_features(
        link,
        PEER_FEATURES | hci.LeFeatureMask.LL_EXTENDED_FEATURE_SET,
    )
    reported = await exchange_feature_page(link, SCI | (SCI_HOST if peer_host else 0))
    assert reported & SCI and bool(reported & SCI_HOST) == local_host
    return link


async def minimum_interval(tester):
    result = await tester.command(
        hci.HCI_LE_Read_Minimum_Supported_Connection_Interval_Command()
    )
    return result.minimum_supported_connection_interval


def rounds(minimum):
    # Tables 4.3-51, 4.3-52, 4.3-68 and 4.3-69.
    return (
        (40, 1, 0, 0),
        (40, 1, 1, 0),
        (40, 2, 0, 1),
        (20, 1, 0, 0),
        (20, 1, 1, 0),
        (20, 3, 0, 1),
        (10, 1, 0, 0),
        (10, 1, 1, 0),
        (10, 6, 0, 1),
        (minimum, 1, 0, 0),
        (20, 1, 0, 0),
        (40, 1, 0, 0),
    )


async def change_rate(tester, *, central, local):
    """Verify the requested rate, event timing, and ACL delivery across the Instant.

    Initial Condition:
        An LE 1M ACL with SCI Host Support set on both sides. Start at 7.5 ms
        for the Peripheral IUT and 10 ms for the Central IUT.

    Test Case Configuration:
        CEN/BV-174-C and PER/BV-178-C are Host initiated. CEN/BV-175-C and
        PER/BV-179-C are peer initiated. Apply the interval, latency, subrate
        factor and continuation number from each round of the respective table.

    Test Steps:
        1. Send 20 data packets under the current parameters and check that
           the IUT forwards them to the Host and sends empty data packets.
           Repeat this before each round for CEN/BV-175-C and PER/BV-178-C.
        2. Request or indicate new parameters and observe the on-air values.
           For PER/BV-178-C, set both Host interval bounds to the target on an
           increase, and construct the indication from the observed request.
        3. Exercise the old schedule up to the Instant, including the event
           just before the Instant.
        4. Verify the completion event and ACL data at the new anchors.

    Expected Outcome:
        Both endpoints continue to exchange data across the timing change. The
        successful HCI event contains the parameters in the indication.

    Source Correction:
        Use the Instant carried by the IUT rather than a fixed 15-event lead.
        Core Vol 6, Part B, 5.5 recommends lead measured in listening events.

    References:
        LL.TS.p28 4.3.5, pages 4.3-267–272, and 4.3.6, pages 4.3-415–420.
        Core Vol 6, Part B, 5.1.32–33.
    """
    minimum = await minimum_interval(tester)
    peripheral_request = not central and local
    initial_interval = INITIAL_INTERVAL if central else LL_CONN_INTERVAL_MIN
    link = None
    for interval, factor, latency, continuation in rounds(minimum):
        if central and not local:
            # Defaults take effect only on subsequent links (Core 7.8.155).
            await tester.command(
                RateDefaults(
                    *host_parameters(minimum, 60, factor, latency, continuation)
                )
            )
        new_connection = link is None
        if new_connection:
            link = await connect(tester, central=central, interval=initial_interval)
        if new_connection or peripheral_request:
            for _ in range(20):
                await link.data()
        while not link.regular():
            link.skip()
        if local:
            current = link.interval_us // RATE_UNIT_US
            low, high = min(interval, current), max(interval, current)
            if peripheral_request and interval > current:
                low = interval
            command = RateRequest(
                link.handle,
                *host_parameters(low, high, factor, latency, continuation),
            )
            # The radio exchange drives HCI admission at short anchors without
            # HciProbe's 1 ms polling advancing through a connection event.
            tester.send(bytes(command))
        if central:
            if local:
                payload = await link.control(RATE_IND)
            else:
                payload = await link.control(
                    RATE_IND,
                    request(
                        interval,
                        interval,
                        factor=factor,
                        latency=latency,
                        continuation=continuation,
                        reference=link.counter,
                    ),
                )
            selected = int.from_bytes(payload[3:5], 'little')
            if local:
                assert low <= selected <= high
            else:
                assert selected == interval
            assert struct.unpack_from('<4H', payload, 7) == (
                factor,
                latency,
                continuation,
                TIMEOUT,
            )
        else:
            timeout = TIMEOUT
            if local:
                sent = await link.control(RATE_REQ)
                interval, maximum, factor, _, latency, continuation, timeout = (
                    struct.unpack_from('<7H', sent, 1)
                )
                assert low <= interval <= maximum <= high
            instant = (link.counter + 15 * link.factor) & LL_EVENT_COUNTER_MASK
            payload = indication(
                interval,
                instant,
                factor=factor,
                latency=latency,
                continuation=continuation,
                timeout=timeout,
            )
            await link.exchange(payload, llid=LlId.CONTROL, check_ack=True)
        if local:
            status = await tester.expect(
                lambda event: isinstance(event, hci.HCI_Command_Status_Event)
                and event.command_opcode == command.op_code
            )
            assert status.status == ErrorCode.SUCCESS
        await link.apply_rate(payload)
        for _ in range(4):
            await link.data()
        if central and not local:
            await link.finish()
            link = None
    if link is not None:
        await link.finish()


async def missing_host_support(tester):
    """Reject a Peripheral rate request without both SCI Host Support bits.

    Test Case Configuration:
        Run once with the IUT Host bit clear and once with the peer bit clear.

    Test Steps:
        Establish the link, exchange both feature pages, and send RATE_REQ.

    Expected Outcome:
        The Central sends an extended rejection without changing the rate.

    References:
        LL.TS.p28 4.3.6, pages 4.3-421–422; Core Vol 6, Part B, 5.1.33.
    """
    for local_host, peer_host in ((False, True), (True, False)):
        link = await connect(
            tester, central=True, local_host=local_host, peer_host=peer_host
        )
        reject = await link.control(Opcode.LL_REJECT_EXT_IND, request())
        assert reject == bytes(
            (
                Opcode.LL_REJECT_EXT_IND,
                RATE_REQ,
                ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR,
            )
        )
        await link.data()
        await link.finish()


async def invalid_request(tester):
    """Reject all invalid fields of LL_CONNECTION_RATE_REQ in Table 4.3-71."""
    link = await connect(tester, central=True)
    rounds = (
        dict(low=2, high=32000),
        dict(low=4, high=3),
        dict(low=3, high=32001),
        dict(factor=1, factor_max=20, latency=25),
        dict(high=100, factor=5, latency=5, timeout=74),
        dict(low=5, high=5, periodicity=6),
        dict(low=6, high=6, offsets=(7, NO_OFFSET, NO_OFFSET, NO_OFFSET)),
        dict(low=6, high=6, offsets=(3, 7, NO_OFFSET, NO_OFFSET)),
    )
    for values in rounds:
        reject = await link.control(Opcode.LL_REJECT_EXT_IND, request(**values))
        assert reject == bytes(
            (
                Opcode.LL_REJECT_EXT_IND,
                RATE_REQ,
                ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR,
            )
        )
        await link.data()
    await link.finish()


async def invalid_indication(tester):
    """Keep the pre-update schedule after an invalid RATE_IND (Table 4.3-54)."""
    link = await connect(tester, central=False)
    for interval, kwargs in ((32001, {}), (100, dict(timeout=74, factor=5, latency=5))):
        invalid = indication(
            interval, (link.counter + 2) & LL_EVENT_COUNTER_MASK, **kwargs
        )
        response = await link.control(Opcode.LL_UNKNOWN_RSP, invalid)
        assert response == bytes((Opcode.LL_UNKNOWN_RSP, RATE_IND))
        for _ in range(12):
            await link.data()
    await link.finish()


async def collision(tester, *, opcode):
    """Continue the Central's legacy procedure when it rejects the local rate request.

    Test Case Configuration:
        Exercise Connection Update, Connection Parameters Request or Subrate
        Update while the Peripheral's HCI Connection Rate Request is outstanding.

    Test Steps:
        1. Send a Connection Rate Request through HCI and observe its LL request.
        2. Send the Central's PDU, then reject the Rate Request with 0x2A.
        3. Complete the Central's procedure and exchange ACL traffic.

    Expected Outcome:
        The Host receives the collision status for the Rate Request. The
        Central's procedure continues using the correct connection schedule.

    References:
        LL.TS.p28 4.3.5, pages 4.3-272–274; Core Vol 6, Part B, 5.3.
    """
    link = await connect(tester, central=False)
    await tester.command(RateRequest(link.handle, *host_parameters(40, 40, 1, 0, 0)))
    await link.control(RATE_REQ)
    legacy_interval = link.parameters.interval
    if opcode == Opcode.LL_CONNECTION_UPDATE_IND:
        payload = struct.pack(
            '<BB5H',
            opcode,
            1,
            0,
            legacy_interval,
            0,
            TIMEOUT,
            (link.counter + 6) & LL_EVENT_COUNTER_MASK,
        )
        await link.exchange(payload, llid=LlId.CONTROL)
    elif opcode == Opcode.LL_CONNECTION_PARAM_REQ:
        payload = struct.pack(
            '<B4HB7H',
            opcode,
            legacy_interval,
            legacy_interval,
            0,
            TIMEOUT,
            0,
            link.counter,
            *(NO_OFFSET,) * 6,
        )
        await link.control(Opcode.LL_CONNECTION_PARAM_RSP, payload)
    else:
        payload = struct.pack('<B5H', opcode, 1, link.counter, 0, 0, TIMEOUT + 1)
        await link.exchange(payload, llid=LlId.CONTROL)
    code = ErrorCode.DIFFERENT_TRANSACTION_COLLISION_ERROR
    await link.exchange(
        bytes((Opcode.LL_REJECT_EXT_IND, RATE_REQ, code)), llid=LlId.CONTROL
    )
    report = await tester.event(RateChange)
    assert (report.connection_handle, report.status) == (link.handle, code)
    if opcode == Opcode.LL_CONNECTION_PARAM_REQ:
        payload = struct.pack(
            '<BB5H',
            Opcode.LL_CONNECTION_UPDATE_IND,
            1,
            0,
            legacy_interval,
            0,
            TIMEOUT,
            (link.counter + 6) & LL_EVENT_COUNTER_MASK,
        )
        await link.exchange(payload, llid=LlId.CONTROL)
    if payload[0] == Opcode.LL_CONNECTION_UPDATE_IND:
        await link.apply_update(payload)
    else:
        status = await tester.event(hci.HCI_LE_Subrate_Change_Event)
        assert status.status == ErrorCode.SUCCESS
        link.parameters = replace(link.parameters, timeout=TIMEOUT + 1)
    await link.data()
    await link.finish()


# Verify timing and ACL delivery for every combination of initiator and IUT role.
PROCEDURES |= procedure_cases(
    change_rate,
    {
        'LL/CON/CEN/BV-174-C': (
            'Initiate a Connection Rate Update, Central',
            {'central': True, 'local': True},
        ),
        'LL/CON/CEN/BV-175-C': (
            'Connection Rate Update, Central, Peer initiated',
            {'central': True, 'local': False},
        ),
        'LL/CON/PER/BV-178-C': (
            'Initiate a Connection Rate Request, Peripheral',
            {'central': False, 'local': True},
        ),
        'LL/CON/PER/BV-179-C': (
            'Connection Rate Update, Peripheral, Peer Initiated',
            {'central': False, 'local': False},
        ),
    },
)

# Check feature admission, invalid wire parameters, and incompatible procedures.
PROCEDURES |= procedure_cases(
    missing_host_support,
    {
        'LL/CON/CEN/BI-20-C': (
            'Reject Connection Rate Request, Connection Rate Host Bit not set',
            {},
        ),
    },
)
PROCEDURES |= procedure_cases(
    invalid_request,
    {
        'LL/CON/CEN/BI-23-C': (
            'Reject Peer Connection Rate Request, invalid parameters',
            {},
        ),
    },
)
PROCEDURES |= procedure_cases(
    invalid_indication,
    {
        'LL/CON/PER/BI-30-C': (
            'Reject Peer Connection Rate Update, invalid parameters',
            {},
        ),
    },
)
PROCEDURES |= procedure_cases(
    collision,
    {
        'LL/CON/PER/BI-27-C': (
            'Connection Rate Request collision with Connection Update',
            {'opcode': Opcode.LL_CONNECTION_UPDATE_IND},
        ),
        'LL/CON/PER/BI-28-C': (
            'Connection Rate Request collision with Connection Parameters Request',
            {'opcode': Opcode.LL_CONNECTION_PARAM_REQ},
        ),
        'LL/CON/PER/BI-29-C': (
            'Connection Rate Request collision with Connection Subrate Update',
            {'opcode': Opcode.LL_SUBRATE_IND},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_connection_rate(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)


@pytest.mark.asyncio
async def test_short_connection_keeps_a_usable_phy(air_dut):
    """Verify a Coded PHY request cannot stall an ACL at a 1.25 ms rate."""
    link = await connect(air_dut, central=True)
    await air_dut.command(RateRequest(link.handle, *host_parameters(10, 10, 1, 0, 0)))
    await link.apply_rate(await link.control(RATE_IND))

    coded = phy_bit(Phy.LE_CODED)
    command = hci.HCI_LE_Set_PHY_Command(
        link.handle, 0, coded, coded, hci.HCI_LE_PHY_OPTIONS_S8_PREFERRED
    )
    air_dut.send(bytes(command))
    assert await link.control(Opcode.LL_PHY_REQ) == bytes(
        [Opcode.LL_PHY_REQ, coded, coded]
    )
    update = await link.control(
        Opcode.LL_PHY_UPDATE_IND, bytes([Opcode.LL_PHY_RSP, coded, coded])
    )
    assert update == bytes([Opcode.LL_PHY_UPDATE_IND, 0, 0, 0, 0])
    event = await air_dut.event(hci.HCI_LE_PHY_Update_Complete_Event)
    assert (event.status, event.connection_handle, event.tx_phy, event.rx_phy) == (
        ErrorCode.SUCCESS,
        link.handle,
        Phy.LE_1M,
        Phy.LE_1M,
    )
    for _ in range(3):
        await link.data()
    await link.finish()
