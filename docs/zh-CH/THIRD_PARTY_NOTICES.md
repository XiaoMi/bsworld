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
- 用途：[NuttX 测试程序](../../tests/integration/uml/README.zh-CN.md)
- 许可证：Apache License 2.0

NuttX 源码通过外部依赖获取，不复制到本仓库。

## Apache NimBLE

- 源码：https://github.com/apache/mynewt-nimble
- 用途：[NuttX](../../tests/integration/uml/README.zh-CN.md) 和 [Linux Guest 测试](../../tests/integration/uml/nimble/README.zh-CN.md)使用的 Bluetooth Host 协议栈
- 许可证：Apache License 2.0

NimBLE 源码通过外部依赖获取，不复制到本仓库。

## Bumble

- 源码：https://github.com/google/bumble
- 用途：[Bumble 对测 Host](../../tests/integration/uml/bumble_peer.py)和 [BSController 示例](../../components/bscontroller/samples/README.zh-CN.md)使用的 Python Bluetooth Host 协议栈
- 许可证：Apache License 2.0

BSWorld 使用公开的 Python 和 HCI transport 接口，不修改 Bumble Host 协议栈。

## Zephyr Project

- 源码：https://github.com/zephyrproject-rtos/zephyr
- 用途：[Zephyr Bluetooth shell 测试](../../tests/integration/uml/README.zh-CN.md)
- 许可证：Apache License 2.0

BSWorld 使用自己的 [native_sim 配置](../../tests/integration/uml/zephyr/prj.conf)，从外部 Zephyr 源码构建 Bluetooth shell。

## Linux

- 源码：https://github.com/torvalds/linux
- 用途：以 User Mode Linux 形式运行 [Linux Guest](../../tests/integration/uml/README.zh-CN.md)，并通过 time-travel 与 BabbleSim 同步时间
- 许可证：GNU General Public License version 2；个别文件可能使用兼容许可证，以上游 `COPYING` 为准

Guest 内核使用未修改的上游提交和 BSWorld 的[配置片段](../../src/bsworld/uml/linux.fragment)编译。

## Moby seccomp 配置

[UML 容器配置](../../docker/uml-seccomp.json)基于 Moby `seccomp/v0.2.4`，增加了 `personality(ADDR_NO_RANDOMIZE)` 的许可。[上游配置](https://github.com/moby/profiles/blob/245180c51918481c0525424b3ee025d2b435d46c/seccomp/default.json)

## BlueZ

- 源码：https://git.kernel.org/pub/scm/bluetooth/bluez.git
- 用途：[Linux Guest 测试](../../tests/integration/uml/bluez/README.zh-CN.md)使用的 Bluetooth Host 协议栈和工具
- 许可证：不同组件分别使用 GNU General Public License version 2 or later 和 GNU Lesser General Public License version 2.1 or later

UML Guest 根文件系统从 Ubuntu 24.04 i386 软件包解包得到。各包的许可证保留在 Guest 的 `/usr/share/doc/<package>/copyright`。
