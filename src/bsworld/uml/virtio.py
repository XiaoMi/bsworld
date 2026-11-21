# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connect UML Bluetooth and console devices to BSWorld."""

from __future__ import annotations

import socket
from collections import deque
from collections.abc import Callable, Collection
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from bscontroller.hci.transport import H4Parser

from .vhost import VhostDevice

if TYPE_CHECKING:
    from bsworld.runtime.pipe import LogCapture


_BLUETOOTH_ID = 40
_CONSOLE_ID = 3


class GuestVirtio:
    """Bridge a guest's HCI and console traffic during its calendar turn."""

    def __init__(
        self, state_dir: Path, name: str, hci_path: Path, log: LogCapture, library: Path
    ) -> None:
        self._bluetooth = VhostDevice(state_dir / f"{name}.bt", _BLUETOOTH_ID, library)
        self._console = VhostDevice(state_dir / f"{name}.con", _CONSOLE_ID, library)
        self._hci_path = hci_path
        self._log = log
        self._hci: socket.socket | None = None
        self._h4 = H4Parser(self)
        self._hci_input: deque[bytes] = deque()
        self._hci_output = bytearray()
        self._console_input = bytearray()

    @property
    def arguments(self) -> tuple[str, str]:
        return (
            f"virtio_uml.device={self._bluetooth.path.name}:{_BLUETOOTH_ID}",
            f"virtio_uml.device={self._console.path.name}:{_CONSOLE_ID}",
        )

    def listen(self) -> None:
        with ExitStack() as rollback:
            self._bluetooth.listen()
            rollback.callback(self._bluetooth.close)
            self._console.listen()
            rollback.callback(self._console.close)
            hci = rollback.enter_context(
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            )
            hci.connect(str(self._hci_path))
            hci.setblocking(False)
            self._hci = hci
            rollback.pop_all()

    def close(self, *, disconnect_hci: Callable[[socket.socket], None]) -> None:
        with ExitStack() as cleanup:
            if self._hci is not None:
                cleanup.callback(disconnect_hci, self._hci)
                self._hci = None
            cleanup.callback(self._bluetooth.close)
            cleanup.callback(self._console.close)

    @property
    def transport_read_fds(self) -> tuple[int, ...]:
        """Device descriptors serviced while the guest owns the clock."""
        return (*self._bluetooth.read_fds, *self._console.read_fds)

    @property
    def read_fds(self) -> tuple[int, ...]:
        # Apply socket backpressure until the guest can accept more packets.
        if self._hci is not None and not self._hci_input:
            return (*self.transport_read_fds, self._hci.fileno())
        return self.transport_read_fds

    @property
    def write_fds(self) -> tuple[int, ...]:
        fds = (*self._bluetooth.write_fds, *self._console.write_fds)
        if self._hci is not None and self._hci_output:
            return (*fds, self._hci.fileno())
        return fds

    @property
    def has_notifications(self) -> bool:
        return self._bluetooth.has_notifications or self._console.has_notifications

    @property
    def notifications_pending(self) -> bool:
        return (
            self._bluetooth.notifications_pending or self._console.notifications_pending
        )

    def flush_notifications(self) -> None:
        self._bluetooth.flush_notifications()
        self._console.flush_notifications()

    def write_console(self, payload: bytes) -> None:
        self._console_input.extend(payload)
        self._fill_console()

    def service(self, readable: Collection[int], writable: Collection[int]) -> None:
        """Service ready I/O without advancing simulation time."""
        if self._bluetooth.service(readable, writable):
            while (packet := self._bluetooth.read()) is not None:
                self._hci_output.extend(packet)
        if self._console.service(readable, writable):
            self._log.drain()
            while (output := self._console.read()) is not None:
                self._log.record(output)

        hci = self._hci
        if hci is not None:
            fd = hci.fileno()
            if fd in readable:
                self._receive_hci(hci)
            if fd in writable:
                self._send_hci(hci)
        while self._hci_input and self._bluetooth.write(self._hci_input[0]):
            self._hci_input.popleft()
        if self._console_input:
            self._fill_console()

    def on_packet(self, packet: bytes) -> None:
        self._hci_input.append(packet)

    def _fill_console(self) -> None:
        while self._console_input:
            length = self._console.write(self._console_input)
            if not length:
                break
            del self._console_input[:length]

    def _receive_hci(self, hci: socket.socket) -> None:
        try:
            data = hci.recv(65536)
        except BlockingIOError:
            return
        if not data:
            raise EOFError(f"Controller closed its H4 socket: {self._hci_path}")
        self._h4.feed_data(data)

    def _send_hci(self, hci: socket.socket) -> None:
        try:
            sent = hci.send(self._hci_output)
        except BlockingIOError:
            return
        if not sent:
            raise EOFError(f"Controller closed its H4 socket: {self._hci_path}")
        del self._hci_output[:sent]
