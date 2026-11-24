# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""PHY Update procedure and directional preferences (Core Vol 6, Part B, 5.1.10).

The procedure owns negotiation and its local/remote exchange context. It submits
an InstantUpdate to the connection-control coordinator; the packet executor only
sees the resulting transmission constraints and committed PHY configuration.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from bscontroller.capabilities import CONTROLLER_SUPPORTED_PHY_MASK
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    PDU_CONTROL_OPCODE_SIZE,
    PDU_CONTROL_PHY_REQ_SIZE,
    PDU_CONTROL_PHY_UPDATE_SIZE,
    ControlOpcode,
)
from bscontroller.linklayer.instant import InstantUpdate
from bscontroller.linklayer.procedure import (
    ProcedureContext,
    ProcedureOrigin,
    ProcedurePhase,
)
from bscontroller.linklayer.state import Role
from bscontroller.radio.base import Phy

if TYPE_CHECKING:
    from bscontroller.linklayer.control import ControlProcedures

Opcode = ControlOpcode
PROCEDURE_COLLISION = (
    ErrorCode.LMP_ERROR_TRANSACTION_COLLISION_OR_LL_PROCEDURE_COLLISION_ERROR
)


class PhyProcedure:
    def __init__(self, owner: ControlProcedures):
        self.owner = owner
        self.connection = owner.connection
        self.context: ProcedureContext | None = None
        self.remote_phy_request: bytes | None = None
        self.tx_phys = self.connection.link.default_tx_phys
        self.rx_phys = self.connection.link.default_rx_phys

    @property
    def phy_request(self) -> bool:
        return self.context is not None and self.context.active

    @property
    def phy_request_local(self) -> bool:
        return self.phy_request and self.context.host_initiated

    def begin(self, origin: ProcedureOrigin) -> ProcedureContext:
        """Give each negotiation a distinct identity, including timer ownership."""
        self.context = ProcedureContext(
            Opcode.LL_PHY_REQ,
            frozenset((Opcode.LL_PHY_RSP, Opcode.LL_PHY_UPDATE_IND)),
            self,
            origin=origin,
            host_initiated=origin == ProcedureOrigin.LOCAL,
        )
        return self.context

    def finish(self, status: int, *, notify: bool = True) -> None:
        context = self.context
        if context is not None:
            self.owner.response_timer.stop(context)
            context.finish()
        if notify:
            self.owner.listener.on_phy_updated(self.connection, status)

    def indication_sent(self, payload: bytes) -> None:
        if self.context is not None:
            self.owner.response_timer.stop(self.context)
        if not (payload[1] or payload[2]):
            self.finish(ErrorCode.SUCCESS, notify=self.phy_request_local)

    def mark_transmitted(self) -> None:
        if self.context is not None:
            self.context.transmitted = True

    def request_phy(self, tx_phys: int, rx_phys: int) -> None:
        """Send LL_PHY_REQ (Core Vol 6, Part B, 5.1.10)."""
        self.tx_phys, self.rx_phys = tx_phys, rx_phys
        context = self.begin(ProcedureOrigin.LOCAL)
        self.owner.response_timer.start(context, self.connection.radio.now_us)
        self.owner.enqueue(bytes([Opcode.LL_PHY_REQ, tx_phys, rx_phys]))

    def reject_phy(self, reason: int) -> None:
        """Finish a rejected PHY request and retain any other procedure's response
        timer.
        """
        self.finish(reason)
        if self.remote_phy_request is not None:
            request, self.remote_phy_request = self.remote_phy_request, None
            self.control(request)

    @staticmethod
    def choose_phy(mask: int, current: int) -> int:
        if mask & (1 << (current - 1)):
            return current
        return next(
            (
                phy
                for phy in (Phy.LE_2M, Phy.LE_1M, Phy.LE_CODED)
                if mask & (1 << (phy - 1))
            ),
            current,
        )

    def _phy_update(self, peer_tx: int, peer_rx: int) -> None:
        """Central chooses PHYs and sends LL_PHY_UPDATE_IND (Core Vol 6, Part B,
        5.1.10).
        """
        connection = self.connection
        peer_tx &= CONTROLLER_SUPPORTED_PHY_MASK
        peer_rx &= CONTROLLER_SUPPORTED_PHY_MASK
        tx_mask, rx_mask = self.tx_phys & peer_rx, self.rx_phys & peer_tx
        # A single identical peer preference requires a symmetric selection.
        if peer_tx == peer_rx and peer_tx.bit_count() == 1:
            tx_mask = rx_mask = tx_mask & rx_mask
        central = self.choose_phy(tx_mask, connection.tx_phy)
        peripheral = self.choose_phy(rx_mask, connection.rx_phy)
        central_change = central if central != connection.tx_phy else 0
        peripheral_change = peripheral if peripheral != connection.rx_phy else 0
        instant = self.owner.instant() if central_change or peripheral_change else 0
        self.owner.enqueue(
            struct.pack(
                '<BBBH',
                Opcode.LL_PHY_UPDATE_IND,
                (1 << (central_change - 1)) if central_change else 0,
                (1 << (peripheral_change - 1)) if peripheral_change else 0,
                instant,
            )
        )
        if instant:
            if self.context is not None:
                self.context.phase = ProcedurePhase.WAIT_INSTANT
            self.owner.pending = InstantUpdate(
                instant,
                central_tx_phy=central_change,
                peripheral_tx_phy=peripheral_change,
                local=True,
            )
            self.owner.transmitted = self.owner.acknowledged = False

    def control(self, payload: bytes) -> bool:
        connection = self.connection
        opcode = payload[0]
        if (
            opcode in (Opcode.LL_PHY_REQ, Opcode.LL_PHY_RSP)
            and len(payload) == PDU_CONTROL_PHY_REQ_SIZE
        ):
            peer_tx, peer_rx = payload[PDU_CONTROL_OPCODE_SIZE:]
            # RFU preference bits are ignored; selection intersects local support.
            if not peer_tx or not peer_rx:
                self.owner.reject(opcode, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR)
            elif opcode == Opcode.LL_PHY_REQ:
                if (
                    connection.role == Role.PERIPHERAL
                    and self.phy_request_local
                    and not (
                        self.owner.pending
                        or self.remote_phy_request
                        or self.owner.connection_parameters.remote_parameters
                        or self.owner.connection_parameters.parameter_request
                        or self.owner.data_length.pending
                        or self.owner.feature_request
                    )
                ):
                    self.remote_phy_request = payload
                elif self.owner.peer_instant_blocked:
                    self.owner.reject(
                        opcode,
                        (
                            PROCEDURE_COLLISION
                            if self.phy_request
                            else ErrorCode.DIFFERENT_TRANSACTION_COLLISION_ERROR
                        ),
                    )
                else:
                    context = self.begin(ProcedureOrigin.REMOTE)
                    self.owner.response_timer.start(context, connection.radio.now_us)
                    if connection.role == Role.CENTRAL:
                        # The Central chooses the resulting PHY pair and Instant;
                        # a Peripheral only returns its directional preferences.
                        self._phy_update(peer_tx, peer_rx)
                    else:
                        self.owner.enqueue(
                            bytes([Opcode.LL_PHY_RSP, self.tx_phys, self.rx_phys])
                        )
            elif self.phy_request and connection.role == Role.CENTRAL:
                self._phy_update(peer_tx, peer_rx)
            return True

        if (
            opcode == Opcode.LL_PHY_UPDATE_IND
            and len(payload) == PDU_CONTROL_PHY_UPDATE_SIZE
        ):
            central, peripheral, instant = struct.unpack(
                '<BBH', payload[PDU_CONTROL_OPCODE_SIZE:]
            )
            if (
                connection.role != Role.PERIPHERAL
                or central not in (0, *(1 << (phy - 1) for phy in Phy))
                or peripheral not in (0, *(1 << (phy - 1) for phy in Phy))
            ):
                connection.complete(ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR)
            elif central or peripheral:
                if self.context is not None:
                    # Acceptance at the current event can apply synchronously and
                    # finish this context. Publish the wait phase before that call.
                    self.context.phase = ProcedurePhase.WAIT_INSTANT
                    self.owner.response_timer.stop(self.context)
                self.owner.accept_instant(
                    InstantUpdate(
                        instant,
                        central_tx_phy=central.bit_length(),
                        peripheral_tx_phy=peripheral.bit_length(),
                    )
                )
            else:
                # An all-zero update completes negotiation without changing PHYs. A Host
                # request still needs a completion event (Vol 6, Part B, 5.1.10).
                self.finish(ErrorCode.SUCCESS, notify=self.phy_request_local)
            return True

        return False
