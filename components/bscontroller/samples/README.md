# Bumble Host samples

English | [简体中文](README.zh-CN.md)

Each sample uses Bumble directly. Every Host connects to its own BSController over H4 on a Unix socket. The shared launcher starts the BabbleSim PHY and runs the Hosts on the simulation clock.

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
| `python -m samples.advertising_coding_selection` | Advertise with S=2 on the primary PHY and S=8 on the secondary PHY, then check both in the scan report. |
| `python -m samples.periodic` | An observer keeps two independent periodic advertising synchronizations and receives data from both. |
| `python -m samples.broadcast_iso` | One broadcaster sends two BIS streams to two synchronized receivers. The sample checks every SDU byte for byte. |
| `python -m samples.gatt` | A Central discovers a Battery Service, reads its value and receives a notification. |
| `python -m samples.central_multilink` | One Central keeps three Peripheral connections and verifies GATT reads and notifications on every link. Use `--peripherals N` to change the number of Peripherals. |
| `python -m samples.connected_iso` | Create one CIS over an ACL connection and exchange an ISO SDU in each direction. |
| `python -m samples.connection_subrating` | Negotiate a subrate factor of three and read the Battery Service. |
| `python -m samples.frame_space_update` | Negotiate a 200 µs LE 1M frame space from Central to Peripheral, then discover services. |
| `python -m samples.connection_rate` | Reduce the connection interval to 5 ms and run GATT on the updated link. |
| `python -m samples.sleep_clock_accuracy` | Read the peer's SCA before and after it improves its clock accuracy. |
| `python -m samples.power_control` | Read the peer's transmit power and receive a path loss zone report. |
| `python -m samples.flushable_acl` | Queue two ATT writes, let the flushable packet expire, and check delivery and HCI credits. |
| `python -m samples.public_key_validation` | Read the local P-256 public key and check rejection of an invalid peer public key. |
| `python -m samples.coexistence` | Five devices keep five ACL links, two periodic advertisers, and four periodic synchronizations while scanning, advertising, and exchanging GATT data. |

`broadcast_iso` accepts `--encrypted`, `--framed`, `--phy 1|2|3` (1M, 2M, Coded) and `--receivers N`.

### Concurrent roles

In `coexistence`, both hubs connect to both leaves. `hub-b` also connects to `hub-a`, so `hub-a` acts as Central and Peripheral at the same time. Each leaf accepts two Centrals.

The hubs each transmit a periodic train and synchronize with the other. An independent observer synchronizes with both. All five links must carry Battery Service notifications and reads while scan reports and periodic reports continue to arrive.

## Logs

Use `--log-dir /tmp/bscontroller-samples` to keep artifacts in a separate subdirectory for each run. `phy.log` contains the PHY process output. Open a device's `.snoop` for HCI traffic and its `.pcapng` for radio traffic. The shared `ellisys.bttrp` records transmissions from all Controllers.

Use `devices.json` to look up names, addresses and capture files by PHY device ID. A successful run also saves `result.json`.
