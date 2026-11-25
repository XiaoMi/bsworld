# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Lower Tester / Upper Tester harness for connectionless LE roles.

Same vocabulary as ``llcp/harness.py``, applied to advertising, scanning,
initiating and periodic synchronization (Core Vol 6, Part B, 4.4):

    ut(command, status)     the Host sends an HCI command; Command Complete/Status
    lt_tx(pdu, ...)         the Lower Tester transmits on air; the IUT receives it
                            the next time it listens (or at a scripted time)
    run(us) / run_until(t)  advance the shared simulation clock through RadioLink
    lt_rx()                 the next PDU the IUT transmitted
    ut_rx(Event, **fields)  the next HCI event the Host receives
    lt_rx_q_is_empty()      nothing more was transmitted
    ut_rx_q_is_empty()      nothing more was reported

Everything runs through the real HCI command handlers, RadioLink dispatch,
role managers and RF procedures. Only the radio is scripted.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass

from bscontroller import hci
from bscontroller.hci.backend import RadioBackend
from bscontroller.hci.controller import Controller
from bscontroller.linklayer import (
    PDU_ADV_CHSEL_MASK,
    PDU_ADV_RXADD_MASK,
    PDU_ADV_TXADD_MASK,
    PDU_ADV_TYPE_MASK,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising import ExtendedPdu, scan_request
from bscontroller.linklayer.parameters import Parameters
from bscontroller.linklayer.timing import LL_RESPONSE_START_TOLERANCE_US
from bscontroller.radio.base import PHY_CODED_S8_FACTOR, Phy, RadioSwitch, Received
from tests.support import LE_EVENT_MASK_V1
from tests.unit.helpers import host_notifications, take_host_notification

ErrorCode = hci.HCI_ErrorCode
PduType = AdvertisingPduType

IUT_ADDRESS = hci.Address('F0:F1:F2:F3:F4:01', hci.AddressType.PUBLIC_DEVICE)
PEER = hci.Address('C0:11:22:33:44:55', hci.AddressType.RANDOM_DEVICE)
OTHER = hci.Address('C0:11:22:33:44:66', hci.AddressType.RANDOM_DEVICE)
PUBLIC_PEER = hci.Address('00:11:22:33:44:77', hci.AddressType.PUBLIC_DEVICE)

# Fixed CONNECT_IND LLData vector (Core Vol 6, Part B, 2.3.3.1).
CONNECT_PARAMETERS = Parameters(
    0x9A328370, 0x123456, 1, 0, 24, 0, 100, (1 << 37) - 1, 7
)
_COMMAND_RESULTS = (hci.HCI_Command_Complete_Event, hci.HCI_Command_Status_Event)


# ---------------------------------------------------------------------------
# Advertising physical channel PDU encoders (Core Vol 6, Part B, 2.3)
# ---------------------------------------------------------------------------
def legacy(
    kind: int,
    advertiser: hci.Address,
    payload: bytes = b'',
    *,
    target: hci.Address | None = None,
    chsel: bool = False,
) -> bytes:
    body = bytes(advertiser) + (bytes(target) if target is not None else payload)
    header = (
        kind
        | (PDU_ADV_CHSEL_MASK if chsel else 0)
        | (PDU_ADV_TXADD_MASK if advertiser.is_random else 0)
        | (PDU_ADV_RXADD_MASK if target is not None and target.is_random else 0)
    )
    return bytes([header, len(body)]) + body


def adv_ind(advertiser=PEER, data=b'', **kw):
    return legacy(PduType.ADV_IND, advertiser, data, **kw)


def adv_scan_ind(advertiser=PEER, data=b''):
    return legacy(PduType.ADV_SCAN_IND, advertiser, data)


def adv_nonconn_ind(advertiser=PEER, data=b''):
    return legacy(PduType.ADV_NONCONN_IND, advertiser, data)


def adv_direct_ind(advertiser=PEER, target=IUT_ADDRESS, **kw):
    return legacy(PduType.ADV_DIRECT_IND, advertiser, target=target, **kw)


def scan_rsp(advertiser=PEER, data=b''):
    return legacy(PduType.SCAN_RSP, advertiser, data)


def scan_req(scanner=PEER, advertiser=IUT_ADDRESS):
    return scan_request(scanner, advertiser)


def connect_ind(
    initiator=PEER,
    advertiser=IUT_ADDRESS,
    parameters: Parameters | bytes = CONNECT_PARAMETERS,
    *,
    chsel=False,
):
    lldata = parameters.encode() if isinstance(parameters, Parameters) else parameters
    body = bytes(initiator) + bytes(advertiser) + lldata
    header = (
        PduType.CONNECT_IND
        | (PDU_ADV_CHSEL_MASK if chsel else 0)
        | (PDU_ADV_TXADD_MASK if initiator.is_random else 0)
        | (PDU_ADV_RXADD_MASK if advertiser.is_random else 0)
    )
    return bytes([header, len(body)]) + body


def address_of(pdu: bytes, offset: int = 2, rx: bool = False) -> hci.Address:
    random = pdu[0] & (PDU_ADV_RXADD_MASK if rx else PDU_ADV_TXADD_MASK)
    return hci.Address(
        pdu[offset : offset + 6],
        hci.AddressType.RANDOM_DEVICE if random else hci.AddressType.PUBLIC_DEVICE,
    )


# ---------------------------------------------------------------------------
# Scripted radio
# ---------------------------------------------------------------------------
@dataclass
class Air:
    """One Lower Tester transmission, materialized when the IUT listens.

    ``at`` fixes an absolute start time. ``after`` places it relative to the start
    of the previously delivered packet (an AuxPtr offset, for instance). Otherwise
    it starts T_IFS-aligned: the IUT's window start plus the start tolerance.
    """

    pdu: bytes
    phy: Phy = Phy.LE_1M
    rssi: float = -40
    valid_crc: bool = True
    channel: int | None = None
    at: int | None = None
    after: int | None = None
    access_address: int | None = None


@dataclass(frozen=True)
class Tx:
    """One IUT transmission observed on air."""

    start_us: int
    end_us: int
    channel: int
    pdu: bytes
    phy: Phy
    access_address: int | None
    power_dbm: int

    @property
    def kind(self) -> int:
        return self.pdu[0] & PDU_ADV_TYPE_MASK

    @property
    def ext(self) -> ExtendedPdu:
        return ExtendedPdu.decode(self.pdu)


class ScriptedRadio:
    """A single-radio PHY double: transmissions are recorded, receptions consume
    the Lower Tester's script in order.
    """

    def __init__(self):
        self.now_us = 0
        self.sent: list[Tx] = []
        self.script: deque[Air] = deque()
        self.delivered: list[Received] = []
        self.listens: list[tuple[int, int, int, Phy]] = []
        self.switch = RadioSwitch()

    def earliest_start_us(self, channel, phy, *, transmit):
        return self.switch.earliest_start_us(
            self.now_us, channel, phy, transmit=transmit
        )

    async def advance_to_us(self, target):
        self.now_us = max(self.now_us, target)
        return self.now_us

    async def transmit(
        self,
        channel,
        pdu,
        *,
        start_us=None,
        phy=Phy.LE_1M,
        coding=PHY_CODED_S8_FACTOR,
        access_address=None,
        power_dbm=0,
        **_,
    ):
        phy = Phy(phy)
        ready = self.earliest_start_us(channel, phy, transmit=True)
        start_us = ready if start_us is None else start_us
        assert start_us >= ready, (start_us, ready)
        end_us = start_us + phy.airtime_us(len(pdu), coding)
        self.sent.append(
            Tx(start_us, end_us, channel, bytes(pdu), phy, access_address, power_dbm)
        )
        self.now_us = end_us - 1
        self.switch.complete(channel, phy, transmit=True, end_us=end_us)
        return end_us

    async def receive(
        self,
        channel,
        until_us,
        *,
        start_us=None,
        phy=Phy.LE_1M,
        access_address=None,
        accept_bad_crc=False,
        **_,
    ):
        phy = Phy(phy)
        start = max(
            start_us or self.now_us + 1,
            self.earliest_start_us(channel, phy, transmit=False),
        )
        self.listens.append((start, until_us, channel, phy))
        packet = self._match(channel, phy, start, until_us, access_address)
        if packet is None:
            self.now_us = max(self.now_us, until_us)
            self.switch.complete(channel, phy, transmit=False, end_us=self.now_us + 1)
            return None
        self.now_us = packet.end_us - 1
        self.switch.complete(channel, phy, transmit=False, end_us=packet.end_us)
        # As the BabbleSim PHY adapter: a CRC failure is only surfaced on request.
        return packet if packet.valid_crc or accept_bad_crc else None

    def _match(self, channel, phy, start, until, access_address):
        if start >= until or not self.script:
            return None
        air = self.script[0]
        if air.channel is not None and air.channel != channel:
            return None
        if air.phy != phy:
            return None
        if air.access_address is not None and air.access_address != access_address:
            return None
        if air.at is not None:
            begin = air.at
        elif air.after is not None:
            begin = self.delivered[-1].start_us + air.after
        else:
            begin = start + LL_RESPONSE_START_TOLERANCE_US
        # Address detection must happen inside the listening window.
        if begin < start or begin + phy.address_us > until:
            if air.at is not None and begin < start:
                self.script.popleft()  # The peer transmitted before we listened.
            return None
        self.script.popleft()
        packet = Received(
            air.pdu,
            air.rssi,
            begin,
            begin + phy.airtime_us(len(air.pdu)),
            air.valid_crc,
            phy,
        )
        self.delivered.append(packet)
        return packet


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
class AirHarness:
    def __init__(self, address=IUT_ADDRESS):
        self.loop = asyncio.new_event_loop()
        self.radio = ScriptedRadio()

        async def create():
            return Controller(
                'iut', backend=RadioBackend(self.radio), public_address=address
            )

        self.controller = self.loop.run_until_complete(create())
        self.events: list = []
        self.controller.send_hci_packet = self.events.append
        self.controller.event_mask = (1 << 64) - 1
        self.controller.le_event_mask = LE_EVENT_MASK_V1
        self.controller.event_mask_page_2 = (1 << 64) - 1
        self.link = self.controller.backend.link
        self.ll = self.controller.le_procedures.ll
        self._cursor = 0

    def close(self):
        self.loop.close()

    @property
    def now_us(self) -> int:
        return self.radio.now_us

    # -- Upper Tester ------------------------------------------------------
    def ut(self, command, status=ErrorCode.SUCCESS):
        """Send an HCI command; return its Command Complete parameters or Status."""

        async def dispatch():
            offset = len(self.events)
            self.controller.on_packet(bytes(command))
            await asyncio.sleep(0)
            return offset

        offset = self.loop.run_until_complete(dispatch())
        reply = next(
            event
            for event in self.events[offset:]
            if isinstance(event, _COMMAND_RESULTS)
        )
        self.events.remove(reply)
        result = getattr(reply, 'return_parameters', reply)
        actual = getattr(result, 'status', result)
        assert actual == status, (type(command).__name__, actual, status)
        return result

    def ut_rx(self, event_type, **fields):
        return take_host_notification(self.events, event_type, **fields)

    def ut_rx_report(self, **fields):
        """Next advertising report (legacy or extended), unwrapped."""
        notifications = host_notifications(self.events)
        assert notifications, 'expected an advertising report, Host queue empty'
        event = notifications[0]
        assert isinstance(
            event,
            (
                hci.HCI_LE_Advertising_Report_Event,
                hci.HCI_LE_Extended_Advertising_Report_Event,
            ),
        ), event
        self.events.remove(event)
        (report,) = event.reports
        for name, value in fields.items():
            assert getattr(report, name) == value, (name, getattr(report, name), value)
        return report

    def ut_rx_q_is_empty(self):
        notifications = host_notifications(self.events)
        assert not notifications, notifications

    def reports(self) -> list:
        """Pop every pending advertising report."""
        found = []
        for event in list(self.events):
            if isinstance(
                event,
                (
                    hci.HCI_LE_Advertising_Report_Event,
                    hci.HCI_LE_Extended_Advertising_Report_Event,
                ),
            ):
                self.events.remove(event)
                found.extend(event.reports)
        return found

    # -- Lower Tester ------------------------------------------------------
    def lt_tx(self, pdu, **air):
        if isinstance(pdu, ExtendedPdu):
            pdu = pdu.encode()
        self.radio.script.append(Air(bytes(pdu), **air))

    def lt_rx(self, kind: int | None = None) -> Tx:
        assert self._cursor < len(self.radio.sent), 'IUT transmitted nothing more'
        packet = self.radio.sent[self._cursor]
        self._cursor += 1
        if kind is not None:
            assert packet.kind == kind, (hex(packet.kind), hex(kind), packet)
        return packet

    def lt_rx_all(self) -> list[Tx]:
        packets = self.radio.sent[self._cursor :]
        self._cursor = len(self.radio.sent)
        return packets

    def lt_rx_q_is_empty(self):
        assert self._cursor == len(self.radio.sent), self.radio.sent[self._cursor :]

    def lt_script_is_consumed(self):
        assert not self.radio.script, self.radio.script

    # -- Time ------------------------------------------------------------
    def run_until(self, target_us: int) -> int:
        return self.loop.run_until_complete(self.link.advance_to_us(target_us))

    def run(self, us: int) -> int:
        return self.run_until(self.now_us + us)

    def run_events(self, count: int, interval_us: int) -> None:
        for _ in range(count):
            self.run(interval_us)

    def call(self, coroutine):
        return self.loop.run_until_complete(coroutine)
