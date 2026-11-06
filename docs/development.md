# Development Checks

[English](development.md) | [简体中文](zh-CH/development.md)

Run BSWorld's unit tests, lint checks, and coverage from the repository root. A [published Docker image](../docker/README.md#run-tests) is the shortest setup; use the README's [local Linux setup](../README.md#local-linux) when you need a native Python environment.

## Unit Tests

With Docker:

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld-zephyr:latest pytest tests/unit -q -p no:cacheprovider
```

With the local virtual environment:

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/unit -q
```

The unit suite covers environment and artifact resolution, topology and Controller lifecycle, BabbleSim command generation, RF configuration, simulation-time scheduling, FIFO clock communication, and log collection. End-to-end Bluetooth behavior is covered by `tests/integration/`.

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
