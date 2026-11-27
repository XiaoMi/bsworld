# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL control procedures and event-counter instants.

Wire procedures own pending state, not the radio scheduler. Changes are applied
at the start of a connection event before channel/PHY selection.
"""

from __future__ import annotations

import struct
from dataclasses import replace
from typing import TYPE_CHECKING

from bscontroller.capabilities import CONTROLLER_SUPPORTED_PHY_MASK
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    LL_DATA_CHANNEL_COUNT,
    LL_DATA_CHANNEL_COUNT_MIN,
    LL_DATA_CHANNEL_MAP_ALL,
    LL_EVENT_COUNTER_MASK,
    LL_INSTANT_MIN_LISTENING_EVENTS,
    PDU_CONTROL_CHANNEL_MAP_SIZE,
    PDU_CONTROL_MIN_USED_CHANNELS_SIZE,
    PDU_CONTROL_OPCODE_SIZE,
    PDU_CONTROL_PERIODIC_SYNC_SIZE,
    PDU_CONTROL_PERIODIC_SYNC_WR_SIZE,
    PDU_CONTROL_REJECT_EXT_SIZE,
    PDU_CONTROL_REJECT_SIZE,
    PDU_CONTROL_UNKNOWN_RSP_SIZE,
    PDU_CONTROL_VERSION_SIZE,
    ControlOpcode,
)
from bscontroller.linklayer.clock_accuracy import ClockAccuracyProcedure
from bscontroller.linklayer.connection_parameters import ConnectionParametersProcedure
from bscontroller.linklayer.connection_rate import (
    ConnectionRateProcedure,
)
from bscontroller.linklayer.data_length import DataLengthProcedure
from bscontroller.linklayer.feature_exchange import FeatureExchangeProcedure
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.frame_space import FrameSpaceProcedure
from bscontroller.linklayer.instant import CHANNEL_MAP, InstantUpdate, anchor_offset
from bscontroller.linklayer.packet_control import TransmissionConstraints
from bscontroller.linklayer.parameters import valid_channel_map
from bscontroller.linklayer.phy import PhyProcedure
from bscontroller.linklayer.power_control import PowerControl
from bscontroller.linklayer.procedure import (
    DeferredProcedure,
    ProcedureCompletion,
    ProcedureContext,
    ProcedureOrigin,
)
from bscontroller.linklayer.state import Role
from bscontroller.linklayer.subrating import Subrating
from bscontroller.linklayer.timing import LL_INSTANT_PAST_THRESHOLD, counter_distance
from bscontroller.linklayer.transfer import (
    PeriodicSyncIndication,
    TrainSource,
    transfer_indication,
)
from bscontroller.radio.base import Phy

if TYPE_CHECKING:
    from bscontroller.linklayer.connection import Connection
    from bscontroller.linklayer.listener import ControlListener

Opcode = ControlOpcode
PAST_INDICATIONS = (Opcode.LL_PERIODIC_SYNC_IND, Opcode.LL_PERIODIC_SYNC_WR_IND)


def valid_minimum_channels(phys: int, minimum: int) -> bool:
    return (
        bool(phys)
        and not phys & ~CONTROLLER_SUPPORTED_PHY_MASK
        and LL_DATA_CHANNEL_COUNT_MIN <= minimum <= LL_DATA_CHANNEL_COUNT
    )


class ControlProcedures:
    def __init__(self, connection: Connection):
        self.connection = connection
        self.phy = PhyProcedure(self)
        self.connection_parameters = ConnectionParametersProcedure(self)
        self._pending: InstantUpdate | None = None
        self._instant_transmitted = False
        self._instant_acknowledged = False
        self._awaiting_instant_ack_confirmation = False
        self.frame_space = FrameSpaceProcedure(self)
        self.data_length = DataLengthProcedure(connection, self.frame_space)
        self.features = FeatureExchangeProcedure(self)
        self.power = PowerControl(self)
        self.clock_accuracy = ClockAccuracyProcedure(self)
        self.subrating = Subrating(self)
        self.rate = ConnectionRateProcedure(self)
        self._local_procedures = (
            self.power,
            self.clock_accuracy,
            self.subrating,
            self.rate,
            self.frame_space,
        )
        # Includes a local indication queued but not yet transmitted during a collision.
        self.version_sent = False
        self.peer_version: tuple[int, int, int] | None = None
        self.version_context: ProcedureContext | None = None
        self.host_channel_map = connection.parameters.channel_map
        self.peer_minimum_channel_phys = 0
        self.peer_minimum_used_channels = LL_DATA_CHANNEL_COUNT_MIN
        self.active_transfer: tuple[TrainSource, int] | None = None

    @property
    def feature_request(self) -> bool:
        return self.features.pending

    @property
    def version_request(self) -> bool:
        return self.version_context is not None and self.version_context.active

    @property
    def version_deadline_us(self) -> int | None:
        return self.version_context.deadline_us if self.version_request else None

    @property
    def listener(self) -> ControlListener:
        return self.connection.link.listener

    @property
    def peer_instant_blocked(self) -> bool:
        """Preserve the admission gates for peer-initiated Instant procedures."""
        return bool(
            self.pending
            or self.connection_parameters.remote_parameters
            or self.connection_parameters.parameter_request
            or self.phy.phy_request
            or self.data_length.pending
            or self.feature_request
        )

    @property
    def blocks_local_procedure(self) -> bool:
        """Keep the shared Instant and peer decision paths available."""
        return (
            self.pending is not None
            or self.connection_parameters.remote_parameters is not None
            or (
                self.phy.phy_request
                and self.phy.context.origin == ProcedureOrigin.REMOTE
            )
            or self.frame_space.response is not None
        )

    def _instant_phys(self, update: InstantUpdate) -> tuple[int, int]:
        if self.connection.role == Role.CENTRAL:
            return update.central_tx_phy, update.peripheral_tx_phy
        return update.peripheral_tx_phy, update.central_tx_phy

    def link_timings(self) -> tuple[tuple[Phy, Phy, int], ...]:
        """Return the current timing and any timing scheduled at an Instant."""
        connection, update = self.connection, self.pending
        current = (
            connection.tx_phy,
            connection.rx_phy,
            connection.parameters.interval_us,
        )
        if update is None:
            return (current,)
        tx, rx = self._instant_phys(update)
        pending = (
            Phy(tx or connection.tx_phy),
            Phy(rx or connection.rx_phy),
            update.interval_us or current[2],
        )
        return (current,) if pending == current else (current, pending)

    def transmission_constraints(self) -> TransmissionConstraints:
        """Combine committed link state with admitted transition restrictions.

        DLE receives a value describing possible PHYs and the shortest interval;
        it does not inspect another procedure's pending fields (Core 5.1.7.4,
        5.1.10.1).
        """
        connection, controls = self.connection, self
        tx_phys = {connection.tx_phy}
        rx_phy = connection.rx_phy
        interval_us = connection.parameters.interval_us
        if controls.connection_parameters.requested_interval_min:
            interval_us = min(
                interval_us,
                controls.connection_parameters.requested_interval_min
                * LL_CONN_INTERVAL_UNIT_US,
            )
        if controls.pending:
            update = controls.pending
            if update.interval_us is not None:
                interval_us = min(interval_us, update.interval_us)
            tx, rx = self._instant_phys(update)
            # PHY packet-time restrictions start at the local control transmission.
            if update.local and not self._instant_transmitted:
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
        return TransmissionConstraints(frozenset(tx_phys), rx_phy, interval_us)

    def reject(self, opcode: int, reason: int, *, priority: bool = False) -> None:
        self.connection.llcp.respond(
            bytes([Opcode.LL_REJECT_EXT_IND, opcode, reason]), priority=priority
        )

    def instant(self) -> int:
        """Follow the recommended six listening-event lead (Core 6.3, Vol 6, Part B,
        5.5).
        """
        return (
            self.connection.event_counter
            + LL_INSTANT_MIN_LISTENING_EVENTS
            * self.subrating.schedule.factor
            * (self.connection.parameters.latency + 1)
        ) & LL_EVENT_COUNTER_MASK

    @property
    def pending(self) -> InstantUpdate | None:
        return self._pending

    def schedule_instant(self, update: InstantUpdate) -> None:
        """Stage an update and initialize its transmission and acknowledgment state."""
        self._pending = update
        self._instant_transmitted = self._instant_acknowledged = False
        self._awaiting_instant_ack_confirmation = False

    def clear_instant(self) -> None:
        """Release a committed or rejected update and its receive hold."""
        self._pending = None
        self._awaiting_instant_ack_confirmation = False

    def next_instant_event(self) -> int | None:
        """Next checkpoint for an Instant or its required radio events."""
        update = self.pending
        if update is None or (update.local and not self._instant_transmitted):
            return None
        distance = counter_distance(update.instant, self.connection.event_counter)
        if distance >= LL_INSTANT_PAST_THRESHOLD:
            return None
        # Vol 6, Part B, 5.5.1: listen at every event until the peer confirms
        # our acknowledgment or the Instant is reached.
        if self._awaiting_instant_ack_confirmation:
            return self.connection.event_index
        return (
            self.connection.event_index
            + distance
            - bool(distance and update.changes_timing)
        )

    @property
    def instant_forces_exchange(self) -> bool:
        return self._awaiting_instant_ack_confirmation or (
            self.pending is not None
            and self.pending.changes_timing
            and self.next_instant_event() == self.connection.event_index
        )

    def confirm_instant_acknowledgment(self) -> None:
        """A valid new Central PDU confirms its previous PDU was acknowledged."""
        self._awaiting_instant_ack_confirmation = False

    def accept_instant(self, update: InstantUpdate) -> None:
        """Peripheral acceptance of an Instant; a past Instant loses the connection
        (Core Vol 6, Part B, 5.5.1).
        """
        distance = counter_distance(update.instant, self.connection.event_counter)
        if distance >= LL_INSTANT_PAST_THRESHOLD:
            self.connection.complete(ErrorCode.INSTANT_PASSED_ERROR)
        elif self.pending is not None:
            self.connection.complete(ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR)
        else:
            self.schedule_instant(update)
            self._awaiting_instant_ack_confirmation = bool(distance)
            if distance == 0 and not self.connection.llcp.check_response_timeout():
                self.apply(received_at_instant=True)

    def request_channel_map(self, channel_map: int) -> None:
        """Central Channel Map Update from the Host classification (Core Vol 6, Part B,
        5.1.2; Vol 4, Part E, 7.8.19).
        """
        self.host_channel_map = channel_map
        self.connection.llcp.schedule_channel_map()

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

    def update_channel_map(self) -> None:
        connection = self.connection
        if connection.role != Role.CENTRAL or connection.closed:
            return
        channel_map = self.selected_channel_map()
        if channel_map == connection.parameters.channel_map:
            return
        update = InstantUpdate(self.instant(), channel_map=channel_map, local=True)
        self.schedule_instant(update)
        connection.llcp.respond(update.encode())

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
        self.connection.llcp.schedule_indication(
            bytes([Opcode.LL_MIN_USED_CHANNELS_IND, phys, minimum])
        )

    def transfer(self, source: TrainSource, service_data: int) -> None:
        """Queue LL_PERIODIC_SYNC_IND for ``source``'s train.

        Wait until the train can be described (2.4.2.27). The procedure occupies
        the ACL from queuing the indication until its first transmission (5.1.13).
        """
        payload = None

        def ready() -> bool:
            nonlocal payload
            if source.ended:
                return True
            if (train := source.reference()) is not None:
                payload = transfer_indication(train, self.connection, service_data)
            return payload is not None

        def start() -> None:
            if source.ended:
                return
            self.active_transfer = source, service_data
            procedure = ProcedureContext(
                payload[0],
                frozenset(),
                None,
                completion=ProcedureCompletion.SENT,
                on_sent=self._finish_transfer,
            )
            self.connection.llcp.send(procedure, payload)

        self.connection.llcp.initiate(DeferredProcedure('past', start, ready=ready))

    def _finish_transfer(self, _payload: bytes) -> None:
        self.active_transfer = None

    def prepare(self, payload: bytes) -> bytes:
        """Finalize a control PDU at its first transmission.

        Describe a released sync transfer relative to the transmitting event,
        and preserve the recommended Instant lead (Core 6.3, Vol 6, Part B,
        2.4.2.27 and 5.5).
        """
        payload = self.frame_space.prepare(payload)
        if payload[0] in PAST_INDICATIONS and self.active_transfer is not None:
            source, service_data = self.active_transfer
            train = source.reference()
            if train is not None:
                payload = (
                    transfer_indication(train, self.connection, service_data) or payload
                )
            return payload
        update = self.pending
        if (
            update is None
            or not update.local
            or self._instant_transmitted
            or payload[0] != update.opcode
        ):
            return payload
        # ARQ may have delayed the first transmission since the Host request.
        instant = self.instant()
        if update.rate is not None:
            update = self.rate.prepare(update, instant)
        else:
            update = replace(update, instant=instant)
            if update.parameters is not None and update.anchor_reference is not None:
                offset = anchor_offset(
                    update.anchor_reference,
                    instant,
                    self.connection.parameters.interval_us // LL_CONN_INTERVAL_UNIT_US,
                    update.parameters.interval,
                )
                update = replace(
                    update, parameters=replace(update.parameters, window_offset=offset)
                )
        self._pending = update
        self._instant_transmitted = True
        return update.encode()

    def acknowledge(self, payload: bytes) -> None:
        update = self.pending
        if (
            update is not None
            and update.local
            and self._instant_transmitted
            and payload[0] == update.opcode
            and payload == update.encode()
        ):
            self._instant_acknowledged = True

    def send_version(self) -> None:
        """Send LL_VERSION_IND at most once per connection (Core Vol 6, Part B,
        5.1.5).
        """
        if not self.version_sent:
            self.version_sent = True
            payload = bytes([Opcode.LL_VERSION_IND]) + struct.pack(
                '<BHH', *self.listener.local_version
            )
            if self.version_request:
                self.connection.llcp.send(self.version_context, payload, priority=True)
            else:
                self.connection.llcp.respond(payload, priority=True)

    def request_version(self) -> None:
        self.version_context = ProcedureContext(
            Opcode.LL_VERSION_IND,
            frozenset((Opcode.LL_VERSION_IND,)),
            self.control,
            host_initiated=True,
        )
        if self.peer_version is not None:
            self.finish_version(ErrorCode.SUCCESS)
            return
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
        if self.data_length.control(payload) or self.features.control(payload):
            return True
        if any(procedure.control(payload) for procedure in self._local_procedures):
            return True
        opcode = payload[0]
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
            self.connection.llcp.schedule_channel_map()
            return True
        if (
            opcode == Opcode.LL_PERIODIC_SYNC_IND
            and len(payload) == PDU_CONTROL_PERIODIC_SYNC_SIZE
            and connection.link.features
            & LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_RECIPIENT
        ) or (
            opcode == Opcode.LL_PERIODIC_SYNC_WR_IND
            and len(payload) == PDU_CONTROL_PERIODIC_SYNC_WR_SIZE
            and connection.link.features
            & LeFeatureMask.PERIODIC_ADVERTISING_WITH_RESPONSES_SCANNER
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
            channels, instant = CHANNEL_MAP.unpack_from(
                payload, PDU_CONTROL_OPCODE_SIZE
            )
            channel_map = int.from_bytes(channels, 'little')
            if connection.role != Role.PERIPHERAL or not valid_channel_map(channel_map):
                connection.complete(ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR)
            else:
                self.accept_instant(InstantUpdate(instant, channel_map=channel_map))
            return True
        if (
            opcode == Opcode.LL_UNKNOWN_RSP
            and len(payload) == PDU_CONTROL_UNKNOWN_RSP_SIZE
        ):
            if (
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
            if self.connection_parameters.parameter_request:
                self.connection_parameters.reject_parameters(payload[1])
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
        if update is None:
            return
        if update.instant != connection.event_counter:
            return
        # Wait until the local update has been sent. Close on an unacknowledged
        # update at the Instant rather than applying a schedule the peer may not know.
        if update.local and not self._instant_transmitted:
            return
        if update.local and not self._instant_acknowledged:
            connection.complete(ErrorCode.INSTANT_PASSED_ERROR)
            return
        # Release the procedure slot before callbacks can start another procedure
        # against the newly committed parameters.
        self.clear_instant()
        if update.central_tx_phy or update.peripheral_tx_phy:
            if self.phy.context is not None:
                self.phy.context.stop_response_timer()
        if update.parameters is not None:
            old = connection.parameters
            notify = (
                update.notify
                if update.notify is not None
                else (
                    self.connection_parameters.parameter_request
                    or self.connection_parameters.requested_interval_min
                    or (old.interval_us, old.latency, old.timeout)
                    != (
                        update.parameters.interval_us,
                        update.parameters.latency,
                        update.parameters.timeout,
                    )
                )
            )
            self.connection_parameters.finish()
            connection.parameters = update.parameters
            if old.interval_us != update.parameters.interval_us:
                self.subrating.reset_schedule()
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
        if update.rate is not None:
            self.rate.apply(update.rate, received_at_instant=received_at_instant)
        if update.channel_map is not None:
            connection.parameters = replace(
                connection.parameters, channel_map=update.channel_map
            )
        if update.central_tx_phy or update.peripheral_tx_phy:
            # On-air fields name roles; translate them into this endpoint's TX/RX.
            tx, rx = self._instant_phys(update)
            connection.tx_phy = Phy(tx or connection.tx_phy)
            connection.rx_phy = Phy(rx or connection.rx_phy)
            self.power.update_transmitter()
            self.phy.finish(ErrorCode.SUCCESS)
            if self.peer_minimum_channel_phys:
                connection.llcp.schedule_channel_map()
        self.data_length.refresh()
