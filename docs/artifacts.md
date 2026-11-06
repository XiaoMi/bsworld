# Logs, Traces, and Captures

[English](artifacts.md) | [简体中文](zh-CH/artifacts.md)

BSWorld preserves framework logs, Application logs, HCI captures, and RF traces for every test. These files are not only console output: together they locate a failure in the Application, Host, Controller, or radio path.

## Output Directory and Isolation

At session start, BSWorld prints the output directory for the run:

```text
[bsworld] log dir: /tmp/run-<timestamp>/<uid>
```

The default root is `/tmp`. Override it with a command-line option or environment variable:

```sh
pytest tests/ --out-dir=/path/to/artifacts
OUT_DIR=/path/to/artifacts pytest tests/
```

Relative output roots are resolved to absolute paths before starting child processes, so PHY inputs remain valid when a child uses another working directory.

BSWorld appends `run-<timestamp>/<uid>` beneath the selected root. [pytest-xdist](https://pytest-xdist.readthedocs.io/) workers in one pytest session share this run directory, while every test writes beneath a separate path derived from its pytest node ID. Parallel tests therefore do not overwrite each other's logs, sockets, or captures.

## Docker and CI Results

The [Docker commands](../docker/README.md#run-tests) bind-mount `/output` at `.cache/docker-results` on the host. The default image command saves `pytest.xml` there, alongside the `run-<timestamp>/<uid>` directories containing per-test logs and captures.

In GitHub Actions, the `CI` workflow runs every check in parallel for pull requests and pushes to main: `Checks` (Ruff and the unit tests), `BSController`, and the `Zephyr`, `NuttX`, `TrouBLE`, and `Linux` platform workflows, which each build their image and run its integration tests inside it. A failing check does not stop the others, and `CI result` passes only when all of them pass. Open **Actions → CI → the run** to view a result; each workflow can also be run alone from the Actions tab. Each platform run uploads a `bsworld-results-<platform>-<run-id>-<attempt>` artifact with `integration.xml`, `integration.log`, the image tag and metadata, and per-test logs and captures under `integration/`. Reports and logs are uploaded on failure too and retained for 14 days.

See the [Docker guide](../docker/README.md#use-a-published-image) to select an image by `latest`, source commit, or Release tag.

## Directory Layout

A typical NuttX scanning test produces this layout for its `[zephyr]` case:

```text
/tmp/run-<timestamp>/<uid>/
    log/
        tests/
            integration/
                nuttx/
                    btsak/
                        test_btsak_scan_advertise/
                            test_nuttx_btsak_scanner_finds_advertiser_zephyr/
                                bsworld.log
                                babblesim/
                                    bs_2G4_phy_v1.log
                                    ellisys.bttrp
                                    att_matrix.txt
                                    timed_att_0_1_0.txt
                                bt_virt_controller/
                                    bt_virt_controller_hci0.log
                                    bt_virt_controller_hci1.log
                                nuttx/
                                    scanners/
                                        nuttx_btsak_scanner/
                                            nuttx_btsak_scanner.log
                                            hci0.snoop
                                    advertisers/
                                        nuttx_btsak_advertiser/
                                            nuttx_btsak_advertiser.log
                                            hci1.snoop
```

The `[bscontroller]` case writes the same files under `..._bscontroller/`, without `bt_virt_controller/`. `att_matrix.txt` and `timed_att_*.txt` appear only when the test defines matching static or time-varying attenuation paths.

## Artifact Reference

| File | Producer | Contents |
|---|---|---|
| `bsworld.log` | BSWorld | Process commands, working directories, PIDs, socket and device-lock barriers, teardown, Ellisys conversion, and framework warnings. |
| `babblesim/bs_2G4_phy_v1.log` | BabbleSim PHY | PHY process stdout and stderr. It can be empty during a normal run. |
| `bt_virt_controller/*.log` | ZephyrController | Controller startup output, assertions, and internal diagnostics. Written only when the test uses `ZephyrController`. |
| `<Application>/<name>.log` | Application | Combined Application stdout and stderr, also used as the source for `wait_log()`. Complete Zephyr firmware writes it under `zephyr/complete/<name>/`. |
| `<Application>/hciN.snoop` | Controller BTSnoop file | HCI commands, events, and ACL data between the Host and its Virtual Controller. |
| `babblesim/ellisys.bttrp` | BSWorld Python converter | Bluetooth LE over-the-air activity for every virtual device on the shared PHY. |
| `babblesim/att_matrix.txt` | BSWorld | Static path-attenuation matrix passed to the `multiatt` channel model. |
| `babblesim/timed_att_*.txt` | BSWorld | Time points and attenuation values for time-varying paths. |

Timestamps at the start of lines in `bsworld.log` use Linux wall-clock time and show how long process startup and teardown took on the host. Simulation timestamps in Application logs are generated by the corresponding runtime and represent discrete simulation time. Do not subtract one from the other to calculate protocol latency.

## Application Logs and `wait_log()`

Each Application's stdout and stderr are written to its `.log` file and made available to `ProcessInstance.wait_log()` as complete lines.

```python
await device.wait_log(r"Bluetooth initialized", timeout=30)
```

`wait_log()` has the following semantics:

- It applies a regular expression to complete lines.
- Its `timeout` uses the active event loop clock. After topology startup, this is simulation time rather than wall-clock time.
- Complete lines that reached the FIFO before the call remain matchable unless an earlier wait already consumed them.
- A later wait cannot match a line that an earlier wait already scanned past.
- An unterminated fragment is written to the `.log` file immediately, but it becomes matchable only after a newline completes the line.

Tail an Application log while a test is running:

```sh
tail -f /tmp/run-<timestamp>/<uid>/log/tests/.../nuttx_btsak_advertiser.log
```

## Framework Log

Every test has one `bsworld.log`, beginning when `topo.start()` starts framework activity for that test. It records:

- The BabbleSim instance ID and device count.
- Full commands for the PHY, Virtual Controllers, and Applications.
- Working directory, log path, and PID for every process.
- Startup barriers for Controller HCI sockets and BabbleSim device locks.
- Processes still running during teardown and their cleanup order.
- Inputs and output used for Ellisys conversion.

If a process exits with a nonzero status before teardown, BSWorld writes a warning here and includes the tail of that process's log.

## HCI BTSnoop Capture

Each Controller records HCI packets at its own transmit and receive boundaries, timestamped with simulation time. Files use BTSnoop v1, H4 datalink type 1002, complete packet lengths, and direction flags.

Open a capture with [Wireshark](https://www.wireshark.org/) or `btmon`:

```sh
wireshark /tmp/run-<timestamp>/<uid>/log/tests/.../hci1.snoop
btmon -r /tmp/run-<timestamp>/<uid>/log/tests/.../hci1.snoop
```

HCI capture is always enabled for both Controller implementations. Application-bound Controllers save the capture under the Application directory; otherwise it goes under `controllers/hciN/`. Complete Zephyr firmware has no external HCI capture. Radio traffic from both Controller implementations and from complete firmware is included in the shared `ellisys.bttrp` trace.

## Ellisys Over-the-Air Trace

The BabbleSim PHY writes per-device CSV dumps during the test. During teardown, BSWorld converts and merges transmission records in Python into `babblesim/ellisys.bttrp`, then removes temporary BabbleSim state, sockets, and the raw results directory.

To open the trace in [Ellisys](https://www.ellisys.com/products/bex400/) software, select **File > Import**, choose **Bluetooth packets**, click **Next**, click **Browse**, and select the test's `ellisys.bttrp` file.

<p align="center">
  <img src="images/ellisys-trace-import.gif" alt="Import a BSWorld Ellisys trace" width="760">
  <br>
  <sub>Importing <code>babblesim/ellisys.bttrp</code> into Ellisys for over-the-air packet analysis.</sub>
</p>

The `.bttrp` trace represents PHY-side radio activity and is useful for determining whether:

- An advertising or connection packet was transmitted.
- The receiver was listening on the matching channel and time.
- Attenuation or interference caused packet loss.
- Radio events from multiple devices collided.

PHY dumps and Ellisys conversion are always enabled.

## Failure Investigation Order

Follow the data path from top to bottom:

1. Check `bsworld.log` for process commands, HCI socket and device-lock barriers, or an early process exit.
2. Check the Application logs to locate the last business state and verify whether the text expected by `wait_log()` appeared.
3. Check the Virtual Controller logs and `hciN.snoop` to verify Host commands and Controller events.
4. Check `ellisys.bttrp` to determine whether packets reached the RF environment and whether attenuation, interference, or collisions explain a receive failure.
5. For an unexpected RF topology, compare the resolved device IDs, directions, and activation times in `att_matrix.txt` and `timed_att_*.txt`.

Artifacts remain in the output directory after the test. BSWorld removes runtime sockets/state under `/tmp/bs_*` and the raw BabbleSim results, but it does not delete the per-test logs and captures.
