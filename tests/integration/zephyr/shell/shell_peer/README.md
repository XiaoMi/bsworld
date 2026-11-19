# Zephyr Shell Peer Tests

[English](README.md) | [简体中文](README.zh-CN.md)

The fixtures in [conftest.py](conftest.py) start Zephyr Bluetooth shells on separate BSControllers. `shell_pair` provides a Peripheral and Central, and `shell_trio` adds a periodic advertiser.

## Build and Run

Build the shell using the [Bluetooth shell guide](../README.md#build-and-run), then run the tests.

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/zephyr/shell/shell_peer -n auto -q
```

| File | Scenarios |
|---|---|
| [test_scan.py](test_scan.py) | Passive scanning of non-connectable advertising and active scanning with SCAN_RSP |
| [test_gatt.py](test_gatt.py) | Encryption, discovery, subscription, reads and notifications |
| [test_link_updates.py](test_link_updates.py) | PHY, data length (both roles), Connection Update, Connection Parameters Request and Channel Map updates, then a 200-octet notification |
| [test_coded.py](test_coded.py) | LE Coded connection (`coded no-1m`) and Peripheral-initiated PHY update |
| [test_privacy.py](test_privacy.py) | Bonding, LTK reuse, Controller RPA resolution and directed advertising with `dir-rpa` |
| [test_sync_transfer.py](test_sync_transfer.py) | Periodic Advertising Set Info Transfer and Sync Transfer from either role |
| [test_radio.py](test_radio.py) | Directional interference and recovery with one advertiser and two scanners |

## Adding Scenarios

```python
from tests.integration.zephyr.shell.shell_peer.common import connect, disconnect


async def test_reconnects(shell_pair):
    peripheral, central = shell_pair
    for _ in range(2):
        await connect(peripheral, central)
        await disconnect(peripheral, central)
```

`connect()` uses the Peripheral's identity for advertising and returns the Central's address as reported by the Peripheral. See [common.py](common.py) for the shared shell operations.

## License

The tests in this directory use Apache License 2.0. See the [parent license notes](../README.md#license).
