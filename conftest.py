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
"""Configure the repository's pytest runs and integration skip reporting."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
_INTEGRATION_DIR = _ROOT / "tests" / "integration"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

pytest_plugins = ["bsworld.plugin"]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("bsworld").addoption(
        "--require-integration",
        action="store_true",
        help="fail if any selected integration test is skipped",
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--require-integration") and not hasattr(config, "workerinput"):
        config.pluginmanager.register(_RequiredIntegration(config.rootpath))


class _RequiredIntegration:
    def __init__(self, rootpath: Path) -> None:
        self._rootpath = rootpath
        self._skipped: set[str] = set()

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        self._record_skip(report)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self._record_skip(report)

    def _record_skip(self, report: pytest.CollectReport | pytest.TestReport) -> None:
        if not report.skipped:
            return
        file_path = self._rootpath / report.nodeid.partition("::")[0]
        if file_path.resolve().is_relative_to(_INTEGRATION_DIR):
            self._skipped.add(report.nodeid)

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if self._skipped and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if self._skipped:
            terminalreporter.section("Required integration tests skipped")
            for nodeid in sorted(self._skipped):
                terminalreporter.write_line(nodeid)
