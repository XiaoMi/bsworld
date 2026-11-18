# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""BIG broadcaster and synchronized receiver ownership (Core Vol 6, B, 4.4.6).

The manager validates Host configuration, owns group lifetimes and publishes
radio reservations. Its RF executor uses the same dispatcher as advertising
and connections; all codec-independent SDU state belongs to IsoDataPaths.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import partial
from itertools import islice
from math import ceil
from typing import TYPE_CHECKING

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import LL_IFS_US, LL_MAFS_US
from bscontroller.linklayer.api import BigConfig, BigSnapshot, BigSyncConfig
from bscontroller.linklayer.bis import (
    BIG_COUNTER_MASK,
    BIG_INFO_AD_TYPE,
    BigInfo,
    BisCipher,
    generate_big_seed,
    group_session_key,
)
from bscontroller.linklayer.extended import periodic_time_us, required_periodic_interval
from bscontroller.linklayer.iso import IsoDataPaths
from bscontroller.linklayer.iso_radio import run_big_event
from bscontroller.linklayer.isoal import IsoalParameters, IsoPdu
from bscontroller.linklayer.parameters import valid_channel_map
from bscontroller.linklayer.pawr import acad_structures
from bscontroller.linklayer.resource import PendingOperation
from bscontroller.linklayer.scheduler import (
    RadioPriority,
    RadioReservation,
    first_radio_gap,
)
from bscontroller.linklayer.timing import window_widening
from bscontroller.radio.base import RADIO_SWITCH_TIME_US, Phy

if TYPE_CHECKING:
    from bscontroller.linklayer.discovery import PeriodicSync
    from bscontroller.linklayer.link import RadioLink

SUCCESS = ErrorCode.SUCCESS
INVALID = ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
DISALLOWED = ErrorCode.COMMAND_DISALLOWED_ERROR
UNSUPPORTED = ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR
UNKNOWN_ADV = ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR
UNKNOWN = ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
LIMITED = ErrorCode.CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR

# Uncoded scheduling margin beyond T_MAFS: separate PA/BIG receive reservations
# and leave room for periodic data growth. Coded and explicit Test geometry keep
# the compact layout for their longer or Host-controlled subevents.
BIG_PERIODIC_GUARD_US = 2000
# Establishment and minimum synchronization timeout (Vol 6, B, 4.4.5.2).
BIG_SYNC_EVENTS = 6


@dataclass
class BigControl:
    payload: bytes
    instant: int
    start: int = 0
    channel_map: int | None = None
    reason: int | None = None


@dataclass
class Big:
    handle: int
    info: BigInfo | None
    stream_handles: tuple[int, ...]
    numbers: tuple[int, ...]
    next_us: int
    transmit: bool
    advertising_handle: int | None = None
    sync_config: BigSyncConfig | None = None
    sync: PeriodicSync | None = None
    last_acquisition_event: int | None = None
    counter: int = 0
    reported: bool = False
    cancelled: bool = False
    deadline_us: int = 0
    last_rx_us: int = 0
    widening_us: int = 30
    sca: int = 0
    cssn: int = 0
    control: BigControl | None = None
    pending_control: BigControl | None = None
    running: bool = False
    received_cssn: int | None = None
    ciphers: dict[int, BisCipher] = field(default_factory=dict)
    # Cache by associated BIG event, not RF transmission time: pre-transmission
    # and repetition must use identical plaintext and CCM nonce.
    payloads: dict[int, dict[int, tuple[IsoPdu, ...]]] = field(default_factory=dict)

    def reservation(self) -> RadioReservation:
        assert self.info is not None
        start = self.next_us - RADIO_SWITCH_TIME_US
        end = self.next_us + self.info.duration_us
        if not self.transmit:
            start -= self.receive_window_us(self.next_us)
            end += self.receive_window_us(end)
        return RadioReservation(
            start, end - start, self.info.interval_us, RadioPriority.ISOCHRONOUS
        )

    def snapshot(self) -> BigSnapshot:
        return BigSnapshot(self.handle, self.info, self.stream_handles)

    def associated_event(self, subevent: int) -> int:
        assert self.info is not None
        group = subevent // self.info.bn
        return self.counter + max(0, group - self.info.irc + 1) * self.info.pto

    def receive_window_us(self, target_us: int, *, active: bool = False) -> int:
        return self.widening_us + window_widening(
            target_us - self.last_rx_us, self.sca, active=active
        )


