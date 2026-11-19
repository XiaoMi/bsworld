# User Mode Linux 集成测试

[English](README.md) | [简体中文](README.zh-CN.md)

64 位 BSWorld 运行 32 位 User Mode Linux（UML）Guest。各 Guest 的 Host 连接各自的 [BSController](../../../docs/zh-CH/controllers.md)，Bumble 在 pytest 中作为对端。同一个拓扑可以启动多个 Guest，共用仿真时钟。

## 准备环境

[Linux 测试镜像](../../../docker/README.zh-CN.md#构建)包含 UML 内核、种子库、BabbleSim 和 i386 Guest 根文件系统。在仓库根目录构建镜像并运行测试。

```sh
docker buildx bake -f docker/docker-bake.hcl --load linux
docker run --rm --security-opt seccomp=docker/uml-seccomp.json \
  bsworld-linux:dev pytest tests/integration/uml -n auto -q
```

本地编译先准备 [BSWorld 和 BabbleSim](../../../docs/zh-CH/README.md#本地-linux)。源码版本和编译命令见 [Dockerfile](../../../docker/linux.Dockerfile) 中的 `uml`、`uml-rootfs`、`zephyr-uml`、`nuttx-uml` 和 `nimble-linux` 阶段。使用 `ARCH=um SUBARCH=i386` 和[配置片段](../../../src/bsworld/uml/linux.fragment)编译内核。[种子库](../../../src/bsworld/uml/getrandom.c)使用 `cc -m32 -shared -fPIC -O2` 编译。[根文件系统脚本](../../../docker/uml-rootfs.sh)使用 `dpkg-deb -x` 解包 i386 软件包，其中包括 BlueZ 的 `btattach`。Dockerfile 随后安装 Guest 程序和可执行的 `/init`。

| 环境变量 | BSWorld 运行端的路径 |
|---|---|
| `BSWORLD_UML_KERNEL` | 可执行的 i386 UML 内核 |
| `BSWORLD_UML_GETRANDOM` | UML 进程加载的 i386 种子库 |
| `BSWORLD_UML_ROOTFS` | 包含 `/init`、i386 程序和库的 Guest 根目录 |

设置这些变量后，在 BSWorld 的 Python 环境中运行 `pytest tests/integration/uml -n auto -q`。

## 测试套件

| Host | 场景 | Guest 内可执行文件 |
|---|---|---|
| [BlueZ](bluez/README.zh-CN.md) | Controller 初始化、双 Guest 并发广播、扫描和连接 | `/usr/bin/bluetoothctl` |
| [Zephyr native_sim](zephyr/) | 广播、扫描、连接、GATT、配对及隐私、链路过程、周期广播与同步转移 | `/opt/bsworld/zephyr-shell`，由 `BSWORLD_UML_ZEPHYR_BIN` 指定 |
| [NuttX sim BTSak](nuttx/test_btsak.py) | 广播、扫描、干扰与恢复 | `/opt/bsworld/nuttx-btsak`，由 `BSWORLD_UML_NUTTX_BIN` 指定 |
| [NuttX sim NimBLE](nuttx/test_nimble.py) | 两种角色的连接、Battery Service 发现和通知 | `/opt/bsworld/nuttx-nimble`，由 `BSWORLD_UML_NUTTX_NIMBLE_BIN` 指定 |
| [NimBLE Linux](nimble/README.zh-CN.md) | 广播、发现和连接 | `/opt/bsworld/nimble-linux`，由 `BSWORLD_UML_NIMBLE_BIN` 指定 |

可执行文件变量填写 **Guest 内的绝对路径**。例如，在 `$BSWORLD_UML_ROOTFS/opt/bsworld/my-shell` 放置程序后，设置 `BSWORLD_UML_ZEPHYR_BIN=/opt/bsworld/my-shell`。

## Guest 运行机制

[启动脚本](../../../src/bsworld/uml/init.sh)在只读的 Guest 根文件系统中挂载私有的运行目录，并启动 shell。每个 Guest 的 `/dev/ttyS0` 通过独立的 H4 连接接入对应 Controller。测试使用 `btattach -B /dev/ttyS0 -P h4` 将它注册为 `hci0`。控制台使用 UML 进程的标准输入和标准输出。

测试事件循环通过 UML `time-travel=ext` 为各 Guest 推进时钟。Guest 运行期间仿真时间保持不变，等待中的串口和控制台输入由 Guest 的周期 tick 处理。只有 Controller、测试时钟和场景中的 RF 干扰设备分配 BabbleSim 设备编号。

## 添加场景

使用 [common.py](common.py) 在 `await topo.start()` 前添加 Controller，再在 `AsyncExitStack` 中调用 `open_uml_guest()`。各 Guest 使用不同的标签和 Controller。通过 `guest.shell.run()` 发命令，用 `guest.wait_log()` 检查控制台输出。退出 stack 时，先关闭 Guest，再由测试框架清理拓扑。

启动器和时钟调度代码位于 [bsworld.uml](../../../src/bsworld/uml)。运行 `pytest tests/unit/uml -q` 可检查其[单元测试](../../unit/uml)，无需构建 UML 内核和 BabbleSim。

## 许可证

测试与 BSWorld 的 UML 适配代码使用 Apache License 2.0。Linux 和 Guest 程序保留各自的上游许可证。[第三方声明](../../../docs/zh-CH/THIRD_PARTY_NOTICES.md)
