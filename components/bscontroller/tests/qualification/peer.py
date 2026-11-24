# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Independent connection peer for HCI-driven LL reference procedures."""

import struct
from collections import deque
from dataclasses import replace

import pytest

from bscontroller import hci
from bscontroller.linklayer import (
    LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
    LL_ADV_PRIMARY_CHANNELS,
    LL_CONN_AUX_WINDOW_DELAY_CODED_US,
    LL_CONN_AUX_WINDOW_DELAY_UNCODED_US,
    LL_CONN_INTERVAL_UNIT_US,
    LL_DATA_CHANNEL_COUNT,
    LL_EVENT_COUNTER_MASK,
    LL_IFS_US,
    LL_INSTANT_MIN_LISTENING_EVENTS,
    PDU_ADV_ADDRESS_SIZE,
    PDU_ADV_ADDRESSES_END,
    PDU_ADV_CHSEL_MASK,
    PDU_ADV_CONNECT_HOP_MASK,
    PDU_ADV_TYPE_MASK,
    PDU_CONTROL_FEATURE_SET_SIZE,
    PDU_CONTROL_INSTANT_SIZE,
    PDU_CONTROL_OPCODE_SIZE,
    PDU_CONTROL_PHY_REQ_SIZE,
    PDU_CONTROL_PHY_UPDATE_SIZE,
    PDU_DATA_LLID_MASK,
    PDU_DATA_NESN_MASK,
    PDU_DATA_PAYLOAD_SIZE_MIN,
    PDU_DATA_PAYLOAD_TIME_MIN_US,
    PDU_DATA_SN_MASK,
    PDU_HEADER_SIZE,
    AdvertisingPduType,
    ControlOpcode,
    LlId,
)
from bscontroller.radio.base import PHY_CODED_S8_FACTOR, Phy
from tests.qualification.tester import (
    DUT_ADDRESS,
    PEER_ADDRESS,
    LinkParameters,
    adv_pdu,
    create_connection,
    csa2_channel,
    extended_enable,
    extended_parameters,
    legacy_parameters,
    receive_extended,
    request_pdu,
)

AdvType = hci.HCI_LE_Set_Advertising_Parameters_Command.AdvertisingType
Opcode = ControlOpcode
ErrorCode = hci.HCI_ErrorCode

# Optional Lower Tester procedures (LL.TS 4.1.6.7.3).
PEER_FEATURES = (
    hci.LeFeatureMask.LE_ENCRYPTION
    | hci.LeFeatureMask.EXTENDED_REJECT_INDICATION
    | hci.LeFeatureMask.PERIPHERAL_INITIATED_FEATURE_EXCHANGE
    | hci.LeFeatureMask.LE_DATA_PACKET_LENGTH_EXTENSION
    | hci.LeFeatureMask.LE_2M_PHY
    | hci.LeFeatureMask.LE_CODED_PHY
    | hci.LeFeatureMask.LE_EXTENDED_ADVERTISING
    | hci.LeFeatureMask.LE_PERIODIC_ADVERTISING
    | hci.LeFeatureMask.CHANNEL_SELECTION_ALGORITHM_2
    | hci.LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_SENDER
    | hci.LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_RECIPIENT
    | hci.LeFeatureMask.PERIODIC_ADVERTISING_ADI_SUPPORT
)

LONG_CONTROL_FEATURES = (
    hci.LeFeatureMask.LE_DATA_PACKET_LENGTH_EXTENSION
    | hci.LeFeatureMask.LE_2M_PHY
    | hci.LeFeatureMask.LE_CODED_PHY
    | hci.LeFeatureMask.PERIPHERAL_INITIATED_FEATURE_EXCHANGE
    | hci.LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_RECIPIENT
    | hci.LeFeatureMask.PERIODIC_ADVERTISING_WITH_RESPONSES_SCANNER
)

Properties = (
    hci.HCI_LE_Set_Extended_Advertising_Parameters_Command.AdvertisingProperties
)


