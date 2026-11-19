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
from __future__ import annotations

from pathlib import Path

import pytest

from bsworld.env import Env, env_path


def test_detect_uses_explicit_babblesim_path(monkeypatch, tmp_path) -> None:
    bsim_dir = tmp_path / "BabbleSim output"
    monkeypatch.setenv("BSIM_OUT_PATH", str(bsim_dir))
    env = Env.detect(out_dir="/tmp/out", now_tag="run-fixed", run_uid="uid")

    assert env.bsim_dir == bsim_dir
    assert env.bsim_bin_dir == bsim_dir / "bin"


def test_detect_selects_image_runtime(monkeypatch, tmp_path) -> None:
    output = tmp_path / "image" / "babblesim"
    monkeypatch.setenv("BSIM_OUT_PATH", str(output))

    env = Env.detect()

    assert env.bsim_dir == output
    assert env.bsim_bin_dir == output / "bin"


def test_detect_does_not_guess_missing_artifacts(monkeypatch) -> None:
    monkeypatch.delenv("BSIM_OUT_PATH", raising=False)
    env = Env.detect()

    assert env.bsim_dir is None
    assert env.bsim_bin_dir is None


def test_env_path_expands_home_and_resolves_relative_paths(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("BSWORLD_NUTTX_BTSAK_BIN", "~/my project/nuttx")
    assert env_path("BSWORLD_NUTTX_BTSAK_BIN") == Path.home() / "my project" / "nuttx"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BSWORLD_NUTTX_BTSAK_BIN", "build/nuttx")
    assert env_path("BSWORLD_NUTTX_BTSAK_BIN") == tmp_path / "build" / "nuttx"


@pytest.mark.parametrize("environment", [False, True], ids=["option", "environment"])
def test_output_root_is_absolute_before_starting_child_processes(
    monkeypatch, tmp_path, environment
):
    monkeypatch.chdir(tmp_path)
    if environment:
        monkeypatch.setenv("OUT_DIR", "relative results")
    env = Env.detect(
        out_dir=None if environment else "relative results",
        now_tag="run-fixed",
        run_uid="uid",
    )
    assert env.out_dir == tmp_path / "relative results" / "run-fixed" / "uid"
    monkeypatch.chdir(tmp_path.parent)
    assert env.log_dir == tmp_path / "relative results" / "run-fixed" / "uid" / "log"
