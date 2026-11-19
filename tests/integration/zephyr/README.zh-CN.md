# Zephyr 测试环境

[English](README.md) | [简体中文](README.zh-CN.md)

[Bluetooth shell](shell/README.zh-CN.md) 测试在原生 `nrf52_bsim` 进程中运行 Zephyr Host，通过 HCI 接入 BSController。

## 准备 Zephyr 工作区

先按[本地 Linux 环境搭建](../../../docs/zh-CH/README.md#本地-linux)准备 BSWorld，并按 Zephyr [Getting Started Guide](https://docs.zephyrproject.org/latest/develop/getting_started/index.html) 安装系统依赖。

为 Zephyr 创建独立的 Python 环境并安装 west：

```sh
export ZEPHYR_WORKSPACE="$HOME/zephyrproject"
mkdir -p "$ZEPHYR_WORKSPACE"
python3.12 -m venv "$ZEPHYR_WORKSPACE/.venv"
source "$ZEPHYR_WORKSPACE/.venv/bin/activate"
python -m pip install west
```

拉取 [zephyr.Dockerfile](../../../docker/zephyr.Dockerfile) 固定的 Zephyr 版本：

```sh
git init "$ZEPHYR_WORKSPACE/zephyr"
git -C "$ZEPHYR_WORKSPACE/zephyr" remote add origin https://github.com/zephyrproject-rtos/zephyr
git -C "$ZEPHYR_WORKSPACE/zephyr" fetch --depth=1 origin f545c1ca45bd0e5cb767d01996f5806f9f1e50be
git -C "$ZEPHYR_WORKSPACE/zephyr" checkout --detach FETCH_HEAD
```

初始化工作区，下载模块和 Python 依赖：

```sh
west init -l "$ZEPHYR_WORKSPACE/zephyr"
cd "$ZEPHYR_WORKSPACE"
west update
west packages pip --install
west zephyr-export
```

安装编译所需的工具链：

```sh
cd "$ZEPHYR_WORKSPACE/zephyr"
west sdk install --gnu-toolchains x86_64-zephyr-elf
```

按[仓库构建说明](../../../docs/zh-CH/README.md#构建与测试)构建 BabbleSim。构建 Zephyr 时保留 `BSWORLD_ROOT`、`BSIM_OUT_PATH` 和 `BSIM_COMPONENTS_PATH` 环境变量。

按 [Bluetooth shell 构建说明](shell/README.zh-CN.md#构建与运行)编译并运行测试。

## 许可证

- 测试代码：Apache License 2.0。
- Zephyr：保留其上游许可证。

详见[第三方声明](../../../docs/zh-CH/THIRD_PARTY_NOTICES.md)。
