# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Independent CIS Lower Tester wire fields (Core Vol 6, Part B, 2.4.2.29–32)."""

import struct
from dataclasses import dataclass, replace

from bscontroller import hci
from bscontroller.linklayer import ControlOpcode, LlId
from bscontroller.radio.base import PHY_CODED_S8_FACTOR, Phy
from tests.qualification.iso import channels
from tests.qualification.link import _EncryptedPeer, start_encryption
from tests.qualification.peer import PeerLink, change_phy, establish, exchange_features
from tests.support import LE_EVENT_MASK_V1

CIS_REQ = 0x1F
CIS_RSP = 0x20
CIS_IND = 0x21
CIS_TERMINATE_IND = 0x22
PHY_BY_MASK = {1 << (phy.value - 1): phy for phy in Phy}
CIS_PHYS = (Phy.LE_1M, Phy.LE_1M)

PEER_CIS_FEATURES = int(
    hci.LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_CENTRAL
    | hci.LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_PERIPHERAL
    | hci.LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM
    | hci.LeFeatureMask.EXTENDED_REJECT_INDICATION
    | hci.LeFeatureMask.LE_2M_PHY
    | hci.LeFeatureMask.LE_CODED_PHY
)


async def enable_cis(tester):
    await tester.command(
        hci.HCI_LE_Set_Host_Feature_Command(
            hci.LeFeature.CONNECTED_ISOCHRONOUS_STREAM, 1
        )
    )


def cis_request(
    counter,
    *,
    c_to_p=1,
    p_to_c=1,
    c_sdu=251,
    p_sdu=251,
    interval_us=20000,
    subinterval_us=5000,
    nse=3,
    framed=False,
    offset_us=7500,
    offset_max_us=None,
    rfu=False,
):
    """BN=1 CIS request with LL.TS.p28, Table 4.10-2 defaults."""
    c_pdu = c_sdu + 5 if framed and c_sdu else c_sdu
    p_pdu = p_sdu + 5 if framed and p_sdu else p_sdu
    c_sdu_field = c_sdu | (framed << 15)
    p_sdu_field = p_sdu
    sdu_interval_field = interval_us
    if rfu:
        # Figure 2.50. Framing_Mode is also RFU in an unframed request.
        c_sdu_field |= 0x3000 if framed else 0x7000
        p_sdu_field |= 0xF000
        sdu_interval_field |= 0xF00000
    return b''.join(
        (
            struct.pack(
                '<BBBBBHH', CIS_REQ, 1, 1, c_to_p, p_to_c, c_sdu_field, p_sdu_field
            ),
            sdu_interval_field.to_bytes(3, 'little') * 2,
            struct.pack('<HHB', c_pdu, p_pdu, nse),
            subinterval_us.to_bytes(3, 'little'),
            struct.pack(
                '<BBBH',
                bool(c_sdu) | (bool(p_sdu) << 4),
                1,
                1,
                interval_us // 1250,
            ),
            offset_us.to_bytes(3, 'little'),
            (offset_us if offset_max_us is None else offset_max_us).to_bytes(
                3, 'little'
            ),
            counter.to_bytes(2, 'little'),
        )
    )


async def connect_cis_acl(
    tester,
    *,
    central=False,
    enable=True,
    interval=32,
    features=PEER_CIS_FEATURES,
    encrypted=False,
    timeout=100,
    connection_parameters=None,
    acl_phy=Phy.LE_1M,
):
    await tester.masks(le=LE_EVENT_MASK_V1)
    if enable:
        await enable_cis(tester)
    link = await establish(
        tester,
        central=central,
        csa2=True,
        interval=interval,
        timeout=timeout,
        connection_parameters=connection_parameters,
    )
    if encrypted:
        link = _EncryptedPeer(link)
        features |= int(hci.LeFeatureMask.LE_ENCRYPTION)
    await exchange_features(link, features)
    if acl_phy != Phy.LE_1M:
        await change_phy(tester, link, acl_phy, local=False)
        assert link.tx_phy == link.rx_phy == acl_phy
    if encrypted:
        await start_encryption(tester, link)
    return link


