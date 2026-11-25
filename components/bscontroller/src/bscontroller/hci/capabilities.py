# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""HCI buffer capacity and capability encodings (Core 6.3, Vol 4, Part E)."""

HCI_SUPPORTED_STATE_MASK = (1 << 42) - 1

HCI_RSSI_NOT_AVAILABLE = 0x7F


# Vol 4, Part E, 7.3.93/94: HCI timeout fields use 10 ms units.
HCI_AUTHENTICATED_PAYLOAD_TIMEOUT_UNIT_US = 10_000

# Vol 4, Part E, 7.3.30: zero disables the automatic flush timeout.
HCI_AUTOMATIC_FLUSH_TIMEOUT_UNIT_US = 625
HCI_AUTOMATIC_FLUSH_TIMEOUT_MAX = 0x07FF
