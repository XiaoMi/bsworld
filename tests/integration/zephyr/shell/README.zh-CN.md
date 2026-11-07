# Zephyr 蓝牙 Shell 测试

[English](README.md) | [简体中文](README.zh-CN.md)

本组测试运行编译成两种形态的上游 Zephyr Bluetooth shell，由 pytest 通过 shell 命令驱动每个进程。完整固件包含 Zephyr Host、Controller 和无线模型，直接接入 PHY；Host-only 固件通过 HCI socket 连接自己的虚拟 Controller。[source/](source/) 存放 BSWorld 的 Kconfig 和 HCI overlay。

| 程序 | 上游入口 | 内容 | 可执行文件变量 |
|---|---|---|---|
| 完整固件 | `tests/bluetooth/shell` | Host、Controller 和无线模型 | `BSWORLD_ZEPHYR_FULL_SHELL_BIN` |
| Host-only | `tests/bluetooth/shell` | Host；Controller 经 HCI 连接 | `BSWORLD_ZEPHYR_SHELL_BIN` |

## 准备环境

先准备 [Zephyr 工作区](../README.zh-CN.md#准备-zephyr-工作区)，按[仓库构建说明](../../../../docs/zh-CH/README.md#构建与测试)构建 BabbleSim。[Zephyr Controller](../../../../docs/zh-CH/controllers.md#编译-zephyr-controller) 是可选的，未编译时跳过 Zephyr Controller 用例。

## 构建与运行

在 Zephyr 环境中设置共用路径：

```sh
source "$ZEPHYR_WORKSPACE/.venv/bin/activate"
cd "$ZEPHYR_WORKSPACE"
SRC="$BSWORLD_ROOT/tests/integration/zephyr/shell/source"
APP="$ZEPHYR_WORKSPACE/zephyr/tests/bluetooth/shell"
```

编译完整固件：

```sh
west build -p always -b nrf52_bsim -d build/bsworld_zephyr_full_shell "$APP" -- \
  -DCONF_FILE="$SRC/prj.conf" -DEXTRA_CONF_FILE="$SRC/le.conf;$SRC/full.conf" \
  -DDTC_OVERLAY_FILE="$APP/xterm-native-shell.overlay"
export BSWORLD_ZEPHYR_FULL_SHELL_BIN="$ZEPHYR_WORKSPACE/build/bsworld_zephyr_full_shell/zephyr/zephyr.exe"
```

编译 Host-only 程序：

```sh
west build -p always -b nrf52_bsim -d build/bsworld_zephyr_shell "$APP" -- \
  -DCONF_FILE="$SRC/prj.conf" -DEXTRA_CONF_FILE="$SRC/le.conf" \
  -DDTC_OVERLAY_FILE="$APP/xterm-native-shell.overlay;$SRC/bsim_host_only.overlay"
export BSWORLD_ZEPHYR_SHELL_BIN="$ZEPHYR_WORKSPACE/build/bsworld_zephyr_shell/zephyr/zephyr.exe"
```

在同一个终端切回 BSWorld 环境并运行测试：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/zephyr/shell -n auto -q
```

| 目录 | 对端 | 拓扑 |
|---|---|---|
| [shell_peer/](shell_peer/README.zh-CN.md) | 另一个 Zephyr shell | 完整固件对完整固件；完整固件对 Host-only，覆盖两种 Controller 和两种角色 |
| [bumble_peer/](bumble_peer/README.zh-CN.md) | Bumble Python Host | 完整固件对 Bumble，两种 Controller 各跑一次 |

## 添加场景

```python
from tests.integration.zephyr.shell.devices import (
    create_zephyr_firmware_shell,
    create_zephyr_shell,
)
from tests.integration.zephyr.shell_common import initialize_shell

advertiser = create_zephyr_firmware_shell(topo, name="advertiser", seed=41)
scanner = create_zephyr_shell(topo, name="scanner")
scanner.attach(topo.add_controller(public_address="C0:FF:EE:41:00:02"))
await topo.start()
for device in (advertiser, scanner):
    await initialize_shell(device)
await scanner.shell.run("bt scan passive")
await scanner.wait_log("Bluetooth passive scan enabled")
```

完整固件不需要 Controller。Host-only shell 必须在 `topo.start()` 前挂载 Controller，其创建函数不接受地址或 Controller 类型。shell 互测场景按功能放在 `shell_peer/`，由其 `shell_pair` fixture 提供拓扑；Bumble 场景放在 `bumble_peer/`。进程输出位于 `zephyr/complete/<name>/` 和 `zephyr/shell/<name>/`，其余见[产物目录结构](../../../../docs/zh-CH/artifacts.md)。

## 许可证

- 测试代码和 `source/` 中的配置：Apache License 2.0。
- Zephyr：保留其上游许可证。

详见[第三方声明](../../../../docs/zh-CH/THIRD_PARTY_NOTICES.md)。
