# BSController 协议测试

[English](PROTOCOL.md) | 简体中文

本测试套件用于验证 BSController 的 HCI 命令、事件和链路层协议行为，参考 HCI.TS.p39、LL.TS.p28 及蓝牙核心规范。测试结果用于开发验证，不代表 Bluetooth SIG 资格认证。

## 测试范围

当前选取 614 个协议用例：133 个 HCI 用例、481 个 LL 用例，分布在 17 个测试文件中。覆盖设备发现、广播、连接、定时、报文编码、跳频、安全及数据传输，也包含隐私和基于决策的广播过滤（DBAF）相关测试。

用例保留测试规范中的编号，可按编号查找和运行。下表的章节号对应测试规范章节。

| 目录 | 来源章节 | 测试文件 | 选取用例数 |
|---|---|---|---:|
| `hci/` | 4.3 Generic Events | [generic_events_test.py](hci/generic_events_test.py) | 5 |
| `hci/` | 4.4 Device Setup | [device_setup_test.py](hci/device_setup_test.py) | 5 |
| `hci/` | 4.5 Controller Flow Control | [controller_flow_control_test.py](hci/controller_flow_control_test.py) | 1 |
| `hci/` | 4.6 Controller Information | [controller_information_test.py](hci/controller_information_test.py) | 5 |
| `hci/` | 4.7 Device Discovery | [device_discovery_test.py](hci/device_discovery_test.py) | 72 |
| `hci/` | 4.8 Host Flow Control | [host_flow_control_test.py](hci/host_flow_control_test.py) | 1 |
| `hci/` | 4.10 Controller Configuration | [controller_configuration_test.py](hci/controller_configuration_test.py) | 32 |
| `hci/` | 4.13 LE Connection Management | [connection_management_test.py](hci/connection_management_test.py) | 7 |
| `hci/` | 4.17 Event Versioning | [event_versioning_test.py](hci/event_versioning_test.py) | 5 |
| `ll/` | 4.2 Device Discovery | [device_discovery_test.py](ll/device_discovery_test.py) | 151 |
| `ll/` | 4.3 Connection | [connection_test.py](ll/connection_test.py) | 219 |
| `ll/` | 4.4 Timing | [timing_test.py](ll/timing_test.py) | 21 |
| `ll/` | 4.5 Encoding | [encoding_test.py](ll/encoding_test.py) | 7 |
| `ll/` | 4.6 Frequency Hopping | [frequency_hopping_test.py](ll/frequency_hopping_test.py) | 6 |
| `ll/` | 4.7 Packet Handling | [packet_handling_test.py](ll/packet_handling_test.py) | 6 |
| `ll/` | 4.8 Security | [security_test.py](ll/security_test.py) | 67 |
| `ll/` | 4.9 Data Flow | [data_flow_test.py](ll/data_flow_test.py) | 4 |

本套件覆盖上述选取用例，不表示对 HCI.TS、LL.TS 或蓝牙核心规范的完整覆盖。实际执行范围还取决于被测 Controller 的能力和测试环境。

## 运行测试

先按[测试 README](../README.zh-CN.md#运行)准备环境。以下命令在 `components/bscontroller` 目录执行；射频用例需要可用的 BabbleSim PHY。

```sh
# 运行全部协议用例。
python -m pytest tests/qualification -n auto --dist worksteal -v -rs

# 只运行 HCI 协议用例。
python -m pytest tests/qualification/hci -n auto --dist worksteal -v -rs

# 按规范编号运行一个用例。
python -m pytest tests/qualification -k 'LL/DDI/SCN/BV-88-C' -v -rs
```

使用 `-n 4` 可指定并行数量。环境配置和并行运行注意事项见[测试 README](../README.zh-CN.md#并行执行)。

## 理解测试结果

pytest 的 `passed` 表示用例执行的断言通过；协议裁决及其限制以该用例的 `protocol.log` 为准。

| 结果 | 含义 |
|---|---|
| `PASS` | 用例检查通过，未标记精度或规范描述方面的限制。 |
| `PARTIAL` | 已执行的检查通过，但存在时间精度或规范描述方面的限制，需结合用例说明评估。 |
| `FAILED` | 执行或检查失败，需要查看失败断言、协议日志和相关报文。 |
| `SKIPPED` | 用例未执行或未完成，不能计为协议通过；原因见 pytest 的跳过说明。 |

`PARTIAL` 对应以下标记，用例可能同时具有两者：

- `limited`：涉及亚微秒级要求，测试采用 1 微秒时间分辨率，结果带有精度限制。
- `source_conflict`：测试规范的步骤、分支或预期结果仍存在解释限制；具体影响见用例说明。

未配置 BabbleSim 时，射频用例会跳过，因此应同时检查执行数量和跳过原因。

## 查看日志与限制

每个用例的 pytest 临时目录中保存 `protocol.log`，记录用例编号、测试说明、参数和协议裁决。射频用例还提供 HCI 抓包及 Controller、PHY 日志。可通过 pytest 的 `--basetemp` 选项指定输出目录。

分析失败或部分通过结果时，先按用例编号定位日志，再查看测试说明中的步骤、预期结果和 `Reference`。必要的规范解释记录在 `Source Correction` 中。

射频用例运行于 BabbleSim 仿真环境，时间相关结果按用例说明中声明的精度解释。

## 许可证

版权所有 2026 Xiaomi Corporation。基于 Apache License 2.0 许可；参见 [LICENSE](../../LICENSE)。
