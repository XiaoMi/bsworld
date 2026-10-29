# QEMU Linux Test Environment

[English](README.md) | [简体中文](README.zh-CN.md)

Runs a Buildroot AArch64 Linux guest with BlueZ and Apache NimBLE in a single-vCPU QEMU `virt` machine. The guest HCI UART connects to a Virtual Controller, and QEMU's `icount` clock is synchronized with BabbleSim.

## Validated Versions

| Component | Revision or version |
|---|---|
| BSWorld QEMU fork | `b2d1776bca209773c4a023e4decb785f7dc3cfb4` |
| Buildroot | `1ad0796f2b40eb8dadc43e95dd1d56708b191375` |
| Linux | `6.18.7` |
| BlueZ | `5.86` |
| Apache NimBLE | `1e8ed60276f35a80ed4d4b4f8bb9d9c6fee53845` |

## Prepare the Environment

The [published Docker image](../../../docker/README.md#run-tests) already includes QEMU, the kernel and rootfs; select the suites with `pytest tests/integration/qemu`. The steps below are for native Linux builds. Build BabbleSim using the [repository instructions](../../../README.md#build-and-test) and the [Controller](../../../docs/controllers.md#building-the-zephyr-controller) first.

Install the system dependencies (Ubuntu 24.04):

```sh
sudo apt-get update
sudo apt-get install -y \
  bash bc binutils bzip2 build-essential cpio debianutils diffutils file findutils \
  gawk gcc g++ git gzip libfdt-dev libffi-dev libglib2.0-dev libncurses-dev \
  libpixman-1-dev libssl-dev make ninja-build patch perl pkg-config \
  python3.12 python3.12-venv python3-pip rsync sed tar unzip wget xz-utils zip zlib1g-dev
```

Install the Python tools for the QEMU build:

```sh
source "$BSWORLD_ROOT/.venv/bin/activate"
python -m pip install wheel 'meson>=1.6.0' 'pycotap>=1.1.0'
```

Fetch the QEMU fork and Buildroot:

```sh
export QEMU_DIR="$HOME/qemu"
export BUILDROOT_DIR="$HOME/buildroot"
git clone https://github.com/LingaoM/qemu.git "$QEMU_DIR"
git clone https://gitlab.com/buildroot.org/buildroot.git "$BUILDROOT_DIR"
git -C "$QEMU_DIR" checkout b2d1776bca209773c4a023e4decb785f7dc3cfb4
git -C "$BUILDROOT_DIR" checkout 1ad0796f2b40eb8dadc43e95dd1d56708b191375
```

## Build QEMU and the Linux Guest

Build QEMU:

```sh
# Use your local QEMU and Buildroot workspace paths.
source "$BSWORLD_ROOT/.venv/bin/activate"
export QEMU_BUILD_DIR="$QEMU_DIR/build/bsworld"
export BUILDROOT_OUTPUT="$BUILDROOT_DIR/output-bsworld-linux"
mkdir -p "$QEMU_BUILD_DIR"
cd "$QEMU_BUILD_DIR"
"$QEMU_DIR/configure" \
  --target-list=aarch64-softmmu \
  --enable-bsim \
  --bsim-components-path="$BSIM_COMPONENTS_PATH" \
  --bsim-out-path="$BSIM_OUT_PATH"
ninja -C "$QEMU_BUILD_DIR" -j"$(nproc)" qemu-system-aarch64
```

Build the kernel and rootfs as a normal user:

```sh
make -C "$BUILDROOT_DIR" \
  O="$BUILDROOT_OUTPUT" \
  BR2_EXTERNAL="$BSWORLD_ROOT/tests/integration/qemu/source" \
  bsworld_qemu_linux_defconfig
make -C "$BUILDROOT_DIR" O="$BUILDROOT_OUTPUT" -j"$(nproc)"
```

Export the artifacts, shared by BlueZ and NimBLE:

```sh
export BSWORLD_QEMU_BIN="$QEMU_BUILD_DIR/qemu-system-aarch64"
export BSWORLD_QEMU_KERNEL="$BUILDROOT_OUTPUT/images/Image"
export BSWORLD_QEMU_ROOTFS="$BUILDROOT_OUTPUT/images/rootfs.cpio"
```

Check the outputs:

```sh
test -x "$BSWORLD_QEMU_BIN"
test -f "$BSWORLD_QEMU_KERNEL"
test -f "$BSWORLD_QEMU_ROOTFS"
test -x "$BUILDROOT_OUTPUT/target/usr/bin/btattach"
test -x "$BUILDROOT_OUTPUT/target/usr/bin/nimble-linux"
```

## Runtime Data Path

QEMU exposes the Virtual Controller socket as the guest's `/dev/ttyS0`, and the test registers it as `hci0` with `btattach -B /dev/ttyS0 -P h4 -S 1000000 -N`. BlueZ uses `hci0` through `bluetoothd`; NimBLE stops `bluetoothd` and claims `hci0` through HCI User Channel.

`-icount` carries the BabbleSim identity, and `bsim-mro=1000` limits each execution interval to 1,000 µs. QEMU record/replay cannot be used together with BabbleSim synchronization.

## Run the Tests

See [BlueZ](bluez/README.md) and [NimBLE](nimble/README.md).

## Rebuild and Inspect the Configuration

```sh
make -C "$BUILDROOT_DIR" O="$BUILDROOT_OUTPUT" show-info
```

After changing the defconfig, Linux fragment or toolchain, rebuild into a new `BUILDROOT_OUTPUT` and update the exports.

## Adding Scenarios

Add an async pytest test under [BlueZ](bluez/README.md) or [NimBLE](nimble/README.md). Call `load_qemu_guest()` from [common.py](common.py) before `topo.start()` and `login_qemu_guest()` after it, then send guest commands through `guest.shell` and verify them with `wait_log()`.

## Licenses

The external configuration and tests in this repository use Apache License 2.0. QEMU, Linux and guest software retain their own licenses; see [Third-Party Notices](../../../THIRD_PARTY_NOTICES.md). Generate the license information for a build with:

```sh
make -C "$BUILDROOT_DIR" O="$BUILDROOT_OUTPUT" legal-info
```
