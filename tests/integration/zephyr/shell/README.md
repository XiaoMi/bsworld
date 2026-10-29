# Zephyr Bluetooth Shell Tests

[English](README.md) | [简体中文](README.zh-CN.md)

These tests run two Host-only upstream Zephyr shell executables. Each process connects to its own Virtual Controller and is driven by pytest through shell commands. [source/](source/) holds the BSWorld Kconfig and HCI overlay.

| Executable | Upstream entry point | Executable variable |
|---|---|---|
| LE | `tests/bluetooth/shell` | `BSWORLD_ZEPHYR_SHELL_BIN` |
| Mesh | `tests/bluetooth/mesh_shell` | `BSWORLD_ZEPHYR_MESH_SHELL_BIN` |

The [Docker runtime image](../../../../docker/README.md#select-a-suite) includes both executables and sets the variables. The steps below are for native Linux builds.

## Prepare the Zephyr Workspace

Prepare BSWorld using the [local Linux setup](../../../../README.md#local-linux) and install the system dependencies from Zephyr's [Getting Started Guide](https://docs.zephyrproject.org/latest/develop/getting_started/index.html).

Create a separate Python environment for Zephyr and install west:

```sh
export ZEPHYR_WORKSPACE="$HOME/zephyrproject"
mkdir -p "$ZEPHYR_WORKSPACE"
python3.12 -m venv "$ZEPHYR_WORKSPACE/.venv"
source "$ZEPHYR_WORKSPACE/.venv/bin/activate"
python -m pip install west
```

Fetch the Zephyr revision pinned by the Dockerfile:

```sh
git init "$ZEPHYR_WORKSPACE/zephyr"
git -C "$ZEPHYR_WORKSPACE/zephyr" remote add origin https://github.com/zephyrproject-rtos/zephyr
git -C "$ZEPHYR_WORKSPACE/zephyr" fetch --depth=1 origin f545c1ca45bd0e5cb767d01996f5806f9f1e50be
git -C "$ZEPHYR_WORKSPACE/zephyr" checkout --detach FETCH_HEAD
```

Initialize the workspace and download its modules and Python dependencies:

```sh
west init -l "$ZEPHYR_WORKSPACE/zephyr"
cd "$ZEPHYR_WORKSPACE"
west update
west packages pip --install
west zephyr-export
```

Install the toolchain:

```sh
cd "$ZEPHYR_WORKSPACE/zephyr"
west sdk install --gnu-toolchains x86_64-zephyr-elf
```

Also build BabbleSim using the [repository instructions](../../../../README.md#build-and-test) and the [Zephyr Virtual Controller](../../../../docs/controllers.md#building-the-zephyr-controller).

## Build and Run

Set the shared paths in the Zephyr environment:

```sh
source "$ZEPHYR_WORKSPACE/.venv/bin/activate"
cd "$ZEPHYR_WORKSPACE"
SRC="$BSWORLD_ROOT/tests/integration/zephyr/shell/source"
SHELL_APP="$ZEPHYR_WORKSPACE/zephyr/tests/bluetooth/shell"
OVERLAY="$SHELL_APP/xterm-native-shell.overlay;$SRC/bsim_host_only.overlay"
```

Build the LE executable:

```sh
west build -p always -b nrf52_bsim -d build/bsworld_zephyr_shell "$SHELL_APP" -- \
  -DCONF_FILE="$SRC/prj.conf" -DEXTRA_CONF_FILE="$SRC/le.conf" \
  -DDTC_OVERLAY_FILE="$OVERLAY"
export BSWORLD_ZEPHYR_SHELL_BIN="$ZEPHYR_WORKSPACE/build/bsworld_zephyr_shell/zephyr/zephyr.exe"
```

Build the Mesh executable:

```sh
west build -p always -b nrf52_bsim -d build/bsworld_zephyr_mesh_shell \
  "$ZEPHYR_WORKSPACE/zephyr/tests/bluetooth/mesh_shell" -- \
  -DCONF_FILE="$SRC/prj.conf" -DEXTRA_CONF_FILE="$SRC/mesh.conf" \
  -DDTC_OVERLAY_FILE="$OVERLAY"
export BSWORLD_ZEPHYR_MESH_SHELL_BIN="$ZEPHYR_WORKSPACE/build/bsworld_zephyr_mesh_shell/zephyr/zephyr.exe"
```

Switch back to the BSWorld environment in the same terminal and run the tests:

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/zephyr/shell -n 2 -q
```

| File | Scenarios | Controllers |
|---|---|---|
| [test_le_features.py](test_le_features.py) | Scanning, interference, connection procedures, pairing and privacy, Coded PHY, periodic sync | Every Zephyr/BSController pairing |
| [test_mesh.py](test_mesh.py) | Provisioning, model configuration, group control | Once per Controller |

## Adding Scenarios

```python
from tests.integration.zephyr.shell.devices import add_zephyr_shell, initialize_shell

scanner = add_zephyr_shell(topo, name="scanner", address="C0:FF:EE:41:00:02")
await topo.start()
await initialize_shell(scanner)
await scanner.shell.run("bt scan passive")
await scanner.wait_log("Bluetooth passive scan enabled")
```

For the Mesh executable, pass `mesh=True` to both helpers. Process output is under `zephyr/shell/<name>/`; see [artifacts](../../../../docs/artifacts.md) for the rest.

## License

The BSWorld test code and configuration use Apache License 2.0. Zephyr retains its upstream licenses; see [Third-Party Notices](../../../../THIRD_PARTY_NOTICES.md).
