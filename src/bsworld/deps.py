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
"""Validate BabbleSim build outputs before spawning a simulation."""

from __future__ import annotations

import os

from .env import Env

_BABBLESIM_OUTPUTS = (
    "bin/bs_2G4_phy_v1",
    "bin/bs_device_2G4_burst_interf",
    "bin/bs_device_2G4_WLAN_actmod",
    "lib/lib_2G4Channel_NtNcable.so",
    "lib/lib_2G4Channel_multiatt.so",
    "lib/lib_2G4Modem_BLE_simple.so",
)


class BSWorldSetupError(RuntimeError):
    """A required artifact is missing or not configured.

    Raised before a topology starts, so pytest reports it as an ordinary
    failure against the test that needed the artifact.
    """


def check_babblesim_ready(env: Env) -> None:
    """Validate the selected BabbleSim runtime outputs."""
    if env.bsim_dir is None:
        raise BSWorldSetupError(
            (
                "Set BSIM_OUT_PATH to the BabbleSim output directory containing bin/ "
                "and lib/. See README.md."
            )
        )
    missing = [
        str(env.bsim_dir / rel)
        for rel in _BABBLESIM_OUTPUTS
        if not (env.bsim_dir / rel).is_file()
        or (rel.startswith("bin/") and not os.access(env.bsim_dir / rel, os.X_OK))
    ]
    if missing:
        raise BSWorldSetupError(
            "BabbleSim build outputs are missing.\nMissing:\n    "
            + "\n    ".join(missing)
            + "\nFollow the repo-based BabbleSim setup in README.md#build-and-test, "
            "then set BSIM_OUT_PATH to the build output directory "
            f"containing bin/ and lib/ (currently {env.bsim_dir})."
        )


__all__ = [
    "BSWorldSetupError",
    "check_babblesim_ready",
]
