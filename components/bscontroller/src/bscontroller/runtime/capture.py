# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Simulation-wide capture of Controller transmissions."""

from pathlib import Path
from threading import Lock
from typing import Self

from bscontroller.radio.base import AirPacket
from bscontroller.radio.capture import write_ellisys


class EllisysCapture:
    """Collect TX from several workers and write a sorted trace on close.

    Close the capture after stopping all Controllers that use it.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._packets: list[AirPacket] | None = []
        self._lock = Lock()

    def record_tx(self, packet: AirPacket) -> None:
        with self._lock:
            if self._packets is None:
                raise RuntimeError('Ellisys capture is closed')
            self._packets.append(packet)

    def close(self) -> None:
        with self._lock:
            if self._packets is None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('w', encoding='ascii', newline='\n') as output:
                write_ellisys(self._packets, output)
            self._packets = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
