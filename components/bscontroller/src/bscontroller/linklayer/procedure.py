# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LLCP exchange identity, progress, and response-timer ownership.

A context describes one local or remote procedure instance. Reply routing keeps
that same identity until the procedure explicitly completes; no second set of
opcode-to-boolean liveness rules is needed in the coordinator.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

from bscontroller.linklayer import LL_RESPONSE_TIMEOUT_US


class ProcedureHandler(Protocol):
    def control(self, payload: bytes) -> bool: ...


class ProcedureOrigin(Enum):
    LOCAL = auto()
    REMOTE = auto()


class ProcedurePhase(Enum):
    WAIT_RESPONSE = auto()
    WAIT_HOST = auto()
    WAIT_INSTANT = auto()
    COMPLETE = auto()


@dataclass(eq=False)
class ProcedureContext:
    request: int
    responses: frozenset[int]
    owner: ProcedureHandler
    origin: ProcedureOrigin = ProcedureOrigin.LOCAL
    phase: ProcedurePhase = ProcedurePhase.WAIT_RESPONSE
    host_initiated: bool = False
    transmitted: bool = False
    deadline_us: int = 0

    @property
    def active(self) -> bool:
        return self.phase != ProcedurePhase.COMPLETE

    def finish(self) -> None:
        self.phase = ProcedurePhase.COMPLETE


class LocalProcedure:
    """A control exchange with its own response deadline and local procedure slot."""

    context: ProcedureContext | None

    @property
    def pending(self) -> bool:
        return self.context is not None and self.context.active

    @property
    def busy(self) -> bool:
        return self.pending

    @property
    def deadline_us(self) -> int:
        return self.context.deadline_us if self.pending else 0


@dataclass
class ProcedureResponseTimer:
    """Serialized Instant-related slot timer with a procedure-instance owner.

    A rejected local PHY procedure must not stop a timer that has already been
    handed to a remote connection-parameter request (Core Vol 6, Part B, 5.2).
    """

    deadline_us: int = 0
    owner: ProcedureContext | None = None

    def start(self, owner: ProcedureContext, now_us: int) -> None:
        self.owner = owner
        self.deadline_us = now_us + LL_RESPONSE_TIMEOUT_US

    def restart(self, now_us: int) -> None:
        if self.deadline_us:
            self.deadline_us = now_us + LL_RESPONSE_TIMEOUT_US

    def stop(self, owner: ProcedureContext | None = None) -> None:
        if owner is None or self.owner is None or self.owner is owner:
            self.deadline_us = 0
            self.owner = None
