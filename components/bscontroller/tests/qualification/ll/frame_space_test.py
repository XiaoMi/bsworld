# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL.TS.p28, Section 4.4: Frame Space Update."""

import struct
from contextlib import asynccontextmanager

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
from bscontroller.radio.base import Phy
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
from tests.qualification.iso import data_path, iso_packet
from tests.qualification.link import _EncryptedPeer, start_encryption
from tests.qualification.peer import (
    PEER_FEATURES,
    establish,
    exchange_iut_features,
    finish_phy_update,
    phy_bit,
    phy_update_pdu,
    received_acl,
)
from tests.qualification.tester import (
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
)
from tests.support import LE_EVENT_MASK_V1

Opcode = ControlOpcode
ErrorCode = hci.HCI_ErrorCode
FrameSpaceUpdate = hci.HCI_LE_Frame_Space_Update_Command
SpacingType = FrameSpaceUpdate.SpacingType
CP = SpacingType.T_IFS_ACL_CP
PC = SpacingType.T_IFS_ACL_PC
ACL_IFS = CP | PC
MCES = SpacingType.T_MCES
CIS_IFS = SpacingType.T_IFS_CIS
CIS_MSS = SpacingType.T_MSS_CIS
SPACING_TYPES = (CP, PC, MCES, CIS_IFS, CIS_MSS)
RFU_SPACING = 1 << 15
FSU_FEATURE = int(hci.LeFeatureMask.FRAME_SPACE_UPDATE)
EXTENDED_FEATURES = int(hci.LeFeatureMask.LL_EXTENDED_FEATURE_SET)
FEATURE_PAGE_0_MASK = (1 << 64) - 1
LOCAL_INITIATOR, PEER_INITIATOR = 0, 2

# The test IXIT selects the implementation's supported minimum and maximum.
# 6.8 ms is also exercised explicitly at the interval limit in BV-54 and BV-55.
FRAME_SPACE_MIN_US = 60
FRAME_SPACE_MAX_US = 10_000
LONG_FRAME_SPACE_US = 6_800
MONITORED_EVENTS = 20
PHY_ORDER = (Phy.LE_1M, Phy.LE_2M, Phy.LE_CODED)

REQ = Opcode.LL_FRAME_SPACE_REQ
RSP = Opcode.LL_FRAME_SPACE_RSP
REJECT = Opcode.LL_REJECT_EXT_IND


def request(minimum, maximum, phys, spacing):
    return struct.pack('<BHHBH', REQ, minimum, maximum, phys, spacing)


def response(space, phys, spacing):
    return struct.pack('<BHBH', RSP, space, phys, spacing)


def control(packet):
    if packet is not None and packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL:
        return packet.pdu[PDU_HEADER_SIZE:]
    return b''


def frame_event(event):
    return isinstance(event, hci.HCI_LE_Frame_Space_Update_Complete_Event)


class FrameSpacePeer(_EncryptedPeer):
    """Keep a wire-side copy of negotiated settings, independent of the IUT."""

    def __init__(self, link):
        super().__init__(link)
        self.spaces = {}

    @property
    def tx_type(self):
        return CP if self.central else PC

    @property
    def rx_type(self):
        return PC if self.central else CP

    @property
    def tx_ifs_us(self):
        return self.spaces.get((self.tx_type, self.tx_phy), LL_IFS_US)

    @property
    def rx_ifs_us(self):
        return self.spaces.get((self.rx_type, self.rx_phy), LL_IFS_US)

    def affects_transmit(self, phys, spacing):
        return bool(phys & phy_bit(self.tx_phy) and spacing & self.tx_type)

    def affects_receive(self, phys, spacing):
        return bool(phys & phy_bit(self.rx_phy) and spacing & self.rx_type)

    def apply(self, space, phys, spacing):
        for spacing_type in SPACING_TYPES:
            if spacing & spacing_type:
                for phy in PHY_ORDER:
                    if phys & phy_bit(phy):
                        self.spaces[spacing_type, phy] = space

    def request_max(self, maximum, phys, spacing):
        return max(
            maximum,
            *(
                self.spaces.get((spacing_type, phy), LL_IFS_US)
                for spacing_type in SPACING_TYPES
                for phy in PHY_ORDER
                if spacing & spacing_type and phys & phy_bit(phy)
            ),
        )

    @asynccontextmanager
    async def event(self):
        yield FrameEvent(self)
        self.anchor += self.parameters.interval * LL_CONN_INTERVAL_UNIT_US
        self.counter = self.counter + 1 & LL_EVENT_COUNTER_MASK


class FrameEvent:
    """Send and observe individual packets within one connection event."""

    def __init__(self, link):
        self.link = link
        self.tester = link.tester
        self.channel = link.channel
        self.previous_end = None
        self.sent_sn = None

    async def send(self, data=b'', llid=LlId.CONTINUATION, *, more=False, gap=None):
        link = self.link
        if self.previous_end is None:
            assert not link.central
            start = link.anchor
        else:
            start = self.previous_end + (link.tx_ifs_us if gap is None else gap)
        self.sent_sn = link.sn
        header = (
            llid
            | self.sent_sn * PDU_DATA_SN_MASK
            | link.nesn * PDU_DATA_NESN_MASK
            | more * PDU_DATA_MD_MASK
        )
        data = link.encode(header, data)
        self.previous_end = await self.tester.transmit(
            self.channel,
            bytes([header, len(data)]) + data,
            start_us=start,
            coding=link.tx_coding,
            power_dbm=link.tx_power_dbm,
            **link.radio_parameters,
        )
        link.last_sent_end_us = self.previous_end

    async def receive(self, gap=None, *, required=True):
        link = self.link
        if self.previous_end is None:
            assert link.central
            earliest = latest = link.anchor
        else:
            if gap is None:
                gap = link.rx_ifs_us
            lower, upper = gap if isinstance(gap, tuple) else (gap, gap)
            earliest, latest = self.previous_end + lower, self.previous_end + upper
        now = self.tester.radio.now_us
        packet = await self.tester.receive(
            self.channel,
            start_us=max(now + 1, earliest - 50),
            duration_us=max(1000, latest - now + 1000),
            **(link.radio_parameters | {'phy': link.rx_phy}),
        )
        if packet is None:
            assert not required, f'no LL packet at {earliest}–{latest} us'
            return None
        assert packet.valid_crc and packet.phy == link.rx_phy
        assert earliest <= packet.start_us <= latest, (
            packet.start_us,
            earliest,
            latest,
            link.counter,
        )
        assert packet.pdu[1] == len(packet.pdu) - PDU_HEADER_SIZE
        packet = link.decode(packet)
        self.previous_end = packet.end_us
        link._accept(packet)
        return packet

    def acknowledged(self, packet):
        assert int(bool(packet.pdu[0] & PDU_DATA_NESN_MASK)) != self.sent_sn


