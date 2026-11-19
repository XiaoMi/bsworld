# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Controller binding, ownership and layout stability through the public API."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from bsworld.devices import ProcessInstance
from bsworld.topology import BluetoothTopology


@pytest.mark.parametrize(
    "registered", [False, True], ids=["unregistered", "registered"]
)
def test_attach_rejects_rebinding_without_changing_the_controller(
    tmp_path, unit_env, registered
):
    topo = BluetoothTopology(unit_env, "sim", tmp_path / "log")
    first, second = topo.add_controller(), topo.add_controller()
    host = ProcessInstance(command=["/host"], label="host")
    if registered:
        topo.add_process(host)
    host.attach(first)
    with pytest.raises(RuntimeError, match="already has a Controller"):
        host.attach(second)
    assert host.controller is first


@pytest.mark.parametrize("participant", ["host", "controller", "interference"])
def test_layout_publication_prevents_new_participants(tmp_path, unit_env, participant):
    topo = BluetoothTopology(unit_env, "sim", tmp_path / "log")
    controller = topo.add_controller()
    layout = topo.bsim.plan_devices()
    with pytest.raises(RuntimeError, match="layout is frozen"):
        if participant == "host":
            topo.add_process(
                ProcessInstance(command=["/host"], label="host", controller=controller)
            )
        elif participant == "controller":
            topo.add_controller()
        else:
            topo.add_burst_interference(2402)
    assert not topo.processes
    assert topo.bsim.plan_devices() is layout
    assert layout.device_count == 2


def test_registered_process_cannot_be_owned_by_two_topologies(tmp_path, unit_env):
    first = BluetoothTopology(unit_env, "first", tmp_path / "first")
    second = BluetoothTopology(unit_env, "second", tmp_path / "second")
    host = ProcessInstance(command=["/host"], label="host")
    first.add_process(host)
    with pytest.raises(ValueError, match="already registered"):
        second.add_process(host)
    assert not second.processes


def test_registered_process_can_attach_before_layout_publication(tmp_path, unit_env):
    topo = BluetoothTopology(unit_env, "sim", tmp_path / "log")
    host = topo.add_process(ProcessInstance(command=["/host"], label="host"))
    controller = topo.add_controller()
    host.attach(controller)

    layout = topo.bsim.plan_devices()
    assert topo.processes == (host,)
    assert layout.device_count == 3  # Controller, Host clock, pytest clock
    assert layout.application_slot(host).dev_id == 1
    assert layout.application_slot(host).hci_sock == controller.hci_sock
    assert layout.resolve_endpoint(host) == 0
    assert controller.hci_snoop_file == tmp_path / "log" / "host" / "hci0.snoop"


@pytest.mark.parametrize("rejected", ["foreign", "shared"])
def test_failed_attachment_preserves_registration_and_allows_recovery(
    tmp_path, unit_env, rejected
):
    topo = BluetoothTopology(unit_env, "sim", tmp_path / "log")
    owner = topo.add_process(ProcessInstance(command=["/owner"], label="owner"))
    owned_controller = topo.add_controller()
    owner.attach(owned_controller)
    candidate = topo.add_process(
        ProcessInstance(command=["/candidate"], label="candidate")
    )
    original_capture = owned_controller.hci_snoop_file
    if rejected == "foreign":
        other = BluetoothTopology(unit_env, "other", tmp_path / "other")
        blocked = other.add_controller()
        reason = "does not belong"
    else:
        blocked = owned_controller
        reason = "only be attached to one"

    with pytest.raises(ValueError, match=reason):
        candidate.attach(blocked)
    assert candidate.controller is None
    assert topo.processes == (owner, candidate)
    assert owned_controller.hci_snoop_file == original_capture

    replacement = topo.add_controller()
    candidate.attach(replacement)
    layout = topo.bsim.plan_devices()
    assert layout.device_count == 5  # Two radios, two Host clocks, pytest clock
    assert layout.application_slot(owner).dev_id == 2
    assert layout.application_slot(candidate).dev_id == 3
    assert layout.application_slot(candidate).hci_sock == replacement.hci_sock
    assert layout.resolve_endpoint(candidate) == 1


async def test_unattached_host_fails_start_before_the_phy_is_spawned(
    tmp_path, unit_env, monkeypatch, spawn_recorder
):
    topo = BluetoothTopology(unit_env, "sim", tmp_path / "log")
    controller = topo.add_controller()
    host = topo.add_process(ProcessInstance(command=["/host"], label="host"))
    monkeypatch.setattr(topo.bsim, "check_deps_ready", lambda: None)
    monkeypatch.setattr(topo.bsim, "prepare", lambda: None)
    monkeypatch.setattr(topo, "pm", spawn_recorder)
    monkeypatch.setattr(
        spawn_recorder, "install_signal_handlers", Mock(), raising=False
    )
    monkeypatch.setattr(
        spawn_recorder, "restore_signal_handlers", Mock(), raising=False
    )
    monkeypatch.setattr(spawn_recorder, "cleanup", Mock(), raising=False)
    monkeypatch.setattr(topo.bsim, "finish", lambda **kwargs: None)

    try:
        with pytest.raises(ValueError, match="host: HCI Host has no Controller"):
            await topo.start()
        assert spawn_recorder.calls == []
        host.attach(controller)
        assert topo.bsim.plan_devices().application_slot(host).dev_id == 1
    finally:
        await topo.stop()


@pytest.mark.parametrize("bind", ["constructor", "attach"])
def test_default_log_directory_does_not_depend_on_how_a_controller_is_bound(
    tmp_path, unit_env, bind
):
    topo = BluetoothTopology(unit_env, "sim", tmp_path / "log")
    controller = topo.add_controller()
    if bind == "constructor":
        host = topo.add_process(
            ProcessInstance(command=["/host"], label="host", controller=controller)
        )
    else:
        host = topo.add_process(ProcessInstance(command=["/host"], label="host"))
        host.attach(controller)

    assert host.log_subdir == Path("host")
    assert controller.hci_snoop_file == tmp_path / "log" / "host" / "hci0.snoop"


def test_registered_processes_can_only_change_through_registration(tmp_path, unit_env):
    topo = BluetoothTopology(unit_env, "sim", tmp_path / "log")
    controller = topo.add_controller()
    host = topo.add_process(
        ProcessInstance(command=["/host"], label="host", controller=controller)
    )
    for view in (topo.processes, topo.bsim.processes):
        with pytest.raises(AttributeError):
            view.append(ProcessInstance(command=["/rogue"], label="rogue"))
    assert topo.processes == (host,)
    assert topo.bsim.plan_devices().device_count == 3
