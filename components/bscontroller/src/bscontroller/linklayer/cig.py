# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Connected isochronous group and stream lifetime (Core Vol 6, Part B, 5.1.15)."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from functools import partial
from typing import TYPE_CHECKING

from bscontroller.errors import ErrorCode
from bscontroller.linklayer import ControlOpcode as Opcode
from bscontroller.linklayer.api import CigConfig, CisEstablished
from bscontroller.linklayer.bitstream import valid_access_address
from bscontroller.linklayer.cig_schedule import (
    future_window,
    new_cis_window,
    plan_frame_spaces,
    validate_supervision,
)
from bscontroller.linklayer.cis import CisEvent, CisEventParameters
from bscontroller.linklayer.cis_parameters import (
    CIS_IND_FIELDS,
    CIS_REQ_FIELDS,
    CIS_WINDOW_FIELDS,
    ISO_INTERVAL_UNIT_US,
    CisIndication,
    CisRequest,
    CisWindow,
    UnsupportedCisBurstOrFlush,
    UnsupportedCisParameters,
    event_anchor,
    event_distance,
    plan_cig,
    valid_cis_supervision_timeout,
)
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.frame_space import REQUEST as FRAME_SPACE_REQUEST
from bscontroller.linklayer.procedure import (
    DIFFERENT_TRANSACTION_COLLISION,
    PROCEDURE_COLLISION,
    DeferredProcedure,
    ProcedureContext,
    ProcedureOrigin,
)
from bscontroller.linklayer.resource import PendingOperation
from bscontroller.linklayer.scheduler import (
    RadioPriority,
    RadioReservation,
)
from bscontroller.linklayer.state import Role

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from bscontroller.linklayer.connection import Connection
    from bscontroller.linklayer.connection_event import Outbound
    from bscontroller.linklayer.iso import IsoDataPaths
    from bscontroller.linklayer.listener import ConnectedIsoListener
    from bscontroller.radio.base import Radio

SUCCESS = ErrorCode.SUCCESS
UNKNOWN = ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
DISALLOWED = ErrorCode.COMMAND_DISALLOWED_ERROR
INVALID = ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
UNSUPPORTED = ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR
REMOTE_UNSUPPORTED = ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
NO_RESOURCES = ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR
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
    setup_deadline_us: int | None = None
    procedure: ProcedureContext | None = None
    reported: bool = False
    cancellation: bool = False
    termination: bytes | None = None
    failure: int | None = None
    indication: CisIndication | None = None
    queued_setup: Outbound | None = None
    queued_termination: Outbound | None = None

    @property
    def setup_pending(self) -> bool:
        if self.stage == Setup.ACTIVE:
            # The initiator waits for establishment before starting another LLCP
            # (6.B.5.1.15). A Peripheral that received LL_CIS_IND can already
            # route the next ACL setup exchange to a different CIS.
            return self.central and (
                not self.reported or self.event.awaiting_indication_ack
            )
        return self.stage in (
            Setup.REQUEST,
            Setup.HOST,
            Setup.RESPONSE,
            Setup.INDICATION,
        )


@dataclass
class Cig:
    config: CigConfig
    streams: dict[int, Cis] = field(default_factory=dict)
    started: bool = False

    @property
    def reference_event(self) -> CisEvent | None:
        return next(
            (
                stream.event
                for stream in self.streams.values()
                if stream.event is not None
            ),
            None,
        )

    @property
    def spans_acls(self) -> bool:
        connections = {stream.connection for stream in self.streams.values()}
        connections.discard(None)
        return len(connections) > 1


