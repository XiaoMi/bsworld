# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LE timing and receive-window widening (Core 6.3, Vol 6, Part B, 4.2/4.5)."""

from bscontroller.linklayer import (
    LL_CONN_AUX_WINDOW_DELAY_CODED_US,
    LL_CONN_AUX_WINDOW_DELAY_UNCODED_US,
    LL_EVENT_COUNTER_MASK,
    LL_IFS_US,
    LL_SCA_50_PPM,
    LL_SCA_PPM,
    PDU_ADV_AUX_CONNECT_RSP_SIZE,
    PDU_ADV_CONNECT_SIZE,
    PDU_ADV_SCAN_REQ_SIZE,
)
from bscontroller.radio.base import PHY_CODED_S8_FACTOR, PHY_TIME_UNITS_PER_SECOND, Phy


def window_widening(
    elapsed_us: int, sca: int, *, active: bool = False, local_sca: int = LL_SCA_50_PPM
) -> int:
    """Peer/local clock uncertainty (Core Vol 6, Part B, 4.2.4).

    Sleep timing uses the advertised SCA and 16-us jitter. Adjacent subevents
    use the active clock's 50 ppm and 2-us jitter.
    """
    ppm = 2 * LL_LOCAL_SCA_PPM if active else LL_SCA_PPM[sca] + LL_SCA_PPM[local_sca]
    return (2 if active else LL_CLOCK_JITTER_US) + (
        max(0, elapsed_us) * ppm + PHY_TIME_UNITS_PER_SECOND - 1
    ) // PHY_TIME_UNITS_PER_SECOND


LL_LOCAL_SCA_PPM = 50

LL_CLOCK_JITTER_US = 16

LL_DEFAULT_SCAN_INTERVAL_US = 30_000

LL_DEFAULT_ADV_INTERVAL_US = 100_000

LL_DIRECT_ADV_INTERVAL_US = 2500

LL_RESPONSE_START_TOLERANCE_US = 2

LL_CONN_EVENT_GUARD_US = 500

LL_CONN_RX_WINDOW_UNCODED_US = 100

LL_CONN_RX_WINDOW_CODED_US = 400

# Core Vol 6, Part B, 5.5.1: (Instant - connEventCount) mod 65536 >= 32767 is in the
# past.
LL_INSTANT_PAST_THRESHOLD = 0x7FFF


# Default authenticated payload supervision period, in Link Layer time units.
LL_AUTHENTICATED_PAYLOAD_TIMEOUT_DEFAULT_US = 30_000_000


def fixed_time(time_us: int, phase: int = 0) -> int:
    """Convert to millionths of a microsecond, preserving the fractional phase."""
    return time_us * PHY_TIME_UNITS_PER_SECOND + phase


def split_fixed_time(time: int) -> tuple[int, int]:
    """Round down to a PHY timestamp and retain the fractional remainder."""
    return divmod(time, PHY_TIME_UNITS_PER_SECOND)


def project_anchor(anchor: int, interval: int, events: int) -> tuple[int, int]:
    """Project a fixed-point anchor by a signed number of event intervals."""
    return split_fixed_time(anchor + events * interval)


def auxiliary_window_delay_us(phy: Phy) -> int:
    return (
        LL_CONN_AUX_WINDOW_DELAY_CODED_US
        if phy == Phy.LE_CODED
        else LL_CONN_AUX_WINDOW_DELAY_UNCODED_US
    )


def connect_exchange_us(
    phy: Phy,
    *,
    request_coding: int = PHY_CODED_S8_FACTOR,
    response_coding: int = PHY_CODED_S8_FACTOR,
) -> int:
    """From the start of AUX_CONNECT_REQ to the end of AUX_CONNECT_RSP."""
    return (
        phy.airtime_us(PDU_ADV_CONNECT_SIZE, request_coding)
        + LL_IFS_US
        + phy.airtime_us(PDU_ADV_AUX_CONNECT_RSP_SIZE, response_coding)
    )


def scan_exchange_us(phy: Phy, response_size: int) -> int:
    """Budget a scan request and its possible response at T_IFS."""
    return (
        phy.airtime_us(PDU_ADV_SCAN_REQ_SIZE)
        + LL_IFS_US
        + LL_RESPONSE_START_TOLERANCE_US
        + phy.airtime_us(response_size)
    )


def counter_distance(target: int, reference: int) -> int:
    """Forward distance between 16-bit Link Layer event counters."""
    return (target - reference) & LL_EVENT_COUNTER_MASK


def signed_counter_distance(target: int, reference: int) -> int:
    """Nearest signed distance, with the half-cycle interpreted as negative."""
    distance = counter_distance(target, reference)
    return distance - (LL_EVENT_COUNTER_MASK + 1) if distance & 0x8000 else distance
