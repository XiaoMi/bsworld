# Zephyr Shell and Bumble Tests

[English](README.md) | [简体中文](README.zh-CN.md)

These tests run a complete Zephyr Bluetooth shell against a [Bumble](../../../bumble/README.md) Host in the pytest event loop. The `peer` fixture in [conftest.py](conftest.py) starts the shell, opens Bumble on its own Virtual Controller and runs once per Controller implementation. It fixes Bumble's SMP random values and key pair so pairing traces repeat between runs.

## Prepare the Environment

Build the complete executable and export `BSWORLD_ZEPHYR_FULL_SHELL_BIN` using the [Bluetooth shell guide](../README.md#build-and-run). The `[zephyr]` cases also need the [Zephyr Controller](../../../../../docs/controllers.md#building-the-zephyr-controller) and are skipped without it.

## Build and Run

The executable is built by the Bluetooth shell guide. Run the tests:

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/zephyr/shell/bumble_peer -n auto -q
```

| File | Scenarios | Controllers |
|---|---|---|
| [test_bumble.py](test_bumble.py) | Scanning in both directions, connections initiated by either side, shell GATT server and client, pairing and bonded reconnection, PHY, data length and connection parameter updates, LE Coded connection, periodic advertising sync, loss and recovery in both directions | Once per Controller |

## Adding Scenarios

```python
async def test_bumble_finds_shell(peer):
    await peer.shell.shell.run("bt advertise on identity")
    await peer.shell.wait_log("Advertising started")
    await peer.bumble.start_scanning(active=True)
```

`peer.shell` is the started and initialized shell process, `peer.bumble` the opened Bumble `Device`, and `peer.controller` Bumble's Controller, with public address `F0:F1:F2:F3:F4:01`. Bumble's HCI capture is under `bumble/`; see [artifacts](../../../../../docs/artifacts.md) for the rest.

## License

The tests in this directory use Apache License 2.0. See the [parent license notes](../README.md#license).
