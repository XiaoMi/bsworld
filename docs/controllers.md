# BSController

English | [简体中文](zh-CH/controllers.md)

Each Bluetooth Host connects to a BSController worker over H4 HCI on a Unix socket. BSController runs the Link Layer and exchanges packets through the shared BabbleSim PHY. Each worker writes an HCI capture (`hciN.snoop`), and the PHY supplies the packets for the shared radio trace (`ellisys.bttrp`). See [Logs, traces, and captures](artifacts.md).

## Adding a Controller

Register a Controller with `topo.add_controller()` and connect the Host to its `hci_sock` after `topo.start()`.

```python
controller = topo.add_controller(public_address="F0:F1:F2:F3:F4:01")
await topo.start()
```

| Argument | Meaning |
|---|---|
| `public_address` | Public device address in `AA:BB:CC:DD:EE:FF` format. If omitted, BSController derives a distinct address from the Controller number. |
| `log_subdir` | Where to write `hciN.snoop`, relative to the test log directory. |
| `config` | A `ControllerConfig` for this Controller. |
| `max_step_us` | Longest simulation step the worker takes while idle. |

## Configuring BSController

Pass a `ControllerConfig` to set buffers, list sizes, limits, and Bluetooth version per Controller. See the [configuration reference](../components/bscontroller/README.md#configuration) for fields, defaults, and ranges.

```python
from bscontroller import ControllerConfig

central = topo.add_controller(config=ControllerConfig(max_connections=1))
peripheral = topo.add_controller(
    config=ControllerConfig(advertising_set_count=2, acl_packet_count=16),
)
```
