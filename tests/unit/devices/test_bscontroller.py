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

import pytest
from tests.unit.helpers import make_env

from bsworld.devices import BSController
from bsworld.topology import BluetoothTopology


def _controller(tmp_path, **options):
    # The backend assigns hci_snoop_file when it allocates a Controller; a
    # directly constructed one has to supply it the same way.
    options.setdefault("hci_snoop_file", tmp_path / "bscontroller" / "hci0.snoop")
    return BSController(0, "sim", tmp_path, **options)


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


@pytest.mark.parametrize("interval", [0, -1])
def test_controller_rejects_invalid_worker_interval(tmp_path, interval):
    with pytest.raises(ValueError, match="max_step_us must be positive"):
        _controller(tmp_path, max_step_us=interval)


async def test_controller_delegates_h4_and_capture_to_worker(tmp_path, worker):
    ctl = _controller(tmp_path, public_address="c0:ff:ee:00:00:01", max_step_us=250)
    await ctl.start()
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


async def test_controller_uses_worker_defaults(tmp_path, worker):
    ctl = _controller(tmp_path)
    await ctl.start()
    await ctl.stop()
    assert worker.instances[0].options == {
        "hci_snoop_file": ctl.hci_snoop_file,
        "hci_socket": ctl.hci_sock,
    }
    assert worker.instances[0].max_step_us == 1750
    assert worker.instances[0].closed
    assert not ctl.hci_sock.exists()


async def test_controller_propagates_worker_failure_during_cleanup(
    tmp_path, worker, monkeypatch
):
    monkeypatch.setattr(worker, "failure", RuntimeError("worker failed"))
    ctl = _controller(tmp_path)
    await ctl.start()
    await worker.instances[0].started.wait()
    with pytest.raises(RuntimeError, match="worker failed"):
        await ctl.stop()
    assert worker.instances[0].closed
    assert not ctl.hci_sock.exists()
    await ctl.stop()


async def test_controller_passes_application_capture_path(tmp_path, worker):
    path = tmp_path / "application" / "hci0.snoop"
    ctl = _controller(tmp_path, hci_snoop_file=path)
    try:
        await ctl.start()
        assert worker.instances[0].options == {
            "hci_snoop_file": path,
            "hci_socket": ctl.hci_sock,
        }
    finally:
        await ctl.stop()


async def test_controller_start_failure_preserves_existing_path(
    tmp_path, worker, monkeypatch
):
    ctl = _controller(tmp_path)
    ctl.hci_sock.write_text("owned by another endpoint")

    async def fail_start(self, sim_id, dev_id):
        raise FileExistsError(ctl.hci_sock)

    monkeypatch.setattr(worker, "start", fail_start)
    with pytest.raises(FileExistsError):
        await ctl.start()
    assert ctl.hci_sock.read_text() == "owned by another endpoint"
    assert worker.instances[0].closed
    await ctl.stop()


async def test_controllers_have_distinct_default_addresses(tmp_path, worker):
    first = _controller(tmp_path)
    second = _controller(tmp_path)
    second.controller_dev_id = 1
    try:
        await first.start()
        await second.start()
        assert len({instance.address for instance in worker.instances}) == 2
    finally:
        await first.stop()
        await second.stop()


async def test_stop_releases_other_controllers_and_phy_after_worker_failure(
    tmp_path, monkeypatch
):
    topo = BluetoothTopology(make_env(tmp_path), "sim", tmp_path / "log")
    first = topo.add_controller()
    second = topo.add_controller()
    events = []

    async def fail_stop():
        events.append("first")
        raise RuntimeError("worker failed")

    async def stop_second():
        events.append("second")

    monkeypatch.setattr(first, "stop", fail_stop)
    monkeypatch.setattr(second, "stop", stop_second)
    monkeypatch.setattr(topo.pm, "cleanup", lambda: events.append("processes"))
    monkeypatch.setattr(topo.bsim, "finish", lambda **kwargs: events.append("finish"))
    with pytest.raises(RuntimeError, match="worker failed"):
        await topo.stop()
    assert events == ["first", "second", "processes", "finish"]


async def test_controller_passes_its_config_to_the_worker(tmp_path, worker):
    """Each Controller's ``ControllerConfig`` reaches its own worker unchanged."""
    from bscontroller import ControllerConfig

    first = ControllerConfig(max_connections=1)
    second = ControllerConfig(advertising_set_count=2)
    controllers = [
        _controller(tmp_path, config=first),
        _controller(tmp_path, config=second),
    ]
    controllers[1].controller_dev_id = 1
    try:
        for ctl in controllers:
            await ctl.start()
        assert [i.options["config"] for i in worker.instances] == [first, second]
    finally:
        for ctl in controllers:
            await ctl.stop()


@pytest.mark.parametrize(
    "config",
    [{"controllers": {"hci0": {"maxConnections": 1}}}, "controllers.json"],
    ids=["mapping", "path"],
)
def test_controller_requires_a_controller_config(tmp_path, config):
    """Only a ``ControllerConfig`` is accepted as ``config``."""
    with pytest.raises(TypeError, match="ControllerConfig"):
        _controller(tmp_path, config=config)
