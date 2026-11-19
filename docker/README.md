# BSWorld UML Test Image

[English](README.md) | [简体中文](README.zh-CN.md)

`ghcr.io/xiaomi/bsworld-linux` includes BSWorld, BabbleSim, and the 32-bit UML Guest environment. The Guest root filesystem includes Ubuntu's i386 BlueZ and D-Bus and the Zephyr, NuttX and NimBLE Host applications. Use a Linux/x86_64 host with [Docker Engine](https://docs.docker.com/engine/install/ubuntu/). See the [UML integration guide](../tests/integration/uml/README.md).

## Use a Published Image

Run the image's integration tests from the repository root. Pass the [UML seccomp profile](uml-seccomp.json) when Docker runs a suite that boots UML Guests.

```sh
sudo docker pull ghcr.io/xiaomi/bsworld-linux:latest
mkdir -p .cache/docker-results
# The published images run as UID/GID 1000.
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  --security-opt seccomp=docker/uml-seccomp.json \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  ghcr.io/xiaomi/bsworld-linux:latest
```

The default command runs the integration suites in parallel and requires all selected tests to run. Results go to `.cache/docker-results`, with `pytest.xml` as the report. See [test artifacts](../docs/artifacts.md).

### Select a Version

Replace `latest` with a commit or Release tag to run another version:

| Version | Image tag |
|---|---|
| Latest tested main | `latest` |
| Full commit ID | `sha-<40-character-commit>` |
| Release tag | `v<version>`, e.g. `v1.1.5` |

CI publishes `sha-<commit>` and `latest` once the tests pass on main. A release tags its commit image with the Release tag.

Image for the current checkout:

```sh
SOURCE_COMMIT="$(git rev-parse HEAD)"
sudo docker pull "ghcr.io/xiaomi/bsworld-linux:sha-$SOURCE_COMMIT"
```

## Run Tests

Mount your checkout at `/workspace` from the repository root. Use the published image for the checkout's commit if available, or build an image with the matching Guest filesystem and firmware.

```sh
TEST_IMAGE="ghcr.io/xiaomi/bsworld-linux:sha-$(git rev-parse HEAD)"
mkdir -p .cache/docker-results
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  --security-opt seccomp=docker/uml-seccomp.json \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  "$TEST_IMAGE" pytest tests/integration \
    --require-integration -n auto -p no:cacheprovider \
    --out-dir /output --junitxml=/output/pytest.xml
```

After changing firmware, its build inputs or a Dockerfile, [build the image](#build) and set `TEST_IMAGE=bsworld-linux:dev`. The pull request's CI builds an image from the change.

### Select a Suite

Use the same image when running a single suite. `--require-integration` fails the run if a selected integration test is skipped.

```sh
sudo docker run --rm \
  --security-opt seccomp=docker/uml-seccomp.json \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  "$TEST_IMAGE" pytest tests/integration/uml/zephyr \
    --require-integration -n auto -p no:cacheprovider \
    --out-dir /output --junitxml=/output/zephyr-shell.xml
```

Run the unit tests.

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  ghcr.io/xiaomi/bsworld-linux:latest pytest tests/unit -q -p no:cacheprovider
```

Ruff:

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  ghcr.io/xiaomi/bsworld-linux:latest ruff check --no-cache .
```

## Build

Run the build from the repository root.

Build the UML image with [docker-bake.hcl](docker-bake.hcl).

```sh
sudo BUILD_UID="$(id -u)" BUILD_GID="$(id -g)" BUILD_JOBS=4 \
  docker buildx bake -f docker/docker-bake.hcl --load
```

The image is tagged `bsworld-linux:dev`. `BUILD_JOBS` sets the compile parallelism.

Run the local image:

```sh
mkdir -p .cache/docker-results
sudo chown "$(id -u):$(id -g)" .cache/docker-results
sudo docker run --rm \
  --security-opt seccomp=docker/uml-seccomp.json \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  bsworld-linux:dev
```

`BSWORLD_VERSION` defaults to `0+local`. CI sets it from Git. Use `bsworld-linux:dev` in place of the published image in the [test commands](#run-tests).
