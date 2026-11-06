# Bumble Tests

[English](README.md) | [简体中文](README.zh-CN.md)

These tests run the [Bumble](https://github.com/google/bumble) Python Host inside the pytest asyncio event loop, sharing the simulation clock with the test. Each device connects to its own Virtual Controller. [common/](common/) holds the shared device helpers.

| Host | Upstream | Version |
|---|---|---|
| Bumble | [google/bumble](https://github.com/google/bumble) | `bumble==0.0.235`, pinned in [pyproject.toml](../../../pyproject.toml) |

Every [Docker image](../../../docker/README.md#select-a-suite) includes Bumble; the Zephyr image runs this suite by default. The steps below are for native Linux.

## Prepare the Environment

Prepare BSWorld using the [local Linux setup](../../../README.md#local-linux), which also installs Bumble, and build BabbleSim using the [repository instructions](../../../README.md#build-and-test).

The `[zephyr]` cases also need the [Zephyr Virtual Controller](../../../docs/controllers.md#building-the-zephyr-controller).

## Build and Run

Bumble needs no build step. Run the tests:

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/bumble -n auto -q
```

| File | Scenarios | Controllers |
|---|---|---|
| [broadcast_observe/test_bumble_broadcast_observe.py](broadcast_observe/test_bumble_broadcast_observe.py) | Legacy advertising and passive scanning, targeted CW interference and recovery | Once per Controller |
| [connection/test_bumble_connection.py](connection/test_bumble_connection.py) | Central and peripheral connection | Once per Controller |
| [periodic_advertising/test_bumble_periodic_advertising.py](periodic_advertising/test_bumble_periodic_advertising.py) | Periodic advertising and synchronization | Once per Controller |

## Adding Scenarios

```python
from contextlib import AsyncExitStack

from tests.integration.bumble.common import add_bumble_controller, open_bumble_device

controller = add_bumble_controller(topo, "observer", "F0:F1:F2:F3:F4:02")
await topo.start()
async with AsyncExitStack() as stack:
    observer = await open_bumble_device(
        stack, "observer", "F0:F1:F2:F3:F4:02", controller.hci_sock
    )
    await observer.start_scanning()
```

For standard two-device scenarios, use the `bumble_pair` fixture from [conftest.py](conftest.py). It creates the Controllers, configures 45 dB attenuation, starts the topology and opens both Hosts. `open_bumble_device()` only opens the HCI connection and initializes Bumble; it does not configure or start the topology. HCI captures are under `bumble/<role>/`; see [artifacts](../../../docs/artifacts.md) for the rest.

## License

- Test code: Apache License 2.0.
- Bumble: Apache License 2.0.

See [Third-Party Notices](../../../THIRD_PARTY_NOTICES.md).
