# RadioController Tests

English | [简体中文](README.zh-CN.md)

| Directory | Coverage | Test environment |
|---|---|---|
| `unit/` | Controller algorithms, state ownership, scheduling, and FIFO, thread, and clock behavior, grouped by implementation module. | Python test environment. |
| `qualification/hci/`, `qualification/ll/` | HCI and Link Layer cases grouped by specification chapter and exercised with independent testers. | Python test environment for HCI-only cases, plus BabbleSim for RF cases. |

Use `unit/hci/` to check the H4 command, status and event that the Host sees. Use `unit/linklayer/llcp/` for control procedure transitions and `unit/linklayer/air/` to exercise radio scheduling with scripted packets. Qualification cases drive the Controller through HCI or the air interface and check the observations required by the specification.

See the [protocol test guide](qualification/PROTOCOL.md) for coverage by chapter, log locations, and how to interpret results.

## Run

Install the test dependencies as described in [Development](../README.md#development). Run the commands from `components/bscontroller/`.

```sh
# Self-contained implementation checks.
python -m pytest tests/unit/ -n auto --dist worksteal -v
```

For RF cases, select a BabbleSim build with `bin/bs_2G4_phy_v1`, the 2G4 v1 and v2 FIFO protocols, the `BLE_simple` modem, and the `multiatt` channel model.

```sh
export BSIM_OUT_PATH=/path/to/bsim/build
python -m pytest tests/qualification -n auto --dist worksteal -v -rs

# Full RadioController regression.
python -m pytest tests/ -n auto --dist worksteal -v
```

RF cases skip when `BSIM_OUT_PATH` is unset. If a path is set, the fixture requires an executable `bin/bs_2G4_phy_v1` and fails if it is missing or cannot run.

## Parallel Execution

The `[test]` extra includes pytest-xdist. The commands above use `-n auto` to select the worker count. Use `-n 4` for a fixed count. `--dist worksteal` redistributes pending cases to idle workers.

Each RF case has its own PHY process, simulation ID, FIFOs, packet dumps, and protocol logs. Separate simulations can reuse RF channels and device addresses.

Give each concurrent pytest invocation a distinct directory if you set `--basetemp` explicitly.

## License

Copyright 2026 Xiaomi Corporation. Apache License 2.0 applies to these tests. See the [component license](../LICENSE).
