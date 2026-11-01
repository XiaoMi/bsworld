# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Standalone Bluetooth LE radio Controller and synchronized runtime."""

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import hci as hci
    from .config import ControllerConfig as ControllerConfig
    from .radio.base import Phy as Phy
    from .radio.base import Radio as Radio
    from .radio.base import Received as Received
    from .runtime.event_loop import RadioEventLoop as RadioEventLoop
    from .runtime.session import BabbleSimSession as RadioSession
    from .runtime.worker import RadioControllerWorker as RadioController

# Importing a protocol module must not construct the HCI/runtime dependency tree.
# Keep the public convenience exports, loading their implementation when requested.
_EXPORTS = {
    'hci': ('.hci', None),
    'ControllerConfig': ('.config', 'ControllerConfig'),
    'Phy': ('.radio.base', 'Phy'),
    'Radio': ('.radio.base', 'Radio'),
    'Received': ('.radio.base', 'Received'),
    'RadioEventLoop': ('.runtime.event_loop', 'RadioEventLoop'),
    'RadioSession': ('.runtime.session', 'BabbleSimSession'),
    'RadioController': ('.runtime.worker', 'RadioControllerWorker'),
}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    module, attribute = _EXPORTS[name]
    imported = import_module(module, __name__)
    value = getattr(imported, attribute) if attribute else imported
    globals()[name] = value
    return value


try:
    __version__ = version('bscontroller')
except PackageNotFoundError:
    __version__ = '0+unknown'

__all__ = [
    'Phy',
    'Radio',
    'RadioController',
    'RadioEventLoop',
    'RadioSession',
    'Received',
    'hci',
]
