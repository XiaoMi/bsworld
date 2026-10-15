# Bumble Integration Tests

[English](README.md) | [中文](README.zh-CN.md)

These tests run the [Bumble](https://github.com/google/bumble) Bluetooth Host stack directly in pytest's asyncio event loop. Each Bumble device receives an independent BSWorld Virtual Controller and connects to it through a Unix-socket HCI transport.

The suite covers:

- legacy advertising and passive scanning;
- targeted CW interference across all three advertising channels, spatial isolation, and post-interference recovery;
- connection establishment between a Bumble central and peripheral;
- extended periodic advertising, periodic synchronization, and periodic report reception.

We recommend the [published Docker image](../../../docker/README.md#run-tests). Select this suite with `pytest tests/integration/bumble`.

Bumble is installed with BSWorld. For native Linux, follow the repository README's [local Linux setup](../../../README.md#local-linux) to prepare the environment and build BabbleSim and the Virtual Controller. Retain `BSWORLD_BSIM_DIR` and `BSWORLD_BT_VIRT_CONTROLLER_BIN`, then run:

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/bumble -n auto
```

The Applications execute inside pytest, so their timers, socket operations, and timeouts use the same simulation-aware asyncio event loop as the test. The Virtual Controllers remain independent Linux processes. Per-device BTSnoop files are stored under the test's `bumble/<role>/` log directories, and RF traffic is included in the shared BabbleSim trace.
