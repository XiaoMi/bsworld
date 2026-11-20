# User Mode Linux Test Environment

[English](README.md) | [简体中文](README.zh-CN.md)

BSWorld runs 32-bit User Mode Linux (UML) Guests on 64-bit Linux. Each Guest Host connects to its own BSController and tests against a Bumble peer in pytest. Multiple Guests in a topology share the simulation clock.

## Prepare the Environment

### Docker

From the repository root, build the Linux test image and run the UML suites. The output mount keeps logs and captures in `.cache/docker-results`.

```sh
docker buildx bake -f docker/docker-bake.hcl --load linux
mkdir -p .cache/docker-results
sudo chown 1000:1000 .cache/docker-results
docker run --rm \
  --security-opt seccomp=docker/uml-seccomp.json \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  bsworld-linux:dev pytest tests/integration/uml --require-integration -n auto -q \
    --out-dir /output/uml --junitxml=/output/uml.xml
```

To run one Host, replace `tests/integration/uml` with its pytest path from the suite table below. To mount your checkout or select an image for a specific commit, follow the [Docker guide](../../../docker/README.md#run-tests).

### Local Linux

Set up BSWorld and BabbleSim by following the [local Linux guide](../../../README.md#local-linux). On Ubuntu 24.04, enable i386 packages and install the kernel build tools.

```sh
sudo dpkg --add-architecture i386
sudo apt-get update
sudo apt-get install -y bc bison flex

UML_DIR="$BSWORLD_ROOT/.cache/uml"
LINUX_DIR="$UML_DIR/linux-src"
mkdir -p "$UML_DIR"

export BSWORLD_UML_KERNEL="$LINUX_DIR/linux"
export BSWORLD_UML_ROOTFS="$UML_DIR/rootfs"
```

| Variable | Host path |
|---|---|
| `BSWORLD_UML_KERNEL` | Executable i386 UML kernel built with the test seed patch |
| `BSWORLD_UML_ROOTFS` | Guest root directory containing `/init`, the i386 programs and their libraries |

#### Build the Kernel

Fetch the Linux revision pinned in the [Dockerfile](../../../docker/linux.Dockerfile), apply the [UML test seed patch](../../../components/uml/random-seed.patch), and build the i386 UML kernel.

```sh
LINUX_REVISION=$(sed -n 's/^ARG LINUX_REVISION=//p' "$BSWORLD_ROOT/docker/linux.Dockerfile")
UML_SEED_PATCH="$BSWORLD_ROOT/components/uml/random-seed.patch"
(
  set -e
  git init "$LINUX_DIR"
  git -C "$LINUX_DIR" fetch --depth=1 https://github.com/torvalds/linux.git "${LINUX_REVISION:?}"
  git -C "$LINUX_DIR" checkout --detach FETCH_HEAD
  if ! git -C "$LINUX_DIR" apply --reverse --check "$UML_SEED_PATCH" 2>/dev/null; then
    git -C "$LINUX_DIR" apply "$UML_SEED_PATCH"
  fi

  cd "${LINUX_DIR:?}"
  make ARCH=um SUBARCH=i386 defconfig
  scripts/kconfig/merge_config.sh -m .config "$BSWORLD_ROOT/components/uml/linux.fragment"
  make ARCH=um SUBARCH=i386 olddefconfig
  make ARCH=um SUBARCH=i386 -j"$(nproc)"
)
```

#### Prepare the Guest Root Filesystem

Run the [rootfs script](../../../components/uml/rootfs.sh) as your local user to extract the Ubuntu i386 runtime packages. Install the UML startup script as `/init`.

```sh
sh "$BSWORLD_ROOT/components/uml/rootfs.sh" "${BSWORLD_UML_ROOTFS:?}" &&
install -m 0755 "$BSWORLD_ROOT/components/uml/init.sh" "$BSWORLD_UML_ROOTFS/init"
```

Use the Host guides below to prepare the programs for the suites you want to run. Their executable overrides take absolute paths **inside the Guest**. For example, a binary installed at `$BSWORLD_UML_ROOTFS/opt/bsworld/my-shell` is selected with `BSWORLD_UML_ZEPHYR_BIN=/opt/bsworld/my-shell`.

## Test Suites

| Host guide | Test entry point |
|---|---|
| [BlueZ](bluez/README.md) | `tests/integration/uml/bluez` |
| [Zephyr Bluetooth shell](zephyr/README.md) | `tests/integration/uml/zephyr` |
| [NuttX BTSak and NimBLE](nuttx/README.md) | `tests/integration/uml/nuttx` |
| [Apache NimBLE Linux](nimble/README.md) | `tests/integration/uml/nimble` |
| [TrouBLE](trouble/README.md) | `tests/integration/uml/trouble` |

After preparing all Hosts, run the full UML suite in the BSWorld virtual environment.

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/uml --require-integration -n auto -q
```

## Guest Runtime

The Guest boots from a read-only root filesystem. Its `/init` script mounts writable runtime directories and starts a shell connected to the UML process's standard input and output. See [init.sh](../../../components/uml/init.sh).

Each Guest reaches its Controller over H4 through `/dev/ttyS0`. The tests use `btattach -B /dev/ttyS0 -P h4` to register the Controller as `hci0`.

The pytest event loop schedules Guests through `time-travel=ext`. A Guest can advance its own clock within the current BabbleSim microsecond. The test clock holds its PHY turn until the Guest yields.

A periodic Guest tick handles input from the console and serial port. BabbleSim device IDs belong to Controllers, the test clock, and configured RF interference sources.

BSWorld derives each Guest's test seed from its label and passes it to the UML kernel in `UML_RANDOM_SEED` as an unsigned decimal integer.

## Adding Scenarios

Before `await topo.start()`, add one Controller for each Guest. Then call `open_uml_guest()` within an `AsyncExitStack`, with a distinct label for each Guest. The stack closes the Guests before pytest tears down the topology. See the helpers in [common.py](common.py).

Send commands with `guest.shell.run()` and check the console output with `guest.wait_log()`.

The reusable launcher and calendar are in [bsworld.uml](../../../src/bsworld/uml). Run their [unit tests](../../unit/uml) with `pytest tests/unit/uml -q`, which requires no UML kernel or BabbleSim build.

## License

The tests and BSWorld's UML adapter use Apache License 2.0. For the licenses of Linux and the Guest programs, see [Third-Party Notices](../../../THIRD_PARTY_NOTICES.md).
