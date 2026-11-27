# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Extended advertising fragmentation and airtime budgets (Core Vol 6, Part B,
2.3.4, 4.4.2.2.2). Pure functions, independent of any role.

Invariants:
    - ``fragments`` splits AdvData into AUX_ADV_IND/AUX_CHAIN_IND PDUs that
      preserve the bytes, keep one ADI, carry the address only in the first PDU,
      and chain with T_MAFS-spaced, 30 us-quantized AuxPtr offsets.
    - A connectable AUX_ADV_IND must carry all its data in one PDU.
    - ``data_capacity`` reflects the actual extended-header fields in use.
    - ``advertising_time_us`` bounds the airtime of the primary PDUs plus chain.

These are the pure codec helpers behind the on-air layout checked with the live
role in ``extended_advertising_test.py`` and ``extended_scanning_test.py``; this
file needs no harness.
"""

import pytest

from bscontroller import hci
from bscontroller.hci import HCI_LE_ADV_DATA_SIZE_MAX
from bscontroller.linklayer import LL_MAFS_US, PDU_ADV_ADDRESS_SIZE, AdvertisingMode
from bscontroller.linklayer.advertising_pdu import ExtendedPdu
from bscontroller.linklayer.discovery import Advertising
from bscontroller.linklayer.extended import advertising_time_us, fragments
from bscontroller.radio.base import Phy

ADDRESS = bytes(PDU_ADV_ADDRESS_SIZE)


@pytest.mark.parametrize(
    'phy,size',
    [
        (Phy.LE_1M, 0),
        (Phy.LE_1M, 245),
        (Phy.LE_1M, 246),
        (Phy.LE_1M, HCI_LE_ADV_DATA_SIZE_MAX),
        (Phy.LE_2M, 246),
        (Phy.LE_CODED, HCI_LE_ADV_DATA_SIZE_MAX),
    ],
)
def test_fragments_preserve_bytes_and_spacing(phy, size):
    """Fragmentation preserves the data and spaces the chain correctly.

    Purpose: Verify that ``fragments`` splits AdvData into extended PDUs whose
    concatenated payloads equal the input, that each PDU fits the 255-octet
    payload limit and keeps the ADI, that only the first PDU carries the address,
    and that every AuxPtr targets the given channel and PHY with a 30 us-quantized
    offset of at least the PDU airtime plus T_MAFS (Vol 6, Part B, 2.3.4).

    Steps:
        1. Build AdvData of the parametrized size.
        2. Fragment it for the parametrized PHY onto channel 12 with ADI 0x1234.

    Expected:
        - The joined fragment payloads equal the original data.
        - Each encoded PDU is at most 257 octets and keeps ADI 0x1234.
        - Only the first fragment carries the address.
        - Each non-final PDU's AuxPtr uses the PHY and channel, is 30 us-aligned,
          and offsets by at least the PDU airtime plus T_MAFS; the last PDU has
          no AuxPtr.

    Parameters range over {LE 1M, LE 2M, LE Coded} with sizes spanning the single
    PDU, the one-octet overflow boundary, and the maximum AdvData length.
    """
    data = bytes(index % 251 for index in range(size))
    packets = fragments(ExtendedPdu(address=ADDRESS, adi=0x1234), data, 12, phy)
    assert b''.join(packet.data for packet in packets) == data
    for index, packet in enumerate(packets):
        assert len(packet.encode()) <= 257 and packet.adi == 0x1234
        assert packet.address == (ADDRESS if index == 0 else None)
        if packet.aux_ptr:
            assert packet.aux_ptr.phy == phy and packet.aux_ptr.channel == 12
            assert packet.aux_ptr.offset_us % 30 == 0
            assert (
                packet.aux_ptr.offset_us
                >= phy.airtime_us(len(packet.encode())) + LL_MAFS_US
            )
        else:
            assert index == len(packets) - 1


def test_data_limit_is_bounded():
    """Data beyond the maximum AdvData length is rejected.

    Purpose: Verify that ``fragments`` refuses AdvData longer than the advertised
    maximum rather than emitting an over-length chain (Vol 6, Part B, 2.3.4.9).

    Steps:
        1. Call ``fragments`` with one octet more than HCI_LE_ADV_DATA_SIZE_MAX.

    Expected:
        - ValueError is raised, its message containing "exceeds".
    """
    with pytest.raises(ValueError, match='exceeds'):
        fragments(ExtendedPdu(), bytes(HCI_LE_ADV_DATA_SIZE_MAX + 1), 12, Phy.LE_1M)


def test_connectable_data_must_fit_one_pdu():
    """Connectable AUX_ADV_IND data must fit a single PDU.

    Purpose: Verify that a connectable extended advertisement fragments into one
    unchained AUX_ADV_IND when the data fits, and is rejected when the data
    exceeds the single-PDU capacity (Vol 6, Part B, 2.3.4).

    Steps:
        1. Fragment 200 octets for a connectable AUX_ADV_IND.
        2. Fragment one octet more than the PDU's data capacity.

    Expected:
        - The 200-octet case yields one PDU with no AuxPtr, keeping the
          connectable mode.
        - The over-capacity case raises ValueError mentioning "connectable".
    """
    first = ExtendedPdu(mode=AdvertisingMode.CONNECTABLE, address=ADDRESS, adi=3)
    (packet,) = fragments(first, bytes(200), 12, Phy.LE_1M)
    assert packet.aux_ptr is None and packet.mode == AdvertisingMode.CONNECTABLE
    with pytest.raises(ValueError, match='connectable'):
        fragments(first, bytes(first.data_capacity() + 1), 12, Phy.LE_1M)


@pytest.mark.parametrize(
    'first,capacity',
    [
        (ExtendedPdu(), 254),
        (ExtendedPdu(address=ADDRESS, adi=1), 245),
        (
            ExtendedPdu(
                mode=AdvertisingMode.CONNECTABLE,
                address=ADDRESS,
                target=ADDRESS,
                adi=1,
                tx_power=0,
            ),
            238,
        ),
    ],
)
def test_capacity_uses_actual_extended_header(first, capacity):
    """Data capacity reflects the extended-header fields actually present.

    Purpose: Verify that ``data_capacity`` accounts for the AdvA, ADI, TargetA and
    TxPower fields a PDU carries, so that fragmenting exactly ``capacity`` octets
    fills one 255-octet PDU, and one extra octet spills into a second PDU for a
    non-connectable set (Vol 6, Part B, 2.3.4.1).

    Steps:
        1. Read the first PDU's reported ``data_capacity``.
        2. Fragment exactly ``capacity`` octets.
        3. For a non-connectable set, fragment one octet more.

    Expected:
        - ``data_capacity`` equals the parametrized value.
        - The ``capacity``-octet case yields a single PDU whose length octet is
          255.
        - One extra octet splits into two PDUs, the first still at length 255.

    Parameters range over a bare AUX_ADV_IND, one with AdvA and ADI, and a
    connectable one with AdvA, TargetA, ADI and TxPower.
    """
    assert first.data_capacity() == capacity
    (packet,) = fragments(first, bytes(capacity), 12, Phy.LE_1M)
    assert packet.encode()[1] == 255
    if not first.mode:
        packets = fragments(first, bytes(capacity + 1), 12, Phy.LE_1M)
        assert len(packets) == 2 and packets[0].encode()[1] == 255


@pytest.mark.parametrize('phy', list(Phy))
def test_event_budget_covers_primary_packets_and_chain(phy):
    """Airtime budget shrinks with fewer channels, less data and empty sets.

    Purpose: Verify that ``advertising_time_us`` bounds the airtime of an extended
    event, counting the primary channel PDUs and the auxiliary chain, so that
    narrowing the channel map, dropping the data, or using a bare set each lowers
    the estimate (Vol 6, Part B, 4.4.2.2.2).

    Steps:
        1. Compute the airtime for a three-channel set with maximum AdvData.
        2. Restrict the channel map to one channel and recompute.
        3. Clear the data and recompute.
        4. Compute the airtime for a bare advertising set.

    Expected:
        - The three-channel estimate is at least the one-channel estimate.
        - Clearing the data strictly lowers the estimate.
        - A bare set has zero airtime.

    Parameters range over every PHY, mapping LE 2M to an LE 1M primary PHY.
    """
    item = Advertising(
        hci.Address('F0:F1:F2:F3:F4:01'),
        handle=0,
        extended=True,
        primary_phy=phy if phy != Phy.LE_2M else Phy.LE_1M,
        secondary_phy=phy,
        data=bytes(HCI_LE_ADV_DATA_SIZE_MAX),
    )
    full = advertising_time_us(item)
    item.channel_map = 1
    assert full >= advertising_time_us(item)
    item.data = b''
    assert full > advertising_time_us(item)
    assert advertising_time_us(Advertising(item.address, handle=0)) == 0
