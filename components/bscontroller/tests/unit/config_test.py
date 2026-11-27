# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Controller-defined configuration: defaults, sources, precedence and validation.

``ControllerConfig`` holds the values Core leaves to the implementation within a
specification range. These tests cover the configuration object only: the
defaults, the camelCase and snake_case JSON forms, precedence from defaults to
``controllers.<name>`` to keyword arguments, per-field rejection of invalid
values, and the JSON template. How each value reaches HCI and LL admission is
covered by ``tests/unit/hci/config_scenario_test.py``.
"""

import json
import logging
from dataclasses import fields

import pytest

from bscontroller.config import RANGES, ControllerConfig

DEFAULTS = ControllerConfig()


def test_every_default_lies_in_its_specification_range():
    """Every built-in default lies inside its declared range.

    Purpose: Verify that each field declares a range and that the defaults are
    valid values, so an unconfigured Controller reports legal parameters.

    Steps:
        1. Check every field's default against its range.

    Expected:
        - Every field has a range, and its default lies within it.
    """
    assert set(RANGES) == {field.name for field in fields(ControllerConfig)}
    for name, (low, high) in RANGES.items():
        assert low <= getattr(DEFAULTS, name) <= high, name


@pytest.mark.parametrize(
    'name,value',
    [
        ('acl_packet_count', 0),
        ('max_tx_octets', 252),
        ('hci_version', 0x05),
        ('max_scan_duplicate_entries', 0),
    ],
)
def test_direct_construction_rejects_out_of_range(name, value):
    """Constructing a configuration with an invalid value raises.

    Purpose: Verify that code constructing ``ControllerConfig`` directly gets an
    error instead of a silently invalid Controller, for a value below a range,
    above a range, and for an unsupported Core version.

    Steps:
        1. Construct ``ControllerConfig`` with the parametrized invalid field.

    Expected:
        - ``ValueError`` names the field.
    """
    with pytest.raises(ValueError, match=name):
        ControllerConfig(**{name: value})


def test_json_keys_accept_camel_and_snake_case():
    """camelCase and snake_case keys set the same field.

    Purpose: Verify that a document may use either key style, matching the
    camelCase that the template writes and the Python field names.

    Steps:
        1. Build configurations from ``aclPacketCount`` and ``acl_packet_count``.

    Expected:
        - Both set ``acl_packet_count``.
    """
    camel = ControllerConfig.from_mapping({'defaults': {'aclPacketCount': 12}})
    snake = ControllerConfig.from_mapping({'defaults': {'acl_packet_count': 12}})
    assert camel.acl_packet_count == snake.acl_packet_count == 12


def test_precedence_defaults_controller_entry_then_arguments():
    """Later sources override earlier ones field by field.

    Purpose: Verify the precedence built-in default < ``defaults`` <
    ``controllers.<name>`` < keyword arguments, and that another Controller's
    entry does not apply.

    Steps:
        1. Build a document that sets ``aclPacketCount`` in ``defaults``, in
           ``controllers.hci0`` and in ``controllers.hci1``, and
           ``maxConnections`` only in ``defaults``.
        2. Load it for hci0 with an ``acl_packet_length`` argument, and for an
           unnamed Controller.

    Expected:
        - hci0 takes the count from its own entry, the connection limit from
          ``defaults``, the length from the argument, and every other field
          from the built-in defaults.
        - Without a name, only ``defaults`` applies.
    """
    document = {
        'defaults': {'aclPacketCount': 16, 'maxConnections': 4},
        'controllers': {'hci0': {'aclPacketCount': 2}, 'hci1': {'aclPacketCount': 3}},
    }
    config = ControllerConfig.from_mapping(document, 'hci0', acl_packet_length=512)
    assert (config.acl_packet_count, config.max_connections) == (2, 4)
    assert config.acl_packet_length == 512
    assert config.max_tx_octets == DEFAULTS.max_tx_octets
    unnamed = ControllerConfig.from_mapping(document)
    assert unnamed.acl_packet_count == 16


@pytest.mark.parametrize(
    'value',
    [0, 256, -1, '8', 8.0, True, None],
    ids=['low', 'high', 'negative', 'string', 'float', 'bool', 'null'],
)
def test_invalid_value_keeps_previous_and_warns(caplog, value):
    """An invalid value is ignored, keeping the previous one, with a warning.

    Purpose: Verify that an out-of-range or non-integer value does not change
    its field, that it falls back to the value from the previous source rather
    than the built-in default, and that other fields in the same object still
    apply.

    Steps:
        1. Set ``aclPacketCount`` = 16 in ``defaults``.
        2. Set the parametrized invalid ``aclPacketCount`` and a valid
           ``maxConnections`` in ``controllers.hci0``.

    Expected:
        - ``acl_packet_count`` stays 16 and ``max_connections`` is 5.
        - A warning names the source and the key.
    """
    document = {
        'defaults': {'aclPacketCount': 16},
        'controllers': {'hci0': {'aclPacketCount': value, 'maxConnections': 5}},
    }
    with caplog.at_level(logging.WARNING, 'bscontroller.config'):
        config = ControllerConfig.from_mapping(document, 'hci0')
    assert (config.acl_packet_count, config.max_connections) == (16, 5)
    assert 'controllers.hci0' in caplog.text and 'aclPacketCount' in caplog.text


@pytest.mark.parametrize(
    'document,message',
    [
        ({'defaults': {'bogus': 1}}, "unknown Controller option 'bogus'"),
        ({'extra': {}}, "unknown Controller configuration section 'extra'"),
        ({'defaults': []}, "'defaults' must be an object"),
        ([], 'must be a JSON object'),
    ],
    ids=['unknown-key', 'unknown-section', 'section-type', 'root-type'],
)
def test_malformed_document_parts_are_ignored_with_a_warning(caplog, document, message):
    """Unknown or malformed parts of a document are ignored.

    Purpose: Verify that an unknown key, an unknown section, a non-object
    section and a non-object root each produce a warning and leave the
    defaults in place.

    Steps:
        1. Build a configuration from the parametrized document.

    Expected:
        - The configuration equals the defaults.
        - The warning text names the problem.
    """
    with caplog.at_level(logging.WARNING, 'bscontroller.config'):
        assert ControllerConfig.from_mapping(document, 'hci0') == DEFAULTS
    assert message in caplog.text


def test_load_reads_a_json_file(tmp_path):
    """``load`` reads a file and selects the Controller entry.

    Purpose: Verify file loading, Controller selection and keyword overrides
    together, and that a missing file raises instead of silently using
    defaults.

    Steps:
        1. Write a document with an hci1 entry and load it for hci1 with an
           override.
        2. Load a path that does not exist.

    Expected:
        - The entry and the override both apply.
        - The missing path raises ``OSError``.
    """
    path = tmp_path / 'bsc.json'
    path.write_text(json.dumps({'controllers': {'hci1': {'manufacturer': 0x59}}}))
    config = ControllerConfig.load(path, 'hci1', advertising_set_count=4)
    assert (config.manufacturer, config.advertising_set_count) == (0x59, 4)
    with pytest.raises(OSError):
        ControllerConfig.load(tmp_path / 'missing.json')


def test_template_round_trips_to_the_defaults():
    """The JSON template lists every field and reloads to the defaults.

    Purpose: Verify that the template printed by ``bscontroller config`` is a
    complete, valid starting point for a user file.

    Steps:
        1. Render the template, parse it and load it.

    Expected:
        - ``defaults`` has one camelCase key per field.
        - Loading the template yields the default configuration.
    """
    document = json.loads(ControllerConfig.template())
    assert len(document['defaults']) == len(RANGES)
    assert 'aclPacketCount' in document['defaults']
    assert ControllerConfig.from_mapping(document) == DEFAULTS


def test_resolve_selects_by_name_for_every_source_kind(tmp_path):
    """``resolve`` gives the named Controller its entry from any source.

    Purpose: Verify the single selection rule used by ``RadioController``, the
    CLI and BSWorld: an explicit ``ControllerConfig`` is used as given, a
    mapping or JSON path applies ``defaults`` and the entry for the name, and
    ``None`` gives the built-in defaults.

    Steps:
        1. Resolve a mapping for hci0 and hci1, the same document as a file for
           hci1, an explicit configuration, and ``None``.

    Expected:
        - hci0 and hci1 get their own entries over the shared ``defaults``.
        - The file resolves like the mapping.
        - The explicit configuration is returned unchanged; ``None`` gives the
          defaults.
    """
    document = {
        'defaults': {'aclPacketCount': 4},
        'controllers': {'hci0': {'maxConnections': 1}, 'hci1': {'maxConnections': 5}},
    }
    first = ControllerConfig.resolve(document, 'hci0')
    second = ControllerConfig.resolve(document, 'hci1')
    assert (first.acl_packet_count, first.max_connections) == (4, 1)
    assert (second.acl_packet_count, second.max_connections) == (4, 5)
    path = tmp_path / 'bsc.json'
    path.write_text(json.dumps(document))
    assert ControllerConfig.resolve(path, 'hci1') == second
    assert ControllerConfig.resolve(str(path), 'hci1') == second
    explicit = ControllerConfig(max_connections=9)
    assert ControllerConfig.resolve(explicit, 'hci0') is explicit
    assert ControllerConfig.resolve(None, 'hci0') == DEFAULTS
