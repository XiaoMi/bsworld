# UML BlueZ Tests

[English](README.md) | [简体中文](README.zh-CN.md)

The tests drive Ubuntu 24.04's i386 BlueZ with `bluetoothctl`. Each UML Guest runs its own D-Bus and `bluetoothd`.

## Run the Tests

After assembling a root filesystem on [local Linux](../README.md#local-linux), install the test configuration that `bluetoothd` reads at startup. Run the commands from the repository root.

```sh
install -Dm644 tests/integration/uml/bluez/main.conf \
  "${BSWORLD_UML_ROOTFS:?}/opt/bsworld/bluez.conf"
```

Run the BlueZ suite.

```sh
pytest tests/integration/uml/bluez --require-integration -q
```

To run the suite in Docker and keep its output on the host, see [Select a Suite](../../../../docker/README.md#select-a-suite).

## Test Coverage

| Test | Verified behavior |
|---|---|
| `test_uml_bluez_guests_advertise_to_bumble_observer` | Both Guests register and power on `hci0`. One Bumble observer receives advertisements with the expected name and address of each Guest. |
| `test_uml_bluez_scans_and_connects_bumble_peripheral` | BlueZ discovers a Bumble peripheral, connects and disconnects. |

## Artifacts

Each test saves console logs and HCI captures under `uml/bluez/` in its log directory. The advertising test creates a subdirectory for each Guest. For the full directory layout, see [Test artifacts](../../../../docs/artifacts.md).
