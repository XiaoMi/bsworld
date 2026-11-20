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
"""Immutable BabbleSim device identity assignment for one simulation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .bscontroller import BSController

if TYPE_CHECKING:
    from ..interference import InterferenceRef, PathEndpoint


@dataclass(frozen=True)
class DeviceLayout:
    """The dev_id assignment every participant of one simulation shares.

    PHY device ids are laid out as follows:

        [0 .. C-1]      Controller workers, in registration order
        [C]             the in-process pytest framework clock
        [C+1 ..]        interference devices

    The layout is derived once from a complete topology, so the ids it
    publishes cannot drift apart from the device count given to the PHY.
    """

    phy_id: str
    controllers: tuple[BSController, ...]
    interference_refs: tuple[InterferenceRef, ...]

    @property
    def clock_dev_id(self) -> int:
        return len(self.controllers)

    @property
    def interference_base(self) -> int:
        return self.clock_dev_id + 1

    @property
    def device_count(self) -> int:
        """Total participants to register with the PHY, including the clock."""
        return self.interference_base + len(self.interference_refs)

    def interference_dev_id(self, ref: InterferenceRef) -> int:
        for dev_id, registered in enumerate(
            self.interference_refs, self.interference_base
        ):
            if registered is ref:
                return dev_id
        raise ValueError(
            f"interference source {ref.index} does not belong to this topology"
        )

    def controller_dev_id(self, controller: BSController) -> int:
        """Resolve a Controller registered with this simulation, by identity.

        Object identity keeps Controller IDs local to this topology.
        """
        for dev_id, registered in enumerate(self.controllers):
            if registered is controller:
                return dev_id
        raise ValueError(f"{controller!r} does not belong to this topology")

    def resolve_endpoint(self, endpoint: PathEndpoint) -> int:
        """Map a device ID, Controller, or interference reference to its PHY ID."""
        from ..interference import InterferenceRef

        # bool is an int subclass, so reject it before the numeric case.
        if isinstance(endpoint, bool):
            raise TypeError(f"unsupported attenuation endpoint: {endpoint!r}")
        if isinstance(endpoint, int):
            if not 0 <= endpoint < self.device_count:
                raise ValueError(
                    f"attenuation endpoint device id {endpoint} is outside "
                    f"0..{self.device_count - 1}"
                )
            return endpoint
        if isinstance(endpoint, InterferenceRef):
            return self.interference_dev_id(endpoint)
        if isinstance(endpoint, BSController):
            return self.controller_dev_id(endpoint)
        raise TypeError(f"unsupported attenuation endpoint: {endpoint!r}")

    def lock_paths(self, sim_dir: Path) -> list[Path]:
        """PHY registration lock files, one per participant."""
        return [
            sim_dir / f"{self.phy_id}.d{dev_id}.lock"
            for dev_id in range(self.device_count)
        ]


__all__ = ["DeviceLayout"]
