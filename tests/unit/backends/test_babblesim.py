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

import pytest
from tests.unit.helpers import SpawnRecorder, make_env

from bsworld.backends import babblesim
from bsworld.backends.babblesim import BabbleSimBackend
from bsworld.devices import zephyr
from bsworld.interference import (
    BurstInterference,
    PlaybackInterference,
    StaticAttenuation,
    TimedAttenuation,
    WlanInterference,
)


def _patch_controller_bin(monkeypatch, tmp_path: Path) -> Path:
    controller = tmp_path / "build" / "hci" / "bt_virt_controller"
    controller.parent.mkdir(parents=True, exist_ok=True)
    controller.write_text("#!/bin/sh\n")
    controller.chmod(0o755)
    monkeypatch.setattr(zephyr, "controller_bin", lambda env: controller)
    return controller


def _add_process_controller(backend: BabbleSimBackend):
    controller = backend.add_controller()
    backend.reserve_application_device(controller)
    return controller


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
    matrix = babblesim._write_attenuation_matrix(
        static_paths=[],
        timed_paths=[
            TimedAttenuation(tx=0, rx=1, times_us=[0, 10], attenuation_db=[40.0, 80.0])
        ],
        out_dir=tmp_path,
    )

    timed_file = tmp_path / "timed_att_0_1_0.txt"
    assert matrix.read_text() == f'0 1 : "{timed_file}"\n'
    assert timed_file.read_text() == "0    40.0\n10    80.0\n"


def test_write_attenuation_matrix_supports_static_and_timed_paths(tmp_path) -> None:
    matrix = babblesim._write_attenuation_matrix(
        static_paths=[
            StaticAttenuation(tx=2, rx=1, attenuation_db=20.0),
            StaticAttenuation(tx=2, rx=0, attenuation_db=120.0),
        ],
        timed_paths=[
            TimedAttenuation(tx=0, rx=1, times_us=[0, 10], attenuation_db=[40.0, 80.0]),
        ],
        out_dir=tmp_path,
    )

    timed_file = tmp_path / "timed_att_0_1_0.txt"
    assert matrix.read_text() == (f"2 1 : 20.0\n2 0 : 120.0\n0 1 : \"{timed_file}\"\n")
    assert timed_file.read_text() == "0    40.0\n10    80.0\n"


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


async def test_start_rejects_a_simulation_without_controllers(tmp_path) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")

    with pytest.raises(RuntimeError, match="at least one Virtual Controller"):
        await backend.start(SpawnRecorder())


async def test_add_controller_rejects_started_backend(tmp_path, monkeypatch) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    backend.check_deps_ready = lambda: None
    backend.prepare = lambda: None
    backend.add_controller()
    _patch_controller_bin(monkeypatch, tmp_path)
    monkeypatch.setattr(babblesim, "wait_for_path", lambda *args, **kwargs: None)
    await backend.start(SpawnRecorder(), log_dir=tmp_path / "log")

    with pytest.raises(RuntimeError, match="after BabbleSim has started"):
        backend.add_controller()


