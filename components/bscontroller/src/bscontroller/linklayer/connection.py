# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""ACL connection lifetime and parameters, above packet execution and LLCP.

ConnectionEvent owns the ARQ queue and radio exchange; LlcpCoordinator connects
control procedures to its RX/ACK events. This context owns supervision and the
current parameters (Core Vol 6, Part B, 4.5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_CONN_ESTABLISHMENT_EVENTS,
    LL_CONN_INTERVAL_UNIT_US,
    LL_DATA_CHANNEL_COUNT,
    LL_EVENT_COUNTER_MASK,
    LL_SUPERVISION_TIMEOUT_UNIT_US,
    ControlOpcode,
)
from bscontroller.linklayer.connection_event import ConnectionEvent
from bscontroller.linklayer.data_path import PacketSecurity
from bscontroller.linklayer.llcp import LlcpCoordinator
from bscontroller.linklayer.parameters import Parameters
from bscontroller.linklayer.state import Role
from bscontroller.linklayer.timing import (
    LL_CONN_RX_WINDOW_CODED_US,
    LL_CONN_RX_WINDOW_UNCODED_US,
    window_widening,
)
from bscontroller.radio.base import PHY_CODED_S8_FACTOR, Phy, Radio

if TYPE_CHECKING:
    from bscontroller.address import Address
    from bscontroller.linklayer.link import RadioLink
    from bscontroller.linklayer.listener import ConnectionListener


Opcode = ControlOpcode


