# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""RF outcomes handed to the LL owner of a role's lifetime.

Packet validation and immediate responses remain in the event executor. These
records describe successful exchanges; role managers commit the resulting state
transition before notifying the upper layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bscontroller.linklayer import LL_CONN_INTERVAL_UNIT_US
from bscontroller.linklayer.state import Role
from bscontroller.radio.base import Phy

if TYPE_CHECKING:
    from bscontroller.address import Address
    from bscontroller.linklayer.advertising import ExtendedPdu, SyncInfo
    from bscontroller.linklayer.discovery import Advertising, Initiating, PeriodicSync
    from bscontroller.linklayer.parameters import Parameters
    from bscontroller.radio.base import Received


@dataclass(frozen=True)
class ConnectionAccepted:
    parameters: Parameters
    peer: Address
    role: Role
    end_us: int
    self_address: Address
    csa2: bool
    advertiser: Advertising | None = None
    window_delay_us: int = LL_CONN_INTERVAL_UNIT_US
    phy: Phy = Phy.LE_1M
    initiator: Initiating | None = None
    local_rpa: Address | None = None


@dataclass(frozen=True)
class SyncInfoReceived:
    address: Address
    sid: int
    info: SyncInfo
    packet: Received
    # The AdvA as received, before address resolution.
    advertiser: Address | None = None


@dataclass(frozen=True)
class PeriodicPacketReceived:
    sync: PeriodicSync
    packet: Received
    pdu: ExtendedPdu


@dataclass(frozen=True)
class ScanEvent:
    """One admitted scan slice; an empty channel means an idle interval."""

    end_us: int
    channel: int | None = None
    phy: Phy = Phy.LE_1M
    initiator: Initiating | None = None
