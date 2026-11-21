# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Python bindings for one UML vhost-user device."""

from __future__ import annotations

import ctypes
import errno
import os
import select
from collections.abc import Collection
from functools import cache
from pathlib import Path

from bsworld.deps import BSWorldSetupError


class _PollFD(ctypes.Structure):
    _fields_ = [
        ("fd", ctypes.c_int),
        ("events", ctypes.c_short),
        ("revents", ctypes.c_short),
    ]


class _State(ctypes.Structure):
    _fields_ = [
        ("interrupt_ready", ctypes.c_bool),
        ("interrupt_pending", ctypes.c_bool),
    ]


@cache
def _load_library(path: Path) -> ctypes.CDLL:
    try:
        library = ctypes.CDLL(str(path), use_errno=True)
        open_device = library.bsw_vhost_open
    except (AttributeError, OSError) as error:
        raise BSWorldSetupError(
            f"cannot load UML vhost library {path}. "
            "Build it with make -C components/uml and set BSWORLD_UML_VHOST_LIBRARY."
        ) from error

    pointer = ctypes.c_void_p
    size = ctypes.c_size_t
    signatures = (
        (open_device, (ctypes.c_char_p, ctypes.c_uint), pointer),
        (library.bsw_vhost_close, (pointer,), None),
        (
            library.bsw_vhost_poll,
            (pointer, ctypes.POINTER(_PollFD), ctypes.POINTER(_State)),
            size,
        ),
        (
            library.bsw_vhost_service,
            (pointer, ctypes.POINTER(_PollFD), size),
            ctypes.c_int,
        ),
        (
            library.bsw_vhost_read,
            (pointer, pointer, size, ctypes.POINTER(size)),
            ctypes.c_ssize_t,
        ),
        (library.bsw_vhost_write, (pointer, pointer, size), ctypes.c_ssize_t),
        (library.bsw_vhost_deliver_interrupts, (pointer,), ctypes.c_int),
        (library.bsw_vhost_error, (pointer,), ctypes.c_char_p),
    )
    for function, arguments, result in signatures:
        function.argtypes = arguments
        function.restype = result
    return library


class VhostDevice:
    """A native endpoint with cached FD interests and deferred notifications."""

    def __init__(self, path: Path, device_id: int, library: Path) -> None:
        self.path = path
        self.device_id = device_id
        self._library = _load_library(library)
        self._handle: int | None = None
        self._polls = (_PollFD * 3)()
        self._count = 0
        self._state = _State()
        self._buffer = ctypes.create_string_buffer(256)
        self._required = ctypes.c_size_t()
        self.read_fds: tuple[int, ...] = ()
        self.write_fds: tuple[int, ...] = ()
        self._watched: frozenset[int] = frozenset()

    def listen(self) -> None:
        if self._handle is not None:
            raise RuntimeError("UML vhost device is already open")
        handle = self._library.bsw_vhost_open(os.fsencode(self.path), self.device_id)
        if handle is None:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(self.path))
        self._handle = handle
        try:
            self._refresh()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._handle is not None:
            self._library.bsw_vhost_close(self._handle)
            self._handle = None
        self.read_fds = self.write_fds = ()
        self._watched = frozenset()
        self._state = _State()

    @property
    def has_notifications(self) -> bool:
        return self._state.interrupt_ready

    @property
    def notifications_pending(self) -> bool:
        return self._state.interrupt_pending

    def service(self, readable: Collection[int], writable: Collection[int]) -> bool:
        """Service ready sockets and report new guest output."""
        if self._watched.isdisjoint(readable) and self._watched.isdisjoint(writable):
            return False
        for index in range(self._count):
            poll = self._polls[index]
            poll.revents = (select.POLLIN if poll.fd in readable else 0) | (
                select.POLLOUT if poll.fd in writable else 0
            )
        result = self._library.bsw_vhost_service(self._handle, self._polls, self._count)
        if result < 0:
            self._raise_error(ctypes.get_errno())
        self._refresh()
        return bool(result)

    def read(self) -> bytes | None:
        """Read one guest output buffer, if available."""
        assert self._handle is not None
        while True:
            length = self._library.bsw_vhost_read(
                self._handle,
                self._buffer,
                len(self._buffer),
                ctypes.byref(self._required),
            )
            if length >= 0:
                self._refresh_notifications()
                return self._buffer[:length]
            code = ctypes.get_errno()
            if code == errno.EAGAIN:
                return None
            if code != errno.EMSGSIZE:
                self._raise_error(code)
            self._buffer = ctypes.create_string_buffer(self._required.value)

    def write(self, data: bytes | bytearray) -> int:
        """Write to one guest input buffer and return the bytes accepted."""
        assert self._handle is not None
        if not data:
            return 0
        buffer = (
            ctypes.byref(ctypes.c_char.from_buffer(data))
            if isinstance(data, bytearray)
            else data
        )
        length = self._library.bsw_vhost_write(self._handle, buffer, len(data))
        if length < 0:
            code = ctypes.get_errno()
            if code == errno.EAGAIN:
                return 0
            self._raise_error(code)
        self._refresh_notifications()
        return length

    def flush_notifications(self) -> None:
        if self.has_notifications:
            if self._library.bsw_vhost_deliver_interrupts(self._handle) < 0:
                self._raise_error(ctypes.get_errno())
            self._refresh()

    def _refresh_notifications(self) -> None:
        self._library.bsw_vhost_poll(self._handle, None, ctypes.byref(self._state))

    def _refresh(self) -> None:
        self._count = self._library.bsw_vhost_poll(
            self._handle, self._polls, ctypes.byref(self._state)
        )
        polls = self._polls[: self._count]
        self.read_fds = tuple(poll.fd for poll in polls if poll.events & select.POLLIN)
        self.write_fds = tuple(
            poll.fd for poll in polls if poll.events & select.POLLOUT
        )
        self._watched = frozenset(self.read_fds + self.write_fds)

    def _raise_error(self, code: int) -> None:
        message = self._library.bsw_vhost_error(self._handle)
        detail = os.fsdecode(message) if message else os.strerror(code)
        if code == errno.EPIPE:
            raise EOFError(detail)
        raise OSError(code, detail)
