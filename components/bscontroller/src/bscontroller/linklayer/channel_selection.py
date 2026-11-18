# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LE Channel Selection Algorithms #1 and #2 (Core 6.3, Vol 6, Part B, 4.5.8)."""

from bscontroller.linklayer import (
    LL_CHANNEL_ID_BITS,
    LL_CHANNEL_ID_MASK,
    LL_CSA2_MAM_MULTIPLIER,
    LL_CSA2_MAM_ROUNDS,
    LL_DATA_CHANNEL_COUNT,
    LL_EVENT_COUNTER_MASK,
)
from bscontroller.linklayer.parameters import valid_channel_map
from bscontroller.radio.base import PHY_OCTET_BITS, PHY_OCTET_MASK


def channel_selection_1(unmapped_channel: int, channel_map: int) -> int:
    """Core Vol 6, Part B, 4.5.8.2; remap an unused channel by unmappedChannel mod
    numUsedChannels.

    The caller owns lastUnmappedChannel and its per-event hopIncrement step.
    """
    if channel_map & (1 << unmapped_channel):
        return unmapped_channel
    enabled = [
        index for index in range(LL_DATA_CHANNEL_COUNT) if channel_map & (1 << index)
    ]
    return enabled[unmapped_channel % len(enabled)]


def _permute_mam(prn: int, channel_id: int) -> int:
    prn = int(
        f'{prn >> PHY_OCTET_BITS:0{PHY_OCTET_BITS}b}'[::-1], 2
    ) << PHY_OCTET_BITS | int(f'{prn & PHY_OCTET_MASK:0{PHY_OCTET_BITS}b}'[::-1], 2)
    return (LL_CSA2_MAM_MULTIPLIER * prn + channel_id) & LL_CHANNEL_ID_MASK


def _event_prn(counter: int, channel_id: int) -> int:
    prn = (counter & LL_EVENT_COUNTER_MASK) ^ channel_id
    for _ in range(LL_CSA2_MAM_ROUNDS):
        prn = _permute_mam(prn, channel_id)
    return prn


def channel_selection_2(counter: int, access_address: int, channel_map: int) -> int:
    """Core Vol 6, Part B, 4.5.8.3; ascending channel remapping."""
    if not valid_channel_map(channel_map):
        raise ValueError('invalid channel map')
    channel_id = (access_address >> LL_CHANNEL_ID_BITS) ^ (
        access_address & LL_CHANNEL_ID_MASK
    )
    prn = _event_prn(counter, channel_id) ^ channel_id
    unmapped = prn % LL_DATA_CHANNEL_COUNT
    if channel_map & (1 << unmapped):
        return unmapped
    enabled = [
        index for index in range(LL_DATA_CHANNEL_COUNT) if channel_map & (1 << index)
    ]
    return enabled[(len(enabled) * prn) >> LL_CHANNEL_ID_BITS]


def isochronous_channels(
    counter: int, access_address: int, channel_map: int, count: int
) -> tuple[int, ...]:
    """ISO event and subsequent subevents (Vol 6, Part B, 4.5.8.3.5–6).

    One sequence belongs to a single BIS or CIS event; its caller owns the
    event counter and applies channel-map changes at the indicated instant.
    """
    if not valid_channel_map(channel_map) or count < 1:
        raise ValueError('invalid ISO channel selection parameters')
    enabled = tuple(n for n in range(LL_DATA_CHANNEL_COUNT) if channel_map & 1 << n)
    channel_id = (access_address >> LL_CHANNEL_ID_BITS) ^ (
        access_address & LL_CHANNEL_ID_MASK
    )
    state = _event_prn(counter, channel_id)
    prn = state ^ channel_id
    unmapped = prn % LL_DATA_CHANNEL_COUNT
    index = (
        enabled.index(unmapped)
        if unmapped in enabled
        else len(enabled) * prn >> LL_CHANNEL_ID_BITS
    )
    channels = [enabled[index]]
    distance = max(1, min(3, len(enabled) - 5), min(11, (len(enabled) - 10) // 2))
    for _ in range(count - 1):
        state = _permute_mam(state, channel_id)
        prn = state ^ channel_id
        index = (
            index + distance + (prn * (len(enabled) - 2 * distance + 1) >> 16)
        ) % len(enabled)
        channels.append(enabled[index])
    return tuple(channels)
