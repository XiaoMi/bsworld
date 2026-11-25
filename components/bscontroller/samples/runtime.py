# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Start a BabbleSim PHY and connect each sample Host to its Controller."""

import argparse
import asyncio
import json
import sys
import uuid
from contextlib import AsyncExitStack, ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

from bumble.device import Device
from bumble.hci import Address
from bumble.transport import open_transport

from bscontroller import EllisysCapture, RadioController, RadioEventLoop, RadioSession
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
        '--log-dir', type=Path, help='retain PHY logs, HCI and RF captures'
    )
    parser.add_argument(
        '--timeout',
        type=positive_int,
        default=30,
        help='simulation seconds, including Host initialization',
    )
    return parser


async def _run(scenario, names, args, sim_id, log_dir, socket_dir, capture):
    controllers = [
        RadioController(
            name,
            f'F0:F1:F2:{index >> 16 & 255:02X}:'
            f'{index >> 8 & 255:02X}:{index & 255:02X}',
            hci_socket=socket_dir / f'{index}.sock',
            hci_snoop_file=log_dir / f'{name}.snoop',
            rf_pcap_file=log_dir / f'{name}.pcapng' if capture else None,
            ellisys_capture=capture,
        )
        for index, name in enumerate(names, 1)
    ]
    if capture:
        devices_info = [
            {
                'dev_id': index,
                'name': name,
                'address': controller.public_address,
                'hci': f'{name}.snoop',
                'radio': f'{name}.pcapng',
            }
            for index, (name, controller) in enumerate(zip(names, controllers))
        ]
        (log_dir / 'devices.json').write_text(
            json.dumps(
                {'sim_id': sim_id, 'clock_dev_id': len(names), 'devices': devices_info},
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
                transport = await stack.enter_async_context(
                    await open_transport(f'unix:{controller.hci_socket}')
                )
                device = Device.with_hci(
                    name,
                    Address(controller.public_address),
                    transport.source,
                    transport.sink,
                )
                stack.push_async_callback(device.power_off)
                await device.power_on()
                devices[name] = device
            result = await scenario(devices)
            if capture:
                (log_dir / 'result.json').write_text(
                    json.dumps(result, indent=2) + '\n'
                )
            print(json.dumps(result, sort_keys=True))


def run(scenario, names, args):
    """Run one finite example with actual Hosts and radios; clean up on failure."""
    sim_id = f'bscontroller_{Path(sys.argv[0]).stem}_{uuid.uuid4().hex[:12]}'
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
        )
        resources.enter_context(phy)
        capture = (
            resources.enter_context(EllisysCapture(log_dir / 'ellisys.bttrp'))
            if args.log_dir
            else None
        )
        with asyncio.Runner(loop_factory=RadioEventLoop) as runner:
            runner.run(_run(scenario, names, args, sim_id, log_dir, temporary, capture))
        if args.log_dir:
            print(f'Logs: {log_dir}')


def main(scenario, names, description):
    run(scenario, names, argument_parser(description).parse_args())
