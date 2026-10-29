# NuttX Test Environment

[English](README.md) | [简体中文](README.zh-CN.md)

## Prepare the Environment

We recommend the [published Docker image](../../../docker/README.md#run-tests), which includes BTSak and NimBLE firmware. Select the NuttX suite with `pytest tests/integration/nuttx`.

The steps below are for native Linux builds. Prepare BSWorld using the [local Linux setup](../../../README.md#local-linux), then install the dependencies from the NuttX [installation guide](https://nuttx.apache.org/docs/latest/quickstart/install.html). On Ubuntu 24.04:

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

Set the workspace paths. BTSak and NimBLE each use their own NuttX and Applications trees:

```sh
export NUTTX_DIR="$HOME/nuttx"
export NUTTX_APPS_DIR="$HOME/nuttx-apps"
export NUTTX_NIMBLE_DIR="$HOME/nuttx-nimble"
export NUTTX_NIMBLE_APPS_DIR="$HOME/nuttx-nimble-apps"
```

Clone NuttX and its Applications at the revisions pinned by the Dockerfile:

```sh
git clone https://github.com/apache/nuttx.git "$NUTTX_DIR"
git clone https://github.com/apache/nuttx-apps.git "$NUTTX_APPS_DIR"
git -C "$NUTTX_DIR" checkout 47180bf5ea00a94b41cf2083b8fed123199e5ce9
git -C "$NUTTX_APPS_DIR" checkout 95833033f9f5c045309c1c48d2af994c78791f6b
```

Copy both trees for NimBLE:

```sh
git clone "$NUTTX_DIR" "$NUTTX_NIMBLE_DIR"
git clone "$NUTTX_APPS_DIR" "$NUTTX_NIMBLE_APPS_DIR"
```

## Compile and Run

Build BabbleSim using the [repository instructions](../../../README.md#build-and-test) and the [Virtual Controller](../../../docs/controllers.md#building-the-zephyr-controller).

| Suite | Build instructions | Test artifact variable |
|---|---|---|
| BTSak | [source/README](btsak/source/README.md) | `BSWORLD_NUTTX_BTSAK_BIN` |
| NimBLE | [source/README](nimble/source/README.md) | `BSWORLD_NUTTX_NIMBLE_BIN` |

## Adding Scenarios

Add an async pytest test under [BTSak](btsak/README.md) or [NimBLE](nimble/README.md), reusing that suite's device profiles with `load_nuttx_profile()` from [common/](common/). Register processes before `topo.start()`, then send the role commands documented in the suite README and verify their output with `wait_log()`.
