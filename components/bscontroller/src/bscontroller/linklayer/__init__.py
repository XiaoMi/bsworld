# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Bluetooth LE PDUs, on-air procedures, and Link Layer state machines."""

from .pdu import *  # noqa: F403

# Internal handle for the advertising set configured by legacy HCI commands.
LEGACY_ADVERTISING_HANDLE = -1
