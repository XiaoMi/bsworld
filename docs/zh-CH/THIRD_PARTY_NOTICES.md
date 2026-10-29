# 第三方声明

[English](../../THIRD_PARTY_NOTICES.md) | [简体中文](THIRD_PARTY_NOTICES.md)

BSWorld 使用的第三方软件遵循各自的许可证。构建和配置细节见下方链接的文档；组件自身的归属说明见 [BSController NOTICE](../../components/bscontroller/NOTICE)。

## BabbleSim

- 源码：https://github.com/BabbleSim
- 用途：[共用 RF 仿真环境](README.md#构建与测试)
- 许可证：Apache License 2.0；个别组件另有声明时，以组件自己的许可证为准

BSWorld 不修改 BabbleSim 上游源码，各组件保留各自的许可证文件。

## Apache NuttX

- 源码：https://github.com/apache/nuttx
- 用途：[NuttX 测试程序](../../tests/integration/nuttx/README.zh-CN.md)
- 许可证：Apache License 2.0

NuttX 源码通过外部依赖获取，不复制到本仓库。

## Apache NimBLE

- 源码：https://github.com/apache/mynewt-nimble
- 用途：[NuttX](../../tests/integration/nuttx/README.zh-CN.md) 和 [Linux Guest 测试](../../tests/integration/qemu/nimble/README.zh-CN.md)使用的 Bluetooth Host 协议栈
- 许可证：Apache License 2.0

NimBLE 源码通过外部依赖获取，不复制到本仓库。

## TrouBLE

- 源码：https://github.com/embassy-rs/trouble
- 用途：[原生 Linux 测试](../../tests/integration/trouble/README.zh-CN.md)使用的 Rust Bluetooth Host 协议栈和 Battery Service 应用
- 许可证：MIT 或 Apache License 2.0

上游协议栈和应用作为外部 Cargo 依赖使用，保持原样。BSWorld 维护原生传输与执行器，适配层保留原有的 MIT OR Apache-2.0 许可。

## Bumble

- 源码：https://github.com/google/bumble
- 用途：[Bumble 测试](../../tests/integration/bumble/README.zh-CN.md)使用的 Python Bluetooth Host 协议栈
- 许可证：Apache License 2.0

BSWorld 使用公开的 Python 和 HCI transport 接口，不修改 Bumble Host 协议栈。

## Zephyr Project

- 源码：https://github.com/zephyrproject-rtos/zephyr
- 用途：[虚拟 Controller](controllers.md#使用-zephyr-controller) 和 [Bluetooth、Mesh shell](../../tests/integration/zephyr/shell/README.zh-CN.md)
- 许可证：Apache License 2.0

虚拟 Controller 使用 Zephyr 的 `samples/bluetooth/hci_virt`，BSWorld 只提供 Kconfig overlay；shell 应用使用外部 Zephyr 源码。

## QEMU

- 上游源码：https://gitlab.com/qemu-project/qemu
- BSWorld fork：https://github.com/LingaoM/qemu
- 用途：运行 [Linux Guest](../../tests/integration/qemu/README.zh-CN.md)，并将指令计数时间同步到 BabbleSim
- 许可证：QEMU 整体使用 GNU General Public License version 2；个别文件可能使用兼容许可证

本仓库不包含 QEMU 源码，使用的是上方链接的修改版。分发其可执行程序时，仍需遵循适用的对应源码和声明义务。

## Buildroot

- 源码：https://gitlab.com/buildroot.org/buildroot
- 用途：构建 [Linux Guest 环境](../../tests/integration/qemu/README.zh-CN.md)
- 许可证：GNU General Public License version 2 or later；例外情况和单个文件的许可证以上游 `COPYING` 为准

本仓库仅维护 BSWorld 的 [external 配置](../../tests/integration/qemu/source/README.zh-CN.md)。Buildroot 的许可证不能替代所组装软件各自的许可证。分发 Guest 镜像前，应检查实际构建的 `legal-info` 输出中的软件包许可证、声明和对应源码材料。

## BlueZ

- 源码：https://git.kernel.org/pub/scm/bluetooth/bluez.git
- 用途：[Linux Guest 测试](../../tests/integration/qemu/bluez/README.zh-CN.md)使用的 Bluetooth Host 协议栈和工具
- 许可证：不同组件分别使用 GNU General Public License version 2 or later 和 GNU Lesser General Public License version 2.1 or later

Guest 还包含 D-Bus、BusyBox、glibc 等依赖，它们的许可证和声明记录在对应的 Buildroot `legal-info` 输出中。
