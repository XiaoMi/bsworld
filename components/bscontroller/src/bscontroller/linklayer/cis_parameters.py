# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""CIG event layout and CIS control PDU parameters (Core Vol 6, Part B)."""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace
from math import ceil, lcm
from typing import TYPE_CHECKING

from bscontroller.linklayer import (
    LL_SUPERVISION_TIMEOUT_UNIT_US,
    ControlOpcode,
)
from bscontroller.linklayer.cis import CisDirection, CisEventParameters
from bscontroller.linklayer.timing import signed_counter_distance
from bscontroller.radio.base import Phy

if TYPE_CHECKING:
    from bscontroller.linklayer.api import CigConfig, CisConfig
    from bscontroller.linklayer.connection import Connection

ISO_INTERVAL_UNIT_US = 1250
MIN_CIS_OFFSET_US = 500
MIN_SUBINTERVAL_US = 400
T_MSS_CIS_US = 150
T_IFS_CIS_US = 150
MAX_CIS_BURST_NUMBER = 1
MAX_CIS_FLUSH_TIMEOUT = 1
# Core Vol 6, Part G, 2.2: SDU delivery may drift by up to 100 ppm.
SDU_DRIFT_FACTOR = 1.0001
PHY_BY_MASK = {1 << (int(phy) - 1): phy for phy in Phy}
CIS_REQ_FIELDS = struct.Struct('<BBBBHH3s3sHHB3sBBBH')
CIS_WINDOW_FIELDS = struct.Struct('<3s3sH')
CIS_IND_FIELDS = struct.Struct('<I3s3s3sH')


class UnsupportedCisParameters(ValueError):
    """A CIS parameter exceeds the supported profile."""


class UnsupportedCisBurstOrFlush(UnsupportedCisParameters):
    """A CIS burst number or flush timeout exceeds the mandatory range."""


def valid_cis_supervision_timeout(interval_us: int, timeout: int) -> bool:
    """The ACL supervision timeout must exceed two ISO intervals (4.5.2)."""
    return 2 * interval_us < timeout * LL_SUPERVISION_TIMEOUT_UNIT_US


def _valid_subevent_timing(interval_us: int, nse: int, subinterval_us: int) -> bool:
    if nse == 1:
        return subinterval_us == 0
    return MIN_SUBINTERVAL_US <= subinterval_us and nse * subinterval_us <= interval_us


def _u24(value: int) -> bytes:
    return value.to_bytes(3, 'little')


def _read_u24(value: bytes) -> int:
    return int.from_bytes(value, 'little')


def _direction(
    phy_mask: int, pdu: int, bn: int, ft: int, interval: int, sdu: int
) -> CisDirection:
    if (
        phy_mask not in PHY_BY_MASK
        or not 255 <= interval <= 0x0FFFFF
        or not 0 <= bn <= 15
        or not 0 <= pdu <= 251
        or bool(sdu) != bool(bn)
        or bool(pdu) != bool(bn)
        or not 1 <= ft <= 255
    ):
        raise ValueError('invalid CIS direction')
    if bn > MAX_CIS_BURST_NUMBER or ft > MAX_CIS_FLUSH_TIMEOUT:
        raise UnsupportedCisBurstOrFlush('CIS burst number or flush timeout')
    return CisDirection(PHY_BY_MASK[phy_mask], pdu, bn, ft, interval, sdu)


def subevent_duration(
    c_to_p: CisDirection,
    p_to_c: CisDirection,
    spacing_us: int = T_IFS_CIS_US,
) -> int:
    def packet_time(direction: CisDirection) -> int:
        # Reserve the MIC as well so an encrypted ACL can be selected later.
        return direction.phy.airtime_us(2 + direction.max_pdu + 4, direction.coding)

    return packet_time(c_to_p) + spacing_us + packet_time(p_to_c)


@dataclass(frozen=True)
class CisWindow:
    offset_min_us: int
    offset_max_us: int
    conn_event_count: int

    def encode(self) -> bytes:
        return CIS_WINDOW_FIELDS.pack(
            _u24(self.offset_min_us), _u24(self.offset_max_us), self.conn_event_count
        )

    @classmethod
    def decode(cls, data: bytes) -> CisWindow:
        minimum, maximum, event = CIS_WINDOW_FIELDS.unpack(data)
        return cls(_read_u24(minimum), _read_u24(maximum), event)

    @property
    def width_us(self) -> int:
        return self.offset_max_us - self.offset_min_us

    def validate(self, connection: Connection) -> None:
        if not (
            MIN_CIS_OFFSET_US
            <= self.offset_min_us
            <= self.offset_max_us
            < connection.parameters.interval_us
        ):
            raise ValueError('invalid CIS anchor window')
        event_anchor(connection, self.conn_event_count)

    def anchor_us(self, connection: Connection) -> int:
        return event_anchor(connection, self.conn_event_count) + self.offset_min_us

    def offset_at(
        self, connection: Connection, counter: int, iso_interval_us: int
    ) -> int:
        """Equivalent window phase in the negotiated ACL and ISO units (6.B.5.1.15)."""
        events = event_distance(connection, counter) - event_distance(
            connection, self.conn_event_count
        )
        elapsed = events * connection.parameters.interval_us
        return (self.offset_min_us - elapsed) % iso_interval_us


