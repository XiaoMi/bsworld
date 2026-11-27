# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connection Rate Request and Update (Core Vol 6, Part B, 5.1.32–33)."""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_CONN_LATENCY_MAX,
    LL_EVENT_COUNTER_MASK,
    LL_SUPERVISION_TIMEOUT_MAX,
    LL_SUPERVISION_TIMEOUT_MIN,
    ControlOpcode,
)
from bscontroller.linklayer.connection_parameters import preferred_interval
from bscontroller.linklayer.features import LeFeature, LeFeatureMask
from bscontroller.linklayer.instant import InstantUpdate
from bscontroller.linklayer.procedure import (
    DIFFERENT_TRANSACTION_COLLISION,
    LocalProcedure,
    ProcedureContext,
    ProcedurePhase,
)
from bscontroller.linklayer.state import Role
from bscontroller.linklayer.subrating import (
    MAX_SUBRATE_FACTOR,
    SubrateSchedule,
    valid_event_spacing,
)

if TYPE_CHECKING:
    from bscontroller.linklayer.control import ControlProcedures

Opcode = ControlOpcode
RATE_UNIT_US = 125
MIN_RATE_INTERVAL = 10
MAX_RATE_INTERVAL = 32_000
MIN_WIRE_INTERVAL = 3
MAX_CE_LENGTH = MAX_RATE_INTERVAL - 1
NO_OFFSET = 0xFFFF
REQUEST = struct.Struct('<13H')
INDICATION = struct.Struct('<7H')

SUCCESS = ErrorCode.SUCCESS
INVALID = ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
INVALID_LL = ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR
DISALLOWED = ErrorCode.COMMAND_DISALLOWED_ERROR
UNSUPPORTED = ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
UNSUPPORTED_INTERVAL = ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR
UNSUPPORTED_TIMING = ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR


@dataclass(frozen=True)
class ConnectionRateParameters:
    connection_interval_min: int = 60
    connection_interval_max: int = MAX_RATE_INTERVAL
    subrate_min: int = 1
    subrate_max: int = 1
    max_latency: int = 0
    continuation_number: int = 0
    supervision_timeout: int = LL_SUPERVISION_TIMEOUT_MAX
    min_ce_length: int = 1
    max_ce_length: int = MAX_CE_LENGTH

    @property
    def valid(self) -> bool:
        return (
            MIN_WIRE_INTERVAL
            <= self.connection_interval_min
            <= self.connection_interval_max
            <= MAX_RATE_INTERVAL
            and 1 <= self.subrate_min <= self.subrate_max <= MAX_SUBRATE_FACTOR
            and 0 <= self.max_latency <= LL_CONN_LATENCY_MAX
            and self.subrate_max * (self.max_latency + 1) <= MAX_SUBRATE_FACTOR
            and 0 <= self.continuation_number < self.subrate_max
            and LL_SUPERVISION_TIMEOUT_MIN
            <= self.supervision_timeout
            <= LL_SUPERVISION_TIMEOUT_MAX
            and 1 <= self.min_ce_length <= self.max_ce_length <= MAX_CE_LENGTH
            and valid_event_spacing(
                self.connection_interval_max * RATE_UNIT_US,
                self.subrate_max,
                self.max_latency,
                self.supervision_timeout,
            )
        )

    def request(self, event_counter: int) -> bytes:
        return bytes([Opcode.LL_CONNECTION_RATE_REQ]) + REQUEST.pack(
            self.connection_interval_min,
            self.connection_interval_max,
            self.subrate_min,
            self.subrate_max,
            self.max_latency,
            self.continuation_number,
            self.supervision_timeout,
            0,
            event_counter,
            NO_OFFSET,
            NO_OFFSET,
            NO_OFFSET,
            NO_OFFSET,
        )

    def accepts(self, rate: ConnectionRateUpdate) -> bool:
        return (
            self.connection_interval_min
            <= rate.interval
            <= self.connection_interval_max
            and self.subrate_min <= rate.factor <= self.subrate_max
            and rate.latency <= self.max_latency
            and rate.continuation >= min(self.continuation_number, rate.factor - 1)
            and rate.timeout <= self.supervision_timeout
        )


