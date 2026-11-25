# BSController Protocol Tests

English | [简体中文](PROTOCOL.zh-CN.md)

This suite checks BSController HCI commands, events, and Link Layer behavior for development verification. The cases draw on HCI.TS.p39, LL.TS.p28, and the Bluetooth Core Specification.

## Coverage

The suite covers discovery, advertising, connections, timing, packet encoding, frequency hopping, security, and data transfer. The selected cases include privacy, Decision-Based Advertising Filtering (DBAF), BIS broadcasting, and synchronized reception.

The table lists 1070 selected protocol cases across 27 files, with 196 HCI cases and 874 LL cases. Find and select cases by their test specification identifiers.

| Directory | Source Chapter | Test File | Selected Cases |
|---|---|---|---:|
| `hci/` | 4.3 Generic Events | [generic_events_test.py](hci/generic_events_test.py) | 5 |
| `hci/` | 4.4 Device Setup | [device_setup_test.py](hci/device_setup_test.py) | 5 |
| `hci/` | 4.5 Controller Flow Control | [controller_flow_control_test.py](hci/controller_flow_control_test.py) | 1 |
| `hci/` | 4.6 Controller Information | [controller_information_test.py](hci/controller_information_test.py) | 6 |
| `hci/` | 4.7 Device Discovery | [device_discovery_test.py](hci/device_discovery_test.py) | 75 |
| `hci/` | 4.8 Host Flow Control | [host_flow_control_test.py](hci/host_flow_control_test.py) | 1 |
| `hci/` | 4.10 Controller Configuration | [controller_configuration_test.py](hci/controller_configuration_test.py) | 68 |
| `hci/` | 4.11 Controller Setup | [controller_setup_test.py](hci/controller_setup_test.py) | 2 |
| `hci/` | 4.13 LE Connection Management | [connection_management_test.py](hci/connection_management_test.py) | 7 |
| `hci/` | 4.14 LE Power Control | [power_control_test.py](hci/power_control_test.py) | 3 |
| `hci/` | 4.15.2 Broadcast Isochronous Streams | [broadcast_isochronous_test.py](hci/broadcast_isochronous_test.py) | 17 |
| `hci/` | 4.17 Event Versioning | [event_versioning_test.py](hci/event_versioning_test.py) | 6 |
| `ll/` | 4.2 Device Discovery | [device_discovery_test.py](ll/device_discovery_test.py) | 183 |
| `ll/` | 4.3 Connection | [connection_test.py](ll/connection_test.py) | 260 |
| `ll/` | 4.3 Connection Subrating | [subrating_test.py](ll/subrating_test.py) | 21 |
| `ll/` | 4.3 Connection Rate | [connection_rate_test.py](ll/connection_rate_test.py) | 10 |
| `ll/` | 4.3.7 Periodic Advertising Sync Transfer | [sync_transfer_test.py](ll/sync_transfer_test.py) | 84 |
| `ll/` | 4.4 Timing | [timing_test.py](ll/timing_test.py) | 29 |
| `ll/` | 4.4 Frame Space Update | [frame_space_test.py](ll/frame_space_test.py) | 94 |
| `ll/` | 4.5 Encoding | [encoding_test.py](ll/encoding_test.py) | 7 |
| `ll/` | 4.6 Frequency Hopping | [frequency_hopping_test.py](ll/frequency_hopping_test.py) | 6 |
| `ll/` | 4.7 Packet Handling | [packet_handling_test.py](ll/packet_handling_test.py) | 6 |
| `ll/` | 4.8 Security | [security_test.py](ll/security_test.py) | 67 |
| `ll/` | 4.9 Data Flow | [data_flow_test.py](ll/data_flow_test.py) | 4 |
| `ll/` | 4.11 Broadcast Isochronous Streams | [broadcast_isochronous_test.py](ll/broadcast_isochronous_test.py) | 28 |
| `ll/` | 4.12 Isochronous Testing | [isochronous_testing_test.py](ll/isochronous_testing_test.py) | 2 |
| `ll/` | 4.13 PCL | [power_control_test.py](ll/power_control_test.py) | 73 |

## Running tests

Run these commands from `components/bscontroller`. To configure the BabbleSim PHY required by RF cases, follow the [test setup instructions](../README.md#run).

Running `tests/qualification` also includes four [Lower Tester regressions](peer_test.py) and two air-interface regressions for [MCES](ll/frame_space_test.py) and [PHY changes at short connection intervals](ll/connection_rate_test.py).

```sh
# Run all protocol cases.
python -m pytest tests/qualification -n auto --dist worksteal -v -rs

# Run HCI protocol cases only.
python -m pytest tests/qualification/hci -n auto --dist worksteal -v -rs

# Select one case by its specification identifier.
python -m pytest tests/qualification -k 'LL/DDI/SCN/BV-88-C' -v -rs
```

Use `-n 4` for a fixed worker count. For worker setup and output isolation, see the [parallel execution guide](../README.md#parallel-execution).

## Interpreting results

A pytest `passed` result means the executed assertions passed. A selected procedure that completes records its protocol verdict and any limitations in `protocol.log`. Pytest reports failures and skips.

| Result | Meaning |
|---|---|
| `PASS` | The checks passed with no timing or source limitation declared. |
| `PARTIAL` | The executed checks passed with a declared limitation. Read the case description to assess the result. |
| `FAILED` | A check or execution failed. Inspect the pytest failure, any protocol log, and relevant packets. |
| `SKIPPED` | The procedure did not run or did not complete. The reason appears in the pytest report. |

A successful procedure reports `PARTIAL` when it carries either of these markers. Some cases carry both.

- `limited` marks sub-microsecond timing requirements that the test approximates at 1 µs resolution.
- `source_conflict` marks unresolved steps, branches, or verdict requirements in the source. The case description explains their effect.

Controller capabilities and the test environment affect which cases run. RF cases skip when `BSIM_OUT_PATH` is unset. Check the executed case count and skip reasons when reading the results.

## Logs and limitations

Once a selected procedure starts, `protocol.log` records its identifier, description, parameters, and trace. RF cases that start the PHY can also produce `hci.snoop`, `controller.log`, and `phy.log`.

Find these files in the per-case pytest temporary directory. Use `--basetemp` to choose its root.

For a failed or partial result, look up the case by identifier and compare its log with the steps, expected result, and `Reference` in the case description. Consult `Source Correction` for any documented interpretation of the test specification. Use the timing precision stated in the case to interpret RF results.

## License

Copyright 2026 Xiaomi Corporation. Licensed under the [Apache License 2.0](../../LICENSE).
