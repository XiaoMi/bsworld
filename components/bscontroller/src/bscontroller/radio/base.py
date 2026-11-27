# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Bluetooth LE radio interface, independent of HCI and the simulator."""

from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol

from bscontroller.radio.packet import (
    LL_DATA_CHANNEL_COUNT,
    PDU_ACCESS_ADDRESS_SIZE,
    PDU_ADV_ACCESS_ADDRESS,
    PDU_ADV_CRC_INIT,
    PDU_ADV_EXT_PAYLOAD_SIZE_MAX,
    PDU_CRC_SIZE,
    PDU_HEADER_SIZE,
    PDU_PREAMBLE_1M_SIZE,
    PDU_PREAMBLE_2M_SIZE,
    PHY_OCTET_BITS,
)

# PHY airtime: Core Vol 6, Part B, 2. Bit counts are before FEC encoding.
PHY_1M_OCTET_TIME_US = 8
PHY_2M_OCTET_TIME_US = 4
# Preamble, Access Address, Coding Indicator, TERM1.
PHY_CODED_FEC1_TIME_US = 376
# TERM2 is sent with the payload's S=2/S=8 coding.
PHY_CODED_TERM2_SIZE_BITS = 3
# Preamble plus Access Address; excludes Coding Indicator on the Coded PHY.
PHY_1M_ADDRESS_TIME_US = (
    PDU_PREAMBLE_1M_SIZE + PDU_ACCESS_ADDRESS_SIZE
) * PHY_1M_OCTET_TIME_US
PHY_2M_ADDRESS_TIME_US = (
    PDU_PREAMBLE_2M_SIZE + PDU_ACCESS_ADDRESS_SIZE
) * PHY_2M_OCTET_TIME_US
PHY_CODED_ADDRESS_TIME_US = 336
# Core Vol 6, Part A, 2.1: primary advertising-channel center frequencies.
PHY_ADV_CHANNEL_FREQUENCIES_MHZ = {37: 2402, 38: 2426, 39: 2480}

# Data-channel centers skip primary advertising channel 38 at 2426 MHz.
PHY_DATA_CHANNEL_BASE_MHZ = 2404
PHY_DATA_CHANNEL_SPACING_MHZ = 2
PHY_DATA_CHANNEL_GAP_INDEX = 11
# Uncoded on-air bit rates.
PHY_1M_BITRATE_BPS = 1_000_000
PHY_2M_BITRATE_BPS = 2_000_000


# LE Coded payload coding factors, Core Vol 6, Part B, 2.2.
PHY_CODED_S2_FACTOR = 2
PHY_CODED_S8_FACTOR = 8
# Common bit-field and clock-unit conversions.
PHY_OCTET_MASK = (1 << PHY_OCTET_BITS) - 1
PHY_TIME_UNITS_PER_SECOND = 1_000_000
# Hardware switch preparation and supported transmit-power range.
# The switch time is absorbed by existing idle time, not added to T_IFS.
RADIO_SWITCH_TIME_US = 60
RADIO_TX_POWER_MIN_DBM = -127
RADIO_TX_POWER_MAX_DBM = 20


def channel_frequency_mhz(channel: int) -> int:
    if channel in PHY_ADV_CHANNEL_FREQUENCIES_MHZ:
        return PHY_ADV_CHANNEL_FREQUENCIES_MHZ[channel]
    if not 0 <= channel < LL_DATA_CHANNEL_COUNT:
        raise ValueError(f'invalid Bluetooth LE channel: {channel}')
    offset = channel + (channel >= PHY_DATA_CHANNEL_GAP_INDEX)
    return PHY_DATA_CHANNEL_BASE_MHZ + offset * PHY_DATA_CHANNEL_SPACING_MHZ