class CigManager:
    def __init__(
        self,
        radio: Radio,
        listener: ConnectedIsoListener,
        iso: IsoDataPaths,
        *,
        available_handles: Callable[[], Iterator[int]],
        find_connection: Callable[[int], Connection | None],
        host_supports_cis: Callable[[], bool],
        new_access_address: Callable[[], int],
        used_access_addresses: Callable[[], set[int]],
    ):
        self.radio = radio
        self.listener = listener
        self.iso = iso
        self._available_handles = available_handles
        self._find_connection = find_connection
        self._host_supports_cis = host_supports_cis
        self._new_access_address = new_access_address
        self._used_access_addresses = used_access_addresses
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

    def shift_anchors(self, connection: Connection, correction_us: int) -> None:
        """Keep a Peripheral's active CISes synchronized with their Central."""
        for event in self.events():
            if event.connection is connection:
                event.anchor_us += correction_us

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
        free = self._available_handles()
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
        if not self._host_supports_cis():
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
            connection = self._find_connection(acl_handle)
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
            procedure = ProcedureContext(
                Opcode.LL_CIS_REQ,
                frozenset((Opcode.LL_CIS_RSP,)),
                partial(self._receive_setup, stream),
                on_sent=partial(self._setup_sent, stream),
                on_acknowledged=partial(self._setup_acknowledged, stream),
            )
            stream.procedure = procedure
            self._schedule_creation(stream, procedure)
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
            or stream.termination is not None
        ):
            return DISALLOWED
        connection = stream.connection
        if not stream.reported:
            stream.cancellation = True
        if stream.stage == Setup.ACTIVE or (
            stream.stage == Setup.INDICATION
            and not connection.llcp.cancel_queued(stream.queued_setup)
        ):
            payload = bytes(
                [Opcode.LL_CIS_TERMINATE_IND, stream.cig_id, stream.cis_id, reason]
            )
            stream.termination = payload
            connection.llcp.initiate(
                DeferredProcedure(
                    'cis_termination', partial(self._queue_termination, stream, payload)
                )
            )
        elif stream.stage == Setup.INDICATION:
            connection.controls.reject(Opcode.LL_CIS_RSP, CANCELLED)
            stream.stage = Setup.QUEUED
        return SUCCESS

    def _queue_termination(self, stream: Cis, payload: bytes) -> None:
        # A deferred request must still own this incarnation of the CIS.
        if stream.termination is not payload or stream.stage != Setup.ACTIVE:
            return
        connection = stream.connection
        assert connection is not None
        if connection.termination_deadline_us is not None:
            return
        # Stop CIS radio activity when the indication enters the ACL queue (6.B.5.1.16).
        stream.procedure = ProcedureContext(
            Opcode.LL_CIS_TERMINATE_IND,
            frozenset(),
            None,
            on_acknowledged=lambda _: self._finish(stream, LOCAL_TERMINATED),
        )
        stream.queued_termination = connection.llcp.send(
            stream.procedure, payload, priority=True
        )
        stream.stage = Setup.TERMINATING
        stream.setup_deadline_us = None

    def close_acl(self, connection: Connection, reason: int) -> None:
        for stream in tuple(self.streams.values()):
            if stream.connection is connection:
                self._finish(stream, reason)

    def _exchange(self, connection: Connection) -> Cis | None:
        return next(
            (
                stream
                for stream in self.streams.values()
                if stream.connection is connection and stream.setup_pending
            ),
            None,
        )

    def busy(self, connection: Connection) -> bool:
        return any(
            stream.connection is connection
            and (stream.setup_pending or stream.stage == Setup.TERMINATING)
            for stream in self.streams.values()
        )

    def _schedule_creation(self, stream: Cis, procedure: ProcedureContext) -> None:
        connection = stream.connection

        def ready() -> bool:
            # Finalize the anchor before another stream negotiates a radio gap.
            return not procedure.active or not any(
                item.central and item.stage in (Setup.REQUEST, Setup.INDICATION)
                for item in self.streams.values()
            )

        def start() -> None:
            if (
                not procedure.active
                or stream.stage != Setup.QUEUED
                or stream.cancellation
            ):
                return
            if connection.peer_features is None:
                connection.controls.features.discover(discovered)
                return
            if (
                not connection.peer_features
                & LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_PERIPHERAL
            ):
                self._finish(stream, REMOTE_UNSUPPORTED)
                return
            try:
                group = self.groups[stream.cig_id]
                plan_frame_spaces(group, stream)
                stream.window = new_cis_window(group, stream, self.reservations)
            except ValueError:
                self._finish(stream, ErrorCode.UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR)
                return
            stream.stage = Setup.REQUEST
            stream.queued_setup = connection.llcp.send(
                procedure,
                CisRequest(
                    stream.cig_id, stream.cis_id, stream.parameters, stream.window
                ).encode(),
            )

        def discovered(status: int) -> None:
            if procedure.active:
                if status:
                    self._finish(stream, status)
                else:
                    self._schedule_creation(stream, procedure)

        # Use the event's applied Instants and current schedule to pick the window.
        connection.llcp.queue_for_event(
            DeferredProcedure('cis_creation', start, ready=ready)
        )

    def _respond(self, stream: Cis) -> None:
        connection = stream.connection
        try:
            stream.window = future_window(stream, stream.window)
        except ValueError:
            connection.controls.reject(
                Opcode.LL_CIS_REQ, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR
            )
            stream.failure = ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR
            return
        stream.stage = Setup.RESPONSE
        stream.setup_deadline_us = None
        connection.llcp.send(
            stream.procedure, bytes([Opcode.LL_CIS_RSP]) + stream.window.encode()
        )

    def _request(self, connection: Connection, payload: bytes) -> None:
        if not self._host_supports_cis():
            connection.controls.reject(Opcode.LL_CIS_REQ, REMOTE_UNSUPPORTED)
            return
        try:
            request = CisRequest.decode(payload)
            request.window.validate(connection)
            params = request.parameters
            validate_supervision(connection, params.interval_us)
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
                PROCEDURE_COLLISION,
            )
            return
        if self._stream_for_ids(connection, request.cig_id, request.cis_id) is not None:
            connection.controls.reject(
                Opcode.LL_CIS_REQ, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR
            )
            return
        handle = next(self._available_handles(), None)
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
            setup_deadline_us=self.radio.now_us + 625 * self.connection_accept_timeout,
            procedure=ProcedureContext(
                Opcode.LL_CIS_REQ,
                frozenset((Opcode.LL_CIS_IND,)),
                None,
                origin=ProcedureOrigin.REMOTE,
                blocks_local=True,
            ),
        )
        connection.llcp.start_response_timer(stream.procedure)
        self.streams[handle] = stream
        frame_space = connection.controls.frame_space
        if frame_space.pending and frame_space.context.transmitted:
            frame_space.collision = True
        if not self.listener.on_cis_request(
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
            requested = stream.window.offset_at(
                connection, window.conn_event_count, params.interval_us
            )
            shift = (window.offset_min_us - requested) % params.interval_us
            if shift + window.width_us > stream.window.width_us:
                raise ValueError('CIS response window lies outside request')
            window = future_window(stream, window)
        except ValueError:
            connection.controls.reject(
                Opcode.LL_CIS_RSP, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR
            )
            self._finish(stream, ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR)
            return
        # Center a shared CIG in the negotiated gap to leave room for the
        # independent ACL exchanges on both sides of its recurring slot.
        group = self.groups[stream.cig_id]
        offset = window.offset_min_us
        if group.spans_acls:
            offset += window.width_us // 2
        stream.indication = CisIndication(
            self._new_access_address(),
            offset,
            params.cig_sync_delay_us,
            params.cis_sync_delay_us,
            window.conn_event_count,
        )
        stream.stage = Setup.INDICATION
        stream.setup_deadline_us = event_anchor(connection, window.conn_event_count)
        stream.queued_setup = connection.llcp.send(
            stream.procedure, stream.indication.encode()
        )

    def _receive_indication(self, stream: Cis, payload: bytes) -> None:
        connection = stream.connection
        indication = CisIndication.decode(payload)
        try:
            anchor = (
                event_anchor(connection, indication.conn_event_count)
                + indication.offset_us
            )
            requested = stream.window.offset_at(
                connection, indication.conn_event_count, stream.parameters.interval_us
            )
            offset = (indication.offset_us - requested) % stream.parameters.interval_us
            if (
                not valid_access_address(indication.access_address)
                or indication.access_address in self._used_access_addresses()
                or offset > stream.window.width_us
                or anchor < self.radio.now_us
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
                stream.setup_deadline_us = None
                if stream.procedure is not None:
                    stream.procedure.finish()
                stream.procedure = ProcedureContext(
                    Opcode.LL_CIS_TERMINATE_IND,
                    frozenset(),
                    None,
                    origin=ProcedureOrigin.REMOTE,
                    blocks_local=True,
                )
                connection.llcp.start_response_timer(stream.procedure)
                connection.event.after_acknowledgment(
                    partial(
                        self._remote_termination_acknowledged,
                        stream,
                        stream.procedure,
                        payload[3],
                    )
                )
            return True
        stream = self._exchange(connection)
        if stream is None or opcode == Opcode.LL_CIS_RSP:
            return False
        return self._receive_setup(stream, payload)

    def _receive_setup(self, stream: Cis, payload: bytes) -> bool:
        opcode = payload[0]
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
            if stream.stage == Setup.REQUEST and stream.procedure.transmitted:
                connection.controls.reject(
                    Opcode.LL_FRAME_SPACE_REQ,
                    DIFFERENT_TRANSACTION_COLLISION,
                    priority=True,
                )
                return True
            if stream.stage in (Setup.INDICATION, Setup.ACTIVE):
                connection.complete(DIFFERENT_TRANSACTION_COLLISION)
                return True
        return False

    def _setup_sent(self, stream: Cis, payload: bytes) -> None:
        if payload[0] == Opcode.LL_CIS_IND and stream.stage == Setup.INDICATION:
            # The Central creates the CIS on transmission (Core 6.B.5.1.15).
            self._activate(stream)

    def _setup_acknowledged(self, stream: Cis, payload: bytes) -> None:
        if payload[0] == Opcode.LL_CIS_IND and stream.stage == Setup.ACTIVE:
            stream.event.start_supervision()
            if stream.reported:
                stream.procedure.finish()

    def _remote_termination_acknowledged(
        self, stream: Cis, procedure: ProcedureContext, reason: int
    ) -> None:
        if stream.procedure is procedure:
            self._finish(stream, reason)

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
        anchor, phase = connection.project_anchor(
            event_distance(connection, indication.conn_event_count)
        )
        anchor += indication.offset_us
        parameters = replace(
            stream.parameters,
            cis_sync_delay_us=indication.cis_sync_delay_us,
            cig_sync_delay_us=indication.cig_sync_delay_us,
        )
        if stream.central and (reference := self.groups[stream.cig_id].reference_event):
            _, phase = reference.anchor_at_or_after(
                anchor
                - (
                    reference.parameters.cis_sync_delay_us
                    - parameters.cis_sync_delay_us
                )
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
            clock_remainder=phase,
        )
        stream.stage = Setup.ACTIVE
        stream.setup_deadline_us = None
        stream.procedure.stop_response_timer()
        if not stream.central:
            stream.procedure.finish()

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
        for forward, direction in ((True, params.c_to_p), (False, params.p_to_c)):
            self.iso.configure_direction(
                stream.handle,
                transmit=stream.central == forward,
                parameters=direction.isoal(
                    framed=params.framed, interval_us=params.interval_us
                )
                if direction.bn
                else None,
                synchronization_delay_us=params.synchronization_delay_us(
                    central_to_peripheral=forward
                ),
            )

    def _release(self, stream: Cis) -> None:
        if stream.queued_termination is not None:
            stream.connection.llcp.cancel_queued(stream.queued_termination)
            stream.queued_termination = None
        stream.termination = None
        if stream.procedure is not None:
            stream.procedure.finish()
            stream.procedure = None
        if not stream.central:
            if self.iso.contains(stream.handle):
                self.iso.release(stream.handle)
            del self.streams[stream.handle]
            return
        self._configure_paths(stream)
        stream.stage = Setup.CONFIGURED
        stream.queued_setup = None
        stream.connection = stream.event = stream.window = stream.indication = None
        stream.reported = False
        stream.cancellation = False
        stream.setup_deadline_us = None
        stream.failure = None

    def _finish(self, stream: Cis, reason: int) -> None:
        if self.streams.get(stream.handle) is not stream or stream.connection is None:
            return
        if stream.cancellation:
            self._cancelled(stream)
            return
        handle, reported = stream.handle, stream.reported
        self._release(stream)
        if reported:
            self.listener.on_cis_disconnected(handle, reason)
        else:
            self.listener.on_cis_established(CisEstablished(handle), reason)

    def _cancelled(self, stream: Cis) -> None:
        self._release(stream)
        self.listener.on_cis_established(CisEstablished(stream.handle), CANCELLED)
        self.listener.on_cis_disconnected(stream.handle, LOCAL_TERMINATED)

    def _expire(self, stream: Cis, deadline: int) -> None:
        if stream.stage == Setup.HOST:
            stream.connection.controls.reject(
                Opcode.LL_CIS_REQ, ErrorCode.CONNECTION_ACCEPT_TIMEOUT_ERROR
            )
            self._finish(stream, ErrorCode.CONNECTION_ACCEPT_TIMEOUT_ERROR)
        elif stream.stage == Setup.INDICATION:
            stream.connection.complete(ErrorCode.INSTANT_PASSED_ERROR)
        elif stream.event is not None:
            self._finish(
                stream,
                ErrorCode.CONNECTION_TIMEOUT_ERROR
                if stream.reported
                else ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR,
            )

    async def _wait_until(self, deadline: int) -> None:
        await self.radio.advance_to_us(deadline)

    def _active_stream(self, event: CisEvent) -> Cis | None:
        stream = self.streams.get(event.handle)
        if (
            stream is not None
            and stream.event is event
            and stream.stage == Setup.ACTIVE
            and not event.connection.closed
        ):
            return stream
        return None

    async def run(self, event: CisEvent) -> None:
        stream = self._active_stream(event)
        if stream is None:
            return

        def is_current() -> bool:
            # A Host command can release the CIS during a PHY wait, even if a
            # new CIS already has the same numeric handle when the wait ends.
            return self._active_stream(event) is stream

        await event.advance(
            is_current=is_current,
            on_established=None
            if stream.reported
            else partial(self._report_established, event),
        )
        if event.mic_failed and is_current():
            self._finish(
                stream, ErrorCode.CONNECTION_TERMINATED_DUE_TO_MIC_FAILURE_ERROR
            )

    def _report_established(self, event: CisEvent) -> None:
        if (stream := self._active_stream(event)) is not None:
            # Establishment can complete while a local termination is waiting its turn.
            stream.cancellation = False
            stream.reported = True
            if stream.procedure is not None and not event.awaiting_indication_ack:
                stream.procedure.finish()
            self.listener.on_cis_established(self._snapshot(stream), SUCCESS)

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
            deadline = (
                event.deadline_us if event is not None else stream.setup_deadline_us
            )
            if deadline is not None:
                if deadline <= self.radio.now_us:
                    self._expire(stream, deadline)
                    continue
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
        return stream.connection.llcp.cancel_queued(stream.queued_setup)

    def reservations(self, exclude: object = None) -> list[RadioReservation]:
        return [
            reservation
            for event in self.events()
            if event is not exclude
            for reservation in event.reservations()
        ]
