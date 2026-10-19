# Zephyr 工作区与公共配置

[English](README.md) | [中文](README.zh-CN.md)

本目录仅维护 BSWorld 的配置：`bsim_host_only.overlay` 选择 Unix socket HCI 传输，`bsim.conf` 启用 Host-only BabbleSim 设置。Mesh 另外使用 `mesh/source/mesh.conf`。示例源码来自 Zephyr 工作区。

## 准备工作区

先按仓库 README 的[本地 Linux 环境搭建](../../../../../../docs/zh-CH/README.md#本地-linux)安装 BSWorld，再参考 Zephyr 官方[入门文档](https://docs.zephyrproject.org/latest/develop/getting_started/index.html)安装额外依赖，并创建以下工作区。源码版本与 Dockerfile 一致。

`$ZEPHYR_WORKSPACE/.venv` 只用于 `west` 和 `west build`。`$BSWORLD_ROOT/.venv` 仍然是 `pip install -e .` 和 pytest 的环境。下面的命令会显式激活 Zephyr 环境；运行 pytest 前再激活 `$BSWORLD_ROOT/.venv`。`source .../bin/activate` 只会改变 `PATH`，已经导出的 `BSWORLD_*` 和 BabbleSim 变量会保留。

本地 Linux 环境会安装 `gcc-multilib` 和 `g++-multilib`，因为 BabbleSim 和 `nrf52_bsim` 开发板需要 32 位主机工具链。

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

完成环境搭建后，先按[README 构建说明](../../../../../../docs/zh-CH/README.md#构建与测试)编译 BabbleSim，再从[示例索引](../../README.zh-CN.md)进入对应测试目录的 README，执行其中的 `west build` 命令并导出测试变量。
