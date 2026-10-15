# Zephyr Broadcast and Observe Sample Tests

[English](README.md) | [中文](README.zh-CN.md)

Validate advertising, passive scanning, and targeted interference with recovery.

## Build

The recommended [Docker test image](../../../../../docker/README.md#run-tests) includes this group's firmware. Use its suite-selection commands with `tests/integration/zephyr/samples/broadcast_observe`. The commands below are for native Linux builds.

Prepare Zephyr, BabbleSim, and the Virtual Controller from the parent README's [environment setup](../README.md#environment-setup), retaining the workspace and build variables. These commands build the two upstream samples used by this group:

```sh
source "$ZEPHYR_WORKSPACE/.venv/bin/activate"
cd "$ZEPHYR_WORKSPACE"

west build -p always -b nrf52_bsim \
  -d "$ZEPHYR_WORKSPACE/build/bsworld_zephyr_broadcaster" \
  "$ZEPHYR_WORKSPACE/zephyr/samples/bluetooth/broadcaster" \
  -- -DEXTRA_CONF_FILE="$BSWORLD_ROOT/tests/integration/zephyr/samples/common/source/bsim.conf" \
  -DDTC_OVERLAY_FILE="$BSWORLD_ROOT/tests/integration/zephyr/samples/common/source/bsim_host_only.overlay"
export BSWORLD_ZEPHYR_BROADCASTER_BIN="$ZEPHYR_WORKSPACE/build/bsworld_zephyr_broadcaster/zephyr/zephyr.exe"

west build -p always -b nrf52_bsim \
  -d "$ZEPHYR_WORKSPACE/build/bsworld_zephyr_observer" \
  "$ZEPHYR_WORKSPACE/zephyr/samples/bluetooth/observer" \
  -- -DEXTRA_CONF_FILE="$BSWORLD_ROOT/tests/integration/zephyr/samples/common/source/bsim.conf" \
  -DDTC_OVERLAY_FILE="$BSWORLD_ROOT/tests/integration/zephyr/samples/common/source/bsim_host_only.overlay"
export BSWORLD_ZEPHYR_OBSERVER_BIN="$ZEPHYR_WORKSPACE/build/bsworld_zephyr_observer/zephyr/zephyr.exe"
```

## Run

Switch back to the BSWorld Python environment in the same shell, retaining the exported artifact variables:

```sh
source "$BSWORLD_ROOT/.venv/bin/activate"
cd "$BSWORLD_ROOT"
pytest tests/integration/zephyr/samples/broadcast_observe -q --no-ellisys
```
