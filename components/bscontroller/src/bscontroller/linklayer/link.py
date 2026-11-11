# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LE radio procedures; no HCI serialization, transport, or simulator binding."""

from __future__ import annotations

import random
from dataclasses import replace

from bscontroller.address import Address
from bscontroller.capabilities import CONTROLLER_SUPPORTED_PHY_MASK
from bscontroller.config import ControllerConfig
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    LL_HOP_INCREMENT_MAX,
    LL_HOP_INCREMENT_MIN,
    LL_IFS_US,
    PDU_CRC_SIZE,
    PDU_DATA_PAYLOAD_SIZE_MIN,
    PDU_DATA_PAYLOAD_TIME_MIN_US,
    PDU_HEADER_SIZE,
)
from bscontroller.linklayer.api import SyncTransferConfig
from bscontroller.linklayer.bitstream import generate_access_address
from bscontroller.linklayer.connection import Connection
from bscontroller.linklayer.decision import DecisionProgram
from bscontroller.linklayer.discovery import (
    Advertising,
    Initiating,
    PeriodicSync,
    Scanning,
)
from bscontroller.linklayer.extended import ExtendedProcedures
from bscontroller.linklayer.listener import Listener
from bscontroller.linklayer.parameters import Parameters
from bscontroller.linklayer.privacy import Privacy
from bscontroller.linklayer.resource import RadioResourceManager
from bscontroller.linklayer.roles import LinkRoles
from bscontroller.linklayer.scheduler import first_radio_gap
from bscontroller.linklayer.timing import (
    LL_CONN_EVENT_GUARD_US,
    LL_RESPONSE_START_TOLERANCE_US,
)
from bscontroller.radio.base import (
    PHY_OCTET_BITS,
    Phy,
    Radio,
    Received,
)


