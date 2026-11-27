# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Channel Map Update and Minimum Number Of Used Channels (Core Vol 6, Part B,
5.1.2, 5.1.11). Scenarios drive the LLCP harness (llcp/harness.py) and follow
Zephyr's ctrl_chmu and ctrl_min_used_chans suites.
"""

import pytest

from bscontroller.linklayer import (
    LL_DATA_CHANNEL_COUNT,
    LL_DATA_CHANNEL_MAP_ALL,
    LL_EVENT_COUNTER_MASK,
    LL_INSTANT_MIN_LISTENING_EVENTS,
)
from bscontroller.linklayer.control import valid_minimum_channels
from bscontroller.linklayer.features import LeFeatureMask
from bscontroller.linklayer.instant import InstantUpdate
from bscontroller.linklayer.timing import LL_INSTANT_PAST_THRESHOLD
from bscontroller.radio.base import Phy
from tests.unit.linklayer.llcp.harness import (
    CENTRAL,
    PHY_1M,
    PHY_2M,
    PHY_CODED,
    ErrorCode,
    Opcode,
    channel_map_ind,
    connection_update_ind,
    hci,
    instant_of,
    pdu,
    reject_ext_ind,
    unknown_rsp,
)

DISCONNECTED = hci.HCI_Disconnection_Complete_Event
MIN_USED = LeFeatureMask.MINIMUM_NUMBER_OF_USED_CHANNELS_PROCEDURE


def min_used(phys, minimum):
    return pdu(Opcode.LL_MIN_USED_CHANNELS_IND, phys, minimum)


@pytest.fixture
def narrow(central):
    """A Central whose Host has classified only channels 0 and 1 as usable."""
    central.set_parameters(channel_map=0x03)
    central.controls.host_channel_map = 0x03
    return central


# ---------------------------------------------------------------------------
# Channel Map Update, Central local
# ---------------------------------------------------------------------------
def test_chmu_central_loc(central):
    """A Central Channel Map Update applies the new map at its Instant.

    Purpose: Verify that a Host channel classification on a Central sends
    CHANNEL_MAP_IND with no response timer, applies the new map at the Instant,
    and produces no Host notification (Vol 6, Part B, 5.1.2).

    Steps:
        1. Upper Tester classifies channels to 0x1F.
        2. Lower Tester receives CHANNEL_MAP_IND and acknowledges.
        3. Idle events run up to the Instant.

    Expected:
        - No response timer is armed; the Instant is the minimum listening events
          ahead.
        - The channel map becomes 0x1F only at the Instant event.
        - No Host notification; the procedure is idle.
    """
    service = central.controller.le_procedures.ll
    assert service.classify_channels(0x1F) == ErrorCode.SUCCESS
    # No response timer for Channel Map Update (5.2).
    assert central.connection.llcp.next_deadline_us is None
    central.event_prepare()
    indication = central.lt_rx(Opcode.LL_CHANNEL_MAP_IND)
    assert int.from_bytes(indication[1:6], 'little') == 0x1F
    assert instant_of(indication) == (
        central.event_counter + LL_INSTANT_MIN_LISTENING_EVENTS
    )
    central.tx_ack()
    central.event_done()
    central.wait_instant(instant_of(indication))
    assert central.connection.parameters.channel_map != 0x1F
    central.event_prepare()
    assert central.connection.parameters.channel_map == 0x1F
    # No Host notification for Channel Map Update.
    central.ut_rx_q_is_empty()
    assert not central.connection.llcp.local_procedure_busy


def test_chmu_waits_for_cis_creation(iut, controller):
    """Queue the latest Host classification behind CIS Creation (Core 6.B.5).

    Steps:
        1. Create a CIS and acknowledge LL_CIS_REQ without answering the request.
        2. Supply two channel classifications a second apart. Observe the ACL
           before and after the peer rejects the CIS request.

    Expected:
        - Both HCI commands succeed. Only Empty PDUs are sent until the rejection.
        - The first Channel Map Update contains the latest classification.
    """
    controller.on_packet(
        bytes(
            hci.HCI_LE_Set_Host_Feature_Command(
                hci.LeFeature.CONNECTED_ISOCHRONOUS_STREAM, 1
            )
        )
    )
    assert controller.events[-1].return_parameters.status == ErrorCode.SUCCESS
    central = iut(CENTRAL)
    central.set_peer_features(LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_PERIPHERAL)
    (result,) = central.hci(
        hci.HCI_LE_Set_CIG_Parameters_Command(
            1, 20_000, 20_000, 0, 0, 0, 40, 40, [1], [40], [40], [1], [1], [1], [1]
        )
    )
    (handle,) = result.return_parameters.connection_handle
    (status,) = central.hci(hci.HCI_LE_Create_CIS_Command([handle], [central.handle]))
    assert status.status == ErrorCode.SUCCESS
    central.event_prepare()
    central.lt_rx(Opcode.LL_CIS_REQ)
    central.tx_ack()
    for channel_map in (0x07, 0x1F):
        central.elapse(1_000_000)
        (result,) = central.hci(
            hci.HCI_LE_Set_Host_Channel_Classification_Command(
                channel_map.to_bytes(5, 'little')
            )
        )
        assert result.return_parameters.status == ErrorCode.SUCCESS
        central.event_prepare()
        central.lt_rx_empty()

    central.lt_tx(
        reject_ext_ind(Opcode.LL_CIS_REQ, ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)
    )
    central.event_prepare()
    update = central.lt_rx(Opcode.LL_CHANNEL_MAP_IND)
    assert int.from_bytes(update[1:6], 'little') == 0x1F


def test_chmu_central_loc_unacked_at_instant(central):
    """An unacknowledged CHANNEL_MAP_IND at its Instant closes the link.

    Purpose: Verify that if the peer never acknowledges the Central's
    CHANNEL_MAP_IND before the Instant, the connection closes with
    instant-passed and the map is not applied.

    Steps:
        1. Upper Tester classifies channels to 0x1F.
        2. Lower Tester receives CHANNEL_MAP_IND but never acknowledges.
        3. Advance the event counter to the Instant and prepare that event.

    Expected:
        - The channel map is not changed to 0x1F.
        - Host gets Disconnection Complete (instant passed).
    """
    central.controller.le_procedures.ll.classify_channels(0x1F)
    central.event_prepare()
    indication = central.lt_rx(Opcode.LL_CHANNEL_MAP_IND)
    central.event_done()
    central.connection.event_counter = instant_of(indication)
    central.event_prepare()
    assert central.connection.parameters.channel_map != 0x1F
    central.ut_rx(DISCONNECTED, reason=ErrorCode.INSTANT_PASSED_ERROR)


def test_chmu_instant_assigned_at_first_transmission(central):
    """The CHANNEL_MAP_IND Instant is fixed at first transmission.

    Purpose: Verify that the Instant is computed from the event counter at the
    first transmission of CHANNEL_MAP_IND and that a retransmission in a later
    event keeps the same Instant.

    Steps:
        1. Request a channel map and set the event counter to 40.
        2. Transmit CHANNEL_MAP_IND and read its Instant.
        3. Advance the counter to 41 and transmit again.

    Expected:
        - The Instant is 40 plus the minimum listening events.
        - The retransmission carries the identical indication.
    """
    central.controls.request_channel_map(3)
    central.connection.event_counter = 40
    indication = central.lt_rx(Opcode.LL_CHANNEL_MAP_IND)
    assert instant_of(indication) == 40 + LL_INSTANT_MIN_LISTENING_EVENTS
    central.connection.event_counter = 41
    assert central.lt_rx(Opcode.LL_CHANNEL_MAP_IND) == indication


def test_chmu_peripheral_host_classification_is_not_sent(peripheral):
    """A Peripheral does not transmit a host channel classification.

    Purpose: Verify that classifying channels on a Peripheral is accepted but
    never produces a CHANNEL_MAP_IND, since only the Central drives the update
    (Vol 6, Part B, 5.1.2).

    Steps:
        1. Upper Tester classifies channels to 0x1F on the Peripheral.

    Expected:
        - The call succeeds.
        - No control PDU is queued for the air.
    """
    service = peripheral.controller.le_procedures.ll
    assert service.classify_channels(0x1F) == ErrorCode.SUCCESS
    peripheral.lt_rx_q_is_empty()


# ---------------------------------------------------------------------------
# Channel Map Update, Peripheral remote
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'counter',
    [
        0,
        LL_EVENT_COUNTER_MASK - LL_INSTANT_MIN_LISTENING_EVENTS + 1,
        LL_EVENT_COUNTER_MASK,
    ],
)
def test_chmu_periph_rem_with_counter_wrap(peripheral, counter):
    """A Peripheral applies a peer CHANNEL_MAP_IND exactly at its Instant.

    Purpose: Verify that a Peripheral defers a received CHANNEL_MAP_IND until its
    Instant event and then applies the new map, including event-counter wrap
    cases around the boundary.

    Steps:
        1. Set the event counter to the parametrized value.
        2. Lower Tester sends CHANNEL_MAP_IND with the Instant ahead.
        3. Run idle events up to, then onto, the Instant.

    Expected:
        - The map stays unchanged for every event before the Instant.
        - The map becomes 0x03 at the Instant and nothing stays pending.
        - No Host notification is produced.
    """
    peripheral.connection.event_counter = counter
    instant = (counter + LL_INSTANT_MIN_LISTENING_EVENTS) & LL_EVENT_COUNTER_MASK
    peripheral.event_prepare()
    peripheral.lt_tx(channel_map_ind(0x03, instant))
    peripheral.event_done()
    for _ in range(LL_INSTANT_MIN_LISTENING_EVENTS - 1):
        peripheral.event_prepare()
        assert peripheral.connection.parameters.channel_map != 0x03
        peripheral.event_done()
    peripheral.event_prepare()
    assert peripheral.connection.parameters.channel_map == 0x03
    assert peripheral.controls.pending is None
    peripheral.ut_rx_q_is_empty()


@pytest.mark.parametrize(
    'counter,instant',
    [
        (0, LL_EVENT_COUNTER_MASK),
        (1, 0),
        (LL_EVENT_COUNTER_MASK, LL_EVENT_COUNTER_MASK - 1),
        (0, LL_INSTANT_PAST_THRESHOLD),
    ],
)
def test_chmu_periph_rem_instant_passed(peripheral, counter, instant):
    """A peer CHANNEL_MAP_IND with a past Instant closes the link.

    Purpose: Verify that a Peripheral rejects a CHANNEL_MAP_IND whose Instant has
    already passed (including wrap and the past-threshold boundary), closing the
    connection with instant-passed.

    Steps:
        1. Set the event counter to the parametrized value.
        2. Lower Tester sends CHANNEL_MAP_IND with the past Instant.

    Expected:
        - Host gets Disconnection Complete (instant passed).
    """
    peripheral.connection.event_counter = counter
    peripheral.lt_tx(channel_map_ind(0x03, instant))
    peripheral.ut_rx(DISCONNECTED, reason=ErrorCode.INSTANT_PASSED_ERROR)


@pytest.mark.parametrize('channel_map', [0, 1, 1 << 37], ids=['none', 'one', 'rfu'])
def test_chmu_periph_rem_invalid_map(peripheral, channel_map):
    """A peer CHANNEL_MAP_IND with an invalid map closes the link.

    Purpose: Verify that a Peripheral rejects a channel map with no usable data
    channel or an RFU bit set, closing the connection with invalid parameters,
    across the none/one/rfu cases.

    Steps:
        1. Lower Tester sends CHANNEL_MAP_IND with the invalid map.

    Expected:
        - Host gets Disconnection Complete (invalid parameters).
    """
    peripheral.lt_tx(channel_map_ind(channel_map, 10))
    peripheral.ut_rx(DISCONNECTED, reason=ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR)


def test_chmu_received_by_central_is_unknown(central):
    """A Central answers a received CHANNEL_MAP_IND with UNKNOWN_RSP.

    Purpose: Verify that CHANNEL_MAP_IND is only legal from a Central, so a
    Central IUT that receives one replies UNKNOWN_RSP.

    Steps:
        1. Lower Tester sends CHANNEL_MAP_IND to the Central.
        2. Lower Tester receives the Central's response.

    Expected:
        - The IUT transmits UNKNOWN_RSP(LL_CHANNEL_MAP_IND).
    """
    central.lt_tx(channel_map_ind(0x03, 10))
    central.lt_rx(Opcode.LL_UNKNOWN_RSP, unknown_rsp(Opcode.LL_CHANNEL_MAP_IND))


def test_chmu_periph_while_connection_update_pending(peripheral):
    """A CHANNEL_MAP_IND while a Connection Update is pending closes the link.

    Purpose: Verify that a Peripheral cannot have two Instant-bearing updates
    pending at once: a CHANNEL_MAP_IND arriving while a Connection Update awaits
    its Instant is invalid and closes the connection.

    Steps:
        1. Lower Tester sends CONNECTION_UPDATE_IND with an Instant ahead.
        2. Lower Tester sends CHANNEL_MAP_IND before the first Instant.

    Expected:
        - Host gets Disconnection Complete (invalid parameters).
    """
    peripheral.lt_tx(connection_update_ind(40, 20))
    peripheral.lt_tx(channel_map_ind(0x03, 22))
    peripheral.ut_rx(DISCONNECTED, reason=ErrorCode.INVALID_LMP_OR_LL_PARAMETERS_ERROR)


def test_chmu_skipped_events_still_apply_instant(central):
    """A channel map Instant still applies after skipped connection events.

    Purpose: Verify that when several connection events are skipped (no radio
    activity), catching up still applies the pending channel map at its Instant
    and advances the unmapped channel consistently.

    Steps:
        1. Accept a pending channel-map InstantUpdate at Instant 1.
        2. Advance simulated time past two connection intervals.
        3. Run skip_missed_events() to catch up.

    Expected:
        - The event counter reaches 3 and the unmapped channel matches the hop.
        - The channel map equals the pending value; nothing stays pending and no
          PDU was transmitted.
    """
    from bscontroller.linklayer import LL_CONN_INTERVAL_UNIT_US

    connection = central.connection
    central.controls.accept_instant(InstantUpdate(1, channel_map=(1 << 2) | (1 << 8)))
    central.radio.now_us = (
        connection.anchor_us
        + 2 * connection.parameters.interval * LL_CONN_INTERVAL_UNIT_US
        + 1
    )
    connection.skip_missed_events()
    assert connection.event_counter == 3
    assert (
        connection.unmapped_channel
        == 3 * connection.parameters.hop % LL_DATA_CHANNEL_COUNT
    )
    assert connection.parameters.channel_map == (1 << 2) | (1 << 8)
    assert central.controls.pending is None and not central.radio.sent


# ---------------------------------------------------------------------------
# Minimum Number Of Used Channels, Central remote
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'rx_phy,phys',
    [
        (Phy.LE_1M, PHY_1M),
        (Phy.LE_1M, PHY_2M),
        (Phy.LE_2M, PHY_2M),
        (Phy.LE_2M, PHY_CODED),
        (Phy.LE_CODED, PHY_CODED),
        (Phy.LE_CODED, PHY_1M),
        (Phy.LE_1M, PHY_1M | PHY_2M | PHY_CODED),
    ],
)
def test_min_used_central_rem(narrow, rx_phy, phys):
    """A MIN_USED_CHANNELS_IND widens the map only when it applies to the RX PHY.

    Purpose: Verify that a Central records the peer's minimum-used-channels
    request and issues a Channel Map Update to widen its narrow map only when the
    indication's PHY mask includes the current receive PHY (Vol 6, Part B,
    5.1.11), across several rx_phy/phys combinations.

    Steps:
        1. Set the connection RX and TX PHYs.
        2. Lower Tester sends MIN_USED_CHANNELS_IND(phys, 4).
        3. If it applies, the Lower Tester receives CHANNEL_MAP_IND and acks.

    Expected:
        - The peer's PHY mask and minimum count are recorded.
        - When applicable, CHANNEL_MAP_IND widens the map to 0x0F; otherwise none.
        - No response timer and no Host event; the stored map stays 0x03.
    """
    connection = narrow.connection
    connection.rx_phy = rx_phy
    connection.tx_phy = Phy.LE_2M if rx_phy == Phy.LE_1M else Phy.LE_1M
    narrow.lt_tx(min_used(phys, 4))
    applies = bool(phys & (1 << (rx_phy - 1)))
    assert narrow.controls.peer_minimum_channel_phys == phys
    assert narrow.controls.peer_minimum_used_channels == 4
    if applies:
        # The Central widens its map with a Channel Map Update (5.1.11).
        indication = narrow.lt_rx(Opcode.LL_CHANNEL_MAP_IND)
        assert int.from_bytes(indication[1:6], 'little') == 0x0F
        narrow.tx_ack()
    else:
        narrow.lt_rx_q_is_empty()
    assert connection.parameters.channel_map == 0x03
    # Indication only; no response timer and no Host event.
    assert narrow.connection.llcp.response_deadline_us is None
    narrow.ut_rx_q_is_empty()


@pytest.mark.parametrize('phys,minimum', [(0, 2), (8, 2), (1, 1), (1, 38)])
def test_min_used_central_rem_invalid(narrow, phys, minimum):
    """An invalid MIN_USED_CHANNELS_IND is answered with UNKNOWN_RSP.

    Purpose: Verify that a MIN_USED_CHANNELS_IND with an empty PHY mask, an RFU
    PHY bit, or an out-of-range minimum is rejected with UNKNOWN_RSP and records
    nothing, across several invalid pairs.

    Steps:
        1. Lower Tester sends MIN_USED_CHANNELS_IND(phys, minimum).
        2. Lower Tester receives the Central's response.

    Expected:
        - The IUT transmits UNKNOWN_RSP(LL_MIN_USED_CHANNELS_IND).
        - No peer PHY mask is recorded and nothing is pending.
    """
    narrow.lt_tx(min_used(phys, minimum))
    narrow.lt_rx(Opcode.LL_UNKNOWN_RSP, unknown_rsp(Opcode.LL_MIN_USED_CHANNELS_IND))
    assert not narrow.controls.peer_minimum_channel_phys
    assert narrow.controls.pending is None


def test_min_used_rfu_bits_are_ignored(narrow):
    """RFU bits in a MIN_USED_CHANNELS_IND PHY mask are ignored.

    Purpose: Verify that an RFU bit set alongside a valid PHY bit is masked off:
    the recorded mask keeps only 1M and the map still widens to 0x0F.

    Steps:
        1. Lower Tester sends MIN_USED_CHANNELS_IND(0x81, 4).

    Expected:
        - The recorded peer PHY mask is PHY_1M only.
        - The pending update's channel map is 0x0F.
    """
    narrow.lt_tx(min_used(0x81, 4))
    assert narrow.controls.peer_minimum_channel_phys == PHY_1M
    assert narrow.controls.pending.channel_map == 0x0F


def test_min_used_preserves_host_channels(narrow):
    """Widening for a peer minimum keeps the Host's classified channels.

    Purpose: Verify that selected_channel_map() unions the Host's classified
    channels with the low channels added to meet the peer minimum, and that a
    full minimum selects every channel (Vol 6, Part B, 5.1.11).

    Steps:
        1. Set a sparse host channel map and record the peer minimum request.
        2. Evaluate selected_channel_map() at a small and a full minimum.

    Expected:
        - At minimum 4, the selection is the host map unioned with 0x03.
        - At the full channel count, the selection is the all-channels map.
    """
    controls = narrow.controls
    controls.host_channel_map = (1 << 5) | (1 << 8)
    controls.peer_minimum_channel_phys = PHY_1M
    controls.peer_minimum_used_channels = 4
    assert controls.selected_channel_map() == controls.host_channel_map | 0x03
    controls.peer_minimum_used_channels = LL_DATA_CHANNEL_COUNT
    assert controls.selected_channel_map() == LL_DATA_CHANNEL_MAP_ALL


def test_min_used_retained_for_later_host_classification(narrow):
    """A recorded peer minimum is reapplied on a later host classification.

    Purpose: Verify that the stored peer minimum-used-channels requirement is
    honoured when the Host later classifies channels: the resulting map unions
    the new classification with the low channels needed for the minimum.

    Steps:
        1. Lower Tester sends MIN_USED_CHANNELS_IND and set the minimum to 4.
        2. Upper Tester classifies a new sparse channel map.

    Expected:
        - The host channel map equals the new classification.
        - The pending update's map is that classification unioned with 0x03.
    """
    narrow.lt_tx(min_used(PHY_1M, 2))
    narrow.controls.peer_minimum_used_channels = 4
    classification = (1 << 5) | (1 << 8)
    narrow.controls.request_channel_map(classification)
    assert narrow.controls.host_channel_map == classification
    assert narrow.controls.pending.channel_map == classification | 0x03


def test_min_used_latest_indication_replaces_previous(narrow):
    """A later MIN_USED_CHANNELS_IND replaces the earlier one.

    Purpose: Verify that only the most recent peer minimum-used-channels
    indication is kept, so the selected map depends on the current RX PHY against
    the latest PHY mask.

    Steps:
        1. Lower Tester sends MIN_USED_CHANNELS_IND for 2M, then for coded.
        2. Evaluate selected_channel_map() with RX PHY 2M, then coded.

    Expected:
        - The recorded mask and minimum are those of the second indication.
        - With RX PHY 2M the selection stays 0x03; with coded it widens to 0x1F.
    """
    narrow.lt_tx(min_used(PHY_2M, 4))
    narrow.lt_tx(min_used(PHY_CODED, 5))
    assert narrow.controls.peer_minimum_channel_phys == PHY_CODED
    assert narrow.controls.peer_minimum_used_channels == 5
    narrow.connection.rx_phy = Phy.LE_2M
    assert narrow.controls.selected_channel_map() == 0x03
    narrow.connection.rx_phy = Phy.LE_CODED
    assert narrow.controls.selected_channel_map() == 0x1F


def test_min_used_is_connection_local(narrow):
    """A peer minimum-used-channels request affects only its own connection.

    Purpose: Verify that recording a peer minimum on one connection does not
    leak into a second connection's channel selection or pending state.

    Steps:
        1. Add a second Central connection sharing the parameters.
        2. Lower Tester sends MIN_USED_CHANNELS_IND on the first connection.

    Expected:
        - The first connection's pending map becomes the all-channels map.
        - The second connection records no peer mask, selects 0x03 and has no
          pending update.
    """
    from tests.unit.helpers import add_connection

    second = add_connection(narrow.connection, 1, CENTRAL)
    second.parameters = narrow.connection.parameters
    second.controls.host_channel_map = 0x03
    narrow.lt_tx(min_used(PHY_1M, LL_DATA_CHANNEL_COUNT))
    assert narrow.controls.pending.channel_map == LL_DATA_CHANNEL_MAP_ALL
    assert not second.controls.peer_minimum_channel_phys
    assert second.controls.selected_channel_map() == 0x03
    assert second.controls.pending is None


def test_min_used_waits_for_pending_instant(narrow):
    """Map widening for a peer minimum defers until a pending update completes.

    Purpose: Verify that when a Connection Update is already awaiting its
    Instant, a peer minimum-used-channels indication does not start a Channel
    Map Update until that update completes, after which the widening runs with a
    new Instant.

    Steps:
        1. Start a Connection Update and receive its UPDATE_IND.
        2. Lower Tester sends MIN_USED_CHANNELS_IND while the update is pending.
        3. Run to the update Instant, then let the CHANNEL_MAP_IND follow.

    Expected:
        - No CHANNEL_MAP_IND is queued while the update is pending.
        - After the update completes, CHANNEL_MAP_IND widens the map to 0x0F.
        - The map becomes 0x0F at its Instant and nothing stays pending.
    """
    controls = narrow.controls
    narrow.set_peer_features(0)
    narrow.host_start('request_connection_update', 40, 40, 0, 100)
    update = narrow.lt_rx(Opcode.LL_CONNECTION_UPDATE_IND)
    narrow.lt_tx(min_used(PHY_1M, 4))
    narrow.lt_rx_q_is_empty()
    narrow.wait_instant(instant_of(update))
    narrow.event_prepare()
    narrow.ut_rx(hci.HCI_LE_Connection_Update_Complete_Event)
    # After the update the deferred map widening starts with a new Instant.
    assert narrow.connection.parameters.interval == 40
    assert narrow.connection.parameters.channel_map == 0x03
    indication = narrow.lt_rx(Opcode.LL_CHANNEL_MAP_IND)
    assert int.from_bytes(indication[1:6], 'little') == 0x0F
    narrow.tx_ack()
    narrow.wait_instant(instant_of(indication))
    narrow.event_prepare()
    assert narrow.connection.parameters.channel_map == 0x0F
    assert controls.pending is None


def test_min_used_rechecked_after_phy_update(narrow):
    """A peer minimum is re-evaluated when a PHY update makes it apply.

    Purpose: Verify that a peer minimum-used-channels indication that did not
    apply to the current PHY is reconsidered after a pending PHY update brings
    the matching PHY into effect, widening the map at that Instant.

    Steps:
        1. Lower Tester sends MIN_USED_CHANNELS_IND for 2M (no effect yet).
        2. Stage a pending PHY update to 2M and advance to its Instant.
        3. Prepare the Instant event.

    Expected:
        - No update is pending immediately after the indication.
        - After the PHY becomes 2M, the pending update's map is 0x0F.
    """
    narrow.lt_tx(min_used(PHY_2M, 4))
    assert narrow.controls.pending is None
    narrow.controls.schedule_instant(InstantUpdate(6, peripheral_tx_phy=Phy.LE_2M))
    narrow.connection.event_counter = 6
    narrow.event_prepare()
    assert narrow.connection.rx_phy == Phy.LE_2M
    assert narrow.controls.pending.channel_map == 0x0F


def test_min_used_central_without_local_feature_is_unknown(narrow):
    """A Central lacking the local feature answers MIN_USED with UNKNOWN_RSP.

    Purpose: Verify that if the IUT does not advertise the Minimum Number Of
    Used Channels feature, a received MIN_USED_CHANNELS_IND is answered with
    UNKNOWN_RSP.

    Steps:
        1. Clear the local MIN_USED feature bit.
        2. Lower Tester sends MIN_USED_CHANNELS_IND(PHY_1M, 2).

    Expected:
        - The IUT transmits UNKNOWN_RSP(LL_MIN_USED_CHANNELS_IND).
    """
    narrow.link.features &= ~int(MIN_USED)
    narrow.lt_tx(min_used(PHY_1M, 2))
    narrow.lt_rx(Opcode.LL_UNKNOWN_RSP, unknown_rsp(Opcode.LL_MIN_USED_CHANNELS_IND))


def test_min_used_received_by_peripheral_is_unknown(peripheral):
    """A Peripheral answers a received MIN_USED_CHANNELS_IND with UNKNOWN_RSP.

    Purpose: Verify that MIN_USED_CHANNELS_IND is only legal from a Peripheral to
    its Central, so a Peripheral that receives one replies UNKNOWN_RSP.

    Steps:
        1. Lower Tester sends MIN_USED_CHANNELS_IND to the Peripheral.
        2. Lower Tester receives the Peripheral's response.

    Expected:
        - The IUT transmits UNKNOWN_RSP(LL_MIN_USED_CHANNELS_IND).
    """
    peripheral.lt_tx(min_used(PHY_1M, 2))
    peripheral.lt_rx(
        Opcode.LL_UNKNOWN_RSP, unknown_rsp(Opcode.LL_MIN_USED_CHANNELS_IND)
    )


@pytest.mark.parametrize(
    'phys,minimum,expected',
    [
        (1, 2, True),
        (7, 37, True),
        (0, 2, False),
        (8, 2, False),
        (9, 2, False),
        (1, 1, False),
        (1, 38, False),
    ],
)
def test_min_used_argument_boundaries(phys, minimum, expected):
    """valid_minimum_channels enforces PHY mask and count bounds.

    Purpose: Verify that valid_minimum_channels accepts only a non-empty PHY mask
    with no RFU bits and a minimum within the allowed channel-count range, across
    several boundary pairs.

    Steps:
        1. Call valid_minimum_channels(phys, minimum) for each parametrized pair.

    Expected:
        - The result matches the expected validity flag.
    """
    assert valid_minimum_channels(phys, minimum) is expected
