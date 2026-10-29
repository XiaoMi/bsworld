# QEMU Linux 测试环境

[English](README.md) | [简体中文](README.zh-CN.md)

在单 vCPU 的 AArch64 QEMU `virt` 机器中运行 Buildroot 构建的 Linux Guest，Guest 内包含 BlueZ 和 Apache NimBLE。Guest 的 HCI UART 连接到虚拟 Controller，QEMU 的 `icount` 时钟与 BabbleSim 同步。

## 已验证版本

| 组件 | 提交或版本 |
|---|---|
| BSWorld QEMU fork | `b2d1776bca209773c4a023e4decb785f7dc3cfb4` |
| Buildroot | `1ad0796f2b40eb8dadc43e95dd1d56708b191375` |
| Linux | `6.18.7` |
| BlueZ | `5.86` |
| Apache NimBLE | `1e8ed60276f35a80ed4d4b4f8bb9d9c6fee53845` |

## 准备环境

[已发布的 Docker 镜像](../../../docker/README.zh-CN.md#运行测试)已包含 QEMU、kernel 和 rootfs，使用 `pytest tests/integration/qemu` 运行。以下步骤用于本机 Linux 构建，先按[仓库构建说明](../../../docs/zh-CH/README.md#构建与测试)构建 BabbleSim 和[虚拟 Controller](../../../docs/zh-CH/controllers.md#编译-zephyr-controller)。

安装系统依赖（Ubuntu 24.04）：

```sh
sudo apt-get update
sudo apt-get install -y \
  bash bc binutils bzip2 build-essential cpio debianutils diffutils file findutils \
  gawk gcc g++ git gzip libfdt-dev libffi-dev libglib2.0-dev libncurses-dev \
  libpixman-1-dev libssl-dev make ninja-build patch perl pkg-config \
  python3.12 python3.12-venv python3-pip rsync sed tar unzip wget xz-utils zip zlib1g-dev
```

安装 QEMU 构建所需的 Python 工具：

```sh
source "$BSWORLD_ROOT/.venv/bin/activate"
python -m pip install wheel 'meson>=1.6.0' 'pycotap>=1.1.0'
```

获取 QEMU fork 和 Buildroot：

```sh
export QEMU_DIR="$HOME/qemu"
export BUILDROOT_DIR="$HOME/buildroot"
git clone https://github.com/LingaoM/qemu.git "$QEMU_DIR"
git clone https://gitlab.com/buildroot.org/buildroot.git "$BUILDROOT_DIR"
git -C "$QEMU_DIR" checkout b2d1776bca209773c4a023e4decb785f7dc3cfb4
git -C "$BUILDROOT_DIR" checkout 1ad0796f2b40eb8dadc43e95dd1d56708b191375
```

## 编译 QEMU 和 Linux Guest

编译 QEMU：

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
```

以普通用户身份构建 kernel 和 rootfs：

```sh
make -C "$BUILDROOT_DIR" \
  O="$BUILDROOT_OUTPUT" \
  BR2_EXTERNAL="$BSWORLD_ROOT/tests/integration/qemu/source" \
  bsworld_qemu_linux_defconfig
make -C "$BUILDROOT_DIR" O="$BUILDROOT_OUTPUT" -j"$(nproc)"
```

导出产物，BlueZ 和 NimBLE 共用：

```sh
export BSWORLD_QEMU_BIN="$QEMU_BUILD_DIR/qemu-system-aarch64"
export BSWORLD_QEMU_KERNEL="$BUILDROOT_OUTPUT/images/Image"
export BSWORLD_QEMU_ROOTFS="$BUILDROOT_OUTPUT/images/rootfs.cpio"
```

检查产物：

```sh
test -x "$BSWORLD_QEMU_BIN"
test -f "$BSWORLD_QEMU_KERNEL"
test -f "$BSWORLD_QEMU_ROOTFS"
test -x "$BUILDROOT_OUTPUT/target/usr/bin/btattach"
test -x "$BUILDROOT_OUTPUT/target/usr/bin/nimble-linux"
```

## 运行时数据路径

QEMU 把虚拟 Controller 的 socket 映射为 Guest 的 `/dev/ttyS0`，测试用 `btattach -B /dev/ttyS0 -P h4 -S 1000000 -N` 注册 `hci0`。BlueZ 通过 `bluetoothd` 使用 `hci0`；NimBLE 停止 `bluetoothd` 后通过 HCI User Channel 独占 `hci0`。

`-icount` 传入 BabbleSim 仿真标识，`bsim-mro=1000` 将每段执行区间限制在 1000 µs 以内。QEMU 的 record/replay 不能与 BabbleSim 同步同时使用。

## 运行测试

见 [BlueZ](bluez/README.zh-CN.md) 和 [NimBLE](nimble/README.zh-CN.md)。

## 重建与检查配置

```sh
make -C "$BUILDROOT_DIR" O="$BUILDROOT_OUTPUT" show-info
```

修改 defconfig、Linux 配置片段或工具链后，换一个新的 `BUILDROOT_OUTPUT` 重新构建，并更新导出的变量。

## 添加场景

在 [BlueZ](bluez/README.zh-CN.md) 或 [NimBLE](nimble/README.zh-CN.md) 目录中添加异步 pytest 测试。`topo.start()` 前调用 [common.py](common.py) 中的 `load_qemu_guest()`，启动后调用 `login_qemu_guest()`，再通过 `guest.shell` 发送命令并用 `wait_log()` 验证。

## 许可证

本仓库的 external 配置和测试使用 Apache License 2.0。QEMU、Linux 及 Guest 软件保持各自许可证，见[第三方声明](../../../docs/zh-CH/THIRD_PARTY_NOTICES.md)。生成构建产物的许可证信息：

```sh
make -C "$BUILDROOT_DIR" O="$BUILDROOT_OUTPUT" legal-info
```