class Connection:
    def __init__(
        self,
        link: RadioLink,
        parameters: Parameters,
        peer: Address,
        role: int,
        connect_end_us: int,
        csa2: bool = False,
        self_address: Address | None = None,
        window_delay_us: int = LL_CONN_INTERVAL_UNIT_US,
        phy: Phy = Phy.LE_1M,
    ):
        self.link = link
        self.handle = link.roles.connections.allocate_handle()
        self.radio: Radio = link.radio
        self.parameters: Parameters = parameters
        self.peer = peer
        self.peer_rpa: Address | None = None
        self.local_rpa: Address | None = None
        self.self_address = self_address
        self.role: int = role
        self.csa2 = csa2
        self.anchor_us: int = (
            connect_end_us
            + window_delay_us
            + parameters.window_offset * LL_CONN_INTERVAL_UNIT_US
        )
        self.event_counter: int = 0
        # The latest event with a received packet and the one before it, for
        # syncConnEventCount; None until there is one.
        self.rx_event_counters: tuple[int | None, int | None] = (None, None)
        # Transfer mode, from the defaults when the connection is created (Core
        # Vol 4, Part E, 7.8.92).
        self.sync_transfer = link.sync_transfer
        self.unmapped_channel = 0
        self.established = False
        # Defaults belong to LL connection creation, before any notification.
        self.reported = False
        self.last_rx_us: int = connect_end_us
        self.supervision_restart_us: int = connect_end_us
        self.last_event_us: int = connect_end_us
        self.remote_termination: int | None = None
        self.termination_deadline_us = 0
        # Disconnection Complete reason once the local ACL Termination procedure ends.
        self.termination_status = ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR
        self.closed: bool = False
        self.advertising_handle: int | None = None
        self.advertising_events = 0
        self.tx_phy: Phy = phy
        self.rx_phy: Phy = phy
        self.peer_features: int | None = None
        self.rssi: int | None = None
        self.tx_coding: int = PHY_CODED_S8_FACTOR
        self.update_window: bool = False
        self.packet_security = PacketSecurity()
        self.llcp = LlcpCoordinator(self)
        self.event = ConnectionEvent(self, self.llcp, self.packet_security)
        self.link.trace(
            f"LL CREATED role={role} aa={parameters.access_address:08x} "
            f"anchor={self.anchor_us} "
            f"interval={parameters.interval * LL_CONN_INTERVAL_UNIT_US}"
        )

    @property
    def controls(self):
        return self.llcp.controls

    @property
    def encryption(self):
        return self.llcp.encryption

    @property
    def listener(self) -> ConnectionListener:
        return self.link.listener

    def enqueue_acl(self, continuation: bool, payload: bytes) -> None:
        return self.event.enqueue_acl(continuation, payload)

    def terminate(
        self,
        reason: int,
        *,
        status: int = ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR,
    ) -> None:
        """ACL Termination procedure: T_terminate starts at connSupervisionTimeout (Core
        Vol 6, Part B, 5.1.6).
        """
        self.termination_status = status
        if not self.termination_deadline_us:
            self.termination_deadline_us = (
                self.radio.now_us
                + self.parameters.timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
            )
        self.enqueue_control(bytes([Opcode.LL_TERMINATE_IND, reason]), priority=True)

    def enqueue_control(self, payload: bytes, *, priority: bool = False) -> None:
        return self.event.enqueue_control(payload, priority=priority)

    def complete(self, reason: int) -> None:
        """Exit the Connection state and report the loss reason to the Host (Core Vol 6,
        Part B, 4.5.12).
        """
        if self.closed:
            return
        if (
            reason == ErrorCode.CONNECTION_TERMINATED_DUE_TO_MIC_FAILURE_ERROR
            and self.peer_rpa is not None
        ):
            # MIC failures can abort before Encryption Change is delivered.
            self.link.privacy.encryption_failed(self.peer)
        self.llcp.close(reason)
        self.closed = True
        self.link.trace(
            f"LL DISCONNECTED reason=0x{reason:02x} retries={self.event.retries}"
        )
        self.link.roles.connections.release(self)
        self.listener.on_disconnected(self, reason)

    def report_created(self) -> None:
        if not self.reported:
            self.reported = True
            self.listener.on_connected(self)

    def received_in_event(self) -> None:
        """Record that this event received a packet from the peer."""
        latest, _ = self.rx_event_counters
        if latest != self.event_counter:
            self.rx_event_counters = (self.event_counter, latest)

    @property
    def sync_event_counter(self) -> int:
        """syncConnEventCount: the latest earlier event with a received packet.

        Core Vol 6, Part B, 2.4.2.27 also allows this event once its packet is
        received. An earlier event costs the recipient one more interval of
        window widening, but keeps syncConnEventCount different from the
        transmitting event, which some recipients require. This event is used
        only when no earlier one received a packet.
        """
        latest, earlier = self.rx_event_counters
        if latest == self.event_counter and earlier is not None:
            return earlier
        return self.event_counter if latest is None else latest

    def establish(self) -> None:
        """Connection established on first reception from the peer, CRC valid or not
        (Core Vol 6, Part B, 4.5.2, 4.5.5).
        """
        if self.established:
            return
        self.established = True
        self.link.trace("LL ESTABLISHED")
        self.report_created()
        self.controls.data_length.start_if_needed()

    def control(self, payload: bytes) -> None:
        self.llcp.control(payload)

    def accept(self, pdu: bytes, valid_crc: bool) -> None:
        return self.event.accept(pdu, valid_crc)

    async def send(self, channel: int, start_us: int) -> bool:
        return await self.event.send(channel, start_us)

    async def receive(
        self, channel: int, until_us: int, *, start_us: int | None = None
    ):
        return await self.event.receive(channel, until_us, start_us=start_us)

    def next_exchange_us(self, *, max_rx: bool = True) -> int:
        return self.event.next_exchange_us(max_rx=max_rx)

    @property
    def response_window_us(self) -> int:
        return (
            LL_CONN_RX_WINDOW_CODED_US
            if self.rx_phy == Phy.LE_CODED
            else LL_CONN_RX_WINDOW_UNCODED_US
        )

    @property
    def receive_window_us(self) -> int:
        return (
            self.parameters.window_size * LL_CONN_INTERVAL_UNIT_US
            if not self.established or self.update_window
            else self.response_window_us
        )

    def skip_missed_events(self) -> None:
        """A radio conflict advances counters and Instants, never transmits late."""
        while not self.closed:
            latest = self.anchor_us
            if self.role == Role.PERIPHERAL:
                latest += self.receive_window_us + window_widening(
                    self.anchor_us - self.last_rx_us, self.parameters.sca
                )
            if latest > self.radio.now_us:
                break
            update = self.controls.pending
            self.llcp.event_prepare()
            if self.closed:
                return
            if update is not self.controls.pending:
                # An Instant can move or widen this event's receive window.
                continue
            self.unmapped_channel = (
                self.unmapped_channel + self.parameters.hop
            ) % LL_DATA_CHANNEL_COUNT
            self.anchor_us += self.parameters.interval * LL_CONN_INTERVAL_UNIT_US
            self.event_counter = (self.event_counter + 1) & LL_EVENT_COUNTER_MASK
            self.link.trace(
                f'LL MISSED handle={self.handle} next_event={self.event_counter}'
            )

    async def advance_to_us(
        self, target_us: int, *, radio_until_us: int | None = None
    ) -> int:
        return await self.event.advance_to_us(target_us, radio_until_us=radio_until_us)

    @property
    def supervision_deadline_us(self) -> int:
        """Core Vol 6, Part B, 4.5.2: connSupervisionTimeout, or six intervals before
        establishment.
        """
        timeout = (
            self.parameters.timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
            if self.established
            else LL_CONN_ESTABLISHMENT_EVENTS
            * self.parameters.interval
            * LL_CONN_INTERVAL_UNIT_US
        )
        return max(self.last_rx_us, self.supervision_restart_us) + timeout

    @property
    def next_deadline_us(self) -> int:
        deadline = self.supervision_deadline_us
        return min(
            deadline,
            self.termination_deadline_us or deadline,
            self.llcp.next_deadline_us or deadline,
        )

    def check_timeout(self) -> bool:
        if self.closed or self.llcp.check_timeout():
            return True
        if (
            self.termination_deadline_us
            and self.radio.now_us >= self.termination_deadline_us
        ):
            self.complete(self.termination_status)
        elif self.radio.now_us >= self.supervision_deadline_us:
            self.complete(
                ErrorCode.CONNECTION_TIMEOUT_ERROR
                if self.established
                else ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR
            )
        return self.closed

    @property
    def next_operation_us(self) -> int:
        return min(self.event_start_us, self.next_deadline_us)

    @property
    def event_start_us(self) -> int:
        start = self.anchor_us
        if self.role == Role.PERIPHERAL:
            start -= window_widening(
                self.anchor_us - self.last_rx_us, self.parameters.sca
            )
        return start
