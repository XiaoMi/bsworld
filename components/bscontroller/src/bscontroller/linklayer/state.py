# Copyright 2021-2025 Google LLC
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""LE roles and connectionless receive results (Core 6.3, Vol 6, Part B)."""

from dataclasses import dataclass
from enum import IntEnum, IntFlag

from bscontroller.serialization import SpecableEnum


class Role(SpecableEnum):
    CENTRAL = 0
    PERIPHERAL = 1


class AdvertisingProperties(IntFlag):
    CONNECTABLE_ADVERTISING = 1
    SCANNABLE_ADVERTISING = 2
    DIRECTED_ADVERTISING = 4
    SCAN_RESPONSE = 8
    LEGACY_ADVERTISING_PDU_USED = 16


class DataStatus(IntEnum):
    COMPLETE = 0
    INCOMPLETE = 1
    TRUNCATED = 2
    # A PAwR packet listened for was not received (Vol 4, Part E, 7.7.65.15,
    # 7.7.65.37).
    RECEIVE_FAILED = 0xFF


@dataclass(frozen=True)
class AdvertisingReport:
    properties: AdvertisingProperties
    data_status: DataStatus = DataStatus.COMPLETE


LL_ADV_FILTER_SCAN_MASK = 1
LL_ADV_FILTER_CONNECT_MASK = 2
