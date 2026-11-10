# BSController

English | [简体中文](README.zh-CN.md)

BSController is a Bluetooth LE Controller written in Python. Your Host stack talks to it over standard H4 HCI, and it transmits and receives over a simulated radio ([BabbleSim](https://babblesim.github.io/)). Controllers on the same simulated radio communicate with each other. Simulation time is discrete, so runs are reproducible.

## Quick start

BSController needs Linux, Python 3.11 or newer, and a BabbleSim build. On Ubuntu 24.04 x86_64, install the build tools:

```sh
sudo apt-get update
sudo apt-get install -y git repo build-essential gcc-multilib g++-multilib python3-venv
```

Fetch BabbleSim and build its PHY, modem and channel models:

```sh
mkdir -p "$HOME/bsim" && cd "$HOME/bsim"
repo init -u https://github.com/BabbleSim/manifest.git -m everything.xml -b master
repo sync
export BSIM_OUT_PATH="$PWD/build"
export BSIM_COMPONENTS_PATH="$PWD/components"
make -j"$(nproc)" ext_2G4_phy_v1 ext_2G4_modem_BLE_simple ext_2G4_channel_multiatt
```

Keep `BSIM_OUT_PATH` exported whenever you run BSController. See the BabbleSim [fetching](https://babblesim.github.io/fetching.html) and [building](https://babblesim.github.io/building.html) guides for other options.

Then install BSController from this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

## Standalone Controller

Start a Controller and connect your Host to its socket:

```sh
bscontroller serve --hci-socket /tmp/radio.sock
```

`serve` starts a BabbleSim PHY and serves H4 HCI on the Unix socket; 

Give `--hci-socket` several paths to run several Controllers on the same PHY. They are named `hci0`, `hci1`, ..., and their public addresses count up from `--address` (default `F0:F1:F2:F3:F4:01`):

```sh
bscontroller serve --hci-socket /tmp/radio0.sock /tmp/radio1.sock
```


By default, simulation time advances as fast as the machine allows. A Host that uses wall-clock timers sees the Controller run ahead, so timers and timeouts on the two sides disagree. Add `--realtime` to keep simulation time at wall-clock time:

```sh
bscontroller serve --hci-socket /tmp/radio.sock --realtime
```

### Using BSController with BlueZ

For example, to let a scanning BlueZ adapter discover an advertising one, start two Controllers:

```sh
bscontroller serve --hci-socket /tmp/radio0.sock /tmp/radio1.sock --realtime
```

Once `serve` prints both sockets, run the four commands below, each in its own terminal:

```sh
socat PTY,link=/tmp/bscontroller-tty0,rawer UNIX-CONNECT:/tmp/radio0.sock
socat PTY,link=/tmp/bscontroller-tty1,rawer UNIX-CONNECT:/tmp/radio1.sock
sudo btattach -B "$(readlink -f /tmp/bscontroller-tty0)" -P h4
sudo btattach -B "$(readlink -f /tmp/bscontroller-tty1)" -P h4
```

In `bluetoothctl`, `select F0:F1:F2:F3:F4:01` and turn on advertising, then `select F0:F1:F2:F3:F4:02` and start scanning.

## Using BSController from Python

`RadioController` runs one Controller in its own worker thread. Create it inside a running asyncio event loop. `RadioSession` joins it to a PHY that is already running. The example below expects a PHY started with the same `sim_id` (`example`) and two devices: device `0` for the Controller and device `1` for the Host clock.

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

[examples/advertise_scan.py](examples/advertise_scan.py) is a complete program built on these APIs: it starts the PHY with `PhyProcess`, runs an advertising and a scanning Controller in one `RadioSession`, and a mock Host scans over the scanner's socket. Run it with `python examples/advertise_scan.py`; see [examples/README.md](examples/README.md).

There are two ways to connect a Host:

- **Socket.** Pass `hci_socket`, as above. Any Host that speaks H4 over a Unix socket can connect. Startup returns once the socket is listening. HCI commands are processed after the Controller has synchronized with the PHY.
- **In process.** Omit `hci_socket`. Register an object with an `on_packet(packet: bytes)` method through `controller.set_packet_sink(sink)` before starting, then send complete H4 packets with `controller.on_packet(packet)`.

Pass `hci_snoop_file=Path(...)` to record a BTSnoop capture, timestamped with simulation time.

Other public names are `ControllerConfig` (see [Configuration](#configuration)), `RadioEventLoop`, `Radio`, `Phy`, `Received` and `hci` (HCI packet classes).

When managing the runtime yourself:

- `RadioSession` does not start the PHY; use `bscontroller.runtime.PhyProcess`.
- With `clock_dev_id`, the Host event loop must be a `RadioEventLoop`. Without it, you provide the clock and a distinct PHY device ID for every participant.
- Stop the Controllers before closing the Host clock and the PHY; `async with RadioSession(...)` does this.

## Configuration

Values the Bluetooth specification leaves to the Controller form a `ControllerConfig`. Unset fields keep their defaults.

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

A configuration file has a `defaults` object for every Controller, and a `controllers` object with one entry per Controller name:

```json
{
  "defaults": {"aclPacketCount": 16},
  "controllers": {"hci0": {"maxConnections": 2}, "hci1": {"maxTxOctets": 27}}
}
```

Keys are camelCase; snake_case also works. For each value, the most specific source wins: built-in default, then `defaults`, then `controllers.<name>`, then Python keyword arguments. An unknown key, or a value outside its range, is skipped with a warning; that field keeps its previous value and the rest of the file still applies.

On the command line:

```sh
bscontroller config > bsc.json        # every default, ready to edit
bscontroller serve --hci-socket /tmp/radio0.sock /tmp/radio1.sock --config bsc.json
```

In Python, pass the file (or an equivalent dict) to `RadioController`. Each Controller picks the `controllers` entry with its own name:

```python
from bscontroller import ControllerConfig, RadioController

central = RadioController("hci0", "F0:F1:F2:F3:F4:01", config="bsc.json")
peripheral = RadioController("hci1", "F0:F1:F2:F3:F4:02", config="bsc.json")
other = RadioController("x", "F0:F1:F2:F3:F4:03", config=ControllerConfig(max_connections=2))
```

`ControllerConfig(...)` raises `ValueError` for an invalid value.

## Supported features

These are the Link Layer features of the Bluetooth Core specification, listed by the version that introduced them. `*` marks implemented features. Assigned Link Layer feature bits are reported in the LE FeatureSet ([Vol 6, Part B, 4.6](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core_v6.3/out/en/low-energy-controller/link-layer-specification.html#UUID-56ada5ed-4ae3-acee-198f-27ead57d86f1)). Bluetooth Classic (BR/EDR) is not.

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
| 5.1 | Periodic Advertising Sync Transfer |  |
| 5.1 | Initiating Periodic Advertising Sync Transfer for Local Periodic Advertising |  |
| 5.1 | Initiating Periodic Advertising Sync Transfer for Remote Periodic Advertising |  |
| 5.2 | Isochronous Broadcaster |  |
| 5.2 | Synchronized Receiver |  |
| 5.2 | Connected Isochronous Stream (CIS) |  |
| 5.3 | Periodic Advertising ADI Support | * |
| 5.3 | LE Channel Classification |  |
| 5.3 | Connection Subrating |  |
| 5.4 | Periodic Advertising with Responses |  |
| 6.0 | Channel Sounding |  |
| 6.0 | Decision-Based Advertising Filtering | * |
| 6.0 | Monitoring Advertisers |  |
| 6.0 | Frame Space Update |  |
| 6.1 | Randomized RPA Updates | * |
| 6.2 | Shorter Connection Intervals |  |
| 6.2 | Channel Sounding Amplitude-based Attack Resilience |  |
| 6.3 | Channel Sounding Inline Phase Correction Term Transfer |  |

## How it works

Each Controller contains an HCI layer, a Link Layer and a radio adapter. All Controllers connect to one BabbleSim PHY, which carries their packets on shared channels and advances one common simulation clock. Protocol behaviour follows [Bluetooth Core 6.3](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core_v6.3/out/en/architecture,-change-history,-and-conventions/architecture.html#UUID-37039e1f-e0cd-37ce-2cf0-15dd6e6cff2e).

<div align="center"><img src="docs/images/radio_controller_arch.svg" alt="Multiple Controllers sharing a BabbleSim PHY" width="80%"><br><em>Figure 1: Controllers on a shared PHY</em></div>

Inside one Controller, the Link Layer keeps a manager for each role (advertising, scanning, initiating, synchronizing, connections). Every ACL connection has its own control-procedure state, encryption state and connection-event executor. All roles share one radio scheduler, which decides what the single radio does next.

**Threads and time.** Each `RadioController` runs in a dedicated thread with its own event loop and PHY connection. The Host thread and the Controller thread share no state; they only pass immutable H4 packets. Radio time is in integer microseconds. Every Controller, and an optional Host clock, synchronizes with the PHY before simulation time advances, so timing does not depend on how fast the machine is.

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
├── examples/             # Runnable two-Controller example with a mock Host
└── tests/
    ├── unit/             # Fast tests, no PHY needed
    └── qualification/    # HCI/LL protocol cases; radio cases need BabbleSim
```

```sh
python -m pip install -e '.[test]'
python -m pytest tests/unit -n auto -q                  # about 3 s
python -m pytest tests/qualification -n auto -v -rs     # a few minutes, needs BSIM_OUT_PATH
python -m build                                         # sdist and wheel in dist/
```

The qualification suite runs 614 selected HCI and Link Layer test cases from the Bluetooth test specifications against independent lower testers. Passing them does not make BSController Bluetooth-qualified. See [tests/README.md](tests/README.md) for the test scope.

Licensing and third-party notices are in [LICENSE](LICENSE) and [NOTICE](NOTICE).
