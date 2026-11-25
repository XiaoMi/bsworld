# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Deterministic RF doubles and controller setup for implementation tests."""

from collections import deque
from dataclasses import replace
from itertools import pairwise

from bscontroller import hci
from bscontroller.hci import HCI_LE_ADV_INTERVAL_MIN, HCI_LE_PERIODIC_SYNC_TIMEOUT_MIN
from bscontroller.linklayer import (
    PDU_ACCESS_ADDRESS_CODED_LSB_BITS,
    PDU_ACCESS_ADDRESS_CODED_LSB_ONES_MIN,
    PDU_ACCESS_ADDRESS_CODED_TRANSITION_BITS,
    PDU_ACCESS_ADDRESS_CODED_TRANSITIONS_MAX,
    PDU_ACCESS_ADDRESS_SIZE,
    PDU_ADV_ACCESS_ADDRESS,
)
from bscontroller.linklayer.bis import BigInfo
from bscontroller.linklayer.connection import Connection
from bscontroller.linklayer.parameters import Parameters
from bscontroller.radio.base import (
    PHY_CODED_S8_FACTOR,
    PHY_OCTET_BITS,
    Phy,
    RadioSwitch,
)

PEER_ADDRESS = hci.Address('C0:11:22:33:44:55', hci.AddressType.RANDOM_DEVICE)
TX_POWER_NO_PREFERENCE = (
    hci.HCI_LE_Set_Extended_Advertising_Parameters_Command.TX_POWER_NO_PREFERENCE
)

# Fixed wire vector for isolated implementation checks, not Controller defaults.
PARAMETERS = Parameters(
    0x9A328370,
    0x123456,
    1,
    0,
    24,
    0,
    100,
    (1 << 37) - 1,
    7,
)


class FakeRadio:
    now_us = 0

    def __init__(self):
        self.sent = []
        self.incoming = deque()
        self.switch = RadioSwitch()

    def earliest_start_us(self, channel, phy, *, transmit):
        return self.switch.earliest_start_us(
            self.now_us, channel, phy, transmit=transmit
        )

    async def advance_to_us(self, target):
        self.now_us = target
        return target

    async def transmit(self, channel, pdu, *, start_us=None, **parameters):
        phy = Phy(parameters.get('phy', Phy.LE_1M))
        ready = self.earliest_start_us(channel, phy, transmit=True)
        start_us = ready if start_us is None else start_us
        assert start_us >= ready
        self.sent.append((start_us, channel, pdu, parameters))
        self.now_us = (
            start_us
            + Phy(parameters.get('phy', Phy.LE_1M)).airtime_us(
                len(pdu), parameters.get('coding', PHY_CODED_S8_FACTOR)
            )
            - 1
        )
        self.switch.complete(channel, phy, transmit=True, end_us=self.now_us + 1)
        return self.now_us + 1

    async def receive(self, channel, until_us, **parameters):
        phy = Phy(parameters.get('phy', Phy.LE_1M))
        start = max(
            parameters.get('start_us') or self.now_us + 1,
            self.earliest_start_us(channel, phy, transmit=False),
        )
        if start >= until_us:
            await self.advance_to_us(until_us)
            return None
        if self.incoming:
            packet = self.incoming.popleft()
            self.now_us = packet.end_us - 1
            self.switch.complete(channel, phy, transmit=False, end_us=packet.end_us)
            return packet
        self.now_us = until_us
        self.switch.complete(channel, phy, transmit=False, end_us=self.now_us + 1)
        return None


def host_notifications(events):
    """Keep ACL completion credits queued for the tests that check flow control."""
    return [
        event
        for event in events
        if not isinstance(event, hci.HCI_Number_Of_Completed_Packets_Event)
    ]


def take_host_notification(events, event_type, **fields):
    notifications = host_notifications(events)
    assert notifications, f'expected {event_type.__name__}, Host queue empty'
    event = notifications[0]
    assert isinstance(event, event_type), f'expected {event_type.__name__}: {event}'
    for name, expected in fields.items():
        actual = getattr(event, name)
        assert actual == expected, (name, actual, expected)
    events.remove(event)
    return event


