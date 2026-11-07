# BSWorld unit tests

The unit suite protects BSWorld's implementation contracts in isolation. It does not require a native BabbleSim build, firmware images, a UML kernel, or external Bluetooth applications. Tests for helpers under `tests/integration` live with the integration tree even when they run without native artifacts.

| Area | Invariants protected |
|---|---|
| `test_topology.py` | Public registration rules, lifecycle ordering, clock binding, capture placement, and topology-level RF resolution. |
| `test_attachment.py` | Explicit Controller binding, ownership, precise freeze boundaries, failed-attachment recovery, and rejection after startup. |
| `test_firmware.py` | Complete-only and mixed topologies, RF endpoint mapping, and frozen participant counts. |
| `test_interference.py` | Declarative RF input validation, numeric boundaries, window ordering, and defaults. |
| `backends/test_babblesim.py` | PHY/controller/interference startup ordering, complete-firmware startup without Virtual Controllers, duplicate registration, barriers, and artifact finalization. |
| `backends/test_ellisys.py` | Deterministic trace conversion, record filtering, coding metadata, and ordering. |
| `devices/test_layout.py` | Immutable device IDs, application slots, lock paths, and RF endpoint identity. |
| `devices/test_instance.py` | Command resolution, process startup boundaries, shell ownership, log capture placement, inherited environment overrides, and default/custom working directories. |
| `devices/test_controller.py`, `devices/test_bscontroller.py` | Controller identity validation and Python-controller adapter ownership/cleanup. |
| `runtime/test_proc.py` | Spawn failures, process-group termination, reaping, diagnostics, and bookkeeping cleanup. |
| `runtime/test_pipe.py`, `runtime/test_shell.py`, `runtime/test_wait.py` | Complete-line log matching, command input, path/predicate waiting, cancellation, and timeout diagnostics. |
| `runtime/scheduler/` | FIFO clock protocol and the rule that ready callbacks and I/O drain before simulation time advances. |
| `test_plugin.py`, `test_env.py`, `test_deps.py`, `test_log.py` | Pytest identity/configuration, explicit artifact discovery, setup failures, and log routing. |
| `test_version.py` | Package version from the installed distribution metadata, with a fallback when the metadata is missing. |

## Test design rules

- Place a test beside the production module whose contract it protects; topology-level binding and firmware scenarios live in `test_attachment.py` and `test_firmware.py`.
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
