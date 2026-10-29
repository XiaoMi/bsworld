# 虚拟 Controller

[English](../controllers.md) | 简体中文

BSWorld 测试中，每个蓝牙 Host 都通过 Unix socket 上的标准 H4 HCI 连接自己专属的虚拟 Controller。Controller 运行 Link Layer，经由共享的 BabbleSim PHY 收发报文，因此不同 Host 的 Controller 能通过模拟射频互相通信。

BSWorld 提供两种实现：

| | `ZephyrController` | `BSController` |
|---|---|---|
| 实现 | Zephyr 上游 `hci_virt` sample，编译目标为 `nrf52_bsim`（[说明](#使用-zephyr-controller)） | Python Controller（[bscontroller](../../components/bscontroller/README.zh-CN.md)） |
| 运行方式 | 原生仿真进程，每个 Controller 一个进程 | pytest 进程内的 worker 线程 |
| 依赖 | `BSWORLD_BT_VIRT_CONTROLLER_BIN`（Docker 镜像已内置） | 安装 BSWorld 即可，无其他依赖 |
| 配置 | 由 Zephyr 编译配置决定 | 可按 Controller 分别设置缓冲、列表大小、上限、版本（见[下文](#配置-bscontroller)） |
| 默认 | 是 | 按测试或按 Controller 选择 |

两种实现都会为每个 Controller 生成一份 HCI 抓包（`hciN.snoop`），也都会记录到共享的射频跟踪文件（`ellisys.bttrp`）中。参见[日志、跟踪文件与抓包](artifacts.md)。

## 在测试中添加 Controller

`topo.add_controller()` 返回一个 Controller。在 `topo.start()` 之后，把 Host 连接到它的 `hci_sock`：

```python
from bsworld.devices import BSController

zephyr_ctl = topo.add_controller(public_address="F0:F1:F2:F3:F4:02")
python_ctl = topo.add_controller(
    controller_type=BSController,
    public_address="F0:F1:F2:F3:F4:01",
)
await topo.start()
# 将 Host 分别连接到 zephyr_ctl.hci_sock 和 python_ctl.hci_sock。
```

| 参数 | 含义 |
|---|---|
| `controller_type` | `ZephyrController`（默认）或 `BSController`，同一拓扑可以混用两种。 |
| `public_address` | 公共设备地址，格式为 `AA:BB:CC:DD:EE:FF`。省略时，`BSController` 根据 Controller 编号生成互不相同的地址，`ZephyrController` 保持其编译配置中的地址。 |
| `log_subdir` | 该 Controller 的 `hciN.snoop` 存放位置，相对于测试日志目录。 |
| `config` | 仅限 `BSController`：包含 Controller 自定义参数的 `ControllerConfig`，见[配置 BSController](#配置-bscontroller)。 |
| `max_step_us` | 仅限 `BSController`：worker 空闲时单步推进的最长仿真时间。 |

## 用两种 Controller 运行同一场景

覆盖 `bsworld_default_controller_type` fixture 可设置 `topo` fixture 的默认 `controller_type`，显式传入的 `controller_type` 仍然优先：

```python
import pytest
from bsworld.devices import BSController, ZephyrController


@pytest.fixture(params=[ZephyrController, BSController], ids=["zephyr", "bscontroller"])
def bsworld_default_controller_type(request):
    return request.param
```

用参数 ID 选择其中一种实现：

```sh
pytest tests/integration -n auto -k '[zephyr]'
pytest tests/integration -n auto -k '[bscontroller]'
```

## 配置 BSController

通过 `config=` 传入一个 `ControllerConfig`。字段、默认值和取值范围见 [BSController 配置说明](../../components/bscontroller/README.zh-CN.md#配置)。

```python
from bscontroller import ControllerConfig
from bsworld.devices import BSController

central = topo.add_controller(
    controller_type=BSController,
    config=ControllerConfig(max_connections=1),
)
peripheral = topo.add_controller(
    controller_type=BSController,
    config=ControllerConfig(advertising_set_count=2, acl_packet_count=16),
)
```

## 使用 Zephyr Controller

Zephyr Controller 直接使用 Zephyr 上游的 [`samples/bluetooth/hci_virt`](https://github.com/zephyrproject-rtos/zephyr/tree/main/samples/bluetooth/hci_virt)，编译目标为 [nrf52_bsim](https://docs.zephyrproject.org/latest/boards/native/nrf_bsim/doc/nrf52_bsim.html)。它运行 Zephyr Bluetooth Controller，并通过 Unix socket 提供 H4 HCI。BSWorld 只额外提供 Kconfig overlay [hci_virt_overlay.conf](../../components/hci_virt_overlay.conf)。

`ZephyrController` 会为每个 Controller 启动一次 `$BSWORLD_BT_VIRT_CONTROLLER_BIN`，输出写在测试日志目录下的 `bt_virt_controller/bt_virt_controller_hciN.log`。

### 启动参数

```text
"$BSWORLD_BT_VIRT_CONTROLLER_BIN" \
  -s=<sim_id> \
  -d=<device_id> \
  -mro=1000 \
  -RealEncryption=1 \
  -hci-socket=<hci_socket_path> \
  -btsnoop-file=<btsnoop_file_path> \
  [-bd-addr=<AA:BB:CC:DD:EE:FF>]
```

- `-s`、`-d`：BabbleSim 仿真标识和设备编号。
- `-mro`：PHY 最大重同步偏移（µs），与 sample 1 ms 的 HCI socket 轮询周期一致。
- `-RealEncryption`：使用真实 AES-CCM，需要 `BSIM_OUT_PATH` 下的 `lib/libCryptov1.so`。
- `-hci-socket`：H4 HCI socket。
- `-btsnoop-file`：BTSnoop 抓包文件。
- `-bd-addr`：Controller 的 public address。

### 编译 Zephyr Controller

[Docker 测试镜像](../../docker/README.zh-CN.md#使用已发布镜像)已包含 Controller。本机构建先按[仓库构建说明](README.md#构建与测试)构建 BabbleSim，并准备 [Zephyr 工作区](../../tests/integration/zephyr/shell/README.zh-CN.md#准备-zephyr-工作区)。

叠加 BSWorld overlay 编译 sample：

```sh
source "$ZEPHYR_WORKSPACE/.venv/bin/activate"
cd "$ZEPHYR_WORKSPACE"
west build -p always -b nrf52_bsim \
  -d "$ZEPHYR_WORKSPACE/build/bsworld_bt_virt_controller" \
  "$ZEPHYR_WORKSPACE/zephyr/samples/bluetooth/hci_virt" -- \
  -DEXTRA_CONF_FILE="$BSWORLD_ROOT/components/hci_virt_overlay.conf"
```

让 BSWorld 使用该程序：

```sh
export BSWORLD_BT_VIRT_CONTROLLER_BIN="$ZEPHYR_WORKSPACE/build/bsworld_bt_virt_controller/zephyr/zephyr.exe"
```
