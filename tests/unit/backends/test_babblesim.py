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

from pathlib import Path

import pytest
from tests.unit.helpers import SpawnRecorder, make_env

from bsworld.backends import babblesim
from bsworld.backends.babblesim import BabbleSimBackend
from bsworld.devices import BSController
from bsworld.interference import (
    BurstInterference,
    PlaybackInterference,
    StaticAttenuation,
    TimedAttenuation,
    WlanInterference,
)


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


def test_write_timed_matrix_keeps_partial_paths_inspectable(tmp_path) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    matrix = babblesim._write_attenuation_matrix(
        static_paths=[],
        timed_paths=[
            TimedAttenuation(tx=0, rx=1, times_us=[0, 10], attenuation_db=[40.0, 80.0])
        ],
        out_dir=tmp_path,
        layout=backend.plan_devices(),
    )

    timed_file = tmp_path / "timed_att_0_1_0.txt"
    assert matrix.read_text() == f'0 1 : "{timed_file}"\n'
    assert timed_file.read_text() == "0    40.0\n10    80.0\n"


def test_write_attenuation_matrix_supports_static_and_timed_paths(tmp_path) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    first = backend.add_controller()
    second = backend.add_controller()
    jammer = backend.add_interference_source(BurstInterference(2402.0))
    static = [
        StaticAttenuation(tx=jammer, rx=second, attenuation_db=20.0),
        StaticAttenuation(tx=jammer, rx=first, attenuation_db=120.0),
    ]
    timed = [
        TimedAttenuation(
            tx=first, rx=second, times_us=[0, 10], attenuation_db=[40.0, 80.0]
        ),
    ]
    matrix = babblesim._write_attenuation_matrix(
        static_paths=static,
        timed_paths=timed,
        out_dir=tmp_path,
        layout=backend.plan_devices(),
    )

    timed_file = tmp_path / "timed_att_0_1_0.txt"
    assert matrix.read_text() == (f"3 1 : 20.0\n3 0 : 120.0\n0 1 : \"{timed_file}\"\n")
    assert timed_file.read_text() == "0    40.0\n10    80.0\n"
    assert static[0].tx is jammer
    assert static[0].rx is second
    assert timed[0].tx is first
    assert timed[0].rx is second


def test_check_deps_ready_only_validates_built_artifacts(tmp_path, monkeypatch) -> None:
    env = make_env(tmp_path)
    backend = BabbleSimBackend(env, "sim")
    calls: list[str] = []

    monkeypatch.setattr(
        babblesim,
        "check_babblesim_ready",
        lambda build_env: calls.append(f"bsim:{build_env is env}"),
    )

    backend.check_deps_ready()

    assert calls == ["bsim:True"]


async def test_start_rejects_an_empty_simulation(tmp_path) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")

    with pytest.raises(RuntimeError, match="at least one BSController"):
        await backend.start(SpawnRecorder())


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


