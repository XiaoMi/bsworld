# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LE radio procedures; no HCI serialization, transport, or simulator binding."""

from __future__ import annotations

import random
from collections.abc import Callable, Iterable
from dataclasses import replace

from bscontroller.address import Address
from bscontroller.capabilities import CONTROLLER_SUPPORTED_PHY_MASK
from bscontroller.config import ControllerConfig
from bscontroller.linklayer import (
    LL_CONN_INTERVAL_UNIT_US,
    LL_HOP_INCREMENT_MAX,
    LL_HOP_INCREMENT_MIN,
    LL_IFS_US,
    LL_SCA_50_PPM,
    PDU_CRC_SIZE,
    PDU_DATA_PAYLOAD_SIZE_MIN,
    PDU_DATA_PAYLOAD_TIME_MIN_US,
    PDU_HEADER_SIZE,
)
from bscontroller.linklayer.advertising_pdu import ExtendedPdu
from bscontroller.linklayer.api import SyncTransferConfig
from bscontroller.linklayer.bitstream import generate_access_address
from bscontroller.linklayer.connection import Connection
from bscontroller.linklayer.connection_rate import ConnectionRateParameters
from bscontroller.linklayer.decision import DecisionProgram
from bscontroller.linklayer.discovery import (
    Advertising,
    Initiating,
    PeriodicSync,
    Scanning,
)
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.listener import Listener
from bscontroller.linklayer.monitoring import AdvertiserMonitoring
from bscontroller.linklayer.parameters import Parameters
from bscontroller.linklayer.power_control import compensated_power
from bscontroller.linklayer.privacy import Privacy
from bscontroller.linklayer.resource import RadioResourceManager
from bscontroller.linklayer.roles import LinkRoles
from bscontroller.linklayer.scheduler import first_radio_gap
from bscontroller.linklayer.subrating import SubrateRequest
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
        self.monitoring = AdvertiserMonitoring(
            radio,
            lambda address, condition: listener.on_monitored_advertiser(
                address, condition
            ),
        )
        self.roles = LinkRoles(self)
        self.resource = RadioResourceManager(
            lambda: self.now_us, self.roles.reservations
        )
        self.accept_list: set[Address] = set()
        self.periodic_advertiser_list: set[tuple[Address, int]] = set()
        # Host default transfer mode for subsequent connections (7.8.92).
        self.sync_transfer = SyncTransferConfig()
        self.default_subrate = SubrateRequest()
        self.default_rate = ConnectionRateParameters()
        self.default_tx_octets = PDU_DATA_PAYLOAD_SIZE_MIN
        self.default_tx_time = PDU_DATA_PAYLOAD_TIME_MIN_US
        self.rf_tx_path_compensation = 0
        self.rf_rx_path_compensation = 0
        self.sleep_clock_accuracy = LL_SCA_50_PPM
        # The device addresses an AUX_CONNECT_REQ on a synchronized PAwR train
        # may name (Core Vol 6, Part B, 4.4.2.12.2); set by the service.
        self.public_address: Address | None = None
        self.random_address: Address | None = None

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

    def trace(self, message: str | Callable[[], str]) -> None:
        self.listener.trace(message)

    def reset(self) -> None:
        self.roles.reset()
        self.resource.deadline_us = None
        self.random.seed(self.seed)
        self.privacy.reset()
        self.decision.tests = ()
        self.monitoring.reset()
        self.accept_list.clear()
        self.periodic_advertiser_list.clear()
        self.sync_transfer = SyncTransferConfig()
        self.default_subrate = SubrateRequest()
        self.default_rate = ConnectionRateParameters()
        self.features &= ~int(
            LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM
            | LeFeatureMask.CONNECTION_SUBRATING_HOST_SUPPORT
            | LeFeatureMask.ADVERTISING_CODING_SELECTION_HOST_SUPPORT
            | LeFeatureMask.SHORTER_CONNECTION_INTERVALS_HOST_SUPPORT
        )
        self.default_tx_octets = PDU_DATA_PAYLOAD_SIZE_MIN
        self.default_tx_time = PDU_DATA_PAYLOAD_TIME_MIN_US
        self.rf_tx_path_compensation = 0
        self.rf_rx_path_compensation = 0
        self.sleep_clock_accuracy = LL_SCA_50_PPM

    @property
    def now_us(self) -> int:
        return self.radio.now_us

    @property
    def can_change_sleep_clock_accuracy(self) -> bool:
        # Other ACLs may still rely on the advertised clock bound.
        return len(self.connections) <= 1 and self.initiating is None

    def new_access_address(
        self, *, exclude: Iterable[int] = (), minimum_distance: int = 1
    ) -> int:
        used = self.used_access_addresses()
        used.update(exclude)
        return generate_access_address(
            self.random, used, minimum_distance=minimum_distance
        )

    def used_access_addresses(self) -> set[int]:
        used = {
            connection.parameters.access_address
            for connection in self.connections.values()
        }
        for item in self.advertisers.values():
            if item.periodic_enabled:
                used.add(item.periodic_aa)
                if item.pawr_state is not None:
                    used.add(item.pawr_state.rsp_aa)
        for group in self.roles.broadcast.groups.values():
            if group.info is not None:
                used.update(
                    group.info.access_address(n) for n in range(group.info.num_bis + 1)
                )
        used.update(self.roles.cig.access_addresses())
        return used

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
            sca=self.sleep_clock_accuracy,
        )

    async def transmit(self, channel: int, pdu: bytes, **kwargs) -> int:
        """Transmit a Link Layer PDU with the configured RF path compensation."""
        self.trace(lambda: f'RADIO TX ch={channel} pdu={pdu.hex()}')
        if self.rf_tx_path_compensation:
            kwargs['power_dbm'] = self.radiative_transmit_power(
                kwargs.get('power_dbm', 0)
            )
        return await self.radio.transmit(channel, pdu, **kwargs)

    def radiative_transmit_power(self, power: int) -> int:
        return compensated_power(power, self.rf_tx_path_compensation)

    def with_radiative_power(self, pdu: ExtendedPdu) -> ExtendedPdu:
        if pdu.tx_power is None or not self.rf_tx_path_compensation:
            return pdu
        return replace(pdu, tx_power=self.radiative_transmit_power(pdu.tx_power))

    def reported_rssi(self, rssi: float) -> int:
        # Vol 4, Part E, 7.8.76: antenna level = transceiver input - RF path gain.
        return compensated_power(rssi, -self.rf_rx_path_compensation)

    def interframe_slot(
        self,
        channel: int,
        received_end_us: int,
        pdu_size: int,
        phy: Phy = Phy.LE_1M,
        *,
        response_size: int = 0,
    ) -> tuple[int, int] | None:
        """Return the T_IFS transmit slot if the exchange fits the radio grant."""
        start_us = received_end_us + LL_IFS_US
        end_us = start_us + phy.airtime_us(pdu_size)
        exchange_end = end_us
        if response_size:
            exchange_end += (
                LL_IFS_US
                + LL_RESPONSE_START_TOLERANCE_US
                + phy.airtime_us(response_size)
            )
        if not self.resource.fits_radio(exchange_end) or start_us < (
            self.radio.earliest_start_us(channel, phy, transmit=True)
        ):
            return None
        return start_us, end_us

    async def receive_response(
        self,
        channel: int,
        transmit_end_us: int,
        phy: Phy = Phy.LE_1M,
        *,
        search_deadline_us: int | None = None,
        deadline_us: int | None = None,
        **parameters,
    ) -> Received | None:
        """Receive within the response window and an optional full-packet deadline."""
        response_start = transmit_end_us + LL_IFS_US
        until_us = response_start + LL_RESPONSE_START_TOLERANCE_US + phy.address_us
        if search_deadline_us is not None:
            until_us = min(until_us, search_deadline_us)
        return await self.radio.receive(
            channel,
            until_us,
            start_us=response_start - LL_RESPONSE_START_TOLERANCE_US,
            deadline_us=deadline_us,
            phy=phy,
            **parameters,
        )

    def _pending_operations(self, target_us: int):
        operations = self.roles.connections.prepare(target_us)
        operations.extend(self.roles.advertising.prepare())
        operations.extend(self.roles.synchronization.prepare())
        operations.extend(self.roles.broadcast.prepare())
        operations.extend(self.roles.cig.prepare())
        return operations, self.roles.synchronization.contexts()

    async def advance_idle_to_us(self, target_us: int) -> None:
        """Pause an idle wait at the first software ACL flush deadline."""
        connections = self.roles.connections
        deadline = connections.next_acl_flush_us
        if deadline is None or target_us < deadline:
            await self.radio.advance_to_us(target_us)
        else:
            await self.radio.advance_to_us(max(self.now_us, deadline))
            connections.expire_acl()

    async def advance_to_us(self, target_us: int) -> int:
        """Dispatch role-prepared events on one radio and one simulation timeline."""
        iso = self.roles.iso
        connections = self.roles.connections
        while self.now_us < target_us:
            iso.flush(self.now_us)
            self.monitoring.expire()
            self.roles.synchronization.expire()
            connections.expire_acl()
            operations, contexts = self._pending_operations(target_us)
            if operations:
                operation = self.resource.select(operations)
                if operation.due_us <= self.now_us + 1:
                    await self.resource.execute(operation)
                    continue
                # Leave the due tick available for the next event's preparation.
                end = min(target_us, operation.due_us - 1)
            else:
                end = target_us
            if self.monitoring.deadline_us is not None:
                end = min(end, self.monitoring.deadline_us)
            if (delivery := iso.next_delivery_us) is not None:
                end = min(end, delivery)
            for context in contexts:
                if context.info is not None:
                    end = min(end, context.deadline_us)
            scan = self.roles.scanning.next_context()
            if scan:
                # Address acquisition can span a software ACL flush deadline.
                await self.roles.scanning.run(scan, end)
            else:
                await self.advance_idle_to_us(end)
        self.monitoring.expire()
        self.roles.broadcast.expire()
        iso.flush(self.now_us)
        connections.expire_acl()
        return self.now_us