async def connect(tester, *, central, interval=80, features=None):
    link = FrameSpacePeer(
        await establish(
            tester,
            central=central,
            interval=interval,
            timeout=600,
            peer_address=(
                LOWER_PERIPHERAL_PUBLIC_ADDRESS
                if central
                else LOWER_CENTRAL_PUBLIC_ADDRESS
            ),
        )
    )
    if features is None:
        features = int(PEER_FEATURES) | FSU_FEATURE
    await exchange_iut_features(
        tester, link, (features & FEATURE_PAGE_0_MASK) | EXTENDED_FEATURES
    )
    page = (features >> 64).to_bytes(24, 'little')
    if central:
        await tester.command(
            hci.HCI_LE_Read_All_Remote_Features_Command(link.handle, 1)
        )
        await link.control(Opcode.LL_FEATURE_EXT_REQ)
        await link.exchange(
            bytes([Opcode.LL_FEATURE_EXT_RSP, 1, 1]) + page, llid=LlId.CONTROL
        )
        await link.exchange()
        event = await tester.event(hci.HCI_LE_Read_All_Remote_Features_Complete_Event)
        assert event.status == ErrorCode.SUCCESS
        assert event.le_features[8:32] == page
    else:
        await link.control(
            Opcode.LL_FEATURE_EXT_RSP, bytes([Opcode.LL_FEATURE_EXT_REQ, 1, 1]) + page
        )
        await link.exchange()
    tester.output.write(
        f'IXIT TSPX_frame_space_min={FRAME_SPACE_MIN_US} us '
        f'TSPX_frame_space_max={FRAME_SPACE_MAX_US} us\n'
    )
    return link


async def select_phy(tester, link, tx, rx, *, local):
    """Set the IUT's TX/RX PHY bits and confirm the change at its LL Instant."""
    if (link.rx_phy, link.tx_phy) == (tx, rx):
        return
    tx_mask, rx_mask = phy_bit(tx), phy_bit(rx)
    if local:
        await tester.command(
            hci.HCI_LE_Set_PHY_Command(link.handle, 0, tx_mask, rx_mask, 0)
        )
        requested = await link.control(Opcode.LL_PHY_REQ)
        assert requested[1] & tx_mask and requested[2] & rx_mask
    if link.central:
        kind = Opcode.LL_PHY_RSP if local else Opcode.LL_PHY_REQ
        update = await link.control(
            Opcode.LL_PHY_UPDATE_IND, bytes([kind, rx_mask, tx_mask])
        )
        allowed = (tx_mask, rx_mask)
    else:
        if not local:
            negotiated = await link.control(
                Opcode.LL_PHY_RSP, bytes([Opcode.LL_PHY_REQ, rx_mask, tx_mask])
            )
            assert negotiated[1] & tx_mask and negotiated[2] & rx_mask
        update = phy_update_pdu(
            link,
            rx_mask if rx != link.tx_phy else 0,
            tx_mask if tx != link.rx_phy else 0,
        )
        await link.exchange(update, llid=LlId.CONTROL)
        allowed = (rx_mask, tx_mask)
    await finish_phy_update(tester, link, update, event_required=True, allowed=allowed)
    assert (link.rx_phy, link.tx_phy) == (tx, rx)


async def complete(link, *, initiator, status=ErrorCode.SUCCESS, expected=None):
    event = await link.tester.expect(
        lambda item: frame_event(item) and item.initiator == initiator
    )
    assert (event.status, event.connection_handle) == (status, link.handle)
    if expected is not None:
        assert (event.frame_space, event.phys, event.spacing_types) == expected
    return event


async def check_timing(link, count=MONITORED_EVENTS, *, forbidden=None):
    """Prove both gaps in each event, including reception and ACK at the peer gap."""
    seen = 0
    for _ in range(count):
        async with link.event() as event:
            if link.central:
                first = await event.receive(required=False)
                if first is None:
                    continue
                assert forbidden is None or control(first)[:1] != bytes([forbidden])
            await event.send(more=True)
            packet = await event.receive(required=False)
            if packet is None:
                continue
            assert forbidden is None or control(packet)[:1] != bytes([forbidden])
            event.acknowledged(packet)
            if not link.central:
                await event.send()
                packet = await event.receive(required=False)
                if packet is None:
                    continue
                event.acknowledged(packet)
            else:
                await event.send()
            seen += 1
    assert seen * 100 >= 95 * count


@asynccontextmanager
async def host_request(link, minimum, maximum, phys, spacing):
    """Yield the live event when an admitted Host request reaches the air."""
    tester = link.tester
    await tester.command(FrameSpaceUpdate(link.handle, minimum, maximum, phys, spacing))
    expected = request(minimum, link.request_max(maximum, phys, spacing), phys, spacing)
    for _ in range(8):
        async with link.event() as event:
            if link.central:
                packet = await event.receive()
                if not control(packet):
                    await event.send(more=True)
                    packet = await event.receive()
            else:
                await event.send(more=True)
                packet = await event.receive()
            if control(packet)[:1] != bytes([REQ]):
                if link.central:
                    await event.send()
                continue
            assert control(packet) == expected
            yield event
            return
    raise AssertionError('LL_FRAME_SPACE_REQ was not received')


