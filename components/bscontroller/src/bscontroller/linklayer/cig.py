# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connected isochronous group and stream lifetime (Core Vol 6, Part B, 5.1.15)."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from functools import partial
from math import gcd
from typing import TYPE_CHECKING

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import (
    LL_EVENT_COUNTER_MASK,
    LL_RESPONSE_TIMEOUT_US,
)
from bscontroller.linklayer import (
    ControlOpcode as Opcode,
)
from bscontroller.linklayer.api import CigConfig, CisEstablished
from bscontroller.linklayer.bitstream import valid_access_address
from bscontroller.linklayer.cis import CisEvent, CisEventParameters
from bscontroller.linklayer.cis_parameters import (
    CIS_IND_FIELDS,
    CIS_REQ_FIELDS,
    CIS_WINDOW_FIELDS,
    ISO_INTERVAL_UNIT_US,
    MIN_CIS_OFFSET_US,
    CisIndication,
    CisRequest,
    CisWindow,
    UnsupportedCisBurstOrFlush,
    UnsupportedCisParameters,
    event_anchor,
    plan_cig,
    valid_cis_supervision_timeout,
)
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.frame_space import REQUEST as FRAME_SPACE_REQUEST
from bscontroller.linklayer.resource import PendingOperation
from bscontroller.linklayer.scheduler import (
    RadioPriority,
    RadioReservation,
    first_radio_gap,
)
from bscontroller.linklayer.state import Role

if TYPE_CHECKING:
    from collections.abc import Iterator

    from bscontroller.linklayer.connection import Connection
    from bscontroller.linklayer.iso import IsoDataPaths
    from bscontroller.linklayer.link import RadioLink

SUCCESS = ErrorCode.SUCCESS
UNKNOWN = ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
DISALLOWED = ErrorCode.COMMAND_DISALLOWED_ERROR
INVALID = ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
UNSUPPORTED = ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR
REMOTE_UNSUPPORTED = ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
NO_RESOURCES = ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR
DIFFERENT_COLLISION = ErrorCode.DIFFERENT_TRANSACTION_COLLISION_ERROR
CANCELLED = ErrorCode.OPERATION_CANCELLED_BY_HOST_ERROR
LOCAL_TERMINATED = ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR

CIS_SETUP_OPCODES = frozenset((Opcode.LL_CIS_REQ, Opcode.LL_CIS_RSP, Opcode.LL_CIS_IND))
MAX_CIS_PER_CIG = 31


class Setup(Enum):
    CONFIGURED = auto()
    QUEUED = auto()
    REQUEST = auto()
    HOST = auto()
    RESPONSE = auto()
    INDICATION = auto()
    ACTIVE = auto()
    TERMINATING = auto()


@dataclass
class Cis:
    handle: int
    cig_id: int
    cis_id: int
    parameters: CisEventParameters
    connection: Connection | None = None
    central: bool = True
    stage: Setup = Setup.CONFIGURED
    window: CisWindow | None = None
    event: CisEvent | None = None
    deadline_us: int = 0
    reported: bool = False
    cancellation: bool = False
    failure: int | None = None
    indication: CisIndication | None = None


@dataclass
class Cig:
    config: CigConfig
    streams: dict[int, Cis] = field(default_factory=dict)
    anchor_us: int | None = None
    started: bool = False


