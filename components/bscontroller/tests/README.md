# RadioController Tests

English | [简体中文](README.zh-CN.md)

The suites separate implementation invariants from Bluetooth protocol behavior. The [component README](../README.md) documents installation, executable API examples, independent HCI service operation, and distribution builds.

| Directory | Scope | BabbleSim Requirement |
|---|---|---|
| `unit/` | Controller algorithms, state ownership, scheduler and FIFO/thread/clock boundaries, organized by implementation module. | No native PHY is required. |
| `qualification/hci/`, `qualification/ll/` | All 614 selected HCI/LL cases, organized by their normative source chapters, with independent lower testers. | Required for RF cases; pure HCI checks run without it. |

The [protocol chapter map](qualification/PROTOCOL.md) lists the 17 primary test files, selected case counts, result meaning, and limits. Each unit test file states its scope in its module docstring, and each test documents its purpose, steps and expected result.

These independently implemented tests do not constitute Bluetooth qualification or imply complete LL.TS/HCI.TS coverage.

## Run

From the RadioController checkout:

```sh
# Self-contained implementation checks.
python -m pytest tests/unit/ -n auto --dist worksteal -v
```

For RF cases, select a BabbleSim build with the 2G4 v1/v2 FIFO protocols, `BLE_simple` modem and `multiatt` channel model:

```sh
export BSIM_OUT_PATH=/path/to/bsim/build
python -m pytest tests/qualification -n auto --dist worksteal -v -rs

# Full RadioController regression.
python -m pytest tests/ -n auto --dist worksteal -v
```

Native cases have independent simulation IDs and support pytest-xdist. They skip when `BSIM_OUT_PATH` is unset; a configured path without the PHY executable fails. Unit collection needs no native artifact.

## Parallel Execution

```sh
python -m pip install pytest-xdist
export BSIM_OUT_PATH=/path/to/bsim/build
python -m pytest tests/ -n auto --dist worksteal -q -rs
```

Each native case owns its PHY process, simulation ID, FIFOs and packet dumps. Worker temporary directories and per-case protocol logs are also separate, so simulations do not interfere through equal RF channels or device addresses. If running multiple pytest invocations with explicit `--basetemp` values, give each invocation its own directory.

`--dist worksteal` redistributes pending cases when workers become idle. A single procedure is not split across workers. Use `-n 4` to choose a fixed worker count. Workers share CPU, memory and storage, so more workers do not guarantee a faster run.

## License

Copyright 2026 Xiaomi Corporation. The independently written tests are licensed under Apache License 2.0; see the repository [LICENSE](../LICENSE).
