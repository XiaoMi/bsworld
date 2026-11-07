# NuttX 测试环境

[English](README.md) | [简体中文](README.zh-CN.md)

本组测试把 NuttX 模拟器固件作为原生 Linux 进程运行，分为 [BTSak](btsak/README.zh-CN.md) 和 [NimBLE](nimble/README.zh-CN.md) 两个套件。每个进程连接自己的 BSController。

## 准备环境

先按[本地 Linux 环境搭建](../../../docs/zh-CH/README.md#本地-linux)准备 BSWorld，按[仓库构建说明](../../../docs/zh-CH/README.md#构建与测试)构建 BabbleSim，再参考 NuttX [安装文档](https://nuttx.apache.org/docs/latest/quickstart/install.html)安装依赖。Ubuntu 24.04：

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

按 [nuttx.Dockerfile](../../../docker/nuttx.Dockerfile) 固定的版本克隆 NuttX 及其 Applications：

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

## 测试套件

| 套件 | 编译说明 | 可执行文件变量 |
|---|---|---|
| BTSak | [BTSak 测试](btsak/README.zh-CN.md#构建与运行) | `BSWORLD_NUTTX_BTSAK_BIN` |
| NimBLE | [NimBLE 测试](nimble/README.zh-CN.md#构建与运行) | `BSWORLD_NUTTX_NIMBLE_BIN` |

运行两个套件：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/nuttx -n auto -q
```

## 添加场景

在 [BTSak](btsak/README.zh-CN.md) 或 [NimBLE](nimble/README.zh-CN.md) 目录中添加异步 pytest 测试，复用套件的设备配置和 [common/](common/) 中的 `create_nuttx_process()`。在 `topo.start()` 前登记进程，启动后按对应 README 发送角色命令，并通过 `wait_log()` 验证输出。Profile 描述程序名称、可执行文件和 shell 行为，创建进程时会把它登记到拓扑中。测试通过 `topo.add_controller()` 添加 Controller，再用 `attach()` 把程序绑定到它。

## 许可证

- 测试代码：Apache License 2.0。
- NuttX 和 NuttX Apps：Apache License 2.0。
- NimBLE 套件使用的 Apache NimBLE：Apache License 2.0。

详见[第三方声明](../../../docs/zh-CH/THIRD_PARTY_NOTICES.md)。
