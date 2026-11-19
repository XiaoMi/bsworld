# BSController

[English](../controllers.md) | 简体中文

每个蓝牙 Host 通过 Unix socket 上的 H4 HCI 连接一个 BSController worker。BSController 运行 Link Layer，并通过共享的 BabbleSim PHY 收发报文。每个 worker 生成独立的 HCI 抓包（`hciN.snoop`），共享的射频跟踪文件（`ellisys.bttrp`）由 PHY 的收发记录生成。详见[日志、跟踪文件与抓包](artifacts.md)。

## 添加 Controller

通过 `topo.add_controller()` 注册 Controller，在 `topo.start()` 后将 Host 连接到它的 `hci_sock`。

```python
controller = topo.add_controller(public_address="F0:F1:F2:F3:F4:01")
await topo.start()
```

| 参数 | 含义 |
|---|---|
| `public_address` | 公共设备地址，格式为 `AA:BB:CC:DD:EE:FF`。省略时，BSController 根据 Controller 编号生成互不相同的地址。 |
| `log_subdir` | 写入 `hciN.snoop` 的目录，相对于测试日志目录。 |
| `config` | 当前 Controller 使用的 `ControllerConfig`。 |
| `max_step_us` | worker 在空闲时采用的最大仿真步长。 |

## 配置 BSController

用 `ControllerConfig` 为各 Controller 分别设置缓冲区、列表容量、上限和蓝牙版本。字段、默认值和取值范围见[配置参考](../../components/bscontroller/README.zh-CN.md#配置)。

```python
from bscontroller import ControllerConfig

central = topo.add_controller(config=ControllerConfig(max_connections=1))
peripheral = topo.add_controller(
    config=ControllerConfig(advertising_set_count=2, acl_packet_count=16),
)
```
