# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Expose a Controller over H4 HCI and scan advertisements with a mock Host."""

import argparse
import asyncio
import uuid
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory

from bscontroller import RadioController, RadioEventLoop, RadioSession, hci
from bscontroller.runtime import PhyProcess


class HciClient:
    """HCI source/sink for scripts; waits follow the caller's event-loop time."""

    def __init__(self, controller):
        self.controller = controller
        self.events = deque()
        self.changed = asyncio.Event()
        self._command_lock = asyncio.Lock()
        controller.set_packet_sink(self)

    def on_packet(self, packet: bytes) -> None:
        self.events.append(hci.HCI_Packet.from_bytes(packet))
        self.changed.set()

    async def wait_event(self, predicate, *, timeout: float = 5):
        async with asyncio.timeout(timeout):
            while True:
                self.changed.clear()
                for index, event in enumerate(self.events):
                    if predicate(event):
                        del self.events[index]
                        return event
                await self.changed.wait()

    async def command(self, command, *, timeout: float = 5):
        async with self._command_lock:
            self.controller.on_packet(bytes(command))
            event = await self.wait_event(
                lambda item: (
                    isinstance(item, (hci.HCI_Command_Complete_Event, hci.HCI_Command_Status_Event))
                    and item.command_opcode == command.op_code
                ),
                timeout=timeout,
            )
            parameters = getattr(event, 'return_parameters', event)
            status = parameters.status
            if status != hci.HCI_ErrorCode.SUCCESS:
                raise RuntimeError(f'{command.name}: HCI status {status}')
            return parameters

    async def initialize(self):
        await self.command(hci.HCI_Reset_Command())
        await self.command(hci.HCI_Set_Event_Mask_Command(((1 << 64) - 1).to_bytes(8, 'little')))
        await self.command(hci.HCI_LE_Set_Event_Mask_Command(((1 << 64) - 1).to_bytes(8, 'little')))


class MockHost:
    """A minimal scanning Host that only exchanges H4 bytes over a socket."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.events = deque()

    async def wait_event(self, predicate):
        async with asyncio.timeout(5):
            for index, event in enumerate(self.events):
                if predicate(event):
                    del self.events[index]
                    return event
            while True:
                # H4 event packets contain a type byte, event code and length.
                header = await self.reader.readexactly(3)
                if header[0] != hci.HCI_EVENT_PACKET:
                    raise RuntimeError(f'Expected an H4 event, received packet type {header[0]}')
                payload = await self.reader.readexactly(header[2])
                event = hci.HCI_Packet.from_bytes(header + payload)
                if predicate(event):
                    return event
                self.events.append(event)

    async def command(self, command):
        self.writer.write(bytes(command))
        await self.writer.drain()
        event = await self.wait_event(
            lambda item: (
                isinstance(item, (hci.HCI_Command_Complete_Event, hci.HCI_Command_Status_Event))
                and item.command_opcode == command.op_code
            )
        )
        parameters = getattr(event, 'return_parameters', event)
        if parameters.status != hci.HCI_ErrorCode.SUCCESS:
            raise RuntimeError(f'{command.name}: HCI status {parameters.status}')
        return parameters

    async def scan(self, address, data):
        await self.command(hci.HCI_Reset_Command())
        await self.command(hci.HCI_Set_Event_Mask_Command(b'\xff' * 8))
        await self.command(hci.HCI_LE_Set_Event_Mask_Command(b'\xff' * 8))
        await self.command(
            hci.HCI_LE_Set_Scan_Parameters_Command(
                le_scan_type=0,
                le_scan_interval=48,
                le_scan_window=48,
                own_address_type=0,
                scanning_filter_policy=0,
            )
        )
        await self.command(hci.HCI_LE_Set_Scan_Enable_Command(1, 0))
        try:
            event = await self.wait_event(
                lambda item: (
                    isinstance(item, hci.HCI_LE_Advertising_Report_Event)
                    and any(report.address == address for report in item.reports)
                )
            )
            report = next(report for report in event.reports if report.address == address)
            if bytes(report.data) != data:
                raise RuntimeError('Advertising data mismatch')
            return report
        finally:
            await self.command(hci.HCI_LE_Set_Scan_Enable_Command(0, 0))


async def run(sim_id, hci_socket):
    # A second Controller supplies actual RF advertisements for the scanner.
    advertiser = RadioController('advertiser', 'F0:F1:F2:F3:F4:01')
    scanner = RadioController('scanner', 'F0:F1:F2:F3:F4:02', hci_socket=hci_socket)
    advertising_host = HciClient(advertiser)
    async with RadioSession(sim_id, [(0, advertiser), (1, scanner)], clock_dev_id=2):
        print(f'Controller H4 HCI socket: {hci_socket}')
        reader, writer = await asyncio.open_unix_connection(hci_socket)
        host = MockHost(reader, writer)
        try:
            await advertising_host.initialize()
            address = (await advertising_host.command(hci.HCI_Read_BD_ADDR_Command())).bd_addr
            data = b'\x08\x09BSRadio'
            await advertising_host.command(
                hci.HCI_LE_Set_Advertising_Parameters_Command(
                    advertising_interval_min=32,
                    advertising_interval_max=32,
                    advertising_type=3,
                    own_address_type=0,
                    peer_address_type=0,
                    peer_address=hci.Address.ANY,
                    advertising_channel_map=7,
                    advertising_filter_policy=0,
                )
            )
            await advertising_host.command(hci.HCI_LE_Set_Advertising_Data_Command(data))
            await advertising_host.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
            try:
                report = await host.scan(address, data)
                print(f'Mock Host received {bytes(report.data).hex()} from {report.address} over H4 HCI')
            finally:
                await advertising_host.command(hci.HCI_LE_Set_Advertising_Enable_Command(0))
        finally:
            writer.close()
            await writer.wait_closed()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bsim-dir', type=Path, help='BabbleSim build output (default: BSIM_OUT_PATH)')
    args = parser.parse_args()
    sim_id = 'bscontroller_example_' + uuid.uuid4().hex[:12]
    with TemporaryDirectory(prefix='bscontroller_example_') as directory:
        with PhyProcess(sim_id, 3, directory=args.bsim_dir, log_dir=Path(directory)):
            with asyncio.Runner(loop_factory=RadioEventLoop) as runner:
                runner.run(run(sim_id, Path(directory) / 'hci.sock'))


if __name__ == '__main__':
    main()
