# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Lower Tester / Upper Tester harness for LL Control Procedure scenarios.

The vocabulary follows Zephyr's ``tests/bluetooth/controller/ctrl_*`` suites so
that every scenario reads as the same fixed sequence of steps:

    event_prepare()        start a connection event; Instants are applied here
    lt_rx(opcode, ...)     the Lower Tester (peer) receives the IUT's next PDU
    lt_tx(opcode, ...)     the Lower Tester transmits a PDU to the IUT
    tx_ack()               the Lower Tester acknowledges the IUT's last PDU
    event_done()           close the connection event; connEventCounter += 1
    ut_rx(event, ...)      the Upper Tester (Host) receives an HCI event
    lt_rx_q_is_empty()     the IUT has no LL Control PDU waiting for the air
    ut_rx_q_is_empty()     the Host has no further notification

Packets travel through the real ConnectionEvent ARQ, so SN/NESN, pause gates,
Instant finalization at first transmission and encryption are all exercised. The
Host side observes real HCI events produced by the notification adapter.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, replace

from bscontroller import crypto, hci
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    LL_EVENT_COUNTER_MASK,
    PDU_DATA_LLID_MASK,
    PDU_DATA_MD_MASK,
    PDU_DATA_NESN_MASK,
    PDU_DATA_SN_MASK,
    PDU_HEADER_SIZE,
    ControlOpcode,
    LlId,
)
from bscontroller.linklayer.connection import Connection
from bscontroller.linklayer.connection_parameters import PARAM_REQUEST, UPDATE
from bscontroller.linklayer.encryption import ENC_REQUEST, ENC_RESPONSE
from bscontroller.linklayer.packet_security import PacketCipher
from tests.unit.helpers import PARAMETERS, host_notifications, take_host_notification

Opcode = ControlOpcode
ErrorCode = hci.HCI_ErrorCode
Role = hci.Role

CENTRAL = hci.Role.CENTRAL
PERIPHERAL = hci.Role.PERIPHERAL
ROLES = (CENTRAL, PERIPHERAL)

PROCEDURE_COLLISION = (
    ErrorCode.LMP_ERROR_TRANSACTION_COLLISION_OR_LL_PROCEDURE_COLLISION_ERROR
)
PEER = hci.Address('F0:F1:F2:F3:F4:02')
LTK = bytes.fromhex('99AD1B5226A37E3E058E3B8E27C2C666')
PHY_1M, PHY_2M, PHY_CODED = (
    int(hci.PhyBit.LE_1M),
    int(hci.PhyBit.LE_2M),
    int(hci.PhyBit.LE_CODED),
)
UNSPECIFIED_OFFSETS = (0xFFFF,) * 6


# ---------------------------------------------------------------------------
# Control PDU encoders (Core Vol 6, Part B, 2.4.2)
# ---------------------------------------------------------------------------
def pdu(opcode: int, *fields: int | bytes) -> bytes:
    body = b''.join(
        value if isinstance(value, bytes) else bytes([value]) for value in fields
    )
    return bytes([opcode]) + body


def connection_update_ind(
    interval: int,
    instant: int,
    *,
    size: int = 1,
    offset: int = 0,
    latency: int = 0,
    timeout: int = 100,
) -> bytes:
    return pdu(
        Opcode.LL_CONNECTION_UPDATE_IND,
        UPDATE.pack(size, offset, interval, latency, timeout, instant),
    )


def connection_param(
    opcode: int,
    minimum: int,
    maximum: int | None = None,
    *,
    latency: int = 0,
    timeout: int = 100,
    periodicity: int = 0,
    reference: int = 0,
    offsets: tuple[int, ...] = UNSPECIFIED_OFFSETS,
) -> bytes:
    maximum = minimum if maximum is None else maximum
    return pdu(
        opcode,
        PARAM_REQUEST.pack(
            minimum, maximum, latency, timeout, periodicity, reference, *offsets
        ),
    )


def connection_param_req(minimum: int, maximum: int | None = None, **kw) -> bytes:
    return connection_param(Opcode.LL_CONNECTION_PARAM_REQ, minimum, maximum, **kw)


def connection_param_rsp(minimum: int, maximum: int | None = None, **kw) -> bytes:
    return connection_param(Opcode.LL_CONNECTION_PARAM_RSP, minimum, maximum, **kw)


