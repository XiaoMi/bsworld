# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""ACL and CIS power control (Core Vol 6, Part B, 4.5.15–16 and 5.1.17–18)."""

from __future__ import annotations

import struct
from bisect import bisect_left
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    ControlOpcode,
)
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.procedure import (
    DeferredProcedure,
    LocalProcedure,
    ProcedureContext,
)
from bscontroller.linklayer.timing import counter_distance
from bscontroller.radio.base import (
    PHY_CODED_S2_FACTOR,
    PHY_TIME_UNITS_PER_SECOND,
    RADIO_TX_POWER_MAX_DBM,
    RADIO_TX_POWER_MIN_DBM,
    Phy,
    Received,
)

if TYPE_CHECKING:
    from bscontroller.linklayer.control import ControlProcedures

Opcode = ControlOpcode
NOT_MANAGED = 126
POWER_UNAVAILABLE = 127
DELTA_UNAVAILABLE = 127
APR_UNAVAILABLE = 0xFF
MAX_COMPENSATION = 1280  # 0.1 dB, Vol 4, Part E, 7.8.76.
DISABLE_HIGH_ZONE = 0xFF
MAX_PATH_LOSS = 0xFE
PATH_LOSS_UNAVAILABLE = 0xFF
MINIMUM_POWER = 1
MAXIMUM_POWER = 2
MAX_POWER_REQUEST = 127
REQUEST = struct.Struct('<BBbb')
RESPONSE = struct.Struct('<BBbbB')
CHANGE = struct.Struct('<BBBbb')

# Nominal ACL transmitter steps and the simulated receiver's working RSSI range.
TX_POWER_LEVELS_DBM = (-24, -16, -8, 0, 4)
DEFAULT_TX_POWER_DBM = 0
RSSI_LOWER_DBM = -70
RSSI_UPPER_DBM = -39
RSSI_TARGET_DBM = -54
POWER_REQUEST_INTERVAL_US = PHY_TIME_UNITS_PER_SECOND


class PowerPhy(IntEnum):
    """Power PHY indices in HCI. The corresponding LL value is a bit mask."""

    LE_1M = 1
    LE_2M = 2
    LE_CODED_S8 = 3
    LE_CODED_S2 = 4

    @classmethod
    def from_radio(cls, phy: Phy, coding: int) -> PowerPhy:
        return (
            cls.LE_CODED_S2
            if phy == Phy.LE_CODED and coding == PHY_CODED_S2_FACTOR
            else cls(phy)
        )

    @property
    def mask(self) -> int:
        return 1 << (self - 1)

    @property
    def managed(self) -> tuple[PowerPhy, ...]:
        # Core Vol 6, Part B, 4.5.15: the two Coded levels have shared activity.
        if self >= PowerPhy.LE_CODED_S8:
            other = (
                PowerPhy.LE_CODED_S2
                if self == PowerPhy.LE_CODED_S8
                else PowerPhy.LE_CODED_S8
            )
            return self, other
        return (self,)


class PowerReport(IntEnum):
    LOCAL_CHANGED = 0
    REMOTE_CHANGED = 1
    REMOTE_READ = 2


class PathLossZone(IntEnum):
    LOW = 0
    MIDDLE = 1
    HIGH = 2


def compensated_power(power: float, compensation: int) -> int:
    return max(
        RADIO_TX_POWER_MIN_DBM,
        min(RADIO_TX_POWER_MAX_DBM, round(power + compensation / 10)),
    )


@dataclass(frozen=True)
class PathLossParameters:
    high: int
    high_hysteresis: int
    low: int
    low_hysteresis: int
    minimum_events: int

    @property
    def valid(self) -> bool:
        return (
            self.high + self.high_hysteresis <= DISABLE_HIGH_ZONE
            and self.low >= self.low_hysteresis
            and self.low + self.low_hysteresis <= self.high - self.high_hysteresis
        )

    def classify(
        self, loss: int, previous: PathLossZone | None, previous_loss: int | None
    ) -> PathLossZone:
        low = self.low
        high = self.high
        if previous is not None:
            low += (
                self.low_hysteresis
                if previous == PathLossZone.LOW
                else -self.low_hysteresis
            )
            high += (
                -self.high_hysteresis
                if previous == PathLossZone.HIGH
                else self.high_hysteresis
            )
        high_inclusive = previous != PathLossZone.HIGH
        low_inclusive = previous != PathLossZone.LOW
        if previous_loss is not None:
            # Coincident boundaries use the direction of the measured crossing.
            if self.high_hysteresis == 0:
                high_inclusive = loss > previous_loss or (
                    loss == previous_loss and previous == PathLossZone.HIGH
                )
            if self.low_hysteresis == 0:
                low_inclusive = loss < previous_loss or (
                    loss == previous_loss and previous == PathLossZone.LOW
                )
        if self.high != DISABLE_HIGH_ZONE and (
            loss > high or (loss == high and high_inclusive)
        ):
            return PathLossZone.HIGH
        if loss < low or (loss == low and low_inclusive):
            return PathLossZone.LOW
        return PathLossZone.MIDDLE


