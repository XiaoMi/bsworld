# Bumble Host 示例

[English](README.md) | 简体中文

每个示例固定执行一次蓝牙场景，每个设备运行上游 Bumble Host，并通过 Unix H4 连接自己的 BSController。所有 Controller 共用 BabbleSim PHY，Host 事件循环使用仿真时间。

在 `components/bscontroller` 下安装 Host 扩展，并选择已构建的 BabbleSim：

```sh
python -m pip install -e '.[bumble]'
export BSIM_OUT_PATH=/path/to/babblesim/build
python -m samples.advertise_scan
```

| 命令 | 场景和检查 |
|---|---|
| `python -m samples.advertise_scan` | 广播端被扫描端发现。 |
| `python -m samples.scan_adv` | 三个设备同时扫描、广播，每个设备均收到另外两个设备的广播。 |
| `python -m samples.gatt` | Central 发现 Battery Service、读取电量并接收通知。 |
| `python -m samples.central_multilink` | 一个 Central 保持三个 Peripheral 连接，逐条验证 GATT 读取和通知；可用 `--peripherals N` 调整设备数量。 |
| `python -m samples.periodic` | 一个扫描端同时保持两个独立的周期广播同步，并收到两路数据。 |
| `python -m samples.coexistence` | 五个设备同时保持五条 ACL、两个周期广播、四个周期同步，以及扫描、广播和 GATT 数据收发。 |

成功后打印 JSON 结果并退出；广播或通知未收到、数据不符、连接丢失都会使示例失败。使用 `--log-dir /tmp/bscontroller-samples` 保留 `result.json`、`phy.log`、每个设备独立的 `.snoop` 抓包，以及 `air/` 下的 PHY 收发记录。`devices.json` 记录空口设备编号、名称、地址与 HCI 抓包的对应关系。每次运行创建独立子目录。`--bsim-dir` 可覆盖 `BSIM_OUT_PATH`；`--timeout` 按仿真秒数限制整个示例的执行时间。

`coexistence` 中，两个 hub 分别连接两个 leaf，`hub-b` 还连接 `hub-a`。因此 `hub-a` 同时承担 Central 和 Peripheral 角色，每个 leaf 接受两个 Central。两个 hub 各自发送周期广播并同步对方的广播，另一个独立扫描端同时同步两路周期广播。检查要求在五条连接收发 Battery Service 通知和读取数据期间，扫描报告与周期广播报告也持续更新。
