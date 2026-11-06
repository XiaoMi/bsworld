# Zephyr Test Environment

[English](README.md) | [简体中文](README.zh-CN.md)

## Prepare the Zephyr Workspace

We recommend the [published Zephyr image](../../../docker/README.md#run-tests), `ghcr.io/xiaomi/bsworld-zephyr`, which includes the Zephyr shell executables and the Zephyr Controller. Select the Zephyr suite with `pytest tests/integration/zephyr`.

The steps below are for native Linux builds. Prepare BSWorld using the [local Linux setup](../../../README.md#local-linux) and install the system dependencies from Zephyr's [Getting Started Guide](https://docs.zephyrproject.org/latest/develop/getting_started/index.html).

Create a separate Python environment for Zephyr and install west:

```sh
export ZEPHYR_WORKSPACE="$HOME/zephyrproject"
mkdir -p "$ZEPHYR_WORKSPACE"
python3.12 -m venv "$ZEPHYR_WORKSPACE/.venv"
source "$ZEPHYR_WORKSPACE/.venv/bin/activate"
python -m pip install west
```

Fetch the Zephyr revision pinned by [base.Dockerfile](../../../docker/base.Dockerfile):

```sh
git init "$ZEPHYR_WORKSPACE/zephyr"
git -C "$ZEPHYR_WORKSPACE/zephyr" remote add origin https://github.com/zephyrproject-rtos/zephyr
git -C "$ZEPHYR_WORKSPACE/zephyr" fetch --depth=1 origin f545c1ca45bd0e5cb767d01996f5806f9f1e50be
git -C "$ZEPHYR_WORKSPACE/zephyr" checkout --detach FETCH_HEAD
```

Initialize the workspace and download its modules and Python dependencies:

```sh
west init -l "$ZEPHYR_WORKSPACE/zephyr"
cd "$ZEPHYR_WORKSPACE"
west update
west packages pip --install
west zephyr-export
```

Install the toolchain:

```sh
cd "$ZEPHYR_WORKSPACE/zephyr"
west sdk install --gnu-toolchains x86_64-zephyr-elf
```

## Compile and Run

Build BabbleSim using the [repository instructions](../../../README.md#build-and-test) and the [Zephyr Controller](../../../docs/controllers.md#building-the-zephyr-controller). Keep `BSWORLD_ROOT`, `BSIM_OUT_PATH` and `BSIM_COMPONENTS_PATH` exported while building Zephyr.

| Suite | Build instructions | Test artifact variables |
|---|---|---|
| Bluetooth shell | [Bluetooth shell tests](shell/README.md#build-and-run) | `BSWORLD_ZEPHYR_FULL_SHELL_BIN`, `BSWORLD_ZEPHYR_SHELL_BIN` |

## License

- Test code: Apache License 2.0.
- Zephyr: retains its upstream licenses.

See [Third-Party Notices](../../../THIRD_PARTY_NOTICES.md).
