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
"""BabbleSim backend: PHY, Controllers and interference.

Bring-up shape:
    1. verify configured BabbleSim artifacts.
    2. freeze the layout for Controllers, the framework clock and interference.
    3. clear stale simulation state and start the PHY.
    4. start Controller workers and interference devices.
    5. connect the pytest clock and wait for PHY registration.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bscontroller import EllisysCapture

from ..deps import check_babblesim_ready
from ..devices.bscontroller import BSController
from ..devices.layout import DeviceLayout
from ..env import Env
from ..interference import (
    BurstInterference,
    ChannelAttenuation,
    InterferenceRef,
    InterferenceSource,
    PlaybackInterference,
    StaticAttenuation,
    TimedAttenuation,
    WlanInterference,
)
from ..log import log, warn
from ..runtime.proc import ProcessManager
from ..runtime.scheduler import BsimClock
from ..runtime.wait import wait_for_path, wait_for_paths, wait_for_paths_async

if TYPE_CHECKING:
    from bscontroller import ControllerConfig

_RESIDUAL_PATTERNS = (
    "bs_2G4_phy_v1",
    "bs_device_2G4_",
)

_PHY_ID = "2G4"
_DEFAULT_MODEM = "BLE_simple"


@dataclass
class BabbleSimBackend:
    env: Env
    sim_id: str
    controllers: list[BSController] = field(default_factory=list)
    channel_attenuation: ChannelAttenuation | None = None
    interference_sources: dict[InterferenceRef, InterferenceSource] = field(
        default_factory=dict, init=False
    )
    _layout: DeviceLayout | None = field(default=None, init=False, repr=False)
    _ellisys_capture: EllisysCapture | None = field(
        default=None, init=False, repr=False
    )
    _started: bool = False
    clock: BsimClock | None = None

    def _ensure_layout_mutable(self) -> None:
        """Reject registrations once PHY identities have been published.

        start() freezes the layout first, so this also covers a started backend.
        """
        if self._layout is not None:
            raise RuntimeError("cannot change participants after the layout is frozen")

    def add_controller(
        self,
        *,
        public_address: str | None = None,
        snoop_dir: Path | None = None,
        config: ControllerConfig | None = None,
        max_step_us: int | None = None,
    ) -> BSController:
        self._ensure_layout_mutable()
        dev_id = len(self.controllers)
        capture_dir = snoop_dir or self.env.log_dir / "controllers"
        ctl = BSController(
            controller_dev_id=dev_id,
            sim_id=self.sim_id,
            socket_dir=self.env.sim_dir(self.sim_id),
            public_address=public_address,
            hci_snoop_file=(capture_dir / f"hci{dev_id}.snoop").resolve(),
            rf_pcap_file=(capture_dir / f"radio{dev_id}.pcapng").resolve(),
            config=config,
            max_step_us=max_step_us,
        )
        self.controllers.append(ctl)
        return ctl

    def add_interference_source(self, source: InterferenceSource) -> InterferenceRef:
        """Record an interference device and return its topology-local reference."""
        self._ensure_layout_mutable()
        ref = InterferenceRef(len(self.interference_sources))
        self.interference_sources[ref] = source
        return ref

    def plan_devices(self) -> DeviceLayout:
        """Freeze the device id assignment for the registered topology.

        Device ids are derived from the registered participants, so the first
        caller after registration fixes the layout for the whole simulation.
        """
        if self._layout is None:
            self._layout = DeviceLayout(
                phy_id=_PHY_ID,
                controllers=tuple(self.controllers),
                interference_refs=tuple(self.interference_sources),
            )
        return self._layout

    def check_deps_ready(self) -> None:
        check_babblesim_ready(self.env)

    def prepare(self) -> None:
        log(f"preparing bsim sim_id={self.sim_id}")
        sim_dir = self.env.sim_dir(self.sim_id)
        sim_dir.mkdir(parents=True, exist_ok=True)
        for pat in _RESIDUAL_PATTERNS:
            subprocess.run(
                ["pkill", "-9", "-f", f"{pat}.*-s={self.sim_id}"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        for ctl in self.controllers:
            if ctl.hci_sock.exists():
                log(f"removing stale {ctl.hci_sock}")
                ctl.hci_sock.unlink()

    async def start(
        self,
        pm: ProcessManager,
        *,
        log_dir: Path | None = None,
        ready_timeout: float = 30.0,
    ) -> None:
        log_dir = log_dir or self.env.log_dir
        bsim_log_dir = log_dir / "babblesim"
        if self._started:
            return
        if not self.controllers:
            raise RuntimeError("a simulation requires at least one BSController")
        self.check_deps_ready()
        layout = self.plan_devices()
        num_devs = layout.device_count

        # Spawn phy.
        phy_bin = self.env.bsim_bin_dir / f"bs_{_PHY_ID}_phy_v1"
        phy_argv: list = [
            phy_bin,
            f"-s={self.sim_id}",
            f"-D={num_devs}",
            f"-defmodem={_DEFAULT_MODEM}",
        ]
        if self.channel_attenuation is not None:
            ca = self.channel_attenuation
            matrix_file = ca.matrix_file
            if ca.static_paths or ca.timed_paths:
                matrix_file = _write_attenuation_matrix(
                    static_paths=ca.static_paths,
                    timed_paths=ca.timed_paths,
                    out_dir=bsim_log_dir,
                    layout=layout,
                )
            phy_argv += [
                "-channel=multiatt",
                "-argschannel",
                f"-at={ca.default_attenuation_db}",
            ]
            if ca.extra_attenuation_db:
                phy_argv.append(f"-atextra={ca.extra_attenuation_db}")
            if matrix_file is not None:
                phy_argv.append(f"-file={matrix_file}")
            # Switch back to main PHY args before disabling CSV dumps.
            phy_argv.append("-argsmain")
        phy_argv.append("-nodump")
        self.prepare()
        phy_process = pm.spawn(
            "bs_2G4_phy_v1",
            phy_argv,
            cwd=self.env.bsim_bin_dir,
            log_dir=bsim_log_dir,
        )
        sim_dir = self.env.sim_dir(self.sim_id)
        wait_for_path(sim_dir / "2G4.phy.lock", timeout=ready_timeout)
        log("phy ready")

        self.clock = BsimClock(
            sim_dir=sim_dir,
            phy_id=_PHY_ID,
            dev_id=layout.clock_dev_id,
            phy_alive=phy_process.alive,
        )

        capture = self._ellisys_capture = EllisysCapture(bsim_log_dir / "ellisys.bttrp")
        for ctl in self.controllers:
            await ctl.start(ellisys_capture=capture)

        for ref, src in self.interference_sources.items():
            dev_id = layout.interference_dev_id(ref)
            argv = _build_interf_argv(
                src, sim_id=self.sim_id, dev_id=dev_id, bin_dir=self.env.bsim_bin_dir
            )
            pm.spawn(
                f"interf_{dev_id}",
                argv,
                cwd=self.env.bsim_bin_dir,
                log_dir=bsim_log_dir,
            )
        self._started = True

    async def stop_controllers(self) -> None:
        """Release Controller runtimes before terminating the shared PHY."""
        results = await asyncio.gather(
            *(ctl.stop() for ctl in self.controllers), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    def device_lock_paths(self) -> list[Path]:
        return self.plan_devices().lock_paths(self.env.sim_dir(self.sim_id))

    def wait_for_devices_registered(self, *, timeout: float = 30.0) -> None:
        locks = self.device_lock_paths()
        log(f"waiting for {len(locks)} BabbleSim device lock(s)")
        if self.clock is not None:
            self.clock.connect(timeout=timeout)
        wait_for_paths(locks, timeout=timeout)
        log("all devices registered to phy")

    async def wait_for_hci_sockets(self, *, timeout: float = 30.0) -> None:
        socks = [ctl.hci_sock for ctl in self.controllers]
        if not socks:
            return
        log(f"waiting for {len(socks)} hci socket(s)")
        await wait_for_paths_async(socks, timeout=timeout)
        log("hci socket(s) bound")

    def finish(self) -> None:
        """Release the clock and finish the trace after the Controllers stop."""
        if self.clock is not None:
            try:
                self.clock.close()
            except Exception as exc:
                warn(f"simulation clock close failed: {exc}")
            finally:
                self.clock = None
        capture, self._ellisys_capture = self._ellisys_capture, None
        try:
            if capture is not None:
                capture.close()
        except (OSError, ValueError) as exc:
            warn(f"Ellisys capture failed for {self.sim_id}: {exc}")
        finally:
            self.cleanup_sim_artifacts()

    def cleanup_sim_artifacts(self) -> None:
        sim_state_dir = self.env.sim_dir(self.sim_id)
        if sim_state_dir.exists():
            log(f"removing sim state {sim_state_dir}")
            shutil.rmtree(sim_state_dir, ignore_errors=True)

        results_dir = (
            self.env.bsim_dir / "results" / self.sim_id if self.env.bsim_dir else None
        )
        if results_dir is not None and results_dir.exists():
            log(f"removing bsim results {results_dir}")
            shutil.rmtree(results_dir, ignore_errors=True)

        for ctl in self.controllers:
            ctl.hci_sock.unlink(missing_ok=True)


def _write_attenuation_matrix(
    *,
    static_paths: list[StaticAttenuation],
    timed_paths: list[TimedAttenuation],
    out_dir: Path,
    layout: DeviceLayout,
) -> Path:
    """Write per-path static/timed attenuation files + matrix_file into out_dir.

    Files are placed in the test log directory so they survive cleanup and are
    available for post-mortem inspection alongside bsworld.log and HCI snoops.
    Returns the path to the generated matrix file ready for -file=<path>.
    """
    resolve = layout.resolve_endpoint
    matrix_lines = [
        f"{resolve(path.tx)} {resolve(path.rx)} : {path.attenuation_db}"
        for path in static_paths
    ]
    timed = [(resolve(path.tx), resolve(path.rx), path) for path in timed_paths]
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, (tx, rx, path) in enumerate(timed):
        timed_file = out_dir / f"timed_att_{tx}_{rx}_{i}.txt"
        with open(timed_file, "w") as fh:
            for t, att in zip(path.times_us, path.attenuation_db, strict=True):
                fh.write(f"{t}    {att}\n")
        matrix_lines.append(f'{tx} {rx} : "{timed_file}"')

    matrix_file = out_dir / "att_matrix.txt"
    with open(matrix_file, "w") as fh:
        fh.write("\n".join(matrix_lines) + "\n")
    return matrix_file


def _build_interf_argv(
    src: InterferenceSource,
    *,
    sim_id: str,
    dev_id: int,
    bin_dir: Path,
) -> list:
    common = [f"-s={sim_id}", f"-d={dev_id}"]

    if isinstance(src, BurstInterference):
        argv: list = [bin_dir / "bs_device_2G4_burst_interf"] + common
        argv += [
            f"-centerfreq={src.center_freq_mhz}",
            f"-power={src.power_dbm}",
            f"-type={src.interf_type}",
        ]
        if src.rand_seed is not None:
            argv.append(f"-rs={src.rand_seed}")
        _append_windows(argv, src.windows)
        return argv

    if isinstance(src, WlanInterference):
        argv = [bin_dir / "bs_device_2G4_WLAN_actmod"] + common
        if src.channel is not None:
            argv.append(f"-channel={src.channel}")
        else:
            argv.append(f"-centerfreq={src.center_freq_mhz}")
        argv += [
            f"-power={src.power_dbm}",
            f"-mod={src.mod_type}",
            f"-ConfigSet={src.config_set}",
        ]
        if src.rand_seed is not None:
            argv.append(f"-rs={src.rand_seed}")
        _append_windows(argv, src.windows)
        return argv

    if isinstance(src, PlaybackInterference):
        argv = [bin_dir / "bs_device_2G4_playbackv2"] + common
        argv.append(f"-inputf={src.input_file}")
        if not src.tx:
            argv.append("-txoff")
        if not src.rx:
            argv.append("-rxoff")
        if not src.rssi:
            argv.append("-rssioff")
        if not src.cca:
            argv.append("-ccaoff")
        return argv

    raise TypeError(f"unknown interference source type: {type(src)}")


def _append_windows(argv: list, windows: list[tuple[int, int]]) -> None:
    if not windows:
        return
    argv.append("-timestart")
    argv.extend(str(start) for start, _ in windows)
    argv.append("-timeend")
    argv.extend(str(end) for _, end in windows)
