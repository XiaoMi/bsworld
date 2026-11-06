# NuttX NimBLE Tests

[English](README.md) | [简体中文](README.zh-CN.md)

These tests run the [Apache NimBLE](https://github.com/apache/mynewt-nimble) examples from nuttx-apps: `nimble_bleprph` as the Peripheral and `nimble_blecent` as the Central. Each process connects to its own Virtual Controller and is driven by pytest through shell commands.

| Executable | Upstream configuration | Executable variable |
|---|---|---|
| NimBLE | NuttX `sim:nimble` with `nimble_bleprph` and `nimble_blecent` enabled | `BSWORLD_NUTTX_NIMBLE_BIN` |

The [NuttX Docker image](../../../../docker/README.md#select-a-suite) includes the executable and sets the variable. The steps below are for native Linux builds.

## Prepare the Environment

Prepare the `$NUTTX_NIMBLE_DIR` and `$NUTTX_NIMBLE_APPS_DIR` workspaces using the [NuttX test environment](../README.md#prepare-the-environment), and build BabbleSim and the [Virtual Controller](../../../../docs/controllers.md#building-the-zephyr-controller).

## Build and Run

Configure `sim:nimble` with the BabbleSim clock, both examples, and the Peripheral, Central and Observer roles enabled. Keep `BSIM_OUT_PATH` and `BSIM_COMPONENTS_PATH` exported from the BabbleSim build:

```sh
source "$BSWORLD_ROOT/.venv/bin/activate"
cd "$NUTTX_NIMBLE_DIR"
# Only needed when this tree was previously configured.
if [ -f .config ]; then make distclean; fi
./tools/configure.sh -l -a "$(realpath --relative-to="$NUTTX_NIMBLE_DIR" "$NUTTX_NIMBLE_APPS_DIR")" sim:nimble
kconfig-tweak --file .config -e SIM_BSIM_TIME \
  -d NIMBLE_PORTING_EXAMPLE -d EXAMPLES_NIMBLE \
  -e EXAMPLES_NIMBLE_BLEPRPH -e EXAMPLES_NIMBLE_BLECENT \
  -e NIMBLE_ROLE_PERIPHERAL -e NIMBLE_ROLE_CENTRAL -e NIMBLE_ROLE_OBSERVER
make olddefconfig
```

Build the executable:

```sh
make -j"$(nproc)"
export BSWORLD_NUTTX_NIMBLE_BIN="$NUTTX_NIMBLE_DIR/nuttx"
```

Run the tests:

```sh
cd "$BSWORLD_ROOT"
pytest tests/integration/nuttx/nimble -q
```

| File | Scenarios | Controllers |
|---|---|---|
| [test_central_peripheral.py](test_central_peripheral.py) | Central scanning, connection, service discovery and notification subscription | Once per Controller |

## Adding Scenarios

```python
from tests.integration.nuttx.common import create_nuttx_process
from tests.integration.nuttx.nimble.devices import NUTTX_NIMBLE_PERIPHERAL

peripheral = create_nuttx_process(
    topo, NUTTX_NIMBLE_PERIPHERAL, log_subdir="nuttx/nimble/peripheral"
)
peripheral.attach(topo.add_controller(public_address="C0:FF:EE:20:00:01"))
await topo.start()
await peripheral.shell.run_many(("ifup bnep0", "bleprph"))
await peripheral.wait_log(r"connection established; status=0 handle=\d+")
```

Profiles describe the program name, executable and shell behavior. Creation registers the process with the supplied topology. The test separately chooses the Controller type and address through `topo.add_controller()` and binds the program with `host.attach(controller)`.

| Role | Shell commands |
|---|---|
| Peripheral | `ifup bnep0`, `bleprph` |
| Central | `ifup bnep0`, `blecent` |

[devices.py](devices.py) provides the Peripheral and Central profiles. `log_subdir` selects the process output directory; the existing tests use `nuttx/nimble/<role>/`; see [artifacts](../../../../docs/artifacts.md) for the rest.

## License

The tests in this directory use Apache License 2.0. See the [parent license notes](../README.md#license).