async def initiate(
    link,
    minimum,
    maximum,
    phys,
    spacing,
    space,
    *,
    rfu=False,
    probe=False,
):
    input_affected = link.affects_transmit(phys, spacing)
    gap = space if input_affected else link.tx_ifs_us
    probe_gap = minimum + 1 if probe and input_affected else None
    async with host_request(link, minimum, maximum, phys, spacing) as event:
        if probe_gap is not None:
            payload = b'\x11\x22'
            await event.send(payload, LlId.START, more=True, gap=probe_gap)
            event.acknowledged(await event.receive())
            await received_acl(link.tester, link, payload, False)
        await event.send(
            response(space, phys, spacing | (RFU_SPACING if rfu else 0)),
            LlId.CONTROL,
            gap=gap,
            more=True,
        )
    await complete(link, initiator=LOCAL_INITIATOR, expected=(space, phys, spacing))
    link.apply(space, phys, spacing)


async def request_from_peer(link, payload, *, allow_deferred=False):
    """Inject an LL request, allowing a deferred rejection on a crowded link."""
    async with link.event() as event:
        if link.central:
            await event.receive()
        await event.send(payload, LlId.CONTROL, more=True)
        packet = await event.receive(required=not allow_deferred)
        if packet is not None:
            event.acknowledged(packet)
            if link.central:
                await event.send()
            return control(packet)
    return await link.control(REJECT)


async def respond(link, minimum, maximum, phys, spacing, *, probe=False):
    """Validate the selected masks, response timing, and transitional RX window."""
    old_tx_gap = link.tx_ifs_us
    async with link.event() as event:
        if link.central:
            await event.receive()
        await event.send(
            request(minimum, maximum, phys, spacing), LlId.CONTROL, more=True
        )
        previous = event.previous_end
        rx_range = (
            (minimum, maximum)
            if link.affects_receive(phys, spacing)
            else link.rx_ifs_us
        )
        packet = await event.receive(rx_range)
        event.acknowledged(packet)
        opcode, space, selected_phys, selected_spacing = struct.unpack(
            '<BHBH', control(packet)
        )
        assert opcode == RSP, control(packet)
        assert minimum <= space <= maximum
        assert selected_phys == phys
        selected = spacing & int(ACL_IFS | CIS_IFS | CIS_MSS)
        assert selected_spacing and selected_spacing & ~selected == 0
        assert packet.start_us - previous == (
            space
            if link.affects_receive(selected_phys, selected_spacing)
            else link.rx_ifs_us
        )
        link.apply(space, selected_phys, selected_spacing)
        if probe and link.affects_transmit(selected_phys, selected_spacing):
            # Source probes one microsecond inside the range between old and new.
            assert space != old_tx_gap
            edge = space + (1 if space < old_tx_gap else -1)
            data = b'\x33\x44'
            await event.send(data, LlId.START, more=True, gap=edge)
            event.acknowledged(await event.receive())
            await received_acl(link.tester, link, data, False)
        elif link.central:
            await event.send()
    await complete(
        link,
        initiator=PEER_INITIATOR,
        expected=(space, selected_phys, selected_spacing),
    )
    return space


async def current_phy_initiator(tester, *, central, tx, rx, encrypted):
    """Initiate inter-frame changes on the PHYs in use.

    Initial Condition:
        Both devices support Frame Space Update. Use the selected IUT PHYs and
        encryption setting, starting with 150 µs in both directions.

    Test Case Configuration:
        Tables 4.4-7/18 select the symmetric rounds and Tables 4.4-8/19 the
        asymmetric PHY and encryption combinations.

    Test Steps:
        1. Establish the ACL, select the PHYs, enable encryption when selected
           and monitor the two directions at 150 µs.
        2. Send the Host command for each direction and then for both together.
           Check the request on air, including directional PHY selection.
        3. Send the selected responses at both extrema. In the Central
           symmetric cases, inject the RFU spacing bit and probe the
           widened window at FS_Min + 1 µs.
        4. Check the HCI completion and observe twenty events per setting.

    Expected Outcome:
        At least 95% of monitored events use the selected spacings. Each
        response at a negotiated extreme is received, and values apply by the
        sixth connection anchor according to Core. Unselected gaps retain their
        previous values.

    Source Correction:
        Apply the revised direction bits from TSE 27953. For a return to 150 µs,
        Core E 7.8.151 raises the request's wire maximum to the current value if
        needed, so the Lower Tester selects 150 µs in that expanded range.

    Reference:
        LL.TS.p28, 4.4.3.4–5, 4.4.4.2–3, Tables 4.4-7/8/17/18/19;
        Core Vol 6, Part B, 4.1.1 and 5.1.30–5.1.30.1.
    """
    link = await connect(tester, central=central)
    await select_phy(tester, link, tx, rx, local=True)
    if encrypted:
        await start_encryption(tester, link)
    await check_timing(link)

    symmetric = tx == rx
    if central and symmetric:
        phys = phy_bit(tx)
        for spacing, space in (
            (CP, FRAME_SPACE_MIN_US),
            (PC, FRAME_SPACE_MAX_US),
            (ACL_IFS, 280),
        ):
            await initiate(
                link,
                FRAME_SPACE_MIN_US,
                FRAME_SPACE_MAX_US,
                phys,
                spacing,
                space,
                rfu=True,
                probe=True,
            )
            await check_timing(link)
    else:
        own_direction = PC if central else CP
        peer_direction = CP if central else PC
        await initiate(
            link,
            FRAME_SPACE_MIN_US,
            LL_IFS_US,
            phy_bit(tx),
            own_direction,
            FRAME_SPACE_MIN_US,
        )
        await check_timing(link)
        await initiate(
            link,
            FRAME_SPACE_MIN_US,
            FRAME_SPACE_MAX_US,
            phy_bit(rx),
            peer_direction,
            FRAME_SPACE_MAX_US,
        )
        await check_timing(link)
        await initiate(
            link, LL_IFS_US, LL_IFS_US, phy_bit(tx) | phy_bit(rx), ACL_IFS, LL_IFS_US
        )
        await check_timing(link)
    await link.disconnect()


