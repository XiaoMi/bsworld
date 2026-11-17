# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Three devices advertise and scan simultaneously."""

import asyncio
from contextlib import AsyncExitStack

from bscontroller.hosts.bumble import advertising, scanning

from . import runtime
from .common import Reports


async def run(devices):
    async with AsyncExitStack() as stack:
        reports = {
            name: stack.enter_context(Reports(device))
            for name, device in devices.items()
        }
        for device in devices.values():
            await stack.enter_async_context(advertising(device))
            await stack.enter_async_context(scanning(device))
        await asyncio.gather(
            *(reports[name].wait(devices.keys() - {name}, count=1) for name in devices)
        )
        return {
            'advertisements': {
                name: dict(report.counts) for name, report in reports.items()
            }
        }


def main():
    runtime.main(run, ('scanner-a', 'scanner-b', 'scanner-c'), __doc__)


if __name__ == '__main__':
    main()
