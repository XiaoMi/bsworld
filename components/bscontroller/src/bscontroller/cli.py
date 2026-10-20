# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Independent native simulation and external-Host entry points."""

import argparse
import asyncio
import logging
import signal
import uuid
from contextlib import nullcontext
from pathlib import Path

from . import RadioController, RadioEventLoop, RadioSession, __version__
from .runtime import PhyProcess


async def serve(args):
    controller = RadioController('controller', args.address, hci_snoop_file=args.btsnoop_file, hci_socket=args.hci_socket)
    async with RadioSession(args.sim_id, [(args.dev_id, controller)], clock_dev_id=args.clock_id):
        print(f'HCI listening on {args.hci_socket}', flush=True)
        await controller.terminated


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
    for name, help_text in [('serve', 'serve H4 HCI over a Unix socket')]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument('--bsim-dir', type=Path, help='BabbleSim build output (default: BSIM_OUT_PATH)')
        command.add_argument('--sim-id', default=None)
        command.add_argument('--log-dir', type=Path)
    server = commands.choices['serve']
    server.add_argument('--hci-socket', type=Path, required=True)
    server.add_argument('--btsnoop-file', type=Path, help='write HCI BTSnoop using the Controller simulation clock')
    server.add_argument('--address', default='F0:F1:F2:F3:F4:01')
    server.add_argument('--join', action='store_true', help='join a PHY managed by the caller')
    server.add_argument('--dev-id', type=int, default=0)
    server.add_argument('--clock-id', type=int, default=1)
    args = parser.parse_args()
    if args.command == 'serve':
        if args.dev_id < 0 or args.clock_id < 0 or args.dev_id == args.clock_id:
            parser.error('Controller and clock IDs must be distinct nonnegative integers')
        if args.join and args.sim_id is None:
            parser.error('--join requires --sim-id and matching PHY device identities')
        if not args.join and (args.dev_id, args.clock_id) != (0, 1):
            parser.error('managed PHY mode uses --dev-id 0 and --clock-id 1')
    args.sim_id = args.sim_id or ('bscontroller_' + uuid.uuid4().hex[:12])
    args.log_dir = args.log_dir or Path('/tmp') / args.sim_id
    logging.basicConfig(level=logging.WARNING)
    try:
        owner = (
            nullcontext()
            if args.command == 'serve' and args.join
            else PhyProcess(
                args.sim_id,
                2,
                directory=args.bsim_dir,
                log_dir=args.log_dir,
            )
        )
        with owner, asyncio.Runner(loop_factory=RadioEventLoop) as runner:
            runner.run(run(args))
    except asyncio.CancelledError:
        return
    except KeyboardInterrupt:
        parser.exit(130)
    except (ValueError, FileExistsError, RuntimeError, TimeoutError, OSError) as error:
        parser.exit(1, f'bscontroller: {error}\n')


if __name__ == '__main__':
    main()
