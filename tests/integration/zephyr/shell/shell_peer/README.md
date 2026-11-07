# Zephyr Shell Peer Tests

[English](README.md) | [简体中文](README.zh-CN.md)

These tests run two Zephyr Bluetooth shells against each other. The `shell_pair` fixture in [conftest.py](conftest.py) starts every scenario in five topology and role assignments and initializes both shells. [common.py](common.py) holds the connection, disconnection and GATT notification commands.

| Parameter ID | Peripheral | Central |
|---|---|---|
| `full-full` | Complete firmware | Complete firmware |
| `full-host-zephyr-full-peripheral` | Complete firmware | Host-only + ZephyrController |
| `full-host-zephyr-full-central` | Host-only + ZephyrController | Complete firmware |
| `full-host-bscontroller-full-peripheral` | Complete firmware | Host-only + BSController |
| `full-host-bscontroller-full-central` | Host-only + BSController | Complete firmware |

## Prepare the Environment

Build both executables and export their paths using the [Bluetooth shell guide](../README.md#build-and-run). `full-full` needs only `BSWORLD_ZEPHYR_FULL_SHELL_BIN`; `full-host-zephyr-*` cases also need the Zephyr Controller and are skipped without it.

## Build and Run

The executables are built by the Bluetooth shell guide. Run the tests:

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/zephyr/shell/shell_peer -n auto -q
```

Select topologies with their parameter IDs:

```sh
pytest tests/integration/zephyr/shell/shell_peer -k 'full-full or targeted_interference' -q
pytest tests/integration/zephyr/shell/shell_peer -k full-host-zephyr -q
pytest tests/integration/zephyr/shell/shell_peer -k full-host-bscontroller -q
```

| File | Scenarios | Topologies |
|---|---|---|
| [test_scan.py](test_scan.py) | Passive scanning of non-connectable advertising; active scanning with SCAN_RSP | All five |
| [test_gatt.py](test_gatt.py) | Encryption, discovery, subscription, reads and notifications | All five |
| [test_link_updates.py](test_link_updates.py) | PHY, data length (both roles), Connection Update, Connection Parameters Request and Channel Map updates, then a 200-octet notification | All five |
| [test_coded.py](test_coded.py) | Central initiates on LE Coded only (`coded no-1m`); Peripheral-initiated PHY update | All five |
| [test_privacy.py](test_privacy.py) | Bonding, LTK reuse, Controller RPA resolution and directed advertising with `dir-rpa` | All five |
| [test_radio.py](test_radio.py) | Directional interference and recovery | Three complete firmware devices, not parameterized |

## Adding Scenarios

```python
from tests.integration.zephyr.shell.shell_peer.common import connect, disconnect


async def test_reconnects(shell_pair):
    peripheral, central = shell_pair
    for _ in range(2):
        await connect(peripheral, central)
        await disconnect(peripheral, central)
```

`shell_pair` returns started, initialized shells as `(peripheral, central)`, so a scenario written once runs in every topology. `connect()` advertises from the Peripheral's identity by default and returns the Central's address as the Peripheral reports it. Add a file per feature and list it above.

## License

The tests in this directory use Apache License 2.0. See the [parent license notes](../README.md#license).
