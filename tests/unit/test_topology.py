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
from tests.unit.helpers import make_env

from bsworld.devices import BSController
from bsworld.interference import (
    StaticAttenuation,
    WlanInterference,
)
from bsworld.runtime.scheduler import BsimEventLoop
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


def test_topology_rejects_configuration_after_start(tmp_path) -> None:
    topo = BluetoothTopology(make_env(tmp_path), "sim0", tmp_path / "log")
    topo._started = True

    with pytest.raises(RuntimeError, match="cannot modify a topology"):
        topo.add_controller()
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


async def test_start_uses_registered_snoop_path_and_binds_clock(
    tmp_path, monkeypatch
) -> None:
    topo = BluetoothTopology(make_env(tmp_path), "sim0", tmp_path / "log")
    controller = topo.add_controller(log_subdir="dev0")
    seen_snoop_paths = []

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

    topo.bsim.clock = FakeClock()
    monkeypatch.setattr(topo.bsim, "wait_for_hci_sockets", AsyncMock())
    monkeypatch.setattr(topo.bsim, "wait_for_devices_registered", lambda: None)

    async def fake_backend_start(pm, *, log_dir):  # noqa: ANN001, ARG001
        seen_snoop_paths.append(controller.hci_snoop_file)

    monkeypatch.setattr(topo.bsim, "start", fake_backend_start)
    monkeypatch.setattr(topo.bsim, "finish", lambda: None)

    loop = asyncio.get_running_loop()
    assert isinstance(loop, BsimEventLoop)
    await topo.start()
    try:
        assert seen_snoop_paths == [tmp_path / "log" / "dev0" / "hci0.snoop"]
        assert loop.radio_selector.clock is topo.bsim.clock
        assert topo.bsim.clock.synchronized
    finally:
        await topo.stop()
    assert loop.radio_selector.clock is None


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
    monkeypatch.setattr(topo.bsim, "wait_for_devices_registered", lambda: None)
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


async def test_cancelled_start_waits_for_registration_before_closing_clock(
    tmp_path, monkeypatch
) -> None:
    topo = BluetoothTopology(make_env(tmp_path), "sim", tmp_path / "log")
    topo.add_controller()
    registering = asyncio.Event()
    release = asyncio.Event()
    events = []
    monkeypatch.setattr(topo.bsim, "start", AsyncMock())
    monkeypatch.setattr(topo.bsim, "wait_for_hci_sockets", AsyncMock())
    monkeypatch.setattr(topo.bsim, "finish", lambda **kwargs: events.append("finish"))

    async def run_worker(fn, *args, **kwargs):
        if fn == topo.bsim.wait_for_devices_registered:
            registering.set()
            await release.wait()
            events.append("registered")
        else:
            fn(*args, **kwargs)

    monkeypatch.setattr("bsworld.topology.asyncio.to_thread", run_worker)
    start = asyncio.create_task(topo.start())
    await registering.wait()

    start.cancel()
    await asyncio.sleep(0)
    assert events == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await start
    await topo.stop()
    assert events == ["registered", "finish"]


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
    monkeypatch.setattr(topo.bsim, "finish", lambda: events.append("finish"))

    if cleanup_fails:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await topo.stop()
    else:
        await topo.stop()

    assert events == ["to_thread", "cleanup", "restore", "finish"]
