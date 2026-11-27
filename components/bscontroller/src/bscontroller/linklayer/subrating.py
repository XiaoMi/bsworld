# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connection Subrating (Core Vol 6, Part B, 4.5.1, 5.1.19, and 5.1.20)."""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_CONN_LATENCY_MAX,
    LL_EVENT_COUNTER_MASK,
    LL_SUPERVISION_TIMEOUT_INTERVAL_FACTOR,
    LL_SUPERVISION_TIMEOUT_MAX,
    LL_SUPERVISION_TIMEOUT_MIN,
    LL_SUPERVISION_TIMEOUT_UNIT_US,
    ControlOpcode,
)
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.procedure import (
    DIFFERENT_TRANSACTION_COLLISION,
    PROCEDURE_COLLISION,
    DeferredProcedure,
    LocalProcedure,
    ProcedureContext,
)
from bscontroller.linklayer.state import Role

if TYPE_CHECKING:
    from bscontroller.linklayer.control import ControlProcedures

Opcode = ControlOpcode
PARAMETERS = struct.Struct('<5H')
MAX_SUBRATE_FACTOR = 500
EVENT_COUNTER_PERIOD = LL_EVENT_COUNTER_MASK + 1
EVENT_COUNTER_QUARTER = EVENT_COUNTER_PERIOD // 4
UNSUPPORTED = ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR


def valid_event_spacing(
    interval_us: int, factor: int, latency: int, timeout: int
) -> bool:
    return (
        LL_SUPERVISION_TIMEOUT_INTERVAL_FACTOR * interval_us * factor * (latency + 1)
        < timeout * LL_SUPERVISION_TIMEOUT_UNIT_US
    )


