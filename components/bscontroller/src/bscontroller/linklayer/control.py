# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL control procedures and event-counter instants.

Wire procedures own pending state, not the radio scheduler. Changes are applied
at the start of a connection event before channel/PHY selection.
"""

from __future__ import annotations

import struct
from collections import deque
from dataclasses import replace
from typing import TYPE_CHECKING

from bscontroller.capabilities import CONTROLLER_SUPPORTED_PHY_MASK
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_CHANNEL_MAP_SIZE,
    LL_CONN_INTERVAL_UNIT_US,
    LL_DATA_CHANNEL_COUNT,
    LL_DATA_CHANNEL_COUNT_MIN,
    LL_DATA_CHANNEL_MAP_ALL,
    LL_EVENT_COUNTER_MASK,
    LL_INSTANT_MIN_LISTENING_EVENTS,
    LL_RESPONSE_TIMEOUT_US,
    PDU_CONTROL_CHANNEL_MAP_SIZE,
    PDU_CONTROL_FEATURE_SET_SIZE,
    PDU_CONTROL_FEATURE_SIZE,
    PDU_CONTROL_INSTANT_SIZE,
    PDU_CONTROL_MIN_USED_CHANNELS_SIZE,
    PDU_CONTROL_OPCODE_SIZE,
    PDU_CONTROL_PERIODIC_SYNC_SIZE,
    PDU_CONTROL_REJECT_EXT_SIZE,
    PDU_CONTROL_REJECT_SIZE,
    PDU_CONTROL_UNKNOWN_RSP_SIZE,
    PDU_CONTROL_VERSION_SIZE,
    ControlOpcode,
)
from bscontroller.linklayer.connection_parameters import (
    UPDATE,
    ConnectionParametersProcedure,
    anchor_offset,
)
from bscontroller.linklayer.data_length import DataLengthProcedure
from bscontroller.linklayer.data_path import TransmissionConstraints
from bscontroller.linklayer.features import FEATURES_NOT_SENT_TO_PEER, LeFeatureMask
from bscontroller.linklayer.instant import InstantUpdate
from bscontroller.linklayer.parameters import valid_channel_map
from bscontroller.linklayer.phy import PhyProcedure
from bscontroller.linklayer.procedure import ProcedureContext, ProcedureResponseTimer
from bscontroller.linklayer.state import Role
from bscontroller.linklayer.timing import LL_INSTANT_PAST_THRESHOLD
from bscontroller.linklayer.transfer import (
    PeriodicSyncIndication,
    TrainSource,
    transfer_indication,
)
from bscontroller.radio.base import PHY_OCTET_MASK, Phy

if TYPE_CHECKING:
    from bscontroller.linklayer.connection import Connection
    from bscontroller.linklayer.listener import ControlListener

PROCEDURE_COLLISION = (
    ErrorCode.LMP_ERROR_TRANSACTION_COLLISION_OR_LL_PROCEDURE_COLLISION_ERROR
)
Opcode = ControlOpcode


def valid_minimum_channels(phys: int, minimum: int) -> bool:
    return (
        bool(phys)
        and not phys & ~CONTROLLER_SUPPORTED_PHY_MASK
        and LL_DATA_CHANNEL_COUNT_MIN <= minimum <= LL_DATA_CHANNEL_COUNT
    )


class ControlProcedures:
    def __init__(self, connection: Connection, response_timer: ProcedureResponseTimer):
        self.connection = connection
        self.phy = PhyProcedure(self)
        self.connection_parameters = ConnectionParametersProcedure(self)
        self.response_timer = response_timer
        self.pending: InstantUpdate | None = None
        self.transmitted = False
        self.acknowledged = False
        self.data_length = DataLengthProcedure(connection)
        self.feature_context: ProcedureContext | None = None
        # Includes a local indication queued but not yet transmitted during a collision.
        self.version_sent = False
        self.peer_version: tuple[int, int, int] | None = None
        self.version_context: ProcedureContext | None = None
        self.host_channel_map = connection.parameters.channel_map
        self.peer_minimum_channel_phys = 0
        self.peer_minimum_used_channels = LL_DATA_CHANNEL_COUNT_MIN
        # LL_PERIODIC_SYNC_IND waiting for a describable train, and those already
        # in the TX queue, both in transmission order.
        self.transfers: deque[tuple[TrainSource, int]] = deque()
        self.released: deque[tuple[TrainSource, int]] = deque()

    @property
    def feature_request(self) -> bool:
        return self.feature_context is not None and self.feature_context.active

    @property
    def feature_deadline_us(self) -> int:
        return self.feature_context.deadline_us if self.feature_request else 0

    @property
    def version_request(self) -> bool:
        return self.version_context is not None and self.version_context.active

    @property
    def version_deadline_us(self) -> int:
        return self.version_context.deadline_us if self.version_request else 0

    def local_contexts(self) -> tuple[ProcedureContext, ...]:
        """Expose procedure instances for routing, without duplicate activity flags."""
        contexts = (
            self.feature_context,
            self.version_context,
            self.phy.context,
            self.connection_parameters.local_context,
            self.data_length.context,
        )
        return tuple(context for context in contexts if context is not None)

    @property
    def deadline_us(self) -> int:
        return self.response_timer.deadline_us

    @property
    def listener(self) -> ControlListener:
        return self.connection.link.listener

    @property
    def busy(self) -> bool:
        return bool(
            self.pending
            or self.connection_parameters.remote_parameters
            or self.connection_parameters.parameter_request
            or self.phy.phy_request
            or self.data_length.pending
            or self.feature_request
        )

    @property
    def next_deadline_us(self) -> int:
        deadlines = [
            deadline
            for deadline in (
                self.deadline_us,
                self.data_length.deadline_us,
                self.feature_deadline_us,
                self.version_deadline_us,
            )
            if deadline
        ]
        return min(deadlines) if deadlines else 0

    def transmission_constraints(self) -> TransmissionConstraints:
        """Combine committed link state with admitted transition restrictions.

        DLE receives a value describing possible PHYs and the shortest interval;
        it does not inspect another procedure's pending fields (Core 5.1.7.4,
        5.1.10.1).
        """
        connection, controls = self.connection, self
        tx_phys = {connection.tx_phy}
        rx_phy = connection.rx_phy
        interval = connection.parameters.interval
        if controls.connection_parameters.requested_interval_min:
            interval = min(
                interval, controls.connection_parameters.requested_interval_min
            )
        if controls.pending:
            update = controls.pending
            if update.parameters:
                interval = min(interval, update.parameters.interval)
            tx, rx = (
                (update.central_tx_phy, update.peripheral_tx_phy)
                if connection.role == Role.CENTRAL
                else (update.peripheral_tx_phy, update.central_tx_phy)
            )
            # PHY packet-time restrictions start at the local control transmission.
            if update.local and not controls.transmitted:
                tx = rx = 0
            if tx:
                tx_phys.add(Phy(tx))
            if rx:
                rx_phy = Phy(rx)
        elif (
            controls.phy.phy_request
            and self.phy.context is not None
            and self.phy.context.transmitted
            and connection.role == Role.PERIPHERAL
        ):
            # Until the Central selects a PHY, a Peripheral must be safe on every
            # PHY it offered, including one with a slower packet airtime.
            tx_phys.update(
                phy for phy in Phy if controls.phy.tx_phys & (1 << (phy - 1))
            )
        return TransmissionConstraints(
            frozenset(tx_phys), rx_phy, interval * LL_CONN_INTERVAL_UNIT_US
        )

    def enqueue(self, payload: bytes) -> None:
        # Core Vol 6, Part B, 5.2: each queued procedure PDU restarts a running T_PRT.
        self.response_timer.restart(self.connection.radio.now_us)
        self.connection.enqueue_control(payload)

    def reject(self, opcode: int, reason: int) -> None:
        self.enqueue(bytes([Opcode.LL_REJECT_EXT_IND, opcode, reason]))

    def instant(self) -> int:
        """Follow the recommended six listening-event lead (Core 6.3, Vol 6, Part B,
        5.5).
        """
        return (
            self.connection.event_counter
            + LL_INSTANT_MIN_LISTENING_EVENTS * (self.connection.parameters.latency + 1)
        ) & LL_EVENT_COUNTER_MASK

    def accept_instant(self, update: InstantUpdate) -> None:
        """Peripheral acceptance of an Instant; a past Instant loses the connection
        (Core Vol 6, Part B, 5.5.1).
        """
        distance = (
            update.instant - self.connection.event_counter
        ) & LL_EVENT_COUNTER_MASK
        if distance >= LL_INSTANT_PAST_THRESHOLD:
            self.connection.complete(ErrorCode.INSTANT_PASSED_ERROR)
        elif self.pending is not None:
            self.connection.complete(ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR)
        else:
            self.pending = update
            if distance == 0:
                self.apply(received_at_instant=True)

    def request_channel_map(self, channel_map: int) -> None:
        """Central Channel Map Update from the Host classification (Core Vol 6, Part B,
        5.1.2; Vol 4, Part E, 7.8.19).
        """
        self.host_channel_map = channel_map
        self._start_channel_map(self.selected_channel_map())

    def selected_channel_map(self) -> int:
        """Prefer Host-usable channels; fill missing indices in ascending order.

        This deterministic classification policy is local, not a Core-mandated
        AFH algorithm. The peer's latest indication applies to its listed PHYs.
        """
        value = self.host_channel_map
        if self.peer_minimum_channel_phys & (1 << (self.connection.rx_phy - 1)):
            for channel in range(LL_DATA_CHANNEL_COUNT):
                if value.bit_count() >= self.peer_minimum_used_channels:
                    break
                value |= 1 << channel
        return value & LL_DATA_CHANNEL_MAP_ALL

    def _refresh_channel_map(self) -> None:
        if (
            self.connection.role == Role.CENTRAL
            and not self.connection.closed
            and self.peer_minimum_channel_phys
            and not self.busy
        ):
            value = self.selected_channel_map()
            if value != self.connection.parameters.channel_map:
                self._start_channel_map(value)

    def _start_channel_map(self, channel_map: int) -> None:
        instant = self.instant()
        self.pending = InstantUpdate(instant, channel_map=channel_map, local=True)
        self.transmitted = self.acknowledged = False
        self.enqueue(
            bytes([Opcode.LL_CHANNEL_MAP_IND])
            + channel_map.to_bytes(LL_CHANNEL_MAP_SIZE, 'little')
            + instant.to_bytes(PDU_CONTROL_INSTANT_SIZE, 'little')
        )

    def request_minimum_channels(self, phys: int, minimum: int) -> None:
        """Queue a Peripheral indication after Feature Exchange, on its radio thread."""
        feature = LeFeatureMask.MINIMUM_NUMBER_OF_USED_CHANNELS_PROCEDURE
        if self.connection.closed or self.connection.role != Role.PERIPHERAL:
            raise ValueError('minimum used channels requires a live Peripheral link')
        if not valid_minimum_channels(phys, minimum):
            raise ValueError('invalid PHY mask or minimum used channel count')
        if not (
            self.connection.link.features
            & (self.connection.peer_features or 0)
            & feature
        ):
            raise ValueError('minimum used channels requires negotiated peer support')
        self.enqueue(bytes([Opcode.LL_MIN_USED_CHANNELS_IND, phys, minimum]))

    def transfer(self, source: TrainSource, service_data: int) -> None:
        """Queue LL_PERIODIC_SYNC_IND for ``source``'s train.

        The procedure has no response and completes once the PDU is sent, so it
        holds no procedure slot and no response timer (Core Vol 6, Part B,
        5.1.13). The PDU waits until the train can be described (2.4.2.27), and
        is dropped if the train ends before that.
        """
        self.transfers.append((source, service_data))
        self.release_transfers()

    def release_transfers(self) -> None:
        """Queue each waiting transfer whose train can now be described.

        The queued PDU describes the train relative to this event. prepare()
        describes it again relative to the transmitting event, and keeps this
        description only if the train can no longer be described then.
        """
        waiting: deque[tuple[TrainSource, int]] = deque()
        for source, service_data in self.transfers:
            if source.ended:
                continue
            train = source.reference()
            payload = (
                None
                if train is None
                else transfer_indication(train, self.connection, service_data)
            )
            if payload is None:
                waiting.append((source, service_data))
                continue
            self.released.append((source, service_data))
            self.connection.enqueue_control(payload)
        self.transfers = waiting

    def prepare(self, payload: bytes) -> bytes:
        """Finalize a control PDU at its first transmission.

        Describe a released sync transfer relative to the transmitting event,
        and preserve the recommended Instant lead (Core 6.3, Vol 6, Part B,
        2.4.2.27 and 5.5).
        """
        if payload[0] == Opcode.LL_PERIODIC_SYNC_IND and self.released:
            source, service_data = self.released.popleft()
            train = source.reference()
            if train is not None:
                payload = (
                    transfer_indication(train, self.connection, service_data) or payload
                )
            return payload
        if self.connection.role == Role.PERIPHERAL and payload[0] in (
            Opcode.LL_PHY_REQ,
            Opcode.LL_PHY_RSP,
        ):
            self.phy.mark_transmitted()
        update = self.pending
        if (
            update is not None
            and update.local
            and not self.transmitted
            and payload[0]
            in (
                Opcode.LL_CONNECTION_UPDATE_IND,
                Opcode.LL_CHANNEL_MAP_IND,
                Opcode.LL_PHY_UPDATE_IND,
            )
        ):
            # A queued ACL retransmission can delay the first control transmission.
            # Reserve six listening events from actual transmission, not HCI input.
            instant = self.instant()
            if update.parameters is not None and update.anchor_reference is not None:
                parameters = replace(
                    update.parameters,
                    window_offset=anchor_offset(
                        update.anchor_reference,
                        instant,
                        self.connection.parameters.interval,
                        update.parameters.interval,
                    ),
                )
                update = replace(update, parameters=parameters)
                payload = bytes([Opcode.LL_CONNECTION_UPDATE_IND]) + UPDATE.pack(
                    parameters.window_size,
                    parameters.window_offset,
                    parameters.interval,
                    parameters.latency,
                    parameters.timeout,
                    instant,
                )
            self.pending = replace(update, instant=instant)
            self.transmitted = True
            return payload[:-PDU_CONTROL_INSTANT_SIZE] + instant.to_bytes(
                PDU_CONTROL_INSTANT_SIZE, 'little'
            )
        return payload

    def acknowledge(self, payload: bytes) -> None:
        update = self.pending
        if (
            update
            and update.local
            and payload[0]
            in (
                Opcode.LL_CONNECTION_UPDATE_IND,
                Opcode.LL_CHANNEL_MAP_IND,
                Opcode.LL_PHY_UPDATE_IND,
            )
            and int.from_bytes(payload[-PDU_CONTROL_INSTANT_SIZE:], 'little')
            == update.instant
        ):
            self.acknowledged = True

    def sent(self, payload: bytes) -> None:
        if payload[0] == Opcode.LL_PHY_UPDATE_IND:
            self.phy.indication_sent(payload)

    def request_features(self) -> None:
        """Start Feature Exchange or report the cached result (Core Vol 6, Part B,
        5.1.4).
        """
        opcode = (
            Opcode.LL_FEATURE_REQ
            if self.connection.role == Role.CENTRAL
            else Opcode.LL_PERIPHERAL_FEATURE_REQ
        )
        self.feature_context = ProcedureContext(
            opcode, frozenset((Opcode.LL_FEATURE_RSP,)), self, host_initiated=True
        )
        if self.connection.peer_features is not None:
            self.finish_features(ErrorCode.SUCCESS)
            return
        self.feature_context.deadline_us = (
            self.connection.radio.now_us + LL_RESPONSE_TIMEOUT_US
        )
        self.enqueue(
            bytes(
                [
                    (
                        Opcode.LL_FEATURE_REQ
                        if self.connection.role == Role.CENTRAL
                        else Opcode.LL_PERIPHERAL_FEATURE_REQ
                    )
                ]
            )
            + (self.connection.link.features & ~FEATURES_NOT_SENT_TO_PEER).to_bytes(
                PDU_CONTROL_FEATURE_SET_SIZE, 'little'
            )
        )

    def finish_features(self, status: int) -> None:
        if not self.feature_request:
            return
        self.feature_context.finish()
        self.listener.on_remote_features(self.connection, status)

    def send_version(self) -> None:
        """Send LL_VERSION_IND at most once per connection (Core Vol 6, Part B,
        5.1.5).
        """
        if not self.version_sent:
            self.version_sent = True
            self.connection.enqueue_control(
                bytes([Opcode.LL_VERSION_IND])
                + struct.pack('<BHH', *self.listener.local_version),
                priority=True,
            )

    def request_version(self) -> None:
        self.version_context = ProcedureContext(
            Opcode.LL_VERSION_IND,
            frozenset((Opcode.LL_VERSION_IND,)),
            self,
            host_initiated=True,
        )
        if self.peer_version is not None:
            self.finish_version(ErrorCode.SUCCESS)
            return
        self.version_context.deadline_us = (
            self.connection.radio.now_us + LL_RESPONSE_TIMEOUT_US
        )
        self.send_version()

    def finish_version(self, status: int | None) -> None:
        if not self.version_request:
            return
        self.version_context.finish()
        if status is not None:
            self.listener.on_remote_version(self.connection, status)

    def control(self, payload: bytes) -> bool:
        """Handle a received LL Control PDU, applying collision rules (Core Vol 6, Part
        B, 5.1, 5.3).
        """
        connection = self.connection
        if self.phy.control(payload) or self.connection_parameters.control(payload):
            return True
        if self.data_length.control(payload):
            return True
        opcode = payload[0]
        if (
            opcode in (Opcode.LL_FEATURE_REQ, Opcode.LL_PERIPHERAL_FEATURE_REQ)
            and len(payload) == PDU_CONTROL_FEATURE_SIZE
        ):
            connection.peer_features = int.from_bytes(payload[1:], 'little')
            # Use the first-octet feature intersection and retain local higher octets
            # for FEATURE_RSP (Vol 6, Part B, 5.1.4.1; 5.1.4.2).
            local_features = connection.link.features & ~FEATURES_NOT_SENT_TO_PEER
            features = (local_features & ~PHY_OCTET_MASK) | (
                local_features & payload[1]
            )
            connection.enqueue_control(
                bytes([Opcode.LL_FEATURE_RSP])
                + features.to_bytes(PDU_CONTROL_FEATURE_SET_SIZE, "little"),
                priority=True,
            )
            return True
        if opcode == Opcode.LL_FEATURE_RSP and len(payload) == PDU_CONTROL_FEATURE_SIZE:
            connection.peer_features = int.from_bytes(payload[1:], 'little')
            self.finish_features(ErrorCode.SUCCESS)
            return True
        if (
            opcode == Opcode.LL_MIN_USED_CHANNELS_IND
            and len(payload) == PDU_CONTROL_MIN_USED_CHANNELS_SIZE
            and connection.role == Role.CENTRAL
            and connection.link.features
            & LeFeatureMask.MINIMUM_NUMBER_OF_USED_CHANNELS_PROCEDURE
        ):
            phys = payload[1] & CONTROLLER_SUPPORTED_PHY_MASK
            minimum = payload[2]
            if not valid_minimum_channels(phys, minimum):
                return False
            self.peer_minimum_channel_phys = phys
            self.peer_minimum_used_channels = minimum
            self._refresh_channel_map()
            return True
        if (
            opcode == Opcode.LL_PERIODIC_SYNC_IND
            and len(payload) == PDU_CONTROL_PERIODIC_SYNC_SIZE
            and connection.link.features
            & LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_RECIPIENT
        ):
            connection.link.roles.synchronization.transfer_received(
                connection, PeriodicSyncIndication.decode(payload)
            )
            return True
        if opcode == Opcode.LL_VERSION_IND and len(payload) == PDU_CONTROL_VERSION_SIZE:
            # Cache the first peer version and send ours at most once. Repeated
            # Host reads can finish from the cache without another air exchange.
            if self.peer_version is None:
                self.peer_version = struct.unpack(
                    '<BHH', payload[PDU_CONTROL_OPCODE_SIZE:]
                )
                self.send_version()
                self.finish_version(ErrorCode.SUCCESS)
            return True
        if (
            opcode == Opcode.LL_CHANNEL_MAP_IND
            and len(payload) == PDU_CONTROL_CHANNEL_MAP_SIZE
        ):
            channel_map = int.from_bytes(
                payload[
                    PDU_CONTROL_OPCODE_SIZE : PDU_CONTROL_OPCODE_SIZE
                    + LL_CHANNEL_MAP_SIZE
                ],
                'little',
            )
            if connection.role != Role.PERIPHERAL or not valid_channel_map(channel_map):
                connection.complete(ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR)
            else:
                self.accept_instant(
                    InstantUpdate(
                        int.from_bytes(
                            payload[PDU_CONTROL_OPCODE_SIZE + LL_CHANNEL_MAP_SIZE :],
                            'little',
                        ),
                        channel_map=channel_map,
                    )
                )
            return True
        if (
            opcode == Opcode.LL_UNKNOWN_RSP
            and len(payload) == PDU_CONTROL_UNKNOWN_RSP_SIZE
        ):
            if self.feature_request and payload[1] in (
                Opcode.LL_FEATURE_REQ,
                Opcode.LL_PERIPHERAL_FEATURE_REQ,
            ):
                self.finish_features(ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)
            elif (
                self.connection_parameters.parameter_request
                and payload[1] == Opcode.LL_CONNECTION_PARAM_REQ
            ):
                self.connection_parameters.reject_parameters(
                    ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
                )
            elif self.phy.phy_request and payload[1] == Opcode.LL_PHY_REQ:
                # UNKNOWN_RSP also ends PHY negotiation (Vol 6, Part B, 5.1.10.1).
                # Report failure to the Host and stop its timer, even while ACL is
                # paused.
                self.phy.reject_phy(ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)
            return True
        if opcode == Opcode.LL_REJECT_IND and len(payload) == PDU_CONTROL_REJECT_SIZE:
            # The legacy reject has no rejected opcode. Route it using the outstanding
            # procedure (Vol 6, Part B, 2.4.2.14).
            if self.feature_request:
                self.finish_features(payload[1])
            elif self.connection_parameters.parameter_request:
                self.connection_parameters.reject_parameters(payload[1])
            return True
        if (
            opcode == Opcode.LL_REJECT_EXT_IND
            and len(payload) == PDU_CONTROL_REJECT_EXT_SIZE
            and payload[1] in (Opcode.LL_FEATURE_REQ, Opcode.LL_PERIPHERAL_FEATURE_REQ)
        ):
            self.finish_features(payload[2])
            return True
        if (
            opcode == Opcode.LL_REJECT_EXT_IND
            and len(payload) == PDU_CONTROL_REJECT_EXT_SIZE
            and payload[1] in (Opcode.LL_CONNECTION_PARAM_REQ, Opcode.LL_PHY_REQ)
        ):
            if (
                payload[1] == Opcode.LL_CONNECTION_PARAM_REQ
                and self.connection_parameters.parameter_request
            ):
                self.connection_parameters.reject_parameters(payload[2])
            elif payload[1] == Opcode.LL_PHY_REQ and self.phy.phy_request:
                self.phy.reject_phy(payload[2])
            return True
        return False

    def apply(self, *, received_at_instant: bool = False) -> None:
        """Apply a pending update when connEventCount reaches its Instant (Core Vol 6,
        Part B, 5.1.1, 5.1.2, 5.1.10, 5.5.1).
        """
        connection = self.connection
        update = self.pending
        if self.check_timeout():
            return
        if update is None:
            self._refresh_channel_map()
            return
        if update.instant != connection.event_counter:
            return
        # Wait until the local update has been sent. Close on an unacknowledged
        # update at the Instant rather than applying a schedule the peer may not know.
        if update.local and not self.transmitted:
            return
        if update.local and not self.acknowledged:
            connection.complete(ErrorCode.INSTANT_PASSED_ERROR)
            return
        # Release the procedure slot before callbacks can start another procedure
        # against the newly committed parameters.
        self.pending = None
        if update.central_tx_phy or update.peripheral_tx_phy:
            if self.phy.context is not None:
                self.response_timer.stop(self.phy.context)
        if update.parameters is not None:
            old = connection.parameters
            notify = (
                update.notify
                if update.notify is not None
                else (
                    self.connection_parameters.parameter_request
                    or self.connection_parameters.requested_interval_min
                    or (old.interval, old.latency, old.timeout)
                    != (
                        update.parameters.interval,
                        update.parameters.latency,
                        update.parameters.timeout,
                    )
                )
            )
            self.connection_parameters.finish()
            connection.parameters = update.parameters
            if not received_at_instant:
                # Reception at the Instant already provides the new anchor; applying the
                # offset again would shift it twice (Vol 6, Part B, 5.1.1).
                connection.anchor_us += (
                    update.parameters.window_offset * LL_CONN_INTERVAL_UNIT_US
                )
            connection.update_window = not received_at_instant
            connection.supervision_restart_us = connection.anchor_us
            if notify:
                self.listener.on_connection_updated(connection, ErrorCode.SUCCESS)
        if update.channel_map is not None:
            connection.parameters = replace(
                connection.parameters, channel_map=update.channel_map
            )
        if update.central_tx_phy or update.peripheral_tx_phy:
            # On-air fields name roles; translate them into this endpoint's TX/RX.
            tx, rx = (
                (update.central_tx_phy, update.peripheral_tx_phy)
                if connection.role == Role.CENTRAL
                else (update.peripheral_tx_phy, update.central_tx_phy)
            )
            connection.tx_phy = Phy(tx or connection.tx_phy)
            connection.rx_phy = Phy(rx or connection.rx_phy)
            self.phy.finish(ErrorCode.SUCCESS)
        self.data_length.refresh()
        self._refresh_channel_map()

    def check_timeout(self) -> bool:
        """Procedure response timeout T_PRT (Core Vol 6, Part B, 5.2)."""
        if self.data_length.check_timeout():
            return True
        if (
            self.version_deadline_us
            and self.connection.radio.now_us >= self.version_deadline_us
        ):
            self.finish_version(ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)
            self.connection.complete(ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)
            return True
        if (
            self.feature_deadline_us
            and self.connection.radio.now_us >= self.feature_deadline_us
        ):
            self.finish_features(ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)
            self.connection.complete(ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)
            return True
        if self.deadline_us and self.connection.radio.now_us >= self.deadline_us:
            self.connection.complete(ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)
            return True
        return False
