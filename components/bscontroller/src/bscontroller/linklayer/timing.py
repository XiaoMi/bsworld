# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LE timing and receive-window widening (Core 6.3, Vol 6, Part B, 4.2/4.5)."""

from bscontroller.linklayer import LL_SCA_50_PPM, LL_SCA_PPM
from bscontroller.radio.base import PHY_TIME_UNITS_PER_SECOND


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
