# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Changes committed when a connection event reaches a control-procedure Instant."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bscontroller.linklayer import LL_CHANNEL_MAP_SIZE, ControlOpcode

if TYPE_CHECKING:
    from bscontroller.linklayer.connection_rate import ConnectionRateUpdate
    from bscontroller.linklayer.parameters import Parameters


CONNECTION_UPDATE = struct.Struct('<BHHHHH')
CHANNEL_MAP = struct.Struct(f'<{LL_CHANNEL_MAP_SIZE}sH')
PHY_UPDATE = struct.Struct('<BBH')


@dataclass(frozen=True)
class InstantUpdate:
    instant: int
    parameters: Parameters | None = None
    channel_map: int | None = None
    central_tx_phy: int = 0
    peripheral_tx_phy: int = 0
    local: bool = False
    anchor_reference: tuple[int, int] | None = None
    notify: bool | None = None
    rate: ConnectionRateUpdate | None = None

    @property
    def opcode(self) -> ControlOpcode:
        if self.parameters is not None:
            return ControlOpcode.LL_CONNECTION_UPDATE_IND
        if self.channel_map is not None:
            return ControlOpcode.LL_CHANNEL_MAP_IND
        if self.rate is not None:
            return ControlOpcode.LL_CONNECTION_RATE_IND
        return ControlOpcode.LL_PHY_UPDATE_IND

    def encode(self) -> bytes:
        """Encode the scheduled values and Instant for the corresponding indication."""
        if self.rate is not None:
            return self.rate.encode(self.instant)
        if self.parameters is not None:
            parameters = self.parameters
            fields = CONNECTION_UPDATE.pack(
                parameters.window_size,
                parameters.window_offset,
                parameters.interval,
                parameters.latency,
                parameters.timeout,
                self.instant,
            )
        elif self.channel_map is not None:
            fields = CHANNEL_MAP.pack(
                self.channel_map.to_bytes(LL_CHANNEL_MAP_SIZE, 'little'), self.instant
            )
        else:
            central, peripheral = self.central_tx_phy, self.peripheral_tx_phy
            fields = PHY_UPDATE.pack(
                1 << (central - 1) if central else 0,
                1 << (peripheral - 1) if peripheral else 0,
                self.instant,
            )
        return bytes([self.opcode]) + fields

    @property
    def changes_timing(self) -> bool:
        return self.parameters is not None or self.rate is not None

    @property
    def interval_us(self) -> int | None:
        if self.parameters is not None:
            return self.parameters.interval_us
        return self.rate.interval_us if self.rate is not None else None

    @property
    def supervision_timeout(self) -> int | None:
        if self.parameters is not None:
            return self.parameters.timeout
        return self.rate.timeout if self.rate is not None else None
