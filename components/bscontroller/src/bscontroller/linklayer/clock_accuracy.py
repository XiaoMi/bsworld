# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Sleep Clock Accuracy Update procedure (Core Vol 6, Part B, 5.1.14)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_SCA_500_PPM,
    LL_SCA_MAX,
    ControlOpcode,
)
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.procedure import (
    DeferredProcedure,
    LocalProcedure,
    ProcedureContext,
)
from bscontroller.linklayer.state import Role

if TYPE_CHECKING:
    from bscontroller.linklayer.control import ControlProcedures

Opcode = ControlOpcode


class ClockAccuracyProcedure(LocalProcedure):
    def __init__(self, owner: ControlProcedures):
        self.owner = owner
        connection = owner.connection
        self.peer_accuracy = (
            connection.parameters.sca
            if connection.role == Role.PERIPHERAL
            else LL_SCA_500_PPM
        )
        self.context: ProcedureContext | None = None
        self.target: int | None = None
        self.reannounce = False
        self.peer_rejected_update = False

    @property
    def peer_supports_update(self) -> bool:
        features = self.owner.connection.peer_features
        return not self.peer_rejected_update and (
            features is None
            or bool(features & LeFeatureMask.SLEEP_CLOCK_ACCURACY_UPDATES)
        )

    def queue_read(self) -> None:
        self.owner.connection.llcp.queue_for_event(
            DeferredProcedure('clock_accuracy', lambda: self._start(None), self._report)
        )

    def queue_change(self, accuracy: int) -> None:
        connection = self.owner.connection
        connection.llcp.queue_for_event(
            DeferredProcedure(
                'clock_accuracy',
                lambda: self._start(accuracy),
                ready=lambda: connection.link.can_change_sleep_clock_accuracy,
            )
        )

    def _start(self, target: int | None) -> None:
        connection = self.owner.connection
        host_read = target is None
        self.target = target
        local = connection.link.sleep_clock_accuracy
        advertised = local if target is None else target
        if advertised > local:
            connection.link.sleep_clock_accuracy = advertised
        if not self.peer_supports_update:
            if host_read:
                self._report(ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)
            return
        self.reannounce = False
        self.context = ProcedureContext(
            Opcode.LL_CLOCK_ACCURACY_REQ,
            frozenset((Opcode.LL_CLOCK_ACCURACY_RSP,)),
            self.control,
            host_initiated=host_read,
        )
        connection.llcp.send(
            self.context, bytes([Opcode.LL_CLOCK_ACCURACY_REQ, advertised])
        )

    def _report(self, status: int) -> None:
        self.owner.listener.on_peer_sca(
            self.owner.connection, status, self.peer_accuracy if not status else 0
        )

    def finish(self, status: int) -> None:
        if not self.pending:
            return
        self.context.finish()
        if self.context.host_initiated:
            self._report(status)
        elif status == ErrorCode.SUCCESS and self.target is not None:
            link = self.owner.connection.link
            if self.target < link.sleep_clock_accuracy and (
                self.reannounce or not link.can_change_sleep_clock_accuracy
            ):
                # A crossed reply or a new ACL still relies on the old value.
                self.queue_change(self.target)
            else:
                link.sleep_clock_accuracy = self.target

    def close(self, reason: int) -> None:
        self.finish(reason)

    def control(self, payload: bytes) -> bool:
        opcode = payload[0]
        connection = self.owner.connection
        if opcode == Opcode.LL_CLOCK_ACCURACY_REQ and len(payload) == 2:
            self.peer_accuracy = payload[1] & LL_SCA_MAX
            local = connection.link.sleep_clock_accuracy
            if self.pending and self.context.transmitted and self.target is not None:
                self.reannounce = self.target < local
            connection.llcp.respond(
                bytes([Opcode.LL_CLOCK_ACCURACY_RSP, local]), priority=True
            )
        elif opcode == Opcode.LL_CLOCK_ACCURACY_RSP and len(payload) == 2:
            if self.pending and self.context.transmitted:
                self.peer_accuracy = payload[1] & LL_SCA_MAX
                self.finish(ErrorCode.SUCCESS)
        elif (
            self.pending
            and self.context.transmitted
            and len(payload) == 2
            and payload == bytes([Opcode.LL_UNKNOWN_RSP, Opcode.LL_CLOCK_ACCURACY_REQ])
        ):
            self.peer_rejected_update = True
            self.finish(ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR)
        elif (
            self.pending
            and self.context.transmitted
            and len(payload) == 3
            and payload[:2]
            == bytes([Opcode.LL_REJECT_EXT_IND, Opcode.LL_CLOCK_ACCURACY_REQ])
        ):
            self.finish(payload[2])
        else:
            return False
        return True
