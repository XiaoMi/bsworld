# Adapted for the independent BSController package.
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""One HCI configuration and notification context shared by LE command groups."""

from typing import TYPE_CHECKING

from bscontroller import hci
from bscontroller.hci.buffers import DataBuffers
from bscontroller.hci.events import LeControllerEvents

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

    from bscontroller.hci.controller import Controller
    from bscontroller.hci.iso import IsoAssembly


SUCCESS = hci.HCI_ErrorCode.SUCCESS
INVALID = hci.HCI_ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
DISALLOWED = hci.HCI_ErrorCode.COMMAND_DISALLOWED_ERROR
UNSUPPORTED = hci.HCI_ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR
UNKNOWN_ADV = hci.HCI_ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR


class LeControllerContext(LeControllerEvents):
    """The public Controller owns HCI configuration; RadioLink owns active RF."""

    def __init__(self, controller: 'Controller'):
        self.controller = controller
        config = controller.config
        self.acl_buffers = DataBuffers(
            f'{controller.name} ACL', config.acl_packet_length, config.acl_packet_count
        )
        self.iso_buffers = DataBuffers(
            f'{controller.name} ISO', config.iso_packet_length, config.iso_packet_count
        )
        # CIS handles known to the Host and whether CIS Established was sent for each.
        self.iso_assemblies: dict[int, IsoAssembly] = {}
        self.cis_established: dict[int, bool] = {}
        self.deferred_cis_credits: dict[int, int] = {}
        self.duplicate_filter = None
        self.fragments: dict[tuple[int, str], bytes] = {}
        self.extended_commands: bool | None = None
        self._p256_private_key: EllipticCurvePrivateKey | None = None

    @property
    def now_us(self) -> int:
        return self.ll.now_us

    def _address(self, own_address_type: int):
        return (
            self.controller.random_address
            if hci.OwnAddressType(own_address_type).uses_random_fallback
            else self.controller.public_address
        )

    @property
    def scan_parameters(self):
        return self.ll.scan_parameters

    @property
    def host_channel_map(self):
        return self.ll.host_channel_map
