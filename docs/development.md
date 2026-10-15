# Development and Unit Tests

[English](development.md) | [中文](zh-CH/development.md)

We recommend the [published Docker image](../docker/README.md#use-a-published-image) for development checks and integration tests. Mount your checkout at `/workspace` to check the current code; see the [Docker test commands](../docker/README.md#run-tests). For a native Python environment, follow the README's [local Linux setup](../README.md#local-linux).

## Run Unit Tests

With the published image, run from the repository root:

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest pytest tests/unit -q -p no:cacheprovider
```

With a local Python environment:

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/unit -q
```

## Unit Test Coverage and Organization

Tests under `tests/unit/` are organized by subsystem. Each test should focus on one stable public behavior, with a name that describes the behavior it verifies.

| Test file | Coverage |
|---|---|
| `test_plugin.py` | pytest plugin options, output directories, xdist identity sharing, and `--no-hci-dump` |
| `test_env.py` | Environment variables, artifact path resolution, and runtime environment detection |
| `test_artifact_paths.py` | Environment-selected artifact paths for Zephyr, NuttX, and QEMU tests |
| `test_dependency_readiness.py` | BabbleSim binaries/modules, Controller files, and executable permissions |
| `test_topology.py` | Topology lifecycle, Application registration, generic process management, and Controller allocation |
| `test_babblesim_backend.py` | BabbleSim process arguments, device IDs and Controller setup, RF interference and attenuation, trace conversion, and cleanup |
| `test_hci.py` | Virtual Controller BTSnoop socket capture |
| `test_scheduler.py` | Discrete simulation time, standard asyncio primitives, callback and I/O ordering |
| `test_bsim_clock.py` | FIFO registration, binary message encoding, connection and response timeouts, partial I/O, and disconnect handling |
| `test_runtime.py` | FIFO log capture, complete-line matching, and `wait_log` |
| `test_interference.py` | RF interference and attenuation configuration validation |

Application behavior, RF propagation itself, and end-to-end Bluetooth LE examples are covered by the simulation suites under `tests/integration/*`.

## Linting

Use [Ruff](https://docs.astral.sh/ruff/) for Python linting:

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest ruff check --no-cache src tests
```

With a local Python environment, check or fix lint findings:

```sh
ruff check src tests
ruff check --fix src tests
```

Configuration lives in `pyproject.toml` under `[tool.ruff]`.

## Documentation

Documentation is maintained as plain Markdown. English project documentation remains under `docs/`, while its Chinese translation is centralized under `docs/zh-CH/`. Component-specific README files under `components/` and `tests/` remain beside the component they describe, with Chinese files using the `.zh-CN.md` suffix.

Use the root README as the documentation entry point, and update the matching English/Chinese page together when behavior changes.

## Extending BSWorld

Most new Bluetooth LE Application environments only need a suite-local helper, tests beside the suite that owns them, and README build commands when native binaries are involved. Zephyr and NuttX keep their process launch contracts in their own integration directories because each environment has a different argv convention. Python stacks such as Bumble can run in pytest and connect directly to a Controller socket. Linux stacks such as BlueZ can run inside a QEMU Guest whose HCI UART is bridged to the same socket interface. Every registered Application receives an independent Virtual Controller.

Framework changes are needed only when the integration introduces a new execution contract:

- **Process launch convention**: add or extend a test-side adapter that creates a `ProcessInstance` with the platform's BabbleSim identity and HCI socket arguments, then exercise that adapter in the owning integration suite.
- **RF model wrapper**: add the public configuration type and validation in `src/bsworld/interference.py`, translate it to BabbleSim arguments in `src/bsworld/backends/babblesim.py`, and test both invalid input and generated commands.

Keep platform-specific profiles, source configuration, and build instructions with their test suite. Shared framework code should contain only behavior that is required by more than one Application environment or forms part of BSWorld's public execution model.

## Coverage

Branch coverage is provided by [coverage.py](https://coverage.readthedocs.io/) and enabled via `[tool.coverage.run]` in `pyproject.toml`.

Run the following commands in the local Python environment:

```sh
# Unit-only coverage, fast and no firmware/BabbleSim required
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
python -m coverage run -m pytest \
  -p pytest_asyncio.plugin -p bsworld.plugin tests/unit
python -m coverage report

# Native integration tests after compiling the required firmware
pytest tests/integration/ -n auto
```

The unit coverage command starts coverage before pytest imports any project module, disables unrelated third-party plugin autoload, and then explicitly loads the asyncio and BSWorld plugins required by the test suite. The configured report covers the `bsworld` package, including its runtime scheduler.
