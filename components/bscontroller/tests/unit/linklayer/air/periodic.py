# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Lower Tester periodic advertiser for acquisition and synchronization tests."""

from bscontroller import hci
from bscontroller.linklayer import (
    LL_DATA_CHANNEL_MAP_ALL,
    LL_EVENT_COUNTER_MASK,
    LL_PERIODIC_INTERVAL_UNIT_US,
)
from bscontroller.linklayer.advertising_pdu import AuxPtr, ExtendedPdu, SyncInfo
from bscontroller.linklayer.channel_selection import channel_selection_2
from bscontroller.radio.base import Phy
from tests.unit.linklayer.air.harness import PEER
from tests.unit.linklayer.periodic import PERIODIC_INTERVAL, SYNC_AA, SYNC_CRC

PERIODIC_REPORT = hci.HCI_LE_Periodic_Advertising_Report_Event
AUX_CHANNEL = 12


class Train:
    """A Lower Tester periodic advertiser: SyncInfo announcements and AUX_SYNC_IND."""

    def __init__(
        self,
        iut,
        *,
        address=PEER,
        sid=3,
        interval=PERIODIC_INTERVAL,
        counter=0,
        phy=Phy.LE_1M,
    ):
        self.iut, self.address, self.sid = iut, address, sid
        self.interval, self.counter, self.phy = interval, counter, phy
        self.channel_map = LL_DATA_CHANNEL_MAP_ALL
        self.anchor_us = None

    @property
    def interval_us(self):
        return self.interval * LL_PERIODIC_INTERVAL_UNIT_US

    def announce(self, offset_us=30_000):
        """ADV_EXT_IND + AUX_ADV_IND with SyncInfo; the train starts offset later."""
        primary = ExtendedPdu(
            adi=self.sid << 12 | 1, aux_ptr=AuxPtr(AUX_CHANNEL, 3000, self.phy)
        )
        info = SyncInfo(
            offset_us, self.interval, self.channel_map, SYNC_AA, SYNC_CRC, self.counter
        )
        auxiliary = ExtendedPdu(
            address=bytes(self.address),
            random_address=self.address.is_random,
            adi=self.sid << 12 | 1,
            sync_info=info,
        )
        self.iut.lt_tx(primary, phy=Phy.LE_1M)
        self.iut.lt_tx(auxiliary, phy=self.phy, channel=AUX_CHANNEL, after=3000)
        self._offset_us = offset_us

    def first_anchor(self):
        """Anchor of the event SyncInfo described, once the announcement arrived."""
        aux = self.iut.radio.delivered[-1]
        self.anchor_us = aux.start_us + self._offset_us
        return self.anchor_us

    def send(self, data=b'', *, skip=False, rssi=-40, **fields):
        """Schedule this train event (or let it pass silently) and step the anchor."""
        if not skip:
            self.iut.lt_tx(
                ExtendedPdu(data=data, **fields),
                phy=self.phy,
                at=self.anchor_us,
                rssi=rssi,
                access_address=SYNC_AA,
                channel=channel_selection_2(self.counter, SYNC_AA, self.channel_map),
            )
        self.anchor_us += self.interval_us
        self.counter = (self.counter + 1) & LL_EVENT_COUNTER_MASK

    def run_through(self):
        self.iut.run_until(self.anchor_us - self.interval_us + 2000)


def drain_periodic(iut):
    found = [event for event in iut.events if isinstance(event, PERIODIC_REPORT)]
    for event in found:
        iut.events.remove(event)
    return [event.data for event in found]
