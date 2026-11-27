# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""BabbleSim PHY executable discovery and process ownership."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from bscontroller.radio.babblesim import simulation_directory


def find_phy(directory: Path | str | None = None) -> Path:
    configured = directory or os.environ.get('BSIM_OUT_PATH')
    if not configured:
        raise ValueError(
            'set BSIM_OUT_PATH or pass --bsim-dir to select a BabbleSim build'
        )
    binary = Path(configured).expanduser().resolve() / 'bin' / 'bs_2G4_phy_v1'
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError(f'BabbleSim PHY executable missing: {binary}')
    return binary


def phy_command(
    binary: Path,
    sim_id: str,
    device_count: int,
    *,
    attenuation_db: float | None = None,
    extra_attenuation_db: float = 0,
    matrix_file: Path | None = None,
    dump: bool = False,
) -> list[str | Path]:
    """Build the shared BLE PHY command for an independently owned process."""
    if device_count < 1:
        raise ValueError('device_count must be positive')
    args = [binary, f'-s={sim_id}', f'-D={device_count}', '-defmodem=BLE_simple']
    if attenuation_db is not None or extra_attenuation_db or matrix_file is not None:
        args.extend(('-channel=multiatt', '-argschannel'))
        if attenuation_db is not None:
            args.append(f'-at={attenuation_db}')
        if extra_attenuation_db:
            args.append(f'-atextra={extra_attenuation_db}')
        if matrix_file is not None:
            args.append(f'-file={matrix_file.resolve()}')
        args.append('-argsmain')
    args.extend(('-dump', '-dump_imm') if dump else ('-nodump',))
    return args


class PhyProcess:
    """Own one fresh simulation and its process; existing simulations are never
    removed.
    """

    def __init__(
        self,
        sim_id: str,
        device_count: int,
        *,
        directory=None,
        log_dir: Path,
        dump: bool = False,
    ):
        if device_count < 1:
            raise ValueError('device_count must be positive')
        self.sim_id = sim_id
        self.device_count = device_count
        self.binary = find_phy(directory)
        self.log_dir = Path(log_dir)
        self.dump = dump
        self.results_dir = self.binary.parent.parent / 'results' / sim_id
        self.state_dir = simulation_directory(sim_id)
        self.process: subprocess.Popen | None = None
        self._output = None

    def __enter__(self):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=False)
        try:
            self._output = (self.log_dir / 'phy.log').open('wb')
            self.process = subprocess.Popen(
                phy_command(
                    self.binary,
                    self.sim_id,
                    self.device_count,
                    attenuation_db=45,
                    dump=self.dump,
                ),
                cwd=self.binary.parent,
                stdout=self._output,
                stderr=subprocess.STDOUT,
            )
        except BaseException:
            if self._output is not None:
                self._output.close()
            shutil.rmtree(self.state_dir, ignore_errors=True)
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self.process is not None:
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
                if exc_type is None and self.process.returncode != 0:
                    raise RuntimeError(
                        f'PHY exited with status {self.process.returncode}; '
                        f'see {self.log_dir / "phy.log"}'
                    )
        finally:
            if self._output is not None:
                self._output.close()
            shutil.rmtree(self.state_dir, ignore_errors=True)
