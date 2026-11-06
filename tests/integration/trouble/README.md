# TrouBLE Tests

[English](README.md) | [简体中文](README.zh-CN.md)

These tests run two [TrouBLE](https://github.com/embassy-rs/trouble) Rust Hosts as native Linux processes. Each process connects to its own Virtual Controller and is verified by pytest through its log. [`source/src/bsim/`](source/src/bsim) provides the Unix transport, executor and BabbleSim clock.

| Executable | Upstream entry point | Executable variable |
|---|---|---|
| `ble_bas` | `trouble-example-apps` `ble_bas_central` / `ble_bas_peripheral` at `43a14413508eb63a114df672f9a93dab624b717d` | `BSWORLD_TROUBLE_NATIVE_BIN`, default `source/target/debug/ble_bas` |

The [TrouBLE Docker image](../../../docker/README.md#select-a-suite) includes the executable and sets the variable. The steps below are for native Linux builds.

## Prepare the Rust Toolchain

Prepare BSWorld using the [local Linux setup](../../../README.md#local-linux) and build BabbleSim using the [repository instructions](../../../README.md#build-and-test).

Install Rust with [rustup](https://rustup.rs/). The first Cargo command installs the toolchain pinned by [rust-toolchain.toml](source/rust-toolchain.toml).

The `[zephyr]` case also needs the [Zephyr Virtual Controller](../../../docs/controllers.md#building-the-zephyr-controller).

## Build and Run

Build the executable:

```sh
cd "$BSWORLD_ROOT"
cargo build --locked --manifest-path tests/integration/trouble/source/Cargo.toml
```

Run the tests:

```sh
source .venv/bin/activate
pytest tests/integration/trouble -q
```

| File | Scenarios | Controllers |
|---|---|---|
| [test_trouble_native.py](test_trouble_native.py) | Advertising and connection, Battery Service discovery and read, notifications | Once per Controller |

Run the adapter unit tests:

```sh
cargo test --locked --manifest-path tests/integration/trouble/source/Cargo.toml
```

## Adding Scenarios

```python
central = _create_host(topo, "central")
peripheral = _create_host(topo, "peripheral")
for host in (central, peripheral):
    host.attach(topo.add_controller(log_subdir=f"trouble/{host.label}"))
await topo.start()
await peripheral.wait_log(r"\[adv\] connection established")
await central.wait_log(r"Read value: \d+")
```

`_create_host()` is defined in [test_trouble_native.py](test_trouble_native.py); role behavior lives in the Rust executable. Process output is under `trouble/<role>/`; see [artifacts](../../../docs/artifacts.md) for the rest.

## License

- Test code and `source/src/main.rs`: Apache License 2.0.
- Adapter in `source/src/bsim/`: retains its original MIT OR Apache-2.0 license.
- TrouBLE: MIT or Apache License 2.0.

Review [Third-Party Notices](../../../THIRD_PARTY_NOTICES.md) before distributing binaries.
