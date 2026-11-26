# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Host-to-Controller packet-based flow control (Core Vol 4, Part E, 4.1.1)."""

import logging
from collections import Counter

logger = logging.getLogger(__name__)


class DataBuffers:
    """One ACL or ISO buffer pool, shared by all handles of that type."""

    def __init__(self, name: str, packet_length: int, packet_count: int):
        self.name = name
        self.packet_length = packet_length
        self.packet_count = packet_count
        self._pending: Counter[int] = Counter()

    def acquire(self, handle: int, length: int) -> bool:
        if length > self.packet_length:
            logger.warning(
                '%s handle 0x%04X: dropping %d-octet packet (maximum %d)',
                self.name,
                handle,
                length,
                self.packet_length,
            )
            return False
        if self._pending.total() >= self.packet_count:
            logger.warning(
                '%s handle 0x%04X: dropping packet (HCI buffer limit %d reached)',
                self.name,
                handle,
                self.packet_count,
            )
            return False
        self._pending[handle] += 1
        return True

    def complete(self, handle: int, count: int) -> None:
        if self._pending[handle] <= count:
            self.release(handle)
        else:
            self._pending[handle] -= count

    def pending(self, handle: int) -> int:
        return self._pending[handle]

    def release(self, handle: int) -> None:
        self._pending.pop(handle, None)

    def reset(self) -> None:
        self._pending.clear()
