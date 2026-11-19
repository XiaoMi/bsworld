# Zephyr 蓝牙 Shell 测试

[English](README.md) | [简体中文](README.zh-CN.md)

Pytest 通过 shell 命令驱动上游 Zephyr `tests/bluetooth/shell`。每个 Zephyr Host 通过 HCI socket 连接自己的 BSController。[source/](source/) 存放 Kconfig 和 HCI overlay。

## 构建与运行

准备 [Zephyr 工作区](../README.zh-CN.md#准备-zephyr-工作区)，编译 shell 并导出可执行文件路径。

```sh
source "$ZEPHYR_WORKSPACE/.venv/bin/activate"
cd "$ZEPHYR_WORKSPACE"
SRC="$BSWORLD_ROOT/tests/integration/zephyr/shell/source"
APP="$ZEPHYR_WORKSPACE/zephyr/tests/bluetooth/shell"

west build -p always -b nrf52_bsim -d build/bsworld_zephyr_shell "$APP" -- \
  -DCONF_FILE="$SRC/prj.conf" \
  -DDTC_OVERLAY_FILE="$APP/xterm-native-shell.overlay;$SRC/bsim_host_only.overlay"
export BSWORLD_ZEPHYR_SHELL_BIN="$ZEPHYR_WORKSPACE/build/bsworld_zephyr_shell/zephyr/zephyr.exe"
```

在同一个终端切回 BSWorld 环境并运行测试：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/zephyr/shell -n auto -q
```

| 目录 | 对端 |
|---|---|
| [shell_peer/](shell_peer/README.zh-CN.md) | Zephyr Bluetooth shell |
| [bumble_peer/](bumble_peer/README.zh-CN.md) | Bumble Python Host |

## 添加场景

```python
from tests.integration.zephyr.shell.devices import create_zephyr_shell
from tests.integration.zephyr.shell_common import initialize_shell

advertiser = create_zephyr_shell(topo, name="advertiser", public_address="C0:FF:EE:41:00:01")
scanner = create_zephyr_shell(topo, name="scanner", public_address="C0:FF:EE:41:00:02")
await topo.start()
for device in (advertiser, scanner):
    await initialize_shell(device)
await scanner.shell.run("bt scan passive")
await scanner.wait_log("Bluetooth passive scan enabled")
```

`create_zephyr_shell` 注册进程并挂载 BSController。Shell 输出位于 `zephyr/shell/<name>/`，HCI 和空口抓包路径见[测试产物](../../../../docs/zh-CH/artifacts.md)。

## 许可证

- 测试代码和 `source/` 中的配置：Apache License 2.0。
- Zephyr：保留其上游许可证。

详见[第三方声明](../../../../docs/zh-CH/THIRD_PARTY_NOTICES.md)。
