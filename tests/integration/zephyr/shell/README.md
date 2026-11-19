# Zephyr Bluetooth Shell Tests

[English](README.md) | [简体中文](README.zh-CN.md)

Pytest drives the upstream Zephyr `tests/bluetooth/shell` through shell commands. Each Zephyr Host connects to its own BSController over an HCI socket. [source/](source/) holds the Kconfig files and the HCI overlay.

## Build and Run

Prepare the [Zephyr workspace](../README.md#prepare-the-zephyr-workspace), then build and export the shell executable.

```sh
source "$ZEPHYR_WORKSPACE/.venv/bin/activate"
cd "$ZEPHYR_WORKSPACE"
SRC="$BSWORLD_ROOT/tests/integration/zephyr/shell/source"
APP="$ZEPHYR_WORKSPACE/zephyr/tests/bluetooth/shell"

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

| Directory | Peer |
|---|---|
| [shell_peer/](shell_peer/README.md) | Zephyr Bluetooth shell |
| [bumble_peer/](bumble_peer/README.md) | Bumble Python Host |

## Adding Scenarios

```python
from tests.integration.zephyr.shell.devices import create_zephyr_shell
from tests.integration.zephyr.shell_common import initialize_shell

advertiser = create_zephyr_shell(topo, name="advertiser", public_address="C0:FF:EE:41:00:01")
scanner = create_zephyr_shell(topo, name="scanner", public_address="C0:FF:EE:41:00:02")
await topo.start()
for device in (advertiser, scanner):
    await initialize_shell(device)
await scanner.shell.run("bt scan passive")
await scanner.wait_log("Bluetooth passive scan enabled")
```

`create_zephyr_shell` registers the process and attaches its BSController. Find the shell output under `zephyr/shell/<name>/`. See [test artifacts](../../../../docs/artifacts.md) for the HCI and radio captures.

## License

- Test code and the configuration in `source/`: Apache License 2.0.
- Zephyr: retains its upstream licenses.

See [Third-Party Notices](../../../../THIRD_PARTY_NOTICES.md).