@dataclass(frozen=True)
class ConnectionRateUpdate:
    offset: int
    interval: int
    factor: int
    latency: int
    continuation: int
    timeout: int

    @property
    def interval_us(self) -> int:
        return self.interval * RATE_UNIT_US

    @classmethod
    def decode(cls, payload: bytes) -> tuple[int, ConnectionRateUpdate]:
        offset, interval, instant, factor, latency, continuation, timeout = (
            INDICATION.unpack_from(payload, 1)
        )
        return instant, cls(offset, interval, factor, latency, continuation, timeout)

    def encode(self, instant: int) -> bytes:
        return bytes([Opcode.LL_CONNECTION_RATE_IND]) + INDICATION.pack(
            self.offset,
            self.interval,
            instant,
            self.factor,
            self.latency,
            self.continuation,
            self.timeout,
        )

    @property
    def valid(self) -> bool:
        return (
            0 <= self.offset <= self.interval
            and ConnectionRateParameters(
                self.interval,
                self.interval,
                self.factor,
                self.factor,
                self.latency,
                self.continuation,
                self.timeout,
            ).valid
        )


@dataclass(eq=False, kw_only=True)
class ConnectionRateContext(ProcedureContext):
    parameters: ConnectionRateParameters
    acknowledged: bool = False


