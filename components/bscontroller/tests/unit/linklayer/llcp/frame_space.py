# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Frame Space Update commands, events and Lower Tester control PDUs."""

import struct

from bscontroller import hci
from bscontroller.linklayer import ControlOpcode as Opcode

UPDATE = hci.HCI_LE_Frame_Space_Update_Command
COMPLETE = hci.HCI_LE_Frame_Space_Update_Complete_Event
CP = UPDATE.SpacingType.T_IFS_ACL_CP
PC = UPDATE.SpacingType.T_IFS_ACL_PC
ONE_MBPS = 1
TWO_MBPS = 2


def request(minimum, maximum, phys, spacing):
    return struct.pack(
        '<BHHBH', Opcode.LL_FRAME_SPACE_REQ, minimum, maximum, phys, spacing
    )


def response(space, phys, spacing):
    return struct.pack('<BHBH', Opcode.LL_FRAME_SPACE_RSP, space, phys, spacing)
