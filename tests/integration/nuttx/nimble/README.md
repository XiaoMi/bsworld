# NuttX NimBLE Tests

[English](README.md) | [中文](README.zh-CN.md)

This directory contains BSWorld tests for the [Apache NimBLE](https://github.com/apache/mynewt-nimble) Host running as stock NuttX apps examples.

The current test starts:

- `examples/nimble_bleprph` as the peripheral.
- `examples/nimble_blecent` as the central.

The peripheral advertises the stock `NuttX NimBLE PRPH` name and Alert Notification Service UUID. The central scans, finds that advertisement, connects, performs service discovery, and subscribes to notifications. The test waits for the logs printed by these NuttX examples.

Both roles use the same NuttX image, and NuttX profiles enable the process shell by default. After `await topo.start()` establishes the initial PHY synchronization boundary, the test sends `ifup bnep0` followed by `bleprph` or `blecent` through `process.shell.run_many(...)`.

Firmware build instructions live in [`source/README.md`](source/README.md). For first-time NuttX environment setup, start with [../README.md](../README.md).

## Files

- `devices.py`: reusable `NuttXProcessProfile` definitions for central and peripheral roles. Both roles use the same NuttX image.
- `test_central_peripheral.py`: end-to-end test coverage for discovery, connection, and the stock NimBLE service discovery flow.
- `source/`: build inputs and details for the stock NuttX NimBLE image.

## Shell Commands

Peripheral processes run:

```sh
ifup bnep0
bleprph
```

Central processes run:

```sh
ifup bnep0
blecent
```

The test waits for the stock NuttX examples' connection, service discovery, and subscription logs.

## Run

We recommend the [Docker test image](../../../../docker/README.md#select-a-suite), using `tests/integration/nuttx/nimble` as the pytest target.

For native Linux, compile with [source/README.md](source/README.md), retain the BabbleSim and Controller variables, then switch back to the BSWorld Python environment and run:

```sh
source "$BSWORLD_ROOT/.venv/bin/activate"
export BSWORLD_NUTTX_NIMBLE_BIN="$NUTTX_NIMBLE_DIR/nuttx"
cd "$BSWORLD_ROOT"
pytest tests/integration/nuttx/nimble -q --no-ellisys
```
