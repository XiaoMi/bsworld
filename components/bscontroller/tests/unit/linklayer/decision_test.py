# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Decision groups, field presence and masked tests (Vol 4, Part E, 7.8.145)."""

from types import SimpleNamespace

import pytest

from bscontroller.address import Address
from bscontroller.linklayer.advertising_pdu import ExtendedPdu
from bscontroller.linklayer.decision import DecisionProgram


def _decision_pdu(*, data, **values):
    return ExtendedPdu(data=data[1:], decision_flags=data[0], **values)


@pytest.fixture
def program():
    return DecisionProgram(
        SimpleNamespace(
            accept_list=set(), privacy=SimpleNamespace(identity=lambda address: address)
        )
    )


def test_no_program_and_atomic_rejection(program):
    """No program rejects decision PDUs and a bad update keeps the old program.

    Purpose: Verify that an empty decision program accepts no decision PDU and
    that a rejected configuration leaves the committed program in place
    unchanged (Vol 4, Part E, 7.8.145).

    Steps:
        1. Evaluate a decision PDU with no program configured.
        2. Configure one Advertising Mode test passing on non-connectable
           non-scannable PDUs and evaluate the PDU again.
        3. Configure an empty program, a program whose first test does not
           start a group, and a program with nine tests.

    Expected:
        - Step 1 rejects the PDU; step 2 succeeds and accepts it.
        - Each step 3 configuration returns an error, the committed program
          object is unchanged and the PDU is still accepted.
    """
    pdu = _decision_pdu(data=b'\0\x42')
    assert not program.accepts(pdu, -40)
    assert program.configure([3], [6], [b'\1' + bytes(15)]) == 0
    assert program.accepts(pdu, -40)
    committed = program.tests
    for flags, fields, parameters in [
        ([], [], []),
        ([2], [6], [bytes(16)]),
        ([3] * 9, [6] * 9, [bytes(16)] * 9),
    ]:
        assert program.configure(flags, fields, parameters) != 0
        assert program.tests is committed
        assert program.accepts(pdu, -40)


@pytest.mark.parametrize('flags', range(1, 16, 2))
@pytest.mark.parametrize(
    'data,outcome_bit', [(b'\0\x42', 2), (b'\0\x43', 4), (b'\0', 8)]
)
def test_match_failure_and_absence_flags(program, flags, data, outcome_bit):
    """Pass-if-match, pass-if-not-match and pass-if-absent act independently.

    Purpose: Verify that each decision test flag selects acceptance for its own
    outcome only: a matching field, a present nonmatching field, or an absent
    field (Vol 4, Part E, 7.8.145).

    Steps:
        1. For each odd flags value 1..15, configure one exact-length-1
           Arbitrary Data test matching octet 0x42.
        2. Evaluate decision data 0x42 (match), 0x43 (no match) and empty
           (absent).

    Expected:
        - The PDU is accepted exactly when the flag for its outcome is set:
          bit 1 for a match, bit 2 for no match, bit 3 for absence.
    """
    parameters = b'\xff' + bytes(7) + b'\x42' + bytes(7)
    program.configure([flags], [17], [parameters])
    assert program.accepts(_decision_pdu(data=data), -40) == bool(flags & outcome_bit)


def test_groups_are_and_then_or(program):
    """Tests within a group are ANDed and groups are ORed.

    Purpose: Verify that a decision program accepts a PDU when every test of
    at least one group passes (Vol 4, Part E, 7.8.145).

    Steps:
        1. Configure group 1 as Advertising Mode non-connectable AND RSSI in
           10..20 dBm, and group 2 as one Arbitrary Data octet equal to 0x42.
        2. Evaluate data 0x42 at -40 dBm.
        3. Evaluate data 0x43 at -40 dBm.
        4. Evaluate data 0x43 at 15 dBm.

    Expected:
        - Step 2 is accepted by group 2 alone.
        - Step 3 is rejected because no group passes.
        - Step 4 is accepted by group 1 alone.
    """
    # First group requires nonconnectable mode AND an impossible RSSI;
    # second group accepts a matching single arbitrary byte.
    parameters = [
        b'\1' + bytes(15),
        bytes([10, 20]) + bytes(14),
        b'\xff' + bytes(7) + b'\x42' + bytes(7),
    ]
    assert program.configure([3, 2, 3], [6, 7, 17], parameters) == 0
    assert program.accepts(_decision_pdu(data=b'\0\x42'), -40)
    assert not program.accepts(_decision_pdu(data=b'\0\x43'), -40)
    assert program.accepts(_decision_pdu(data=b'\0\x43'), 15)


