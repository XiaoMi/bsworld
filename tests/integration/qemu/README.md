# QEMU Linux Test Environment

[English](README.md) | [中文](README.zh-CN.md)

BSWorld can run a complete Linux guest as one participant in the BabbleSim time domain. The validated setup uses a single-vCPU AArch64 QEMU `virt` machine and a Buildroot Linux kernel and initramfs containing BlueZ, Apache NimBLE, and TrouBLE. QEMU connects the guest's HCI UART to a BSWorld Virtual Controller through a Host Unix socket, while its instruction-counting clock is synchronized with BabbleSim.

This path is intended for Linux Bluetooth software that should be tested without porting the Application or Host stack to an RTOS. Buildroot is used only to build the guest kernel, root filesystem, and cross toolchain.

## Validated Versions

| Component | Revision or version | Purpose |
|---|---|---|
| BSWorld QEMU fork | `b2d1776bca209773c4a023e4decb785f7dc3cfb4` | Synchronizes TCG `icount` with BabbleSim and orders vCPU execution and Main Loop I/O deterministically |
| Buildroot | `1ad0796f2b40eb8dadc43e95dd1d56708b191375` | Builds the AArch64 Linux guest |
| Linux | `6.18.7` | Guest kernel selected by the maintained defconfig |
| BlueZ | `5.86` | Guest Bluetooth Host stack and command-line tools |
| Apache NimBLE | `1e8ed60276f35a80ed4d4b4f8bb9d9c6fee53845` | Linux Host example inside the Guest |
| TrouBLE | `730198ecf94ee6953163b7485cf902d5f9d45dbd` | Rust Linux Host example inside the Guest |

The Buildroot and package versions are pinned by the validated Buildroot revision and the BSWorld defconfig. Use these revisions when reproducing the current tests; moving either repository independently may change build options, package versions, or runtime behavior.

## Prepare the Environment

