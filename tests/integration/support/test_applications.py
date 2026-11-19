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
"""The registration flow every integration suite shares."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.integration.applications import create_application, require_artifact

from bsworld.devices.layout import AppSlot
from bsworld.env import Env
from bsworld.topology import BluetoothTopology

_GUIDE = "docs/writing-tests.md"


def _env(tmp_path: Path) -> Env:
    return Env(
        run_uid="uid",
        out_dir=tmp_path / "out",
        log_dir=tmp_path / "out" / "log",
        bsim_dir=tmp_path / "bsim",
        bs_state_dir=tmp_path / "state",
    )


def _executable(tmp_path: Path, name: str = "firmware") -> Path:
    binary = tmp_path / name
    binary.write_text("native output")
    binary.chmod(0o755)
    return binary


def _argv(binary: Path, slot: AppSlot) -> list[str | Path]:
    return [binary, f"--dev={slot.dev_id}", f"--sim={slot.sim_id}", slot.hci_sock]


def test_application_can_attach_its_controller_after_registration(
    tmp_path, monkeypatch
):
    binary = _executable(tmp_path)
    monkeypatch.setenv("BSWORLD_TEST_BIN", str(binary))
    topo = BluetoothTopology(_env(tmp_path), "sim0", tmp_path / "log")
    process = create_application(
        topo,
        binary_env="BSWORLD_TEST_BIN",
        guide=_GUIDE,
        argv=_argv,
        label="device",
        log_subdir="devices/device",
    )
    assert process.controller is None
    assert topo.processes == (process,)
    assert topo.bsim.controllers == []

    controller = topo.add_controller(public_address="c0:ff:ee:00:00:09")
    assert process.attach(controller) is process
    assert process.controller is controller
    assert topo.bsim.controllers == [controller]
    slot = topo.bsim.plan_devices().application_slot(process)
    assert [str(part) for part in process._resolve_command(slot)] == [
        str(binary),
        "--dev=1",
        "--sim=sim0",
        str(tmp_path / "state" / "sim0" / "hci0.sock"),
    ]


def test_missing_application_skips_before_creating_a_process(
    topo, tmp_path, monkeypatch
):
    monkeypatch.delenv("BSWORLD_TEST_BIN", raising=False)
    with pytest.raises(pytest.skip.Exception, match="BSWORLD_TEST_BIN"):
        create_application(
            topo,
            binary_env="BSWORLD_TEST_BIN",
            guide=_GUIDE,
            argv=_argv,
            label="device",
        )


def test_host_only_program_without_attachment_fails_before_the_phy_starts(
    topo, tmp_path
):
    process = create_application(
        topo, binary=_executable(tmp_path), argv=_argv, label="host"
    )
    with pytest.raises(ValueError, match="host: HCI Host has no Controller"):
        topo.bsim.plan_devices()

    # The rejected plan is not frozen, so the test can still attach and continue.
    process.attach(topo.add_controller())
    assert topo.bsim.plan_devices().application_slot(process).dev_id == 1


def test_a_prebuilt_binary_bypasses_the_environment_lookup(topo, tmp_path, monkeypatch):
    binary = _executable(tmp_path)
    monkeypatch.delenv("BSWORLD_TEST_BIN", raising=False)
    process = create_application(
        topo, binary=binary, argv=_argv, label="device", shell=True
    )
    assert process.shell.enabled
    slot = AppSlot(sim_id="sim", phy_id="2G4", dev_id=1, hci_sock=tmp_path / "hci.sock")
    assert process._resolve_command(slot)[0] == binary


@pytest.mark.parametrize(
    "kwargs", [{}, {"binary": Path("/bin/true"), "binary_env": "BSWORLD_TEST_BIN"}]
)
def test_exactly_one_binary_source_is_required(topo, kwargs):
    with pytest.raises(TypeError, match="exactly one of binary or binary_env"):
        create_application(topo, argv=_argv, label="device", guide=_GUIDE, **kwargs)


def test_an_environment_selected_binary_requires_its_guide(topo):
    with pytest.raises(TypeError, match="requires guide"):
        create_application(
            topo, binary_env="BSWORLD_TEST_BIN", argv=_argv, label="device"
        )


@pytest.mark.parametrize("executable", [False, True])
def test_artifacts_are_resolved_on_every_call(
    tmp_path, monkeypatch, executable
) -> None:
    artifact = tmp_path / "artifact"
    artifact.write_text("output")
    monkeypatch.setenv("BSWORLD_TEST_BIN", str(artifact))

    if executable:
        with pytest.raises(pytest.skip.Exception, match="compiled executable"):
            require_artifact("BSWORLD_TEST_BIN", guide=_GUIDE)
        artifact.chmod(0o755)
    assert (
        require_artifact("BSWORLD_TEST_BIN", guide=_GUIDE, executable=executable)
        == artifact
    )

    # Reselecting the artifact takes effect without reimporting any profile.
    monkeypatch.setenv("BSWORLD_TEST_BIN", str(tmp_path / "missing"))
    with pytest.raises(pytest.skip.Exception, match="BSWORLD_TEST_BIN"):
        require_artifact("BSWORLD_TEST_BIN", guide=_GUIDE, executable=executable)
