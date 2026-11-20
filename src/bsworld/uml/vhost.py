# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Vhost-user split queues for UML Bluetooth and console devices."""

from __future__ import annotations

import mmap
import os
import socket
import struct
from collections.abc import Collection
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

# Vhost-user uses native byte order. Modern virtqueues are little endian.
_HEADER = struct.Struct("=III")
_U64 = struct.Struct("=Q")
_STATE = struct.Struct("=II")
_RING_ADDR = struct.Struct("=IIQQQQ")
_REGION = struct.Struct("=QQQQ")
_CONFIG = struct.Struct("=III")
_INDEX = struct.Struct("<H")
_DESCRIPTOR = struct.Struct("<QIHH")
_USED_ENTRY = struct.Struct("<II")

_VERSION = 1
_REPLY = 1 << 2
_NEED_REPLY = 1 << 3
_FEATURES = (1 << 30) | (1 << 32)  # PROTOCOL_FEATURES and VERSION_1
_NOTIFY_FEATURES = (1 << 3) | (1 << 5) | (1 << 14)  # ACK, BACKEND_REQ, INBAND
_CONFIG_FEATURE = 1 << 9
_BACKEND_VRING_CALL = 4
_DESC_NEXT = 1 << 0
_DESC_WRITE = 1 << 1
_MAX_PAYLOAD = 512


class _Request(IntEnum):
    GET_FEATURES = 1
    SET_FEATURES = 2
    SET_OWNER = 3
    SET_MEM_TABLE = 5
    SET_VRING_NUM = 8
    SET_VRING_ADDR = 9
    SET_VRING_BASE = 10
    GET_VRING_BASE = 11
    GET_PROTOCOL_FEATURES = 15
    SET_PROTOCOL_FEATURES = 16
    SET_VRING_ENABLE = 18
    SET_BACKEND_REQ_FD = 21
    GET_CONFIG = 24
    VRING_KICK = 35


class _Channel:
    """Frame a nonblocking socket without reading across an FD-bearing message."""

    def __init__(self, sock: socket.socket):
        self.socket = sock
        self.socket.setblocking(False)
        self._incoming = bytearray()
        self._fds: list[int] = []
        self.outgoing = bytearray()

    def fileno(self) -> int:
        return self.socket.fileno()

    def close(self) -> None:
        self.socket.close()
        for fd in self._fds:
            os.close(fd)
        self._fds.clear()

    def receive(self) -> tuple[int, int, bytes, list[int]] | None:
        while True:
            needed = _HEADER.size
            if len(self._incoming) >= _HEADER.size:
                request, flags, size = _HEADER.unpack_from(self._incoming)
                if size > _MAX_PAYLOAD:
                    raise ValueError("vhost-user message is too large")
                needed += size
                if len(self._incoming) == needed:
                    payload = bytes(self._incoming[_HEADER.size :])
                    self._incoming.clear()
                    fds, self._fds = self._fds, []
                    return request, flags, payload, fds
            try:
                data, fds, flags, _ = socket.recv_fds(
                    self.socket,
                    needed - len(self._incoming),
                    1,
                    socket.MSG_CMSG_CLOEXEC,
                )
            except BlockingIOError:
                return None
            self._fds.extend(fds)
            if flags & socket.MSG_CTRUNC or len(self._fds) > 1:
                raise ValueError("unexpected vhost-user file descriptors")
            if not data:
                raise EOFError("UML closed its vhost-user connection")
            self._incoming.extend(data)

    def send(self, request: int, flags: int, payload: bytes) -> None:
        self.outgoing.extend(_HEADER.pack(request, flags, len(payload)))
        self.outgoing.extend(payload)

    def flush(self) -> None:
        if self.outgoing:
            try:
                sent = self.socket.send(self.outgoing)
            except BlockingIOError:
                return
            del self.outgoing[:sent]


@dataclass(slots=True)
class _Memory:
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
class _Virtqueue:
    size: int = 0
    next_available: int = 0
    addresses: tuple[int, int, int] | None = None
    enabled: bool = False
    started: bool = False
    outstanding: dict[int, VhostBuffer] = field(default_factory=dict)

    def setup(self, memory: _Memory, desc: int, used: int, avail: int) -> None:
        if not self.size:
            raise ValueError("virtqueue addresses arrived before its size")
        self.addresses = (
            memory.translate(desc, _DESCRIPTOR.size * self.size, user=True),
            memory.translate(avail, 4 + _INDEX.size * self.size, user=True),
            memory.translate(used, 4 + _USED_ENTRY.size * self.size, user=True),
        )

    def pop(self, memory: _Memory) -> VhostBuffer | None:
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