def event_distance(connection: Connection, counter: int) -> int:
    difference = signed_counter_distance(counter, connection.event_counter)
    if not -0x4000 < difference < 0x4000:
        raise ValueError('invalid CIS connection event count')
    return difference


def event_anchor(connection: Connection, counter: int) -> int:
    return connection.project_anchor(event_distance(connection, counter))[0]


@dataclass(frozen=True)
class CisRequest:
    cig_id: int
    cis_id: int
    parameters: CisEventParameters
    window: CisWindow

    def encode(self) -> bytes:
        parameters = self.parameters
        central, peripheral = parameters.c_to_p, parameters.p_to_c
        framed = int(parameters.framed) << 15 | parameters.framing_mode << 14
        return (
            bytes([ControlOpcode.LL_CIS_REQ])
            + CIS_REQ_FIELDS.pack(
                self.cig_id,
                self.cis_id,
                1 << (int(central.phy) - 1),
                1 << (int(peripheral.phy) - 1),
                central.max_sdu | framed,
                peripheral.max_sdu,
                _u24(central.sdu_interval_us),
                _u24(peripheral.sdu_interval_us),
                central.max_pdu,
                peripheral.max_pdu,
                parameters.nse,
                _u24(parameters.subinterval_us),
                central.bn | peripheral.bn << 4,
                central.ft,
                peripheral.ft,
                parameters.interval_us // ISO_INTERVAL_UNIT_US,
            )
            + self.window.encode()
        )

    @classmethod
    def decode(cls, payload: bytes) -> CisRequest:
        (
            cig_id,
            cis_id,
            c_phy,
            p_phy,
            c_sdu,
            p_sdu,
            c_interval,
            p_interval,
            c_pdu,
            p_pdu,
            nse,
            subinterval,
            bn,
            c_ft,
            p_ft,
            iso_interval,
        ) = CIS_REQ_FIELDS.unpack_from(payload, 1)
        framed = bool(c_sdu & 0x8000)
        if framed and c_sdu & 0x4000:
            raise UnsupportedCisParameters('unsegmented CIS framing')
        # Ignore the SDU RFU fields (Figure 2.50). Each PHY field is checked
        # in full for a single supported bit (Core Vol 6, Part B, 5.1.15).
        central = _direction(
            c_phy,
            c_pdu,
            bn & 0x0F,
            c_ft,
            _read_u24(c_interval) & 0x0FFFFF,
            c_sdu & 0x0FFF,
        )
        peripheral = _direction(
            p_phy,
            p_pdu,
            bn >> 4,
            p_ft,
            _read_u24(p_interval) & 0x0FFFFF,
            p_sdu & 0x0FFF,
        )
        interval_us = iso_interval * ISO_INTERVAL_UNIT_US
        subinterval_us = _read_u24(subinterval)
        if not (
            4 <= iso_interval <= 3200
            and 1 <= nse <= 31
            and max(central.bn, peripheral.bn) <= nse
            and central.bn + peripheral.bn > 0
            and _valid_subevent_timing(interval_us, nse, subinterval_us)
        ):
            raise ValueError('invalid CIS event parameters')
        for direction in (central, peripheral):
            if direction.bn:
                direction.isoal(framed=framed, interval_us=interval_us)
        parameters = CisEventParameters(
            central,
            peripheral,
            interval_us,
            subinterval_us,
            nse,
            framed,
            cis_sync_delay_us=0,
            cig_sync_delay_us=0,
        )
        return cls(
            cig_id,
            cis_id,
            parameters,
            CisWindow.decode(payload[1 + CIS_REQ_FIELDS.size :]),
        )


@dataclass(frozen=True)
class CisIndication:
    access_address: int
    offset_us: int
    cig_sync_delay_us: int
    cis_sync_delay_us: int
    conn_event_count: int

    def encode(self) -> bytes:
        return bytes([ControlOpcode.LL_CIS_IND]) + CIS_IND_FIELDS.pack(
            self.access_address,
            _u24(self.offset_us),
            _u24(self.cig_sync_delay_us),
            _u24(self.cis_sync_delay_us),
            self.conn_event_count,
        )

    @classmethod
    def decode(cls, payload: bytes) -> CisIndication:
        address, offset, cig_delay, cis_delay, count = CIS_IND_FIELDS.unpack_from(
            payload, 1
        )
        return cls(
            address,
            _read_u24(offset),
            _read_u24(cig_delay),
            _read_u24(cis_delay),
            count,
        )


