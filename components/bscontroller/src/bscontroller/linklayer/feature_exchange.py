# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Feature Exchange and extended feature pages (Core Vol 6, Part B, 5.1.4).

The procedure owns its Host request, per-connection page cache and response
context. The LLCP coordinator tracks each context until that exchange completes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    PDU_CONTROL_FEATURE_SET_SIZE,
    PDU_CONTROL_FEATURE_SIZE,
    PDU_CONTROL_REJECT_EXT_SIZE,
    PDU_CONTROL_REJECT_SIZE,
    PDU_CONTROL_UNKNOWN_RSP_SIZE,
    ControlOpcode,
)
from bscontroller.linklayer.features import (
    FEATURE_MAX_PAGE,
    FEATURE_PAGE_SIZE,
    FEATURE_SET_SIZE,
    FEATURES_NOT_SENT_TO_PEER,
    LeFeature,
    LeFeatureMask,
    feature_max_page,
    feature_page,
)
from bscontroller.linklayer.procedure import LocalProcedure, ProcedureContext
from bscontroller.linklayer.state import Role
from bscontroller.radio.base import PHY_OCTET_MASK

if TYPE_CHECKING:
    from bscontroller.linklayer.control import ControlProcedures

Opcode = ControlOpcode


class FeatureExchangeProcedure(LocalProcedure):
    def __init__(self, owner: ControlProcedures):
        self.owner = owner
        self.connection = owner.connection
        self.context: ProcedureContext | None = None
        self._pages_requested: int | None = None
        self._peer_pages: dict[int, bytes] = {}
        self._peer_max_page: int | None = None
        self._page_pending: int | None = None
        self._completion: Callable[[int], None] | None = None

    @property
    def all_pending(self) -> bool:
        """Whether the Host has an outstanding Read All Remote Features request."""
        return self.pending and self._pages_requested is not None

    @property
    def local_features(self) -> int:
        return self.connection.link.features & ~FEATURES_NOT_SENT_TO_PEER

    def supports(self, feature: LeFeature) -> bool | None:
        """Return the peer's feature bit when its page is known."""
        base = self.connection.peer_features
        if feature < PDU_CONTROL_FEATURE_SET_SIZE * 8:
            return None if base is None else bool(base & (1 << feature))
        page, bit = divmod(
            feature - PDU_CONTROL_FEATURE_SET_SIZE * 8, FEATURE_PAGE_SIZE * 8
        )
        page += 1
        if (data := self._peer_pages.get(page)) is not None:
            return bool(data[bit // 8] & (1 << (bit % 8)))
        if (
            self._peer_max_page is not None
            and page > self._peer_max_page
            or base is not None
            and not base & LeFeatureMask.LL_EXTENDED_FEATURE_SET
        ):
            return False
        return None

    def request(self, pages: int | None = None) -> None:
        """Start Feature Exchange or report the cached result (Core Vol 6, Part B,
        5.1.4).
        """
        self._request(pages, None)

    def discover(
        self, completion: Callable[[int], None], *, pages: int | None = None
    ) -> None:
        """Fetch peer features for another LL procedure."""
        self._request(pages, completion)

    def _request(
        self, pages: int | None, completion: Callable[[int], None] | None
    ) -> None:
        self._completion = completion
        self._pages_requested = pages
        self._page_pending = None
        opcode = (
            Opcode.LL_FEATURE_REQ
            if self.connection.role == Role.CENTRAL
            else Opcode.LL_PERIPHERAL_FEATURE_REQ
        )
        self.context = ProcedureContext(
            opcode,
            frozenset((Opcode.LL_FEATURE_RSP,)),
            self,
            host_initiated=completion is None,
        )
        if self.connection.peer_features is not None:
            self._continue()
            return
        self.context.response_timer = self.owner.response_timer
        self.connection.enqueue_control(
            bytes([opcode]) + feature_page(self.local_features, 0)
        )

    def _continue(self) -> None:
        """Fetch the requested extended pages after page 0 (Vol 6, Part B, 5.1.4.3)."""
        if not self.pending:
            return
        page = self._next_page(self._pages_requested)
        if page is None:
            self.finish(ErrorCode.SUCCESS)
            return
        self.context.finish()
        self.context = ProcedureContext(
            Opcode.LL_FEATURE_EXT_REQ,
            frozenset((Opcode.LL_FEATURE_EXT_RSP,)),
            self,
            host_initiated=True,
        )
        self.context.response_timer = self.owner.response_timer
        self._page_pending = page
        local = self.local_features
        self.connection.enqueue_control(
            bytes([Opcode.LL_FEATURE_EXT_REQ, feature_max_page(local), page])
            + feature_page(local, page)
        )

    def _next_page(self, pages: int | None) -> int | None:
        if pages is None or not (
            (self.connection.peer_features or 0) & LeFeatureMask.LL_EXTENDED_FEATURE_SET
        ):
            return None
        # Page 1 discovers the peer's MaxPage, even if the Host only needs page 0.
        limit = 1 if self._peer_max_page is None else min(pages, self._peer_max_page)
        return next(
            (page for page in range(1, limit + 1) if page not in self._peer_pages), None
        )

    def can_report_cached(self, pages: int | None) -> bool:
        return (
            self.connection.peer_features is not None and self._next_page(pages) is None
        )

    def finish(self, status: int) -> None:
        if not self.pending:
            return
        self.context.finish()
        self._page_pending = None
        completion, self._completion = self._completion, None
        if completion is None:
            self.report(status, self._pages_requested)
        else:
            completion(status)

    def report(self, status: int, pages: int | None) -> None:
        if pages is None:
            self.owner.listener.on_remote_features(self.connection, status)
            return
        features = bytearray(FEATURE_SET_SIZE)
        features[:PDU_CONTROL_FEATURE_SET_SIZE] = feature_page(
            self.connection.peer_features or 0, 0
        )
        valid = 0
        for page in range(1, (self._peer_max_page or 0) + 1):
            if page not in self._peer_pages:
                break
            offset = PDU_CONTROL_FEATURE_SET_SIZE + (page - 1) * FEATURE_PAGE_SIZE
            features[offset : offset + FEATURE_PAGE_SIZE] = self._peer_pages[page]
            valid = page
        self.owner.listener.on_remote_features_all(
            self.connection, status, self._peer_max_page or 0, valid, bytes(features)
        )

    def control(self, payload: bytes) -> bool:
        """Process feature PDUs and refusals belonging to the current exchange."""
        opcode = payload[0]
        if (
            opcode in (Opcode.LL_FEATURE_REQ, Opcode.LL_PERIPHERAL_FEATURE_REQ)
            and len(payload) == PDU_CONTROL_FEATURE_SIZE
        ):
            self.connection.peer_features = int.from_bytes(payload[1:], 'little')
            # Use the first-octet feature intersection and retain local higher octets
            # for FEATURE_RSP (Vol 6, Part B, 5.1.4.1; 5.1.4.2).
            local_features = int.from_bytes(
                feature_page(self.local_features, 0),
                'little',
            )
            features = (local_features & ~PHY_OCTET_MASK) | (
                local_features & payload[1]
            )
            self.connection.enqueue_control(
                bytes([Opcode.LL_FEATURE_RSP])
                + features.to_bytes(PDU_CONTROL_FEATURE_SET_SIZE, "little"),
                priority=True,
            )
            return True
        if opcode == Opcode.LL_FEATURE_RSP and len(payload) == PDU_CONTROL_FEATURE_SIZE:
            self.connection.peer_features = int.from_bytes(payload[1:], 'little')
            if self._page_pending is None:
                self._continue()
            return True
        if opcode in (Opcode.LL_FEATURE_EXT_REQ, Opcode.LL_FEATURE_EXT_RSP):
            if len(payload) != 3 + FEATURE_PAGE_SIZE:
                return False
            maximum, page = payload[1:3]
            if maximum > FEATURE_MAX_PAGE or not 1 <= page <= FEATURE_MAX_PAGE:
                return False
            if opcode == Opcode.LL_FEATURE_EXT_RSP and (
                not self.pending or page != self._page_pending
            ):
                return True
            self._peer_max_page = maximum
            self._peer_pages[page] = payload[3:]
            if opcode == Opcode.LL_FEATURE_EXT_REQ:
                local = self.local_features
                self.connection.enqueue_control(
                    bytes([Opcode.LL_FEATURE_EXT_RSP, feature_max_page(local), page])
                    + feature_page(local, page),
                    priority=True,
                )
            else:
                self._continue()
            return True
        if not self.pending:
            return False
        if (
            opcode == Opcode.LL_UNKNOWN_RSP
            and len(payload) == PDU_CONTROL_UNKNOWN_RSP_SIZE
            and payload[1] == self.context.request
        ):
            self.finish(ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)
            return True
        if opcode == Opcode.LL_REJECT_IND and len(payload) == PDU_CONTROL_REJECT_SIZE:
            # A legacy rejection has no opcode; the outstanding request owns it.
            self.finish(payload[1])
            return True
        if (
            opcode == Opcode.LL_REJECT_EXT_IND
            and len(payload) == PDU_CONTROL_REJECT_EXT_SIZE
            and payload[1] == self.context.request
        ):
            self.finish(payload[2])
            return True
        return False
