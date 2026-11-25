# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Changes committed when a connection event reaches a control-procedure Instant."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bscontroller.linklayer.connection_rate import ConnectionRateUpdate
    from bscontroller.linklayer.parameters import Parameters


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
