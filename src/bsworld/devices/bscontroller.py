# Copyright 2026 Xiaomi Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Python Controller with a dedicated radio worker thread and an H:4 endpoint."""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field
from pathlib import Path
from string import hexdigits
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bscontroller import ControllerConfig, EllisysCapture, RadioController


def _normalize_controller_address(address: str) -> str:
    parts = address.split(":")
    if len(parts) != 6 or any(
        len(part) != 2 or any(char not in hexdigits for char in part) for part in parts
    ):
        raise ValueError("controller address must use AA:BB:CC:DD:EE:FF format")
    if all(part == "00" for part in parts):
        raise ValueError("controller address must not be all zero")
    return address.upper()


@dataclass(eq=False, repr=False)
class BSController:
    """One PHY participant exposing an H:4 socket to its Bluetooth Host.

    ``config`` is a ``bscontroller.ControllerConfig`` with this Controller's
    Controller-defined values (buffers, list sizes, limits).
    """

    controller_dev_id: int
    sim_id: str
    socket_dir: Path
    public_address: str | None = None
    hci_snoop_file: Path | None = None
    max_step_us: int | None = None
    config: ControllerConfig | None = None
    rf_pcap_file: Path | None = None
    _worker: RadioController | None = field(default=None, init=False, repr=False)
    _closing_hci: dict[socket.socket, asyncio.AbstractEventLoop] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.public_address is not None:
            self.public_address = _normalize_controller_address(self.public_address)
        if self.max_step_us is not None and self.max_step_us <= 0:
            raise ValueError("max_step_us must be positive")
        if self.config is not None:
            from bscontroller import ControllerConfig

            if not isinstance(self.config, ControllerConfig):
                raise TypeError("config must be a bscontroller.ControllerConfig")

    def __repr__(self) -> str:
        return f"<BSController dev{self.controller_dev_id}>"

    @property
    def hci_sock(self) -> Path:
        return self.socket_dir / f"hci{self.controller_dev_id}.sock"

    async def start(self, *, ellisys_capture: EllisysCapture | None = None) -> None:
        """Launch the worker while the backend brings up other PHY participants."""
        from bscontroller import RadioController

        if self._worker is not None:
            raise RuntimeError("BSController is already started")
        if self.hci_snoop_file is None:
            raise RuntimeError(
                "hci_snoop_file is assigned when the Controller is allocated"
            )
        name = f"hci{self.controller_dev_id}"
        options = {"hci_snoop_file": self.hci_snoop_file, "hci_socket": self.hci_sock}
        if self.rf_pcap_file is not None:
            options["rf_pcap_file"] = self.rf_pcap_file
        if ellisys_capture is not None:
            options["ellisys_capture"] = ellisys_capture
        if self.max_step_us is not None:
            options["max_step_us"] = self.max_step_us
        if self.config is not None:
            options["config"] = self.config
        self._worker = RadioController(
            name,
            self.public_address
            or (
                f"F0:F1:F2:{(self.controller_dev_id >> 16) & 255:02X}:"
                f"{(self.controller_dev_id >> 8) & 255:02X}:"
                f"{self.controller_dev_id & 255:02X}"
            ),
            **options,
        )
        try:
            await self._worker.start(self.sim_id, self.controller_dev_id)
        except BaseException:
            try:
                await self._worker.close()
            finally:
                self._worker = None
            raise

    def _disconnect_hci(self, hci: socket.socket) -> None:
        """End host input and drain Controller output until its endpoint closes."""
        try:
            hci.shutdown(socket.SHUT_WR)
        except OSError:
            hci.close()
            return

        try:
            loop = asyncio.get_running_loop()
            loop.add_reader(hci, self._drain_hci, hci)
        except BaseException:
            hci.close()
            raise
        self._closing_hci[hci] = loop

    def _drain_hci(self, hci: socket.socket) -> None:
        try:
            if hci.recv(65536):
                return
        except BlockingIOError:
            return
        except OSError:
            pass
        self._close_hci(hci)

    def _close_hci(self, hci: socket.socket) -> None:
        loop = self._closing_hci.pop(hci)
        if not loop.is_closed():
            loop.remove_reader(hci)
        hci.close()

    async def stop(self) -> None:
        """Release the worker and its transport resources."""
        try:
            if self._worker is not None:
                await self._worker.close()
        finally:
            self._worker = None
            for hci in tuple(self._closing_hci):
                self._close_hci(hci)