async def host_send(
    tester,
    link,
    data,
    continuation=False,
    *,
    peer_data=None,
    peer_continuation=False,
    receiver=None,
):
    boundary = (
        hci.HCI_ACL_PB_CONTINUATION
        if continuation
        else hci.HCI_ACL_PB_FIRST_NON_FLUSHABLE
    )
    await tester.wait(0)
    tester.send(bytes(hci.HCI_AclDataPacket(link.handle, boundary, 0, len(data), data)))
    received = b''
    fragments = 0
    for attempt in range(32):
        sn = link.nesn
        packet = await link.exchange(
            peer_data if peer_data is not None and not attempt else b'',
            llid=(
                LlId.CONTINUATION
                if peer_continuation or peer_data is None or attempt
                else LlId.START
            ),
        )
        if packet.pdu[0] & PDU_DATA_LLID_MASK != LlId.CONTROL and packet.pdu[1]:
            assert int(bool(packet.pdu[0] & PDU_DATA_SN_MASK)) == sn
            assert packet.pdu[0] & PDU_DATA_LLID_MASK == (
                LlId.CONTINUATION if continuation or received else LlId.START
            )
            received += packet.pdu[PDU_HEADER_SIZE:]
            fragments += 1
        if len(received) == len(data):
            break
    assert received == data
    await link.exchange()
    event = await tester.event(hci.HCI_Number_Of_Completed_Packets_Event)
    assert event.connection_handles == [
        link.handle
    ] and event.num_completed_packets == [1]
    if peer_data is not None:
        if receiver is None:
            await received_acl(tester, link, peer_data, peer_continuation)
        else:
            receiver.poll()
    return fragments


async def peer_send(tester, link, data, continuation=False, *, receiver=None):
    sn = link.sn
    packet = await link.exchange(
        data, llid=LlId.CONTINUATION if continuation else LlId.START
    )
    if link.central:
        # The Central's preceding PDU may acknowledge our previous empty packet.
        sn = link.sn
        packet = await link.exchange()
    assert int(bool(packet.pdu[0] & PDU_DATA_NESN_MASK)) != sn
    if receiver is None:
        await received_acl(tester, link, data, continuation)
    else:
        receiver.poll()
    return packet


async def received_acl(tester, link, data, continuation):
    """Preserve bytes and start boundaries without prescribing HCI fragmentation."""
    received = bytearray()
    start_seen = continuation
    while True:
        event = await tester.event(hci.HCI_AclDataPacket)
        assert event.connection_handle == link.handle
        assert event.bc_flag == 0 and event.data_total_length == len(event.data)
        if event.pb_flag == hci.HCI_ACL_PB_FIRST_FLUSHABLE:
            assert not continuation and not received, 'unexpected HCI start boundary'
            start_seen = True
        else:
            assert event.pb_flag == hci.HCI_ACL_PB_CONTINUATION and start_seen
        received.extend(event.data)
        assert data.startswith(received), 'HCI ACL payload mismatch'
        if len(received) == len(data):
            return


async def exchange_features(link, features):
    """Complete the Section 4.1.6 preamble for a test of optional features."""
    opcode = Opcode.LL_PERIPHERAL_FEATURE_REQ if link.central else Opcode.LL_FEATURE_REQ
    response = await link.control(
        Opcode.LL_FEATURE_RSP,
        bytes([opcode]) + features.to_bytes(PDU_CONTROL_FEATURE_SET_SIZE, 'little'),
    )
    assert len(response) == PDU_CONTROL_OPCODE_SIZE + PDU_CONTROL_FEATURE_SET_SIZE
    await link.exchange()


