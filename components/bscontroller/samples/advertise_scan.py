# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Bumble broadcaster and observer over two BSControllers."""

from bscontroller.hosts.bumble import advertising, scanning

from . import runtime
from .common import Reports


async def run(devices):
    advertiser, observer = devices['advertiser'], devices['observer']
    with Reports(observer) as reports:
        async with advertising(advertiser), scanning(observer):
            await reports.wait([advertiser.name], count=1)
            return {'advertisements': dict(reports.counts), 'host': 'bumble'}


def main():
    runtime.main(run, ('advertiser', 'observer'), __doc__)


if __name__ == '__main__':
    main()
