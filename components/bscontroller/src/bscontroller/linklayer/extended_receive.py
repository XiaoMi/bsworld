# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""AuxPtr timing and chain reception for scanning and periodic synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bscontroller.capabilities import CONTROLLER_ADV_DATA_SIZE_MAX
from bscontroller.linklayer import (
    LL_MAFS_US,
    LL_SCA_50_PPM,
    LL_SCA_500_PPM,
    AdvertisingPduType,
)
from bscontroller.linklayer.advertising_pdu import ExtendedPdu
from bscontroller.linklayer.state import DataStatus
from bscontroller.linklayer.timing import window_widening
from bscontroller.radio.base import Phy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from bscontroller.linklayer.resource import RadioResourceManager
    from bscontroller.radio.base import Radio, Received


async def follow_auxiliary(
    radio: Radio,
    resource: RadioResourceManager,
    packet: Received,
    ext: ExtendedPdu,
    **parameters,
):
    """Receive the auxiliary packet an AuxPtr points to, widened per Core Vol 6,
    Part B, 4.2.4 Table 4.1.
    """
    aux = ext.aux_ptr
    assert aux is not None
    start = packet.start_us + aux.offset_us
    phy = Phy(aux.phy)
    widening = window_widening(
        aux.offset_us, LL_SCA_50_PPM if aux.clock_accuracy else LL_SCA_500_PPM
    )
    unit = aux.unit_us
    if start + unit <= packet.end_us + LL_MAFS_US:
        return None
    until = start + widening + unit + phy.address_us
    if until <= radio.now_us or not resource.fits_radio(until):
        return None
    received = await radio.receive(
        aux.channel,
        until,
        start_us=max(radio.now_us + 1, start - widening),
        deadline_us=resource.deadline_us,
        phy=phy,
        **parameters,
    )
    return (
        received
        if received and received.start_us >= packet.end_us + LL_MAFS_US
        else None
    )


@dataclass(frozen=True)
class ChainFragment:
    data: bytes
    status: DataStatus
    rssi: float
    tx_power: int | None


async def receive_chain(
    radio: Radio,
    resource: RadioResourceManager,
    packet: Received,
    first: ExtendedPdu,
    *,
    is_current: Callable[[], bool],
    **parameters,
) -> AsyncIterator[ChainFragment]:
    """Yield received fragments followed by a complete or truncated report.

    See Bluetooth Core 6.3, Vol 6, Part B, 2.3.1.8 and Vol 4, Part E, 7.7.65.13.
    """
    data = first.data
    current = first
    total = len(data)
    # Any failed follow/decode/identity/size check leaves a truncated report.
    # Only reaching a PDU without AuxPtr proves the chain is complete.
    status = DataStatus.TRUNCATED
    while current.aux_ptr:
        if current.aux_ptr.phy != packet.phy:
            break
        if data:
            yield ChainFragment(
                data, DataStatus.INCOMPLETE, packet.rssi, current.tx_power
            )
            data = b''
        if not is_current():
            break
        received = await follow_auxiliary(
            radio, resource, packet, current, **parameters
        )
        if received is None or not is_current():
            break
        try:
            ext = ExtendedPdu.decode(received.pdu)
        except ValueError:
            break
        if (
            ext.kind != AdvertisingPduType.AUX_CHAIN_IND
            or ext.mode
            or (first.adi is not None and ext.adi != first.adi)
        ):
            # Stop on a different ADI rather than joining data from two trains
            # (Vol 6, Part B, 2.3.1.8).
            break
        if ext.aux_ptr and not ext.data:
            break
        # Core Vol6 PartB Table2.7:chain addresses/SyncInfo/ACAD are ignored.
        if total + len(ext.data) > CONTROLLER_ADV_DATA_SIZE_MAX:
            break
        current = ext
        data = ext.data
        total += len(ext.data)
        packet = received
    else:
        status = DataStatus.COMPLETE
    yield ChainFragment(data, status, packet.rssi, current.tx_power)


async def collect_chain(
    radio: Radio,
    resource: RadioResourceManager,
    packet: Received,
    first: ExtendedPdu,
    *,
    is_current: Callable[[], bool],
) -> tuple[bytes, DataStatus, int | None]:
    """Assemble a single extended advertising report from received fragments."""
    data = []
    async for fragment in receive_chain(
        radio, resource, packet, first, is_current=is_current
    ):
        data.append(fragment.data)
    return b''.join(data), fragment.status, fragment.tx_power
