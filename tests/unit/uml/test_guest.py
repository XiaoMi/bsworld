# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from bsworld.topology import BluetoothTopology
from bsworld.uml import UmlGuest
from bsworld.uml import guest as guest_module


@pytest.fixture
def model_guest(tmp_path, unit_env, monkeypatch):
    topo = BluetoothTopology(unit_env, "sim", tmp_path / "log")
    controller = topo.add_controller()
    events = []
    clock = SimpleNamespace(now_us=0)
    proc = SimpleNamespace(alive=lambda: True)

    def claim(guest):
        events.append("claim")
        return clock

    def release(guest):
        events.append("release")

    class Calendar:
        def __init__(self, path, *, on_failure):
            self.path = path
            self.on_failure = on_failure

        def listen(self):
            events.append("listen")

        def connect(self, loop, guest_clock, *, alive, console):
            assert loop is asyncio.get_running_loop()
            assert guest_clock is clock
            events.append("connect")

        def suspend(self):
            events.append("suspend")

        def close(self):
            events.append("close calendar")

    def spawn(*args, **options):
        assert len(options["pass_fds"]) == 1
        events.append("spawn")
        return proc

    def stop(process):
        assert process is proc
        events.append("stop process")

    monkeypatch.setattr(guest_module, "TimeTravelCalendar", Calendar)
    monkeypatch.setattr(
        guest_module,
        "_connect_controller",
        lambda path: os.open(os.devnull, os.O_RDONLY),
    )
    monkeypatch.setattr(topo, "_claim_uml_guest", claim)
    monkeypatch.setattr(topo, "_release_uml_guest", release)
    monkeypatch.setattr(topo.pm, "spawn", spawn)
    monkeypatch.setattr(topo.pm, "stop", stop)
    guest = UmlGuest(
        topo, controller, kernel=tmp_path / "linux", init="/init", label="g"
    )
    return guest, events, Calendar


async def test_guest_suspends_calendar_before_stopping_process(model_guest) -> None:
    guest, events, _ = model_guest

    with guest:
        assert events == ["claim", "listen", "spawn", "connect"]
        capture = guest._pipe

    assert events[4:] == ["suspend", "stop process", "close calendar", "release"]
    assert capture.read_fd is None
    assert guest.shell._proc is None


async def test_guest_rolls_back_a_spawned_process_after_handshake_failure(
    model_guest, monkeypatch
) -> None:
    guest, events, calendar = model_guest

    def fail_connect(*args, **kwargs):
        raise TimeoutError("handshake did not complete")

    monkeypatch.setattr(calendar, "connect", fail_connect)

    with pytest.raises(TimeoutError, match="handshake did not complete"):
        guest.start()

    assert events == [
        "claim",
        "listen",
        "spawn",
        "suspend",
        "stop process",
        "close calendar",
        "release",
    ]
    assert list(guest.topo.test_log_dir.rglob("*.fifo")) == []
    assert guest.shell._proc is None


async def test_guest_context_reports_unconsumed_calendar_failure(model_guest) -> None:
    guest, _, _ = model_guest

    with pytest.raises(TimeoutError, match="Guest did not yield"):
        with guest:
            guest._calendar.on_failure(TimeoutError("Guest did not yield"))

    guest.stop()


async def test_guest_context_reports_exit_before_calendar_callback(model_guest) -> None:
    guest, _, _ = model_guest

    with pytest.raises(EOFError, match="UML guest exited unexpectedly"):
        with guest:
            guest._proc.alive = lambda: False


async def test_guest_context_preserves_body_failure(model_guest) -> None:
    guest, _, _ = model_guest

    with pytest.raises(AssertionError, match="original assertion") as exc_info:
        with guest:
            guest._calendar.on_failure(TimeoutError("Guest did not yield"))
            raise AssertionError("original assertion")

    assert any("Guest did not yield" in note for note in exc_info.value.__notes__)


async def test_guest_does_not_repeat_a_failure_reported_by_wait_log(
    model_guest,
) -> None:
    guest, _, _ = model_guest

    with guest:
        guest._calendar.on_failure(EOFError("Guest exited"))
        with pytest.raises(EOFError, match="Guest exited"):
            await guest.wait_log("ready")


async def test_failed_launch_releases_guest_resources(
    tmp_path, unit_env, monkeypatch
) -> None:
    topo = BluetoothTopology(unit_env, "sim", tmp_path / "log")
    controller = topo.add_controller()
    socket_dir = controller.hci_sock.parent
    socket_dir.mkdir(parents=True)
    topo.bsim.clock = SimpleNamespace(now_us=0)
    topo._event_loop = asyncio.get_running_loop()
    topo._started = True

    def unavailable(socket_path: Path) -> int:
        raise ConnectionRefusedError(socket_path)

    monkeypatch.setattr(guest_module, "_connect_controller", unavailable)
    guest = UmlGuest(
        topo, controller, kernel=tmp_path / "linux", init="/init", label="g"
    )

    for _ in range(2):
        with pytest.raises(ConnectionRefusedError, match="hci0.sock"):
            guest.start()
        assert list(socket_dir.iterdir()) == []
        assert list(topo.test_log_dir.rglob("*.fifo")) == []


async def test_second_guest_cannot_claim_an_active_controller(
    tmp_path, unit_env
) -> None:
    topo = BluetoothTopology(unit_env, "sim", tmp_path / "log")
    controller = topo.add_controller()
    topo.bsim.clock = SimpleNamespace(now_us=0)
    topo._event_loop = asyncio.get_running_loop()
    topo._started = True
    first, second = (
        UmlGuest(topo, controller, kernel=tmp_path / "linux", init="/init", label=label)
        for label in ("first", "second")
    )
    topo._claim_uml_guest(first)

    try:
        with pytest.raises(ValueError, match="Controller is already attached"):
            second.start()
    finally:
        first.stop()