class Phy(IntEnum):
    LE_1M = 1
    LE_2M = 2
    LE_CODED = 3

    @property
    def address_us(self) -> int:
        return {
            Phy.LE_1M: PHY_1M_ADDRESS_TIME_US,
            Phy.LE_2M: PHY_2M_ADDRESS_TIME_US,
            Phy.LE_CODED: PHY_CODED_ADDRESS_TIME_US,
        }[self]

    def airtime_us(self, pdu_size: int, coding: int = PHY_CODED_S8_FACTOR) -> int:
        """Preamble through CRC (and TERM2 for Coded), Core Vol 6, Part B, 2."""
        if (
            not PDU_HEADER_SIZE
            <= pdu_size
            <= PDU_HEADER_SIZE + PDU_ADV_EXT_PAYLOAD_SIZE_MAX
        ):
            raise ValueError('invalid LE PDU size')
        if self == Phy.LE_1M:
            return (
                PDU_PREAMBLE_1M_SIZE + PDU_ACCESS_ADDRESS_SIZE + pdu_size + PDU_CRC_SIZE
            ) * PHY_1M_OCTET_TIME_US
        if self == Phy.LE_2M:
            return (
                PDU_PREAMBLE_2M_SIZE + PDU_ACCESS_ADDRESS_SIZE + pdu_size + PDU_CRC_SIZE
            ) * PHY_2M_OCTET_TIME_US
        if coding not in (PHY_CODED_S2_FACTOR, PHY_CODED_S8_FACTOR):
            raise ValueError('LE Coded requires S=2 or S=8')
        return (
            PHY_CODED_FEC1_TIME_US
            + ((pdu_size + PDU_CRC_SIZE) * PHY_OCTET_BITS + PHY_CODED_TERM2_SIZE_BITS)
            * coding
        )


@dataclass(frozen=True)
class Received:
    """Packet bounds are half-open; now_us remains the actual PHY grant time."""

    pdu: bytes
    rssi: float
    start_us: int
    end_us: int
    valid_crc: bool
    phy: Phy = Phy.LE_1M
    coding: int = 0


@dataclass(frozen=True, slots=True)
class AirPacket:
    """A dewhitened LE PDU and CRC with its radio metadata.

    ``channel`` is the logical LE channel index. RSSI is present on RX only.
    """

    start_us: int
    channel: int
    access_address: int
    phy: Phy
    coding: int
    packet: bytes
    rssi: float | None = None
    crc_valid: bool | None = None


class Radio(Protocol):
    @property
    def now_us(self) -> int: ...

    def earliest_start_us(self, channel: int, phy: Phy, *, transmit: bool) -> int: ...

    async def advance_to_us(self, target_us: int) -> int: ...

    async def transmit(
        self,
        channel: int,
        pdu: bytes,
        *,
        start_us: int | None = None,
        access_address: int = PDU_ADV_ACCESS_ADDRESS,
        crc_init: int = PDU_ADV_CRC_INIT,
        phy: Phy = Phy.LE_1M,
        coding: int = PHY_CODED_S8_FACTOR,
        power_dbm: int = 0,
    ) -> int: ...

    async def receive(
        self,
        channel: int,
        until_us: int,
        *,
        start_us: int | None = None,
        deadline_us: int | None = None,
        access_address: int = PDU_ADV_ACCESS_ADDRESS,
        crc_init: int = PDU_ADV_CRC_INIT,
        accept_bad_crc: bool = False,
        phy: Phy = Phy.LE_1M,
    ) -> Received | None:
        """Bound address search by ``until_us`` and reception by ``deadline_us``."""
        ...


@dataclass
class RadioSwitch:
    """Track the last occupied boundary; no RF operation occurs during switching."""

    switch_time_us: int = RADIO_SWITCH_TIME_US
    state: tuple[int, Phy, bool] | None = None
    end_us: int = 0

    def __post_init__(self) -> None:
        if self.switch_time_us < 0:
            raise ValueError('radio switching time must be nonnegative')

    def earliest_start_us(
        self, now_us: int, channel: int, phy: Phy, *, transmit: bool
    ) -> int:
        switching = self.state is not None and self.state != (channel, phy, transmit)
        return max(now_us + 1, self.end_us + (self.switch_time_us if switching else 0))

    def complete(self, channel: int, phy: Phy, *, transmit: bool, end_us: int) -> None:
        self.state = (channel, phy, transmit)
        self.end_us = end_us
