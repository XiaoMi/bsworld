# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Translate Link Layer notifications into Controller-owned HCI events."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bscontroller import hci
from bscontroller.hci import (
    HCI_EVENT_PARAMETER_SIZE_MAX,
    HCI_LE_ADV_COMPLETED_EVENTS_MAX,
    HCI_LE_EXT_ADV_REPORT_DATA_SIZE_MAX,
    HCI_LE_EXT_ADV_REPORT_DATA_STATUS_MASK,
    HCI_LE_EXT_ADV_REPORT_DATA_STATUS_SHIFT,
    HCI_LE_EXT_ADV_REPORT_DATA_STATUS_VALUE_MASK,
    HCI_LE_EXT_ADV_REPORT_LEGACY_PROPERTIES_MASK,
    HCI_LE_NO_ADVERTISING_HANDLE,
    HCI_LE_NO_SYNC_HANDLE,
    HCI_LE_PERIODIC_REPORT_CTE_NONE,
    HCI_LE_PERIODIC_REPORT_DATA_SIZE_MAX,
    HCI_LE_PERIODIC_REPORT_V2_DATA_SIZE_MAX,
    HCI_LE_RESPONSE_REPORT_OVERHEAD_SIZE,
    HCI_LE_RESPONSE_REPORT_RESPONSE_SIZE,
    HCI_LE_RESPONSE_REPORT_RESPONSES_MAX,
)
from bscontroller.hci.records import ConnectionRecord
from bscontroller.linklayer import PDU_CONTROL_FEATURE_SET_SIZE
from bscontroller.linklayer.api import (
    AdvertisingSnapshot,
    BigSnapshot,
    CisEstablished,
    ConnectionSnapshot,
    PeriodicSyncSnapshot,
)
from bscontroller.linklayer.state import AdvertisingProperties, DataStatus
from bscontroller.radio.base import PHY_CODED_S2_FACTOR, Phy

if TYPE_CHECKING:
    from bscontroller.hci.controller import Controller
    from bscontroller.linklayer.bis import BigInfo
    from bscontroller.linklayer.isoal import IsoSdu
    from bscontroller.linklayer.service import LinkLayerService

SUCCESS = hci.HCI_ErrorCode.SUCCESS
ReportFlags = hci.HCI_LE_Extended_Advertising_Report_Event.EventType
AdvertisingReport = hci.HCI_LE_Extended_Advertising_Report_Event
PeriodicReportV2 = hci.HCI_LE_Periodic_Advertising_Report_V2_Event
ResponseReport = hci.HCI_LE_Periodic_Advertising_Response_Report_Event


