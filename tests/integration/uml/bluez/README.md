# UML BlueZ Tests

[English](README.md) | [简体中文](README.zh-CN.md)

Runs the host's BlueZ in the [UML Linux guest](../README.md) and drives it through `bluetoothctl`. Bumble is the peer.

| Program | Source | Executable variable |
|---|---|---|
| `bluetoothd`, `bluetoothctl`, `btattach` | Installed on the host; validated with `bluetoothd` 5.86 and `bluetoothctl` 5.64. The Linux Docker image installs Ubuntu 24.04's BlueZ 5.72 | None |

## Prepare the Environment

Build the kernel and seed library using the [UML test environment](../README.md#build-the-linux-guest). Install 64-bit BlueZ on the host; [init.sh](../source/init.sh) starts `/usr/libexec/bluetooth/bluetoothd`, or `/usr/lib/bluetooth/bluetoothd` when the first is missing.

## Build and Run

BlueZ is not built for these tests. Run them:

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/uml/bluez -q
```

| Test | Verified behavior |
|---|---|
| `test_uml_bluez_initializes_virtual_controller` | `hci0` registers with the expected public address and powers on. |
| `test_uml_bluez_advertises_to_bumble_observer` | A Bumble observer receives BlueZ's connectable advertising with the expected name and address. |
| `test_uml_bluez_scans_and_connects_bumble_peripheral` | BlueZ discovers a Bumble peripheral, connects and disconnects. |

BlueZ uses `hci0` through `bluetoothd`. The guest console is under `uml/bluez/uml_bluez.log`; see [artifacts](../../../../docs/artifacts.md) for the rest.

## License

The tests in this directory use Apache License 2.0. See the [parent license notes](../README.md#license).
