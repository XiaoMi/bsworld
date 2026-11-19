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

from bsworld import plugin

pytest_plugins = ("pytester",)

_ISOLATED_PLUGIN_ARGS = (
    "-p",
    "xdist.plugin",
    "-p",
    "pytest_asyncio.plugin",
    "-p",
    "bsworld.plugin",
)


class _OptionGroup:
    def __init__(self) -> None:
        self.options: dict[str, dict] = {}

    def addoption(self, *args, **kwargs) -> None:
        self.options[args[0]] = kwargs


class _Parser:
    def __init__(self) -> None:
        self.group = _OptionGroup()

    def getgroup(self, name: str) -> _OptionGroup:  # noqa: ARG002
        return self.group


class _Config:
    def __init__(
        self,
        *,
        workerinput=None,
    ) -> None:
        self.workerinput = workerinput
        self._options = {
            "--out-dir": None,
        }
        self._markers: list[str] = []

    def getoption(self, name, default=None, skip=False):  # noqa: ARG002
        return self._options.get(name, default)

    def addinivalue_line(self, name: str, value: str) -> None:
        self._markers.append(f"{name}:{value}")


def test_bsim_cli_options_only_configure_output_directory() -> None:
    parser = _Parser()
    plugin.pytest_addoption(parser)
    assert set(parser.group.options) == {"--out-dir"}


def test_configure_worker_reuses_controller_run_identity() -> None:
    cfg = _Config(
        workerinput={
            "bsworld_out_dir": "/tmp/out",
            "bsworld_now_tag": "run-fixed",
            "bsworld_run_uid": "uid",
        }
    )

    plugin.pytest_configure(cfg)

    assert cfg._bsworld_out_dir == "/tmp/out"
    assert cfg._bsworld_now_tag == "run-fixed"
    assert cfg._bsworld_run_uid == "uid"


def test_nodeid_helpers_are_stable_and_path_safe(tmp_path) -> None:
    sim_id = plugin._sim_id_for_node(
        "tests/foo/test_bar.py::test_case[param/a]", "abc123"
    )
    log_dir = plugin._test_log_dir_for_node(
        "tests/foo/test_bar.py::test_case[param/a]",
        tmp_path,
    )

    assert sim_id.startswith("bsw_abc123_")
    assert len(sim_id.rsplit("_", 1)[-1]) == 8
    assert log_dir == tmp_path / "tests" / "foo" / "test_bar" / "test_case_param_a"


def test_topo_fixture_derives_identity_and_log_dir_from_the_node(
    pytester, monkeypatch
) -> None:
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("RUN_UID", "abc123")
    test_file = pytester.makepyfile(
        test_identity="""
            import pytest
            from bsworld.devices import BSController

            @pytest.mark.asyncio
            async def test_identity(topo, bsworld_env):
                assert topo.env is bsworld_env
                assert topo.sim_id.startswith("bsw_abc123_")
                assert topo.test_log_dir == (
                    bsworld_env.log_dir / "test_identity" / "test_identity"
                )
                assert isinstance(topo.add_controller(), BSController)
        """
    )

    result = pytester.runpytest_subprocess(
        *_ISOLATED_PLUGIN_ARGS,
        "-o",
        "asyncio_mode=strict",
        "--out-dir",
        str(pytester.path / "logs"),
        "-q",
        str(test_file),
    )

    result.assert_outcomes(passed=1)


def test_topo_fixture_works_in_pytest_asyncio_strict_mode(
    pytester,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    test_file = pytester.makepyfile(
        test_strict_topo="""
            import asyncio
            import pytest
            from bsworld.runtime.scheduler import BsimEventLoop

            @pytest.mark.asyncio
            async def test_topo_fixture(topo):
                assert topo is not None
                assert isinstance(asyncio.get_running_loop(), BsimEventLoop)
        """
    )

    result = pytester.runpytest_subprocess(
        *_ISOLATED_PLUGIN_ARGS,
        "-o",
        "asyncio_mode=strict",
        "--out-dir",
        str(pytester.path / "logs"),
        "-q",
        str(test_file),
    )

    result.assert_outcomes(passed=1)


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
