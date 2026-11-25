# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Lower Tester encryption setup and encrypted ACL traffic."""

import random

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from bscontroller import hci
from bscontroller.linklayer import (
    LL_ENCRYPTION_IV_HALF_SIZE,
    LL_ENCRYPTION_SKD_HALF_SIZE,
    PDU_CONTROL_ENC_REQ_SIZE,
    PDU_CONTROL_ENC_RSP_SIZE,
    PDU_DATA_LLID_MASK,
    PDU_HEADER_SIZE,
    ControlOpcode,
    LlId,
)
from tests.qualification.link import _EncryptedPeer
from tests.qualification.peer import establish, exchange_features
from tests.qualification.tester import (
    LOWER_CENTRAL_PUBLIC_ADDRESS,
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
)

ErrorCode = hci.HCI_ErrorCode
Opcode = ControlOpcode

# LL.TS.p28, printed 4.1-29: public common IR/ER value, in HCI octet order.
ROOT = bytes.fromhex('112233445566778899aabbccddeeff00')[::-1]

# Mode Setup sends 1000 zero octets in ten-octet HCI packets before and after setup.
MODE_SETUP_DATA_SIZE = 10
MODE_SETUP_TOTAL_OCTETS = 1000


class SecurityPeer(_EncryptedPeer):
    def __init__(self, link):
        super().__init__(link)
        self.received_data = []
        self.received_controls = []
        self.termination_received = False
        self.last_new_mic_tx_us = None
        self.generator = random.Random(0)

    async def _send(self, channel, data, llid, start):
        new_mic = self.tx_enabled and data and self.tx_sn != self.sn
        await super()._send(channel, data, llid, start)
        if new_mic:
            self.last_new_mic_tx_us = self.last_sent_end_us - 1

    def decode(self, packet):
        packet = super().decode(packet)
        if packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL:
            self.received_controls.append((packet, self.rx_enabled))
        if (
            packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTROL
            and len(packet.pdu) > PDU_HEADER_SIZE
            and packet.pdu[PDU_HEADER_SIZE] == Opcode.LL_TERMINATE_IND
        ):
            self.termination_received = True
        if (
            packet.pdu[0] & PDU_DATA_LLID_MASK != LlId.CONTROL
            and packet.pdu[PDU_HEADER_SIZE:]
        ):
            self.received_data.append((packet.pdu[PDU_HEADER_SIZE:], self.rx_enabled))
        return packet


def aes(key, plaintext):
    encryptor = Cipher(algorithms.AES(key[::-1]), modes.ECB()).encryptor()
    return encryptor.update(plaintext[::-1])[::-1]


async def begin_encryption(tester, link, *, refresh=False):
    """Stop after ENC_REQ reception (Central) or ENC_RSP/LTK request (Peripheral)."""
    link.skd_component = link.generator.randbytes(LL_ENCRYPTION_SKD_HALF_SIZE)
    link.iv_component = link.generator.randbytes(LL_ENCRYPTION_IV_HALF_SIZE)
    if link.central:
        if not refresh:
            await tester.command(
                hci.HCI_LE_Enable_Encryption_Command(
                    link.handle, link.rand, link.ediv, link.ltk
                )
            )
        request = await link.control(Opcode.LL_ENC_REQ)
        assert len(request) == PDU_CONTROL_ENC_REQ_SIZE
        assert (
            request[1:9] == link.rand
            and int.from_bytes(request[9:11], 'little') == link.ediv
        )
        dhk = aes(ROOT, (2).to_bytes(16, 'little'))
        div = int.from_bytes(request[9:11], 'little') ^ int.from_bytes(
            aes(dhk, request[1:9] + bytes(8))[:2], 'little'
        )
        assert aes(ROOT, div.to_bytes(16, 'little')) == link.ltk
        link.install(
            request[11:19] + link.skd_component, request[19:23] + link.iv_component
        )
    else:
        response = await link.control(
            Opcode.LL_ENC_RSP,
            bytes([Opcode.LL_ENC_REQ])
            + link.rand
            + link.ediv.to_bytes(2, 'little')
            + link.skd_component
            + link.iv_component,
        )
        assert len(response) == PDU_CONTROL_ENC_RSP_SIZE
        event = await tester.event(hci.HCI_LE_Long_Term_Key_Request_Event)
        assert event.connection_handle == link.handle
        assert (
            event.random_number == link.rand
            and event.encryption_diversifier == link.ediv
        )
        link.install(
            link.skd_component + response[1:9], link.iv_component + response[9:13]
        )


async def exchange_security_features(link, *, ping=True, extended_reject=False):
    features = hci.LeFeatureMask.LE_ENCRYPTION
    if ping:
        features |= hci.LeFeatureMask.LE_PING
    if extended_reject:
        features |= hci.LeFeatureMask.EXTENDED_REJECT_INDICATION
    await exchange_features(link, features)


