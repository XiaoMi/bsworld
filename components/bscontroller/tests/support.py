# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Host payload builders for Controller tests; independent wire values."""

L2CAP_LE_U_DYNAMIC_CID_RANGE_START = 0x0040
AD_COMPLETE_LOCAL_NAME = 0x09
AD_CHANNEL_MAP_UPDATE_INDICATION = 0x28
AD_PERIODIC_RESPONSE_TIMING_INFORMATION = 0x32


def advertising_data(structures):
    return b''.join(bytes([len(data) + 1, kind]) + data for kind, data in structures)


# LE Meta subevent codes of the [v2] events that only a PAwR-aware Host unmasks
# (Vol 4, Part E, 7.7.65.10, .14, .15, .24).
LE_V2_EVENT_CODES = (0x24, 0x25, 0x26, 0x29, 0x2A)
LE_EVENT_MASK_ALL = (1 << 64) - 1
# A Host that predates PAwR: every LE event except the [v2] versions, so the
# Controller reports with the [v1] events.
LE_EVENT_MASK_V1 = LE_EVENT_MASK_ALL & ~sum(
    1 << (code - 1) for code in LE_V2_EVENT_CODES
)


# Expected HCI wire values for this IUT, independent of implementation constants.
CONTROLLER_COMPANY_IDENTIFIER = 0xFFFF
AUTHENTICATED_PAYLOAD_TIMEOUT_DEFAULT = 3000
AUTHENTICATED_PAYLOAD_TIMEOUT_UNIT_US = 10_000
