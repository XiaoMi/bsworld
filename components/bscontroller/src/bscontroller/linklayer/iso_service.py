# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Host-facing ISO operations (Core Vol 4, Part E, 7.8.96–116)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from bscontroller.errors import ErrorCode
from bscontroller.linklayer.iso import IsoTestCounters

if TYPE_CHECKING:
    from bscontroller.linklayer.api import BigConfig, BigSyncConfig, CigConfig
    from bscontroller.linklayer.iso import IsoTxSync
    from bscontroller.linklayer.link import RadioLink


class CigCommands(Protocol):
    @property
    def connection_accept_timeout(self) -> int: ...

    def set_accept_timeout(self, timeout: int) -> int: ...
    def contains(self, handle: int) -> bool: ...
    def set_cig_parameters(self, config: CigConfig) -> tuple[int, tuple[int, ...]]: ...
    def create_cis(self, pairs: tuple[tuple[int, int], ...]) -> int: ...
    def remove_cig(self, cig_id: int) -> int: ...
    def accept_cis(self, handle: int) -> int: ...
    def reject_cis(self, handle: int, reason: int) -> int: ...


class BigCommands(Protocol):
    def create(self, config: BigConfig) -> int: ...
    def terminate(self, handle: int, reason: int) -> int: ...
    def synchronize(self, config: BigSyncConfig) -> int: ...
    def terminate_sync(self, handle: int) -> int: ...


class IsoService:
    """Resolve a Host handle against the CIS and BIS transports and their data paths."""

    def __init__(self, link: RadioLink):
        self._link = link
        self._paths = link.roles.iso
        self._cig = link.roles.cig

    def link_quality(self, handle: int) -> tuple[int, tuple[int, ...]]:
        if self._cig.contains(handle):
            return self._cig.iso_link_quality(handle)
        return self._link.roles.broadcast.iso_link_quality(handle)

    def _awaiting_paths(self, handle: int) -> bool:
        return self._cig.contains(handle) and not self._paths.contains(handle)

    def setup(
        self,
        handle: int,
        direction: int,
        path_id: int,
        codec: int,
        configuration: bytes,
        controller_delay_us: int,
    ) -> int:
        if self._awaiting_paths(handle):
            return ErrorCode.COMMAND_DISALLOWED_ERROR
        return self._paths.setup(
            handle, direction, path_id, codec, configuration, controller_delay_us
        )

    def remove(self, handle: int, direction: int) -> int:
        if self._awaiting_paths(handle):
            return ErrorCode.COMMAND_DISALLOWED_ERROR
        return self._paths.remove(handle, direction)

    def test(self, handle: int, transmit: bool, payload_type: int) -> int:
        if self._cig.waiting_for_establishment(handle):
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        return self._paths.test(handle, transmit, payload_type)

    def counters(
        self, handle: int, *, end: bool = False
    ) -> tuple[int, IsoTestCounters]:
        if self._cig.waiting_for_establishment(handle):
            return ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR, IsoTestCounters()
        return self._paths.counters(handle, end=end)

    def read_tx_sync(self, handle: int) -> tuple[int, IsoTxSync | None]:
        return self._paths.read_tx_sync(handle)

    def accepts_data(self, handle: int) -> bool:
        # A Central retains its CIS handle and configured paths after disconnect.
        return self._paths.contains(handle) and not self._cig.waiting_for_establishment(
            handle
        )

    def input(
        self,
        handle: int,
        fragment: bytes,
        pb_flag: int,
        sequence_number: int | None,
        length: int | None,
        timestamp_us: int | None,
    ) -> None:
        self._paths.input(
            handle,
            fragment,
            pb_flag,
            sequence_number,
            length,
            timestamp_us,
            self._link.now_us,
        )
