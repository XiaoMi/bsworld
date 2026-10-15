# BSWorld

[English](../../README.md) | [中文](README.md)

BSWorld 是一个面向 Bluetooth Host 及其业务程序的确定性仿真测试框架。它将通常依赖多台实体设备的端到端测试迁移到可重复运行的 Linux 虚拟环境，使同一场景能够在本地和 CI 中稳定复现，并在统一框架下验证不同的 Bluetooth Host 协议栈。

测试场景通过 [pytest](https://docs.pytest.org/) 定义设备拓扑、业务操作和预期结果。BSWorld 负责测试组件编排、仿真时间与 RF 条件控制，以及运行日志和协议跟踪文件的归档。

图 1 展示了 BSWorld 如何在同一场景中运行不同的 Bluetooth 业务程序和 Host 协议栈。RTOS、Python 和基于 QEMU 的 Linux 设备均保留原生接口，并通过标准 HCI 连接各自独立的虚拟 Controller。这些运行环境可以自由组合，实际规模取决于主机资源和场景复杂度。

<div align="center"><img src="../images/babblesim_arch.svg" alt="BSWorld 总体架构" width="80%"><br><em>图 1：BSWorld 总体架构</em><br><br></div>

所有虚拟 Controller 与可选的干扰设备共享 [BabbleSim](https://babblesim.github.io/) 提供的 PHY 和仿真时间。因此，无论 Host 运行在 RTOS、pytest 还是 Linux Guest 中，测试都能以相同方式控制设备规模、RF 条件和时间推进。

## 为什么需要 BSWorld

真实硬件验证是产品发布前不可替代的环节，但将所有系统测试都部署在物理实验室中，会显著限制测试频率和规模。一个完整场景通常需要多块开发板和网关，并涉及接线、烧录、串口配置和反复上下电；有限的硬件资源制约并行执行，操作系统调度抖动和周围 RF 环境也会增加偶发失败的复现难度。

BSWorld 因此定位为真实硬件测试的补充：将高频回归、长时间运行、大规模组网和 RF 故障注入前移到可控的虚拟环境，再由真实硬件完成最终验证。支撑这一能力的两个核心机制是 **虚拟 Controller** 和 **离散时间调度**。

## 技术架构

BSWorld 不重新实现 Bluetooth LE 协议栈或 RF 仿真器，而是以标准 HCI 为边界，将真实业务程序及 Host 协议栈、功能完整的虚拟 Controller、BabbleSim 无线环境和 pytest 测试编排组合成端到端系统。

### Application、Host 与 Controller

一个典型的 Bluetooth LE 设备包含三个与系统测试直接相关的层次：

- **Application** 负责实现产品功能并调用 Host API。
- **Host** 负责实现 GAP、GATT、L2CAP、SMP 和 Mesh 等协议。
- **Controller** 负责实现 Link Layer 及无线侧处理逻辑。

HCI 是 Host 与 Controller 之间的标准接口。Host 既可以连接片内 Controller，也可以通过 UART、USB 或其他传输方式连接符合 HCI 规范的 Controller。图 2 展示了一个常见的双芯片结构，其中 Host 与 Controller 的边界清晰可见。

<div align="center"><img src="../images/zephyr-bluetooth-dual-chip.png" alt="Bluetooth LE Host 与 Controller 的双芯片结构" width="75%"><br><em>图 2：Bluetooth LE Host-only 与 Controller-only 结构（来源：Zephyr Project，Apache-2.0）</em><br><br></div>

BSWorld 以同一条 HCI 边界作为虚拟化接口。业务程序与真实 Host 协议栈保留在被测环境中，框架既不以测试桩替换 Host API，也不要求业务程序改用仿真专用的 Bluetooth LE 接口。

### 虚拟 Controller

每个 Host 实例通过 Unix socket 与一个独立的虚拟 Controller 进程交换标准 HCI 数据。虚拟 Controller 并非返回预设结果的模拟桩，而是执行完整的 Controller 数据路径：处理 HCI 命令、事件和 ACL 数据，运行 Link Layer 协议逻辑，并通过 BabbleSim PHY 完成无线数据收发与 RF 交互。因此，业务程序实际经历广播、扫描、建连、GATT 和 Mesh 等完整协议流程。

图 3 对比了真实设备与 BSWorld 虚拟设备的协议路径。业务程序、Host 和 HCI 边界保持不变；物理 Controller 由功能完整的虚拟 Controller 进程承载，物理无线介质则由 BabbleSim 提供。

<div align="center"><img src="../images/real_vs_virtual_controller.svg" alt="虚拟 Controller 的完整协议链路" width="80%"><br><em>图 3：虚拟 Controller 的完整 HCI、Link Layer 与 RF 交互链路</em><br><br></div>

虚拟 Controller 的实现、运行接口和构建方式见 [虚拟 Controller](../../components/bt_virt_controller/README.zh-CN.md)。

HCI 和 RF 两个边界提供互补的调试信息。Unix socket 上的 HCI 交互保存为 `hciN.snoop`，可使用 [Wireshark](https://www.wireshark.org/) 等支持 BTSnoop 的工具分析；BabbleSim PHY 上的无线交互保存为 `babblesim/ellisys.bttrp`，可使用 [Ellisys](https://www.ellisys.com/products/bex400/) 查看空口时间线。前者用于定位 Host 与 Controller 的交互，后者用于确认报文收发、信道监听、干扰和碰撞。两类文件均按测试归档，具体位置和生成机制见 [日志、跟踪文件与抓包](artifacts.md)。

图 4 演示了将 `ellisys.bttrp` 导入 Ellisys 并进行报文级分析的过程。

<div align="center">
  <img src="../images/ellisys-trace-import.gif" alt="导入 BSWorld Ellisys 跟踪文件" width="760">
  <br>
  <em>图 4：在 Ellisys 中打开 BSWorld 空口跟踪文件进行报文级调试</em>
  <br><br>
</div>

### 离散时间调度

BSWorld 明确区分两种时间基准：

| 术语 | 含义 | 在 BSWorld 中的作用 |
|---|---|---|
| **离散时间（仿真时间）** | 由仿真时间戳表示的逻辑时间，仅在所有参与者完成同步后推进。 | 决定协议定时器、报文时序、RF 事件、异步等待和测试超时。 |
| **墙钟时间** | 主机实际经过的时间，其中包含 Linux 进程调度、CPU 负载、进程启动和文件 I/O 等真实执行开销。 | 决定用户等待一次测试完成所需的实际时长，但不决定 BabbleSim 测试中的协议时序。 |

两者不存在固定换算关系。Linux 进程可能消耗 100 ms 墙钟时间等待调度，而仿真时间保持不变；一次时间推进也可以跨过没有事件的仿真区间。处理同一段仿真时间所需的墙钟时间取决于事件密度和主机计算性能。图 5 展示了这种差异：墙钟时间连续流动，离散时间在同步边界之间推进，并在处理同一时间戳上的回调、I/O 和断言时保持不变。

<div align="center">
  <img src="../images/discrete-vs-wall-time.gif" alt="离散仿真时间与墙钟时间对比" width="760">
  <br>
  <em>图 5：墙钟时间连续流动，离散仿真时间在事件时间戳之间跳跃</em>
  <br><br>
</div>

仅将程序编译为 Linux 进程并不能形成确定性仿真。若各进程依赖墙钟运行，Linux 调度抖动会改变定时器到期和报文交互顺序。该问题在 RF 事件密集时尤为明显：Bluetooth LE Link Layer 存在微秒级时序约束，例如帧间间隔 `T_IFS = 150 μs`，而通用 Linux 调度器无法保证多个用户态进程在该时间内完成唤醒、socket 数据交换和代码执行。以墙钟作为协议时间可能拉长报文间隔，改变碰撞关系，甚至重排无线事件。

BSWorld 将原生业务程序进程、虚拟 Controller、干扰设备和 pytest 进程注册到同一个 BabbleSim 仿真实例。直接运行在 pytest 中的 Python Host 协议栈使用同一套仿真时钟。所有参与者通过共享同步屏障沿同一条时间轴推进，Linux 计算时间步所产生的墙钟开销不会计入仿真时间，因此协议中的 150 μs 始终保持为准确的 150 μs。

为了将 pytest 纳入相同的离散时间调度，BSWorld 将 pytest 进程自身注册为 BabbleSim 参与者。Python 时钟客户端通过 BabbleSim 的 FIFO 通信协议发送指定绝对时间的 WAIT 请求，并等待 PHY 应答。只有本次同步完成后，pytest 才会继续执行。

pytest 侧的调度器保留标准 [asyncio](https://docs.python.org/3/library/asyncio.html) 编程模型，仅接管 event loop 的阻塞等待并通过 FIFO 时钟客户端推进仿真时间。测试可用的异步接口、时间语义和每轮调度流程见 [编写测试和 RF 场景](writing-tests.md#异步等待与仿真时间)。

### 确定性的边界

在二进制和测试输入一致的前提下，BSWorld 控制仿真标识与设备编号、时间推进目标与仿真超时、Controller 地址、RF 模型输入，以及 Python 检查测试条件的时间边界。确定性还要求随机源可控；外部网络、依赖墙钟的服务和其他 Host I/O 需要经过适配或隔离，否则仍可能引入非确定性。

## 核心能力

- **加速长时间测试**：仿真定时器无需等待等长的墙钟时间。24 小时仿真超时不要求实际等待 24 小时；加速倍数取决于业务负载和主机性能，当前典型场景可达到约 80 倍。
- **稳定复现协议与 RF 行为**：`T_IFS = 150 μs` 等协议时序由仿真时间定义，主机负载只影响测试耗时，不改变协议时间轴。
- **验证端到端行为**：覆盖广播、扫描、建连、GATT、周期广播、Mesh 配网和多设备交互，并保留真实 Host 协议栈与标准 HCI 边界。
- **注入 RF 故障**：支持固定或随时间变化的路径衰减、突发干扰、WLAN 活动模型和录制信号回放。
- **并行执行独立场景**：每个测试使用独立的仿真标识、socket 目录和日志目录，可通过 [pytest-xdist](https://pytest-xdist.readthedocs.io/) 在 CI 中并行运行。
- **跨层故障诊断**：按测试保存进程日志、HCI BTSnoop、PHY 日志和 Ellisys 空口跟踪文件，可从业务程序、HCI 和 RF 三个层面关联分析。

确定性执行、结构化测试结果和完整协议记录也为自动化及 AI 辅助开发形成可复现的反馈闭环：代码变更可以在完整系统场景中验证，失败信息可作为后续自动分析与迭代的输入。

## 已验证场景

### 代表性端到端测试

| 业务程序环境 | 已验证场景 | 文档 |
|---|---|---|
| Zephyr Bluetooth 示例程序 | 广播与扫描、定向干扰与恢复、Heart Rate Central/Peripheral、周期广播与同步、Mesh 配网和配置 | [测试与示例构建](../../tests/integration/zephyr/samples/README.zh-CN.md) · [环境](../../tests/integration/zephyr/samples/common/source/README.zh-CN.md) |
| Bumble Python Host 协议栈 | 广播与扫描、定向干扰与恢复、连接、周期广播与同步 | [测试说明](../../tests/integration/bumble/README.zh-CN.md) |
| QEMU 中的 Linux 与 BlueZ | 通过 Linux Bluetooth 子系统完成 Controller 初始化、广播、扫描、建连和断连 | [测试说明](../../tests/integration/qemu/bluez/README.zh-CN.md) · [环境与构建](../../tests/integration/qemu/README.zh-CN.md) |
| QEMU 中的 TrouBLE Rust Host 协议栈 | 通过 HCI User Channel 完成广播发现、建连、Battery Service 发现和 Battery Level 读取 | [测试说明](../../tests/integration/qemu/trouble/README.zh-CN.md) · [环境与构建](../../tests/integration/qemu/README.zh-CN.md) |
| NuttX Bluetooth Socket / BTSak | 广播、扫描、Shell 动态控制、多广播设备、定向干扰与恢复 | [测试说明](../../tests/integration/nuttx/btsak/README.zh-CN.md) |

### 同一协议栈的多种运行环境

BSWorld 可以根据协议栈现有的运行环境选择接入路径：可移植的嵌入式代码可以运行在已支持的 RTOS 或其他兼容 POSIX 的仿真环境中；基于 asyncio 的 Python 协议栈可以直接运行在 pytest 中；Linux 业务程序和系统服务可以随完整 Guest 运行在 QEMU 中。平台相关的启动与 HCI 传输逻辑由测试适配层处理，Bluetooth 业务程序与 Host 继续使用原生接口。

Apache NimBLE 是同一套 Host 协议栈跨不同运行环境接入 BSWorld 的一个完整示例。它同时提供 NuttX port 和 Linux 示例，当前仓库已验证以下两种运行方式：

| NimBLE 运行环境 | 接入方式 | 已验证行为 | 文档 |
|---|---|---|---|
| NuttX | 使用 NuttX 上游 NimBLE port 与业务程序，构建为独立仿真进程 | 发现、连接、GATT 服务发现和订阅 | [测试说明](../../tests/integration/nuttx/nimble/README.zh-CN.md) · [NuttX 环境](../../tests/integration/nuttx/README.zh-CN.md) |
| QEMU 中的 Linux | 使用 NimBLE 上游 Linux 示例，通过 HCI User Channel 访问 `hci0` | 广播发现与精确地址和数据校验、建连和断连 | [测试说明](../../tests/integration/qemu/nimble/README.zh-CN.md) · [QEMU 环境](../../tests/integration/qemu/README.zh-CN.md) |

## 快速开始

BSWorld 运行于 Linux/x86_64。推荐使用已发布的 Docker 镜像，其中已包含 Python、BSWorld 和集成测试所需的全部固件。也可以通过 Dockerfile 自行构建镜像，或直接在 Linux 系统上搭建环境。

### Docker（推荐）

按 Docker 官方 [Ubuntu 安装说明](https://docs.docker.com/engine/install/ubuntu/)安装 Docker Engine，然后拉取镜像并运行其中的集成测试：

```sh
sudo docker pull ghcr.io/xiaomi/bsworld:latest
mkdir -p .cache/docker-results
# The published image runs as UID/GID 1000.
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  -e PYTHONPATH=/workspace \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  ghcr.io/xiaomi/bsworld:latest
```

上述命令运行镜像内置的集成测试，将 `pytest.xml`、日志和抓包文件保存到当前目录的 `.cache/docker-results` 下。镜像支持通过 `latest`、完整 commit ID（`sha-<commit>`）和已发布的 Release tag（例如 `v1.0.0`）选择。拉取和运行时使用相同的镜像地址；测试旧版本源码时，选择对应 commit 或 Release 的镜像。版本选择示例见 [Docker 使用说明](../../docker/README.zh-CN.md#使用已发布镜像)。

测试当前仓库时，将仓库根目录挂载到 `/workspace`，固件使用镜像内的编译产物。挂载方式、测试套件选择、单元测试、静态检查及自行构建镜像的方法见 [Docker 使用说明](../../docker/README.zh-CN.md)。

### 本地 Linux

开发和测试需要 Python 3.12 或更高版本。以下以本地 Ubuntu 24.04 为例，先安装 Git、Make、编译器、Python 虚拟环境支持，以及 BabbleSim 使用的基础工具：

```sh
sudo apt-get update
sudo apt-get install -y \
  bash ca-certificates curl git file make binutils build-essential \
  python3.12 python3.12-dev python3.12-venv python3-pip \
  gcc-multilib g++-multilib coreutils findutils grep sed gawk
```

`gcc-multilib` 和 `g++-multilib` 提供 BabbleSim 与 Zephyr `nrf52_bsim` 目标所需的 32 位工具链。

首次使用时，先获取仓库：

```sh
git clone https://github.com/XiaoMi/bsworld.git
cd bsworld
```

在仓库根目录创建项目虚拟环境，并安装 BSWorld：

```sh
export BSWORLD_ROOT="$PWD"
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

这个 `.venv` 用于 BSWorld 自身：`pip install -e .`、pytest 和 `ruff`。Zephyr 使用另一个虚拟环境 `$ZEPHYR_WORKSPACE/.venv`，只运行 `west` 和 `west build`。创建方法见 [Zephyr 工作区说明](../../tests/integration/zephyr/samples/common/source/README.zh-CN.md)。运行命令前先激活对应环境。激活只会改变 `PATH`，已经导出的 `BSWORLD_*` 和 BabbleSim 变量会保留。

开发检查：

```sh
ruff check src tests
pytest tests/unit -q
```

#### 构建与测试

先使用 BabbleSim 自带的 Makefile 构建：

```sh
cd "$BSWORLD_ROOT"
git submodule update --init --recursive components/babblesim
export BSIM_OUT_PATH="$BSWORLD_ROOT/components/babblesim/build"
export BSIM_COMPONENTS_PATH="$BSWORLD_ROOT/components/babblesim/components"
export BSIM_BASE_PATH="$BSIM_COMPONENTS_PATH"
export BSIM_BUILD_FAIL_ASAP=1
make -C "$BSWORLD_ROOT/components/babblesim" everything
export BSWORLD_BSIM_DIR="$BSIM_OUT_PATH"
```

然后按各目录 README 的命令配置和编译 [Controller](../../components/bt_virt_controller/README.zh-CN.md)、[Zephyr 示例](../../tests/integration/zephyr/samples/README.zh-CN.md)、[NuttX](../../tests/integration/nuttx/README.zh-CN.md)及 [QEMU / Buildroot](../../tests/integration/qemu/README.zh-CN.md)。

#### 预编译二进制

[Release 压缩包](https://github.com/XiaoMi/bsworld/releases/download/v0.3.9/bsworld-prebuilt-v0.3.9.zip)提供已经用 Exodus 打包的固件（虚拟 Controller、八个 Zephyr 示例，以及 NuttX BTSak 和 NimBLE），供本机 Linux 在没有 Zephyr 或 NuttX 工具链时运行测试。压缩包不含 BabbleSim，仍需按上面的[构建与测试](#构建与测试)自行编译，也不含 QEMU 相关文件。请选择与检出对应的 Release（`build-info.txt` 记录了对应提交）。

```sh
ver=v0.3.9
curl -fL -o "/tmp/bsworld-prebuilt-${ver}.zip" \
  "https://github.com/XiaoMi/bsworld/releases/download/${ver}/bsworld-prebuilt-${ver}.zip"
unzip -qo "/tmp/bsworld-prebuilt-${ver}.zip" -d /tmp
rsync -a --prune-empty-dirs --include='*/' --include='**/prebuilt/***' --exclude='*' \
  "/tmp/dist/bsworld-prebuilt-${ver}/" "$BSWORLD_ROOT/"
export BSWORLD_BT_VIRT_CONTROLLER_BIN="$BSWORLD_ROOT/components/bt_virt_controller/prebuilt/bt_virt_controller/exodus/bin/bt_virt_controller"
export BSWORLD_ZEPHYR_BROADCASTER_BIN="$BSWORLD_ROOT/tests/integration/zephyr/samples/broadcast_observe/prebuilt/broadcaster/exodus/bin/zephyr.exe"
export BSWORLD_ZEPHYR_OBSERVER_BIN="$BSWORLD_ROOT/tests/integration/zephyr/samples/broadcast_observe/prebuilt/observer/exodus/bin/zephyr.exe"
# Same prebuilt/<name>/exodus/bin/<launcher> path for BSWORLD_ZEPHYR_{PERIPHERAL_HR,CENTRAL_HR,PERIODIC_ADV,PERIODIC_SYNC,MESH,MESH_PROVISIONER}_BIN and BSWORLD_NUTTX_{BTSAK,NIMBLE}_BIN.
pytest tests/integration/zephyr/samples/broadcast_observe -q --no-ellisys
```

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration -n auto
```

## 文档

- [编写测试和 RF 场景](writing-tests.md)：进程配置、asyncio 等待、Shell 命令、路径衰减和干扰注入。
- [日志、跟踪文件与抓包](artifacts.md)：日志目录、BTSnoop 和 Ellisys 空口跟踪文件。
- [开发和单元测试](development.md)：开发约定、静态检查、单元测试和测试覆盖率。

## 社区协作

- [贡献指南](CONTRIBUTING.md)：开发流程、验证要求和 Pull Request 约定。
- [社区行为准则](CODE_OF_CONDUCT.md)：参与项目交流时应遵守的行为要求。
- [安全策略](SECURITY.md)：受支持版本和私密漏洞报告方式。

## 开源协议

BSWorld 使用 [Apache License 2.0](../../LICENSE)。第三方组件继续遵循各自的许可证，详见 [第三方声明](THIRD_PARTY_NOTICES.md)。
