# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""One Bumble observer synchronizes with two independent periodic advertisers."""

import asyncio
from contextlib import AsyncExitStack

from bscontroller.hosts.bumble import advertising, scanning

from . import runtime
from .common import Reports, synchronization

NAMES = ('periodic-a', 'periodic-b', 'observer')


async def run(devices):
    observer = devices['observer']
    sources = [devices[name] for name in NAMES[:-1]]
    async with AsyncExitStack() as stack:
        reports = stack.enter_context(Reports(observer))
        for sid, source in enumerate(sources, 1):
            await stack.enter_async_context(
                advertising(
                    source,
                    legacy=False,
                    sid=sid,
                    periodic_interval=80 + sid * 20,
                )
            )
        await stack.enter_async_context(scanning(observer))
        await reports.wait([source.name for source in sources])
        syncs = [
            await stack.enter_async_context(
                synchronization(
                    observer,
                    source,
                    sid=sid,
                    name=source.name,
                )
            )
            for sid, source in enumerate(sources, 1)
        ]
        await asyncio.gather(
            *(
                sync.wait([source.name], after=sync.counts.copy())
                for sync, source in zip(syncs, sources)
            )
        )
        return {
            'syncs': len(syncs),
            'periodic_reports': {
                source.name: sync.counts[source.name]
                for source, sync in zip(sources, syncs)
            },
        }


def main():
    runtime.main(run, NAMES, __doc__)


if __name__ == '__main__':
    main()
