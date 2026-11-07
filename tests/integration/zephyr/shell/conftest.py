# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

# Only the Controller matrix is shared: bumble_peer selects the Bumble peer's
# Controller explicitly, and shell_peer defines its own topology matrix.
from tests.integration.zephyr.controllers import controller_type

__all__ = ["controller_type"]