@dataclass
class CisPeer:
    """Advance an independent CIS alongside the linked ACL."""

    link: PeerLink
    access_address: int
    anchor_us: int
    interval_us: int
    nse: int
    subinterval_us: int
    spacing_us: int = 150
    counter: int = 0
    subevent: int = 0
    phys: tuple[Phy, Phy] = CIS_PHYS
    coding: int = PHY_CODED_S8_FACTOR

    @property
    def nominal_us(self):
        return self.anchor_us + self.subevent * self.subinterval_us

    def step(self):
        self.subevent += 1
        if self.subevent == self.nse:
            self.subevent = 0
            self.counter += 1
            self.anchor_us += self.interval_us

    def skip_elapsed(self):
        """Advance the Lower Tester's clocks after an HCI observation wait."""
        link = self.link
        ready_us = link.tester.radio.now_us + 150
        while link.anchor <= ready_us:
            link.anchor += link.parameters.interval * 1250
            link.counter = (link.counter + 1) & 0xFFFF
        while self.nominal_us <= ready_us:
            self.step()

    def _crypt(self, header, data, *, transmit):
        link = self.link
        if not data or not isinstance(link, _EncryptedPeer):
            return data
        if not (link.tx_enabled if transmit else link.rx_enabled):
            return data
        # Vol 6, Part E, 2.1–2.2. The CIS has BN=1 and FT=1, so the
        # payload counter is the event counter even for a retransmission.
        central = not link.central if transmit else link.central
        iv = (int.from_bytes(link.iv, 'little') ^ self.access_address).to_bytes(
            8, 'little'
        )
        nonce = (self.counter | (int(central) << 39)).to_bytes(5, 'little') + iv
        authentication = bytes([header & 0xA3])
        cipher = link.aes.encrypt if transmit else link.aes.decrypt
        return cipher(nonce, data, authentication)

    async def exchange(
        self,
        data=b'',
        *,
        header=0x40,
        power_dbm=0,
        transmit=True,
        expect_rx=True,
        bad_mic=False,
        bad_crc=False,
    ):
        link = self.link
        tester = link.tester
        rx_phy, tx_phy = self.phys if link.central else self.phys[::-1]
        nominal = self.nominal_us
        while link.anchor + 1000 < nominal:
            await link.exchange()
        channel = channels(
            self.counter,
            self.access_address,
            link.parameters.channel_map,
            self.nse,
        )[self.subevent]
        kwargs = {
            'access_address': self.access_address,
            'crc_init': link.parameters.crc_init,
        }

        def outbound(received=None):
            flags = header(received) if callable(header) else header
            payload = self._crypt(flags, data, transmit=True)
            if bad_mic:
                assert len(payload) > len(data), 'cannot corrupt an absent MIC'
                payload = payload[:-1] + bytes((payload[-1] ^ 1,))
            return bytes((flags, len(payload))) + payload

        async def receive(start_us):
            packet = await tester.receive(
                channel,
                start_us=max(tester.radio.now_us + 1, start_us - 10),
                duration_us=start_us + rx_phy.address_us + 4 - tester.radio.now_us,
                phy=rx_phy,
                **kwargs,
            )
            if expect_rx is not None:
                assert (packet is not None) == expect_rx
            if packet is not None:
                assert len(packet.pdu) >= 2 and packet.pdu[1] == len(packet.pdu) - 2
                assert packet.valid_crc and abs(packet.start_us - start_us) <= 2
            return packet

        if link.central:
            packet = await receive(nominal)
            if transmit and packet is not None:
                await tester.transmit(
                    channel,
                    outbound(packet),
                    start_us=packet.end_us + self.spacing_us,
                    power_dbm=power_dbm,
                    bad_crc=bad_crc,
                    phy=tx_phy,
                    coding=self.coding,
                    **kwargs,
                )
        else:
            if transmit:
                end = await tester.transmit(
                    channel,
                    outbound(),
                    start_us=nominal,
                    power_dbm=power_dbm,
                    bad_crc=bad_crc,
                    phy=tx_phy,
                    coding=self.coding,
                    **kwargs,
                )
            else:
                end = nominal + tx_phy.airtime_us(2, self.coding)
            packet = await receive(end + self.spacing_us)
        if packet is None:
            self.step()
            return None
        header = packet.pdu[0]
        plaintext = self._crypt(header, packet.pdu[2:], transmit=False)
        self.step()
        return replace(packet, pdu=bytes((header, len(plaintext))) + plaintext)


