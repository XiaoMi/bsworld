# BSWorld NuttX BTSak Tests

[English](README.md) | [中文](README.zh-CN.md)

This directory contains end-to-end tests for the stock NuttX `btsak` Application. The tests run one NuttX image that includes `btsak`, then use process shell commands to choose the role for each process.

Application build instructions and artifact environment variables are documented in [source/README.md](source/README.md). For first-time NuttX environment setup, start with [../README.md](../README.md).

## Files

- `devices.py`: reusable `NuttXProcessProfile` definitions for scanner and advertiser roles. Both roles use the same NuttX image.
- `test_btsak_scan_advertise.py`: end-to-end scenarios covering one scanner discovering one or more advertisers.
- `source/`: build inputs and details for the stock NuttX image.

`NuttXProcessProfile` entries set `controller_address`. BSWorld passes that address to the Virtual Controller before the Host connects.

## Shell Commands

NuttX profiles enable the process shell by default. After `await topo.start()` establishes the initial PHY synchronization boundary, the test issues role commands through the process shell. Advertiser processes run:

```sh
ifup bnep0
bt bnep0 advertise start
```

Scanner processes run:

```sh
ifup bnep0
bt bnep0 scan start
sleep 1
bt bnep0 scan get
...
```

The scanner test waits for non-zero addresses printed by `bt bnep0 scan get`. The stock `btsak` advertiser currently uses a fixed scan-response name, so the test validates discovery through scan table entries instead of custom names. Role commands are defined in the test and can be issued together with `shell.run_many(...)` or individually with `shell.run(...)`.

## Test Cases

- `test_nuttx_btsak_scanner_finds_advertiser`: one scanner discovers one advertiser.
- `test_nuttx_btsak_shell_commands_can_stage_scan`: the test starts both process shells, then issues role commands in explicit stages.
- `test_nuttx_btsak_scanner_finds_multiple_advertisers`: one scanner discovers two advertisers.
- `test_nuttx_btsak_targeted_interference_isolates_and_recovers`: three timed CW sources cover advertising channels 37, 38, and 39. Per-path attenuation places them next to one scanner while isolating a second scanner; the protected scanner keeps receiving during the interference window, and the jammed scanner recovers after the window closes.

The interference case uses the same unmodified NuttX image as the other cases. Only the BSWorld RF topology changes.

## Run

We recommend the [Docker test image](../../../../docker/README.md#select-a-suite), using `tests/integration/nuttx/btsak` as the pytest target.

For native Linux, compile with [source/README.md](source/README.md), retain the BabbleSim and Controller variables, then switch back to the BSWorld Python environment and run:

```sh
source "$BSWORLD_ROOT/.venv/bin/activate"
export BSWORLD_NUTTX_BTSAK_BIN="$NUTTX_DIR/nuttx"
cd "$BSWORLD_ROOT"
pytest tests/integration/nuttx/btsak -q --no-ellisys
```