class PeerLink:
    def __init__(
        self,
        tester,
        parameters,
        connect_end,
        *,
        central=False,
        csa2=False,
        phy=Phy.LE_1M,
    ):
        self.tester = tester
        self.parameters = parameters
        self.central = central
        self.csa2 = csa2
        self.tx_phy = self.rx_phy = phy
        self.tx_coding = PHY_CODED_S8_FACTOR
        self.tx_power_dbm = 0
        self.counter = self.sn = self.nesn = 0
        self.anchor = connect_end + LL_CONN_INTERVAL_UNIT_US * (
            1 + parameters.window_offset
        )
        self.handle = None
        self.last_sent_end_us = None
        # Optional wire observer can queue control replies during another procedure.
        self.on_receive = None
        self.control_responses = deque()

    @property
    def channel(self):
        p = self.parameters
        if self.csa2:
            return csa2_channel(self.counter, p.access_address, p.channel_map)
        channel = (
            (self.counter + 1)
            * (p.hop_sca & PDU_ADV_CONNECT_HOP_MASK)
            % LL_DATA_CHANNEL_COUNT
        )
        used = [
            index
            for index in range(LL_DATA_CHANNEL_COUNT)
            if p.channel_map & (1 << index)
        ]
        return channel if channel in used else used[channel % len(used)]

    @property
    def radio_parameters(self):
        return {
            'access_address': self.parameters.access_address,
            'crc_init': self.parameters.crc_init,
            'phy': self.tx_phy,
        }

    async def exchange(
        self,
        data=b'',
        *,
        llid=LlId.CONTINUATION,
        reply=None,
        check_ack=False,
        required=True,
        window_us=0,
    ):
        channel = self.channel
        kwargs = self.radio_parameters
        if self.central:
            assert window_us >= 0
            receive = self.tester.expect_pdu if required else self.tester.receive
            response = await receive(
                channel,
                start_us=max(self.tester.radio.now_us + 1, self.anchor - 50),
                duration_us=max(
                    1000, self.anchor - self.tester.radio.now_us + window_us + 3000
                ),
                **(kwargs | {'phy': self.rx_phy}),
            )
            if response is None:
                self.anchor += self.parameters.interval * LL_CONN_INTERVAL_UNIT_US
                self.counter = (self.counter + 1) & LL_EVENT_COUNTER_MASK
                self.last_received = None
                return None
            assert self.anchor <= response.start_us <= self.anchor + window_us
            self.anchor = response.start_us
            response = self.decode(response)
            if check_ack and self.last_sent_end_us is not None:
                assert int(bool(response.pdu[0] & PDU_DATA_NESN_MASK)) != self.sn, (
                    'peer PDU not acknowledged'
                )
            self._accept(response)
            if reply is not None:
                data, llid = reply(response)
            start = response.end_us + LL_IFS_US
        else:
            assert reply is None, 'only a peer Peripheral can reply to a received PDU'
            start = self.anchor
        sent_sn = self.sn
        await self._send(channel, data, llid, start)
        if not self.central:
            end = self.tester.radio.now_us + 1
            receive = self.tester.expect_pdu if required else self.tester.receive
            response = await receive(
                channel,
                duration_us=max(
                    1000,
                    self.rx_phy.airtime_us(PDU_HEADER_SIZE + PDU_DATA_PAYLOAD_SIZE_MIN),
                ),
                **(kwargs | {'phy': self.rx_phy}),
            )
            if response is not None:
                assert response.start_us == end + LL_IFS_US
                if check_ack:
                    assert int(bool(response.pdu[0] & PDU_DATA_NESN_MASK)) != sent_sn, (
                        'peer PDU not acknowledged'
                    )
                response = self.decode(response)
                self._accept(response)
        self.anchor += self.parameters.interval * LL_CONN_INTERVAL_UNIT_US
        self.counter = (self.counter + 1) & LL_EVENT_COUNTER_MASK
        self.last_received = response
        return response

    def _accept(self, packet):
        header = packet.pdu[0]
        if int(bool(header & PDU_DATA_NESN_MASK)) != self.sn:
            self.sn ^= 1
        if int(bool(header & PDU_DATA_SN_MASK)) == self.nesn:
            self.nesn ^= 1
        if (observer := self.on_receive) is not None:
            if response := observer(packet):
                self.control_responses.append(response)

    def decode(self, packet):
        return packet

    def encode(self, header, data):
        return data

    async def _send(self, channel, data, llid, start):
        if not data and self.control_responses:
            data, llid = self.control_responses.popleft(), LlId.CONTROL
        header = llid | self.sn * PDU_DATA_SN_MASK | self.nesn * PDU_DATA_NESN_MASK
        data = self.encode(header, data)
        pdu = (
            bytes(
                [
                    header,
                    len(data),
                ]
            )
            + data
        )
        await self.tester.transmit(
            channel,
            pdu,
            start_us=start,
            coding=self.tx_coding,
            power_dbm=self.tx_power_dbm,
            **self.radio_parameters,
        )
        self.last_sent_end_us = self.tester.radio.now_us + 1

    async def control(
        self, opcode, payload=b'', *, required=True, check_ack=False, response=None
    ):
        options = {}
        if response is not None:
            assert self.central and not payload and not check_ack

            def reply(packet):
                if (
                    packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
                    and packet.pdu[PDU_HEADER_SIZE] == opcode
                ):
                    return response, LlId.CONTROL
                return b'', LlId.CONTINUATION

            options['reply'] = reply
        packet = await self.exchange(
            payload,
            llid=LlId.CONTROL if payload else LlId.CONTINUATION,
            check_ack=check_ack,
            **options,
        )
        if self.central and payload and check_ack:
            sent_sn = self.sn
            packet = await self.exchange()
            assert int(bool(packet.pdu[0] & PDU_DATA_NESN_MASK)) != sent_sn, (
                'peer control PDU not acknowledged'
            )
        for _ in range(16):
            if (
                packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
                and packet.pdu[PDU_HEADER_SIZE] == opcode
            ):
                return packet.pdu[PDU_HEADER_SIZE:]
            packet = await self.exchange(**options)
        if required:
            raise AssertionError(f'LL control {opcode.name} not received')
        return None

    async def apply_update(
        self, payload, *, check_event_timing=False, forbidden_opcodes=()
    ):
        opcode = payload[0]
        instant = int.from_bytes(payload[-PDU_CONTROL_INSTANT_SIZE:], 'little')
        distance = (instant - self.counter) & LL_EVENT_COUNTER_MASK
        assert distance <= LL_EVENT_COUNTER_MASK // 2, 'update instant is already past'
        event_type = (
            hci.HCI_LE_PHY_Update_Complete_Event
            if opcode == Opcode.LL_PHY_UPDATE_IND
            else hci.HCI_LE_Connection_Update_Complete_Event
        )
        for _ in range(distance):
            if check_event_timing:
                assert not any(
                    isinstance(event, event_type) and event.status == ErrorCode.SUCCESS
                    for event in self.tester.events
                ), 'successful update reported before Instant'
            packet = await self.exchange()
            assert not (
                forbidden_opcodes
                and packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
                and packet.pdu[PDU_HEADER_SIZE] in forbidden_opcodes
            ), 'unexpected LL control before Instant'
        if check_event_timing:
            assert not any(
                isinstance(event, event_type) and event.status == ErrorCode.SUCCESS
                for event in self.tester.events
            ), 'successful update reported before Instant'
        options = {}
        if opcode == Opcode.LL_CONNECTION_UPDATE_IND:
            size, offset, interval, latency, timeout, _ = struct.unpack(
                '<BHHHHH', payload[1:]
            )
            self.anchor += offset * LL_CONN_INTERVAL_UNIT_US
            self.parameters = replace(
                self.parameters,
                window_size=size,
                window_offset=offset,
                interval=interval,
                latency=latency,
                timeout=timeout,
            )
            if self.central:
                options['window_us'] = size * LL_CONN_INTERVAL_UNIT_US
        elif opcode == Opcode.LL_CHANNEL_MAP_IND:
            self.parameters = replace(
                self.parameters,
                channel_map=int.from_bytes(
                    payload[1:-PDU_CONTROL_INSTANT_SIZE], 'little'
                ),
            )
        elif opcode == Opcode.LL_PHY_UPDATE_IND:
            central_phy, peripheral_phy = payload[1:3]
            tx, rx = (
                (peripheral_phy, central_phy)
                if self.central
                else (central_phy, peripheral_phy)
            )
            self.tx_phy = Phy(tx.bit_length()) if tx else self.tx_phy
            self.rx_phy = Phy(rx.bit_length()) if rx else self.rx_phy
        await self.exchange(**options)

    async def disconnect(self, *, local=True):
        reason = ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
        if local:
            await self.tester.command(hci.HCI_Disconnect_Command(self.handle, reason))
            await self.control(Opcode.LL_TERMINATE_IND)
            if self.central:
                # Central sends termination first, so the exchange already acknowledged
                # it.
                await self.tester.wait(0)
            else:
                await self._send(self.channel, b'', LlId.CONTINUATION, self.anchor)
        else:
            await self.exchange(
                bytes([Opcode.LL_TERMINATE_IND, reason]), llid=LlId.CONTROL
            )
        event = await self.tester.event(hci.HCI_Disconnection_Complete_Event)
        assert event.connection_handle == self.handle
        assert event.reason == (
            ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR if local else reason
        )


