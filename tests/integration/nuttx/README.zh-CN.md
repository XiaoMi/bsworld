# NuttX 测试环境

[English](README.md) | [中文](README.zh-CN.md)

## 准备环境

推荐使用[已发布的 Docker 镜像](../../../docker/README.zh-CN.md#运行测试)，其中已包含 BTSak 和 NimBLE 固件。选择 NuttX 套件时使用 `pytest tests/integration/nuttx`。

以下步骤用于本机 Linux 构建。先按仓库 README 的[本地 Linux 环境搭建](../../../docs/zh-CH/README.md#本地-linux)安装 BSWorld 并设置 `BSWORLD_ROOT`。`NUTTX_DIR`、`NUTTX_APPS_DIR` 用于 BTSak；`NUTTX_NIMBLE_DIR`、`NUTTX_NIMBLE_APPS_DIR` 用于 NimBLE。两个配置分别使用独立源码树，因为 NuttX 的 Make 构建会在源码根目录生成同名 `nuttx`，apps 也有配置相关的编译产物。

本机 Linux 构建先参考 Apache NuttX 官方[安装文档](https://nuttx.apache.org/docs/latest/quickstart/install.html)。Ubuntu 24.04 可安装以下依赖；sim 配置使用主机 GCC/multilib，不需要 ARM 交叉工具链：

```sh
sudo apt-get update
sudo apt-get install -y \
  autoconf automake bison flex gettext texinfo gperf libtool pkg-config \
  build-essential git libncurses-dev xxd genromfs kconfig-frontends \
  libgmp-dev libmpc-dev libmpfr-dev libisl-dev binutils-dev libelf-dev \
  libexpat1-dev gcc-multilib g++-multilib lib32z1-dev picocom u-boot-tools util-linux
source "$BSWORLD_ROOT/.venv/bin/activate"
python -m pip install kconfiglib
```

`sim:nimble` 配置编译 32 位可执行文件，并启用需要链接宿主机 zlib 的 FAT 文件系统支持。安装 `lib32z1-dev` 提供 32 位 zlib 库；仅有 64 位的 `zlib1g-dev` 库无法完成此链接。

然后准备源码工作区；以下版本与 Dockerfile 的验证版本一致。

```sh
export NUTTX_DIR="$HOME/nuttx"
export NUTTX_APPS_DIR="$HOME/nuttx-apps"
export NUTTX_NIMBLE_DIR="$HOME/nuttx-nimble"
export NUTTX_NIMBLE_APPS_DIR="$HOME/nuttx-nimble-apps"

git clone https://github.com/apache/nuttx.git "$NUTTX_DIR"
git clone https://github.com/apache/nuttx-apps.git "$NUTTX_APPS_DIR"
git -C "$NUTTX_DIR" checkout 89c4b8ccaf03f1c6d33624d93ef640d8d176a78d
git -C "$NUTTX_APPS_DIR" checkout 8404450fb89346ed244d1c701e5feb5d19f32271

# Separate source trees retain both in-tree simulator executables.
git clone "$NUTTX_DIR" "$NUTTX_NIMBLE_DIR"
git clone "$NUTTX_APPS_DIR" "$NUTTX_NIMBLE_APPS_DIR"
```

## 编译与运行

先按[README 构建说明](../../../docs/zh-CH/README.md#构建与测试)编译 BabbleSim，并保留 `BSWORLD_ROOT` 和 `BSIM_*` 变量，再按照 [Controller README](../../../components/bt_virt_controller/README.zh-CN.md)编译虚拟 Controller。

| 套件 | 编译说明 | 测试产物变量 |
|---|---|---|
| BTSak | [source/README](btsak/source/README.zh-CN.md) | `BSWORLD_NUTTX_BTSAK_BIN` |
| NimBLE | [source/README](nimble/source/README.zh-CN.md) | `BSWORLD_NUTTX_NIMBLE_BIN` |

两个配置均启用 `CONFIG_SIM_BSIM_TIME=y`，接入 BabbleSim 离散时间。
