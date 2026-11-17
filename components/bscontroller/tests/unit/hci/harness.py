# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Upper Tester harness for the HCI layer (Core Vol 4, Part E).

Everything crosses the real H4 byte boundary in both directions:

    Host bytes -> Controller.on_packet -> command handler -> LL service
    LL notification -> HCI event -> Controller.send_hci_packet -> Host bytes

so encoding, decoding, Command Complete/Status framing, event masks and the
host sink path are all part of every scenario.

    cmd(command, status)      send one command; return its return parameters
    cmd_status(command, s)    send an asynchronous command; expect Command Status
    acl(handle, pb, data)     send Host ACL data
    ut_rx(Event, **fields)    the next event the Host received
    ut_rx_q_is_empty()        no further event
    connection(role)          an established LL connection published to HCI
"""

from __future__ import annotations

import asyncio

from bscontroller import hci
from bscontroller.hci.backend import RadioBackend
from bscontroller.hci.controller import Controller
from bscontroller.linklayer.connection import Connection
from tests.support import LE_EVENT_MASK_V1
from tests.unit.helpers import PARAMETERS
from tests.unit.linklayer.air.harness import ScriptedRadio

ErrorCode = hci.HCI_ErrorCode
IUT_ADDRESS = hci.Address('F0:F1:F2:F3:F4:01', hci.AddressType.PUBLIC_DEVICE)
PEERS = [
    hci.Address(f'C0:11:22:33:44:{index:02X}', hci.AddressType.RANDOM_DEVICE)
    for index in range(16)
]
ALL = (1 << 64) - 1
UNKNOWN_HANDLE = 0x0EFF


class HostSink:
    """The Host end of the transport: receives raw H4 bytes."""

    def __init__(self):
        self.received: list[bytes] = []

    def on_packet(self, data: bytes) -> None:
        self.received.append(bytes(data))


class HciHarness:
    def __init__(self, *, unmask=True, address=IUT_ADDRESS, config=None, le_mask=None):
        self.loop = asyncio.new_event_loop()
        self.radio = ScriptedRadio()
        self.host = HostSink()

        async def create():
            return Controller(
                'iut',
                host_sink=self.host,
                public_address=address,
                backend=RadioBackend(self.radio),
                config=config,
            )

        self.controller = self.loop.run_until_complete(create())
        self.link = self.controller.backend.link
        self.ll = self.controller.le_procedures.ll
        self.events: list[hci.HCI_Packet] = []
        if unmask:
            le_mask = LE_EVENT_MASK_V1 if le_mask is None else le_mask
            self.cmd(hci.HCI_Set_Event_Mask_Command(ALL.to_bytes(8, 'little')))
            self.cmd(hci.HCI_Set_Event_Mask_Page_2_Command(ALL.to_bytes(8, 'little')))
            self.cmd(hci.HCI_LE_Set_Event_Mask_Command(le_mask.to_bytes(8, 'little')))

    def close(self):
        self.loop.close()

    # -- transport ---------------------------------------------------------
    def _drain(self):
        async def settle():
            # send_hci_packet schedules delivery with call_soon; a few turns
            # also deliver events scheduled by those callbacks (cancellations).
            for _ in range(3):
                await asyncio.sleep(0)

        self.loop.run_until_complete(settle())
        while self.host.received:
            raw = self.host.received.pop(0)
            event = hci.HCI_Packet.from_bytes(raw)
            event.wire = raw
            self.events.append(event)

    def send(self, packet) -> None:
        self.loop.call_soon(self.controller.on_packet, bytes(packet))
        self._drain()

    # -- Upper Tester ------------------------------------------------------
    def _reply(self, opcode):
        for event in self.events:
            if (
                isinstance(
                    event,
                    (hci.HCI_Command_Complete_Event, hci.HCI_Command_Status_Event),
                )
                and event.command_opcode == opcode
            ):
                self.events.remove(event)
                return event
        raise AssertionError(f'no reply for opcode 0x{opcode:04x}: {self.events}')

    def cmd(self, command, status=ErrorCode.SUCCESS):
        """Send a command answered by Command Complete; return its parameters."""
        self.send(command)
        reply = self._reply(command.op_code)
        assert isinstance(reply, hci.HCI_Command_Complete_Event), reply
        assert reply.num_hci_command_packets == 1
        result = reply.return_parameters
        result.wire = reply.wire
        # A Host without a schema for this opcode only sees generic bytes; the
        # status is always the first return parameter (Vol 4, Part E, 7.7.14).
        actual = getattr(result, 'status', None)
        if actual is None:
            actual = bytes(result.data)[0]
        assert actual == status, (type(command).__name__, actual, status)
        return result

    def cmd_status(self, command, status=ErrorCode.SUCCESS):
        """Send a command answered by Command Status."""
        self.send(command)
        reply = self._reply(command.op_code)
        assert isinstance(reply, hci.HCI_Command_Status_Event), reply
        assert reply.status == status, (type(command).__name__, reply.status, status)
        return reply

    @staticmethod
    def wire_handle(result) -> int:
        """Connection_Handle as sent on the wire after Status (7.7.14)."""
        return int.from_bytes(result.wire[7:9], 'little') & 0x0FFF

    def reply(self, command):
        """Send a command and return whichever reply event it produced."""
        self.send(command)
        return self._reply(command.op_code)

    def acl(self, handle, pb, data, bc=0):
        self.send(hci.HCI_AclDataPacket(handle, pb, bc, len(data), data))

    def ut_rx(self, event_type, **fields):
        self._drain()
        assert self.events, f'expected {event_type.__name__}, Host queue empty'
        event = self.events.pop(0)
        assert isinstance(event, event_type), f'expected {event_type.__name__}: {event}'
        for name, value in fields.items():
            assert getattr(event, name) == value, (name, getattr(event, name), value)
        return event

    def ut_rx_q_is_empty(self):
        self._drain()
        assert not self.events, self.events

    # -- Link Layer setup ----------------------------------------------------
    def in_loop(self, function, *args, **kwargs):
        """Run LL-side code inside the Controller's event loop, as the radio
        thread does, so events reach the Host through the real sink path.
        """

        async def call():
            return function(*args, **kwargs)

        result = self.loop.run_until_complete(call())
        self._drain()
        return result

    def emit(self, event) -> None:
        self.in_loop(self.controller.le_procedures._emit_hci_packet, event)

    def connection(self, role=hci.Role.CENTRAL, peer=None) -> Connection:
        """An established connection, published to the Host (Connection Complete
        consumed).
        """
        peer = peer or PEERS[len(self.link.connections)]
        connection = Connection(self.link, PARAMETERS, peer, role, 0)
        self.link.connections[connection.handle] = connection
        self.in_loop(connection.establish)
        self.events = [
            event
            for event in self.events
            if not isinstance(
                event,
                (
                    hci.HCI_LE_Enhanced_Connection_Complete_Event,
                    hci.HCI_LE_Connection_Complete_Event,
                    hci.HCI_LE_Channel_Selection_Algorithm_Event,
                ),
            )
        ]
        return connection

    def notify(self, method, *args, **kwargs):
        """Raise an LL notification exactly as a procedure would."""
        return self.in_loop(getattr(self.link.listener, method), *args, **kwargs)
