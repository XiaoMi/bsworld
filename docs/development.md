# Development Checks

[English](development.md) | [简体中文](zh-CH/development.md)

Prepare the [local Linux environment](../README.md#local-linux) and run the development checks from the repository root. For Docker, see how to select an image under [Verifying Changes](#verifying-changes).

## Unit Tests

Activate the local virtual environment and run the unit suite.

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/unit -q
```

To check a mounted checkout in Docker, run pytest in the test image.

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  ghcr.io/xiaomi/bsworld-linux:latest pytest tests/unit -q -p no:cacheprovider
```

The unit suite exercises the framework in `src/bsworld/`, including topology and Controller lifecycle, RF configuration, simulation-time scheduling, FIFO clock communication, UML Guest lifecycle and time-travel, and log collection.

## Code Style

Both Python packages pin Ruff 0.14.10. Use it to check the Python source, examples, and tests maintained in this repository. Ruff sorts imports and formats Python with an 88-character target line width, four-space indentation, and the existing quote style.

`E501` checks long lines, including comments and strings. Ruff allows unbreakable words and trailing URLs. Two public Bluetooth error identifiers use inline `# noqa: E501` to preserve their API names. Wrap explanatory prose by hand and keep exemptions scoped to individual lines.

Run Ruff from the repository root in the development environment.

```sh
ruff check .
ruff format --check .
```

To sort imports and apply formatting locally, run Ruff in fix mode and then format the code.

```sh
ruff check --select I --fix .
ruff format .
```

CI runs both Ruff checks and the unit suite in `Checks` for pull requests to `main` and pushes to `main`.

## Coverage

`pyproject.toml` configures branch coverage for the `bsworld` package, including the runtime scheduler. Run the unit suite under coverage in the local environment.

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
python -m coverage run -m pytest \
  -p pytest_asyncio.plugin -p bsworld.plugin tests/unit
python -m coverage report
```

## Verifying Changes

To verify end-to-end Bluetooth behavior locally, build the required firmware and run the integration suite.

```sh
pytest tests/integration/ -n auto
```

When you mount a checkout at `/workspace`, the container runs its tests using the executables from the image. Pass the [UML seccomp profile](../docker/uml-seccomp.json) for UML integration tests in Docker.

| Change | Verify with |
|---|---|
| Python framework, tests or scenarios | The image for the checkout's commit, with the checkout mounted. See [Run Tests](../docker/README.md#run-tests). |
| Firmware, its build inputs, or the Dockerfile | [Build the image](../docker/README.md#build) from the checkout or use the pull request's CI build. |

If a local result differs from CI, reproduce it in the image for the same commit, `sha-<commit>`. Compare the compiler and Python versions and check the i386 BlueZ and D-Bus package versions in the Guest root filesystem.
