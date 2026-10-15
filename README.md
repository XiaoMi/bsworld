# BSWorld

[English](README.md) | [中文](docs/zh-CH/README.md)

BSWorld is a deterministic simulation framework for Bluetooth Hosts and their Applications. It moves end-to-end scenarios that normally depend on multiple physical devices into a repeatable Linux environment, allowing the same scenario to run reliably in local development and CI while validating different Bluetooth Host stacks under one framework.

A [pytest](https://docs.pytest.org/) scenario defines the device topology, drives the required behavior, and verifies the outcome. BSWorld orchestrates the participants, controls simulation time and radio conditions, and archives runtime logs and protocol traces.

Figure 1 shows how BSWorld runs different Bluetooth Applications and Host stacks in one scenario. RTOS, Python, and QEMU-based Linux participants keep their native interfaces while connecting to independent Virtual Controllers through standard HCI. These environments can be mixed freely, with practical scale determined by host resources and scenario complexity.

<div align="center"><img src="docs/images/babblesim_arch.svg" alt="BSWorld architecture" width="80%"><br><em>Figure 1: BSWorld architecture</em><br><br></div>

All Virtual Controllers and optional interference sources share the PHY and simulation clock provided by [BabbleSim](https://babblesim.github.io/). The test can therefore control device scale, RF conditions, and time progression in the same way whether a Host runs in an RTOS, pytest, or a Linux guest.

## Why BSWorld

Hardware validation remains essential before release, but relying on a physical lab for every system test constrains both execution frequency and scale. A complete scenario may require several development boards and gateways, plus wiring, flashing, serial-port configuration, and repeated power cycles. Scarce hardware limits parallel execution, while host scheduling jitter and ambient RF activity make intermittent failures difficult to reproduce.

BSWorld therefore complements hardware testing by moving frequent regression, long-duration, large-scale, and RF fault-injection scenarios into a controlled virtual environment, while final product validation remains on physical hardware. This model depends on two core mechanisms: the **Virtual Controller** and **Discrete-Time Scheduling**.

## Technical Architecture

BSWorld does not reimplement a Bluetooth LE stack or RF simulator. Instead, it composes real Applications and Host stacks, a complete Virtual Controller, the BabbleSim radio environment, and pytest orchestration into an end-to-end system built around the standard HCI boundary.

### Application, Host, and Controller

A typical Bluetooth LE device contains three layers relevant to system testing:

- The **Application** implements product behavior and uses Host APIs.
- The **Host** implements protocols such as GAP, GATT, L2CAP, SMP, and Mesh.
- The **Controller** implements the Link Layer and radio-facing logic.

HCI is the standard contract between the Host and Controller. The Host may communicate with an integrated Controller, a Controller connected over UART or USB, or any other Controller that implements the required HCI behavior. Figure 2 shows a common dual-chip arrangement in which this boundary is explicit.

<div align="center"><img src="docs/images/zephyr-bluetooth-dual-chip.png" alt="Bluetooth LE Host and Controller in a dual-chip configuration" width="75%"><br><em>Figure 2: Host-only and Controller-only Bluetooth LE configurations (source: Zephyr Project, Apache-2.0)</em><br><br></div>

BSWorld uses this same HCI boundary as its virtualization interface. The Application and real Host stack remain in the environment under test; BSWorld neither replaces Host APIs with test doubles nor requires simulation-specific Bluetooth LE interfaces.

### Virtual Controller

Each Host instance exchanges standard HCI traffic with an independent Virtual Controller process over a Unix socket. The Virtual Controller is not a mock that returns scripted responses. It runs the complete Controller data path: HCI commands, events, and ACL data; Link Layer protocol procedures; and radio transmission and reception through the BabbleSim PHY. The Application therefore exercises complete protocol flows such as advertising, scanning, connection establishment, GATT, and Mesh.

Figure 3 compares the protocol path of a physical device with its BSWorld counterpart. The Application, Host, and HCI boundary remain unchanged. A complete Virtual Controller process replaces the physical Controller, while BabbleSim provides the radio medium.

<div align="center"><img src="docs/images/real_vs_virtual_controller.svg" alt="Complete Virtual Controller protocol path" width="80%"><br><em>Figure 3: Complete HCI, Link Layer, and RF paths through the Virtual Controller</em><br><br></div>

For the Virtual Controller implementation, runtime contract, and build process, see [Virtual Controller](components/bt_virt_controller/README.md).

The HCI and RF boundaries provide complementary diagnostics. Traffic on the HCI Unix socket is stored as `hciN.snoop` for analysis with [Wireshark](https://www.wireshark.org/) or another BTSnoop-capable tool. Radio activity on the BabbleSim PHY is stored as `babblesim/ellisys.bttrp` for over-the-air analysis with [Ellisys](https://www.ellisys.com/products/bex400/). The former identifies Host-to-Controller exchanges; the latter shows packet transmission, channel listening, interference, and collisions. Both are archived per test. See [Logs, traces, and captures](docs/artifacts.md) for file locations and generation details.

Figure 4 demonstrates the packet-level workflow for importing an `ellisys.bttrp` trace into Ellisys.

<div align="center">
  <img src="docs/images/ellisys-trace-import.gif" alt="Import a BSWorld Ellisys trace" width="760">
  <br>
  <em>Figure 4: Opening a BSWorld RF trace in Ellisys for packet-level debugging</em>
  <br><br>
</div>

### Discrete-Time Scheduling

BSWorld distinguishes two time bases:

| Term | Meaning | Role in BSWorld |
|---|---|---|
| **Discrete simulation time** | A logical timeline represented by simulation timestamps. It advances only after all participants synchronize. | Defines protocol timers, packet timing, RF events, asynchronous waits, and test timeouts. |
| **Wall-clock time** | The actual elapsed time on the host, including Linux scheduling, CPU load, process startup, and file I/O. | Determines how long a test takes for the user, but does not define protocol timing in a BabbleSim run. |

There is no fixed conversion between them. A Linux process may spend 100 ms of wall-clock time waiting to run while simulation time remains unchanged; a simulation step may also cross an interval with no pending events. The wall-clock cost of processing a given simulation interval depends on event density and host performance. Figure 5 illustrates the distinction: wall-clock time flows continuously, whereas discrete time advances between synchronization boundaries and remains fixed while callbacks, I/O, and assertions at a timestamp are processed.

<div align="center">
  <img src="docs/images/discrete-vs-wall-time.gif" alt="Discrete simulation time compared with wall-clock time" width="760">
  <br>
  <em>Figure 5: Wall-clock time flows continuously, while discrete simulation time jumps between event timestamps</em>
  <br><br>
</div>

Running binaries as Linux processes is not sufficient for deterministic simulation. If every process follows wall-clock time, scheduling jitter changes timer expiry and packet ordering. The effect is particularly significant during dense RF activity: the Bluetooth LE Link Layer has microsecond-scale constraints such as the 150 μs inter-frame space (`T_IFS`), but a general-purpose Linux scheduler cannot guarantee that several user-space processes will wake, exchange socket data, and execute within that interval. Using wall-clock time as protocol time can stretch packet intervals, alter collisions, and reorder radio events.

BSWorld registers native Application processes, Virtual Controllers, interference devices, and pytest in one BabbleSim simulation. Python Host stacks running inside pytest use the same simulation clock. A shared synchronization barrier advances every participant on one timeline, and the wall-clock cost of computing a step is excluded from simulation time. A 150 μs protocol interval therefore remains exactly 150 μs regardless of host load.

To place pytest on that timeline, BSWorld registers the pytest process itself as a BabbleSim participant. A Python clock client sends absolute-time WAIT requests over BabbleSim's FIFO protocol and waits for the PHY acknowledgment. pytest resumes only after that synchronization completes.

The pytest scheduler preserves the standard [asyncio](https://docs.python.org/3/library/asyncio.html) programming model. It intercepts only the event loop's blocking wait and advances simulation time through the FIFO clock client. See [Writing tests and RF scenarios](docs/writing-tests.md#async-waiting-and-simulation-time) for the supported asynchronous APIs, time semantics, and scheduler cycle.

### Determinism Boundary

Given identical binaries and test inputs, BSWorld controls simulation IDs and device allocation, clock targets and simulation-time deadlines, Controller addresses, RF-model inputs, and the simulation-step boundary at which Python evaluates each condition. Repeatability also requires controlled random sources. External networks, services driven by wall-clock time, and other Host I/O must be adapted or isolated; otherwise they can still introduce nondeterminism.

## Core Capabilities

- **Accelerated long-duration testing**: simulated timers do not require equivalent wall-clock delays. A simulated 24-hour timeout need not take 24 hours to execute; speedup depends on workload and host performance, with current representative scenarios reaching roughly 80x.
- **Repeatable protocol and RF behavior**: timing such as `T_IFS = 150 μs` is defined by simulation time. Host load changes execution duration, not the protocol timeline.
- **End-to-end validation**: exercise advertising, scanning, connection establishment, GATT, periodic advertising, Mesh provisioning, and multi-device interaction while retaining the real Host stack and standard HCI boundary.
- **RF fault injection**: define static or time-varying attenuation, burst interference, WLAN activity models, and recorded-signal playback.
- **Isolated parallel execution**: each test receives its own simulation ID, socket directory, and log directory, allowing [pytest-xdist](https://pytest-xdist.readthedocs.io/) workers to run independent radio environments concurrently.
- **Cross-layer diagnostics**: archive process logs, HCI BTSnoop captures, PHY logs, and Ellisys traces per test for correlated analysis across the Application, HCI, and RF layers.

Deterministic execution, structured test results, and complete protocol records also create a reproducible feedback loop for automation and AI-assisted development: code changes can be evaluated in whole-system scenarios, and failures provide inputs for subsequent analysis and iteration.

## Validated Scenarios

### Representative End-to-End Tests

| Application environment | Validated scenarios | Documentation |
|---|---|---|
| Zephyr Bluetooth samples | Broadcast/observe, targeted interference/recovery, Heart Rate central/peripheral, periodic advertising/sync, Mesh provisioning/configuration | [Tests and sample builds](tests/integration/zephyr/samples/README.md) · [Environment](tests/integration/zephyr/samples/common/source/README.md) |
| Bumble Python Host stack | Advertising/scanning, targeted interference/recovery, connection, periodic advertising/sync | [Tests](tests/integration/bumble/README.md) |
| Linux and BlueZ under QEMU | Controller initialization, advertising, scanning, connection, and disconnection through the Linux Bluetooth subsystem | [Tests](tests/integration/qemu/bluez/README.md) · [Environment and build](tests/integration/qemu/README.md) |
| TrouBLE Rust Host stack under QEMU | Advertising discovery, connection, Battery Service discovery, and Battery Level reads through HCI User Channel | [Tests](tests/integration/qemu/trouble/README.md) · [Environment and build](tests/integration/qemu/README.md) |
| NuttX Bluetooth Socket / BTSak | Advertising, scanning, shell-driven control, multiple advertisers, targeted interference/recovery | [Tests](tests/integration/nuttx/btsak/README.md) |

### One Host Stack, Multiple Runtime Environments

BSWorld supports several integration paths based on the stack's existing runtime. Portable embedded code can run on a supported RTOS or another POSIX-compatible simulation environment; asyncio-based Python stacks can run directly in pytest; Linux Applications and system services can run inside a complete QEMU guest. Test adapters handle platform-specific startup and HCI transport details, while the Bluetooth Application and Host retain their native interfaces.

Apache NimBLE provides a complete example of one Host stack using BSWorld across different runtime environments. It includes both a NuttX port and a Linux example, and this repository validates both paths:

| NimBLE runtime | Integration path | Validated behavior | Documentation |
|---|---|---|---|
| NuttX | Stock NuttX NimBLE port and Applications, built as a standalone simulation process | Discovery, connection, GATT service discovery, subscription | [Tests](tests/integration/nuttx/nimble/README.md) · [NuttX environment](tests/integration/nuttx/README.md) |
| Linux under QEMU | Upstream NimBLE Linux example using `hci0` through HCI User Channel | Advertising discovery with exact address and payload validation, connection, disconnection | [Tests](tests/integration/qemu/nimble/README.md) · [QEMU environment](tests/integration/qemu/README.md) |

## Quick Start

BSWorld runs on Linux/x86_64. We recommend the published Docker image, which includes Python, BSWorld, and all firmware needed by the integration tests. You can also build the image from the Dockerfile or prepare a native Linux environment.

### Docker (Recommended)

Install Docker Engine using the official [Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/), then pull the image and run its integration tests:

```sh
sudo docker pull ghcr.io/xiaomi/bsworld:latest
mkdir -p .cache/docker-results
# The published image runs as UID/GID 1000.
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  -e PYTHONPATH=/workspace \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  ghcr.io/xiaomi/bsworld:latest
```

These commands run the integration tests packaged in the image and save `pytest.xml`, logs, and captures under `.cache/docker-results` in the current directory. Images can be selected by `latest`, full commit ID (`sha-<commit>`), or a published Release tag such as `v1.0.0`. Use the same reference for pulling and running; select the matching commit or Release image when testing older source. Version selection examples are in the [Docker guide](docker/README.md#use-a-published-image).

To test your current checkout, mount the repository root at `/workspace`; firmware uses the compiled artifacts in the image. Mounting instructions, suite selection, unit tests, lint, and image build commands are described in the [Docker guide](docker/README.md).

### Local Linux

Development and test commands require Python 3.12 or newer. On a local Ubuntu 24.04 machine, first install Git, Make, compilers, Python virtual environment support, and the basic tools used by BabbleSim:

```sh
sudo apt-get update
sudo apt-get install -y \
  bash ca-certificates curl git file make binutils build-essential \
  python3.12 python3.12-dev python3.12-venv python3-pip \
  gcc-multilib g++-multilib coreutils findutils grep sed gawk
```

`gcc-multilib` and `g++-multilib` provide the 32-bit toolchain BabbleSim and Zephyr `nrf52_bsim` targets need.

For a new checkout:

```sh
git clone https://github.com/XiaoMi/bsworld.git
cd bsworld
```

From the repository root, create the project virtual environment and install BSWorld:

```sh
export BSWORLD_ROOT="$PWD"
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

This `.venv` is for BSWorld: `pip install -e .`, pytest, and `ruff`. Zephyr uses a second virtual environment, `$ZEPHYR_WORKSPACE/.venv`, only for `west` and `west build`. The [Zephyr workspace guide](tests/integration/zephyr/samples/common/source/README.md) creates it. Activate the environment that owns the next command. Activation changes `PATH` and leaves exported `BSWORLD_*` and BabbleSim variables set.

For development checks:

```sh
ruff check src tests
pytest tests/unit -q
```

#### Build and Test

Compile with BabbleSim's own Makefile:

```sh
cd "$BSWORLD_ROOT"
git submodule update --init --recursive components/babblesim
export BSIM_OUT_PATH="$BSWORLD_ROOT/components/babblesim/build"
export BSIM_COMPONENTS_PATH="$BSWORLD_ROOT/components/babblesim/components"
export BSIM_BASE_PATH="$BSIM_COMPONENTS_PATH"
export BSIM_BUILD_FAIL_ASAP=1
make -C "$BSWORLD_ROOT/components/babblesim" everything
export BSWORLD_BSIM_DIR="$BSIM_OUT_PATH"
```

Then follow the README commands for the [Controller](components/bt_virt_controller/README.md), [Zephyr samples](tests/integration/zephyr/samples/README.md), [NuttX](tests/integration/nuttx/README.md), and [QEMU / Buildroot](tests/integration/qemu/README.md).

#### Prebuilt binaries

A [release archive](https://github.com/XiaoMi/bsworld/releases/download/v0.3.9/bsworld-prebuilt-v0.3.9.zip) supplies Exodus-packaged firmware (Virtual Controller, eight Zephyr samples, NuttX BTSak and NimBLE) for native Linux tests without a Zephyr or NuttX toolchain. BabbleSim still has to be built as shown [above](#build-and-test); the archive has no QEMU assets. Choose the release that matches your checkout (`build-info.txt` lists its commits).

```sh
ver=v0.3.9
curl -fL -o "/tmp/bsworld-prebuilt-${ver}.zip" \
  "https://github.com/XiaoMi/bsworld/releases/download/${ver}/bsworld-prebuilt-${ver}.zip"
unzip -qo "/tmp/bsworld-prebuilt-${ver}.zip" -d /tmp
rsync -a --prune-empty-dirs --include='*/' --include='**/prebuilt/***' --exclude='*' \
  "/tmp/dist/bsworld-prebuilt-${ver}/" "$BSWORLD_ROOT/"
export BSWORLD_BT_VIRT_CONTROLLER_BIN="$BSWORLD_ROOT/components/bt_virt_controller/prebuilt/bt_virt_controller/exodus/bin/bt_virt_controller"
export BSWORLD_ZEPHYR_BROADCASTER_BIN="$BSWORLD_ROOT/tests/integration/zephyr/samples/broadcast_observe/prebuilt/broadcaster/exodus/bin/zephyr.exe"
export BSWORLD_ZEPHYR_OBSERVER_BIN="$BSWORLD_ROOT/tests/integration/zephyr/samples/broadcast_observe/prebuilt/observer/exodus/bin/zephyr.exe"
# Same prebuilt/<name>/exodus/bin/<launcher> path for BSWORLD_ZEPHYR_{PERIPHERAL_HR,CENTRAL_HR,PERIODIC_ADV,PERIODIC_SYNC,MESH,MESH_PROVISIONER}_BIN and BSWORLD_NUTTX_{BTSAK,NIMBLE}_BIN.
pytest tests/integration/zephyr/samples/broadcast_observe -q --no-ellisys
```

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration -n auto
```

## Documentation

- [Writing tests and RF scenarios](docs/writing-tests.md): process adapters, asyncio waits, shell commands, attenuation, and interference.
- [Logs, traces, and captures](docs/artifacts.md): log directories, BTSnoop captures, and Ellisys traces.
- [Development and unit tests](docs/development.md): development conventions, lint, unit tests, and coverage.

## Community

- [Contributing](CONTRIBUTING.md): development workflow, validation, and pull-request expectations.
- [Code of Conduct](CODE_OF_CONDUCT.md): standards for participation in project spaces.
- [Security Policy](SECURITY.md): supported versions and private vulnerability reporting.

## License

BSWorld is licensed under the [Apache License 2.0](LICENSE). Third-party components remain governed by their own licenses; see [Third-Party Notices](THIRD_PARTY_NOTICES.md).
