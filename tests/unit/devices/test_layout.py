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
"""Immutable BabbleSim device identity layout tests."""

from dataclasses import FrozenInstanceError

import pytest

from bsworld.devices import BSController, ProcessInstance
from bsworld.devices.layout import AppSlot, build_device_layout
from bsworld.interference import InterferenceRef


def _controllers(tmp_path):
    return [BSController(index, "sim", tmp_path) for index in range(2)]


def _host(controller, label="host"):
    return ProcessInstance(command=["/host"], controller=controller, label=label)


def _layout(controllers, processes=(), interference_count=0):
    return build_device_layout(
        phy_id="2G4",
        sim_id="sim",
        controllers=list(controllers),
        processes=list(processes),
        interference_count=interference_count,
    )


def test_layout_assigns_mixed_participants_once(tmp_path) -> None:
    standalone, attached = _controllers(tmp_path)
    host = _host(attached)

    layout = _layout([standalone, attached], [host], 1)

    assert layout.controller_dev_id(standalone) == 0
    assert layout.controller_dev_id(attached) == 1
    assert layout.application_slot(host) == AppSlot(
        sim_id="sim",
        phy_id="2G4",
        dev_id=2,
        hci_sock=tmp_path / "hci1.sock",
    )
    assert layout.clock_dev_id == 3
    assert layout.interference_dev_id(0) == 4
    assert layout.device_count == 5
    assert layout.lock_paths(tmp_path) == [
        tmp_path / f"2G4.d{index}.lock" for index in range(5)
    ]

    with pytest.raises(KeyError, match="no Application device reserved"):
        layout.application_slot(_host(standalone, "unregistered"))
    with pytest.raises(FrozenInstanceError):
        layout.clock_dev_id = 9
    with pytest.raises(TypeError):
        layout.application_slots[0] = None


def test_layout_orders_hosts_by_their_controllers(tmp_path) -> None:
    first, second = _controllers(tmp_path)
    late = _host(first, "late")
    early = _host(second, "early")

    layout = _layout([first, second], [early, late])

    assert layout.application_slot(late).dev_id == 2
    assert layout.application_slot(early).dev_id == 3


def test_layout_rejects_a_host_waiting_for_its_controller(tmp_path) -> None:
    host = ProcessInstance(command=["/host"], label="host")

    with pytest.raises(ValueError, match="host: HCI Host has no Controller"):
        _layout(_controllers(tmp_path), [host])


@pytest.mark.parametrize("problem", ["foreign", "shared"])
def test_layout_rejects_inconsistent_attachments(tmp_path, problem) -> None:
    local, other = _controllers(tmp_path)
    if problem == "foreign":
        processes = [_host(other)]
        reason = "does not belong"
    else:
        processes = [_host(local, "first"), _host(local, "second")]
        reason = "only be attached to one"

    with pytest.raises(ValueError, match=reason):
        _layout([local], processes)


def test_layout_resolves_declared_rf_endpoints_by_identity(tmp_path) -> None:
    first, second = _controllers(tmp_path)
    process = _host(second, "device")
    layout = _layout([first, second], [process], 1)

    assert layout.resolve_endpoint(7) == 7
    assert layout.resolve_endpoint(first) == 0
    assert layout.resolve_endpoint(process) == 1
    assert layout.resolve_endpoint(InterferenceRef(0)) == 4


@pytest.mark.parametrize("index", [-1, 1])
def test_layout_rejects_unknown_interference(index, tmp_path) -> None:
    layout = _layout(_controllers(tmp_path), interference_count=1)

    with pytest.raises(ValueError, match="unknown interference"):
        layout.resolve_endpoint(InterferenceRef(index))


@pytest.mark.parametrize("endpoint", [-1, True, object()])
def test_layout_rejects_invalid_raw_endpoint(tmp_path, endpoint: object) -> None:
    layout = _layout(_controllers(tmp_path))

    with pytest.raises((TypeError, ValueError), match="attenuation endpoint"):
        layout.resolve_endpoint(endpoint)


def test_layout_rejects_participants_from_another_topology(tmp_path) -> None:
    local, foreign = _controllers(tmp_path)
    layout = _layout([local])

    with pytest.raises(ValueError, match="does not belong to this topology"):
        layout.resolve_endpoint(foreign)
    with pytest.raises(ValueError, match="does not belong to this topology"):
        layout.resolve_endpoint(_host(local, "unregistered"))
