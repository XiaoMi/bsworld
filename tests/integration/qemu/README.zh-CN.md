# QEMU Linux 测试环境

[English](README.md) | [中文](README.zh-CN.md)

BSWorld 可以将完整的 Linux Guest 作为 BabbleSim 时间域中的一个参与者运行。当前验证环境使用单 vCPU 的 AArch64 QEMU `virt` 机器，由 Buildroot 生成 Linux kernel 和 initramfs，并在同一 Guest 镜像中提供 BlueZ、Apache NimBLE 与 TrouBLE Host 协议栈。QEMU 通过 Host Unix socket 将 Guest 的 HCI UART 连接到 BSWorld 虚拟 Controller，同时将基于指令计数的虚拟时钟与 BabbleSim 同步。

该接入方式面向不适合迁移到 RTOS 的 Linux Bluetooth 软件。Buildroot 只负责构建 Guest kernel、根文件系统和交叉工具链。

## 已验证版本

| 组件 | 提交或版本 | 用途 |
|---|---|---|
| BSWorld QEMU fork | `b2d1776bca209773c4a023e4decb785f7dc3cfb4` | 将 TCG `icount` 接入 BabbleSim，并确定性地编排 vCPU 与 Main Loop 的执行顺序 |
| Buildroot | `1ad0796f2b40eb8dadc43e95dd1d56708b191375` | 构建 AArch64 Linux Guest |
| Linux | `6.18.7` | 由仓库内 defconfig 选择的 Guest kernel |
| BlueZ | `5.86` | Guest 内的 Bluetooth Host 协议栈和命令行工具 |
| Apache NimBLE | `1e8ed60276f35a80ed4d4b4f8bb9d9c6fee53845` | Guest 内的 Linux Host 示例 |
| TrouBLE | `730198ecf94ee6953163b7485cf902d5f9d45dbd` | Guest 内的 Rust Linux Host 示例 |

Linux 与 BlueZ 版本由经过验证的 Buildroot 提交和 BSWorld defconfig 共同确定。复现当前测试时应使用表中的提交；单独升级任一外部仓库都可能改变构建选项、软件版本或运行行为。

## 准备环境

