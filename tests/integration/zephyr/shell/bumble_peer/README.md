# Zephyr Shell and Bumble Tests

[English](README.md) | [简体中文](README.zh-CN.md)

The `peer` fixture runs a Zephyr Bluetooth shell and a [Bumble](../../../bumble/README.md) Host on separate BSControllers. It fixes Bumble's SMP random values and key pair so pairing traces repeat between runs. [conftest.py](conftest.py)

## Build and Run

Build the shell using the [Bluetooth shell guide](../README.md#build-and-run), then run the tests.

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/zephyr/shell/bumble_peer -n auto -q
```

[test_bumble.py](test_bumble.py) covers scanning and connections initiated by either side, GATT, pairing and bonded reconnection, link updates, LE Coded connections, and periodic advertising sync, loss and recovery in both directions.

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
