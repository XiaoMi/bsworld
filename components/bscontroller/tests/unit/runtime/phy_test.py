# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Locating the BabbleSim PHY executable (``find_phy``).

Checks how the runtime resolves the ``bs_2G4_phy_v1`` binary:

- it looks under ``BSIM_OUT_PATH`` relative to the current directory, or under
  an explicit build directory passed by the caller;
- it requires ``BSIM_OUT_PATH`` to be set when no build directory is given;
- it rejects a missing or non-executable binary with a clear error.

Tests manipulate the environment and a temporary filesystem via monkeypatch and
``tmp_path``; they touch no real BabbleSim install.
"""

import pytest

from bscontroller.runtime import find_phy, phy_command


def test_find_phy_uses_babblesim_output_path(monkeypatch, tmp_path):
    """find_phy resolves the PHY under BSIM_OUT_PATH in the current directory.

    Purpose: Verify ``find_phy`` locates ``bin/bs_2G4_phy_v1`` beneath the
    directory named by ``BSIM_OUT_PATH`` interpreted relative to the current
    working directory.

    Steps:
        1. Create an executable ``build/bin/bs_2G4_phy_v1`` under ``tmp_path``.
        2. Change the working directory to ``tmp_path`` and set
           ``BSIM_OUT_PATH=build``.
        3. Call ``find_phy()``.

    Expected:
        - The returned path is the created binary.
    """
    binary = tmp_path / 'build' / 'bin' / 'bs_2G4_phy_v1'
    binary.parent.mkdir(parents=True)
    binary.touch(mode=0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('BSIM_OUT_PATH', 'build')

    assert find_phy() == binary


def test_find_phy_accepts_explicit_build_override(monkeypatch, tmp_path):
    """An explicit build directory overrides BSIM_OUT_PATH.

    Purpose: Verify ``find_phy`` resolves the binary under a build directory
    passed by the caller, ignoring a differing ``BSIM_OUT_PATH``.

    Steps:
        1. Create an executable ``bin/bs_2G4_phy_v1`` under ``tmp_path``.
        2. Set ``BSIM_OUT_PATH`` to an unrelated directory.
        3. Call ``find_phy(tmp_path)``.

    Expected:
        - The returned path is the binary under the explicit directory.
    """
    binary = tmp_path / 'bin' / 'bs_2G4_phy_v1'
    binary.parent.mkdir()
    binary.touch(mode=0o755)
    monkeypatch.setenv('BSIM_OUT_PATH', str(tmp_path / 'other-build'))

    assert find_phy(tmp_path) == binary


@pytest.mark.parametrize('value', [None, ''])
def test_find_phy_requires_configured_output(monkeypatch, value):
    """find_phy requires BSIM_OUT_PATH when no build directory is given.

    Purpose: Verify ``find_phy`` raises a clear error when neither an explicit
    build directory nor a usable ``BSIM_OUT_PATH`` is configured.

    Steps:
        1. Either unset ``BSIM_OUT_PATH`` or set it to the empty string.
        2. Call ``find_phy()`` with no explicit directory.

    Expected:
        - ``ValueError`` mentioning 'set BSIM_OUT_PATH' is raised for both the
          unset and empty cases.
    """
    if value is None:
        monkeypatch.delenv('BSIM_OUT_PATH', raising=False)
    else:
        monkeypatch.setenv('BSIM_OUT_PATH', value)

    with pytest.raises(ValueError, match='set BSIM_OUT_PATH'):
        find_phy()


@pytest.mark.parametrize('mode', [None, 0o644])
def test_find_phy_rejects_missing_or_nonexecutable_binary(monkeypatch, tmp_path, mode):
    """find_phy rejects a PHY that is absent or not executable.

    Purpose: Verify ``find_phy`` raises when the expected binary does not exist
    or exists without the executable bit set.

    Steps:
        1. Point ``BSIM_OUT_PATH`` at ``tmp_path``.
        2. For ``mode=None`` leave the binary absent; for ``mode=0o644`` create
           it without execute permission.
        3. Call ``find_phy()``.

    Expected:
        - ``ValueError`` mentioning 'PHY executable missing' is raised in both
          cases.
    """
    binary = tmp_path / 'bin' / 'bs_2G4_phy_v1'
    if mode is not None:
        binary.parent.mkdir()
        binary.touch(mode=mode)
    monkeypatch.setenv('BSIM_OUT_PATH', str(tmp_path))

    with pytest.raises(ValueError, match='PHY executable missing'):
        find_phy()


def test_phy_command_selects_channel_arguments_without_overriding_defaults(tmp_path):
    """A propagation matrix can select multiatt without a default attenuation."""
    binary, matrix = tmp_path / 'phy', tmp_path / 'matrix'
    basic = [binary, '-s=sim', '-D=2', '-defmodem=BLE_simple']
    assert phy_command(binary, 'sim', 2) == [*basic, '-nodump']
    assert phy_command(binary, 'sim', 2, matrix_file=matrix, dump=True) == [
        *basic,
        '-channel=multiatt',
        '-argschannel',
        f'-file={matrix}',
        '-argsmain',
        '-dump',
        '-dump_imm',
    ]
