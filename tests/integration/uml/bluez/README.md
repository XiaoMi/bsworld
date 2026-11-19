# UML BlueZ Tests

[English](README.md) | [简体中文](README.zh-CN.md)

The tests run Ubuntu's i386 `bluetoothd`, `bluetoothctl` and `btattach` in a [UML guest](../README.md). Each guest starts its own D-Bus and `bluetoothd`, with [main.conf](main.conf) installed at `/opt/bsworld/bluez.conf`.

## Build and Run

Prepare the [UML test environment](../README.md#prepare-the-environment), then select BlueZ tests from the repository root.

```sh
docker run --rm --security-opt seccomp=docker/uml-seccomp.json \
  bsworld-linux:dev pytest tests/integration/uml/bluez -q
```

| Test | Verified behavior |
|---|---|
| `test_uml_bluez_guests_advertise_to_bumble_observer` | Two guests each register and power on their own `hci0`. One Bumble observer receives advertising from both at the expected names and addresses. |
| `test_uml_bluez_scans_and_connects_bumble_peripheral` | BlueZ discovers a Bumble peripheral, connects and disconnects. |

Find the console and HCI captures under `uml/bluez/`. The advertising test writes a subdirectory for each guest. [Test artifacts](../../../../docs/artifacts.md)
