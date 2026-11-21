# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Split virtqueues in UML guest memory."""

from __future__ import annotations

import mmap
import struct
from dataclasses import dataclass, field

# Modern virtqueue fields are little endian.
_INDEX = struct.Struct("<H")
_DESCRIPTOR = struct.Struct("<QIHH")
_USED_ENTRY = struct.Struct("<II")

_DESC_NEXT = 1 << 0
_DESC_WRITE = 1 << 1


@dataclass(slots=True)
class GuestMemory:
    mapping: mmap.mmap
    guest_base: int
    user_base: int

    def translate(self, addr: int, size: int, *, user: bool = False) -> int:
        offset = addr - (self.user_base if user else self.guest_base)
        if offset < 0 or offset + size > len(self.mapping):
            raise ValueError("virtqueue address outside guest memory")
        return offset


@dataclass(frozen=True, slots=True)
class VhostBuffer:
    """One descriptor chain removed from a guest's available ring."""

    _mapping: mmap.mmap
    _head: int
    _segments: tuple[tuple[int, int], ...]
    _writable: bool

    @property
    def capacity(self) -> int:
        return sum(size for _, size in self._segments) if self._writable else 0

    def read(self) -> bytes:
        if self._writable:
            raise ValueError("cannot read a guest receive buffer")
        return b"".join(self._mapping[ptr : ptr + size] for ptr, size in self._segments)

    def write(self, data: bytes | bytearray) -> int:
        if not self._writable or len(data) > self.capacity:
            raise ValueError("data does not fit in the guest receive buffer")
        offset = 0
        for ptr, capacity in self._segments:
            size = min(capacity, len(data) - offset)
            self._mapping[ptr : ptr + size] = data[offset : offset + size]
            offset += size
            if offset == len(data):
                break
        return offset


@dataclass(slots=True)
class Virtqueue:
    size: int = 0
    next_available: int = 0
    addresses: tuple[int, int, int] | None = None
    enabled: bool = False
    started: bool = False
    outstanding: dict[int, VhostBuffer] = field(default_factory=dict)

    def setup(self, memory: GuestMemory, desc: int, used: int, avail: int) -> None:
        if not self.size:
            raise ValueError("virtqueue addresses arrived before its size")
        self.addresses = (
            memory.translate(desc, _DESCRIPTOR.size * self.size, user=True),
            memory.translate(avail, 4 + _INDEX.size * self.size, user=True),
            memory.translate(used, 4 + _USED_ENTRY.size * self.size, user=True),
        )

    def pop(self, memory: GuestMemory) -> VhostBuffer | None:
        if not self.enabled or not self.started or self.addresses is None:
            return None
        desc, avail, _ = self.addresses
        available = _INDEX.unpack_from(memory.mapping, avail + 2)[0]
        count = (available - self.next_available) & 0xFFFF
        if not count:
            return None
        if count + len(self.outstanding) > self.size:
            raise ValueError("virtqueue available index exceeds its size")
        slot = self.next_available % self.size
        head = _INDEX.unpack_from(memory.mapping, avail + 4 + _INDEX.size * slot)[0]
        if head in self.outstanding:
            raise ValueError("guest reused a pending virtqueue buffer")

        index = head
        seen = set()
        segments = []
        writable = None
        while True:
            if index >= self.size or index in seen:
                raise ValueError("invalid virtqueue descriptor chain")
            seen.add(index)
            addr, length, flags, index = _DESCRIPTOR.unpack_from(
                memory.mapping, desc + index * _DESCRIPTOR.size
            )
            segment_writable = bool(flags & _DESC_WRITE)
            if flags & ~(_DESC_NEXT | _DESC_WRITE) or (
                writable is not None and writable != segment_writable
            ):
                raise ValueError("unsupported virtqueue descriptor flags")
            writable = segment_writable
            segments.append((memory.translate(addr, length), length))
            if not flags & _DESC_NEXT:
                break

        buffer = VhostBuffer(memory.mapping, head, tuple(segments), writable)
        self.outstanding[head] = buffer
        self.next_available = (self.next_available + 1) & 0xFFFF
        return buffer

    def complete(self, buffer: VhostBuffer, length: int) -> None:
        if self.outstanding.get(buffer._head) is not buffer:
            raise ValueError("buffer does not belong to the pending virtqueue")
        if not 0 <= length <= buffer.capacity:
            raise ValueError("invalid virtqueue completion length")
        assert self.addresses is not None
        _, _, used = self.addresses
        index = _INDEX.unpack_from(buffer._mapping, used + 2)[0]
        _USED_ENTRY.pack_into(
            buffer._mapping,
            used + 4 + (index % self.size) * _USED_ENTRY.size,
            buffer._head,
            length,
        )
        _INDEX.pack_into(buffer._mapping, used + 2, (index + 1) & 0xFFFF)
        del self.outstanding[buffer._head]