def maximum_latency(interval_us: int, factor: int, timeout: int) -> int:
    """Largest valid latency at this subrate and supervision timeout."""
    spacing = LL_SUPERVISION_TIMEOUT_INTERVAL_FACTOR * interval_us * factor
    budget = timeout * LL_SUPERVISION_TIMEOUT_UNIT_US - 1
    return min(MAX_SUBRATE_FACTOR // factor, budget // spacing) - 1


@dataclass(frozen=True)
class SubrateRequest:
    minimum: int = 1
    maximum: int = 1
    latency: int = 0
    continuation: int = 0
    timeout: int = LL_SUPERVISION_TIMEOUT_MAX

    @property
    def valid_fields(self) -> bool:
        return (
            1 <= self.minimum <= self.maximum <= MAX_SUBRATE_FACTOR
            and 0 <= self.latency <= LL_CONN_LATENCY_MAX
            and 0 <= self.continuation < MAX_SUBRATE_FACTOR
            and self.maximum * (self.latency + 1) <= MAX_SUBRATE_FACTOR
            and LL_SUPERVISION_TIMEOUT_MIN <= self.timeout <= LL_SUPERVISION_TIMEOUT_MAX
        )

    @property
    def valid_host_parameters(self) -> bool:
        return self.valid_fields and self.continuation < self.maximum

    def fits_connection(self, interval_us: int) -> bool:
        return valid_event_spacing(
            interval_us, self.maximum, self.latency, self.timeout
        )

    def pack(self) -> bytes:
        return PARAMETERS.pack(
            self.minimum, self.maximum, self.latency, self.continuation, self.timeout
        )


@dataclass(frozen=True)
class SubrateSchedule:
    factor: int = 1
    base: int = 0
    continuation: int = 0

    def next_event(self, index: int, activity: int | None = None) -> int:
        # Absolute event indices keep the phase continuous across the wire wrap.
        phase = (index - self.base) % self.factor
        if not phase or (
            activity is not None
            and index - phase <= activity < index
            and index - activity <= self.continuation
        ):
            return index
        return index + self.factor - phase


@dataclass(frozen=True)
class SubrateUpdate:
    schedule: SubrateSchedule
    latency: int
    timeout: int

    def pack(self) -> bytes:
        schedule = self.schedule
        return PARAMETERS.pack(
            schedule.factor,
            schedule.base & LL_EVENT_COUNTER_MASK,
            self.latency,
            schedule.continuation,
            self.timeout,
        )

    @classmethod
    def unpack(cls, payload: bytes, index: int) -> SubrateUpdate:
        factor, base, latency, continuation, timeout = PARAMETERS.unpack(payload)
        counter = index & LL_EVENT_COUNTER_MASK
        # An indication queued in the last quarter may arrive after the wrap.
        if base >= 3 * EVENT_COUNTER_QUARTER and counter < EVENT_COUNTER_QUARTER:
            base -= EVENT_COUNTER_PERIOD
        return cls(
            SubrateSchedule(factor, index - counter + base, continuation),
            latency,
            timeout,
        )

    def valid_for(self, interval_us: int) -> bool:
        request = SubrateRequest(
            self.schedule.factor,
            self.schedule.factor,
            self.latency,
            self.schedule.continuation,
            self.timeout,
        )
        return request.valid_host_parameters and request.fits_connection(interval_us)


@dataclass(eq=False, kw_only=True)
class SubrateContext(ProcedureContext):
    parameters: SubrateRequest
    acknowledged: bool = False

    def accepts(self, update: SubrateUpdate) -> bool:
        request = self.parameters
        schedule = update.schedule
        return (
            request.minimum <= schedule.factor <= request.maximum
            and update.latency <= request.latency
            and schedule.continuation >= min(request.continuation, schedule.factor - 1)
            and update.timeout == request.timeout
        )


@dataclass(frozen=True)
class PendingSubrateReport:
    request: SubrateContext | None


class Subrating(LocalProcedure):
    def __init__(self, owner: ControlProcedures):
        self.owner = owner
        self.connection = owner.connection
        self.limits = self.connection.link.default_subrate
        self.schedule = SubrateSchedule()
        self.activity: int | None = None
        self.context: SubrateContext | None = None
        self.update: SubrateUpdate | None = None
        self.peer_request: SubrateRequest | None = None
        self.report_after_ack: PendingSubrateReport | None = None
        self.peer_rejected = False

    @property
    def peer_supported(self) -> bool:
        features = self.connection.peer_features
        mask = (
            LeFeatureMask.CONNECTION_SUBRATING_HOST_SUPPORT
            if self.connection.role == Role.CENTRAL
            else LeFeatureMask.CONNECTION_SUBRATING
        )
        return not self.peer_rejected and (features is None or bool(features & mask))

    @property
    def transition(self) -> SubrateSchedule | None:
        if (
            self.update is not None
            and self.context is not None
            and self.context.transmitted
        ):
            return self.update.schedule
        return None

    def next_event(self, index: int) -> int:
        if (transition := self.transition) is not None:
            # Retransmit on the union of the two regular schedules until the ACK.
            return min(
                self.schedule.next_event(index),
                transition.next_event(index),
            )
        return self.schedule.next_event(index, self.activity)

    def record_activity(self, length: int) -> None:
        if length:
            self.activity = self.connection.event_index

    def reset_schedule(self) -> None:
        self.schedule = SubrateSchedule()
        self.activity = None

    def _new_context(
        self, opcode: int, parameters: SubrateRequest, *, host: bool
    ) -> None:
        responses = (Opcode.LL_SUBRATE_IND,) if opcode == Opcode.LL_SUBRATE_REQ else ()
        self.context = SubrateContext(
            opcode,
            frozenset(responses),
            self.control,
            host_initiated=host,
            parameters=parameters,
        )

    def _enqueue(self, opcode: int, body: bytes) -> None:
        self.connection.llcp.send(self.context, bytes([opcode]) + body)

    def request(self, request: SubrateRequest) -> None:
        connection = self.connection
        if not request.fits_connection(
            connection.parameters.interval_us
        ) or not connection.fits_cis_supervision_timeout(request.timeout):
            self.report(ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR)
        elif not self.peer_supported:
            self.report(UNSUPPORTED)
        elif connection.role == Role.CENTRAL:
            self.limits = request
            self._start_update(request, host=True)
        else:
            self._new_context(Opcode.LL_SUBRATE_REQ, request, host=True)
            self._enqueue(Opcode.LL_SUBRATE_REQ, request.pack())

    def _start_update(self, request: SubrateRequest, *, host: bool) -> None:
        self._new_context(Opcode.LL_SUBRATE_IND, request, host=host)
        if self.connection.peer_features is None:
            self.owner.features.discover(
                lambda status: self._send_update(request, status)
            )
        else:
            self._send_update(request, ErrorCode.SUCCESS)

    def _send_update(self, request: SubrateRequest, status: int) -> None:
        if not self.pending:
            return
        if status or not self.peer_supported:
            if (
                not self.context.host_initiated
                and status != ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR
            ):
                self.owner.reject(Opcode.LL_SUBRATE_REQ, UNSUPPORTED)
            self._fail(status or UNSUPPORTED)
            return
        self.update = SubrateUpdate(
            SubrateSchedule(
                request.minimum,
                self.connection.event_index,
                min(request.continuation, request.minimum - 1),
            ),
            request.latency,
            request.timeout,
        )
        self._enqueue(Opcode.LL_SUBRATE_IND, self.update.pack())

    def _start_peer_request(self) -> None:
        request, self.peer_request = self.peer_request, None
        limits = self.limits
        interval_us = self.connection.parameters.interval_us
        factor = max(request.minimum, limits.minimum)
        if not self.peer_supported:
            self.owner.reject(Opcode.LL_SUBRATE_REQ, UNSUPPORTED)
        elif not (
            request.valid_fields
            and request.latency <= limits.latency
            and request.timeout <= limits.timeout
            and self.connection.fits_cis_supervision_timeout(request.timeout)
            and factor <= min(request.maximum, limits.maximum)
            and valid_event_spacing(
                interval_us, request.minimum, request.latency, request.timeout
            )
            and valid_event_spacing(interval_us, factor, 0, request.timeout)
        ):
            self.owner.reject(
                Opcode.LL_SUBRATE_REQ,
                ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR,
            )
        else:
            self._start_update(
                SubrateRequest(
                    factor,
                    factor,
                    min(
                        request.latency,
                        maximum_latency(interval_us, factor, request.timeout),
                    ),
                    max(limits.continuation, request.continuation),
                    request.timeout,
                ),
                host=False,
            )

    def _apply(self, update: SubrateUpdate) -> bool:
        old = self.connection.parameters
        changed = (
            self.schedule.factor,
            old.latency,
            self.schedule.continuation,
            old.timeout,
        ) != (
            update.schedule.factor,
            update.latency,
            update.schedule.continuation,
            update.timeout,
        )
        self.schedule = update.schedule
        self.connection.parameters = replace(
            old, latency=update.latency, timeout=update.timeout
        )
        return changed

    def sent(self, payload: bytes) -> None:
        if payload[0] == Opcode.LL_SUBRATE_IND and self.pending:
            self.context.transmitted = True

    def acknowledged(self, payload: bytes) -> None:
        if (
            not self.pending
            or not self.context.transmitted
            or self.context.request != payload[0]
        ):
            return
        if payload[0] == Opcode.LL_SUBRATE_REQ:
            self.context.acknowledged = True
            return
        if self.update is None:
            return
        changed = self._apply(self.update)
        self.update = None
        self.context.finish()
        if changed or self.context.host_initiated:
            self.report(ErrorCode.SUCCESS)

    def sent_packet(self, length: int) -> bool:
        self.record_activity(length)
        report, self.report_after_ack = self.report_after_ack, None
        if report is None:
            return False
        if report.request is not None:
            report.request.finish()
        self.report(ErrorCode.SUCCESS)
        return report.request is not None

    def report(self, status: int) -> None:
        connection = self.connection
        connection.link.listener.on_subrate_change(
            connection,
            status,
            self.schedule.factor,
            connection.parameters.latency,
            self.schedule.continuation,
            connection.parameters.timeout,
        )

    def _fail(self, reason: int) -> None:
        if self.pending:
            self.context.finish()
            if self.context.host_initiated:
                self.report(reason)
        self.update = None

    def close(self, reason: int) -> None:
        self._fail(reason)
        self.peer_request = None
        self.report_after_ack = None

    def _receive_update(self, payload: bytes) -> None:
        connection = self.connection
        update = SubrateUpdate.unpack(payload[1:], connection.event_index)
        if not update.valid_for(
            connection.parameters.interval_us
        ) or not connection.fits_cis_supervision_timeout(update.timeout):
            self.owner.reject(
                Opcode.LL_SUBRATE_IND, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR
            )
            return
        changed = self._apply(update)
        request = self.context
        # A Central may update independently while our own request is in flight.
        # Match at receipt, before the outgoing ACK can also transmit that request.
        if not (
            self.pending
            and request.request == Opcode.LL_SUBRATE_REQ
            and request.acknowledged
            and request.accepts(update)
        ):
            request = None
        if changed or request is not None:
            self.report_after_ack = PendingSubrateReport(request)

    def control(self, payload: bytes) -> bool:
        opcode = payload[0]
        if opcode in (Opcode.LL_SUBRATE_REQ, Opcode.LL_SUBRATE_IND):
            if len(payload) != 1 + PARAMETERS.size or not (
                self.connection.link.features & LeFeatureMask.CONNECTION_SUBRATING
            ):
                return False
            if opcode == Opcode.LL_SUBRATE_REQ:
                if not (
                    self.connection.link.features
                    & LeFeatureMask.CONNECTION_SUBRATING_HOST_SUPPORT
                ):
                    self.owner.reject(
                        opcode, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
                    )
                elif self.owner.rate.instant_pending:
                    self.owner.reject(opcode, DIFFERENT_TRANSACTION_COLLISION)
                elif self.peer_request is not None:
                    self.owner.reject(
                        opcode,
                        PROCEDURE_COLLISION,
                    )
                else:
                    self.peer_request = SubrateRequest(
                        *PARAMETERS.unpack_from(payload, 1)
                    )
                    self.connection.llcp.initiate(
                        DeferredProcedure('peer_subrate', self._start_peer_request)
                    )
            else:
                self._receive_update(payload)
            return True
        if not self.pending or not self.context.transmitted:
            return False
        if payload == bytes([Opcode.LL_UNKNOWN_RSP, self.context.request]):
            self.peer_rejected = True
            self._fail(UNSUPPORTED)
            return True
        if len(payload) == 3 and payload[:2] == bytes(
            [Opcode.LL_REJECT_EXT_IND, self.context.request]
        ):
            self._fail(payload[2])
            return True
        return False