class RadioLink:
    """One physical radio, with explicit, integer-microsecond operation deadlines."""

    def __init__(
        self,
        radio: Radio,
        listener: Listener,
        features: int = 0,
        seed: int = 1,
        config: ControllerConfig | None = None,
    ):
        self.radio = radio
        self.config = config or ControllerConfig()
        self.listener = listener
        self.features = features
        self.default_tx_phys = self.default_rx_phys = CONTROLLER_SUPPORTED_PHY_MASK
        self.seed = seed
        self.random = random.Random(seed)
        self.privacy = Privacy(self)
        self.decision = DecisionProgram(self)
        self.roles = LinkRoles(self)
        self.extended = ExtendedProcedures(self)
        self.resource = RadioResourceManager(
            lambda: self.now_us, self.roles.reservations
        )
        self.accept_list: set[Address] = set()
        self.periodic_advertiser_list: set[tuple[Address, int]] = set()
        # Host default transfer mode for subsequent connections (7.8.92).
        self.sync_transfer = SyncTransferConfig()
        self.default_tx_octets = PDU_DATA_PAYLOAD_SIZE_MIN
        self.default_tx_time = PDU_DATA_PAYLOAD_TIME_MIN_US

    # Internal LL views; managers retain ownership of active contexts.
    @property
    def advertisers(self) -> dict[int, Advertising]:
        return self.roles.advertising.contexts

    @property
    def connections(self) -> dict[int, Connection]:
        return self.roles.connections.contexts

    @property
    def scan(self) -> Scanning | None:
        return self.roles.scanning.active

    @property
    def initiating(self) -> Initiating | None:
        return self.roles.scanning.initiating

    @property
    def sync(self) -> PeriodicSync | None:
        return self.roles.synchronization.pending

    @property
    def syncs(self) -> dict[int, PeriodicSync]:
        return self.roles.synchronization.active

    def trace(self, message: str) -> None:
        self.listener.trace(message)

    def reset(self) -> None:
        self.roles.reset()
        self.resource.deadline_us = None
        self.random.seed(self.seed)
        self.privacy.reset()
        self.decision.tests = ()
        self.accept_list.clear()
        self.periodic_advertiser_list.clear()
        self.sync_transfer = SyncTransferConfig()
        self.default_tx_octets = PDU_DATA_PAYLOAD_SIZE_MIN
        self.default_tx_time = PDU_DATA_PAYLOAD_TIME_MIN_US

    @property
    def now_us(self) -> int:
        return self.radio.now_us

    def new_access_address(self) -> int:
        used = {
            connection.parameters.access_address
            for connection in self.connections.values()
        }
        used.update(
            item.periodic_aa
            for item in self.advertisers.values()
            if item.periodic_enabled
        )
        return generate_access_address(self.random, used)

    def connection_parameters(
        self,
        parameters: Parameters,
        request_end_us: int,
        window_delay_us: int,
        phy: Phy = Phy.LE_1M,
    ) -> Parameters | None:
        """Allocate independent wire identity and a legal initial transmit window."""
        start = request_end_us + window_delay_us
        # Reserve the larger of the initial window and a complete exchange, plus
        # a guard. Keep the chosen offset on the 1.25-ms grid (Vol 6, Part B, 4.5.3).
        gap = first_radio_gap(
            self.resource.reservations(),
            start,
            max(
                parameters.window_size * LL_CONN_INTERVAL_UNIT_US,
                2 * phy.airtime_us(PDU_HEADER_SIZE + PDU_DATA_PAYLOAD_SIZE_MIN)
                + 2 * LL_IFS_US,
            )
            + LL_CONN_EVENT_GUARD_US,
            start
            + (parameters.interval - parameters.window_size) * LL_CONN_INTERVAL_UNIT_US,
            quantum_us=LL_CONN_INTERVAL_UNIT_US,
        )
        if gap is None:
            return None
        return replace(
            parameters,
            window_offset=(gap - start) // LL_CONN_INTERVAL_UNIT_US,
            access_address=self.new_access_address(),
            crc_init=self.random.getrandbits(PDU_CRC_SIZE * PHY_OCTET_BITS),
            hop=self.random.randint(LL_HOP_INCREMENT_MIN, LL_HOP_INCREMENT_MAX),
        )

    async def _transmit(self, channel: int, pdu: bytes, **kwargs) -> int:
        self.trace(f'RADIO TX ch={channel} pdu={pdu.hex()}')
        return await self.radio.transmit(channel, pdu, **kwargs)

    async def receive_response(
        self,
        channel: int,
        transmit_end_us: int,
        phy: Phy = Phy.LE_1M,
        *,
        deadline_us: int | None = None,
    ) -> Received | None:
        """Limit address detection, not the length of an acquired response."""
        response_start = transmit_end_us + LL_IFS_US
        until_us = response_start + LL_RESPONSE_START_TOLERANCE_US + phy.address_us
        if deadline_us is not None:
            until_us = min(until_us, deadline_us)
        return await self.radio.receive(
            channel,
            until_us,
            start_us=response_start - LL_RESPONSE_START_TOLERANCE_US,
            phy=phy,
        )

    def _pending_operations(self, target_us: int):
        operations = self.roles.connections.prepare(target_us)
        operations.extend(self.roles.advertising.prepare())
        operations.extend(self.roles.synchronization.prepare())
        return operations, self.roles.synchronization.contexts()

    async def advance_to_us(self, target_us: int) -> int:
        """Dispatch role-prepared events on one radio and one simulation timeline."""
        while self.now_us < target_us:
            now = self.now_us
            self.roles.synchronization.expire()
            operations, contexts = self._pending_operations(target_us)
            if operations:
                operation = self.resource.select(operations)
                if operation.due_us <= now + 1:
                    await self.resource.execute(operation)
                    continue
                # Leave the due tick available for the next event's preparation.
                end = min(target_us, operation.due_us - 1)
            else:
                end = target_us
            for context in contexts:
                if context.info is not None:
                    end = min(end, context.deadline_us)
            scan = self.roles.scanning.next_context()
            if scan:
                await self.roles.scanning.run(scan, end)
            else:
                await self.radio.advance_to_us(end)
        return self.now_us
