# Development Checks

[English](development.md) | [简体中文](zh-CH/development.md)

Develop in the README's [local Linux setup](../README.md#local-linux), and run BSWorld's unit tests, lint checks and coverage there from the repository root. A [published Docker image](../docker/README.md#run-tests) runs the same checks on your checkout without a local setup, and [Verifying Changes](#verifying-changes) describes what it can verify.

## Unit Tests

With the local virtual environment:

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/unit -q
```

With Docker:

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  ghcr.io/xiaomi/bsworld-linux:latest pytest tests/unit -q -p no:cacheprovider
```

The unit suite covers topology and Controller lifecycle, RF configuration, simulation-time scheduling, FIFO clock communication, UML Guest lifecycle and time-travel, and log collection. End-to-end Bluetooth coverage is in `tests/integration/`.

## Code Style

The rules apply to all Python source, examples, and tests maintained in this repository. Dependency checkouts and generated build artifacts are excluded.

| Language | Formatter | Rules |
|---|---|---|
| Python | Ruff 0.14.10 | 88-character target line width, four-space indentation, sorted imports, preserved quote style |

Python uses Ruff for both import sorting and formatting. Both Python packages pin the same Ruff version. `E501` checks long lines that the formatter cannot wrap, including comments and strings. Two public Bluetooth error identifiers require local `# noqa: E501` exceptions to retain their existing API names; Ruff also permits unbreakable words and trailing URLs. Wrap explanatory prose rather than adding blanket exceptions.

From the repository root, use the Python development environment to check all maintained Python code:

```sh
ruff check .
ruff format --check .
```

To apply formatting locally:

```sh
ruff check --select I --fix .
ruff format .
```

The `Checks` GitHub Actions workflow, which `CI` runs for every pull request and push to main, runs these checks and the unit tests, and fails when formatting, lint, or a unit test fails. It checks all maintained Python files in the checkout, including existing files.

## Coverage

Branch coverage is enabled in `pyproject.toml`. Run the unit suite under coverage in the local environment:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
python -m coverage run -m pytest \
  -p pytest_asyncio.plugin -p bsworld.plugin tests/unit
python -m coverage report
```

After building the required firmware, run the integration suite separately:

```sh
pytest tests/integration/ -n auto
```

The coverage report includes the complete `bsworld` Python package, including the runtime scheduler.

## Verifying Changes

A published image runs the checkout mounted at `/workspace` with the executables it was built with. Pass the [UML seccomp profile](../docker/uml-seccomp.json) when running the UML integration suites in Docker.

| Change | Verify with |
|---|---|
| Python framework, tests or scenarios | The image for the checkout's commit, with the checkout mounted. See [Run Tests](../docker/README.md#run-tests). |
| Firmware or its build inputs, such as the Zephyr shell configuration or the UML kernel fragment, or a Dockerfile | An image [built](../docker/README.md#build) from the checkout, or the pull request's CI, which builds the images from it |

The published images are the environment CI tested. Native results can differ from them when local tools or packages differ, for example the compiler, Python, or the Guest root filesystem's BlueZ and D-Bus packages. When a local result differs from CI, first reproduce it in the image of the same commit, `sha-<commit>`, then compare the local versions with the image's, for example `gcc --version`, or the version of the BlueZ package in the Guest root filesystem.
