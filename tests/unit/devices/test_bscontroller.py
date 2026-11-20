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
import socket

import pytest
from bscontroller import EllisysCapture

from bsworld.devices import BSController


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
        self.closed = False
        self.instances.append(self)

    async def start(self, sim_id, dev_id):
        self.identity = (sim_id, dev_id)

    async def close(self):
        self.closed = True
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
    rf_capture = tmp_path / "bscontroller" / "radio0.pcapng"
    ellisys_capture = EllisysCapture(tmp_path / "babblesim" / "ellisys.bttrp")
    ctl = _controller(
        tmp_path,
        public_address="c0:ff:ee:00:00:01",
        rf_pcap_file=rf_capture,
        max_step_us=250,
    )
    await ctl.start(ellisys_capture=ellisys_capture)
    instance = worker.instances[0]
    assert instance.identity == ("sim", 0)
    assert instance.address == "C0:FF:EE:00:00:01"
    assert instance.max_step_us == 250
    assert instance.options["hci_snoop_file"] == ctl.hci_snoop_file
    assert instance.options["hci_socket"] == ctl.hci_sock
    assert instance.options["rf_pcap_file"] == rf_capture
    assert instance.options["ellisys_capture"] is ellisys_capture
    assert ctl.hci_snoop_file == tmp_path / "bscontroller" / "hci0.snoop"
    await ctl.stop()
    assert instance.closed
    await ctl.stop()


async def test_controller_propagates_worker_failure_during_cleanup(
    tmp_path, worker, monkeypatch
):
    monkeypatch.setattr(worker, "failure", RuntimeError("worker failed"))
    ctl = _controller(tmp_path)
    await ctl.start()
    with pytest.raises(RuntimeError, match="worker failed"):
        await ctl.stop()
    assert worker.instances[0].closed
    await ctl.stop()


async def test_hci_disconnect_drains_output_until_peer_eof(tmp_path):
    ctl = _controller(tmp_path)
    loop = asyncio.get_running_loop()
    host, peer = socket.socketpair()
    with host, peer:
        host.setblocking(False)
        peer.setblocking(False)
        await loop.sock_sendall(peer, b"queued output")
        ctl._disconnect_hci(host)
        try:
            async with asyncio.timeout(1):
                assert await loop.sock_recv(peer, 1) == b""
                await loop.sock_sendall(peer, b"final output")
                peer.shutdown(socket.SHUT_WR)
                while host.fileno() != -1:
                    await asyncio.sleep(0)
        finally:
            await ctl.stop()


async def test_controller_passes_capture_path_and_preserves_worker_defaults(
    tmp_path, worker
):
    path = tmp_path / "application" / "hci0.snoop"
    ctl = _controller(tmp_path, hci_snoop_file=path)
    try:
        await ctl.start()
        assert worker.instances[0].options == {
            "hci_snoop_file": path,
            "hci_socket": ctl.hci_sock,
        }
        assert worker.instances[0].max_step_us == 1750
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
