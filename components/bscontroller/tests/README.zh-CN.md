# RadioController 测试

[English](README.md) | [简体中文](README.zh-CN.md)

这些测试套件将实现层的不变量与蓝牙协议行为区分开来。[组件 README](../README.zh-CN.md) 记录了安装方式、可执行文件 API 示例、独立 HCI 服务的运行，以及发行版构建。

| 目录 | 范围 | BabbleSim 需求 |
|---|---|---|
| `unit/` | Controller 算法、状态归属、调度器以及 FIFO/线程/时钟边界，按实现模块组织。 | 无需原生 PHY。 |
| `qualification/hci/`、`qualification/ll/` | 全部 614 个选定的 HCI/LL 用例，按其规范来源章节组织，配有独立的下位测试器。 | RF 用例需要；纯 HCI 检查无需它即可运行。 |

[协议章节映射](qualification/PROTOCOL.zh-CN.md) 列出 17 个主测试文件、选取用例数、结果含义与限制。每个单元测试文件在模块 docstring 中说明测试范围，每个测试函数都写明测试目的、步骤和预期结果。

这些独立实现的测试并不构成 Bluetooth 资格认证，也不意味着对 LL.TS/HCI.TS 的完整覆盖。

## 运行

在 RadioController 检出目录中：

```sh
# 运行自包含的实现检查。
python -m pytest tests/unit/ -n auto --dist worksteal -v
```

对于 RF 用例，选择一个带有 2G4 v1/v2 FIFO 协议、`BLE_simple` modem 和 `multiatt` 信道模型的 BabbleSim 构建：

```sh
export BSIM_OUT_PATH=/path/to/bsim/build
python -m pytest tests/qualification -n auto --dist worksteal -v -rs

# 运行完整的 RadioController 回归测试。
python -m pytest tests/ -n auto --dist worksteal -v
```

原生用例拥有独立的仿真 ID 并支持 pytest-xdist。当 `BSIM_OUT_PATH` 未设置时它们会被跳过；若配置了路径但缺少 PHY 可执行文件则会失败。单元测试的收集无需任何原生产物。

## 并行执行

```sh
python -m pip install pytest-xdist
export BSIM_OUT_PATH=/path/to/bsim/build
python -m pytest tests/ -n auto --dist worksteal -q -rs
```

每个原生用例拥有自己的 PHY 进程、仿真 ID、FIFO 和报文转储（packet dump）。Worker 临时目录和每个用例的协议日志也相互独立，因此各仿真不会通过相同的 RF 信道或设备地址相互干扰。如果以显式的 `--basetemp` 值运行多个 pytest 调用，请为每次调用指定各自独立的目录。

`--dist worksteal` 会在 worker 空闲时重新分配待处理的用例。单个流程不会被拆分到多个 worker 之间。使用 `-n 4` 选择固定的 worker 数量。Worker 共享 CPU、内存和存储，因此增加 worker 数量并不保证运行更快。

## 许可证

版权所有 2026 小米公司（Xiaomi Corporation）。这些独立编写的测试基于 Apache License 2.0 授权；参见仓库 [LICENSE](../LICENSE)。