class CigManager:
    def __init__(self, link: RadioLink, iso: IsoDataPaths):
        self.link = link
        self.iso = iso
        self.groups: dict[int, Cig] = {}
        self.streams: dict[int, Cis] = {}
        self.connection_accept_timeout = 0x1F40

    def connection_handles(self) -> set[int]:
        return set(self.streams)

    def contains(self, handle: int) -> bool:
        return handle in self.streams

    def has_created_cis(self, connection: Connection) -> bool:
        return any(
            stream.connection is connection and stream.event is not None
            for stream in self.streams.values()
        )

    def fits_supervision_timeout(self, connection: Connection, timeout: int) -> bool:
        """Retain the ACL timeout bound once a stream has reserved its timing."""
        return all(
            valid_cis_supervision_timeout(stream.parameters.interval_us, timeout)
            for stream in self.streams.values()
            if stream.connection is connection and stream.window is not None
        )

    def access_addresses(self) -> set[int]:
        return {
            stream.indication.access_address
            for stream in self.streams.values()
            if stream.indication is not None
        }

    def reset(self) -> None:
        self.connection_accept_timeout = 0x1F40
        self.streams.clear()
        self.groups.clear()

    def events(self) -> Iterator[CisEvent]:
        for stream in self.streams.values():
            if stream.event is not None and stream.stage == Setup.ACTIVE:
                yield stream.event

    def event_for_handle(self, handle: int) -> CisEvent | None:
        stream = self.streams.get(handle)
        return stream.event if stream is not None else None

    def waiting_for_establishment(self, handle: int) -> bool:
        stream = self.streams.get(handle)
        return stream is not None and not stream.reported

    def set_accept_timeout(self, timeout: int) -> int:
        # Core Vol 4, Part E, 6.7: units of 625 microseconds.
        if not 1 <= timeout <= 0xB540:
            return INVALID
        self.connection_accept_timeout = timeout
        return SUCCESS

    def iso_link_quality(self, handle: int) -> tuple[int, tuple[int, ...]]:
        event = self.event_for_handle(handle)
        if event is None or not event.established:
            return UNKNOWN, (0,) * 7
        return SUCCESS, event.link_quality()

    def set_cig_parameters(self, config: CigConfig) -> tuple[int, tuple[int, ...]]:
        group = self.groups.get(config.cig_id)
        if group is not None and group.started:
            return DISALLOWED, ()
        selected = tuple(item.cis_id for item in config.cis_parameters)
        if len(set(selected)) != len(selected):
            return INVALID, ()
        if group is not None:
            old = {item.cis_id: item for item in group.config.cis_parameters}
            old.update((item.cis_id, item) for item in config.cis_parameters)
            config = replace(config, cis_parameters=tuple(old.values()))
        if len(config.cis_parameters) > MAX_CIS_PER_CIG:
            return ErrorCode.CONNECTION_LIMIT_EXCEEDED_ERROR, ()
        try:
            parameters = plan_cig(config)
        except UnsupportedCisParameters:
            return UNSUPPORTED, ()
        except ValueError:
            return INVALID, ()
        if group is None:
            group = Cig(config)
        for cis_id, stream in group.streams.items():
            params = parameters[cis_id]
            if any(
                self._configured_path(stream.handle, transmit)
                and not (params.c_to_p if transmit else params.p_to_c).bn
                for transmit in (True, False)
            ):
                return DISALLOWED, ()
        new_ids = parameters.keys() - group.streams.keys()
        free = self.link.roles.available_connection_handles()
        handles = [next(free, None) for _ in new_ids]
        if None in handles:
            return NO_RESOURCES, ()
        self.groups[config.cig_id] = group
        group.config = config
        handle_iterator = iter(handles)
        for cis_id, event_params in parameters.items():
            if cis_id not in group.streams:
                handle = next(handle_iterator)
                stream = Cis(handle, config.cig_id, cis_id, event_params)
                self.streams[handle] = group.streams[cis_id] = stream
            stream = group.streams[cis_id]
            stream.parameters = event_params
            self._configure_paths(stream)
        return SUCCESS, tuple(group.streams[cis_id].handle for cis_id in selected)

    def remove_cig(self, cig_id: int) -> int:
        group = self.groups.get(cig_id)
        if group is None:
            return UNKNOWN
        if any(item.stage != Setup.CONFIGURED for item in group.streams.values()):
            return DISALLOWED
        for item in group.streams.values():
            if self.iso.contains(item.handle):
                self.iso.release(item.handle)
            del self.streams[item.handle]
        del self.groups[cig_id]
        return SUCCESS

    def create_cis(self, pairs: tuple[tuple[int, int], ...]) -> int:
        if not self.link.features & LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM:
            return DISALLOWED
        if any(
            item.central and not item.reported and item.stage != Setup.CONFIGURED
            for item in self.streams.values()
        ):
            return DISALLOWED
        if not 1 <= len(pairs) <= 31 or len({handle for handle, _ in pairs}) != len(
            pairs
        ):
            return INVALID
        assignments = []
        for handle, acl_handle in pairs:
            stream = self.streams.get(handle)
            connection = self.link.roles.connections.contexts.get(acl_handle)
            if stream is None or not stream.central or connection is None:
                return UNKNOWN
            if stream.stage != Setup.CONFIGURED:
                return ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR
            if connection.role != Role.CENTRAL:
                return DISALLOWED
            assignments.append((stream, connection))
        for stream, connection in assignments:
            stream.connection = connection
            stream.stage = Setup.QUEUED
            self.groups[stream.cig_id].started = True
        return SUCCESS

    def accept_cis(self, handle: int) -> int:
        stream = self.streams.get(handle)
        if stream is None:
            return UNKNOWN
        if stream.stage != Setup.HOST:
            return DISALLOWED
        self._configure_paths(stream)
        self._respond(stream)
        return SUCCESS

    def reject_cis(self, handle: int, reason: int) -> int:
        stream = self.streams.get(handle)
        if stream is None:
            return UNKNOWN
        if stream.stage != Setup.HOST:
            return DISALLOWED
        stream.connection.controls.reject(Opcode.LL_CIS_REQ, reason)
        self._release(stream)
        return SUCCESS

    def disconnect(self, handle: int, reason: int) -> int:
        stream = self.streams.get(handle)
        if stream is None:
            return UNKNOWN
        if (
            stream.stage in (Setup.CONFIGURED, Setup.TERMINATING)
            or (not stream.central and not stream.reported)
            or stream.cancellation
        ):
            return DISALLOWED
        connection = stream.connection
        if not stream.reported:
            stream.cancellation = True
        if stream.stage == Setup.ACTIVE or (
            stream.stage == Setup.INDICATION
            and not connection.event.cancel_queued_control(stream.indication.encode())
        ):
            connection.enqueue_control(
                bytes(
                    [Opcode.LL_CIS_TERMINATE_IND, stream.cig_id, stream.cis_id, reason]
                ),
                priority=True,
            )
            stream.stage = Setup.TERMINATING
            stream.deadline_us = self.link.now_us + LL_RESPONSE_TIMEOUT_US
        elif stream.stage == Setup.INDICATION:
            connection.controls.reject(Opcode.LL_CIS_RSP, CANCELLED)
            stream.stage = Setup.QUEUED
        return SUCCESS

    def close_acl(self, connection: Connection, reason: int) -> None:
        for stream in tuple(self.streams.values()):
            if stream.connection is connection:
                self._finish(stream, reason)

    def _exchange(self, connection: Connection) -> Cis | None:
        return next(
            (
                stream
                for stream in self.streams.values()
                if stream.connection is connection
                and (
                    stream.stage
                    in (Setup.REQUEST, Setup.HOST, Setup.RESPONSE, Setup.INDICATION)
                    or (
                        stream.stage == Setup.ACTIVE
                        and stream.event.awaiting_indication_ack
                    )
                )
            ),
            None,
        )

    def busy(self, connection: Connection) -> bool:
        return self._exchange(connection) is not None

    def start_pending(self, connection: Connection) -> None:
        if connection.llcp.local_procedure_busy or connection.llcp.deferred:
            return
        for stream in self.streams.values():
            if (
                stream.connection is not connection
                or stream.stage != Setup.QUEUED
                or stream.cancellation
            ):
                continue
            # Finalize the anchor before any other CIG negotiates a gap or
            # another stream in this group uses the common timing.
            if any(
                item.central and item.stage in (Setup.REQUEST, Setup.INDICATION)
                for item in self.streams.values()
            ):
                continue
            if connection.peer_features is None:
                connection.controls.features.discover(
                    lambda status: self._finish(stream, status) if status else None
                )
            elif (
                not connection.peer_features
                & LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_PERIPHERAL
            ):
                self._finish(stream, REMOTE_UNSUPPORTED)
            else:
                try:
                    self._plan_for_connection(stream)
                    stream.window = self._new_window(stream)
                except ValueError:
                    self._finish(
                        stream, ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR
                    )
                    return
                stream.stage = Setup.REQUEST
                connection.enqueue_control(
                    CisRequest(
                        stream.cig_id, stream.cis_id, stream.parameters, stream.window
                    ).encode()
                )
            return

    @staticmethod
    def _spaces(stream: Cis) -> tuple[int, int]:
        params = stream.parameters
        return stream.connection.controls.frame_space.cis_spaces(
            params.c_to_p.phy, params.p_to_c.phy
        )

    def _plan_for_connection(self, stream: Cis) -> None:
        group = self.groups[stream.cig_id]
        if group.anchor_us is not None:
            ifs, mss = self._spaces(stream)
            params = stream.parameters
            params = replace(params, spacing_us=ifs, minimum_subevent_space_us=mss)
            if params.nse > 1 and params.subinterval_us < params.minimum_subevent_us:
                raise ValueError('negotiated frame spaces exceed the reserved CIS slot')
            stream.parameters = params
            return
        spaces = {
            item.cis_id: self._spaces(item)
            for item in group.streams.values()
            if item.connection is not None
        }
        for cis_id, params in plan_cig(group.config, spaces=spaces).items():
            group.streams[cis_id].parameters = params

    def _new_window(self, stream: Cis) -> CisWindow:
        connection, params = stream.connection, stream.parameters
        self._validate_supervision(connection, params.interval_us)
        group = self.groups[stream.cig_id]
        interval = connection.parameters.interval_us
        minimum = max(MIN_CIS_OFFSET_US, connection.first_exchange_us(max_rx=False))
        maximum = interval - params.cis_sync_delay_us - params.minimum_subevent_space_us
        minimum = max(minimum, params.cig_sync_delay_us - params.cis_sync_delay_us)
        if minimum > maximum:
            raise ValueError('CIS event does not fit the ACL interval')
        offset = params.cig_sync_delay_us - params.cis_sync_delay_us
        if group.anchor_us is None:
            counter = (connection.event_counter + 6) & LL_EVENT_COUNTER_MASK
            minimum = max(minimum, min(maximum, 2000 + offset))
            reservations = self.reservations()
            if reservations:
                anchor = event_anchor(connection, counter) - offset
                minimum, maximum = self._available_cig_offsets(
                    params, reservations, anchor, minimum, maximum
                )
            return CisWindow(minimum, maximum, counter)
        for ahead in range(6, 100):
            counter = (connection.event_counter + ahead) & LL_EVENT_COUNTER_MASK
            anchor = event_anchor(connection, counter)
            selected = (group.anchor_us + offset - anchor) % params.interval_us
            if selected < minimum:
                selected += (
                    (minimum - selected + params.interval_us - 1) // params.interval_us
                ) * params.interval_us
            if selected <= maximum:
                return CisWindow(selected, selected, counter)
        raise ValueError('CIS event does not align with the CIG')

    @staticmethod
    def _validate_supervision(connection: Connection, interval_us: int) -> None:
        pending = connection.controls.pending
        timeout = connection.parameters.timeout
        if pending is not None and pending.supervision_timeout is not None:
            # The new ACL timeout may already be in effect at the first CIS event.
            timeout = min(timeout, pending.supervision_timeout)
        if not valid_cis_supervision_timeout(interval_us, timeout):
            raise ValueError('CIS interval exceeds the ACL supervision budget')

    @staticmethod
    def _available_cig_offsets(
        params: CisEventParameters,
        reservations: list[RadioReservation],
        anchor_us: int,
        minimum: int,
        maximum: int,
    ) -> tuple[int, int]:
        # A gap must recur for every pair of CIG events. Project the windows
        # onto their shared period and offer the largest clear window within
        # one ISO interval, so the peer can choose a phase on its radio too.
        duration = params.cig_sync_delay_us
        recurring = []
        for reservation in reservations:
            period = gcd(params.interval_us, reservation.interval_us)
            if duration + reservation.duration_us > period:
                raise ValueError('no recurring gap between CIGs')
            start = (reservation.start_us - anchor_us) % period - period
            recurring.append(replace(reservation, start_us=start, interval_us=period))
        maximum = min(maximum, minimum + params.interval_us - 1)
        best = None
        while (
            start := first_radio_gap(recurring, minimum, duration, maximum)
        ) is not None:
            next_start = min(item.window_after(start)[0] for item in recurring)
            end = min(maximum, next_start - duration)
            if best is None or end - start > best[1] - best[0]:
                best = (start, end)
            minimum = next_start
        if best is None:
            raise ValueError('no recurring gap between CIGs')
        return best

    def _future_window(self, stream: Cis, window: CisWindow) -> CisWindow:
        connection, period = stream.connection, stream.parameters.interval_us
        old_start = window.anchor_us(connection)
        if old_start >= connection.next_anchor_us:
            return window
        for ahead in range(2, 100):
            counter = (connection.event_counter + ahead) & LL_EVENT_COUNTER_MASK
            anchor = event_anchor(connection, counter)
            offset = (old_start - anchor) % period
            if offset < MIN_CIS_OFFSET_US:
                offset += ((MIN_CIS_OFFSET_US - offset + period - 1) // period) * period
            if offset + window.width_us < connection.parameters.interval_us:
                return CisWindow(offset, offset + window.width_us, counter)
        raise ValueError('CIS window does not fit a future ACL interval')

    def _respond(self, stream: Cis) -> None:
        connection = stream.connection
        try:
            stream.window = self._future_window(stream, stream.window)
        except ValueError:
            connection.controls.reject(
                Opcode.LL_CIS_REQ, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR
            )
            stream.failure = ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR
            return
        stream.stage = Setup.RESPONSE
        stream.deadline_us = 0
        connection.enqueue_control(bytes([Opcode.LL_CIS_RSP]) + stream.window.encode())

    def _request(self, connection: Connection, payload: bytes) -> None:
        if not self.link.features & LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM:
            connection.controls.reject(Opcode.LL_CIS_REQ, REMOTE_UNSUPPORTED)
            return
        try:
            request = CisRequest.decode(payload)
            request.window.validate(connection)
            params = request.parameters
            self._validate_supervision(connection, params.interval_us)
            ifs, mss = connection.controls.frame_space.cis_spaces(
                params.c_to_p.phy, params.p_to_c.phy
            )
            params = replace(params, spacing_us=ifs, minimum_subevent_space_us=mss)
        except UnsupportedCisBurstOrFlush:
            connection.controls.reject(
                Opcode.LL_CIS_REQ, ErrorCode.PARAMETER_OUT_OF_MANDATORY_RANGE_ERROR
            )
            return
        except UnsupportedCisParameters:
            connection.controls.reject(
                Opcode.LL_CIS_REQ, ErrorCode.UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR
            )
            return
        except (ValueError, struct.error):
            connection.controls.reject(
                Opcode.LL_CIS_REQ, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR
            )
            return
        if self.busy(connection):
            connection.controls.reject(
                Opcode.LL_CIS_REQ,
                ErrorCode.LMP_ERROR_TRANSACTION_COLLISION_OR_LL_PROCEDURE_COLLISION_ERROR,
            )
            return
        if self._stream_for_ids(connection, request.cig_id, request.cis_id) is not None:
            connection.controls.reject(
                Opcode.LL_CIS_REQ, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR
            )
            return
        handle = next(self.link.roles.available_connection_handles(), None)
        if handle is None:
            connection.controls.reject(
                Opcode.LL_CIS_REQ, ErrorCode.CONNECTION_LIMIT_EXCEEDED_ERROR
            )
            return
        stream = Cis(
            handle,
            request.cig_id,
            request.cis_id,
            params,
            connection,
            False,
            Setup.HOST,
            request.window,
            deadline_us=self.link.now_us + 625 * self.connection_accept_timeout,
        )
        self.streams[handle] = stream
        frame_space = connection.controls.frame_space
        if frame_space.pending and frame_space.context.transmitted:
            frame_space.collision = True
        if not self.link.listener.on_cis_request(
            connection.handle, handle, request.cig_id, request.cis_id
        ):
            self.reject_cis(
                handle, ErrorCode.CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR
            )

    def _response(self, stream: Cis, payload: bytes) -> None:
        connection, params = stream.connection, stream.parameters
        if stream.cancellation:
            connection.controls.reject(Opcode.LL_CIS_RSP, CANCELLED)
            self._cancelled(stream)
            return
        try:
            window = CisWindow.decode(payload[1:])
            window.validate(connection)
            shift = (
                window.anchor_us(connection) - stream.window.anchor_us(connection)
            ) % params.interval_us
            if shift + window.width_us > stream.window.width_us:
                raise ValueError('CIS response window lies outside request')
            window = self._future_window(stream, window)
        except ValueError:
            connection.controls.reject(
                Opcode.LL_CIS_RSP, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR
            )
            self._finish(stream, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR)
            return
        stream.indication = CisIndication(
            self.link.new_access_address(minimum_distance=2),
            window.offset_min_us,
            params.cig_sync_delay_us,
            params.cis_sync_delay_us,
            window.conn_event_count,
        )
        stream.stage = Setup.INDICATION
        stream.deadline_us = event_anchor(connection, window.conn_event_count)
        connection.enqueue_control(stream.indication.encode())

    def _receive_indication(self, stream: Cis, payload: bytes) -> None:
        connection = stream.connection
        indication = CisIndication.decode(payload)
        try:
            anchor = (
                event_anchor(connection, indication.conn_event_count)
                + indication.offset_us
            )
            offset = (
                anchor - stream.window.anchor_us(connection)
            ) % stream.parameters.interval_us
            if (
                not valid_access_address(indication.access_address)
                or indication.access_address in self.link.used_access_addresses()
                or offset > stream.window.width_us
                or anchor < self.link.now_us
                or not 0
                < indication.cis_sync_delay_us
                <= indication.cig_sync_delay_us
                < stream.parameters.interval_us
            ):
                raise ValueError('invalid CIS indication')
            stream.indication = indication
        except ValueError:
            self._finish(stream, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR)
            return
        self._activate(stream)

    def control(self, connection: Connection, payload: bytes) -> bool:
        opcode = payload[0]
        if (
            opcode == Opcode.LL_FRAME_SPACE_REQ
            and len(payload) == 1 + FRAME_SPACE_REQUEST.size
            and connection.role == Role.CENTRAL
            and self._frame_space_collides(connection)
        ):
            return True
        if (
            opcode == Opcode.LL_CIS_REQ
            and len(payload) == 1 + CIS_REQ_FIELDS.size + CIS_WINDOW_FIELDS.size
        ):
            self._request(connection, payload)
            return True
        if opcode == Opcode.LL_CIS_TERMINATE_IND and len(payload) == 4:
            stream = self._stream_for_ids(connection, payload[1], payload[2])
            if stream is not None and stream.stage in (
                Setup.INDICATION,
                Setup.ACTIVE,
                Setup.TERMINATING,
            ):
                # Stop CIS radio activity on receipt, then notify the Host
                # when the ACL acknowledges it (Core Vol 6, Part B, 5.1.16).
                stream.stage = Setup.TERMINATING
                stream.deadline_us = 0
                connection.event.after_acknowledgment(
                    partial(self._finish, stream, payload[3])
                )
            return True
        stream = self._exchange(connection)
        if stream is None:
            return False
        if (
            opcode == Opcode.LL_CIS_RSP
            and stream.stage == Setup.REQUEST
            and len(payload) == 1 + CIS_WINDOW_FIELDS.size
        ):
            self._response(stream, payload)
            return True
        if (
            opcode == Opcode.LL_CIS_IND
            and stream.stage == Setup.RESPONSE
            and len(payload) == 1 + CIS_IND_FIELDS.size
        ):
            self._receive_indication(stream, payload)
            return True
        if (
            opcode == Opcode.LL_REJECT_EXT_IND
            and len(payload) == 3
            and payload[1] in CIS_SETUP_OPCODES
        ):
            self._finish(stream, payload[2])
            return True
        if (
            opcode == Opcode.LL_UNKNOWN_RSP
            and len(payload) == 2
            and payload[1] in CIS_SETUP_OPCODES
        ):
            self._finish(stream, REMOTE_UNSUPPORTED)
            return True
        return False

    def _frame_space_collides(self, connection: Connection) -> bool:
        """Central wins a collision with a Peripheral Frame Space Update (5.3)."""
        for stream in self.streams.values():
            if stream.connection is not connection or stream.reported:
                continue
            if stream.stage == Setup.REQUEST and stream.deadline_us:
                connection.controls.reject(
                    Opcode.LL_FRAME_SPACE_REQ, DIFFERENT_COLLISION, priority=True
                )
                return True
            if stream.stage in (Setup.INDICATION, Setup.ACTIVE):
                connection.complete(DIFFERENT_COLLISION)
                return True
        return False

    def sent(self, connection: Connection, payload: bytes) -> None:
        opcode = payload[0]
        if (
            opcode not in CIS_SETUP_OPCODES
            or (stream := self._exchange(connection)) is None
        ):
            return
        if opcode == Opcode.LL_CIS_IND:
            if stream.stage == Setup.INDICATION:
                # The Central creates the CIS on transmission (Core 6.B.5.1.15).
                self._activate(stream)
        else:
            stream.deadline_us = self.link.now_us + LL_RESPONSE_TIMEOUT_US

    def acknowledged(self, connection: Connection, payload: bytes) -> None:
        if payload[0] == Opcode.LL_CIS_IND:
            stream = self._exchange(connection)
            if stream is not None and stream.stage == Setup.ACTIVE:
                stream.event.start_supervision()
        elif payload[0] == Opcode.LL_CIS_TERMINATE_IND:
            stream = self._stream_for_ids(connection, payload[1], payload[2])
            if stream is not None:
                self._locally_terminated(stream)

    def _stream_for_ids(
        self, connection: Connection, cig_id: int, cis_id: int
    ) -> Cis | None:
        return next(
            (
                stream
                for stream in self.streams.values()
                if stream.connection is connection
                and stream.cig_id == cig_id
                and stream.cis_id == cis_id
            ),
            None,
        )

    def _activate(self, stream: Cis) -> None:
        connection, indication = stream.connection, stream.indication
        anchor = (
            event_anchor(connection, indication.conn_event_count) + indication.offset_us
        )
        parameters = replace(
            stream.parameters,
            cis_sync_delay_us=indication.cis_sync_delay_us,
            cig_sync_delay_us=indication.cig_sync_delay_us,
        )
        if stream.central:
            group = self.groups[stream.cig_id]
            group.anchor_us = anchor - (
                indication.cig_sync_delay_us - indication.cis_sync_delay_us
            )
        stream.parameters = parameters
        self._configure_paths(stream)
        stream.event = CisEvent(
            connection,
            stream.handle,
            indication.access_address,
            connection.parameters.crc_init,
            anchor,
            parameters,
            iso=self.iso,
        )
        stream.stage = Setup.ACTIVE
        stream.deadline_us = 0

    def _snapshot(self, stream: Cis) -> CisEstablished:
        params, tx, rx = (
            stream.parameters,
            stream.parameters.c_to_p,
            stream.parameters.p_to_c,
        )
        return CisEstablished(
            connection_handle=stream.handle,
            cig_sync_delay=params.cig_sync_delay_us,
            cis_sync_delay=params.cis_sync_delay_us,
            transport_latency_c_to_p=params.transport_latency_us(
                central_to_peripheral=True
            ),
            transport_latency_p_to_c=params.transport_latency_us(
                central_to_peripheral=False
            ),
            phy_c_to_p=int(tx.phy),
            phy_p_to_c=int(rx.phy),
            nse=params.nse,
            bn_c_to_p=tx.bn,
            bn_p_to_c=rx.bn,
            ft_c_to_p=tx.ft,
            ft_p_to_c=rx.ft,
            max_pdu_c_to_p=tx.max_pdu,
            max_pdu_p_to_c=rx.max_pdu,
            iso_interval=params.interval_us // ISO_INTERVAL_UNIT_US,
            sub_interval=params.subinterval_us,
            max_sdu_c_to_p=tx.max_sdu,
            max_sdu_p_to_c=rx.max_sdu,
            sdu_interval_c_to_p=tx.sdu_interval_us,
            sdu_interval_p_to_c=rx.sdu_interval_us,
            framing=int(params.framed),
        )

    def _configured_path(self, handle: int, transmit: bool) -> bool:
        path = self.iso.streams.get(handle, {}).get(transmit)
        return path is not None and path.configured

    def _configure_paths(self, stream: Cis) -> None:
        """Refresh ISOAL timing while preserving the Central's configured HCI paths."""
        params = stream.parameters
        previous = self.iso.streams.get(stream.handle, {})
        if previous:
            self.iso.release(stream.handle)
        for forward, direction in ((True, params.c_to_p), (False, params.p_to_c)):
            if not direction.bn:
                continue
            transmit = stream.central == forward
            self.iso.add(
                stream.handle,
                transmit,
                direction.isoal(framed=params.framed, interval_us=params.interval_us),
                synchronization_delay_us=params.synchronization_delay_us(
                    central_to_peripheral=forward,
                ),
            )
            if (old := previous.get(transmit)) is not None:
                path = self.iso.get_stream(stream.handle, transmit)
                path.configured = old.configured
                path.controller_delay_us = old.controller_delay_us

    def _release(self, stream: Cis) -> None:
        if not stream.central:
            if self.iso.contains(stream.handle):
                self.iso.release(stream.handle)
            del self.streams[stream.handle]
            return
        self._configure_paths(stream)
        stream.stage = Setup.CONFIGURED
        stream.connection = stream.event = stream.window = stream.indication = None
        stream.reported = False
        stream.cancellation = False
        stream.deadline_us = 0
        stream.failure = None
        group = self.groups[stream.cig_id]
        if not any(item.event is not None for item in group.streams.values()):
            group.anchor_us = None

    def _finish(self, stream: Cis, reason: int) -> None:
        if self.streams.get(stream.handle) is not stream or stream.connection is None:
            return
        if stream.cancellation:
            self._cancelled(stream)
            return
        handle, reported = stream.handle, stream.reported
        self._release(stream)
        if reported:
            self.link.listener.on_cis_disconnected(handle, reason)
        else:
            self.link.listener.on_cis_established(CisEstablished(handle), reason)

    def _cancelled(self, stream: Cis) -> None:
        self._release(stream)
        self.link.listener.on_cis_established(CisEstablished(stream.handle), CANCELLED)
        self.link.listener.on_cis_disconnected(stream.handle, LOCAL_TERMINATED)

    def _locally_terminated(self, stream: Cis) -> None:
        self._finish(stream, LOCAL_TERMINATED)

    def _expire(self, stream: Cis) -> None:
        if stream.stage == Setup.HOST:
            stream.connection.controls.reject(
                Opcode.LL_CIS_REQ, ErrorCode.CONNECTION_ACCEPT_TIMEOUT_ERROR
            )
            self._finish(stream, ErrorCode.CONNECTION_ACCEPT_TIMEOUT_ERROR)
        elif stream.stage in (Setup.REQUEST, Setup.RESPONSE):
            stream.connection.complete(ErrorCode.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)
        elif stream.stage == Setup.INDICATION:
            stream.connection.complete(ErrorCode.INSTANT_PASSED_ERROR)
        elif stream.stage == Setup.TERMINATING:
            self._locally_terminated(stream)
        elif stream.event is not None:
            self._finish(
                stream,
                ErrorCode.CONNECTION_TIMEOUT_ERROR
                if stream.reported
                else ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR,
            )

    async def _wait_until(self, deadline: int) -> None:
        await self.link.radio.advance_to_us(deadline)

    async def run(self, event: CisEvent) -> None:
        stream = self.streams.get(event.handle)
        if stream is None or stream.event is not event or stream.stage != Setup.ACTIVE:
            return
        await event.advance(
            on_established=None
            if stream.reported
            else partial(self._report_established, stream)
        )
        if event.mic_failed and stream.event is event and stream.stage == Setup.ACTIVE:
            self._finish(
                stream, ErrorCode.CONNECTION_TERMINATED_DUE_TO_MIC_FAILURE_ERROR
            )

    def _report_established(self, stream: Cis) -> None:
        if stream.stage == Setup.ACTIVE:
            stream.reported = True
            self.link.listener.on_cis_established(self._snapshot(stream), SUCCESS)

    def prepare(self) -> list[PendingOperation]:
        operations = []
        for stream in tuple(self.streams.values()):
            if (
                stream.connection is None
                or self.streams.get(stream.handle) is not stream
            ):
                continue
            if stream.failure is not None:
                self._finish(stream, stream.failure)
                continue
            if stream.cancellation and self._cancel_before_send(stream):
                self._cancelled(stream)
                continue
            event = stream.event if stream.stage == Setup.ACTIVE else None
            if event is not None and event.exhausted:
                self._finish(
                    stream, ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR
                )
                continue
            deadline = event.deadline_us if event is not None else stream.deadline_us
            if deadline and deadline <= self.link.now_us:
                self._expire(stream)
                continue
            if deadline:
                operations.append(
                    PendingOperation(
                        deadline,
                        RadioPriority.EXPIRY,
                        stream,
                        partial(self._wait_until, deadline),
                    )
                )
            if event is not None:
                event.skip_missed()
                operations.append(
                    PendingOperation(
                        event.reservation().start_us,
                        RadioPriority.ISOCHRONOUS,
                        event,
                        partial(self.run, event),
                    )
                )
        return operations

    @staticmethod
    def _cancel_before_send(stream: Cis) -> bool:
        if stream.stage == Setup.QUEUED:
            return True
        if stream.stage != Setup.REQUEST:
            return False
        payload = CisRequest(
            stream.cig_id, stream.cis_id, stream.parameters, stream.window
        ).encode()
        return stream.connection.event.cancel_queued_control(payload)

    def reservations(self, exclude: object = None) -> list[RadioReservation]:
        return [
            reservation
            for event in self.events()
            if event is not exclude
            for reservation in event.reservations()
        ]
