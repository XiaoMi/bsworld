# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Broadcast two ISO streams to independent Bumble synchronized receivers."""

import asyncio
from contextlib import AsyncExitStack

from bumble.device import (
    BigParameters,
    BigSyncParameters,
    BisLink,
    PeriodicAdvertisingSync,
)

from bscontroller.hosts.bumble import advertising, scanning

from . import runtime

SDU_INTERVAL_US = 20000
SDU_COUNT = 20


class IsoReceiver:
    def __init__(self, number):
        self.number = number
        self.count = 0
        self.completed = asyncio.Event()

    def receive(self, packet):
        # The sample uses complete SDUs smaller than one HCI ISO buffer.
        if packet.packet_status_flag or not packet.iso_sdu_fragment:
            return
        data = packet.iso_sdu_fragment
        assert packet.pb_flag == 2 and len(data) == 64
        assert data[0] == self.number
        sequence = int.from_bytes(data[1:3], 'little')
        assert data[3:] == bytes((sequence + n) & 255 for n in range(61))
        assert sequence == self.count
        self.count += 1
        if self.count == SDU_COUNT:
            self.completed.set()


async def run(devices, *, encrypted=False, framed=False, phy=2):
    source = devices['broadcaster']
    sinks = [device for name, device in devices.items() if name != 'broadcaster']
    code = b'broadcast-sample' if encrypted else None
    async with AsyncExitStack() as stack:
        advertiser = await stack.enter_async_context(
            advertising(source, legacy=False, sid=1, periodic_interval=80)
        )
        big = await source.create_big(
            advertiser,
            BigParameters(
                num_bis=2,
                sdu_interval=SDU_INTERVAL_US,
                max_sdu=64,
                max_transport_latency=80,
                rtn=1,
                phy=1 << (phy - 1),
                framing=BigParameters.Framing.FRAMED
                if framed
                else BigParameters.Framing.UNFRAMED,
                broadcast_code=code,
            ),
        )
        stack.push_async_callback(big.terminate)
        receivers = []
        for sink in sinks:
            await stack.enter_async_context(scanning(sink))
            sync = await sink.create_periodic_advertising_sync(
                source.public_address, sid=1, sync_timeout=5
            )
            stack.push_async_callback(sync.terminate)
            synchronized = asyncio.Event()
            sync.once(PeriodicAdvertisingSync.EVENT_ESTABLISHMENT, synchronized.set)
            if sync.state != PeriodicAdvertisingSync.State.ESTABLISHED:
                await synchronized.wait()
            big_sync = await sink.create_big_sync(
                sync,
                BigSyncParameters(
                    big_sync_timeout=100, bis=[1, 2], broadcast_code=code
                ),
            )
            stack.push_async_callback(big_sync.terminate)
            for number, link in enumerate(big_sync.bis_links, 1):
                receiver = IsoReceiver(number)
                link.sink = receiver.receive
                await link.setup_data_path(BisLink.Direction.CONTROLLER_TO_HOST)
                receivers.append(receiver)
        for link in big.bis_links:
            await link.setup_data_path(BisLink.Direction.HOST_TO_CONTROLLER)
        for sequence in range(SDU_COUNT):
            for number, link in enumerate(big.bis_links, 1):
                link.write(
                    bytes([number])
                    + sequence.to_bytes(2, 'little')
                    + bytes((sequence + n) & 255 for n in range(61))
                )
            await asyncio.sleep(SDU_INTERVAL_US / 1000000)
        await asyncio.gather(*(receiver.completed.wait() for receiver in receivers))
        await asyncio.gather(*(link.drain() for link in big.bis_links))
        return {
            'bis': 2,
            'receivers': len(sinks),
            'sdus_per_stream': SDU_COUNT,
            'encrypted': encrypted,
            'framed': framed,
            'phy': phy,
        }


def main():
    parser = runtime.argument_parser(__doc__)
    parser.add_argument('--encrypted', action='store_true')
    parser.add_argument('--framed', action='store_true')
    parser.add_argument(
        '--phy',
        type=int,
        choices=(1, 2, 3),
        default=2,
        help='1=LE 1M, 2=LE 2M, 3=LE Coded',
    )
    parser.add_argument('--receivers', type=runtime.positive_int, default=2)
    args = parser.parse_args()

    async def scenario(devices):
        return await run(
            devices, encrypted=args.encrypted, framed=args.framed, phy=args.phy
        )

    runtime.run(
        scenario,
        ('broadcaster', *(f'receiver-{n}' for n in range(args.receivers))),
        args,
    )


if __name__ == '__main__':
    main()
