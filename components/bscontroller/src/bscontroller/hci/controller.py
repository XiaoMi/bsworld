# Adapted for the independent BSController package.
# Copyright 2021-2022 Google LLC
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------
"""Lower HCI command dispatch and Controller identity (Core 6.3, Vol 4, Part E).

Link Layer procedures execute through a required radio backend. This endpoint
does not implement an in-memory peer link or Host protocols.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from bscontroller import hci
from bscontroller.colors import color
from bscontroller.config import ControllerConfig
from bscontroller.errors import InvalidPacketError
from bscontroller.hci.records import AdvertisingSet, ConnectionRecord, LegacyAdvertiser
from bscontroller.hci.transport import TransportSink
from bscontroller.linklayer.features import (
    FEATURE_SET_SIZE,
    feature_max_page,
    feature_page,
)

if TYPE_CHECKING:
    from bscontroller.hci.backend import ControllerBackend
    from bscontroller.hci.snoop import BtsnoopWriter

logger = logging.getLogger(__name__)


class Controller:
    """One LE Controller's Lower HCI endpoint."""

    def __init__(
        self,
        name: str,
        *,
        host_sink: TransportSink | None = None,
        public_address: bytes | str | hci.Address | None = None,
        backend: ControllerBackend,
        snoop: BtsnoopWriter | None = None,
        config: ControllerConfig | None = None,
    ) -> None:
        self.name = name
        self.config = config or ControllerConfig()
        self.backend = backend
        self._snoop = snoop
        self._command_events: list[hci.HCI_Packet] | None = None
        self.supported_commands = set(backend.supported_commands)
        self.lmp_features = backend.lmp_features
        self.le_states = backend.le_states
        self.le_connections: dict[int, ConnectionRecord] = {}
        self.advertising_sets: dict[int, AdvertisingSet] = {}
        self.le_legacy_advertiser = LegacyAdvertiser(self)
        # Vol 4, Part E, 7.3.1, 7.3.69 and 7.8.1: power-on masks equal the Reset
        # defaults.
        self.event_mask = hci.HCI_EVENT_MASK_DEFAULT
        self.event_mask_page_2 = 0
        self.le_event_mask = hci.HCI_LE_EVENT_MASK_DEFAULT
        if not isinstance(public_address, hci.Address):
            public_address = hci.Address(
                public_address or bytes(6), hci.AddressType.PUBLIC_DEVICE
            )
        self.hci_sink = host_sink
        self.terminated: asyncio.Future[None] = (
            asyncio.get_running_loop().create_future()
        )
        self.le_procedures = backend.bind(self, public_address)

    def trace(self, message: str | Callable[[], str]) -> None:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                '[%s] %s', self.name, message() if callable(message) else message
            )

    @property
    def host(self) -> TransportSink | None:
        return self.hci_sink

    @host.setter
    def host(self, host: TransportSink | None) -> None:
        '''
        Sets the host (sink) for this controller, and set this controller as the
        controller (sink) for the host
        '''
        self.set_packet_sink(host)

    def set_packet_sink(self, sink: TransportSink | None) -> None:
        '''
        Method from the Packet Source interface
        '''
        self.hci_sink = sink

    @property
    def public_address(self) -> hci.Address:
        return self.le_procedures.ll.public_address

    @public_address.setter
    def public_address(self, address: hci.Address | str) -> None:
        if isinstance(address, str):
            address = hci.Address(address, hci.AddressType.PUBLIC_DEVICE)
        self.le_procedures.ll.set_public_address(address)

    @property
    def random_address(self) -> hci.Address:
        return self.le_procedures.ll.random_address or hci.Address.ANY_RANDOM

    @random_address.setter
    def random_address(self, address: hci.Address | str) -> None:
        if isinstance(address, str):
            address = hci.Address(address)
        status = self.le_procedures.ll.set_random_address(address)
        if status:
            raise ValueError(f'cannot set random address: {status.name}')

    @property
    def le_features(self) -> int:
        return self.le_procedures.ll.features

    def on_packet(self, packet: bytes) -> None:
        if self._snoop is not None:
            self._snoop.capture(packet, received=False)
        if not packet or packet[0] != hci.HCI_COMMAND_PACKET:
            try:
                decoded = hci.HCI_Packet.from_bytes(packet)
            except hci.InvalidIsoDataError as error:
                logger.warning('Dropping %s', error)
                return
            self.on_hci_packet(decoded)
            return
        opcode, parameters = hci.HCI_Command.split_packet(packet)
        command_class = hci.HCI_Command.command_classes.get(opcode)
        if command_class is None or opcode not in self.supported_commands:
            # Unknown commands have no parameter schema at this Controller.
            generic = (
                hci.HCI_AsyncCommand
                if command_class and issubclass(command_class, hci.HCI_AsyncCommand)
                else hci.HCI_Command
            )
            self.on_hci_packet(generic(parameters, op_code=opcode))
            return
        try:
            command = command_class.decode_parameters(parameters)
        except InvalidPacketError:
            status = hci.HCI_ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
            if issubclass(command_class, hci.HCI_AsyncCommand):
                self._send_hci_command_status(status, opcode)
            else:
                self.send_hci_packet(
                    hci.HCI_Command_Complete_Event(
                        1,
                        opcode,
                        command_class.error_return_parameters(status, parameters),
                    )
                )
            return
        self.on_hci_packet(command)

    def on_hci_packet(self, packet: hci.HCI_Packet) -> None:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                '%s [%s] %s: %s',
                color('<<<', 'blue'),
                self.name,
                color('HOST -> CONTROLLER', 'blue'),
                packet,
            )

        # If the packet is a command, invoke the handler for this packet
        match packet:
            case hci.HCI_Command():
                self.on_hci_command_packet(packet)
            case hci.HCI_AclDataPacket():
                self.on_hci_acl_data_packet(packet)
            case hci.HCI_IsoDataPacket():
                self.le_procedures.on_iso_packet(packet)
            case hci.HCI_Event():
                self.on_hci_event_packet(packet)
            case _:
                logger.warning(f'!!! unknown packet type {packet.hci_packet_type}')

    def on_hci_command_packet(self, command: hci.HCI_Command) -> None:
        handler_name = f'on_{command.name.lower()}'
        handler = getattr(
            self.le_procedures, handler_name, getattr(self, handler_name, None)
        )
        asynchronous = isinstance(command, hci.HCI_AsyncCommand)
        events = []
        if command.op_code not in self.supported_commands or handler is None:
            result = (
                hci.HCI_ErrorCode.UNKNOWN_HCI_COMMAND_ERROR
                if asynchronous
                else self.on_hci_command(command)
            )
        elif not self.le_procedures.command_allowed(command.op_code):
            status = hci.HCI_ErrorCode.COMMAND_DISALLOWED_ERROR
            result = (
                status
                if asynchronous
                else command.error_return_parameters(status, command.parameters)
            )
        else:
            # The handler runs without yielding on the Controller loop. Buffer
            # its notifications until the command response has been queued.
            self._command_events = events
            try:
                result = handler(command)
            finally:
                self._command_events = None

        if asynchronous:
            self._send_hci_command_status(result, command.op_code)
        else:
            self.send_hci_packet(
                hci.HCI_Command_Complete_Event(1, command.op_code, result)
            )
        for event in events:
            self.send_hci_packet(event)

    def send_event(self, event: hci.HCI_Event) -> None:
        if self._command_events is None:
            self.send_hci_packet(event)
        else:
            self._command_events.append(event)

    def on_hci_event_packet(self, _event: hci.HCI_Packet) -> None:
        logger.warning('!!! unexpected event packet')

    def on_hci_acl_data_packet(self, packet: hci.HCI_AclDataPacket) -> None:
        self.le_procedures.on_hci_acl_data_packet(packet)

    def send_hci_packet(self, packet: hci.HCI_Packet) -> None:
        self.send_hci_packets((packet,))

    def send_hci_packets(self, packets) -> None:
        """Send an ordered batch, preserving individual H4 frames in the trace."""
        if self.host:
            frames = []
            for packet in packets:
                logger.debug(
                    '%s [%s] %s: %s',
                    color('>>>', 'green'),
                    self.name,
                    color('CONTROLLER -> HOST', 'green'),
                    packet,
                )
                data = bytes(packet)
                if self._snoop is not None:
                    self._snoop.capture(data, received=True)
                frames.append(data)
            loop = asyncio.get_running_loop()
            receive_batch = getattr(self.host, 'on_packets', None)
            if len(frames) > 1 and receive_batch is not None:
                loop.call_soon(receive_batch, frames)
            else:
                for frame in frames:
                    loop.call_soon(self.host.on_packet, frame)

    def _send_hci_command_status(self, status: int, op_code: int) -> None:
        self.send_hci_packet(
            hci.HCI_Command_Status_Event(
                status=status, num_hci_command_packets=1, command_opcode=op_code
            )
        )

    @property
    def is_advertising(self) -> bool:
        return self.le_legacy_advertiser.enabled or any(
            s.enabled for s in self.advertising_sets.values()
        )

    def on_hci_command(
        self, command: hci.HCI_Command
    ) -> hci.HCI_StatusReturnParameters:
        logger.warning(color(f'--- Unsupported command {command}', 'red'))
        return hci.HCI_StatusReturnParameters(
            hci.HCI_ErrorCode.UNKNOWN_HCI_COMMAND_ERROR
        )

    def on_hci_set_event_mask_command(
        self, command: hci.HCI_Set_Event_Mask_Command
    ) -> hci.HCI_StatusReturnParameters:
        '''
        See Bluetooth spec Vol 4, Part E - 7.3.1 Set Event Mask Command
        '''
        self.event_mask = int.from_bytes(
            command.event_mask, byteorder='little', signed=False
        )
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_set_event_mask_page_2_command(
        self, command: hci.HCI_Set_Event_Mask_Page_2_Command
    ) -> hci.HCI_StatusReturnParameters:
        '''
        See Bluetooth spec Vol 4, Part E - 7.3.69 Set Event Mask Page 2 Command
        '''
        self.event_mask_page_2 = int.from_bytes(
            command.event_mask_page_2, byteorder='little', signed=False
        )
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_read_local_version_information_command(
        self, _command: hci.HCI_Read_Local_Version_Information_Command
    ) -> hci.HCI_Read_Local_Version_Information_ReturnParameters:
        '''
        See Bluetooth spec Vol 4, Part E - 7.4.1 Read Local Version Information Command
        '''
        return hci.HCI_Read_Local_Version_Information_ReturnParameters(
            status=hci.HCI_ErrorCode.SUCCESS,
            hci_version=self.config.hci_version,
            hci_subversion=self.config.hci_revision,
            lmp_version=self.config.lmp_version,
            company_identifier=self.config.manufacturer,
            lmp_subversion=self.config.lmp_subversion,
        )

    def on_hci_read_local_supported_commands_command(
        self, _command: hci.HCI_Read_Local_Supported_Commands_Command
    ) -> hci.HCI_Read_Local_Supported_Commands_ReturnParameters:
        '''
        See Bluetooth spec Vol 4, Part E - 7.4.2 Read Local Supported Commands Command
        '''
        return hci.HCI_Read_Local_Supported_Commands_ReturnParameters(
            hci.HCI_ErrorCode.SUCCESS,
            supported_commands=sum(
                hci.HCI_SUPPORTED_COMMANDS_MASKS.get(opcode, 0)
                for opcode in self.supported_commands
            ).to_bytes(64, 'little'),
        )

    def on_hci_read_local_supported_features_command(
        self, _command: hci.HCI_Read_Local_Supported_Features_Command
    ) -> hci.HCI_Read_Local_Supported_Features_ReturnParameters:
        '''
        See Bluetooth spec Vol 4, Part E - 7.4.3 Read Local Supported Features Command
        '''
        return hci.HCI_Read_Local_Supported_Features_ReturnParameters(
            hci.HCI_ErrorCode.SUCCESS,
            lmp_features=self.lmp_features.to_bytes(8, 'little'),
        )

    def on_hci_read_bd_addr_command(
        self, _command: hci.HCI_Read_BD_ADDR_Command
    ) -> hci.HCI_Read_BD_ADDR_ReturnParameters:
        '''
        See Bluetooth spec Vol 4, Part E - 7.4.6 Read BD_ADDR Command
        '''
        return hci.HCI_Read_BD_ADDR_ReturnParameters(
            status=hci.HCI_ErrorCode.SUCCESS,
            bd_addr=self.public_address,
        )

    def on_hci_le_set_event_mask_command(
        self, command: hci.HCI_LE_Set_Event_Mask_Command
    ) -> hci.HCI_StatusReturnParameters:
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.1 LE Set Event Mask Command
        '''
        self.le_event_mask = int.from_bytes(
            command.le_event_mask, byteorder='little', signed=False
        )
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_le_read_buffer_size_command(
        self, _command: hci.HCI_LE_Read_Buffer_Size_Command
    ) -> hci.HCI_LE_Read_Buffer_Size_ReturnParameters:
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.2 LE Read Buffer Size Command
        '''
        return hci.HCI_LE_Read_Buffer_Size_ReturnParameters(
            status=hci.HCI_ErrorCode.SUCCESS,
            le_acl_data_packet_length=self.config.acl_packet_length,
            total_num_le_acl_data_packets=self.config.acl_packet_count,
        )

    def on_hci_le_read_local_supported_features_command(
        self, _command: hci.HCI_LE_Read_Local_Supported_Features_Command
    ) -> hci.HCI_LE_Read_Local_Supported_Features_ReturnParameters:
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.3 LE Read Local Supported Features
        Command
        '''
        return hci.HCI_LE_Read_Local_Supported_Features_ReturnParameters(
            status=hci.HCI_ErrorCode.SUCCESS,
            le_features=feature_page(self.le_features, 0),
        )

    def on_hci_le_read_all_local_supported_features_command(
        self, _command: hci.HCI_LE_Read_All_Local_Supported_Features_Command
    ) -> hci.HCI_LE_Read_All_Local_Supported_Features_ReturnParameters:
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.128 LE Read All Local Supported
        Features Command
        '''
        return hci.HCI_LE_Read_All_Local_Supported_Features_ReturnParameters(
            status=hci.HCI_ErrorCode.SUCCESS,
            max_page=feature_max_page(self.le_features),
            le_features=self.le_features.to_bytes(FEATURE_SET_SIZE, 'little'),
        )

    def on_hci_le_read_supported_states_command(
        self, _command: hci.HCI_LE_Read_Supported_States_Command
    ) -> hci.HCI_LE_Read_Supported_States_ReturnParameters:
        '''
        See Bluetooth spec Vol 4, Part E - 7.8.27 LE Read Supported States Command
        '''
        return hci.HCI_LE_Read_Supported_States_ReturnParameters(
            status=hci.HCI_ErrorCode.SUCCESS, le_states=self.le_states
        )

    def find_connection_by_handle(self, handle: int) -> ConnectionRecord | None:
        return self.le_connections.get(handle)
