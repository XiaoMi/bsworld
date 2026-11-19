# BSController

English | [简体中文](README.zh-CN.md)

BSController is a Bluetooth LE Controller written in Python. Host stacks connect over standard H4 HCI, and Controllers exchange radio packets through a shared [BabbleSim PHY](https://babblesim.github.io/). The PHY advances a discrete simulation clock for reproducible timing.

## Quick start

BSController requires Linux, Python 3.11 or newer, and a BabbleSim build. Install the build tools on Ubuntu 24.04 x86_64.

```sh
sudo apt-get update
sudo apt-get install -y git repo build-essential gcc-multilib g++-multilib python3-venv
```

Fetch BabbleSim and build the PHY, modem and channel models.

```sh
mkdir -p "$HOME/bsim" && cd "$HOME/bsim"
repo init -u https://github.com/BabbleSim/manifest.git -m everything.xml -b master
repo sync
export BSIM_OUT_PATH="$PWD/build"
export BSIM_COMPONENTS_PATH="$PWD/components"
make -j"$(nproc)" ext_2G4_phy_v1 ext_2G4_modem_BLE_simple ext_2G4_channel_multiatt
```

Keep `BSIM_OUT_PATH` exported in the shell used to run BSController. Other checkout and build options are covered in the BabbleSim [fetching](https://babblesim.github.io/fetching.html) and [building](https://babblesim.github.io/building.html) guides.

Return to the BSController directory to install the package.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

## Standalone Controller

`bscontroller serve` starts a BabbleSim PHY and exposes H4 HCI on a Unix socket. Point the Host at the socket path.

```sh
bscontroller serve --hci-socket /tmp/radio.sock
```

Pass multiple paths to `--hci-socket` to run Controllers on a shared PHY. Each socket gets its own Controller, named `hci0`, `hci1`, and so on. Public addresses increment from `--address`, which defaults to `F0:F1:F2:F3:F4:01`.

```sh
bscontroller serve --hci-socket /tmp/radio0.sock /tmp/radio1.sock
```

Add `--capture-air --log-dir /tmp/bscontroller` to save a shared `ellisys.bttrp` and two captures per Controller, `radioN.pcapng` and `hciN.btsnoop`.

The Ellisys trace records all Controller transmissions in simulation order. Each PCAPNG records that Controller's transmissions and packets returned by the PHY during reception. Each BTSnoop records H4 traffic between the Host and its Controller.

Simulation time advances as fast as the machine allows by default. For a Host that uses wall-clock timers, add `--realtime` to keep simulation time in step with wall-clock time.

```sh
bscontroller serve --hci-socket /tmp/radio.sock --realtime
```

### Using BSController with BlueZ

To discover one BlueZ adapter from another, start two Controllers in real time.

```sh
bscontroller serve --hci-socket /tmp/radio0.sock /tmp/radio1.sock --realtime
```

Once `serve` lists both sockets, use `socat` to create a pseudo-terminal for each socket. Attach the terminals as HCI UART devices with BlueZ's `btattach`. Run each command in a separate terminal.

```sh
socat PTY,link=/tmp/bscontroller-tty0,rawer UNIX-CONNECT:/tmp/radio0.sock
socat PTY,link=/tmp/bscontroller-tty1,rawer UNIX-CONNECT:/tmp/radio1.sock
sudo btattach -B "$(readlink -f /tmp/bscontroller-tty0)" -P h4
sudo btattach -B "$(readlink -f /tmp/bscontroller-tty1)" -P h4
```

In `bluetoothctl`, use `select F0:F1:F2:F3:F4:01` to select the advertising adapter and enable advertising. Select `F0:F1:F2:F3:F4:02` and start scanning.

## Using BSController from Python

Create a `RadioController` inside a running asyncio event loop. It runs the Controller in a dedicated thread. `RadioSession` starts and stops that thread and attaches the Controller to a running PHY.

For this example, start the PHY with `sim_id` set to `example` and two device slots. The Controller uses device `0`, and the Host clock uses device `1`.

```python
import asyncio
from pathlib import Path

from bscontroller import RadioController, RadioEventLoop, RadioSession


async def main():
    controller = RadioController(
        "controller", "F0:F1:F2:F3:F4:01", hci_socket=Path("/tmp/radio.sock")
    )
    async with RadioSession("example", [(0, controller)], clock_dev_id=1):
        await controller.terminated


with asyncio.Runner(loop_factory=RadioEventLoop) as runner:
    runner.run(main())
```

Choose the Host connection when creating the Controller.

- **Unix socket** with `hci_socket`. The worker opens the socket on startup and begins processing HCI commands once the Controller has synchronized with the PHY.
- **In-process H4** with `hci_socket` omitted. Before starting the Controller, register a sink with `controller.set_packet_sink(sink)`. The sink receives H4 packets through its `on_packet(packet: bytes)` method. To send an H4 packet to the Controller, call `controller.on_packet(packet)`.

Pass `hci_snoop_file=Path(...)` to record a BTSnoop capture, timestamped with simulation time.

Pass `rf_pcap_file=Path(...)` to write the Controller's radio traffic to PCAPNG. To merge transmissions from multiple Controllers, create one `EllisysCapture(Path(...))` and pass it as `ellisys_capture` to each Controller. Close it after the `RadioSession` exits to save the trace.

The package also exposes `Radio`, `Phy`, `Received` and the HCI packet classes in `hci`. Controller settings are described under [Configuration](#configuration).

When managing the simulation from Python, keep the PHY running for the lifetime of the session.

- Start the PHY with `bscontroller.runtime.PhyProcess` before entering `RadioSession`.
- To run the Host on simulation time, use a `RadioEventLoop` and give its clock a `clock_dev_id` distinct from the Controller device IDs.
- Exiting `RadioSession` stops the Controllers before closing its Host clock connection. Exit `PhyProcess` after the session ends.

For runnable examples of advertising, scanning, GATT, periodic synchronization and concurrent roles, see the [Bumble Host samples](samples/README.md).

## Configuration

`ControllerConfig` defines the values the Bluetooth specification leaves to the Controller. Fields you omit retain their defaults.

| JSON key | Default | Range | Spec (Vol 4, Part E) |
|---|---|---|---|
| `hciVersion` | `0x0F` (6.1) | `0x06`–`0x10` | 7.4.1 |
| `hciRevision` | `0` | `0`–`0xFFFF` | 7.4.1 |
| `lmpVersion` | `0x0F` (6.1) | `0x06`–`0x10` | 7.4.1 |
| `lmpSubversion` | `0` | `0`–`0xFFFF` | 7.4.1 |
| `manufacturer` | `0xFFFF` | `0`–`0xFFFF` | 7.4.1 |
| `aclPacketCount` | `8` | `1`–`255` | 7.8.2 |
| `aclPacketLength` | `251` | `27`–`65535` | 7.8.2 |
| `maxTxOctets` | `251` | `27`–`251` | 7.8.46 |
| `maxTxTime` | `17040` | `328`–`17040` | 7.8.46 |
| `maxRxOctets` | `251` | `27`–`251` | 7.8.46 |
| `maxRxTime` | `17040` | `328`–`17040` | 7.8.46 |
| `advertisingSetCount` | `16` | `1`–`240` | 7.8.58 |
| `maxAdvertisingDataLength` | `1650` | `31`–`1650` | 7.8.57 |
| `filterAcceptListSize` | `16` | `1`–`255` | 7.8.14 |
| `resolvingListSize` | `16` | `1`–`255` | 7.8.41 |
| `periodicAdvertiserListSize` | `8` | `1`–`255` | 7.8.73 |
| `maxConnections` | `64` | `1`–`3839` | 5.3.1 |
| `maxPeriodicSyncs` | `16` | `1`–`3839` | 5.3.1 |
| `isoPacketCount` | `16` | `1`–`255` | 7.8.2 |
| `isoPacketLength` | `251` | `5`–`16383` | 7.8.2 |
| `maxBigs` | `4` | `1`–`240` | 7.8.103, 7.8.106 |
| `maxBis` | `16` | `1`–`3839` | 5.3.1, 7.8.103, 7.8.106 |

Use `defaults` for settings shared by all Controllers and `controllers` for overrides by name.

```json
{
  "defaults": {"aclPacketCount": 16},
  "controllers": {"hci0": {"maxConnections": 2}, "hci1": {"maxTxOctets": 27}}
}
```

Keys accept camelCase and snake_case. Settings in `defaults` override the built-in values, and `controllers.<name>` overrides the shared settings for that Controller. `ControllerConfig.load()` and `ControllerConfig.from_mapping()` also accept keyword overrides with the highest priority. An unknown key or out-of-range value triggers a warning and leaves the current settings for that entry in effect.

Export the built-in defaults to a file, edit it and pass it to `serve`.

```sh
bscontroller config > bsc.json        # every default, ready to edit
bscontroller serve --hci-socket /tmp/radio0.sock /tmp/radio1.sock --config bsc.json
```

In a running asyncio event loop, pass a file path, an equivalent mapping or a `ControllerConfig` to `RadioController`. A file or mapping applies the `controllers` entry matching the Controller name.

```python
from bscontroller import ControllerConfig, RadioController

central = RadioController("hci0", "F0:F1:F2:F3:F4:01", config="bsc.json")
peripheral = RadioController("hci1", "F0:F1:F2:F3:F4:02", config="bsc.json")
other = RadioController("x", "F0:F1:F2:F3:F4:03", config=ControllerConfig(max_connections=2))
```

`ControllerConfig(...)` raises `ValueError` for an invalid value.

## Supported features

The table groups Link Layer features by the Bluetooth Core version that introduced them. `*` marks implemented features. BSController reports the assigned feature bits in the LE FeatureSet. See [Vol 6, Part B, 4.6](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core_v6.3/out/en/low-energy-controller/link-layer-specification.html#UUID-56ada5ed-4ae3-acee-198f-27ead57d86f1).

| Core spec version | Link Layer feature | Supported |
|---|---|:---:|
| 4.0 | LE Encryption | * |
| 4.1 | Connection Parameters Request Procedure | * |
| 4.1 | Extended Reject Indication | * |
| 4.1 | Peripheral-initiated Features Exchange | * |
| 4.1 | LE Ping Procedure | * |
| 4.2 | LE Data Packet Length Extension | * |
| 4.2 | LL Privacy | * |
| 4.2 | Extended Scanner Filter Policies | * |
| 5.0 | LE 2M PHY | * |
| 5.0 | LE Coded PHY | * |
| 5.0 | Channel Selection Algorithm #2 | * |
| 5.0 | Minimum Number of Used Channels Procedure | * |
| 5.0 | Extended Advertising | * |
| 5.0 | Periodic Advertising | * |
| 5.1 | Connection CTE Request |  |
| 5.1 | Connection CTE Response |  |
| 5.1 | Connectionless CTE Transmitter |  |
| 5.1 | Connectionless CTE Receiver |  |
| 5.1 | Periodic Advertising Sync Transfer | * |
| 5.1 | Initiating Periodic Advertising Sync Transfer for Local Periodic Advertising | * |
| 5.1 | Initiating Periodic Advertising Sync Transfer for Remote Periodic Advertising | * |
| 5.2 | Isochronous Broadcaster | * |
| 5.2 | Synchronized Receiver | * |
| 5.2 | Connected Isochronous Stream (CIS) |  |
| 5.3 | Periodic Advertising ADI Support | * |
| 5.3 | LE Channel Classification |  |
| 5.3 | Connection Subrating |  |
| 5.4 | Periodic Advertising with Responses | * |
| 6.0 | Channel Sounding |  |
| 6.0 | Decision-Based Advertising Filtering | * |
| 6.0 | LL Extended Feature Set | * |
| 6.0 | Monitoring Advertisers | * |
| 6.0 | Frame Space Update |  |
| 6.1 | Randomized RPA Updates | * |
| 6.2 | Shorter Connection Intervals |  |
| 6.2 | Channel Sounding Amplitude-based Attack Resilience |  |
| 6.3 | Channel Sounding Inline Phase Correction Term Transfer |  |

## How it works

The HCI layer exchanges packets with the Host. The Link Layer implements the controller procedures, and the radio adapter passes packets to and from the BabbleSim PHY. Protocol behavior follows [Bluetooth Core 6.3](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core_v6.3/out/en/architecture,-change-history,-and-conventions/architecture.html#UUID-37039e1f-e0cd-37ce-2cf0-15dd6e6cff2e).

<div align="center"><img src="docs/images/radio_controller_arch.svg" alt="Multiple Controllers sharing a BabbleSim PHY" width="80%"><br><em>Figure 1: Controllers on a shared PHY</em></div>

The Link Layer has separate managers for advertising, scanning, initiating connections, synchronization and connections. Each ACL connection keeps its own control-procedure state, encryption state and event executor. A single scheduler coordinates radio use across the roles.

Each Controller thread has its own event loop and PHY connection. The Host and Controller exchange complete H4 packets. The PHY advances simulation time in integer microseconds as the Controllers and any Host clock synchronize with it.

| Object | Owns |
| --- | --- |
| `RadioController` | Worker thread, Controller event loop, radio connection |
| `RadioSession` | Starting and stopping workers, optional Host clock |
| `H4Server` | Unix socket, the Host connection, H4 framing |
| `PhyProcess` | BabbleSim PHY process, its logs and simulation directory |

## Development

```text
.
├── src/bscontroller/     # Package: HCI, Link Layer, radio, runtime, config
├── samples/              # Bumble Host scenarios, including concurrent roles
└── tests/
    ├── unit/             # Unit tests that run without a PHY
    └── qualification/    # HCI and Link Layer specification tests
```

```sh
python -m pip install -e '.[test]'
python -m pytest tests/unit -n auto -q
python -m pytest tests/qualification -n auto -v -rs     # Radio cases use BSIM_OUT_PATH
python -m build                                        # sdist and wheel in dist/
```

The qualification suite uses independent lower testers to exercise 821 selected HCI and Link Layer cases from the Bluetooth test specifications. See the [test guide](tests/README.md) for coverage, setup and logs.

Licensing and third-party notices are in [LICENSE](LICENSE) and [NOTICE](NOTICE).
