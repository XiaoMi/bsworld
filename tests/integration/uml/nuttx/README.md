# UML NuttX Tests

[English](README.md) | [简体中文](README.zh-CN.md)

BTSak and NimBLE run as NuttX `sim` programs in UML guests and exchange Bluetooth traffic with Bumble peers.

## Prepare the Environment

Both suites use the shared [UML test environment](../README.md#prepare-the-environment). The `nuttx-uml` stage builds both i386 executables from pinned NuttX and NuttX Apps revisions. Find the revisions and build dependencies in the [Dockerfile](../../../../docker/linux.Dockerfile).

| Suite | NuttX configuration | Default Guest executable | Override |
|---|---|---|---|
| BTSak | `sim:bthcisock` | `/opt/bsworld/nuttx-btsak` | `BSWORLD_UML_NUTTX_BIN` |
| NimBLE | `sim:nimble` | `/opt/bsworld/nuttx-nimble` | `BSWORLD_UML_NUTTX_NIMBLE_BIN` |

To select another executable, set its override to an absolute path inside the guest.

For a local build, prepare separate NuttX and NuttX Apps source trees for BTSak and NimBLE. Use absolute host paths for the source directories and point `BSWORLD_UML_ROOTFS` to a writable guest root filesystem.

Run both suites from the BSWorld repository root.

```sh
pytest tests/integration/uml/nuttx --require-integration -q
```

To save logs and captures from Docker on the host, pass `tests/integration/uml/nuttx` to the single-suite command in the [Docker guide](../../../../docker/README.md#select-a-suite).

## BTSak

Build and install from the prepared `NUTTX_DIR` and `NUTTX_APPS_DIR` worktrees.

```sh
(
  cd "$NUTTX_DIR" &&
  ./tools/configure.sh -l -a "$(realpath --relative-to=. "$NUTTX_APPS_DIR")" sim:bthcisock &&
  kconfig-tweak --file .config -e SIM_M32 -d BLUETOOTH_SMP_SELFTEST &&
  make olddefconfig &&
  make -j"$(nproc)" &&
  install -Dm755 nuttx "${BSWORLD_UML_ROOTFS:?}/opt/bsworld/nuttx-btsak"
) && pytest tests/integration/uml/nuttx/test_btsak.py --require-integration -q
```

The [BTSak tests](test_btsak.py) verify that Bumble receives connectable NuttX advertising and that NuttX detects one or two Bumble advertisers.

During targeted interference, one NuttX scanner still detects the advertiser. The targeted scanner detects it only after the interference window ends.

## NimBLE

Build and install from the prepared `NUTTX_NIMBLE_DIR` and `NUTTX_NIMBLE_APPS_DIR` worktrees.

```sh
(
  cd "$NUTTX_NIMBLE_DIR" &&
  ./tools/configure.sh -l -a "$(realpath --relative-to=. "$NUTTX_NIMBLE_APPS_DIR")" sim:nimble &&
  kconfig-tweak --file .config -e SIM_M32 \
    -d NIMBLE_PORTING_EXAMPLE -d EXAMPLES_NIMBLE \
    -e EXAMPLES_NIMBLE_BLEPRPH -e EXAMPLES_NIMBLE_BLECENT \
    -e NIMBLE_ROLE_PERIPHERAL -e NIMBLE_ROLE_CENTRAL -e NIMBLE_ROLE_OBSERVER &&
  make olddefconfig &&
  make -j"$(nproc)" &&
  install -Dm755 nuttx "${BSWORLD_UML_ROOTFS:?}/opt/bsworld/nuttx-nimble"
) && pytest tests/integration/uml/nuttx/test_nimble.py --require-integration -q
```

In the [NimBLE tests](test_nimble.py), NuttX runs as Peripheral with `bleprph`. Bumble connects, reads the Battery Level characteristic, and receives a notification.

With `blecent`, NuttX runs as Central. It connects to Bumble, discovers services, reads and writes Alert Notification characteristics, and receives a Battery Level notification.

## Artifacts

Single-guest tests save the console log and Controller's HCI capture under `uml/nuttx/`. In the interference test, the two scanners write to `uml/nuttx-protected/` and `uml/nuttx-jammed/`.

For the Bumble captures and shared RF trace, see the [test artifact directory layout](../../../../docs/artifacts.md#directory-layout).
