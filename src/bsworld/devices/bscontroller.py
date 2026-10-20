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

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..env import Env
from ..runtime.proc import ProcessManager
from .controller import Controller

if TYPE_CHECKING:
    from bscontroller import RadioController


@dataclass(eq=False, repr=False)
class BSController(Controller):
    max_step_us: int | None = None
    _worker: RadioController | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.max_step_us is not None and self.max_step_us <= 0:
            raise ValueError("max_step_us must be positive")

    async def start(self, pm: ProcessManager, env: Env, *, log_dir: Path) -> None:
        from bscontroller import RadioController

        if self._worker is not None:
            raise RuntimeError("BSController is already started")
        self.hci_snoop_file = (
            self.hci_snoop_file or log_dir / "bscontroller" / f"hci{self.controller_dev_id}.snoop"
        ).resolve()
        options = {"hci_snoop_file": self.hci_snoop_file, "hci_socket": self.hci_sock}
        if self.max_step_us is not None:
            options["max_step_us"] = self.max_step_us
        self._worker = RadioController(
            f"hci{self.controller_dev_id}",
            self.public_address or (
                f"F0:F1:F2:{(self.controller_dev_id >> 16) & 255:02X}:"
                f"{(self.controller_dev_id >> 8) & 255:02X}:{self.controller_dev_id & 255:02X}"
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

    async def stop(self) -> None:
        if self._worker is not None:
            try:
                await self._worker.close()
            finally:
                self._worker = None
