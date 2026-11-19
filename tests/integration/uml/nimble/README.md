# UML NimBLE Linux Test

[English](README.md) | [简体中文](README.zh-CN.md)

The suite runs the upstream Apache NimBLE Linux Host example in a UML Guest and uses Bumble as the Central.

## Build and Run

The test starts `/opt/bsworld/nimble-linux` with HCI device index `0`. To use another executable, set `BSWORLD_UML_NIMBLE_BIN` to its absolute path inside the Guest.

For a local build, first prepare the [UML test environment](../README.md#local-linux) and install `libmbedtls-dev:i386`. Set `NIMBLE_DIR` to a checkout of `NIMBLE_REVISION`, pinned in the `nimble-linux` stage of the [Dockerfile](../../../../docker/linux.Dockerfile).

```sh
make -C "${NIMBLE_DIR:?}/porting/examples/linux" CC=gcc CXX=g++ -j"$(nproc)" &&
install -Dm755 "$NIMBLE_DIR/porting/examples/linux/nimble-linux" \
  "${BSWORLD_UML_ROOTFS:?}/opt/bsworld/nimble-linux"
```

Run the suite from the repository root.

```sh
pytest tests/integration/uml/nimble --require-integration -q
```

To run the suite in Docker and keep its output on the host, see [Select a Suite](../../../../docker/README.md#select-a-suite).

## Test Coverage

[test_uml_nimble_linux_advertises_and_accepts_connection](test_uml_nimble_linux.py) checks that a Bumble Central discovers the `nimble` advertiser at `AA:BB:CC:DD:EE:FE`, connects, and disconnects.

## Artifacts

The test saves `uml_nimble.log` and the Controller's HCI capture under `uml/nimble/` in its log directory. For the full directory layout, see [Test artifacts](../../../../docs/artifacts.md).