async def current_phy_responder(tester, *, central, phy, encrypted):
    """Respond to directional inter-frame changes on the current PHY.

    Initial Condition:
        Symmetric selected PHYs, the selected encryption state, and initial
        CP and PC gaps of 150 µs.

    Test Case Configuration:
        Select the role, PHY and encryption from Table 4.4-9 or 4.4-20.

    Test Steps:
        1. Monitor twenty events with both gaps at 150 µs.
        2. Send the three variants in Table 4.4-10 or 4.4-21, setting the
           highest RFU spacing bit.
        3. Verify the response uses the new outgoing gap where applicable.
           Transmit a data PDU one microsecond inside the old/new receive
           window and require its acknowledgment and HCI delivery.
        4. Check the reported selection and monitor both resulting gaps.

    Expected Outcome:
        RFU is clear in the returned masks. The responder accepts the probe
        in the transitional receive window and subsequent traffic uses the
        selected spacings in at least 95% of observed events.

    Source Correction:
        For Central Step 8 the description labels bit 1 as CP. Bit 1 is PC,
        as also stated by the sentence's verdict and Table 2.41 in Core.

    Reference:
        LL.TS.p28, 4.4.3.6, 4.4.4.4, Tables 4.4-9/10/20/21;
        Core Vol 6, Part B, 2.4.2.54 and 5.1.30.1.
    """
    link = await connect(tester, central=central)
    await select_phy(tester, link, phy, phy, local=True)
    if encrypted:
        await start_encryption(tester, link)
    await check_timing(link)
    for minimum, maximum, spacing in (
        (FRAME_SPACE_MIN_US, LL_IFS_US, CP),
        (FRAME_SPACE_MIN_US, LL_IFS_US, PC),
        (FRAME_SPACE_MAX_US, FRAME_SPACE_MAX_US, ACL_IFS),
    ):
        await respond(
            link, minimum, maximum, phy_bit(phy), spacing | RFU_SPACING, probe=True
        )
        await check_timing(link)
    await link.disconnect()


async def different_phy(tester, *, central, original, target, local):
    """Preserve the current gaps when programming another PHY.

    Initial Condition:
        The link uses the original IUT TX/RX pair with both frame spaces
        at 150 µs.

    Test Case Configuration:
        Role, initiator, original and target pair follow Tables 4.4-11 through
        4.4-14 and 4.4-22 through 4.4-25.

    Test Steps:
        1. Monitor the original PHYs. For the symmetric procedure, negotiate
           both spacing types on the target PHY. For the asymmetric procedure
           negotiate a different spacing for each of the target directions.
        2. Check the LL and HCI responses and verify the original PHYs continue
           to use their original gaps prior to the PHY Update Instant.
        3. Change PHY by the initiator specified by the case. Check the HCI
           PHY Update Complete event and monitor twenty new-parameter events.

    Expected Outcome:
        The spacings are scoped to selected PHY and direction. They only
        change active RF timing once the applicable PHY is in use.

    Source Correction:
        The Central asymmetric initiator Step 9 reverses the minimum and
        maximum when the advertised minimum is below 150. Select a valid
        ascending range with 150 as lower bound as in the corresponding
        Peripheral procedure. The Central symmetric initiator's early traffic
        stays at 150 before the Instant because the target PHY is not in use.

    Reference:
        LL.TS.p28, 4.4.3.7–10, 4.4.4.5–8; Core Vol 6, Part B, 4.1.1,
        5.1.10 and 5.1.30.1.
    """
    link = await connect(tester, central=central)
    await select_phy(tester, link, *original, local=False)
    await check_timing(link)
    tx, rx = target
    if tx == rx:
        rounds = (
            (FRAME_SPACE_MIN_US, LL_IFS_US, phy_bit(tx), ACL_IFS, FRAME_SPACE_MIN_US),
        )
    else:
        own_direction = PC if central else CP
        peer_direction = CP if central else PC
        rounds = (
            (
                FRAME_SPACE_MIN_US,
                LL_IFS_US,
                phy_bit(tx),
                own_direction,
                FRAME_SPACE_MIN_US,
            ),
            (150, FRAME_SPACE_MAX_US, phy_bit(rx), peer_direction, 200),
        )
    for minimum, maximum, phys, spacing, space in rounds:
        if local:
            await initiate(link, minimum, maximum, phys, spacing, space)
        else:
            # Keep FS_Max above the existing 150-us setting, including decreases.
            await respond(link, space, maximum, phys, spacing)
    assert link.rx_ifs_us == link.tx_ifs_us == LL_IFS_US
    await check_timing(link)
    await select_phy(tester, link, tx, rx, local=local)
    await check_timing(link)
    await link.disconnect()


async def same_procedure_collision(tester, *, central):
    """Give the Central request precedence in a same-procedure collision.

    Initial Condition:
        An unencrypted LE 1M ACL with both directional gaps at 150 µs.

    Test Case Configuration:
        CEN BV-24 tests the Central winner and PER BV-27 the Peripheral loser.

    Test Steps:
        1. Admit a Host request and wait for its first LL transmission.
        2. Cross an LL_FRAME_SPACE_REQ from the Lower Tester. Supply the
           appropriate LL response or LL Procedure Collision rejection.
        3. Check the HCI outcomes and monitor the negotiated gaps.

    Expected Outcome:
        Central rejects the peer request with 0x23 and completes its own.
        Peripheral processes the Central request and reports both the peer
        success and the local 0x23 rejection.

    Source Correction:
        PER BV-27 states 200 µs in its verdict but uses the advertised IXIT
        maximum in the test steps. Check the actual negotiated IXIT value.

    Reference:
        LL.TS.p28, pages 4.4-35–37 and 4.4-74–75, Figures 4.4-18/36;
        Core Vol 6, Part B, 5.3.
    """
    link = await connect(tester, central=central)
    await check_timing(link)
    phys = phy_bit(Phy.LE_1M)
    minimum, maximum = (
        (LL_IFS_US, FRAME_SPACE_MAX_US) if central else (FRAME_SPACE_MIN_US, LL_IFS_US)
    )
    expected = (FRAME_SPACE_MAX_US, phys, ACL_IFS)
    reject = bytes(
        [
            REJECT,
            REQ,
            ErrorCode.LMP_ERROR_TRANSACTION_COLLISION_OR_LL_PROCEDURE_COLLISION_ERROR,
        ]
    )
    async with host_request(link, minimum, maximum, phys, ACL_IFS) as event:
        if central:
            await event.send(
                request(FRAME_SPACE_MIN_US, LL_IFS_US, phys, ACL_IFS),
                LlId.CONTROL,
                more=True,
            )
            assert control(await event.receive()) == reject
            await event.send(response(*expected), LlId.CONTROL, gap=FRAME_SPACE_MAX_US)
        else:
            await event.send(
                request(FRAME_SPACE_MAX_US, FRAME_SPACE_MAX_US, phys, ACL_IFS),
                LlId.CONTROL,
                more=True,
            )
            assert control(await event.receive(FRAME_SPACE_MAX_US)) == response(
                *expected
            )
            await event.send(reject, LlId.CONTROL, gap=FRAME_SPACE_MAX_US)
    if central:
        await complete(link, initiator=LOCAL_INITIATOR, expected=expected)
    else:
        await complete(
            link,
            initiator=LOCAL_INITIATOR,
            status=ErrorCode.LMP_ERROR_TRANSACTION_COLLISION_OR_LL_PROCEDURE_COLLISION_ERROR,
        )
        await complete(link, initiator=PEER_INITIATOR, expected=expected)
    link.apply(*expected)
    await check_timing(link)
    await link.disconnect()


