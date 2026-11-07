# Bumble 测试

[English](README.md) | [简体中文](README.zh-CN.md)

本组测试在 pytest 的 asyncio event loop 中运行 [Bumble](https://github.com/google/bumble) Python Host，与测试共用仿真时钟。每个设备连接自己的 BSController，即默认的[虚拟 Controller](../../../docs/zh-CH/controllers.md)。[common/](common/) 存放共用的设备辅助函数。

| Host | 上游 | 版本 |
|---|---|---|
| Bumble | [google/bumble](https://github.com/google/bumble) | `bumble==0.0.235`，在 [pyproject.toml](../../../pyproject.toml) 中固定 |

每个 [Docker 镜像](../../../docker/README.zh-CN.md#选择测试套件)都包含 Bumble，Zephyr 镜像默认运行该套件。以下步骤用于本机 Linux。

## 准备环境

先按[本地 Linux 环境搭建](../../../docs/zh-CH/README.md#本地-linux)准备 BSWorld（同时安装 Bumble），并按[仓库构建说明](../../../docs/zh-CH/README.md#构建与测试)构建 BabbleSim。

## 构建与运行

Bumble 无需构建。运行测试：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/bumble -n auto -q
```

| 文件 | 场景 | Controller |
|---|---|---|
| [broadcast_observe/test_bumble_broadcast_observe.py](broadcast_observe/test_bumble_broadcast_observe.py) | 传统广播与被动扫描、定向 CW 干扰与恢复 | 两种 Controller 各跑一次 |
| [connection/test_bumble_connection.py](connection/test_bumble_connection.py) | Central 与 Peripheral 建立连接 | 两种 Controller 各跑一次 |
| [periodic_advertising/test_bumble_periodic_advertising.py](periodic_advertising/test_bumble_periodic_advertising.py) | 周期广播与同步 | 两种 Controller 各跑一次 |

## 添加场景

```python
from contextlib import AsyncExitStack

from tests.integration.bumble.common import add_bumble_controller, open_bumble_device

controller = add_bumble_controller(topo, "observer", "F0:F1:F2:F3:F4:02")
await topo.start()
async with AsyncExitStack() as stack:
    observer = await open_bumble_device(
        stack, "observer", "F0:F1:F2:F3:F4:02", controller.hci_sock
    )
    await observer.start_scanning()
```

标准双设备场景使用 [conftest.py](conftest.py) 中的 `bumble_pair` fixture。它负责创建 Controller、配置 45 dB 衰减、启动拓扑并打开两个 Host。`open_bumble_device()` 只打开 HCI 连接并初始化 Bumble，不配置或启动拓扑。HCI 抓包位于 `bumble/<role>/`，其余见[产物目录结构](../../../docs/zh-CH/artifacts.md)。

## 许可证

- 测试代码：Apache License 2.0。
- Bumble：Apache License 2.0。

详见[第三方声明](../../../docs/zh-CH/THIRD_PARTY_NOTICES.md)。
