# NuttX 测试环境

[English](README.md) | [简体中文](README.zh-CN.md)

## 准备环境

推荐使用[已发布的 Docker 镜像](../../../docker/README.zh-CN.md#运行测试)，其中已包含 BTSak 和 NimBLE 固件。选择 NuttX 套件时使用 `pytest tests/integration/nuttx`。

以下步骤用于本机 Linux 构建。先按[本地 Linux 环境搭建](../../../docs/zh-CH/README.md#本地-linux)准备 BSWorld，再参考 NuttX [安装文档](https://nuttx.apache.org/docs/latest/quickstart/install.html)安装依赖。Ubuntu 24.04：

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

设置工作区路径，BTSak 和 NimBLE 各用一套 NuttX 和 Applications 源码树：

```sh
export NUTTX_DIR="$HOME/nuttx"
export NUTTX_APPS_DIR="$HOME/nuttx-apps"
export NUTTX_NIMBLE_DIR="$HOME/nuttx-nimble"
export NUTTX_NIMBLE_APPS_DIR="$HOME/nuttx-nimble-apps"
```

按 Dockerfile 固定的版本克隆 NuttX 及其 Applications：

```sh
git clone https://github.com/apache/nuttx.git "$NUTTX_DIR"
git clone https://github.com/apache/nuttx-apps.git "$NUTTX_APPS_DIR"
git -C "$NUTTX_DIR" checkout 47180bf5ea00a94b41cf2083b8fed123199e5ce9
git -C "$NUTTX_APPS_DIR" checkout 95833033f9f5c045309c1c48d2af994c78791f6b
```

为 NimBLE 复制两棵源码树：

```sh
git clone "$NUTTX_DIR" "$NUTTX_NIMBLE_DIR"
git clone "$NUTTX_APPS_DIR" "$NUTTX_NIMBLE_APPS_DIR"
```

## 编译与运行

先按[仓库构建说明](../../../docs/zh-CH/README.md#构建与测试)构建 BabbleSim，并构建[虚拟 Controller](../../../docs/zh-CH/controllers.md#编译-zephyr-controller)。

| 套件 | 编译说明 | 测试产物变量 |
|---|---|---|
| BTSak | [source/README](btsak/source/README.zh-CN.md) | `BSWORLD_NUTTX_BTSAK_BIN` |
| NimBLE | [source/README](nimble/source/README.zh-CN.md) | `BSWORLD_NUTTX_NIMBLE_BIN` |

## 添加场景

在 [BTSak](btsak/README.zh-CN.md) 或 [NimBLE](nimble/README.zh-CN.md) 目录中添加异步 pytest 测试，复用套件的设备配置和 [common/](common/) 中的 `load_nuttx_profile()`。在 `topo.start()` 前登记进程，启动后按对应 README 发送角色命令，并通过 `wait_log()` 验证输出。
