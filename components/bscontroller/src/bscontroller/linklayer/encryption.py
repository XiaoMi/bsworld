# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LE encryption/refresh and authenticated payload supervision, Core 5.1.3/5.1.8."""

from __future__ import annotations

import struct
from enum import Enum, auto
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidTag

from bscontroller import crypto
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_ENCRYPTION_IV_HALF_SIZE,
    LL_ENCRYPTION_RANDOM_SIZE,
    LL_ENCRYPTION_SKD_HALF_SIZE,
    PDU_DATA_AUTHENTICATED_HEADER_MASK,
    ControlOpcode,
)
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.packet_security import PacketCipher
from bscontroller.linklayer.procedure import PROCEDURE_COLLISION, ProcedureContext
from bscontroller.linklayer.state import Role
from bscontroller.linklayer.timing import LL_AUTHENTICATED_PAYLOAD_TIMEOUT_DEFAULT_US

if TYPE_CHECKING:
    from bscontroller.linklayer.connection import Connection
    from bscontroller.linklayer.listener import EncryptionListener
    from bscontroller.linklayer.procedure import ProcedureResponseTimer

Opcode = ControlOpcode
ENC_REQUEST = struct.Struct(
    f'<{LL_ENCRYPTION_RANDOM_SIZE}sH{LL_ENCRYPTION_SKD_HALF_SIZE}s{LL_ENCRYPTION_IV_HALF_SIZE}s'
)
ENC_RESPONSE = struct.Struct(
    f'<{LL_ENCRYPTION_SKD_HALF_SIZE}s{LL_ENCRYPTION_IV_HALF_SIZE}s'
)
KEY_MISSING_REJECTIONS = (
    bytes([Opcode.LL_REJECT_IND, ErrorCode.PIN_OR_KEY_MISSING_ERROR]),
    bytes(
        [
            Opcode.LL_REJECT_EXT_IND,
            Opcode.LL_ENC_REQ,
            ErrorCode.PIN_OR_KEY_MISSING_ERROR,
        ]
    ),
)


class State(Enum):
    IDLE = auto()
    ENC_RSP = auto()
    START_REQ = auto()
    KEY = auto()
    START_RSP = auto()
    PAUSE_RSP = auto()
    PAUSE_ACK = auto()
    RESTART = auto()
    REJECT = auto()


