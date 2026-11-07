# NuttX BTSak Tests

[English](README.md) | [简体中文](README.zh-CN.md)

These tests run the stock NuttX `btsak`. Each process connects to its own BSController, the default [Virtual Controller](../../../../docs/controllers.md), and is driven by pytest through shell commands as an advertiser or a scanner.

| Executable | Upstream configuration | Executable variable |
|---|---|---|
| BTSak | NuttX `sim:bthcisock` | `BSWORLD_NUTTX_BTSAK_BIN` |

## Prepare the Environment

Prepare the `$NUTTX_DIR` and `$NUTTX_APPS_DIR` workspaces using the [NuttX test environment](../README.md#prepare-the-environment), and build BabbleSim.

## Build and Run

Configure `sim:bthcisock` with the BabbleSim clock enabled and the SMP self-test disabled. Keep `BSIM_OUT_PATH` and `BSIM_COMPONENTS_PATH` exported from the BabbleSim build:

```sh
source "$BSWORLD_ROOT/.venv/bin/activate"
cd "$NUTTX_DIR"
# Only needed when this tree was previously configured.
if [ -f .config ]; then make distclean; fi
./tools/configure.sh -l -a "$(realpath --relative-to="$NUTTX_DIR" "$NUTTX_APPS_DIR")" sim:bthcisock
kconfig-tweak --file .config -e SIM_BSIM_TIME -d BLUETOOTH_SMP_SELFTEST
make olddefconfig
```

Build the executable:

```sh
make -j"$(nproc)"
export BSWORLD_NUTTX_BTSAK_BIN="$NUTTX_DIR/nuttx"
```

Run the tests:

```sh
cd "$BSWORLD_ROOT"
pytest tests/integration/nuttx/btsak -q
```

| File | Scenarios |
|---|---|
| [test_btsak_scan_advertise.py](test_btsak_scan_advertise.py) | Discovery of one and multiple advertisers, staged shell commands, targeted CW interference and recovery |

## Adding Scenarios

```python
from tests.integration.nuttx.btsak.devices import NUTTX_BTSAK_ADVERTISER
from tests.integration.nuttx.common import create_nuttx_process

advertiser = create_nuttx_process(
    topo, NUTTX_BTSAK_ADVERTISER, log_subdir="nuttx/advertisers/advertiser"
)
advertiser.attach(topo.add_controller(public_address="C0:FF:EE:00:00:01"))
await topo.start()
await advertiser.shell.run_many(("ifup bnep0", "bt bnep0 advertise start"))
```

| Role | Shell commands |
|---|---|
| Advertiser | `ifup bnep0`, `bt bnep0 advertise start` |
| Scanner | `ifup bnep0`, `bt bnep0 scan start`, then repeated `sleep 1` and `bt bnep0 scan get` |

[devices.py](devices.py) provides the advertiser and scanner profiles. `log_subdir` selects the process output directory; the existing tests use `nuttx/advertisers/<name>/` and `nuttx/scanners/<name>/`; see [artifacts](../../../../docs/artifacts.md) for the rest.

## License

The tests in this directory use Apache License 2.0. See the [parent license notes](../README.md#license).
