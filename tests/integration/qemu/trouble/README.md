# QEMU TrouBLE Linux Test

[English](README.md) | [中文](README.zh-CN.md)

This test runs the upstream TrouBLE Battery Service peripheral example inside the synchronized AArch64 Linux Guest. QEMU exposes the Virtual Controller through `/dev/ttyS0`; `btattach` registers it as `hci0`, and TrouBLE claims the device through HCI User Channel.

## Validated Behavior

`test_qemu_trouble_linux_advertises_and_serves_battery_level` verifies the complete path:

1. The Guest stops `bluetoothd`, attaches `/dev/ttyS0`, and takes `hci0` down as required by HCI User Channel.
2. The cross-compiled TrouBLE example initializes its Host stack and advertises `Trouble Example` from its fixed random address.
3. An independent Bumble Central discovers and connects to TrouBLE over the simulated radio link.
4. Bumble discovers the Battery Service and reads a valid Battery Level characteristic value.

## Build and Run

We recommend the [Docker test image](../../../../docker/README.md#select-a-suite), using `tests/integration/qemu/trouble` as the pytest target.

For native Linux, compile with the parent [QEMU / Buildroot README](../README.md), retain the BabbleSim/Controller exports and `BSWORLD_QEMU_BIN`, `BSWORLD_QEMU_KERNEL`, `BSWORLD_QEMU_ROOTFS`, then run from the repository root:

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/qemu/trouble -q
```

The QEMU console is saved as `qemu/trouble/qemu_trouble.log`. HCI BTSnoop and BabbleSim RF traces are archived with the other per-test artifacts described in [Logs, Traces, and Captures](../../../../docs/artifacts.md).

## License

The test code in this directory is licensed under Apache License 2.0. TrouBLE is available under MIT or Apache License 2.0; QEMU, Linux, BlueZ, Buildroot, and the other Guest components retain their own licenses. Before distributing binaries, review the repository [Third-Party Notices](../../../../THIRD_PARTY_NOTICES.md) and the parent documentation's [license notes](../README.md#licenses).
