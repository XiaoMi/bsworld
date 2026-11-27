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

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.unit.helpers import SpawnRecorder, make_env

from bsworld.backends import babblesim
from bsworld.backends.babblesim import BabbleSimBackend
from bsworld.deps import BSWorldSetupError
from bsworld.devices import BSController
from bsworld.interference import (
    BurstInterference,
    PlaybackInterference,
    StaticAttenuation,
    TimedAttenuation,
    WlanInterference,
)
from bsworld.topology import BluetoothTopology


@pytest.fixture
def controller_starts(monkeypatch):
    started = []

    async def start(controller, *, ellisys_capture):
        started.append((controller, ellisys_capture))

    monkeypatch.setattr(BSController, "start", start)
    return started


def test_interference_argv_builders_cover_burst_wlan_and_playback(tmp_path) -> None:
    bin_dir = tmp_path / "bin"

    burst = babblesim._build_interf_argv(
        BurstInterference(
            2402.0, power_dbm=5, interf_type="CW", windows=[(1, 2)], rand_seed=7
        ),
        sim_id="sim",
        dev_id=3,
        bin_dir=bin_dir,
    )
    wlan = babblesim._build_interf_argv(
        WlanInterference(channel=6, config_set="VideoConf", rand_seed=9),
        sim_id="sim",
        dev_id=4,
        bin_dir=bin_dir,
    )
    playback = babblesim._build_interf_argv(
        PlaybackInterference(input_file=Path("capture"), rx=False, cca=False),
        sim_id="sim",
        dev_id=5,
        bin_dir=bin_dir,
    )

    assert burst == [
        bin_dir / "bs_device_2G4_burst_interf",
        "-s=sim",
        "-d=3",
        "-centerfreq=2402.0",
        "-power=5",
        "-type=CW",
        "-rs=7",
        "-timestart",
        "1",
        "-timeend",
        "2",
    ]
    assert "-channel=6" in wlan
    assert "-ConfigSet=VideoConf" in wlan
    assert "-rs=9" in wlan
    assert playback[-2:] == ["-rxoff", "-ccaoff"]


async def test_start_checks_artifacts_before_spawning_phy(tmp_path) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    backend.add_controller()
    recorder = SpawnRecorder()

    with pytest.raises(BSWorldSetupError, match="bs_2G4_phy_v1"):
        await backend.start(recorder)
    assert recorder.calls == []


async def test_add_controller_rejects_started_backend(
    tmp_path, monkeypatch, controller_starts
) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    backend.check_deps_ready = lambda: None
    backend.prepare = lambda: None
    backend.add_controller()
    monkeypatch.setattr(babblesim, "wait_for_path", lambda *args, **kwargs: None)
    await backend.start(SpawnRecorder(), log_dir=tmp_path / "log")

    with pytest.raises(RuntimeError, match="layout is frozen"):
        backend.add_controller()


async def test_start_uses_allocated_ids_for_phy_workers_and_attenuation(
    tmp_path, monkeypatch, controller_starts
) -> None:
    env = make_env(tmp_path)
    topo = BluetoothTopology(env, "sim", tmp_path / "log")
    backend = topo.bsim
    backend.check_deps_ready = lambda: None
    backend.prepare = lambda: None
    first = topo.add_controller()
    second = topo.add_controller()
    jammer = topo.add_burst_interference(2402.0)
    topo.set_channel_attenuation(
        static_paths=[
            StaticAttenuation(tx=jammer, rx=second, attenuation_db=20.0),
        ],
        timed_paths=[
            TimedAttenuation(
                tx=first, rx=1, times_us=[0, 10], attenuation_db=[40.0, 80.0]
            ),
        ],
    )
    recorder = SpawnRecorder()

    monkeypatch.setattr(babblesim, "wait_for_path", lambda *args, **kwargs: None)

    try:
        await backend.start(recorder, log_dir=topo.test_log_dir)
        log_dir = topo.test_log_dir / "babblesim"
        capture = controller_starts[0][1]
        assert controller_starts == [(first, capture), (second, capture)]
        assert capture.path == log_dir / "ellisys.bttrp"
        (phy_name, phy_args, phy_options), (name, args, options) = recorder.calls
        assert phy_name == "bs_2G4_phy_v1"
        assert "-D=4" in phy_args
        assert phy_args.index("-defmodem=BLE_simple") < phy_args.index("-argschannel")
        assert "-channel=multiatt" in phy_args
        assert phy_args.index("-argsmain") < phy_args.index("-nodump")
        assert name == "interf_3"
        assert "-d=3" in args
        assert phy_options["log_dir"] == options["log_dir"] == log_dir
        assert backend.clock is not None
        assert backend.clock.connection.prefix == env.sim_dir("sim") / "2G4.d2"
        matrix = log_dir / "att_matrix.txt"
        curve = log_dir / "timed_att_0_1_0.txt"
        assert f"-file={matrix}" in phy_args
        assert matrix.read_text() == f'3 1 : 20.0\n0 1 : "{curve}"\n'
        assert curve.read_text() == "0    40.0\n10    80.0\n"
    finally:
        backend.finish()


