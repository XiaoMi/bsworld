# QEMU BlueZ Tests

[English](README.md) | [中文](README.zh-CN.md)

These tests run an unmodified BlueZ Host stack inside an AArch64 Linux guest whose virtual time is synchronized with BabbleSim. QEMU exposes the Virtual Controller's Unix-socket HCI stream to Linux as `/dev/ttyS0`; `btattach` registers it as `hci0`, after which the tests control BlueZ through `bluetoothctl`.

## Scenarios

| Test | Verified behavior |
|---|---|
| `test_qemu_bluez_initializes_virtual_controller` | Linux registers `hci0`; BlueZ enumerates the expected public Controller address and powers it on. |
| `test_qemu_bluez_advertises_to_bumble_observer` | BlueZ configures connectable advertising; an independent Bumble observer verifies the complete local name and public address over the simulated radio. |
| `test_qemu_bluez_scans_and_connects_bumble_peripheral` | BlueZ discovers a Bumble peripheral by exact name and address, establishes an LE connection, verifies the remote connection handle, and disconnects cleanly. |

Bumble is used only as a controlled peer. BlueZ, the Linux Bluetooth subsystem, the HCI UART path, QEMU, the Virtual Controller, and BabbleSim remain on the behavior under test.

## Build and Run

We recommend the [Docker test image](../../../../docker/README.md#select-a-suite), using `tests/integration/qemu/bluez` as the pytest target.

For native Linux, compile with the parent [QEMU / Buildroot README](../README.md), retain the BabbleSim/Controller exports and `BSWORLD_QEMU_BIN`, `BSWORLD_QEMU_KERNEL`, `BSWORLD_QEMU_ROOTFS`, then run from the repository root:

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/qemu/bluez -q
```

Each test receives an isolated simulation ID, HCI socket, and log directory. The QEMU console is recorded under `qemu/bluez/qemu_bluez.log`; HCI BTSnoop and BabbleSim RF traces are generated alongside the standard per-test artifacts described in [Logs, traces, and captures](../../../../docs/artifacts.md).

## License

The tests in this directory are licensed under Apache License 2.0. QEMU, Linux, BlueZ, Buildroot, and the other packages in the Guest image retain their own licenses. Review [Third-Party Notices](../../../../THIRD_PARTY_NOTICES.md) and the parent guide's [license notes](../README.md#licenses) before distributing binaries.
