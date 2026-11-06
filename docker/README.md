# BSWorld Docker Test Images

[English](README.md) | [简体中文](README.zh-CN.md)

Each platform has its own image with BSWorld, the tests, the Zephyr Controller and that platform's compiled firmware. The images need a Linux/x86_64 host with [Docker Engine](https://docs.docker.com/engine/install/ubuntu/). The [User Mode Linux tests](../tests/integration/uml/README.md) need a locally built kernel and are skipped in the images.

| Image | Firmware | Default suites |
|---|---|---|
| `ghcr.io/xiaomi/bsworld-zephyr` | Zephyr Bluetooth shells | `tests/integration/zephyr`, `tests/integration/bumble`, `tests/integration/babblesim` |
| `ghcr.io/xiaomi/bsworld-nuttx` | NuttX BTSak and NimBLE | `tests/integration/nuttx` |
| `ghcr.io/xiaomi/bsworld-trouble` | TrouBLE `ble_bas` | `tests/integration/trouble` |

Every image also includes Bumble, so any of them runs the Bumble suite, the unit tests and Ruff.

## Use a Published Image

Run an image's integration tests from any directory, here the Zephyr image:

```sh
sudo docker pull ghcr.io/xiaomi/bsworld-zephyr:latest
mkdir -p .cache/docker-results
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld-zephyr:latest
```

The default command runs the image's suites with `pytest -n auto`. Results go to `.cache/docker-results`, with `pytest.xml` as the report; see [artifacts](../docs/artifacts.md).

### Select a Version

Replace `latest` with a commit or Release tag to run another version:

| Version | Image tag |
|---|---|
| Latest tested main | `latest` |
| Full commit ID | `sha-<40-character-commit>` |
| Release tag | `v1.0.0` |

Each platform's CI publishes its own image: `sha-<commit>` and `latest` once its tests pass on main, and a Release tag from the release commit's image. A failing platform does not hold back the others, so `latest` can name different commits for different platforms, while one Release tag always names one commit. Releases before the per-platform images used a single image, `ghcr.io/xiaomi/bsworld`.

Image for the current checkout:

```sh
SOURCE_COMMIT="$(git rev-parse HEAD)"
sudo docker pull "ghcr.io/xiaomi/bsworld-zephyr:sha-$SOURCE_COMMIT"
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
  ghcr.io/xiaomi/bsworld-zephyr:latest
```

Firmware still comes from the image; firmware changes require [rebuilding it](#build).

### Select a Suite

Put a pytest command after the name of the image that contains the suite's firmware:

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld-zephyr:latest \
  pytest tests/integration/zephyr/shell -n auto \
    --out-dir /output --junitxml=/output/zephyr-shell.xml
```

Unit tests, in any image:

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld-zephyr:latest pytest tests/unit -q -p no:cacheprovider
```

Ruff:

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld-zephyr:latest ruff check --no-cache src tests
```

## Build

Clone the repository with its tags, then run the remaining commands from its root:

```sh
git clone https://github.com/XiaoMi/bsworld.git
cd bsworld
```

Build the tooling image and read the package version:

```sh
sudo docker build --progress=plain -f docker/base.Dockerfile \
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
sudo docker build --progress=plain -f docker/base.Dockerfile \
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

Build a platform image with [docker-bake.hcl](docker-bake.hcl), which links its Dockerfile to the stages it shares in [base.Dockerfile](base.Dockerfile). The targets are `zephyr`, `nuttx` and `trouble`:

```sh
sudo BUILD_UID="$(id -u)" BUILD_GID="$(id -g)" BUILD_JOBS=4 \
  BSWORLD_VERSION="$BSWORLD_VERSION" \
  docker buildx bake -f docker/docker-bake.hcl --load zephyr
```

The image is tagged `bsworld-zephyr:dev`. `BUILD_JOBS` sets the compile parallelism. Each image has its own Dockerfile:

| File | Stages |
|---|---|
| `base.Dockerfile` | `build-base`, `bsworld` (Python project dependencies), `python-test` (installed project and tests), `babblesim`, `zephyr-workspace` (Zephyr workspace and SDK), `zephyr-controller`, `runtime-base` (runtime libraries, BabbleSim and the Zephyr Controller) |
| `zephyr.Dockerfile` | `zephyr-shell`, `runtime-zephyr` |
| `nuttx.Dockerfile` | `nuttx`, `runtime-nuttx` |
| `trouble.Dockerfile` | `trouble`, `runtime-trouble` |

Run the local image:

```sh
mkdir -p .cache/docker-results
sudo chown "$(id -u):$(id -g)" .cache/docker-results
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  bsworld-zephyr:dev
```

Without Git metadata, pass `BSWORLD_VERSION` directly; it defaults to `0+local`. Use `bsworld-zephyr:dev` in place of the published image in the [test commands](#run-tests).
