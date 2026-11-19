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
"""BluetoothTopology: declarative wiring for one test case.

Lifecycle (driven by the `topo` fixture):
    1. test code registers controllers, processes, and RF conditions. These
       calls only record the topology; nothing has been spawned yet.
    2. test code calls topology.start()
       — BabbleSim backend brings up the PHY/controllers/interference devices,
       then every registered process starts and joins the PHY directly.
    3. pytest joins the PHY through its FIFO clock and binds the event loop.
    4. fixture teardown calls topology.stop() — the process manager stops
       child processes and the backend releases its clock and artifacts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bsworld.runtime.scheduler import BsimEventLoop

from .backends.babblesim import BabbleSimBackend
from .devices.bscontroller import BSController
from .devices.instance import ProcessInstance
from .env import Env
from .interference import (
    BurstInterference,
    ChannelAttenuation,
    InterferenceRef,
    InterfType,
    PlaybackInterference,
    StaticAttenuation,
    TimedAttenuation,
    WlanConfigSet,
    WlanInterference,
)
from .log import close_log_file, open_log_file, warn
from .runtime.proc import ProcessManager

if TYPE_CHECKING:
    from bscontroller import ControllerConfig


@dataclass
class BluetoothTopology:
    env: Env
    sim_id: str
    test_log_dir: Path
    pm: ProcessManager = field(default_factory=ProcessManager)
    bsim: BabbleSimBackend = field(init=False)
    _event_loop: BsimEventLoop | None = field(default=None, init=False, repr=False)
    _started: bool = False

    def __post_init__(self) -> None:
        self.bsim = BabbleSimBackend(self.env, self.sim_id)
        self.env.out_dir.mkdir(parents=True, exist_ok=True)
        self.test_log_dir.mkdir(parents=True, exist_ok=True)

    @property
    def processes(self) -> tuple[ProcessInstance, ...]:
        """Registered processes, read-only; register through add_process()."""
        return self.bsim.processes

    # ------------- process registration -------------

    def _ensure_configurable(self) -> None:
        if self._started:
            raise RuntimeError("cannot modify a topology after it has started")

    def add_controller(
        self,
        *,
        public_address: str | None = None,
        log_subdir: Path | str | None = None,
        config: ControllerConfig | None = None,
        max_step_us: int | None = None,
    ) -> BSController:
        """Allocate a BSController for one Bluetooth Host."""
        self._ensure_configurable()
        log_path = (
            Path(log_subdir)
            if log_subdir is not None
            else Path("controllers") / f"hci{len(self.bsim.controllers)}"
        )
        return self.bsim.add_controller(
            public_address=public_address,
            snoop_dir=self.test_log_dir / log_path,
            config=config,
            max_step_us=max_step_us,
        )

    def add_process(self, process: ProcessInstance) -> ProcessInstance:
        """Register an Application process that will be started with the topology."""
        self._ensure_configurable()
        if process._topology is not None:
            raise ValueError("process is already registered with a topology")
        if any(existing.label == process.label for existing in self.processes):
            raise ValueError(
                f"Application process label is already registered: {process.label}"
            )
        # The backend validates the whole registration before recording it, so
        # the capture is only moved once registration can no longer fail.
        self.bsim.register_process(process)
        process._topology = self
        if process.controller is not None:
            self._move_capture(process, process.controller)
        return process

    def _attach_process_controller(
        self, process: ProcessInstance, controller: BSController
    ) -> None:
        """Validate a late attachment of a registered process before it is recorded."""
        self._ensure_configurable()
        self.bsim.validate_attachment(process, controller)
        self._move_capture(process, controller)

    def _move_capture(self, process: ProcessInstance, controller: BSController) -> None:
        # An Application keeps its Controller's capture beside its own logs.
        self.bsim.set_snoop_dir(controller, self.test_log_dir / process.log_subdir)

    # ------------- interference -------------

    def set_channel_attenuation(
        self,
        *,
        default_db: float = 60.0,
        extra_db: float = 0.0,
        matrix_file: "Path | None" = None,
        static_paths: "list[StaticAttenuation] | None" = None,
        timed_paths: "list[TimedAttenuation] | None" = None,
    ) -> None:
        """Configure PHY channel attenuation (multiatt model).

        Use static_paths for fixed per-path attenuation and timed_paths for
        dynamic per-path attenuation that varies over simulation time.
        """
        self._ensure_configurable()
        self.bsim.channel_attenuation = ChannelAttenuation(
            default_attenuation_db=default_db,
            extra_attenuation_db=extra_db,
            matrix_file=matrix_file,
            static_paths=static_paths or [],
            timed_paths=timed_paths or [],
        )

    def add_burst_interference(
        self,
        center_freq_mhz: float,
        *,
        power_dbm: float = 20.0,
        interf_type: InterfType = "CW",
        windows: "list[tuple[int, int]] | None" = None,
        rand_seed: "int | None" = None,
    ) -> InterferenceRef:
        """Inject a burst interferer at the given frequency.

        windows: list of (start_us, end_us) pairs; omit for full-simulation
        interference.
        """
        return self._add_interference_source(
            BurstInterference(
                center_freq_mhz=center_freq_mhz,
                power_dbm=power_dbm,
                interf_type=interf_type,
                windows=windows or [],
                rand_seed=rand_seed,
            )
        )

    def add_wlan_interference(
        self,
        *,
        channel: "int | None" = None,
        center_freq_mhz: "float | None" = None,
        power_dbm: float = 20.0,
        config_set: WlanConfigSet = "5",
        mod_type: InterfType = "WLAN",
        windows: "list[tuple[int, int]] | None" = None,
        rand_seed: "int | None" = None,
    ) -> InterferenceRef:
        """Inject a WLAN activity model interferer.

        Specify either channel (1-14) or center_freq_mhz.
        """
        return self._add_interference_source(
            WlanInterference(
                channel=channel,
                center_freq_mhz=center_freq_mhz,
                power_dbm=power_dbm,
                config_set=config_set,
                mod_type=mod_type,
                windows=windows or [],
                rand_seed=rand_seed,
            )
        )

    def add_playback_interference(self, input_file: "Path | str") -> InterferenceRef:
        """Replay recorded interference from BabbleSim CSV dumps.

        input_file: path prefix without extension (.Tx/.Rx/.RSSI/.CCA.csv expected
        alongside).
        """
        return self._add_interference_source(
            PlaybackInterference(input_file=Path(input_file))
        )

    def _add_interference_source(self, source) -> InterferenceRef:
        self._ensure_configurable()
        return InterferenceRef(self.bsim.add_interference_source(source))

    # ------------- start / stop -------------

    async def start(self) -> None:
        if self._started:
            return
        open_log_file(self.test_log_dir / "bsworld.log")
        self.pm.install_signal_handlers()
        self._resolve_channel_attenuation()
        await self.bsim.start(self.pm, log_dir=self.test_log_dir)
        await self.bsim.wait_for_hci_sockets()
        layout = self.bsim.plan_devices()
        for process in self.processes:
            process.start(
                self.pm,
                self.test_log_dir,
                layout.application_slot(process),
            )
        await asyncio.to_thread(self.bsim.wait_for_devices_registered)
        if self.bsim.clock is not None:
            loop = asyncio.get_running_loop()
            if not isinstance(loop, BsimEventLoop):
                raise RuntimeError("the active asyncio event loop is not BSIM-aware")
            # Registration alone does not mean every device has yielded to PHY.
            self.bsim.clock.synchronize()
            self._event_loop = loop
            self._event_loop.bind_clock(self.bsim.clock)
        self._started = True

    def _resolve_channel_attenuation(self) -> None:
        ca = self.bsim.channel_attenuation
        if ca is None:
            return
        if ca.matrix_file is not None:
            return

        layout = self.bsim.plan_devices()
        static_paths = [
            StaticAttenuation(
                tx=layout.resolve_endpoint(path.tx),
                rx=layout.resolve_endpoint(path.rx),
                attenuation_db=path.attenuation_db,
            )
            for path in ca.static_paths
        ]
        timed_paths = [
            TimedAttenuation(
                tx=layout.resolve_endpoint(path.tx),
                rx=layout.resolve_endpoint(path.rx),
                times_us=path.times_us,
                attenuation_db=path.attenuation_db,
            )
            for path in ca.timed_paths
        ]
        ca.static_paths[:] = static_paths
        ca.timed_paths[:] = timed_paths

    async def stop(self) -> None:
        if self._event_loop is not None:
            try:
                self._event_loop.unbind_clock()
            except Exception as exc:
                warn(f"simulation clock unbind failed: {exc}")
            finally:
                self._event_loop = None
        try:
            try:
                await self.bsim.stop_controllers()
            finally:
                await asyncio.to_thread(self.pm.cleanup)
        finally:
            self.pm.restore_signal_handlers()
            for process in self.processes:
                try:
                    process.stop_capture()
                except Exception as exc:
                    warn(f"{process.label}: failed to stop log capture: {exc}")
            try:
                self.bsim.finish(log_dir=self.test_log_dir)
            finally:
                close_log_file()