class LeControllerEvents:
    """Event side of LeControllerProcedures; the derived class owns all state."""

    controller: Controller
    ll: LinkLayerService
    extended_scan: bool
    filter_duplicates: bool
    period_duplicate_reset: bool
    seen: set[tuple]

    def trace(self, message: str) -> None:
        self.controller.trace(message)

    def _emit_hci_packet(self, packet):
        # Vol 4, Part E, 7.3.1, 7.3.69 and 7.8.1: masked events are not sent to the
        # Host.
        if isinstance(packet, hci.HCI_LE_Meta_Event):
            if not self.controller.event_mask & (1 << (hci.HCI_LE_META_EVENT - 1)):
                return
            if not self.controller.le_event_mask & (1 << (packet.subevent_code - 1)):
                return
        elif isinstance(
            packet,
            (
                hci.HCI_Disconnection_Complete_Event,
                hci.HCI_Encryption_Change_Event,
                hci.HCI_Encryption_Key_Refresh_Complete_Event,
                hci.HCI_Read_Remote_Version_Information_Complete_Event,
            ),
        ):
            if not self.controller.event_mask & (1 << (packet.event_code - 1)):
                return
        elif isinstance(packet, hci.HCI_Authenticated_Payload_Timeout_Expired_Event):
            if not self.controller.event_mask_page_2 & (
                1 << (packet.event_code - hci.HCI_EVENT_MASK_PAGE_2_CODE_BASE)
            ):
                return
        self.controller.send_hci_packet(packet)

    def on_scan_timeout(self):
        self._emit_hci_packet(hci.HCI_LE_Scan_Timeout_Event())

    def on_scan_period(self):
        if self.period_duplicate_reset:
            self.seen.clear()

    def on_long_term_key_request(self, connection: ConnectionSnapshot, rand, ediv):
        self._emit_hci_packet(
            hci.HCI_LE_Long_Term_Key_Request_Event(connection.handle, rand, ediv)
        )

    def on_encryption_changed(self, connection: ConnectionSnapshot, status, refresh):
        if refresh:
            self._emit_hci_packet(
                hci.HCI_Encryption_Key_Refresh_Complete_Event(status, connection.handle)
            )
        else:
            self._emit_hci_packet(
                hci.HCI_Encryption_Change_Event(
                    status,
                    connection.handle,
                    (
                        hci.HCI_Encryption_Change_Event.Enabled.OFF
                        if status
                        else hci.HCI_Encryption_Change_Event.Enabled.E0_OR_AES_CCM
                    ),
                )
            )

    def on_authenticated_payload_timeout(self, connection: ConnectionSnapshot):
        self._emit_hci_packet(
            hci.HCI_Authenticated_Payload_Timeout_Expired_Event(connection.handle)
        )

    def on_remote_features(self, connection: ConnectionSnapshot, status):
        self._emit_hci_packet(
            hci.HCI_LE_Read_Remote_Features_Complete_Event(
                status=status,
                connection_handle=connection.handle,
                le_features=(connection.peer_features or 0).to_bytes(
                    PDU_CONTROL_FEATURE_SET_SIZE, 'little'
                ),
            )
        )

    def on_peer_sca(self, connection: ConnectionSnapshot, status: int, sca: int):
        """Report the peer's sleep clock accuracy (Core Vol 4, Part E, 7.7.65.31)."""
        self._emit_hci_packet(
            hci.HCI_LE_Request_Peer_SCA_Complete_Event(status, connection.handle, sca)
        )

    def on_remote_features_all(
        self,
        connection: ConnectionSnapshot,
        status: int,
        maximum: int,
        valid: int,
        features: bytes,
    ) -> None:
        self._emit_hci_packet(
            hci.HCI_LE_Read_All_Remote_Features_Complete_Event(
                status, connection.handle, maximum, valid, features
            )
        )

    @property
    def local_version(self) -> tuple[int, int, int]:
        return (
            self.controller.config.lmp_version,
            self.controller.config.manufacturer,
            self.controller.config.lmp_subversion,
        )

    def on_remote_version(self, connection: ConnectionSnapshot, status):
        version, manufacturer, subversion = connection.peer_version or (
            0,
            0,
            0,
        )
        self._emit_hci_packet(
            hci.HCI_Read_Remote_Version_Information_Complete_Event(
                status, connection.handle, version, manufacturer, subversion
            )
        )

    def on_connection_updated(self, connection: ConnectionSnapshot, status):
        parameters = connection.parameters
        self._emit_hci_packet(
            hci.HCI_LE_Connection_Update_Complete_Event(
                status=status,
                connection_handle=connection.handle,
                connection_interval=parameters.interval,
                peripheral_latency=parameters.latency,
                supervision_timeout=parameters.timeout,
            )
        )

    def on_subrate_change(
        self,
        connection: ConnectionSnapshot,
        status: int,
        factor: int,
        latency: int,
        continuation: int,
        timeout: int,
    ) -> None:
        """Report the Subrate Update result (Core Vol 4, Part E, 7.7.65.35)."""
        self._emit_hci_packet(
            hci.HCI_LE_Subrate_Change_Event(
                status, connection.handle, factor, latency, continuation, timeout
            )
        )

    def on_connection_rate_changed(
        self,
        connection: ConnectionSnapshot,
        status: int,
        interval: int,
        factor: int,
        latency: int,
        continuation: int,
        timeout: int,
    ) -> None:
        """Report the Connection Rate result (Core Vol 4, Part E, 7.7.65.50)."""
        self._emit_hci_packet(
            hci.HCI_LE_Connection_Rate_Change_Event(
                status,
                connection.handle,
                interval,
                factor,
                latency,
                continuation,
                timeout,
            )
        )

    def on_frame_space_updated(
        self,
        connection: ConnectionSnapshot,
        status: int,
        initiator: int,
        space: int,
        phys: int,
        spacing: int,
    ) -> None:
        """Report negotiated frame space (Core Vol 4, Part E, 7.7.65.48)."""
        self._emit_hci_packet(
            hci.HCI_LE_Frame_Space_Update_Complete_Event(
                status, connection.handle, initiator, space, phys, spacing
            )
        )

    def on_remote_parameters(
        self, connection: ConnectionSnapshot, minimum, maximum, latency, timeout
    ):
        event = hci.HCI_LE_Remote_Connection_Parameter_Request_Event(
            connection_handle=connection.handle,
            interval_min=minimum,
            interval_max=maximum,
            max_latency=latency,
            timeout=timeout,
        )
        if self.controller.le_event_mask & (1 << (event.subevent_code - 1)):
            self._emit_hci_packet(event)
        else:
            self.ll.reject_parameters(
                connection.handle, hci.HCI_ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR
            )

    def on_phy_updated(self, connection: ConnectionSnapshot, status):
        self._emit_hci_packet(
            hci.HCI_LE_PHY_Update_Complete_Event(
                status=status,
                connection_handle=connection.handle,
                tx_phy=connection.tx_phy,
                rx_phy=connection.rx_phy,
            )
        )

    def on_data_length_changed(self, connection: ConnectionSnapshot):
        lengths = connection.data_lengths
        self._emit_hci_packet(
            hci.HCI_LE_Data_Length_Change_Event(
                connection_handle=connection.handle,
                max_tx_octets=lengths.tx_octets,
                max_tx_time=lengths.tx_time,
                max_rx_octets=lengths.rx_octets,
                max_rx_time=lengths.rx_time,
            )
        )

    def on_transmit_power_reporting(
        self, connection: ConnectionSnapshot, status, reason, phy, power, flags, delta
    ):
        """Report local or remote transmit power (Core Vol 4, Part E, 7.7.65.33)."""
        self._emit_hci_packet(
            hci.HCI_LE_Transmit_Power_Reporting_Event(
                status, connection.handle, reason, phy, power, flags, delta
            )
        )

    def on_path_loss_threshold(self, connection: ConnectionSnapshot, path_loss, zone):
        """Report a path loss zone change (Core Vol 4, Part E, 7.7.65.32)."""
        self._emit_hci_packet(
            hci.HCI_LE_Path_Loss_Threshold_Event(connection.handle, path_loss, zone)
        )

    def on_connected(self, connection: ConnectionSnapshot):
        """LE (Enhanced) Connection Complete and Channel Selection Algorithm events (Vol
        4, Part E, 7.7.65.1, 7.7.65.10, 7.7.65.20).
        """
        parameters = connection.parameters
        self.controller.le_connections[connection.peer] = ConnectionRecord(
            self.controller, connection
        )
        self._emit_hci_packet(
            self._connection_complete_event(
                status=SUCCESS,
                connection_handle=connection.handle,
                role=connection.role,
                peer_address_type=connection.peer.address_type,
                peer_address=connection.peer,
                peer_resolvable_private_address=connection.peer_rpa or hci.Address.ANY,
                local_resolvable_private_address=connection.local_rpa
                or hci.Address.ANY,
                connection_interval=parameters.interval,
                peripheral_latency=parameters.latency,
                supervision_timeout=parameters.timeout,
                central_clock_accuracy=parameters.sca,
                advertising_handle=connection.pawr_advertising_handle,
                sync_handle=connection.pawr_sync_handle,
            )
        )
        if (
            connection.role == hci.Role.CENTRAL
            or connection.advertising_handle is not None
            or connection.pawr_sync_handle is not None
        ):
            self._emit_hci_packet(
                hci.HCI_LE_Channel_Selection_Algorithm_Event(
                    connection_handle=connection.handle,
                    channel_selection_algorithm=int(connection.csa2),
                )
            )
        if connection.advertising_handle is not None:
            self._emit_hci_packet(
                hci.HCI_LE_Advertising_Set_Terminated_Event(
                    status=SUCCESS,
                    advertising_handle=connection.advertising_handle,
                    connection_handle=connection.handle,
                    num_completed_extended_advertising_events=min(
                        HCI_LE_ADV_COMPLETED_EVENTS_MAX, connection.advertising_events
                    ),
                )
            )

    def on_disconnected(self, connection: ConnectionSnapshot, reason):
        """Disconnection Complete (Vol 4, Part E, 7.7.5)."""
        record = self.controller.le_connections.pop(connection.peer, None)
        if record is not None:
            record.disconnected(connection)
        if connection.reported:
            self._emit_hci_packet(
                hci.HCI_Disconnection_Complete_Event(
                    status=SUCCESS, connection_handle=connection.handle, reason=reason
                )
            )
        else:
            self._emit_hci_packet(
                self._connection_complete_event(
                    status=hci.HCI_ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR,
                    connection_handle=0,
                    role=connection.role,
                    peer_address_type=connection.peer.address_type,
                    peer_address=connection.peer,
                    connection_interval=0,
                    peripheral_latency=0,
                    supervision_timeout=0,
                    central_clock_accuracy=0,
                )
            )

    def on_acl_received(self, handle: int, continuation, data):
        self._emit_hci_packet(
            hci.HCI_AclDataPacket(
                connection_handle=handle,
                pb_flag=(
                    hci.HCI_ACL_PB_CONTINUATION
                    if continuation
                    else hci.HCI_ACL_PB_FIRST_FLUSHABLE
                ),
                bc_flag=0,
                data_total_length=len(data),
                data=data,
            )
        )

    def _connection_complete_event(self, **parameters):
        """The newest connection complete event the Host unmasked: Enhanced [v2],
        Enhanced [v1], then LE Connection Complete (Vol 4, Part E, 7.7.65.1,
        7.7.65.10).
        """
        local_rpa = parameters.pop('local_resolvable_private_address', hci.Address.ANY)
        peer_rpa = parameters.pop('peer_resolvable_private_address', hci.Address.ANY)
        advertising_handle = parameters.pop('advertising_handle', None)
        sync_handle = parameters.pop('sync_handle', None)
        if self._le_event_unmasked(hci.HCI_LE_ENHANCED_CONNECTION_COMPLETE_V2_EVENT):
            return hci.HCI_LE_Enhanced_Connection_Complete_V2_Event(
                **parameters,
                local_resolvable_private_address=local_rpa,
                peer_resolvable_private_address=peer_rpa,
                advertising_handle=(
                    HCI_LE_NO_ADVERTISING_HANDLE
                    if advertising_handle is None
                    else advertising_handle
                ),
                sync_handle=HCI_LE_NO_SYNC_HANDLE
                if sync_handle is None
                else sync_handle,
            )
        if self._le_event_unmasked(hci.HCI_LE_ENHANCED_CONNECTION_COMPLETE_EVENT):
            return hci.HCI_LE_Enhanced_Connection_Complete_Event(
                **parameters,
                local_resolvable_private_address=local_rpa,
                peer_resolvable_private_address=peer_rpa,
            )
        parameters['peer_address_type'] = (
            hci.AddressType.PUBLIC_DEVICE
            if parameters['peer_address_type']
            in (hci.AddressType.PUBLIC_DEVICE, hci.AddressType.PUBLIC_IDENTITY)
            else hci.AddressType.RANDOM_DEVICE
        )
        return hci.HCI_LE_Connection_Complete_Event(**parameters)

    def _le_event_unmasked(self, subevent_code: int) -> bool:
        return bool(self.controller.le_event_mask & (1 << (subevent_code - 1)))

    def on_connect_failed(self, peer, status):
        """An initiation the Link Layer gave up on, such as an unanswered PAwR
        AUX_CONNECT_REQ (Vol 4, Part E, 7.7.65.10).
        """
        self._emit_hci_packet(
            self._connection_complete_event(
                status=status,
                connection_handle=0,
                role=hci.Role.CENTRAL,
                peer_address_type=peer.address_type,
                peer_address=peer,
                connection_interval=0,
                peripheral_latency=0,
                supervision_timeout=0,
                central_clock_accuracy=0,
            )
        )

    def on_acl_completed(self, handle: int):
        """Number Of Completed Packets (Vol 4, Part E, 7.7.19)."""
        self._emit_hci_packet(
            hci.HCI_Number_Of_Completed_Packets_Event(
                connection_handles=[handle], num_completed_packets=[1]
            )
        )

    def _advertising_rssi(self, rssi: float | None) -> int:
        return (
            self.ll.reported_rssi(rssi)
            if rssi is not None
            else AdvertisingReport.RSSI_NOT_AVAILABLE
        )

    def on_advertisement(
        self,
        address,
        data,
        rssi,
        report,
        sid,
        interval,
        tx_power,
        *,
        target=None,
        primary_phy=Phy.LE_1M,
        secondary_phy=Phy.LE_1M,
    ):
        """Convert an LL report into filtered, size-bounded Host events.

        See Bluetooth Core 6.3, Vol 4, Part E, 7.7.65.2 and 7.7.65.13.
        """
        if not self.ll.scanning_enabled:
            return
        # Map LL receive properties to Vol 4, Part E event encoding here.
        properties = report.properties
        event_type = (
            sum(
                int(wire_flag)
                for ll_flag, wire_flag in (
                    (
                        AdvertisingProperties.CONNECTABLE_ADVERTISING,
                        ReportFlags.CONNECTABLE_ADVERTISING,
                    ),
                    (
                        AdvertisingProperties.SCANNABLE_ADVERTISING,
                        ReportFlags.SCANNABLE_ADVERTISING,
                    ),
                    (
                        AdvertisingProperties.DIRECTED_ADVERTISING,
                        ReportFlags.DIRECTED_ADVERTISING,
                    ),
                    (AdvertisingProperties.SCAN_RESPONSE, ReportFlags.SCAN_RESPONSE),
                    (
                        AdvertisingProperties.LEGACY_ADVERTISING_PDU_USED,
                        ReportFlags.LEGACY_ADVERTISING_PDU_USED,
                    ),
                )
                if properties & ll_flag
            )
            | int(report.data_status) << HCI_LE_EXT_ADV_REPORT_DATA_STATUS_SHIFT
        )
        sid = sid if sid is not None else AdvertisingReport.NO_ADI_FIELD_PROVIDED
        tx_power = (
            tx_power
            if tx_power is not None
            else AdvertisingReport.TX_POWER_INFORMATION_NOT_AVAILABLE
        )
        key = (address, data, event_type, sid)
        if self.filter_duplicates:
            # Filter the logical report before HCI fragmentation, so one fragment
            # cannot suppress the rest. TODO: use ADI in the duplicate key
            # (Vol 6, Part B, 4.4.3.5; see specification-references.md).
            if key in self.seen:
                return
            self.seen.add(key)
        rssi = self._advertising_rssi(rssi)
        if (
            not self.extended_scan
            and target is not None
            and target.address_type == hci.AddressType.UNABLE_TO_RESOLVE
        ):
            # 7.7.65.11 uses a dedicated legacy event for an unresolved RPA
            # TargetA; its Direct_Address_Type is Random, not the extended
            # report's 0xFE "unable to resolve" metadata value.
            self._emit_hci_packet(
                hci.HCI_LE_Directed_Advertising_Report_Event(
                    [
                        hci.HCI_LE_Directed_Advertising_Report_Event.Report(
                            event_type=hci.HCI_LE_Advertising_Report_Event.EventType.ADV_DIRECT_IND,
                            address_type=address.address_type,
                            address=address,
                            direct_address_type=hci.AddressType.RANDOM_DEVICE,
                            direct_address=hci.Address(
                                bytes(target), hci.AddressType.RANDOM_DEVICE
                            ),
                            rssi=rssi,
                        )
                    ]
                )
            )
            return
        if self.extended_scan:
            if (
                self.ll.features
                & hci.LeFeatureMask.ADVERTISING_CODING_SELECTION_HOST_SUPPORT
            ):
                if (
                    primary_phy == Phy.LE_CODED
                    and report.primary_coding == PHY_CODED_S2_FACTOR
                ):
                    primary_phy = AdvertisingReport.LE_CODED_S2
                if (
                    secondary_phy == Phy.LE_CODED
                    and report.secondary_coding == PHY_CODED_S2_FACTOR
                ):
                    secondary_phy = AdvertisingReport.LE_CODED_S2
            for offset in range(
                0, max(1, len(data)), HCI_LE_EXT_ADV_REPORT_DATA_SIZE_MAX
            ):
                fragment = data[offset : offset + HCI_LE_EXT_ADV_REPORT_DATA_SIZE_MAX]
                # Intermediate HCI fragments advertise "more to come". The final
                # fragment preserves LL's complete/truncated status, including empty
                # data.
                status = (
                    AdvertisingReport.DATA_INCOMPLETE_MORE_TO_COME
                    if offset + HCI_LE_EXT_ADV_REPORT_DATA_SIZE_MAX < len(data)
                    else (event_type >> HCI_LE_EXT_ADV_REPORT_DATA_STATUS_SHIFT)
                    & HCI_LE_EXT_ADV_REPORT_DATA_STATUS_VALUE_MASK
                )
                self._emit_hci_packet(
                    hci.HCI_LE_Extended_Advertising_Report_Event(
                        [
                            hci.HCI_LE_Extended_Advertising_Report_Event.Report(
                                event_type=(
                                    event_type & ~HCI_LE_EXT_ADV_REPORT_DATA_STATUS_MASK
                                )
                                | status << HCI_LE_EXT_ADV_REPORT_DATA_STATUS_SHIFT,
                                address_type=address.address_type,
                                address=address,
                                primary_phy=primary_phy,
                                secondary_phy=(
                                    0
                                    if event_type
                                    & ReportFlags.LEGACY_ADVERTISING_PDU_USED
                                    else secondary_phy
                                ),
                                advertising_sid=sid,
                                tx_power=tx_power,
                                rssi=rssi,
                                periodic_advertising_interval=interval,
                                direct_address_type=(
                                    target.address_type if target else 0
                                ),
                                direct_address=target or hci.Address.ANY,
                                data=fragment,
                            )
                        ]
                    )
                )
        elif event_type & ReportFlags.LEGACY_ADVERTISING_PDU_USED:
            # Legacy reports cannot represent extended PDUs or their PHY/SID fields.
            legacy_type = (
                hci.HCI_LE_Advertising_Report_Event.EventType.SCAN_RSP
                if event_type & ReportFlags.SCAN_RESPONSE
                else AdvertisingReport.LEGACY_PDU_TYPE_MAP[
                    event_type & HCI_LE_EXT_ADV_REPORT_LEGACY_PROPERTIES_MASK
                ]
            )
            self._emit_hci_packet(
                hci.HCI_LE_Advertising_Report_Event(
                    [
                        hci.HCI_LE_Advertising_Report_Event.Report(
                            event_type=legacy_type,
                            address_type=address.address_type,
                            address=address,
                            data=data,
                            rssi=rssi,
                        )
                    ]
                )
            )

    def on_advertising_ended(self, item: AdvertisingSnapshot, status):
        """LE Advertising Set Terminated, or legacy completion (Vol 4, Part E,
        7.7.65.18).
        """
        handle = item.handle
        if handle == -1:
            self._emit_hci_packet(
                self._connection_complete_event(
                    status=status,
                    connection_handle=0,
                    role=hci.Role.PERIPHERAL,
                    peer_address_type=item.target.address_type if item.target else 0,
                    peer_address=item.target or hci.Address.ANY,
                    connection_interval=0,
                    peripheral_latency=0,
                    supervision_timeout=0,
                    central_clock_accuracy=0,
                )
            )
        else:
            self._emit_hci_packet(
                hci.HCI_LE_Advertising_Set_Terminated_Event(
                    status=status,
                    advertising_handle=handle,
                    connection_handle=0,
                    num_completed_extended_advertising_events=min(
                        HCI_LE_ADV_COMPLETED_EVENTS_MAX, item.events
                    ),
                )
            )

    def on_monitored_advertiser(self, address, condition):
        """LE Monitored Advertisers Report (Vol 4, Part E, 7.7.65.47)."""
        self._emit_hci_packet(
            hci.HCI_LE_Monitored_Advertisers_Report_Event(
                address.address_type, address, condition
            )
        )

    def on_scan_request(self, item: AdvertisingSnapshot, peer):
        handle = item.handle
        self._emit_hci_packet(
            hci.HCI_LE_Scan_Request_Received_Event(
                advertising_handle=handle,
                scanner_address_type=peer.address_type,
                scanner_address=peer,
            )
        )

    def on_sync_established(self, sync: PeriodicSyncSnapshot, status):
        """LE Periodic Advertising Sync Established [v2] or [v1], the newest one
        unmasked (Vol 4, Part E, 7.7.65.14). A failure reports Sync_Handle zero.
        """
        parameters = dict(
            status=status,
            sync_handle=sync.handle if status == SUCCESS else 0,
            advertising_sid=sync.sid,
            advertiser_address_type=sync.address.address_type,
            advertiser_address=sync.address,
            advertiser_phy=sync.phy,
            periodic_advertising_interval=sync.interval,
            advertiser_clock_accuracy=sync.sca,
        )
        self._emit_hci_packet(
            hci.HCI_LE_Periodic_Advertising_Sync_Established_V2_Event(
                **parameters, **self._pawr_timing(sync)
            )
            if self._le_event_unmasked(
                hci.HCI_LE_PERIODIC_ADVERTISING_SYNC_ESTABLISHED_V2_EVENT
            )
            else hci.HCI_LE_Periodic_Advertising_Sync_Established_Event(**parameters)
        )

    @staticmethod
    def _pawr_timing(sync: PeriodicSyncSnapshot) -> dict[str, int]:
        """[v2] PAwR fields; zero for a train without responses."""
        timing = sync.pawr
        return dict(
            num_subevents=timing.num_subevents if timing else 0,
            subevent_interval=timing.subevent_interval if timing else 0,
            response_slot_delay=timing.response_slot_delay if timing else 0,
            response_slot_spacing=timing.response_slot_spacing if timing else 0,
        )

    def on_sync_transfer_received(self, sync: PeriodicSyncSnapshot, status):
        """LE Periodic Advertising Sync Transfer Received [v2] or [v1], the newest
        one unmasked (Vol 4, Part E, 7.7.65.24). As for Sync Established, a
        failure reports Sync_Handle zero.
        """
        parameters = dict(
            status=status,
            connection_handle=sync.connection_handle,
            service_data=sync.service_data,
            sync_handle=sync.handle if status == SUCCESS else 0,
            advertising_sid=sync.sid,
            advertiser_address_type=sync.address.address_type,
            advertiser_address=sync.address,
            advertiser_phy=sync.phy,
            periodic_advertising_interval=sync.interval,
            advertiser_clock_accuracy=sync.sca,
        )
        self._emit_hci_packet(
            hci.HCI_LE_Periodic_Advertising_Sync_Transfer_Received_V2_Event(
                **parameters, **self._pawr_timing(sync)
            )
            if self._le_event_unmasked(
                hci.HCI_LE_PERIODIC_ADVERTISING_SYNC_TRANSFER_RECEIVED_V2_EVENT
            )
            else hci.HCI_LE_Periodic_Advertising_Sync_Transfer_Received_Event(
                **parameters
            )
        )

    def on_periodic_report(
        self,
        sync: PeriodicSyncSnapshot,
        data,
        rssi,
        tx_power,
        data_status=AdvertisingReport.DATA_COMPLETE,
        *,
        counter=0,
        subevent=None,
    ):
        """LE Periodic Advertising Report [v2] or [v1], fragmented by Data_Status
        (Vol 4, Part E, 7.7.65.15).

        [v2] adds the event counter and subevent, and reports a PAwR subevent
        listened to in vain; [v1] can represent neither.
        """
        v2 = self._le_event_unmasked(hci.HCI_LE_PERIODIC_ADVERTISING_REPORT_V2_EVENT)
        if not v2 and data_status == DataStatus.RECEIVE_FAILED:
            return
        tx_power = (
            tx_power
            if tx_power is not None
            else AdvertisingReport.TX_POWER_INFORMATION_NOT_AVAILABLE
        )
        rssi = self._advertising_rssi(rssi)
        size = (
            HCI_LE_PERIODIC_REPORT_V2_DATA_SIZE_MAX
            if v2
            else HCI_LE_PERIODIC_REPORT_DATA_SIZE_MAX
        )
        for offset in range(0, max(1, len(data)), size):
            parameters = dict(
                sync_handle=sync.handle,
                tx_power=tx_power,
                rssi=rssi,
                cte_type=HCI_LE_PERIODIC_REPORT_CTE_NONE,
                data_status=(
                    AdvertisingReport.DATA_INCOMPLETE_MORE_TO_COME
                    if offset + size < len(data)
                    else data_status
                ),
                data=data[offset : offset + size],
            )
            self._emit_hci_packet(
                hci.HCI_LE_Periodic_Advertising_Report_V2_Event(
                    **parameters,
                    periodic_event_counter=counter,
                    subevent=PeriodicReportV2.NO_SUBEVENT
                    if subevent is None
                    else subevent,
                )
                if v2
                else hci.HCI_LE_Periodic_Advertising_Report_Event(**parameters)
            )

    def on_subevent_data_request(self, item: AdvertisingSnapshot, start, count):
        """LE Periodic Advertising Subevent Data Request (Vol 4, Part E, 7.7.65.36)."""
        self._emit_hci_packet(
            hci.HCI_LE_Periodic_Advertising_Subevent_Data_Request_Event(
                advertising_handle=item.handle,
                subevent_start=start,
                subevent_data_count=count,
            )
        )

    def on_response_report(
        self, item: AdvertisingSnapshot, subevent, transmitted, responses
    ):
        """LE Periodic Advertising Response Report, one per requested slot (Vol 4,
        Part E, 7.7.65.37).

        Responses share an event while its parameters fit; a longer response is
        split, with "more data to come" before its last part.
        """
        reports = []
        for response in responses:
            if response.data is None:
                reports.append((response, b'', ResponseReport.DATA_RECEIVE_FAILED))
                continue
            data = response.data
            size = (
                HCI_EVENT_PARAMETER_SIZE_MAX
                - HCI_LE_RESPONSE_REPORT_OVERHEAD_SIZE
                - HCI_LE_RESPONSE_REPORT_RESPONSE_SIZE
            )
            for offset in range(0, max(1, len(data)), size):
                reports.append(
                    (
                        response,
                        data[offset : offset + size],
                        AdvertisingReport.DATA_INCOMPLETE_MORE_TO_COME
                        if offset + size < len(data)
                        else AdvertisingReport.DATA_COMPLETE,
                    )
                )
        batch: list = []
        used = HCI_LE_RESPONSE_REPORT_OVERHEAD_SIZE
        for response, data, status in reports:
            needed = HCI_LE_RESPONSE_REPORT_RESPONSE_SIZE + len(data)
            if batch and (
                used + needed > HCI_EVENT_PARAMETER_SIZE_MAX
                or len(batch) == HCI_LE_RESPONSE_REPORT_RESPONSES_MAX
            ):
                self._response_report(item, subevent, transmitted, batch)
                batch, used = [], HCI_LE_RESPONSE_REPORT_OVERHEAD_SIZE
            batch.append(
                ResponseReport.Response(
                    tx_power=(
                        response.tx_power
                        if response.tx_power is not None
                        else AdvertisingReport.TX_POWER_INFORMATION_NOT_AVAILABLE
                    ),
                    rssi=self._advertising_rssi(
                        response.rssi if response.data is not None else None
                    ),
                    cte_type=HCI_LE_PERIODIC_REPORT_CTE_NONE,
                    response_slot=response.slot,
                    data_status=status,
                    data=data,
                )
            )
            used += needed
        if batch:
            self._response_report(item, subevent, transmitted, batch)

    def _response_report(self, item, subevent, transmitted, responses):
        self._emit_hci_packet(
            ResponseReport(
                advertising_handle=item.handle,
                subevent=subevent,
                tx_status=0 if transmitted else ResponseReport.TX_FAILED,
                responses=responses,
            )
        )

    def on_sync_lost(self, sync: PeriodicSyncSnapshot):
        self._emit_hci_packet(
            hci.HCI_LE_Periodic_Advertising_Sync_Lost_Event(sync_handle=sync.handle)
        )

    def on_cis_request(
        self, acl_handle: int, cis_handle: int, cig_id: int, cis_id: int
    ) -> bool:
        """LE CIS Request (Vol 4, Part E, 7.7.65.26)."""
        if not self.controller.event_mask & (1 << (hci.HCI_LE_META_EVENT - 1)):
            return False
        if not self._le_event_unmasked(hci.HCI_LE_CIS_REQUEST_EVENT):
            return False
        self._emit_hci_packet(
            hci.HCI_LE_CIS_Request_Event(acl_handle, cis_handle, cig_id, cis_id)
        )
        return True

    def on_cis_established(self, result: CisEstablished, status: int):
        """LE CIS Established (Vol 4, Part E, 7.7.65.25)."""
        parameters = dict(
            status=status,
            connection_handle=result.connection_handle,
            cig_sync_delay=result.cig_sync_delay,
            cis_sync_delay=result.cis_sync_delay,
            transport_latency_c_to_p=result.transport_latency_c_to_p,
            transport_latency_p_to_c=result.transport_latency_p_to_c,
            phy_c_to_p=result.phy_c_to_p,
            phy_p_to_c=result.phy_p_to_c,
            nse=result.nse,
            bn_c_to_p=result.bn_c_to_p,
            bn_p_to_c=result.bn_p_to_c,
            ft_c_to_p=result.ft_c_to_p,
            ft_p_to_c=result.ft_p_to_c,
            max_pdu_c_to_p=result.max_pdu_c_to_p,
            max_pdu_p_to_c=result.max_pdu_p_to_c,
            iso_interval=result.iso_interval,
        )
        if self._le_event_unmasked(hci.HCI_LE_CIS_ESTABLISHED_V2_EVENT):
            event = hci.HCI_LE_CIS_Established_V2_Event(
                **parameters,
                sub_interval=result.sub_interval,
                max_sdu_c_to_p=result.max_sdu_c_to_p,
                max_sdu_p_to_c=result.max_sdu_p_to_c,
                sdu_interval_c_to_p=result.sdu_interval_c_to_p,
                sdu_interval_p_to_c=result.sdu_interval_p_to_c,
                framing=result.framing,
            )
        else:
            event = hci.HCI_LE_CIS_Established_Event(**parameters)
        self._emit_hci_packet(event)

    def on_cis_disconnected(self, cis_handle: int, reason: int):
        """Report CIS termination without removing its ACL record."""
        self._emit_hci_packet(
            hci.HCI_Disconnection_Complete_Event(SUCCESS, cis_handle, reason)
        )

    @staticmethod
    def _big_event_parameters(group: BigSnapshot, status: int):
        """Fields shared by Create BIG Complete and BIG Sync Established."""
        info = group.info
        return dict(
            status=status,
            big_handle=group.handle,
            transport_latency_big=info.transport_latency_us if info else 0,
            nse=info.nse if info else 0,
            bn=info.bn if info else 0,
            pto=info.pto if info else 0,
            irc=info.irc if info else 0,
            max_pdu=info.max_pdu if info else 0,
            iso_interval=info.iso_interval if info else 0,
            connection_handle=group.stream_handles if status == SUCCESS else (),
        )

    def on_big_created(self, group: BigSnapshot, status: int):
        """LE Create BIG Complete (Vol 4, Part E, 7.7.65.27)."""
        info = group.info
        self._emit_hci_packet(
            hci.HCI_LE_Create_BIG_Complete_Event(
                **self._big_event_parameters(group, status),
                big_sync_delay=info.sync_delay_us if info else 0,
                phy=int(info.phy) if info else 0,
            )
        )

    def on_big_terminated(self, handle: int, reason: int):
        """LE Terminate BIG Complete (Vol 4, Part E, 7.7.65.28)."""
        self._emit_hci_packet(hci.HCI_LE_Terminate_BIG_Complete_Event(handle, reason))

    def on_big_synchronized(self, group: BigSnapshot, status: int):
        """LE BIG Sync Established (Vol 4, Part E, 7.7.65.29)."""
        self._emit_hci_packet(
            hci.HCI_LE_BIG_Sync_Established_Event(
                **self._big_event_parameters(group, status)
            )
        )

    def on_big_lost(self, handle: int, reason: int):
        """LE BIG Sync Lost (Vol 4, Part E, 7.7.65.30)."""
        self._emit_hci_packet(hci.HCI_LE_BIG_Sync_Lost_Event(handle, reason))

    def on_big_info(self, sync_handle: int, info: BigInfo):
        """LE BIGInfo Advertising Report (Vol 4, Part E, 7.7.65.34)."""
        self._emit_hci_packet(
            hci.HCI_LE_BIGInfo_Advertising_Report_Event(
                sync_handle,
                info.num_bis,
                info.nse,
                info.iso_interval,
                info.bn,
                info.pto,
                info.irc,
                info.max_pdu,
                info.sdu_interval_us,
                info.max_sdu,
                int(info.phy),
                1 + info.framing_mode if info.framed else 0,
                int(info.encrypted),
            )
        )

    def on_iso_received(self, handle: int, sdu: IsoSdu):
        """Deliver one SDU as ordered HCI ISO fragments (Vol 4, Part E, 5.4.5)."""
        size = self.controller.config.iso_packet_length
        timestamped = size >= 9
        overhead = 8 if timestamped else 4
        packets = []
        offset = 0
        while True:
            first = offset == 0
            capacity = size - overhead if first else size
            fragment = sdu.data[offset : offset + capacity]
            offset += len(fragment)
            last = offset == len(sdu.data)
            packets.append(
                hci.HCI_IsoDataPacket(
                    connection_handle=handle,
                    data_total_length=len(fragment) + (overhead if first else 0),
                    iso_sdu_fragment=fragment,
                    pb_flag=2 if first and last else 0 if first else 3 if last else 1,
                    time_stamp=sdu.reference_us & 0xFFFFFFFF
                    if first and timestamped
                    else None,
                    packet_sequence_number=sdu.sequence_number if first else None,
                    iso_sdu_length=len(sdu.data) if first else None,
                    packet_status_flag=int(sdu.status) if first else None,
                )
            )
            if last:
                break
        self.controller.send_hci_packets(packets)

    def on_iso_completed(self, handle: int, count: int):
        self._emit_hci_packet(
            hci.HCI_Number_Of_Completed_Packets_Event([handle], [count])
        )
