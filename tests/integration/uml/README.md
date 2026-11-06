# User Mode Linux Test Environment

[English](README.md) | [简体中文](README.zh-CN.md)

Runs a Linux guest in User Mode Linux (UML): a mainline Linux kernel built as an ordinary host program, running the BlueZ installed on the host. The guest's `/dev/ttyS0` connects to a Virtual Controller, and UML time-travel synchronizes the guest clock with BabbleSim simulation time.

## Validated Versions

| Component | Revision or version |
|---|---|
| Linux | `7.4-rc1` (`f66469d9905b39d2c260a15d7d6db450f4db67c1`) |
| BlueZ (host) | `bluetoothd` 5.86, `bluetoothctl` 5.64 |

## Prepare the Environment

Build BabbleSim and the [Virtual Controller](../../../docs/controllers.md#building-the-zephyr-controller) using the [repository instructions](../../../README.md#build-and-test) first.

The guest mounts the host's root file system read-only and runs its programs, so the host needs 64-bit `bluetoothd`, `bluetoothctl`, `btattach` and `dbus-daemon`. A 64-bit UML kernel cannot run 32-bit programs; check with `file /usr/bin/dbus-daemon`.

Fetch Linux:

```sh
export UML_DIR="$HOME/bsworld-uml"
export LINUX_DIR="$UML_DIR/linux"
git init "$LINUX_DIR"
git -C "$LINUX_DIR" fetch --depth 1 https://github.com/torvalds/linux.git f66469d9905b39d2c260a15d7d6db450f4db67c1
git -C "$LINUX_DIR" checkout --detach FETCH_HEAD
```

## Build the Linux Guest

Configure the kernel with [linux.fragment](source/linux.fragment) and build it:

```sh
cd "$LINUX_DIR"
make ARCH=um defconfig
scripts/kconfig/merge_config.sh -m .config "$BSWORLD_ROOT/tests/integration/uml/source/linux.fragment"
make ARCH=um olddefconfig
make ARCH=um -j"$(nproc)"
```

UML seeds the guest's random number generator from the host at boot. Build [getrandom.c](source/getrandom/getrandom.c), which replaces that seed with a fixed one, so random addresses and keys repeat on every run:

```sh
cc -shared -fPIC -O2 -o "$UML_DIR/getrandom.so" \
  "$BSWORLD_ROOT/tests/integration/uml/source/getrandom/getrandom.c"
```

Export the artifacts:

```sh
export BSWORLD_UML_KERNEL="$LINUX_DIR/linux"
export BSWORLD_UML_GETRANDOM="$UML_DIR/getrandom.so"
```

Check the outputs:

```sh
test -x "$BSWORLD_UML_KERNEL"
test -f "$BSWORLD_UML_GETRANDOM"
```

## Runtime Data Path

The test hands the Virtual Controller socket to the guest as its `/dev/ttyS0` and registers it as `hci0` with `btattach -B /dev/ttyS0 -P h4`. The line is a socket, so it needs no baud rate or flow control. The guest console is the process's stdin and stdout. [init.sh](source/init.sh) mounts memory file systems over `/tmp`, `/run` and `/var/lib/bluetooth`, starts D-Bus and `bluetoothd` with [main.conf](source/main.conf), and opens a root shell.

The kernel boots with `time-travel=ext`, so the pytest event loop grants every advance of its clock, and simulation time stands still while the guest runs. The serial line and console cannot wake a waiting guest, so the kernel uses a periodic tick: input is handled at the next tick, at most 10 ms (HZ=100) after it arrives.

The guest's random seed, wall-clock start (2026-01-01T00:00:00Z) and kernel command line are fixed, so the guest log, HCI captures and RF trace are identical on every run. The kernel mixes its command line into its random pool, so the Controller socket always reaches the guest as the highest descriptor the process may open (`ssl0=fd:N`, N = `ulimit -n` − 1).

## Run the Tests

See [BlueZ](bluez/README.md).

## Rebuild the Configuration

After changing [linux.fragment](source/linux.fragment), rerun the configuration and build commands above in `$LINUX_DIR`.

## Adding Scenarios

Add an async pytest test under [BlueZ](bluez/README.md). Register the Controller with `add_uml_controller(topo, ...)` from [common.py](common.py) before startup. After `topo.start()`, boot the guest with `open_uml_guest(stack, topo, controller, ...)` in an `AsyncExitStack`, so the guest stops when the stack closes. Then send guest commands through `guest.shell` and verify them with `wait_log()`.

## License

- Tests and the files in `source/`: Apache License 2.0.
- Linux and the host software the guest runs: retain their own licenses.

See [Third-Party Notices](../../../THIRD_PARTY_NOTICES.md).
