# QEMU NimBLE Linux Test

[English](README.md) | [中文](README.zh-CN.md)

This test runs the upstream Apache NimBLE Linux Host example inside the AArch64 Linux Guest. QEMU exposes the Virtual Controller's Unix-socket HCI stream as `/dev/ttyS0`, `btattach` registers the H4 serial transport as `hci0`, and the NimBLE Linux port claims that device through HCI User Channel. The integration neither modifies NimBLE source nor adds a BSWorld-specific transport.

## Validated Scenario

`test_qemu_nimble_linux_advertises_and_accepts_connection` validates the complete path:

1. NimBLE initializes its Host and Controller connection and starts connectable advertising.
2. An independent Bumble Central receives the advertisement over the simulated radio and verifies public address `AA:BB:CC:DD:EE:FE` and complete local name `nimble`.
3. Bumble connects to NimBLE, verifies a valid connection handle, and disconnects cleanly.

## Build and Run

We recommend the [Docker test image](../../../../docker/README.md#select-a-suite), using `tests/integration/qemu/nimble` as the pytest target.

For native Linux, compile with the parent [QEMU / Buildroot README](../README.md), retain the BabbleSim/Controller exports and `BSWORLD_QEMU_BIN`, `BSWORLD_QEMU_KERNEL`, `BSWORLD_QEMU_ROOTFS`, then run from the repository root:

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/qemu/nimble -q
```

The QEMU console is stored as `qemu/nimble/qemu_nimble.log`. HCI BTSnoop captures and BabbleSim RF traces are archived with the other per-test files described in [Logs, Traces, and Packet Captures](../../../../docs/artifacts.md).

## License

The tests in this directory are licensed under Apache License 2.0. Apache NimBLE is licensed under Apache License 2.0; QEMU, Linux, Buildroot, and other Guest packages retain their own licenses. Review [Third-Party Notices](../../../../THIRD_PARTY_NOTICES.md) and the parent guide's [license notes](../README.md#licenses) before distributing binaries.
