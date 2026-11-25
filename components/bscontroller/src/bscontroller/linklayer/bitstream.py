# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LE data Access Address generation (Core 6.3, Vol 6, Part B, 2.1.2).

The CRC (3.1.1) belongs to the Radio packet format, see radio.packet.crc24.
"""

from random import Random

from bscontroller.linklayer import (
    PDU_ACCESS_ADDRESS_CODED_LSB_BITS,
    PDU_ACCESS_ADDRESS_CODED_LSB_ONES_MIN,
    PDU_ACCESS_ADDRESS_CODED_TRANSITION_BITS,
    PDU_ACCESS_ADDRESS_CODED_TRANSITIONS_MAX,
    PDU_ACCESS_ADDRESS_CONSECUTIVE_BITS_MAX,
    PDU_ACCESS_ADDRESS_MSB_TRANSITIONS_MIN,
    PDU_ACCESS_ADDRESS_MSB_WINDOW_BITS,
    PDU_ACCESS_ADDRESS_SIZE,
    PDU_ACCESS_ADDRESS_TRANSITIONS_MAX,
    PDU_ADV_ACCESS_ADDRESS,
)
from bscontroller.radio.base import PHY_OCTET_BITS


def generate_access_address(
    generator: Random, used: set[int], *, minimum_distance: int = 1
) -> int:
    """Seeded data Access Address, valid for uncoded and Coded transmission."""
    # Use the caller's seeded RNG to reproduce simulation runs. This checks the
    # address patterns, not physical entropy (Vol 6, Part B, 2.1.2).
    width = PDU_ACCESS_ADDRESS_SIZE * PHY_OCTET_BITS
    while True:
        value = generator.getrandbits(width)
        # Avoid live identities and anything within one bit of the fixed
        # advertising Access Address, before applying waveform constraints.
        if value in used or (value ^ PDU_ADV_ACCESS_ADDRESS).bit_count() <= 1:
            continue
        if minimum_distance > 1 and any(
            (value ^ address).bit_count() < minimum_distance for address in used
        ):
            continue
        if valid_access_address(value):
            return value


def valid_access_address(value: int) -> bool:
    """Waveform constraints for uncoded and Coded PHYs, also used by BIS seeds."""
    width = PDU_ACCESS_ADDRESS_SIZE * PHY_OCTET_BITS
    if not 0 <= value < 1 << width or (value ^ PDU_ADV_ACCESS_ADDRESS).bit_count() <= 1:
        return False
    bits = f'{value:0{width}b}'
    run = PDU_ACCESS_ADDRESS_CONSECUTIVE_BITS_MAX + 1
    transitions = value ^ (value >> 1)
    msb = value >> (width - PDU_ACCESS_ADDRESS_MSB_WINDOW_BITS)
    return not (
        '0' * run in bits
        or '1' * run in bits
        or len(set(value.to_bytes(PDU_ACCESS_ADDRESS_SIZE, 'little'))) == 1
        or (transitions & ((1 << (width - 1)) - 1)).bit_count()
        > PDU_ACCESS_ADDRESS_TRANSITIONS_MAX
        or (
            (msb ^ (msb >> 1)) & ((1 << (PDU_ACCESS_ADDRESS_MSB_WINDOW_BITS - 1)) - 1)
        ).bit_count()
        < PDU_ACCESS_ADDRESS_MSB_TRANSITIONS_MIN
        or (value & ((1 << PDU_ACCESS_ADDRESS_CODED_LSB_BITS) - 1)).bit_count()
        < PDU_ACCESS_ADDRESS_CODED_LSB_ONES_MIN
        or (
            transitions & ((1 << (PDU_ACCESS_ADDRESS_CODED_TRANSITION_BITS - 1)) - 1)
        ).bit_count()
        > PDU_ACCESS_ADDRESS_CODED_TRANSITIONS_MAX
    )
