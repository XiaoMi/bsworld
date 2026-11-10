# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""HCI advertising, scanning and sync state seen by the Host (Core Vol 4, Part
E, 7.8.5-7.8.11, 7.8.53-7.8.71, 7.7.65).

On-air behaviour behind these commands is specified in linklayer/air/. Here the
matrix is the HCI contract: {command} x {current state} x {parameter validity},
the public record views, and how LL reports become Host events (masks,
duplicates, legacy vs extended report formats, fragmentation).
"""

from dataclasses import replace

import pytest

from bscontroller import hci
from bscontroller.config import ControllerConfig
from bscontroller.hci import HCI_LE_ADV_DATA_SIZE_MAX
from bscontroller.linklayer import LL_MAFS_US, LL_PERIODIC_INTERVAL_UNIT_US
from bscontroller.linklayer.discovery import Advertising, PeriodicSync
from bscontroller.linklayer.extended import advertising_time_us, periodic_time_us
from bscontroller.linklayer.state import (
    AdvertisingProperties,
    AdvertisingReport,
    DataStatus,
)
from bscontroller.radio.base import Phy
from tests.unit.hci.harness import IUT_ADDRESS, PEERS, ErrorCode
from tests.unit.helpers import PEER_ADDRESS, create_sync, extended_parameters

Operation = hci.HCI_LE_Set_Extended_Advertising_Data_Command.Operation
Options = hci.HCI_LE_Periodic_Advertising_Create_Sync_Command.Options
Properties = (
    hci.HCI_LE_Set_Extended_Advertising_Parameters_Command.AdvertisingProperties
)
LEGACY = AdvertisingProperties.LEGACY_ADVERTISING_PDU_USED
ESTABLISHED = hci.HCI_LE_Periodic_Advertising_Sync_Established_Event


def data(handle, operation, payload, field='advertising'):
    command = (
        hci.HCI_LE_Set_Extended_Advertising_Data_Command
        if field == 'advertising'
        else hci.HCI_LE_Set_Extended_Scan_Response_Data_Command
    )
    return command(handle, operation, 0, payload)


def periodic_data(handle, operation, payload):
    return hci.HCI_LE_Set_Periodic_Advertising_Data_Command(handle, operation, payload)


# ---------------------------------------------------------------------------
# Public advertising records (read-only views of LL state)
# ---------------------------------------------------------------------------
def test_extended_set_record_is_published_with_parameters(host):
    """An extended advertising set publishes a record carrying its parameters.

    Purpose: Verify setting extended advertising parameters publishes a
    read-only advertising-set record with the right handle, address and empty
    initial data (Vol 4, Part E, 7.8.53).

    Steps:
        1. Set extended advertising parameters for handle 3.

    Expected:
        - The record at handle 3 carries that handle, the IUT public address, no
          random address, is disabled and has empty data.
    """
    command = extended_parameters(advertising_handle=3)
    host.cmd(command)
    record = host.controller.advertising_sets[3]
    assert record.parameters.advertising_handle == 3
    assert record.address == IUT_ADDRESS and record.random_address is None
    assert not record.enabled and record.data == b''


def test_record_views_follow_ll_payloads_and_are_immutable(host):
    """Advertising-set record views track LL payloads and cannot be mutated.

    Purpose: Verify the record exposes advertising, scan-response and periodic
    payloads from LL state as immutable snapshots, that re-sending parameters
    preserves stored payloads, and that later LL changes do not alter an earlier
    snapshot (Vol 4, Part E, 7.8.53-7.8.62).

    Steps:
        1. Set parameters, store the three payload kinds via the LL service and
           snapshot the data.
        2. Attempt to assign to ``record.data`` and re-send parameters.
        3. Store new advertising data.

    Expected:
        - Assigning ``record.data`` raises ``AttributeError``; re-sending
          parameters keeps all three payloads; the earlier snapshot stays at the
          old value while the record reflects the new one.
    """
    # Legacy PDUs through extended commands may carry both payload kinds.
    host.cmd(extended_parameters(advertising_event_properties=16))
    record = host.controller.advertising_sets[0]
    service = host.ll.advertising
    service.store_data(0, 'data', b'advertising data')
    service.store_data(0, 'scan_response', b'scan response')
    service.store_data(0, 'periodic_data', b'periodic data')
    snapshot = record.data
    assert isinstance(snapshot, bytes)
    with pytest.raises(AttributeError):
        record.data = b'bypass'
    # Re-sending parameters must not discard the published payloads.
    host.cmd(extended_parameters(advertising_event_properties=16))
    assert (
        record.data,
        record.scan_response_data,
        record.periodic_advertising_data,
    ) == (
        b'advertising data',
        b'scan response',
        b'periodic data',
    )
    service.store_data(0, 'data', b'new')
    assert record.data == b'new' and snapshot == b'advertising data'


def test_record_without_ll_state_uses_its_own_storage(host):
    """A record with no LL backing returns its own stored fields.

    Purpose: Verify ``AdvertisingSet`` and ``LegacyAdvertiser`` records
    constructed without live LL state expose the values they were created with.

    Steps:
        1. Build an ``AdvertisingSet`` with explicit data, enabled flag and
           random address, and a ``LegacyAdvertiser`` with its own fields.

    Expected:
        - Each record reports its stored payloads, enabled flag and addresses;
          the set with a random address has no public address.
    """
    from bscontroller.hci.records import AdvertisingSet, LegacyAdvertiser

    record = AdvertisingSet(
        host.controller,
        9,
        data=bytearray(b'd'),
        scan_response_data=bytearray(b's'),
        periodic_advertising_data=bytearray(b'p'),
        enabled=True,
        random_address=PEERS[0],
    )
    assert (
        record.data,
        record.scan_response_data,
        record.periodic_advertising_data,
    ) == (
        b'd',
        b's',
        b'p',
    )
    assert record.enabled and record.random_address == PEERS[0]
    assert record.address is None
    legacy = LegacyAdvertiser(
        host.controller, advertising_data=b'a', scan_response_data=b'r', enabled=True
    )
    assert (legacy.advertising_data, legacy.scan_response_data, legacy.enabled) == (
        b'a',
        b'r',
        True,
    )


def test_random_address_record_selection(host):
    """A set's advertising address follows its own-address type and random addr.

    Purpose: Verify Set Advertising Set Random Address is disallowed while the
    set is enabled, that once disabled it updates the record and live address,
    and that the chosen advertising address depends on the own-address type, for
    public and random sets (Vol 4, Part E, 7.8.52).

    Steps:
        1. For a public and a random set, try the random-address command while
           enabled, then while disabled.
        2. Try the command for an unknown handle.

    Expected:
        - While enabled it returns COMMAND_DISALLOWED and leaves the address;
          while disabled it sets the random address and selects the address per
          own-address type; an unknown handle returns
          UNKNOWN_ADVERTISING_IDENTIFIER.
    """
    random = hci.Address('C0:12:34:56:78:9A')
    for handle, own in enumerate(
        (hci.OwnAddressType.PUBLIC, hci.OwnAddressType.RANDOM)
    ):
        host.cmd(extended_parameters(advertising_handle=handle, own_address_type=own))
        live = host.link.advertisers[handle]
        original = live.address
        command = hci.HCI_LE_Set_Advertising_Set_Random_Address_Command(handle, random)
        live.enabled = True
        host.cmd(command, ErrorCode.COMMAND_DISALLOWED_ERROR)
        assert live.address == original and live.random_address is None
        live.enabled = False
        host.cmd(command)
        record = host.controller.advertising_sets[handle]
        assert record.random_address == random
        assert record.address == (
            random if own == hci.OwnAddressType.RANDOM else IUT_ADDRESS
        )
        assert live.address == (
            random if own == hci.OwnAddressType.RANDOM else original
        )
    host.cmd(
        hci.HCI_LE_Set_Advertising_Set_Random_Address_Command(99, random),
        ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR,
    )


def test_is_advertising_tracks_both_families(host):
    """is_advertising reflects both legacy and extended advertising state.

    Purpose: Verify the Controller's ``is_advertising`` flag tracks enabling via
    both the legacy and extended advertising-enable commands (Vol 4, Part E,
    7.8.9, 7.8.56).

    Steps:
        1. Enable then disable legacy advertising, observing the flag.
        2. Reset, configure an extended set and enable it.

    Expected:
        - ``is_advertising`` is true while the legacy advertiser is enabled and
          again once the extended set is enabled.
    """
    controller = host.controller
    assert not controller.is_advertising
    host.cmd(hci.HCI_LE_Set_Advertising_Enable_Command(1))
    assert controller.is_advertising and controller.le_legacy_advertiser.enabled
    host.cmd(hci.HCI_LE_Set_Advertising_Enable_Command(0))
    host.cmd(hci.HCI_Reset_Command())
    host.cmd(extended_parameters(advertising_event_properties=0))
    host.cmd(hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0], [0], [0]))
    assert controller.is_advertising and controller.advertising_sets[0].enabled


def test_legacy_record_address_selection(host):
    """A legacy advertiser uses the random address when the own type is random.

    Purpose: Verify the legacy advertiser's address is the configured random
    address when the advertising parameters select a random own-address type
    (Vol 4, Part E, 7.8.5, 7.8.4).

    Steps:
        1. Set a random address and legacy advertising parameters with random
           own-address type.

    Expected:
        - The legacy advertiser's address equals the random address.
    """
    random = hci.Address('C0:00:00:00:00:07')
    host.cmd(hci.HCI_LE_Set_Random_Address_Command(random))
    host.cmd(
        hci.HCI_LE_Set_Advertising_Parameters_Command(
            32, 32, 0, 1, 0, IUT_ADDRESS, 7, 0
        )
    )
    assert host.controller.le_legacy_advertiser.address == random


def test_remove_and_clear_drop_records(host):
    """Remove and Clear Advertising Sets drop the matching records.

    Purpose: Verify Remove Advertising Set removes one set's record and Clear
    Advertising Sets removes all sets and their live advertisers (Vol 4,
    Part E, 7.8.59, 7.8.60).

    Steps:
        1. Configure sets 0 and 1.
        2. Remove set 0, then clear all.

    Expected:
        - After remove only set 1 remains; after clear no records or live
          advertisers remain.
    """
    host.cmd(extended_parameters(advertising_handle=0))
    host.cmd(extended_parameters(advertising_handle=1))
    host.cmd(hci.HCI_LE_Remove_Advertising_Set_Command(0))
    assert list(host.controller.advertising_sets) == [1]
    host.cmd(hci.HCI_LE_Clear_Advertising_Sets_Command())
    assert not host.controller.advertising_sets and not host.link.advertisers


def test_legacy_data_update_keeps_the_running_context(host):
    """Updating legacy advertising data keeps the running advertiser object.

    Purpose: Verify changing legacy advertising data while enabled updates the
    data in place without replacing the executing advertiser (Vol 4, Part E,
    7.8.7).

    Steps:
        1. Enable legacy advertising and capture the running advertiser.
        2. Set new advertising data.

    Expected:
        - The same advertiser object is still running, enabled, with the updated
          data.
    """
    host.cmd(hci.HCI_LE_Set_Advertising_Enable_Command(1))
    executing = host.link.advertisers[-1]
    host.cmd(hci.HCI_LE_Set_Advertising_Data_Command(b'updated'))
    assert host.link.advertisers[-1] is executing
    assert executing.enabled and bytes(executing.data) == b'updated'


@pytest.mark.parametrize('enable', [2, 0xFF])
def test_legacy_enable_validation(host, enable):
    """Legacy advertising enable rejects out-of-range enable values.

    Purpose: Verify Set Advertising Enable rejects enable values other than 0/1
    (Vol 4, Part E, 7.8.9).

    Steps:
        1. Send Set Advertising Enable with the parametrized invalid value.

    Expected:
        - The command returns INVALID_COMMAND_PARAMETERS.
    """
    host.cmd(
        hci.HCI_LE_Set_Advertising_Enable_Command(enable),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


# ---------------------------------------------------------------------------
# Advertising Tx power (7.8.6)
# ---------------------------------------------------------------------------
def test_read_advertising_power_is_listed_and_defaults_to_zero(host):
    """Read Advertising Physical Channel Tx Power is supported and defaults to 0.

    Purpose: Verify the legacy advertising Tx-power read is listed as supported
    and returns 0 dBm by default without creating any LL advertising state
    (Vol 4, Part E, 7.8.6).

    Steps:
        1. Read the supported-commands mask and check this command's bit.
        2. Read the advertising physical-channel Tx power.

    Expected:
        - The command bit is set; the reply is the known zero-power wire bytes;
          no LL advertising snapshot was created.
    """
    mask = int.from_bytes(
        host.cmd(hci.HCI_Read_Local_Supported_Commands_Command()).supported_commands,
        'little',
    )
    assert (
        mask
        & hci.HCI_SUPPORTED_COMMANDS_MASKS[
            hci.HCI_LE_READ_ADVERTISING_PHYSICAL_CHANNEL_TX_POWER_COMMAND
        ]
    )
    result = host.cmd(hci.HCI_LE_Read_Advertising_Physical_Channel_Tx_Power_Command())
    assert result.wire == bytes.fromhex('040e050107200000')
    assert host.ll.advertising.snapshot(-1) is None  # A read creates no LL state.


@pytest.mark.parametrize('power', [-127, -20, 0, 20])
def test_read_advertising_power_uses_signed_legacy_power(host, power):
    """The advertising Tx-power read returns the signed legacy power.

    Purpose: Verify Read Advertising Physical Channel Tx Power reports the
    configured legacy Tx power as a signed octet (Vol 4, Part E, 7.8.6).

    Steps:
        1. Configure the legacy advertiser with the parametrized Tx power.
        2. Read the advertising physical-channel Tx power.

    Expected:
        - The reported level equals the configured power and the last wire octet
          is its signed encoding.
    """
    commands = host.controller.le_procedures
    host.ll.advertising.configure_legacy(
        replace(commands._legacy_configuration(), tx_power_dbm=power)
    )
    result = host.cmd(hci.HCI_LE_Read_Advertising_Physical_Channel_Tx_Power_Command())
    assert result.tx_power_level == power
    assert result.wire[-1:] == power.to_bytes(1, 'little', signed=True)


# ---------------------------------------------------------------------------
# Extended advertising data assembly (7.8.54, 7.8.55, 7.8.62)
# ---------------------------------------------------------------------------
def test_fragments_publish_only_on_last(host):
    """Fragmented advertising data publishes only on the last fragment.

    Purpose: Verify extended advertising data assembled across FIRST and LAST
    fragments is published only when the last fragment arrives, and a stray last
    fragment afterward is refused (Vol 4, Part E, 7.8.54).

    Steps:
        1. Configure a set and send a FIRST fragment, checking nothing is
           published.
        2. Send a LAST fragment, then another LAST fragment.

    Expected:
        - Data is empty after FIRST; after LAST it equals the concatenation and
          matches the live advertiser with no leftover fragments; the extra LAST
          returns COMMAND_DISALLOWED.
    """
    host.cmd(extended_parameters(advertising_event_properties=0))
    record = host.controller.advertising_sets[0]
    host.cmd(data(0, Operation.FIRST_FRAGMENT, b'first'))
    assert record.data == b''
    host.cmd(data(0, Operation.LAST_FRAGMENT, b'last'))
    assert record.data == b'firstlast' == bytes(host.link.advertisers[0].data)
    assert not host.controller.le_procedures.fragments
    host.cmd(
        data(0, Operation.LAST_FRAGMENT, b'orphan'), ErrorCode.COMMAND_DISALLOWED_ERROR
    )


def test_oversize_sequence_discards_all_data(host):
    """An oversize fragment sequence is rejected and discards all data.

    Purpose: Verify assembling more extended advertising data than the maximum
    fails on the last fragment with MEMORY_CAPACITY_EXCEEDED and leaves no data,
    and that a later orphan last fragment is disallowed (Vol 4, Part E, 7.8.54).

    Steps:
        1. Configure a set, store some complete data, then feed an oversize
           FIRST + several INTERMEDIATE fragments.
        2. Send the LAST fragment that overflows, then an orphan LAST.

    Expected:
        - The LAST returns MEMORY_CAPACITY_EXCEEDED; the record data is empty;
          the orphan LAST returns COMMAND_DISALLOWED; the max is below 7*251.
    """
    host.cmd(extended_parameters(advertising_event_properties=0))
    host.cmd(data(0, Operation.COMPLETE_DATA, b'old'))
    host.cmd(data(0, Operation.FIRST_FRAGMENT, bytes(251)))
    for _ in range(5):
        host.cmd(data(0, Operation.INTERMEDIATE_FRAGMENT, bytes(251)))
    host.cmd(
        data(0, Operation.LAST_FRAGMENT, bytes(251)),
        ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR,
    )
    assert host.controller.advertising_sets[0].data == b''
    host.cmd(data(0, Operation.LAST_FRAGMENT, b'x'), ErrorCode.COMMAND_DISALLOWED_ERROR)
    assert HCI_LE_ADV_DATA_SIZE_MAX < 7 * 251


def test_fragments_disallowed_while_enabled(host):
    """Fragmented data writes are disallowed while the set is enabled.

    Purpose: Verify a FIRST fragment is refused while the advertising set is
    enabled, though a single COMPLETE_DATA write is allowed (Vol 4, Part E,
    7.8.54).

    Steps:
        1. Configure and enable an extended set.
        2. Send a FIRST fragment, then a COMPLETE_DATA write.

    Expected:
        - The FIRST fragment returns COMMAND_DISALLOWED; COMPLETE_DATA succeeds.
    """
    host.cmd(extended_parameters(advertising_event_properties=0))
    host.cmd(hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0], [0], [0]))
    host.cmd(
        data(0, Operation.FIRST_FRAGMENT, b'x'), ErrorCode.COMMAND_DISALLOWED_ERROR
    )
    host.cmd(data(0, Operation.COMPLETE_DATA, b'x'))


@pytest.mark.parametrize(
    'properties,field,status',
    [
        (
            Properties.SCANNABLE_ADVERTISING,
            'advertising',
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
        (0, 'scan', ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (
            Properties.USE_LEGACY_ADVERTISING_PDUS,
            'advertising-fragment',
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
        (
            Properties.USE_LEGACY_ADVERTISING_PDUS,
            'advertising-long',
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
    ],
    ids=['scannable-data', 'nonscannable-scan', 'legacy-fragment', 'legacy-long'],
)
def test_data_placement_rules(host, properties, field, status):
    """Data placement rules reject payloads incompatible with the set type.

    Purpose: Verify the data-placement checks for extended advertising: no
    advertising data on a scannable set, no scan-response data on a
    non-scannable set, and no fragments or long payloads on legacy PDUs (Vol 4,
    Part E, 7.8.54, 7.8.55).

    Steps:
        1. Configure a set with the parametrized properties.
        2. Send the matching advertising/scan/fragment/long-data command.

    Expected:
        - Each case returns INVALID_COMMAND_PARAMETERS.
    """
    host.cmd(extended_parameters(advertising_event_properties=properties))
    if field == 'advertising':
        command = data(0, Operation.COMPLETE_DATA, b'x')
    elif field == 'scan':
        command = data(0, Operation.COMPLETE_DATA, b'x', field='scan')
    elif field == 'advertising-fragment':
        command = data(0, Operation.FIRST_FRAGMENT, b'x')
    else:
        command = data(0, Operation.COMPLETE_DATA, bytes(32))
    host.cmd(command, status)


@pytest.mark.parametrize(
    'stored,properties,status',
    [
        (
            'scan',
            Properties.CONNECTABLE_ADVERTISING | Properties.DIRECTED_ADVERTISING,
            ErrorCode.SUCCESS,
        ),
        ('scan', 0, ErrorCode.SUCCESS),
        (
            'advertising',
            Properties.SCANNABLE_ADVERTISING,
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
    ],
    ids=['scan-to-directed', 'scan-to-nonscannable', 'data-to-scannable'],
)
def test_reconfigure_with_stored_payload(host, stored, properties, status):
    """Only stored advertising data can make new event properties invalid.

    Purpose: Verify LE Set Extended Advertising Parameters returns Invalid HCI
    Command Parameters when the event type does not support advertising data
    and the set already contains some, and has no such error for scan
    response data left by an earlier scannable configuration (Vol 4, Part E,
    7.8.53). Zephyr's Host re-uses a set this way after legacy scannable
    advertising.

    Steps:
        1. Configure a set that accepts the stored payload and store one octet.
        2. Re-send parameters with the parametrized properties.

    Expected:
        - Scan response data does not block the change and stays stored;
          advertising data on a now-scannable set returns
          INVALID_COMMAND_PARAMETERS.
    """
    first = Properties.SCANNABLE_ADVERTISING if stored == 'scan' else 0
    host.cmd(
        extended_parameters(
            advertising_event_properties=first,
            peer_address=PEER_ADDRESS,
        )
    )
    host.cmd(data(0, Operation.COMPLETE_DATA, b'x', field=stored))
    host.cmd(
        extended_parameters(
            advertising_event_properties=properties,
            peer_address=PEER_ADDRESS,
        ),
        status,
    )
    if stored == 'scan':
        assert host.controller.advertising_sets[0].scan_response_data == b'x'


def test_empty_fragment_is_invalid(host):
    """A first fragment with no data is invalid.

    Purpose: Verify an empty FIRST fragment is rejected (Vol 4, Part E, 7.8.54).

    Steps:
        1. Configure a set and send a FIRST fragment with empty data.

    Expected:
        - The command returns INVALID_COMMAND_PARAMETERS.
    """
    host.cmd(extended_parameters(advertising_event_properties=0))
    host.cmd(
        data(0, Operation.FIRST_FRAGMENT, b''),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


def test_unchanged_data_requires_enabled_set_with_data(host):
    """Unchanged-data operation requires an enabled set that already has data.

    Purpose: Verify the UNCHANGED_DATA operation (advance the DID without
    changing data) is valid only on an enabled set with existing data and an
    empty payload (Vol 4, Part E, 7.8.54).

    Steps:
        1. Configure a set; send UNCHANGED_DATA before any data.
        2. Store data and enable; capture the DID, send UNCHANGED_DATA, then
           send UNCHANGED_DATA with a non-empty payload.

    Expected:
        - The first UNCHANGED_DATA returns INVALID_COMMAND_PARAMETERS; once
          enabled with data it advances the DID; a non-empty payload returns
          INVALID_COMMAND_PARAMETERS.
    """
    host.cmd(extended_parameters(advertising_event_properties=0))
    host.cmd(
        data(0, Operation.UNCHANGED_DATA, b''),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    host.cmd(data(0, Operation.COMPLETE_DATA, b'd'))
    host.cmd(hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0], [0], [0]))
    did = host.link.advertisers[0].did
    host.cmd(data(0, Operation.UNCHANGED_DATA, b''))
    assert host.link.advertisers[0].did != did
    host.cmd(
        data(0, Operation.UNCHANGED_DATA, b'x'),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


def test_periodic_unchanged_data_renews_only_the_periodic_did(host):
    """Periodic UNCHANGED_DATA follows Vol 4, Part E, 7.8.62.

    Purpose: Verify that Operation 0x04 needs enabled periodic advertising, stored
    periodic data and an empty payload, and that it renews only the periodic DID,
    keeping the data and the advertising set's DID (Vol 4, Part E, 7.8.62;
    Vol 6, Part B, 4.4.2.11).

    Steps:
        1. Configure a periodic set with data; send 0x04 while periodic
           advertising is disabled.
        2. Enable periodic advertising; send 0x04 with data, then without data.
        3. Clear the data with Operation 0x03 and an empty payload; send 0x04.

    Expected:
        - Step 1 and the 0x04 command with data return INVALID_COMMAND_PARAMETERS.
        - The empty 0x04 command succeeds, changes the periodic DID and keeps
          the data and the advertising set's DID.
        - Clearing succeeds; 0x04 without stored data returns
          INVALID_COMMAND_PARAMETERS.
    """
    host.cmd(extended_parameters(advertising_event_properties=0))
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 80, 80, 0))
    host.cmd(periodic_data(0, Operation.COMPLETE_DATA, b'periodic'))
    host.cmd(
        periodic_data(0, Operation.UNCHANGED_DATA, b''),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    host.cmd(
        periodic_data(0, Operation.UNCHANGED_DATA, b'periodic'),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    item = host.link.advertisers[0]
    did, periodic_did = item.did, item.periodic_did
    host.cmd(periodic_data(0, Operation.UNCHANGED_DATA, b''))
    assert item.periodic_did != periodic_did and item.did == did
    assert bytes(item.periodic_data) == b'periodic'
    host.cmd(periodic_data(0, Operation.COMPLETE_DATA, b''))
    host.cmd(
        periodic_data(0, Operation.UNCHANGED_DATA, b''),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


def test_advertising_and_periodic_data_have_separate_dids(host):
    """Each payload renews only its own DID.

    Purpose: Verify that the advertising set and its periodic train keep separate
    Advertising DIDs: new periodic data renews the periodic DID only, and new
    advertising data renews the set's DID only, even when the data is the same
    (Vol 6, Part B, 4.4.2.11).

    Steps:
        1. Configure a periodic set.
        2. Store the same periodic data twice.
        3. Store advertising data.

    Expected:
        - Each periodic store changes the periodic DID to a new value and keeps
          the set's DID.
        - The advertising store changes the set's DID and keeps the periodic DID.
    """
    host.cmd(extended_parameters(advertising_event_properties=0))
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 80, 80, 0))
    item = host.link.advertisers[0]
    for _ in range(2):
        did, periodic_did = item.did, item.periodic_did
        host.cmd(periodic_data(0, Operation.COMPLETE_DATA, b'same'))
        assert item.periodic_did != periodic_did and item.did == did
    periodic_did = item.periodic_did
    host.cmd(data(0, Operation.COMPLETE_DATA, b'advertising'))
    assert item.did != did and item.periodic_did == periodic_did


def test_periodic_did_is_random_and_never_repeats(host, monkeypatch):
    """The periodic DID is chosen at random and differs from the last one.

    Purpose: Verify that the first periodic configuration draws the DID from all
    4096 values, and that each renewal draws from the 4095 values other than the
    current DID, so 0 and 0xFFF are both reachable (Vol 6, Part B, 4.4.2.11).

    Steps:
        1. Make the Controller's random source return 0, 0, 0xFFE and 0xFFE.
        2. Configure a periodic set, then set its periodic parameters again.
        3. Store periodic data three times.

    Expected:
        - The first configuration draws from 4096 values and sets DID 0; the
          second configuration keeps it.
        - The three stores draw from 4095 values each and set DIDs 1, 0xFFF and
          0xFFE: a draw at or above the current DID skips over it.
    """
    draws = iter([0, 0, 0xFFE, 0xFFE])
    ranges = []

    def randrange(stop):
        ranges.append(stop)
        return next(draws)

    monkeypatch.setattr(host.link.random, 'randrange', randrange)
    host.cmd(extended_parameters(advertising_event_properties=0))
    for _ in range(2):
        host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 80, 80, 0))
    item = host.link.advertisers[0]
    assert (item.periodic_did, ranges) == (0, [4096])
    dids = []
    for _ in range(3):
        host.cmd(periodic_data(0, Operation.COMPLETE_DATA, b'same'))
        dids.append(item.periodic_did)
    assert dids == [1, 0xFFF, 0xFFE]
    assert ranges == [4096, 4095, 4095, 4095]


def test_active_data_update_that_does_not_fit_discards_data(host):
    """An active data update that cannot fit the interval is rejected.

    Purpose: Verify that updating a running advertiser with data that will not
    fit the current interval/PHY returns PACKET_TOO_LONG and clears the data,
    leaving no pending fragments (Vol 4, Part E, 7.8.54).

    Steps:
        1. Configure a set, mark it enabled with data, set Coded PHYs and a
           small interval.
        2. Send a 250-octet COMPLETE_DATA update.

    Expected:
        - The update returns PACKET_TOO_LONG; the advertiser data is empty and
          no fragments remain.
    """
    host.cmd(extended_parameters(advertising_event_properties=0))
    item = host.link.advertisers[0]
    item.enabled, item.data = True, bytearray(b'old')
    item.primary_phy = item.secondary_phy = Phy.LE_CODED
    item.interval_us = item.interval_max_us = 20_000
    host.cmd(
        data(0, Operation.COMPLETE_DATA, bytes(250)), ErrorCode.PACKET_TOO_LONG_ERROR
    )
    assert not item.data and not host.controller.le_procedures.fragments


def test_enable_widens_interval_within_host_range(host):
    """Enabling widens the interval to fit the data within the host range.

    Purpose: Verify that enabling a Coded-PHY set with large data widens the
    advertising interval, within the host's min/max, to a value that fits the
    data and is a multiple of 625 us (Vol 4, Part E, 7.8.56).

    Steps:
        1. Configure a Coded-PHY set with a min/max interval window.
        2. Store 250 octets and enable the set.

    Expected:
        - The on-air advertising time is above 20 ms and no greater than the
          chosen interval, which lies within the host range and is a 625-us
          multiple.
    """
    host.cmd(
        extended_parameters(
            advertising_event_properties=0,
            primary_advertising_phy=Phy.LE_CODED,
            secondary_advertising_phy=Phy.LE_CODED,
            primary_advertising_interval_min=32,
            primary_advertising_interval_max=160,
        )
    )
    host.cmd(data(0, Operation.COMPLETE_DATA, bytes(250)))
    host.cmd(hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0], [0], [0]))
    item = host.link.advertisers[0]
    assert 20_000 < advertising_time_us(item) <= item.interval_us <= 100_000
    assert item.interval_us % 625 == 0


@pytest.mark.parametrize('phy', list(Phy))
def test_periodic_maximum_data_fits_its_train(host, phy):
    """Maximum periodic advertising data assembles and fits its train.

    Purpose: Verify that the maximum-size periodic advertising data can be
    assembled across fragments and enabled for each secondary PHY (Vol 4,
    Part E, 7.8.61, 7.8.62).

    Steps:
        1. Configure a set with the parametrized secondary PHY and periodic
           parameters.
        2. Feed the maximum data in 250-octet fragments, then enable periodic.

    Expected:
        - The stored periodic data length equals the maximum and enabling
          succeeds for every PHY.
    """
    host.cmd(
        extended_parameters(
            advertising_event_properties=0, secondary_advertising_phy=phy
        )
    )
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 400, 400, 0))
    for offset in range(0, HCI_LE_ADV_DATA_SIZE_MAX, 250):
        operation = (
            Operation.FIRST_FRAGMENT
            if offset == 0
            else Operation.LAST_FRAGMENT
            if offset + 250 >= HCI_LE_ADV_DATA_SIZE_MAX
            else Operation.INTERMEDIATE_FRAGMENT
        )
        host.cmd(
            periodic_data(
                0, operation, bytes(min(250, HCI_LE_ADV_DATA_SIZE_MAX - offset))
            )
        )
    assert (
        len(host.controller.advertising_sets[0].periodic_advertising_data)
        == HCI_LE_ADV_DATA_SIZE_MAX
    )
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))


def test_periodic_enable_without_radio_time_is_disallowed(host):
    """Enabling periodic advertising with no room in the interval is disallowed.

    Purpose: Verify that enabling periodic advertising fails when the configured
    interval cannot fit the periodic data on the Coded PHY (Vol 4, Part E,
    7.8.61).

    Steps:
        1. Configure a Coded-PHY set and a very short periodic interval, with
           191 octets of data.
        2. Enable periodic advertising.

    Expected:
        - Enabling returns COMMAND_DISALLOWED and the set is not periodic-enabled.
    """
    host.cmd(
        extended_parameters(
            advertising_event_properties=0, secondary_advertising_phy=Phy.LE_CODED
        )
    )
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 6, 6, 0))
    host.cmd(data(0, Operation.COMPLETE_DATA, bytes(191)))
    host.cmd(
        hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )
    assert not host.link.advertisers[0].periodic_enabled


def test_periodic_data_on_running_train_respects_combined_budget(host):
    """Periodic data on a running train respects the combined time budget.

    Purpose: Verify that updating periodic data on an enabled train rejects a
    payload that exceeds the interval's time budget and leaves the train's data
    empty (Vol 4, Part E, 7.8.62).

    Steps:
        1. Configure a Coded-PHY set and short periodic interval, store small
           periodic data, enable periodic.
        2. Send a 191-octet periodic COMPLETE_DATA update.

    Expected:
        - The update returns PACKET_TOO_LONG and the periodic data is empty.
    """
    host.cmd(
        extended_parameters(
            advertising_event_properties=0, secondary_advertising_phy=Phy.LE_CODED
        )
    )
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 6, 6, 0))
    host.cmd(periodic_data(0, Operation.COMPLETE_DATA, b'old'))
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    host.cmd(
        periodic_data(0, Operation.COMPLETE_DATA, bytes(191)),
        ErrorCode.PACKET_TOO_LONG_ERROR,
    )
    assert not host.link.advertisers[0].periodic_data


def test_advertising_data_cannot_starve_running_periodic_train(host):
    """Advertising data cannot grow to starve a running periodic train.

    Purpose: Verify that adding extended advertising data is refused when it
    would leave no room for an already-running periodic train (Vol 4, Part E,
    7.8.54, 7.8.61).

    Steps:
        1. Configure a Coded-PHY set with a wide primary interval and an enabled
           periodic train.
        2. Send 200 octets of advertising COMPLETE_DATA.

    Expected:
        - The advertising data write returns COMMAND_DISALLOWED.
    """
    host.cmd(
        extended_parameters(
            advertising_event_properties=0,
            secondary_advertising_phy=Phy.LE_CODED,
            primary_advertising_interval_max=0x4000,
        )
    )
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 8, 8, 0))
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    host.cmd(
        data(0, Operation.COMPLETE_DATA, bytes(200)), ErrorCode.COMMAND_DISALLOWED_ERROR
    )


def test_periodic_enable_requires_compatible_set(host):
    """Periodic enable requires a configured, compatible, existing set.

    Purpose: Verify Set Periodic Advertising Enable needs periodic parameters
    first, forbids making a running periodic set connectable, rejects an
    unknown set and an Enable value with a reserved bit (Vol 4, Part E, 7.8.61,
    7.8.53, 7.8.63).

    Steps:
        1. Configure a set and try to enable periodic before parameters.
        2. Set periodic parameters and enable; then reconfigure as connectable,
           enable an unknown set and send Enable with reserved bit 2 set.

    Expected:
        - Enable before parameters returns COMMAND_DISALLOWED; after parameters
          it succeeds; the connectable reconfigure returns
          INVALID_COMMAND_PARAMETERS; enabling set 2 returns
          UNKNOWN_ADVERTISING_IDENTIFIER; Enable 0x04 returns
          UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE.
    """
    host.cmd(extended_parameters(advertising_event_properties=0))
    host.cmd(
        hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 80, 80, 0))
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 0))
    # Making the running periodic set connectable is invalid (7.8.53).
    host.cmd(
        extended_parameters(
            advertising_event_properties=Properties.CONNECTABLE_ADVERTISING
        ),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    host.cmd(
        hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1, 2),
        ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR,
    )
    host.cmd(
        hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0b100, 0),
        ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
    )


def test_periodic_enable_includes_adi_on_request(host):
    """Enable bit 1 selects the ADI field; disabling ignores it.

    Purpose: Verify Set Periodic Advertising Enable records whether AUX_SYNC_IND
    carries an ADI, that a later enable without bit 1 removes it from the
    running train, and that Enable 0x02 disables the train (Vol 4, Part E,
    7.8.63).

    Steps:
        1. Configure a periodic set and enable it with Enable 0x03.
        2. Re-enable the running train with Enable 0x01.
        3. Send Enable 0x02.

    Expected:
        - After step 1 the train is enabled with the ADI; after step 2 it is
          still enabled, without the ADI; step 3 succeeds and disables it.
    """
    host.cmd(extended_parameters(advertising_event_properties=0))
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 80, 80, 0))
    item = host.link.advertisers[0]
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0b11, 0))
    assert item.periodic_enabled and item.periodic_adi
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0b01, 0))
    assert item.periodic_enabled and not item.periodic_adi
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0b10, 0))
    assert not item.periodic_enabled and not item.periodic_adi


def test_periodic_enable_counts_adi_airtime(host):
    """Enabling with the ADI checks the longer train against the interval.

    Purpose: Verify that the two ADI octets in AUX_SYNC_IND and each
    AUX_CHAIN_IND count toward the periodic interval budget, so data that fits
    without the ADI can be too long with it (Vol 4, Part E, 7.8.63; Vol 6,
    Part B, 2.3.1.7, 2.3.1.8).

    Steps:
        1. Configure a periodic set with a 7.5-ms interval.
        2. Store the longest data whose train fits only without the ADI.
        3. Enable with Enable 0x03.

    Expected:
        - Enable 0x03 returns PACKET_TOO_LONG and leaves the train disabled.
    """
    host.cmd(extended_parameters(advertising_event_properties=0))
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 6, 6, 0))
    item = host.link.advertisers[0]
    budget = 6 * LL_PERIODIC_INTERVAL_UNIT_US - LL_MAFS_US

    def fits(length, adi):
        candidate = replace(item, periodic_data=bytes(length), periodic_adi=adi)
        return periodic_time_us(candidate) < budget

    length = max(n for n in range(1, 1000) if fits(n, False))
    assert not fits(length, True)
    for offset in range(0, length, 250):
        chunk = bytes(min(250, length - offset))
        operation = (
            Operation.FIRST_FRAGMENT
            if not offset
            else Operation.LAST_FRAGMENT
            if offset + 250 >= length
            else Operation.INTERMEDIATE_FRAGMENT
        )
        host.cmd(periodic_data(0, operation, chunk))
    assert len(item.periodic_data) == length
    host.cmd(
        hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(0b11, 0),
        ErrorCode.PACKET_TOO_LONG_ERROR,
    )
    assert not item.periodic_enabled


# ---------------------------------------------------------------------------
# Extended advertising enable and capacity (7.8.56-7.8.60)
# ---------------------------------------------------------------------------
def test_set_capacity_and_atomic_enable(host):
    """Advertising-set capacity is enforced and extended enable is atomic.

    Purpose: Verify the supported-set count matches capacity, configuring one
    set beyond it fails, and an extended enable naming an unknown set fails
    atomically (no set enabled) while a valid multi-set enable enables all
    named sets (Vol 4, Part E, 7.8.56, 7.8.58).

    Steps:
        1. Read the supported set count and configure that many sets, then one
           more.
        2. Enable a pair including an unknown set, then a valid pair.

    Expected:
        - The count equals the ``ControllerConfig`` default; the extra
          configure returns MEMORY_CAPACITY_EXCEEDED; the unknown-set enable
          returns UNKNOWN_ADVERTISING_IDENTIFIER with nothing advertising; the
          valid enable enables both sets.
    """
    count = host.cmd(
        hci.HCI_LE_Read_Number_Of_Supported_Advertising_Sets_Command()
    ).num_supported_advertising_sets
    assert count == ControllerConfig().advertising_set_count
    for handle in range(count):
        host.cmd(extended_parameters(advertising_handle=handle))
    host.cmd(
        extended_parameters(advertising_handle=count),
        ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR,
    )
    host.cmd(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0, 254], [0, 0], [0, 0]),
        ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR,
    )
    assert not host.controller.is_advertising
    host.cmd(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0, 1], [0, 0], [0, 0])
    )
    assert all(host.link.advertisers[handle].enabled for handle in (0, 1))


def test_enable_with_mismatched_arrays_or_empty_enable(host):
    """Extended enable rejects mismatched arrays and an empty enable list.

    Purpose: Verify Set Extended Advertising Enable rejects an empty set list
    when enabling and an enable value of 2 (Vol 4, Part E, 7.8.56).

    Steps:
        1. Configure a set.
        2. Enable with empty arrays, then with enable value 2.

    Expected:
        - Both return INVALID_COMMAND_PARAMETERS.
    """
    host.cmd(extended_parameters())
    host.cmd(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [], [], []),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    host.cmd(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(2, [0], [0], [0]),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


def test_enable_rejects_data_that_cannot_fit_interval(host):
    """Enabling rejects data too large to fit the advertising interval.

    Purpose: Verify Set Extended Advertising Enable returns PACKET_TOO_LONG when
    the stored data cannot be carried within the configured interval/PHY
    (Vol 4, Part E, 7.8.56).

    Steps:
        1. Configure a Coded-PHY set and give the live advertiser 1600 octets.
        2. Enable the set.

    Expected:
        - Enabling returns PACKET_TOO_LONG.
    """
    host.cmd(
        extended_parameters(
            advertising_event_properties=0,
            primary_advertising_phy=Phy.LE_CODED,
            secondary_advertising_phy=Phy.LE_CODED,
        )
    )
    host.link.advertisers[0].data = bytearray(1600)
    host.cmd(
        hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0], [0], [0]),
        ErrorCode.PACKET_TOO_LONG_ERROR,
    )


def test_high_duty_duration_is_bounded(host):
    """A high-duty directed advertiser's timeout is bounded.

    Purpose: Verify enabling high-duty-cycle directed connectable advertising
    sets a bounded deadline regardless of a longer requested duration (Vol 4,
    Part E, 7.8.56, 4.4.2).

    Steps:
        1. Configure a high-duty directed connectable legacy set with a peer.
        2. Enable it with a 500-unit duration.

    Expected:
        - The advertiser's deadline is capped at 1_280_000 us.
    """
    properties = (
        Properties.USE_LEGACY_ADVERTISING_PDUS
        | Properties.CONNECTABLE_ADVERTISING
        | Properties.DIRECTED_ADVERTISING
        | Properties.HIGH_DUTY_CYCLE_DIRECTED_CONNECTABLE_ADVERTISING
    )
    host.cmd(
        extended_parameters(
            advertising_event_properties=properties, peer_address=PEERS[0]
        )
    )
    host.cmd(hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0], [500], [0]))
    assert host.link.advertisers[0].deadline_us == 1_280_000


@pytest.mark.parametrize(
    'changes,status',
    [
        (
            dict(own_address_type=4),
            ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
        ),
        (
            dict(advertising_filter_policy=4),
            ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
        ),
        (
            dict(secondary_advertising_phy=4),
            ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
        ),
        (dict(advertising_tx_power=50), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR),
        (
            dict(scan_request_notification_enable=2),
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
        (
            dict(advertising_event_properties=1 << 10),
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
        (
            dict(
                advertising_event_properties=Properties.USE_LEGACY_ADVERTISING_PDUS
                | Properties.DIRECTED_ADVERTISING
            ),
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
        (
            dict(
                advertising_event_properties=Properties.USE_LEGACY_ADVERTISING_PDUS,
                primary_advertising_phy=Phy.LE_CODED,
            ),
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
        (
            dict(
                advertising_event_properties=Properties.HIGH_DUTY_CYCLE_DIRECTED_CONNECTABLE_ADVERTISING
            ),
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
        (
            dict(
                advertising_event_properties=Properties.ANONYMOUS_ADVERTISING
                | Properties.SCANNABLE_ADVERTISING
            ),
            ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
    ],
)
def test_extended_parameter_validation(host, changes, status):
    """Extended advertising parameter validation rejects invalid combinations.

    Purpose: Verify Set Extended Advertising Parameters rejects unsupported
    address/filter/PHY values and invalid property/power combinations, creating
    no set (Vol 4, Part E, 7.8.53).

    Steps:
        1. Send extended parameters with the parametrized invalid change.

    Expected:
        - The command returns the expected status and no advertising set is
          created.
    """
    host.cmd(extended_parameters(**changes), status)
    assert not host.controller.advertising_sets


def test_tx_power_selection_is_returned(host):
    """The selected Tx power is returned from Set Extended Parameters.

    Purpose: Verify Set Extended Advertising Parameters echoes the selected Tx
    power the Controller chose for the requested value (Vol 4, Part E, 7.8.53).

    Steps:
        1. Send extended parameters requesting -8 dBm.

    Expected:
        - The returned selected Tx power is -8.
    """
    result = host.cmd(extended_parameters(advertising_tx_power=-8))
    assert result.selected_tx_power == -8


# ---------------------------------------------------------------------------
# Advertising reports to the Host (7.7.65.2, 7.7.65.13)
# ---------------------------------------------------------------------------
def report(host, data=b'data', properties=LEGACY, status=DataStatus.COMPLETE, **kw):
    host.notify(
        'on_advertisement',
        PEER_ADDRESS,
        data,
        kw.pop('rssi', -45.4),
        AdvertisingReport(properties, status),
        kw.pop('sid', None),
        kw.pop('interval', 0),
        kw.pop('tx_power', None),
        **kw,
    )


def enable_scan(host, extended=False, duplicates=0):
    if extended:
        host.cmd(
            hci.HCI_LE_Set_Extended_Scan_Parameters_Command(0, 0, 1, [0], [16], [16])
        )
        host.cmd(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, duplicates, 0, 0))
    else:
        host.cmd(hci.HCI_LE_Set_Scan_Parameters_Command(0, 16, 16, 0, 0))
        host.cmd(hci.HCI_LE_Set_Scan_Enable_Command(1, duplicates))


def test_reports_are_dropped_while_not_scanning(host):
    """Advertising reports are dropped when scanning is not enabled.

    Purpose: Verify an advertising report from the LL is discarded when the Host
    is not scanning, producing no event (Vol 4, Part E, 7.7.65.2).

    Steps:
        1. Without enabling scanning, raise an advertising report.

    Expected:
        - The Host queue stays empty.
    """
    report(host)
    host.ut_rx_q_is_empty()


@pytest.mark.parametrize(
    'properties,expected',
    [
        (
            LEGACY
            | AdvertisingProperties.CONNECTABLE_ADVERTISING
            | AdvertisingProperties.SCANNABLE_ADVERTISING,
            0,
        ),
        (
            LEGACY
            | AdvertisingProperties.CONNECTABLE_ADVERTISING
            | AdvertisingProperties.DIRECTED_ADVERTISING,
            1,
        ),
        (LEGACY | AdvertisingProperties.SCANNABLE_ADVERTISING, 2),
        (LEGACY, 3),
        (
            LEGACY
            | AdvertisingProperties.SCANNABLE_ADVERTISING
            | AdvertisingProperties.SCAN_RESPONSE,
            4,
        ),
    ],
)
def test_legacy_report_event_types(host, properties, expected):
    """Legacy advertising reports encode the spec's event-type values.

    Purpose: Verify that under legacy scanning each advertising-properties
    combination maps to the correct legacy report event type and carries the
    address, data and RSSI (Vol 4, Part E, 7.7.65.2).

    Steps:
        1. Enable legacy scanning.
        2. Raise a report with the parametrized properties.

    Expected:
        - A single Advertising Report entry has the expected event type, the
          peer address, data 'data' and RSSI -45.
    """
    enable_scan(host)
    report(host, properties=properties)
    event = host.ut_rx(hci.HCI_LE_Advertising_Report_Event)
    (entry,) = event.reports
    assert (entry.event_type, entry.address, entry.data, entry.rssi) == (
        expected,
        PEER_ADDRESS,
        b'data',
        -45,
    )


def test_legacy_scan_cannot_report_extended_pdus(host):
    """Legacy scanning does not report extended advertising PDUs.

    Purpose: Verify that a report describing an extended (non-legacy) PDU is
    dropped under legacy scanning (Vol 4, Part E, 7.7.65.2).

    Steps:
        1. Enable legacy scanning.
        2. Raise a report with no legacy property bit set.

    Expected:
        - The Host queue stays empty.
    """
    enable_scan(host)
    report(host, properties=AdvertisingProperties(0))
    host.ut_rx_q_is_empty()


def test_extended_report_fields_and_fragmentation(host):
    """Extended reports carry all fields and fragment long data correctly.

    Purpose: Verify extended scanning reports a long truncated advertisement
    across fragments with the right data-status bits, SID, interval, Tx power,
    PHYs, direct address and clamped RSSI (Vol 4, Part E, 7.7.65.13).

    Steps:
        1. Enable extended scanning.
        2. Raise a 500-octet truncated report with extended fields.

    Expected:
        - Three Extended Advertising Report events reassemble the payload with
          data-status bits [1, 1, 2]; the first carries SID 5, interval 80, Tx
          power -3, Coded/2M PHYs, the direct address and clamped RSSI -127; the
          queue is then empty.
    """
    enable_scan(host, extended=True)
    payload = bytes(range(250)) * 2
    report(
        host,
        payload,
        AdvertisingProperties.CONNECTABLE_ADVERTISING,
        DataStatus.TRUNCATED,
        sid=5,
        interval=80,
        tx_power=-3,
        target=IUT_ADDRESS,
        primary_phy=Phy.LE_CODED,
        secondary_phy=Phy.LE_2M,
        rssi=-500,
    )
    events = [
        host.ut_rx(hci.HCI_LE_Extended_Advertising_Report_Event) for _ in range(3)
    ]
    entries = [event.reports[0] for event in events]
    assert b''.join(entry.data for entry in entries) == payload
    assert [(entry.event_type >> 5) & 3 for entry in entries] == [1, 1, 2]
    first = entries[0]
    assert (
        first.advertising_sid,
        first.periodic_advertising_interval,
        first.tx_power,
    ) == (5, 80, -3)
    assert (first.primary_phy, first.secondary_phy) == (Phy.LE_CODED, Phy.LE_2M)
    assert first.direct_address == IUT_ADDRESS and first.rssi == -127
    host.ut_rx_q_is_empty()


def test_extended_report_of_legacy_pdu_has_no_secondary_phy(host):
    """An extended report of a legacy PDU omits the secondary PHY and SID.

    Purpose: Verify that under extended scanning a legacy PDU is reported with
    no secondary PHY, an unavailable SID and Tx power, and empty data (Vol 4,
    Part E, 7.7.65.13).

    Steps:
        1. Enable extended scanning.
        2. Raise a legacy report with empty data and a secondary PHY hint.

    Expected:
        - The entry's secondary PHY is 0, SID is 0xFF, Tx power is 0x7F and data
          is empty.
    """
    enable_scan(host, extended=True)
    report(host, b'', LEGACY, secondary_phy=Phy.LE_2M)
    (entry,) = host.ut_rx(hci.HCI_LE_Extended_Advertising_Report_Event).reports
    assert entry.secondary_phy == 0 and entry.advertising_sid == 0xFF
    assert entry.tx_power == 0x7F and entry.data == b''


@pytest.mark.parametrize('extended', [False, True])
def test_duplicate_filter_uses_whole_logical_report(host, extended):
    """Duplicate filtering keys on the whole logical report.

    Purpose: Verify that with duplicate filtering on, two identical reports
    yield one event and a different report yields another, for legacy and
    extended scanning (Vol 4, Part E, 7.8.10, 7.8.64).

    Steps:
        1. Enable scanning with duplicate filtering.
        2. Raise reports with payloads 'same', 'same', 'other'.

    Expected:
        - Exactly two events are delivered.
    """
    enable_scan(host, extended=extended, duplicates=1)
    for payload in (b'same', b'same', b'other'):
        report(host, payload)
    assert len(host.events) == 2


def test_duplicate_history_resets_when_scanning_restarts(host):
    """Duplicate history clears when scanning is disabled and re-enabled.

    Purpose: Verify restarting scanning clears the duplicate-filter history so a
    previously seen report is reported again (Vol 4, Part E, 7.8.11).

    Steps:
        1. Enable duplicate-filtered scanning and raise a report.
        2. Disable and re-enable scanning, then raise the same report.

    Expected:
        - Two events total are delivered.
    """
    enable_scan(host, duplicates=1)
    report(host)
    host.cmd(hci.HCI_LE_Set_Scan_Enable_Command(0, 0))
    host.cmd(hci.HCI_LE_Set_Scan_Enable_Command(1, 1))
    report(host)
    assert len(host.events) == 2


def test_duplicate_history_resets_each_period_only_when_asked(host):
    """Per-period duplicate reset happens only with the reset-each-period option.

    Purpose: Verify extended scanning clears duplicate history on each scan
    period only when the duplicates value requests it (Vol 4, Part E, 7.8.64).

    Steps:
        1. Enable extended scanning with the reset-each-period duplicates value;
           report, signal a scan period, report again.
        2. Re-enable with plain duplicate filtering; report, signal a period,
           report again.

    Expected:
        - With reset-each-period two events are delivered; with plain filtering
          only one is.
    """
    host.cmd(hci.HCI_LE_Set_Extended_Scan_Parameters_Command(0, 0, 1, [0], [16], [16]))
    host.cmd(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 2, 1, 1))
    report(host)
    host.notify('on_scan_period')
    report(host)
    assert len(host.events) == 2
    host.events.clear()
    host.cmd(hci.HCI_LE_Set_Extended_Scan_Enable_Command(1, 1, 1, 1))
    report(host)
    host.notify('on_scan_period')
    report(host)
    assert len(host.events) == 1


def test_masked_report_is_not_counted_as_seen(host):
    """A masked report is not recorded in the duplicate-filter history.

    Purpose: Verify that when the advertising-report event is masked, a dropped
    report is not remembered, so it is not later filtered as a duplicate
    (Vol 4, Part E, 7.3.1, 7.8.10).

    Steps:
        1. Enable duplicate-filtered scanning, then clear the event mask.
        2. Raise a report.

    Expected:
        - The Host queue stays empty (the report was masked, not just filtered).
    """
    enable_scan(host, duplicates=1)
    host.cmd(hci.HCI_LE_Set_Event_Mask_Command(bytes(8)))
    report(host)
    host.ut_rx_q_is_empty()


def test_scan_request_received_event(host):
    """A scan request from a peer raises a Scan Request Received event.

    Purpose: Verify an LL scan-request notification for an advertising set
    produces a Scan Request Received event carrying the handle and scanner
    address (Vol 4, Part E, 7.7.65.19).

    Steps:
        1. Configure a set at handle 2 and notify a scan request from a peer.

    Expected:
        - A Scan Request Received event reports handle 2 and the scanner
          address.
    """
    host.cmd(extended_parameters(advertising_handle=2))
    item = host.link.advertisers[2]
    host.notify('on_scan_request', item, PEERS[1])
    host.ut_rx(
        hci.HCI_LE_Scan_Request_Received_Event,
        advertising_handle=2,
        scanner_address=PEERS[1],
    )


@pytest.mark.parametrize('legacy', [False, True])
def test_advertising_ended_event_format(host, legacy):
    """Advertising end is reported per family as the correct event.

    Purpose: Verify that advertising ending on timeout is reported as an
    Enhanced Connection Complete (status timeout) for a legacy directed
    advertiser and as an Advertising Set Terminated event for an extended set,
    with the completed-event count clamped (Vol 4, Part E, 7.7.65.18, 7.7.65).

    Steps:
        1. Enable the parametrized legacy or extended advertiser (extended with
           300 events).
        2. Notify advertising ended with ADVERTISING_TIMEOUT.

    Expected:
        - Legacy yields an Enhanced Connection Complete (timeout, peer); extended
          yields an Advertising Set Terminated (timeout, handle 4, completed
          count clamped to 255).
    """
    if legacy:
        host.cmd(
            hci.HCI_LE_Set_Advertising_Parameters_Command(
                0, 0, 1, 0, PEERS[0].address_type, PEERS[0], 7, 0
            )
        )
        host.cmd(hci.HCI_LE_Set_Advertising_Enable_Command(1))
        item = host.link.advertisers[-1]
    else:
        host.cmd(extended_parameters(advertising_handle=4))
        item = host.link.advertisers[4]
        item.events = 300
    host.notify('on_advertising_ended', item, ErrorCode.ADVERTISING_TIMEOUT_ERROR)
    if legacy:
        host.ut_rx(
            hci.HCI_LE_Enhanced_Connection_Complete_Event,
            status=ErrorCode.ADVERTISING_TIMEOUT_ERROR,
            peer_address=PEERS[0],
        )
    else:
        host.ut_rx(
            hci.HCI_LE_Advertising_Set_Terminated_Event,
            status=ErrorCode.ADVERTISING_TIMEOUT_ERROR,
            advertising_handle=4,
            num_completed_extended_advertising_events=255,
        )


def test_masked_advertising_completion_still_updates_records(host):
    """A masked advertising completion still updates the Controller records.

    Purpose: Verify that even with the advertising-end event masked, a periodic
    advertiser's completion stops advertising and clears the record's enabled
    flag, while the radio still transmitted (Vol 4, Part E, 7.3.1, 7.8.56).

    Steps:
        1. Clear the event mask, configure and enable a limited extended set.
        2. Advance simulation time past its duration and drain.

    Expected:
        - The Controller is no longer advertising and the set is disabled; the
          radio sent packets but no Host event was produced.
    """
    host.cmd(hci.HCI_Set_Event_Mask_Command(bytes(8)))
    host.cmd(extended_parameters(advertising_event_properties=16))
    host.cmd(hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0], [0], [1]))
    assert host.controller.is_advertising
    host.loop.run_until_complete(host.link.advance_to_us(2000))
    host._drain()
    assert (
        not host.controller.is_advertising
        and not host.controller.advertising_sets[0].enabled
    )
    assert host.radio.sent and not host.events


# ---------------------------------------------------------------------------
# Scan configuration and Filter Accept List state (7.8.10-7.8.17)
# ---------------------------------------------------------------------------
def test_scan_configuration_is_detached(host):
    """The scan-parameters view exposed over HCI is frozen and detached.

    Purpose: Verify the scan parameters HCI reads are an immutable snapshot:
    mutation fails and the live LL state is unaffected (Vol 4, Part E, 7.8.10).

    Steps:
        1. Enable scanning and take the ``scan_parameters`` view.
        2. Attempt to set its ``start_us``.

    Expected:
        - The mutation raises ``FrozenInstanceError`` and the live backoff count
          stays 1.
    """
    host.ll.enable_scanning(True)
    view = host.ll.scan_parameters
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        view.start_us = 1
    assert host.ll.scan_parameters.backoff.count == 1


def test_filter_list_locked_while_any_role_uses_it(host):
    """The Filter Accept List is locked while an advertiser uses it.

    Purpose: Verify Filter Accept List edits are refused while an advertiser
    with a filter policy is enabled (Vol 4, Part E, 7.8.16).

    Steps:
        1. Configure a filtered legacy advertiser and enable it.
        2. Try Add, Remove and Clear on the Filter Accept List.

    Expected:
        - Each edit returns COMMAND_DISALLOWED.
    """
    host.cmd(
        hci.HCI_LE_Set_Advertising_Parameters_Command(32, 32, 0, 0, 0, PEERS[0], 7, 3)
    )
    host.cmd(hci.HCI_LE_Set_Advertising_Enable_Command(1))
    for command in (
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(0, PEERS[0]),
        hci.HCI_LE_Remove_Device_From_Filter_Accept_List_Command(0, PEERS[0]),
        hci.HCI_LE_Clear_Filter_Accept_List_Command(),
    ):
        host.cmd(command, ErrorCode.COMMAND_DISALLOWED_ERROR)


def test_filter_list_locked_while_initiating_with_policy(host):
    """The Filter Accept List is locked while initiating with a filter policy.

    Purpose: Verify Filter Accept List edits are refused while a Create
    Connection using the accept-list policy is pending (Vol 4, Part E, 7.8.16).

    Steps:
        1. Start Create Connection with initiator filter policy 1.
        2. Try to add a device to the accept list.

    Expected:
        - The add returns COMMAND_DISALLOWED.
    """
    from tests.unit.helpers import create_connection

    host.cmd_status(create_connection(initiator_filter_policy=1))
    host.cmd(
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(0, PEERS[0]),
        ErrorCode.COMMAND_DISALLOWED_ERROR,
    )


def test_filter_list_copies_address_keys(host):
    """The Filter Accept List stores a copied address key.

    Purpose: Verify adding an address to the accept list copies the key so a
    later mutation of the caller's address does not change the stored entry
    (Vol 4, Part E, 7.8.16).

    Steps:
        1. Add a supplied address via the LL ``edit_filter``.
        2. Mutate the supplied address's bytes.

    Expected:
        - An equivalent fresh address is still present in the accept list.
    """
    supplied = hci.Address('C0:12:34:56:78:9A')
    expected = hci.Address(bytes(supplied), supplied.address_type)
    host.ll.edit_filter(supplied, True, 8)
    supplied.address_bytes = bytes(6)
    assert expected in host.link.accept_list


# ---------------------------------------------------------------------------
# Periodic synchronization state (7.8.67-7.8.71, 7.8.88)
# ---------------------------------------------------------------------------
Add = hci.HCI_LE_Add_Device_To_Periodic_Advertiser_List_Command
Remove = hci.HCI_LE_Remove_Device_From_Periodic_Advertiser_List_Command
Clear = hci.HCI_LE_Clear_Periodic_Advertiser_List_Command


def test_periodic_list_capacity_errors_and_reset(host):
    """The Periodic Advertiser List enforces capacity, duplicates and reset.

    Purpose: Verify the Periodic Advertiser List rejects duplicates and
    over-capacity adds, validates address type, supports remove/add, and is
    cleared by Reset (Vol 4, Part E, 7.8.70, 7.8.71, 7.8.72).

    Steps:
        1. Read the list size, add that many distinct addresses.
        2. Re-add the first (duplicate) and add one more (over capacity); remove
           one then add again; add with invalid identity and SID.
        3. Reset and try to remove an entry.

    Expected:
        - Duplicate returns INVALID_COMMAND_PARAMETERS; over-capacity returns
          MEMORY_CAPACITY_EXCEEDED; after remove the add succeeds; invalid
          identity/SID return INVALID_COMMAND_PARAMETERS; after Reset the remove
          returns UNKNOWN_ADVERTISING_IDENTIFIER.
    """
    capacity = host.cmd(
        hci.HCI_LE_Read_Periodic_Advertiser_List_Size_Command()
    ).periodic_advertiser_list_size
    addresses = [
        hci.Address((0x123456789A00 + index).to_bytes(6, 'little'), 0)
        for index in range(capacity + 1)
    ]
    for address in addresses[:-1]:
        host.cmd(Add(0, address, 0))
    host.cmd(Add(0, addresses[0], 0), ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR)
    host.cmd(Add(0, addresses[-1], 0), ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR)
    host.cmd(Remove(0, addresses[0], 0))
    host.cmd(Add(0, addresses[-1], 0))
    host.cmd(
        Add(hci.AddressType.PUBLIC_IDENTITY, PEER_ADDRESS, 0),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    host.cmd(
        Add(PEER_ADDRESS.address_type, PEER_ADDRESS, 16),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )
    host.cmd(hci.HCI_Reset_Command())
    host.cmd(
        Remove(0, addresses[-1], 0), ErrorCode.UNKNOWN_ADVERTISING_IDENTIFIER_ERROR
    )


@pytest.mark.parametrize('use_list', [False, True])
def test_periodic_list_locked_during_pending_sync(host, use_list):
    """The Periodic Advertiser List is locked while a sync creation is pending.

    Purpose: Verify Periodic Advertiser List edits are refused while a Create
    Sync is pending (whether or not it uses the list), and succeed again after
    the sync is cancelled (Vol 4, Part E, 7.8.70, 7.8.67, 7.8.68).

    Steps:
        1. Add an entry, then start Create Sync with or without the use-list
           option.
        2. Try Add, Remove and Clear; then cancel the sync and consume its
           Established event; Clear again.

    Expected:
        - Each edit during the pending sync returns COMMAND_DISALLOWED; cancel
          yields an Established event with OPERATION_CANCELLED_BY_HOST; the final
          Clear succeeds.
    """
    host.cmd(Add(PEER_ADDRESS.address_type, PEER_ADDRESS, 3))
    host.cmd_status(
        create_sync(
            options=int(Options.USE_PERIODIC_ADVERTISER_LIST) if use_list else 0
        )
    )
    for command in (
        Add(PEER_ADDRESS.address_type, PEER_ADDRESS, 4),
        Remove(PEER_ADDRESS.address_type, PEER_ADDRESS, 3),
        Clear(),
    ):
        host.cmd(command, ErrorCode.COMMAND_DISALLOWED_ERROR)
    host.cmd(hci.HCI_LE_Periodic_Advertising_Create_Sync_Cancel_Command())
    host.ut_rx(ESTABLISHED, status=ErrorCode.OPERATION_CANCELLED_BY_HOST_ERROR)
    host.cmd(Clear())


def test_sync_capacity_handles_and_reset(host):
    """Create Sync enforces capacity, reuses freed handles and resets.

    Purpose: Verify Create Sync rejects an already-synced SID and an
    over-capacity request, reuses a handle freed by Terminate Sync, reports the
    cancelled SID with handle 0, and clears all syncs on Reset (Vol 4, Part E,
    7.8.67, 7.8.69).

    Steps:
        1. Fill the sync table with established syncs.
        2. Create Sync for an existing SID then an over-capacity SID; terminate
           one and create again; cancel and inspect the event; Reset.

    Expected:
        - Existing SID returns CONNECTION_ALREADY_EXISTS; over-capacity returns
          MEMORY_CAPACITY_EXCEEDED; after terminate the new sync reuses handle
          4; cancel reports handle 0 with SID 8; after Reset no syncs remain.
    """
    # SID 8 stays free: the table holds one sync per SID 0-7, then other peers.
    for index in range(ControllerConfig().max_periodic_syncs):
        address = PEER_ADDRESS if index < 8 else PEERS[index]
        host.link.syncs[index + 1] = PeriodicSync(
            address, index % 8, 100_000, 100_000, established=True, handle=index + 1
        )
    host.cmd_status(
        create_sync(advertising_sid=3), ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR
    )
    host.cmd_status(
        create_sync(advertising_sid=8), ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR
    )
    host.cmd(hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(4))
    host.cmd_status(create_sync(advertising_sid=8))
    assert host.link.sync.handle == 4
    host.cmd(hci.HCI_LE_Periodic_Advertising_Create_Sync_Cancel_Command())
    cancelled = host.ut_rx(
        ESTABLISHED, status=ErrorCode.OPERATION_CANCELLED_BY_HOST_ERROR
    )
    assert cancelled.sync_handle == 0 and cancelled.advertising_sid == 8
    host.cmd(hci.HCI_Reset_Command())
    assert not host.link.syncs and host.link.sync is None


def test_sync_events_content(host):
    """Sync establishment, reports and loss carry the expected content.

    Purpose: Verify the periodic sync lifecycle events report correct handle,
    SID, address, PHY, interval and clock accuracy; fragmented reports with
    clamped RSSI/Tx power; and sync loss (Vol 4, Part E, 7.7.65.14-7.7.65.16).

    Steps:
        1. Notify sync established for a PeriodicSync with SyncInfo.
        2. Notify a 300-octet truncated report, then sync lost.

    Expected:
        - The Established event matches handle 3, SID 2, peer, 2M PHY, interval
          80 and SCA 5; two report events reassemble 300 octets with data-status
          1 then 2 and clamped RSSI/Tx power; a Sync Lost event reports handle 3.
    """
    from bscontroller.linklayer.advertising import SyncInfo

    sync = PeriodicSync(
        PEER_ADDRESS,
        2,
        100_000,
        0,
        handle=3,
        phy=Phy.LE_2M,
        info=SyncInfo(0, 80, (1 << 37) - 1, 1, 1, 0, sca=5),
    )
    host.notify('on_sync_established', sync, 0)
    host.ut_rx(
        ESTABLISHED,
        status=0,
        sync_handle=3,
        advertising_sid=2,
        advertiser_address=PEER_ADDRESS,
        advertiser_phy=Phy.LE_2M,
        periodic_advertising_interval=80,
        advertiser_clock_accuracy=5,
    )
    host.notify(
        'on_periodic_report', sync, bytes(300), -200.0, None, DataStatus.TRUNCATED
    )
    first = host.ut_rx(
        hci.HCI_LE_Periodic_Advertising_Report_Event,
        data_status=1,
        rssi=-127,
        tx_power=127,
    )
    last = host.ut_rx(hci.HCI_LE_Periodic_Advertising_Report_Event, data_status=2)
    assert len(first.data) + len(last.data) == 300
    host.notify('on_sync_lost', sync)
    host.ut_rx(hci.HCI_LE_Periodic_Advertising_Sync_Lost_Event, sync_handle=3)


def test_failed_sync_reports_handle_zero(host):
    """A failed sync establishment reports handle 0 and zero interval.

    Purpose: Verify that a sync establishment failure reports a Sync Established
    event with handle 0 and no interval (Vol 4, Part E, 7.7.65.14).

    Steps:
        1. Notify sync established with a failure status.

    Expected:
        - The Established event has sync handle 0 and interval 0.
    """
    sync = PeriodicSync(PEER_ADDRESS, 2, 100_000, 0, handle=3)
    host.notify(
        'on_sync_established', sync, ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED_ERROR
    )
    host.ut_rx(ESTABLISHED, sync_handle=0, periodic_advertising_interval=0)


def test_create_sync_option_and_cte_validation(host):
    """Create Sync validates the CTE type and advertiser address type.

    Purpose: Verify Create Sync rejects an unsupported sync CTE type and an
    invalid advertiser address type (Vol 4, Part E, 7.8.67).

    Steps:
        1. Create Sync with a non-zero CTE type.
        2. Create Sync with a public-identity advertiser address type.

    Expected:
        - The CTE case returns UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE; the
          address-type case returns INVALID_COMMAND_PARAMETERS.
    """
    host.cmd_status(
        create_sync(sync_cte_type=1),
        ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
    )
    host.cmd_status(
        create_sync(advertiser_address_type=hci.AddressType.PUBLIC_IDENTITY),
        ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    )


def test_receive_enable_changes_reporting(host):
    """Set Periodic Advertising Receive Enable toggles report delivery.

    Purpose: Verify Set Periodic Advertising Receive Enable turns a sync's
    report delivery off and on (Vol 4, Part E, 7.8.88).

    Steps:
        1. Install an established sync at handle 1.
        2. Disable then enable receiving for it.

    Expected:
        - The sync's ``reports_enabled`` is false after disable and true after
          enable.
    """
    host.link.syncs[1] = PeriodicSync(
        PEER_ADDRESS, 1, 100_000, 0, established=True, handle=1
    )
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command(1, 0))
    assert not host.link.syncs[1].reports_enabled
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command(1, 1))
    assert host.link.syncs[1].reports_enabled


def test_extended_advertising_record_parameters_follow_reconfigure(host):
    """An advertising-set record reflects a reconfigured SID.

    Purpose: Verify re-sending extended parameters updates the published
    record's parameters (here the advertising SID) while the live advertiser
    stays an ``Advertising`` object (Vol 4, Part E, 7.8.53).

    Steps:
        1. Set extended parameters with SID 1, then with SID 2.

    Expected:
        - The record's parameters report SID 2 and the live advertiser is an
          ``Advertising`` instance.
    """
    host.cmd(extended_parameters(advertising_sid=1))
    host.cmd(extended_parameters(advertising_sid=2))
    assert host.controller.advertising_sets[0].parameters.advertising_sid == 2
    assert isinstance(host.link.advertisers[0], Advertising)
