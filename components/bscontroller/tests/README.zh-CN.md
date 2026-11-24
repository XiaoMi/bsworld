# RadioController 测试

[English](README.md) | [简体中文](README.zh-CN.md)

| 目录 | 覆盖范围 | 测试环境 |
|---|---|---|
| `unit/` | 按实现模块组织，覆盖 Controller 算法、状态归属、调度以及 FIFO、线程和时钟行为。 | Python 测试环境。 |
| `qualification/hci/`、`qualification/ll/` | 选取 915 个 HCI 和 Link Layer 用例，按规范章节组织，由独立测试器执行。 | 纯 HCI 用例使用 Python 测试环境，射频用例还需要 BabbleSim。 |

各章节的覆盖范围、日志位置和结果解读见[协议测试指南](qualification/PROTOCOL.zh-CN.md)。

## 运行

按[开发说明](../README.zh-CN.md#开发)安装测试依赖，并从 `components/bscontroller/` 目录运行命令。

```sh
# 运行单元测试。
python -m pytest tests/unit/ -n auto --dist worksteal -v
```

射频用例需要 BabbleSim 的 `bin/bs_2G4_phy_v1`、2G4 v1 和 v2 FIFO 协议、`BLE_simple` 调制解调器及 `multiatt` 信道模型。

```sh
export BSIM_OUT_PATH=/path/to/bsim/build
python -m pytest tests/qualification -n auto --dist worksteal -v -rs

# 运行完整的 RadioController 回归测试。
python -m pytest tests/ -n auto --dist worksteal -v
```

未设置 `BSIM_OUT_PATH` 时，射频用例会跳过。设置后，测试要求该目录提供可执行的 `bin/bs_2G4_phy_v1`，否则用例会失败。

## 并行执行

`[test]` 扩展包含 pytest-xdist。上面的命令使用 `-n auto` 自动选择 worker 数量，也可用 `-n 4` 固定数量。`--dist worksteal` 将待运行的用例重新分配给空闲 worker。

每个射频用例独享 PHY 进程、仿真 ID、FIFO、报文转储和协议日志。独立的仿真可以使用相同的射频信道和设备地址。

显式设置 `--basetemp` 时，为每次并发运行指定独立目录。

## 许可证

版权所有 2026 小米公司（Xiaomi Corporation）。这些测试采用 Apache License 2.0，见[组件许可证](../LICENSE)。