async def test_start_resolves_rf_input_paths_before_changing_child_directory(
    tmp_path, monkeypatch, controller_starts
) -> None:
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    monkeypatch.chdir(caller_dir)
    topo = BluetoothTopology(make_env(tmp_path), "sim", tmp_path / "log")
    backend = topo.bsim
    backend.check_deps_ready = lambda: None
    backend.prepare = lambda: None
    topo.add_controller()
    topo.set_channel_attenuation(matrix_file=Path("attenuation.txt"))
    topo.add_playback_interference("recording/capture")
    recorder = SpawnRecorder()

    monkeypatch.setattr(babblesim, "wait_for_path", lambda *args, **kwargs: None)

    try:
        await backend.start(recorder, log_dir=topo.test_log_dir)
        _, phy_args, phy_options = recorder.calls[0]
        _, playback_args, playback_options = recorder.calls[1]
        for options in (phy_options, playback_options):
            assert options["cwd"] == topo.env.bsim_bin_dir
        assert f"-file={caller_dir / 'attenuation.txt'}" in phy_args
        assert f"-inputf={caller_dir / 'recording/capture'}" in playback_args
    finally:
        backend.finish()


async def test_registration_connects_clock_before_waiting_for_all_device_locks(
    tmp_path, monkeypatch
) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    backend.add_controller()
    calls = []

    connect = AsyncMock(side_effect=lambda timeout: calls.append(("connect", timeout)))
    backend.clock = SimpleNamespace(connect=connect)
    monkeypatch.setattr(
        babblesim,
        "wait_for_paths_async",
        AsyncMock(side_effect=lambda paths, *, timeout: calls.append((paths, timeout))),
    )
    await backend.wait_for_devices_registered(timeout=5)
    assert calls == [
        ("connect", 5),
        (backend.device_lock_paths(), 5),
    ]


async def test_registration_does_not_wait_for_locks_after_clock_connection_failure(
    tmp_path, monkeypatch
) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    backend.add_controller()

    connect = AsyncMock(side_effect=TimeoutError("clock connection failed"))
    backend.clock = SimpleNamespace(connect=connect)
    wait_for_locks = AsyncMock()
    monkeypatch.setattr(babblesim, "wait_for_paths_async", wait_for_locks)
    with pytest.raises(TimeoutError, match="clock connection failed"):
        await backend.wait_for_devices_registered(timeout=0)
    wait_for_locks.assert_not_awaited()


async def test_registration_cancels_pending_connection_when_phy_exits(
    tmp_path, monkeypatch
) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    backend.add_controller()
    connecting = asyncio.Event()
    cancelled = asyncio.Event()

    async def connect(timeout):
        connecting.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    backend.clock = SimpleNamespace(connect=connect)
    backend._phy_process = SimpleNamespace(alive=lambda: not connecting.is_set())
    wait_for_locks = AsyncMock()
    monkeypatch.setattr(babblesim, "wait_for_paths_async", wait_for_locks)

    with pytest.raises(RuntimeError, match="PHY exited before clock connection"):
        await backend.wait_for_devices_registered(timeout=1)
    assert cancelled.is_set()
    wait_for_locks.assert_not_awaited()


def test_allocating_a_controller_assigns_its_capture_paths(tmp_path) -> None:
    env = make_env(tmp_path)
    backend = BabbleSimBackend(env, "sim")

    default = backend.add_controller()
    located = backend.add_controller(snoop_dir=tmp_path / "python" / "bumble")

    assert default.hci_snoop_file == env.log_dir / "controllers" / "hci0.snoop"
    assert default.rf_pcap_file == env.log_dir / "controllers" / "radio0.pcapng"
    assert located.hci_snoop_file == tmp_path / "python" / "bumble" / "hci1.snoop"
    assert located.rf_pcap_file == tmp_path / "python" / "bumble" / "radio1.pcapng"


@pytest.mark.parametrize("failing_resource", [None, "clock", "capture"])
def test_finish_releases_clock_and_capture_before_removing_simulation_state(
    tmp_path, monkeypatch, failing_resource
) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    backend.add_controller()
    calls = []
    warnings = []

    class Clock:
        def close(self):
            calls.append("clock")
            if failing_resource == "clock":
                raise RuntimeError("close failed")

    class Capture:
        def close(self):
            calls.append("capture")
            if failing_resource == "capture":
                raise OSError("write failed")

    backend.clock = Clock()
    backend._ellisys_capture = Capture()
    monkeypatch.setattr(babblesim, "warn", warnings.append)
    monkeypatch.setattr(
        backend, "cleanup_sim_artifacts", lambda: calls.append("cleanup")
    )
    backend.finish()

    assert calls == ["clock", "capture", "cleanup"]
    assert backend.clock is None
    assert backend._ellisys_capture is None
    assert (
        warnings
        == {
            None: [],
            "clock": ["simulation clock close failed: close failed"],
            "capture": ["Ellisys capture failed for sim: write failed"],
        }[failing_resource]
    )
    backend.finish()
    assert calls == ["clock", "capture", "cleanup", "cleanup"]
