# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LL data-length negotiation and per-PHY transmission limits, Core 4.5.10/5.1.9."""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    PDU_CONTROL_OPCODE_SIZE,
    PDU_CONTROL_REJECT_EXT_SIZE,
    PDU_CONTROL_UNKNOWN_RSP_SIZE,
    PDU_DATA_EMPTY_TIME_CODED_S8_US,
    PDU_DATA_MIC_SIZE,
    PDU_DATA_OCTET_TIME_CODED_S8_US,
    PDU_DATA_PAYLOAD_SIZE_MAX,
    PDU_DATA_PAYLOAD_SIZE_MIN,
    PDU_DATA_PAYLOAD_TIME_CODED_MAX_US,
    PDU_DATA_PAYLOAD_TIME_CODED_MIN_US,
    PDU_DATA_PAYLOAD_TIME_MIN_US,
    PDU_DATA_PAYLOAD_TIME_UNCODED_MAX_US,
    PDU_HEADER_SIZE,
    ControlOpcode,
)
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.procedure import LocalProcedure, ProcedureContext
from bscontroller.radio.base import PHY_CODED_S8_FACTOR, Phy

if TYPE_CHECKING:
    from bscontroller.linklayer.connection import Connection
    from bscontroller.linklayer.frame_space import FrameSpaceProcedure
    from bscontroller.linklayer.listener import ControlListener

Opcode = ControlOpcode
LENGTH_PARAMETERS = struct.Struct('<HHHH')


def initial_time_us(phy: Phy) -> int:
    """Core Vol 6, Part B, 4.5.10: initial connMax*Time is 328 us uncoded or 2704 us
    Coded.
    """
    return (
        PDU_DATA_PAYLOAD_TIME_CODED_MIN_US
        if phy == Phy.LE_CODED
        else PDU_DATA_PAYLOAD_TIME_MIN_US
    )


def valid_data_length(octets: int, time_us: int) -> bool:
    return (
        PDU_DATA_PAYLOAD_SIZE_MIN <= octets <= PDU_DATA_PAYLOAD_SIZE_MAX
        and PDU_DATA_PAYLOAD_TIME_MIN_US
        <= time_us
        <= PDU_DATA_PAYLOAD_TIME_CODED_MAX_US
    )


@dataclass(frozen=True)
class DataLengths:
    rx_octets: int
    rx_time: int
    tx_octets: int
    tx_time: int

    def encode(self) -> bytes:
        return LENGTH_PARAMETERS.pack(
            self.rx_octets, self.rx_time, self.tx_octets, self.tx_time
        )