class PathLossMonitoring:
    def __init__(self):
        self.parameters: PathLossParameters | None = None
        self.enabled = False
        self.zone: PathLossZone | None = None
        self.last_sample: tuple[int, PathLossZone] | None = None
        self.candidate: tuple[PathLossZone, int, int] | None = None
        self.reconfigured = False

    def configure(self, parameters: PathLossParameters) -> None:
        self.parameters = parameters
        self.candidate = None
        self.reconfigured = self.enabled

    def set_enabled(self, enabled: bool) -> None:
        if enabled != self.enabled:
            self.enabled = enabled
            self._reset_measurement()

    def _reset_measurement(self) -> None:
        self.zone = None
        self.last_sample = None
        self.candidate = None
        self.reconfigured = False

    def unavailable(self) -> bool:
        """Lose a reported estimate until a new reliable measurement is available."""
        if not self.enabled or self.zone is None:
            return False
        # Core 4.5.16 requires a fresh zone report, even for the same zone.
        self._reset_measurement()
        return True

    def observe(self, loss: int, counter: int) -> PathLossZone | None:
        parameters = self.parameters
        if not self.enabled or parameters is None:
            return None
        previous_loss, measured_zone = self.last_sample or (None, PathLossZone.MIDDLE)
        zone = (
            measured_zone
            if loss == previous_loss and not self.reconfigured
            else parameters.classify(loss, self.zone, previous_loss)
        )
        self.last_sample = loss, zone
        if zone == self.zone:
            self.candidate = None
            self.reconfigured = False
            return None
        candidate, last_counter, elapsed = self.candidate or (None, counter, 0)
        # Accumulate each interval so sparse samples can span the full counter cycle.
        elapsed = (
            elapsed + counter_distance(counter, last_counter)
            if candidate == zone
            else 0
        )
        if (
            self.zone is not None
            and not self.reconfigured
            and elapsed < parameters.minimum_events
        ):
            self.candidate = zone, counter, elapsed
            return None
        self.zone = zone
        self.candidate = None
        self.reconfigured = False
        return zone


