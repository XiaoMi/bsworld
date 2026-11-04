# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Independent native simulation and external-Host entry points."""

import argparse
import asyncio
import logging
import signal
import uuid
from functools import partial
from pathlib import Path

from . import RadioController, RadioEventLoop, RadioSession, __version__
from .config import ControllerConfig
from .runtime import PhyProcess


def controller_addresses(first: str, count: int) -> list[str]:
    """Return count public addresses, counting up from first."""
    octets = first.split(':')
    if len(octets) != 6 or not all(len(octet) == 2 for octet in octets):
        raise ValueError(f'invalid Bluetooth address: {first}')
    base = int(''.join(octets), 16)
    if base + count > 1 << 48:
        raise ValueError(
            f'{count} addresses starting at {first} exceed FF:FF:FF:FF:FF:FF'
        )
    return [
        ':'.join(f'{base + index:012X}'[i : i + 2] for i in range(0, 12, 2))
        for index in range(count)
    ]


async def serve(args):
    addresses = controller_addresses(args.address, len(args.hci_socket))
    controllers = [
        RadioController(f'hci{index}', address, hci_socket=path, config=args.config)
        for index, (path, address) in enumerate(zip(args.hci_socket, addresses))
    ]
    # Controllers take PHY devices 0..n-1 and the serve clock takes device n.
    async with RadioSession(
        args.sim_id, list(enumerate(controllers)), clock_dev_id=len(controllers)
    ):
        for controller, address in zip(controllers, addresses):
            print(f'{controller.name} {address} on {controller.hci_socket}', flush=True)
        # Each Controller stops when its socket connection closes; serve ends with
        # the last. terminated also completes when a worker fails, and only close()
        # raises the error, so close each Controller as soon as it stops.
        remaining = {controller.terminated: controller for controller in controllers}
        while remaining:
            done, _ = await asyncio.wait(remaining, return_when=asyncio.FIRST_COMPLETED)
            for terminated in done:
                await remaining.pop(terminated).close()


async def run(args):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await serve(args)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main():
    parser = argparse.ArgumentParser(description='BSController Bluetooth LE Controller')
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser(
        'config', help='print a JSON configuration with every default value'
    )
    server = commands.add_parser(
        'serve', help='run Controllers on one PHY, each serving H4 HCI on a socket'
    )
    server.add_argument(
        '--bsim-dir',
        type=Path,
        help='BabbleSim build output (default: BSIM_OUT_PATH)',
    )
    server.add_argument('--sim-id', default=None)
    server.add_argument('--log-dir', type=Path)
    server.add_argument(
        '--hci-socket',
        type=Path,
        nargs='+',
        required=True,
        help='Unix socket of each Controller; give several paths for several',
    )
    server.add_argument(
        '--address',
        default='F0:F1:F2:F3:F4:01',
        help='public address of the first Controller; the others count up from it',
    )
    server.add_argument(
        '--config',
        type=Path,
        help='JSON Controller configuration (see "bscontroller config"); '
        'Controllers are named hci0, hci1, ... in its "controllers" object',
    )
    server.add_argument(
        '--realtime',
        action='store_true',
        help='keep simulation time at wall-clock time, for Hosts with real timers',
    )
    args = parser.parse_args()
    if args.command == 'config':
        print(ControllerConfig.template())
        return
    if len(set(args.hci_socket)) != len(args.hci_socket):
        parser.error('each Controller needs its own --hci-socket')
    if args.config is not None and not args.config.is_file():
        parser.error(f'configuration file not found: {args.config}')
    try:
        controller_addresses(args.address, len(args.hci_socket))
    except ValueError as error:
        parser.error(str(error))
    args.sim_id = args.sim_id or ('bscontroller_' + uuid.uuid4().hex[:12])
    args.log_dir = args.log_dir or Path('/tmp') / args.sim_id
    logging.basicConfig(level=logging.WARNING)
    try:
        phy = PhyProcess(
            args.sim_id,
            len(args.hci_socket) + 1,
            directory=args.bsim_dir,
            log_dir=args.log_dir,
        )
        loop_factory = partial(RadioEventLoop, realtime=args.realtime)
        with phy, asyncio.Runner(loop_factory=loop_factory) as runner:
            runner.run(run(args))
    except asyncio.CancelledError:
        return
    except KeyboardInterrupt:
        parser.exit(130)
    except (ValueError, FileExistsError, RuntimeError, TimeoutError, OSError) as error:
        parser.exit(1, f'bscontroller: {error}\n')


if __name__ == '__main__':
    main()
