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

pytest_plugins = ("pytester",)

_ISOLATED_PLUGIN_ARGS = (
    "-p",
    "pytest_asyncio.plugin",
    "-p",
    "bsworld.plugin",
)


def test_xdist_workers_share_a_run_and_isolate_test_artifacts(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("RUN_UID", raising=False)
    env_logs = pytester.path / "environment_logs"
    monkeypatch.setenv("OUT_DIR", str(env_logs))
    pytester.makepyfile(
        test_identity="""
            import asyncio

            import pytest
            from bscontroller.runtime.event_loop import RadioEventLoop

            @pytest.mark.asyncio
            @pytest.mark.parametrize("label", ["a/b", "a:b"])
            async def test_identity(topo, bsworld_env, worker_id, label):
                assert topo.env is bsworld_env
                assert isinstance(asyncio.get_running_loop(), RadioEventLoop)
                marker = topo.test_log_dir / "identity"
                marker.write_text("\\n".join((worker_id, topo.sim_id, label)))

            @pytest.mark.asyncio
            async def test_ordinary_path(topo, bsworld_env):
                assert topo.test_log_dir == (
                    bsworld_env.log_dir / "test_identity" / "test_ordinary_path"
                )
        """
    )

    result = pytester.runpytest_subprocess(
        *_ISOLATED_PLUGIN_ARGS,
        "-p",
        "xdist.plugin",
        "-n",
        "2",
        "--dist=load",
        "-o",
        "asyncio_mode=strict",
        "--out-dir=logs",
        "--color=no",
        "-q",
    )

    result.assert_outcomes(passed=3)
    assert not env_logs.exists()
    [run_dir] = (pytester.path / "logs").glob("run-*/*")
    assert result.stdout.lines.count(f"[bsworld] log dir: {run_dir}") == 1

    workers, simulations, labels = set(), set(), set()
    for marker in run_dir.glob("log/test_identity/test_identity_a_b*/identity"):
        worker, sim_id, label = marker.read_text().splitlines()
        assert sim_id.startswith(f"bsw_{run_dir.name}_")
        workers.add(worker)
        simulations.add(sim_id)
        labels.add(label)
    assert len(workers) == len(simulations) == 2
    assert labels == {"a/b", "a:b"}


def test_plugin_does_not_create_log_dir_without_bsworld_fixtures(
    pytester,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    test_file = pytester.makepyfile(
        test_plain="""
            def test_plain():
                assert True
        """
    )
    log_dir = pytester.path / "logs"

    result = pytester.runpytest_subprocess(
        *_ISOLATED_PLUGIN_ARGS,
        "--out-dir",
        str(log_dir),
        "-q",
        str(test_file),
    )

    result.assert_outcomes(passed=1)
    assert not log_dir.exists()
