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
from tests.unit.helpers import make_env

from bsworld.backends.babblesim import BabbleSimBackend
from bsworld.interference import BurstInterference


def test_layout_assigns_controllers_clock_and_interference(tmp_path) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    first = backend.add_controller()
    second = backend.add_controller()
    jammer = backend.add_interference_source(BurstInterference(2402.0))
    layout = backend.plan_devices()

    assert layout.controller_dev_id(first) == 0
    assert layout.controller_dev_id(second) == 1
    assert layout.clock_dev_id == 2
    assert layout.interference_dev_id(jammer) == 3
    assert layout.device_count == 4
    assert layout.lock_paths(tmp_path) == [
        tmp_path / f"2G4.d{index}.lock" for index in range(4)
    ]

    with pytest.raises(FrozenInstanceError):
        layout.controllers = ()


def test_layout_resolves_declared_rf_endpoints_by_identity(tmp_path) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    first = backend.add_controller()
    second = backend.add_controller()
    jammer = backend.add_interference_source(BurstInterference(2402.0))
    layout = backend.plan_devices()

    assert layout.resolve_endpoint(7) == 7
    assert layout.resolve_endpoint(first) == 0
    assert layout.resolve_endpoint(second) == 1
    assert layout.resolve_endpoint(jammer) == 3


@pytest.mark.parametrize("endpoint", [-1, True, object()])
def test_layout_rejects_invalid_raw_endpoint(tmp_path, endpoint: object) -> None:
    layout = BabbleSimBackend(make_env(tmp_path), "sim").plan_devices()

    with pytest.raises((TypeError, ValueError), match="attenuation endpoint"):
        layout.resolve_endpoint(endpoint)


@pytest.mark.parametrize("device_type", ["controller", "interference"])
def test_layout_rejects_participants_from_another_topology(
    tmp_path, device_type
) -> None:
    local = BabbleSimBackend(make_env(tmp_path), "local")
    foreign = BabbleSimBackend(make_env(tmp_path), "foreign")
    if device_type == "controller":
        local.add_controller()
        endpoint = foreign.add_controller()
    else:
        local.add_interference_source(BurstInterference(2402.0))
        endpoint = foreign.add_interference_source(BurstInterference(2402.0))
    layout = local.plan_devices()

    with pytest.raises(ValueError, match="does not belong to this topology"):
        layout.resolve_endpoint(endpoint)
