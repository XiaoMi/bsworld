# BSController

[English](README.md) | 简体中文

BSController 是用 Python 实现的低功耗蓝牙 Controller。Host 通过标准 H4 HCI 接入，多个 Controller 通过同一个 [BabbleSim PHY](https://babblesim.github.io/) 收发空口报文。PHY 按离散步进推进仿真时钟，使协议时序可复现。

## 快速开始

BSController 需要 Linux、Python 3.11 或更高版本，以及编译好的 BabbleSim。以 Ubuntu 24.04 x86_64 为例，先安装构建工具。

```sh
sudo apt-get update
sudo apt-get install -y git repo build-essential gcc-multilib g++-multilib python3-venv
```

获取 BabbleSim 源码并编译 PHY、调制解调器和信道模型。

```sh
mkdir -p "$HOME/bsim" && cd "$HOME/bsim"
repo init -u https://github.com/BabbleSim/manifest.git -m everything.xml -b master
repo sync
export BSIM_OUT_PATH="$PWD/build"
export BSIM_COMPONENTS_PATH="$PWD/components"
make -j"$(nproc)" ext_2G4_phy_v1 ext_2G4_modem_BLE_simple ext_2G4_channel_multiatt
```

在运行 BSController 的终端中保留 `BSIM_OUT_PATH` 环境变量。其他选项见 BabbleSim 的[源码获取](https://babblesim.github.io/fetching.html)和[编译](https://babblesim.github.io/building.html)指南。

返回 BSController 目录安装 Python 包。

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

## 独立运行 Controller

`bscontroller serve` 启动 BabbleSim PHY，并通过 Unix socket 提供 H4 HCI。启动后，让 Host 连接指定的 socket。

```sh
bscontroller serve --hci-socket /tmp/radio.sock
```

给 `--hci-socket` 传入多个路径，可在同一个 PHY 上运行多个 Controller。每个 socket 对应一个 Controller，依次命名为 `hci0`、`hci1` 等。公共地址从 `--address` 指定的值开始递增，默认从 `F0:F1:F2:F3:F4:01` 开始。

```sh
bscontroller serve --hci-socket /tmp/radio0.sock /tmp/radio1.sock
```

加上 `--capture-air --log-dir /tmp/bscontroller` 可保存一份共享的 `ellisys.bttrp`，并为每个 Controller 保存 `radioN.pcapng` 和 `hciN.btsnoop`。

Ellisys 文件按仿真时间记录所有 Controller 的发射。每份 PCAPNG 记录对应 Controller 发送的报文及 PHY 在接收期间返回的报文。每份 BTSnoop 记录 Host 与对应 Controller 之间的 H4 报文。

默认情况下，仿真以机器允许的最快速度运行。如果 Host 使用墙钟定时器，请加上 `--realtime`，让仿真时间与墙钟时间同步。

```sh
bscontroller serve --hci-socket /tmp/radio.sock --realtime
```

### 接入 BlueZ

要让一个 BlueZ 适配器扫描到另一个适配器的广播，先以实时模式启动两个 Controller。

```sh
bscontroller serve --hci-socket /tmp/radio0.sock /tmp/radio1.sock --realtime
```

等 `serve` 打印出两个 socket 路径后，用 `socat` 为每个 socket 创建一个伪终端，再用 BlueZ 的 `btattach` 将这两个伪终端注册为 HCI UART 设备。以下四条命令分别在独立的终端中运行。

```sh
socat PTY,link=/tmp/bscontroller-tty0,rawer UNIX-CONNECT:/tmp/radio0.sock
socat PTY,link=/tmp/bscontroller-tty1,rawer UNIX-CONNECT:/tmp/radio1.sock
sudo btattach -B "$(readlink -f /tmp/bscontroller-tty0)" -P h4
sudo btattach -B "$(readlink -f /tmp/bscontroller-tty1)" -P h4
```

在 `bluetoothctl` 中执行 `select F0:F1:F2:F3:F4:01`，为第一个适配器开启广播。选中 `F0:F1:F2:F3:F4:02` 后开始扫描。

## 在 Python 中使用

在运行中的 asyncio 事件循环里创建 `RadioController`。Controller 在独立线程中运行，由 `RadioSession` 管理线程的启动和退出，并接入已启动的 PHY。

运行下面的示例前，先用 `example` 作为 `sim_id` 启动 PHY，并分配两个设备编号。Controller 使用编号 `0`，Host 时钟使用编号 `1`。

```python
import asyncio
from pathlib import Path

from bscontroller import RadioController, RadioEventLoop, RadioSession


async def main():
    controller = RadioController(
        "controller", "F0:F1:F2:F3:F4:01", hci_socket=Path("/tmp/radio.sock")
    )
    async with RadioSession("example", [(0, controller)], clock_dev_id=1):
        await controller.terminated


with asyncio.Runner(loop_factory=RadioEventLoop) as runner:
    runner.run(main())
```

创建 Controller 时选择 Host 的接入方式。

- **Unix socket** 方式通过 `hci_socket` 指定路径。Controller 启动时开始监听 socket，与 PHY 完成同步后开始处理 HCI 命令。
- **进程内 H4** 方式省略 `hci_socket`。启动前调用 `controller.set_packet_sink(sink)` 注册接收端，由其 `on_packet(packet: bytes)` 接收 H4 报文。调用 `controller.on_packet(packet)` 向 Controller 发送完整的 H4 报文。

传入 `hci_snoop_file=Path(...)` 可以录制 BTSnoop 抓包，时间戳使用仿真时间。

传入 `rf_pcap_file=Path(...)` 可将当前 Controller 的空口报文写入 PCAPNG。需要汇总多个 Controller 的发射时，创建一个 `EllisysCapture(Path(...))`，并通过 `ellisys_capture` 传给每个 Controller。退出 `RadioSession` 后关闭它，即可保存汇总文件。

包还导出 `Radio`、`Phy`、`Received` 和 `hci` 中的报文类。`ControllerConfig` 用法见[配置](#配置)。

从 Python 管理仿真时，PHY 必须在会话期间保持运行。

- 进入 `RadioSession` 前，先用 `bscontroller.runtime.PhyProcess` 启动 PHY。
- 让 Host 按仿真时间运行时，选择 `RadioEventLoop`，并为 Host 时钟设置一个与 Controller 设备编号不同的 `clock_dev_id`。
- 退出 `RadioSession` 时，会先停止 Controller，再关闭会话的 Host 时钟连接。会话结束后再退出 `PhyProcess`。

广播、扫描、GATT、周期同步和多角色共存的运行方式见 [Bumble Host 示例](samples/README.zh-CN.md)。

## 配置

`ControllerConfig` 管理蓝牙规范交由 Controller 决定的参数。省略的字段使用默认值。

| JSON 键 | 默认值 | 范围 | 规范章节（Vol 4, Part E） |
|---|---|---|---|
| `hciVersion` | `0x0F` (6.1) | `0x06`–`0x10` | 7.4.1 |
| `hciRevision` | `0` | `0`–`0xFFFF` | 7.4.1 |
| `lmpVersion` | `0x0F` (6.1) | `0x06`–`0x10` | 7.4.1 |
| `lmpSubversion` | `0` | `0`–`0xFFFF` | 7.4.1 |
| `manufacturer` | `0xFFFF` | `0`–`0xFFFF` | 7.4.1 |
| `aclPacketCount` | `8` | `1`–`255` | 7.8.2 |
| `aclPacketLength` | `251` | `27`–`65535` | 7.8.2 |
| `maxTxOctets` | `251` | `27`–`251` | 7.8.46 |
| `maxTxTime` | `17040` | `328`–`17040` | 7.8.46 |
| `maxRxOctets` | `251` | `27`–`251` | 7.8.46 |
| `maxRxTime` | `17040` | `328`–`17040` | 7.8.46 |
| `advertisingSetCount` | `16` | `1`–`240` | 7.8.58 |
| `maxAdvertisingDataLength` | `1650` | `31`–`1650` | 7.8.57 |
| `filterAcceptListSize` | `16` | `1`–`255` | 7.8.14 |
| `resolvingListSize` | `16` | `1`–`255` | 7.8.41 |
| `periodicAdvertiserListSize` | `8` | `1`–`255` | 7.8.73 |
| `maxConnections` | `64` | `1`–`3839` | 5.3.1 |
| `maxPeriodicSyncs` | `16` | `1`–`3839` | 5.3.1 |
| `isoPacketCount` | `16` | `1`–`255` | 7.8.2 |
| `isoPacketLength` | `251` | `5`–`16383` | 7.8.2 |
| `maxBigs` | `4` | `1`–`240` | 7.8.103, 7.8.106 |
| `maxBis` | `16` | `1`–`3839` | 5.3.1, 7.8.103, 7.8.106 |

在 `defaults` 中填写所有 Controller 共用的设置，在 `controllers` 中按名称覆盖单个 Controller 的设置。

```json
{
  "defaults": {"aclPacketCount": 16},
  "controllers": {"hci0": {"maxConnections": 2}, "hci1": {"maxTxOctets": 27}}
}
```

键名支持 camelCase 和 snake_case。`defaults` 覆盖内置默认值，`controllers.<name>` 再覆盖该 Controller 的共用设置。`ControllerConfig.load()` 和 `ControllerConfig.from_mapping()` 接受优先级最高的关键字参数。遇到未知的键或超出范围的值时，会输出警告并保留该项原有设置。

先将内置默认值导出到文件，再将修改后的文件传给 `serve`。

```sh
bscontroller config > bsc.json        # 导出全部默认值，在此基础上修改
bscontroller serve --hci-socket /tmp/radio0.sock /tmp/radio1.sock --config bsc.json
```

在运行中的 asyncio 事件循环里，可以向 `RadioController` 传入配置文件路径、相同结构的映射或 `ControllerConfig` 实例。传入文件或映射时，Controller 按名称读取 `controllers` 中对应的条目。

```python
from bscontroller import ControllerConfig, RadioController

central = RadioController("hci0", "F0:F1:F2:F3:F4:01", config="bsc.json")
peripheral = RadioController("hci1", "F0:F1:F2:F3:F4:02", config="bsc.json")
other = RadioController("x", "F0:F1:F2:F3:F4:03", config=ControllerConfig(max_connections=2))
```

直接构造 `ControllerConfig` 时，参数值无效会抛出 `ValueError`。

## 支持的特性

下表按蓝牙 Core 规范首次引入特性的版本排列，`*` 标记已实现的 Link Layer 特性。BSController 在 LE FeatureSet 中报告相应的特性位，位定义见 [Vol 6, Part B, 4.6](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core_v6.3/out/en/low-energy-controller/link-layer-specification.html#UUID-56ada5ed-4ae3-acee-198f-27ead57d86f1)。

| Core 规范版本 | Link Layer 特性 | 支持 |
|---|---|:---:|
| 4.0 | LE Encryption | * |
| 4.1 | Connection Parameters Request Procedure | * |
| 4.1 | Extended Reject Indication | * |
| 4.1 | Peripheral-initiated Features Exchange | * |
| 4.1 | LE Ping Procedure | * |
| 4.2 | LE Data Packet Length Extension | * |
| 4.2 | LL Privacy | * |
| 4.2 | Extended Scanner Filter Policies | * |
| 5.0 | LE 2M PHY | * |
| 5.0 | LE Coded PHY | * |
| 5.0 | Channel Selection Algorithm #2 | * |
| 5.0 | Minimum Number of Used Channels Procedure | * |
| 5.0 | Extended Advertising | * |
| 5.0 | Periodic Advertising | * |
| 5.1 | Connection CTE Request |  |
| 5.1 | Connection CTE Response |  |
| 5.1 | Connectionless CTE Transmitter |  |
| 5.1 | Connectionless CTE Receiver |  |
| 5.1 | Periodic Advertising Sync Transfer | * |
| 5.1 | Initiating Periodic Advertising Sync Transfer for Local Periodic Advertising | * |
| 5.1 | Initiating Periodic Advertising Sync Transfer for Remote Periodic Advertising | * |
| 5.2 | Isochronous Broadcaster | * |
| 5.2 | Synchronized Receiver | * |
| 5.2 | Connected Isochronous Stream (CIS) |  |
| 5.2 | LE Power Control Request | * |
| 5.2 | LE Path Loss Monitoring | * |
| 5.3 | Periodic Advertising ADI Support | * |
| 5.3 | LE Channel Classification |  |
| 5.3 | Connection Subrating |  |
| 5.4 | Periodic Advertising with Responses | * |
| 6.0 | Channel Sounding |  |
| 6.0 | Decision-Based Advertising Filtering | * |
| 6.0 | LL Extended Feature Set | * |
| 6.0 | Monitoring Advertisers | * |
| 6.0 | Frame Space Update |  |
| 6.1 | Randomized RPA Updates | * |
| 6.2 | Shorter Connection Intervals |  |
| 6.2 | Channel Sounding Amplitude-based Attack Resilience |  |
| 6.3 | Channel Sounding Inline Phase Correction Term Transfer |  |

## 工作原理

HCI 层与 Host 交换报文，Link Layer 执行 Controller 侧的协议流程，射频适配层负责与 BabbleSim PHY 收发空口报文。协议行为遵循 [Bluetooth Core 6.3](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core_v6.3/out/en/architecture,-change-history,-and-conventions/architecture.html#UUID-37039e1f-e0cd-37ce-2cf0-15dd6e6cff2e)。

<div align="center"><img src="docs/images/radio_controller_arch.svg" alt="多个 Controller 共享 BabbleSim PHY" width="80%"><br><em>图 1：共享同一 PHY 的多个 Controller</em></div>

Link Layer 分别管理广播、扫描、发起连接、同步和已建立的连接。每条 ACL 连接独立维护控制流程状态、加密状态和连接事件执行器。各角色由同一个调度器协调射频使用。

每个 Controller 线程有自己的事件循环和 PHY 连接，通过完整的 H4 报文与 Host 通信。各 Controller 和 Host 时钟（如有）与 PHY 同步，PHY 以整数微秒为单位推进仿真时间。

| 对象 | 负责的资源 |
| --- | --- |
| `RadioController` | Controller 线程、事件循环和射频连接 |
| `RadioSession` | Controller 线程的启动和停止，以及可选的 Host 时钟 |
| `H4Server` | Unix socket、Host 连接、H4 分帧 |
| `PhyProcess` | BabbleSim PHY 进程及其日志和仿真目录 |

## 开发

```text
.
├── src/bscontroller/     # 包：HCI、Link Layer、射频、运行时、配置
├── samples/              # Bumble Host 示例，包含多角色共存
└── tests/
    ├── unit/             # 单元测试，无需启动 PHY
    └── qualification/    # HCI 和 Link Layer 规范用例
```

```sh
python -m pip install -e '.[test]'
python -m pytest tests/unit -n auto -q
python -m pytest tests/qualification -n auto -v -rs     # 射频用例使用 BSIM_OUT_PATH
python -m build                                        # 在 dist/ 生成 sdist 和 wheel
```

认证测试使用独立实现的 Lower Tester 执行蓝牙测试规范中选定的 899 个 HCI 和 Link Layer 用例。覆盖范围、运行条件和日志位置见[测试指南](tests/README.zh-CN.md)。

许可证和第三方声明见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。