class Encryption:
    def __init__(self, connection: Connection):
        self.connection = connection
        self.state = State.IDLE
        self.enc_rsp_sent = False
        self.security = connection.packet_security
        self.enabled = False
        self.refresh = False
        self.response_timer: ProcedureResponseTimer | None = None
        self.ltk = b''
        self.rand = b''
        self.ediv = 0
        self.skd_c = self.skd_p = self.iv_c = self.iv_p = b''
        self.auth_timeout_us = LL_AUTHENTICATED_PAYLOAD_TIMEOUT_DEFAULT_US
        self.last_authenticated_us = 0
        self.ping_context: ProcedureContext | None = None

    @property
    def ping_pending(self) -> bool:
        return self.ping_context is not None and self.ping_context.active

    def stop_ping(self) -> None:
        if self.ping_context is not None:
            self.ping_context.finish()

    def set_authenticated_timeout(self, timeout_us: int) -> None:
        """A Host write restarts the running timer (Core Vol 6, Part B, 5.4)."""
        self.auth_timeout_us = timeout_us
        if self.enabled:
            self.last_authenticated_us = self.connection.radio.now_us

    @property
    def listener(self) -> EncryptionListener:
        return self.connection.link.listener

    @property
    def busy(self) -> bool:
        return self.state != State.IDLE

    @property
    def deadline_us(self) -> int:
        return self.response_timer.deadline_us if self.response_timer is not None else 0

    def enqueue(self, opcode: int, payload: bytes = b'') -> None:
        if self.busy:
            self.response_timer = self.connection.llcp.response_timer
        self.connection.enqueue_control(bytes([opcode]) + payload)

    def start(self, rand: bytes, ediv: int, ltk: bytes) -> None:
        """Host-requested encryption start, or refresh via the Encryption Pause
        procedure (Core Vol 6, Part B, 5.1.3).
        """
        self.rand, self.ediv, self.ltk = rand, ediv, ltk
        self.refresh = self.enabled
        self.response_timer = None
        if self.refresh:
            self.state = State.PAUSE_RSP
            self.enqueue(Opcode.LL_PAUSE_ENC_REQ)
        else:
            self.request()

    def request(self) -> None:
        """Central sends LL_ENC_REQ with fresh SKD_C and IV_C (Core Vol 6, Part B,
        5.1.3.1).
        """
        generator = self.connection.link.random
        self.skd_c = generator.randbytes(LL_ENCRYPTION_SKD_HALF_SIZE)
        self.iv_c = generator.randbytes(LL_ENCRYPTION_IV_HALF_SIZE)
        self.state = State.ENC_RSP
        self.response_timer = None
        self.enqueue(
            Opcode.LL_ENC_REQ,
            ENC_REQUEST.pack(self.rand, self.ediv, self.skd_c, self.iv_c),
        )

    def install(self) -> None:
        # e() exposes little-endian Bluetooth AES; AESCCM expects the AES key order.
        """Session key e(LTK, SKD), SKD = SKD_P || SKD_C, IV = IV_P || IV_C (Core Vol 6,
        Part B, 5.1.3.1).
        """
        key = crypto.e(self.ltk, self.skd_c + self.skd_p)[::-1]
        self.security.cipher = PacketCipher(
            key, self.iv_c + self.iv_p, central=self.connection.role == Role.CENTRAL
        )
        self.security.last_received = None

    def supply_key(self, ltk: bytes | None) -> None:
        """Peripheral Host's LTK reply or negative reply (Core Vol 6, Part B,
        5.1.3.1).
        """
        if ltk is None and self.refresh:
            # After an Encryption Pause the link stays paused and is closed by the ACL
            # Termination procedure with PIN or Key Missing, not by a reject PDU.
            self.state = State.REJECT
            # T_terminate now owns this failure; the aborted encryption timer must not
            # win.
            self.response_timer = None
            self.connection.terminate(
                ErrorCode.PIN_OR_KEY_MISSING_ERROR,
                status=ErrorCode.PIN_OR_KEY_MISSING_ERROR,
            )
            return
        if ltk is None:
            extended = self.connection.peer_features is not None and bool(
                self.connection.peer_features & LeFeatureMask.EXTENDED_REJECT_INDICATION
            )
            payload = bytes([Opcode.LL_ENC_REQ]) if extended else b''
            self.state = State.REJECT
            self.enqueue(
                Opcode.LL_REJECT_EXT_IND if extended else Opcode.LL_REJECT_IND,
                payload + bytes([ErrorCode.PIN_OR_KEY_MISSING_ERROR]),
            )
            return
        self.ltk = ltk
        self.install()
        self.state = State.START_RSP
        self.enqueue(Opcode.LL_START_ENC_REQ)

    def finish(self, status: int = ErrorCode.SUCCESS) -> None:
        """Complete the procedure and report Encryption Change or Key Refresh Complete
        (Vol 4, Part E, 7.7.8, 7.7.39).
        """
        self.state = State.IDLE
        self.response_timer = None
        self.enabled = not status
        self.last_authenticated_us = self.connection.radio.now_us
        self.stop_ping()
        if status and self.connection.peer_rpa is not None:
            self.connection.link.privacy.encryption_failed(self.connection.peer)
        self.listener.on_encryption_changed(self.connection, status, self.refresh)
        if status:
            self.security.tx_enabled = self.security.rx_enabled = False
            if self.refresh:
                self.connection.complete(status)

    def control(self, payload: bytes) -> bool:
        """Encryption Start/Pause and LE Ping PDUs (Core Vol 6, Part B, 5.1.3,
        5.1.8).
        """
        opcode, data = payload[0], payload[1:]
        central = self.connection.role == Role.CENTRAL
        if (
            opcode == Opcode.LL_ENC_REQ
            and not central
            and len(data) == ENC_REQUEST.size
        ):
            if self.enabled and self.state == State.IDLE:
                # A new start cannot replace active encryption without the pause
                # handshake; treating it as a refresh would change cipher state
                # mid-link.
                self.connection.complete(
                    ErrorCode.CONNECTION_TERMINATED_DUE_TO_MIC_FAILURE_ERROR
                )
                return True
            if self.state not in (State.IDLE, State.RESTART):
                self.connection.enqueue_control(
                    bytes([Opcode.LL_REJECT_EXT_IND, opcode, PROCEDURE_COLLISION])
                )
                return True
            self.refresh = self.enabled or self.state == State.RESTART
            self.enc_rsp_sent = False
            self.rand, self.ediv, self.skd_c, self.iv_c = ENC_REQUEST.unpack(data)
            generator = self.connection.link.random
            self.skd_p = generator.randbytes(LL_ENCRYPTION_SKD_HALF_SIZE)
            self.iv_p = generator.randbytes(LL_ENCRYPTION_IV_HALF_SIZE)
            self.state = State.KEY
            # ENC_RSP starts T_PRT while the Host resolves the LTK. User ACL stays
            # queued in KEY even if the peer continues sending Empty keepalive PDUs.
            self.enqueue(Opcode.LL_ENC_RSP, ENC_RESPONSE.pack(self.skd_p, self.iv_p))
            self.listener.on_long_term_key_request(
                self.connection, self.rand, self.ediv
            )
            return True
        if (
            opcode == Opcode.LL_ENC_RSP
            and central
            and self.state == State.ENC_RSP
            and len(data) == ENC_RESPONSE.size
        ):
            self.skd_p, self.iv_p = ENC_RESPONSE.unpack(data)
            # Installing the session key does not enable encryption yet. The
            # Peripheral's unencrypted START_ENC_REQ is the next handshake step.
            self.install()
            self.state = State.START_REQ
            return True
        if (
            opcode == Opcode.LL_START_ENC_REQ
            and not data
            and central
            and self.state == State.START_REQ
        ):
            self.security.tx_enabled = self.security.rx_enabled = True
            self.state = State.START_RSP
            self.enqueue(Opcode.LL_START_ENC_RSP)
            return True
        if (
            opcode == Opcode.LL_START_ENC_RSP
            and not data
            and self.state == State.START_RSP
        ):
            if not central:
                # Queue the final encrypted response ahead of the resumed ACL data.
                # ARQ retains its ciphertext through retries (Vol 6, Part B, 4.5.9).
                self.security.tx_enabled = True
                self.connection.enqueue_control(
                    bytes([Opcode.LL_START_ENC_RSP]), priority=True
                )
            self.finish()
            return True
        if (
            opcode == Opcode.LL_PAUSE_ENC_REQ
            and not data
            and not central
            and self.enabled
            and not self.busy
        ):
            self.refresh = True
            self.state = State.PAUSE_ACK
            self.enqueue(Opcode.LL_PAUSE_ENC_RSP)
            return True
        if opcode == Opcode.LL_PAUSE_ENC_RSP and not data:
            if central and self.state == State.PAUSE_RSP:
                # The Central's final PAUSE_ENC_RSP is plaintext. The Peripheral
                # changes RX mode after sending its own response to accept that step.
                self.security.tx_enabled = self.security.rx_enabled = False
                self.state = State.PAUSE_ACK
                self.enqueue(Opcode.LL_PAUSE_ENC_RSP)
                return True
            if not central and self.state == State.PAUSE_ACK:
                self.security.tx_enabled = self.security.rx_enabled = False
                self.state = State.RESTART
                return True
        if (
            opcode
            in (Opcode.LL_REJECT_IND, Opcode.LL_REJECT_EXT_IND, Opcode.LL_UNKNOWN_RSP)
            and central
            and self.state in (State.ENC_RSP, State.START_REQ)
        ):
            # Reject/unknown responses complete the Central's pending start.
            # finish also closes a failed refresh so paused ACL cannot escape in
            # plaintext.
            if opcode == Opcode.LL_UNKNOWN_RSP and data == bytes([Opcode.LL_ENC_REQ]):
                self.finish(ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)
                return True
            if (opcode == Opcode.LL_REJECT_IND and len(data) == 1) or (
                opcode == Opcode.LL_REJECT_EXT_IND
                and len(data) == 2
                and data[0] == Opcode.LL_ENC_REQ
            ):
                self.finish(data[-1])
                return True
        if opcode == Opcode.LL_PING_REQ and not data:
            self.enqueue(Opcode.LL_PING_RSP)
            return True
        return opcode == Opcode.LL_PING_RSP and not data

    def sent(self, payload: bytes) -> None:
        if payload[0] == Opcode.LL_ENC_RSP and self.connection.role == Role.PERIPHERAL:
            self.enc_rsp_sent = True
        if (
            payload == bytes([Opcode.LL_START_ENC_REQ])
            and self.state == State.START_RSP
        ):
            self.security.rx_enabled = True
        elif (
            payload == bytes([Opcode.LL_PAUSE_ENC_RSP])
            and self.connection.role == Role.PERIPHERAL
            and self.state == State.PAUSE_ACK
        ):
            self.security.rx_enabled = False

    def acknowledged(self, payload: bytes) -> None:
        if (
            self.state == State.REJECT
            and not self.refresh
            and payload in KEY_MISSING_REJECTIONS
        ):
            # Core Vol 6, Part B, 5.1.3.1: keep Host ACL paused until rejection is
            # acknowledged.
            self.state = State.IDLE
            self.response_timer = None
        elif (
            payload == bytes([Opcode.LL_PAUSE_ENC_RSP])
            and self.connection.role == Role.CENTRAL
            and self.state == State.PAUSE_ACK
        ):
            self.request()

    def decrypt(self, header: int, payload: bytes, *, duplicate: bool) -> bytes | None:
        """Authenticate without committing ACK or procedure state (Core Vol 6, Part E,
        2).
        """
        if not payload:
            return payload
        authenticated_header = header & PDU_DATA_AUTHENTICATED_HEADER_MASK
        # The pause transition may change RX mode before an earlier PDU is retried.
        if duplicate and self.security.last_received is not None:
            previous_header, wire, plaintext, authenticated = (
                self.security.last_received
            )
            if (
                authenticated_header == previous_header
                and payload == wire
                and (
                    authenticated == self.security.rx_enabled
                    or (
                        authenticated and self.state in (State.PAUSE_ACK, State.RESTART)
                    )
                )
            ):
                if authenticated:
                    self.last_authenticated_us = self.connection.radio.now_us
                    self.stop_ping()
                return plaintext
        if self.security.rx_enabled:
            assert self.security.cipher is not None
            try:
                payload = self.security.cipher.decrypt(
                    header, payload, duplicate=duplicate
                )
            except (InvalidTag, OverflowError, ValueError):
                self.connection.complete(
                    ErrorCode.CONNECTION_TERMINATED_DUE_TO_MIC_FAILURE_ERROR
                )
                return None
            self.last_authenticated_us = self.connection.radio.now_us
            self.stop_ping()
        return payload

    @property
    def next_deadline_us(self) -> int:
        """Send LL_PING_REQ early enough for a reply within authenticatedPayloadTO (Core
        Vol 6, Part B, 5.1.8, 5.4).
        """
        if self.busy:
            return self.deadline_us
        if not self.enabled:
            return 0
        timeout_us = self.auth_timeout_us
        if self.ping_pending or self.connection.controls.busy:
            return self.last_authenticated_us + timeout_us
        reserve_us = min(
            timeout_us // 2,
            # One interval to align the request to an event and another for
            # the peer's response when the request closes that event.
            2 * self.connection.listening_interval_us,
        )
        return self.last_authenticated_us + timeout_us - reserve_us

    def check_timeout(self) -> bool:
        """T_PRT for encryption procedures, then the authenticated payload timeout (Core
        Vol 6, Part B, 5.2, 5.4).
        """
        now = self.connection.radio.now_us
        if self.busy and self.deadline_us and now >= self.deadline_us:
            self.connection.complete(ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)
            return True
        if self.enabled and not self.busy and now >= self.next_deadline_us:
            if self.ping_pending or self.connection.controls.busy:
                self.listener.on_authenticated_payload_timeout(self.connection)
                self.last_authenticated_us = now
                self.stop_ping()
            else:
                self.enqueue(Opcode.LL_PING_REQ)
                self.ping_context = ProcedureContext(
                    Opcode.LL_PING_REQ, frozenset((Opcode.LL_PING_RSP,)), self
                )
        return False
