# QEMU NimBLE Linux 测试

[English](README.md) | [中文](README.zh-CN.md)

该测试在 AArch64 Linux Guest 中运行 Apache NimBLE 上游自带的 Linux Host 示例。QEMU 将虚拟 Controller 的 Unix-socket HCI 数据映射为 `/dev/ttyS0`，`btattach` 将 H4 串口注册为 `hci0`，NimBLE Linux port 再通过 HCI User Channel 独占该设备。测试不修改 NimBLE 源码，也不引入 BSWorld 专用 transport。

## 测试场景

`test_qemu_nimble_linux_advertises_and_accepts_connection` 验证以下完整链路：

1. NimBLE 完成 Host 与 Controller 初始化并开始可连接广播。
2. 独立 Bumble Central 从仿真无线链路发现广播，并校验公共地址 `AA:BB:CC:DD:EE:FE` 和完整设备名 `nimble`。
3. Bumble 与 NimBLE 建立连接，确认连接句柄有效后正常断开。

## 构建与运行

推荐使用 [Docker 测试镜像](../../../../docker/README.zh-CN.md#选择测试套件)，将 pytest 测试路径设为 `tests/integration/qemu/nimble`。

本机 Linux 按父目录 [QEMU / Buildroot README](../README.zh-CN.md) 编译，保留 BabbleSim、Controller 变量及 `BSWORLD_QEMU_BIN`、`BSWORLD_QEMU_KERNEL`、`BSWORLD_QEMU_ROOTFS`，再从仓库根目录运行：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/qemu/nimble -q
```

QEMU console 保存在 `qemu/nimble/qemu_nimble.log`，HCI BTSnoop 与 BabbleSim RF 跟踪文件按用例归档，具体结构见[日志、跟踪文件与抓包](../../../../docs/zh-CH/artifacts.md)。

## 许可证

当前目录中的测试使用 Apache License 2.0。Apache NimBLE 使用 Apache License 2.0；QEMU、Linux、Buildroot 和 Guest 镜像内的其他软件继续遵循各自许可证。分发二进制前，应检查[第三方声明](../../../../docs/zh-CH/THIRD_PARTY_NOTICES.md)和上一级文档中的[许可证说明](../README.zh-CN.md#许可证)。
