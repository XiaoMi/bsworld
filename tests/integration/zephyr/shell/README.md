# Zephyr Bluetooth Shell Tests

[English](README.md) | [简体中文](README.zh-CN.md)

These tests run the upstream Zephyr Bluetooth shell, built in two forms, and drive each process through shell commands. Complete firmware contains the Zephyr Host, Controller and radio and joins the PHY directly; Host-only firmware connects to its own Virtual Controller over an HCI socket. [source/](source/) holds the BSWorld Kconfig and HCI overlay.

| Executable | Upstream entry point | Contents | Executable variable |
|---|---|---|---|
| Complete | `tests/bluetooth/shell` | Host, Controller and radio | `BSWORLD_ZEPHYR_FULL_SHELL_BIN` |
| Host-only | `tests/bluetooth/shell` | Host; Controller over HCI | `BSWORLD_ZEPHYR_SHELL_BIN` |

## Prepare the Environment

Prepare the [Zephyr workspace](../README.md#prepare-the-zephyr-workspace), build BabbleSim using the [repository instructions](../../../../README.md#build-and-test). The [Zephyr Controller](../../../../docs/controllers.md#building-the-zephyr-controller) is optional: without it, the Zephyr Controller cases are skipped.

## Build and Run

Set the shared paths in the Zephyr environment:

```sh
source "$ZEPHYR_WORKSPACE/.venv/bin/activate"
cd "$ZEPHYR_WORKSPACE"
SRC="$BSWORLD_ROOT/tests/integration/zephyr/shell/source"
APP="$ZEPHYR_WORKSPACE/zephyr/tests/bluetooth/shell"
```

Build the complete executable:

```sh
west build -p always -b nrf52_bsim -d build/bsworld_zephyr_full_shell "$APP" -- \
  -DCONF_FILE="$SRC/prj.conf" -DEXTRA_CONF_FILE="$SRC/le.conf;$SRC/full.conf" \
  -DDTC_OVERLAY_FILE="$APP/xterm-native-shell.overlay"
export BSWORLD_ZEPHYR_FULL_SHELL_BIN="$ZEPHYR_WORKSPACE/build/bsworld_zephyr_full_shell/zephyr/zephyr.exe"
```

Build the Host-only executable:

```sh
west build -p always -b nrf52_bsim -d build/bsworld_zephyr_shell "$APP" -- \
  -DCONF_FILE="$SRC/prj.conf" -DEXTRA_CONF_FILE="$SRC/le.conf" \
  -DDTC_OVERLAY_FILE="$APP/xterm-native-shell.overlay;$SRC/bsim_host_only.overlay"
export BSWORLD_ZEPHYR_SHELL_BIN="$ZEPHYR_WORKSPACE/build/bsworld_zephyr_shell/zephyr/zephyr.exe"
```

Switch back to the BSWorld environment in the same terminal and run the tests:

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/zephyr/shell -n auto -q
```

| Directory | Peer | Topologies |
|---|---|---|
| [shell_peer/](shell_peer/README.md) | Another Zephyr shell | Complete with complete; complete with Host-only on either Controller, in both roles |
| [bumble_peer/](bumble_peer/README.md) | Bumble Python Host | Complete firmware with Bumble, once per Controller |

## Adding Scenarios

```python
from tests.integration.zephyr.shell.devices import (
    create_zephyr_firmware_shell,
    create_zephyr_shell,
)
from tests.integration.zephyr.shell_common import initialize_shell

advertiser = create_zephyr_firmware_shell(topo, name="advertiser", seed=41)
scanner = create_zephyr_shell(topo, name="scanner")
scanner.attach(topo.add_controller(public_address="C0:FF:EE:41:00:02"))
await topo.start()
for device in (advertiser, scanner):
    await initialize_shell(device)
await scanner.shell.run("bt scan passive")
await scanner.wait_log("Bluetooth passive scan enabled")
```

Complete firmware needs no Controller. A Host-only shell must be attached to one before `topo.start()`; its factory takes neither an address nor a Controller type. Add shell-to-shell scenarios by feature under `shell_peer/`, whose `shell_pair` fixture supplies the topology, and Bumble scenarios under `bumble_peer/`. Process output is under `zephyr/complete/<name>/` and `zephyr/shell/<name>/`; see [artifacts](../../../../docs/artifacts.md) for the rest.

## License

- Test code and the configuration in `source/`: Apache License 2.0.
- Zephyr: retains its upstream licenses.

See [Third-Party Notices](../../../../THIRD_PARTY_NOTICES.md).
