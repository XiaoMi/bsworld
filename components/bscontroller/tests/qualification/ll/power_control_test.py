# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.13."""

import struct
from statistics import mean

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    PDU_DATA_LLID_MASK,
    PDU_HEADER_SIZE,
    ControlOpcode,
    LlId,
)
from bscontroller.radio.base import PHY_CODED_S2_FACTOR, PHY_CODED_S8_FACTOR, Phy
from tests.qualification.cases import (
    ProcedureMap,
    collect_cases,
    procedure_cases,
    verify_case,
)
from tests.qualification.cis import (
    PEER_CIS_FEATURES,
    disconnect_cis,
    enable_cis,
    establish_peer_cis,
)
from tests.qualification.peer import (
    PEER_FEATURES,
    change_phy,
    establish,
    exchange_iut_features,
)
from tests.support import LE_EVENT_MASK_V1

Opcode = ControlOpcode
ErrorCode = hci.HCI_ErrorCode
POWER_FEATURES = (
    hci.LeFeatureMask.LE_POWER_CONTROL_REQUEST
    | hci.LeFeatureMask.LE_POWER_CONTROL_REQUEST_DUP
    | hci.LeFeatureMask.LE_PATH_LOSS_MONITORING
)
POWER_EVENT = hci.HCI_LE_Transmit_Power_Reporting_Event
LOSS_EVENT = hci.HCI_LE_Path_Loss_Threshold_Event

