# Zephyr Shell 互测

[English](README.md) | [简体中文](README.zh-CN.md)

本组测试让两个 Zephyr Bluetooth shell 互相通信。[conftest.py](conftest.py) 中的 `shell_pair` fixture 让每个场景在五种拓扑和角色组合下运行，并完成两个 shell 的初始化。[common.py](common.py) 存放连接、断连和 GATT 通知操作。

| 参数 ID | Peripheral | Central |
|---|---|---|
| `full-full` | 完整固件 | 完整固件 |
| `full-host-zephyr-full-peripheral` | 完整固件 | Host-only + ZephyrController |
| `full-host-zephyr-full-central` | Host-only + ZephyrController | 完整固件 |
| `full-host-bscontroller-full-peripheral` | 完整固件 | Host-only + BSController |
| `full-host-bscontroller-full-central` | Host-only + BSController | 完整固件 |

## 准备环境

按 [Bluetooth shell 构建说明](../README.zh-CN.md#构建与运行)编译两个程序并导出路径。`full-full` 只需要 `BSWORLD_ZEPHYR_FULL_SHELL_BIN`；`full-host-zephyr-*` 用例还需要 Zephyr Controller，未构建时跳过。

## 构建与运行

程序已按 Bluetooth shell 构建说明编译。运行测试：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/zephyr/shell/shell_peer -n auto -q
```

用参数 ID 选择拓扑：

```sh
pytest tests/integration/zephyr/shell/shell_peer -k 'full-full or targeted_interference' -q
pytest tests/integration/zephyr/shell/shell_peer -k full-host-zephyr -q
pytest tests/integration/zephyr/shell/shell_peer -k full-host-bscontroller -q
```

| 文件 | 场景 | 拓扑 |
|---|---|---|
| [test_scan.py](test_scan.py) | 被动扫描不可连接广播；主动扫描并收到 SCAN_RSP | 全部五种 |
| [test_gatt.py](test_gatt.py) | 加密、服务发现、订阅、读取和通知 | 全部五种 |
| [test_link_updates.py](test_link_updates.py) | PHY、数据长度（双向）、Connection Update、Connection Parameters Request 与 Channel Map 更新，之后发送 200 字节通知 | 全部五种 |
| [test_coded.py](test_coded.py) | Central 仅在 LE Coded 上发起连接（`coded no-1m`）；Peripheral 发起 PHY 更新 | 全部五种 |
| [test_privacy.py](test_privacy.py) | 绑定、复用 LTK、Controller 解析 RPA，以及 `dir-rpa` 定向广播 | 全部五种 |
| [test_radio.py](test_radio.py) | 定向干扰与恢复 | 三个完整固件设备，不参数化 |

## 添加场景

```python
from tests.integration.zephyr.shell.shell_peer.common import connect, disconnect


async def test_reconnects(shell_pair):
    peripheral, central = shell_pair
    for _ in range(2):
        await connect(peripheral, central)
        await disconnect(peripheral, central)
```

`shell_pair` 以 `(peripheral, central)` 返回已启动并初始化的 shell，同一场景只写一次即可在全部拓扑中运行。`connect()` 默认让 Peripheral 用 identity 地址广播，并返回 Peripheral 侧报告的 Central 地址。每个功能新建一个文件，并加入上表。

## 许可证

本目录的测试使用 Apache License 2.0，其余见[上级许可证说明](../README.zh-CN.md#许可证)。