async def test_start_launches_phy_controller_workers_and_interference(
    tmp_path, monkeypatch, controller_starts
) -> None:
    env = make_env(tmp_path)
    backend = BabbleSimBackend(env, "sim")
    backend.check_deps_ready = lambda: None
    backend.prepare = lambda: None
    first = backend.add_controller()
    second = backend.add_controller()
    backend.add_interference_source(BurstInterference(2402.0))
    recorder = SpawnRecorder()

    monkeypatch.setattr(babblesim, "wait_for_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(babblesim, "wait_for_paths", lambda *args, **kwargs: None)

    await backend.start(recorder, log_dir=tmp_path / "log")

    assert [controller for controller, _ in controller_starts] == [first, second]
    capture = controller_starts[0][1]
    assert controller_starts[1][1] is capture
    assert capture.path == tmp_path / "log" / "babblesim" / "ellisys.bttrp"
    assert [name for name, _, _ in recorder.calls] == ["bs_2G4_phy_v1", "interf_3"]
    assert "-D=4" in recorder.calls[0][1]
    assert "-defmodem=BLE_simple" in recorder.calls[0][1]
    assert "-nodump" in recorder.calls[0][1]
    assert "-dump" not in recorder.calls[0][1]
    assert "-dump_imm" not in recorder.calls[0][1]
    assert backend.clock is not None
    assert backend.clock.dev_id == 2
    assert backend.clock.sim_dir == env.sim_dir("sim")
    assert backend.clock.phy_alive()
    assert "-d=3" in recorder.calls[1][1]
    log_dirs = {name: kwargs["log_dir"] for name, _, kwargs in recorder.calls}
    assert log_dirs["bs_2G4_phy_v1"] == tmp_path / "log" / "babblesim"
    assert log_dirs["interf_3"] == tmp_path / "log" / "babblesim"


async def test_start_places_default_modem_before_channel_plugin_args(
    tmp_path, monkeypatch, controller_starts
) -> None:
    env = make_env(tmp_path)
    backend = BabbleSimBackend(env, "sim")
    backend.check_deps_ready = lambda: None
    backend.prepare = lambda: None
    backend.add_controller()
    backend.channel_attenuation = babblesim.ChannelAttenuation(
        default_attenuation_db=70.0
    )
    recorder = SpawnRecorder()

    monkeypatch.setattr(babblesim, "wait_for_path", lambda *args, **kwargs: None)

    await backend.start(recorder, log_dir=tmp_path / "log")

    phy_args = recorder.calls[0][1]
    assert phy_args.index("-defmodem=BLE_simple") < phy_args.index("-argschannel")
    assert "-channel=multiatt" in phy_args
    assert phy_args.index("-argsmain") < phy_args.index("-nodump")


def test_device_lock_paths_cover_controllers_clock_and_interference(
    tmp_path,
) -> None:
    env = make_env(tmp_path)
    backend = BabbleSimBackend(env, "sim")
    backend.add_controller()
    backend.add_controller()
    backend.add_interference_source(BurstInterference(2402.0))

    assert backend.device_lock_paths() == [
        tmp_path / "state" / "sim" / "2G4.d0.lock",
        tmp_path / "state" / "sim" / "2G4.d1.lock",
        tmp_path / "state" / "sim" / "2G4.d2.lock",
        tmp_path / "state" / "sim" / "2G4.d3.lock",
    ]


def test_registration_connects_clock_before_waiting_for_all_device_locks(
    tmp_path, monkeypatch
) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    backend.add_controller()
    calls = []

    class Clock:
        dev_id = 1

        def connect(self, *, timeout):
            calls.append(("connect", timeout))

    backend.clock = Clock()
    monkeypatch.setattr(
        babblesim,
        "wait_for_paths",
        lambda paths, *, timeout: calls.append((paths, timeout)),
    )
    backend.wait_for_devices_registered(timeout=5)
    assert calls == [
        ("connect", 5),
        (backend.device_lock_paths(), 5),
    ]


def test_registration_does_not_wait_for_locks_after_clock_connection_failure(
    tmp_path, monkeypatch
) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    backend.add_controller()

    class Clock:
        dev_id = 1

        def connect(self, *, timeout):
            raise TimeoutError("clock connection failed")

    backend.clock = Clock()
    monkeypatch.setattr(
        babblesim,
        "wait_for_paths",
        lambda *args, **kwargs: pytest.fail("clock not connected"),
    )
    with pytest.raises(TimeoutError, match="clock connection failed"):
        backend.wait_for_devices_registered(timeout=0)


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


def test_finish_releases_clock_and_keeps_capture_after_partial_start(
    tmp_path,
) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    calls = []

    class Clock:
        def close(self):
            calls.append("close")

    backend.clock = Clock()
    trace_path = tmp_path / "log" / "babblesim" / "ellisys.bttrp"
    backend._ellisys_capture = babblesim.EllisysCapture(trace_path)
    backend.finish()
    assert calls == ["close"]
    assert backend.clock is None
    assert trace_path.is_file()
