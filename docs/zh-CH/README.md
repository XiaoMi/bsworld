<div align="center">

<img src="../images/bsworld-logo.svg" alt="BSWorld" width="200">

[English](../../README.md) | 简体中文

[![CI](https://github.com/XiaoMi/bsworld/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/XiaoMi/bsworld/actions/workflows/ci.yml) [![最新版本](https://img.shields.io/github/v/release/XiaoMi/bsworld?label=release&filter=v*)](https://github.com/XiaoMi/bsworld/releases/latest) [![PyPI bscontroller](https://img.shields.io/pypi/v/bscontroller?label=pypi%20bscontroller)](https://pypi.org/project/bscontroller/) [![许可证](https://img.shields.io/github/license/XiaoMi/bsworld)](../../LICENSE)

</div>

BSWorld 是一个在 Linux 上测试 Bluetooth Host 及其业务程序的确定性仿真框架。pytest 场景负责定义设备拓扑和预期行为；BSWorld 让所有参与者共享 BabbleSim 无线环境与仿真时钟，并归档日志和协议跟踪文件。

使用 BSWorld 可以：

- 测试 Zephyr、NuttX、BlueZ、NimBLE、Bumble 等开源 Host 协议栈，无需修改其源码。
- 无需硬件，Host 通过标准 H4 HCI 连接 BSController。
- 保留业务程序日志、HCI BTSnoop、PHY 日志和 Ellisys 跟踪文件定位故障。
- 每个场景里可以同时仿真多台设备，同一场景重复运行，日志及时序均保持一致，不受主机负载和调度影响。
- 所有设备共享离散仿真时钟；场景之间相互隔离，结合 pytest-xdist 并行运行，最高可达百倍加速。
- 注入路径衰减、干扰、WLAN 活动和录制的无线信号。


## 快速开始

BSWorld 运行于 Linux/x86_64。发布的 Docker 镜像内置 BabbleSim、32 位 UML 内核、Guest 根文件系统及对测所需的可执行文件。本地开发见[本地 Linux](#本地-linux)。

### Docker

安装 [Docker Engine](https://docs.docker.com/engine/install/ubuntu/)，然后从仓库根目录运行 UML 集成测试。

```sh
sudo docker pull ghcr.io/xiaomi/bsworld-linux:latest
mkdir -p .cache/docker-results
# 已发布镜像以 UID/GID 1000 运行。
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  --security-opt seccomp=docker/uml-seccomp.json \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  ghcr.io/xiaomi/bsworld-linux:latest
```

测试结果写入 `.cache/docker-results`，包括 `pytest.xml`、进程日志、HCI 抓包和 RF 跟踪文件。选择套件、镜像版本和本地构建方法见 [Docker 使用说明](../../docker/README.zh-CN.md)。

### 本地 Linux

本地环境用自己的工具编译固件、运行测试，修改后可以增量编译。

开发和测试需要 Python 3.12 或更高版本。在 Ubuntu 24.04 上安装所需工具：

```sh
sudo apt-get update
sudo apt-get install -y \
  bash ca-certificates curl git repo file make binutils build-essential \
  python3.12 python3.12-dev python3.12-venv python3-pip \
  gcc-multilib g++-multilib coreutils findutils grep sed
```

获取并安装 BSWorld：

```sh
git clone https://github.com/XiaoMi/bsworld.git
cd bsworld
export BSWORLD_ROOT="$PWD"
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e components/bscontroller -e '.[test]'
```

运行开发检查：

```sh
ruff check .
ruff format --check .
pytest tests/unit -q
```

#### 构建与测试

获取 `components/babblesim.xml` 指定的 BabbleSim 版本：

```sh
mkdir -p "$BSWORLD_ROOT/.cache/babblesim"
cd "$BSWORLD_ROOT/.cache/babblesim"
repo init --standalone-manifest --depth=1 -u "file://$BSWORLD_ROOT/components/babblesim.xml"
repo sync -c --no-tags
```

设置输出路径并构建全部组件：

```sh
export BSIM_OUT_PATH="$PWD/build"
export BSIM_COMPONENTS_PATH="$PWD/components"
export BSIM_BUILD_FAIL_ASAP=1
make -j"$(nproc)" everything
```

每个新开的、用于构建或运行测试的 shell 都要重新导出 `BSWORLD_ROOT`、`BSIM_OUT_PATH` 和 `BSIM_COMPONENTS_PATH`；没有 `BSIM_OUT_PATH` 时，集成测试会报环境配置错误并停止。

运行 BabbleSim 测试。

```sh
cd "$BSWORLD_ROOT"
pytest tests/integration/babblesim -n auto
```

准备 32 位 Guest 后可运行全部集成套件，参见 [UML 测试环境](../../tests/integration/uml/README.zh-CN.md)。

## 支持的集成环境

各 Host 运行在 UML Guest 中，与同一仿真拓扑中的 [Bumble 对测 Host](../../tests/integration/uml/bumble_peer.py) 交互。

| Guest 中的 Host | 测试覆盖 |
|---|---|
| BlueZ | 多 Guest 广播、扫描、连接与断开 |
| Zephyr `native_sim` 蓝牙 shell | 广播与扫描、GATT、配对和隐私、链路更新、周期广播及同步转移 |
| NuttX `sim` BTSak 与 NimBLE | 广播、多广播端扫描、定向干扰恢复、GATT 读写与通知 |
| Apache NimBLE Linux | 发现、连接 |

## 技术架构

每个 Host 通过 H4 HCI 连接自己的 BSController。Bumble 运行在 pytest event loop 中，UML Guest 分别通过 time-travel socket 从同一个 event loop 获得仿真时间。PHY 设备编号分配给 Controller、pytest 时钟以及显式配置的干扰源。

<div align="center"><img src="../images/babblesim_arch.svg" alt="蓝牙 Host 通过各自的 BSController worker 连接到共享的 BabbleSim PHY" width="80%"><br><em>图 1：BSWorld 架构</em><br><br></div>

同一拓扑可以包含多个 UML Guest。各 Controller 的设备地址、缓冲区大小和蓝牙版本可以单独设置，详见 [BSController 配置](controllers.md)。

HCI 交互保存为 `hciN.snoop`，可使用 Wireshark 等支持 BTSnoop 的工具分析；无线交互保存为 `babblesim/ellisys.bttrp`，可使用 Ellisys 分析。文件位置和排查顺序参见[日志、跟踪文件与抓包](artifacts.md)。

### 离散时间调度

| 时间基准 | 含义 |
|---|---|
| **仿真时间** | 决定协议定时器、报文时序、RF 事件、asyncio 等待和测试超时。仅在所有参与者完成同步后推进。 |
| **墙钟时间** | 表示测试在主机上的实际耗时。CPU 负载和 Linux 调度会影响该耗时，但不会改变协议时序。 |

BSController、干扰源与 pytest 时钟注册到同一个 PHY，Guest 由这个时钟调度。无论主机负载如何，`T_IFS = 150 μs` 等协议间隔都保持不变。拓扑启动后，标准 `asyncio` 等待和超时使用仿真时间，参见[异步等待与仿真时间](writing-tests.md#异步等待与仿真时间)。

稳定复现要求二进制和输入保持一致，并控制随机源。外部网络、依赖墙钟的服务和未适配的 Host I/O 仍可能引入非确定性。

## 文档

- [编写测试和 RF 场景](writing-tests.md)：定义拓扑、接入业务程序、使用 asyncio 等待并配置 RF 条件。
- [BSController 配置](controllers.md)介绍拓扑 API 和 Controller 参数。
- [日志、跟踪文件与抓包](artifacts.md)：查找和分析测试产物。
- [开发检查](development.md)：运行单元测试、静态检查和覆盖率统计。
- [Docker 使用说明](../../docker/README.zh-CN.md)：运行 UML 镜像、选择测试套件和在本地构建镜像。

## 社区协作

- [贡献指南](CONTRIBUTING.md)
- [社区行为准则](CODE_OF_CONDUCT.md)
- [安全策略](SECURITY.md)

## 开源协议

BSWorld 使用 [Apache License 2.0](../../LICENSE)。第三方组件继续遵循各自的许可证，详见[第三方声明](THIRD_PARTY_NOTICES.md)。