推荐使用[已发布的 Docker 镜像](../../../docker/README.zh-CN.md#运行测试)，其中已包含 QEMU、kernel 和 rootfs。选择 QEMU 套件时使用 `pytest tests/integration/qemu`。

以下步骤用于本机 Linux 构建。先按[README 构建说明](../../../docs/zh-CH/README.md#构建与测试)编译 BabbleSim，并按 [Controller README](../../../components/bt_virt_controller/README.zh-CN.md)编译虚拟 Controller，保留 `BSWORLD_ROOT`、`BSIM_OUT_PATH` 和 `BSIM_COMPONENTS_PATH`。

本机 Linux 构建可按官方 [QEMU 构建说明](https://www.qemu.org/docs/master/devel/build-system.html)及 [Buildroot 手册](https://buildroot.org/downloads/manual/manual.html)准备环境。必须使用支持 BabbleSim 的 QEMU fork。环境搭建示例：

```sh
sudo apt-get update
sudo apt-get install -y \
  bash bc binutils bzip2 build-essential cpio debianutils diffutils file findutils \
  gawk gcc g++ git gzip libfdt-dev libffi-dev libglib2.0-dev libncurses-dev \
  libpixman-1-dev libssl-dev make ninja-build patch perl pkg-config \
  python3.12 python3.12-venv python3-pip rsync sed tar unzip wget xz-utils zip zlib1g-dev
source "$BSWORLD_ROOT/.venv/bin/activate"
python -m pip install wheel 'meson>=1.6.0' 'pycotap>=1.1.0'

export QEMU_DIR="$HOME/qemu"
export BUILDROOT_DIR="$HOME/buildroot"
git clone https://github.com/LingaoM/qemu.git "$QEMU_DIR"
git clone https://gitlab.com/buildroot.org/buildroot.git "$BUILDROOT_DIR"
git -C "$QEMU_DIR" checkout b2d1776bca209773c4a023e4decb785f7dc3cfb4
git -C "$BUILDROOT_DIR" checkout 1ad0796f2b40eb8dadc43e95dd1d56708b191375
```

## 编译 QEMU 和 Linux Guest

直接调用 QEMU 的 configure / ninja 以及 Buildroot 的 make。以普通用户身份运行 Buildroot。

```sh
# Use your local QEMU and Buildroot workspace paths.
source "$BSWORLD_ROOT/.venv/bin/activate"
export QEMU_BUILD_DIR="$QEMU_DIR/build/bsworld"
export BUILDROOT_OUTPUT="$BUILDROOT_DIR/output-bsworld-linux"
mkdir -p "$QEMU_BUILD_DIR"
cd "$QEMU_BUILD_DIR"
"$QEMU_DIR/configure" \
  --target-list=aarch64-softmmu \
  --enable-bsim \
  --bsim-components-path="$BSIM_COMPONENTS_PATH" \
  --bsim-out-path="$BSIM_OUT_PATH"
ninja -C "$QEMU_BUILD_DIR" -j"$(nproc)" qemu-system-aarch64

make -C "$BUILDROOT_DIR" \
  O="$BUILDROOT_OUTPUT" \
  BR2_EXTERNAL="$BSWORLD_ROOT/tests/integration/qemu/source" \
  bsworld_qemu_linux_defconfig
make -C "$BUILDROOT_DIR" O="$BUILDROOT_OUTPUT" -j"$(nproc)"

export BSWORLD_QEMU_BIN="$QEMU_BUILD_DIR/qemu-system-aarch64"
export BSWORLD_QEMU_KERNEL="$BUILDROOT_OUTPUT/images/Image"
export BSWORLD_QEMU_ROOTFS="$BUILDROOT_OUTPUT/images/rootfs.cpio"
```

BlueZ、NimBLE、TrouBLE 共用该 QEMU、kernel 和 rootfs。

该 QEMU fork 的 `--enable-bsim` 将 BabbleSim 通信静态库链接到程序。Buildroot external tree 位于 `tests/integration/qemu/source`，包括 guest defconfig、Linux 配置片段以及 NimBLE 和 TrouBLE 软件包。首次编译会下载并构建交叉工具链、Linux 和 guest 软件，耗时明显长于增量编译。

rootfs 是 initramfs，每次测试启动 guest 时重新展开，确保测试从同一镜像状态开始。产物可直接检查：

```sh
test -x "$BSWORLD_QEMU_BIN"
test -f "$BSWORLD_QEMU_KERNEL"
test -f "$BSWORLD_QEMU_ROOTFS"
test -x "$BUILDROOT_OUTPUT/target/usr/bin/btattach"
test -x "$BUILDROOT_OUTPUT/target/usr/bin/nimble-linux"
test -x "$BUILDROOT_OUTPUT/target/usr/bin/trouble-ble-bas-peripheral"
```

## 运行时数据路径

运行时，BSWorld 先创建虚拟 Controller 的 Unix socket，再以 socket-backed character device 启动 QEMU。QEMU 将该字符设备映射为 Guest 中的第一个 PCI 串口 `/dev/ttyS0`。测试随后执行 `btattach -B /dev/ttyS0 -P h4 -S 1000000 -N`，把 H4 串口注册为 Linux Bluetooth 子系统中的 `hci0`。BlueZ 测试由 `bluetoothd` 和 `bluetoothctl` 通过标准 Linux Bluetooth kernel 接口控制 `hci0`；NimBLE 和 TrouBLE 测试停止 `bluetoothd`，再通过 HCI User Channel 独占同一个 `hci0`。TrouBLE 的 Linux transport 要求调用方先关闭设备，因此测试还会执行 `hciconfig hci0 down`。

QEMU 启动命令通过 `-icount` 传入 BabbleSim 仿真标识。`bsim-mro=1000` 将每段虚拟执行区间限制在 1000 微秒以内；如果定时器更早到期，则使用更短的区间。到达同步边界后，QEMU 向 PHY 提交目标时间，并保持 vCPU 等待，直到收到 PHY 确认。因此，Guest 虚拟时钟、定时器中断和 sleep 与虚拟 Controller、PHY、pytest 及其他仿真设备沿同一条 BabbleSim 时间轴运行。

为了避免宿主机线程调度改变 Guest 执行与宿主机 I/O 派发的先后关系，QEMU fork 使用一个由 vCPU 线程和 Main Loop 共用的公平执行顺序锁。该机制从 QEMU record/replay 原有的锁中抽取而来，并保留其现有接口；BabbleSim 时间同步与 record/replay 不能同时启用。

vCPU 等待 PHY 确认时会释放执行顺序锁，Main Loop 继续使用阻塞式轮询处理宿主机 I/O。PHY 发出确认后会阻塞等待该设备的下一条请求，因此仿真不会再次向前推进。QEMU 随后改用非阻塞轮询，直到已经到达本机的 fd 事件全部处理完毕，再提交目标虚拟时间并恢复 vCPU 执行。

## 运行测试

pytest 命令、必需的产物环境变量、已验证场景和日志说明分别见 [BlueZ 测试](bluez/README.zh-CN.md)、[NimBLE Linux 测试](nimble/README.zh-CN.md)与 [TrouBLE Linux 测试](trouble/README.zh-CN.md)。

## 重建与检查配置

用以下命令检查当前 Buildroot 配置：

```sh
make -C "$BUILDROOT_DIR" O="$BUILDROOT_OUTPUT" show-info
```

改变 defconfig 或 Linux fragment 后，选择一个新的 `BUILDROOT_OUTPUT`，重新执行上述 defconfig 和 make 命令，并更新 kernel/rootfs 环境变量。

## 许可证

本仓库的 external 配置和测试使用 Apache License 2.0。QEMU、Linux 及 guest 软件保持各自许可证。需要分发构建产物时，可针对实际输出执行 Buildroot 自带的 `legal-info`，参见[第三方声明](../../../docs/zh-CH/THIRD_PARTY_NOTICES.md)。

```sh
make -C "$BUILDROOT_DIR" O="$BUILDROOT_OUTPUT" legal-info
```
