# BSController

[English](../controllers.md) | 简体中文

每个蓝牙 Host 使用各自的 BSController worker。worker 运行 Link Layer，通过 Unix socket 与 Host 交换 H4 报文，经共享的 BabbleSim PHY 收发空口报文。

worker 将 HCI 抓包写入 `hciN.snoop`，将自身空口抓包写入 `radioN.pcapng`。BSWorld 汇总同一拓扑内各 Controller 的发送记录，生成 `babblesim/ellisys.bttrp`。抓包范围和分析工具见[日志、跟踪文件与抓包](artifacts.md)。

## 添加 Controller

启动拓扑前，通过 `topo.add_controller()` 注册 Controller。`await topo.start()` 返回后，Host 即可连接 `controller.hci_sock`。

```python
controller = topo.add_controller(public_address="F0:F1:F2:F3:F4:01")
await topo.start()
```

使用 UML Guest 时，BSWorld 将该 socket 接到 Guest 的 virtio Bluetooth Controller。启动方式见[在 UML 中运行 Host](writing-tests.md#在-uml-中运行-host)。

| 参数 | 含义 |
|---|---|
| `public_address` | 公共设备地址，格式为 `AA:BB:CC:DD:EE:FF`。省略时，BSController 根据 Controller 编号生成互不相同的地址。 |
| `log_subdir` | `hciN.snoop` 和 `radioN.pcapng` 的存放目录，相对于测试日志目录，默认为 `controllers/hciN/`。 |
| `config` | 以 `ControllerConfig` 传入的 Controller 配置。 |
| `max_step_us` | worker 空闲时的最大仿真步长，单位为微秒。 |

## 配置 BSController

用 `ControllerConfig` 分别设置各 Controller 的缓冲区、列表容量、上限和蓝牙版本。字段、默认值和取值范围见 [BSController 配置参考](../../components/bscontroller/README.zh-CN.md#配置)。

```python
from bscontroller import ControllerConfig

central = topo.add_controller(config=ControllerConfig(max_connections=1))
peripheral = topo.add_controller(
    config=ControllerConfig(advertising_set_count=2, acl_packet_count=16),
)
```