def channel_map_ind(channel_map: int, instant: int) -> bytes:
    return pdu(
        Opcode.LL_CHANNEL_MAP_IND,
        channel_map.to_bytes(5, 'little'),
        instant.to_bytes(2, 'little'),
    )


def phy_req(tx: int, rx: int) -> bytes:
    return pdu(Opcode.LL_PHY_REQ, tx, rx)


def phy_rsp(tx: int, rx: int) -> bytes:
    return pdu(Opcode.LL_PHY_RSP, tx, rx)


def phy_update_ind(central: int, peripheral: int, instant: int) -> bytes:
    return pdu(
        Opcode.LL_PHY_UPDATE_IND, central, peripheral, instant.to_bytes(2, 'little')
    )


def length(opcode: int, rx: int, rx_time: int, tx: int, tx_time: int) -> bytes:
    return pdu(opcode, struct.pack('<HHHH', rx, rx_time, tx, tx_time))


def feature(opcode: int, features: int) -> bytes:
    return pdu(opcode, features.to_bytes(8, 'little'))


def version_ind(version: int = 9, company: int = 0x05F1, subversion: int = 1) -> bytes:
    return pdu(Opcode.LL_VERSION_IND, struct.pack('<BHH', version, company, subversion))


def unknown_rsp(opcode: int) -> bytes:
    return pdu(Opcode.LL_UNKNOWN_RSP, opcode)


def reject_ind(reason: int) -> bytes:
    return pdu(Opcode.LL_REJECT_IND, reason)


def reject_ext_ind(opcode: int, reason: int) -> bytes:
    return pdu(Opcode.LL_REJECT_EXT_IND, opcode, reason)


def terminate_ind(reason: int) -> bytes:
    return pdu(Opcode.LL_TERMINATE_IND, reason)


def enc_req(
    rand: bytes = bytes(8), ediv: int = 0, skd: bytes = bytes(8), iv: bytes = bytes(4)
) -> bytes:
    return pdu(Opcode.LL_ENC_REQ, ENC_REQUEST.pack(rand, ediv, skd, iv))


def enc_rsp(skd: bytes = bytes(range(8)), iv: bytes = bytes(range(4))) -> bytes:
    return pdu(Opcode.LL_ENC_RSP, ENC_RESPONSE.pack(skd, iv))


@dataclass(frozen=True)
class AirPdu:
    """One PDU observed by the Lower Tester."""

    llid: int
    payload: bytes
    md: bool

    @property
    def opcode(self) -> int:
        assert self.llid == LlId.CONTROL and self.payload, self
        return self.payload[0]