async def invalid_wire_parameters(tester, *, central):
    """Reject invalid requests and ignore invalid response selections.

    Initial Condition:
        Unencrypted LE 1M ACL with both gaps at 150 µs.

    Test Case Configuration:
        CEN BI-01 and PER BI-01 run the same sequence in opposite roles.

    Test Steps:
        1. Exercise the four malformed LL requests from Table 4.4-31.
        2. For two Host requests, send a response with unrequested PHY
           and/or spacing bits. Require failure and unchanged RF timing.
        3. Send a request with FS_Max = 10001 and verify that it has no
           effect on the connection.

    Expected Outcome:
        Invalid requests receive an extended rejection. A zero PHYS or
        Spacing_Types field is rejected with Invalid LL Parameters (0x1E).
        Unexpected response bits do not alter either spacing or report a
        successful Host update.

    Source Correction:
        Step 9 omits clean rejection and permits negotiating with a request
        above 10000 µs. Core limits FS_Max to 10000 and specifies an extended
        rejection of a malformed parameter. This suite checks that rejection.

    Reference:
        LL.TS.p28, 4.4.5.3, Tables 4.4-30/31, Figures 4.4-47/48;
        Core Vol 6, Part B, 2.4.2.54 and 5.1.30.
    """
    link = await connect(tester, central=central)
    phys = phy_bit(Phy.LE_1M)
    for parameters in (
        (FRAME_SPACE_MAX_US, FRAME_SPACE_MAX_US - 1, phys, CP),
        (151, 150, phys, CP),
    ):
        rejected = await request_from_peer(link, request(*parameters))
        assert len(rejected) == 3 and rejected[:2] == bytes([REJECT, REQ])
        assert rejected[2] != ErrorCode.SUCCESS
    for phys_field, spacing in ((phys, 0), (0, CP)):
        rejected = await request_from_peer(
            link, request(FRAME_SPACE_MIN_US, LL_IFS_US, phys_field, spacing)
        )
        assert rejected == bytes(
            [REJECT, REQ, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR]
        )
    for spacing, other_phy in ((CP, phy_bit(Phy.LE_2M)), (PC, phys)):
        async with host_request(
            link, FRAME_SPACE_MIN_US, FRAME_SPACE_MAX_US, phys, spacing
        ) as event:
            await event.send(response(200, other_phy, ACL_IFS), LlId.CONTROL)
        await complete(
            link,
            initiator=LOCAL_INITIATOR,
            status=ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR,
        )
        await check_timing(link, 3)
    rejected = await request_from_peer(
        link, request(LL_IFS_US, FRAME_SPACE_MAX_US + 1, phys, CP)
    )
    assert rejected == bytes(
        [REJECT, REQ, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR]
    )
    assert not any(frame_event(item) for item in tester.events)
    await check_timing(link, 3)
    await link.disconnect()


async def remote_feature_not_supported(tester, *, central, phy, spacing):
    """Fail an FSU targeting a feature that the remote Controller lacks.

    Initial Condition:
        Unencrypted LE 1M ACL. Lower Tester feature bits 8, 11, 28, and 29
        are clear.

    Test Case Configuration:
        Select role, requested PHY, and spacing from Table 4.4-34.

    Test Steps:
        Request the specified PHY and spacing through HCI. If admitted,
        observe the LL request and have the Lower Tester reject it with 0x1A.

    Expected Outcome:
        Command Status or the eventual Host completion reports Unsupported
        Remote Feature. Existing frame spaces remain at 150 µs.

    Reference:
        LL.TS.p28, 4.4.5.6, Table 4.4-34, Figure 4.4-51.
    """
    features = int(PEER_FEATURES) | FSU_FEATURE
    features &= ~int(
        hci.LeFeatureMask.LE_2M_PHY
        | hci.LeFeatureMask.LE_CODED_PHY
        | hci.LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_CENTRAL
        | hci.LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_PERIPHERAL
    )
    link = await connect(tester, central=central, features=features)
    phys = phy_bit(phy)
    status = await tester.command(
        FrameSpaceUpdate(link.handle, FRAME_SPACE_MIN_US, LL_IFS_US, phys, spacing),
        (ErrorCode.SUCCESS, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR),
    )
    if status.status == ErrorCode.SUCCESS:
        reject = bytes([REJECT, REQ, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR])
        if link.central:
            payload = await link.control(REQ, response=reject)
        else:
            payload = await link.control(REQ)
            await link.exchange(reject, llid=LlId.CONTROL)
        assert payload == request(FRAME_SPACE_MIN_US, LL_IFS_US, phys, spacing)
        await complete(
            link,
            initiator=LOCAL_INITIATOR,
            status=ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR,
        )
    else:
        await check_timing(link, 5, forbidden=REQ)
    assert not any(frame_event(item) for item in tester.events)
    await link.disconnect()


