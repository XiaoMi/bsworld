# Zephyr Shell 与 Bumble 测试

[English](README.md) | [简体中文](README.zh-CN.md)

本组测试让完整 Zephyr Bluetooth shell 与运行在 pytest event loop 中的 [Bumble](../../../bumble/README.zh-CN.md) Host 互测。[conftest.py](conftest.py) 中的 `peer` fixture 启动 shell，在独立的虚拟 Controller 上打开 Bumble，并让两种 Controller 实现各跑一次。它固定了 Bumble 的 SMP 随机数和密钥对，使配对过程的抓包在多次运行之间保持一致。

## 准备环境

按 [Bluetooth shell 构建说明](../README.zh-CN.md#构建与运行)编译完整固件并导出 `BSWORLD_ZEPHYR_FULL_SHELL_BIN`。`[zephyr]` 用例还需要构建 [Zephyr Controller](../../../../../docs/zh-CH/controllers.md#编译-zephyr-controller)，未构建时跳过。

## 构建与运行

程序已按 Bluetooth shell 构建说明编译。运行测试：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/zephyr/shell/bumble_peer -n auto -q
```

| 文件 | 场景 | Controller |
|---|---|---|
| [test_bumble.py](test_bumble.py) | 双向扫描、任一侧发起连接、shell 作为 GATT 服务端和客户端、配对与绑定后重连、PHY/数据长度/连接参数更新、LE Coded 连接，以及双向的周期广播同步、丢失与恢复 | 两种 Controller 各跑一次 |

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
