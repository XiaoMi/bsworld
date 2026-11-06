# BSWorld Docker Test Image

[English](README.md) | [简体中文](README.zh-CN.md)

The image contains BSWorld, the tests and the compiled firmware. It needs a Linux/x86_64 host with [Docker Engine](https://docs.docker.com/engine/install/ubuntu/). The [User Mode Linux tests](../tests/integration/uml/README.md) need a locally built kernel and are skipped in the image.

## Use a Published Image

Run the packaged integration tests from any directory:

```sh
sudo docker pull ghcr.io/xiaomi/bsworld:latest
mkdir -p .cache/docker-results
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest
```

The default command is `pytest tests/integration -n 2`. Results go to `.cache/docker-results`, with `pytest.xml` as the report; see [artifacts](../docs/artifacts.md).

### Select a Version

Replace `latest` with a commit or Release tag to run another version:

| Version | Image reference |
|---|---|
| Latest tested main | `ghcr.io/xiaomi/bsworld:latest` |
| Full commit ID | `ghcr.io/xiaomi/bsworld:sha-<40-character-commit>` |
| Release tag | `ghcr.io/xiaomi/bsworld:v1.0.0` |

Image for the current checkout:

```sh
SOURCE_COMMIT="$(git rev-parse HEAD)"
sudo docker pull "ghcr.io/xiaomi/bsworld:sha-$SOURCE_COMMIT"
```

## Run Tests

To test your checkout, mount it at `/workspace` from the repository root:

```sh
mkdir -p .cache/docker-results
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest
```

Firmware still comes from the image; firmware changes require [rebuilding it](#build).

### Select a Suite

Put a pytest command after the image name to select a suite:

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest \
  pytest tests/integration/zephyr/shell -n 2 \
    --out-dir /output --junitxml=/output/zephyr-shell.xml
```

Unit tests:

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest pytest tests/unit -q -p no:cacheprovider
```

Ruff:

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest ruff check --no-cache src tests
```

## Build

Clone the repository with its tags, then run the remaining commands from its root:

```sh
git clone https://github.com/XiaoMi/bsworld.git
cd bsworld
```

Build the tooling image and read the package version:

```sh
sudo docker build --progress=plain -f docker/Dockerfile \
  --target bsworld -t bsworld:tooling \
  --build-arg BUILD_UID="$(id -u)" \
  --build-arg BUILD_GID="$(id -g)" .
BSWORLD_VERSION="$(sudo docker run --rm \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 --env GIT_OPTIONAL_LOCKS=0 \
  bsworld:tooling python -m setuptools_scm)"
```

Build `python-test` and run lint and unit tests:

```sh
sudo docker build --progress=plain -f docker/Dockerfile \
  --target python-test \
  --build-arg BUILD_UID="$(id -u)" \
  --build-arg BUILD_GID="$(id -g)" \
  --build-arg BSWORLD_VERSION="$BSWORLD_VERSION" \
  -t bsworld:python-test .
sudo docker run --rm --env PYTHONDONTWRITEBYTECODE=1 \
  bsworld:python-test python -m ruff check --no-cache src tests
sudo docker run --rm --env PYTHONDONTWRITEBYTECODE=1 \
  bsworld:python-test python -m pytest tests/unit -q -p no:cacheprovider
```

Build the runtime image:

```sh
sudo docker build --progress=plain -f docker/Dockerfile \
  --target runtime \
  --build-arg BUILD_UID="$(id -u)" \
  --build-arg BUILD_GID="$(id -g)" \
  --build-arg BUILD_JOBS=4 \
  --build-arg BSWORLD_VERSION="$BSWORLD_VERSION" \
  -t bsworld:dev .
```

`BUILD_JOBS` sets the compile parallelism. Build stages:

```text
build-base
├── bsworld (Python project dependencies)
│   └── python-test (installed project and tests)
│       └── runtime (+ runtime libraries and firmware from the branches below)
├── babblesim
│   ├── zephyr
│   ├── nuttx
│   └── trouble
```

Run the local image:

```sh
mkdir -p .cache/docker-results
sudo chown "$(id -u):$(id -g)" .cache/docker-results
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  bsworld:dev
```

Without Git metadata, pass `BSWORLD_VERSION` directly; it defaults to `0+local`. Use `bsworld:dev` in place of the published image in the [test commands](#run-tests).
