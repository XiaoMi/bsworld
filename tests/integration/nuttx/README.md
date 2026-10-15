# NuttX Test Environment

[English](README.md) | [中文](README.zh-CN.md)

## Prepare the Environment

We recommend the [published Docker image](../../../docker/README.md#run-tests), which includes BTSak and NimBLE firmware. Select the NuttX suite with `pytest tests/integration/nuttx`.

The steps below are for native Linux builds. Install BSWorld and set `BSWORLD_ROOT` using the repository README's [local Linux setup](../../../README.md#local-linux). `NUTTX_DIR` and `NUTTX_APPS_DIR` serve BTSak; `NUTTX_NIMBLE_DIR` and `NUTTX_NIMBLE_APPS_DIR` serve NimBLE. Each configuration uses separate source trees because Make produces an in-tree executable named `nuttx`, and apps contains configuration-dependent build outputs.

For native Linux builds, follow the Apache NuttX official [installation guide](https://nuttx.apache.org/docs/latest/quickstart/install.html). Ubuntu 24.04 can use the following dependencies. Simulator configurations use host GCC/multilib rather than an ARM cross toolchain:

```sh
sudo apt-get update
sudo apt-get install -y \
  autoconf automake bison flex gettext texinfo gperf libtool pkg-config \
  build-essential git libncurses-dev xxd genromfs kconfig-frontends \
  libgmp-dev libmpc-dev libmpfr-dev libisl-dev binutils-dev libelf-dev \
  libexpat1-dev gcc-multilib g++-multilib lib32z1-dev picocom u-boot-tools util-linux
source "$BSWORLD_ROOT/.venv/bin/activate"
python -m pip install kconfiglib
```

The `sim:nimble` configuration builds a 32-bit executable and enables FAT filesystem support, which links against host zlib. Install `lib32z1-dev` to provide the 32-bit zlib library; the 64-bit `zlib1g-dev` library alone cannot satisfy this link.

Then prepare the source workspaces. These revisions match the validated Dockerfile pins.

```sh
export NUTTX_DIR="$HOME/nuttx"
export NUTTX_APPS_DIR="$HOME/nuttx-apps"
export NUTTX_NIMBLE_DIR="$HOME/nuttx-nimble"
export NUTTX_NIMBLE_APPS_DIR="$HOME/nuttx-nimble-apps"

git clone https://github.com/apache/nuttx.git "$NUTTX_DIR"
git clone https://github.com/apache/nuttx-apps.git "$NUTTX_APPS_DIR"
git -C "$NUTTX_DIR" checkout 89c4b8ccaf03f1c6d33624d93ef640d8d176a78d
git -C "$NUTTX_APPS_DIR" checkout 8404450fb89346ed244d1c701e5feb5d19f32271

# Separate source trees retain both in-tree simulator executables.
git clone "$NUTTX_DIR" "$NUTTX_NIMBLE_DIR"
git clone "$NUTTX_APPS_DIR" "$NUTTX_NIMBLE_APPS_DIR"
```

## Compile and Run

Build BabbleSim from [README build instructions](../../../README.md#build-and-test), retaining `BSWORLD_ROOT` and `BSIM_*`, then compile the [Virtual Controller](../../../components/bt_virt_controller/README.md).

| Suite | Build instructions | Test artifact variable |
|---|---|---|
| BTSak | [source/README](btsak/source/README.md) | `BSWORLD_NUTTX_BTSAK_BIN` |
| NimBLE | [source/README](nimble/source/README.md) | `BSWORLD_NUTTX_NIMBLE_BIN` |

Both configurations enable `CONFIG_SIM_BSIM_TIME=y` to join BabbleSim discrete time.
