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
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bscontroller import ControllerConfig
from bscontroller.runtime.event_loop import RadioEventLoop
from tests.unit.helpers import make_env

from bsworld.backends import babblesim
from bsworld.devices import BSController
from bsworld.interference import (
    StaticAttenuation,
    WlanInterference,
)
from bsworld.topology import BluetoothTopology


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
    assert ctl.hci_sock == tmp_path / "state" / "sim0" / "hci0.sock"


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
    source = topo.bsim.interference_sources[ref]
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


async def test_start_binds_clock_after_device_registration(
    tmp_path, monkeypatch
) -> None:
    topo = BluetoothTopology(make_env(tmp_path), "sim0", tmp_path / "log")
    topo.add_controller()
    events = []

    class FakeClock:
        now_us = 0

        def bind(self, loop) -> None:
            assert loop.radio_selector.clock is None
            events.append("bind")
            loop.bind_clock(self)

    topo.bsim.clock = FakeClock()
    monkeypatch.setattr(topo.bsim, "start", AsyncMock())
    monkeypatch.setattr(
        topo.bsim,
        "wait_for_hci_sockets",
        AsyncMock(side_effect=lambda: events.append("hci ready")),
    )
    monkeypatch.setattr(
        topo.bsim,
        "wait_for_devices_registered",
        AsyncMock(side_effect=lambda: events.append("registered")),
    )
    monkeypatch.setattr(topo.bsim, "finish", lambda: None)

    loop = asyncio.get_running_loop()
    assert isinstance(loop, RadioEventLoop)
    await topo.start()
    try:
        assert events == ["hci ready", "registered", "bind"]
        assert loop.radio_selector.clock is topo.bsim.clock
        with pytest.raises(RuntimeError, match="cannot modify a topology"):
            topo.add_controller()
        with pytest.raises(RuntimeError, match="cannot modify a topology"):
            topo.set_channel_attenuation(default_db=50.0)
        with pytest.raises(RuntimeError, match="cannot modify a topology"):
            topo.add_burst_interference(2402.0)
    finally:
        await topo.stop()
    assert loop.radio_selector.clock is None


async def test_start_rolls_back_when_second_controller_fails(
    tmp_path, monkeypatch
) -> None:
    topo = BluetoothTopology(make_env(tmp_path), "sim", tmp_path / "log")
    sim_dir = topo.env.sim_dir(topo.sim_id)
    topo.add_controller()
    topo.add_controller()
    startup_error = RuntimeError("second Controller failed")
    events = []

    def spawn_phy(name, argv, **options):
        assert name == "bs_2G4_phy_v1"
        sim_dir.mkdir(parents=True)
        (sim_dir / "2G4.phy.lock").touch()
        events.append("PHY started")
        return SimpleNamespace(alive=lambda: True)

    class Worker:
        def __init__(self, name, address, **options):
            self.name = name

        async def start(self, sim_id, dev_id):
            events.append(f"{self.name} started")
            if dev_id == 1:
                raise startup_error

        async def close(self):
            events.append(f"{self.name} closed")

    monkeypatch.setattr("bscontroller.RadioController", Worker)
    monkeypatch.setattr(babblesim, "check_babblesim_ready", lambda env: None)
    monkeypatch.setattr(topo.bsim, "prepare", lambda: None)
    monkeypatch.setattr(topo.pm, "spawn", spawn_phy)
    monkeypatch.setattr(topo.pm, "cleanup", lambda: events.append("PHY stopped"))

    loop = asyncio.get_running_loop()
    assert isinstance(loop, RadioEventLoop)
    try:
        with pytest.raises(RuntimeError) as failure:
            await topo.start()

        assert failure.value is startup_error
        assert events == [
            "PHY started",
            "hci0 started",
            "hci1 started",
            "hci1 closed",
            "hci0 closed",
            "PHY stopped",
        ]
        assert loop.radio_selector.clock is None
        assert topo.bsim.clock is None
        assert not sim_dir.exists()
        assert (topo.test_log_dir / "babblesim" / "ellisys.bttrp").is_file()
    finally:
        await topo.stop()


def test_channel_attenuation_keeps_its_own_path_list(tmp_path) -> None:
    topo = BluetoothTopology(make_env(tmp_path), "sim", tmp_path / "log")
    host, peer = topo.add_controller(), topo.add_controller()
    path = StaticAttenuation(tx=host, rx=peer, attenuation_db=55.0)
    paths = [path]

    topo.set_channel_attenuation(static_paths=paths)
    paths.clear()

    assert topo.bsim.channel_attenuation.static_paths == [path]


