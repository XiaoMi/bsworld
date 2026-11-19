# Zephyr Shell 互测

[English](README.md) | [简体中文](README.zh-CN.md)

[conftest.py](conftest.py) 中的 fixture 为各 Zephyr Bluetooth shell 启动独立的 BSController。`shell_pair` 提供 Peripheral 和 Central，`shell_trio` 再加入一个周期广播设备。

## 构建与运行

按 [Bluetooth shell 构建说明](../README.zh-CN.md#构建与运行)编译 shell，然后运行测试。

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/zephyr/shell/shell_peer -n auto -q
```

| 文件 | 场景 |
|---|---|
| [test_scan.py](test_scan.py) | 被动扫描不可连接广播，主动扫描并收到 SCAN_RSP |
| [test_gatt.py](test_gatt.py) | 加密、服务发现、订阅、读取和通知 |
| [test_link_updates.py](test_link_updates.py) | PHY、数据长度（双向）、Connection Update、Connection Parameters Request 与 Channel Map 更新，之后发送 200 字节通知 |
| [test_coded.py](test_coded.py) | LE Coded 连接（`coded no-1m`）和 Peripheral 发起 PHY 更新 |
| [test_privacy.py](test_privacy.py) | 绑定、复用 LTK、Controller 解析 RPA，以及 `dir-rpa` 定向广播 |
| [test_sync_transfer.py](test_sync_transfer.py) | 由任一角色发起 Periodic Advertising Set Info Transfer 和 Sync Transfer |
| [test_radio.py](test_radio.py) | 一个广播设备与两个扫描设备之间的定向干扰与恢复 |

## 添加场景

```python
from tests.integration.zephyr.shell.shell_peer.common import connect, disconnect


async def test_reconnects(shell_pair):
    peripheral, central = shell_pair
    for _ in range(2):
        await connect(peripheral, central)
        await disconnect(peripheral, central)
```

`connect()` 默认让 Peripheral 用 identity 地址广播，并返回 Peripheral 报告的 Central 地址。共用的 shell 操作见 [common.py](common.py)。

## 许可证

本目录的测试使用 Apache License 2.0，其余见[上级许可证说明](../README.zh-CN.md#许可证)。
