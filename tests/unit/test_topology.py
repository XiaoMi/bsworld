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
from unittest.mock import AsyncMock

import pytest
from bscontroller import ControllerConfig
from tests.unit.helpers import make_env, register_process

from bsworld.devices import BSController
from bsworld.devices.instance import ProcessInstance
from bsworld.devices.layout import AppSlot
from bsworld.interference import (
    StaticAttenuation,
    TimedAttenuation,
    WlanInterference,
)
from bsworld.runtime.scheduler import BsimEventLoop
from bsworld.topology import BluetoothTopology


def test_add_process_registers_controller_application_device(tmp_path) -> None:
    exe = tmp_path / "host"
    exe.write_text("")
    topo = BluetoothTopology(make_env(tmp_path), "sim0", tmp_path / "log")

    dev = register_process(topo, exe=exe, label="dev0", args=("--foo=bar",))

    assert dev.label == "dev0"
    assert dev.controller is topo.bsim.controllers[0]
    assert topo.bsim.plan_devices().application_slot(dev).dev_id == 1
    assert dev.controller.hci_sock == tmp_path / "state" / "sim0" / "hci0.sock"


def test_add_controller_allocates_bscontroller_with_its_configuration(tmp_path) -> None:
    topo = BluetoothTopology(make_env(tmp_path), "sim0", tmp_path / "log")
    config = ControllerConfig(max_connections=2)

    ctl = topo.add_controller(
        public_address="c0:ff:ee:00:00:03", config=config, max_step_us=250
    )

    assert isinstance(ctl, BSController)
    assert ctl is topo.bsim.controllers[0]
    assert ctl.public_address == "C0:FF:EE:00:00:03"
    assert ctl.config is config
    assert ctl.max_step_us == 250
    with pytest.raises(KeyError, match="no Application device reserved"):
        topo.bsim.plan_devices().application_slot(ctl)
    assert ctl.hci_sock == tmp_path / "state" / "sim0" / "hci0.sock"


def test_add_process_rejects_duplicate_label(tmp_path) -> None:
    exe = tmp_path / "host"
    exe.write_text("")
    topo = BluetoothTopology(make_env(tmp_path), "sim0", tmp_path / "log")
    original = register_process(topo, exe=exe, label="device")

    with pytest.raises(ValueError, match="label is already registered"):
        register_process(topo, exe=exe, label="device")
    assert topo.processes == (original,)
    assert len(topo.bsim.controllers) == 1


def test_add_process_rejects_shared_controller(tmp_path) -> None:
    exe = tmp_path / "host"
    exe.write_text("")
    topo = BluetoothTopology(make_env(tmp_path), "sim0", tmp_path / "log")
    first = register_process(topo, exe=exe, label="first")
    second = ProcessInstance(command=[exe], controller=first.controller, label="second")

    with pytest.raises(ValueError, match="only be attached to one Application"):
        topo.add_process(second)
    assert topo.processes == (first,)
    assert topo.bsim.plan_devices().device_count == 3


def test_topology_rejects_configuration_after_start(tmp_path) -> None:
    topo = BluetoothTopology(make_env(tmp_path), "sim0", tmp_path / "log")
    controller = topo.add_controller()
    process = ProcessInstance(
        command=[tmp_path / "host"], controller=controller, label="device"
    )
    topo._started = True

    with pytest.raises(RuntimeError, match="cannot modify a topology"):
        topo.add_controller()
    with pytest.raises(RuntimeError, match="cannot modify a topology"):
        topo.add_process(process)
    with pytest.raises(RuntimeError, match="cannot modify a topology"):
        topo.set_channel_attenuation(default_db=50.0)
    with pytest.raises(RuntimeError, match="cannot modify a topology"):
        topo.add_burst_interference(2402.0)


def test_add_wlan_interference_exposes_full_wlan_parameters(tmp_path) -> None:
    topo = BluetoothTopology(make_env(tmp_path), "sim0", tmp_path / "log")

    ref = topo.add_wlan_interference(
        center_freq_mhz=2437.0,
        power_dbm=11.0,
        config_set="VideoConf",
        mod_type="WN20",
        windows=[(5, 10)],
        rand_seed=7,
    )

    assert ref.index == 0
    source = topo.bsim.interference_sources[0]
    assert isinstance(source, WlanInterference)
    assert source.center_freq_mhz == 2437.0
    assert source.power_dbm == 11.0
    assert source.config_set == "VideoConf"
    assert source.mod_type == "WN20"
    assert source.windows == [(5, 10)]
    assert source.rand_seed == 7


async def test_start_requires_a_controller(tmp_path) -> None:
    topo = BluetoothTopology(make_env(tmp_path), "sim0", tmp_path / "log")

    with pytest.raises(RuntimeError, match="at least one BSController"):
        await topo.start()
    # Teardown still has to release whatever the failed start opened.
    await topo.stop()


