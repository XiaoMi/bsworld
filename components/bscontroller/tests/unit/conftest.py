# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Controller-only fixtures; no qualification peers or verdict algorithms."""

import asyncio
import os

import pytest

from bscontroller import hci
from bscontroller.hci.backend import RadioBackend
from bscontroller.hci.controller import Controller
from bscontroller.linklayer.connection import Connection
from bscontroller.radio.babblesim import PhyConnection
from tests.support import LE_EVENT_MASK_V1
from tests.unit.helpers import PARAMETERS, FakeRadio


@pytest.fixture
def controller():
    async def create():
        return Controller(
            'test',
            backend=RadioBackend(FakeRadio()),
            public_address='F0:F1:F2:F3:F4:01',
        )

    result = asyncio.run(create())
    result.events = []
    result.send_hci_packet = result.events.append
    result.event_mask = (1 << 64) - 1
    result.le_event_mask = LE_EVENT_MASK_V1
    return result


@pytest.fixture
def connection(controller):
    result = Connection(
        controller.backend.link,
        PARAMETERS,
        hci.Address('F0:F1:F2:F3:F4:02'),
        hci.Role.PERIPHERAL,
        0,
    )
    controller.backend.link.connections[result.handle] = result
    return result


@pytest.fixture
def endpoints(tmp_path):
    if not hasattr(os, 'pipe2'):
        pytest.skip('FIFO tests require os.pipe2')
    read_request, write_request = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    read_response, write_response = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    device = PhyConnection(tmp_path, 0)
    device.read_fd, device.write_fd = read_response, write_request
    phy = PhyConnection(tmp_path, 0)
    phy.read_fd, phy.write_fd = read_request, write_response
    try:
        yield device, phy
    finally:
        for fd in (read_request, write_request, read_response, write_response):
            os.close(fd)
