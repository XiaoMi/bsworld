# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connection-event execution and ARQ, below connection management and LLCP.

Own the transmit queue and retry snapshot here. A validated receive reports its
ACK and control payload to LLCP; protocol state changes stay above this executor.
Core Vol 6, Part B, 4.5.9 and Vol 6, Part E, 2 define the packet data path.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from bscontroller.linklayer import (
    LL_CONN_CRC_ERROR_LIMIT,
    LL_DATA_CHANNEL_COUNT,
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
from bscontroller.linklayer.packet_control import PacketControl, ReceiveContext
from bscontroller.linklayer.packet_security import PacketSecurity
from bscontroller.linklayer.state import Role
from bscontroller.linklayer.timing import LL_RESPONSE_START_TOLERANCE_US
from bscontroller.radio.base import Phy, Received

if TYPE_CHECKING:
    from collections.abc import Callable

    from bscontroller.linklayer.connection import Connection
    from bscontroller.linklayer.procedure import ProcedureContext


@dataclass(eq=False)
class AclFlush:
    """One lifetime shared by all fragments of a flushable L2CAP PDU."""

    deadline_us: int
    discarded: bool = False


@dataclass(frozen=True, eq=False)
class Outbound:
    llid: int
    payload: bytes
    host_packet: bool = False
    flush: AclFlush | None = None
    procedure: ProcedureContext | None = None

    @property
    def opcode(self) -> int | None:
        return self.payload[0] if self.llid == LlId.CONTROL and self.payload else None


EMPTY_PDU = Outbound(LlId.CONTINUATION, b'')


@dataclass(frozen=True)
class Transmission:
    outbound: Outbound
    length: int
    start_us: int


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
        self.acl_flush_timeout_us = 0
        self.host_flush: AclFlush | None = None
        self.flush_timers: set[AclFlush] = set()
        self.retries = 0
        self._acknowledgment_callback: Callable[[], None] | None = None

    def enqueue_acl(
        self, continuation: bool, payload: bytes, *, flushable: bool = False
    ) -> None:
        """Preserve Host L2CAP boundaries (Core Vol 6, Part B, 4.5.17.1)."""
        self.expire_acl()
        if not continuation:
            self.host_flush = None
            if flushable and self.acl_flush_timeout_us:
                self.host_flush = AclFlush(
                    self.connection.radio.now_us + self.acl_flush_timeout_us
                )
                self.flush_timers.add(self.host_flush)
        if self.host_flush is not None and self.host_flush.discarded:
            self.connection.listener.on_acl_completed(self.connection)
            return
        if not payload:
            # An empty HCI start retains its boundary, never LLID=2/Length=0 on air.
            self.tx_start_pending |= not continuation
            self.connection.listener.on_acl_completed(self.connection)
            return
        continuation = continuation and not self.tx_start_pending
        self.tx_start_pending = False
        self.queue.append(
            Outbound(
                LlId.CONTINUATION if continuation else LlId.START,
                payload,
                True,
                self.host_flush,
            )
        )

    @property
    def next_flush_us(self) -> int | None:
        return min((flush.deadline_us for flush in self.flush_timers), default=None)

    def expire_acl(self) -> None:
        if not self.flush_timers:
            return
        now = self.connection.radio.now_us
        expired = {flush for flush in self.flush_timers if flush.deadline_us <= now}
        if not expired:
            return
        self.flush_timers.difference_update(expired)
        for flush in expired:
            flush.discarded = True
        retained = deque()
        completed = 0
        for item in self.queue:
            if item.flush in expired:
                completed += item.host_packet
            else:
                retained.append(item)
        self.queue = retained
        if completed:
            self.connection.listener.on_acl_completed(self.connection, completed)

    def _enqueue_control(
        self,
        payload: bytes,
        *,
        priority: bool = False,
        procedure: ProcedureContext | None = None,
    ) -> Outbound:
        item = Outbound(LlId.CONTROL, payload, procedure=procedure)
        if priority:
            self.queue.appendleft(item)
        else:
            self.queue.append(item)

        return item

    def cancel_queued_control(self, item: Outbound | None) -> bool:
        """Withdraw an entry until ARQ selects it for transmission."""
        try:
            self.queue.remove(item)
        except ValueError:
            return False
        return True

    def after_acknowledgment(self, callback: Callable[[], None]) -> None:
        """Run when the next TX carries the acknowledgment for this RX."""
        self._acknowledgment_callback = callback

    def accept(self, pdu: bytes, valid_crc: bool) -> bool:
        """Validate RX, then process its ACK independently of new peer data.

        See Bluetooth Core 6.3, Vol 6, Part B, 4.5.9.
        """
        if (
            not valid_crc
            or len(pdu) < PDU_HEADER_SIZE
            or pdu[1] != len(pdu) - PDU_HEADER_SIZE
        ):
            return False
        header, _ = pdu[:PDU_HEADER_SIZE]
        duplicate = bool(header & PDU_DATA_SN_MASK) != self.rx_nesn
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
                duplicate=duplicate,
                acknowledged_control=acknowledged_control,
            )
        )
        if payload is None or len(payload) > (
            PDU_CONTROL_PAYLOAD_SIZE_MAX
            if header & PDU_DATA_LLID_MASK == LlId.CONTROL
            else self.control.packet_limits().rx_octets
        ):
            return False
        # Validate the payload before using its ACK or returning Host credit;
        # a failed receive must leave the pending TX snapshot intact.
        self.connection.last_rx_us = self.connection.radio.now_us
        self.connection.establish()
        self.control.packet_received(pdu[1], duplicate=duplicate)
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
                terminate_reason = self.control.acknowledged(
                    acknowledged.payload, acknowledged.procedure
                )
            if acknowledged.host_packet:
                self.connection.listener.on_acl_completed(self.connection)
            if terminate_reason is not None:
                self.connection.complete(terminate_reason)
                return True
        if duplicate:
            # Retransmissions can carry a new ACK, but must not redeliver their payload.
            self.connection.link.trace("LL RX DUPLICATE")
            return True
        self.rx_nesn ^= 1
        llid = header & PDU_DATA_LLID_MASK
        # Reserved LLID participates in ARQ, but has no payload interpretation.
        if llid == LlId.CONTROL:
            self.control.control(payload)
        elif llid != LlId.RESERVED and payload:
            self.connection.listener.on_acl_received(
                self.connection, llid == LlId.CONTINUATION, payload
            )
        return True

    def _sendable(self, item: Outbound, start_us: int) -> bool:
        flush = item.flush
        if (
            flush is not None
            and flush in self.flush_timers
            and flush.deadline_us <= start_us
        ):
            return False
        return self.control.can_send(item.llid, item.payload)

    def _next_outbound(self, start_us: int | None = None) -> Outbound:
        if start_us is None:
            start_us = self.connection.radio.now_us
        return next(
            (item for item in self.queue if self._sendable(item, start_us)),
            EMPTY_PDU,
        )

    def _payload_length(self, item: Outbound) -> int:
        if self.pending is not None or item.llid == LlId.CONTROL or not item.payload:
            return len(item.payload)
        return min(len(item.payload), self.control.packet_limits().tx_payload)

    def _tx_airtime_us(self, length: int) -> int:
        if self.pending_wire is not None:
            length = len(self.pending_wire)
        elif length and self.security.tx_enabled:
            length += PDU_DATA_MIC_SIZE
        return self.connection.tx_phy.airtime_us(
            PDU_HEADER_SIZE + length, self.connection.tx_coding
        )

    def _rx_airtime_us(self, max_rx: bool) -> int:
        length = self.control.packet_limits().rx_payload if max_rx else 0
        if length and self.security.rx_enabled:
            length += PDU_DATA_MIC_SIZE
        return self.connection.rx_phy.airtime_us(PDU_HEADER_SIZE + length)

    async def send(self, channel: int, start_us: int) -> tuple[bool, int]:
        item = self.pending or self._next_outbound(start_us)
        return await self._transmit(
            channel, Transmission(item, self._payload_length(item), start_us)
        )

    async def _transmit(
        self, channel: int, transmission: Transmission
    ) -> tuple[bool, int]:
        """Send a queued fragment or retransmit the pending wire snapshot.

        Select and encrypt once per SN; retain the pending bytes until ACK.
        See Bluetooth Core 6.3, Vol 6, Part B, 4.5.9 and Part E, 2.1.
        """
        if self.pending is None:
            # Select once per SN. Subsequent sends reuse this PDU until its ACK,
            # even if procedure state or effective data length changes meanwhile.
            self.pending = transmission.outbound
            if self.pending in self.queue:
                self.queue.remove(self.pending)
            # The first TX retains this L2CAP PDU and its retries permanently.
            if self.pending.flush is not None:
                self.flush_timers.discard(self.pending.flush)
            limit = transmission.length
            if len(self.pending.payload) > limit:
                # Return the HCI completion credit on the final LL fragment
                # (Core Vol 4, Part E, 7.7.19).
                packet = self.pending
                self.queue.appendleft(
                    replace(
                        packet, llid=LlId.CONTINUATION, payload=packet.payload[limit:]
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
            item = replace(item, payload=self.control.prepare(item.payload))
            self.pending = item
        # Paused Host data must not keep an otherwise idle event open.
        more = any(self._sendable(entry, transmission.start_us) for entry in self.queue)
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
            lambda: (
                f"LL TX event={self.connection.event_counter} "
                f"ch={channel} pdu={pdu.hex()}"
            )
        )
        end_us = await self.connection.radio.transmit(
            channel,
            pdu,
            start_us=transmission.start_us,
            access_address=self.connection.parameters.access_address,
            crc_init=self.connection.parameters.crc_init,
            phy=self.connection.tx_phy,
            coding=self.connection.tx_coding,
            power_dbm=self.control.transmit_power_dbm(
                item.payload if item.llid == LlId.CONTROL else None
            ),
        )
        if self.connection.closed:
            return False, end_us
        self.control.sent(
            item.payload if item.llid == LlId.CONTROL else None,
            first=not self.sent_pending,
            procedure=item.procedure,
            length=len(self.pending_wire),
        )
        self.sent_pending = True
        if (callback := self._acknowledgment_callback) is not None:
            self._acknowledgment_callback = None
            callback()
        self.expire_acl()
        return bool(header & PDU_DATA_MD_MASK), end_us

    async def receive(
        self,
        channel: int,
        until_us: int,
        *,
        start_us: int | None = None,
        anchor: bool = False,
    ) -> Received | None:
        """Receive within the connection event; Coded PHY never rejects below 2704 us
        (Core Vol 6, Part B, 4.5.10).
        """
        # End address search at the next timer; an already found frame is nonpreemptive.
        packet = await self.connection.radio.receive(
            channel,
            min(until_us, self.connection.next_deadline_us),
            start_us=start_us,
            access_address=self.connection.parameters.access_address,
            crc_init=self.connection.parameters.crc_init,
            accept_bad_crc=True,
            phy=self.connection.rx_phy,
        )
        if self.connection.closed:
            return None
        if packet is not None:
            self.connection.link.trace(
                lambda: f"LL RX event={self.connection.event_counter} "
                f"ch={channel} crc={int(packet.valid_crc)} "
                f"pdu={packet.pdu.hex()}"
            )
            if anchor:
                connection = self.connection
                if connection.update_window or not connection.established:
                    # A transmit window deliberately permits a new ACL anchor.
                    # Its offset does not move an existing CIS.
                    connection.last_anchor_rx_us = connection.anchor_us = (
                        packet.start_us
                    )
                    connection.update_window = False
                else:
                    connection.synchronize_clock(packet.start_us, connection.anchor_us)
            # Connection establishment is a physical reception, independent of CRC.
            self.connection.establish()
            self.connection.received_in_event()
            if packet.valid_crc:
                self.connection.last_rssi_dbm = packet.rssi
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
            if (
                self.accept(packet.pdu, packet.valid_crc and within_time)
                and not self.connection.closed
            ):
                self.control.measure_received_power(packet)
        return packet

    def first_exchange_us(self, *, max_rx: bool = True) -> int:
        """Duration of Central TX and Peripheral TX at the anchor (Core 4.5.1)."""
        item = self.pending or self._next_outbound(self.connection.anchor_us)
        space_us = (
            self._rx_spacing_us(item, max_rx)
            if self.connection.role == Role.CENTRAL
            else self.control.tx_spacing_us(item.opcode)
        )
        return (
            self._tx_airtime_us(self._payload_length(item))
            + space_us
            + self._rx_airtime_us(max_rx)
        )

    def _rx_spacing_us(self, item: Outbound, max_rx: bool) -> int:
        start_us, end_us = self.control.rx_spacing_window_us(item.opcode)
        return end_us if max_rx else start_us

    def _deadline_us(
        self, radio_until_us: int | None, opcode: int | None = None
    ) -> int:
        end_us = self.connection.next_anchor_us - self.connection.event_guard_for_us(
            opcode
        )
        return end_us if radio_until_us is None else min(end_us, radio_until_us)

    def _fit_transmission(
        self, item: Outbound, start_us: int, budget_us: int
    ) -> Transmission | None:
        length = self._payload_length(item)
        if self._tx_airtime_us(length) <= budget_us:
            return Transmission(item, length, start_us)
        if self.pending is not None or item.llid == LlId.CONTROL:
            return None
        # Only new ACL data can be fragmented. Retries keep the original
        # sequence number and wire snapshot, and control PDUs are indivisible.
        length = bisect_right(range(length + 1), budget_us, key=self._tx_airtime_us) - 1
        return Transmission(item, length, start_us) if length > 0 else None

    def _plan_transmission(
        self,
        channel: int,
        item: Outbound,
        radio_until_us: int | None,
        *,
        previous_end_us: int | None = None,
    ) -> Transmission | None:
        start_us = (
            self.connection.anchor_us
            if previous_end_us is None
            else previous_end_us + self.control.tx_spacing_us(item.opcode)
        )
        if start_us < self.connection.radio.earliest_start_us(
            channel, self.connection.tx_phy, transmit=True
        ):
            return None
        budget_us = self._deadline_us(radio_until_us, item.opcode) - start_us
        if self.connection.role == Role.CENTRAL:
            max_rx = previous_end_us is not None
            budget_us -= self._rx_spacing_us(item, max_rx) + self._rx_airtime_us(max_rx)
        return self._fit_transmission(item, start_us, budget_us)

    async def _receive_response(
        self, channel: int, end_us: int, deadline_us: int
    ) -> Received | None:
        if self.connection.closed:
            return None
        low, high = self.control.rx_spacing_window_us()
        start_us = max(
            self.connection.radio.now_us + 1,
            end_us + low - LL_RESPONSE_START_TOLERANCE_US,
        )
        # A pending Frame Space request can span a wider range than this event.
        # Stop address acquisition at the radio deadline, even if no peer replies.
        until_us = min(deadline_us, end_us + high + self.connection.response_window_us)
        if start_us >= until_us:
            return None
        packet = await self.receive(
            channel,
            until_us,
            start_us=start_us,
        )
        if packet is not None and packet.valid_crc and not self.connection.closed:
            self.control.response_received(packet.start_us - end_us)
        return packet

    async def _central_exchange(self, channel: int, radio_until_us: int | None) -> None:
        previous_end_us = None
        while not self.connection.closed:
            acl_start_us = (
                self.connection.anchor_us
                if previous_end_us is None
                else previous_end_us + self.control.tx_spacing_us(None)
            )
            item = self.pending or self._next_outbound(acl_start_us)
            transmission = self._plan_transmission(
                channel, item, radio_until_us, previous_end_us=previous_end_us
            )
            if transmission is None:
                break
            if previous_end_us is None:
                self.connection.last_event_us = self.connection.radio.now_us
            more, end_us = await self._transmit(channel, transmission)
            if self.connection.remote_termination is not None:
                self.connection.complete(self.connection.remote_termination)
                break
            packet = await self._receive_response(
                channel, end_us, self._deadline_us(radio_until_us)
            )
            if (
                packet is None
                or not packet.valid_crc
                or not (more or packet.pdu[0] & PDU_DATA_MD_MASK)
            ):
                break
            previous_end_us = packet.end_us

    async def _peripheral_exchange(
        self, channel: int, radio_until_us: int | None
    ) -> None:
        connection = self.connection
        deadline_us = self._deadline_us(radio_until_us)
        start_us = max(connection.radio.now_us + 1, connection.event_start_us)
        # The initial budget admits a Central Empty PDU and our shortest reply.
        min_reply_us = self.control.tx_spacing_us(None) + self._tx_airtime_us(0)
        if start_us + self._rx_airtime_us(False) + min_reply_us > deadline_us:
            return
        connection.last_event_us = connection.radio.now_us
        packet = await self.receive(
            channel,
            min(
                deadline_us,
                connection.anchor_us
                + connection.receive_window_us
                + connection.receive_widening_us
                + connection.rx_phy.address_us
                - 1,
            ),
            start_us=start_us,
            anchor=True,
        )
        crc_errors = 0
        while packet is not None and not connection.closed:
            crc_errors = 0 if packet.valid_crc else crc_errors + 1
            if crc_errors == LL_CONN_CRC_ERROR_LIMIT:
                break
            item = self.pending or self._next_outbound(
                packet.end_us + self.control.tx_spacing_us(None)
            )
            transmission = self._plan_transmission(
                channel, item, radio_until_us, previous_end_us=packet.end_us
            )
            if transmission is None and self.pending is None and item != EMPTY_PDU:
                # A control response can require a new gap. ACK the received
                # packet at the current spacing and keep the response queued.
                transmission = self._plan_transmission(
                    channel, EMPTY_PDU, radio_until_us, previous_end_us=packet.end_us
                )
            if transmission is None:
                break
            more, end_us = await self._transmit(channel, transmission)
            if connection.remote_termination is not None:
                connection.complete(connection.remote_termination)
                break
            if packet.valid_crc and not (more or packet.pdu[0] & PDU_DATA_MD_MASK):
                break
            deadline_us = self._deadline_us(radio_until_us)
            # Leave room for the next received packet and its ACK. The exact TX
            # packet and spacing will be known after processing that reception.
            _, max_spacing_us = self.control.rx_spacing_window_us()
            min_reply_us = self.control.tx_spacing_us(None) + self._tx_airtime_us(0)
            if (
                end_us + max_spacing_us + self._rx_airtime_us(True) + min_reply_us
                > deadline_us
            ):
                break
            packet = await self._receive_response(channel, end_us, deadline_us)

    async def advance_to_us(
        self, target_us: int, *, radio_until_us: int | None = None
    ) -> int:
        """Run one connection event: channel selection, exchanges until close (Core Vol
        6, Part B, 4.5.1, 4.5.6, 4.5.8).
        """
        if self.connection.check_timeout():
            return self.connection.radio.now_us
        self.connection.skip_missed_events()
        if self.connection.closed:
            return self.connection.radio.now_us
        deadline = self.connection.next_deadline_us
        if (
            target_us < self.connection.event_start_us
            or self.connection.event_start_us >= deadline
        ):
            await self.connection.link.advance_idle_to_us(min(target_us, deadline))
            self.connection.check_timeout()
            return self.connection.radio.now_us
        previous_anchor = self.connection.anchor_us
        exchange = self.control.event_prepare()
        if self.connection.closed:
            return self.connection.radio.now_us
        if self.connection.anchor_us != previous_anchor and radio_until_us is not None:
            # An Instant can move this same ACL event beyond the current
            # radio grant. Let the intervening event run before retrying it.
            return self.connection.radio.now_us
        self.connection.unmapped_channel = (
            self.connection.unmapped_channel + self.connection.parameters.hop
        ) % LL_DATA_CHANNEL_COUNT
        if not exchange:
            # A pending non-connection-update Instant can fall on a skipped event.
            await self.connection.radio.advance_to_us(self.connection.anchor_us)
            self.connection.step_event()
            self.connection.check_timeout()
            return self.connection.radio.now_us
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

        if self.connection.role == Role.CENTRAL:
            await self._central_exchange(channel, radio_until_us)
        else:
            await self._peripheral_exchange(channel, radio_until_us)
        self.connection.step_event()
        self.connection.check_timeout()
        return self.connection.radio.now_us
