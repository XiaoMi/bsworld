# NuttX Test Environment

[English](README.md) | [简体中文](README.zh-CN.md)

These tests run NuttX simulator firmware as native Linux processes, in two suites: [BTSak](btsak/README.md) and [NimBLE](nimble/README.md). Each process connects to its own BSController.

## Prepare the Environment

Prepare BSWorld using the [local Linux setup](../../../README.md#local-linux) and build BabbleSim using the [repository instructions](../../../README.md#build-and-test), then install the dependencies from the NuttX [installation guide](https://nuttx.apache.org/docs/latest/quickstart/install.html). On Ubuntu 24.04:

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

Clone NuttX and its Applications at the revisions pinned by [nuttx.Dockerfile](../../../docker/nuttx.Dockerfile):

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

## Test Suites

| Suite | Build instructions | Executable variable |
|---|---|---|
| BTSak | [BTSak tests](btsak/README.md#build-and-run) | `BSWORLD_NUTTX_BTSAK_BIN` |
| NimBLE | [NimBLE tests](nimble/README.md#build-and-run) | `BSWORLD_NUTTX_NIMBLE_BIN` |

Run both suites:

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/nuttx -n auto -q
```

## Adding Scenarios

Add an async pytest test under [BTSak](btsak/README.md) or [NimBLE](nimble/README.md), reusing that suite's device profiles with `create_nuttx_process()` from [common/](common/). Register processes before `topo.start()`, then send the role commands documented in the suite README and verify their output with `wait_log()`. A profile describes the program name, executable and shell behavior, and creating the process registers it with the topology. The test adds the Controller with `topo.add_controller()` and binds the program to it with `attach()`.

## License

- Test code: Apache License 2.0.
- NuttX and NuttX Apps: Apache License 2.0.
- Apache NimBLE, used by the NimBLE suite: Apache License 2.0.

See [Third-Party Notices](../../../THIRD_PARTY_NOTICES.md).