async def establish_peer_cis(
    tester,
    *,
    central,
    receive_only=False,
    link=None,
    interval_us=20000,
    ifs_us=150,
    mss_us=150,
    access_address=0x7654BA98,
    before_accept=None,
    after_response=None,
    before_first_packet=None,
    test_nse=None,
    framed=False,
    sdu_sizes=None,
    offset_us=7500,
    phys=CIS_PHYS,
    coding=PHY_CODED_S8_FACTOR,
    exchange_first=True,
    choose_response=None,
    check_request=None,
    subinterval_us=None,
    offset_max_us=None,
    choose_indication=None,
    request_rfu=False,
):
    """Complete the four wire exchanges and establish with a peer NULL PDU."""
    if link is None:
        link = await connect_cis_acl(tester, central=central)
    max_sdu = 246 if framed else 251
    c_sdu, p_sdu = sdu_sizes or (
        0 if central and receive_only else max_sdu,
        0 if not central and receive_only else max_sdu,
    )
    c_pdu = c_sdu + 5 if c_sdu and framed else c_sdu
    p_pdu = p_sdu + 5 if p_sdu and framed else p_sdu
    c_phy, p_phy = (1 << (phy.value - 1) for phy in phys)
    if central:
        if test_nse is None:
            command = hci.HCI_LE_Set_CIG_Parameters_Command(
                1,
                interval_us,
                interval_us,
                0,
                0,
                int(framed),
                max(40, interval_us // 1000),
                max(40, interval_us // 1000),
                [1],
                [c_sdu],
                [p_sdu],
                [c_phy],
                [p_phy],
                [3],
                [3],
            )
        else:
            command = hci.HCI_LE_Set_CIG_Parameters_Test_Command(
                1,
                interval_us,
                interval_us,
                1,
                1,
                interval_us // 1250,
                0,
                0,
                int(framed),
                [1],
                [test_nse],
                [c_sdu],
                [p_sdu],
                [c_pdu],
                [p_pdu],
                [c_phy],
                [p_phy],
                [int(bool(c_sdu))],
                [int(bool(p_sdu))],
            )
        result = await tester.command(command)
        (handle,) = result.connection_handle
        return await establish_configured_cis(
            tester,
            link,
            handle,
            ifs_us=ifs_us,
            before_first_packet=before_first_packet,
            coding=coding,
            exchange_first=exchange_first,
            choose_response=choose_response,
            check_request=check_request,
        )
    else:
        # MPT reserves the MIC and assumes S=8 even when transmitting with S=2.
        max_packet_time = sum(
            phy.airtime_us(pdu + 6) for phy, pdu in zip(phys, (c_pdu, p_pdu))
        )
        nse = 3 if test_nse is None else test_nse
        subinterval = 0 if nse == 1 else max(5000, max_packet_time + ifs_us + mss_us)
        if subinterval_us is not None:
            subinterval = subinterval_us
        request = cis_request(
            (link.counter + 6) & 0xFFFF,
            c_to_p=c_phy,
            p_to_c=p_phy,
            c_sdu=c_sdu,
            p_sdu=p_sdu,
            interval_us=interval_us,
            subinterval_us=subinterval,
            nse=nse,
            framed=framed,
            offset_us=offset_us,
            offset_max_us=offset_max_us,
            rfu=request_rfu,
        )
        await link.exchange(request, llid=LlId.CONTROL)
        pending = await tester.event(hci.HCI_LE_CIS_Request_Event)
        assert (pending.acl_connection_handle, pending.cig_id, pending.cis_id) == (
            link.handle,
            request[1],
            request[2],
        )
        handle = pending.cis_connection_handle
        if before_accept is not None:
            await before_accept(handle)
        await tester.command(hci.HCI_LE_Accept_CIS_Request_Command(handle))
        response = await link.control(ControlOpcode.LL_CIS_RSP)
        if after_response is not None:
            await after_response(handle)
        count = int.from_bytes(response[-2:], 'little')
        offset = int.from_bytes(response[1:4], 'little')
        if choose_indication is not None:
            offset, count = choose_indication(request, response)
        address = access_address
        duration = (nse - 1) * subinterval + max_packet_time + ifs_us
        indication = (
            bytes([CIS_IND])
            + address.to_bytes(4, 'little')
            + offset.to_bytes(3, 'little')
            + duration.to_bytes(3, 'little') * 2
            + count.to_bytes(2, 'little')
        )
        await link.exchange(indication, llid=LlId.CONTROL, check_ack=True)
    return await _observe_establishment(
        tester,
        link,
        handle,
        indication,
        interval_us=interval_us,
        nse=nse,
        subinterval_us=subinterval,
        ifs_us=ifs_us,
        before_first_packet=before_first_packet,
        phys=phys,
        coding=coding,
        exchange_first=exchange_first,
    )


async def establish_configured_cis(
    tester,
    link,
    handle,
    *,
    check_request=None,
    check_indication=None,
    ifs_us=150,
    before_first_packet=None,
    coding=PHY_CODED_S8_FACTOR,
    exchange_first=True,
    choose_response=None,
):
    """Create one configured CIS on the existing Central ACL."""
    await tester.command(hci.HCI_LE_Create_CIS_Command([handle], [link.handle]))
    request = await link.control(ControlOpcode.LL_CIS_REQ)
    if check_request is not None:
        check_request(request)
    response = request[-8:] if choose_response is None else choose_response(request)
    await link.exchange(bytes([CIS_RSP]) + response, llid=LlId.CONTROL)
    indication = await link.control(ControlOpcode.LL_CIS_IND)
    if check_indication is not None:
        check_indication(request, indication)
    return await _observe_establishment(
        tester,
        link,
        handle,
        indication,
        interval_us=1250 * int.from_bytes(request[26:28], 'little'),
        nse=request[19],
        subinterval_us=int.from_bytes(request[20:23], 'little'),
        ifs_us=ifs_us,
        before_first_packet=before_first_packet,
        phys=(PHY_BY_MASK[request[3]], PHY_BY_MASK[request[4]]),
        coding=coding,
        exchange_first=exchange_first,
    )


async def _observe_establishment(
    tester,
    link,
    handle,
    indication,
    *,
    interval_us,
    nse,
    subinterval_us,
    ifs_us,
    before_first_packet,
    phys,
    coding,
    exchange_first=True,
):
    if before_first_packet is not None:
        await before_first_packet(handle)
    address = int.from_bytes(indication[1:5], 'little')
    offset = int.from_bytes(indication[5:8], 'little')
    count = int.from_bytes(indication[-2:], 'little')
    difference = (count - link.counter + 32768) % 65536 - 32768
    anchor = link.anchor + difference * link.parameters.interval * 1250 + offset
    air = CisPeer(
        link,
        address,
        anchor,
        interval_us,
        nse,
        subinterval_us,
        ifs_us,
        phys=phys,
        coding=coding,
    )
    if not exchange_first:
        return link, air, handle
    await air.exchange()
    event = await tester.event(
        (hci.HCI_LE_CIS_Established_Event, hci.HCI_LE_CIS_Established_V2_Event)
    )
    assert (event.status, event.connection_handle) == (0, handle)
    return link, air, event


async def disconnect_cis(tester, link, handle):
    """Verify local CIS termination and another exchange on its surviving ACL."""
    reason = hci.HCI_ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    await tester.command(hci.HCI_Disconnect_Command(handle, reason))
    assert await link.control(CIS_TERMINATE_IND) == bytes(
        (CIS_TERMINATE_IND, 1, 1, reason)
    )
    if not link.central:
        # The Lower Tester acknowledges the Peripheral's termination.
        await link.exchange()
    event = await tester.event(hci.HCI_Disconnection_Complete_Event)
    assert (event.status, event.connection_handle, event.reason) == (0, handle, 0x16)
    await link.exchange()
