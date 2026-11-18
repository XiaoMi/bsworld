# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Controller-defined parameters within their Core specification ranges.

Precedence: built-in defaults, ``defaults``, ``controllers.<name>``, keyword
arguments. JSON keys are camelCase or snake_case. Invalid or unknown entries are
skipped with a warning; direct construction with an invalid value raises.

Example file::

    {
      "defaults": {"aclPacketCount": 16},
      "controllers": {"hci0": {"maxConnections": 2}}
    }
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from os import PathLike
from typing import Any, Union

logger = logging.getLogger(__name__)


def _option(default: int, low: int, high: int) -> Any:
    """A field with its inclusive specification range."""
    return field(default=default, metadata={'range': (low, high)})


@dataclass(frozen=True)
class ControllerConfig:
    """Controller-defined values; comments cite Core Vol 4, Part E."""

    # 7.4.1 Read Local Version Information. Default Core 6.1 includes
    # decision filtering and the v2 randomized RPA timeout command.
    hci_version: int = _option(0x0F, 0x06, 0x10)
    hci_revision: int = _option(0, 0, 0xFFFF)
    lmp_version: int = _option(0x0F, 0x06, 0x10)
    lmp_subversion: int = _option(0, 0, 0xFFFF)
    manufacturer: int = _option(0xFFFF, 0, 0xFFFF)
    # 7.8.2 LE Read Buffer Size.
    acl_packet_count: int = _option(8, 1, 0xFF)
    acl_packet_length: int = _option(251, 0x1B, 0xFFFF)
    # 7.8.46 LE Read Maximum Data Length.
    max_tx_octets: int = _option(251, 0x1B, 0xFB)
    max_tx_time: int = _option(17040, 0x148, 0x4290)
    max_rx_octets: int = _option(251, 0x1B, 0xFB)
    max_rx_time: int = _option(17040, 0x148, 0x4290)
    # 7.8.58 / 7.8.57 advertising sets and data.
    advertising_set_count: int = _option(16, 1, 0xF0)
    max_advertising_data_length: int = _option(1650, 0x1F, 0x672)
    # 7.8.14 / 7.8.73 list sizes.
    filter_accept_list_size: int = _option(16, 1, 0xFF)
    # 7.8.41 LE Read Resolving List Size.
    resolving_list_size: int = _option(16, 1, 0xFF)
    periodic_advertiser_list_size: int = _option(8, 1, 0xFF)
    # Concurrency limits; 5.3.1 bounds them by the 0x0000-0x0EFF handle space.
    max_connections: int = _option(64, 1, 0x0EFF)
    max_periodic_syncs: int = _option(16, 1, 0x0EFF)
    # 7.8.2 LE Read Buffer Size v2; separate ISO flow-control credits.
    iso_packet_count: int = _option(16, 1, 0xFF)
    iso_packet_length: int = _option(251, 5, 0x3FFF)
    # 7.8.103 / 7.8.106 BIG resources; each BIG contains 1–31 BISes.
    max_bigs: int = _option(4, 1, 0xF0)
    max_bis: int = _option(16, 1, 0x0EFF)

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if not _valid(item.name, value):
                low, high = RANGES[item.name]
                raise ValueError(f'{item.name}={value!r} is outside {low}-{high}')

    def merged(
        self, values: dict[str, Any] | None, source: str = 'config'
    ) -> ControllerConfig:
        """Apply ``values``; invalid entries keep the current value."""
        changes = {}
        for key, value in (values or {}).items():
            name = _NAMES.get(key) if isinstance(key, str) else None
            if name is None:
                logger.warning('%s: unknown Controller option %r ignored', source, key)
            elif not _valid(name, value):
                low, high = RANGES[name]
                logger.warning(
                    '%s: %s=%r is outside %d-%d; keeping %r',
                    source,
                    key,
                    value,
                    low,
                    high,
                    changes.get(name, getattr(self, name)),
                )
            else:
                changes[name] = int(value)
        return replace(self, **changes)

    @classmethod
    def from_mapping(
        cls, document: Any, name: str | None = None, **overrides
    ) -> ControllerConfig:
        """Build a configuration from a ``{defaults, controllers}`` document."""
        if document is None:
            document = {}
        elif not isinstance(document, dict):
            logger.warning('Controller configuration must be a JSON object')
            document = {}
        for key in document.keys() - {'defaults', 'controllers'}:
            logger.warning('unknown Controller configuration section %r', key)
        config = cls().merged(_section(document, 'defaults'), 'defaults')
        if name is not None:
            entry = _section(_section(document, 'controllers'), name)
            config = config.merged(entry, f'controllers.{name}')
        return config.merged(overrides, 'arguments')

    @classmethod
    def load(
        cls, path: str | PathLike[str], name: str | None = None, **overrides
    ) -> ControllerConfig:
        """Read a JSON file. A missing or unreadable file raises ``OSError``;
        malformed JSON raises ``ValueError``.
        """
        with open(path, encoding='utf-8') as source:
            document = json.load(source)
        return cls.from_mapping(document, name, **overrides)

    @classmethod
    def resolve(cls, source: ConfigSource, name: str | None) -> ControllerConfig:
        """The configuration for the Controller called ``name``.

        A ``ControllerConfig`` is used as given. A ``{defaults, controllers}``
        mapping, or the path of such a JSON file, applies ``defaults`` and then
        the entry for ``name``. ``None`` gives the built-in defaults.
        """
        if source is None:
            return cls()
        if isinstance(source, ControllerConfig):
            return source
        if isinstance(source, Mapping):
            return cls.from_mapping(dict(source), name)
        return cls.load(source, name)

    @classmethod
    def template(cls) -> str:
        """A complete JSON document with every default, for editing."""
        defaults = {_camel(key): value for key, value in asdict(cls()).items()}
        return json.dumps({'defaults': defaults, 'controllers': {}}, indent=2)


ConfigSource = Union[ControllerConfig, Mapping[str, Any], str, PathLike[str], None]
"""Anything ``ControllerConfig.resolve`` accepts."""

RANGES: dict[str, tuple[int, int]] = {
    item.name: item.metadata['range'] for item in fields(ControllerConfig)
}


def _valid(name: str, value: object) -> bool:
    """Integers (including int enums) within range; bool and floats are not."""
    low, high = RANGES[name]
    return (
        isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
    )


def _camel(name: str) -> str:
    head, *rest = name.split('_')
    return head + ''.join(part.title() for part in rest)


_NAMES = {name: name for name in RANGES} | {_camel(name): name for name in RANGES}


def _section(document: dict, key: str) -> dict:
    value = document.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        logger.warning('Controller configuration %r must be an object', key)
        return {}
    return value