@pytest.mark.parametrize(
    'field,low,high,tx_power', [(7, -50, -40, None), (8, 40, 50, 0)]
)
def test_inclusive_signal_bounds(program, field, low, high, tx_power):
    """RSSI and path-loss ranges include both bounds.

    Purpose: Verify that the RSSI and Path Loss decision tests pass on their
    lower and upper bounds and fail one dB outside (Vol 4, Part E, 7.8.145).

    Steps:
        1. Configure an RSSI test of -50..-40 dBm, or a Path Loss test of
           40..50 dB with a PDU TxPower of 0 dBm.
        2. Evaluate the PDU at RSSI -50, -40, -51 and -39 dBm.

    Expected:
        - -50 and -40 dBm are accepted.
        - -51 and -39 dBm are rejected.
    """
    parameters = bytes([low & 255, high & 255]) + bytes(14)
    program.configure([3], [field], [parameters])
    pdu = _decision_pdu(tx_power=tx_power, data=b'\0')
    assert program.accepts(pdu, -50)
    assert program.accepts(pdu, -40)
    assert not program.accepts(pdu, -51)
    assert not program.accepts(pdu, -39)


@pytest.mark.parametrize(
    'field,length,expected',
    [
        (18, 1, False),
        (18, 2, True),
        (18, 3, False),
        (34, 1, False),
        (34, 2, True),
        (34, 3, True),
        (50, 1, True),
        (50, 2, True),
        (50, 3, False),
    ],
)
def test_arbitrary_length_classes(program, field, length, expected):
    """Arbitrary Data length classes decide whether the field is present.

    Purpose: Verify that the exact-length, minimum-length and maximum-length
    Arbitrary Data fields treat decision data of other lengths as absent
    (Vol 4, Part E, 7.8.145).

    Steps:
        1. Configure one pass-if-match test with an all-zero mask on field 18
           (exactly 2), 34 (at least 2) or 50 (at most 2).
        2. Evaluate decision data of 1, 2 and 3 octets.

    Expected:
        - Exact 2 accepts only 2 octets; minimum 2 accepts 2 and 3; maximum 2
          accepts 1 and 2.
    """
    program.configure([3], [field], [bytes(16)])
    assert program.accepts(_decision_pdu(data=b'\0' + bytes(length)), -40) == expected


def test_advertiser_filter_accept_list(program):
    """The Advertiser Address test can check the Filter Accept List.

    Purpose: Verify that the Advertiser Address decision test with the Filter
    Accept List check passes only for a listed AdvA and treats a PDU without
    AdvA as absent (Vol 4, Part E, 7.8.145).

    Steps:
        1. Configure one pass-if-match Advertiser Address test using the Filter
           Accept List.
        2. Evaluate a PDU whose random AdvA is not listed.
        3. Add that address to the Filter Accept List and evaluate it again.
        4. Evaluate a PDU without AdvA.

    Expected:
        - Step 2 is rejected, step 3 is accepted and step 4 is rejected.
    """
    peer = Address('C0:11:22:33:44:55')
    program.configure([3], [9], [bytes(16)])
    pdu = _decision_pdu(address=bytes(peer), random_address=True, data=b'\0')
    assert not program.accepts(pdu, -40)
    program.link.accept_list.add(peer)
    assert program.accepts(pdu, -40)
    assert not program.accepts(_decision_pdu(data=b'\0'), -40)


def test_core_decision_wire_format():
    """A decision PDU decodes and re-encodes to the spec wire layout.

    Purpose: Verify the decision PDU layout where the decision flags occupy the
    six bits normally used by the extended header length (Vol 6, Part B,
    Fig 2.10). Fixed bytes prevent an encoder/decoder pair from hiding the
    same mistake.

    Steps:
        1. Decode a fixed connectable decision PDU with AuxPtr 0x000a0a
           (channel 10, offset 10 x 30 us, LE 1M) and six octets of data.
        2. Re-encode the decoded PDU.

    Expected:
        - Mode is connectable, decision flags are 1 and the data is
          11 22 33 44 55 66.
        - AuxPtr gives channel 10 and an offset of 300 us.
        - The data capacity is 8 octets and re-encoding reproduces the wire.
    """
    wire = bytes.fromhex('090b41100a0a00112233445566')
    pdu = ExtendedPdu.decode(wire)
    assert pdu.mode == 1 and pdu.decision_flags == 1
    assert pdu.data == bytes.fromhex('112233445566')
    assert pdu.aux_ptr.channel == 10 and pdu.aux_ptr.offset_us == 300
    assert pdu.data_capacity() == 8
    assert pdu.encode() == wire


@pytest.mark.parametrize(
    'wire',
    [
        '090401100a0a',  # truncated AuxPtr, no tag
        '090b01110a0a00112233445566',  # AdvA flag without six address octets
        '090b01000a0a00112233445566',  # missing mandatory AuxPtr
        '090b01120a0a00112233445566',  # forbidden TargetA
    ],
)
def test_malformed_decision_wire_is_rejected(wire):
    """Malformed decision PDUs fail to decode.

    Purpose: Verify that the decision PDU decoder rejects layouts the spec does
    not allow (Vol 6, Part B, Fig 2.10).

    Steps:
        1. Decode each parametrized wire: truncated AuxPtr, AdvA flag without
           six address octets, missing mandatory AuxPtr, forbidden TargetA.

    Expected:
        - Each decode raises ValueError.
    """
    with pytest.raises(ValueError):
        ExtendedPdu.decode(bytes.fromhex(wire))