We recommend the [published Docker image](../../../docker/README.md#run-tests), which includes QEMU, the kernel, and rootfs. Select the QEMU suites with `pytest tests/integration/qemu`.

The steps below are for native Linux builds. First build BabbleSim from [README build instructions](../../../README.md#build-and-test) and the [Controller](../../../components/bt_virt_controller/README.md), retaining `BSWORLD_ROOT`, `BSIM_OUT_PATH`, and `BSIM_COMPONENTS_PATH`.

Native Linux builds can follow the official [QEMU build documentation](https://www.qemu.org/docs/master/devel/build-system.html) and [Buildroot manual](https://buildroot.org/downloads/manual/manual.html). Use the BabbleSim-aware QEMU fork. Example environment preparation:

```sh
sudo apt-get update
sudo apt-get install -y \
  bash bc binutils bzip2 build-essential cpio debianutils diffutils file findutils \
  gawk gcc g++ git gzip libfdt-dev libffi-dev libglib2.0-dev libncurses-dev \
  libpixman-1-dev libssl-dev make ninja-build patch perl pkg-config \
  python3.12 python3.12-venv python3-pip rsync sed tar unzip wget xz-utils zip zlib1g-dev
source "$BSWORLD_ROOT/.venv/bin/activate"
python -m pip install wheel 'meson>=1.6.0' 'pycotap>=1.1.0'

export QEMU_DIR="$HOME/qemu"
export BUILDROOT_DIR="$HOME/buildroot"
git clone https://github.com/LingaoM/qemu.git "$QEMU_DIR"
git clone https://gitlab.com/buildroot.org/buildroot.git "$BUILDROOT_DIR"
git -C "$QEMU_DIR" checkout b2d1776bca209773c4a023e4decb785f7dc3cfb4
git -C "$BUILDROOT_DIR" checkout 1ad0796f2b40eb8dadc43e95dd1d56708b191375
```

## Build QEMU and the Linux Guest

Invoke QEMU configure/ninja and Buildroot make directly. Run Buildroot as a normal user.

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

make -C "$BUILDROOT_DIR" \
  O="$BUILDROOT_OUTPUT" \
  BR2_EXTERNAL="$BSWORLD_ROOT/tests/integration/qemu/source" \
  bsworld_qemu_linux_defconfig
make -C "$BUILDROOT_DIR" O="$BUILDROOT_OUTPUT" -j"$(nproc)"

export BSWORLD_QEMU_BIN="$QEMU_BUILD_DIR/qemu-system-aarch64"
export BSWORLD_QEMU_KERNEL="$BUILDROOT_OUTPUT/images/Image"
export BSWORLD_QEMU_ROOTFS="$BUILDROOT_OUTPUT/images/rootfs.cpio"
```

BlueZ, NimBLE, and TrouBLE share this QEMU, kernel, and rootfs.

The fork's `--enable-bsim` links the BabbleSim communication static libraries into QEMU. The Buildroot external tree at `tests/integration/qemu/source` maintains the guest defconfig, Linux fragment, and NimBLE/TrouBLE package inputs. The first build downloads and compiles the cross toolchain, Linux, and guest packages, so it takes substantially longer than incremental builds.

The rootfs is an initramfs expanded afresh at each guest boot. Inspect the original outputs before testing:

```sh
test -x "$BSWORLD_QEMU_BIN"
test -f "$BSWORLD_QEMU_KERNEL"
test -f "$BSWORLD_QEMU_ROOTFS"
test -x "$BUILDROOT_OUTPUT/target/usr/bin/btattach"
test -x "$BUILDROOT_OUTPUT/target/usr/bin/nimble-linux"
test -x "$BUILDROOT_OUTPUT/target/usr/bin/trouble-ble-bas-peripheral"
```

## Runtime Data Path

At runtime, BSWorld creates the Virtual Controller's Unix socket and starts QEMU with a socket-backed character device. QEMU exposes that character device as the guest's first PCI serial port, `/dev/ttyS0`. The test runs `btattach -B /dev/ttyS0 -P h4 -S 1000000 -N`, which registers `hci0` with the guest Linux Bluetooth subsystem. BlueZ tests control `hci0` through `bluetoothd`, `bluetoothctl`, and the standard kernel API. NimBLE and TrouBLE tests stop `bluetoothd` and claim the same `hci0` through HCI User Channel; TrouBLE first runs `hciconfig hci0 down` because its Linux transport requires the caller to take the device down.

The QEMU command supplies the BabbleSim simulation identity through `-icount`. `bsim-mro=1000` limits each virtual execution interval to 1,000 microseconds, while an earlier timer deadline shortens that interval. At each boundary, QEMU submits the target time to the PHY and holds the vCPU until the PHY acknowledges it. Guest virtual time, timer interrupts, and sleeps therefore follow the same BabbleSim timeline as the Virtual Controller, PHY, pytest, and every other simulated device.

The fork uses a fair execution-order lock shared by the vCPU thread and QEMU Main Loop so Host thread scheduling cannot change the order between Guest execution and Host-side I/O dispatch. This primitive is extracted from QEMU's record/replay lock and preserves the existing record/replay interfaces; BabbleSim synchronization and record/replay are mutually exclusive.

While the vCPU waits for the PHY acknowledgment, it releases the execution-order lock and the Main Loop continues processing Host I/O with blocking polls. After acknowledging the wait, the PHY blocks for this device's next request, so the simulation cannot advance again. QEMU then polls without blocking until no locally queued fd event remains, commits the target virtual time, and allows the vCPU to resume.

## Run the Tests

See the [BlueZ tests](bluez/README.md), [NimBLE Linux test](nimble/README.md), and [TrouBLE Linux test](trouble/README.md) for pytest commands, artifact environment variables, validated behavior, and per-test logs.

## Rebuild and Inspect the Configuration

Inspect the current Buildroot configuration:

```sh
make -C "$BUILDROOT_DIR" O="$BUILDROOT_OUTPUT" show-info
```

After changing the defconfig or Linux fragment, choose a fresh `BUILDROOT_OUTPUT`, rerun the defconfig and make commands above, and update the kernel/rootfs exports.

## Licenses

The external configuration and tests in this repository use Apache License 2.0. QEMU, Linux, and guest software retain their own licenses. For distribution, generate Buildroot's `legal-info` from the actual build output; see [Third-Party Notices](../../../THIRD_PARTY_NOTICES.md).

```sh
make -C "$BUILDROOT_DIR" O="$BUILDROOT_OUTPUT" legal-info
```
