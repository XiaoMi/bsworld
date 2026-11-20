# Writing Tests

English | [简体中文](zh-CH/writing-tests.md)

Use the pytest `topo` fixture to configure Controllers and RF conditions, connect Bluetooth Hosts, and verify their behavior on a shared simulation clock. For the components behind the fixture, see the [BSWorld architecture](../README.md#technical-architecture).

## Quick Index

- [Test Lifecycle](#test-lifecycle)
- [Adding Controllers](#adding-controllers)
- [Async Waiting and Simulation Time](#async-waiting-and-simulation-time)
- [Running Hosts in UML](#running-hosts-in-uml)
- [Process Shell Commands](#process-shell-commands)
- [RF Conditions and Interference](#rf-conditions-and-interference)
- [Topology as a pytest Test](#topology-as-a-pytest-test)

## Test Lifecycle

The `topo` fixture gives each test a `BluetoothTopology`.

1. Register one Controller per Host with `topo.add_controller()` and configure RF conditions.
2. Call `await topo.start()` to start the PHY, Controllers, interference sources, and simulation clock.
3. Boot UML Guests and check their behavior through their consoles. For Python Hosts such as Bumble, connect to the Controller's HCI socket from the test's asyncio event loop.
4. The fixture stops the Guests, Controllers, and PHY at teardown and saves the logs and captures.

The fixture assigns each test a simulation ID and separate Unix socket and log directories. Parallel runs keep their Controller state, RF state, and artifacts isolated.

## Adding Controllers

Allocate a virtual Controller per Host with `topo.add_controller()`. Connect the Host to the Controller's `hci_sock` once `topo.start()` has returned.

```python
ctl = topo.add_controller(public_address="F0:F1:F2:F3:F4:01")
await topo.start()
# Connect the Host to ctl.hci_sock.
```

To adjust the Controller's address, buffers, or other limits, see [BSController configuration](controllers.md).

## Async Waiting and Simulation Time

Tests use standard asyncio tasks, futures, queues, events, streams, and sockets on a `BsimEventLoop`. The loop follows wall-clock time during setup. When `await topo.start()` finishes registering the BabbleSim participants, BSWorld binds the loop to the simulation clock.

From then on, `loop.time()`, `asyncio.sleep()`, and `asyncio.wait_for()` use simulation time. The example's `timeout=30` allows 30 simulated seconds.

```python
import asyncio

await topo.start()

await dev.wait_log("ready", timeout=30)
await asyncio.sleep(0.2)
result = await asyncio.wait_for(operation(), timeout=5)
```

The scheduler handles ready callbacks and I/O at the current simulated instant before advancing the clock.

<div align="center"><img src="images/bluetooth-diagram.svg" alt="One BSWorld discrete-time step" width="80%"><br><em>Figure 1: One deterministic simulation-time step</em><br><br></div>

`UmlGuest.wait_log()` also measures its timeout in simulation time. It scans buffered and incoming lines in arrival order and returns the first line that matches the regular expression. A line becomes available when its newline arrives. The call consumes every line it scans, so write successive log assertions in the expected event order.

> [!WARNING]
> Use asynchronous APIs in tests. Calls such as `time.sleep()` block the event loop. Use `asyncio.to_thread()` for unavoidable blocking work on the host that is independent of protocol timing.

## Running Hosts in UML

Add one Controller per Guest before starting the topology. After startup, open each Guest with `bsworld.uml.UmlGuest` as a context manager.

The Guest boots the supplied kernel with a read-only root filesystem and connects the Controller's H4 socket to `/dev/ttyS0`. Set `init` to an absolute path inside the Guest, such as `/init`.

```python
from bsworld.uml import UmlGuest

controller = topo.add_controller(log_subdir="uml/host")
await topo.start()

with UmlGuest(
    topo,
    controller,
    kernel=kernel,
    rootfs=rootfs,
    init="/init",
    label="host",
    log_subdir="uml/host",
) as guest:
    await guest.wait_log(r"^BSWORLD_UML_READY$")
    await guest.shell.run("btattach -B /dev/ttyS0 -P h4 &")
    await guest.wait_log(r"Device index 0 attached")
```

Give each Guest its own label and Controller when testing several Guests. Each Guest has a separate console and time-travel socket. All Guests share the topology's clock and use no PHY device IDs of their own.

For instructions on preparing the test kernel and root filesystem, see the [UML test environment](../tests/integration/uml/README.md).

## Process Shell Commands

Once the Guest console is ready, send a command with `shell.run()`. It returns as soon as the line has been written to standard input. Use `wait_log()` or another observable condition to confirm the command's result.

```python
await dev.shell.run("...")
await dev.wait_log(r"...", timeout=5)
```

Choose a command and its expected output from the corresponding [UML test suite guide](../tests/integration/uml/README.md#test-suites).

## RF Conditions and Interference

Register Controllers and interference sources and configure RF conditions before `await topo.start()`. Controllers and interference sources share one BabbleSim PHY, which uses the `BLE_simple` modem by default. RSSI, SNR, and path attenuation affect packet reception.

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
| `static_paths` | `None` | List of fixed per-path attenuation values. |
| `timed_paths` | `None` | List of time-varying per-path attenuation curves. |

### Per-Path Attenuation

Use Controllers already registered with the topology for `peripheral` and `central`.

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

`tx` and `rx` accept registered `BSController` objects, `InterferenceRef` objects from `add_*_interference()`, and raw BabbleSim device IDs.

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

Use `windows=[]` to keep the source active for the full simulation, or `[(start_us, end_us)]` to specify active windows.

To set the loss from the source to a particular receiver, use the returned `InterferenceRef` as the `tx` endpoint of `StaticAttenuation` or `TimedAttenuation`.

### WLAN Activity Model

```python
topo.add_wlan_interference(
    channel=6,
    power_dbm=20.0,
    config_set="VideoConf",
)
```

Specify either a channel from 1 to 14 or a center frequency in MHz. Use `config_set` to select a traffic profile such as `VoIP`, `VideoConf`, `FileDownload`, `100`, `50`, `25`, `10`, or `5`.

### Playback Interference

```python
topo.add_playback_interference("path/to/recording")
```

The input path is a prefix. BabbleSim expects `.Tx.csv`, `.Rx.csv`, `.RSSI.csv`, and `.CCA.csv` files with that prefix.

### Parameter Constraints

BSWorld validates these RF inputs before spawning BabbleSim processes.

| Area | Parameter | Accepted range |
|---|---|---|
| Burst / WLAN | `center_freq_mhz` | Absolute frequency `> 2273.0` and `< 2527.0` MHz. Bluetooth LE primary advertising channels use 2402, 2426, and 2480 MHz. |
| Burst / WLAN | `power_dbm` | `-128.0 .. 127.0` dBm. |
| Burst / WLAN | `windows` | At most 256 windows in integer microseconds. Each `(start_us, end_us)` requires `start_us > 0` and `end_us > start_us`. Each window must start after the previous window ends. `[]` enables the source for the full run. |
| Burst / WLAN | `rand_seed` | Unsigned 32-bit integer, `0 .. 4294967295`. |
| Burst / WLAN | `interf_type` / `mod_type` | `"CW"`, `"WLAN"`, `"BLE"`, `"WN1"`, `"WN2"`, `"WN4"`, `"WN8"`, `"WN16"`, `"WN20"`, `"WN40"`, `"WN80"`. |
| WLAN | `channel` | Wi-Fi channel `1 .. 14`. Channel 14 maps to 2484 MHz. |
| WLAN | `config_set` | `"VoIP"`, `"VideoConf"`, `"FileDownload"`, `"FileDownload_24M"`, `"FileDownload_12M"`, `"FileDownload_6M"`, `"100"`, `"100_24M"`, `"100_12M"`, `"100_6M"`, `"50"`, `"50_24M"`, `"50_12M"`, `"50_6M"`, `"25"`, `"25_24M"`, `"25_12M"`, `"25_6M"`, `"10"`, `"10_24M"`, `"10_12M"`, `"10_6M"`, `"5"`. |
| Channel | `default_db` / `extra_db` | `-100.0 .. 100.0` dB. |
| Channel | `StaticAttenuation.attenuation_db` | Any finite dB value. The value is written to the matrix file for the selected path. |
| Channel | `TimedAttenuation.times_us` | Integer microseconds, `>= 0`, strictly ascending, with at least two points. |
| Channel | `TimedAttenuation.attenuation_db` | A sequence of finite dB values with the same length as `times_us`. The PHY interpolates linearly between adjacent points. |

## Topology as a pytest Test

- For two Guests advertising to a shared Bumble scanner, see the [BlueZ multi-Guest test](../tests/integration/uml/bluez/test_uml_bluez.py).
- For directional attenuation and scan recovery after interference ends, see the [NuttX interference test](../tests/integration/uml/nuttx/test_btsak.py).
