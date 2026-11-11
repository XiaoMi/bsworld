# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Per-connection LL control routing, pause gates and procedure lifecycle.

The packet executor reports RX and TX acknowledgments here. Individual procedures
own their state transitions; this coordinator routes cross-procedure replies and
aggregates their deadlines (Core Vol 6, Part B, 5.1, 5.2 and 5.3).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
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
from bscontroller.linklayer.data_path import PacketLimits, ReceiveContext
from bscontroller.linklayer.encryption import KEY_MISSING_REJECTIONS, Encryption, State
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.parameters import valid_timing
from bscontroller.linklayer.procedure import (
    ProcedureContext,
    ProcedureOrigin,
    ProcedureResponseTimer,
)
from bscontroller.linklayer.state import Role

if TYPE_CHECKING:
    from bscontroller.linklayer.connection import Connection
    from bscontroller.linklayer.transfer import TrainSource

SUCCESS = ErrorCode.SUCCESS
DISALLOWED = ErrorCode.COMMAND_DISALLOWED_ERROR
INVALID = ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR


@dataclass
class PreparedProcedure:
    """An admitted operation started after HCI has published Command Status.

    Some procedures complete immediately from cached peer information. Keeping
    admission separate from execution preserves Command Status before completion;
    start consumes the action so a caller cannot accidentally initiate it twice.
    """

    status: int
    action: Callable[[], None] | None = None

    def start(self) -> None:
        action, self.action = self.action, None
        if action is not None:
            action()


Opcode = ControlOpcode

# Core Vol 6, Part B, 5.1: supported PDUs restricted to one sender role.
LL_CONTROL_PDU_SENDER_ROLES: dict[int, Role] = {
    Opcode.LL_CONNECTION_UPDATE_IND: Role.CENTRAL,
    Opcode.LL_CHANNEL_MAP_IND: Role.CENTRAL,
    Opcode.LL_ENC_REQ: Role.CENTRAL,
    Opcode.LL_FEATURE_REQ: Role.CENTRAL,
    Opcode.LL_PAUSE_ENC_REQ: Role.CENTRAL,
    Opcode.LL_PHY_UPDATE_IND: Role.CENTRAL,
    Opcode.LL_ENC_RSP: Role.PERIPHERAL,
    Opcode.LL_START_ENC_REQ: Role.PERIPHERAL,
    Opcode.LL_PERIPHERAL_FEATURE_REQ: Role.PERIPHERAL,
    Opcode.LL_PHY_RSP: Role.PERIPHERAL,
    Opcode.LL_MIN_USED_CHANNELS_IND: Role.PERIPHERAL,
}


