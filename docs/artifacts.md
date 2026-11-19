# Logs, Traces, and Captures

[English](artifacts.md) | [简体中文](zh-CH/artifacts.md)

BSWorld stores artifacts by test so you can trace a failure from the Guest console through HCI to the simulated radio.

## Output Directory and Isolation

At session start, BSWorld prints the run directory.

```text
[bsworld] log dir: /tmp/run-<timestamp>/<uid>
```

The output root defaults to `/tmp`. Set it with `--out-dir` or `OUT_DIR`.

```sh
pytest tests/ --out-dir=/path/to/artifacts
OUT_DIR=/path/to/artifacts pytest tests/
```

Relative output roots are resolved to absolute paths before starting child processes, so PHY inputs remain valid when a child uses another working directory.

BSWorld creates `run-<timestamp>/<uid>` under the output root. Workers in the same pytest-xdist session share this run directory. Each test writes its logs and captures to a separate directory derived from its pytest node ID.

## Docker and CI Results

The default Docker command writes `pytest.xml` and the `run-<timestamp>/<uid>` directories to `/output`. To retain them on the host, mount `.cache/docker-results` at `/output` as shown in the [Docker test commands](../docker/README.md#run-tests).

For a particular revision or release, choose an image tag from the [Docker version guide](../docker/README.md#select-a-version).

In CI, the UML job runs the Guest and BabbleSim integration tests in one image. It uploads the test report, logs, captures and image metadata to `bsworld-results-uml-<run-id>-<attempt>` for 14 days.

## Directory Layout

For example, the test where Bumble scans for NuttX advertising produces this directory.

```text
/tmp/run-<timestamp>/<uid>/log/tests/integration/uml/nuttx/
    test_btsak/test_bumble_scans_nuttx_advertising/
        bsworld.log
        babblesim/
            bs_2G4_phy_v1.log
            ellisys.bttrp
        uml/nuttx/
            nuttx.log
            hci0.snoop
        bumble/scanner/
            hci1.snoop
```

## Artifact Reference

| File | Producer | Contents |
|---|---|---|
| `bsworld.log` | BSWorld | Topology startup, teardown and warnings. |
| `babblesim/bs_2G4_phy_v1.log` | BabbleSim PHY | PHY process stdout and stderr. It can be empty during a normal run. |
| `uml/<label>/<label>.log` | UML Guest | Combined console stdout and stderr. A test can set a different log directory. |
| `hciN.snoop` | BSController | H4 traffic between the Host and its Controller. |
| `babblesim/ellisys.bttrp` | BSWorld | Time-sorted Bluetooth LE transmissions from the PHY dumps. |
| `babblesim/att_matrix.txt` | BSWorld | Static path-attenuation matrix passed to the `multiatt` channel model. |
| `babblesim/timed_att_*.txt` | BSWorld | Time points and attenuation values for time-varying paths. |

Timestamps in `bsworld.log` use the host wall clock. Use them to measure startup and teardown. Measure protocol intervals using timestamps from the same simulation clock, such as those in an HCI capture.

## Application Logs and `wait_log()`

BSWorld saves the Guest console to a `.log` file. In a test, use `UmlGuest.wait_log()` to match its output.

```python
await device.wait_log(r"Bluetooth initialized", timeout=30)
```

`wait_log()` matches a regular expression against complete lines in arrival order. It consumes each line it scans, including the match. Unread lines can still match after a later call starts. A fragment appears in the file before its terminating newline makes it eligible for matching.

After topology startup, `timeout` uses simulation time.

Set `TEST_LOG_DIR` to the per-test directory shown above to follow the NuttX console.

```sh
tail -f "${TEST_LOG_DIR:?}/uml/nuttx/nuttx.log"
```

## Framework Log

Starting with `topo.start()`, BSWorld writes the following details to `bsworld.log`.

- The BabbleSim instance ID and device count.
- PHY and UML Guest startup commands.
- The working directory, log path, and PID of each process.
- Readiness of Controller HCI sockets and BabbleSim device locks.
- Which processes were still running at teardown and the order they were stopped.
- Inputs and output used for Ellisys conversion.

If a managed process exits with a nonzero status before cleanup, BSWorld logs the exit and the final lines of its output.

## HCI BTSnoop Capture

Each Controller records the HCI packets it sends and receives with simulation timestamps. The file uses BTSnoop v1 and H4 datalink type 1002, with full packet lengths and direction flags.

With `TEST_LOG_DIR` set to the example test directory, open the Bumble capture with `btmon` or [Wireshark](https://www.wireshark.org/).

```sh
wireshark "${TEST_LOG_DIR:?}/bumble/scanner/hci1.snoop"
btmon -r "${TEST_LOG_DIR:?}/bumble/scanner/hci1.snoop"
```

Controllers write to `controllers/hciN/` by default. Set `log_subdir` when adding a Controller to put its capture alongside the Guest or Bumble artifacts.

## Ellisys Over-the-Air Trace

BSWorld enables per-device PHY CSV dumps at startup. At teardown, it merges the available transmission records into `babblesim/ellisys.bttrp`.

In [Ellisys](https://www.ellisys.com/products/bex400/), select **File > Import**, choose **Bluetooth packets**, then use **Next** and **Browse** to open `ellisys.bttrp`.

<p align="center">
  <img src="images/ellisys-trace-import.gif" alt="Import a BSWorld Ellisys trace" width="760">
</p>

Use the trace to check which packets were transmitted, their channels and times, and whether transmissions overlap. It contains transmit records and a fixed RSSI value. To investigate a receive failure, compare it with the receiving Controller's HCI capture and the test's attenuation and interference settings.

## Failure Investigation Order

1. Check `bsworld.log` for startup commands, missing sockets or device locks, and early process exits.
2. Check the Guest console for its last reported state and the line expected by `wait_log()`.
3. Check `hciN.snoop` for the expected Host commands and Controller events.
4. Check `ellisys.bttrp` for the expected transmissions and their timing.
5. For an unexpected RF path, compare the device IDs, directions, and times in the generated `att_matrix.txt` and `timed_att_*.txt` files.

BSWorld keeps the per-test artifacts in the output directory and removes temporary BabbleSim state, sockets, and raw results during teardown.
