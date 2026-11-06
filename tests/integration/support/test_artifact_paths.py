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

import pytest
from tests.integration.nuttx.common import NuttXProcessProfile, create_nuttx_process
from tests.integration.nuttx.common import require_binaries as require_nuttx_binaries
from tests.integration.zephyr.shell.devices import (
    create_zephyr_firmware_shell,
    create_zephyr_shell,
)

from bsworld.devices import BSController, ZephyrController


def test_nuttx_profiles_follow_exports_without_staging(monkeypatch, tmp_path) -> None:
    profile = NuttXProcessProfile("nuttx_device", "BSWORLD_NUTTX_BTSAK_BIN")
    require = require_nuttx_binaries

    monkeypatch.delenv(profile.binary_env, raising=False)
    with pytest.raises(pytest.skip.Exception, match=profile.binary_env):
        require([profile])

    binary = tmp_path / "upstream project" / "original executable"
    binary.parent.mkdir()
    binary.write_text("native output")
    monkeypatch.setenv(profile.binary_env, str(binary))
    with pytest.raises(pytest.skip.Exception, match=profile.binary_env):
        require([profile])

    binary.chmod(0o755)
    require([profile])
    assert profile.path == binary
    assert list(binary.parent.iterdir()) == [binary]

    # Switching the selected executable does not require reimporting profiles.
    monkeypatch.setenv(profile.binary_env, str(tmp_path / "missing executable"))
    with pytest.raises(pytest.skip.Exception, match=profile.binary_env):
        require([profile])


@pytest.mark.parametrize("host_only", [False, True], ids=["full", "host-only"])
def test_zephyr_shell_follows_exports_without_staging(
    monkeypatch, tmp_path, topo, host_only
) -> None:
    binary_env = (
        "BSWORLD_ZEPHYR_SHELL_BIN" if host_only else "BSWORLD_ZEPHYR_FULL_SHELL_BIN"
    )

    def register():
        if host_only:
            host = create_zephyr_shell(topo, name="device")
            host.attach(topo.add_controller(public_address="F0:F1:F2:F3:F4:01"))
            return host
        device = create_zephyr_firmware_shell(topo, name="device", seed=42)
        assert topo.processes == (device,)
        return device

    monkeypatch.delenv(binary_env, raising=False)
    with pytest.raises(pytest.skip.Exception, match=binary_env):
        register()

    binary = tmp_path / "upstream project" / "original executable"
    binary.parent.mkdir()
    binary.write_text("native output")
    monkeypatch.setenv(binary_env, str(binary))
    with pytest.raises(pytest.skip.Exception, match=binary_env):
        register()

    binary.chmod(0o755)
    device = register()
    assert device.shell.enabled is True
    slot = topo.bsim.plan_devices().application_slot(device)
    if host_only:
        assert device.controller is not None
        tail = ["-mro=1000", f"--bt-dev={slot.hci_sock}"]
    else:
        assert device.controller is None
        assert not topo.bsim.controllers
        assert slot.hci_sock is None
        tail = ["-rs=42", "-mro=1000", "-RealEncryption=1"]
    assert device._resolve_command(slot) == [
        binary,
        f"-s={slot.sim_id}",
        f"-p={slot.phy_id}",
        f"-d={slot.dev_id}",
        *tail,
    ]
    assert list(binary.parent.iterdir()) == [binary]

    monkeypatch.setenv(binary_env, str(tmp_path / "missing executable"))
    with pytest.raises(pytest.skip.Exception, match=binary_env):
        register()


@pytest.mark.parametrize("controller_type", [BSController, ZephyrController])
def test_nuttx_profile_attaches_to_the_supplied_controller(
    topo, tmp_path, monkeypatch, controller_type
):
    binary = tmp_path / "nuttx"
    binary.write_text("native output")
    binary.chmod(0o755)
    monkeypatch.setenv("BSWORLD_NUTTX_BTSAK_BIN", str(binary))
    profile = NuttXProcessProfile("device", "BSWORLD_NUTTX_BTSAK_BIN")
    controller = topo.add_controller(
        controller_type=controller_type, public_address="C0:FF:EE:00:00:09"
    )
    device = create_nuttx_process(topo, profile)
    device.attach(controller)

    assert device.controller is controller
    assert topo.bsim.controllers == [controller]
    slot = topo.bsim.plan_devices().application_slot(device)
    assert device._resolve_command(slot)[-1] == f"--bt-dev={controller.hci_sock}"
    assert slot.hci_sock == controller.hci_sock
