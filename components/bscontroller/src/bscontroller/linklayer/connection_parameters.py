# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connection parameter requests and updates (Core Vol 6, Part B, 5.1.1, 5.1.7).

Local and remote contexts can coexist during a collision. A remote Host decision
owns its timer independently of the rejected local request; Instant commits stay
with the connection-control coordinator.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    LL_CONN_PREFERRED_PERIODICITY_MIN,
    LL_CONN_WINDOW_SIZE_MAX,
    LL_CONN_WINDOW_SIZE_MIN,
    LL_EVENT_COUNTER_MASK,
    PDU_CONTROL_CONN_PARAM_OFFSET_COUNT,
    PDU_CONTROL_CONN_PARAM_OFFSET_UNSPECIFIED,
    PDU_CONTROL_OPCODE_SIZE,
    ControlOpcode,
)
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.instant import CONNECTION_UPDATE as UPDATE
from bscontroller.linklayer.instant import InstantUpdate
from bscontroller.linklayer.parameters import valid_timing
from bscontroller.linklayer.procedure import (
    DIFFERENT_TRANSACTION_COLLISION,
    PROCEDURE_COLLISION,
    ProcedureContext,
    ProcedureOrigin,
    ProcedurePhase,
)
from bscontroller.linklayer.state import Role
from bscontroller.linklayer.subrating import maximum_latency

if TYPE_CHECKING:
    from bscontroller.linklayer.control import ControlProcedures

Opcode = ControlOpcode

PARAM_REQUEST = struct.Struct('<HHHHBH6H')


def preferred_interval(minimum: int, maximum: int, periodicity: int) -> int:
    """Prefer a periodicity multiple only when it exists in the accepted range."""
    if periodicity:
        candidate = (minimum + periodicity - 1) // periodicity * periodicity
        if candidate <= maximum:
            return candidate
    return minimum


def preferred_periodicity(minimum: int, maximum: int, preference: int = 0) -> int:
    """Encode a valid preference for an interval range, or zero for a fixed interval."""
    if minimum == maximum:
        return 0
    return (
        preference if 0 < preference <= maximum else LL_CONN_PREFERRED_PERIODICITY_MIN
    )


def anchor_offset(
    reference: tuple[int, int], instant: int, old_interval: int, interval: int
) -> int:
    """Express a preferred reference anchor relative to the update Instant."""
    counter, offset = reference
    distance = (instant - counter) & LL_EVENT_COUNTER_MASK
    return (offset - distance * old_interval) % interval


@dataclass(eq=False)
class ConnectionParameterContext(ProcedureContext):
    requested_parameters: tuple[int, int, int, int] | None = None
    requested_interval_min: int = 0
    peer_payload: bytes | None = None