def check_access_address(value):
    """Independent bit-string oracle for generated addresses, including Coded PHY."""
    assert (value ^ PDU_ADV_ACCESS_ADDRESS).bit_count() > 1
    bits = f'{value:0{PDU_ACCESS_ADDRESS_SIZE * PHY_OCTET_BITS}b}'
    assert '0' * 7 not in bits and '1' * 7 not in bits
    assert len(set(value.to_bytes(PDU_ACCESS_ADDRESS_SIZE, 'little'))) > 1
    assert sum(left != right for left, right in pairwise(bits)) <= 24
    assert sum(left != right for left, right in pairwise(bits[:6])) >= 2
    assert bits[-PDU_ACCESS_ADDRESS_CODED_LSB_BITS:].count('1') >= (
        PDU_ACCESS_ADDRESS_CODED_LSB_ONES_MIN
    )
    coded = bits[-PDU_ACCESS_ADDRESS_CODED_TRANSITION_BITS:]
    assert sum(left != right for left, right in pairwise(coded)) <= (
        PDU_ACCESS_ADDRESS_CODED_TRANSITIONS_MAX
    )


def extended_parameters(*, coding_options=None, **changes):
    values = {
        'advertising_handle': 0,
        'advertising_event_properties': 0,
        'primary_advertising_interval_min': HCI_LE_ADV_INTERVAL_MIN,
        'primary_advertising_interval_max': HCI_LE_ADV_INTERVAL_MIN,
        'primary_advertising_channel_map': 1,
        'own_address_type': hci.OwnAddressType.PUBLIC,
        'peer_address_type': hci.AddressType.PUBLIC_DEVICE,
        'peer_address': hci.Address.ANY,
        'advertising_filter_policy': 0,
        'advertising_tx_power': TX_POWER_NO_PREFERENCE,
        'primary_advertising_phy': Phy.LE_1M,
        'secondary_advertising_max_skip': 0,
        'secondary_advertising_phy': Phy.LE_1M,
        'advertising_sid': 3,
        'scan_request_notification_enable': 0,
    }
    if coding_options is None:
        return hci.HCI_LE_Set_Extended_Advertising_Parameters_Command(
            **(values | changes)
        )
    return hci.HCI_LE_Set_Extended_Advertising_Parameters_V2_Command(
        **(values | changes),
        primary_advertising_phy_options=coding_options[0],
        secondary_advertising_phy_options=coding_options[1],
    )


def create_connection(*, extended=False, **changes):
    values = {
        'initiator_filter_policy': 0,
        'own_address_type': hci.OwnAddressType.PUBLIC,
        'peer_address_type': hci.AddressType.RANDOM_DEVICE,
        'peer_address': PEER_ADDRESS,
    }
    if extended:
        values |= {
            'initiating_phys': hci.PhyBit.LE_1M,
            'scan_intervals': [48],
            'scan_windows': [48],
            'connection_interval_mins': [24],
            'connection_interval_maxs': [24],
            'max_latencies': [0],
            'supervision_timeouts': [100],
            'min_ce_lengths': [0],
            'max_ce_lengths': [0],
        }
        return hci.HCI_LE_Extended_Create_Connection_Command(**(values | changes))
    values |= {
        'le_scan_interval': 48,
        'le_scan_window': 48,
        'connection_interval_min': 24,
        'connection_interval_max': 24,
        'max_latency': 0,
        'supervision_timeout': 100,
        'min_ce_length': 0,
        'max_ce_length': 0,
    }
    return hci.HCI_LE_Create_Connection_Command(**(values | changes))


def create_sync(**changes):
    values = {
        'options': 0,
        'advertising_sid': 3,
        'advertiser_address_type': hci.AddressType.RANDOM_DEVICE,
        'advertiser_address': PEER_ADDRESS,
        'skip': 0,
        'sync_timeout': HCI_LE_PERIODIC_SYNC_TIMEOUT_MIN,
        'sync_cte_type': 0,
    }
    return hci.HCI_LE_Periodic_Advertising_Create_Sync_Command(**(values | changes))


def add_connection(first, index, role=hci.Role.CENTRAL):
    first.report_created()
    link = first.link
    result = Connection(
        link,
        replace(
            first.parameters, access_address=first.parameters.access_address ^ index
        ),
        hci.Address(f'F0:F1:F2:F3:F4:{index + 2:02X}', hci.AddressType.PUBLIC_DEVICE),
        role,
        0,
    )
    link.connections[result.handle] = result
    result.report_created()
    return result


def bis_info(**changes):
    return replace(
        BigInfo(
            offset_us=990,
            iso_interval=8,
            num_bis=1,
            nse=2,
            bn=1,
            sub_interval_us=600,
            pto=0,
            bis_spacing_us=1200,
            irc=2,
            max_pdu=32,
            seed_access_address=0x12345678,
            sdu_interval_us=10000,
            max_sdu=32,
            base_crc_init=0x1234,
            channel_map=0x1FFFFFFFFF,
            phy=Phy.LE_1M,
        ),
        **changes,
    )
