# QEMU BlueZ 测试

[English](README.md) | [中文](README.zh-CN.md)

该测试集在 AArch64 Linux Guest 中运行未经修改的 BlueZ Host 协议栈，并将 Guest 虚拟时间接入 BabbleSim。QEMU 把虚拟 Controller 的 Unix-socket HCI 数据映射为 Linux 中的 `/dev/ttyS0`，`btattach` 再将其注册为 `hci0`，测试随后通过 `bluetoothctl` 控制 BlueZ。

## 测试场景

| 测试 | 验证内容 |
|---|---|
| `test_qemu_bluez_initializes_virtual_controller` | Linux 成功注册 `hci0`，BlueZ 枚举到预期的 Controller 公共地址并完成上电。 |
| `test_qemu_bluez_advertises_to_bumble_observer` | BlueZ 配置可连接广播，独立 Bumble Observer 通过仿真无线链路校验完整设备名和公共地址。 |
| `test_qemu_bluez_scans_and_connects_bumble_peripheral` | BlueZ 按设备名和地址发现 Bumble Peripheral，建立 LE 连接，确认对端连接句柄，并正常断开。 |

Bumble 在这些用例中只作为行为可控的对端。测试主体仍然是 BlueZ、Linux Bluetooth 子系统、HCI UART 路径、QEMU、虚拟 Controller 和 BabbleSim 组成的完整数据链路。

## 构建与运行

推荐使用 [Docker 测试镜像](../../../../docker/README.zh-CN.md#选择测试套件)，将 pytest 测试路径设为 `tests/integration/qemu/bluez`。

本机 Linux 按父目录 [QEMU / Buildroot README](../README.zh-CN.md) 编译，保留 BabbleSim、Controller 变量及 `BSWORLD_QEMU_BIN`、`BSWORLD_QEMU_KERNEL`、`BSWORLD_QEMU_ROOTFS`，再从仓库根目录运行：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/qemu/bluez -q
```

每个测试都使用独立的仿真标识、HCI socket 和日志目录。QEMU console 保存在 `qemu/bluez/qemu_bluez.log`，HCI BTSnoop 与 BabbleSim RF 跟踪文件则与其他按用例生成的文件一起归档，具体结构见[日志、跟踪文件与抓包](../../../../docs/zh-CH/artifacts.md)。

## 许可证

当前目录中的测试使用 Apache License 2.0。QEMU、Linux、BlueZ、Buildroot 以及 Guest 镜像内的其他软件继续遵循各自许可证。分发二进制前，应检查[第三方声明](../../../../docs/zh-CH/THIRD_PARTY_NOTICES.md)和上一级文档中的[许可证说明](../README.zh-CN.md#许可证)。
