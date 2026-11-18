# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Independent BIS Lower Tester (Core Vol 6, B, 2.6/4.4.6; E; G).

Wire fields, channel selection, group key derivation and ISOAL framing are
encoded here without importing the Controller's BIS, ISOAL or BIG procedures.
The peer runs on its own native PHY endpoint, including encryption and CRC.
"""

from collections import deque
from dataclasses import dataclass
from random import Random

from cryptography.hazmat.primitives.ciphers import algorithms
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.cmac import CMAC

from bscontroller import hci
from bscontroller.radio.base import Phy
from tests.qualification.periodic import PeriodicAdvertiser, PeriodicTrain
from tests.qualification.tester import (
    LOWER_PERIPHERAL_PUBLIC_ADDRESS,
    csa2_channel,
    decode_extended,
    ext_pdu,
    follow_auxiliary,
)

CODE = b'BIS-qual-code-01'  # IXIT Broadcast_Code, 16 HCI-order octets.
ALL_CHANNELS = (1 << 37) - 1


def channels(counter, address, channel_map, count):
    """Independent ISO CSA #2 state (Vol 6, Part B, 4.5.8.3.5–6)."""
    identifier = address >> 16 ^ address & 0xFFFF
    state = counter & 0xFFFF ^ identifier

    def step(value):
        permuted = sum(
            ((value >> bit) & 1) << ((bit & 8) + 7 - (bit & 7)) for bit in range(16)
        )
        return (17 * permuted + identifier) & 0xFFFF

    for _ in range(3):
        state = step(state)
    used = [n for n in range(37) if channel_map & 1 << n]
    first = csa2_channel(counter, address, channel_map)
    index = used.index(first)
    distance = max(1, min(3, len(used) - 5), min(11, (len(used) - 10) // 2))
    result = [first]
    for _ in range(count - 1):
        state = step(state)
        index = (
            index
            + distance
            + ((state ^ identifier) * (len(used) - 2 * distance + 1) // 65536)
        ) % len(used)
        result.append(used[index])
    return result


def group_key(code, gskd):
    def cmac(key, data):
        context = CMAC(algorithms.AES(key))
        context.update(data)
        return context.finalize()

    igltk = cmac(b'\0' * 12 + b'BIG1', code[::-1])
    gltk = cmac(igltk, b'BIG2')
    return cmac(cmac(gskd[::-1], gltk), b'BIG3')


def valid_identity(address):
    """Independent bit-string AA oracle, including LE Coded constraints."""
    bits = f'{address:032b}'
    octets = address.to_bytes(4, 'little')

    def transitions(value):
        return sum(a != b for a, b in zip(value, value[1:]))

    return (
        '0000000' not in bits
        and '1111111' not in bits
        and (address ^ 0x8E89BED6).bit_count() > 1
        and len(set(octets)) > 1
        and transitions(bits) <= 24
        and transitions(bits[:6]) >= 2
        and bits[-8:].count('1') >= 3
        and transitions(bits[-16:]) <= 11
    )


@dataclass
class BigTrain:
    anchor: int
    interval: int = 8
    num_bis: int = 1
    nse: int = 3
    bn: int = 1
    irc: int = 3
    pto: int = 0
    sub_interval: int = 600
    spacing: int = 1800
    max_pdu: int = 32
    max_sdu: int = 32
    sdu_interval: int = 10000
    phy: Phy = Phy.LE_1M
    seed: int = 0x78F746BF
    crc: int = 0xA1B2
    channel_map: int = ALL_CHANNELS
    counter: int = 0
    framed: bool = False
    mode: int = 0
    giv: bytes = b''
    gskd: bytes = b''
    code: bytes = CODE
    unit: int = 30
    offset_events: int = 1
    advertised_phy: int | None = None
    last_cssn: int | None = None

    @property
    def interval_us(self):
        return self.interval * 1250

    @property
    def control_offset(self):
        return (
            self.num_bis * self.spacing
            if self.spacing >= self.nse * self.sub_interval
            else self.nse * self.sub_interval
        )

    @property
    def transport_latency_us(self):
        """Calculate ISOAL latency from observed BIGInfo (Vol 6, G, 3.2)."""
        final_subevent = self.schedule()[-1][0]
        mpt = self.phy.airtime_us(2 + self.max_pdu + (4 if self.giv else 0))
        pretransmissions = self.nse // self.bn - self.irc
        delay = final_subevent + mpt + pretransmissions * self.pto * self.interval_us
        if self.framed:
            return delay + self.interval_us + self.sdu_interval
        return delay + self.interval_us - self.sdu_interval

    def address(self, number):
        diversifier = (35 * number + 42) % 128
        # DW bits 31..26 repeat D0; the remaining assignments are specified
        # individually in Vol 6, Part B, 2.1.2.
        word = sum((diversifier & 1) << bit for bit in range(26, 32))
        for target, source in (
            (25, 1),
            (24, 6),
            (23, 1),
            (21, 5),
            (20, 4),
            (18, 3),
            (17, 2),
        ):
            word |= ((diversifier >> source) & 1) << target
        return self.seed ^ word

    def parameters(self, number):
        return dict(
            access_address=self.address(number),
            crc_init=self.crc << 8 | number,
            phy=self.phy,
        )

    def crypt(self, number, header, payload, counter, *, decrypt=False):
        if not self.giv or not payload:
            return payload
        iv = (int.from_bytes(self.giv[:4], 'little') ^ self.address(number)).to_bytes(
            4, 'little'
        ) + self.giv[4:]
        nonce = (counter | 1 << 39).to_bytes(5, 'little') + iv
        cipher = AESCCM(group_key(self.code, self.gskd), tag_length=4)
        return (cipher.decrypt if decrypt else cipher.encrypt)(
            nonce, payload, bytes([header & 0xC3])
        )

    def big_info(self, packet_start):
        steps = max(
            0,
            (packet_start + 630 - self.anchor + self.interval_us - 1)
            // self.interval_us,
        )
        steps += self.offset_events - 1
        offset = self.anchor + steps * self.interval_us - packet_start
        unit = 300 if offset >= 491460 else 30
        # Fixed bit positions provide an oracle independent of the production
        # codec's sequential field packing.
        fields = (
            (0, offset // unit),
            (14, int(unit == 300)),
            (15, self.interval),
            (27, self.num_bis),
            (32, self.nse),
            (37, self.bn),
            (40, self.sub_interval),
            (60, self.pto),
            (64, self.spacing),
            (84, self.irc),
            (88, self.max_pdu),
            (103, self.mode),
            (104, self.seed),
            (136, self.sdu_interval),
            (156, self.max_sdu),
            (168, self.crc),
            (184, self.channel_map),
            (
                221,
                int(self.phy) - 1
                if self.advertised_phy is None
                else self.advertised_phy,
            ),
            (224, (self.counter + steps) * self.bn),
            (263, int(self.framed)),
        )
        value = sum(value << position for position, value in fields)
        data = value.to_bytes(33, 'little') + self.giv + self.gskd
        return bytes([len(data) + 1, 0x2C]) + data

    @classmethod
    def from_periodic(cls, packet):
        fields = decode_extended(packet.pdu)
        acad = fields.get('acad', b'')
        position = 0
        while position < len(acad):
            length = acad[position]
            assert length and position + length < len(acad)
            if acad[position + 1] == 0x2C:
                data = acad[position + 2 : position + length + 1]
                assert len(data) in (33, 57)
                value = int.from_bytes(data[:33], 'little')

                def bits(position, width):
                    return value >> position & ((1 << width) - 1)

                unit = 300 if bits(14, 1) else 30
                offset = bits(0, 14) * unit
                assert offset > 600
                assert offset >= 491460 or unit == 30
                bn = bits(37, 3)
                assert bn
                return cls(
                    anchor=packet.start_us + offset,
                    interval=bits(15, 12),
                    num_bis=bits(27, 5),
                    nse=bits(32, 5),
                    bn=bn,
                    irc=bits(84, 4),
                    pto=bits(60, 4),
                    sub_interval=bits(40, 20),
                    spacing=bits(64, 20),
                    max_pdu=bits(88, 8),
                    max_sdu=bits(156, 12),
                    sdu_interval=bits(136, 20),
                    phy=Phy(bits(221, 3) + 1),
                    seed=bits(104, 32),
                    crc=bits(168, 16),
                    channel_map=bits(184, 37),
                    counter=bits(224, 39) // bn,
                    framed=bool(bits(263, 1)),
                    mode=bits(103, 1),
                    giv=data[33:41],
                    gskd=data[41:],
                    unit=unit,
                )
            position += length + 1
        return None

    def schedule(self, numbers=None):
        numbers = numbers or range(1, self.num_bis + 1)
        return sorted(
            ((n - 1) * self.spacing + se * self.sub_interval, n, se)
            for n in numbers
            for se in range(self.nse)
        )

    def associated(self, subevent):
        return self.counter + max(0, subevent // self.bn - self.irc + 1) * self.pto

    def next_event(self):
        self.anchor += self.interval_us
        self.counter += 1

    def align(self, now_us):
        while self.anchor <= now_us:
            self.next_event()

    async def receive(self, tester, *, required=True, numbers=None):
        payloads = {}
        channels_by_bis = {
            n: channels(self.counter, self.address(n), self.channel_map, self.nse)
            for n in range(1, self.num_bis + 1)
        }
        control = None
        cstf = cssn = None
        for offset, number, subevent in self.schedule(numbers):
            nominal = self.anchor + offset
            if nominal <= tester.radio.now_us:
                assert not required, ('late observer', nominal, tester.radio.now_us)
                continue
            packet = await tester.receive(
                channels_by_bis[number][subevent],
                start_us=nominal - 2,
                duration_us=nominal
                + self.unit
                + self.phy.address_us
                + 2
                - tester.radio.now_us,
                **self.parameters(number),
            )
            if packet is None:
                assert not required, ('missing BIS', number, self.counter, subevent)
                continue
            assert packet.valid_crc and packet.phy == self.phy
            assert nominal - 2 <= packet.start_us <= nominal + self.unit + 2
            header, length = packet.pdu[:2]
            assert length == len(packet.pdu) - 2
            assert not header & 0xC0
            if cstf is not None:
                assert cstf == bool(header & 0x20) and cssn == header >> 2 & 7
            cstf, cssn = bool(header & 0x20), header >> 2 & 7
            associated = self.associated(subevent)
            counter = associated * self.bn + subevent % self.bn
            data = self.crypt(number, header, packet.pdu[2:], counter, decrypt=True)
            key = (number, counter)
            if key in payloads:
                assert payloads[key] == (header & 3, data)
            payloads[key] = header & 3, data
            self.anchor = packet.start_us - offset
            self.unit = 2
        if cssn is not None:
            self.last_cssn = cssn
        if cstf:
            nominal = self.anchor + self.control_offset
            packet = await tester.receive(
                channels(self.counter, self.address(0), self.channel_map, 1)[0],
                start_us=nominal - 2,
                duration_us=nominal + self.phy.address_us + 4 - tester.radio.now_us,
                **self.parameters(0),
            )
            assert packet is not None and packet.valid_crc
            header, length = packet.pdu[:2]
            assert header == (3 | cssn << 2)
            assert length == len(packet.pdu) - 2
            control = self.crypt(
                0, header, packet.pdu[2:], self.counter * self.bn, decrypt=True
            )
        self.next_event()
        return payloads, control

    async def receive_control(self, tester, prefix, *, supply=None):
        """Check one control procedure from its first PDU up to Instant."""
        previous = self.last_cssn
        assert previous is not None
        for _ in range(100):
            if supply is not None:
                supply()
            _, control = await self.receive(tester)
            if control is not None:
                break
            assert self.last_cssn == previous
        else:
            raise AssertionError('no BIG control PDU within 100 observed events')
        assert len(control) == len(prefix) + 2 and control[:-2] == prefix
        cssn = (previous + 1) % 8
        assert self.last_cssn == cssn
        instant = int.from_bytes(control[-2:], 'little')
        distance = (instant - (self.counter - 1)) & 0xFFFF
        assert 6 <= distance < 0x7FFF
        for elapsed in range(1, distance):
            if supply is not None:
                supply()
            _, repeated = await self.receive(tester)
            assert self.last_cssn == cssn
            # Six consecutive copies are mandatory. Further repetitions before
            # Instant are optional and carry the same CSSN and payload.
            assert repeated == control or (elapsed >= 6 and repeated is None)

    async def transmit(
        self,
        tester,
        payloads=None,
        *,
        control=None,
        cssn=0,
        bad_crc=False,
        mic_error=False,
    ):
        payloads = payloads or {}
        for offset, number, subevent in self.schedule():
            counter = self.associated(subevent) * self.bn + subevent % self.bn
            value = payloads.get(
                (number, subevent), payloads.get(number, (2 if self.framed else 1, b''))
            )
            if value is None:
                continue
            llid, data = value
            header = llid | cssn << 2 | (0x20 if control else 0)
            data = self.crypt(number, header, data, counter)
            if mic_error and data and self.giv:
                data = data[:-1] + bytes([data[-1] ^ 1])
            await tester.transmit(
                channels(
                    self.counter, self.address(number), self.channel_map, self.nse
                )[subevent],
                bytes([header, len(data)]) + data,
                start_us=self.anchor + offset,
                bad_crc=bad_crc(subevent) if callable(bad_crc) else bad_crc,
                **self.parameters(number),
            )
        if control:
            header = 3 | cssn << 2
            data = self.crypt(0, header, control, self.counter * self.bn)
            await tester.transmit(
                channels(self.counter, self.address(0), self.channel_map, 1)[0],
                bytes([header, len(data)]) + data,
                start_us=self.anchor + self.control_offset,
                **self.parameters(0),
            )
        self.next_event()


def framed_sdu(data, offset=1000):
    return bytes([2, len(data) + 3]) + offset.to_bytes(3, 'little') + data


class FramedSource:
    """Independent SDU segmenter for Lower Tester timing/length changes."""

    def __init__(self, max_pdu):
        self.max_pdu = max_pdu
        self.pending = deque()

    def enqueue(self, data, reference_us):
        self.pending.append((bytes(data), reference_us, False))

    def pdu(self, anchor):
        payload = bytearray()
        while self.pending:
            data, reference, continuation = self.pending[0]
            overhead = 2 if continuation else 5
            available = self.max_pdu - len(payload) - overhead
            if available < 0 or (data and not available):
                break
            segment, remaining = data[:available], data[available:]
            complete = not remaining
            payload += bytes(
                [int(continuation) | int(complete) << 1, len(segment) + overhead - 2]
            )
            if not continuation:
                assert 0 < anchor - reference < 1 << 24
                payload += (anchor - reference).to_bytes(3, 'little')
            payload += segment
            self.pending.popleft()
            if remaining:
                self.pending.appendleft((remaining, reference, True))
                break
        return bytes(payload)


def iso_packet(handle, sequence, data, *, timestamp=None):
    return hci.HCI_IsoDataPacket(
        handle,
        len(data) + (8 if timestamp is not None else 4),
        data,
        2,
        time_stamp=timestamp,
        packet_sequence_number=sequence,
        iso_sdu_length=len(data),
        packet_status_flag=0,
    )


async def data_path(tester, handle, direction):
    return await tester.command(
        hci.HCI_LE_Setup_ISO_Data_Path_Command(
            handle, direction, 0, hci.CodingFormat(hci.CodecID.TRANSPARENT), 0, b''
        )
    )


async def configure_periodic(
    tester,
    handle=0,
    *,
    interval=16,
    secondary_phy=1,
    data=b'',
    pawr=False,
    extended=True,
):
    await tester.command(
        hci.HCI_LE_Set_Extended_Advertising_Parameters_Command(
            handle,
            0,
            0x100,
            0x100,
            1,
            hci.OwnAddressType.PUBLIC,
            hci.AddressType.PUBLIC_DEVICE,
            hci.Address('00:00:00:00:00:00'),
            0,
            0x7F,
            1,
            0,
            secondary_phy,
            handle,
            0,
        )
    )
    if pawr:
        await tester.command(
            hci.HCI_LE_Set_Periodic_Advertising_Parameters_V2_Command(
                handle, interval, interval, 0, 1, 0, 6, 8, 1
            )
        )
    else:
        await tester.command(
            hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(
                handle, interval, interval, 0
            )
        )
    if data:
        await tester.command(
            hci.HCI_LE_Set_Periodic_Advertising_Data_Command(handle, 3, data)
        )
    await tester.command(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, handle))
    if extended:
        await tester.command(
            hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [handle], [0], [0])
        )


def create_command(*, test=False, **changes):
    values = dict(
        big_handle=0,
        advertising_handle=0,
        num_bis=1,
        sdu_interval=10000,
        max_sdu=32,
        phy=1,
        packing=0,
        framing=0,
        encryption=0,
        broadcast_code=bytes(16),
    )
    if test:
        values.update(iso_interval=8, nse=3, max_pdu=32, bn=1, irc=3, pto=0)
    else:
        values.update(max_transport_latency=80, rtn=2)
    values.update(changes)
    return (
        hci.HCI_LE_Create_BIG_Test_Command if test else hci.HCI_LE_Create_BIG_Command
    )(**values)


async def acquire_broadcaster(
    tester,
    *,
    advertising_handle=0,
    big_handle=0,
    encrypted=False,
    framed=False,
    test=True,
    phy=1,
    num_bis=1,
    interval=8,
    sdu_interval=10000,
    max_sdu=None,
    max_pdu=None,
    nse=3,
    irc=3,
    packing=0,
    rtn=2,
    latency_ms=80,
    expected_framed=None,
    configured=False,
):
    await tester.masks()
    if not configured:
        await configure_periodic(
            tester, advertising_handle, interval=max(16, interval * 2)
        )
    max_sdu = (19 if framed else 32) if max_sdu is None else max_sdu
    max_pdu = (37 if framed else 32) if max_pdu is None else max_pdu
    if test:
        command = hci.HCI_LE_Create_BIG_Test_Command(
            big_handle,
            advertising_handle,
            num_bis,
            sdu_interval,
            interval,
            nse,
            max_sdu,
            max_pdu,
            phy,
            packing,
            int(framed),
            1,
            irc,
            0,
            int(encrypted),
            CODE if encrypted else bytes(16),
        )
    else:
        command = hci.HCI_LE_Create_BIG_Command(
            big_handle,
            advertising_handle,
            num_bis,
            sdu_interval,
            max_sdu,
            latency_ms,
            rtn,
            phy,
            packing,
            int(framed),
            int(encrypted),
            CODE if encrypted else bytes(16),
        )
    await tester.command(command)
    complete = await tester.event(hci.HCI_LE_Create_BIG_Complete_Event)
    assert complete.status == 0 and complete.big_handle == big_handle
    # Follow actual announcements and their SyncInfo, then inspect the first
    # periodic PDU's BIGInfo. No live Controller context is consulted.
    big = await observe_broadcaster(tester, sid=advertising_handle)
    assert big.framed == (framed if expected_framed is None else expected_framed)
    assert bool(big.giv) == encrypted
    assert (
        big.num_bis == num_bis
        and big.sdu_interval == sdu_interval
        and big.max_sdu == max_sdu
    )
    assert big.phy == {1: Phy.LE_1M, 2: Phy.LE_2M, 4: Phy.LE_CODED}[phy]
    assert 4 <= big.interval <= 3200 and 1 <= big.max_pdu <= 251
    assert big.nse > 0 and big.bn > 0 and big.nse % big.bn == 0 and big.irc > 0
    return big, complete


async def periodic_packet(tester, sid=0):
    for _ in range(20):
        primary = await tester.expect_pdu(kind=7, duration_us=400000)
        if int.from_bytes(decode_extended(primary.pdu)['adi'], 'little') >> 12 == sid:
            break
    else:
        raise AssertionError(f'no advertising set with SID {sid}')
    _, auxiliary, fields = await follow_auxiliary(
        tester, primary, decode_extended(primary.pdu)
    )
    train = PeriodicTrain.from_advertisement(auxiliary, fields)
    packet = await tester.receive(
        train.channel,
        start_us=max(tester.radio.now_us + 1, train.anchor - 1),
        duration_us=train.anchor - tester.radio.now_us + 1000,
        **train.parameters,
    )
    assert packet is not None
    return packet


async def observe_broadcaster(tester, sid=0):
    big = BigTrain.from_periodic(await periodic_packet(tester, sid))
    assert big is not None
    big.align(tester.radio.now_us)
    return big


async def terminate_broadcaster(tester, big, handle=0, supply=None):
    # One idle event establishes the preceding sequence number and leaves a
    # full interval for the command before the next event preparation boundary.
    big.align(tester.radio.now_us)
    previous_cssn = big.last_cssn
    _, control = await big.receive(tester)
    assert control is None and (previous_cssn is None or big.last_cssn == previous_cssn)
    await tester.command(hci.HCI_LE_Terminate_BIG_Command(handle, 0x13))
    await big.receive_control(tester, b'\x01\x13', supply=supply)
    # Probe the former BIS subevents at Instant, including for long intervals.
    # A completion event alone cannot show whether the radio actually stopped.
    assert tester.radio.now_us < big.anchor
    assert (
        tester.take(lambda p: isinstance(p, hci.HCI_LE_Terminate_BIG_Complete_Event))
        is None
    )
    payloads, control = await big.receive(tester, required=False)
    assert not payloads and control is None
    event = await tester.event(hci.HCI_LE_Terminate_BIG_Complete_Event)
    assert event.big_handle == handle and event.reason == 0x16


async def finish_receiver(tester, peer, *, terminate=True):
    if terminate:
        await tester.command(hci.HCI_LE_BIG_Terminate_Sync_Command(peer.big_handle))
    await tester.command(
        hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(peer.sync_handle)
    )
    while (
        tester.take(
            lambda value: isinstance(
                value,
                (
                    hci.HCI_LE_Periodic_Advertising_Report_Event,
                    hci.HCI_LE_BIGInfo_Advertising_Report_Event,
                ),
            )
            and value.sync_handle == peer.sync_handle
        )
        is not None
    ):
        pass
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(0, 0, 0, 0))


class IsoBroadcaster:
    def __init__(self, tester, *, encrypted=False, framed=False, **changes):
        now = tester.radio.now_us
        generator = Random(now)
        train = PeriodicTrain(now + 4000, interval=16)
        self.advertiser = PeriodicAdvertiser(
            train, now + 100, 20000, address=LOWER_PERIPHERAL_PUBLIC_ADDRESS
        )
        defaults = dict(
            anchor=now + 9000,
            framed=framed,
            max_sdu=19 if framed else 32,
            max_pdu=37 if framed else 32,
            giv=generator.randbytes(8) if encrypted else b'',
            gskd=generator.randbytes(16) if encrypted else b'',
        )
        self.big = BigTrain(**(defaults | changes))
        train.interval = max(16, self.big.interval * 2)
        self.periodic_enabled = True
        self.periodic_data = b''
        self.periodic_update = None
        self.big_update = None
        self.include_info = True

    async def step(self, tester, payloads=None, *, omit_bis=False, **options):
        train = self.advertiser.train
        if train.anchor < self.big.anchor:
            if not self.periodic_enabled:
                train.next_event()
                return False
            await self.advertiser.announce_until(tester, train.anchor)
            acad = self.big.big_info(train.anchor) if self.include_info else b''
            if self.periodic_update is not None:
                channel_map, instant = self.periodic_update
                if train.counter == instant:
                    train.channel_map = channel_map
                    self.periodic_update = None
                else:
                    acad += (
                        bytes([8, 0x28])
                        + channel_map.to_bytes(5, 'little')
                        + instant.to_bytes(2, 'little')
                    )
            await tester.transmit(
                train.channel,
                ext_pdu(acad=acad, data=self.periodic_data),
                start_us=train.anchor,
                **train.parameters,
            )
            train.next_event()
            return False
        await self.advertiser.announce_until(tester, self.big.anchor)
        if self.big_update is not None and self.big.counter >= self.big_update[1]:
            self.big.channel_map = self.big_update[0]
            self.big_update = None
        if omit_bis:
            await tester.wait(
                max(
                    0,
                    self.big.anchor
                    + self.big.nse * self.big.sub_interval
                    - tester.radio.now_us,
                )
            )
            self.big.next_event()
        else:
            await self.big.transmit(tester, payloads, **options)
        return True

    async def until_event(self, tester, event_type, **options):
        for _ in range(100):
            event = tester.take(lambda value: isinstance(value, event_type))
            if event is not None:
                return event
            await self.step(tester, **options)
        raise AssertionError(f'peer did not receive {event_type.__name__}')


async def acquire_receiver(
    tester,
    *,
    encrypted=False,
    framed=False,
    encryption=None,
    big_handle=0,
    optional_info_intervals=0,
    **changes,
):
    peer, sync_handle = await acquire_periodic_receiver(
        tester,
        encrypted=encrypted,
        framed=framed,
        optional_info_intervals=optional_info_intervals,
        **changes,
    )
    if sync_handle is None:
        return peer, None
    await tester.command(
        hci.HCI_LE_BIG_Create_Sync_Command(
            big_handle,
            sync_handle,
            int(encrypted if encryption is None else encryption),
            CODE if encrypted else bytes(16),
            0,
            max(10, (3 * peer.big.interval_us + 9999) // 10000),
            [1],
        )
    )
    synchronized = await peer.until_event(tester, hci.HCI_LE_BIG_Sync_Established_Event)
    peer.big_handle = big_handle
    return peer, synchronized


async def acquire_periodic_receiver(
    tester,
    *,
    encrypted=False,
    framed=False,
    expect_big_info=True,
    omit_bis=False,
    optional_info_intervals=0,
    **changes,
):
    await tester.masks()
    await tester.command(
        hci.HCI_LE_Set_Extended_Scan_Parameters_Command(0, 0, 1, [0], [16], [16])
    )
    await tester.command(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 0, 0, 0))
    peer = IsoBroadcaster(tester, encrypted=encrypted, framed=framed, **changes)
    await peer.advertiser.announce_until(tester, peer.advertiser.train.anchor)
    await tester.event(hci.HCI_LE_Extended_Advertising_Report_Event)
    await tester.command(
        hci.HCI_LE_Periodic_Advertising_Create_Sync_Command(
            0,
            3,
            LOWER_PERIPHERAL_PUBLIC_ADDRESS.address_type,
            LOWER_PERIPHERAL_PUBLIC_ADDRESS,
            0,
            max(10, (3 * peer.advertiser.train.interval * 1250 + 9999) // 10000),
            0,
        )
    )
    established = await peer.until_event(
        tester,
        hci.HCI_LE_Periodic_Advertising_Sync_Established_Event,
        omit_bis=omit_bis,
    )
    assert established.status == 0
    peer.sync_handle = established.sync_handle
    if expect_big_info:
        pair = (
            hci.HCI_LE_Periodic_Advertising_Report_Event,
            hci.HCI_LE_BIGInfo_Advertising_Report_Event,
        )
        report = await tester.expect(lambda value: isinstance(value, pair))
        assert isinstance(report, pair[0])
        if optional_info_intervals:
            # LL/BIS/SNC/BV-22-C permits an immediate Pass when the receiver
            # ignores the BIGInfo for 20 periodic advertising intervals.
            deadline = peer.advertiser.train.counter + optional_info_intervals
            while True:
                info = tester.take(lambda value: isinstance(value, pair))
                if isinstance(info, pair[1]):
                    break
                if isinstance(info, pair[0]):
                    report = info
                elif peer.advertiser.train.counter >= deadline:
                    return peer, None
                else:
                    await peer.step(tester, omit_bis=omit_bis)
        else:
            info = await tester.expect(lambda value: isinstance(value, pair))
            assert isinstance(info, pair[1])
        assert report.sync_handle == info.sync_handle == established.sync_handle
        assert (
            info.num_bis,
            info.nse,
            info.bn,
            info.pto,
            info.irc,
            info.iso_interval,
        ) == (
            peer.big.num_bis,
            peer.big.nse,
            peer.big.bn,
            peer.big.pto,
            peer.big.irc,
            peer.big.interval,
        )
        assert (
            info.max_pdu,
            info.max_sdu,
            info.sdu_interval,
            info.phy,
            info.framing,
            info.encryption,
        ) == (
            peer.big.max_pdu,
            peer.big.max_sdu,
            peer.big.sdu_interval,
            peer.big.phy,
            int(peer.big.framed),
            int(bool(peer.big.giv)),
        )
    return peer, established.sync_handle