class ConnectionParametersProcedure:
    def __init__(self, owner: ControlProcedures):
        self.owner = owner
        self.connection = owner.connection
        self.local_context: ConnectionParameterContext | None = None
        self.remote_context: ConnectionParameterContext | None = None
        self.parameter_request_unsupported = False

    @property
    def remote_parameters(self) -> bytes | None:
        context = self.remote_context
        return (
            context.peer_payload
            if context is not None and context.phase == ProcedurePhase.WAIT_HOST
            else None
        )

    @property
    def parameter_request(self) -> bool:
        context = self.local_context
        return (
            context is not None
            and context.active
            and context.request == Opcode.LL_CONNECTION_PARAM_REQ
        )

    @property
    def request_pending(self) -> bool:
        return self.parameter_request or bool(
            self.remote_context and self.remote_context.active
        )

    @property
    def requested_parameters(self) -> tuple[int, int, int, int] | None:
        context = self.local_context
        return (
            context.requested_parameters
            if context is not None and context.active
            else None
        )

    @property
    def requested_interval_min(self) -> int:
        contexts = (self.local_context, self.remote_context)
        return min(
            (
                context.requested_interval_min
                for context in contexts
                if context is not None
                and context.active
                and context.requested_interval_min
            ),
            default=0,
        )

    def receive_request(self, payload: bytes) -> None:
        self.remote_context = ConnectionParameterContext(
            Opcode.LL_CONNECTION_PARAM_REQ,
            frozenset((Opcode.LL_CONNECTION_UPDATE_IND,)),
            self,
            origin=ProcedureOrigin.REMOTE,
            phase=ProcedurePhase.WAIT_HOST,
            peer_payload=payload,
        )

    def finish(self) -> None:
        for context in (self.local_context, self.remote_context):
            if context is not None:
                self.owner.stop_instant_timer(context)
                context.finish()

    @property
    def supports_parameter_request(self) -> bool:
        features = self.connection.peer_features
        return not self.parameter_request_unsupported and (
            features is None
            or bool(features & LeFeatureMask.CONNECTION_PARAMETERS_REQUEST_PROCEDURE)
        )

    def request_update(
        self,
        interval: int,
        latency: int,
        timeout: int,
        *,
        reference: tuple[int, int] | None = None,
        notify: bool = True,
    ) -> None:
        """Central sends LL_CONNECTION_UPDATE_IND (Core Vol 6, Part B, 5.1.1)."""
        old_interval_us = self.connection.parameters.interval_us
        interval_us = interval * LL_CONN_INTERVAL_UNIT_US
        if interval_us == old_interval_us:
            factor = self.owner.subrating.schedule.factor
            latency = min(latency, maximum_latency(interval_us, factor, timeout))
            if latency < 0:
                reason = ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR
                if self.remote_context is not None and self.remote_context.active:
                    self.reject_remote_parameters(reason)
                else:
                    self.finish()
                if notify:
                    self.owner.listener.on_connection_updated(self.connection, reason)
                return
        instant = self.owner.instant()
        if old_interval_us % LL_CONN_INTERVAL_UNIT_US:
            reference = None
        offset = (
            anchor_offset(
                reference,
                instant,
                old_interval_us // LL_CONN_INTERVAL_UNIT_US,
                interval,
            )
            if reference
            else 0
        )
        parameters = replace(
            self.connection.parameters,
            window_size=LL_CONN_WINDOW_SIZE_MIN,
            window_offset=offset,
            interval=interval,
            latency=latency,
            timeout=timeout,
            rate_interval_us=None,
        )
        context = self.local_context
        if context is None or not context.active:
            if self.remote_context is not None and self.remote_context.active:
                context = self.remote_context
            else:
                context = ConnectionParameterContext(
                    Opcode.LL_CONNECTION_UPDATE_IND,
                    frozenset(),
                    self,
                    host_initiated=notify,
                )
                self.local_context = context
        context.phase = ProcedurePhase.WAIT_INSTANT
        self.owner.pending = InstantUpdate(
            instant,
            parameters=parameters,
            local=True,
            anchor_reference=reference,
            notify=notify,
        )
        self.owner.transmitted = self.owner.acknowledged = False
        self.connection.enqueue_control(self.owner.pending.encode())

    def request_parameters(
        self, minimum: int, maximum: int, latency: int, timeout: int
    ) -> None:
        """Send LL_CONNECTION_PARAM_REQ (Core Vol 6, Part B, 5.1.7.1)."""
        self.local_context = ConnectionParameterContext(
            Opcode.LL_CONNECTION_PARAM_REQ,
            frozenset(
                (Opcode.LL_CONNECTION_PARAM_RSP, Opcode.LL_CONNECTION_UPDATE_IND)
            ),
            self,
            host_initiated=True,
            requested_parameters=(minimum, maximum, latency, timeout),
            requested_interval_min=minimum,
        )
        self.owner.start_instant_timer(self.local_context)
        self.connection.enqueue_control(
            bytes([Opcode.LL_CONNECTION_PARAM_REQ])
            + PARAM_REQUEST.pack(
                minimum,
                maximum,
                latency,
                timeout,
                preferred_periodicity(minimum, maximum),
                self.connection.event_counter,
                *(
                    [PDU_CONTROL_CONN_PARAM_OFFSET_UNSPECIFIED]
                    * PDU_CONTROL_CONN_PARAM_OFFSET_COUNT
                ),
            )
        )

    def reply_parameters(
        self,
        minimum: int,
        maximum: int,
        latency: int,
        timeout: int,
        *,
        notify: bool = True,
    ) -> None:
        """Answer LL_CONNECTION_PARAM_REQ: Central updates, Peripheral responds (Core
        Vol 6, Part B, 5.1.7.2).
        """
        assert self.remote_parameters is not None
        _, _, _, _, periodicity, reference, *offsets = PARAM_REQUEST.unpack(
            self.remote_parameters
        )
        assert self.remote_context is not None
        self.remote_context.phase = ProcedurePhase.WAIT_INSTANT
        self.remote_context.host_initiated = notify
        interval = preferred_interval(minimum, maximum, periodicity)
        if self.connection.role == Role.CENTRAL:
            offset = next((value for value in offsets if value < interval), None)
            self.request_update(
                interval,
                latency,
                timeout,
                reference=(reference, offset) if offset is not None else None,
                notify=notify,
            )
        else:
            self.remote_context.requested_interval_min = minimum if notify else 0
            self.connection.enqueue_control(
                bytes([Opcode.LL_CONNECTION_PARAM_RSP])
                + PARAM_REQUEST.pack(
                    minimum,
                    maximum,
                    latency,
                    timeout,
                    preferred_periodicity(minimum, maximum, periodicity),
                    self.connection.event_counter,
                    *(
                        [PDU_CONTROL_CONN_PARAM_OFFSET_UNSPECIFIED]
                        * PDU_CONTROL_CONN_PARAM_OFFSET_COUNT
                    ),
                )
            )
        assert self.remote_context is not None
        self.owner.start_instant_timer(self.remote_context)

    def reject_parameters(self, reason: int) -> None:
        """Handle a rejected Connection Parameters Request; Unsupported Remote Feature
        falls back to Connection Update (Core Vol 6, Part B, 5.1.1, 5.1.7).
        """
        requested = self.requested_parameters
        if self.local_context is not None:
            self.owner.stop_instant_timer(self.local_context)
            self.local_context.finish()
        if reason == ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR:
            self.parameter_request_unsupported = True
            if self.connection.peer_features is not None:
                self.connection.peer_features &= ~int(
                    LeFeatureMask.CONNECTION_PARAMETERS_REQUEST_PROCEDURE
                )
        if (
            self.connection.role == Role.CENTRAL
            and reason == ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
            and requested is not None
        ):
            minimum, maximum, latency, timeout = requested
            interval = max(minimum, self.owner.frame_space.minimum_interval())
            if interval <= maximum:
                self.request_update(interval, latency, timeout)
                return
            reason = ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR
        self.owner.listener.on_connection_updated(self.connection, reason)
        if self.remote_parameters is not None:
            self.notify_remote_parameters()

    def reject_remote_parameters(self, reason: int) -> None:
        self.owner.reject(Opcode.LL_CONNECTION_PARAM_REQ, reason)
        if self.remote_context is not None:
            self.owner.stop_instant_timer(self.remote_context)
            self.remote_context.finish()

    def notify_remote_parameters(self) -> None:
        assert self.remote_parameters is not None
        minimum, maximum, latency, timeout, *_ = PARAM_REQUEST.unpack(
            self.remote_parameters
        )
        assert self.remote_context is not None
        self.owner.start_instant_timer(self.remote_context)
        parameters = self.connection.parameters
        if (
            minimum == maximum
            and minimum * LL_CONN_INTERVAL_UNIT_US == parameters.interval_us
            and latency == parameters.latency
            and timeout == parameters.timeout
        ):
            self.reply_parameters(minimum, maximum, latency, timeout, notify=False)
            return
        self.owner.listener.on_remote_parameters(
            self.connection, minimum, maximum, latency, timeout
        )

    def control(self, payload: bytes) -> bool:
        connection = self.connection
        opcode = payload[0]
        if (
            opcode == Opcode.LL_CONNECTION_UPDATE_IND
            and len(payload) == PDU_CONTROL_OPCODE_SIZE + UPDATE.size
        ):
            if connection.role != Role.PERIPHERAL:
                self.owner.reject(opcode, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)
                return True
            size, offset, interval, latency, timeout, instant = UPDATE.unpack(
                payload[PDU_CONTROL_OPCODE_SIZE:]
            )
            if (
                not valid_timing(interval, interval, latency, timeout)
                or not connection.fits_cis_supervision_timeout(timeout)
                or not self.owner.frame_space.fits_interval(interval)
                or (
                    interval * LL_CONN_INTERVAL_UNIT_US
                    == connection.parameters.interval_us
                    and latency
                    > maximum_latency(
                        interval * LL_CONN_INTERVAL_UNIT_US,
                        self.owner.subrating.schedule.factor,
                        timeout,
                    )
                )
                or not LL_CONN_WINDOW_SIZE_MIN
                <= size
                <= min(LL_CONN_WINDOW_SIZE_MAX, interval - 1)
                or offset > interval
            ):
                # Close on unusable timing so the two peers cannot adopt different
                # connection-event schedules.
                connection.complete(ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR)
            else:
                self.owner.accept_instant(
                    InstantUpdate(
                        instant,
                        parameters=replace(
                            connection.parameters,
                            window_size=size,
                            window_offset=offset,
                            interval=interval,
                            latency=latency,
                            timeout=timeout,
                            rate_interval_us=None,
                        ),
                    )
                )
            return True

        if (
            opcode in (Opcode.LL_CONNECTION_PARAM_REQ, Opcode.LL_CONNECTION_PARAM_RSP)
            and len(payload) == PDU_CONTROL_OPCODE_SIZE + PARAM_REQUEST.size
        ):
            minimum, maximum, latency, timeout, *_ = PARAM_REQUEST.unpack(
                payload[PDU_CONTROL_OPCODE_SIZE:]
            )
            if not valid_timing(
                minimum, maximum, latency, timeout
            ) or not connection.fits_cis_supervision_timeout(timeout):
                self.owner.reject(opcode, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR)
            elif not self.owner.frame_space.fits_interval(maximum):
                reason = ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR
                if opcode == Opcode.LL_CONNECTION_PARAM_REQ:
                    self.owner.reject(opcode, reason)
                elif self.parameter_request:
                    self.reject_parameters(reason)
            elif opcode == Opcode.LL_CONNECTION_PARAM_REQ:
                if connection.role == Role.CENTRAL and self.owner.rate.instant_pending:
                    self.owner.reject(opcode, DIFFERENT_TRANSACTION_COLLISION)
                elif (
                    connection.role == Role.PERIPHERAL
                    and (self.parameter_request or self.owner.phy.phy_request_local)
                    and not (
                        self.remote_parameters
                        or self.owner.pending
                        or self.owner.phy.remote_phy_request
                        or self.owner.data_length.pending
                    )
                ):
                    # Preserve the Central's request across local rejection. Only a
                    # same-procedure collision delays its Host notification.
                    self.receive_request(payload[PDU_CONTROL_OPCODE_SIZE:])
                    if self.owner.phy.phy_request_local:
                        self.notify_remote_parameters()
                elif self.owner.peer_instant_blocked:
                    self.owner.reject(
                        opcode,
                        (
                            DIFFERENT_TRANSACTION_COLLISION
                            if self.owner.pending
                            and self.owner.pending.channel_map is not None
                            else PROCEDURE_COLLISION
                        ),
                    )
                else:
                    self.receive_request(payload[PDU_CONTROL_OPCODE_SIZE:])
                    self.notify_remote_parameters()
            elif self.parameter_request and connection.role == Role.CENTRAL:
                requested = self.requested_parameters
                # Frame spacing can change while the request is on the air.
                minimum = max(minimum, self.owner.frame_space.minimum_interval())
                if requested is not None:
                    minimum, maximum = (
                        max(minimum, requested[0]),
                        min(maximum, requested[1]),
                    )
                if minimum > maximum:
                    self.reject_parameters(
                        ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR
                    )
                else:
                    self.request_update(minimum, latency, timeout)
            return True

        return False
