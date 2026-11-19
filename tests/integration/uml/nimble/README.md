# UML NimBLE Linux Test

[English](README.md) | [简体中文](README.zh-CN.md)

The test runs the upstream Apache NimBLE Linux Host example in a [UML guest](../README.md). It attaches the Controller as `hci0` and starts the program with HCI device index `0`. A Bumble central discovers the `nimble` advertiser, connects and disconnects.

## Build and Run

The [UML test environment](../README.md#prepare-the-environment) uses the i386 executable built in the [Dockerfile's](../../../../docker/linux.Dockerfile) `nimble-linux` stage. Set `BSWORLD_UML_NIMBLE_BIN` to override the default guest path `/opt/bsworld/nimble-linux`.

Run the test from the repository root.

```sh
docker run --rm --security-opt seccomp=docker/uml-seccomp.json \
  bsworld-linux:dev pytest tests/integration/uml/nimble -q
```

The guest console and HCI capture are under `uml/nimble/`. [Test artifacts](../../../../docs/artifacts.md)
