# UML BlueZ Tests

[English](README.md) | [简体中文](README.zh-CN.md)

Runs BlueZ in the [UML Linux guest](../README.md) and drives it through `bluetoothctl`. Bumble is the peer.

## Scenarios

| Test | Verified behavior |
|---|---|
| `test_uml_bluez_initializes_virtual_controller` | `hci0` registers with the expected public address and powers on. |
| `test_uml_bluez_advertises_to_bumble_observer` | A Bumble observer receives BlueZ's connectable advertising with the expected name and address. |
| `test_uml_bluez_scans_and_connects_bumble_peripheral` | BlueZ discovers a Bumble peripheral, connects and disconnects. |

## Run

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/uml/bluez -q
```

The guest console is under `uml/bluez/uml_bluez.log`; see [artifacts](../../../../docs/artifacts.md) for the rest.

## License

The tests in this directory use Apache License 2.0. See the [parent license notes](../README.md#license).
