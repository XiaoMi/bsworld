# BSController Protocol Tests

English | [简体中文](PROTOCOL.zh-CN.md)

This suite validates BSController HCI commands, events and Link Layer behavior against HCI.TS.p39, LL.TS.p28 and the Bluetooth Core Specification. Its results support development verification and do not constitute Bluetooth SIG qualification.

## Coverage

The suite selects 614 protocol cases: 133 HCI and 481 LL cases across 17 test files. Coverage includes discovery, advertising, connections, timing, packet encoding, frequency hopping, security and data transfer, including privacy and Decision-Based Advertising Filtering (DBAF).

Cases retain their specification identifiers for lookup and selection. The chapter numbers below refer to the test specifications.

| Directory | Source Chapter | Test File | Selected Cases |
|---|---|---|---:|
| `hci/` | 4.3 Generic Events | [generic_events_test.py](hci/generic_events_test.py) | 5 |
| `hci/` | 4.4 Device Setup | [device_setup_test.py](hci/device_setup_test.py) | 5 |
| `hci/` | 4.5 Controller Flow Control | [controller_flow_control_test.py](hci/controller_flow_control_test.py) | 1 |
| `hci/` | 4.6 Controller Information | [controller_information_test.py](hci/controller_information_test.py) | 5 |
| `hci/` | 4.7 Device Discovery | [device_discovery_test.py](hci/device_discovery_test.py) | 72 |
| `hci/` | 4.8 Host Flow Control | [host_flow_control_test.py](hci/host_flow_control_test.py) | 1 |
| `hci/` | 4.10 Controller Configuration | [controller_configuration_test.py](hci/controller_configuration_test.py) | 32 |
| `hci/` | 4.13 LE Connection Management | [connection_management_test.py](hci/connection_management_test.py) | 7 |
| `hci/` | 4.17 Event Versioning | [event_versioning_test.py](hci/event_versioning_test.py) | 5 |
| `ll/` | 4.2 Device Discovery | [device_discovery_test.py](ll/device_discovery_test.py) | 151 |
| `ll/` | 4.3 Connection | [connection_test.py](ll/connection_test.py) | 219 |
| `ll/` | 4.4 Timing | [timing_test.py](ll/timing_test.py) | 21 |
| `ll/` | 4.5 Encoding | [encoding_test.py](ll/encoding_test.py) | 7 |
| `ll/` | 4.6 Frequency Hopping | [frequency_hopping_test.py](ll/frequency_hopping_test.py) | 6 |
| `ll/` | 4.7 Packet Handling | [packet_handling_test.py](ll/packet_handling_test.py) | 6 |
| `ll/` | 4.8 Security | [security_test.py](ll/security_test.py) | 67 |
| `ll/` | 4.9 Data Flow | [data_flow_test.py](ll/data_flow_test.py) | 4 |

Coverage is limited to these selected cases and does not imply complete HCI.TS, LL.TS or Core coverage. The cases that can run also depend on Controller capabilities and the test environment.

## Running tests

Prepare the environment as described in the [test README](../README.md#run). Run these commands from `components/bscontroller`; RF cases require a working BabbleSim PHY.

```sh
# Run all protocol cases.
python -m pytest tests/qualification -n auto --dist worksteal -v -rs

# Run HCI protocol cases only.
python -m pytest tests/qualification/hci -n auto --dist worksteal -v -rs

# Select one case by its specification identifier.
python -m pytest tests/qualification -k 'LL/DDI/SCN/BV-88-C' -v -rs
```

Use `-n 4` to select a fixed worker count. Environment setup and parallel execution guidance are in the [test README](../README.md#parallel-execution).

## Interpreting results

A pytest `passed` result means the executed assertions passed. Consult the case's `protocol.log` for its protocol verdict and any limitations.

| Result | Meaning |
|---|---|
| `PASS` | The checks passed without a declared timing-precision or source-description limitation. |
| `PARTIAL` | The executed checks passed with a timing-precision or source-description limitation; assess the result alongside the case description. |
| `FAILED` | Execution or a check failed; inspect the assertion, protocol log and relevant packets. |
| `SKIPPED` | The case did not run or complete and cannot count as a protocol pass; consult the pytest skip reason. |

`PARTIAL` corresponds to the following markers; a case can have both:

- `limited`: a requirement involves sub-microsecond timing, while the test uses 1-us timestamp resolution.
- `source_conflict`: interpretation of the source steps, branches or expected outcome remains limited; see the case description for its effect.

RF cases skip when BabbleSim is not configured, so check both the execution count and skip reasons.

## Logs and limitations

Each case's pytest temporary directory contains `protocol.log`, recording its identifier, description, parameters and protocol verdict. RF cases also provide an HCI capture and Controller/PHY logs. Use pytest's `--basetemp` option to select an output directory.

For a failed or partial result, locate the log by case identifier, then consult the case's steps, expected outcome and `Reference`. Necessary specification interpretations are recorded in `Source Correction`.

RF cases run in the BabbleSim simulation environment. Interpret timing results at the precision declared in the case description.

## License

Copyright 2026 Xiaomi Corporation. Licensed under Apache License 2.0; see [LICENSE](../../LICENSE).
