# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Broadcast two ISO streams to independent Bumble synchronized receivers."""

import asyncio
from contextlib import AsyncExitStack
from functools import partial

from bumble.device import (
    AdvertisingEventProperties,
    AdvertisingParameters,
    BigParameters,
    BigSyncParameters,
    PeriodicAdvertisingParameters,
)
from bumble.hci import OwnAddressType, Phy

from . import runtime

SDU_INTERVAL_US = 20_000
SDU_COUNT = 20


async def run(devices, *, encrypted=False, framed=False, phy=2):
    source = devices['broadcaster']
    sinks = [device for name, device in devices.items() if name != 'broadcaster']
    code = b'broadcast-sample' if encrypted else None
    async with AsyncExitStack() as stack:
        advertiser = await source.create_advertising_set(
            advertising_parameters=AdvertisingParameters(
                advertising_event_properties=AdvertisingEventProperties(
                    is_connectable=False
                ),
                own_address_type=OwnAddressType.PUBLIC,
                advertising_sid=1,
                primary_advertising_interval_min=40,
                primary_advertising_interval_max=60,
            ),
            periodic_advertising_parameters=PeriodicAdvertisingParameters(80, 80),
        )
        stack.push_async_callback(advertiser.remove)
        stack.push_async_callback(advertiser.stop)
        await advertiser.start_periodic()
        stack.push_async_callback(advertiser.stop_periodic)
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

        received = []
        for sink in sinks:
            await sink.start_scanning(
                active=False,
                own_address_type=OwnAddressType.PUBLIC,
                scan_interval=60,
                scan_window=30,
                scanning_phys=[Phy.LE_1M],
            )
            stack.push_async_callback(sink.stop_scanning)
            sync = await sink.create_periodic_advertising_sync(
                source.public_address, sid=1, sync_timeout=5
            )
            stack.push_async_callback(sync.terminate)
            if sync.state != sync.State.ESTABLISHED:
                ready = asyncio.Event()
                sync.once(sync.EVENT_ESTABLISHMENT, ready.set)
                await ready.wait()
            big_sync = await sink.create_big_sync(
                sync,
                BigSyncParameters(
                    big_sync_timeout=100, bis=[1, 2], broadcast_code=code
                ),
            )
            stack.push_async_callback(big_sync.terminate)
            queues = []
            for link in big_sync.bis_links:
                queue = asyncio.Queue()
                link.sink = queue.put_nowait
                await link.setup_data_path(link.Direction.CONTROLLER_TO_HOST)
                queues.append(queue)
            received.append(queues)

        for link in big.bis_links:
            await link.setup_data_path(link.Direction.HOST_TO_CONTROLLER)
        streams = [
            [bytes([bis, sequence]) + bytes(range(62)) for sequence in range(SDU_COUNT)]
            for bis in (1, 2)
        ]
        for frame in zip(*streams, strict=True):
            for link, payload in zip(big.bis_links, frame, strict=True):
                link.write(payload)
            await asyncio.sleep(SDU_INTERVAL_US / 1_000_000)
        for queues in received:
            for queue, stream in zip(queues, streams, strict=True):
                for expected in stream:
                    packet = await queue.get()
                    # An unused transmission slot can yield an empty report.
                    while not packet.iso_sdu_fragment:
                        packet = await queue.get()
                    assert packet.pb_flag == 2 and packet.packet_status_flag == 0
                    assert packet.iso_sdu_fragment == expected
        for link in big.bis_links:
            await link.drain()
        return {
            'bis': len(big.bis_links),
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
    runtime.run(
        partial(run, encrypted=args.encrypted, framed=args.framed, phy=args.phy),
        ('broadcaster', *(f'receiver-{n}' for n in range(args.receivers))),
        args,
    )


if __name__ == '__main__':
    main()
