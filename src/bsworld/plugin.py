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
"""pytest plugin entry point: registers CLI flags and the `topo` fixture."""

from __future__ import annotations

import hashlib
import re
from functools import partial
from pathlib import Path

import pytest
import pytest_asyncio

from bsworld.runtime.scheduler import PYTEST_MAX_STEP_US, BsimEventLoop

from .env import Env, resolve_run_identity
from .topology import BluetoothTopology


def pytest_asyncio_loop_factories(config, item):  # noqa: ANN001, ARG001
    """Run every async test on a loop that can switch to BabbleSim time."""

    return {"bsim": partial(BsimEventLoop, PYTEST_MAX_STEP_US)}


def pytest_addoption(parser: pytest.Parser) -> None:
    import argparse

    group = parser.getgroup("bsworld")

    def _add(*args, **kwargs):
        try:
            group.addoption(*args, **kwargs)
        except argparse.ArgumentError:
            pass

    _add(
        "--out-dir",
        default=None,
        help="base output directory; run-<timestamp>/<uid> is appended (default: /tmp)",
    )


def pytest_configure(config: pytest.Config) -> None:
    # An xdist worker inherits the controller's identity so every worker in a
    # run writes into the same output directory.
    worker_input = getattr(config, "workerinput", None)
    if worker_input and "bsworld_out_dir" in worker_input:
        base_dir = worker_input["bsworld_out_dir"]
        now_tag = worker_input["bsworld_now_tag"]
        uid = worker_input["bsworld_run_uid"]
    else:
        base_dir, now_tag, uid = resolve_run_identity(
            out_dir=config.getoption("--out-dir", default=None)
        )
    config._bsworld_out_dir = base_dir
    config._bsworld_now_tag = now_tag
    config._bsworld_run_uid = uid


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node) -> None:  # xdist controller hook
    node.workerinput["bsworld_out_dir"] = node.config._bsworld_out_dir
    node.workerinput["bsworld_now_tag"] = node.config._bsworld_now_tag
    node.workerinput["bsworld_run_uid"] = node.config._bsworld_run_uid


def pytest_sessionstart(session: pytest.Session) -> None:
    if getattr(session.config, "workerinput", None):
        return

    out_dir = (
        Path(session.config._bsworld_out_dir)
        / session.config._bsworld_now_tag
        / session.config._bsworld_run_uid
    )
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    if tr is not None:
        tr.write_line(f"[bsworld] log dir: {out_dir}", bold=True, green=True)


@pytest.fixture(scope="session")
def bsworld_env(request: pytest.FixtureRequest) -> Env:
    """Environment shared by every test in the session.

    Built from the identity resolved in pytest_configure so all xdist workers
    write into the same output directory, even when a worker starts later.
    """
    cfg = request.config
    return Env.detect(
        out_dir=cfg._bsworld_out_dir,
        now_tag=cfg._bsworld_now_tag,
        run_uid=cfg._bsworld_run_uid,
    )


@pytest_asyncio.fixture
async def topo(
    bsworld_env: Env,
    request: pytest.FixtureRequest,
):
    t = BluetoothTopology(
        bsworld_env,
        sim_id=_sim_id_for_node(request.node.nodeid, bsworld_env.run_uid),
        test_log_dir=_test_log_dir_for_node(request.node.nodeid, bsworld_env.log_dir),
    )
    try:
        yield t
    finally:
        await t.stop()


def _sim_id_for_node(nodeid: str, run_uid: str) -> str:
    case_hash = hashlib.md5(nodeid.encode()).hexdigest()[:8]
    return f"bsw_{run_uid}_{case_hash}"


def _test_log_dir_for_node(nodeid: str, log_dir: Path) -> Path:
    # nodeid: tests/integration/uml/nuttx/test_foo.py::test_bar
    # -> log_dir / tests/integration/uml/nuttx/test_foo/test_bar
    file_part, _, func_part = nodeid.partition("::")
    parts = Path(file_part).with_suffix("").parts
    func_safe = re.sub(r"[^A-Za-z0-9_]+", "_", func_part).strip("_") or "test"
    return log_dir.joinpath(*parts, func_safe)