async def verify_cis_spacing(air, handle, mss_us):
    """Exchange full data PDUs to exercise the negotiated RF timing."""
    tester = air.link.tester
    # LE 1M airtime for 251 data octets, MIC and packet overhead in each direction.
    assert air.subinterval_us >= 2 * (251 + 4 + 10) * 8 + air.spacing_us + mss_us
    while air.nominal_us < tester.radio.now_us + 60 or air.subevent:
        air.step()
    for _ in range(MONITORED_EVENTS):
        sequence = air.counter
        tx = bytes([((sequence & 0x7F) << 1) | 1]) * 251
        rx = bytes([(sequence & 0x7F) << 1]) * 251
        tester.send(bytes(iso_packet(handle, sequence & 0xFFFF, tx)))
        packet = await air.exchange(rx, header=(sequence & 1) << 3)
        assert packet.pdu[2:] == tx
        # A subsequent subevent acknowledges the received SDU.
        packet = await air.exchange()
        assert bool(packet.pdu[0] & 0x04) != bool(sequence & 1)
        while air.subevent:
            await air.exchange()


async def connect_fsu_cis(tester, *, central, interval):
    await tester.masks(le=LE_EVENT_MASK_V1)
    await enable_cis(tester)
    return await connect(
        tester,
        central=central,
        interval=interval,
        features=PEER_CIS_FEATURES | FSU_FEATURE,
    )


async def cis_frame_space(tester, *, central, local):
    """Use negotiated CIS spacing for the next CIS on the same ACL.

    Initial Condition:
        Unencrypted LE 1M ACL and a bidirectional CIS with 100 ms SDU and ISO
        intervals. The initial CIS inter-frame and minimum subevent spaces
        are 150 µs.

    Test Case Configuration:
        Select the IUT role and FSU initiator. In three rounds select CIS
        inter-frame space, minimum subevent space, and both, respectively.

    Test Steps:
        Exchange SDUs on the original CIS and negotiate the spacing update.
        Verify timing on the existing CIS, terminate it, and recreate it on
        the same ACL. Exchange SDUs for 20 intervals on the new CIS.

    Expected Outcome:
        The update completes with the selected spacing types and values.
        The existing CIS retains its spacing. The new CIS uses the negotiated
        inter-frame and minimum subevent spaces.

    Reference:
        LL.TS.p28, 4.4.5.1–2, Tables 4.4-26 through 4.4-29, Figures 4.4-45–46.
    """
    interval_us = 100000
    link = await connect_fsu_cis(tester, central=central, interval=80)
    phys = phy_bit(Phy.LE_1M)
    ifs_us = mss_us = LL_IFS_US

    async def create(address):
        _, air, event = await establish_peer_cis(
            tester,
            central=central,
            link=link,
            interval_us=interval_us,
            ifs_us=ifs_us,
            mss_us=mss_us,
            access_address=address,
        )
        handle = event.connection_handle
        await data_path(tester, handle, 0)
        await data_path(tester, handle, 1)
        return air, handle

    air, handle = await create(0x7654BA98)
    await verify_cis_spacing(air, handle, mss_us)
    for spacing, minimum, maximum, address in (
        (CIS_IFS, FRAME_SPACE_MIN_US, ifs_us, 0x2265B1F5),
        (CIS_MSS, FRAME_SPACE_MIN_US, mss_us, 0x91B7584A),
        (CIS_IFS | CIS_MSS, FRAME_SPACE_MAX_US, FRAME_SPACE_MAX_US, 0x1027C4D1),
    ):
        if local:
            await initiate(link, minimum, maximum, phys, spacing, minimum)
            selected = minimum
        else:
            selected = await respond(link, minimum, maximum, phys, spacing)
        await verify_cis_spacing(air, handle, mss_us)
        await disconnect_cis(tester, link, handle)
        if central:
            await tester.command(hci.HCI_LE_Remove_CIG_Command(1))
        if spacing & CIS_IFS:
            ifs_us = selected
        if spacing & CIS_MSS:
            mss_us = selected
        air, handle = await create(address)
        await verify_cis_spacing(air, handle, mss_us)
    await disconnect_cis(tester, link, handle)
    await link.disconnect()


async def cis_creation_collision(tester):
    """Accept CIS creation while a local Frame Space Update is outstanding.

    Initial Condition:
        The IUT is connected as an unencrypted LE 1M Peripheral. The Lower
        Tester keeps MD set in the ACL exchange.

    Test Steps:
        Request Frame Space Update locally. After its request reaches the air,
        send LL_CIS_REQ and reject the FSU with LL Procedure Collision.
        Accept and establish the CIS, then exchange ISO data at the original
        spacing.

    Expected Outcome:
        HCI reports the FSU error and completes CIS establishment. The CIS
        exchanges the expected data with the original 150 µs spacing.

    Reference:
        LL.TS.p28, 4.4.3.6, LL/TIM/PER/BV-28-C, Figure 4.4-19.
    """
    link = await connect_fsu_cis(tester, central=False, interval=32)
    await check_timing(link)
    phys = phy_bit(Phy.LE_1M)
    async with host_request(
        link, FRAME_SPACE_MIN_US, LL_IFS_US, phys, ACL_IFS
    ) as event:
        await event.send(more=True)
        event.acknowledged(await event.receive())

    async def reject_frame_space(_handle):
        error = (
            ErrorCode.LMP_ERROR_TRANSACTION_COLLISION_OR_LL_PROCEDURE_COLLISION_ERROR
        )
        await link.exchange(bytes((REJECT, REQ, error)), llid=LlId.CONTROL)
        await complete(link, initiator=LOCAL_INITIATOR, status=error)

    _, air, established = await establish_peer_cis(
        tester,
        central=False,
        link=link,
        before_accept=reject_frame_space,
        offset_us=5000,
    )
    handle = established.connection_handle
    await data_path(tester, handle, 0)
    await data_path(tester, handle, 1)
    await verify_cis_spacing(air, handle, LL_IFS_US)
    await disconnect_cis(tester, link, handle)
    await link.disconnect()


