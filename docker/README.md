# BSWorld UML Test Image

English | [简体中文](README.zh-CN.md)

`ghcr.io/xiaomi/bsworld-linux` packages BSWorld, BabbleSim, and the 32-bit UML test environment. For the programs included in the Guest root filesystem and the tests they run, see the [UML test environment guide](../tests/integration/uml/README.md).

## Use a Published Image

Install [Docker Engine](https://docs.docker.com/engine/install/ubuntu/) on a Linux/x86_64 host. From the repository root, run the integration tests with the [UML seccomp profile](uml-seccomp.json).

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

The default command runs the integration suites in parallel and fails if a selected test is skipped.

Find `pytest.xml`, logs, and captures under `.cache/docker-results`. For the directory layout, see [Test artifacts](../docs/artifacts.md).

### Select a Version

| Version | Image tag |
|---|---|
| Latest tested main | `latest` |
| Full commit ID | `sha-<40-character-commit>` |
| Release tag | `v<version>`, e.g. `v1.1.5` |

CI publishes `sha-<commit>` for each tested main commit. It updates `latest` when that commit is still the tip of main. A release adds its Git tag to the tested image for that commit.

Pull the tested image for the current checkout.

```sh
SOURCE_COMMIT="$(git rev-parse HEAD)"
sudo docker pull "ghcr.io/xiaomi/bsworld-linux:sha-$SOURCE_COMMIT"
```

## Run Tests

To test your checkout, mount it at `/workspace` and use the image for the same commit. Run the command from the repository root.

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

If you change the Guest executables, their build inputs, or the Dockerfile, [build a local image](#build) and set `TEST_IMAGE=bsworld-linux:dev`.

### Select a Suite

Select a suite by passing its directory to pytest. `--require-integration` fails the run if any selected integration test is skipped.

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

Check the code with Ruff.

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  ghcr.io/xiaomi/bsworld-linux:latest ruff check --no-cache .
```

## Build

Run Buildx Bake from the repository root. `BUILD_UID` and `BUILD_GID` set the image user's IDs. `BUILD_JOBS` controls build parallelism. For the build settings, see the [Bake configuration](docker-bake.hcl).

```sh
sudo BUILD_UID="$(id -u)" BUILD_GID="$(id -g)" BUILD_JOBS=4 \
  docker buildx bake -f docker/docker-bake.hcl --load
```

The build tags the image `bsworld-linux:dev`. Run its default integration suites with the following command.

```sh
mkdir -p .cache/docker-results
sudo chown "$(id -u):$(id -g)" .cache/docker-results
sudo docker run --rm \
  --security-opt seccomp=docker/uml-seccomp.json \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  bsworld-linux:dev
```

Set `BSWORLD_VERSION` to override the package version. Local builds default to `0+local`, and CI derives the version from Git.
