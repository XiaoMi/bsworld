# QEMU TrouBLE Linux 测试

[English](README.md) | [中文](README.zh-CN.md)

该测试在同步的 AArch64 Linux Guest 中运行 TrouBLE 上游 Battery Service peripheral 示例。QEMU 通过 `/dev/ttyS0` 暴露虚拟 Controller，`btattach` 将其注册为 `hci0`，TrouBLE 再通过 HCI User Channel 独占该设备。

## 验证内容

`test_qemu_trouble_linux_advertises_and_serves_battery_level` 验证以下完整链路：

1. Guest 停止 `bluetoothd`，挂接 `/dev/ttyS0`，并按 HCI User Channel 的要求关闭 `hci0`。
2. 交叉编译的 TrouBLE 示例初始化 Host 协议栈，以固定随机地址广播 `Trouble Example`。
3. 独立 Bumble Central 通过仿真无线链路发现 TrouBLE 并建立连接。
4. Bumble 发现 Battery Service，并读取有效的 Battery Level characteristic 值。

## 构建与运行

推荐使用 [Docker 测试镜像](../../../../docker/README.zh-CN.md#选择测试套件)，将 pytest 测试路径设为 `tests/integration/qemu/trouble`。

本机 Linux 按父目录 [QEMU / Buildroot README](../README.zh-CN.md) 编译，保留 BabbleSim、Controller 变量及 `BSWORLD_QEMU_BIN`、`BSWORLD_QEMU_KERNEL`、`BSWORLD_QEMU_ROOTFS`，再从仓库根目录运行：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/qemu/trouble -q
```

QEMU console 保存在 `qemu/trouble/qemu_trouble.log`，HCI BTSnoop 与 BabbleSim RF 跟踪文件按用例归档，具体结构见[日志、跟踪文件与抓包](../../../../docs/zh-CH/artifacts.md)。

## 许可证

当前目录中的测试使用 Apache License 2.0。TrouBLE 使用 MIT 或 Apache License 2.0；QEMU、Linux、BlueZ、Buildroot 和 Guest 中的其他软件继续遵循各自许可证。分发二进制前，应检查仓库[第三方声明](../../../../docs/zh-CH/THIRD_PARTY_NOTICES.md)和上一级文档中的[许可证说明](../README.zh-CN.md#许可证)。
