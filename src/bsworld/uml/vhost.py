# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Vhost-user socket protocol for UML devices."""

from __future__ import annotations

import mmap
import os
import socket
import struct
from collections.abc import Collection
from enum import IntEnum
from pathlib import Path

from .virtqueue import GuestMemory, VhostBuffer, Virtqueue

# Vhost-user messages use native byte order.
_HEADER = struct.Struct("=III")
_U64 = struct.Struct("=Q")
_STATE = struct.Struct("=II")
_RING_ADDR = struct.Struct("=IIQQQQ")
_REGION = struct.Struct("=QQQQ")
_CONFIG = struct.Struct("=III")

_VERSION = 1
_REPLY = 1 << 2
_NEED_REPLY = 1 << 3
_FEATURES = (1 << 30) | (1 << 32)  # PROTOCOL_FEATURES and VERSION_1
_NOTIFY_FEATURES = (1 << 3) | (1 << 5) | (1 << 14)  # ACK, BACKEND_REQ, INBAND
_CONFIG_FEATURE = 1 << 9
_BACKEND_VRING_CALL = 4
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
        self._memory: GuestMemory | None = None
        self._queues = (Virtqueue(), Virtqueue())
        self._interrupts: set[int] = set()
        # Keep one call outstanding on the reply stream. New calls for the
        # same queue can share a notification until it is sent.
        self._calling = False
        self._pending_calls: dict[int, None] = {}
        self.read_fds: tuple[int, ...] = ()
        self.write_fds: tuple[int, ...] = ()
        self._watched: frozenset[int] = frozenset()

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
        self._refresh_fds()

    def close(self) -> None:
        for channel in (self._listener, self._connection, self._calls):
            if channel is not None:
                channel.close()
        self._listener = self._connection = self._calls = None
        self._refresh_fds()
        self._interrupts.clear()
        self._pending_calls.clear()
        self._calling = False
        if self._memory is not None:
            self._memory.mapping.close()
            self._memory = None
        self._queues = (Virtqueue(), Virtqueue())
        self.path.unlink(missing_ok=True)

    def _refresh_fds(self) -> None:
        channels = (self._connection, self._calls)
        listener = self._listener if self._connection is None else None
        self.read_fds = tuple(
            channel.fileno() for channel in (listener, *channels) if channel is not None
        )
        self.write_fds = tuple(
            channel.fileno()
            for channel in channels
            if channel is not None and channel.outgoing
        )
        self._watched = frozenset(self.read_fds)

    @property
    def has_notifications(self) -> bool:
        return bool(self._interrupts)

    @property
    def notification_pending(self) -> bool:
        """A queued call still needs to be sent or acknowledged by UML."""
        return self._calling or bool(self._pending_calls)

    def pop(self, index: int) -> VhostBuffer | None:
        if self._memory is None:
            return None
        return self._queue(index).pop(self._memory)

    def complete(
        self, index: int, buffer: VhostBuffer, length: int, *, notify: bool = True
    ) -> None:
        self._queue(index).complete(buffer, length)
        if notify:
            self._interrupts.add(index)

    def flush_notifications(self) -> None:
        """Queue used-ring notifications when the caller can schedule them."""
        if not self._interrupts:
            return
        if self._calls is None:
            raise RuntimeError("UML has not established a vhost-user request channel")
        for index in sorted(self._interrupts):
            self._pending_calls[index] = None
        self._interrupts.clear()
        self._send_call()
        self._refresh_fds()

    def _send_call(self) -> None:
        if self._calling or not self._pending_calls:
            return
        assert self._calls is not None
        index = next(iter(self._pending_calls))
        self._calls.send(
            _BACKEND_VRING_CALL, _VERSION | _NEED_REPLY, _STATE.pack(index, 0)
        )
        del self._pending_calls[index]
        self._calling = True

    def service(
        self, readable: Collection[int], writable: Collection[int] = ()
    ) -> tuple[int, ...]:
        """Service ready sockets and return the queues kicked by the guest."""
        if self._watched.isdisjoint(readable) and self._watched.isdisjoint(writable):
            return ()
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
            fd = channel.fileno()
            if fd in writable:
                channel.flush()
            if fd not in readable:
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
        self._refresh_fds()
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
            self._queues = (Virtqueue(), Virtqueue())
            self._interrupts.clear()
        self._memory = GuestMemory(mapping, guest, user)

    def _queue(self, index: int) -> Virtqueue:
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
