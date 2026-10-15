# Zephyr Workspace and Shared Configuration

[English](README.md) | [中文](README.zh-CN.md)

This directory maintains only BSWorld configuration: `bsim_host_only.overlay` selects Unix socket HCI transport, and `bsim.conf` enables shared Host-only BabbleSim settings. Mesh also uses `mesh/source/mesh.conf`. Sample source lives in the upstream Zephyr workspace.

## Prepare the Workspace

Prepare BSWorld using the repository README's [local Linux setup](../../../../../../README.md#local-linux). Install Zephyr's additional dependencies from the official [Getting Started Guide](https://docs.zephyrproject.org/latest/develop/getting_started/index.html), then create the workspace below. The source revision matches the Dockerfile.

`$ZEPHYR_WORKSPACE/.venv` is only for `west` and `west build`. `$BSWORLD_ROOT/.venv` stays the environment for `pip install -e .` and pytest. The commands below activate the Zephyr environment explicitly; activate `$BSWORLD_ROOT/.venv` again before pytest. `source .../bin/activate` changes `PATH` and leaves exported `BSWORLD_*` and BabbleSim variables set.

That local Linux setup installs `gcc-multilib` and `g++-multilib` because BabbleSim and the `nrf52_bsim` board need a 32-bit host toolchain.

```sh
export ZEPHYR_WORKSPACE="$HOME/zephyrproject"
python3.12 -m venv "$ZEPHYR_WORKSPACE/.venv"
source "$ZEPHYR_WORKSPACE/.venv/bin/activate"
python -m pip install west

# Use the same revision as the Dockerfile when reproducing the validated setup.
git init "$ZEPHYR_WORKSPACE/zephyr"
git -C "$ZEPHYR_WORKSPACE/zephyr" remote add origin https://github.com/zephyrproject-rtos/zephyr
git -C "$ZEPHYR_WORKSPACE/zephyr" fetch --depth=1 origin 35c46048d330ce1bea9248d4dd6a9879d0fe5be7
git -C "$ZEPHYR_WORKSPACE/zephyr" checkout --detach FETCH_HEAD
west init -l "$ZEPHYR_WORKSPACE/zephyr"
cd "$ZEPHYR_WORKSPACE"
west update
west packages pip --install
west zephyr-export
cd zephyr
west sdk install --gnu-toolchains x86_64-zephyr-elf
```

After setup, compile BabbleSim from [README build instructions](../../../../../../README.md#build-and-test), then follow the [sample index](../../README.md) to each test directory's README for its `west build` commands and test exports.
