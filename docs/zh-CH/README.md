# BSWorld

[English](../../README.md) | [简体中文](README.md)

BSWorld 是一个在 Linux 上测试 Bluetooth Host 及其业务程序的确定性仿真框架。pytest 场景负责定义设备拓扑和预期行为；BSWorld 让所有参与者共享 BabbleSim 无线环境与仿真时钟，并归档日志和协议跟踪文件。

使用 BSWorld 可以：

- 通过真实 Host 协议栈测试广播、扫描、连接、GATT、周期广播、Mesh 和多设备交互。
- 稳定复现协议时序与 RF 行为，不受 Linux 调度抖动影响。
- 注入路径衰减、干扰、WLAN 活动和录制的无线信号。
- 使用 pytest-xdist 并行运行相互隔离的场景。
- 结合业务程序日志、HCI BTSnoop、PHY 日志和 Ellisys 跟踪文件定位故障。

## 快速开始

BSWorld 运行于 Linux/x86_64。已发布的 Docker 镜像包含 Python、BSWorld 和集成测试所需的固件。

### Docker（推荐）

安装 [Docker Engine](https://docs.docker.com/engine/install/ubuntu/)，然后运行镜像内的集成测试：

```sh
sudo docker pull ghcr.io/xiaomi/bsworld:latest
mkdir -p .cache/docker-results
# 已发布镜像以 UID/GID 1000 运行。
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  -e PYTHONPATH=/workspace \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  ghcr.io/xiaomi/bsworld:latest
```

测试结果写入 `.cache/docker-results`，包括 `pytest.xml`、进程日志、HCI 抓包和 RF 跟踪文件。选择测试套件、测试当前检出版本、选择镜像版本或自行构建镜像，参见 [Docker 使用说明](../../docker/README.zh-CN.md)。

### 本地 Linux

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
python -m pip install -e .
```

运行开发检查：

```sh
ruff check src tests
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

按照 [Zephyr Controller](controllers.md#编译-zephyr-controller)、[Zephyr shell](../../tests/integration/zephyr/shell/README.zh-CN.md)、[NuttX](../../tests/integration/nuttx/README.zh-CN.md)或 [QEMU / Buildroot](../../tests/integration/qemu/README.zh-CN.md) 的说明构建所需 Host 和 Controller 二进制，然后运行集成测试：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration -n auto
```

### 选择测试用的 Controller

集成场景默认分别使用 Zephyr Controller（`[zephyr]`）和 Python BSController（`[bscontroller]`）运行，参见[虚拟 Controller](controllers.md)。通过 pytest 参数 ID 选择一种实现：

```sh
pytest tests/integration -n auto -v -k '[zephyr]'
pytest tests/integration -n auto -v -k '[bscontroller]'
```

## 支持的集成环境

| 环境 | 已验证行为 | 文档 |
|---|---|---|
| Zephyr Bluetooth shell | 广播与扫描、干扰与恢复、连接与 GATT、跨 Controller 组合的 LL 过程、配对与隐私、周期广播、Mesh 配网和配置 | [Zephyr 测试](../../tests/integration/zephyr/shell/README.zh-CN.md) |
| Bumble Python Host | 广播、扫描、连接、周期广播与同步 | [Bumble 测试](../../tests/integration/bumble/README.zh-CN.md) |
| QEMU 中的 Linux 与 BlueZ | 通过 Linux Bluetooth 子系统完成 Controller 初始化、广播、扫描、连接和断连 | [BlueZ 测试](../../tests/integration/qemu/bluez/README.zh-CN.md) |
| TrouBLE Rust Host | 广播、连接、Battery Service 发现、读取和通知 | [TrouBLE 测试](../../tests/integration/trouble/README.zh-CN.md) |
| NuttX BTSak | 广播、扫描、多广播设备、干扰与恢复 | [BTSak 测试](../../tests/integration/nuttx/btsak/README.zh-CN.md) |
| Apache NimBLE | 在 NuttX 上完成发现、连接、GATT 和订阅；在 QEMU 中完成发现和连接 | [NuttX 测试](../../tests/integration/nuttx/nimble/README.zh-CN.md) · [QEMU 测试](../../tests/integration/qemu/nimble/README.zh-CN.md) |

## 技术架构

BSWorld 保留业务程序和 Host 协议栈的原生接口。每个 Host 都通过标准 HCI 连接独立的 Zephyr Controller 进程或 Python BSController worker。所有 Controller 和干扰源共享 BabbleSim PHY 与仿真时钟。

<div align="center"><img src="../images/babblesim_arch.svg" alt="BSWorld 参与者通过独立的虚拟 Controller 连接到共享的 BabbleSim PHY" width="80%"><br><em>图 1：BSWorld 架构</em><br><br></div>

虚拟 Controller 实现 HCI 命令、事件、ACL 数据、Link Layer 流程以及无线收发。同一拓扑可以混用 RTOS、Python 和基于 QEMU 的 Linux 参与者，也可以混用两种 Controller。选择和配置方式参见[虚拟 Controller](controllers.md)。

HCI 交互保存为 `hciN.snoop`，可使用 Wireshark 等支持 BTSnoop 的工具分析；无线交互保存为 `babblesim/ellisys.bttrp`，可使用 Ellisys 分析。文件位置和排查顺序参见[日志、跟踪文件与抓包](artifacts.md)。

### 离散时间调度

| 时间基准 | 含义 |
|---|---|
| **仿真时间** | 决定协议定时器、报文时序、RF 事件、asyncio 等待和测试超时。仅在所有参与者完成同步后推进。 |
| **墙钟时间** | 表示测试在主机上的实际耗时。CPU 负载和 Linux 调度会影响该耗时，但不会改变协议时序。 |

BSWorld 将业务程序、Controller、干扰设备和 pytest 注册到同一仿真中，并通过共享同步屏障沿同一时间轴推进。因此，无论主机负载如何，`T_IFS = 150 μs` 等协议间隔都保持不变。拓扑启动后，标准 `asyncio` 等待和超时使用仿真时间，参见[异步等待与仿真时间](writing-tests.md#异步等待与仿真时间)。

稳定复现要求二进制和输入保持一致，并控制随机源。外部网络、依赖墙钟的服务和未适配的 Host I/O 仍可能引入非确定性。

## 文档

- [编写测试和 RF 场景](writing-tests.md)：定义拓扑、接入业务程序、使用 asyncio 等待并配置 RF 条件。
- [虚拟 Controller](controllers.md)：选择 Zephyr 或 Python Controller、用两种实现运行同一场景、配置 BSController。
- [日志、跟踪文件与抓包](artifacts.md)：查找和分析测试产物。
- [开发检查](development.md)：运行单元测试、静态检查和覆盖率统计。
- [Docker 使用说明](../../docker/README.zh-CN.md)：运行已发布镜像、选择测试套件和构建本地镜像。

## 社区协作

- [贡献指南](CONTRIBUTING.md)
- [社区行为准则](CODE_OF_CONDUCT.md)
- [安全策略](SECURITY.md)

## 开源协议

BSWorld 使用 [Apache License 2.0](../../LICENSE)。第三方组件继续遵循各自的许可证，详见[第三方声明](THIRD_PARTY_NOTICES.md)。
