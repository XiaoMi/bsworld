# 编写测试

[English](../writing-tests.md) | [简体中文](writing-tests.md)

仓库 [README](README.md#技术架构) 已经介绍了虚拟 Controller 和离散时间调度。本文按照测试的实际编写过程，说明如何声明拓扑、接入业务程序、使用 asyncio 执行与等待、配置 RF 场景，并验证预期的 Bluetooth LE 行为。

## 快速索引

- [测试生命周期](#测试生命周期)
- [选择 Controller](#选择-controller)
- [异步等待与仿真时间](#异步等待与仿真时间)
- [接入业务程序进程](#接入业务程序进程)
- [向进程发送 Shell 命令](#向进程发送-shell-命令)
- [RF 条件与干扰](#rf-条件与干扰)
- [使用 pytest 描述拓扑](#使用-pytest-描述拓扑)

## 测试生命周期

`topo` fixture 为每个测试提供一个 `BluetoothTopology`。测试声明参与者、连接关系和 RF 条件，不直接编排底层操作系统进程。原生业务程序由所在测试目录的创建函数描述为 `ProcessInstance`，获得日志等待和可选的 Shell 输入能力，并自动登记到传入的拓扑中；基于 asyncio 的 Python Host 协议栈则直接注册虚拟 Controller，并在拓扑启动后连接它的 HCI socket。User Mode Linux Guest 同样只预先注册 Controller，在拓扑启动后再启动。

一套拓扑依次经过四个阶段：

1. 测试 fixture 通过程序创建函数、`attach()`、`topo.add_process()`、`topo.add_controller()` 和 RF 配置接口记录预期拓扑，此时不会创建子进程或推进仿真时间。
2. `await topo.start()` 确定各设备标识，创建隔离的 socket 和日志路径，并启动 PHY、虚拟 Controller、干扰源和业务程序。当所有参与者完成注册、event loop 已运行在仿真时间上时，该调用返回。
3. 测试通过 `ProcessInstance`、进程内 Host 协议栈接口、Shell 命令和标准 asyncio API 执行业务操作与断言。event loop 处理完当前时间戳上的回调和 I/O 后，才会推进共享仿真时间。
4. fixture 清理阶段停止 Controller 和进程，写完日志和抓包文件，生成 Ellisys 跟踪文件并删除仿真临时文件。

每个测试都有独立的仿真标识、Unix socket 目录和日志目录，因此多个虚拟测试可以并行运行，不会共享 Controller 状态、RF 状态或输出文件。

## 选择 Controller

每个 Host 通过 `topo.add_controller()` 获得自己的虚拟 Controller，在 `topo.start()` 之后连接到它的 `hci_sock`：

```python
from bsworld.devices import BSController

ctl = topo.add_controller(controller_type=BSController, public_address="F0:F1:F2:F3:F4:01")
await topo.start()
# 将 Host 连接到 ctl.hci_sock。
```

默认使用 `BSController`，`ZephyrController` 是原生实现的替代选项，同一拓扑可以混用两种。完整 Zephyr 固件自带 Controller，两者都不需要，参见[完整固件](#完整固件)和[虚拟 Controller](controllers.md)。

## 异步等待与仿真时间

BSWorld 保留标准 asyncio 编程模型，测试无需改用专用的任务、I/O、sleep 或超时接口。pytest 使用的 `BsimEventLoop` 仍然管理 asyncio 的任务、Future、队列、Event、stream 和 socket；与普通 event loop 的区别只在于仿真启动后采用的时间来源和等待方式。

创建 `topo` fixture 后，event loop 先按照墙钟运行，用于完成测试初始化。`await topo.start()` 启动全部参与者并完成 BabbleSim 注册后，BSWorld 将 BabbleSim 时钟客户端绑定到 event loop。此后，`loop.time()` 随仿真时间推进，`asyncio.sleep()`、`asyncio.wait_for()` 和其他基于 event loop 的超时也都使用仿真时间。例如，`timeout=30` 表示最多等待 30 秒仿真时间，而不是 30 秒墙钟时间：

```python
import asyncio

await topo.start()

await dev.wait_log("ready", timeout=30)
await asyncio.sleep(0.2)
result = await asyncio.wait_for(operation(), timeout=5)
```

仿真时间只有在当前时间戳上的全部回调和 I/O 事件都处理完成后才会推进，因此测试总是先看到同一时刻调度的所有工作，时钟才继续前进。图 1 展示了这样一次推进。

<div align="center"><img src="../images/bluetooth-diagram.svg" alt="一次 BSWorld 离散时间推进" width="80%"><br><em>图 1：一次确定性的离散时间推进</em><br><br></div>

`ProcessInstance.wait_log()` 建立在同一套异步等待机制之上，因此其 `timeout` 同样使用仿真时间。它按到达顺序对已经缓存和后续产生的完整日志行执行正则匹配；未收到换行符的片段不会提前参与匹配，已经扫描过的行也不会被后续调用重复检查。因此，连续的日志断言应按照预期事件顺序书写。

> [!WARNING]
> 异步测试中不能调用 `time.sleep()` 这类阻塞函数，因为它会阻塞 event loop 线程。应优先使用异步接口；不参与协议时序且无法避免的 Host 侧阻塞操作可以交给 `asyncio.to_thread()`。

## 接入业务程序进程

各集成测试目录通过自己的创建函数生成业务程序。创建函数以拓扑为第一个参数，并把新进程登记到拓扑中；测试随后选择 Controller，在 `topo.start()` 前通过 `host.attach(controller)` 绑定。Python Host 协议栈在拓扑启动后打开 Controller 的 `hci_sock`，直接运行在测试的 asyncio event loop 中。添加场景时复用所在套件的现有辅助函数，环境准备、初始化和平台命令见对应 README。

```python
from tests.integration.zephyr.shell.devices import create_zephyr_shell
from bsworld.devices import BSController

host = create_zephyr_shell(topo, name="host")
host.attach(topo.add_controller(
    controller_type=BSController, public_address="F0:F1:F2:F3:F4:02"
))
```

设备标识发布前都可以修改绑定关系，通常由 `topo.start()` 发布，提前调用 `plan_devices()` 也会冻结。自行构造 `ProcessInstance` 时，需通过 `topo.add_process()` 登记；如果稍后才挂载 Controller，需传入 `requires_controller=True`。这样漏掉 `attach()` 时会在规划设备布局阶段、PHY 启动前报错，而不会被当作完整固件。套件的创建函数已经设置了该参数。

| 平台 | 测试说明 |
|---|---|
| Bumble | [Bumble 集成测试](../../tests/integration/bumble/README.zh-CN.md#添加场景) |
| NuttX | [BTSak 与 NimBLE](../../tests/integration/nuttx/README.zh-CN.md#添加场景) |
| TrouBLE | [原生 TrouBLE 测试](../../tests/integration/trouble/README.zh-CN.md#添加场景) |
| UML Linux | [BlueZ 与 NimBLE](../../tests/integration/uml/README.zh-CN.md#添加场景) |
| Zephyr | [Bluetooth shell](../../tests/integration/zephyr/shell/README.zh-CN.md#添加场景) |

## 完整固件

自带 Controller 和无线模型的固件（例如 Zephyr `nrf52_bsim` 构建）直接接入 PHY，登记时不挂载 Controller：

```python
from pathlib import Path
from bsworld.devices import ProcessInstance

node = topo.add_process(ProcessInstance(
    command=lambda slot: [
        Path("/absolute/path/to/zephyr.exe"),
        f"-s={slot.sim_id}", f"-p={slot.phy_id}", f"-d={slot.dev_id}",
        "-rs=41", "-mro=1000", "-RealEncryption=1",
    ],
    label="node",
    shell=True,
    # -RealEncryption=1 loads libCryptov1.so from BabbleSim.
    env={"LD_LIBRARY_PATH": str(topo.env.bsim_dir / "lib")},
))
await topo.start()
```

- 仅当镜像把 UART 接到 stdin/stdout 时设置 `shell=True`；否则用 `wait_log()` 检查输出。
- 该进程可直接作为衰减路径的 `tx` 或 `rx`。
- 没有 `hci_sock` 和 `hciN.snoop`，排查时看控制台日志和 `ellisys.bttrp`。

Zephyr shell 镜像的构建步骤见 [Zephyr 蓝牙 Shell 测试](../../tests/integration/zephyr/shell/README.zh-CN.md#构建与运行)。

## 向进程发送 Shell 命令

对于启用 Shell 的 `ProcessInstance`，在 `await topo.start()` 和平台初始化完成后发送命令。假设 `dev` 已初始化，基本用法为：

```python
await dev.shell.run("...")
await dev.wait_log(r"...", timeout=5)
```

根据平台 README 将占位符替换为具体命令及预期输出。`shell.run()` 只向标准输入写入一行命令，不等待命令执行完成；应通过 `wait_log()` 或其他可观察条件确认结果。

## RF 条件与干扰

RF 条件必须在 `await topo.start()` 之前配置；拓扑启动后，PHY 参数和参与者集合保持不变。路径衰减与干扰源作用于同一 BabbleSim PHY 上的全部虚拟 Controller。PHY 默认使用 `BLE_simple` 调制解调器，因此 RSSI、SNR 和路径衰减都会影响报文接收结果。

### 信道衰减

```python
topo.set_channel_attenuation(
    default_db=70.0,
    extra_db=0.0,
)
```

| 参数 | 默认值 | 含义 |
|---|---|---|
| `default_db` | `60.0` | 所有未被固定或时变配置覆盖的有向 `tx -> rx` 路径损耗。 |
| `extra_db` | `0.0` | 叠加到每条路径上的额外衰减。 |
| `matrix_file` | `None` | 原始 `multiatt` 矩阵文件路径，与自动生成的路径配置互斥。 |
| `static_paths` | `[]` | 各条固定路径的衰减配置。 |
| `timed_paths` | `[]` | 各条路径随时间变化的衰减曲线。 |

### 按路径配置衰减

这里的 `peripheral` 和 `central` 是已经登记到拓扑中的设备。

```python
from bsworld.interference import StaticAttenuation, TimedAttenuation

topo.set_channel_attenuation(
    default_db=50.0,
    static_paths=[
        StaticAttenuation(
            tx=peripheral,
            rx=central,
            attenuation_db=55.0,
        ),
    ],
    timed_paths=[
        TimedAttenuation(
            tx=central,
            rx=peripheral,
            times_us=[0, 10_000_000, 20_000_000],
            attenuation_db=[50.0, 80.0, 50.0],
        ),
    ],
)
```

路径具有方向性；如果两个方向需要不同的损耗，应分别配置 `tx -> rx` 和 `rx -> tx`。`TimedAttenuation` 的时间点与衰减值必须一一对应，相邻时间点之间采用线性插值。

`tx` 和 `rx` 可以是已经登记的 `ProcessInstance` 或 `Controller`、`add_*_interference()` 返回的 `InterferenceRef`，也可以是原始 BabbleSim 设备编号。HCI Host 进程解析为其虚拟 Controller 的 RF 设备编号；完整固件解析为自身的无线设备编号。

### 突发干扰

```python
jammer = topo.add_burst_interference(
    center_freq_mhz=2402.0,
    power_dbm=10.0,
    interf_type="CW",
    windows=[],
    rand_seed=None,
)
```

`windows=[]` 表示干扰在整个仿真期间生效；使用 `[(start_us, end_us)]` 可以限定生效窗口。返回的 `InterferenceRef` 可以作为 `StaticAttenuation` 或 `TimedAttenuation` 的 `tx`，用于设置干扰源到指定接收端的路径损耗。

### WLAN 活动模型

```python
topo.add_wlan_interference(
    channel=6,
    power_dbm=20.0,
    config_set="VideoConf",
)
```

必须使用 `channel=1..14` 或 `center_freq_mhz=<MHz>` 中的一种方式指定频点。`config_set` 选择流量模型，例如 `VoIP`、`VideoConf`、`FileDownload`、`100`、`50`、`25`、`10` 或 `5`。

### 回放干扰

```python
topo.add_playback_interference("path/to/recording")
```

传入的路径是文件名前缀。BabbleSim 要求该前缀对应的 `.Tx.csv`、`.Rx.csv`、`.RSSI.csv` 和 `.CCA.csv` 文件同时存在。

### 参数约束

BSWorld 在启动 BabbleSim 进程前校验 RF 参数：

| 类型 | 参数 | 有效范围 |
|---|---|---|
| 突发干扰 / WLAN | `center_freq_mhz` | `> 2273.0` 且 `< 2527.0` MHz。接口使用绝对频率；常用 Bluetooth LE 广播信道对应 2402、2426 和 2480 MHz。 |
| 突发干扰 / WLAN | `power_dbm` | `-128.0 .. 127.0` dBm。 |
| 突发干扰 / WLAN | `windows` | 最多 256 个窗口。每个 `(start_us, end_us)` 都必须以整数微秒表示，满足 `start_us > 0` 和 `end_us > start_us`，而且各窗口必须严格递增、互不重叠。`[]` 表示全程生效。 |
| 突发干扰 / WLAN | `rand_seed` | 无符号 32 位整数，`0 .. 4294967295`。 |
| 突发干扰 / WLAN | `interf_type` / `mod_type` | `"CW"`、`"WLAN"`、`"BLE"`、`"WN1"`、`"WN2"`、`"WN4"`、`"WN8"`、`"WN16"`、`"WN20"`、`"WN40"`、`"WN80"`。 |
| WLAN | `channel` | Wi-Fi 信道 `1 .. 14`；信道 14 对应 2484 MHz。 |
| WLAN | `config_set` | `"VoIP"`、`"VideoConf"`、`"FileDownload"`、`"FileDownload_24M"`、`"FileDownload_12M"`、`"FileDownload_6M"`、`"100"`、`"100_24M"`、`"100_12M"`、`"100_6M"`、`"50"`、`"50_24M"`、`"50_12M"`、`"50_6M"`、`"25"`、`"25_24M"`、`"25_12M"`、`"25_6M"`、`"10"`、`"10_24M"`、`"10_12M"`、`"10_6M"`、`"5"`。 |
| 信道 | `default_db` / `extra_db` | `-100.0 .. 100.0` dB。 |
| 信道 | `StaticAttenuation.attenuation_db` | 任意有限的 dB 值。该值会写入矩阵文件，作为指定路径的衰减值。 |
| 信道 | `TimedAttenuation.times_us` | 以整数微秒表示，必须 `>= 0`、严格递增，并且至少包含两个时间点。 |
| 信道 | `TimedAttenuation.attenuation_db` | 与 `times_us` 等长的有限 dB 值序列，相邻时间点之间采用线性插值。 |

## 使用 pytest 描述拓扑

`topo` fixture 将拓扑声明、RF 条件、业务操作和断言组织在同一个 Python 测试中。下例由套件内的 `peripheral` 和 `central` fixture 在测试开始前登记业务程序进程。RF 配置接口只登记预期状态；执行 `await topo.start()` 后，框架才会创建对应的运行实例。

```python
async def test_central_receives_notification(topo, peripheral, central):
    topo.set_channel_attenuation(default_db=45.0)

    await topo.start()

    await peripheral.wait_log(r"Advertising successfully started")
    await central.wait_log(r"Connected")
    await central.wait_log(r"\[NOTIFICATION\]")
```

图 2 将示例代码映射到实际执行过程：测试 fixture 创建 Controller、挂载 Host-only 业务程序并登记进程，`set_channel_attenuation()` 配置共享 PHY 上的 RF 路径。阶段 1 对应 `topo.start()`；HCI socket 和全部 BabbleSim 参与者就绪后，该调用才会返回。后续阶段对应三个 `wait_log()`，event loop 推进仿真时间，业务程序依次完成广播、连接和通知交互。

<div align="center"><img src="../images/pytest-topology-execution.svg" alt="pytest 拓扑展开为确定性的 Bluetooth LE 执行" width="80%"><br><em>图 2：从 pytest 拓扑到确定性的 Bluetooth LE 执行</em><br><br></div>

图的下半部分展示了日志断言对应的 HCI 命令、事件和仿真空口数据，使业务行为能够与协议交互直接关联。测试只需描述可观察结果；BSWorld 负责设备标识与 socket 分配、进程生命周期、时间推进以及日志和协议跟踪文件的保存。
