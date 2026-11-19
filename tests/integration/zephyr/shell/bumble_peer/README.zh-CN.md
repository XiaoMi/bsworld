# Zephyr Shell 与 Bumble 测试

[English](README.md) | [简体中文](README.zh-CN.md)

`peer` fixture 在两个独立的 BSController 上运行 Zephyr Bluetooth shell 和 [Bumble](../../../bumble/README.zh-CN.md) Host。它固定了 Bumble 的 SMP 随机数和密钥对，以便复现配对抓包。 [conftest.py](conftest.py)

## 构建与运行

按 [Bluetooth shell 构建说明](../README.zh-CN.md#构建与运行)编译 shell，然后运行测试。

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/zephyr/shell/bumble_peer -n auto -q
```

[test_bumble.py](test_bumble.py) 覆盖双向扫描和连接、GATT、配对与绑定后重连、链路更新、LE Coded 连接，以及双向的周期广播同步、丢失与恢复。

## 添加场景

```python
async def test_bumble_finds_shell(peer):
    await peer.shell.shell.run("bt advertise on identity")
    await peer.shell.wait_log("Advertising started")
    await peer.bumble.start_scanning(active=True)
```

`peer.shell` 是已启动并初始化的 shell 进程，`peer.bumble` 是已打开的 Bumble `Device`，`peer.controller` 是 Bumble 的 Controller，公共地址为 `F0:F1:F2:F3:F4:01`。Bumble 的 HCI 抓包位于 `bumble/`，其余见[产物目录结构](../../../../../docs/zh-CH/artifacts.md)。

## 许可证

本目录的测试使用 Apache License 2.0，其余见[上级许可证说明](../README.zh-CN.md#许可证)。
