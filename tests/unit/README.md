# BSWorld unit tests

The unit suite exercises `src/bsworld` with deterministic fakes for BabbleSim and UML.

| Tests | Coverage |
|---|---|
| `test_topology.py` | BSController allocation and configuration, registration, lifecycle ordering, clock binding, capture placement, and RF endpoint resolution. |
| `test_interference.py` | Declarative RF input validation, numeric boundaries, window ordering, and defaults. |
| `backends/test_babblesim.py` | PHY, Controller worker, and interference startup, registration barriers, and artifact finalization. |
| `backends/test_ellisys.py` | Deterministic trace conversion, record filtering, coding metadata, and ordering. |
| `devices/test_layout.py` | Immutable device IDs for Controllers, the clock, and interference, plus lock paths and RF endpoint identity. |
| `devices/test_controller.py`, `devices/test_bscontroller.py` | Controller address validation, worker configuration, transport ownership, and cleanup. |
| `runtime/test_proc.py` | Spawn failures, process-group termination, reaping, diagnostics, and bookkeeping cleanup. |
| `runtime/test_pipe.py`, `runtime/test_shell.py`, `runtime/test_wait.py` | Complete-line log matching, command input, path/predicate waiting, cancellation, and timeout diagnostics. |
| `runtime/scheduler/` | FIFO clock protocol and the rule that ready callbacks and I/O drain before simulation time advances. |
| `uml/` | Guest lifecycle and Controller ownership, time-travel messages, simulation scheduling, and failure cleanup. |
| `test_plugin.py`, `test_env.py`, `test_deps.py`, `test_log.py` | Pytest identity/configuration, explicit artifact discovery, setup failures, and log routing. |
| `test_version.py` | Package version from the installed distribution metadata, with a fallback when the metadata is missing. |

## Test Design

- Group tests by the module they exercise under `src/bsworld`.
- Check that rejected registration or Guest startup leaves the topology usable. Assert Controller ownership and capture placement after a successful retry.
- Assert one observable behavior per test.
- Use small deterministic fakes at the process, clock, and RF boundaries. Tests that start a native PHY belong in `tests/integration`.
- Derive expected protocol values and file contents independently of the implementation where practical.

## Run

```sh
pytest tests/unit -q
ruff check .
```
