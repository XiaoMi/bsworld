# bt_virt_controller

[English](README.md) | [中文](README.zh-CN.md)

`bt_virt_controller` is Xiaomi's Virtual Controller implementation for BSWorld. It is an out-of-tree Zephyr Application built for [nrf52_bsim](https://docs.zephyrproject.org/latest/boards/native/nrf_bsim/doc/nrf52_bsim.html). It runs the complete HCI interface and Link Layer protocol logic, and participates in radio transmission, reception, and RF interaction through the BabbleSim PHY. The Virtual Controller exposes a bidirectional H4 transport over a Unix socket and an optional, non-blocking BTSnoop capture socket.

Application source, configuration, and build instructions live here. The executable stays in the Zephyr workspace and is selected with `BSWORLD_BT_VIRT_CONTROLLER_BIN`.

## Runtime Contract

BSWorld starts one Virtual Controller per HCI socket:

```text
"$BSWORLD_BT_VIRT_CONTROLLER_BIN" \
  -s=<sim_id> \
  -d=<device_id> \
  [-hci-poll-interval=<microseconds>] \
  -hci-socket=<hci_socket_path> \
  [-bd-addr=<AA:BB:CC:DD:EE:FF>] \
  [-btsnoop-socket=<btsnoop_socket_path>]
```

- `-s` and `-d` identify the BabbleSim simulation and Controller device.
- `-hci-poll-interval` sets the HCI socket poll interval and applies the same value to the PHY maximum resynchronization offset. It defaults to 1,000 microseconds (1 ms).
- `-hci-socket` carries bidirectional H4 HCI frames.
- `-bd-addr` optionally initializes the Controller public address before the Host connects.
- `-btsnoop-socket` enables a best-effort capture channel that never blocks the HCI data path.

## Build

The recommended [Docker test image](../../docker/README.md#use-a-published-image) includes the Controller. The commands below are for native Linux builds.

Build BabbleSim as described in [README build instructions](../../README.md#build-and-test), retaining `BSWORLD_ROOT`, `BSIM_OUT_PATH`, `BSIM_COMPONENTS_PATH`, and `BSIM_BASE_PATH`. For a native setup, see the [Zephyr environment guide](../../tests/integration/zephyr/samples/common/source/README.md) and set `ZEPHYR_WORKSPACE`.

Use the official [west build](https://docs.zephyrproject.org/latest/develop/west/build-flash-debug.html) command:

```sh
source "$ZEPHYR_WORKSPACE/.venv/bin/activate"
cd "$ZEPHYR_WORKSPACE"
west build -p always -b nrf52_bsim \
  -d "$ZEPHYR_WORKSPACE/build/bsworld_bt_virt_controller" \
  "$BSWORLD_ROOT/components/bt_virt_controller"
export BSWORLD_BT_VIRT_CONTROLLER_BIN="$ZEPHYR_WORKSPACE/build/bsworld_bt_virt_controller/zephyr/zephyr.exe"
"$BSWORLD_BT_VIRT_CONTROLLER_BIN" --help
```

Before running pytest, switch back to the BSWorld Python environment in the same shell:

```sh
source "$BSWORLD_ROOT/.venv/bin/activate"
```

The default uses the open-source Zephyr Bluetooth Controller. An equivalent nRF Connect SDK build follows that SDK's own license terms.

## License and Dependencies

The BSWorld source in this directory is owned by Xiaomi Corporation and licensed under Apache License 2.0. Zephyr and BabbleSim retain their own licenses; see [Third-Party Notices](../../THIRD_PARTY_NOTICES.md).