async def test_start_uses_registered_snoop_path_and_binds_clock(
    tmp_path, monkeypatch
) -> None:
    topo = BluetoothTopology(make_env(tmp_path), "sim0", tmp_path / "log")
    exe = tmp_path / "host"
    exe.write_text("")
    dev = register_process(topo, exe=exe, label="dev0")
    seen_snoop_paths: list[Path] = []
    started_slots: list[AppSlot] = []

    class FakeClock:
        now_us = 0
        synchronized = False

        def synchronize(self) -> None:
            assert asyncio.get_running_loop().radio_selector.clock is None
            self.synchronized = True

        def advance_to_us(self, target_us: int) -> int:
            self.now_us = target_us
            return self.now_us

        def close(self) -> None:
            return None

    def fake_start(pm, log_dir, slot):  # noqa: ANN001, ARG001
        started_slots.append(slot)
        return None

    topo.bsim.clock = FakeClock()
    monkeypatch.setattr(dev, "start", fake_start)
    monkeypatch.setattr(topo.bsim, "wait_for_hci_sockets", AsyncMock())
    monkeypatch.setattr(topo.bsim, "wait_for_devices_registered", lambda: None)

    async def fake_backend_start(pm, *, log_dir):  # noqa: ANN001, ARG001
        seen_snoop_paths.append(dev.controller.hci_snoop_file)

    monkeypatch.setattr(topo.bsim, "start", fake_backend_start)
    monkeypatch.setattr(topo.bsim, "finish", lambda *, log_dir: None)

    loop = asyncio.get_running_loop()
    assert isinstance(loop, BsimEventLoop)
    await topo.start()
    try:
        assert seen_snoop_paths == [tmp_path / "log" / "dev0" / "hci0.snoop"]
        assert started_slots == [
            AppSlot(
                sim_id="sim0",
                phy_id="2G4",
                dev_id=1,
                hci_sock=tmp_path / "state" / "sim0" / "hci0.sock",
            )
        ]
        assert loop.radio_selector.clock is topo.bsim.clock
        assert topo.bsim.clock.synchronized
    finally:
        await topo.stop()
    assert loop.radio_selector.clock is None


@pytest.mark.parametrize("application", [False, True])
def test_snoop_path_uses_the_controller_or_application_log_directory(
    tmp_path, application
):
    topo = BluetoothTopology(make_env(tmp_path), "sim0", tmp_path / "log")
    ctl = topo.add_controller(log_subdir="python/bumble")
    assert ctl.hci_snoop_file == tmp_path / "log" / "python/bumble" / "hci0.snoop"
    if application:
        exe = tmp_path / "host"
        exe.touch()
        topo.add_process(
            ProcessInstance(
                command=[exe], controller=ctl, label="dev0", log_subdir="devices/dev0"
            )
        )
    subdir = "devices/dev0" if application else "python/bumble"
    assert ctl.hci_snoop_file == tmp_path / "log" / subdir / "hci0.snoop"


def test_channel_attenuation_resolves_processes_to_virtual_controllers(
    tmp_path,
) -> None:
    topo = BluetoothTopology(make_env(tmp_path), "sim0", tmp_path / "log")
    host_exe = tmp_path / "host"
    dev_exe = tmp_path / "device"
    host_exe.write_text("")
    dev_exe.write_text("")
    host = register_process(
        topo,
        exe=host_exe,
        label="host",
        log_subdir=Path("devices") / "host",
    )
    device = register_process(
        topo,
        exe=dev_exe,
        label="device",
        log_subdir=Path("devices") / "device",
    )
    jammer = topo.add_burst_interference(2402.0)
    assert [process.controller.controller_dev_id for process in topo.processes] == [
        0,
        1,
    ]
    layout = topo.bsim.plan_devices()
    dev_ids = [layout.application_slot(process).dev_id for process in topo.processes]
    assert dev_ids == [2, 3]

    topo.set_channel_attenuation(
        default_db=90.0,
        static_paths=[
            StaticAttenuation(tx=host, rx=device, attenuation_db=55.0),
            StaticAttenuation(tx=jammer, rx=device, attenuation_db=20.0),
            StaticAttenuation(tx=jammer, rx=host, attenuation_db=120.0),
        ],
        timed_paths=[
            TimedAttenuation(
                tx=host,
                rx=device,
                times_us=[0, 10],
                attenuation_db=[40.0, 80.0],
            ),
        ],
    )

    topo._resolve_channel_attenuation()

    ca = topo.bsim.channel_attenuation
    assert ca is not None
    assert ca.static_paths == [
        StaticAttenuation(tx=0, rx=1, attenuation_db=55.0),
        StaticAttenuation(tx=5, rx=1, attenuation_db=20.0),
        StaticAttenuation(tx=5, rx=0, attenuation_db=120.0),
    ]
    assert ca.timed_paths == [
        TimedAttenuation(tx=0, rx=1, times_us=[0, 10], attenuation_db=[40.0, 80.0]),
    ]


def test_channel_attenuation_rejects_foreign_controller(tmp_path) -> None:
    topo = BluetoothTopology(make_env(tmp_path), "sim0", tmp_path / "log")
    other = BluetoothTopology(make_env(tmp_path), "sim1", tmp_path / "other-log")
    local = topo.add_controller()
    foreign = other.add_controller()
    topo.set_channel_attenuation(
        static_paths=[StaticAttenuation(tx=local, rx=foreign, attenuation_db=50.0)],
    )

    with pytest.raises(ValueError, match="does not belong to this topology"):
        topo._resolve_channel_attenuation()


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_stop_finalizes_backend_even_if_process_cleanup_fails(
    tmp_path, monkeypatch, cleanup_fails
) -> None:
    events: list[str] = []
    topo = BluetoothTopology(make_env(tmp_path), "sim0", tmp_path / "log")

    class FakeProcessManager:
        def cleanup(self) -> None:
            events.append("cleanup")
            if cleanup_fails:
                raise RuntimeError("cleanup failed")

        def restore_signal_handlers(self) -> None:
            events.append("restore")

    async def fake_to_thread(fn, *args, **kwargs):
        events.append("to_thread")
        return fn(*args, **kwargs)

    topo.pm = FakeProcessManager()
    monkeypatch.setattr("bsworld.topology.asyncio.to_thread", fake_to_thread)
    monkeypatch.setattr(topo.bsim, "finish", lambda *, log_dir: events.append("finish"))

    if cleanup_fails:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await topo.stop()
    else:
        await topo.stop()

    assert events == ["to_thread", "cleanup", "restore", "finish"]
