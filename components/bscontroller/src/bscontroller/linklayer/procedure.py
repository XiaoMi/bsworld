# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LLCP exchange identity, progress, and response-timer ownership.

A context describes one local or remote procedure instance. Reply routing keeps
that same identity until the procedure explicitly completes; no second set of
opcode-to-boolean liveness rules is needed in the coordinator.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import LL_RESPONSE_TIMEOUT_US

PROCEDURE_COLLISION = (
    ErrorCode.LMP_ERROR_TRANSACTION_COLLISION_OR_LL_PROCEDURE_COLLISION_ERROR
)
DIFFERENT_TRANSACTION_COLLISION = ErrorCode.DIFFERENT_TRANSACTION_COLLISION_ERROR


@dataclass(frozen=True, eq=False)
class DeferredProcedure:
    kind: str
    start: Callable[[], None]
    cancel: Callable[[int], None] | None = None
    ready: Callable[[], bool] | None = None


class ProcedureOrigin(Enum):
    LOCAL = auto()
    REMOTE = auto()


class ProcedurePhase(Enum):
    WAIT_RESPONSE = auto()
    WAIT_HOST = auto()
    WAIT_INSTANT = auto()
    COMPLETE = auto()


class ProcedureCompletion(Enum):
    EXPLICIT = auto()
    SENT = auto()
    ACKNOWLEDGED = auto()


@dataclass(eq=False)
class ProcedureContext:
    request: int
    responses: frozenset[int]
    receive: Callable[[bytes], bool] | None
    origin: ProcedureOrigin = ProcedureOrigin.LOCAL
    phase: ProcedurePhase = ProcedurePhase.WAIT_RESPONSE
    host_initiated: bool = False
    transmitted: bool = False
    acknowledged: bool = False
    response_timer: ProcedureResponseTimer | None = None
    # Some peer exchanges also reserve this endpoint's local procedure slot.
    blocks_local: bool = False
    completion: ProcedureCompletion = ProcedureCompletion.EXPLICIT
    on_sent: Callable[[bytes], None] | None = None
    on_acknowledged: Callable[[bytes], None] | None = None

    @property
    def active(self) -> bool:
        return self.phase != ProcedurePhase.COMPLETE

    @property
    def deadline_us(self) -> int | None:
        return (
            self.response_timer.deadline_us
            if self.response_timer and self.active
            else None
        )

    def stop_response_timer(self) -> None:
        self.response_timer = None

    def finish(self) -> None:
        self.phase = ProcedurePhase.COMPLETE


class LocalProcedure:
    """A local control exchange and its response deadline while active."""

    context: ProcedureContext | None

    @property
    def pending(self) -> bool:
        return self.context is not None and self.context.active

    @property
    def deadline_us(self) -> int | None:
        return self.context.deadline_us if self.context is not None else None


@dataclass
class ProcedureResponseTimer:
    """Shared ACL deadline, observed by procedures while T_PRT applies (6.B.5.2)."""

    deadline_us: int | None = None

    def restart(self, now_us: int) -> None:
        self.deadline_us = now_us + LL_RESPONSE_TIMEOUT_US
