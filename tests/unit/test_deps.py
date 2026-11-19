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
"""Readiness checks for artifacts built outside pytest."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bsworld import deps


def _bsim_env(tmp_path: Path) -> SimpleNamespace:
    bsim_dir = tmp_path / "build" / "babblesim" / "bsim"
    return SimpleNamespace(bsim_dir=bsim_dir, bsim_bin_dir=bsim_dir / "bin")


def _write_bsim_outputs(env: SimpleNamespace) -> None:
    for rel in deps._BABBLESIM_OUTPUTS:
        path = env.bsim_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("output\n")
        if rel.startswith("bin/"):
            path.chmod(0o755)


def test_babblesim_check_accepts_runtime_without_source(tmp_path) -> None:
    env = _bsim_env(tmp_path)
    _write_bsim_outputs(env)

    # Only the selected build output matters; no source workspace is consulted.
    deps.check_babblesim_ready(env)


@pytest.mark.parametrize(
    "break_output",
    [
        lambda env: (env.bsim_dir / "lib" / "lib_2G4Channel_multiatt.so").unlink(),
        lambda env: (env.bsim_bin_dir / "bs_2G4_phy_v1").unlink(),
        lambda env: (env.bsim_bin_dir / "bs_2G4_phy_v1").chmod(0o644),
    ],
    ids=["missing-radio-module", "missing-phy", "non-executable-phy"],
)
def test_babblesim_check_names_the_unusable_output(tmp_path, break_output) -> None:
    env = _bsim_env(tmp_path)
    _write_bsim_outputs(env)
    break_output(env)

    with pytest.raises(
        deps.BSWorldSetupError, match="BabbleSim build outputs are missing"
    ):
        deps.check_babblesim_ready(env)


def test_babblesim_check_rejects_unset_path() -> None:
    with pytest.raises(deps.BSWorldSetupError, match="BSIM_OUT_PATH"):
        deps.check_babblesim_ready(SimpleNamespace(bsim_dir=None))


def test_setup_errors_are_ordinary_failures_not_interpreter_exits() -> None:
    # A SystemExit raised inside a test is recorded as a plain failure, so the
    # readiness checks use an exception pytest can report and tests can assert.
    assert issubclass(deps.BSWorldSetupError, RuntimeError)
    assert not issubclass(deps.BSWorldSetupError, SystemExit)