class VhostUserBackend:
    """One UML virtio device with two queues and in-band notifications."""

    def __init__(self, path: Path, *, config: bytes | None = None):
        self.path = path
        self._config = config
        self._protocol_features = _NOTIFY_FEATURES
        if config is not None:
            self._protocol_features |= _CONFIG_FEATURE
        self._listener: socket.socket | None = None
        self._connection: _Channel | None = None
        self._calls: _Channel | None = None
        self._memory: _Memory | None = None
        self._queues = (_Virtqueue(), _Virtqueue())
        # Keep one call outstanding on the reply stream. New calls for the
        # same queue can share a notification until it is sent.
        self._calling = False
        self._pending_calls: dict[int, None] = {}

    def listen(self) -> None:
        self.path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            listener.listen(1)
            listener.setblocking(False)
        except BaseException:
            listener.close()
            raise
        self._listener = listener

    def close(self) -> None:
        for channel in (self._listener, self._connection, self._calls):
            if channel is not None:
                channel.close()
        self._listener = self._connection = self._calls = None
        self._pending_calls.clear()
        self._calling = False
        if self._memory is not None:
            self._memory.mapping.close()
            self._memory = None
        self._queues = (_Virtqueue(), _Virtqueue())
        self.path.unlink(missing_ok=True)

    @property
    def read_fds(self) -> tuple[int, ...]:
        listener = self._listener if self._connection is None else None
        return tuple(
            channel.fileno()
            for channel in (listener, self._connection, self._calls)
            if channel is not None
        )

    @property
    def write_fds(self) -> tuple[int, ...]:
        return tuple(
            channel.fileno()
            for channel in (self._connection, self._calls)
            if channel is not None and channel.outgoing
        )

    @property
    def notification_pending(self) -> bool:
        """A queued call still needs to be sent or acknowledged by UML."""
        return self._calling or bool(self._pending_calls)

    def pop(self, index: int) -> VhostBuffer | None:
        if self._memory is None:
            return None
        return self._queue(index).pop(self._memory)

    def complete(self, index: int, buffer: VhostBuffer, length: int) -> None:
        self._queue(index).complete(buffer, length)

    def notify(self, index: int) -> None:
        """Queue an interrupt for a used ring without blocking for its ACK."""
        if self._calls is None:
            raise RuntimeError("UML has not established a vhost-user request channel")
        self._queue(index)
        self._pending_calls[index] = None
        self._send_call()

    def _send_call(self) -> None:
        if self._calling or not self._pending_calls:
            return
        assert self._calls is not None
        index = next(iter(self._pending_calls))
        del self._pending_calls[index]
        self._calls.send(
            _BACKEND_VRING_CALL, _VERSION | _NEED_REPLY, _STATE.pack(index, 0)
        )
        self._calling = True

    def service(
        self, readable: Collection[int], writable: Collection[int] = ()
    ) -> tuple[int, ...]:
        """Service ready sockets and return the queues kicked by the guest."""
        listener = self._listener
        if (
            self._connection is None
            and listener is not None
            and listener.fileno() in readable
        ):
            connection, _ = listener.accept()
            self._connection = _Channel(connection)

        kicks = []
        for channel in (self._connection, self._calls):
            if channel is None:
                continue
            if channel.fileno() in writable:
                channel.flush()
            if channel.fileno() not in readable:
                continue
            while (message := channel.receive()) is not None:
                request, flags, payload, fds = message
                try:
                    if channel is self._connection:
                        if (
                            index := self._handle(request, flags, payload, fds)
                        ) is not None:
                            kicks.append(index)
                    else:
                        self._ack_call(request, flags, payload, fds)
                finally:
                    for fd in fds:
                        os.close(fd)
        return tuple(kicks)

    def _ack_call(
        self, request: int, flags: int, payload: bytes, fds: list[int]
    ) -> None:
        if (
            not self._calling
            or request != _BACKEND_VRING_CALL
            or flags != _VERSION | _REPLY
            or payload != _U64.pack(0)
            or fds
        ):
            raise ValueError("invalid UML vhost-user interrupt acknowledgment")
        self._calling = False
        self._send_call()

    def _handle(
        self, request: int, flags: int, payload: bytes, fds: list[int]
    ) -> int | None:
        if flags & ~_NEED_REPLY != _VERSION:
            raise ValueError("invalid UML vhost-user message flags")
        fd_requests = (_Request.SET_MEM_TABLE, _Request.SET_BACKEND_REQ_FD)
        if len(fds) != int(request in fd_requests):
            raise ValueError("invalid UML vhost-user file descriptor count")

        try:
            reply, kick = self._dispatch(_Request(request), payload, fds)
        except (ValueError, struct.error) as exc:
            raise ValueError(f"vhost-user request {request}: {exc}") from exc
        if reply is not None or flags & _NEED_REPLY:
            assert self._connection is not None
            self._connection.send(
                request, _VERSION | _REPLY, reply if reply is not None else _U64.pack(0)
            )
        return kick

    def _dispatch(
        self, request: _Request, payload: bytes, fds: list[int]
    ) -> tuple[bytes | None, int | None]:
        match request:
            case (
                _Request.GET_FEATURES
                | _Request.GET_PROTOCOL_FEATURES
                | _Request.SET_OWNER
            ):
                if payload:
                    raise ValueError("unexpected payload")
                if request == _Request.GET_FEATURES:
                    return _U64.pack(_FEATURES), None
                if request == _Request.GET_PROTOCOL_FEATURES:
                    return _U64.pack(self._protocol_features), None
            case _Request.SET_FEATURES | _Request.SET_PROTOCOL_FEATURES:
                (features,) = _U64.unpack(payload)
                expected = (
                    _FEATURES
                    if request == _Request.SET_FEATURES
                    else self._protocol_features
                )
                if features != expected:
                    raise ValueError("UML did not negotiate the supported features")
            case _Request.SET_MEM_TABLE:
                self._set_memory(payload, fds[0])
            case _Request.SET_BACKEND_REQ_FD:
                if payload or self._calls is not None:
                    raise ValueError("invalid vhost-user request channel setup")
                channel = socket.socket(fileno=fds[0])
                fds.clear()
                self._calls = _Channel(channel)
            case _Request.GET_CONFIG:
                offset, size, _ = _CONFIG.unpack_from(payload)
                if (
                    self._config is None
                    or offset + size > len(self._config)
                    or len(payload) != _CONFIG.size + size
                ):
                    raise ValueError("invalid virtio device config read")
                return payload[: _CONFIG.size] + self._config[
                    offset : offset + size
                ], None
            case _Request.SET_VRING_ADDR:
                index, flags, desc, used, avail, _ = _RING_ADDR.unpack(payload)
                if flags or self._memory is None:
                    raise ValueError("invalid virtqueue addresses")
                self._queue(index).setup(self._memory, desc, used, avail)
            case _:
                return self._queue_state(request, payload)
        return None, None

    def _set_memory(self, payload: bytes, fd: int) -> None:
        count, _ = _STATE.unpack_from(payload)
        if (
            count != 1
            or self.notification_pending
            or any(queue.enabled for queue in self._queues)
        ):
            raise ValueError("expected one UML memory region before enabling queues")
        guest, length, user, offset = _REGION.unpack(payload[_STATE.size :])
        if not length or offset + length > os.fstat(fd).st_size:
            raise ValueError("invalid UML memory region size")
        mapping = mmap.mmap(fd, length, access=mmap.ACCESS_WRITE, offset=offset)
        if self._memory is not None:
            self._memory.mapping.close()
            self._queues = (_Virtqueue(), _Virtqueue())
        self._memory = _Memory(mapping, guest, user)

    def _queue(self, index: int) -> _Virtqueue:
        if not 0 <= index < len(self._queues):
            raise ValueError("invalid virtqueue index")
        return self._queues[index]

    def _queue_state(
        self, request: _Request, payload: bytes
    ) -> tuple[bytes | None, int | None]:
        index, value = _STATE.unpack(payload)
        queue = self._queue(index)
        match request:
            case _Request.SET_VRING_NUM:
                if not 0 < value <= 256 or value & (value - 1):
                    raise ValueError("invalid virtqueue size")
                queue.size = value
            case _Request.SET_VRING_BASE:
                if value > 0xFFFF:
                    raise ValueError("invalid virtqueue base")
                queue.next_available = value
            case _Request.GET_VRING_BASE:
                queue.started = False
                return _STATE.pack(index, queue.next_available), None
            case _Request.SET_VRING_ENABLE:
                if value not in (0, 1) or value and queue.addresses is None:
                    raise ValueError("invalid virtqueue enable state")
                queue.enabled = bool(value)
            case _Request.VRING_KICK:
                if value or not queue.enabled:
                    raise ValueError("invalid virtqueue kick")
                queue.started = True
                return None, index
        return None, None


__all__ = ["VhostBuffer", "VhostUserBackend"]
