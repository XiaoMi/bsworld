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
"""Path / env resolution for bsworld.

A single Env instance is created per pytest run and threaded through every
backend, build, and runtime module.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bscontroller.runtime import simulation_root


def env_path(name: str) -> Path | None:
    """Resolve a configured artifact without assuming a build location."""
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else None


def _now_tag() -> str:
    return "run-" + datetime.now().strftime("%Y%m%d_%H%M%S")


def _run_uid() -> str:
    return uuid.uuid4().hex[:8]


def resolve_run_identity(
    *,
    out_dir: str | None = None,
    now_tag: str | None = None,
    run_uid: str | None = None,
) -> tuple[str, str, str]:
    """Resolve (base output dir, run tag, run uid) for one test session.

    Explicit arguments win, then OUT_DIR and RUN_UID, then freshly generated
    values. Resolved in one place so the plugin and Env cannot disagree about
    where a run writes its artifacts.
    """
    base_dir = out_dir or os.environ.get("OUT_DIR") or "/tmp"
    return (
        str(Path(base_dir).expanduser().resolve()),
        now_tag or _now_tag(),
        run_uid or os.environ.get("RUN_UID") or _run_uid(),
    )


@dataclass(frozen=True)
class Env:
    run_uid: str
    out_dir: Path
    log_dir: Path

    bsim_dir: Path | None

    bs_state_dir: Path

    @property
    def bsim_bin_dir(self) -> Path | None:
        return self.bsim_dir / "bin" if self.bsim_dir else None

    @classmethod
    def detect(
        cls,
        *,
        out_dir: str | Path | None = None,
        now_tag: str | None = None,
        run_uid: str | None = None,
    ) -> "Env":
        """Resolve an environment, defaulting the run identity from os.environ.

        out_dir is the base output directory; each run writes beneath
        <out_dir>/<now_tag>/<run_uid>. The pytest plugin resolves the identity
        once per session and passes it in, so every xdist worker shares one
        output directory.
        """
        base_dir, resolved_tag, uid = resolve_run_identity(
            out_dir=str(out_dir) if out_dir is not None else None,
            now_tag=now_tag,
            run_uid=run_uid,
        )
        run_dir = Path(base_dir) / resolved_tag / uid
        log_dir = run_dir / "log"

        bsim_dir = env_path("BSIM_OUT_PATH")

        return cls(
            run_uid=uid,
            out_dir=run_dir,
            log_dir=log_dir,
            bsim_dir=bsim_dir,
            # Native layout shared with the Controllers; bscontroller owns it.
            bs_state_dir=simulation_root(),
        )

    def sim_dir(self, sim_id: str) -> Path:
        return self.bs_state_dir / sim_id
