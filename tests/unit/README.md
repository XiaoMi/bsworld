# BSWorld unit tests

The unit suite exercises `src/bsworld` with deterministic fakes for BabbleSim and UML.

| Area | Invariants protected |
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

## Test design rules

- Place a test beside the production module whose contract it protects.
- Check that rejected registration or Guest startup leaves the topology usable. Assert Controller ownership and capture placement after a successful retry.
- Test one externally meaningful invariant per test; do not assert that obsolete symbols or unused keyword arguments are absent.
- Prefer small deterministic fakes at process, clock, and RF boundaries. Do not start a native PHY in this suite.
- Compute expected protocol/file values independently of the implementation where practical.
- Add a regression test only when it would fail for a plausible defect in core behavior, validation, or cleanup.

## Run

```sh
pytest tests/unit -q
ruff check .
```