def _configured_direction(
    config: CigConfig,
    stream: CisConfig,
    *,
    central: bool,
    interval_us: int,
) -> CisDirection:
    if central:
        phy, sdu, sdu_interval, rtn, pdu, bn, ft = (
            stream.phy_c_to_p,
            stream.max_sdu_c_to_p,
            config.sdu_interval_c_to_p,
            stream.rtn_c_to_p,
            stream.max_pdu_c_to_p,
            stream.bn_c_to_p,
            config.ft_c_to_p,
        )
    else:
        phy, sdu, sdu_interval, rtn, pdu, bn, ft = (
            stream.phy_p_to_c,
            stream.max_sdu_p_to_c,
            config.sdu_interval_p_to_c,
            stream.rtn_p_to_c,
            stream.max_pdu_p_to_c,
            stream.bn_p_to_c,
            config.ft_p_to_c,
        )
    if not 0 <= sdu <= 4095 or not phy or not 0 <= rtn <= 255:
        raise ValueError('invalid CIS SDU or PHY')
    if phy & ~0x07:
        raise UnsupportedCisParameters('unsupported CIS PHY')
    if config.iso_interval is None:
        # Prefer uncoded 2M, then 1M, then Coded when the Host offers a choice.
        phy = next(mask for mask in (2, 1, 4) if phy & mask)
        if sdu:
            if config.framing:
                # Vol 6, Part G, 2.2: account for the first segment header
                # and time offset when choosing the bandwidth of a framed CIS.
                drift_intervals = SDU_DRIFT_FACTOR * interval_us / sdu_interval
                capacity = ceil(drift_intervals) * 5 + ceil(drift_intervals * sdu)
                pdu = min(251, max(sdu + 5, capacity + 2))
                bn = ceil(drift_intervals * (sdu + 5) / (pdu - 2))
            else:
                count = interval_us // sdu_interval
                segments = ceil(sdu / 251)
                pdu, bn = ceil(sdu / segments), count * segments
        else:
            pdu = bn = 0
            if not 255 <= sdu_interval <= 0x0FFFFF:
                sdu_interval = min(interval_us, 0x0FFFFF)
    direction = _direction(phy, pdu, bn, ft, sdu_interval, sdu)
    if bn:
        direction.isoal(framed=bool(config.framing), interval_us=interval_us)
    return direction


@dataclass(frozen=True)
class _CisTiming:
    c_to_p: CisDirection
    p_to_c: CisDirection
    nse: int
    spacing_us: int
    minimum_subevent_space_us: int

    @property
    def span_us(self) -> int:
        return subevent_duration(self.c_to_p, self.p_to_c, self.spacing_us)

    @property
    def slot_us(self) -> int:
        return max(MIN_SUBINTERVAL_US, self.span_us + self.minimum_subevent_space_us)


def _place_cig(
    timings: dict[int, _CisTiming],
    config: CigConfig,
    interval_us: int,
) -> dict[int, CisEventParameters]:
    grouped = config.packing == 1 and len(timings) > 1
    group_slot = sum(timing.slot_us for timing in timings.values())
    offset_us = end_us = 0
    parameters = {}
    for cis_id, timing in timings.items():
        subinterval_us = (
            (group_slot if grouped else timing.slot_us) if timing.nse > 1 else 0
        )
        end = offset_us + (timing.nse - 1) * subinterval_us + timing.span_us
        end_us = max(end_us, end)
        parameters[cis_id] = CisEventParameters(
            timing.c_to_p,
            timing.p_to_c,
            interval_us,
            subinterval_us,
            timing.nse,
            bool(config.framing),
            cis_sync_delay_us=-offset_us,
            cig_sync_delay_us=0,
            spacing_us=timing.spacing_us,
            minimum_subevent_space_us=timing.minimum_subevent_space_us,
        )
        offset_us = (
            offset_us + timing.slot_us
            if grouped
            else end + timing.minimum_subevent_space_us
        )
    return {
        cis_id: replace(
            params,
            cig_sync_delay_us=end_us,
            cis_sync_delay_us=end_us + params.cis_sync_delay_us,
        )
        for cis_id, params in parameters.items()
    }


def _cig_fits(config: CigConfig, parameters: dict[int, CisEventParameters]) -> bool:
    for cis in parameters.values():
        if not _valid_subevent_timing(cis.interval_us, cis.nse, cis.subinterval_us) or (
            cis.cig_sync_delay_us + cis.minimum_subevent_space_us > cis.interval_us
        ):
            return False
        if config.iso_interval is None and (
            cis.transport_latency_us(central_to_peripheral=True)
            > config.max_transport_latency_c_to_p * 1000
            or cis.transport_latency_us(central_to_peripheral=False)
            > config.max_transport_latency_p_to_c * 1000
        ):
            return False
    return True


