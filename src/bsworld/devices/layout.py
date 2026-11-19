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

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from .bscontroller import BSController

if TYPE_CHECKING:
    from .instance import ProcessInstance


@dataclass(frozen=True)
class AppSlot:
    """Everything an Application process needs to join BabbleSim itself.

    A slot is handed to the process command factory once the topology is
    complete, so a command never has to read an identity that is still
    unassigned.
    """

    sim_id: str
    phy_id: str
    dev_id: int
    hci_sock: Path


@dataclass(frozen=True)
class DeviceLayout:
    """The dev_id assignment every participant of one simulation shares.

    PHY device ids are laid out as follows:

        [0 .. C-1]      Controller workers, in registration order
        [C .. C+A-1]    HCI Host processes, in their Controllers' order
        [C+A]           the in-process pytest framework clock
        [C+A+1 ..]      interference devices

    The layout is derived once from a complete topology, so the ids it
    publishes cannot drift apart from the device count given to the PHY.
    """

    phy_id: str
    controller_ids: Mapping[int, int]
    application_slots: Mapping[int, AppSlot]
    clock_dev_id: int
    interference_base: int
    interference_count: int

    @property
    def device_count(self) -> int:
        """Total participants to register with the PHY, including the clock."""
        return self.interference_base + self.interference_count

    def application_slot(self, process: ProcessInstance) -> AppSlot:
        """Look up a registered process by identity."""
        slot = self.application_slots.get(id(process))
        if slot is None:
            raise KeyError(f"{process!r} has no Application device reserved")
        return slot

    def interference_dev_id(self, index: int) -> int:
        if not 0 <= index < self.interference_count:
            raise ValueError(f"unknown interference source index: {index}")
        return self.interference_base + index

    def controller_dev_id(self, controller: BSController) -> int:
        """Resolve a Controller registered with this simulation, by identity.

        Object identity keeps Controller IDs local to this topology.
        """
        dev_id = self.controller_ids.get(id(controller))
        if dev_id is None:
            raise ValueError(f"{controller!r} does not belong to this topology")
        return dev_id

    def resolve_endpoint(self, endpoint) -> int:
        """Map an RF path endpoint declared by a test onto its PHY dev_id.

        Accepts a raw dev_id, an InterferenceRef, a Controller, or a registered
        process. A Host resolves to its Controller's radio.
        """
        # Imported here because bsworld.interference and .instance depend on
        # this module.
        from ..interference import InterferenceRef
        from .instance import ProcessInstance

        # bool is an int subclass, so reject it before the numeric case.
        if isinstance(endpoint, bool):
            raise TypeError(f"unsupported attenuation endpoint: {endpoint!r}")
        if isinstance(endpoint, int):
            if endpoint < 0:
                raise ValueError("attenuation endpoint device id must be non-negative")
            return endpoint
        if isinstance(endpoint, InterferenceRef):
            index = endpoint.index
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < self.interference_count
            ):
                raise ValueError(f"unknown interference source ref: {index}")
            return self.interference_base + index
        if isinstance(endpoint, BSController):
            return self.controller_dev_id(endpoint)
        if not isinstance(endpoint, ProcessInstance):
            raise TypeError(f"unsupported attenuation endpoint: {endpoint!r}")
        if id(endpoint) not in self.application_slots:
            raise ValueError(f"{endpoint.label} does not belong to this topology")
        controller = endpoint.controller
        assert controller is not None
        return self.controller_dev_id(controller)

    def lock_paths(self, sim_dir: Path) -> list[Path]:
        """PHY registration lock files, one per participant."""
        return [
            sim_dir / f"{self.phy_id}.d{dev_id}.lock"
            for dev_id in range(self.device_count)
        ]


def build_device_layout(
    *,
    phy_id: str,
    sim_id: str,
    controllers: list[BSController],
    processes: list[ProcessInstance],
    interference_count: int,
) -> DeviceLayout:
    """Assign every PHY device id for a complete topology in one pass.

    Each Host slot is keyed by its process and follows its Controller's order.
    """
    controller_ids = {
        id(controller): dev_id for dev_id, controller in enumerate(controllers)
    }
    hosts: dict[int, ProcessInstance] = {}
    for process in processes:
        controller = process.controller
        if controller is None:
            raise ValueError(
                f"{process.label}: HCI Host has no Controller. "
                "Call attach(controller) before starting the topology"
            )
        controller_dev_id = controller_ids.get(id(controller))
        if controller_dev_id is None:
            raise ValueError(f"{process.label}: Controller does not belong here")
        if controller_dev_id in hosts:
            raise ValueError(
                "a Controller can only be attached to one Application process"
            )
        hosts[controller_dev_id] = process

    slots: dict[int, AppSlot] = {}
    next_dev_id = len(controllers)
    for controller_dev_id in sorted(hosts):
        host = hosts[controller_dev_id]
        slots[id(host)] = AppSlot(
            sim_id=sim_id,
            phy_id=phy_id,
            dev_id=next_dev_id,
            hci_sock=controllers[controller_dev_id].hci_sock,
        )
        next_dev_id += 1
    return DeviceLayout(
        phy_id=phy_id,
        controller_ids=MappingProxyType(controller_ids),
        application_slots=MappingProxyType(slots),
        clock_dev_id=next_dev_id,
        interference_base=next_dev_id + 1,
        interference_count=interference_count,
    )


__all__ = ["AppSlot", "DeviceLayout", "build_device_layout"]
