# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Per-connection LL control routing, pause gates and procedure lifecycle.

The packet executor reports RX and TX acknowledgments here. Individual procedures
own their state transitions; this coordinator routes cross-procedure replies and
aggregates their deadlines (Core Vol 6, Part B, 5.1, 5.2 and 5.3).
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_RESPONSE_TIMEOUT_US,
    PDU_CONTROL_PAYLOAD_SIZE_INITIAL_MAX,
    PDU_CONTROL_TERMINATE_SIZE,
    PDU_CONTROL_UNKNOWN_RSP_SIZE,
    PDU_DATA_AUTHENTICATED_HEADER_MASK,
    PDU_DATA_LLID_MASK,
    ControlOpcode,
    LlId,
)
from bscontroller.linklayer.control import ControlProcedures
from bscontroller.linklayer.data_length import valid_data_length
from bscontroller.linklayer.encryption import KEY_MISSING_REJECTIONS, Encryption, State
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.frame_space import FrameSpaceRequest
from bscontroller.linklayer.packet_control import PacketLimits, ReceiveContext
from bscontroller.linklayer.parameters import valid_timing
from bscontroller.linklayer.procedure import (
    DeferredProcedure,
    ProcedureCompletion,
    ProcedureContext,
    ProcedureOrigin,
)
from bscontroller.linklayer.state import Role
from bscontroller.radio.base import Received

if TYPE_CHECKING:
    from bscontroller.linklayer.cig import CigManager
    from bscontroller.linklayer.connection import Connection
    from bscontroller.linklayer.connection_event import Outbound
    from bscontroller.linklayer.connection_rate import ConnectionRateParameters
    from bscontroller.linklayer.subrating import SubrateRequest
    from bscontroller.linklayer.transfer import TrainSource

SUCCESS = ErrorCode.SUCCESS
DISALLOWED = ErrorCode.COMMAND_DISALLOWED_ERROR
INVALID = ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR


Opcode = ControlOpcode

# Core Vol 6, Part B, 5.1: supported PDUs restricted to one sender role.
LL_CONTROL_PDU_SENDER_ROLES: dict[int, Role] = {
    Opcode.LL_CONNECTION_UPDATE_IND: Role.CENTRAL,
    Opcode.LL_CHANNEL_MAP_IND: Role.CENTRAL,
    Opcode.LL_ENC_REQ: Role.CENTRAL,
    Opcode.LL_FEATURE_REQ: Role.CENTRAL,
    Opcode.LL_PAUSE_ENC_REQ: Role.CENTRAL,
    Opcode.LL_PHY_UPDATE_IND: Role.CENTRAL,
    Opcode.LL_SUBRATE_IND: Role.CENTRAL,
    Opcode.LL_CONNECTION_RATE_IND: Role.CENTRAL,
    Opcode.LL_CIS_REQ: Role.CENTRAL,
    Opcode.LL_CIS_IND: Role.CENTRAL,
    Opcode.LL_ENC_RSP: Role.PERIPHERAL,
    Opcode.LL_START_ENC_REQ: Role.PERIPHERAL,
    Opcode.LL_PERIPHERAL_FEATURE_REQ: Role.PERIPHERAL,
    Opcode.LL_PHY_RSP: Role.PERIPHERAL,
    Opcode.LL_MIN_USED_CHANNELS_IND: Role.PERIPHERAL,
    Opcode.LL_SUBRATE_REQ: Role.PERIPHERAL,
    Opcode.LL_CONNECTION_RATE_REQ: Role.PERIPHERAL,
    Opcode.LL_CIS_RSP: Role.PERIPHERAL,
}


