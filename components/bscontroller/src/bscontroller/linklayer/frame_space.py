# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""ACL Frame Space Update (Core Vol 6, Part B, 4.1 and 5.1.30)."""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, replace
from enum import IntFlag
from typing import TYPE_CHECKING

from bscontroller.capabilities import CONTROLLER_SUPPORTED_PHY_MASK
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    LL_IFS_US,
    LL_RESPONSE_TIMEOUT_US,
    ControlOpcode,
)
from bscontroller.linklayer.features import LeFeature, LeFeatureMask
from bscontroller.linklayer.procedure import ProcedureContext
from bscontroller.linklayer.state import Role
from bscontroller.radio.base import RADIO_SWITCH_TIME_US, Phy

if TYPE_CHECKING:
    from bscontroller.linklayer.control import ControlProcedures

Opcode = ControlOpcode
REQUEST = struct.Struct('<HHBH')
RESPONSE = struct.Struct('<HBH')
MAX_SPACE_US = 10_000
CIS_SPACINGS = 0x18
INVALID = ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
INVALID_LL = ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR
UNSUPPORTED = ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
COLLISION = ErrorCode.LMP_ERROR_TRANSACTION_COLLISION_OR_LL_PROCEDURE_COLLISION_ERROR
LOCAL_HOST, REMOTE = 0, 2


class Spacing(IntFlag):
    CENTRAL_TO_PERIPHERAL = 1
    PERIPHERAL_TO_CENTRAL = 2
    EVENT = 4


ACL_SPACINGS = int(
    Spacing.CENTRAL_TO_PERIPHERAL | Spacing.PERIPHERAL_TO_CENTRAL | Spacing.EVENT
)


def phy_bit(phy: Phy) -> int:
    return 1 << (phy - 1)


@dataclass(frozen=True)
class FrameSpaceChange:
    space: int
    phys: int
    spacing: int

    def affects(self, phy: Phy, spacing: Spacing) -> bool:
        return bool(self.phys & phy_bit(phy) and self.spacing & spacing)

    def pack(self) -> bytes:
        return RESPONSE.pack(self.space, self.phys, self.spacing)


@dataclass(frozen=True)
class FrameSpaceRequest:
    minimum: int
    maximum: int
    phys: int
    spacing: int

    @property
    def valid_fields(self) -> bool:
        return 0 <= self.minimum <= self.maximum <= MAX_SPACE_US and bool(
            self.phys and self.spacing
        )

    def affects(self, phy: Phy, spacing: Spacing) -> bool:
        return bool(self.phys & phy_bit(phy) and self.spacing & spacing)

    def pack(self) -> bytes:
        return REQUEST.pack(self.minimum, self.maximum, self.phys, self.spacing)

    def accepts(self, response: FrameSpaceChange) -> bool:
        return (
            self.minimum <= response.space <= self.maximum
            and not response.phys & ~self.phys
            and not response.spacing & ~self.spacing
            and bool(response.phys) == bool(response.spacing)
        )


