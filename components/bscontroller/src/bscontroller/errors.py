# Copyright 2021-2025 Google LLC
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Controller error codes (Core 6.3, Vol 1, Part F) and codec exceptions."""

from __future__ import annotations

from bscontroller.serialization import SpecableEnum


class ControllerError(Exception):
    """Base Error raised by BSController."""


class BaseError(ControllerError):
    """Base class for errors with an error code, error name and namespace"""

    def __init__(
        self,
        error_code: int | None,
        error_namespace: str = '',
        error_name: str = '',
        details: str = '',
    ):
        super().__init__()
        self.error_code = error_code
        self.error_namespace = error_namespace
        self.error_name = error_name
        self.details = details

    def __str__(self):
        if self.error_namespace:
            namespace = f'{self.error_namespace}/'
        else:
            namespace = ''
        have_name = self.error_name != ''
        have_code = self.error_code is not None
        if have_name and have_code:
            error_text = f'{self.error_name} [0x{self.error_code:X}]'
        elif have_name and not have_code:
            error_text = self.error_name
        elif not have_name and have_code:
            error_text = f'0x{self.error_code:X}'
        else:
            error_text = '<unspecified>'

        return f'{type(self).__name__}({namespace}{error_text})'


class ProtocolError(BaseError):
    """Protocol Error"""


class InvalidArgumentError(ControllerError, ValueError):
    """Invalid Argument Error"""


class InvalidPacketError(ControllerError, ValueError):
    """Invalid Packet Error"""


class ErrorCode(SpecableEnum):
    SUCCESS = 0x00
    UNKNOWN_HCI_COMMAND_ERROR = 0x01
    UNKNOWN_CONNECTION_IDENTIFIER_ERROR = 0x02
    HARDWARE_FAILURE_ERROR = 0x03
    PAGE_TIMEOUT_ERROR = 0x04
    AUTHENTICATION_FAILURE_ERROR = 0x05
    PIN_OR_KEY_MISSING_ERROR = 0x06
    MEMORY_CAPACITY_EXCEEDED_ERROR = 0x07
    CONNECTION_TIMEOUT_ERROR = 0x08
    CONNECTION_LIMIT_EXCEEDED_ERROR = 0x09
    SYNCHRONOUS_CONNECTION_LIMIT_TO_A_DEVICE_EXCEEDED_ERROR = 0x0A
    CONNECTION_ALREADY_EXISTS_ERROR = 0x0B
    COMMAND_DISALLOWED_ERROR = 0x0C
    CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR = 0x0D
    CONNECTION_REJECTED_DUE_TO_SECURITY_REASONS_ERROR = 0x0E
    CONNECTION_REJECTED_DUE_TO_UNACCEPTABLE_BD_ADDR_ERROR = 0x0F
    CONNECTION_ACCEPT_TIMEOUT_ERROR = 0x10
    UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR = 0x11
    INVALID_COMMAND_PARAMETERS_ERROR = 0x12
    REMOTE_USER_TERMINATED_CONNECTION_ERROR = 0x13
    REMOTE_DEVICE_TERMINATED_CONNECTION_DUE_TO_LOW_RESOURCES_ERROR = 0x14
    REMOTE_DEVICE_TERMINATED_CONNECTION_DUE_TO_POWER_OFF_ERROR = 0x15
    CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR = 0x16
    REPEATED_ATTEMPTS_ERROR = 0x17
    PAIRING_NOT_ALLOWED_ERROR = 0x18
    UNKNOWN_LMP_PDU_ERROR = 0x19
    UNSUPPORTED_REMOTE_FEATURE_ERROR = 0x1A
    SCO_OFFSET_REJECTED_ERROR = 0x1B
    SCO_INTERVAL_REJECTED_ERROR = 0x1C
    SCO_AIR_MODE_REJECTED_ERROR = 0x1D
    INVALID_LMP_OR_LL_PARAMETERS_ERROR = 0x1E
    UNSPECIFIED_ERROR_ERROR = 0x1F
    UNSUPPORTED_LMP_OR_LL_PARAMETER_VALUE_ERROR = 0x20
    ROLE_CHANGE_NOT_ALLOWED_ERROR = 0x21
    LMP_OR_LL_RESPONSE_TIMEOUT_ERROR = 0x22
    LMP_ERROR_TRANSACTION_COLLISION_OR_LL_PROCEDURE_COLLISION_ERROR = 0x23
    LMP_PDU_NOT_ALLOWED_ERROR = 0x24
    ENCRYPTION_MODE_NOT_ACCEPTABLE_ERROR = 0x25
    LINK_KEY_CANNOT_BE_CHANGED_ERROR = 0x26
    REQUESTED_QOS_NOT_SUPPORTED_ERROR = 0x27
    INSTANT_PASSED_ERROR = 0x28
    PAIRING_WITH_UNIT_KEY_NOT_SUPPORTED_ERROR = 0x29
    DIFFERENT_TRANSACTION_COLLISION_ERROR = 0x2A
    RESERVED_FOR_FUTURE_USE = 0x2B
    QOS_UNACCEPTABLE_PARAMETER_ERROR = 0x2C
    QOS_REJECTED_ERROR = 0x2D
    CHANNEL_CLASSIFICATION_NOT_SUPPORTED_ERROR = 0x2E
    INSUFFICIENT_SECURITY_ERROR = 0x2F
    PARAMETER_OUT_OF_MANDATORY_RANGE_ERROR = 0x30
    ROLE_SWITCH_PENDING_ERROR = 0x32
    RESERVED_SLOT_VIOLATION_ERROR = 0x34
    ROLE_SWITCH_FAILED_ERROR = 0x35
    EXTENDED_INQUIRY_RESPONSE_TOO_LARGE_ERROR = 0x36
    SECURE_SIMPLE_PAIRING_NOT_SUPPORTED_BY_HOST_ERROR = 0x37
    HOST_BUSY_PAIRING_ERROR = 0x38
    CONNECTION_REJECTED_DUE_TO_NO_SUITABLE_CHANNEL_FOUND_ERROR = 0x39
    CONTROLLER_BUSY_ERROR = 0x3A
    UNACCEPTABLE_CONNECTION_PARAMETERS_ERROR = 0x3B
    ADVERTISING_TIMEOUT_ERROR = 0x3C
    CONNECTION_TERMINATED_DUE_TO_MIC_FAILURE_ERROR = 0x3D
    CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR = 0x3E
    # Keep the public constant name unchanged despite exceeding the line limit.
    COARSE_CLOCK_ADJUSTMENT_REJECTED_BUT_WILL_TRY_TO_ADJUST_USING_CLOCK_DRAGGING_ERROR = 0x40  # noqa: E501
    TYPE0_SUBMAP_NOT_DEFINED_ERROR = 0x41
    UNKNOWN_ADVERTISING_IDENTIFIER_ERROR = 0x42
    LIMIT_REACHED_ERROR = 0x43
    OPERATION_CANCELLED_BY_HOST_ERROR = 0x44
    PACKET_TOO_LONG_ERROR = 0x45
    TOO_LATE_ERROR = 0x46