class LlcpCoordinator:
    def __init__(self, connection: Connection):
        self.connection = connection
        self.response_timer = ProcedureResponseTimer()
        self.controls = ControlProcedures(connection, self.response_timer)
        self.encryption = Encryption(connection)
        self.local_exchanges: dict[int, ProcedureContext] = {}
        # Host-requested procedures admitted while another procedure runs.
        # Vol 6, Part B, 5 allows one local procedure at a time; the HCI
        # commands (Vol 4, Part E, 7.8.18, 7.8.33, 7.8.49) define no Command
        # Disallowed for that, so they queue instead of failing.
        self.deferred: deque[tuple[str, Callable[[], None]]] = deque()

    def _admit(self, kind: str, start: Callable[[], None]) -> int:
        """Start now, queue behind another procedure, or refuse a duplicate."""
        if self._duplicate(kind):
            return DISALLOWED
        if self.controls.busy:
            self.deferred.append((kind, start))
        else:
            start()
        return SUCCESS

    def _duplicate(self, kind: str) -> bool:
        """The Host already has this procedure running or queued."""
        return self._local_pending(kind) or any(
            item == kind for item, _ in self.deferred
        )

    def _local_pending(self, kind: str) -> bool:
        controls = self.controls
        if kind == 'phy':
            return bool(controls.phy.phy_request)
        if kind == 'parameters':
            return bool(controls.connection_parameters.parameter_request)
        return bool(controls.data_length.pending)

    def start_deferred(self) -> None:
        """Start the next queued Host procedure once the link is idle."""
        if self.deferred and not self.controls.busy and not self.connection.closed:
            _, start = self.deferred.popleft()
            start()

    @property
    def next_deadline_us(self) -> int:
        deadlines = [
            value
            for value in (
                self.controls.next_deadline_us,
                self.encryption.next_deadline_us,
            )
            if value
        ]
        return min(deadlines, default=0)

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

    def can_send(self, llid: int, payload: bytes) -> bool:
        """PDUs allowed while an encryption procedure is in progress (Core Vol 6, Part
        B, 5.1.3.1, 5.1.3.2).
        """
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

    def sent(self, payload: bytes, *, first: bool) -> None:
        if first:
            contexts = (*self.controls.local_contexts(), self.encryption.ping_context)
            for context in contexts:
                if (
                    context is not None
                    and context.active
                    and context.origin == ProcedureOrigin.LOCAL
                    and context.responses
                    and context.request == payload[0]
                ):
                    context.transmitted = True
                    self.local_exchanges[context.request] = context
                    break
        if first:
            self.controls.sent(payload)
        self.encryption.sent(payload)
        self._prune_exchanges()

    def acknowledged(self, payload: bytes) -> int | None:
        self.controls.acknowledge(payload)
        self.encryption.acknowledged(payload)
        if payload[0] == Opcode.LL_TERMINATE_IND:
            return self.connection.termination_status
        return None

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
            and payload[1] in self.local_exchanges
        )

    def _prune_exchanges(self) -> None:
        self.local_exchanges = {
            opcode: context
            for opcode, context in self.local_exchanges.items()
            if context.active
        }

    def event_prepare(self) -> None:
        self.controls.apply()
        self.controls.release_transfers()
        self.start_deferred()
        self._prune_exchanges()

    def close(self, reason: int) -> None:
        self.deferred.clear()
        self.controls.finish_features(reason)
        self.controls.finish_version(None)
        self.local_exchanges.clear()
        self.response_timer.stop()

    def check_timeout(self) -> bool:
        return self.controls.check_timeout() or self.encryption.check_timeout()

    def control(self, payload: bytes) -> None:
        """Route control PDUs after packet validation; termination remains always
        available.
        """
        connection = self.connection
        if not payload:
            return
        opcode = payload[0]
        if LL_CONTROL_PDU_SENDER_ROLES.get(opcode) == connection.role:
            connection.enqueue_control(
                bytes([Opcode.LL_UNKNOWN_RSP, opcode]), priority=True
            )
            return
        # Replies belong to the outstanding exchange, including one paused by
        # encryption. Do not make the encryption state machine identify PHY/DLE replies.
        exchange = None
        if opcode == Opcode.LL_UNKNOWN_RSP and len(payload) == 2:
            exchange = self.local_exchanges.get(payload[1])
        elif opcode == Opcode.LL_REJECT_EXT_IND and len(payload) == 3:
            exchange = self.local_exchanges.get(payload[1])
        else:
            exchange = next(
                (
                    entry
                    for entry in self.local_exchanges.values()
                    if opcode in entry.responses
                ),
                None,
            )
        if exchange is not None and exchange.owner.control(payload):
            self.local_exchanges.pop(exchange.request, None)
            return
        if self.encryption.control(payload):
            return
        if self.controls.control(payload):
            self._prune_exchanges()
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
            connection.enqueue_control(
                bytes([Opcode.LL_UNKNOWN_RSP, opcode]), priority=True
            )

    def prepare_encryption(
        self, rand: bytes, ediv: int, key: bytes
    ) -> PreparedProcedure:
        if self.connection.role != Role.CENTRAL or self.encryption.busy:
            return PreparedProcedure(DISALLOWED)
        return PreparedProcedure(
            SUCCESS, lambda: self.encryption.start(rand, ediv, key)
        )

    def prepare_features(self) -> PreparedProcedure:
        if self.controls.feature_request:
            return PreparedProcedure(DISALLOWED)
        return PreparedProcedure(SUCCESS, self.controls.request_features)

    def prepare_version(self) -> PreparedProcedure:
        if self.controls.version_request:
            return PreparedProcedure(DISALLOWED)
        return PreparedProcedure(SUCCESS, self.controls.request_version)

    def prepare_phy(
        self, tx: int, rx: int, coding: int, parameter_status: int
    ) -> PreparedProcedure:
        if parameter_status:
            return PreparedProcedure(parameter_status)

        def request():
            self.connection.tx_coding = coding
            self.controls.phy.request_phy(tx, rx)

        def start():
            self._admit('phy', request)

        if self._duplicate('phy'):
            return PreparedProcedure(DISALLOWED)
        return PreparedProcedure(SUCCESS, start)

    def prepare_parameters(
        self,
        minimum: int,
        maximum: int,
        latency: int,
        timeout: int,
        *,
        valid_ce_lengths: bool,
    ) -> PreparedProcedure:
        if self._duplicate('parameters'):
            return PreparedProcedure(DISALLOWED)
        if not valid_timing(minimum, maximum, latency, timeout) or not valid_ce_lengths:
            return PreparedProcedure(INVALID)
        if (
            self.connection.role == Role.PERIPHERAL
            and not self.controls.connection_parameters.supports_parameter_request
        ):
            return PreparedProcedure(ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)

        def request():
            # Only the Central can fall back to UPDATE_IND (Vol 6, Part B, 5.1.1).
            if (
                self.connection.role == Role.CENTRAL
                and not self.controls.connection_parameters.supports_parameter_request
            ):
                self.controls.connection_parameters.request_update(
                    minimum, latency, timeout
                )
            else:
                self.controls.connection_parameters.request_parameters(
                    minimum, maximum, latency, timeout
                )

        return PreparedProcedure(SUCCESS, lambda: self._admit('parameters', request))

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
        if not valid_timing(minimum, maximum, latency, timeout):
            return INVALID
        self.controls.connection_parameters.reply_parameters(
            minimum, maximum, latency, timeout
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
        if self._duplicate('data_length'):
            return DISALLOWED
        if (
            self.connection.peer_features is not None
            and not self.connection.peer_features
            & LeFeatureMask.LE_DATA_PACKET_LENGTH_EXTENSION
        ):
            return ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
        return self._admit(
            'data_length', lambda: self.controls.data_length.request(octets, time_us)
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