class LongControlPeer(PeerLink):
    """Lower Tester for long control PDUs (LL.TS 4.3.7.11-4.3.7.15).

    Observe packets from the first connection event, answer autonomous DLU
    with the current peer limits, and apply the selected Feature Exchange rule.
    """

    def __init__(
        self,
        *args,
        features=LONG_CONTROL_FEATURES,
        lengths=(PDU_DATA_PAYLOAD_SIZE_MIN, PDU_DATA_PAYLOAD_TIME_MIN_US) * 2,
        reject=False,
        feature_delay_us=0,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.features = features
        self.lengths = lengths
        self.reject = reject
        self.feature_delay_us = feature_delay_us
        self.feature_request = None

    def _accept(self, packet):
        super()._accept(packet)
        assert packet.valid_crc and packet.phy == self.rx_phy
        if packet.pdu[0] & PDU_DATA_LLID_MASK != LlId.CONTROL:
            return
        payload = packet.pdu[PDU_HEADER_SIZE:]
        if self.reject:
            # Core 4.5.11 counts CtrData only; the opcode is additional.
            assert len(payload) - 1 <= 26, (
                'long LL Control PDU sent without peer support'
            )
        if payload[0] == Opcode.LL_LENGTH_REQ:
            response = bytes([Opcode.LL_LENGTH_RSP]) + struct.pack(
                '<HHHH', *self.lengths
            )
        elif payload[0] in (Opcode.LL_FEATURE_REQ, Opcode.LL_PERIPHERAL_FEATURE_REQ):
            if self.features is None and self.central:
                self.feature_request = packet.start_us
                return
            response = (
                bytes([Opcode.LL_UNKNOWN_RSP, payload[0]])
                if self.features is None
                else bytes([Opcode.LL_FEATURE_RSP])
                + int(self.features).to_bytes(8, 'little')
            )
        else:
            return
        self.control_responses.append(response)

    async def exchange(self, *args, **kwargs):
        packet = await super().exchange(*args, **kwargs)
        if self.feature_request is not None:
            # LL.TS 4.3.7.12 requires a one-second delay before completing
            # the autonomous Central exchange and ending Inconclusive.
            deadline = self.feature_request + self.feature_delay_us
            while self.anchor < deadline:
                await super().exchange()
            await super().exchange(
                bytes([Opcode.LL_FEATURE_RSP])
                + int(LONG_CONTROL_FEATURES).to_bytes(8, 'little'),
                llid=LlId.CONTROL,
            )
            await super().exchange()
            pytest.skip('Inconclusive: autonomous Central Feature Exchange')
        return packet


async def establish(
    tester,
    *,
    central=False,
    directed=False,
    csa2=False,
    extended=False,
    legacy=False,
    random=False,
    random_address=None,
    phy=Phy.LE_1M,
    peer_address=PEER_ADDRESS,
    timeout=100,
    interval=24,
    latency=0,
    channel=37,
    advertising_channels=None,
    high_duty=False,
    legacy_advertising_data=None,
    connection_parameters=None,
    connection_complete_event=None,
    air_address=None,
    peer_factory=PeerLink,
):
    """Establish through HCI and raw connection PDUs, then check the completion
    event.

    ``air_address`` is the Lower Tester's over-air address, such as an RPA the
    IUT resolves to the identity ``peer_address``; it defaults to that address.
    ``legacy_advertising_data`` sets the IUT's advertising payload before enable.
    """
    address = DUT_ADDRESS
    air_address = peer_address if air_address is None else air_address
    if random:
        address = random_address or hci.Address(
            'C0:12:34:56:78:9A', hci.AddressType.RANDOM_DEVICE
        )
        await tester.command(hci.HCI_LE_Set_Random_Address_Command(address))
    if central:
        await tester.command(
            create_connection(
                extended=extended,
                peer_address=peer_address,
                peer_address_type=peer_address.address_type,
                own_address_type=(
                    hci.OwnAddressType.RANDOM if random else hci.OwnAddressType.PUBLIC
                ),
                **(
                    {
                        'supervision_timeouts': [timeout],
                        'connection_interval_mins': [interval],
                        'connection_interval_maxs': [interval],
                        'max_latencies': [latency],
                    }
                    if extended
                    else {
                        'supervision_timeout': timeout,
                        'connection_interval_min': interval,
                        'connection_interval_max': interval,
                        'max_latency': latency,
                    }
                ),
            )
        )
        pdu = adv_pdu(csa2=csa2, address=air_address)
        if directed:
            pdu = request_pdu(
                AdvertisingPduType.ADV_DIRECT_IND,
                advertiser=address,
                scanner=air_address,
            )
            if csa2:
                pdu = bytes([pdu[0] | PDU_ADV_CHSEL_MASK]) + pdu[1:]
        channels = (channel,) if advertising_channels is None else advertising_channels
        assert channels and all(item in LL_ADV_PRIMARY_CHANNELS for item in channels)
        request = None
        for advertising_channel in channels:
            advertisement_end = await tester.transmit(advertising_channel, pdu)
            request = await tester.receive(advertising_channel, duration_us=1000)
            if request is not None:
                assert request.valid_crc
                assert (
                    request.pdu[0] & PDU_ADV_TYPE_MASK == AdvertisingPduType.CONNECT_IND
                )
                assert request.pdu[1] == len(request.pdu) - PDU_HEADER_SIZE
                break
        assert request is not None, (
            'no CONNECT_IND on the configured advertising channels'
        )
        assert abs(request.start_us - advertisement_end - LL_IFS_US) <= 2
        assert bool(request.pdu[0] & PDU_ADV_CHSEL_MASK) == csa2
        assert request.pdu[PDU_HEADER_SIZE:PDU_ADV_ADDRESSES_END] == bytes(
            address
        ) + bytes(air_address)
        parameters = LinkParameters.decode(request.pdu[PDU_ADV_ADDRESSES_END:])
        link = peer_factory(tester, parameters, request.end_us, central=True, csa2=csa2)
    else:
        if extended:
            properties = Properties.CONNECTABLE_ADVERTISING
            if directed:
                properties |= Properties.DIRECTED_ADVERTISING
            if legacy:
                properties |= (
                    Properties.SCANNABLE_ADVERTISING
                    | Properties.USE_LEGACY_ADVERTISING_PDUS
                )
            await tester.command(
                extended_parameters(
                    advertising_event_properties=properties,
                    secondary_advertising_phy=phy,
                    peer_address=peer_address,
                    peer_address_type=peer_address.address_type,
                    primary_advertising_channel_map=LL_ADV_PRIMARY_CHANNEL_MAP_ALL,
                    own_address_type=(
                        hci.OwnAddressType.RANDOM
                        if random
                        else hci.OwnAddressType.PUBLIC
                    ),
                )
            )
            await tester.command(extended_enable())
        else:
            await tester.command(
                legacy_parameters(
                    own_address_type=(
                        hci.OwnAddressType.RANDOM
                        if random
                        else hci.OwnAddressType.PUBLIC
                    ),
                    advertising_type=(
                        AdvType.ADV_DIRECT_IND
                        if directed and high_duty
                        else (
                            AdvType.ADV_DIRECT_IND_LOW_DUTY
                            if directed
                            else AdvType.ADV_IND
                        )
                    ),
                    peer_address=peer_address,
                    peer_address_type=peer_address.address_type,
                    advertising_channel_map=(
                        LL_ADV_PRIMARY_CHANNEL_MAP_ALL
                        if csa2 or high_duty
                        else 1 << (channel - 37)
                    ),
                )
            )
            if legacy_advertising_data is not None:
                await tester.command(
                    hci.HCI_LE_Set_Advertising_Data_Command(legacy_advertising_data)
                )
            await tester.command(hci.HCI_LE_Set_Advertising_Enable_Command(1))
        if extended and not legacy:
            channel, advertisement, fields = await receive_extended(tester, phy=phy)
            assert fields['address'] == bytes(address)
            kind = AdvertisingPduType.ADV_EXT_IND
        else:
            kind = (
                AdvertisingPduType.ADV_DIRECT_IND
                if directed
                else AdvertisingPduType.ADV_IND
            )
            advertisement = await tester.expect_pdu(channel, kind=kind)
            assert advertisement.pdu[
                PDU_HEADER_SIZE : (
                    PDU_ADV_ADDRESSES_END
                    if directed
                    else PDU_HEADER_SIZE + PDU_ADV_ADDRESS_SIZE
                )
            ] == bytes(address) + (bytes(peer_address) if directed else b'')
            if csa2:
                assert advertisement.pdu[0] & PDU_ADV_CHSEL_MASK
        parameters = connection_parameters or LinkParameters(
            timeout=timeout, interval=interval, latency=latency
        )
        request = request_pdu(
            AdvertisingPduType.CONNECT_IND,
            advertiser=address,
            scanner=air_address,
            data=parameters.encode(),
        )
        if csa2:
            request = bytes([request[0] | PDU_ADV_CHSEL_MASK]) + request[1:]
        await tester.transmit(
            channel, request, start_us=advertisement.end_us + LL_IFS_US, phy=phy
        )
        end = tester.radio.now_us + 1
        link = peer_factory(tester, parameters, end, csa2=csa2, phy=phy)
        if kind == AdvertisingPduType.ADV_EXT_IND:
            response = await tester.expect_pdu(channel, duration_us=1000, phy=phy)
            assert response.start_us == end + LL_IFS_US
            assert (
                response.pdu[0] & PDU_ADV_TYPE_MASK
                == AdvertisingPduType.AUX_CONNECT_RSP
            )
            assert response.pdu[-PDU_ADV_ADDRESS_SIZE:] == bytes(air_address)
            link.csa2 = True
            link.anchor = end + (
                LL_CONN_AUX_WINDOW_DELAY_CODED_US
                if phy == Phy.LE_CODED
                else LL_CONN_AUX_WINDOW_DELAY_UNCODED_US
            )
    await link.exchange()
    event = await tester.event(hci.HCI_LE_Connection_Complete_Event)
    if connection_complete_event is not None:
        assert type(event) is connection_complete_event
    assert event.status == ErrorCode.SUCCESS
    assert event.role == (hci.Role.CENTRAL if central else hci.Role.PERIPHERAL)
    assert event.peer_address == peer_address
    assert event.connection_interval == link.parameters.interval
    assert event.peripheral_latency == link.parameters.latency
    assert event.supervision_timeout == link.parameters.timeout
    link.handle = event.connection_handle
    return link


def phy_bit(phy):
    return 1 << phy - 1


def phy_request(packet):
    if packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL and packet.pdu[
        PDU_HEADER_SIZE : PDU_HEADER_SIZE + 1
    ] == bytes([Opcode.LL_PHY_REQ]):
        request = packet.pdu[PDU_HEADER_SIZE:]
        assert len(request) == PDU_CONTROL_PHY_REQ_SIZE
        supported = sum(phy_bit(phy) for phy in Phy)
        assert request[1] and request[2] and not (request[1] | request[2]) & ~supported
        return request
    return None


async def settle_autonomous_phy(link):
    """Answer an autonomous Central request with current-PHY-only preferences."""
    response = bytes([Opcode.LL_PHY_RSP, phy_bit(link.tx_phy), phy_bit(link.rx_phy)])
    update = await link.control(Opcode.LL_PHY_UPDATE_IND, response, check_ack=True)
    assert update == bytes([Opcode.LL_PHY_UPDATE_IND, 0, 0, 0, 0])
    assert not any(
        isinstance(event, hci.HCI_LE_PHY_Update_Complete_Event)
        for event in link.tester.events
    ), 'autonomous no-change procedure notified the Host'


def check_phy_choice(update, allowed, current, *, constrained_zero=False):
    """Validate directional choices independently of the Controller selection."""
    assert len(update) == PDU_CONTROL_PHY_UPDATE_SIZE
    assert update[0] == Opcode.LL_PHY_UPDATE_IND
    choices = update[1:3]
    for selected, common, old in zip(choices, allowed, current):
        assert selected in (0, *(phy_bit(phy) for phy in Phy))
        assert not selected or selected & common
        assert not selected or selected != phy_bit(old), 'unchanged PHY must use zero'
        if constrained_zero and any(choices) and not selected:
            assert not common or common & phy_bit(old), (
                'illegal one-direction no-change'
            )
    return any(choices)


async def finish_phy_update(
    tester,
    link,
    update,
    *,
    event_required,
    allowed,
    constrained_zero=False,
    allow_autonomous=False,
):
    current = (link.rx_phy, link.tx_phy) if link.central else (link.tx_phy, link.rx_phy)
    changed = check_phy_choice(
        update, allowed, current, constrained_zero=constrained_zero
    )
    instant = int.from_bytes(update[-PDU_CONTROL_INSTANT_SIZE:], 'little')
    pending = None
    if changed:
        assert (
            0
            <= (instant - link.counter & LL_EVENT_COUNTER_MASK)
            < LL_EVENT_COUNTER_MASK // 2
        )
        options = (
            {'forbidden_opcodes': (Opcode.LL_PHY_REQ,)} if allow_autonomous else {}
        )
        await link.apply_update(update, check_event_timing=True, **options)
        if allow_autonomous:
            pending = phy_request(link.last_received)
    else:
        assert instant == 0
    for _ in range(2):
        packet = await link.exchange()
        if allow_autonomous:
            request = phy_request(packet)
            if request is not None:
                assert pending is None or pending == request
                pending = request
    if event_required or changed:
        event = await tester.event(hci.HCI_LE_PHY_Update_Complete_Event)
        assert (event.status, event.connection_handle, event.tx_phy, event.rx_phy) == (
            ErrorCode.SUCCESS,
            link.handle,
            link.rx_phy,
            link.tx_phy,
        )
    assert not any(
        isinstance(event, hci.HCI_LE_PHY_Update_Complete_Event)
        for event in tester.events
    ), 'unexpected or duplicate PHY completion'
    if pending is not None:
        await settle_autonomous_phy(link)
    return changed


def phy_update_pdu(link, central, peripheral):
    instant = (
        link.counter + LL_INSTANT_MIN_LISTENING_EVENTS + 1 & LL_EVENT_COUNTER_MASK
        if central or peripheral
        else 0
    )
    return bytes([Opcode.LL_PHY_UPDATE_IND, central, peripheral]) + instant.to_bytes(
        PDU_CONTROL_INSTANT_SIZE, 'little'
    )


async def change_phy(tester, link, phy, *, local, phy_options=0):
    mask = 1 << phy - 1
    peer_request = b''
    if local:
        await tester.command(
            hci.HCI_LE_Set_PHY_Command(link.handle, 0, mask, mask, phy_options)
        )
        response = await link.control(Opcode.LL_PHY_REQ)
        assert len(response) == PDU_CONTROL_PHY_REQ_SIZE and response[1] and response[2]
        if not response[1] & response[2] & mask:
            pytest.skip(
                'Inconclusive: PHY setup request cannot select the required PHY'
            )
        if link.central:
            await link.exchange(
                bytes([Opcode.LL_PHY_RSP, mask, mask]), llid=LlId.CONTROL
            )
    elif not link.central:
        response = await link.control(
            Opcode.LL_PHY_RSP, bytes([Opcode.LL_PHY_REQ, mask, mask])
        )
        assert len(response) == PDU_CONTROL_PHY_REQ_SIZE and response[1] and response[2]
        if not response[1] & response[2] & mask:
            pytest.skip(
                'Inconclusive: PHY setup response cannot select the required PHY'
            )
    else:
        # The Central IUT replies to the Peripheral's request with the update.
        peer_request = bytes([Opcode.LL_PHY_REQ, mask, mask])
    if link.central:
        update = await link.control(Opcode.LL_PHY_UPDATE_IND, peer_request)
        assert len(update) == PDU_CONTROL_PHY_UPDATE_SIZE
        allowed = {0, *(phy_bit(supported) for supported in Phy)}
        assert all(value in allowed for value in update[1:3])
        current = (link.rx_phy, link.tx_phy)
        if any(
            (Phy(value.bit_length()) if value else previous) != phy
            for value, previous in zip(update[1:3], current)
        ):
            pytest.skip('Inconclusive: PHY setup did not select the required PHY')
    else:
        update = phy_update_pdu(link, mask, mask)
        await link.exchange(update, llid=LlId.CONTROL)
    await finish_phy_update(
        tester, link, update, event_required=local, allowed=(mask, mask)
    )


async def exchange_iut_features(tester, link, features=PEER_FEATURES):
    """LL.TS 4.1.6.7.3: CEN IUT initiates Feature Exchange; PER Lower Tester."""
    if not link.central:
        await exchange_features(link, features)
        return
    await tester.command(hci.HCI_LE_Read_Remote_Features_Command(link.handle))
    await link.control(Opcode.LL_FEATURE_REQ)
    await link.exchange(
        bytes([Opcode.LL_FEATURE_RSP]) + int(features).to_bytes(8, 'little'),
        llid=LlId.CONTROL,
    )
    event = await tester.event(hci.HCI_LE_Read_Remote_Features_Complete_Event)
    assert event.status == ErrorCode.SUCCESS