class ConnectionRateProcedure(LocalProcedure):
    def __init__(self, owner: ControlProcedures):
        self.owner = owner
        self.connection = owner.connection
        self.limits = self.connection.link.default_rate
        self.context: ConnectionRateContext | None = None

    @property
    def instant_pending(self) -> bool:
        update = self.owner.pending
        return update is not None and update.rate is not None

    @property
    def in_progress(self) -> bool:
        return self.pending or self.instant_pending

    @property
    def deadline_us(self) -> int:
        # The Connection Rate Update completes when its parameters apply (6.B.5.1.32).
        if self.instant_pending:
            return self.owner.response_timer.deadline_us
        return super().deadline_us

    @property
    def can_send_request(self) -> bool:
        """An unsent Peripheral request waits behind a peer timing change."""
        return not (
            self.owner.pending
            or self.owner.connection_parameters.request_pending
            or self.owner.subrating.report_after_ack
        )

    @property
    def local_supported(self) -> bool:
        return bool(
            self.connection.link.features
            & LeFeatureMask.SHORTER_CONNECTION_INTERVALS_HOST_SUPPORT
        )

    @property
    def peer_supported(self) -> bool | None:
        return self.owner.features.supports(
            LeFeature.SHORTER_CONNECTION_INTERVALS_HOST_SUPPORT
        )

    def _spacing_us(self) -> tuple[int, int]:
        connection = self.connection
        tx, rx = connection.tx_phy, connection.rx_phy
        spacing = self.owner.frame_space.interval_overhead_us(tx, rx)
        return (
            self.owner.data_length.interval_required_us(rx, spacing),
            self.owner.data_length.minimum_interval_us(tx, rx, spacing),
        )

    def validate(self, parameters: ConnectionRateParameters) -> int:
        if (
            self.owner.connection_parameters.request_pending
            or self.owner.subrating.pending
            or self.owner.subrating.peer_request is not None
        ):
            return DISALLOWED
        if not parameters.valid or not self.connection.fits_cis_supervision_timeout(
            parameters.supervision_timeout
        ):
            return INVALID
        if not self.local_supported or self.peer_supported is False:
            return UNSUPPORTED
        required, minimum = self._spacing_us()
        maximum = parameters.connection_interval_max
        if maximum * RATE_UNIT_US < required:
            return UNSUPPORTED_TIMING
        if maximum < MIN_RATE_INTERVAL or maximum * RATE_UNIT_US < minimum:
            return UNSUPPORTED_INTERVAL
        return SUCCESS

    def request(self, parameters: ConnectionRateParameters) -> None:
        if status := self.validate(parameters):
            self.report(status)
            return
        local_central = self.connection.role == Role.CENTRAL
        opcode = (
            Opcode.LL_CONNECTION_RATE_IND
            if local_central
            else Opcode.LL_CONNECTION_RATE_REQ
        )
        self.context = ConnectionRateContext(
            opcode,
            frozenset(
                (Opcode.LL_UNKNOWN_RSP,)
                if local_central
                else (Opcode.LL_CONNECTION_RATE_IND,)
            ),
            self,
            parameters=parameters,
            host_initiated=True,
        )

        def begin(status: int) -> None:
            if not self.pending:
                return
            if status or not self.peer_supported:
                self.close(status or UNSUPPORTED)
            elif local_central:
                self.limits = parameters
                self._select_update(parameters)
            else:
                self.context.response_timer = self.owner.response_timer
                self.connection.enqueue_control(
                    parameters.request(self.connection.event_counter)
                )

        if self.peer_supported is None:
            self.owner.features.discover(begin, pages=1)
        else:
            begin(SUCCESS)

    def _select_update(
        self,
        request: ConnectionRateParameters,
        *,
        periodicity: int = 0,
        reference: int = 0,
        offsets: tuple[int, ...] = (),
        peer: bool = False,
    ) -> None:
        _, minimum_us = self._spacing_us()
        limits = self.limits
        minimum = max(
            request.connection_interval_min,
            limits.connection_interval_min,
            MIN_RATE_INTERVAL,
            (minimum_us + RATE_UNIT_US - 1) // RATE_UNIT_US,
        )
        maximum = min(request.connection_interval_max, limits.connection_interval_max)
        factor = max(request.subrate_min, limits.subrate_min)
        if (
            minimum > maximum
            or factor > min(request.subrate_max, limits.subrate_max)
            or request.max_latency > limits.max_latency
            or request.supervision_timeout > limits.supervision_timeout
            or not self.connection.fits_cis_supervision_timeout(
                request.supervision_timeout
            )
        ):
            if peer:
                self.owner.reject(Opcode.LL_CONNECTION_RATE_REQ, UNSUPPORTED_INTERVAL)
            else:
                self.close(UNSUPPORTED_INTERVAL)
            return
        interval = preferred_interval(minimum, maximum, periodicity)
        offset = next((offset for offset in offsets if offset < interval), None)
        update = InstantUpdate(
            self.owner.instant(),
            local=True,
            anchor_reference=(reference, offset) if offset is not None else None,
            rate=ConnectionRateUpdate(
                0,
                interval,
                factor,
                request.max_latency,
                min(
                    max(request.continuation_number, limits.continuation_number),
                    factor - 1,
                ),
                request.supervision_timeout,
            ),
        )
        self.owner.pending = update
        self.owner.transmitted = self.owner.acknowledged = False
        self.connection.enqueue_control(update.encode())

    def prepare(self, update: InstantUpdate, instant: int) -> InstantUpdate:
        rate = update.rate
        if update.anchor_reference is not None:
            reference, offset = update.anchor_reference
            distance = (instant - reference) & LL_EVENT_COUNTER_MASK
            old_interval = self.connection.parameters.interval_us // RATE_UNIT_US
            rate = replace(
                rate, offset=(offset - distance * old_interval) % rate.interval
            )
        if self.pending and self.context.request == Opcode.LL_CONNECTION_RATE_IND:
            self.context.phase = ProcedurePhase.WAIT_INSTANT
        return replace(update, instant=instant, rate=rate)

    def acknowledge(self, payload: bytes) -> None:
        if (
            payload[0] == Opcode.LL_CONNECTION_RATE_REQ
            and self.pending
            and self.context.transmitted
            and self.context.request == Opcode.LL_CONNECTION_RATE_REQ
        ):
            self.context.acknowledged = True

    def report(self, status: int) -> None:
        connection = self.connection
        current = connection.parameters
        schedule = self.owner.subrating.schedule
        self.owner.listener.on_connection_rate_changed(
            connection,
            status,
            current.interval_us // RATE_UNIT_US,
            schedule.factor,
            current.latency,
            schedule.continuation,
            current.timeout,
        )

    def close(self, status: int) -> None:
        if self.pending:
            self.context.finish()
            if self.context.host_initiated:
                self.report(status)

    def apply(self, rate: ConnectionRateUpdate, *, received_at_instant: bool) -> None:
        connection = self.connection
        old = connection.parameters
        subrating = self.owner.subrating
        changed = (
            old.interval_us,
            subrating.schedule.factor,
            old.latency,
            subrating.schedule.continuation,
            old.timeout,
        ) != (
            rate.interval_us,
            rate.factor,
            rate.latency,
            rate.continuation,
            rate.timeout,
        )
        connection.parameters = replace(
            old,
            latency=rate.latency,
            timeout=rate.timeout,
            rate_interval_us=rate.interval_us,
        )
        subrating.schedule = SubrateSchedule(
            rate.factor, connection.event_index, rate.continuation
        )
        subrating.activity = None
        if not received_at_instant:
            connection.anchor_us += rate.offset * RATE_UNIT_US
        connection.update_window = False
        connection.supervision_restart_us = connection.anchor_us
        if self.pending and self.context.phase == ProcedurePhase.WAIT_INSTANT:
            self.close(SUCCESS)
        elif changed:
            self.report(SUCCESS)

    def _receive_request(self, payload: bytes) -> None:
        fields = REQUEST.unpack_from(payload, 1)
        request = ConnectionRateParameters(*fields[:7])
        periodicity, reference, *offsets = fields[7:]
        valid_offsets = [offset for offset in offsets if offset != NO_OFFSET]
        opcode = Opcode.LL_CONNECTION_RATE_REQ
        if not (
            request.valid
            and periodicity <= request.connection_interval_max
            and all(
                offset < request.connection_interval_max for offset in valid_offsets
            )
            and len(set(valid_offsets)) == len(valid_offsets)
            and offsets[: len(valid_offsets)] == valid_offsets
        ):
            self.owner.reject(opcode, INVALID_LL)
            return
        if not self.local_supported or self.peer_supported is False:
            self.owner.reject(opcode, UNSUPPORTED)
            return
        if (
            self.owner.peer_instant_blocked
            or self.owner.subrating.pending
            or self.pending
        ):
            self.owner.reject(opcode, DIFFERENT_TRANSACTION_COLLISION)
            return
        required_us, _ = self._spacing_us()
        if request.connection_interval_max * RATE_UNIT_US < required_us:
            self.owner.reject(opcode, UNSUPPORTED_TIMING)
            return

        def begin(status: int) -> None:
            if status or not self.peer_supported:
                self.owner.reject(opcode, status or UNSUPPORTED)
            elif self.owner.pending or self.pending:
                self.owner.reject(opcode, DIFFERENT_TRANSACTION_COLLISION)
            else:
                self._select_update(
                    request,
                    periodicity=periodicity,
                    reference=reference,
                    offsets=tuple(valid_offsets),
                    peer=True,
                )

        if self.peer_supported is None:
            self.owner.features.discover(begin, pages=1)
        else:
            begin(SUCCESS)

    def _receive_update(self, payload: bytes) -> None:
        instant, rate = ConnectionRateUpdate.decode(payload)
        _, minimum_us = self._spacing_us()
        request = self.context
        responds_to_local = (
            request is not None
            and request.phase == ProcedurePhase.WAIT_RESPONSE
            and request.request == Opcode.LL_CONNECTION_RATE_REQ
            and request.acknowledged
        )
        if (
            not self.local_supported
            or not rate.valid
            or rate.interval < MIN_RATE_INTERVAL
            or rate.interval_us < minimum_us
            or not self.connection.fits_cis_supervision_timeout(rate.timeout)
        ):
            self.connection.enqueue_control(
                bytes([Opcode.LL_UNKNOWN_RSP, Opcode.LL_CONNECTION_RATE_IND])
            )
            if responds_to_local:
                self.close(INVALID_LL)
            return
        if responds_to_local and request.parameters.accepts(rate):
            request.phase = ProcedurePhase.WAIT_INSTANT
        elif not responds_to_local:
            self.owner.response_timer.restart(self.connection.radio.now_us)
        self.owner.accept_instant(InstantUpdate(instant, rate=rate))

    def control(self, payload: bytes) -> bool:
        opcode = payload[0]
        supported = (
            self.connection.link.features & LeFeatureMask.SHORTER_CONNECTION_INTERVALS
        )
        if (
            supported
            and opcode == Opcode.LL_CONNECTION_RATE_REQ
            and len(payload) == 1 + REQUEST.size
        ):
            self._receive_request(payload)
            return True
        if (
            supported
            and opcode == Opcode.LL_CONNECTION_RATE_IND
            and len(payload) == 1 + INDICATION.size
        ):
            self._receive_update(payload)
            return True
        if payload == bytes([Opcode.LL_UNKNOWN_RSP, Opcode.LL_CONNECTION_RATE_IND]):
            update = self.owner.pending
            if update is not None and update.local and update.rate is not None:
                self.owner.pending = None
                self.close(UNSUPPORTED)
                return True
        if not self.pending or not self.context.transmitted:
            return False
        if payload == bytes([Opcode.LL_UNKNOWN_RSP, self.context.request]):
            self.close(UNSUPPORTED)
            return True
        if len(payload) == 3 and payload[:2] == bytes(
            [Opcode.LL_REJECT_EXT_IND, self.context.request]
        ):
            self.close(payload[2])
            return True
        return False
