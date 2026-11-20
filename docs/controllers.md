# BSController

English | [简体中文](zh-CH/controllers.md)

Each Bluetooth Host uses its own BSController worker. The worker runs the Link Layer, accepts H4 traffic over a Unix socket, and exchanges packets through the shared BabbleSim PHY.

The worker writes HCI traffic to `hciN.snoop` and its radio traffic to `radioN.pcapng`. BSWorld combines Controller transmissions into `babblesim/ellisys.bttrp` for the topology. For capture scope and analysis tools, see [Logs, traces, and captures](artifacts.md).

## Adding a Controller

Register the Controller with `topo.add_controller()` before starting the topology. The Host can connect to `controller.hci_sock` once `await topo.start()` returns.

```python
controller = topo.add_controller(public_address="F0:F1:F2:F3:F4:01")
await topo.start()
```

For a UML Guest, BSWorld bridges the socket to the Guest's virtio Bluetooth Controller. The launcher setup is described in [Running Hosts in UML](writing-tests.md#running-hosts-in-uml).

| Argument | Meaning |
|---|---|
| `public_address` | Public device address in `AA:BB:CC:DD:EE:FF` format. If omitted, BSController derives a distinct address from the Controller number. |
| `log_subdir` | Directory for `hciN.snoop` and `radioN.pcapng`, relative to the test log directory. Defaults to `controllers/hciN/`. |
| `config` | Per-Controller settings supplied as a `ControllerConfig`. |
| `max_step_us` | Maximum idle simulation step in microseconds. |

## Configuring BSController

Use `ControllerConfig` to set buffers, list sizes, limits, and Bluetooth version for each Controller. Available fields, defaults, and ranges are listed in the [BSController configuration reference](../components/bscontroller/README.md#configuration).

```python
from bscontroller import ControllerConfig

central = topo.add_controller(config=ControllerConfig(max_connections=1))
peripheral = topo.add_controller(
    config=ControllerConfig(advertising_set_count=2, acl_packet_count=16),
)
```
