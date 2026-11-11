# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connection-event execution and ARQ, below connection management and LLCP.

Own the transmit queue and retry snapshot here. A validated receive reports its
ACK and control payload to LLCP; protocol state changes stay above this executor.
Core Vol 6, Part B, 4.5.9 and Vol 6, Part E, 2 define the packet data path.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from bscontroller.linklayer import (
    LL_CONN_CRC_ERROR_LIMIT,
    LL_CONN_INTERVAL_UNIT_US,
    LL_DATA_CHANNEL_COUNT,
    LL_EVENT_COUNTER_MASK,
    LL_IFS_US,
    PDU_CONTROL_PAYLOAD_SIZE_MAX,
    PDU_DATA_LLID_MASK,
    PDU_DATA_MD_MASK,
    PDU_DATA_MIC_SIZE,
    PDU_DATA_NESN_MASK,
    PDU_DATA_PAYLOAD_TIME_CODED_MIN_US,
    PDU_DATA_SN_MASK,
    PDU_HEADER_SIZE,
    LlId,
)
from bscontroller.linklayer.channel_selection import (
    channel_selection_1,
    channel_selection_2,
)
from bscontroller.linklayer.data_path import (
    PacketControl,
    PacketSecurity,
    ReceiveContext,
)
from bscontroller.linklayer.state import Role
from bscontroller.linklayer.timing import (
    LL_CONN_EVENT_GUARD_US,
    window_widening,
)
from bscontroller.radio.base import RADIO_TX_POWER_MAX_DBM, RADIO_TX_POWER_MIN_DBM, Phy

if TYPE_CHECKING:
    from bscontroller.linklayer.connection import Connection


@dataclass(frozen=True)
class Outbound:
    llid: int
    payload: bytes
    host_packet: bool = False


