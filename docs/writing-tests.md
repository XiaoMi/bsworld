# Writing Tests

[English](writing-tests.md) | [简体中文](zh-CH/writing-tests.md)

The repository [README](../README.md#technical-architecture) introduces the Virtual Controller and discrete-time scheduler. This guide follows the practical test-authoring workflow: declare a topology, integrate Applications, drive and await behavior with asyncio, configure RF conditions, and verify the expected Bluetooth LE result.

## Quick Index

- [Test Lifecycle](#test-lifecycle)
- [Choosing Controllers](#choosing-controllers)
- [Async Waiting and Simulation Time](#async-waiting-and-simulation-time)
- [Integrating Application Processes](#integrating-application-processes)
- [Complete Firmware](#complete-firmware)
- [Process Shell Commands](#process-shell-commands)
- [RF Conditions and Interference](#rf-conditions-and-interference)
- [Topology as a pytest Test](#topology-as-a-pytest-test)

## Test Lifecycle

The `topo` fixture gives each test a `BluetoothTopology`. Tests declare participants, connectivity, and RF conditions instead of orchestrating operating-system processes directly. Suite-local factories describe native Applications as `ProcessInstance` objects with log waiting and optional shell input, and register them with the supplied topology; asyncio-based Python Host stacks register Virtual Controllers directly and open their HCI sockets after topology startup. A User Mode Linux guest likewise registers only its Controller beforehand and boots after startup.

The topology moves through four phases:

1. Test fixtures use program factories, `attach()`, `topo.add_process()`, `topo.add_controller()`, and RF configuration methods to record the intended topology without spawning processes or advancing simulation time.
2. `await topo.start()` assigns device identities, prepares isolated socket and log paths, and starts the PHY, Virtual Controllers, interference sources, and Application processes. It returns once every participant has registered and the event loop is running on simulation time.
3. Tests drive behavior and evaluate results through `ProcessInstance`, in-process Host stack APIs, shell commands, and standard asyncio APIs. The event loop advances shared simulation time only after all callbacks and I/O at the current timestamp have been processed.
4. Fixture teardown stops Controllers and processes, flushes logs and capture files, generates the Ellisys trace, and removes temporary simulation files.

Every test receives its own simulation ID, Unix socket directory, and log directory. Virtual tests can therefore run concurrently without sharing Controller state, RF state, or output files.

## Choosing Controllers

Each Host gets its own virtual Controller from `topo.add_controller()` and connects to its `hci_sock` after `topo.start()`:

```python
from bsworld.devices import BSController

ctl = topo.add_controller(controller_type=BSController, public_address="F0:F1:F2:F3:F4:01")
await topo.start()
# Connect the Host to ctl.hci_sock.
```

`BSController` is the default and `ZephyrController` is the native alternative; one topology may mix both. Complete Zephyr firmware carries its own Controller and needs neither; see [Complete Firmware](#complete-firmware) and [Virtual Controllers](controllers.md).

## Async Waiting and Simulation Time

BSWorld preserves the standard asyncio programming model. Tests do not need simulation-specific task, I/O, sleep, or timeout APIs. Pytest runs on a `BsimEventLoop`, which retains asyncio's tasks, futures, queues, events, streams, and sockets; only its source of time and blocking behavior change after simulation startup.

The event loop initially follows wall-clock time while the test prepares its topology. Once `await topo.start()` has launched and registered every BabbleSim participant, BSWorld binds the BabbleSim clock client to the loop. From that point, `loop.time()` advances with simulation time, and APIs such as `asyncio.sleep()` and `asyncio.wait_for()` use the same clock. A `timeout=30` therefore allows 30 simulated seconds, not 30 seconds of wall-clock time:

```python
import asyncio

await topo.start()

await dev.wait_log("ready", timeout=30)
await asyncio.sleep(0.2)
result = await asyncio.wait_for(operation(), timeout=5)
```

Simulation time only advances once every callback and I/O event at the current timestamp has been processed, so a test observes all work scheduled for one instant before the clock moves. Figure 1 shows one such step.

<div align="center"><img src="images/bluetooth-diagram.svg" alt="One BSWorld discrete-time step" width="80%"><br><em>Figure 1: One deterministic simulation-time step</em><br><br></div>

`ProcessInstance.wait_log()` uses the same asynchronous clock, so its `timeout` is also measured in simulation time. It applies a regular expression to complete buffered and newly arriving lines in arrival order. A fragment is not eligible until its terminating newline arrives, and a later call does not revisit lines that have already been scanned. Write consecutive log assertions in the expected event order.

> [!WARNING]
> Do not call blocking functions such as `time.sleep()` inside an async test because they block the event loop thread. Prefer asynchronous APIs; host-side blocking work that does not participate in protocol timing can be moved to `asyncio.to_thread()` when unavoidable.

## Integrating Application Processes

Each integration suite owns the factories that create its Applications. A factory takes the topology as its first argument and registers the new process; the test then selects a Controller and binds it with `host.attach(controller)` before `topo.start()`. Python Host stacks open their Controller's `hci_sock` after startup and run in the test's asyncio event loop. Reuse the suite's existing helpers when adding a scenario; its README describes setup, initialization, and available commands.

```python
from tests.integration.zephyr.shell.devices import create_zephyr_shell
from bsworld.devices import BSController

host = create_zephyr_shell(topo, name="host")
host.attach(topo.add_controller(
    controller_type=BSController, public_address="F0:F1:F2:F3:F4:02"
))
```

Attachments can change until device identities are published, normally by `topo.start()`; an earlier `plan_devices()` call also freezes them. When you construct a `ProcessInstance` yourself, register it with `topo.add_process()` and pass `requires_controller=True` if its Controller is attached later. A missing `attach()` then fails when the layout is planned, before the PHY starts, instead of the Host being treated as complete firmware. The suite factories already set it.

| Platform | Test guide |
|---|---|
| Bumble | [Bumble integration tests](../tests/integration/bumble/README.md#adding-scenarios) |
| NuttX | [BTSak and NimBLE](../tests/integration/nuttx/README.md#adding-scenarios) |
| TrouBLE | [Native TrouBLE tests](../tests/integration/trouble/README.md#adding-scenarios) |
| UML Linux | [BlueZ and NimBLE](../tests/integration/uml/README.md#adding-scenarios) |
| Zephyr | [Bluetooth shell](../tests/integration/zephyr/shell/README.md#adding-scenarios) |

## Complete Firmware

Firmware that contains its own Controller and radio, such as a Zephyr `nrf52_bsim` build, joins the PHY directly. Register it without a Controller:

```python
from pathlib import Path
from bsworld.devices import ProcessInstance

node = topo.add_process(ProcessInstance(
    command=lambda slot: [
        Path("/absolute/path/to/zephyr.exe"),
        f"-s={slot.sim_id}", f"-p={slot.phy_id}", f"-d={slot.dev_id}",
        "-rs=41", "-mro=1000", "-RealEncryption=1",
    ],
    label="node",
    shell=True,
    # -RealEncryption=1 loads libCryptov1.so from BabbleSim.
    env={"LD_LIBRARY_PATH": str(topo.env.bsim_dir / "lib")},
))
await topo.start()
```

- Set `shell=True` only for images with the UART on stdin/stdout; otherwise check output with `wait_log()`.
- Use the process as a `tx` or `rx` endpoint in attenuation paths.
- There is no `hci_sock` and no `hciN.snoop`; use the console log and `ellisys.bttrp`.

Build steps for the Zephyr shell images: [Zephyr Bluetooth shell](../tests/integration/zephyr/shell/README.md#build-and-run).

## Process Shell Commands

For a `ProcessInstance` registered with shell support, send commands after `await topo.start()` and the platform's initialization. Given an initialized `dev`, the basic pattern is:

```python
await dev.shell.run("...")
await dev.wait_log(r"...", timeout=5)
```

Replace the placeholders with a command and its expected output from the platform guide. `shell.run()` writes one line to standard input; it does not wait for command completion. Use `wait_log()` or another observable condition to verify the result.

## RF Conditions and Interference

Configure RF conditions before `await topo.start()`; the PHY configuration and participant set are immutable after startup. Attenuation and interference sources share one BabbleSim PHY with all Virtual Controllers. The PHY uses the `BLE_simple` modem by default, so RSSI, SNR, and path attenuation affect packet reception.

### Channel Attenuation

```python
topo.set_channel_attenuation(
    default_db=70.0,
    extra_db=0.0,
)
```

| Parameter | Default | Meaning |
|---|---|---|
| `default_db` | `60.0` | Path loss for every directed `tx -> rx` path not covered by a static or timed override. |
| `extra_db` | `0.0` | Extra attenuation added on top of every path. |
| `matrix_file` | `None` | Path to a raw `multiatt` matrix file. Mutually exclusive with generated paths. |
| `static_paths` | `[]` | List of fixed per-path attenuation values. |
| `timed_paths` | `[]` | List of time-varying per-path attenuation curves. |

### Per-Path Attenuation

Here, `peripheral` and `central` are devices already registered with the topology.

```python
from bsworld.interference import StaticAttenuation, TimedAttenuation

topo.set_channel_attenuation(
    default_db=50.0,
    static_paths=[
        StaticAttenuation(
            tx=peripheral,
            rx=central,
            attenuation_db=55.0,
        ),
    ],
    timed_paths=[
        TimedAttenuation(
            tx=central,
            rx=peripheral,
            times_us=[0, 10_000_000, 20_000_000],
            attenuation_db=[50.0, 80.0, 50.0],
        ),
    ],
)
```

Paths are directional. Configure both `tx -> rx` and `rx -> tx` when the two directions require distinct loss values. A `TimedAttenuation` curve pairs each timestamp with one attenuation value and linearly interpolates between adjacent points.

`tx` and `rx` can be registered `ProcessInstance` or `Controller` objects, `InterferenceRef` objects returned by `add_*_interference()`, or raw BabbleSim device IDs. An HCI Host process resolves to its associated Virtual Controller. A complete firmware process resolves to its own radio device ID.

### Burst Interference

```python
jammer = topo.add_burst_interference(
    center_freq_mhz=2402.0,
    power_dbm=10.0,
    interf_type="CW",
    windows=[],
    rand_seed=None,
)
```

`windows=[]` keeps the source active for the full simulation; use `[(start_us, end_us)]` for bounded windows. The returned `InterferenceRef` can be used as the `tx` endpoint of `StaticAttenuation` or `TimedAttenuation` to control loss from the source to a particular receiver.

### WLAN Activity Model

```python
topo.add_wlan_interference(
    channel=6,
    power_dbm=20.0,
    config_set="VideoConf",
)
```

Specify the frequency with exactly one of `channel=1..14` and `center_freq_mhz=<MHz>`. `config_set` selects a traffic profile such as `VoIP`, `VideoConf`, `FileDownload`, `100`, `50`, `25`, `10`, or `5`.

### Playback Interference

```python
topo.add_playback_interference("path/to/recording")
```

The input path is a prefix. BabbleSim expects `.Tx.csv`, `.Rx.csv`, `.RSSI.csv`, and `.CCA.csv` files with that prefix.

### Parameter Constraints

BSWorld validates RF inputs before spawning BabbleSim processes:

| Area | Parameter | Accepted range |
|---|---|---|
| Burst / WLAN | `center_freq_mhz` | `> 2273.0` and `< 2527.0` MHz. The API expects absolute MHz; common Bluetooth LE advertising channels are 2402, 2426, and 2480 MHz. |
| Burst / WLAN | `power_dbm` | `-128.0 .. 127.0` dBm. |
| Burst / WLAN | `windows` | At most 256 windows. Each `(start_us, end_us)` must use integer microseconds, `start_us > 0`, `end_us > start_us`, and windows must be strictly ordered and non-overlapping. `[]` enables the source for the full run. |
| Burst / WLAN | `rand_seed` | Unsigned 32-bit integer, `0 .. 4294967295`. |
| Burst / WLAN | `interf_type` / `mod_type` | `"CW"`, `"WLAN"`, `"BLE"`, `"WN1"`, `"WN2"`, `"WN4"`, `"WN8"`, `"WN16"`, `"WN20"`, `"WN40"`, `"WN80"`. |
| WLAN | `channel` | Wi-Fi channel `1 .. 14`; channel 14 maps to 2484 MHz. |
| WLAN | `config_set` | `"VoIP"`, `"VideoConf"`, `"FileDownload"`, `"FileDownload_24M"`, `"FileDownload_12M"`, `"FileDownload_6M"`, `"100"`, `"100_24M"`, `"100_12M"`, `"100_6M"`, `"50"`, `"50_24M"`, `"50_12M"`, `"50_6M"`, `"25"`, `"25_24M"`, `"25_12M"`, `"25_6M"`, `"10"`, `"10_24M"`, `"10_12M"`, `"10_6M"`, `"5"`. |
| Channel | `default_db` / `extra_db` | `-100.0 .. 100.0` dB. |
| Channel | `StaticAttenuation.attenuation_db` | Any finite dB value. The value is written to the matrix file for the selected path. |
| Channel | `TimedAttenuation.times_us` | Integer microseconds, `>= 0`, strictly ascending, with at least two points. |
| Channel | `TimedAttenuation.attenuation_db` | A sequence of finite dB values with the same length as `times_us`; adjacent points are linearly interpolated. |

## Topology as a pytest Test

The `topo` fixture keeps topology declarations, RF conditions, Application actions, and assertions in one Python test. In this example, suite-local `peripheral` and `central` fixtures register their Application processes before the test starts. RF configuration calls record the desired state; `await topo.start()` materializes it as a running simulation.

```python
async def test_central_receives_notification(topo, peripheral, central):
    topo.set_channel_attenuation(default_db=45.0)

    await topo.start()

    await peripheral.wait_log(r"Advertising successfully started")
    await central.wait_log(r"Connected")
    await central.wait_log(r"\[NOTIFICATION\]")
```

Figure 2 maps the example to its runtime behavior. Test fixtures create Controllers, attach Host-only Applications and register the processes, while `set_channel_attenuation()` configures RF paths on their shared PHY. Phase 1 corresponds to `topo.start()`, which returns only after the HCI sockets and all BabbleSim participants are ready. The remaining phases correspond to the three `wait_log()` calls as the event loop advances simulation time through advertising, connection establishment, and notification delivery.

<div align="center"><img src="images/pytest-topology-execution.svg" alt="A pytest topology expanded into deterministic Bluetooth LE execution" width="80%"><br><em>Figure 2: From a pytest topology to deterministic Bluetooth LE execution</em><br><br></div>

The lower half of the figure relates each log assertion to its HCI commands, events, and simulated over-the-air packets. The test states observable outcomes; BSWorld owns device and socket allocation, process lifecycles, time advancement, and persistence of logs and protocol traces.