class FrameSpaceProcedure:
    def __init__(self, owner: ControlProcedures):
        self.owner = owner
        self.connection = owner.connection
        self.spaces: dict[tuple[Phy, Spacing], int] = {}
        self.context: ProcedureContext | None = None
        self.local_request: FrameSpaceRequest | None = None
        self.response: FrameSpaceChange | None = None
        self.previous_rx: tuple[Phy, int] | None = None
        self.peer_rejected = False
        self.peer_rejected_pairs: set[tuple[int, int]] = set()
        self.collision = False

    @property
    def pending(self) -> bool:
        return self.context is not None and self.context.active

    @property
    def busy(self) -> bool:
        return self.pending or self.response is not None

    @property
    def deadline_us(self) -> int:
        return self.context.deadline_us if self.pending else 0

    @property
    def tx_direction(self) -> Spacing:
        return (
            Spacing.PERIPHERAL_TO_CENTRAL
            if self.connection.role == Role.CENTRAL
            else Spacing.CENTRAL_TO_PERIPHERAL
        )

    @property
    def rx_direction(self) -> Spacing:
        return (
            Spacing.CENTRAL_TO_PERIPHERAL
            if self.connection.role == Role.CENTRAL
            else Spacing.PERIPHERAL_TO_CENTRAL
        )

    def space(self, phy: Phy, spacing: Spacing) -> int:
        return self.spaces.get((phy, spacing), LL_IFS_US)

    def _planned_response(self, opcode: int | None) -> FrameSpaceChange | None:
        change = self.response
        if opcode == Opcode.LL_FRAME_SPACE_RSP and change and self._fits_link(change):
            return change
        return None

    def tx_space_us(self, opcode: int | None) -> int:
        phy = self.connection.tx_phy
        direction = self.tx_direction
        change = self._planned_response(opcode)
        if change and change.affects(phy, direction):
            return change.space
        return self.space(phy, direction)

    def rx_window_us(self, opcode: int | None = None) -> tuple[int, int]:
        phy = self.connection.rx_phy
        direction = self.rx_direction
        current = low = high = self.space(phy, direction)
        request = self.local_request
        if (
            self.pending
            and not self.collision
            and request.affects(phy, direction)
            and (self.context.transmitted or opcode == Opcode.LL_FRAME_SPACE_REQ)
        ):
            low, high = min(current, request.minimum), max(current, request.maximum)
        if self.previous_rx is not None and self.previous_rx[0] == phy:
            previous = self.previous_rx[1]
            low, high = min(low, previous), max(high, previous)
        change = self._planned_response(opcode)
        if change and change.affects(phy, direction):
            low, high = min(low, change.space), max(high, change.space)
        return low, high

    def received_after_us(self, spacing: int) -> None:
        phy = self.connection.rx_phy
        if self.previous_rx is not None and self.previous_rx[0] == phy:
            if abs(spacing - self.space(phy, self.rx_direction)) <= 2:
                self.previous_rx = None

    def event_spacing_us(self, central_phy: Phy, opcode: int | None = None) -> int:
        change = self._planned_response(opcode)
        if change and change.affects(central_phy, Spacing.EVENT):
            return change.space
        return self.space(central_phy, Spacing.EVENT)

    def interval_overhead_us(
        self, tx_phy: Phy, rx_phy: Phy, change: FrameSpaceChange | None = None
    ) -> int:
        central, peripheral = (
            (tx_phy, rx_phy)
            if self.connection.role == Role.CENTRAL
            else (rx_phy, tx_phy)
        )

        def value(phy: Phy, spacing: Spacing) -> int:
            if change is not None and change.affects(phy, spacing):
                return change.space
            return self.space(phy, spacing)

        return value(peripheral, Spacing.CENTRAL_TO_PERIPHERAL) + value(
            central, Spacing.EVENT
        )

    def fits_interval(self, interval: int) -> bool:
        return interval >= self.minimum_interval()

    def minimum_interval(self) -> int:
        tx, rx = self.connection.tx_phy, self.connection.rx_phy
        spacing = self.interval_overhead_us(tx, rx)
        if spacing == 2 * LL_IFS_US:
            return 0
        required = self.owner.data_length.minimum_interval_us(tx, rx, spacing)
        return (required + LL_CONN_INTERVAL_UNIT_US - 1) // LL_CONN_INTERVAL_UNIT_US

    def fits_phy_pair(self, tx_phy: Phy, rx_phy: Phy) -> bool:
        spacing = self.interval_overhead_us(tx_phy, rx_phy)
        parameters = self.connection.parameters
        return (
            parameters.rate_interval_us is None and spacing == 2 * LL_IFS_US
        ) or parameters.interval_us >= self.owner.data_length.minimum_interval_us(
            tx_phy, rx_phy, spacing
        )

    def _fits_link(self, change: FrameSpaceChange) -> bool:
        for tx, rx, interval_us in self.owner.link_timings():
            spacing = self.interval_overhead_us(tx, rx, change)
            if spacing > self.interval_overhead_us(
                tx, rx
            ) and interval_us < self.owner.data_length.minimum_interval_us(
                tx, rx, spacing
            ):
                return False
        return True

    @staticmethod
    def _keys(phys: int, spacing: int) -> Iterator[tuple[Phy, Spacing]]:
        for phy in Phy:
            if phys & phy_bit(phy):
                for direction in Spacing:
                    if spacing & direction:
                        yield phy, direction

    def validate(self, request: FrameSpaceRequest) -> int:
        if not request.valid_fields:
            return INVALID
        if (
            request.phys & ~CONTROLLER_SUPPORTED_PHY_MASK
            or request.spacing & ~ACL_SPACINGS
        ):
            return ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR
        features = self.connection.peer_features
        if (
            self.peer_rejected
            or self.owner.features.supports(LeFeature.FRAME_SPACE_UPDATE) is False
            or (request.phys, request.spacing) in self.peer_rejected_pairs
        ):
            return UNSUPPORTED
        if features is not None and (
            request.phys & phy_bit(Phy.LE_2M)
            and not features & LeFeatureMask.LE_2M_PHY
            or request.phys & phy_bit(Phy.LE_CODED)
            and not features & LeFeatureMask.LE_CODED_PHY
        ):
            return UNSUPPORTED
        minimum = max(request.minimum, RADIO_SWITCH_TIME_US)
        if not self._fits_link(
            FrameSpaceChange(minimum, request.phys, request.spacing)
        ):
            return INVALID
        return ErrorCode.SUCCESS

    def _wire_request(self, request: FrameSpaceRequest) -> FrameSpaceRequest:
        current = max(
            self.space(phy, direction)
            for phy, direction in self._keys(request.phys, request.spacing)
        )
        return replace(
            request,
            minimum=max(request.minimum, RADIO_SWITCH_TIME_US),
            maximum=max(request.maximum, current),
        )

    def request(self, request: FrameSpaceRequest) -> None:
        if status := self.validate(request):
            self._report(status, LOCAL_HOST)
            return
        self.local_request = self._wire_request(request)
        self.context = ProcedureContext(
            Opcode.LL_FRAME_SPACE_REQ,
            frozenset((Opcode.LL_FRAME_SPACE_RSP,)),
            self,
            host_initiated=True,
            deadline_us=self.connection.radio.now_us + LL_RESPONSE_TIMEOUT_US,
        )
        self.collision = False
        self.owner.enqueue(
            bytes([Opcode.LL_FRAME_SPACE_REQ]) + self.local_request.pack()
        )

    def _report(
        self, status: int, initiator: int, change: FrameSpaceChange | None = None
    ) -> None:
        self.owner.listener.on_frame_space_updated(
            self.connection,
            status,
            initiator,
            change.space if change else 0,
            change.phys if change else 0,
            change.spacing if change else 0,
        )

    def _finish(self, status: int, change: FrameSpaceChange | None = None) -> None:
        if self.pending:
            self.context.finish()
            self.local_request = None
            self._report(status, LOCAL_HOST, change)

    def close(self, reason: int) -> None:
        self._finish(reason)
        self.response = None
        self.previous_rx = None

    def cancel(self, reason: int) -> None:
        self._report(reason, LOCAL_HOST)

    def _apply(self, change: FrameSpaceChange, *, responding: bool = False) -> None:
        phy, direction = self.connection.rx_phy, self.rx_direction
        if responding and change.affects(phy, direction):
            previous = self.space(phy, direction)
            if previous != change.space:
                self.previous_rx = phy, previous
        for key in self._keys(change.phys, change.spacing):
            self.spaces[key] = change.space
        self.owner.data_length.refresh()

    def prepare(self, payload: bytes) -> bytes:
        if payload[0] == Opcode.LL_FRAME_SPACE_REQ and self.pending:
            # A peer update may have changed FS_Max while this request was queued.
            self.local_request = self._wire_request(self.local_request)
            return bytes([Opcode.LL_FRAME_SPACE_REQ]) + self.local_request.pack()
        if payload[0] == Opcode.LL_FRAME_SPACE_RSP and self.response is not None:
            change = self._planned_response(payload[0])
            if change is None:
                # The peer may schedule a timing change while this reply is queued.
                self.response = None
                return bytes(
                    (
                        Opcode.LL_REJECT_EXT_IND,
                        Opcode.LL_FRAME_SPACE_REQ,
                        ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR,
                    )
                )
            self._apply(change, responding=True)
        return payload

    def sent(self, payload: bytes) -> None:
        if payload[0] == Opcode.LL_FRAME_SPACE_RSP and self.response is not None:
            change, self.response = self.response, None
            if change.phys and change.spacing:
                self._report(ErrorCode.SUCCESS, REMOTE, change)

    def _receive_request(self, request: FrameSpaceRequest) -> None:
        local_queued = self.pending and not self.context.transmitted
        reply_first = local_queued and self.response is None

        def reject(reason: int) -> None:
            self.owner.reject(Opcode.LL_FRAME_SPACE_REQ, reason, priority=reply_first)

        if not request.valid_fields:
            reject(INVALID_LL)
            return
        if request.spacing & CIS_SPACINGS:
            reject(UNSUPPORTED)
            return
        phys = request.phys & CONTROLLER_SUPPORTED_PHY_MASK
        spacing = request.spacing & ACL_SPACINGS
        if not phys or not spacing:
            reject(INVALID_LL)
            return
        if self.response is not None:
            reject(COLLISION)
            return
        if self.pending and not local_queued:
            if self.connection.role == Role.CENTRAL:
                reject(COLLISION)
                return
            self.collision = True
        space = max(request.minimum, RADIO_SWITCH_TIME_US)
        if space > request.maximum or any(
            self.space(phy, direction) > request.maximum
            for phy, direction in self._keys(phys, spacing)
        ):
            reject(ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR)
            return
        if not self._fits_link(FrameSpaceChange(space, phys, spacing)):
            reject(ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR)
            return

        changed_phys = changed_spacings = 0
        for phy, direction in self._keys(phys, spacing):
            if self.space(phy, direction) != space:
                changed_phys |= phy_bit(phy)
                changed_spacings |= direction
        self.response = FrameSpaceChange(space, changed_phys, changed_spacings)
        self.owner.enqueue(
            bytes([Opcode.LL_FRAME_SPACE_RSP]) + self.response.pack(),
            priority=reply_first,
        )

    def control(self, payload: bytes) -> bool:
        opcode = payload[0]
        if opcode == Opcode.LL_FRAME_SPACE_REQ and len(payload) == 1 + REQUEST.size:
            self._receive_request(FrameSpaceRequest(*REQUEST.unpack_from(payload, 1)))
            return True
        if not self.pending or not self.context.transmitted:
            return False
        if opcode == Opcode.LL_FRAME_SPACE_RSP and len(payload) == 1 + RESPONSE.size:
            if self.collision:
                return True
            space, phys, spacing = RESPONSE.unpack_from(payload, 1)
            # Reserved fields in an incoming LL PDU do not become Host values.
            change = FrameSpaceChange(
                space,
                phys & CONTROLLER_SUPPORTED_PHY_MASK,
                spacing & (ACL_SPACINGS | CIS_SPACINGS),
            )
            if not self.local_request.accepts(change) or not self._fits_link(change):
                self._finish(INVALID_LL)
            else:
                self._apply(change)
                self._finish(ErrorCode.SUCCESS, change)
            return True
        if payload == bytes([Opcode.LL_UNKNOWN_RSP, Opcode.LL_FRAME_SPACE_REQ]):
            self.peer_rejected = True
            self._finish(UNSUPPORTED)
            return True
        if len(payload) == 3 and payload[:2] == bytes(
            [Opcode.LL_REJECT_EXT_IND, Opcode.LL_FRAME_SPACE_REQ]
        ):
            status = payload[2]
            request = self.local_request
            # A multi-bit rejection cannot identify the unsupported selection.
            if (
                status == UNSUPPORTED
                and request.phys.bit_count() == request.spacing.bit_count() == 1
            ):
                self.peer_rejected_pairs.add((request.phys, request.spacing))
            self._finish(status)
            return True
        return False