def plan_cig(
    config: CigConfig,
    *,
    spaces: dict[int, tuple[int, int]] | None = None,
) -> dict[int, CisEventParameters]:
    """Lay out the CIG with optional (T_IFS_CIS, T_MSS_CIS) per CIS ID."""
    if not (
        0 <= config.cig_id <= 239
        and all(0 <= stream.cis_id <= 239 for stream in config.cis_parameters)
        and len({stream.cis_id for stream in config.cis_parameters})
        == len(config.cis_parameters)
        and config.packing in (0, 1)
        and config.framing in (0, 1, 2)
        and 0 <= config.worst_case_sca <= 7
    ):
        raise ValueError('invalid CIG parameters')
    if config.framing == 2:
        raise UnsupportedCisParameters('unsegmented CIS framing')
    if config.iso_interval is None:
        has_tx = any(item.max_sdu_c_to_p for item in config.cis_parameters)
        has_rx = any(item.max_sdu_p_to_c for item in config.cis_parameters)
        if (
            has_tx
            and not 5 <= config.max_transport_latency_c_to_p <= 4000
            or has_rx
            and not 5 <= config.max_transport_latency_p_to_c <= 4000
        ):
            raise ValueError('invalid CIG transport latency')
        intervals = [
            interval
            for supported, interval in (
                (has_tx, config.sdu_interval_c_to_p),
                (has_rx, config.sdu_interval_p_to_c),
            )
            if supported
        ]
        if any(not 255 <= interval <= 0x0FFFFF for interval in intervals):
            raise ValueError('invalid CIG SDU interval')
        if not config.framing:
            multiple = lcm(ISO_INTERVAL_UNIT_US, *intervals)
            interval_us = ceil(5000 / multiple) * multiple
            try:
                return _plan_cig_interval(config, spaces, interval_us)
            except ValueError:
                # Vol 4, Part E, 7.8.97: unframed permits framing when the
                # unframed requirements cannot be satisfied.
                config = replace(config, framing=1)
        # A smaller ISO interval can satisfy SDU throughput with BN=1 even
        # when a complete framed SDU will not fit in one CIS PDU.
        bandwidth_intervals = (
            249 * interval / (SDU_DRIFT_FACTOR * (sdu + 5))
            for stream in config.cis_parameters
            for interval, sdu in (
                (config.sdu_interval_c_to_p, stream.max_sdu_c_to_p),
                (config.sdu_interval_p_to_c, stream.max_sdu_p_to_c),
            )
            if sdu > 0
        )
        shortest = min(
            min(intervals, default=5000), min(bandwidth_intervals, default=5000)
        )
        interval_us = max(
            5000, int(shortest // ISO_INTERVAL_UNIT_US) * ISO_INTERVAL_UNIT_US
        )
        return _plan_cig_interval(config, spaces, interval_us)

    return _plan_cig_interval(
        config, spaces, config.iso_interval * ISO_INTERVAL_UNIT_US
    )


def _plan_cig_interval(
    config: CigConfig,
    spaces: dict[int, tuple[int, int]] | None,
    interval_us: int,
) -> dict[int, CisEventParameters]:
    if not 5000 <= interval_us <= 4000000:
        raise ValueError('invalid CIS interval')

    timings = {}
    for stream in config.cis_parameters:
        tx = _configured_direction(
            config, stream, central=True, interval_us=interval_us
        )
        rx = _configured_direction(
            config, stream, central=False, interval_us=interval_us
        )
        nse = stream.nse
        if nse is None:
            nse = min(
                31,
                max(tx.bn * (1 + stream.rtn_c_to_p), rx.bn * (1 + stream.rtn_p_to_c)),
            )
        if not 1 <= nse <= 31 or nse < max(tx.bn, rx.bn) or not (tx.bn or rx.bn):
            raise ValueError('invalid CIS subevent count')
        spacing, subevent_space = (spaces or {}).get(
            stream.cis_id, (T_IFS_CIS_US, T_MSS_CIS_US)
        )
        timings[stream.cis_id] = _CisTiming(tx, rx, nse, spacing, subevent_space)

    parameters = _place_cig(timings, config, interval_us)
    if _cig_fits(config, parameters):
        return parameters
    if config.iso_interval is None:
        # Core Vol 4, Part E, 7.8.97: RTN is a recommendation. Use one
        # subevent per CIS when the preferred repetitions do not fit.
        minimum = {cis_id: replace(timing, nse=1) for cis_id, timing in timings.items()}
        parameters = _place_cig(minimum, config, interval_us)
        if _cig_fits(config, parameters):
            return parameters
    raise ValueError('CIG exceeds available event time or transport latency')
