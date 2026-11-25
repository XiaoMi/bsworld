# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Access Address generation and CRC-24 against fixed vectors.

Protects two bit-exact invariants used when a connection is created (Core Vol 6,
Part B, 2.1.2 and 3.1.1):

- ``generate_access_address`` yields values that satisfy every Access Address
  constraint, including the stricter LE Coded PHY rules, and is reproducible
  from a seeded ``random.Random`` so that scenarios replay identically. The
  bit-string checks live in ``tests.unit.helpers.check_access_address``.
- ``crc24`` matches an independent radio model over empty, sample and maximal
  PDUs.

Higher-level advertising/connection wire vectors live in ``advertising_test.py``
and ``parameters_test.py``.
"""

import random
from unittest.mock import Mock

import pytest

from bscontroller.linklayer.bitstream import generate_access_address
from bscontroller.radio.packet import crc24
from tests.unit.helpers import check_access_address


def test_cis_access_address_rejects_a_one_bit_neighbor():
    """Core Vol 6, Part B, 2.1.2 requires a distinct CIS Access Address."""
    existing = 0x9A328370
    neighbor, independent = 0x9A328378, 0x7654BA98
    generator = Mock()
    generator.getrandbits.side_effect = (neighbor, independent)
    assert (
        generate_access_address(generator, {existing}, minimum_distance=2)
        == independent
    )


def test_access_address_generation_is_reproducible_and_valid_on_coded():
    """Generated Access Addresses pass every constraint and replay from a seed.

    Purpose: Verify that ``generate_access_address`` produces values meeting the
    Access Address rules for all PHYs, including LE Coded (Vol 6, Part B,
    2.1.2), never repeats a value already in use, and yields the same sequence
    for the same seed so scenarios are deterministic.

    Steps:
        1. Seed a ``random.Random(17)`` and an empty set of used values.
        2. Generate 1000 addresses, each validated by ``check_access_address``,
           recording each value and marking it used so it cannot recur.
        3. Re-seed a fresh ``random.Random(17)`` and regenerate the sequence
           from an empty used set.

    Expected:
        - Every generated value satisfies the bit-string oracle.
        - The replay from the same seed reproduces the identical list.
    """
    generator = random.Random(17)
    used = set()
    values = []
    for _ in range(1000):
        value = generate_access_address(generator, used)
        check_access_address(value)
        used.add(value)
        values.append(value)
    again = random.Random(17)
    assert values == [generate_access_address(again, set()) for _ in values]


@pytest.mark.parametrize(
    'pdu,expected',
    [
        (b'', 'aaaaaa'),
        (b'123456789', '565ac2'),
        (bytes(range(39)), '1d0e9b'),
        (
            bytes.fromhex(
                '021c01f4f3f2f1f00201041209507974686f6e2d526164696f2d44656d6f'
            ),
            '9f800b',
        ),
    ],
)
def test_crc_against_independent_radio_model(pdu, expected):
    """CRC-24 matches a known-good model for empty, sample and maximal PDUs.

    Purpose: Verify that ``crc24`` computes the LE CRC (Vol 6, Part B, 3.1.1)
    bit-for-bit against independently derived expected values, so the radio
    layer frames packets a real peer will accept.

    Steps:
        1. For each parametrized PDU and its expected CRC, compute ``crc24``.

    Expected:
        - The hex of the 24-bit CRC equals the expected value for every PDU,
          ranging over empty input, an ASCII sample, a 39-octet ramp and a
          realistic advertising PDU.
    """
    assert crc24(pdu).hex() == expected