async def reject_active_interval(tester, *, central):
    """Reject a proposed spacing that cannot fit the active 7.5 ms interval.

    Initial Condition:
        An unencrypted LE 1M ACL with a 7.5 ms interval.

    Test Case Configuration:
        Central and Peripheral variants use both Host and peer requests.

    Test Steps:
        1. Deliver a 27-octet LL Data PDU to the IUT.
        2. Request a 6.8 ms CP gap locally and require Invalid HCI Command
           Parameters.
        3. Send the same request on air and require the extended rejection.

    Expected Outcome:
        The active connection rejects both changes and keeps 150 µs spacing.

    Source Correction:
        The LL.TS verdict says 0x11 for the LL rejection. Core Vol 6, Part B,
        5.1.30 mandates Unsupported LL Parameter Value (0x20) for a gap which
        would make the PHY-specific minimum exceed the active interval.

    Reference:
        LL.TS.p28, 4.4.3.11, Table 4.4-15, Figure 4.4-25.
    """
    link = await connect(tester, central=central, interval=6)
    data = bytes(range(27))
    async with link.event() as event:
        if link.central:
            await event.receive()
        await event.send(data, LlId.START, more=True)
        event.acknowledged(await event.receive())
        if link.central:
            await event.send()
    await received_acl(tester, link, data, False)
    phys = phy_bit(Phy.LE_1M)
    await tester.command(
        FrameSpaceUpdate(
            link.handle, LONG_FRAME_SPACE_US, LONG_FRAME_SPACE_US, phys, CP
        ),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    rejected = await request_from_peer(
        link, request(LONG_FRAME_SPACE_US, LONG_FRAME_SPACE_US, phys, CP)
    )
    assert rejected == bytes(
        [REJECT, REQ, ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR]
    )
    assert not any(frame_event(event) for event in tester.events)
    await check_timing(link, 3)
    await link.disconnect()


async def reject_shorter_interval(tester, *, peer_request):
    """Keep a large negotiated spacing when asked for an incompatible interval.

    Initial Condition:
        Central IUT on a 15 ms LE 1M ACL with CP spacing set to 6.8 ms.

    Test Case Configuration:
        BV-54: Host connection update with no remote LL parameter request
        support. BV-55: Lower Tester initiates an LL parameter request.

    Test Steps:
        1. Negotiate and observe the 6.8 ms CP spacing at 15 ms.
        2. Request a 7.5 ms interval through the case's entry point.
        3. Verify rejection and check that no incompatible update is sent.

    Expected Outcome:
        The interval and negotiated timing are preserved. The IUT rejects
        the 7.5 ms interval before queuing a connection update.

    Reference:
        LL.TS.p28, pages 4.4-89–91, Figures 4.4-43/44; Core Vol 6, Part B,
        4.5.10, 5.1.1 and 5.1.7.
    """
    features = int(PEER_FEATURES) | FSU_FEATURE
    if peer_request:
        features |= int(hci.LeFeatureMask.CONNECTION_PARAMETERS_REQUEST_PROCEDURE)
    link = await connect(tester, central=True, interval=12, features=features)
    await initiate(
        link,
        LONG_FRAME_SPACE_US,
        LONG_FRAME_SPACE_US,
        phy_bit(Phy.LE_1M),
        CP,
        LONG_FRAME_SPACE_US,
    )
    await check_timing(link, 1)
    if peer_request:
        parameters = struct.pack(
            '<B4HB7H',
            Opcode.LL_CONNECTION_PARAM_REQ,
            6,
            6,
            0,
            link.parameters.timeout,
            0,
            link.counter,
            *(0xFFFF for _ in range(6)),
        )
        rejected = await request_from_peer(link, parameters, allow_deferred=True)
        assert len(rejected) == 3 and rejected[:2] == bytes(
            [REJECT, Opcode.LL_CONNECTION_PARAM_REQ]
        )
        assert rejected[2] != ErrorCode.SUCCESS
        await check_timing(link, 3, forbidden=Opcode.LL_CONNECTION_UPDATE_IND)
    else:
        await tester.command(
            hci.HCI_LE_Connection_Update_Command(
                link.handle, 6, 6, 0, link.parameters.timeout, 0, 0
            ),
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        )
        await check_timing(link, 50, forbidden=Opcode.LL_CONNECTION_UPDATE_IND)
    await link.disconnect(local=False)


PHY_LABELS = ('LE 1M', 'LE 2M', 'LE Coded')
LE_1M, LE_2M, LE_CODED = PHY_ORDER
PHY_TRANSITIONS = (
    (LE_1M, LE_2M),
    (LE_1M, LE_CODED),
    (LE_2M, LE_1M),
    (LE_2M, LE_CODED),
    (LE_CODED, LE_1M),
    (LE_CODED, LE_2M),
)
ASYMMETRIC_TRANSITIONS = (
    ((LE_1M, LE_2M), (LE_2M, LE_1M)),
    ((LE_1M, LE_CODED), (LE_CODED, LE_2M)),
    ((LE_2M, LE_1M), (LE_1M, LE_2M)),
    ((LE_2M, LE_CODED), (LE_CODED, LE_1M)),
    ((LE_CODED, LE_1M), (LE_2M, LE_CODED)),
    ((LE_CODED, LE_2M), (LE_1M, LE_CODED)),
)


def phy_label(phy):
    return PHY_LABELS[PHY_ORDER.index(phy)]


def case_id(central, number, *, invalid=False):
    role = 'CEN' if central else 'PER'
    category = 'BI' if invalid else 'BV'
    return f'LL/TIM/{role}/{category}-{number:02}-C'


def phy_cases(central, start, *, symmetric, local):
    variants = (
        tuple(((old, old), (new, new)) for old, new in PHY_TRANSITIONS)
        if symmetric
        else ASYMMETRIC_TRANSITIONS
    )
    action = 'Initiate' if local else 'Respond to'
    return {
        case_id(central, index): (
            f'{action} Frame Space Update on a different PHY, '
            f'{phy_label(original[0])}/{phy_label(original[1])} to '
            f'{phy_label(target[0])}/{phy_label(target[1])}',
            {
                'central': central,
                'original': original,
                'target': target,
                'local': local,
            },
        )
        for index, (original, target) in enumerate(variants, start)
    }


PROCEDURES = ProcedureMap()

# Same PHY, current directional gap and encryption matrices in Tables 4.4-7/8/9
# and 4.4-17/18/19/20.
for central, local_start, asymmetric_start, peer_start, asymmetric_pair in (
    (True, 8, 14, 18, (LE_2M, LE_CODED)),
    (False, 11, 17, 21, (LE_1M, LE_CODED)),
):
    for encrypted in (False, True):
        security = 'encrypted' if encrypted else 'unencrypted'
        PROCEDURES |= procedure_cases(
            current_phy_initiator,
            {
                case_id(central, local_start + 3 * encrypted + index): (
                    f'Initiate Frame Space Update on {phy_label(phy)}, {security}',
                    {'central': central, 'tx': phy, 'rx': phy, 'encrypted': encrypted},
                )
                for index, phy in enumerate(PHY_ORDER)
            }
            | {
                case_id(central, asymmetric_start + 2 * encrypted + index): (
                    f'Initiate Frame Space Update on '
                    f'{phy_label(tx)}/{phy_label(rx)}, {security}',
                    {'central': central, 'tx': tx, 'rx': rx, 'encrypted': encrypted},
                )
                for index, (tx, rx) in enumerate(((LE_1M, LE_2M), asymmetric_pair))
            },
        )
        PROCEDURES |= procedure_cases(
            current_phy_responder,
            {
                case_id(central, peer_start + 3 * encrypted + index): (
                    f'Respond to Frame Space Update on {phy_label(phy)}, {security}',
                    {'central': central, 'phy': phy, 'encrypted': encrypted},
                )
                for index, phy in enumerate(PHY_ORDER)
            },
        )

# Future-PHY timing selections and LL_PHY update at the specified Instant.
for central, first in ((True, 25), (False, 29)):
    for offset, symmetric, local in (
        (0, True, True),
        (6, True, False),
        (12, False, True),
        (18, False, False),
    ):
        PROCEDURES |= procedure_cases(
            different_phy,
            phy_cases(central, first + offset, symmetric=symmetric, local=local),
        )

# Same-procedure collision and unsupported or malformed selections.
PROCEDURES |= procedure_cases(
    same_procedure_collision,
    {
        case_id(True, 24): (
            'Frame Space Request Collision, Central',
            {'central': True},
        ),
        case_id(False, 27): (
            'Frame Space Request Collision, Peripheral',
            {'central': False},
        ),
    },
)
for number, procedure, title in (
    (1, invalid_wire_parameters, 'Frame Space Change, Invalid Parameters'),
    (5, reject_active_interval, 'Frame Space Change, insufficient active interval'),
):
    PROCEDURES |= procedure_cases(
        procedure,
        {
            case_id(central, number, invalid=True): (title, {'central': central})
            for central in (False, True)
        },
    )
PROCEDURES |= procedure_cases(
    remote_feature_not_supported,
    {
        case_id(central, first + index): (
            'Frame Space Change, remote device does not support '
            f'{("CIS" if spacing == CIS_IFS else phy_label(phy))}',
            {'central': central, 'phy': phy, 'spacing': spacing},
        )
        for central, first in ((True, 51), (False, 55))
        for index, (phy, spacing) in enumerate(
            ((LE_2M, CP), (LE_CODED, CP), (LE_1M, CIS_IFS))
        )
    },
)

# CIS frame space values are snapshotted at CIS creation.
PROCEDURES |= procedure_cases(
    cis_frame_space,
    {
        case_id(central, number): (
            description,
            {'central': central, 'local': local},
        )
        for central, number, local, description in (
            (True, 49, True, 'Initiate a Frame Space Change in a CIS, Central'),
            (False, 53, True, 'Initiate a Frame Space Change in a CIS, Peripheral'),
            (
                True,
                50,
                False,
                'Respond to a Frame Space Change Request in a CIS, Central',
            ),
            (
                False,
                54,
                False,
                'Respond to a Frame Space Change Request in a CIS, Peripheral',
            ),
        )
    },
)
PROCEDURES |= procedure_cases(
    cis_creation_collision,
    {
        'LL/TIM/PER/BV-28-C': (
            'Handling Frame Space Request Collision with CIS Creation, Peripheral',
            {},
        )
    },
)

# Reject a connection parameter change that would violate the negotiated spacing.
PROCEDURES |= procedure_cases(
    reject_shorter_interval,
    {
        case_id(True, 54): (
            'Frame Space Change, Connection Update, insufficient time',
            {'peer_request': False},
        ),
        case_id(True, 55): (
            'Frame Space Change, Connection Parameters Request, insufficient time',
            {'peer_request': True},
        ),
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'identifier,description,procedure,parameters,marks',
    collect_cases(PROCEDURES),
)
async def test_frame_space(
    air_dut, identifier, description, procedure, parameters, marks
):
    await verify_case(air_dut, identifier, description, procedure, parameters, marks)


@pytest.mark.asyncio
async def test_negotiated_mces_preserves_next_anchor(air_dut):
    """Apply T_MCES to the on-air event boundary (Core Vol 6, Part B, 4.1.5)."""
    link = await connect(air_dut, central=True, interval=6)
    phys = phy_bit(Phy.LE_1M)
    await initiate(link, 300, 300, phys, ACL_IFS, 300)
    async with link.event() as event:
        await event.receive()
        await event.send(more=True)
        await event.receive()
        await event.send()

    mces = 6500
    await initiate(link, mces, mces, phys, MCES, mces)
    async with link.event() as event:
        await event.receive()
        await event.send(more=True)
        last_peripheral_end = event.previous_end
        # A second exchange of even empty PDUs would overrun this boundary.
        assert await event.receive(required=False) is None

    async with link.event() as event:
        packet = await event.receive()
        assert packet.start_us - last_peripheral_end >= mces
        await event.send()
    await link.disconnect()
