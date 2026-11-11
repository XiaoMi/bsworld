# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Translate Link Layer notifications into Controller-owned HCI events."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bscontroller import hci
from bscontroller.hci import (
    HCI_LE_ADV_COMPLETED_EVENTS_MAX,
    HCI_LE_EXT_ADV_REPORT_DATA_SIZE_MAX,
    HCI_LE_EXT_ADV_REPORT_DATA_STATUS_MASK,
    HCI_LE_EXT_ADV_REPORT_DATA_STATUS_SHIFT,
    HCI_LE_EXT_ADV_REPORT_DATA_STATUS_VALUE_MASK,
    HCI_LE_EXT_ADV_REPORT_LEGACY_PROPERTIES_MASK,
    HCI_LE_PERIODIC_REPORT_CTE_NONE,
    HCI_LE_PERIODIC_REPORT_DATA_SIZE_MAX,
)
from bscontroller.hci.records import ConnectionRecord
from bscontroller.linklayer import PDU_CONTROL_FEATURE_SET_SIZE
from bscontroller.linklayer.api import (
    AdvertisingSnapshot,
    ConnectionSnapshot,
    PeriodicSyncSnapshot,
)
from bscontroller.linklayer.state import AdvertisingProperties
from bscontroller.radio.base import RADIO_TX_POWER_MAX_DBM, RADIO_TX_POWER_MIN_DBM, Phy

if TYPE_CHECKING:
    from bscontroller.hci.controller import Controller
    from bscontroller.linklayer.service import LinkLayerService

SUCCESS = hci.HCI_ErrorCode.SUCCESS
ReportFlags = hci.HCI_LE_Extended_Advertising_Report_Event.EventType
AdvertisingReport = hci.HCI_LE_Extended_Advertising_Report_Event


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
            )
        )
        if (
            connection.role == hci.Role.CENTRAL
            or connection.advertising_handle is not None
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

    def on_acl_received(self, connection: ConnectionSnapshot, continuation, data):
        self._emit_hci_packet(
            hci.HCI_AclDataPacket(
                connection_handle=connection.handle,
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
        local_rpa = parameters.pop('local_resolvable_private_address', hci.Address.ANY)
        peer_rpa = parameters.pop('peer_resolvable_private_address', hci.Address.ANY)
        if self.controller.le_event_mask & (
            1 << (hci.HCI_LE_ENHANCED_CONNECTION_COMPLETE_EVENT - 1)
        ):
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

    def on_acl_completed(self, connection: ConnectionSnapshot):
        """Number Of Completed Packets (Vol 4, Part E, 7.7.19)."""
        self._emit_hci_packet(
            hci.HCI_Number_Of_Completed_Packets_Event(
                connection_handles=[connection.handle], num_completed_packets=[1]
            )
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
        rssi = max(RADIO_TX_POWER_MIN_DBM, min(RADIO_TX_POWER_MAX_DBM, round(rssi)))
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
        """LE Periodic Advertising Sync Established (Vol 4, Part E, 7.7.65.14)."""
        self._emit_hci_packet(
            hci.HCI_LE_Periodic_Advertising_Sync_Established_Event(
                status=status,
                sync_handle=sync.handle if status == SUCCESS else 0,
                advertising_sid=sync.sid,
                advertiser_address_type=sync.address.address_type,
                advertiser_address=sync.address,
                advertiser_phy=sync.phy,
                periodic_advertising_interval=sync.interval,
                advertiser_clock_accuracy=sync.sca,
            )
        )

    def on_sync_transfer_received(self, sync: PeriodicSyncSnapshot, status):
        """LE Periodic Advertising Sync Transfer Received [v1] (Vol 4, Part E,
        7.7.65.24).

        Only v1 is generated: v2 describes PAwR, which this Controller does not
        support. As for Sync Established, a failure reports Sync_Handle zero.
        """
        self._emit_hci_packet(
            hci.HCI_LE_Periodic_Advertising_Sync_Transfer_Received_Event(
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
        )

    def on_periodic_report(
        self,
        sync: PeriodicSyncSnapshot,
        data,
        rssi,
        tx_power,
        data_status=AdvertisingReport.DATA_COMPLETE,
    ):
        """LE Periodic Advertising Report, fragmented by Data_Status (Vol 4, Part E,
        7.7.65.15).
        """
        tx_power = (
            tx_power
            if tx_power is not None
            else AdvertisingReport.TX_POWER_INFORMATION_NOT_AVAILABLE
        )
        for offset in range(0, max(1, len(data)), HCI_LE_PERIODIC_REPORT_DATA_SIZE_MAX):
            self._emit_hci_packet(
                hci.HCI_LE_Periodic_Advertising_Report_Event(
                    sync_handle=sync.handle,
                    tx_power=tx_power,
                    rssi=max(
                        RADIO_TX_POWER_MIN_DBM, min(RADIO_TX_POWER_MAX_DBM, round(rssi))
                    ),
                    cte_type=HCI_LE_PERIODIC_REPORT_CTE_NONE,
                    data_status=(
                        AdvertisingReport.DATA_INCOMPLETE_MORE_TO_COME
                        if offset + HCI_LE_PERIODIC_REPORT_DATA_SIZE_MAX < len(data)
                        else data_status
                    ),
                    data=data[offset : offset + HCI_LE_PERIODIC_REPORT_DATA_SIZE_MAX],
                )
            )

    def on_sync_lost(self, sync: PeriodicSyncSnapshot):
        self._emit_hci_packet(
            hci.HCI_LE_Periodic_Advertising_Sync_Lost_Event(sync_handle=sync.handle)
        )
