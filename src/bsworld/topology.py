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
"""Run Bluetooth Hosts on a shared PHY and simulation clock.

Tests register their Controllers and RF conditions, then start the topology
before opening H4 connections or booting UML guests.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bscontroller import RadioEventLoop

from .backends.babblesim import BabbleSimBackend
from .devices.bscontroller import BSController
from .env import Env
from .interference import (
    BurstInterference,
    ChannelAttenuation,
    InterferenceRef,
    InterferenceSource,
    InterfType,
    PlaybackInterference,
    StaticAttenuation,
    TimedAttenuation,
    WlanConfigSet,
    WlanInterference,
)
from .log import close_log_file, open_log_file, warn
from .runtime.proc import ProcessManager
from .uml.scheduler import TimeTravelScheduler

if TYPE_CHECKING:
    from bscontroller import ControllerConfig

    from .uml import UmlGuest


@dataclass
class BluetoothTopology:
    env: Env
    sim_id: str
    test_log_dir: Path
    pm: ProcessManager = field(default_factory=ProcessManager)
    bsim: BabbleSimBackend = field(init=False)
    _event_loop: RadioEventLoop | None = field(default=None, init=False, repr=False)
    _uml_guests: list[UmlGuest] = field(default_factory=list, init=False, repr=False)
    _uml_scheduler: TimeTravelScheduler | None = field(
        default=None, init=False, repr=False
    )
    _lifecycle_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    _started: bool = field(default=False, init=False)
    _stopped: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.bsim = BabbleSimBackend(self.env, self.sim_id)
        self.env.out_dir.mkdir(parents=True, exist_ok=True)
        self.test_log_dir.mkdir(parents=True, exist_ok=True)

    # ------------- controller registration -------------

    def _ensure_configurable(self) -> None:
        if self._started:
            raise RuntimeError("cannot modify a topology after it has started")
        if self._stopped or self._lifecycle_lock.locked():
            raise RuntimeError(
                "cannot modify a topology during or after a state change"
            )

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

    def _claim_uml_guest(self, guest: UmlGuest) -> TimeTravelScheduler:
        clock = self.bsim.clock
        loop = self._event_loop
        if (
            clock is None
            or not self._started
            or loop is None
            or loop is not asyncio.get_running_loop()
        ):
            raise RuntimeError(f"{guest.label}: start the topology before the guest")
        if guest.controller not in self.bsim.controllers:
            raise ValueError(
                f"{guest.label}: Controller does not belong to this topology"
            )
        if any(other.label == guest.label for other in self._uml_guests):
            raise ValueError(f"{guest.label}: UML guest label is already in use")
        if any(other.controller is guest.controller for other in self._uml_guests):
            raise ValueError(f"{guest.label}: Controller is already attached to a Host")
        if self._uml_scheduler is None:
            self._uml_scheduler = TimeTravelScheduler(loop, clock)
        self._uml_guests.append(guest)
        return self._uml_scheduler

    def _release_uml_guest(self, guest: UmlGuest) -> None:
        if guest in self._uml_guests:
            self._uml_guests.remove(guest)

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
            static_paths=list(static_paths or ()),
            timed_paths=list(timed_paths or ()),
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

        The prefix identifies the .Txv2.csv, .Rxv2.csv, .RSSI.csv and .CCA.csv files.
        """
        return self._add_interference_source(
            PlaybackInterference(input_file=Path(input_file))
        )

    def _add_interference_source(self, source: InterferenceSource) -> InterferenceRef:
        self._ensure_configurable()
        return self.bsim.add_interference_source(source)

    # ------------- start / stop -------------

    async def start(self) -> None:
        """Start the simulation once and bind the test loop to its clock."""
        async with self._lifecycle_lock:
            if self._stopped:
                raise RuntimeError("cannot start a stopped topology")
            if self._started:
                return
            try:
                open_log_file(self.test_log_dir / "bsworld.log")
                self.pm.install_signal_handlers()
                await self.bsim.start(self.pm, log_dir=self.test_log_dir)
                await self.bsim.wait_for_hci_sockets()
                await self.bsim.wait_for_devices_registered()
                if self.bsim.clock is not None:
                    loop = asyncio.get_running_loop()
                    if not isinstance(loop, RadioEventLoop):
                        raise RuntimeError(
                            "the active asyncio event loop is not BSIM-aware"
                        )
                    self.bsim.clock.bind(loop)
                    self._event_loop = loop
            except BaseException as exc:
                try:
                    await self._shutdown()
                except Exception as cleanup_error:
                    exc.add_note(f"topology cleanup failed: {cleanup_error}")
                raise
            self._started = True

    async def stop(self) -> None:
        """Stop Guests before releasing their Controllers and clock."""
        async with self._lifecycle_lock:
            if not self._stopped:
                await self._shutdown()

    async def _shutdown(self) -> None:
        self._stopped = True
        self._started = False
        failures: list[Exception] = []
        for guest in reversed(tuple(self._uml_guests)):
            try:
                guest.stop()
            except Exception as exc:
                failures.append(exc)
        self._uml_scheduler = None
        if self._event_loop is not None:
            try:
                self._event_loop.unbind_clock()
            except Exception as exc:
                warn(f"simulation clock unbind failed: {exc}")
            finally:
                self._event_loop = None
        try:
            async with AsyncExitStack() as cleanup:
                cleanup.callback(close_log_file)
                cleanup.callback(self.bsim.finish)
                cleanup.callback(self.pm.restore_signal_handlers)
                cleanup.push_async_callback(asyncio.to_thread, self.pm.cleanup)
                await self.bsim.stop_controllers()
        except Exception as exc:
            failures.append(exc)

        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise ExceptionGroup("topology teardown failed", failures)
