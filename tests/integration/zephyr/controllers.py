# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Run unchanged Zephyr Host scenarios against both Controller implementations.

Only the Zephyr suite covers both; the other suites use the framework default,
BSController.
"""

import pytest
from tests.integration.applications import require_artifact

from bsworld.devices import BSController, Controller, ZephyrController


def require_controller(controller_type: type[Controller]) -> None:
    """Skip when the selected Controller needs an artifact that was not built."""
    if controller_type is ZephyrController:
        require_artifact("BSWORLD_BT_VIRT_CONTROLLER_BIN", guide="docs/controllers.md")


@pytest.fixture(params=[ZephyrController, BSController], ids=["zephyr", "bscontroller"])
def controller_type(request):
    require_controller(request.param)
    return request.param


__all__ = ["controller_type", "require_controller"]
