# Bumble Host 示例

[English](README.md) | 简体中文

每个示例运行一次蓝牙场景。每台设备使用 Bumble Host，通过 Unix socket 与各自的 BSController 交换 H4 报文。所有 Controller 共享一个 BabbleSim PHY，Host 事件循环使用仿真时间。

## 运行示例

在 `components/bscontroller` 目录安装 `bumble` 扩展，并将 `BSIM_OUT_PATH` 指向 BabbleSim 构建目录。编译步骤见[快速开始](../README.zh-CN.md#快速开始)。

```sh
python -m pip install -e '.[bumble]'
export BSIM_OUT_PATH=/path/to/babblesim/build
python -m samples.advertise_scan
```

运行成功后，示例会打印 JSON 结果。使用 `--bsim-dir` 可覆盖 `BSIM_OUT_PATH`。`--timeout` 以仿真秒为单位设置超时，包含 Host 初始化时间。

## 示例场景

| 命令 | 场景和检查 |
|---|---|
| `python -m samples.advertise_scan` | 扫描端发现广播端。 |
| `python -m samples.scan_adv` | 三个设备同时扫描、广播。每台设备都收到另外两台的广播。 |
| `python -m samples.gatt` | Central 发现 Battery Service、读取电量并接收通知。 |
| `python -m samples.central_multilink` | 一个 Central 保持三个 Peripheral 连接，逐条验证 GATT 读取和通知。可用 `--peripherals N` 调整 Peripheral 数量。 |
| `python -m samples.periodic` | 一个扫描端同时保持两个独立的周期广播同步，并收到两路数据。 |
| `python -m samples.broadcast_iso` | 一个广播端向两个同步接收端发送两路 BIS。示例逐字节检查每个 SDU。 |
| `python -m samples.coexistence` | 五台设备在扫描、广播和传输 GATT 数据期间，同时保持五条 ACL 连接、两路周期广播和四个周期广播同步。 |

`broadcast_iso` 支持 `--encrypted`、`--framed`、`--phy 1|2|3`（1M、2M、Coded）和 `--receivers N`。

### 多角色共存

在 `coexistence` 中，`hub-a` 和 `hub-b` 都连接 `leaf-a` 和 `leaf-b`。`hub-b` 还连接 `hub-a`，使 `hub-a` 同时担当 Central 和 Peripheral。

`hub-a` 和 `hub-b` 各自发送周期广播并同步对方的广播，独立的扫描端同步两路广播。示例验证五条连接均能读取 Battery Service 并接收通知，同时仍能收到新的扫描报告和周期广播报告。

## 日志

使用 `--log-dir /tmp/bscontroller-samples` 保留日志。每次运行会创建独立的子目录，其中保存 `phy.log`、各设备的 `.snoop` 抓包和 `air/` 下的 PHY 收发记录。通过 `devices.json` 可按 PHY 设备编号查找名称、地址和 HCI 抓包。运行成功时还会保存 `result.json`。
