# User Mode Linux Integration Tests

[English](README.md) | [简体中文](README.zh-CN.md)

The suites run 32-bit User Mode Linux (UML) guests on the 64-bit BSWorld runner. Each guest Host uses its own [BSController](../../../docs/controllers.md) and exchanges traffic with a Bumble peer in pytest. One topology can run several guests on the same simulation clock.

## Prepare the Environment

The [Linux test image](../../../docker/README.md#build) bundles the UML kernel, seed library, BabbleSim and an i386 guest root file system. Use it to build and run the suites from the repository root.

```sh
docker buildx bake -f docker/docker-bake.hcl --load linux
docker run --rm --security-opt seccomp=docker/uml-seccomp.json \
  bsworld-linux:dev pytest tests/integration/uml -n auto -q
```

For local builds, prepare [BSWorld and BabbleSim](../../../README.md#local-linux). The [Dockerfile](../../../docker/linux.Dockerfile) records the source revisions and build commands in the `uml`, `uml-rootfs`, `zephyr-uml`, `nuttx-uml` and `nimble-linux` stages. Build the kernel with `ARCH=um SUBARCH=i386` and the [configuration fragment](../../../src/bsworld/uml/linux.fragment). Build the [seed library](../../../src/bsworld/uml/getrandom.c) with `cc -m32 -shared -fPIC -O2`. The [rootfs script](../../../docker/uml-rootfs.sh) extracts the i386 packages with `dpkg-deb -x`, including BlueZ's `btattach`, and the Dockerfile installs the guest programs and the executable `/init`.

| Variable | Path on the BSWorld runner |
|---|---|
| `BSWORLD_UML_KERNEL` | Executable i386 UML kernel |
| `BSWORLD_UML_GETRANDOM` | i386 seed library loaded by the UML process |
| `BSWORLD_UML_ROOTFS` | Guest root directory containing `/init`, the i386 programs and their libraries |

Set those variables and run `pytest tests/integration/uml -n auto -q` in the BSWorld Python environment.

## Test Suites

| Host | Scenarios | Guest executable |
|---|---|---|
| [BlueZ](bluez/README.md) | Controller setup, concurrent advertising from two guests, scanning and connection | `/usr/bin/bluetoothctl` |
| [Zephyr native_sim](zephyr/) | Advertising, scanning, connection, GATT, pairing and privacy, link procedures, periodic advertising and sync transfer | `/opt/bsworld/zephyr-shell` via `BSWORLD_UML_ZEPHYR_BIN` |
| [NuttX sim BTSak](nuttx/test_btsak.py) | Advertising, scanning, interference and recovery | `/opt/bsworld/nuttx-btsak` via `BSWORLD_UML_NUTTX_BIN` |
| [NuttX sim NimBLE](nuttx/test_nimble.py) | Connections, Battery Service discovery and notifications in both roles | `/opt/bsworld/nuttx-nimble` via `BSWORLD_UML_NUTTX_NIMBLE_BIN` |
| [NimBLE Linux](nimble/README.md) | Advertising, discovery and connection | `/opt/bsworld/nimble-linux` via `BSWORLD_UML_NIMBLE_BIN` |

Executable overrides take absolute paths **inside the guest**. For example, an executable at `$BSWORLD_UML_ROOTFS/opt/bsworld/my-shell` is selected with `BSWORLD_UML_ZEPHYR_BIN=/opt/bsworld/my-shell`.

## Guest Runtime

The [bootstrap script](../../../src/bsworld/uml/init.sh) runs on the read-only guest root, mounts private runtime directories and starts a shell. A dedicated H4 connection maps the guest's `/dev/ttyS0` to its Controller. The tests use `btattach -B /dev/ttyS0 -P h4` to register it as `hci0`. The console shares the UML process's standard input and output.

The test loop grants clock advances to each guest through UML `time-travel=ext`. Simulation time stands still while a guest executes. A periodic guest tick handles pending console and serial input. Only the Controllers, the test clock, and any RF interference devices have BabbleSim device IDs.

## Adding Scenarios

Use [common.py](common.py) to add a Controller before `await topo.start()`, then call `open_uml_guest()` within an `AsyncExitStack`. Give each guest a distinct label and Controller. Send commands with `guest.shell.run()` and match the console with `guest.wait_log()`. The stack closes each guest before the topology is torn down.

The reusable launcher and calendar are in [bsworld.uml](../../../src/bsworld/uml). Run their [unit tests](../../unit/uml) with `pytest tests/unit/uml -q`, which requires no UML kernel or BabbleSim build.

## License

The tests and BSWorld's UML adapter use Apache License 2.0. Linux and the guest programs keep their upstream licenses. [Third-Party Notices](../../../THIRD_PARTY_NOTICES.md)