class LlcpCoordinator:
    def __init__(self, connection: Connection, cig: CigManager):
        self.connection = connection
        self.cig = cig
        self._response_deadline_us: int | None = None
        self.controls = ControlProcedures(connection)
        self.encryption = Encryption(connection)
        self._procedures: dict[ProcedureContext, None] = {}
        # Vol 6, Part B, 5 permits only one locally initiated procedure per ACL.
        self._deferred: deque[DeferredProcedure] = deque()

    @property
    def local_procedure_busy(self) -> bool:
        return (
            self.controls.pending is not None
            or self.encryption.busy
            or any(
                procedure.active
                and (
                    procedure.origin == ProcedureOrigin.LOCAL or procedure.blocks_local
                )
                for procedure in self._procedures
            )
        )

    def queue_for_event(self, procedure: DeferredProcedure) -> None:
        """Wait for ACL event preparation before trying to start a procedure."""
        self._deferred.append(procedure)

    def initiate(self, procedure: DeferredProcedure) -> None:
        """Start a local procedure if its slot is free, otherwise keep it queued."""
        self.queue_for_event(procedure)
        if len(self._deferred) == 1:
            self.start_deferred()

    def has_pending(self, kind: str) -> bool:
        return any(procedure.kind == kind for procedure in self._deferred)

    def register(self, procedure: ProcedureContext) -> None:
        """Track a procedure as soon as it takes a slot, even before sending."""
        self._procedures[procedure] = None

    def start_response_timer(self, procedure: ProcedureContext) -> None:
        """Register a procedure waiting on the peer or Host without an outgoing PDU."""
        self.register(procedure)
        procedure.timed = True
        self._restart_response_timer()

    def send(
        self,
        procedure: ProcedureContext,
        payload: bytes,
        *,
        timed: bool = True,
        priority: bool = False,
    ) -> Outbound:
        """Continue an admitted procedure and attach it to the ACL transmission."""
        self.register(procedure)
        if timed:
            procedure.timed = True
        return self._enqueue(payload, priority=priority, procedure=procedure)

    def respond(self, payload: bytes, *, priority: bool = False) -> Outbound:
        """Queue a peer response or the unconditional ACL Termination PDU."""
        return self._enqueue(payload, priority=priority)

    def cancel_queued(self, packet: Outbound | None) -> bool:
        return self.connection.event.cancel_queued_control(packet)

    def _enqueue(
        self,
        payload: bytes,
        *,
        priority: bool,
        procedure: ProcedureContext | None = None,
    ) -> Outbound:
        # New control PDUs reset T_PRT (6.B.5.2). ARQ retries reuse the same PDU.
        self._restart_response_timer()
        return self.connection.event._enqueue_control(
            payload, priority=priority, procedure=procedure
        )

    def _restart_response_timer(self) -> None:
        self._response_deadline_us = (
            self.connection.radio.now_us + LL_RESPONSE_TIMEOUT_US
        )

    def schedule_channel_map(self) -> None:
        """Use the latest classification when this ACL's turn arrives (6.B.5.1.2)."""
        if not self.has_pending('channel_map'):
            self.initiate(
                DeferredProcedure('channel_map', self.controls.update_channel_map)
            )

    def schedule_indication(self, payload: bytes) -> None:
        opcode = Opcode(payload[0])
        assert opcode in (Opcode.LL_MIN_USED_CHANNELS_IND, Opcode.LL_POWER_CHANGE_IND)

        def start() -> None:
            if self.connection.termination_deadline_us is not None:
                return
            # Power Change Indication completes on first transmission (6.B.5.1.18).
            # Minimum Number Of Used Channels completes on acknowledgment (6.B.5.1.11).
            completion = (
                ProcedureCompletion.SENT
                if opcode == Opcode.LL_POWER_CHANGE_IND
                else ProcedureCompletion.ACKNOWLEDGED
            )
            self.send(
                ProcedureContext(opcode, frozenset(), None, completion=completion),
                payload,
            )

        self.initiate(DeferredProcedure(opcode.name, start))

    def _request(self, procedure: DeferredProcedure, *, active: bool = False) -> int:
        if active or self.has_pending(procedure.kind):
            return DISALLOWED
        self.initiate(procedure)
        return SUCCESS

    @property
    def all_features_pending(self) -> bool:
        return self.controls.features.all_pending or self.has_pending('all_features')

    def start_deferred(self) -> None:
        """Start the next queued local procedure once the link is idle."""
        if not self._deferred or self.local_procedure_busy or self.connection.closed:
            return
        for procedure in self._deferred:
            if procedure.ready is None or procedure.ready():
                self._deferred.remove(procedure)
                procedure.start()
                return

    @property
    def response_deadline_us(self) -> int | None:
        if any(procedure.timed and procedure.active for procedure in self._procedures):
            return self._response_deadline_us
        return None

    def deadline_for(self, procedure: ProcedureContext | None) -> int | None:
        if procedure is not None and procedure.timed and procedure.active:
            return self._response_deadline_us
        return None

    @property
    def next_deadline_us(self) -> int | None:
        deadlines = (
            self.encryption.next_deadline_us,
            self.response_deadline_us,
        )
        return min(
            (deadline for deadline in deadlines if deadline is not None), default=None
        )

    def packet_limits(self) -> PacketLimits:
        """Publish packet limits without exposing the negotiation machinery."""
        lengths = self.controls.data_length
        effective = lengths.effective()
        rx_payload = max(
            PDU_CONTROL_PAYLOAD_SIZE_INITIAL_MAX,
            lengths.packet_limit(
                self.connection.rx_phy,
                effective.rx_time,
                effective.rx_octets,
                encrypted=self.encryption.security.rx_enabled,
            ),
        )
        return PacketLimits(
            lengths.local.rx_octets,
            lengths.local.rx_time,
            lengths.tx_payload_limit(),
            rx_payload,
        )

    def transmit_power_dbm(self, control_payload: bytes | None) -> int:
        return self.controls.power.transmit_power_dbm(control_payload)

    def measure_received_power(self, packet: Received) -> None:
        self.controls.power.received(packet)

    def can_send(self, llid: int, payload: bytes) -> bool:
        """Apply procedure transmit exclusions (Core Vol 6, Part B, 5.1.3 and 5.3)."""
        if (
            llid == LlId.CONTROL
            and payload
            and payload[0] == Opcode.LL_CONNECTION_RATE_REQ
            and not self.controls.rate.can_send_request
        ):
            return False
        if not self.encryption.busy:
            return True
        return (
            llid == LlId.CONTROL
            and bool(payload)
            and payload[0]
            in (
                Opcode.LL_ENC_REQ,
                Opcode.LL_ENC_RSP,
                Opcode.LL_START_ENC_REQ,
                Opcode.LL_START_ENC_RSP,
                Opcode.LL_PAUSE_ENC_REQ,
                Opcode.LL_PAUSE_ENC_RSP,
                Opcode.LL_TERMINATE_IND,
                Opcode.LL_REJECT_IND,
                Opcode.LL_REJECT_EXT_IND,
            )
        )

    def prepare(self, payload: bytes) -> bytes:
        return self.controls.prepare(payload)

    def tx_spacing_us(self, opcode: int | None) -> int:
        return self.controls.frame_space.tx_space_us(opcode)

    def rx_spacing_window_us(self, opcode: int | None = None) -> tuple[int, int]:
        return self.controls.frame_space.rx_window_us(opcode)

    def response_received(self, spacing_us: int) -> None:
        self.controls.frame_space.received_after_us(spacing_us)

    def sent(
        self,
        payload: bytes | None,
        *,
        length: int,
        first: bool,
        procedure: ProcedureContext | None = None,
    ) -> None:
        if payload is not None:
            if first:
                if procedure is not None and procedure.active:
                    procedure.transmitted = True
                    if procedure.on_sent is not None:
                        procedure.on_sent(payload)
                    if procedure.completion == ProcedureCompletion.SENT:
                        procedure.finish()
            self.encryption.sent(payload)
        self.controls.subrating.sent_packet(length)

    def acknowledged(
        self, payload: bytes, procedure: ProcedureContext | None = None
    ) -> int | None:
        if procedure is not None and procedure.active:
            if payload[0] == procedure.request:
                procedure.acknowledged = True
            if procedure.on_acknowledged is not None:
                procedure.on_acknowledged(payload)
            if procedure.completion == ProcedureCompletion.ACKNOWLEDGED:
                procedure.finish()
        self.controls.acknowledge(payload)
        self.encryption.acknowledged(payload)
        if payload[0] == Opcode.LL_TERMINATE_IND:
            return self.connection.termination_status
        return None

    def packet_received(self, length: int, *, duplicate: bool) -> None:
        self.controls.subrating.record_activity(length)
        if not duplicate:
            self.controls.confirm_instant_acknowledgment()

    def receive(self, context: ReceiveContext) -> bytes | None:
        """Authenticate, check cross-procedure admissibility, then permit ACK
        commitment.
        """
        header, payload, duplicate = context.header, context.payload, context.duplicate
        if not payload:
            return payload
        authenticated_header = header & PDU_DATA_AUTHENTICATED_HEADER_MASK
        wire, authenticated = payload, self.encryption.security.rx_enabled
        payload = self.encryption.decrypt(header, payload, duplicate=duplicate)
        if payload is None:
            return None
        rejection_ack = (
            self.encryption.state == State.REJECT
            and not self.encryption.refresh
            and context.acknowledged_control in KEY_MISSING_REJECTIONS
        )
        # Vol 6, Part B, 5.1.3.1 permits a delayed UNKNOWN_RSP after ENC_RSP for a
        # previously sent Peripheral control PDU. Authenticate first, then let the
        # control handler finish that procedure without releasing the encryption pause.
        delayed_unknown_response = self.permits_delayed_unknown(header, payload)
        # The Central can resume data in the same PDU that acknowledges our rejection.
        # Connection.accept commits the acknowledgment only after validating the whole
        # PDU.
        if (
            not duplicate
            and not rejection_ack
            and not delayed_unknown_response
            and self.encryption.state
            in (
                State.KEY,
                State.START_REQ,
                State.START_RSP,
                State.PAUSE_ACK,
                State.RESTART,
                State.REJECT,
            )
        ):
            expected = {
                State.KEY: Opcode.LL_TERMINATE_IND,
                State.START_REQ: Opcode.LL_START_ENC_REQ,
                State.START_RSP: Opcode.LL_START_ENC_RSP,
                State.PAUSE_ACK: Opcode.LL_PAUSE_ENC_RSP,
                State.RESTART: Opcode.LL_ENC_REQ,
                State.REJECT: Opcode.LL_TERMINATE_IND,
            }[self.encryption.state]
            if header & PDU_DATA_LLID_MASK != LlId.CONTROL or payload[0] not in (
                expected,
                Opcode.LL_TERMINATE_IND,
                Opcode.LL_REJECT_IND,
                Opcode.LL_REJECT_EXT_IND,
            ):
                self.connection.complete(
                    ErrorCode.CONNECTION_TERMINATED_DUE_TO_MIC_FAILURE_ERROR
                )
                return None
        self.encryption.security.last_received = (
            authenticated_header,
            wire,
            payload,
            authenticated,
        )
        return payload

    def permits_delayed_unknown(self, header: int, payload: bytes) -> bool:
        """A prior Peripheral procedure may answer after ENC_RSP (Vol 6, Part B,
        5.1.3.1).
        """
        return (
            self.connection.role == Role.PERIPHERAL
            and self.encryption.enc_rsp_sent
            and self.encryption.state in (State.KEY, State.START_RSP, State.REJECT)
            and header & PDU_DATA_LLID_MASK == LlId.CONTROL
            and len(payload) == PDU_CONTROL_UNKNOWN_RSP_SIZE
            and payload[0] == Opcode.LL_UNKNOWN_RSP
            and self._reply_procedure(payload) is not None
        )

    def _reply_procedure(self, payload: bytes) -> ProcedureContext | None:
        opcode = payload[0]
        request = (
            payload[1]
            if (opcode == Opcode.LL_UNKNOWN_RSP and len(payload) == 2)
            or (opcode == Opcode.LL_REJECT_EXT_IND and len(payload) == 3)
            else None
        )
        for procedure in self._procedures:
            if (
                procedure.active
                and procedure.transmitted
                and procedure.origin == ProcedureOrigin.LOCAL
                and procedure.receive is not None
                and (
                    procedure.request == request
                    if request is not None
                    else opcode in procedure.responses
                )
            ):
                return procedure
        return None

    def event_prepare(self) -> bool:
        """Apply due control changes and tell the executor whether to use the radio."""
        force_exchange = self.controls.instant_forces_exchange
        self.controls.apply()
        if self.connection.closed:
            return False
        self.start_deferred()
        self.controls.power.update_transmitter()
        self.controls.power.request_if_needed()
        for procedure in tuple(self._procedures):
            if not procedure.active:
                del self._procedures[procedure]
        index = self.connection.event_index
        return force_exchange or self.controls.subrating.next_event(index) == index

    def close(self, reason: int) -> None:
        self.cig.close_acl(self.connection, reason)
        while self._deferred:
            procedure = self._deferred.popleft()
            if procedure.cancel is not None:
                procedure.cancel(reason)
        self.controls.subrating.close(reason)
        self.controls.rate.close(reason)
        self.controls.frame_space.close(reason)
        self.controls.features.finish(reason)
        self.controls.finish_version(None)
        self.controls.power.close(reason)
        self.controls.clock_accuracy.close(reason)
        self._procedures.clear()

    def check_response_timeout(self) -> bool:
        deadline = self.response_deadline_us
        if deadline is None or self.connection.radio.now_us < deadline:
            return False
        reason = ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR
        if self.controls.version_deadline_us is not None:
            self.controls.finish_version(reason)
        self.connection.complete(reason)
        return True

    def check_timeout(self) -> bool:
        if self.check_response_timeout():
            return True
        self.encryption.check_authenticated_timeout()
        return False

    def control(self, payload: bytes) -> None:
        """Route control PDUs after packet validation; termination remains always
        available.
        """
        connection = self.connection
        if not payload:
            return
        opcode = payload[0]
        if LL_CONTROL_PDU_SENDER_ROLES.get(opcode) == connection.role:
            self.respond(bytes([Opcode.LL_UNKNOWN_RSP, opcode]), priority=True)
            return
        # Replies belong to the outstanding exchange, including one paused by
        # encryption. Do not make the encryption state machine identify PHY/DLE replies.
        exchange = self._reply_procedure(payload)
        if exchange is not None and exchange.receive(payload):
            return
        if self.encryption.control(payload):
            return
        if self.cig.control(connection, payload):
            return
        if self.controls.control(payload):
            return
        if (
            opcode == Opcode.LL_TERMINATE_IND
            and len(payload) == PDU_CONTROL_TERMINATE_SIZE
        ):
            connection.remote_termination = payload[1]
        elif opcode != Opcode.LL_UNKNOWN_RSP:
            # Unsupported, reserved or wrong-length PDUs that no procedure continued
            # are answered with LL_UNKNOWN_RSP (Vol 6, Part B, 2.4.2). Never answer an
            # LL_UNKNOWN_RSP itself: two Link Layers would echo each other forever.
            self.respond(bytes([Opcode.LL_UNKNOWN_RSP, opcode]), priority=True)

    def request_subrate(self, request: SubrateRequest) -> int:
        if self.controls.rate.in_progress or self.has_pending('connection_rate'):
            return DISALLOWED
        if not self.connection.fits_cis_supervision_timeout(request.timeout):
            return INVALID
        subrating = self.controls.subrating
        if not subrating.peer_supported:
            return ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
        return self._request(
            DeferredProcedure(
                'subrate', lambda: subrating.request(request), subrating.report
            ),
            active=subrating.pending,
        )

    def request_connection_rate(self, parameters: ConnectionRateParameters) -> int:
        rate = self.controls.rate
        if status := rate.validate(parameters):
            return status
        self.initiate(
            DeferredProcedure(
                'connection_rate', lambda: rate.request(parameters), rate.report
            )
        )
        return SUCCESS

    def request_frame_space(self, request: FrameSpaceRequest) -> int:
        frame_space = self.controls.frame_space
        if status := frame_space.validate(request):
            return status
        return self._request(
            DeferredProcedure(
                'frame_space',
                lambda: frame_space.request(request),
                frame_space.cancel,
            ),
            active=frame_space.pending,
        )

    def enable_encryption(self, rand: bytes, ediv: int, key: bytes) -> int:
        connection = self.connection
        if (
            connection.role != Role.CENTRAL
            or self.encryption.busy
            or self.cig.has_created_cis(connection)
        ):
            return DISALLOWED
        return self._request(
            DeferredProcedure(
                'encryption', lambda: self.encryption.start(rand, ediv, key)
            ),
        )

    def read_remote_features(self, pages: int | None = None) -> int:
        features = self.controls.features
        kind = 'features' if pages is None else 'all_features'
        if (
            features.pending
            or self.has_pending('features')
            or self.has_pending('all_features')
        ):
            return DISALLOWED
        if features.can_report_cached(pages):
            features.request(pages)
        else:
            self.initiate(
                DeferredProcedure(
                    kind,
                    lambda: features.request(pages),
                    lambda status: features.report(status, pages),
                )
            )
        return SUCCESS

    def read_remote_version(self) -> int:
        if self.controls.version_request or self.has_pending('version'):
            return DISALLOWED
        if self.controls.peer_version is not None:
            self.controls.request_version()
            return SUCCESS
        self.initiate(DeferredProcedure('version', self.controls.request_version))
        return SUCCESS

    def request_phy(self, tx: int, rx: int, coding: int) -> int:
        def request():
            self.connection.tx_coding = coding
            self.controls.phy.request_phy(tx, rx)

        return self._request(
            DeferredProcedure('phy', request), active=self.controls.phy.phy_request
        )

    @property
    def timing_update_pending(self) -> bool:
        update = self.controls.pending
        return (
            self.controls.connection_parameters.request_pending
            or (update is not None and update.parameters is not None)
            or self.controls.rate.in_progress
            or self.has_pending('parameters')
            or self.has_pending('connection_rate')
            or self.has_pending('peer_rate')
        )

    def request_connection_update(
        self,
        minimum: int,
        maximum: int,
        latency: int,
        timeout: int,
    ) -> int:
        def valid_request() -> bool:
            return (
                self.connection.fits_cis_supervision_timeout(timeout)
                and self.controls.frame_space.fits_interval(maximum)
                and valid_timing(
                    minimum,
                    maximum,
                    latency,
                    timeout,
                    subrate=self.controls.subrating.schedule.factor,
                )
            )

        if not valid_request():
            return INVALID
        if (
            self.connection.role == Role.PERIPHERAL
            and not self.controls.connection_parameters.supports_parameter_request
        ):
            return ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR

        def complete(status: int) -> None:
            self.controls.listener.on_connection_updated(self.connection, status)

        def request():
            if not valid_request():
                complete(INVALID)
                return
            allowed_minimum = max(minimum, self.controls.frame_space.minimum_interval())
            # Only the Central can fall back to UPDATE_IND (Vol 6, Part B, 5.1.1).
            if (
                self.connection.role == Role.CENTRAL
                and not self.controls.connection_parameters.supports_parameter_request
            ):
                self.controls.connection_parameters.request_update(
                    allowed_minimum, latency, timeout
                )
            else:
                self.controls.connection_parameters.request_parameters(
                    allowed_minimum, maximum, latency, timeout
                )

        return self._request(
            DeferredProcedure('parameters', request, complete),
            active=self.controls.connection_parameters.parameter_request,
        )

    def supply_key(self, key: bytes | None) -> int:
        if (
            self.connection.role != Role.PERIPHERAL
            or self.encryption.state != State.KEY
        ):
            return DISALLOWED
        self.encryption.supply_key(key)
        return SUCCESS

    def reply_parameters(
        self, minimum: int, maximum: int, latency: int, timeout: int
    ) -> int:
        if self.controls.connection_parameters.remote_parameters is None:
            return DISALLOWED
        frame_space = self.controls.frame_space
        if (
            not self.connection.fits_cis_supervision_timeout(timeout)
            or not frame_space.fits_interval(maximum)
            or not valid_timing(
                minimum,
                maximum,
                latency,
                timeout,
                subrate=self.controls.subrating.schedule.factor,
            )
        ):
            return INVALID
        self.controls.connection_parameters.reply_parameters(
            max(minimum, frame_space.minimum_interval()), maximum, latency, timeout
        )
        return SUCCESS

    def reject_parameters(self, reason: int) -> int:
        if self.controls.connection_parameters.remote_parameters is None:
            return DISALLOWED
        self.controls.connection_parameters.reject_remote_parameters(reason)
        return SUCCESS

    def set_data_length(self, octets: int, time_us: int) -> int:
        if not valid_data_length(octets, time_us):
            return INVALID
        if self.controls.data_length.pending or self.has_pending('data_length'):
            return DISALLOWED
        if not self._peer_supports_data_length:
            return ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR

        def start() -> None:
            if self._peer_supports_data_length:
                self.controls.data_length.request(octets, time_us)

        self.initiate(DeferredProcedure('data_length', start))
        return SUCCESS

    @property
    def _peer_supports_data_length(self) -> bool:
        features = self.connection.peer_features
        return features is None or bool(
            features & LeFeatureMask.LE_DATA_PACKET_LENGTH_EXTENSION
        )

    def transfer(self, source: TrainSource, service_data: int) -> int:
        """Start the Periodic Advertising Sync Transfer procedure (Core Vol 6, Part
        B, 5.1.13; Vol 4, Part E, 7.8.89 and 7.8.90).

        Require the Recipient feature from the peer. Unknown peer features are
        not an indication of support, and the 34-octet CtrData may not be sent
        before a Feature Exchange anyway (Core Vol 6, Part B, 4.5.11).
        """
        if not (
            (self.connection.peer_features or 0)
            & LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_RECIPIENT
        ):
            return ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
        self.controls.transfer(source, service_data)
        return SUCCESS
