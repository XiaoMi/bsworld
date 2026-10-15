# BSWorld Docker Test Image

[English](README.md) | [中文](README.zh-CN.md)

We recommend pulling `ghcr.io/xiaomi/bsworld:latest` to run tests on Linux/x86_64. You can also build your own image with the [Dockerfile](Dockerfile).

## Image Contents

The [Dockerfile](Dockerfile) prepares dependencies and builds artifacts in BSWorld, BabbleSim, Zephyr, NuttX, QEMU, and Buildroot order, then creates an independent `runtime` image.

The final image includes Python 3.12, BSWorld, pytest, tests, BabbleSim runtime components and the Ellisys converter, the Virtual Controller, eight Zephyr samples, two NuttX BTSak/NimBLE executables, and QEMU with the Linux kernel and guest rootfs. Image environment variables select the firmware paths.

The final image contains the runtime programs and libraries. Upstream source, SDKs, toolchains, and intermediate files remain in Docker build caches.

## Prerequisites

Use a Linux/x86_64 host with Docker Engine. On Ubuntu, follow Docker's [installation instructions](https://docs.docker.com/engine/install/ubuntu/), then verify the installation:

```sh
sudo docker info
```

Testing the current checkout requires Git. Building an image also requires Docker's Buildx plugin.

## Use a Published Image

Pull the image and run its packaged integration tests from any directory:

```sh
sudo docker pull ghcr.io/xiaomi/bsworld:latest
mkdir -p .cache/docker-results
# The published image runs as UID/GID 1000.
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest
```

`latest` selects the most recently tested environment published from `main`. To use a specific environment, replace it with the `env-<hash>` tag shown in that CI run's summary.

The image includes Python, BSWorld, tests, and compiled firmware. The default command is `pytest tests/integration -n 2 --out-dir /output --junitxml=/output/pytest.xml`.

## Run Tests

### Test the Current Checkout

Clone the repository if needed. Run these commands from the repository root to test your Python source and tests with the image's firmware:

```sh
git clone https://github.com/XiaoMi/bsworld.git
cd bsworld
mkdir -p .cache/docker-results
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest
```

Mounting `/workspace` uses your checkout's Python source and tests. Firmware remains in `/opt`; changes to firmware source or build configuration require rebuilding the image.

### Select a Suite

From the repository root, pass a pytest command after the image name. For example:

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest \
  pytest tests/integration/zephyr/samples/heart_rate -n 2 \
    --out-dir /output --junitxml=/output/heart-rate.xml
```

### Unit Tests and Lint

Check the current checkout:

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest pytest tests/unit -q -p no:cacheprovider
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest ruff check --no-cache src tests
```

## Build

Clone the repository if needed, then run from its root:

```sh
git clone https://github.com/XiaoMi/bsworld.git
cd bsworld
sudo docker buildx version
sudo docker build --progress=plain -f docker/Dockerfile \
  --build-arg BUILD_UID="$(id -u)" \
  --build-arg BUILD_GID="$(id -g)" \
  --build-arg BUILD_JOBS=4 \
  -t bsworld:dev .

mkdir -p .cache/docker-results
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  bsworld:dev
```

| Argument | Purpose | Default |
|---|---|---|
| `BUILD_UID` / `BUILD_GID` | UID/GID of the image's `bsworld` user; match the current user when building locally | `1000` / `1000` |
| `BUILD_JOBS` | Firmware compilation parallelism | `4` |

Source revisions and SDK options are configured in the Dockerfile. The first build downloads source/SDKs and compiles all firmware; later builds reuse Docker layers. Image construction checks binaries and their runtime library dependencies.

To use this image with the [test commands](#run-tests), replace `ghcr.io/xiaomi/bsworld:latest` with `bsworld:dev`. The local build uses your UID/GID, so keep the output directory owned by your user.

## Files and Logs

| Container path | Contents |
|---|---|
| `/workspace` | BSWorld Python source and tests; mount the commit under test here |
| `/opt/firmware` | Controller, Zephyr, NuttX, QEMU, kernel, and rootfs |
| `/opt/babblesim` | BabbleSim programs, dynamic models, and trace converter |
| `/opt/venv` | BSWorld Python environment |
| `/output` | JUnit reports and per-test logs/captures |

The commands above mount `/output` at the host's `.cache/docker-results`. This directory must be writable by the image's `bsworld` user. Local builds match UID/GID through the build arguments; the published-image example prepares the directory for UID/GID 1000. The CI workflow prepares its result-directory permissions automatically.

Pytest terminal output appears in the caller's terminal or CI step logs. The default command passes pytest's exit code through `docker run`, allowing CI to determine success or failure. The workflow uploads its result directory after testing, including on test failure.

### Shell

From the repository root, open a shell with the current checkout mounted:

```sh
sudo docker run --rm -it \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest bash
```

For native Linux builds, see the [repository README](../README.md#local-linux). See [Logs and captures](../docs/artifacts.md) for the output layout and [Third-Party Notices](../THIRD_PARTY_NOTICES.md) for component notices.
