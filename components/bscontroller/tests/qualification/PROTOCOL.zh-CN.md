# BSController 协议测试

[English](PROTOCOL.md) | 简体中文

本套件用于开发验证，检查 BSController 的 HCI 命令、事件及链路层行为。用例依据 HCI.TS.p39、LL.TS.p28 和蓝牙核心规范编写。

## 测试范围

测试覆盖设备发现、广播、连接、定时、报文编码、跳频、安全和数据传输。选取的用例也覆盖隐私、基于决策的广播过滤（DBAF）、BIS 广播和同步接收。

下表按测试规范章节列出 26 个文件中的 1042 个协议用例，其中 HCI 187 个、LL 855 个。可按测试规范编号查找和运行用例。

| 目录 | 来源章节 | 测试文件 | 选取用例数 |
|---|---|---|---:|
| `hci/` | 4.3 Generic Events | [generic_events_test.py](hci/generic_events_test.py) | 5 |
| `hci/` | 4.4 Device Setup | [device_setup_test.py](hci/device_setup_test.py) | 5 |
| `hci/` | 4.5 Controller Flow Control | [controller_flow_control_test.py](hci/controller_flow_control_test.py) | 1 |
| `hci/` | 4.6 Controller Information | [controller_information_test.py](hci/controller_information_test.py) | 6 |
| `hci/` | 4.7 Device Discovery | [device_discovery_test.py](hci/device_discovery_test.py) | 75 |
| `hci/` | 4.8 Host Flow Control | [host_flow_control_test.py](hci/host_flow_control_test.py) | 1 |
| `hci/` | 4.10 Controller Configuration | [controller_configuration_test.py](hci/controller_configuration_test.py) | 59 |
| `hci/` | 4.11 Controller Setup | [controller_setup_test.py](hci/controller_setup_test.py) | 2 |
| `hci/` | 4.13 LE Connection Management | [connection_management_test.py](hci/connection_management_test.py) | 7 |
| `hci/` | 4.14 LE Power Control | [power_control_test.py](hci/power_control_test.py) | 3 |
| `hci/` | 4.15.2 Broadcast Isochronous Streams | [broadcast_isochronous_test.py](hci/broadcast_isochronous_test.py) | 17 |
| `hci/` | 4.17 Event Versioning | [event_versioning_test.py](hci/event_versioning_test.py) | 6 |
| `ll/` | 4.2 Device Discovery | [device_discovery_test.py](ll/device_discovery_test.py) | 176 |
| `ll/` | 4.3 Connection | [connection_test.py](ll/connection_test.py) | 258 |
| `ll/` | 4.3 Connection Subrating | [subrating_test.py](ll/subrating_test.py) | 21 |
| `ll/` | 4.3.7 Periodic Advertising Sync Transfer | [sync_transfer_test.py](ll/sync_transfer_test.py) | 84 |
| `ll/` | 4.4 Timing | [timing_test.py](ll/timing_test.py) | 29 |
| `ll/` | 4.4 Frame Space Update | [frame_space_test.py](ll/frame_space_test.py) | 94 |
| `ll/` | 4.5 Encoding | [encoding_test.py](ll/encoding_test.py) | 7 |
| `ll/` | 4.6 Frequency Hopping | [frequency_hopping_test.py](ll/frequency_hopping_test.py) | 6 |
| `ll/` | 4.7 Packet Handling | [packet_handling_test.py](ll/packet_handling_test.py) | 6 |
| `ll/` | 4.8 Security | [security_test.py](ll/security_test.py) | 67 |
| `ll/` | 4.9 Data Flow | [data_flow_test.py](ll/data_flow_test.py) | 4 |
| `ll/` | 4.11 Broadcast Isochronous Streams | [broadcast_isochronous_test.py](ll/broadcast_isochronous_test.py) | 28 |
| `ll/` | 4.12 Isochronous Testing | [isochronous_testing_test.py](ll/isochronous_testing_test.py) | 2 |
| `ll/` | 4.13 PCL | [power_control_test.py](ll/power_control_test.py) | 73 |

## 运行测试

在 `components/bscontroller` 目录运行以下命令。射频用例需要可用的 BabbleSim PHY，配置步骤见[测试环境说明](../README.zh-CN.md#运行)。

运行 `tests/qualification` 目录也会执行 4 项 [Lower Tester 回归测试](peer_test.py)和 1 项 [MCES 空口回归测试](ll/frame_space_test.py)。

```sh
# 运行全部协议用例。
python -m pytest tests/qualification -n auto --dist worksteal -v -rs

# 只运行 HCI 协议用例。
python -m pytest tests/qualification/hci -n auto --dist worksteal -v -rs

# 按规范编号运行一个用例。
python -m pytest tests/qualification -k 'LL/DDI/SCN/BV-88-C' -v -rs
```

使用 `-n 4` 可指定 worker 数量。worker 配置及输出目录隔离见[并行执行说明](../README.zh-CN.md#并行执行)。

## 理解测试结果

pytest 的 `passed` 表示已执行的断言通过。选取的协议用例执行完成后，会把协议裁决和限制写入 `protocol.log`。失败和跳过状态由 pytest 报告。

| 结果 | 含义 |
|---|---|
| `PASS` | 检查通过，用例未声明时间精度或规范描述方面的限制。 |
| `PARTIAL` | 已执行的检查通过，存在声明的限制。需结合用例说明评估。 |
| `FAILED` | 执行或检查失败。查看 pytest 的失败信息、已生成的协议日志和相关报文。 |
| `SKIPPED` | 用例未执行或未完成。原因见 pytest 报告。 |

通过的用例带有以下任一标记时，协议裁决为 `PARTIAL`。用例可以同时带有两种标记。

- `limited` 标记需要亚微秒级时间精度的用例。测试以 1 微秒分辨率近似检查。
- `source_conflict` 标记测试规范中尚未明确的步骤、分支或裁决要求。具体影响见用例说明。

实际执行范围取决于 Controller 能力和测试环境。未设置 `BSIM_OUT_PATH` 时，射频用例会跳过。查看结果时，同时核对执行数量和跳过原因。

## 查看日志与限制

选取的协议用例开始执行后，`protocol.log` 会记录用例编号、说明、参数和执行过程。启动了 PHY 的射频用例也可生成 `hci.snoop`、`controller.log` 和 `phy.log`。

这些文件位于用例的 pytest 临时目录。可通过 `--basetemp` 指定其根目录。

排查失败或部分通过时，按用例编号找到日志，对照用例说明中的步骤、预期结果和 `Reference`。测试对规范的解释记录在 `Source Correction` 中。按用例声明的时间精度分析射频结果。

## 许可证

版权所有 2026 Xiaomi Corporation。采用 [Apache License 2.0](../../LICENSE)。