async def test_concurrent_starts_share_one_startup(tmp_path, monkeypatch) -> None:
    topo = BluetoothTopology(make_env(tmp_path), "sim", tmp_path / "log")
    topo.add_controller()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def start_backend(*args, **kwargs):
        entered.set()
        await release.wait()

    start = AsyncMock(side_effect=start_backend)
    monkeypatch.setattr(topo.bsim, "start", start)
    monkeypatch.setattr(topo.bsim, "wait_for_hci_sockets", AsyncMock())
    monkeypatch.setattr(topo.bsim, "wait_for_devices_registered", AsyncMock())
    first = asyncio.create_task(topo.start())
    await entered.wait()
    second = asyncio.create_task(topo.start())
    try:
        await asyncio.sleep(0)
        assert start.await_count == 1
        with pytest.raises(RuntimeError, match="cannot modify a topology"):
            topo.set_channel_attenuation(default_db=50)
    finally:
        release.set()
        try:
            await asyncio.gather(first, second)
        finally:
            await topo.stop()

    assert start.await_count == 1
    with pytest.raises(RuntimeError, match="cannot start a stopped topology"):
        await topo.start()


async def test_cancelled_start_cleans_up_registration_before_closing_clock(
    tmp_path, monkeypatch
) -> None:
    topo = BluetoothTopology(make_env(tmp_path), "sim", tmp_path / "log")
    topo.add_controller()
    registering = asyncio.Event()
    events = []
    monkeypatch.setattr(topo.bsim, "start", AsyncMock())
    monkeypatch.setattr(topo.bsim, "wait_for_hci_sockets", AsyncMock())
    monkeypatch.setattr(topo.bsim, "finish", lambda **kwargs: events.append("finish"))

    async def connect(timeout):
        registering.set()
        try:
            await asyncio.Future()
        finally:
            events.append("connection cancelled")

    topo.bsim.clock = SimpleNamespace(connect=connect)
    start = asyncio.create_task(topo.start())
    await registering.wait()

    try:
        start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start
        assert events == ["connection cancelled", "finish"]
    finally:
        await topo.stop()


async def test_stop_reports_guest_failure_after_releasing_the_topology(
    tmp_path, monkeypatch
) -> None:
    topo = BluetoothTopology(make_env(tmp_path), "sim", tmp_path / "log")
    events = []

    def failed_guest():
        events.append("guest")
        raise TimeoutError("guest stopped responding")

    topo._uml_guests.append(SimpleNamespace(stop=failed_guest))
    topo._event_loop = SimpleNamespace(unbind_clock=lambda: events.append("unbind"))
    monkeypatch.setattr(topo.bsim, "finish", lambda **kwargs: events.append("finish"))

    with pytest.raises(TimeoutError, match="guest stopped responding"):
        await topo.stop()

    assert events == ["guest", "unbind", "finish"]


async def test_stop_releases_other_controllers_and_phy_after_worker_failure(
    tmp_path, monkeypatch
):
    topo = BluetoothTopology(make_env(tmp_path), "sim", tmp_path / "log")
    first = topo.add_controller()
    second = topo.add_controller()
    stop_second = AsyncMock()
    stopped = []

    fail_stop = AsyncMock(side_effect=RuntimeError("worker failed"))
    monkeypatch.setattr(first, "stop", fail_stop)
    monkeypatch.setattr(second, "stop", stop_second)
    monkeypatch.setattr(topo.pm, "cleanup", lambda: stopped.append("PHY"))
    with pytest.raises(RuntimeError, match="worker failed"):
        await topo.stop()
    stop_second.assert_awaited_once()
    assert stopped == ["PHY"]


async def test_stop_finalizes_backend_even_if_process_cleanup_fails(
    tmp_path, monkeypatch
) -> None:
    events: list[str] = []
    topo = BluetoothTopology(make_env(tmp_path), "sim0", tmp_path / "log")

    class FakeProcessManager:
        def cleanup(self) -> None:
            events.append("cleanup")
            raise RuntimeError("cleanup failed")

        def restore_signal_handlers(self) -> None:
            events.append("restore")

    topo.pm = FakeProcessManager()
    monkeypatch.setattr(topo.bsim, "finish", lambda: events.append("finish"))

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await topo.stop()

    assert events == ["cleanup", "restore", "finish"]
