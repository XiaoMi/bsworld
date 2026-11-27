# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""CIS subevents, acknowledgments and payload lifetime (Core Vol 6, Part B, 4.5.13)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidTag

from bscontroller.linklayer import (
    LL_ENCRYPTION_COUNTER_SIZE,
    LL_ENCRYPTION_DIRECTION_MASK,
    LL_EVENT_COUNTER_MASK,
    LL_IFS_US,
    LL_SUPERVISION_TIMEOUT_UNIT_US,
    PDU_DATA_MIC_SIZE,
    PDU_HEADER_SIZE,
)
from bscontroller.linklayer.channel_selection import isochronous_channels
from bscontroller.linklayer.isoal import IsoalParameters, IsoPdu
from bscontroller.linklayer.power_control import PowerPhy
from bscontroller.linklayer.scheduler import RadioPriority, RadioReservation
from bscontroller.linklayer.state import Role
from bscontroller.linklayer.timing import (
    LL_RESPONSE_START_TOLERANCE_US,
    window_widening,
)
from bscontroller.radio.base import (
    PHY_CODED_S8_FACTOR,
    PHY_TIME_UNITS_PER_SECOND,
    RADIO_SWITCH_TIME_US,
    Phy,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from bscontroller.linklayer.connection import Connection
    from bscontroller.linklayer.iso import IsoDataPaths
    from bscontroller.radio.base import Received


CIS_LLID_MASK = 0x03
CIS_NESN = 0x04
CIS_SN = 0x08
CIS_CIE = 0x10
CIS_NPI = 0x40
CIS_AUTHENTICATED_MASK = 0xFF ^ (CIS_NESN | CIS_SN | CIS_CIE | CIS_NPI)
CIS_ESTABLISHMENT_EVENTS = 6
CIS_PAYLOAD_COUNTER_LIMIT = (1 << 39) - 1


@dataclass(frozen=True)
class CisDirection:
    phy: Phy
    max_pdu: int
    bn: int
    ft: int
    sdu_interval_us: int
    max_sdu: int
    coding: int = PHY_CODED_S8_FACTOR

    @property
    def max_airtime_us(self) -> int:
        """MPT assumes S=8 on LE Coded and reserves the MIC when applicable."""
        return self.phy.airtime_us(
            PDU_HEADER_SIZE + self.max_pdu + (PDU_DATA_MIC_SIZE if self.max_pdu else 0)
        )

    def isoal(self, *, framed: bool, interval_us: int) -> IsoalParameters:
        return IsoalParameters(
            framed,
            self.bn,
            self.max_pdu,
            self.max_sdu,
            interval_us,
            self.sdu_interval_us,
        )

    def flush_point(self, payload: int, nse: int) -> tuple[int, int]:
        """Return the CIS event and the number of completed subevents at expiry."""
        event, index = divmod(payload, self.bn)
        return event + self.ft - 1, nse - nse // self.bn * (self.bn - 1 - index)


@dataclass(frozen=True)
class CisEventParameters:
    c_to_p: CisDirection
    p_to_c: CisDirection
    interval_us: int
    subinterval_us: int
    nse: int
    framed: bool
    cis_sync_delay_us: int
    cig_sync_delay_us: int
    framing_mode: int = 0
    spacing_us: int = LL_IFS_US
    minimum_subevent_space_us: int = LL_IFS_US

    @property
    def minimum_subevent_us(self) -> int:
        return (
            self.c_to_p.max_airtime_us
            + self.spacing_us
            + self.p_to_c.max_airtime_us
            + self.minimum_subevent_space_us
        )

    def transport_latency_us(self, *, central_to_peripheral: bool) -> int:
        """Transport_Latency in Core Vol 6, Part G, 3.2."""
        direction = self.c_to_p if central_to_peripheral else self.p_to_c
        if not direction.bn:
            return 0
        adjustment = (
            (0 if self.framing_mode else direction.sdu_interval_us)
            if self.framed
            else -direction.sdu_interval_us
        )
        return self.cig_sync_delay_us + direction.ft * self.interval_us + adjustment

    def synchronization_delay_us(self, *, central_to_peripheral: bool) -> int:
        """Adjustment to the reference anchor before applying Time_Offset."""
        if central_to_peripheral:
            return self.transport_latency_us(central_to_peripheral=True) + (
                self.cis_sync_delay_us
                - self.cig_sync_delay_us
                + (0 if self.framed else self.c_to_p.sdu_interval_us - self.interval_us)
            )
        return (
            self.cis_sync_delay_us
            - self.cig_sync_delay_us
            + (0 if self.framed else self.p_to_c.sdu_interval_us - self.interval_us)
        )


@dataclass
class CisLinkQuality:
    unacked: int = 0
    flushed: int = 0
    last_subevent: int = 0
    retransmitted: int = 0
    crc_error: int = 0
    unreceived: int = 0
    duplicate: int = 0


class CisEvent:
    def __init__(
        self,
        connection: Connection,
        handle: int,
        access_address: int,
        crc_init: int,
        anchor_us: int,
        parameters: CisEventParameters,
        *,
        iso: IsoDataPaths,
        clock_remainder: int = 0,
    ):
        self.connection = connection
        self.handle = handle
        self.access_address = access_address
        self.crc_init = crc_init
        self.parameters = parameters
        self.iso = iso
        self.central = connection.role == Role.CENTRAL
        self.tx = parameters.c_to_p if self.central else parameters.p_to_c
        self.rx = parameters.p_to_c if self.central else parameters.c_to_p
        self.tx_power_phy = PowerPhy.from_radio(self.tx.phy, self.tx.coding)
        self.rx_power_phy = PowerPhy.from_radio(self.rx.phy, self.rx.coding)
        self.anchor_us = anchor_us
        self._clock_remainder = clock_remainder
        self.last_rx_us = connection.last_anchor_rx_us
        self.last_valid_rx_us: int | None = None
        self._supervision_start = None if self.central else self._anchor_fixed
        self.counter = self.subevent = 0
        self.established = self.mic_failed = False
        self._event_closed = self._active_clock = False
        self._channels: tuple[int, ...] = ()
        self._outbound: dict[int, IsoPdu] = {}
        self._inbound: dict[int, IsoPdu] = {}
        self._tx_next = self._rx_next = self._delivered = 0
        self._tx_sent = False
        self._quality = CisLinkQuality()
        self._iv_xor = access_address.to_bytes(4, 'little') + bytes(4)

    @property
    def _anchor_fixed(self) -> int:
        return self.anchor_us * PHY_TIME_UNITS_PER_SECOND + self._clock_remainder

    @property
    def interval_fixed_us(self) -> int:
        return self.parameters.interval_us * self.connection.clock_rate

    def anchor_at_or_after(self, time_us: int) -> tuple[int, int]:
        """Project the recurring CIS phase onto an integer PHY deadline."""
        interval = self.interval_fixed_us
        events = -(
            (self._anchor_fixed - time_us * PHY_TIME_UNITS_PER_SECOND) // interval
        )
        return divmod(self._anchor_fixed + events * interval, PHY_TIME_UNITS_PER_SECOND)

    @property
    def awaiting_indication_ack(self) -> bool:
        return self.central and self._supervision_start is None

    def start_supervision(self) -> None:
        """Start at the next CIS event after the Central receives the ACL ACK."""
        start, phase = self.anchor_at_or_after(
            max(self.anchor_us, self.connection.radio.now_us + 1)
        )
        self._supervision_start = start * PHY_TIME_UNITS_PER_SECOND + phase

    @property
    def deadline_us(self) -> int | None:
        if not self.established:
            if self._supervision_start is None:
                return None
            return (
                self._supervision_start
                + CIS_ESTABLISHMENT_EVENTS * self.interval_fixed_us
            ) // PHY_TIME_UNITS_PER_SECOND
        last_rx = self.last_valid_rx_us
        if last_rx is None:
            if self._supervision_start is None:
                return None
            last_rx = self._supervision_start // PHY_TIME_UNITS_PER_SECOND
        return (
            last_rx
            + self.connection.parameters.timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
        )

    @property
    def exhausted(self) -> bool:
        return max(self._tx_next, self._rx_next) >= CIS_PAYLOAD_COUNTER_LIMIT

    def link_quality(self) -> tuple[int, int, int, int, int, int, int]:
        quality = self._quality
        return (
            quality.unacked & 0xFFFFFFFF,
            quality.flushed & 0xFFFFFFFF,
            quality.last_subevent & 0xFFFFFFFF,
            quality.retransmitted & 0xFFFFFFFF,
            quality.crc_error & 0xFFFFFFFF,
            quality.unreceived & 0xFFFFFFFF,
            quality.duplicate & 0xFFFFFFFF,
        )

    @property
    def spacing_us(self) -> int:
        return self.parameters.spacing_us

    @property
    def nominal_us(self) -> int:
        return self.anchor_us + self.subevent * self.parameters.subinterval_us

    def _widening_us(self, nominal: int) -> int:
        if self.central:
            return 0
        active = self._active_clock and nominal < (
            self.anchor_us + self.parameters.interval_us
        )
        last_sync = (
            self.last_rx_us
            if active
            else max(self.last_rx_us, self.connection.last_anchor_rx_us)
        )
        return window_widening(
            nominal - last_sync,
            self.connection.controls.clock_accuracy.peer_accuracy,
            active=active,
            local_sca=self.connection.link.sleep_clock_accuracy,
        )

    @property
    def next_us(self) -> int:
        return self.reservation().start_us

    def _reservation(self, subevent: int, anchor_fixed: int) -> RadioReservation:
        anchor_us, phase = divmod(anchor_fixed, PHY_TIME_UNITS_PER_SECOND)
        nominal = anchor_us + subevent * self.parameters.subinterval_us
        widening = self._widening_us(nominal)
        preparation = widening + RADIO_SWITCH_TIME_US
        duration = (
            self.tx.max_airtime_us
            + self.spacing_us
            + self.rx.max_airtime_us
            + preparation
            + widening
            + LL_RESPONSE_START_TOLERANCE_US
        )
        interval, fraction = divmod(self.interval_fixed_us, PHY_TIME_UNITS_PER_SECOND)
        return RadioReservation(
            nominal - preparation,
            duration,
            interval,
            RadioPriority.ISOCHRONOUS,
            interval_fraction=fraction,
            start_fraction=phase,
        )

    def reservation(self) -> RadioReservation:
        return self._reservation(self.subevent, self._anchor_fixed)

    def reservations(self) -> list[RadioReservation]:
        following = self._anchor_fixed + self.interval_fixed_us
        return [
            self._reservation(
                index,
                following
                if index < self.subevent or self._event_closed
                else self._anchor_fixed,
            )
            for index in range(self.parameters.nse)
        ]

    def _open_event(self) -> None:
        parameters = self.parameters
        self._channels = isochronous_channels(
            self.counter & LL_EVENT_COUNTER_MASK,
            self.access_address,
            self.connection.parameters.channel_map,
            parameters.nse,
        )
        if self.tx.bn:
            stream = self.iso.get_stream(self.handle, True)
            # ISOAL timestamps use the CIG reference point for every CIS
            # (Core Vol 6, Part G, 3.1).
            reference_us = (
                self.anchor_us
                + parameters.cis_sync_delay_us
                - parameters.cig_sync_delay_us
            )
            burst = stream.burst(reference_us, self.counter)
            # With no unframed SDU, there are no fragments to pad. The link
            # sends a CIS Null PDU instead of assigning SN to empty padding.
            if parameters.framed or burst.started:
                payload = self.counter * self.tx.bn
                self._outbound.update(enumerate(burst.pdus, payload))

    def _advance_tx(self) -> None:
        self._outbound.pop(self._tx_next, None)
        self._tx_next += 1
        self._tx_sent = False
        if self._tx_next % self.tx.bn == 0:
            self.iso.get_stream(self.handle, True).expire_payloads(
                self._tx_next // self.tx.bn - 1
            )

    def _flush(self) -> None:
        now = (self.counter, self.subevent + 1)
        while (
            self.tx.bn
            and self.tx.flush_point(self._tx_next, self.parameters.nse) <= now
        ):
            if self._tx_sent:
                self._quality.unacked += 1
            else:
                self._quality.flushed += 1
            self._advance_tx()
        while (
            self.rx.bn
            and self.rx.flush_point(self._rx_next, self.parameters.nse) <= now
        ):
            self._quality.unreceived += 1
            self._rx_next += 1

    def _deliver(self) -> None:
        if not self.rx.bn:
            return
        if not self.established or self.mic_failed:
            # There is no established HCI stream to receive earlier loss reports.
            self._delivered = self._rx_next // self.rx.bn
            return
        stream = self.iso.get_stream(self.handle, False)
        while (self._delivered + 1) * self.rx.bn <= self._rx_next:
            anchor = (
                self._anchor_fixed
                - (self.counter - self._delivered) * self.interval_fixed_us
            ) // PHY_TIME_UNITS_PER_SECOND
            start = self._delivered * self.rx.bn
            pdus = tuple(
                self._inbound.pop(index, None)
                for index in range(start, start + self.rx.bn)
            )
            stream.received(
                pdus, anchor, self._delivered, now_us=self.connection.radio.now_us
            )
            self._delivered += 1

    def _close_subevent(self) -> None:
        self._flush()
        self._deliver()
        self.subevent += 1
        if self.subevent == self.parameters.nse:
            self.subevent = 0
            self.counter += 1
            self.anchor_us, self._clock_remainder = divmod(
                self._anchor_fixed + self.interval_fixed_us, PHY_TIME_UNITS_PER_SECOND
            )
            self._event_closed = self._active_clock = False

    def skip_missed(self) -> None:
        """Advance counters through subevents that ended while the radio was busy."""
        now = self.connection.radio.now_us
        deadline = self.deadline_us
        while (
            deadline is None or now < deadline
        ) and self.nominal_us + self._widening_us(self.nominal_us) + (
            0 if self.central else self.parameters.c_to_p.phy.address_us
        ) <= now:
            if not self.subevent:
                self._open_event()
            self._close_subevent()

    def _crypt(
        self, header: int, payload: bytes, counter: int, *, transmit: bool
    ) -> bytes:
        security = self.connection.packet_security
        if not payload or not (
            security.tx_enabled if transmit else security.rx_enabled
        ):
            return payload
        cipher = security.cipher
        assert cipher is not None
        iv = bytes(x ^ y for x, y in zip(cipher.iv, self._iv_xor))
        from_central = self.central if transmit else not self.central
        nonce = (
            counter | (LL_ENCRYPTION_DIRECTION_MASK if from_central else 0)
        ).to_bytes(LL_ENCRYPTION_COUNTER_SIZE, 'little') + iv
        associated = bytes([header & CIS_AUTHENTICATED_MASK])
        if transmit:
            return cipher.aes.encrypt(nonce, payload, associated)
        return cipher.aes.decrypt(nonce, payload, associated)

    async def _send(
        self, channel: int, start_us: int, is_current: Callable[[], bool]
    ) -> int | None:
        radio = self.connection.radio
        if start_us < radio.earliest_start_us(channel, self.tx.phy, transmit=True):
            return None
        header = CIS_NESN if self._rx_next & 1 else 0
        pdu = self._outbound.get(self._tx_next)
        if pdu is None:
            header |= CIS_NPI
            data = b''
        else:
            header |= pdu.llid | (CIS_SN if self._tx_next & 1 else 0)
            data = self._crypt(header, pdu.data, self._tx_next, transmit=True)
        end_us = start_us + self.tx.phy.airtime_us(
            PDU_HEADER_SIZE + len(data), self.tx.coding
        )
        if not self.connection.link.resource.fits_radio(end_us):
            return None
        packet = bytes([header, len(data)]) + data
        self.connection.link.trace(lambda: f'RADIO TX ch={channel} pdu={packet.hex()}')
        end_us = await radio.transmit(
            channel,
            packet,
            start_us=start_us,
            access_address=self.access_address,
            crc_init=self.crc_init,
            phy=self.tx.phy,
            coding=self.tx.coding,
            power_dbm=self.connection.controls.power.transmit_iso_power_dbm(
                self.tx_power_phy
            ),
        )
        if not is_current():
            return None
        if pdu is not None:
            if self._tx_sent:
                self._quality.retransmitted += 1
            self._tx_sent = True
            if not self.central and (
                self._event_closed or self.subevent + 1 == self.parameters.nse
            ):
                self._quality.last_subevent += 1
            self.iso.get_stream(self.handle, True).transmitted(self._tx_next)
        return end_us

    async def _receive(
        self, channel: int, nominal: int, tolerance: int
    ) -> Received | None:
        link = self.connection.link
        start = max(link.now_us + 1, nominal - tolerance)
        until = nominal + tolerance + self.rx.phy.address_us
        if link.resource.deadline_us is not None:
            until = min(until, link.resource.deadline_us)
        if start >= until:
            return None
        return await link.radio.receive(
            channel,
            until,
            start_us=start,
            access_address=self.access_address,
            crc_init=self.crc_init,
            phy=self.rx.phy,
            accept_bad_crc=True,
        )

    def _accept(self, packet: Received) -> None:
        self.established = True
        if not self.central:
            # Reception synchronizes the active clock even if the CRC fails.
            self._active_clock = True
            observed_anchor = (
                packet.start_us - self.subevent * self.parameters.subinterval_us
            )
            self.connection.synchronize_clock(observed_anchor, self.anchor_us)
            self.last_rx_us = packet.start_us
            self.anchor_us = observed_anchor
        if not packet.valid_crc:
            self._quality.crc_error += 1
            return
        self.last_valid_rx_us = packet.end_us
        self.rx_power_phy = PowerPhy.from_radio(packet.phy, packet.coding)
        self.connection.controls.power.received_iso(packet)
        if len(packet.pdu) < PDU_HEADER_SIZE:
            return
        header, size = packet.pdu[:PDU_HEADER_SIZE]
        if size != len(packet.pdu) - PDU_HEADER_SIZE:
            return
        if self._tx_sent and bool(header & CIS_NESN) != bool(self._tx_next & 1):
            self._advance_tx()
        self._event_closed = bool(header & CIS_CIE)
        if header & CIS_NPI:
            return
        if bool(header & CIS_SN) != bool(self._rx_next & 1):
            self._quality.duplicate += 1
            return
        if self._rx_next >= (self.counter + 1) * self.rx.bn:
            return
        llid = header & CIS_LLID_MASK
        if llid not in ((2,) if self.parameters.framed else (0, 1)):
            return
        try:
            data = self._crypt(
                header, packet.pdu[PDU_HEADER_SIZE:], self._rx_next, transmit=False
            )
        except InvalidTag:
            self.mic_failed = True
            return
        if len(data) <= self.rx.max_pdu:
            self._inbound[self._rx_next] = IsoPdu(llid, data)
            self._rx_next += 1

    async def advance(
        self,
        *,
        is_current: Callable[[], bool],
        on_established: Callable[[], None] | None = None,
    ) -> None:
        """Run one subevent so different CISes may interleave on the same radio."""
        if not is_current():
            return
        if not self.subevent:
            self._open_event()
        nominal = self.nominal_us
        window = self.reservation()
        if not self._event_closed and self.connection.link.resource.admit(
            self, window.start_us, window.start_us + window.duration_us, window.priority
        ):
            channel = self._channels[self.subevent]
            if self.central:
                end = await self._send(channel, nominal, is_current)
                if end is not None:
                    packet = await self._receive(
                        channel, end + self.spacing_us, LL_RESPONSE_START_TOLERANCE_US
                    )
                    if packet is not None and is_current():
                        self._accept(packet)
            else:
                packet = await self._receive(
                    channel, nominal, self._widening_us(nominal)
                )
                if packet is not None and is_current():
                    self._accept(packet)
                    if not self.mic_failed:
                        await self._send(
                            channel, packet.end_us + self.spacing_us, is_current
                        )
        if not is_current():
            return
        if on_established is not None and self.established and not self.mic_failed:
            on_established()
        if is_current():
            self._close_subevent()
