# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Bridge a UML guest's Virtio Bluetooth and console queues."""

from __future__ import annotations

import socket
from collections import deque
from collections.abc import Callable, Collection
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from bscontroller.hci import H4Parser

from .vhost import VhostUserBackend

if TYPE_CHECKING:
    from bsworld.runtime.pipe import LogCapture


# Device IDs and queue indices from the Virtio Bluetooth and console layouts.
_BLUETOOTH_ID = 40
_CONSOLE_ID = 3
_BT_TX, _BT_RX = 0, 1
_CONSOLE_RX, _CONSOLE_TX = 0, 1
# A primary Controller, with no vendor or Microsoft HCI extension.
_BT_CONFIG = bytes(5)


class GuestVirtio:
    """Two vhost-user endpoints coupled to the guest's calendar turn."""

    def __init__(
        self, state_dir: Path, name: str, hci_path: Path, log: LogCapture
    ) -> None:
        self._bluetooth = VhostUserBackend(state_dir / f"{name}.bt", config=_BT_CONFIG)
        self._console = VhostUserBackend(state_dir / f"{name}.con")
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
            rollback.callback(self._bluetooth.close)
            self._bluetooth.listen()
            rollback.callback(self._console.close)
            self._console.listen()
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
        """Virtio descriptors that need servicing during the guest's turn."""
        return (*self._bluetooth.read_fds, *self._console.read_fds)

    @property
    def read_fds(self) -> tuple[int, ...]:
        # Apply socket backpressure until UML replenishes the receive buffers.
        hci_fd = (
            (self._hci.fileno(),)
            if self._hci is not None and not self._hci_input
            else ()
        )
        return (*self.transport_read_fds, *hci_fd)

    @property
    def write_fds(self) -> tuple[int, ...]:
        hci_fd = (
            (self._hci.fileno(),) if self._hci is not None and self._hci_output else ()
        )
        return (*self._bluetooth.write_fds, *self._console.write_fds, *hci_fd)

    @property
    def has_notifications(self) -> bool:
        return self._bluetooth.has_notifications or self._console.has_notifications

    @property
    def notifications_pending(self) -> bool:
        return (
            self._bluetooth.notification_pending or self._console.notification_pending
        )

    def flush_notifications(self) -> None:
        """Send completions when the calendar can schedule the resulting IRQs."""
        self._bluetooth.flush_notifications()
        self._console.flush_notifications()

    def write_console(self, payload: bytes) -> None:
        self._console_input.extend(payload)
        self._fill_console()

    def service(self, readable: Collection[int], writable: Collection[int]) -> None:
        """Drain ready I/O without advancing simulation time."""
        bt_kicks = self._bluetooth.service(readable, writable)
        if _BT_TX in bt_kicks:
            self._drain_bluetooth()
        console_kicks = self._console.service(readable, writable)
        if _CONSOLE_TX in console_kicks:
            self._drain_console()

        hci = self._hci
        if hci is not None and hci.fileno() in readable:
            self._receive_hci(hci)
        if hci is not None and hci.fileno() in writable:
            self._send_hci(hci)
        if self._hci_input:
            self._fill_bluetooth()
        if self._console_input:
            self._fill_console()

    def on_packet(self, packet: bytes) -> None:
        """Queue complete H4 packets from the Controller socket."""
        self._hci_input.append(packet)

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

    def _drain_bluetooth(self) -> None:
        while (buffer := self._bluetooth.pop(_BT_TX)) is not None:
            self._hci_output.extend(buffer.read())
            self._bluetooth.complete(_BT_TX, buffer, 0)

    def _drain_console(self) -> None:
        self._log.drain()
        while (buffer := self._console.pop(_CONSOLE_TX)) is not None:
            self._log.record(buffer.read())
            # Linux hvc spins for this used-ring entry with interrupts disabled.
            self._console.complete(_CONSOLE_TX, buffer, 0, notify=False)

    def _fill_bluetooth(self) -> None:
        while self._hci_input and (buffer := self._bluetooth.pop(_BT_RX)) is not None:
            # The driver expects one whole H4 packet per receive descriptor.
            length = buffer.write(self._hci_input.popleft())
            self._bluetooth.complete(_BT_RX, buffer, length)

    def _fill_console(self) -> None:
        while (
            self._console_input
            and (buffer := self._console.pop(_CONSOLE_RX)) is not None
        ):
            length = buffer.write(self._console_input[: buffer.capacity])
            if not length:
                raise ValueError("Virtio console has an empty input buffer")
            del self._console_input[:length]
            self._console.complete(_CONSOLE_RX, buffer, length)
