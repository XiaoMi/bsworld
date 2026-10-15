# Zephyr Bluetooth Samples

[English](README.md) | [中文](README.zh-CN.md)

These tests run unmodified Zephyr Bluetooth samples as Host-only processes in BSWorld. A shared configuration and devicetree overlay connects each Host to a separate BSWorld Virtual Controller over a Unix socket.

| Test directory | Zephyr samples | Coverage |
|---|---|---|
| [broadcast_observe/](broadcast_observe/README.md) | `broadcaster`, `observer` | Advertising, passive scanning, and targeted three-channel interference with recovery |
| [heart_rate/](heart_rate/README.md) | `peripheral_hr`, `central_hr` | Connection, GATT discovery, and notifications |
| [periodic_advertising/](periodic_advertising/README.md) | `periodic_adv`, `periodic_sync` | Periodic advertising synchronization |
| [mesh/](mesh/README.md) | `mesh`, `mesh_provisioner` | Mesh provisioning and configuration-model control |

Each directory owns its Python tests, profiles, and optional configuration fragments.

## Environment Setup

We recommend the [published Docker image](../../../../docker/README.md#run-tests), which includes all eight Zephyr samples. Select the Zephyr suites with `pytest tests/integration/zephyr/samples`. The preparation steps below are for native Linux builds.

Prepare source and SDK from the [Zephyr workspace guide](common/source/README.md), compile BabbleSim from [README build instructions](../../../../README.md#build-and-test), then build the [Virtual Controller](../../../../components/bt_virt_controller/README.md).

Retain `BSWORLD_ROOT`, `ZEPHYR_WORKSPACE`, `BSIM_OUT_PATH`, `BSIM_COMPONENTS_PATH`, and `BSIM_BASE_PATH`, plus the test variables `BSWORLD_BSIM_DIR` and `BSWORLD_BT_VIRT_CONTROLLER_BIN`. Set these variables to your local workspace paths.

## Build and Run

Each test directory's README documents its sample-specific `west build` commands, original output paths, environment variables, and pytest command. Follow the links in the table above. Build and test in the same shell, retaining exports when switching Python environments.

After compiling every group and exporting its variables, run all groups in the BSWorld Python environment:

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/zephyr/samples -q --no-ellisys
```
