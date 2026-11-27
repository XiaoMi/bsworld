# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Advertiser RSSI hysteresis and supervision (Core Vol 6, Part B, 4.4.3.8)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bscontroller.address import Address
from bscontroller.errors import ErrorCode

if TYPE_CHECKING:
    from collections.abc import Callable

    from bscontroller.radio.base import Radio

MONITORED_ADVERTISERS_MAX = 8
MONITOR_TIMEOUT_UNIT_US = 1_000_000


@dataclass
class MonitoredAdvertiser:
    address: Address
    low: int
    high: int
    timeout_us: int
    deadline_us: int | None = None
    awaiting_high: bool = False


class AdvertiserMonitoring:
    """Monitoring is independent of report duplicate filtering and the accept list.

    Entries keep their last observed reporting address, including resolved identity
    address types. Keys compare the underlying public/random address identity.
    """

    def __init__(self, radio: Radio, report: Callable[[Address, int], None]):
        self._radio = radio
        self._report = report
        self.entries: dict[tuple[bytes, bool], MonitoredAdvertiser] = {}
        self.enabled = False

    @staticmethod
    def _key(address: Address) -> tuple[bytes, bool]:
        return bytes(address), address.is_random

    def add(self, address: Address, low: int, high: int, timeout: int) -> int:
        key = self._key(address)
        if key in self.entries:
            return ErrorCode.SUCCESS
        if len(self.entries) == MONITORED_ADVERTISERS_MAX:
            return ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR
        duration = timeout * MONITOR_TIMEOUT_UNIT_US
        self.entries[key] = MonitoredAdvertiser(
            address,
            low,
            high,
            duration,
            self._radio.now_us + duration if self.enabled else None,
        )
        return ErrorCode.SUCCESS

    def remove(self, address: Address) -> int:
        if self.entries.pop(self._key(address), None) is None:
            return ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
        return ErrorCode.SUCCESS

    def clear(self) -> None:
        self.entries.clear()

    def reset(self) -> None:
        self.clear()
        self.enabled = False

    def enable(self, enabled: bool) -> None:
        self.enabled = enabled
        for entry in self.entries.values():
            entry.awaiting_high = False
            entry.deadline_us = (
                self._radio.now_us + entry.timeout_us if enabled else None
            )

    @property
    def deadline_us(self) -> int | None:
        return min(
            (e.deadline_us for e in self.entries.values() if e.deadline_us is not None),
            default=None,
        )

    def expire(self) -> None:
        for key, entry in list(self.entries.items()):
            if self.entries.get(key) is not entry:
                continue
            if (
                entry.deadline_us is not None
                and entry.deadline_us <= self._radio.now_us
            ):
                entry.deadline_us = None
                entry.awaiting_high = True
                self._report(entry.address, 0)

    def received(self, address: Address, rssi: float) -> None:
        if not self.enabled:
            return
        self.expire()
        entry = self.entries.get(self._key(address))
        if entry is None:
            return
        entry.address = address
        if entry.awaiting_high:
            if rssi >= entry.high:
                entry.awaiting_high = False
                entry.deadline_us = self._radio.now_us + entry.timeout_us
                self._report(address, 1)
        elif rssi > entry.low:
            entry.deadline_us = self._radio.now_us + entry.timeout_us
