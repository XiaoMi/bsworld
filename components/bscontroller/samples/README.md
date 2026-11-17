# Bumble Host samples

English | [简体中文](README.zh-CN.md)

Each sample runs one finite Bluetooth scenario using an upstream Bumble Host on every device. Each Host connects over Unix H4 to its own BSController. All Controllers share a BabbleSim PHY and the Host event loop uses simulation time.

From `components/bscontroller`, install the Host extra and select your BabbleSim build:

```sh
python -m pip install -e '.[bumble]'
export BSIM_OUT_PATH=/path/to/babblesim/build
python -m samples.advertise_scan
```

| Command | Scenario and checks |
|---|---|
| `python -m samples.advertise_scan` | A broadcaster is discovered by an observer. |
| `python -m samples.scan_adv` | Three devices advertise and scan concurrently; every device receives the other two. |
| `python -m samples.gatt` | A Central discovers a Battery Service, reads its value and receives a notification. |
| `python -m samples.central_multilink` | One Central keeps three Peripheral connections and verifies GATT reads and notifications on every link. Use `--peripherals N` to change the device count. |
| `python -m samples.periodic` | An observer keeps two independent periodic advertising synchronizations and receives data from both. |
| `python -m samples.coexistence` | Five devices keep five ACL links, two periodic advertisers and four periodic synchronizations while scanning, advertising and exchanging GATT data. |

Successful runs print a JSON result and exit; a missing report, wrong payload or lost connection fails the sample. Use `--log-dir /tmp/bscontroller-samples` to retain `result.json`, `phy.log`, a separate `.snoop` capture for each device, and the PHY's transmit/receive records under `air/`. `devices.json` maps PHY device numbers to names, addresses and HCI captures. Each run creates its own subdirectory. `--bsim-dir` overrides `BSIM_OUT_PATH`; `--timeout` bounds the complete sample in simulation seconds.

In `coexistence`, both hubs connect to both leaves, and `hub-b` also connects to `hub-a`. Thus `hub-a` has Central and Peripheral connections at the same time, and each leaf accepts two Centrals. Each hub advertises a periodic train and synchronizes with the other hub; a separate observer synchronizes with both. The check requires fresh scan reports and periodic reports while all five links carry Battery Service notifications and reads.
