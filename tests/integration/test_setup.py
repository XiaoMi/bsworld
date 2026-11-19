# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Artifact selection and skip reporting for integration tests."""

from pathlib import Path

import pytest
from tests.integration.uml.common import _require_artifact

pytest_plugins = ("pytester",)


@pytest.mark.parametrize("executable", [False, True])
def test_artifacts_are_resolved_on_every_call(
    tmp_path, monkeypatch, executable
) -> None:
    artifact = tmp_path / "artifact"
    artifact.write_text("output")
    monkeypatch.setenv("BSWORLD_TEST_BIN", str(artifact))

    if executable:
        with pytest.raises(pytest.skip.Exception, match="compiled executable"):
            _require_artifact("BSWORLD_TEST_BIN")
        artifact.chmod(0o755)
    assert _require_artifact("BSWORLD_TEST_BIN", executable=executable) == artifact

    monkeypatch.setenv("BSWORLD_TEST_BIN", str(tmp_path / "missing"))
    with pytest.raises(pytest.skip.Exception, match="BSWORLD_TEST_BIN"):
        _require_artifact("BSWORLD_TEST_BIN", executable=executable)


@pytest.mark.parametrize("workers", [0, 2])
def test_strict_run_rejects_integration_skips(pytester, monkeypatch, workers) -> None:
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    conftest = Path(__file__).resolve().parents[2] / "conftest.py"
    pytester.makeconftest(conftest.read_text())
    pytester.makepyfile(
        **{
            "tests/integration/test_runtime_skip": """
                import pytest

                def test_runtime_skip():
                    pytest.skip("no kernel")

                def test_ok():
                    pass
            """,
            "tests/integration/test_collection_skip": """
                import pytest
                pytest.skip("no dependency", allow_module_level=True)
            """,
            "tests/test_optional": """
                import pytest

                def test_optional():
                    pytest.skip("optional unit test")
            """,
        }
    )
    args = (
        "-p",
        "no:bsworld.plugin",
        "-p",
        "xdist.plugin",
        "-n",
        str(workers),
        "-q",
        "-p",
        "no:cacheprovider",
    )
    result = pytester.runpytest_subprocess(*args)
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=1, skipped=3)

    result = pytester.runpytest_subprocess(*args, "--require-integration")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        [
            "*Required integration tests skipped*",
            "tests/integration/test_collection_skip.py",
            "tests/integration/test_runtime_skip.py::test_runtime_skip",
        ]
    )
    assert "tests/test_optional.py" not in result.stdout.str()
