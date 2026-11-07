# Zephyr 测试环境

[English](README.md) | [简体中文](README.zh-CN.md)

本组测试把 Zephyr `nrf52_bsim` 固件作为原生 Linux 进程运行。[Bluetooth shell](shell/README.zh-CN.md) 套件会在 Zephyr Controller 和 BSController 上分别运行各场景，参见[虚拟 Controller](../../../docs/zh-CH/controllers.md#用两种-controller-运行同一场景)。

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

先按[仓库构建说明](../../../docs/zh-CH/README.md#构建与测试)构建 BabbleSim。[Zephyr Controller](../../../docs/zh-CH/controllers.md#编译-zephyr-controller) 是可选的，未编译时跳过 Zephyr Controller 用例。构建 Zephyr 时保留 `BSWORLD_ROOT`、`BSIM_OUT_PATH` 和 `BSIM_COMPONENTS_PATH` 环境变量。

## 测试套件

| 套件 | 编译说明 | 可执行文件变量 |
|---|---|---|
| Bluetooth shell | [Bluetooth shell 测试](shell/README.zh-CN.md#构建与运行) | `BSWORLD_ZEPHYR_FULL_SHELL_BIN`、`BSWORLD_ZEPHYR_SHELL_BIN` |
| Zephyr Controller（可选） | [编译 Zephyr Controller](../../../docs/zh-CH/controllers.md#编译-zephyr-controller) | `BSWORLD_BT_VIRT_CONTROLLER_BIN` |

运行该套件：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/zephyr -n auto -q
```

## 许可证

- 测试代码：Apache License 2.0。
- Zephyr：保留其上游许可证。

详见[第三方声明](../../../docs/zh-CH/THIRD_PARTY_NOTICES.md)。