async def test_start_spawns_phy_virtual_controllers_and_interference(
    tmp_path, monkeypatch
) -> None:
    env = make_env(tmp_path)
    backend = BabbleSimBackend(env, "sim")
    backend.check_deps_ready = lambda: None
    backend.prepare = lambda: None
    _add_process_controller(backend)
    _add_process_controller(backend)
    backend.interference_sources.append(BurstInterference(2402.0))
    recorder = SpawnRecorder()

    _patch_controller_bin(monkeypatch, tmp_path)
    monkeypatch.setattr(babblesim, "wait_for_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(babblesim, "wait_for_paths", lambda *args, **kwargs: None)

    await backend.start(recorder, log_dir=tmp_path / "log")

    names = [name for name, _, _ in recorder.calls]
    assert names == [
        "bs_2G4_phy_v1",
        "bt_virt_controller_hci0",
        "bt_virt_controller_hci1",
        "interf_5",
    ]
    assert "-D=6" in recorder.calls[0][1]
    assert "-defmodem=BLE_simple" in recorder.calls[0][1]
    assert "-dump" in recorder.calls[0][1]
    assert "-dump_imm" in recorder.calls[0][1]
    assert backend.clock is not None
    assert backend.clock.dev_id == 4
    assert backend.clock.sim_dir == env.sim_dir("sim")
    assert backend.clock.phy_alive()
    assert "-d=0" in recorder.calls[1][1]
    # The upstream hci_virt sample relies on -mro for 1 ms PHY resynchronization.
    assert "-mro=1000" in recorder.calls[1][1]
    # Real AES-CCM needs libCryptov1, which the HW models load from ../lib.
    assert "-RealEncryption=1" in recorder.calls[1][1]
    assert recorder.calls[1][2]["cwd"] == env.bsim_bin_dir
    assert (
        f"-hci-socket={tmp_path / 'state' / 'sim' / 'hci0.sock'}"
        in recorder.calls[1][1]
    )
    # Allocated by the backend, so the capture path is known before start.
    assert (
        f"-btsnoop-file={env.log_dir / 'controllers' / 'hci0.snoop'}"
        in recorder.calls[1][1]
    )
    assert "-d=1" in recorder.calls[2][1]
    assert (
        f"-hci-socket={tmp_path / 'state' / 'sim' / 'hci1.sock'}"
        in recorder.calls[2][1]
    )
    assert (
        f"-btsnoop-file={env.log_dir / 'controllers' / 'hci1.snoop'}"
        in recorder.calls[2][1]
    )
    assert "-d=5" in recorder.calls[3][1]
    log_dirs = {name: kwargs["log_dir"] for name, _, kwargs in recorder.calls}
    assert log_dirs["bs_2G4_phy_v1"] == tmp_path / "log" / "babblesim"
    assert (
        log_dirs["bt_virt_controller_hci0"] == tmp_path / "log" / "bt_virt_controller"
    )
    assert (
        log_dirs["bt_virt_controller_hci1"] == tmp_path / "log" / "bt_virt_controller"
    )
    assert log_dirs["interf_5"] == tmp_path / "log" / "babblesim"


async def test_start_passes_public_address_to_virtual_controller(
    tmp_path, monkeypatch
) -> None:
    env = make_env(tmp_path)
    backend = BabbleSimBackend(env, "sim")
    backend.check_deps_ready = lambda: None
    backend.prepare = lambda: None
    ctl = backend.add_controller()
    ctl.public_address = "C0:FF:EE:00:00:01"
    recorder = SpawnRecorder()

    _patch_controller_bin(monkeypatch, tmp_path)
    monkeypatch.setattr(babblesim, "wait_for_path", lambda *args, **kwargs: None)

    await backend.start(recorder, log_dir=tmp_path / "log")

    controller_args = recorder.calls[1][1]
    assert "-bd-addr=C0:FF:EE:00:00:01" in controller_args


async def test_start_places_default_modem_before_channel_plugin_args(
    tmp_path, monkeypatch
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

    _patch_controller_bin(monkeypatch, tmp_path)
    monkeypatch.setattr(babblesim, "wait_for_path", lambda *args, **kwargs: None)

    await backend.start(recorder, log_dir=tmp_path / "log")

    phy_args = recorder.calls[0][1]
    assert phy_args.index("-defmodem=BLE_simple") < phy_args.index("-argschannel")
    assert "-channel=multiatt" in phy_args
    assert "-argsmain" in phy_args


def test_device_lock_paths_cover_applications_and_interference_devices(
    tmp_path,
) -> None:
    env = make_env(tmp_path)
    backend = BabbleSimBackend(env, "sim")
    _add_process_controller(backend)
    _add_process_controller(backend)
    backend.interference_sources.append(BurstInterference(2402.0))

    assert backend.device_lock_paths() == [
        tmp_path / "state" / "sim" / "2G4.d0.lock",
        tmp_path / "state" / "sim" / "2G4.d1.lock",
        tmp_path / "state" / "sim" / "2G4.d2.lock",
        tmp_path / "state" / "sim" / "2G4.d3.lock",
        tmp_path / "state" / "sim" / "2G4.d4.lock",
        tmp_path / "state" / "sim" / "2G4.d5.lock",
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


def test_allocating_a_controller_assigns_its_capture_path(tmp_path) -> None:
    env = make_env(tmp_path)
    backend = BabbleSimBackend(env, "sim")

    default = backend.add_controller()
    located = backend.add_controller(snoop_dir=tmp_path / "python" / "bumble")

    assert default.hci_snoop_file == env.log_dir / "controllers" / "hci0.snoop"
    assert located.hci_snoop_file == tmp_path / "python" / "bumble" / "hci1.snoop"


def test_a_capture_can_be_relocated_until_the_simulation_starts(
    tmp_path, monkeypatch
) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    backend.check_deps_ready = lambda: None
    backend.prepare = lambda: None
    controller = backend.add_controller()

    backend.set_snoop_dir(controller, tmp_path / "devices" / "dev0")
    assert controller.hci_snoop_file == tmp_path / "devices" / "dev0" / "hci0.snoop"

    foreign = BabbleSimBackend(make_env(tmp_path), "sim1").add_controller()
    with pytest.raises(ValueError, match="does not belong to this BabbleSim backend"):
        backend.set_snoop_dir(foreign, tmp_path / "elsewhere")

    _patch_controller_bin(monkeypatch, tmp_path)
    monkeypatch.setattr(babblesim, "wait_for_path", lambda *args, **kwargs: None)

    asyncio.run(backend.start(SpawnRecorder(), log_dir=tmp_path / "log"))
    with pytest.raises(RuntimeError, match="after BabbleSim has started"):
        backend.set_snoop_dir(controller, tmp_path / "too-late")


@pytest.mark.parametrize("close_fails", [False, True])
def test_finish_releases_clock_before_artifacts_even_if_close_fails(
    tmp_path, monkeypatch, close_fails
) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    backend.add_controller()
    calls = []
    warnings = []

    class Clock:
        def close(self):
            calls.append("close")
            if close_fails:
                raise RuntimeError("close failed")

    backend.clock = Clock()
    monkeypatch.setattr(babblesim, "warn", warnings.append)
    monkeypatch.setattr(
        backend, "convert_ellisys", lambda **kwargs: calls.append("convert")
    )
    monkeypatch.setattr(
        backend, "cleanup_sim_artifacts", lambda: calls.append("cleanup")
    )
    backend.finish(log_dir=tmp_path / "log")

    assert calls == ["close", "convert", "cleanup"]
    assert backend.clock is None
    assert warnings == (
        ["simulation clock close failed: close failed"] if close_fails else []
    )
    backend.finish(log_dir=tmp_path / "log")
    assert calls.count("close") == 1


def test_finish_releases_clock_after_partial_start_without_controllers(
    tmp_path,
) -> None:
    backend = BabbleSimBackend(make_env(tmp_path), "sim")
    calls = []

    class Clock:
        def close(self):
            calls.append("close")

    backend.clock = Clock()
    backend.finish()
    assert calls == ["close"]
    assert backend.clock is None


def test_convert_ellisys_writes_trace_without_a_subprocess(
    tmp_path, monkeypatch
) -> None:
    env = make_env(tmp_path)
    results_dir = env.bsim_dir / "results" / "sim"
    results_dir.mkdir(parents=True)
    tx_file = results_dir / "d_2G4_0.Tx.csv"
    tx_file.write_text(
        "start_time,end_time,center_freq,phy_address,modulation,power_level,"
        "abort_time,recheck_time,packet_size,packet\n"
        "100,139,2.000000,0x8E89BED6,16,0,0,0,5,01 00 00 00 00\n"
    )
    backend = BabbleSimBackend(env, "sim")

    def unexpected_subprocess(*args, **kwargs):
        pytest.fail("Ellisys conversion must run in process")

    monkeypatch.setattr(babblesim.subprocess, "run", unexpected_subprocess)

    backend.convert_ellisys(log_dir=tmp_path / "log")

    assert (tmp_path / "log" / "babblesim" / "ellisys.bttrp").read_text() == (
        "FileFormat:Bluetooth\n                version=1.0\n\nItemFormat:LE "
        "version=1.1\n"
        'Item time=100000 aa=0x8E89BED6 rssi=-40 rfchannel=0 phy=1Mbps rawdata="01 '
        '00 00 00 00"\n'
    )


def test_convert_ellisys_warns_when_dumps_are_missing(tmp_path, monkeypatch) -> None:
    env = make_env(tmp_path)
    warnings = []
    monkeypatch.setattr(babblesim, "warn", warnings.append)

    BabbleSimBackend(env, "sim").convert_ellisys()

    assert len(warnings) == 1
    assert "no BabbleSim Tx CSV dumps" in warnings[0]
    assert not (env.log_dir / "babblesim" / "ellisys.bttrp").exists()