class PowerControl(LocalProcedure):
    def __init__(self, owner: ControlProcedures):
        self.owner = owner
        self.active_phy = self.tx_phy
        self.managed_phys = frozenset(self.active_phy.managed)
        self.local_levels = dict.fromkeys(PowerPhy, DEFAULT_TX_POWER_DBM)
        self.remote_levels: dict[PowerPhy, int] = {}
        self.remote_flags: dict[PowerPhy, int] = {}
        self.rssi: dict[PowerPhy, float] = {}
        self.last_rx_phy = self.active_phy
        self.path_loss = PathLossMonitoring()
        self.report_local = False
        self.report_remote = False
        self.tx_compensation = self.connection.link.rf_tx_path_compensation
        self.peer_requested = False
        self.requested = False
        self.next_request_us = 0
        self.context: ProcedureContext | None = None
        self.request_phy = self.active_phy

    @property
    def connection(self):
        return self.owner.connection

    @property
    def tx_phy(self) -> PowerPhy:
        return PowerPhy.from_radio(self.connection.tx_phy, self.connection.tx_coding)

    @property
    def rx_phy(self) -> PowerPhy:
        if self.connection.rx_phy == Phy.LE_CODED:
            if self.last_rx_phy >= PowerPhy.LE_CODED_S8:
                return self.last_rx_phy
            return PowerPhy.LE_CODED_S8
        return PowerPhy(self.connection.rx_phy)

    def transmit_power_dbm(self, control_payload: bytes | None) -> int:
        """Transmit a current-PHY indication at the level it reports (Core 5.1.18)."""
        if (
            control_payload
            and control_payload[0] == Opcode.LL_POWER_CHANGE_IND
            and len(control_payload) == CHANGE.size
        ):
            _, phys, _, _, level = CHANGE.unpack(control_payload)
            if (
                phys & self.active_phy.mask
                and RADIO_TX_POWER_MIN_DBM <= level <= RADIO_TX_POWER_MAX_DBM
            ):
                return level
        return self._local_power(self.active_phy)

    def transmit_iso_power_dbm(self, phy: PowerPhy) -> int:
        return self._local_power(phy)

    def _radiative(self, nominal: int) -> int:
        return compensated_power(nominal, self.tx_compensation)

    def _local_power(self, phy: PowerPhy) -> int:
        return self._radiative(self.local_levels[phy])

    def read_local(self, phy: PowerPhy) -> tuple[int, int]:
        level = (
            self._local_power(phy) if phy in self.managed_phys else POWER_UNAVAILABLE
        )
        return level, self._radiative(TX_POWER_LEVELS_DBM[-1])

    def queue_read(self, phy: PowerPhy) -> None:
        def fail(status: int) -> None:
            self._report(PowerReport.REMOTE_READ, phy, POWER_UNAVAILABLE, 0, 0, status)

        def start() -> None:
            if self.peer_supports_request:
                self._request(phy, 0, host=True)
            else:
                fail(ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)

        self.connection.llcp.initiate(
            DeferredProcedure('power_read', start, fail), defer=True
        )

    def _report(
        self,
        reason: PowerReport,
        phy: PowerPhy,
        level: int,
        flags: int,
        delta: int,
        status: int = ErrorCode.SUCCESS,
    ) -> None:
        self.owner.listener.on_transmit_power_reporting(
            self.connection, status, reason, phy, level, flags, delta
        )

    def _flags(self, level: int) -> int:
        return MINIMUM_POWER * (
            level == self._radiative(TX_POWER_LEVELS_DBM[0])
        ) | MAXIMUM_POWER * (level == self._radiative(TX_POWER_LEVELS_DBM[-1]))

    def _local_changed(self, phy: PowerPhy, level: int, delta: int) -> None:
        if self.report_local and delta:
            self._report(
                PowerReport.LOCAL_CHANGED, phy, level, self._flags(level), delta
            )

    def _indicate(self, phy: PowerPhy, level: int, delta: int) -> None:
        if self.peer_requested:
            self.connection.llcp.schedule_indication(
                CHANGE.pack(
                    Opcode.LL_POWER_CHANGE_IND,
                    phy.mask,
                    self._flags(level),
                    delta,
                    level,
                )
            )

    def update_transmitter(self) -> None:
        """Apply the committed PHY and pending RF compensation at the next event."""
        phy, compensation = self.tx_phy, self.connection.link.rf_tx_path_compensation
        managed = frozenset(phy.managed).union(
            *(event.tx_power_phy.managed for event in self._iso_events())
        )
        if (
            phy == self.active_phy
            and compensation == self.tx_compensation
            and managed == self.managed_phys
        ):
            return
        previous = {
            member: self._local_power(member) for member in sorted(self.managed_phys)
        }
        self.active_phy, self.tx_compensation = phy, compensation
        self.managed_phys = managed
        for member in sorted(managed):
            level = self._local_power(member)
            if member not in previous:
                self._indicate(member, level, 0)
            elif level != previous[member]:
                delta = level - previous[member]
                if not -128 <= delta < DELTA_UNAVAILABLE:
                    delta = DELTA_UNAVAILABLE
                self._indicate(member, level, delta)
                self._local_changed(member, level, delta)
        for member in previous:
            if member not in managed:
                self._indicate(member, NOT_MANAGED, 0)

    def _iso_events(self):
        connection = self.connection
        return (
            event
            for event in connection.link.roles.cig.events()
            if event.connection is connection and event.established
        )

    def _observe_rssi(self, packet: Received) -> PowerPhy:
        phy = PowerPhy.from_radio(packet.phy, packet.coding)
        rssi = self.connection.link.reported_rssi(packet.rssi)
        self.rssi[phy] = (3 * self.rssi.get(phy, rssi) + rssi) / 4
        return phy

    def received_iso(self, packet: Received) -> None:
        self._observe_rssi(packet)

    def received(self, packet: Received) -> None:
        phy = self.last_rx_phy = self._observe_rssi(packet)
        if (power := self.remote_levels.get(phy, POWER_UNAVAILABLE)) >= NOT_MANAGED:
            if self.path_loss.unavailable():
                # The Host ignores Zone_Entered when the path loss is unavailable.
                self.owner.listener.on_path_loss_threshold(
                    self.connection, PATH_LOSS_UNAVAILABLE, PathLossZone.LOW
                )
            return
        loss = max(0, min(MAX_PATH_LOSS, round(power - self.rssi[phy])))
        zone = self.path_loss.observe(loss, self.connection.event_counter)
        if zone is not None:
            self.owner.listener.on_path_loss_threshold(self.connection, loss, zone)

    @property
    def peer_supports_request(self) -> bool:
        features = self.connection.peer_features
        return features is None or bool(
            features
            & (
                LeFeatureMask.LE_POWER_CONTROL_REQUEST
                | LeFeatureMask.LE_POWER_CONTROL_REQUEST_DUP
            )
        )

    def request_if_needed(self) -> None:
        llcp = self.connection.llcp
        if (
            llcp.local_procedure_busy
            or llcp.has_pending('power_adjustment')
            or not self.peer_supports_request
            or self.connection.radio.now_us < self.next_request_us
        ):
            return
        features = self.connection.peer_features
        phy = self.rx_phy
        peer_power = self.remote_levels.get(phy, NOT_MANAGED)
        if (self.report_remote and not self.requested) or (
            self.path_loss.enabled and peer_power == NOT_MANAGED
        ):
            self._schedule_adjustment(phy, 0)
            return
        candidates = tuple(
            dict.fromkeys((phy, *(event.rx_power_phy for event in self._iso_events())))
        )
        for candidate in candidates:
            if (
                features is not None
                or self.peer_requested
                or candidate in self.remote_levels
            ):
                delta = self._peer_adjustment(candidate)
                # A neutral query recovers ACL loss monitoring at steady RSSI.
                if delta or (
                    candidate == phy
                    and self.path_loss.enabled
                    and peer_power == POWER_UNAVAILABLE
                ):
                    self._schedule_adjustment(candidate, delta)
                    return

    def _schedule_adjustment(self, phy: PowerPhy, delta: int) -> None:
        self.connection.llcp.initiate(
            DeferredProcedure('power_adjustment', lambda: self._request(phy, delta))
        )

    def _peer_adjustment(self, phy: PowerPhy) -> int:
        rssi = self.rssi.get(phy)
        if rssi is None:
            return 0
        flags = self.remote_flags.get(phy, 0)
        if (rssi > RSSI_UPPER_DBM and not flags & MINIMUM_POWER) or (
            rssi < RSSI_LOWER_DBM and not flags & MAXIMUM_POWER
        ):
            return max(-128, min(126, round(RSSI_TARGET_DBM - rssi)))
        return 0

    def _request(self, phy: PowerPhy, delta: int, *, host: bool = False) -> None:
        now = self.connection.radio.now_us
        self.request_phy = phy
        self.requested = True
        self.next_request_us = now + POWER_REQUEST_INTERVAL_US
        self.context = ProcedureContext(
            Opcode.LL_POWER_CONTROL_REQ,
            frozenset((Opcode.LL_POWER_CONTROL_RSP,)),
            self.control,
            host_initiated=host,
        )
        tx_power = (
            self._local_power(phy) if phy in self.managed_phys else POWER_UNAVAILABLE
        )
        self.connection.llcp.send(
            self.context,
            REQUEST.pack(Opcode.LL_POWER_CONTROL_REQ, phy.mask, delta, tx_power),
        )

    def _peer_changed(
        self,
        phy: PowerPhy,
        level: int,
        flags: int | None,
        delta: int | None,
        *,
        report: bool = True,
    ) -> None:
        previous = self.remote_levels.get(phy)
        self.remote_levels[phy] = level
        if flags is not None or level != previous:
            self.remote_flags[phy] = (
                0 if flags is None or level >= NOT_MANAGED else flags
            )
        if previous is not None and previous != level and level < NOT_MANAGED:
            # Samples before the power change were measured at the old level.
            self.rssi.pop(phy, None)
        if (
            report
            and self.report_remote
            and (level != previous or delta not in (None, 0))
        ):
            # A first query establishes the baseline. An unsolicited nonzero
            # Delta reports a change even when no earlier level is known.
            if previous is None and delta in (None, 0):
                return
            if delta is None:
                delta = (
                    level - previous
                    if previous is not None and previous < NOT_MANAGED
                    else DELTA_UNAVAILABLE
                )
                if not -128 <= delta < DELTA_UNAVAILABLE:
                    delta = DELTA_UNAVAILABLE
            self._report(
                PowerReport.REMOTE_CHANGED, phy, level, self.remote_flags[phy], delta
            )

    def finish(
        self, status: int, level: int = POWER_UNAVAILABLE, flags: int = 0
    ) -> None:
        if not self.pending:
            return
        self.context.finish()
        if self.context.host_initiated:
            self._report(
                PowerReport.REMOTE_READ, self.request_phy, level, flags, 0, status
            )

    def close(self, reason: int) -> None:
        self.finish(reason)

    def _receive_request(self, payload: bytes) -> None:
        _, mask, requested, peer_power = REQUEST.unpack(payload)
        if mask not in (1, 2, 4, 8):
            self.owner.reject(
                Opcode.LL_POWER_CONTROL_REQ,
                ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR,
            )
            return
        if peer_power == NOT_MANAGED:
            self.owner.reject(
                Opcode.LL_POWER_CONTROL_REQ,
                ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR,
            )
            return
        phy = PowerPhy(mask.bit_length())
        rssi = self.rssi.get(phy)
        self.peer_requested = True
        if peer_power != POWER_UNAVAILABLE:
            self._peer_changed(phy, peer_power, None, None)
        if phy not in self.managed_phys:
            response = RESPONSE.pack(
                Opcode.LL_POWER_CONTROL_RSP, 0, 0, NOT_MANAGED, APR_UNAVAILABLE
            )
        else:
            previous = self._local_power(phy)
            if requested:
                levels = tuple(
                    self._radiative(nominal) for nominal in TX_POWER_LEVELS_DBM
                )
                target = previous + requested
                index = (
                    len(levels) - 1
                    if requested == MAX_POWER_REQUEST
                    else min(bisect_left(levels, target), len(levels) - 1)
                )
                self.local_levels[phy] = TX_POWER_LEVELS_DBM[index]
            level = self._local_power(phy)
            delta = level - previous
            apr = (
                APR_UNAVAILABLE
                if self.pending or rssi is None
                else max(0, round(rssi - RSSI_LOWER_DBM))
            )
            if 0 < apr < APR_UNAVAILABLE:
                self.next_request_us = max(
                    self.next_request_us,
                    self.connection.radio.now_us
                    + 2 * self.connection.parameters.interval_us,
                )
            response = RESPONSE.pack(
                Opcode.LL_POWER_CONTROL_RSP, self._flags(level), delta, level, apr
            )
            self._local_changed(phy, level, delta)
        self.connection.llcp.respond(response, priority=True)

    def control(self, payload: bytes) -> bool:
        opcode = payload[0]
        if opcode == Opcode.LL_POWER_CONTROL_REQ and len(payload) == REQUEST.size:
            self._receive_request(payload)
        elif opcode == Opcode.LL_POWER_CONTROL_RSP and len(payload) == RESPONSE.size:
            if self.pending and self.context.transmitted:
                _, flags, delta, power, _ = RESPONSE.unpack(payload)
                flags &= MINIMUM_POWER | MAXIMUM_POWER
                if power >= NOT_MANAGED:
                    flags = delta = 0
                self._peer_changed(
                    self.request_phy,
                    power,
                    flags,
                    delta,
                    report=not self.context.host_initiated,
                )
                self.finish(ErrorCode.SUCCESS, power, flags)
        elif opcode == Opcode.LL_POWER_CHANGE_IND and len(payload) == CHANGE.size:
            _, mask, flags, delta, power = CHANGE.unpack(payload)
            for phy in PowerPhy:
                if mask & phy.mask:
                    self._peer_changed(
                        phy, power, flags & (MINIMUM_POWER | MAXIMUM_POWER), delta
                    )
        elif (
            self.pending
            and self.context.transmitted
            and payload[:2]
            == bytes((Opcode.LL_UNKNOWN_RSP, Opcode.LL_POWER_CONTROL_REQ))
        ):
            self.finish(ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)
        elif (
            self.pending
            and self.context.transmitted
            and len(payload) == 3
            and payload[:2]
            == bytes((Opcode.LL_REJECT_EXT_IND, Opcode.LL_POWER_CONTROL_REQ))
        ):
            self.finish(payload[2])
        else:
            return False
        return True
