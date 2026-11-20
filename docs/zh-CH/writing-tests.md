# 编写测试

[English](../writing-tests.md) | 简体中文

用 pytest 的 `topo` fixture 配置 Controller 和 RF 条件、接入蓝牙 Host，并在共享仿真时钟上验证行为。框架组成见 [BSWorld 技术架构](README.md#技术架构)。

## 快速索引

- [测试生命周期](#测试生命周期)
- [添加 Controller](#添加-controller)
- [异步等待与仿真时间](#异步等待与仿真时间)
- [在 UML 中运行 Host](#在-uml-中运行-host)
- [向进程发送 Shell 命令](#向进程发送-shell-命令)
- [RF 条件与干扰](#rf-条件与干扰)
- [使用 pytest 描述拓扑](#使用-pytest-描述拓扑)

## 测试生命周期

`topo` fixture 为每个测试提供 `BluetoothTopology`。

1. 通过 `topo.add_controller()` 为各 Host 分配 Controller，并配置 RF 条件。
2. 调用 `await topo.start()` 启动 PHY、Controller、干扰源和仿真时钟。
3. 启动 UML Guest 并通过控制台验证行为。Bumble 等 Python Host 可从测试的 asyncio 事件循环连接 Controller 的 HCI socket。
4. 测试结束后，fixture 停止 Guest、Controller 和 PHY，并保存日志与抓包。

fixture 为各测试分配独立的仿真标识、Unix socket 目录和日志目录。并行运行时，各测试的 Controller 状态、RF 状态和输出文件相互隔离。

## 添加 Controller

用 `topo.add_controller()` 为每个 Host 分配一个虚拟 Controller。`topo.start()` 返回后，将 Host 连接到对应 Controller 的 `hci_sock`。

```python
ctl = topo.add_controller(public_address="F0:F1:F2:F3:F4:01")
await topo.start()
# 将 Host 连接到 ctl.hci_sock。
```

地址、缓冲区等可调参数见 [BSController 配置](controllers.md)。

## 异步等待与仿真时间

测试使用标准 asyncio 任务、Future、队列、Event、stream 和 socket。事件循环在准备阶段使用墙钟时间。`await topo.start()` 完成 BabbleSim 参与者注册后，将事件循环绑定到 BSController 提供的仿真时钟。

此后，`loop.time()`、`asyncio.sleep()` 和 `asyncio.wait_for()` 使用仿真时间。示例中的 `timeout=30` 表示最多等待 30 秒仿真时间。

```python
import asyncio

await topo.start()

await dev.wait_log("ready", timeout=30)
await asyncio.sleep(0.2)
result = await asyncio.wait_for(operation(), timeout=5)
```

调度器先处理当前仿真时刻已就绪的回调和 I/O，再推进时钟。

<div align="center"><img src="../images/bluetooth-diagram.svg" alt="一次 BSWorld 离散时间推进" width="80%"><br><em>图 1：一次确定性的离散时间推进</em><br><br></div>

`UmlGuest.wait_log()` 的超时也以仿真时间计。每次调用按到达顺序扫描日志，返回第一条与正则表达式匹配的完整行。接收到换行符后，该行才会参与匹配。扫描过的行会移出缓冲区，连续的日志断言应按预期事件顺序编写。

> [!WARNING]
> 测试中应使用异步接口，`time.sleep()` 等调用会阻塞事件循环。主机上无法避免且与协议时序无关的阻塞操作可交给 `asyncio.to_thread()`。

## 在 UML 中运行 Host

启动拓扑前为每个 Guest 添加一个 Controller。启动拓扑后，用 `bsworld.uml.UmlGuest` 上下文管理器运行 Guest。

Guest 使用指定的内核和只读根文件系统，Controller 的 H4 socket 接到 `/dev/ttyS0`。参数 `init` 使用 Guest 内的绝对路径，例如 `/init`。

```python
from bsworld.uml import UmlGuest

controller = topo.add_controller(log_subdir="uml/host")
await topo.start()

with UmlGuest(
    topo,
    controller,
    kernel=kernel,
    rootfs=rootfs,
    init="/init",
    label="host",
    log_subdir="uml/host",
) as guest:
    await guest.wait_log(r"^BSWORLD_UML_READY$")
    await guest.shell.run("btattach -B /dev/ttyS0 -P h4 &")
    await guest.wait_log(r"Device index 0 attached")
```

同一个测试可启动多个 Guest。每个 Guest 使用独立的名称、Controller、控制台和 time-travel socket。Guest 共享拓扑时钟，自身不占用 PHY 设备 ID。

测试内核和根文件系统的准备方式见 [UML 测试环境](../../tests/integration/uml/README.zh-CN.md)。

## 向进程发送 Shell 命令

Guest 控制台就绪后，用 `shell.run()` 发送命令。命令写入标准输入后，调用即返回。可用 `wait_log()` 或其他可观察条件确认命令的执行结果。

```python
await dev.shell.run("...")
await dev.wait_log(r"...", timeout=5)
```

各 Host 的命令和预期输出可参考[测试套件说明](../../tests/integration/uml/README.zh-CN.md#测试套件)。

## RF 条件与干扰

在 `await topo.start()` 前配置 RF 条件、注册 Controller 和干扰源。Controller 和干扰源共享同一个 BabbleSim PHY，默认使用 `BLE_simple` 调制解调器。RSSI、SNR 和路径衰减会影响报文接收。

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
| `static_paths` | `None` | 各条固定路径的衰减配置。 |
| `timed_paths` | `None` | 各条路径随时间变化的衰减曲线。 |

### 按路径配置衰减

示例中的 `peripheral` 和 `central` 使用已注册到拓扑的 Controller。

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

衰减按方向配置。若两个方向需要不同的损耗，分别设置 `tx -> rx` 和 `rx -> tx`。`TimedAttenuation` 中的时间点和衰减值一一对应，PHY 在相邻时间点之间采用线性插值。

`tx` 和 `rx` 可引用已注册的 `BSController`、`add_*_interference()` 返回的 `InterferenceRef` 或 BabbleSim 设备编号。

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

`windows=[]` 表示干扰在整个仿真期间生效。可用 `[(start_us, end_us)]` 指定生效窗口。

需要设置干扰源到指定接收端的路径损耗时，将返回的 `InterferenceRef` 用作 `StaticAttenuation` 或 `TimedAttenuation` 的 `tx`。

### WLAN 活动模型

```python
topo.add_wlan_interference(
    channel=6,
    power_dbm=20.0,
    config_set="VideoConf",
)
```

使用 `channel` 指定 1 到 14 的信道编号，或使用 `center_freq_mhz` 指定以 MHz 为单位的中心频率。`config_set` 用于选择流量模型，例如 `VoIP`、`VideoConf`、`FileDownload`、`100`、`50`、`25`、`10` 或 `5`。

### 回放干扰

```python
topo.add_playback_interference("path/to/recording")
```

传入的路径是文件名前缀。BabbleSim 要求该前缀对应的 `.Txv2.csv`、`.Rxv2.csv`、`.RSSI.csv` 和 `.CCA.csv` 文件同时存在。

### 参数约束

BSWorld 在启动 BabbleSim 进程前校验以下 RF 参数。

| 类型 | 参数 | 有效范围 |
|---|---|---|
| 突发干扰 / WLAN | `center_freq_mhz` | 绝对频率 `> 2273.0` 且 `< 2527.0` MHz。Bluetooth LE 主广播信道对应 2402、2426 和 2480 MHz。 |
| 突发干扰 / WLAN | `power_dbm` | `-128.0 .. 127.0` dBm。 |
| 突发干扰 / WLAN | `windows` | 最多 256 个窗口，以整数微秒表示。每个 `(start_us, end_us)` 须满足 `start_us > 0` 和 `end_us > start_us`。各窗口的开始时间必须晚于前一窗口的结束时间。`[]` 表示全程生效。 |
| 突发干扰 / WLAN | `rand_seed` | 无符号 32 位整数，`0 .. 4294967295`。 |
| 突发干扰 / WLAN | `interf_type` / `mod_type` | `"CW"`、`"WLAN"`、`"BLE"`、`"WN1"`、`"WN2"`、`"WN4"`、`"WN8"`、`"WN16"`、`"WN20"`、`"WN40"`、`"WN80"`。 |
| WLAN | `channel` | Wi-Fi 信道 `1 .. 14`。信道 14 对应 2484 MHz。 |
| WLAN | `config_set` | `"VoIP"`、`"VideoConf"`、`"FileDownload"`、`"FileDownload_24M"`、`"FileDownload_12M"`、`"FileDownload_6M"`、`"100"`、`"100_24M"`、`"100_12M"`、`"100_6M"`、`"50"`、`"50_24M"`、`"50_12M"`、`"50_6M"`、`"25"`、`"25_24M"`、`"25_12M"`、`"25_6M"`、`"10"`、`"10_24M"`、`"10_12M"`、`"10_6M"`、`"5"`。 |
| 信道 | `default_db` / `extra_db` | `-100.0 .. 100.0` dB。 |
| 信道 | `StaticAttenuation.attenuation_db` | 任意有限的 dB 值。该值会写入矩阵文件，作为指定路径的衰减值。 |
| 信道 | `TimedAttenuation.times_us` | 以整数微秒表示，必须 `>= 0`、严格递增，并且至少包含两个时间点。 |
| 信道 | `TimedAttenuation.attenuation_db` | 与 `times_us` 等长的有限 dB 值序列。PHY 在相邻时间点之间采用线性插值。 |

## 使用 pytest 描述拓扑

- Bumble 扫描同一拓扑中两个 Guest 的广播，见 [BlueZ 多 Guest 用例](../../tests/integration/uml/bluez/test_uml_bluez.py)。
- 设备间的定向衰减和干扰结束后的扫描恢复，见 [NuttX 干扰用例](../../tests/integration/uml/nuttx/test_btsak.py)。