# The fixture's conducted IUT levels are declared as the PCL IXIT.
POWER_LEVELS = (-24, -16, -8, 0, 4)
# Bounds for the Lower Tester's apparent RF power in the autonomous test.
PEER_MIN_DBM, PEER_MAX_DBM = -32, 18
PEER_START_DBM = max(PEER_MAX_DBM - 10, (PEER_MIN_DBM + PEER_MAX_DBM) // 2)
CIS_PEER_START_DBM = min(PEER_MIN_DBM + 10, (PEER_MIN_DBM + PEER_MAX_DBM) // 2)
CIS_ISO_INTERVAL_US = (20_000, 20_000, 40_000, 40_000)
PEER_RF_STEP_US = 5_000_000
PATH_LOSS_LOW_DB, PATH_LOSS_HIGH_DB = 34, 56
# Link preamble for the PCL procedures uses 7.5 ms, zero latency, and 1 s timeout.
INTERVAL = 6
MAXIMUM_EVENTS = 24
MINIMUM_FLAG = 1
MAXIMUM_FLAG = 2
POWER_NOT_MANAGED = 126
POWER_UNAVAILABLE = 127
PATH_LOSS_UNAVAILABLE = 0xFF

PHY_VALUES = (
    ('LE 1M', 0x01, Phy.LE_1M, PHY_CODED_S8_FACTOR),
    ('LE 2M', 0x02, Phy.LE_2M, PHY_CODED_S8_FACTOR),
    ('LE Coded S=8', 0x04, Phy.LE_CODED, PHY_CODED_S8_FACTOR),
    ('LE Coded S=2', 0x08, Phy.LE_CODED, PHY_CODED_S2_FACTOR),
)


def _control_payload(packet):
    if packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL:
        return packet.pdu[PDU_HEADER_SIZE:]
    return b''


def _request(phy, delta, power):
    return struct.pack('<BBbb', Opcode.LL_POWER_CONTROL_REQ, phy, delta, power)


def _response(flags, delta, power, apr=0):
    return struct.pack('<BBbbB', Opcode.LL_POWER_CONTROL_RSP, flags, delta, power, apr)


def _indication(phy, flags, delta, power):
    return struct.pack('<BBBbb', Opcode.LL_POWER_CHANGE_IND, phy, flags, delta, power)


async def _set_phy(tester, link, mode, *, local):
    _, _, phy, coding = PHY_VALUES[mode]
    link.tx_coding = coding
    if link.tx_phy != phy or link.rx_phy != phy:
        # Core 4.E 7.8.49: PHY_Options 1 requests S=2 and 2 requests S=8.
        options = int(coding != PHY_CODED_S2_FACTOR) + 1 if phy == Phy.LE_CODED else 0
        await change_phy(tester, link, phy, local=local, phy_options=options)


async def _power_link(tester, *, central, mode=0):
    link = await establish(tester, central=central, interval=INTERVAL)
    await exchange_iut_features(tester, link, PEER_FEATURES | POWER_FEATURES)
    await _set_phy(tester, link, mode, local=True)
    return link


async def _current_power(link, mode):
    # HCI uses a sequential enumeration, whereas the lower-tester PDU uses a mask.
    phy = mode + 1
    reply = await link.tester.command(
        hci.HCI_LE_Enhanced_Read_Transmit_Power_Level_Command(link.handle, phy)
    )
    assert (reply.connection_handle, reply.phy) == (link.handle, phy)
    return reply.current_tx_power_level, reply.max_tx_power_level


async def _adjust_power(link, mode, delta):
    response = await link.control(
        Opcode.LL_POWER_CONTROL_RSP,
        _request(PHY_VALUES[mode][1], delta, link.tx_power_dbm),
    )
    _, flags, applied, power, apr = struct.unpack('<BBbbB', response)
    if power != POWER_NOT_MANAGED:
        assert flags & ~(MINIMUM_FLAG | MAXIMUM_FLAG) == 0
    return flags, applied, power, apr


async def _event(link, event_type, *, reason=None):
    for _ in range(MAXIMUM_EVENTS):
        event = link.tester.take(
            lambda event: isinstance(event, event_type)
            and (reason is None or event.reason == reason)
        )
        if event is not None:
            return event
        await link.exchange()
    raise AssertionError(f'no {event_type.__name__} on connection {link.handle}')


async def _continuous_data(link, duration_us):
    deadline = link.anchor + duration_us
    while link.anchor < deadline:
        await link.exchange()


def _answer_power_query(link, *, reported_power=None):
    """Answer optional requests without changing the Lower Tester's current power."""

    def receive(packet):
        payload = _control_payload(packet)
        if payload and payload[0] == Opcode.LL_POWER_CONTROL_REQ:
            return _response(
                0, 0, link.tx_power_dbm if reported_power is None else reported_power
            )
        return None

    return receive


async def _query_peer_power(link):
    request = await link.control(Opcode.LL_POWER_CONTROL_REQ)
    assert struct.unpack('<BBbb', request)[1:3] == (PHY_VALUES[0][1], 0)
    assert request[3] != POWER_NOT_MANAGED
    # The procedure exercises ignore-on-receive for the response's RFU flag bits.
    await link.exchange(_response(0xB4, 0, link.tx_power_dbm), llid=LlId.CONTROL)


async def _pursue_power_request(link, *, direction, mode):
    """Drive real ACL events for five virtual seconds between 2-dB RF steps."""
    target = PEER_MAX_DBM if direction > 0 else PEER_MIN_DBM
    phy = PHY_VALUES[mode][1]
    while link.tx_power_dbm != target:
        link.tx_power_dbm += 2 * direction
        deadline = link.anchor + PEER_RF_STEP_US
        while link.anchor < deadline:
            packet = await link.exchange()
            payload = _control_payload(packet)
            if payload and payload[0] == Opcode.LL_POWER_CONTROL_REQ:
                _, request_phy, delta, tx_power = struct.unpack('<BBbb', payload)
                assert request_phy == phy
                assert tx_power != POWER_NOT_MANAGED
                if direction * delta < 0:
                    return delta
                await link.exchange(
                    _response(0, 0, link.tx_power_dbm),
                    llid=LlId.CONTROL,
                )
    raise AssertionError(f'no request for PHY {phy:#x} at RF endpoint {target} dBm')


async def _acknowledge_adjustment(link, delta):
    previous = link.tx_power_dbm
    # The Lower Tester has 2-dB steps. Positive requests may round upwards.
    adjustment = 2 * ((delta + 1) // 2)
    power = max(PEER_MIN_DBM, min(PEER_MAX_DBM, previous + adjustment))
    link.tx_power_dbm = power
    flags = (MINIMUM_FLAG if power == PEER_MIN_DBM else 0) | (
        MAXIMUM_FLAG if power == PEER_MAX_DBM else 0
    )
    await link.exchange(_response(flags, power - previous, power), llid=LlId.CONTROL)


async def path_loss_monitoring(tester, *, central):
    """Path Loss Monitoring.

    Initial Condition:
        Establish a 7.5-ms LE 1M ACL in the selected role. Set the IXIT zone
        boundaries to 34 and 56 dB around the fixture's nominal 45-dB path.
        The Lower Tester initially advertises 0 dBm on the ACL.

    Test Steps:
        1-6. Keep the ACL alive, configure both zone boundaries with 3-dB
             hysteresis and one-event dwell, and enable path loss reporting.
             Respond to optional power queries, then require middle.
        7-9. Gradually change apparent path loss without a Power Change
             Indication. Require low, middle, and then high in that order.
        10-11. Set the high threshold to the observed loss plus 5 dB and
              both hysteresis values to 1 dB. Require middle again.

    Expected Outcome:
        Every zone event uses the current connection handle; the high report
        includes the observed loss used to set the new threshold.

    Reference:
        LL.TS.p28 4.13.4.1, Table 4.13-1; Core Vol 6, Part B, 4.5.16.
    """
    link = await _power_link(tester, central=central)
    link.on_receive = _answer_power_query(link, reported_power=0)
    low, high = PATH_LOSS_LOW_DB, PATH_LOSS_HIGH_DB

    async def set_zones(high, hysteresis):
        await tester.command(
            hci.HCI_LE_Set_Path_Loss_Reporting_Parameters_Command(
                link.handle, high, hysteresis, low, hysteresis, 1
            )
        )

    async def require_zone(zone):
        event = await _event(link, LOSS_EVENT)
        assert (event.connection_handle, event.zone_entered) == (link.handle, zone)
        return event.current_path_loss

    await set_zones(high, 3)
    await tester.command(
        hci.HCI_LE_Set_Path_Loss_Reporting_Enable_Command(link.handle, 1)
    )
    assert low < await require_zone(1) < high
    for zone, target in ((0, 18), (1, 0), (2, -24)):
        step = 2 if target > link.tx_power_dbm else -2
        for power in range(link.tx_power_dbm + step, target + step, step):
            link.tx_power_dbm = power
            for _ in range(4):
                await link.exchange()
                if any(isinstance(event, LOSS_EVENT) for event in tester.events):
                    break
            else:
                continue
            break
        observed = await require_zone(zone)
    await set_zones(observed + 5, 1)
    await require_zone(1)
    await link.disconnect()


async def path_loss_monitoring_unavailable(tester, *, central, mode):
    """Path Loss Monitoring – Unavailable.

    Initial Condition:
        Establish the selected ACL role and PHY. The Lower Tester starts
        10 dB below its maximum or at its power midpoint, whichever is higher.

    Test Steps:
        1–3. Enable path loss reporting with the IXIT zone boundaries, 3-dB
             hysteresis and a one-event dwell. Answer the initial power query.
        4–8. Increase the peer's RF power in 2-dB steps at least five seconds
             apart until the IUT asks for a decrease. Reply with TxPower 127
             and expect Current_Path_Loss 0xFF.
        9–13. Decrease the peer's RF power under the same cadence until the
              IUT asks for an increase. Reply with its current power. A
              neutral query also receives the current power, so the fresh
              path loss report may arrive while waiting for the increase.

    Expected Outcome:
        The active ACL reports unavailable, then a valid measurement even
        though the estimate returns to the previously reported middle zone.

    Reference:
        LL.TS.p28 4.13.4.14, Table 4.13-15; Core Vol 6, Part B, 4.5.16.
    """
    link = await _power_link(tester, central=central, mode=mode)
    link.tx_power_dbm = PEER_START_DBM
    low, high = PATH_LOSS_LOW_DB, PATH_LOSS_HIGH_DB
    link.on_receive = _answer_power_query(link)
    await tester.command(
        hci.HCI_LE_Set_Path_Loss_Reporting_Parameters_Command(
            link.handle, high, 3, low, 3, 1
        )
    )
    await tester.command(
        hci.HCI_LE_Set_Path_Loss_Reporting_Enable_Command(link.handle, 1)
    )
    initial = await _event(link, LOSS_EVENT)
    assert (initial.connection_handle, initial.zone_entered) == (link.handle, 1)
    assert low < initial.current_path_loss < high
    link.on_receive = None

    await _pursue_power_request(link, direction=1, mode=mode)
    await link.exchange(_response(0, 0, POWER_UNAVAILABLE), llid=LlId.CONTROL)
    unavailable = await _event(link, LOSS_EVENT)
    assert (unavailable.connection_handle, unavailable.current_path_loss) == (
        link.handle,
        PATH_LOSS_UNAVAILABLE,
    )

    link.on_receive = _answer_power_query(link)
    await _continuous_data(link, PEER_RF_STEP_US)
    link.on_receive = None
    await _pursue_power_request(link, direction=-1, mode=mode)
    restored = tester.take(lambda event: isinstance(event, LOSS_EVENT))
    await link.exchange(_response(0, 0, link.tx_power_dbm), llid=LlId.CONTROL)
    if restored is None:
        restored = await _event(link, LOSS_EVENT)
    assert (restored.connection_handle, restored.zone_entered) == (link.handle, 1)
    assert low < restored.current_path_loss < high
    await link.disconnect()


async def initiate_power_control(tester, *, central, mode):
    """Power Control Request – Initiate.

    Initial Condition:
        Establish the selected ACL role and PHY at 7.5 ms. The Lower Tester
        starts 10 dB below its maximum or at its power midpoint, whichever
        is higher.

    Test Steps:
        1-3. Increase apparent peer transmit power in 2-dB steps, with at
             least five seconds of ACL traffic between adjustments. Observe
             a negative request and answer with the applied power change.
        4-6. Decrease the peer transmit power under the same cadence. Observe
             a positive request and answer with the applied power change.
             The Lower Tester uses the selected coding on a Coded PHY.

    Expected Outcome:
        Both requests specify the selected PHY and request the expected sign
        of adjustment before reaching the corresponding RF endpoint.

    Reference:
        LL.TS.p28 4.13.4.2, Table 4.13-2; Core Vol 6, Part B, 5.1.17.
    """
    link = await _power_link(tester, central=central, mode=mode)
    link.tx_power_dbm = PEER_START_DBM
    decrease = await _pursue_power_request(link, direction=1, mode=mode)
    await _acknowledge_adjustment(link, decrease)
    # The response changes the peer level. Drive traffic before the next RF step.
    await _continuous_data(link, PEER_RF_STEP_US)
    increase = await _pursue_power_request(link, direction=-1, mode=mode)
    await _acknowledge_adjustment(link, increase)
    await link.disconnect()


async def _cis_power_link(tester, *, central, acl_mode, cis_mode):
    await tester.masks(le=LE_EVENT_MASK_V1)
    await enable_cis(tester)
    interval = 10 * CIS_ISO_INTERVAL_US[cis_mode] // LL_CONN_INTERVAL_UNIT_US
    link = await establish(tester, central=central, interval=interval, csa2=True)
    await exchange_iut_features(
        tester, link, PEER_FEATURES | PEER_CIS_FEATURES | POWER_FEATURES
    )
    await _set_phy(tester, link, acl_mode, local=True)
    link.tx_power_dbm = CIS_PEER_START_DBM
    return link


async def _start_power_cis(link, mode):
    _, _, phy, coding = PHY_VALUES[mode]
    coded = phy == Phy.LE_CODED
    _, air, event = await establish_peer_cis(
        link.tester,
        central=link.central,
        link=link,
        test_nse=2 if coded else 3,
        sdu_sizes=(50, 50) if coded else None,
        interval_us=CIS_ISO_INTERVAL_US[mode],
        offset_us=5000,
        phys=(phy, phy),
        coding=coding,
    )
    return air, event.connection_handle


async def _cis_power_exchange(air, power_dbm):
    while air.nominal_us <= air.link.tester.radio.now_us:
        air.step()
    payload = bytes([air.counter & 0xFF]) * 32
    return await air.exchange(
        payload, header=(air.counter & 1) << 3, power_dbm=power_dbm
    )


async def initiate_cis_power_control(tester, *, central, acl_mode, cis_mode=0):
    """Request peer power on the PHY of an active CIS.

    Initial Condition:
        The Lower Tester starts at the lower of its transmit-power midpoint
        and 10 dB above its minimum. The ACL interval is ten ISO intervals.

    Test Case Configuration:
        Select the ACL PHY, CIS PHY and coding, and IUT role from Table 4.13-8.

    Test Steps:
        Query the IUT power on the ACL PHY and establish the CIS. During
        continuous CIS data, decrease the Lower Tester's CIS transmit level
        by 2 dB at five-second intervals. Reply to the IUT request with zero
        applied delta.

    Expected Outcome:
        The IUT requests a positive power delta for the selected CIS PHY.

    Reference:
        LL.TS.p28 4.13.4.8, Table 4.13-8, Figure 4.13-9.
    """
    link = await _cis_power_link(
        tester, central=central, acl_mode=acl_mode, cis_mode=cis_mode
    )
    _, applied, power, _ = await _adjust_power(link, acl_mode, 0)
    assert applied == 0 and power != POWER_NOT_MANAGED
    air, handle = await _start_power_cis(link, cis_mode)
    cis_phy = PHY_VALUES[cis_mode][1]
    positive = []
    level = CIS_PEER_START_DBM

    def receive(packet):
        payload = _control_payload(packet)
        if payload[:1] != bytes([Opcode.LL_POWER_CONTROL_REQ]):
            return None
        _, phy, delta, power = struct.unpack('<BBbb', payload)
        if level < CIS_PEER_START_DBM and phy == cis_phy and delta > 0:
            assert power != POWER_NOT_MANAGED
            positive.append(delta)
        return _response(0, 0, level if phy == cis_phy else link.tx_power_dbm)

    link.on_receive = receive
    for level in range(CIS_PEER_START_DBM, PEER_MIN_DBM - 1, -2):
        deadline = air.nominal_us + PEER_RF_STEP_US
        while air.nominal_us < deadline and not positive:
            await _cis_power_exchange(air, level)
        if positive:
            break
    assert positive, 'CIS power increase was not requested before the IXIT minimum'
    # Complete a queued Lower Tester response while the CIS stays active.
    while link.control_responses:
        await _cis_power_exchange(air, level)
    link.on_receive = None
    await disconnect_cis(tester, link, handle)
    await link.disconnect()


async def respond_to_cis_power_requests(tester, *, central, acl_mode, cis_mode=0):
    """Reflect a peer's CIS power adjustment in the ACL HCI readback.

    Initial Condition:
        An ACL supports Power Control on the selected role and PHY.

    Test Case Configuration:
        Select the ACL PHY, CIS PHY and coding, and IUT role from Table 4.13-9.

    Test Steps:
        Establish the CIS, then query its current power over the ACL. Request
        -6 dB and +6 dB in sequence, reading the CIS PHY's power via HCI
        after each response. Observe CIS packets at the resulting levels.

    Expected Outcome:
        HCI and LL levels agree and each reported delta equals the change
        from the previous level. CIS packets using that PHY and coding
        reflect the change.

    Reference:
        LL.TS.p28 4.13.4.9, Table 4.13-9, Figure 4.13-10.
    """
    link = await _cis_power_link(
        tester, central=central, acl_mode=acl_mode, cis_mode=cis_mode
    )
    air, handle = await _start_power_cis(link, cis_mode)
    _, applied, previous, _ = await _adjust_power(link, cis_mode, 0)
    assert applied == 0
    assert await _current_power(link, cis_mode) == (previous, POWER_LEVELS[-1])

    async def read_rssi():
        readings = []
        _, _, phy, coding = PHY_VALUES[cis_mode]
        for _ in range(6):
            packet = await _cis_power_exchange(air, CIS_PEER_START_DBM)
            if phy != Phy.LE_CODED or packet.coding == coding:
                readings.append(packet.rssi)
        return mean(readings) if readings else None

    last_rssi = await read_rssi()
    for delta in (-6, 6):
        _, applied, actual, _ = await _adjust_power(link, cis_mode, delta)
        assert actual == previous + applied
        assert actual in POWER_LEVELS
        assert await _current_power(link, cis_mode) == (actual, POWER_LEVELS[-1])
        rssi = await read_rssi()
        if rssi is not None and last_rssi is not None:
            assert rssi - last_rssi == pytest.approx(applied, abs=3)
        previous, last_rssi = actual, rssi
    await disconnect_cis(tester, link, handle)
    await link.disconnect()


async def _power_level_walk(link, mode, levels):
    """Execute Steps 2–23 of the common response procedure for one IXIT table."""
    flags, _, power, _ = await _adjust_power(link, mode, -64)
    assert power == levels[0] and flags == MINIMUM_FLAG
    assert await _current_power(link, mode) == (levels[0], levels[-1])

    async def move(previous, delta, expected):
        flags, applied, actual, _ = await _adjust_power(link, mode, delta)
        assert (applied, actual) == (expected - previous, expected)
        assert flags == (
            MINIMUM_FLAG
            if expected == levels[0]
            else MAXIMUM_FLAG
            if expected == levels[-1]
            else 0
        )
        assert await _current_power(link, mode) == (expected, levels[-1])

    for lower, higher in zip(levels, levels[1:]):
        await move(lower, 1, higher)
    # The supported levels have no adjacent 1-dB steps. An insufficient
    # negative request must leave the highest level unchanged in Steps 14–17.
    await move(levels[-1], -1, levels[-1])
    # Steps 19–23 use a requested reduction between the first two lower levels.
    delta = levels[-2] - levels[-1] - 1
    assert levels[-3] - levels[-1] < delta < levels[-2] - levels[-1]
    await move(levels[-1], delta, levels[-2])


async def respond_to_power_requests(tester, *, central, mode, compensated=False):
    """Power Control Response.

    Initial Condition:
        Establish the selected ACL role and PHY. The IUT declares five
        supported levels in its IXIT.

    Test Case Configuration:
        BV-08 through BV-11 use the uncompensated levels. BV-33 through
        BV-36 repeat the procedure for the four RF compensation rounds
        listed in Table 4.13-12.

    Test Steps:
        1-11. Request -64 dB to reach the minimum. Increase to each supported
              level by requesting +1 dB, and compare HCI with the LL response.
        12-18. An insufficient negative request leaves the maximum unchanged
               because the next lower supported level is more than 1 dB away.
        19-23. Request a decrease between the first two lower levels. Check the
               choice of the closer supported level and the reported delta.

    Expected Outcome:
        The transmit level and flags on the air agree with the supported IXIT
        values for the current compensation and with the HCI readback.

    Reference:
        LL.TS.p28 4.13.4.3 and 4.13.4.11, Tables 4.13-3 and 4.13-11/12.
    """
    link = await _power_link(tester, central=central, mode=mode)
    rounds = (0, 5, -5, 0) if compensated else (0,)
    if compensated:
        original = await tester.command(hci.HCI_LE_Read_RF_Path_Compensation_Command())
        assert original.rf_tx_path_compensation_value == 0
    for offset in rounds:
        if compensated:
            await tester.command(
                hci.HCI_LE_Write_RF_Path_Compensation_Command(
                    original.rf_tx_path_compensation_value + 10 * offset,
                    original.rf_rx_path_compensation_value,
                )
            )
        await _power_level_walk(
            link, mode, tuple(level + offset for level in POWER_LEVELS)
        )
    await link.disconnect()


async def report_remote_minimum_and_maximum(tester, *, central):
    """Power Configuration Response, Min and Max Power Level Reached.

    Initial Condition:
        Establish the selected ACL role on LE 1M and use the Lower Tester's
        declared power range -24 to +4 dBm.

    Test Steps:
        1-3. Enable remote power reporting and answer the current-PHY
             zero-delta request. Use nonzero RFU bits in the response.
        4-8. Send 2-dB power increase indications at 1-s intervals, with
             the maximum flag on the endpoint only.
        9-13. Repeat with -2 dB and the minimum flag on the lower endpoint.
              Set RFU bits in the indications and inspect each HCI event.

    Expected Outcome:
        The Host receives Reason 1 with the exact PHY, signed delta, new
        transmit power, and the appropriate two-bit bound flag.

    Reference:
        LL.TS.p28 4.13.4.4, Table 4.13-4; Core Vol 6, Part B, 5.1.18.
    """
    link = await _power_link(tester, central=central)
    link.tx_power_dbm = -14
    await tester.command(
        hci.HCI_LE_Set_Transmit_Power_Reporting_Enable_Command(link.handle, 0, 1)
    )
    await _query_peer_power(link)
    link.on_receive = _answer_power_query(link, reported_power=link.tx_power_dbm)
    for initial, endpoint, delta, end_flag in (
        (-14, 4, 2, MAXIMUM_FLAG),
        (-6, -24, -2, MINIMUM_FLAG),
    ):
        link.tx_power_dbm = initial
        for power in range(initial + delta, endpoint + delta, delta):
            await _continuous_data(link, 1_000_000)
            link.tx_power_dbm = power
            flag = end_flag if power == endpoint else 0
            link.on_receive = _answer_power_query(link, reported_power=power)
            await link.exchange(
                _indication(1, flag | 0xB4, delta, power), llid=LlId.CONTROL
            )
            event = await _event(link, POWER_EVENT, reason=1)
            assert (event.status, event.connection_handle, event.phy) == (
                0,
                link.handle,
                1,
            )
            assert (event.tx_power_level, event.tx_power_level_flag, event.delta) == (
                power,
                flag,
                delta,
            )
    await link.disconnect()


async def crossing_power_requests(tester, *, central):
    """Properly handle a Power Request PDU when waiting for a Power Response.

    Initial Condition:
        Establish the selected 7.5-ms ACL role. Start the Lower Tester at
        the lesser of its midpoint and 10 dB above its minimum.

    Test Steps:
        1-3. Decrease peer power at intervals of five seconds until the
             IUT sends a positive Power Control Request.
        4-5. Withhold the response and send an independent positive request.
             Check the APR in the IUT's response before completing its request.

    Expected Outcome:
        The outstanding local procedure does not block the peer's request.
        The response sets APR to 0xFF while the local request is pending.

    Reference:
        LL.TS.p28 4.13.4.5, Table 4.13-5; Core Vol 6, Part B, 5.1.17.
    """
    link = await _power_link(tester, central=central)
    midpoint = (PEER_MIN_DBM + PEER_MAX_DBM) // 2
    link.tx_power_dbm = min(PEER_MIN_DBM + 10, midpoint)
    increase = await _pursue_power_request(link, direction=-1, mode=0)
    _, _, _, apr = await _adjust_power(link, 0, 2)
    assert apr == 0xFF
    await _acknowledge_adjustment(link, increase)
    await link.disconnect()


async def invalid_power_request_phy(tester, *, central, phy):
    """Power Control Request with an invalid or unsupported PHY.

    Initial Condition:
        Establish the selected connected role.

    Test Steps:
        1. Send a Power Control Request with the PHY from Table 4.13-6.
        2. Receive an LL_REJECT_EXT_IND for that opcode.

    Expected Outcome:
        The Controller rejects the request with a valid error code.

    Reference:
        LL.TS.p28 4.13.4.6, Table 4.13-6; Core Vol 6, Part B, 5.1.17.
    """
    link = await _power_link(tester, central=central)
    response = await link.control(Opcode.LL_REJECT_EXT_IND, _request(phy, 0, 0))
    assert response[:2] == bytes(
        [Opcode.LL_REJECT_EXT_IND, Opcode.LL_POWER_CONTROL_REQ]
    )
    assert ErrorCode(response[2]) != ErrorCode.SUCCESS
    await link.disconnect()


async def invalid_power_indication_phy(tester, *, central, phy):
    """Power Change Indication with an invalid or unsupported PHY.

    Initial Condition:
        Establish the selected ACL role on LE 1M.

    Test Steps:
        1-3. Enable remote reporting and respond to the initial power query.
        4-5. Reduce peer transmit power and send a Power Change Indication
             with the PHY from Table 4.13-14. Observe the connection and
             check the Host event queue.

    Expected Outcome:
        No Transmit Power Reporting event follows the invalid indication.

    Reference:
        LL.TS.p28 4.13.4.13, Table 4.13-14; Core Vol 6, Part B, 5.1.18.
    """
    link = await _power_link(tester, central=central)
    await tester.command(
        hci.HCI_LE_Set_Transmit_Power_Reporting_Enable_Command(link.handle, 0, 1)
    )
    await _query_peer_power(link)
    await _continuous_data(link, 5 * INTERVAL * LL_CONN_INTERVAL_UNIT_US)
    delta = -2
    link.tx_power_dbm += delta
    await link.exchange(
        _indication(phy, 0, delta, link.tx_power_dbm), llid=LlId.CONTROL
    )
    await _continuous_data(link, 5 * INTERVAL * LL_CONN_INTERVAL_UNIT_US)
    assert not any(isinstance(event, POWER_EVENT) for event in tester.events)
    await link.disconnect()


async def power_indication_after_phy_change(tester, *, central, original, new):
    """Power Change Indication on PHY Change.

    Initial Condition:
        Establish the selected role on LE 1M.

    Test Steps:
        1-5. Complete a peer-initiated PHY update to the original PHY if
             needed. Read the original and new PHY power with zero-delta
             requests and require managed power on the original PHY.
        6-10. Complete the peer-initiated update to the new PHY and verify
              its HCI PHY event. When its previously reported level was
              unavailable, require a Change Indication with the newly
              active PHY and its actual power.

    Expected Outcome:
        A PHY becoming managed is announced during valid data exchange on
        the new PHY. The indication agrees with the HCI readback.

    Reference:
        LL.TS.p28 4.13.4.7, Table 4.13-7; Core Vol 6, Part B, 5.1.18.
    """
    link = await _power_link(tester, central=central)
    await _set_phy(tester, link, original, local=False)
    _, _, original_power, _ = await _adjust_power(link, original, 0)
    assert original_power != POWER_NOT_MANAGED
    _, _, initial_power, _ = await _adjust_power(link, new, 0)
    indications = []

    def receive(packet):
        payload = _control_payload(packet)
        if payload and payload[0] == Opcode.LL_POWER_CHANGE_IND:
            indications.append(payload)
        return None

    link.on_receive = receive
    await _set_phy(tester, link, new, local=False)
    actual, _ = await _current_power(link, new)
    if initial_power != actual:
        for _ in range(MAXIMUM_EVENTS):
            if any(indication[1] == PHY_VALUES[new][1] for indication in indications):
                break
            await link.exchange()
        assert any(
            indication[1] == PHY_VALUES[new][1]
            and struct.unpack('<b', indication[4:5])[0] == actual
            for indication in indications
        )
    await link.disconnect()


async def read_remote_power(tester):
    """Remote Transmit Power Level Request.

    Initial Condition:
        Establish a Peripheral IUT connection on LE 1M.

    Test Steps:
        1-5. Request the remote level through HCI, answer with a zero delta,
             and check the completed read event.
        6-7. Send a Power Control Request whose TxPower is 126. The IUT
             shall reject that value in a request.

    Expected Outcome:
        The HCI event reports success, Reason 2, zero delta, and the Lower
        Tester's level. The malformed request is rejected with 0x1E.

    Source Correction:
        Use the Core LE 1M PHY value 0x01 in both HCI and the LL PDU.
        Table 4.13-10 lists 0x00 for this PHY.

    Reference:
        LL.TS.p28 4.13.4.10, Table 4.13-10; Core Vol 6, Part B, 5.1.17.
    """
    link = await _power_link(tester, central=False)
    await tester.command(
        hci.HCI_LE_Read_Remote_Transmit_Power_Level_Command(link.handle, 1)
    )
    await _query_peer_power(link)
    event = await _event(link, POWER_EVENT, reason=2)
    assert (event.status, event.connection_handle, event.phy) == (0, link.handle, 1)
    assert (event.delta, event.tx_power_level) == (0, link.tx_power_dbm)
    reject = await link.control(
        Opcode.LL_REJECT_EXT_IND, _request(1, 0, POWER_NOT_MANAGED)
    )
    assert reject == bytes(
        [Opcode.LL_REJECT_EXT_IND, Opcode.LL_POWER_CONTROL_REQ, 0x1E]
    )
    await link.disconnect()


async def report_local_minimum_and_maximum(tester, *, central):
    """Max and Min Power Level Response at Max and Min Power.

    Initial Condition:
        Establish the selected 7.5-ms ACL with its default power within
        the declared supported range.

    Test Steps:
        1-9. Enable the local power event. Request +2 dB once per second
             until maximum and verify that a further request has no effect.
        10-16. Request -2 dB and increase the requested reduction by 1 dB
               after each unchanged response. Once at minimum, verify that
               a further -2 dB request leaves the power unchanged.

    Expected Outcome:
        Each real adjustment generates a Host local-change event with the
        observed delta and bound flag. The out-of-range requests at the
        respective limits return zero delta and the corresponding flag.

    Reference:
        LL.TS.p28 4.13.4.12, Table 4.13-13; Core Vol 6, Part B, 5.1.17.
    """
    link = await _power_link(tester, central=central)
    await tester.command(
        hci.HCI_LE_Set_Transmit_Power_Reporting_Enable_Command(link.handle, 1, 0)
    )

    async def adjust(delta):
        flags, applied, power, _ = await _adjust_power(link, 0, delta)
        if applied:
            event = await _event(link, POWER_EVENT, reason=0)
            assert (event.status, event.connection_handle, event.phy) == (
                0,
                link.handle,
                1,
            )
            assert (event.delta, event.tx_power_level, event.tx_power_level_flag) == (
                applied,
                power,
                flags,
            )
        return flags, applied, power

    for direction, limit, limit_flag in (
        (1, POWER_LEVELS[-1], MAXIMUM_FLAG),
        (-1, POWER_LEVELS[0], MINIMUM_FLAG),
    ):
        delta = direction * 2
        for _ in range(MAXIMUM_EVENTS):
            flags, applied, power = await adjust(delta)
            if power == limit:
                assert flags == limit_flag
                break
            if applied == 0 and direction < 0:
                delta -= 1
            elif direction > 0:
                assert applied >= delta
            else:
                assert delta <= applied <= 0
            await _continuous_data(link, 1_000_000)
        else:
            raise AssertionError(f'no power clamp at {limit} dBm')
        assert await adjust(direction * 2) == (limit_flag, 0, limit)
    await link.disconnect()


async def clamp_to_maximum_power(tester, *, central, mode):
    """Power Control Response, Max Power.

    Initial Condition:
        Establish the selected ACL role and PHY. The IUT's maximum
        supported level in this fixture is +4 dBm.

    Test Steps:
        1-5. Query the current power. If at maximum, first request -15 dB.
        6-10. Request +10 dB until the maximum flag is set. Request another
              +10 dB and check that the IUT stays at its maximum.

    Expected Outcome:
        The last response has Delta 0, maximum flag set and the IXIT maximum
        transmit power for the PHY. Every reported delta equals the change.

    Reference:
        LL.TS.p28 4.13.4.15, Table 4.13-16; Core Vol 6, Part B, 5.1.17.
    """
    link = await _power_link(tester, central=central, mode=mode)
    flags, _, power, _ = await _adjust_power(link, mode, 0)
    if power == POWER_LEVELS[-1]:
        flags, _, power, _ = await _adjust_power(link, mode, -15)
        assert not flags & MAXIMUM_FLAG
    for _ in POWER_LEVELS:
        previous = power
        flags, delta, power, _ = await _adjust_power(link, mode, 10)
        assert delta == power - previous and delta > 0
        if flags & MAXIMUM_FLAG:
            break
    else:
        raise AssertionError('maximum power flag never set')
    assert (await _adjust_power(link, mode, 10))[:3] == (
        MAXIMUM_FLAG,
        0,
        POWER_LEVELS[-1],
    )
    await link.disconnect()


async def power_indication_after_compensation(tester, *, central):
    """Send Power Change Indication After LE Write RF Path Compensation.

    Initial Condition:
        Establish a LE 1M ACL in the selected role.

    Test Steps:
        1-4. Read and restore the default compensation, then query the
             current power. Require the Controller to manage that PHY.
        5-8. Write compensation values of default +5 and default -5 dB.
             Receive a Change Indication for each write and measure RSSI.

    Expected Outcome:
        The reported TX power and actual received RF power change by +5 then
        -10 dB. Average consecutive packets to resolve the RF step in the PHY
        model within the Section 4.13.2 measurement tolerance.

    Reference:
        LL.TS.p28 4.13.4.16, Table 4.13-17; Core Vol 6, Part B, 5.1.18.
    """
    link = await _power_link(tester, central=central)
    original = await tester.command(hci.HCI_LE_Read_RF_Path_Compensation_Command())

    async def write_compensation(delta):
        await tester.command(
            hci.HCI_LE_Write_RF_Path_Compensation_Command(
                original.rf_tx_path_compensation_value + delta * 10,
                original.rf_rx_path_compensation_value,
            )
        )

    async def received_power():
        samples = []
        for _ in range(16):
            packet = await link.exchange()
            samples.append(packet.rssi)
        return mean(samples)

    await write_compensation(0)
    _, _, old_power, _ = await _adjust_power(link, 0, 0)
    assert old_power != POWER_NOT_MANAGED
    previous_rssi = await received_power()
    for offset, delta in ((5, 5), (-5, -10)):
        await write_compensation(offset)
        report = await link.control(Opcode.LL_POWER_CHANGE_IND)
        _, phy, _, applied, new_power = struct.unpack('<BBBbb', report)
        assert (phy, applied, new_power) == (1, delta, old_power + delta)
        rssi = await received_power()
        assert rssi - previous_rssi == pytest.approx(delta, abs=3)
        old_power, previous_rssi = new_power, rssi
    await link.disconnect()


def _role_cases(suffix, title, **parameters):
    return {
        f'LL/PCL/{code}/{suffix}-C': (
            f'{title}, {role}',
            {'central': code == 'CEN', **parameters},
        )
        for code, role in (('CEN', 'Central'), ('PER', 'Peripheral'))
    }


def _phy_cases(numbers, title, **parameters):
    return {
        identifier: case
        for mode, number in enumerate(numbers)
        for identifier, case in _role_cases(
            f'BV-{number:02}',
            f'{title}, {PHY_VALUES[mode][0]}',
            mode=mode,
            **parameters,
        ).items()
    }


PROCEDURES = ProcedureMap()

# Confirm all three path-loss zones and a live threshold change.
PROCEDURES |= procedure_cases(
    path_loss_monitoring, _role_cases('BV-01', 'Path Loss Monitoring')
)

# Report unavailable and restore the same zone on each active PHY.
PROCEDURES |= procedure_cases(
    path_loss_monitoring_unavailable,
    _phy_cases((49, 50, 51, 52), 'Path Loss Monitoring Unavailable'),
)

# Initiate the power request in both directions on the four PHY/coding options.
PROCEDURES |= procedure_cases(
    initiate_power_control, _phy_cases((3, 4, 5, 47), 'Power Control Request')
)

# Negotiate on the ACL while measuring or adjusting the separate CIS radio PHY.
for procedure, numbers, title in (
    (initiate_cis_power_control, (20, 45), 'Power Control Request, CIS, Initiate'),
    (respond_to_cis_power_requests, (25, 46), 'Power Control Response, CIS'),
):
    PROCEDURES |= procedure_cases(
        procedure,
        {
            identifier: case
            for acl_mode, number in enumerate(numbers)
            for identifier, case in _role_cases(
                f'BV-{number:02}',
                f'{title}, {PHY_VALUES[acl_mode][0]} ACL',
                acl_mode=acl_mode,
            ).items()
        },
    )

# Coded CIS variants use a 1M ACL, and the case IDs differ by role and coding.
for procedure, cases, title in (
    (
        initiate_cis_power_control,
        ((True, 23, 2), (False, 22, 2), (True, 48, 3), (False, 48, 3)),
        'Power Control Request, CIS, Initiate',
    ),
    (
        respond_to_cis_power_requests,
        ((True, 27, 2), (False, 28, 3)),
        'Power Control Response, CIS',
    ),
):
    PROCEDURES |= procedure_cases(
        procedure,
        {
            f'LL/PCL/{"CEN" if central else "PER"}/BV-{number:02}-C': (
                f'{title}, {PHY_VALUES[cis_mode][0]}',
                {'central': central, 'acl_mode': 0, 'cis_mode': cis_mode},
            )
            for central, number, cis_mode in cases
        },
    )

# Verify the responder's advertised power steps with and without compensation.
PROCEDURES |= procedure_cases(
    respond_to_power_requests,
    _phy_cases((8, 9, 10, 11), 'Power Control Response')
    | _phy_cases(
        (33, 34, 35, 36),
        'Power Control Response with RF Path Compensation',
        compensated=True,
    ),
)

# Propagate valid remote Min/Max flags and signed deltas into HCI.
PROCEDURES |= procedure_cases(
    report_remote_minimum_and_maximum,
    _role_cases(
        'BV-12', 'Power Configuration Response, Min and Max Power Level Reached'
    ),
)

# Handle an incoming request while the outgoing request awaits a response.
PROCEDURES |= procedure_cases(
    crossing_power_requests,
    _role_cases(
        'BV-17',
        'Properly handle a Power Request PDU when waiting for a Power Response PDU',
    ),
)

# Reject malformed PHY values in requests; indications do not elicit a report.
PROCEDURES |= procedure_cases(
    invalid_power_request_phy,
    _role_cases('BI-02', 'Power Control Request without a PHY', phy=0x00)
    | _role_cases('BI-06', 'Power Control Request with multiple PHYs', phy=0x03)
    | _role_cases('BI-07', 'Power Control Request with an RFU PHY', phy=0x80),
)
PROCEDURES |= procedure_cases(
    invalid_power_indication_phy,
    _role_cases('BI-08', 'Power Change Indication without a PHY', phy=0x00)
    | _role_cases('BI-13', 'Power Change Indication with an RFU PHY', phy=0xF0),
)

# Check the new power indication after the standard PHY update Instant.
PROCEDURES |= procedure_cases(
    power_indication_after_phy_change,
    {
        identifier: case
        for number, original, new in (
            (37, 0, 1),
            (38, 0, 2),
            (41, 1, 0),
            (42, 1, 2),
            (43, 2, 0),
            (44, 2, 1),
        )
        for identifier, case in _role_cases(
            f'BV-{number:02}',
            f'Power Change Indication, {PHY_VALUES[original][0]} '
            f'to {PHY_VALUES[new][0]}',
            original=original,
            new=new,
        ).items()
    },
)

# Compare a Peripheral's HCI remote read with the request, response and invalid TxPower.
PROCEDURES |= procedure_cases(
    read_remote_power,
    {'LL/PCL/PER/BV-29-C': ('Remote Transmit Power Level Request – LE 1M PHY', {})},
)

# Check local HCI events and no further power adjustment at the two limits.
PROCEDURES |= procedure_cases(
    report_local_minimum_and_maximum,
    _role_cases('BV-40', 'Max and Min Power Level Response at Max and Min Power'),
)

# Challenge the same maximum on each supported PHY/coding combination.
PROCEDURES |= procedure_cases(
    clamp_to_maximum_power,
    _phy_cases((53, 54, 55, 56), 'Power Control Response, Max Power'),
)

# Check both the Power Change Indication and the effective RF level.
PROCEDURES |= procedure_cases(
    power_indication_after_compensation,
    _role_cases(
        'BV-57', 'Send Power Change Indication After LE Write RF Path Compensation'
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES, radio_cases=PROCEDURES, radio=True),
)
async def test_power_control(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)
