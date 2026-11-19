# BSWorld unit tests

The unit suite uses Python test dependencies and deterministic fakes for BabbleSim and external applications. Integration helpers are tested under `tests/integration`.

| Area | Invariants protected |
|---|---|
| `test_topology.py` | BSController allocation and configuration, registration, lifecycle ordering, clock binding, capture placement, and RF endpoint resolution. |
| `test_attachment.py` | Host attachment, ownership, layout freeze, and recovery from invalid or missing attachments. |
| `test_interference.py` | Declarative RF input validation, numeric boundaries, window ordering, and defaults. |
| `backends/test_babblesim.py` | PHY, Controller worker, and interference startup, duplicate registration, barriers, and artifact finalization. |
| `backends/test_ellisys.py` | Deterministic trace conversion, record filtering, coding metadata, and ordering. |
| `devices/test_layout.py` | Immutable device IDs, application slots, lock paths, and RF endpoint identity. |
| `devices/test_instance.py` | Command resolution, process startup boundaries, shell ownership, log capture placement, inherited environment overrides, and default/custom working directories. |
| `devices/test_controller.py`, `devices/test_bscontroller.py` | Controller address validation, worker configuration, transport ownership, and cleanup. |
| `runtime/test_proc.py` | Spawn failures, process-group termination, reaping, diagnostics, and bookkeeping cleanup. |
| `runtime/test_pipe.py`, `runtime/test_shell.py`, `runtime/test_wait.py` | Complete-line log matching, command input, path/predicate waiting, cancellation, and timeout diagnostics. |
| `runtime/scheduler/` | FIFO clock protocol and the rule that ready callbacks and I/O drain before simulation time advances. |
| `test_plugin.py`, `test_env.py`, `test_deps.py`, `test_log.py` | Pytest identity/configuration, explicit artifact discovery, setup failures, and log routing. |
| `test_version.py` | Package version from the installed distribution metadata, with a fallback when the metadata is missing. |

## Test design rules

- Place a test beside the production module whose contract it protects. Use `test_attachment.py` for topology-level Host attachment.
- Shared registration helpers follow the public register-then-attach flow. Keep pre-attached constructor cases only where that compatibility contract is being exercised.
- Check that rejected registration or attachment leaves the topology usable; assert the resulting slots and capture ownership after a successful retry.
- Test one externally meaningful invariant per test; do not assert that obsolete symbols or unused keyword arguments are absent.
- Prefer small deterministic fakes at process, clock, and RF boundaries. Do not start a native PHY in this suite.
- Compute expected protocol/file values independently of the implementation where practical.
- Keep integration helper tests under `tests/integration`; unit means an isolated production module, not merely a fast test.
- Add a regression test only when it would fail for a plausible defect in core behavior, validation, or cleanup.

## Run

```sh
pytest tests/unit -q
ruff check .
```
