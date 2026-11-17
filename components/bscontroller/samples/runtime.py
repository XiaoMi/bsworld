# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Shared PHY, Controller, Host and simulation-clock ownership for examples."""

import argparse
import asyncio
import inspect
import json
import shutil
import uuid
from contextlib import AsyncExitStack, ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

from bscontroller import RadioController, RadioEventLoop, RadioSession
from bscontroller.hosts.bumble import open_device
from bscontroller.runtime import PhyProcess


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def argument_parser(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--bsim-dir', type=Path, help='BabbleSim build (BSIM_OUT_PATH)')
    parser.add_argument(
        '--log-dir', type=Path, help='retain PHY and per-device HCI logs'
    )
    parser.add_argument(
        '--timeout',
        type=positive_int,
        default=30,
        help='simulation seconds, including Host initialization',
    )
    return parser


async def _run(scenario, names, args, sim_id, log_dir, socket_dir):
    controllers = [
        RadioController(
            name,
            f'F0:F1:F2:{index >> 16 & 255:02X}:'
            f'{index >> 8 & 255:02X}:{index & 255:02X}',
            hci_socket=socket_dir / f'{index}.sock',
            hci_snoop_file=log_dir / f'{name}.snoop',
        )
        for index, name in enumerate(names, 1)
    ]
    (log_dir / 'devices.json').write_text(
        json.dumps(
            {
                'sim_id': sim_id,
                'clock_dev_id': len(names),
                'devices': [
                    {
                        'dev_id': index,
                        'name': name,
                        'address': controller.public_address,
                        'hci': f'{name}.snoop',
                    }
                    for index, (name, controller) in enumerate(zip(names, controllers))
                ],
            },
            indent=2,
        )
        + '\n'
    )
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            RadioSession(sim_id, list(enumerate(controllers)), clock_dev_id=len(names))
        )
        async with asyncio.timeout(args.timeout):
            devices = {}
            for name, controller in zip(names, controllers):
                devices[name] = await stack.enter_async_context(
                    open_device(
                        name, str(controller.public_address), controller.hci_socket
                    )
                )
            result = await scenario(devices)
            (log_dir / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result, sort_keys=True))


def _collect_air(phy, log_dir):
    if phy.results_dir.is_dir():
        shutil.move(str(phy.results_dir), log_dir / 'air')


def run(scenario, names, args):
    """Run one finite example with actual Hosts and radios; clean up on failure."""
    sim_id = (
        f'bscontroller_{Path(inspect.getfile(scenario)).stem}_{uuid.uuid4().hex[:12]}'
    )
    with ExitStack() as resources:
        temporary = Path(
            resources.enter_context(TemporaryDirectory(prefix='bsc-sample-'))
        )
        log_dir = args.log_dir / sim_id if args.log_dir else temporary
        log_dir.mkdir(parents=True, exist_ok=True)
        phy = PhyProcess(
            sim_id,
            len(names) + 1,
            directory=args.bsim_dir,
            log_dir=log_dir,
            dump=bool(args.log_dir),
        )
        if args.log_dir:
            resources.callback(_collect_air, phy, log_dir)
        resources.enter_context(phy)
        with asyncio.Runner(loop_factory=RadioEventLoop) as runner:
            runner.run(_run(scenario, names, args, sim_id, log_dir, temporary))
        if args.log_dir:
            print(f'Logs: {log_dir}')


def main(scenario, names, description):
    run(scenario, names, argument_parser(description).parse_args())
