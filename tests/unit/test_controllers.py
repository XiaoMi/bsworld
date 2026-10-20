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
"""Controller configuration, transport ownership and failed-start cleanup."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from tests.unit.test_babblesim_backend import _env

from bsworld.devices import BSController, ZephyrController
from bsworld.runtime.proc import ProcessManager
from bsworld.topology import BluetoothTopology


def _python_controller(tmp_path, **options):
    return BSController(0, "sim", "2G4", tmp_path, **options)


class Worker:
    instances = []
    failure = None

    def __init__(self, name, address, **options):
        self.name = name
        self.address = address
        self.options = options
        # A distinct worker default detects defaults duplicated by the adapter.
        self.max_step_us = options.get("max_step_us", 1750)
        self.started = asyncio.Event()
        self.terminated = asyncio.get_running_loop().create_future()
        self.closed = False
        self.instances.append(self)

    async def start(self, sim_id, dev_id):
        self.identity = (sim_id, dev_id)
        self.started.set()

    async def close(self):
        self.closed = True
        if not self.terminated.done():
            self.terminated.set_result(None)
        if self.failure is not None:
            raise self.failure


@pytest.fixture
def worker(monkeypatch):
    import bscontroller

    Worker.instances = []
    monkeypatch.setattr(bscontroller, "RadioController", Worker)
    return Worker


def test_mixed_controller_registration_preserves_independent_configuration(tmp_path):
    topo = BluetoothTopology(_env(tmp_path), "sim", tmp_path / "log")
    zephyr = topo.add_controller(public_address="f0:f1:f2:f3:f4:01")
    python = topo.add_controller(controller_type=BSController, public_address="f0:f1:f2:f3:f4:02", max_step_us=250)
    another = topo.add_controller(controller_type=BSController, max_step_us=500)
    topo.bsim.reserve_application_device(python)
    assert isinstance(zephyr, ZephyrController)
    assert isinstance(python, BSController)
    assert [ctl.controller_dev_id for ctl in topo.bsim.controllers] == [0, 1, 2]
    assert python.application_dev_id == 3
    assert zephyr.application_dev_id is another.application_dev_id is None
    assert python.public_address == "F0:F1:F2:F3:F4:02"
    assert python.max_step_us == 250
    assert another.max_step_us == 500
    assert len({ctl.hci_sock for ctl in topo.bsim.controllers}) == 3
    assert topo.bsim.interference_dev_id(0) == 5


@pytest.mark.parametrize("interval", [0, -1])
def test_python_controller_rejects_invalid_worker_interval(tmp_path, interval):
    with pytest.raises(ValueError, match="max_step_us must be positive"):
        _python_controller(tmp_path, max_step_us=interval)


async def test_python_controller_delegates_h4_and_capture_to_worker(tmp_path, worker):
    ctl = _python_controller(tmp_path, public_address="c0:ff:ee:00:00:01", max_step_us=250)
    await ctl.start(ProcessManager(), SimpleNamespace(), log_dir=tmp_path)
    instance = worker.instances[0]
    await instance.started.wait()
    assert instance.identity == ("sim", 0)
    assert instance.address == "C0:FF:EE:00:00:01"
    assert instance.max_step_us == 250
    assert instance.options["hci_snoop_file"] == ctl.hci_snoop_file
    assert instance.options["hci_socket"] == ctl.hci_sock
    assert ctl.hci_snoop_file == tmp_path / "bscontroller" / "hci0.snoop"
    await ctl.stop()
    assert instance.closed
    assert not ctl.hci_sock.exists()
    await ctl.stop()


async def test_python_controller_uses_worker_defaults(tmp_path, worker):
    ctl = _python_controller(tmp_path)
    await ctl.start(ProcessManager(), SimpleNamespace(), log_dir=tmp_path)
    await ctl.stop()
    assert worker.instances[0].options == {"hci_snoop_file": ctl.hci_snoop_file, "hci_socket": ctl.hci_sock}
    assert worker.instances[0].max_step_us == 1750
    assert worker.instances[0].closed
    assert not ctl.hci_sock.exists()


async def test_python_controller_propagates_worker_failure_during_cleanup(tmp_path, worker, monkeypatch):
    monkeypatch.setattr(worker, "failure", RuntimeError("worker failed"))
    ctl = _python_controller(tmp_path)
    await ctl.start(ProcessManager(), SimpleNamespace(), log_dir=tmp_path)
    await worker.instances[0].started.wait()
    with pytest.raises(RuntimeError, match="worker failed"):
        await ctl.stop()
    assert worker.instances[0].closed
    assert not ctl.hci_sock.exists()
    await ctl.stop()


async def test_python_controller_passes_application_capture_path(tmp_path, worker):
    path = tmp_path / "application" / "hci0.snoop"
    ctl = _python_controller(tmp_path, hci_snoop_file=path)
    try:
        await ctl.start(ProcessManager(), SimpleNamespace(), log_dir=tmp_path)
        assert worker.instances[0].options == {"hci_snoop_file": path, "hci_socket": ctl.hci_sock}
    finally:
        await ctl.stop()


async def test_python_controller_start_failure_preserves_existing_path(tmp_path, worker, monkeypatch):
    ctl = _python_controller(tmp_path)
    ctl.hci_sock.write_text("owned by another endpoint")
    async def fail_start(self, sim_id, dev_id):
        raise FileExistsError(ctl.hci_sock)

    monkeypatch.setattr(worker, "start", fail_start)
    with pytest.raises(FileExistsError):
        await ctl.start(ProcessManager(), SimpleNamespace(), log_dir=tmp_path)
    assert ctl.hci_sock.read_text() == "owned by another endpoint"
    assert worker.instances[0].closed
    await ctl.stop()


async def test_python_controllers_have_distinct_default_addresses(tmp_path, worker):
    first = _python_controller(tmp_path)
    second = _python_controller(tmp_path)
    second.controller_dev_id = 1
    try:
        await first.start(ProcessManager(), SimpleNamespace(), log_dir=tmp_path)
        await second.start(ProcessManager(), SimpleNamespace(), log_dir=tmp_path)
        assert len({instance.address for instance in worker.instances}) == 2
    finally:
        await first.stop()
        await second.stop()


async def test_stop_releases_other_controllers_and_phy_after_worker_failure(tmp_path, monkeypatch):
    topo = BluetoothTopology(_env(tmp_path), "sim", tmp_path / "log")
    python = topo.add_controller(controller_type=BSController)
    zephyr = topo.add_controller()
    events = []

    async def fail_stop():
        events.append("python")
        raise RuntimeError("worker failed")

    async def stop_zephyr():
        events.append("zephyr")

    monkeypatch.setattr(python, "stop", fail_stop)
    monkeypatch.setattr(zephyr, "stop", stop_zephyr)
    monkeypatch.setattr(topo.pm, "cleanup", lambda: events.append("processes"))
    monkeypatch.setattr(topo.bsim, "finish", lambda **kwargs: events.append("finish"))
    with pytest.raises(RuntimeError, match="worker failed"):
        await topo.stop()
    assert events == ["python", "zephyr", "processes", "finish"]