async def finish_encryption(
    tester, link, *, refresh=False, response_sent=False, check_event=True
):
    expect_no_encryption_result(tester)
    received_before = len(link.received_data)
    if link.central:
        if not response_sent:
            await link.exchange(
                bytes([Opcode.LL_ENC_RSP]) + link.skd_component + link.iv_component,
                llid=LlId.CONTROL,
            )
        link.rx_enabled = True
        packet = await link.exchange(
            bytes([Opcode.LL_START_ENC_REQ]), llid=LlId.CONTROL
        )
        assert packet.pdu[0] & PDU_DATA_LLID_MASK == LlId.CONTINUATION
        assert packet.pdu[PDU_HEADER_SIZE:] == b''
        assert await link.control(Opcode.LL_START_ENC_RSP, check_ack=True) == bytes(
            [Opcode.LL_START_ENC_RSP]
        )
        assert len(link.received_data) == received_before, (
            'user data sent before encryption completed'
        )
        expect_no_encryption_result(tester)
        link.tx_enabled = True
        await link.exchange(bytes([Opcode.LL_START_ENC_RSP]), llid=LlId.CONTROL)
    else:
        result = await tester.command(
            hci.HCI_LE_Long_Term_Key_Request_Reply_Command(link.handle, link.ltk)
        )
        assert result.connection_handle == link.handle
        assert await link.control(Opcode.LL_START_ENC_REQ) == bytes(
            [Opcode.LL_START_ENC_REQ]
        )
        await link.exchange()
        link.tx_enabled = link.rx_enabled = True
        assert await link.control(
            Opcode.LL_START_ENC_RSP,
            bytes([Opcode.LL_START_ENC_RSP]),
            check_ack=True,
        ) == bytes([Opcode.LL_START_ENC_RSP])
        assert len(link.received_data) == received_before, (
            'queued data leaked before refresh completed'
        )
    await link.exchange()
    if not check_event:
        # Event-version procedures verify their own mask-dependent HCI result.
        return
    event = await tester.event(
        hci.HCI_Encryption_Key_Refresh_Complete_Event
        if refresh
        else hci.HCI_Encryption_Change_Event
    )
    assert event.status == ErrorCode.SUCCESS and event.connection_handle == link.handle
    if not refresh:
        assert (
            event.encryption_enabled
            == hci.HCI_Encryption_Change_Event.Enabled.E0_OR_AES_CCM
        )
    expect_no_encryption_result(tester)


async def prepare_encryption_keys(tester):
    """Exercise the HCI key-calculation boundary, independently checking AES."""
    div = int.from_bytes(
        (await tester.command(hci.HCI_LE_Rand_Command())).random_number[:2], 'little'
    )
    rand = (await tester.command(hci.HCI_LE_Rand_Command())).random_number

    async def encrypt(key, plaintext):
        result = await tester.command(hci.HCI_LE_Encrypt_Command(key, plaintext))
        assert result.encrypted_data == aes(key, plaintext)
        return result.encrypted_data

    dhk = await encrypt(ROOT, (2).to_bytes(16, 'little'))
    y = int.from_bytes((await encrypt(dhk, rand + bytes(8)))[:2], 'little')
    ediv = div ^ y
    recovered_div = ediv ^ int.from_bytes(aes(dhk, rand + bytes(8))[:2], 'little')
    assert recovered_div == div
    ltk = await encrypt(ROOT, div.to_bytes(16, 'little'))
    assert ltk == aes(ROOT, recovered_div.to_bytes(16, 'little'))
    return (rand, ediv, ltk)


def expect_no_encryption_result(tester):
    assert not any(
        isinstance(
            event,
            (
                hci.HCI_Encryption_Change_Event,
                hci.HCI_Encryption_Key_Refresh_Complete_Event,
            ),
        )
        for event in tester.events
    ), 'encryption result reported before the required handshake completed'


async def establish_security_link(
    tester, central, *, random_address=False, random_peer=True
):
    rand, ediv, ltk = await prepare_encryption_keys(tester)
    address = None
    peer = LOWER_PERIPHERAL_PUBLIC_ADDRESS if central else LOWER_CENTRAL_PUBLIC_ADDRESS
    if random_address:
        irk = aes(ROOT, (1).to_bytes(16, 'little'))
        prand = bytes.fromhex('948170')
        address = hci.Address(
            aes(irk, prand + bytes(13))[:3] + prand, hci.AddressType.RANDOM_DEVICE
        )
        assert address.is_resolvable
        if random_peer:
            peer = hci.Address('C0:11:22:33:44:55', hci.AddressType.RANDOM_DEVICE)
    link = SecurityPeer(
        await establish(
            tester,
            central=central,
            peer_address=peer,
            random=random_address,
            random_address=address,
            interval=8,
            timeout=30,
        )
    )
    link.rand, link.ediv, link.ltk = (rand, ediv, ltk)
    link.generator.seed(rand)
    return link
