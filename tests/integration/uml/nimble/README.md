# UML NimBLE Linux Test

[English](README.md) | [简体中文](README.zh-CN.md)

Runs the upstream [Apache NimBLE](https://github.com/apache/mynewt-nimble) Linux Host example in the [UML Linux guest](../README.md). Bumble is the peer.

| Program | Upstream revision | Executable variable |
|---|---|---|
| `nimble-linux` | `porting/examples/linux` at `1e8ed60276f35a80ed4d4b4f8bb9d9c6fee53845` | `BSWORLD_UML_NIMBLE_BIN` |

## Prepare the Environment

Build the kernel and seed library using the [UML test environment](../README.md#prepare-the-environment), which also sets `UML_DIR`. Install the mbedtls headers (Ubuntu):

```sh
sudo apt-get install -y libmbedtls-dev
```

Fetch Apache NimBLE beside the Linux tree:

```sh
export NIMBLE_DIR="$UML_DIR/mynewt-nimble"
git init "$NIMBLE_DIR"
git -C "$NIMBLE_DIR" fetch --depth 1 https://github.com/apache/mynewt-nimble.git 1e8ed60276f35a80ed4d4b4f8bb9d9c6fee53845
git -C "$NIMBLE_DIR" checkout --detach FETCH_HEAD
```

## Build and Run

The guest runs the example from the host's file system. Upstream builds it 32-bit by default (`-m32`), so clear those flags for the 64-bit guest. `CC` and `CXX` call the compilers directly instead of through the ccache that upstream's Makefile uses:

```sh
make -C "$NIMBLE_DIR/porting/examples/linux" CC=gcc CXX=g++ \
  NIMBLE_CFLAGS= NIMBLE_LDFLAGS= -j"$(nproc)"
export BSWORLD_UML_NIMBLE_BIN="$NIMBLE_DIR/porting/examples/linux/nimble-linux"
```

Run the test:

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/uml/nimble -q
```

`test_uml_nimble_linux_advertises_and_accepts_connection`: NimBLE advertises as `nimble` from `AA:BB:CC:DD:EE:FE`, and a Bumble Central connects and disconnects.

The guest console is under `uml/nimble/uml_nimble.log`; see [artifacts](../../../../docs/artifacts.md) for the rest.

## License

The tests in this directory use Apache License 2.0. Apache NimBLE uses Apache License 2.0. See the [parent license notes](../README.md#license).
