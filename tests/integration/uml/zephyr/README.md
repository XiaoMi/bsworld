# UML Zephyr Host Tests

[English](README.md) | [简体中文](README.zh-CN.md)

The Zephyr Bluetooth shell runs in a UML Guest and communicates with a Bumble peer through BSController.

## Build and Run

The shell's default path in the Guest is `/opt/bsworld/zephyr-shell`. To use a different executable, set `BSWORLD_UML_ZEPHYR_BIN` to its absolute path inside the Guest.

To run the suite in Docker, use the Zephyr command under [Select a Suite](../../../../docker/README.md#select-a-suite).

For a native build, prepare the [local UML environment](../README.md#local-linux). Use the Zephyr revision and modules specified in the `zephyr-uml` stage of the [Dockerfile](../../../../docker/linux.Dockerfile) to prepare the west workspace.

Set `ZEPHYR_WORKSPACE` to that workspace and activate its Python environment. Then build and install the shell from the BSWorld repository root using [prj.conf](prj.conf).

```sh
BSWORLD_CHECKOUT="$PWD"
(
  cd "$ZEPHYR_WORKSPACE" &&
  ZEPHYR_TOOLCHAIN_VARIANT=host west build -b native_sim/native \
    -d build/bsworld_uml_zephyr zephyr/tests/bluetooth/shell \
    -- -DCONF_FILE="$BSWORLD_CHECKOUT/tests/integration/uml/zephyr/prj.conf"
) && install -D "$ZEPHYR_WORKSPACE/build/bsworld_uml_zephyr/zephyr/zephyr.exe" \
  "${BSWORLD_UML_ROOTFS:?}/opt/bsworld/zephyr-shell"
```

Run the suite in the BSWorld Python environment.

```sh
pytest tests/integration/uml/zephyr --require-integration -q
```

## Test Coverage

| Test file | Scenarios |
|---|---|
| [test_discovery.py](test_discovery.py) | Active and passive scanning, discovery of Zephyr advertising, Filter Accept List connections, and a connection over LE Coded that switches to LE 2M. |
| [test_gatt.py](test_gatt.py) | Zephyr sends encrypted notifications and acts as a GATT client to discover and read Bumble attributes. |
| [test_link.py](test_link.py) | Updates to PHY, data length and connection parameters, plus GATT traffic after a channel map update. |
| [test_security.py](test_security.py) | Bonding, RPA resolution on reconnection through undirected and directed advertising, and re-encryption. |
| [test_periodic_advertising.py](test_periodic_advertising.py) | Periodic advertising and synchronization in both directions, including sync loss and recovery. |
| [test_periodic_sync_transfer.py](test_periodic_sync_transfer.py) | Zephyr sends and receives advertising set information and third-device periodic syncs in both ACL roles. |

## Artifacts

Each test's `uml/zephyr/` directory contains `zephyr.log` and the Controller's HCI BTSnoop and radio PCAPNG captures. See [Logs, Traces, and Captures](../../../../docs/artifacts.md) for output locations and formats.
