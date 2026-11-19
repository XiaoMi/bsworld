# UML TrouBLE Tests

[English](README.md) | [简体中文](README.zh-CN.md)

The suite runs TrouBLE's upstream Linux Battery Service examples in a 32-bit UML Guest. The tests attach the Controller with `btattach` and open `hci0` through the Linux HCI User Channel.

## Build and Run

The Linux test image includes both executables. Select `tests/integration/uml/trouble` in the [Docker suite command](../../../../docker/README.md#select-a-suite).

| Role | Default Guest executable | Override |
|---|---|---|
| Central | `/opt/bsworld/trouble/ble_bas_central` | `BSWORLD_UML_TROUBLE_CENTRAL_BIN` |
| Peripheral | `/opt/bsworld/trouble/ble_bas_peripheral` | `BSWORLD_UML_TROUBLE_PERIPHERAL_BIN` |

Set an override to an absolute path inside the Guest to select a different executable.

For a native build, prepare the [local UML environment](../README.md#local-linux) and install `rustup`. Build and install the Linux examples with the Rust 1.97.1 i686 toolchain and the TrouBLE revision pinned in the [Dockerfile](../../../../docker/linux.Dockerfile).

```sh
rustup toolchain install 1.97.1 --profile minimal --target i686-unknown-linux-gnu
TROUBLE_DIR="$BSWORLD_ROOT/.cache/trouble"
TROUBLE_REVISION=$(sed -n 's/^ARG TROUBLE_REVISION=//p' "$BSWORLD_ROOT/docker/linux.Dockerfile")
git init "$TROUBLE_DIR"
git -C "$TROUBLE_DIR" fetch --depth=1 https://github.com/embassy-rs/trouble.git "${TROUBLE_REVISION:?}"
git -C "$TROUBLE_DIR" checkout --detach FETCH_HEAD

(
  cd "$TROUBLE_DIR/examples/linux" &&
  CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1 \
    cargo +1.97.1 build --locked --release --target i686-unknown-linux-gnu \
      --bin ble_bas_central --bin ble_bas_peripheral &&
  install -Dm755 -t "${BSWORLD_UML_ROOTFS:?}/opt/bsworld/trouble" \
    target/i686-unknown-linux-gnu/release/ble_bas_central \
    target/i686-unknown-linux-gnu/release/ble_bas_peripheral
)
```

Run the suite from the repository root in the BSWorld virtual environment.

```sh
pytest tests/integration/uml/trouble --require-integration -q
```

## Test Coverage

[test_trouble.py](test_trouble.py) exercises the Battery Service with both Host roles.

- With TrouBLE as Peripheral, Bumble connects, discovers the Battery Service, reads Battery Level, and receives notifications.
- With TrouBLE as Central, the TrouBLE example connects to Bumble and performs the same discovery, read, and notification flow.

## Artifacts

The Guest console log is `uml/trouble/trouble.log`. TrouBLE's Controller captures are in the same directory, and the Bumble captures are in `bumble/central/` or `bumble/peripheral/`. See [Logs, Traces, and Captures](../../../../docs/artifacts.md) for the per-test output directory and file formats.