class DataLengthProcedure(LocalProcedure):
    def __init__(self, connection: Connection, frame_space: FrameSpaceProcedure):
        self.connection = connection
        self.frame_space = frame_space
        initial_time = initial_time_us(connection.tx_phy)
        self.local = DataLengths(
            PDU_DATA_PAYLOAD_SIZE_MIN,
            initial_time,
            *self._local_tx(
                connection.link.default_tx_octets,
                max(initial_time, connection.link.default_tx_time),
            ),
        )
        self.remote = DataLengths(
            PDU_DATA_PAYLOAD_SIZE_MIN,
            initial_time,
            PDU_DATA_PAYLOAD_SIZE_MIN,
            initial_time,
        )
        self.context: ProcedureContext | None = None
        self.reported = self.effective()

    def _local_tx(self, octets: int, time_us: int) -> tuple[int, int]:
        """Host-suggested TX limits capped by connMaxTx supported (4.5.10)."""
        config = self.connection.link.config
        return min(octets, config.max_tx_octets), min(time_us, config.max_tx_time)

    @property
    def listener(self) -> ControlListener:
        return self.connection.link.listener

    def _receive_limits(self, phy: Phy) -> tuple[int, int]:
        octets = min(self.local.rx_octets, self.remote.tx_octets)
        time = min(self.local.rx_time, self.remote.tx_time)
        if phy == Phy.LE_CODED:
            time = max(PDU_DATA_PAYLOAD_TIME_CODED_MIN_US, time)
        return octets, time

    def interval_required_us(self, rx_phy: Phy, spacing_us: int) -> int:
        octets, time = self._receive_limits(rx_phy)
        return spacing_us + min(
            time,
            octets * PDU_DATA_OCTET_TIME_CODED_S8_US + PDU_DATA_EMPTY_TIME_CODED_S8_US,
        )

    def minimum_interval_us(self, tx_phy: Phy, rx_phy: Phy, spacing_us: int) -> int:
        """connIntervalUncodedMin or connIntervalCodedMin (Core 6, Part B, 4.5.10)."""
        limiting_phy = Phy.LE_CODED if Phy.LE_CODED in (tx_phy, rx_phy) else tx_phy
        return self.interval_required_us(rx_phy, spacing_us) + initial_time_us(
            limiting_phy
        )

    def effective(self, *, tx_phy=None, rx_phy=None, interval_us=None) -> DataLengths:
        """connEffectiveMax* values, with the Coded PHY minimum of 2704 us (Core Vol 6,
        Part B, 4.5.10).
        """
        connection = self.connection
        tx_phy = connection.tx_phy if tx_phy is None else tx_phy
        rx_phy = connection.rx_phy if rx_phy is None else rx_phy
        interval_us = (
            connection.parameters.interval_us if interval_us is None else interval_us
        )
        rx_octets, rx_time = self._receive_limits(rx_phy)
        # Match each sender maximum against the opposite endpoint's receiver
        # maximum; the resulting TX and RX limits can differ.
        tx_octets = min(self.local.tx_octets, self.remote.rx_octets)
        tx_time = min(self.local.tx_time, self.remote.rx_time)
        if tx_phy == Phy.LE_CODED:
            spacing = self.frame_space.interval_overhead_us(tx_phy, rx_phy)
            required = self.interval_required_us(rx_phy, spacing)
            tx_time = max(
                PDU_DATA_PAYLOAD_TIME_CODED_MIN_US, min(tx_time, interval_us - required)
            )
        return DataLengths(rx_octets, rx_time, tx_octets, tx_time)

    def refresh(self) -> None:
        effective = self.effective()
        if effective != self.reported:
            self.reported = effective
            self.listener.on_data_length_changed(self.connection)

    def packet_limit(
        self,
        phy: Phy,
        time_us: int,
        octets: int,
        coding=PHY_CODED_S8_FACTOR,
        *,
        encrypted=False,
    ) -> int:
        """Largest payload that fits the effective packet time on a PHY (Core Vol 6,
        Part B, 4.5.10).
        """
        return next(
            size
            for size in range(octets, -1, -1)
            if phy.airtime_us(
                PDU_HEADER_SIZE
                + size
                + (PDU_DATA_MIC_SIZE if encrypted and size else 0),
                coding,
            )
            <= time_us
        )

    def tx_payload_limit(self) -> int:
        """Payload limit during pending PHY or interval changes (Core Vol 6, Part B,
        5.1.7.4, 5.1.10.1).
        """
        connection = self.connection
        constraints = connection.controls.transmission_constraints()
        limits = []
        for phy in constraints.tx_phys:
            effective = self.effective(
                tx_phy=phy,
                rx_phy=constraints.rx_phy,
                interval_us=constraints.interval_us,
            )
            limits.append(
                self.packet_limit(
                    phy,
                    effective.tx_time,
                    effective.tx_octets,
                    connection.tx_coding,
                    encrypted=connection.packet_security.tx_enabled,
                )
            )
        # Choose the tightest limit across the current and candidate PHYs, since
        # a pending PDU may need retransmission after the transition.
        return min(limits)

    def enqueue(self, opcode: int) -> None:
        """LL_LENGTH_REQ/RSP with local uncoded limits (Core 6.3, Vol 6, Part B, 4.5.10
        Table 4.8; 5.1.9).
        """
        parameters = self.local
        features = self.connection.peer_features
        if features is not None and not features & LeFeatureMask.LE_CODED_PHY:
            # Core 6.3, Vol 6, Part B, 5.1.9 allows up to 2128 us without Coded;
            # Table 4.8 in 4.5.10 sets 2120 us for this implementation's no-CTE case.
            parameters = replace(
                parameters,
                rx_time=min(parameters.rx_time, PDU_DATA_PAYLOAD_TIME_UNCODED_MAX_US),
                tx_time=min(parameters.tx_time, PDU_DATA_PAYLOAD_TIME_UNCODED_MAX_US),
            )
        payload = bytes([opcode]) + parameters.encode()
        llcp = self.connection.llcp
        if opcode == Opcode.LL_LENGTH_REQ:
            llcp.send(self.context, payload, priority=True)
        else:
            llcp.respond(payload, priority=True)

    def request(self, octets: int, time_us: int) -> None:
        """Start the Data Length Update procedure (Core Vol 6, Part B, 5.1.9)."""
        config = self.connection.link.config
        self.local = DataLengths(
            config.max_rx_octets,
            config.max_rx_time,
            *self._local_tx(octets, time_us),
        )
        self.context = ProcedureContext(
            Opcode.LL_LENGTH_REQ,
            frozenset((Opcode.LL_LENGTH_RSP,)),
            self.control,
        )
        self.enqueue(Opcode.LL_LENGTH_REQ)

    def start_if_needed(self) -> None:
        # Core Vol 6, Part B, 4.5.10: start the update when local values differ from
        # initial ones.
        initial_time = initial_time_us(self.connection.tx_phy)
        if (
            self.local.tx_octets != PDU_DATA_PAYLOAD_SIZE_MIN
            or self.local.tx_time != initial_time
        ):
            self.connection.llcp.set_data_length(
                self.local.tx_octets, self.local.tx_time
            )

    def control(self, payload: bytes) -> bool:
        """Handle LL_LENGTH_REQ/RSP and rejection of LL_LENGTH_REQ (Core Vol 6, Part B,
        5.1.9).
        """
        opcode = payload[0]
        if opcode in (Opcode.LL_LENGTH_REQ, Opcode.LL_LENGTH_RSP):
            if len(payload) != PDU_CONTROL_OPCODE_SIZE + LENGTH_PARAMETERS.size:
                # Use the common LL_UNKNOWN_RSP path for an invalid PDU length.
                return False
            rx, rx_time, tx, tx_time = LENGTH_PARAMETERS.unpack(
                payload[PDU_CONTROL_OPCODE_SIZE:]
            )
            if not valid_data_length(rx, rx_time) or not valid_data_length(tx, tx_time):
                self.connection.controls.reject(
                    opcode, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR
                )
                return True
            if opcode == Opcode.LL_LENGTH_RSP and not self.pending:
                # An unsolicited response must not overwrite negotiated limits.
                return True
            self.remote = DataLengths(rx, rx_time, tx, tx_time)
            if opcode == Opcode.LL_LENGTH_REQ:
                config = self.connection.link.config
                self.local = replace(
                    self.local,
                    rx_octets=config.max_rx_octets,
                    rx_time=config.max_rx_time,
                )
                self.enqueue(Opcode.LL_LENGTH_RSP)
            else:
                self.context.finish()
            self.refresh()
            return True
        if (
            opcode == Opcode.LL_UNKNOWN_RSP
            and len(payload) == PDU_CONTROL_UNKNOWN_RSP_SIZE
            and payload[1] == Opcode.LL_LENGTH_REQ
        ) or (
            opcode == Opcode.LL_REJECT_EXT_IND
            and len(payload) == PDU_CONTROL_REJECT_EXT_SIZE
            and payload[1] == Opcode.LL_LENGTH_REQ
        ):
            if self.pending:
                self.context.finish()
                # Fall back to the initial limits after rejection, so subsequent packets
                # do not assume that the peer accepted the larger data length.
                initial_time = initial_time_us(self.connection.tx_phy)
                self.local = self.remote = DataLengths(
                    PDU_DATA_PAYLOAD_SIZE_MIN,
                    initial_time,
                    PDU_DATA_PAYLOAD_SIZE_MIN,
                    initial_time,
                )
                self.refresh()
            return True
        return False