def isoal_parameters(info: BigInfo) -> IsoalParameters:
    return IsoalParameters(
        info.framed,
        info.bn,
        info.max_pdu,
        info.max_sdu,
        info.interval_us,
        info.sdu_interval_us,
    )


class BroadcastManager:
    def __init__(self, link: RadioLink, iso: IsoDataPaths):
        self.link = link
        self.groups: dict[int, Big] = {}
        self.iso = iso

    def reset(self) -> None:
        self.groups.clear()

    def _capacity(self, count: int) -> bool:
        return (
            len(self.groups) < self.link.config.max_bigs
            and len(self.connection_handles()) + count <= self.link.config.max_bis
            and count <= self.link.roles.free_connection_handle_count()
        )

    def connection_handles(self) -> frozenset[int]:
        """Include pending acquisitions, whose handles are not yet ISO streams."""
        return frozenset(
            handle for group in self.groups.values() for handle in group.stream_handles
        )

    def _handles(self, count: int) -> tuple[int, ...]:
        return tuple(islice(self.link.roles.available_connection_handles(), count))

    def associated_group(self, advertising_handle: int) -> Big | None:
        return next(
            (
                group
                for group in self.groups.values()
                if group.advertising_handle == advertising_handle
            ),
            None,
        )

    def create(self, config: BigConfig) -> int:
        """Validate and publish atomically; completion follows on the dispatcher."""
        range_error = UNSUPPORTED if config.test is not None else INVALID
        if not 0 <= config.handle <= 0xEF or not 0 <= config.advertising_handle <= 0xEF:
            return range_error
        if not 1 <= config.phy <= 7:
            return UNSUPPORTED
        if config.handle in self.groups:
            return DISALLOWED
        item = self.link.advertisers.get(config.advertising_handle)
        if (
            item is None
            or not item.periodic_interval
            or item.pawr is not None
            or self.associated_group(config.advertising_handle) is not None
        ):
            return UNKNOWN_ADV
        if config.framing == 2:
            return UNSUPPORTED
        if not (
            1 <= config.num_bis <= 31
            and 255 <= config.sdu_interval_us <= 0x0FFFFF
            and 1 <= config.max_sdu <= 4095
            and config.packing in (0, 1)
            and config.framing in (0, 1)
            and config.encryption in (0, 1)
            and len(config.broadcast_code) == 16
        ):
            return range_error
        if not self._capacity(config.num_bis):
            return LIMITED
        if config.test is None and not (
            5 <= config.max_transport_latency_ms <= 4000 and 0 <= config.rtn <= 30
        ):
            return INVALID
        if config.test is not None:
            test = config.test
            if not (
                config.phy in (1, 2, 4)
                and 4 <= test.iso_interval <= 3200
                and 1 <= test.bn <= 7
                and 1 <= test.nse <= 31
                and test.nse % test.bn == 0
                and 1 <= test.irc <= 15
                and test.nse >= test.irc * test.bn
                and 0 <= test.pto <= 15
                and 1 <= test.max_pdu <= 251
            ):
                return UNSUPPORTED
        try:
            info = self._parameters(config)
        except ValueError:
            return INVALID
        placeholder = info.encode()
        candidate = replace(item, big_info=placeholder)
        # Leave room for a later PA channel-map indication as well as BIGInfo.
        # Otherwise its extra ACAD can overlap every BIG anchor and starve PA.
        periodic_duration = max(
            periodic_time_us(candidate),
            periodic_time_us(
                replace(candidate, periodic_channel_update=(0, info.channel_map))
            ),
        )
        # An enabled train has a fixed interval. Before enable the Controller
        # may choose any interval up to the Host maximum (Vol 4, E, 7.8.103).
        periodic_interval = max(
            item.periodic_interval, required_periodic_interval(candidate)
        )
        if periodic_interval > item.periodic_interval_limit:
            return ErrorCode.PACKET_TOO_LONG_ERROR
        group = Big(
            config.handle,
            info,
            self._handles(config.num_bis),
            tuple(range(1, config.num_bis + 1)),
            0,
            True,
            config.advertising_handle,
        )
        periodic_guard = LL_MAFS_US
        if (
            config.test is None
            and info.phy != Phy.LE_CODED
            and (
                info.duration_us
                + periodic_duration
                + BIG_PERIODIC_GUARD_US
                + LL_IFS_US
                + RADIO_SWITCH_TIME_US
                <= info.interval_us
            )
        ):
            periodic_guard = BIG_PERIODIC_GUARD_US
        start = max(
            self.link.now_us + 2000,
            item.periodic_next_us + periodic_duration + periodic_guard
            if item.periodic_enabled
            else 0,
        )
        anchor = first_radio_gap(
            self.link.resource.reservations(),
            start,
            info.duration_us + LL_IFS_US + RADIO_SWITCH_TIME_US,
            start + info.interval_us,
        )
        if anchor is None:
            return LIMITED
        group.next_us = anchor + RADIO_SWITCH_TIME_US
        for handle in group.stream_handles:
            self.iso.add(handle, True, isoal_parameters(info))
        self._ciphers(group, config.broadcast_code)
        self.groups[group.handle] = group
        item.periodic_interval = periodic_interval
        item.big_info = placeholder
        return SUCCESS

    def _parameters(self, config: BigConfig) -> BigInfo:
        phy = (
            Phy.LE_2M
            if config.phy & 2
            else Phy.LE_1M
            if config.phy & 1
            else Phy.LE_CODED
        )
        framed = bool(config.framing)
        if config.test is not None:
            test = config.test
            interval, nse, bn, pto, irc, max_pdu = (
                test.iso_interval,
                test.nse,
                test.bn,
                test.pto,
                test.irc,
                test.max_pdu,
            )
        else:
            # Prefer the requested unframed geometry. When its integral timing
            # or burst capacity is unavailable, the Host allows framed PDUs.
            interval = max(4, min(3200, config.sdu_interval_us // 1250))
            if interval * 1250 != config.sdu_interval_us:
                framed = True
            budget = config.max_sdu + (7 if framed else 0)
            bn = max(1, ceil(budget / 249))
            if bn > 7:
                # Framed SDUs can span events; shorten the interval to provide
                # enough bursts per SDU interval without exceeding Max_PDU.
                interval = max(4, config.sdu_interval_us * 7 * 249 // (budget * 1250))
                bn, framed = 7, True
            max_pdu = (
                min(
                    251,
                    ceil(
                        (config.max_sdu + 7)
                        * interval
                        * 1250
                        / config.sdu_interval_us
                        / bn
                    )
                    + 2,
                )
                if framed
                else ceil(config.max_sdu / bn)
            )
            irc = min(15, 31 // bn, config.rtn + 1)
            nse, pto = bn * irc, 0
        mpt = phy.airtime_us(2 + max_pdu + 4 * config.encryption)
        spacing = mpt + LL_IFS_US
        sub_interval = spacing * config.num_bis if config.packing else spacing
        bis_spacing = spacing if config.packing else spacing * nse
        used = self.link.used_access_addresses()
        seed = generate_big_seed(
            self.link.random,
            config.num_bis,
            used,
            (
                group.info.seed_access_address
                for group in self.groups.values()
                if group.transmit and group.info is not None
            ),
        )
        info = BigInfo(
            offset_us=990,
            iso_interval=interval,
            num_bis=config.num_bis,
            nse=nse,
            bn=bn,
            sub_interval_us=sub_interval,
            pto=pto,
            bis_spacing_us=bis_spacing,
            irc=irc,
            max_pdu=max_pdu,
            seed_access_address=seed,
            sdu_interval_us=config.sdu_interval_us,
            max_sdu=config.max_sdu,
            base_crc_init=self.link.random.getrandbits(16),
            channel_map=self.link.roles.host_channel_map,
            phy=phy,
            framed=framed,
            giv=self.link.random.randbytes(8) if config.encryption else b'',
            gskd=self.link.random.randbytes(16) if config.encryption else b'',
        )
        isoal_parameters(info)
        if info.framed and (
            info.bn * (info.max_pdu - 2) * info.sdu_interval_us * 10000
            < (info.max_sdu + 5) * info.interval_us * 10001
        ):
            raise ValueError('framed data exceeds ISOAL bandwidth')
        # RTN is a recommendation (Vol 4, E, 7.8.103). Reduce repetitions when
        # their airtime cannot fit, rather than rejecting an otherwise viable
        # Coded or multi-BIS stream. Explicit test parameters are never altered.
        while (
            config.test is None
            and info.irc > 1
            and (
                info.duration_us + LL_IFS_US > info.interval_us
                or info.transport_latency_us > config.max_transport_latency_ms * 1000
            )
        ):
            irc = info.irc - 1
            info = replace(
                info,
                nse=bn * irc,
                irc=irc,
                bis_spacing_us=spacing if config.packing else spacing * bn * irc,
            )
        if info.duration_us + LL_IFS_US > info.interval_us:
            raise ValueError('BIG subevents exceed ISO_Interval')
        if info.transport_latency_us > 0xFFFFFF or (
            config.test is None
            and info.transport_latency_us > config.max_transport_latency_ms * 1000
        ):
            raise ValueError('BIG exceeds requested transport latency')
        return info

    @staticmethod
    def _ciphers(group: Big, code: bytes) -> None:
        info = group.info
        assert info is not None
        if info.encrypted:
            key = group_session_key(code, info.gskd)
            group.ciphers = {
                number: BisCipher(key, info.giv, info.access_address(number))
                for number in (0, *group.numbers)
            }

    def advertising_info(self, handle: int, start_us: int) -> bytes:
        group = self.associated_group(handle)
        if group is None or group.info is None:
            return b''
        steps = max(
            0,
            (start_us + 630 - group.next_us + group.info.interval_us - 1)
            // group.info.interval_us,
        )
        return replace(
            group.info,
            offset_us=group.next_us + steps * group.info.interval_us - start_us,
            payload_count=(group.counter + steps) * group.info.bn,
        ).encode()

    def synchronize(self, config: BigSyncConfig) -> int:
        if config.handle in self.groups:
            return DISALLOWED
        sync = self.link.syncs.get(config.sync_handle)
        if sync is None:
            return UNKNOWN_ADV
        if any(
            group.sync is not None
            and (
                not group.reported
                or (group.sync.address, group.sync.sid) == (sync.address, sync.sid)
            )
            for group in self.groups.values()
        ):
            return DISALLOWED
        if not (
            0 <= config.handle <= 0xEF
            and config.encryption in (0, 1)
            and len(config.broadcast_code) == 16
            and 0 <= config.mse <= 31
            and 10 <= config.timeout <= 0x4000
            and 1 <= len(config.bis) <= 31
            and all(1 <= n <= 31 for n in config.bis)
            and tuple(sorted(set(config.bis))) == config.bis
        ):
            return INVALID
        if not self._capacity(len(config.bis)):
            return LIMITED
        group = Big(
            config.handle,
            None,
            self._handles(len(config.bis)),
            config.bis,
            0,
            False,
            sync_config=config,
            sync=sync,
        )
        self.groups[group.handle] = group
        # Wait until prepare() so the command's Status precedes all completion
        # events, including errors inferred from an already received BIGInfo.
        return SUCCESS

    def observed_info(
        self, sync: PeriodicSync, acad: bytes, packet_start_us: int
    ) -> None:
        if self.link.syncs.get(sync.handle) is not sync:
            return
        sync.big_info = None
        try:
            structures = acad_structures(acad)
        except ValueError:
            return
        for kind, data in structures:
            if kind != BIG_INFO_AD_TYPE:
                continue
            try:
                info = BigInfo.decode(data)
            except ValueError:
                continue
            sync.big_info = info, packet_start_us
            self.link.listener.on_big_info(sync.handle, info)

    def _acquire(self, group: Big) -> None:
        config = group.sync_config
        sync = group.sync
        assert config is not None and sync is not None
        if self.link.syncs.get(sync.handle) is not sync:
            self._close(group, ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR)
            return
        if sync.big_info is None:
            return
        info, start_us = sync.big_info
        if max(group.numbers) > info.num_bis:
            self._close(group, UNSUPPORTED)
            return
        if config.mse and config.mse < info.bn:
            self._close(group, INVALID)
            return
        if info.encrypted != bool(config.encryption):
            self._close(group, ErrorCode.ENCRYPTION_MODE_NOT_ACCEPTABLE_ERROR)
            return
        try:
            parameters = isoal_parameters(info)
            supported = (
                not (info.framed and info.framing_mode)
                and info.transport_latency_us <= 0xFFFFFF
            )
            if not supported:
                raise ValueError('unsupported BIGInfo')
        except ValueError:
            self._close(group, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)
            return
        group.info = info
        group.counter = info.payload_count // info.bn
        group.next_us = start_us + info.offset_us
        group.widening_us = 300 if info.offset_us >= 491460 else 30
        group.last_rx_us = start_us
        if sync.info is not None:
            group.sca = sync.info.sca
        while group.next_us + info.duration_us < self.link.now_us:
            group.next_us += info.interval_us
            group.counter += 1
        delay = info.sync_delay_us + (
            info.interval_us + info.sdu_interval_us if info.framed else 0
        )
        for handle in group.stream_handles:
            self.iso.add(handle, False, parameters, synchronization_delay_us=delay)
        self._ciphers(group, config.broadcast_code)

    def terminate(self, handle: int, reason: int) -> int:
        group = self.groups.get(handle)
        if group is None:
            return UNKNOWN_ADV
        if not group.transmit:
            return DISALLOWED
        if reason not in set(ErrorCode):
            return INVALID
        if not group.reported:
            group.cancelled = True
            return SUCCESS
        if group.control is not None or group.pending_control is not None:
            return DISALLOWED
        start = group.counter + int(group.running)
        instant = start + 6
        group.pending_control = BigControl(
            bytes([1, reason]) + (instant & 0xFFFF).to_bytes(2, 'little'),
            instant,
            start=start,
            reason=reason,
        )
        return SUCCESS

    def terminate_sync(self, handle: int) -> int:
        group = self.groups.get(handle)
        if group is None:
            return UNKNOWN_ADV
        if group.transmit:
            return DISALLOWED
        pending = not group.reported
        snapshot = group.snapshot()
        self._release(group)
        if pending:
            self.link.listener.on_big_synchronized(
                snapshot, ErrorCode.OPERATION_CANCELLED_BY_HOST_ERROR
            )
        return SUCCESS

    def update_channel_map(self, channel_map: int) -> None:
        for group in self.groups.values():
            info = group.info
            if (
                not group.transmit
                or info is None
                or info.channel_map == channel_map
                or group.control is not None
                or group.pending_control is not None
            ):
                continue
            start = group.counter + int(group.running)
            instant = start + 6
            group.pending_control = BigControl(
                b'\0'
                + channel_map.to_bytes(5, 'little')
                + (instant & 0xFFFF).to_bytes(2, 'little'),
                instant,
                start=start,
                channel_map=channel_map,
            )

    def owns(self, group: Big) -> bool:
        return self.groups.get(group.handle) is group

    def transmit_payload(
        self, group: Big, associated: int, number: int, index: int
    ) -> IsoPdu | None:
        """Segment each associated burst once, including future pre-transmissions."""
        info = group.info
        assert info is not None
        if associated not in group.payloads:
            first = max(group.payloads, default=group.counter - 1) + 1
            for event in range(first, associated + 1):
                anchor = group.next_us + (event - group.counter) * info.interval_us
                bursts = {}
                for bis_number, handle in zip(group.numbers, group.stream_handles):
                    stream = self.iso.get_stream(handle, True)
                    bursts[bis_number] = stream.burst(anchor, event).pdus
                    if not self.owns(group):
                        return None
                group.payloads[event] = bursts
        return group.payloads[associated][number][index]

    def listening(self, group: Big) -> None:
        if not group.reported and group.last_acquisition_event is None:
            group.last_acquisition_event = group.counter + BIG_SYNC_EVENTS - 1

    def received(self, group: Big, start_us: int, offset_us: int) -> None:
        """Commit a valid data PDU, including the first synchronization."""
        info, config = group.info, group.sync_config
        assert info is not None and config is not None
        group.last_rx_us = start_us
        group.next_us = start_us - offset_us
        group.widening_us = 0
        group.deadline_us = start_us + max(
            config.timeout * 10000, BIG_SYNC_EVENTS * info.interval_us
        )
        if not group.reported:
            group.reported = True
            self.link.listener.on_big_synchronized(group.snapshot(), SUCCESS)

    def completed(self, group: Big, received: dict[int, list[IsoPdu | None]]) -> None:
        """Advance one live BIG event, delivering each selected BIS once."""
        info = group.info
        assert info is not None
        if not self.owns(group):
            return
        if group.transmit:
            group.payloads.pop(group.counter, None)
            for handle in group.stream_handles:
                self.iso.get_stream(handle, True).expire_payloads(group.counter)
        elif group.reported:
            for number, handle in zip(group.numbers, group.stream_handles):
                self.iso.get_stream(handle, False).received(
                    tuple(received[number]),
                    group.next_us,
                    group.counter,
                    now_us=self.link.now_us,
                )
                if not self.owns(group):
                    return
        elif (
            group.last_acquisition_event is not None
            and group.counter >= group.last_acquisition_event
        ):
            self._close(group, ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR)
            return
        group.counter += 1
        group.next_us += info.interval_us

    def mic_failure(self, group: Big) -> None:
        self._close(group, ErrorCode.CONNECTION_TERMINATED_DUE_TO_MIC_FAILURE_ERROR)

    def control_received(self, group: Big, payload: bytes, cssn: int) -> None:
        if not payload or cssn == group.received_cssn:
            return
        opcode = payload[0]
        if (
            opcode == 0
            and len(payload) == 8
            and valid_channel_map(int.from_bytes(payload[1:6], 'little'))
        ) or (opcode == 1 and len(payload) == 4):
            distance = (int.from_bytes(payload[-2:], 'little') - group.counter) & 0xFFFF
            if distance >= 32767:
                return
            group.control = BigControl(
                payload,
                group.counter + distance,
                channel_map=int.from_bytes(payload[1:6], 'little')
                if opcode == 0
                else None,
                reason=payload[1] if opcode == 1 else None,
            )
            group.received_cssn = cssn

    def _release(self, group: Big) -> None:
        self.groups.pop(group.handle, None)
        if group.sync is not None:
            group.sync.big_info = None
        for handle in group.stream_handles:
            if self.iso.contains(handle):
                self.iso.release(handle)
        if group.transmit:
            item = self.link.advertisers.get(group.advertising_handle)
            if item is not None:
                item.big_info = b''

    def _close(self, group: Big, reason: int) -> None:
        snapshot = group.snapshot()
        self._release(group)
        if group.transmit:
            self.link.listener.on_big_terminated(
                group.handle, ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR
            )
        elif group.reported:
            self.link.listener.on_big_lost(group.handle, reason)
        else:
            self.link.listener.on_big_synchronized(snapshot, reason)

    def prepare(self) -> list[PendingOperation]:
        self.expire()
        # A second classification may arrive while the first control procedure
        # is pending. Apply the latest Host map once that procedure completes.
        self.update_channel_map(self.link.roles.host_channel_map)
        operations = []
        for group in tuple(self.groups.values()):
            if not group.transmit and group.info is None:
                self._acquire(group)
            if not self.owns(group):
                continue
            if group.transmit and not group.reported:
                operations.append(
                    PendingOperation(
                        self.link.now_us + 1,
                        RadioPriority.EXPIRY,
                        group,
                        partial(self._created, group),
                    )
                )
            if group.deadline_us:
                operations.append(
                    PendingOperation(
                        group.deadline_us,
                        RadioPriority.EXPIRY,
                        group,
                        partial(self._expired, group),
                    )
                )
            info = group.info
            if info is None:
                continue
            if group.control is not None and group.counter >= group.control.instant:
                operations.append(
                    PendingOperation(
                        group.next_us
                        if group.control.reason is not None
                        else group.next_us - RADIO_SWITCH_TIME_US,
                        RadioPriority.EXPIRY,
                        group,
                        partial(self._instant, group),
                    )
                )
                continue
            if (
                group.associated_event(info.nse - 1) * info.bn + info.bn
                > BIG_COUNTER_MASK
            ):
                self._close(group, ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR)
                continue
            operations.append(
                PendingOperation(
                    group.reservation().start_us,
                    RadioPriority.ISOCHRONOUS,
                    group,
                    partial(self._run, group),
                )
            )
        return operations

    async def _expired(self, group: Big) -> None:
        await self.link.radio.advance_to_us(group.deadline_us)

    async def _created(self, group: Big) -> None:
        await self.link.radio.advance_to_us(self.link.now_us + 1)
        if not self.owns(group):
            return
        snapshot = group.snapshot()
        if group.cancelled:
            self._release(group)
            self.link.listener.on_big_created(
                snapshot, ErrorCode.OPERATION_CANCELLED_BY_HOST_ERROR
            )
        else:
            group.reported = True
            self.link.listener.on_big_created(snapshot, SUCCESS)

    async def _instant(self, group: Big) -> None:
        control, info = group.control, group.info
        assert control is not None and info is not None
        await self.link.radio.advance_to_us(
            group.next_us
            if control.reason is not None
            else group.next_us - RADIO_SWITCH_TIME_US
        )
        if not self.owns(group):
            return
        if control.reason is not None:
            self._close(group, control.reason)
        else:
            assert control.channel_map is not None
            group.info = replace(info, channel_map=control.channel_map)
            group.control = None

    def expire(self) -> None:
        for group in tuple(self.groups.values()):
            if group.deadline_us and self.link.now_us >= group.deadline_us:
                self._close(
                    group,
                    ErrorCode.CONNECTION_TIMEOUT_ERROR
                    if group.reported
                    else ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR,
                )

    async def _run(self, group: Big) -> None:
        if (
            group.pending_control is not None
            and group.counter >= group.pending_control.start
        ):
            group.control = group.pending_control
            group.pending_control = None
            group.cssn = (group.cssn + 1) & 7
        group.running = True
        try:
            await run_big_event(self.link, self, group)
        finally:
            group.running = False

    def reservations(self, exclude: object = None) -> list[RadioReservation]:
        return [
            group.reservation()
            for group in self.groups.values()
            if group is not exclude and group.info is not None
        ]