class LlcpHarness:
    """One IUT connection, a scripted peer (LT) and the Host (UT)."""

    def __init__(self, controller, role: int):
        self.controller = controller
        self.link = controller.backend.link
        self.radio = self.link.radio
        self.loop = asyncio.new_event_loop()
        self.connection = Connection(self.link, PARAMETERS, PEER, role, 0)
        self.link.connections[self.connection.handle] = self.connection
        self.connection.report_created()
        self.connection.establish()
        self.peer_cipher: PacketCipher | None = None
        self.air: list[AirPdu] = []
        # A Host that wants every event, including page 2 (Vol 4, Part E, 7.3.69).
        controller.event_mask_page_2 = (1 << 64) - 1
        controller.events.clear()

    def close(self) -> None:
        self.loop.close()

    # -- configuration -----------------------------------------------------
    @property
    def role(self) -> int:
        return self.connection.role

    @property
    def controls(self):
        return self.connection.controls

    @property
    def encryption(self):
        return self.connection.encryption

    @property
    def event_counter(self) -> int:
        return self.connection.event_counter

    @property
    def handle(self) -> int:
        return self.connection.handle

    def set_peer_features(self, features: int | None) -> None:
        self.connection.peer_features = features

    def set_parameters(self, **changes) -> None:
        self.connection.parameters = replace(self.connection.parameters, **changes)

    # -- Upper Tester (Host) ----------------------------------------------
    def host(self, method: str, *args, **kwargs):
        """Invoke an LL service entry point with this connection's handle."""
        service = self.controller.le_procedures.ll
        return getattr(service, method)(self.handle, *args, **kwargs)

    def host_start(
        self, method: str, *args, status=ErrorCode.SUCCESS, **kwargs
    ) -> None:
        """Admit a prepared procedure and start it, as HCI does after Command
        Status.
        """
        prepared = self.host(method, *args, **kwargs)
        assert prepared.status == status, (prepared.status, status)
        prepared.start()

    def hci(self, command) -> list:
        """Send a raw HCI command through the Controller; return produced events."""
        offset = len(self.controller.events)
        self.controller.on_packet(bytes(command))
        return self.controller.events[offset:]

    def ut_rx(self, event_type, **fields):
        """Pop the next Host notification, which must match type and fields."""
        return take_host_notification(self.controller.events, event_type, **fields)

    def ut_rx_q_is_empty(self) -> None:
        notifications = host_notifications(self.controller.events)
        assert not notifications, notifications

    def completed_packets(self) -> int:
        """Pop Number Of Completed Packets credits for this handle."""
        credits = 0
        for event in list(self.controller.events):
            if isinstance(event, hci.HCI_Number_Of_Completed_Packets_Event):
                self.controller.events.remove(event)
                credits += sum(event.num_completed_packets)
        return credits

    def acl_received(self) -> list[bytes]:
        data = []
        for event in list(self.controller.events):
            if isinstance(event, hci.HCI_AclDataPacket):
                self.controller.events.remove(event)
                data.append(event.data)
        return data

    # -- Connection events ---------------------------------------------------
    def event_prepare(self) -> None:
        self.connection.llcp.event_prepare()

    def event_done(self) -> None:
        connection = self.connection
        connection.event_counter = (connection.event_counter + 1) & (
            LL_EVENT_COUNTER_MASK
        )
        connection.anchor_us += connection.parameters.interval * (
            LL_CONN_INTERVAL_UNIT_US
        )

    def run_events(self, count: int) -> None:
        """Idle connection events: neither side has a control PDU to send."""
        for _ in range(count):
            self.event_prepare()
            self.lt_rx_q_is_empty()
            self.event_done()

    def wait_instant(self, instant: int) -> None:
        """Run idle events until the next prepare is the Instant's event."""
        while self.event_counter != instant:
            self.event_prepare()
            self.lt_rx_q_is_empty()
            self.ut_rx_q_is_empty()
            self.event_done()

    def elapse(self, us: int, *, keepalive: bool = True) -> bool:
        """Advance simulated time. With keepalive the peer kept sending empty PDUs,
        so only procedure timers (not supervision) can expire.
        """
        self.radio.now_us += us
        if keepalive:
            self.connection.last_rx_us = self.radio.now_us
        return self.connection.check_timeout()

    def expire_response_timer(self, *, early_us: int = 0) -> bool:
        """Advance to the earliest procedure deadline (minus ``early_us``)."""
        deadline = self.connection.llcp.next_deadline_us
        assert deadline is not None, 'no procedure timer is running'
        return self.elapse(deadline - early_us - self.radio.now_us)

    # -- Lower Tester ------------------------------------------------------
    def _send(self) -> AirPdu:
        connection = self.connection
        encrypted = connection.packet_security.tx_enabled
        retransmission = connection.event.sent_pending
        start = max(
            connection.radio.now_us + 1,
            connection.radio.earliest_start_us(0, connection.tx_phy, transmit=True),
        )
        self.loop.run_until_complete(connection.send(0, start))
        if connection.remote_termination is not None:
            # As the event executor does: the ACK of TERMINATE_IND ends the link.
            connection.complete(connection.remote_termination)
        wire = connection.radio.sent[-1][2]
        header, size = wire[:PDU_HEADER_SIZE]
        body = wire[PDU_HEADER_SIZE:]
        assert size == len(body)
        if encrypted and body:
            assert self.peer_cipher is not None, 'encrypted PDU without peer key'
            body = self.peer_cipher.decrypt(header, body, duplicate=retransmission)
        packet = AirPdu(
            header & PDU_DATA_LLID_MASK, body, bool(header & PDU_DATA_MD_MASK)
        )
        self.air.append(packet)
        return packet

    def lt_rx(self, opcode: int | None = None, payload: bytes | None = None) -> bytes:
        """The IUT transmits; the peer must receive this LL Control PDU."""
        packet = self._send()
        assert packet.llid == LlId.CONTROL and packet.payload, (
            f'expected control PDU 0x{opcode or 0:02x}, got {packet}'
        )
        if opcode is not None:
            assert packet.payload[0] == opcode, (
                f'expected 0x{opcode:02x}, got {packet.payload.hex()}'
            )
        if payload is not None:
            assert packet.payload == payload, (packet.payload.hex(), payload.hex())
        return packet.payload

    def lt_rx_data(self) -> AirPdu:
        """The IUT transmits a data-channel PDU that is not control."""
        packet = self._send()
        assert packet.llid != LlId.CONTROL, packet
        return packet

    def lt_rx_empty(self, *, ack: bool = True) -> None:
        """The IUT has only an Empty PDU to send (data paused or nothing queued)."""
        packet = self._send()
        assert packet.llid == LlId.CONTINUATION and not packet.payload, packet
        if ack and not self.connection.closed:
            self.tx_ack()

    def lt_rx_q_is_empty(self) -> None:
        """No LL Control PDU is waiting for (re)transmission."""
        event = self.connection.event
        llcp = self.connection.llcp
        if event.pending is not None and event.pending.llid == LlId.CONTROL:
            raise AssertionError(f'unacknowledged control PDU {event.pending}')
        waiting = [
            item.payload.hex()
            for item in event.queue
            if item.llid == LlId.CONTROL and llcp.can_send(item.llid, item.payload)
        ]
        assert not waiting, f'queued control PDUs {waiting}'

    def data_paused(self) -> bool:
        return not self.connection.llcp.can_send(LlId.START, b'data')

    def _header(self, llid: int, *, ack: bool | None) -> int:
        event = self.connection.event
        outstanding = event.pending is not None and event.sent_pending
        if ack is None:
            ack = outstanding
        nesn = event.tx_sn ^ int(bool(ack) and outstanding)
        return (
            llid
            | (PDU_DATA_SN_MASK if event.rx_nesn else 0)
            | (PDU_DATA_NESN_MASK if nesn else 0)
        )

    def lt_tx_raw(
        self,
        llid: int,
        payload: bytes,
        *,
        ack: bool | None = None,
        encrypt: bool | None = None,
        valid_crc: bool = True,
        sn: int | None = None,
    ) -> None:
        header = self._header(llid, ack=ack)
        if sn is not None:
            header = (header & ~PDU_DATA_SN_MASK) | (PDU_DATA_SN_MASK if sn else 0)
        if encrypt is None:
            encrypt = self.connection.packet_security.rx_enabled
        if encrypt and payload:
            assert self.peer_cipher is not None, 'peer has no session key'
            payload = self.peer_cipher.encrypt(header, payload)
        self.connection.accept(bytes([header, len(payload)]) + payload, valid_crc)

    def lt_tx(self, payload: bytes, **options) -> None:
        """The peer transmits an LL Control PDU. By default it also acknowledges
        the IUT's outstanding PDU, as a response always does on air.
        """
        self.lt_tx_raw(LlId.CONTROL, payload, **options)

    def lt_tx_data(self, data: bytes, *, start: bool = True, **options) -> None:
        self.lt_tx_raw(LlId.START if start else LlId.CONTINUATION, data, **options)

    def tx_ack(self) -> None:
        """The peer acknowledges with an Empty PDU."""
        event = self.connection.event
        assert event.pending is not None and event.sent_pending, 'nothing to ack'
        self.lt_tx_raw(LlId.CONTINUATION, b'', ack=True)

    # -- Encryption helpers --------------------------------------------------
    def install_peer_cipher(self) -> None:
        """Derive the session key exactly as the peer would (Vol 6, Part B,
        5.1.3.1), from the values exchanged on air.
        """
        encryption = self.encryption
        key = crypto.e(encryption.ltk, encryption.skd_c + encryption.skd_p)[::-1]
        self.peer_cipher = PacketCipher(
            key,
            encryption.iv_c + encryption.iv_p,
            central=self.connection.role == PERIPHERAL,
        )

    def enable_encryption(self) -> None:
        """Shortcut for scenarios that start on an already encrypted link."""
        connection = self.connection
        connection.encryption.ltk = LTK
        connection.encryption.skd_c = bytes(range(8))
        connection.encryption.skd_p = bytes(range(8, 16))
        connection.encryption.iv_c = bytes(range(4))
        connection.encryption.iv_p = bytes(range(4, 8))
        connection.encryption.install()
        connection.encryption.enabled = True
        connection.packet_security.tx_enabled = True
        connection.packet_security.rx_enabled = True
        connection.encryption.last_authenticated_us = self.radio.now_us
        self.install_peer_cipher()


def instant_of(payload: bytes) -> int:
    return int.from_bytes(payload[-2:], 'little')
