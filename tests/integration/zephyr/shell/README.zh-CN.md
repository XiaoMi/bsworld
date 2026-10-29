# Zephyr 蓝牙 Shell 测试

[English](README.md) | [简体中文](README.zh-CN.md)

本组测试运行两个 Host-only 的上游 Zephyr shell 程序，每个进程连接自己的虚拟 Controller，由 pytest 通过 shell 命令驱动。[source/](source/) 存放 BSWorld 的 Kconfig 和 HCI overlay。

| 程序 | 上游入口 | 可执行文件变量 |
|---|---|---|
| LE | `tests/bluetooth/shell` | `BSWORLD_ZEPHYR_SHELL_BIN` |
| Mesh | `tests/bluetooth/mesh_shell` | `BSWORLD_ZEPHYR_MESH_SHELL_BIN` |

[Docker 运行镜像](../../../../docker/README.zh-CN.md#选择测试套件)已包含这两个程序并设置好变量。以下步骤用于本机 Linux 构建。

## 准备 Zephyr 工作区

先按[本地 Linux 环境搭建](../../../../docs/zh-CH/README.md#本地-linux)准备 BSWorld，并按 Zephyr [Getting Started Guide](https://docs.zephyrproject.org/latest/develop/getting_started/index.html) 安装系统依赖。

为 Zephyr 创建独立的 Python 环境并安装 west：

```sh
export ZEPHYR_WORKSPACE="$HOME/zephyrproject"
mkdir -p "$ZEPHYR_WORKSPACE"
python3.12 -m venv "$ZEPHYR_WORKSPACE/.venv"
source "$ZEPHYR_WORKSPACE/.venv/bin/activate"
python -m pip install west
```

拉取 Dockerfile 固定的 Zephyr 版本：

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

还需要按[仓库构建说明](../../../../docs/zh-CH/README.md#构建与测试)构建 BabbleSim，并构建 [Zephyr 虚拟 Controller](../../../../docs/zh-CH/controllers.md#编译-zephyr-controller)。

## 构建与运行

在 Zephyr 环境中设置公共路径：

```sh
source "$ZEPHYR_WORKSPACE/.venv/bin/activate"
cd "$ZEPHYR_WORKSPACE"
SRC="$BSWORLD_ROOT/tests/integration/zephyr/shell/source"
SHELL_APP="$ZEPHYR_WORKSPACE/zephyr/tests/bluetooth/shell"
OVERLAY="$SHELL_APP/xterm-native-shell.overlay;$SRC/bsim_host_only.overlay"
```

构建 LE 程序：

```sh
west build -p always -b nrf52_bsim -d build/bsworld_zephyr_shell "$SHELL_APP" -- \
  -DCONF_FILE="$SRC/prj.conf" -DEXTRA_CONF_FILE="$SRC/le.conf" \
  -DDTC_OVERLAY_FILE="$OVERLAY"
export BSWORLD_ZEPHYR_SHELL_BIN="$ZEPHYR_WORKSPACE/build/bsworld_zephyr_shell/zephyr/zephyr.exe"
```

构建 Mesh 程序：

```sh
west build -p always -b nrf52_bsim -d build/bsworld_zephyr_mesh_shell \
  "$ZEPHYR_WORKSPACE/zephyr/tests/bluetooth/mesh_shell" -- \
  -DCONF_FILE="$SRC/prj.conf" -DEXTRA_CONF_FILE="$SRC/mesh.conf" \
  -DDTC_OVERLAY_FILE="$OVERLAY"
export BSWORLD_ZEPHYR_MESH_SHELL_BIN="$ZEPHYR_WORKSPACE/build/bsworld_zephyr_mesh_shell/zephyr/zephyr.exe"
```

在同一终端切回 BSWorld 环境运行测试：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/zephyr/shell -n 2 -q
```

| 文件 | 场景 | Controller |
|---|---|---|
| [test_le_features.py](test_le_features.py) | 扫描、干扰、连接过程、配对与隐私、Coded PHY、周期同步 | Zephyr 与 BSController 两两组合 |
| [test_mesh.py](test_mesh.py) | 配网、模型配置、组控 | 两种 Controller 各跑一次 |

## 添加场景

```python
from tests.integration.zephyr.shell.devices import add_zephyr_shell, initialize_shell

scanner = add_zephyr_shell(topo, name="scanner", address="C0:FF:EE:41:00:02")
await topo.start()
await initialize_shell(scanner)
await scanner.shell.run("bt scan passive")
await scanner.wait_log("Bluetooth passive scan enabled")
```

Mesh 程序在两个辅助函数中都传入 `mesh=True`。进程输出位于 `zephyr/shell/<name>/`，其余见[产物目录结构](../../../../docs/zh-CH/artifacts.md)。

## 许可证

BSWorld 的测试代码和配置使用 Apache License 2.0；Zephyr 保留其上游许可证，见[第三方声明](../../../../docs/zh-CH/THIRD_PARTY_NOTICES.md)。
