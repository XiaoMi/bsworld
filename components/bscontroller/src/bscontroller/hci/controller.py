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
import struct
from typing import TYPE_CHECKING

from bscontroller import hci
from bscontroller.colors import color
from bscontroller.config import ControllerConfig
from bscontroller.hci.records import AdvertisingSet, ConnectionRecord, LegacyAdvertiser
from bscontroller.hci.transport import TransportSink
from bscontroller.linklayer.features import (
    FEATURE_SET_SIZE,
    feature_max_page,
    feature_page,
)

if TYPE_CHECKING:
    from bscontroller.hci.backend import ControllerBackend
    from bscontroller.hci.le import LeControllerProcedures
    from bscontroller.hci.snoop import BtsnoopWriter

logger = logging.getLogger(__name__)


class Controller:
    """One LE Controller's Lower HCI endpoint."""

    def __init__(
        self,
        name: str,
        host_source=None,
        host_sink: TransportSink | None = None,
        public_address: bytes | str | hci.Address | None = None,
        *,
        backend: ControllerBackend,
        snoop: BtsnoopWriter | None = None,
        config: ControllerConfig | None = None,
    ) -> None:
        self.name = name
        self.config = config or ControllerConfig()
        self.backend = backend
        self._snoop = snoop
        self.le_procedures: LeControllerProcedures
        self.le_connections: dict[hci.Address, ConnectionRecord] = {}
        self.advertising_sets: dict[int, AdvertisingSet] = {}
        self.le_legacy_advertiser = LegacyAdvertiser(self)
        # Vol 4, Part E, 7.3.1, 7.3.69 and 7.8.1: power-on masks equal the Reset
        # defaults.
        self.event_mask = hci.HCI_EVENT_MASK_DEFAULT
        self.event_mask_page_2 = 0
        self.le_event_mask = hci.HCI_LE_EVENT_MASK_DEFAULT
        self._random_address = hci.Address('00:00:00:00:00:00')
        if isinstance(public_address, hci.Address):
            self._public_address = public_address
        else:
            self._public_address = hci.Address(
                public_address or '00:00:00:00:00:00', hci.AddressType.PUBLIC_DEVICE
            )
        self.hci_sink = host_sink
        self.terminated: asyncio.Future[None] = (
            asyncio.get_running_loop().create_future()
        )
        backend.bind(self)
        if host_source is not None:
            host_source.set_packet_sink(self)

    def trace(self, message: str) -> None:
        logger.debug('[%s] %s', self.name, message)

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
        return self._public_address

    @public_address.setter
    def public_address(self, address: hci.Address | str) -> None:
        if isinstance(address, str):
            address = hci.Address(address)
        self._public_address = address

    @property
    def random_address(self) -> hci.Address:
        return self._random_address

    @random_address.setter
    def random_address(self, address: hci.Address | str) -> None:
        if isinstance(address, str):
            address = hci.Address(address)
        self._random_address = address
        logger.debug(f'new random address: {address}')

    def on_packet(self, packet: bytes) -> None:
        if self._snoop is not None:
            self._snoop.capture(packet, received=False)
        if len(packet) >= 4 and packet[0] == hci.HCI_COMMAND_PACKET:
            opcode, size = struct.unpack_from('<HB', packet, 1)
            if opcode not in self.supported_commands and len(packet) == size + 4:
                # An unsupported opcode has no parameter schema at this Controller.
                command_class = hci.HCI_Command.command_classes.get(
                    opcode, hci.HCI_Command
                )
                generic = (
                    hci.HCI_AsyncCommand
                    if issubclass(command_class, hci.HCI_AsyncCommand)
                    else hci.HCI_Command
                )
                self.on_hci_packet(generic(packet[4:], op_code=opcode))
                return
        self.on_hci_packet(hci.HCI_Packet.from_bytes(packet))

    def on_hci_packet(self, packet: hci.HCI_Packet) -> None:
        logger.debug(
            f'{color("<<<", "blue")} [{self.name}] '
            f'{color("HOST -> CONTROLLER", "blue")}: {packet}'
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
        # Vol 4, Part E, 4.5: unknown and disallowed commands complete with an error
        # through Command Status (asynchronous) or Command Complete (synchronous).
        handler_name = f'on_{command.name.lower()}'
        if command.op_code not in self.supported_commands:
            if isinstance(command, hci.HCI_AsyncCommand):
                self._send_hci_command_status(
                    hci.HCI_ErrorCode.UNKNOWN_HCI_COMMAND_ERROR, command.op_code
                )
                return
            handler = self.on_hci_command
        else:
            handler = getattr(
                self.le_procedures,
                handler_name,
                getattr(self, handler_name, self.on_hci_command),
            )
        if handler != self.on_hci_command and not self.le_procedures.command_allowed(
            command.op_code
        ):
            if isinstance(command, hci.HCI_AsyncCommand):
                self._send_hci_command_status(
                    hci.HCI_ErrorCode.COMMAND_DISALLOWED_ERROR, command.op_code
                )
                return
            result = hci.HCI_StatusReturnParameters(
                hci.HCI_ErrorCode.COMMAND_DISALLOWED_ERROR
            )
        else:
            result = handler(command)
        if isinstance(command, hci.HCI_SyncCommand) or handler == self.on_hci_command:
            if result is None:
                logger.error("Sync command handlers should return parameters, got None")
                return
            self.send_hci_packet(
                hci.HCI_Command_Complete_Event(
                    num_hci_command_packets=1,
                    command_opcode=command.op_code,
                    return_parameters=result,
                )
            )
        elif result is not None:
            logger.error("Async command handlers should return None, got %s", result)

    def on_hci_event_packet(self, _event: hci.HCI_Packet) -> None:
        logger.warning('!!! unexpected event packet')

    def on_hci_acl_data_packet(self, packet: hci.HCI_AclDataPacket) -> None:
        # Look for the connection to which this data belongs
        connection = self.find_connection_by_handle(packet.connection_handle)
        if connection is None:
            logger.warning(
                f'!!! no connection for handle 0x{packet.connection_handle:04X}'
            )
            return

        # Pass the packet to the connection
        connection.on_hci_acl_data_packet(packet)

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
            bd_addr=self._public_address or hci.Address.ANY,
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
        return next(
            (
                record
                for record in self.le_connections.values()
                if record.handle == handle
            ),
            None,
        )