class ConnectionEvent:
    def __init__(
        self, connection: Connection, control: PacketControl, security: PacketSecurity
    ):
        self.connection = connection
        self.control = control
        self.security = security
        self.tx_sn = self.rx_nesn = 0
        self.pending: Outbound | None = None
        self.sent_pending = False
        self.pending_wire: bytes | None = None
        self.queue: deque[Outbound] = deque()
        self.tx_start_pending = False
        self.retries = 0

    def enqueue_acl(self, continuation: bool, payload: bytes) -> None:
        """Queue Host ACL data; LLID follows Core Vol 6, Part B, 2.4.1."""
        if not payload:
            # An empty HCI start retains its boundary, never LLID=2/Length=0 on air.
            self.tx_start_pending |= not continuation
            self.connection.listener.on_acl_completed(self.connection)
            return
        continuation = continuation and not self.tx_start_pending
        self.tx_start_pending = False
        self.queue.append(
            Outbound(LlId.CONTINUATION if continuation else LlId.START, payload, True)
        )

    def enqueue_control(self, payload: bytes, *, priority: bool = False) -> None:
        item = Outbound(LlId.CONTROL, payload)
        if priority:
            self.queue.appendleft(item)
        else:
            self.queue.append(item)

    def accept(self, pdu: bytes, valid_crc: bool) -> None:
        """Validate RX, then process its ACK independently of new peer data.

        See Bluetooth Core 6.3, Vol 6, Part B, 4.5.9.
        """
        if (
            not valid_crc
            or len(pdu) < PDU_HEADER_SIZE
            or pdu[1] != len(pdu) - PDU_HEADER_SIZE
        ):
            return
        header, _ = pdu[:PDU_HEADER_SIZE]
        acknowledged_control = (
            self.pending.payload
            if self.pending is not None
            and self.pending.llid == LlId.CONTROL
            and self.sent_pending
            and bool(header & PDU_DATA_NESN_MASK) != self.tx_sn
            else None
        )
        payload = self.control.receive(
            ReceiveContext(
                header,
                pdu[PDU_HEADER_SIZE:],
                duplicate=bool(header & PDU_DATA_SN_MASK) != self.rx_nesn,
                acknowledged_control=acknowledged_control,
            )
        )
        if payload is None or len(payload) > (
            PDU_CONTROL_PAYLOAD_SIZE_MAX
            if header & PDU_DATA_LLID_MASK == LlId.CONTROL
            else self.control.packet_limits().rx_octets
        ):
            return
        # Validate the payload before using its ACK or returning Host credit;
        # a failed receive must leave the pending TX snapshot intact.
        self.connection.last_rx_us = self.connection.radio.now_us
        self.connection.establish()
        if (
            self.pending is not None
            and self.sent_pending
            and bool(header & PDU_DATA_NESN_MASK) != self.tx_sn
        ):
            # Process NESN even when the peer payload is a duplicate: it may still
            # acknowledge our last TX and release its pending snapshot.
            acknowledged = self.pending
            self.pending = None
            self.sent_pending = False
            self.pending_wire = None
            self.tx_sn ^= 1
            terminate_reason = None
            if acknowledged.llid == LlId.CONTROL:
                terminate_reason = self.control.acknowledged(acknowledged.payload)
            if acknowledged.host_packet:
                self.connection.listener.on_acl_completed(self.connection)
            if terminate_reason is not None:
                self.connection.complete(terminate_reason)
                return
        if bool(header & PDU_DATA_SN_MASK) != self.rx_nesn:
            # Retransmissions can carry a new ACK, but must not redeliver their payload.
            self.connection.link.trace("LL RX DUPLICATE")
            return
        self.rx_nesn ^= 1
        llid = header & PDU_DATA_LLID_MASK
        # Reserved LLID participates in ARQ, but has no payload interpretation.
        if llid == LlId.CONTROL:
            self.control.control(payload)
        elif llid != LlId.RESERVED and payload:
            self.connection.listener.on_acl_received(
                self.connection, llid == LlId.CONTINUATION, payload
            )

    def _next_outbound(self) -> Outbound:
        return next(
            (
                item
                for item in self.queue
                if self.control.can_send(item.llid, item.payload)
            ),
            Outbound(LlId.CONTINUATION, b''),
        )

    async def send(self, channel: int, start_us: int) -> bool:
        """Send a queued fragment or retransmit the pending wire snapshot.

        Select and encrypt once per SN; retain the pending bytes until ACK.
        See Bluetooth Core 6.3, Vol 6, Part B, 4.5.9 and Part E, 2.1.
        """
        if self.pending is None:
            # Select once per SN. Subsequent sends reuse this PDU until its ACK,
            # even if procedure state or effective data length changes meanwhile.
            self.pending = self._next_outbound()
            if self.pending in self.queue:
                self.queue.remove(self.pending)
            if self.pending.llid != LlId.CONTROL:
                limit = self.control.packet_limits().tx_payload
                if len(self.pending.payload) > limit:
                    # Keep the original HCI packet's single completion credit on the
                    # final
                    # LL fragment; splitting must not create credits (Vol 4, Part E,
                    # 7.7.19).
                    packet = self.pending
                    self.queue.appendleft(
                        Outbound(
                            LlId.CONTINUATION,
                            packet.payload[limit:],
                            packet.host_packet,
                        )
                    )
                    self.pending = replace(
                        packet, payload=packet.payload[:limit], host_packet=False
                    )
        elif self.sent_pending:
            self.retries += 1
            self.connection.link.trace(f"LL RETRANSMIT sn={self.tx_sn}")
        item = self.pending
        if item.llid == LlId.CONTROL and not self.sent_pending:
            # Finalize the Instant just before first TX to account for queueing delay.
            # Retries retain the already transmitted value.
            item = Outbound(LlId.CONTROL, self.control.prepare(item.payload))
            self.pending = item
        # Paused Host data must not keep an otherwise idle event open.
        more = any(
            self.control.can_send(entry.llid, entry.payload) for entry in self.queue
        )
        header = (
            item.llid
            | (PDU_DATA_NESN_MASK if self.rx_nesn else 0)
            | (PDU_DATA_SN_MASK if self.tx_sn else 0)
            | (PDU_DATA_MD_MASK if more else 0)
        )
        if self.pending_wire is None:
            # Cache ciphertext so a retry reuses its packet counter and MIC instead
            # of encrypting again (Vol 6, Part E, 2.1).
            self.pending_wire = item.payload
            self.pending_wire = self.security.encrypt(header, self.pending_wire)
        pdu = bytes([header, len(self.pending_wire)]) + self.pending_wire
        self.connection.link.trace(
            f"LL TX event={self.connection.event_counter} ch={channel} pdu={pdu.hex()}"
        )
        await self.connection.radio.transmit(
            channel,
            pdu,
            start_us=start_us,
            access_address=self.connection.parameters.access_address,
            crc_init=self.connection.parameters.crc_init,
            phy=self.connection.tx_phy,
            coding=self.connection.tx_coding,
        )
        if item.llid == LlId.CONTROL:
            self.control.sent(item.payload, first=not self.sent_pending)
        self.sent_pending = True
        return bool(header & PDU_DATA_MD_MASK)

    async def receive(
        self, channel: int, until_us: int, *, start_us: int | None = None
    ):
        # End address search at the next timer; an already found frame is nonpreemptive.
        """Receive within the connection event; Coded PHY never rejects below 2704 us
        (Core Vol 6, Part B, 4.5.10).
        """
        packet = await self.connection.radio.receive(
            channel,
            min(until_us, self.connection.next_deadline_us),
            start_us=start_us,
            access_address=self.connection.parameters.access_address,
            crc_init=self.connection.parameters.crc_init,
            accept_bad_crc=True,
            phy=self.connection.rx_phy,
        )
        if packet is not None:
            self.connection.link.trace(
                f"LL RX event={self.connection.event_counter} "
                f"ch={channel} crc={int(packet.valid_crc)} "
                f"pdu={packet.pdu.hex()}"
            )
            # Connection establishment is a physical reception, independent of CRC.
            self.connection.establish()
            self.connection.received_in_event()
            if packet.valid_crc:
                self.connection.rssi = max(
                    RADIO_TX_POWER_MIN_DBM,
                    min(RADIO_TX_POWER_MAX_DBM, round(packet.rssi)),
                )
            within_time = len(packet.pdu) >= PDU_HEADER_SIZE and (
                packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
                or packet.end_us - packet.start_us
                <= max(
                    self.control.packet_limits().rx_time,
                    (
                        PDU_DATA_PAYLOAD_TIME_CODED_MIN_US
                        if self.connection.rx_phy == Phy.LE_CODED
                        else 0
                    ),
                )
            )
            self.accept(packet.pdu, packet.valid_crc and within_time)
        return packet

    def next_exchange_us(self, *, max_rx: bool = True) -> int:
        """Air time of the next TX plus a maximum effective RX, separated by T_IFS (Core
        Vol 6, Part B, 4.1.1, 4.5.10).
        """
        item = self.pending or self._next_outbound()
        limits = self.control.packet_limits()
        tx_length = (
            len(item.payload)
            if self.pending or item.llid == LlId.CONTROL
            else min(len(item.payload), limits.tx_payload)
        )
        tx_length += PDU_DATA_MIC_SIZE if self.security.tx_enabled and tx_length else 0
        rx_length = limits.rx_payload if max_rx else 0
        rx_length += PDU_DATA_MIC_SIZE if self.security.rx_enabled and rx_length else 0
        return (
            self.connection.tx_phy.airtime_us(
                PDU_HEADER_SIZE + tx_length, self.connection.tx_coding
            )
            + self.connection.rx_phy.airtime_us(PDU_HEADER_SIZE + rx_length)
            + 2 * LL_IFS_US
        )

    async def advance_to_us(
        self, target_us: int, *, radio_until_us: int | None = None
    ) -> int:
        """Run one connection event: channel selection, exchanges until close (Core Vol
        6, Part B, 4.5.1, 4.5.6, 4.5.8).
        """
        self.connection.skip_missed_events()
        if self.connection.check_timeout():
            return self.connection.radio.now_us
        deadline = self.connection.next_deadline_us
        if (
            target_us < self.connection.event_start_us
            or self.connection.event_start_us >= deadline
        ):
            await self.connection.radio.advance_to_us(min(target_us, deadline))
            self.connection.check_timeout()
            return self.connection.radio.now_us
        self.control.event_prepare()
        if self.connection.closed:
            return self.connection.radio.now_us
        self.connection.unmapped_channel = (
            self.connection.unmapped_channel + self.connection.parameters.hop
        ) % LL_DATA_CHANNEL_COUNT
        channel = (
            channel_selection_2(
                self.connection.event_counter,
                self.connection.parameters.access_address,
                self.connection.parameters.channel_map,
            )
            if self.connection.csa2
            else channel_selection_1(
                self.connection.unmapped_channel, self.connection.parameters.channel_map
            )
        )
        # Leave room for the next connection event. MD may extend this event,
        # but never beyond its connection interval.
        event_end_us = (
            self.connection.anchor_us
            + self.connection.parameters.interval * LL_CONN_INTERVAL_UNIT_US
            - LL_CONN_EVENT_GUARD_US
        )
        if radio_until_us is not None:
            event_end_us = min(event_end_us, radio_until_us)
        if max(
            self.connection.radio.now_us + 1, self.connection.event_start_us
        ) + self.next_exchange_us(max_rx=False) >= event_end_us or (
            self.connection.role == Role.CENTRAL
            and self.connection.radio.earliest_start_us(
                channel, self.connection.tx_phy, transmit=True
            )
            > self.connection.anchor_us
        ):
            # Admit known TX plus an empty ACK. An actual received frame is
            # nonpreemptive; its size must not be guessed from the maximum DLE.
            self.connection.anchor_us += (
                self.connection.parameters.interval * LL_CONN_INTERVAL_UNIT_US
            )
            self.connection.event_counter = (
                self.connection.event_counter + 1
            ) & LL_EVENT_COUNTER_MASK
            return self.connection.radio.now_us
        self.connection.last_event_us = self.connection.radio.now_us
        if self.connection.role == Role.CENTRAL:
            start_us = self.connection.anchor_us
            while not self.connection.closed:
                more = await self.send(channel, start_us)
                if self.connection.remote_termination is not None:
                    self.connection.complete(self.connection.remote_termination)
                    break
                packet = await self.receive(
                    channel,
                    self.connection.radio.now_us
                    + LL_IFS_US
                    + self.connection.response_window_us,
                )
                if packet is None or not packet.valid_crc or self.connection.closed:
                    break
                start_us = packet.end_us + LL_IFS_US
                if (
                    not (more or packet.pdu[0] & PDU_DATA_MD_MASK)
                    or start_us + self.next_exchange_us() >= event_end_us
                ):
                    break
        else:
            # Core Vol 6, Part B, 4.2.4/4.5.5: listen from widening before the anchor.
            widening = window_widening(
                self.connection.anchor_us - self.connection.last_rx_us,
                self.connection.parameters.sca,
            )
            packet = await self.receive(
                channel,
                self.connection.anchor_us
                + self.connection.receive_window_us
                + widening
                + self.connection.rx_phy.address_us
                - 1,
                start_us=max(
                    self.connection.radio.now_us + 1,
                    self.connection.anchor_us - widening,
                ),
            )
            if packet is not None:
                self.connection.update_window = False
                self.connection.anchor_us = packet.start_us
            # Core Vol 6, Part B, 4.5.6: two consecutive CRC errors close the event.
            crc_errors = 0
            while packet is not None and not self.connection.closed:
                crc_errors = 0 if packet.valid_crc else crc_errors + 1
                if crc_errors == LL_CONN_CRC_ERROR_LIMIT:
                    break
                more = await self.send(channel, packet.end_us + LL_IFS_US)
                if self.connection.remote_termination is not None:
                    self.connection.complete(self.connection.remote_termination)
                    break
                if (
                    (
                        packet.valid_crc
                        and not (more or packet.pdu[0] & PDU_DATA_MD_MASK)
                    )
                    or self.connection.radio.now_us + self.next_exchange_us()
                    >= event_end_us
                ):
                    break
                packet = await self.receive(
                    channel,
                    self.connection.radio.now_us
                    + LL_IFS_US
                    + self.connection.response_window_us,
                )
        self.connection.anchor_us += (
            self.connection.parameters.interval * LL_CONN_INTERVAL_UNIT_US
        )
        self.connection.event_counter = (
            self.connection.event_counter + 1
        ) & LL_EVENT_COUNTER_MASK
        self.connection.check_timeout()
        return self.connection.radio.now_us
