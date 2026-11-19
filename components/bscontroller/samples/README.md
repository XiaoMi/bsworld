# Bumble Host samples

English | [简体中文](README.zh-CN.md)

Each sample runs one Bluetooth scenario. Every device uses a Bumble Host connected to its own BSController over H4 on a Unix socket. The Controllers share one BabbleSim PHY, and the Host event loop uses simulation time.

## Run a sample

From `components/bscontroller`, install the `bumble` extra and point `BSIM_OUT_PATH` at your BabbleSim build. See the [quick start](../README.md#quick-start) for the build steps.

```sh
python -m pip install -e '.[bumble]'
export BSIM_OUT_PATH=/path/to/babblesim/build
python -m samples.advertise_scan
```

The sample prints a JSON result on success. Use `--bsim-dir` to override `BSIM_OUT_PATH` and `--timeout` to set the time limit in simulation seconds, including Host initialization.

## Scenarios

| Command | Scenario and checks |
|---|---|
| `python -m samples.advertise_scan` | An observer discovers a broadcaster. |
| `python -m samples.scan_adv` | Three devices advertise and scan concurrently. Each receives advertisements from the other two. |
| `python -m samples.gatt` | A Central discovers a Battery Service, reads its value and receives a notification. |
| `python -m samples.central_multilink` | One Central keeps three Peripheral connections and verifies GATT reads and notifications on every link. Use `--peripherals N` to change the number of Peripherals. |
| `python -m samples.periodic` | An observer keeps two independent periodic advertising synchronizations and receives data from both. |
| `python -m samples.broadcast_iso` | One broadcaster sends two BIS streams to two synchronized receivers. The sample checks every SDU byte for byte. |
| `python -m samples.coexistence` | Five devices keep five ACL links, two periodic advertisers, and four periodic synchronizations while scanning, advertising, and exchanging GATT data. |

`broadcast_iso` accepts `--encrypted`, `--framed`, `--phy 1|2|3` (1M, 2M, Coded) and `--receivers N`.

### Concurrent roles

In `coexistence`, both hubs connect to both leaves. `hub-b` also connects to `hub-a`, so `hub-a` acts as Central and Peripheral at the same time. Each leaf accepts two Centrals.

The hubs each transmit a periodic train and synchronize with the other. An independent observer synchronizes with both. All five links must carry Battery Service notifications and reads while scan reports and periodic reports continue to arrive.

## Logs

Use `--log-dir /tmp/bscontroller-samples` to retain `phy.log`, per-device `.snoop` captures and the PHY's transmit and receive records under `air/`. Each run creates its own subdirectory. Use `devices.json` to match PHY device IDs to the corresponding names, addresses and HCI captures. A successful run also saves `result.json`.
