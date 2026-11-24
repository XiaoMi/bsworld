# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Layer ownership contracts: LL never imports HCI/runtime, LL owns handles and
contexts before any upper-layer publication, and HCI only sees detached views.

LL Control Procedure behaviour is specified by the scenario matrix in llcp/.
"""

import ast
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bscontroller import hci
from bscontroller.address import Address
from bscontroller.errors import ErrorCode
from bscontroller.linklayer import roles
from bscontroller.linklayer.api import (
    AdvertisingConfig,
    ConnectionSnapshot,
    PeriodicSyncConfig,
)
from bscontroller.linklayer.connection import Connection
from bscontroller.linklayer.discovery import Advertising
from bscontroller.linklayer.link import RadioLink
from bscontroller.linklayer.service import LinkLayerService
from bscontroller.linklayer.state import Role
from tests.unit.helpers import PARAMETERS, FakeRadio


@pytest.mark.parametrize(
    'module',
    [
        'api',
        'bis',
        'broadcast',
        'iso',
        'isoal',
        'iso_radio',
        'service',
        'notifications',
        'connection_event',
        'data_path',
        'procedure',
        'phy',
        'connection_parameters',
        'resource',
        'roles',
        'link',
    ],
)
def test_ll_modules_do_not_load_hci_or_runtime(module):
    """Link-Layer modules import no HCI or runtime package.

    Purpose: Verify each listed ``bscontroller.linklayer`` module can be
    imported in a clean interpreter without pulling in any ``bscontroller.hci``
    or ``bscontroller.runtime`` module, enforcing the layering contract.

    Steps:
        1. For each parametrized module, run a fresh Python process that imports
           it and then asserts no hci/runtime module is present in
           ``sys.modules``.

    Expected:
        - The subprocess exits zero (the import-isolation assertion holds) for
          every module.
    """
    # Run in a fresh interpreter: the fixture's Host codec must not conceal a
    # dependency.
    subprocess.run(
        [
            sys.executable,
            '-c',
            "import sys, importlib; "
            f"importlib.import_module('bscontroller.linklayer.{module}'); "
            "assert not any(name == prefix or name.startswith(prefix + '.') "
            "for name in sys.modules "
            "for prefix in ('bscontroller.hci', 'bscontroller.runtime'))",
        ],
        check=True,
    )


def test_ll_creates_connection_with_defaults_before_notifying_upper_layer():
    """A new connection carries the service's default PHYs when first reported.

    Purpose: Verify the Link Layer applies the service's default TX/RX PHYs to a
    connection's controls before it notifies the upper layer of the connection,
    so the first ``on_connected`` sees established defaults.

    Steps:
        1. Build a RadioLink with a listener recording the connection's TX/RX
           PHYs on ``on_connected``.
        2. Create a LinkLayerService and set default PHYs to (2, 4).
        3. Create a peripheral Connection and call ``report_created``.

    Expected:
        - The listener observed exactly one connection with PHYs (2, 4).
    """
    observed = []
    listener = SimpleNamespace(
        trace=lambda message: None,
        on_connected=lambda connection: observed.append(
            (connection.controls.phy.tx_phys, connection.controls.phy.rx_phys)
        ),
    )
    link = RadioLink(FakeRadio(), listener)
    service = LinkLayerService(link, Address.ANY, 30_000, 20_000)
    service.set_default_phys(2, 4)
    connection = Connection(link, PARAMETERS, Address.ANY, Role.PERIPHERAL, 0)
    connection.report_created()
    assert observed == [(2, 4)]


def test_ll_allocates_distinct_handles_before_hci_publication(connection, controller):
    """The LL owns and separates connection handles before HCI publishes them.

    Purpose: Verify handle ownership follows the LL resource table: a second
    connection gets a distinct handle and is registered in the link before any
    HCI publication (no Connection Complete, no HCI lookup record).

    Steps:
        1. Start from a fixture connection that has not been reported.
        2. Create a second peripheral connection on the same link.

    Expected:
        - The new handle differs from the first; the first is registered in
          ``link.connections``; the controller's HCI connection table stays
          empty.
    """
    # Handle ownership must follow the LL resource table, even before an upper
    # layer has received Connection Complete or installed its own lookup record.
    assert not connection.reported and not controller.le_connections
    other = Connection(
        connection.link,
        PARAMETERS,
        Address('F0:F1:F2:F3:F4:03'),
        Role.PERIPHERAL,
        0,
    )
    assert other.handle != connection.handle
    assert connection.link.connections[connection.handle] is connection
    assert not controller.le_connections


def test_acl_admission_counts_handles_reserved_for_bis(
    connection, controller, monkeypatch
):
    """An ACL cannot consume a handle reserved for an ISO stream.

    Steps:
        1. Limit the handle space to two, with one live ACL and one reservation.
        2. Check incoming and outgoing ACL admission, then free the reservation.

    Expected:
        - Both directions reject while the handle space is full.
        - The released handle becomes allocatable again.
    """
    link = connection.link
    peer = Address('F0:F1:F2:F3:F4:03')
    service = controller.le_procedures.ll
    monkeypatch.setattr(roles, 'LL_CONNECTION_HANDLE_MAX', 2)
    with monkeypatch.context() as reserved:
        reserved.setattr(link.roles.broadcast, 'connection_handles', lambda: {2})
        assert not link.roles.connections.can_accept(peer)
        assert service.initiating_status(peer, False) == (
            ErrorCode.CONNECTION_LIMIT_EXCEEDED_ERROR
        )

    assert link.roles.connections.can_accept(peer)
    assert link.roles.connections.allocate_handle() == 2


def test_hci_scan_configuration_is_a_detached_view(controller):
    """The HCI scan-parameter view is a frozen copy of live LL state.

    Purpose: Verify that the scan parameters HCI exposes are a detached,
    immutable snapshot: mutating the view fails and does not change the live LL
    scan parameters.

    Steps:
        1. Enable scanning via the LL service.
        2. Take the HCI ``scan_parameters`` view and attempt to set its
           ``start_us`` and nested ``backoff.count``.

    Expected:
        - Both mutations raise ``FrozenInstanceError``.
        - The live LL scan parameters keep ``start_us == 0`` and
          ``backoff.count == 1``.
    """
    commands = controller.le_procedures
    commands.ll.enable_scanning(True)
    view = commands.scan_parameters
    with pytest.raises(FrozenInstanceError):
        view.start_us = 123456
    with pytest.raises(FrozenInstanceError):
        view.backoff.count = 42
    assert commands.ll.scan_parameters.start_us == 0
    assert commands.ll.scan_parameters.backoff.count == 1


def test_ll_filter_admission_does_not_depend_on_hci_validation(controller):
    """The LL enforces its own filter-edit admission rule, not HCI's.

    Purpose: Verify the Link Layer rejects a filter edit while scanning on its
    own authority (returning COMMAND_DISALLOWED) and leaves the accept list
    unchanged, independent of any HCI-level validation.

    Steps:
        1. Configure and enable scanning on the LL service.
        2. Call ``edit_filter`` to add an address while scanning.

    Expected:
        - ``edit_filter`` returns ``COMMAND_DISALLOWED_ERROR``.
        - The link's accept list stays empty.
    """
    service = controller.le_procedures.ll
    service.configure_scanning(Address.ANY, 30_000, 20_000, False, 1)
    service.enable_scanning(True)
    assert (
        service.edit_filter(Address.ANY, True, 8) == ErrorCode.COMMAND_DISALLOWED_ERROR
    )
    assert not service.link.accept_list


def test_ll_rejects_reconfiguration_of_an_active_advertiser(controller):
    """Reconfiguring a running advertiser is refused and its state preserved.

    Purpose: Verify the LL advertising service rejects ``configure`` for a set
    that is currently enabled (returning COMMAND_DISALLOWED) and leaves the live
    advertiser object and its data untouched.

    Steps:
        1. Install an enabled advertiser with data b'original' at handle 0.
        2. Call ``configure`` with a new AdvertisingConfig for handle 0.

    Expected:
        - ``configure`` returns ``COMMAND_DISALLOWED_ERROR``.
        - The live advertiser is the same object with its original data.
    """
    service = controller.le_procedures.ll.advertising
    original = Advertising(Address.ANY, enabled=True, data=b'original')
    service.link.advertisers[0] = original
    candidate = AdvertisingConfig(Address.ANY)
    assert service.configure(0, candidate) == ErrorCode.COMMAND_DISALLOWED_ERROR
    assert service.link.advertisers[0] is original and original.data == b'original'


def test_sync_cancellation_releases_ll_state_before_notification(controller):
    """Cancelling a sync detaches its state and frees the slot for a new one.

    Purpose: Verify ``cancel_synchronizing`` returns a detached copy of the
    pending sync (not the live object), clears the link's active sync, and
    allows a new acquisition to start.

    Steps:
        1. Start synchronizing and capture the live sync.
        2. Cancel synchronizing and inspect the returned value.
        3. Start synchronizing again with a different SID.

    Expected:
        - The cancelled value has the same handle and SID but is a different
          object; ``link.sync`` becomes ``None``.
        - The second start installs a sync with the new SID.
    """
    service = controller.le_procedures.ll
    configuration = PeriodicSyncConfig(Address.ANY, 1, 100_000)
    service.start_synchronizing(configuration)
    sync = service.link.sync
    cancelled = service.cancel_synchronizing()
    assert cancelled.handle == sync.handle and cancelled.sid == sync.sid
    assert cancelled is not sync
    assert service.link.sync is None
    replacement = PeriodicSyncConfig(Address.ANY, 2, 100_000)
    service.start_synchronizing(replacement)
    assert service.link.sync.sid == replacement.sid


def test_radio_claim_restores_previous_deadline_after_failure(connection, monkeypatch):
    """A failed radio claim restores the resource's prior deadline.

    Purpose: Verify ``resource.claim`` sets the owner's radio limit as the
    deadline for the duration of the claim and restores the previous deadline
    if the claimed block raises, so a failed radio event leaves no stale limit.

    Steps:
        1. Set the resource's deadline to 123 and stub ``radio_limit`` to 77.
        2. Enter ``claim(connection)``, assert the deadline is 77, then raise
           RuntimeError('event failed') inside the block.

    Expected:
        - The RuntimeError propagates.
        - After the claim the deadline is restored to 123.
    """
    resource = connection.link.resource
    resource.deadline_us = 123
    monkeypatch.setattr(resource, 'radio_limit', lambda owner=None: 77)
    with pytest.raises(RuntimeError, match='event failed'):
        with resource.claim(connection):
            assert resource.deadline_us == 77
            raise RuntimeError('event failed')
    assert resource.deadline_us == 123


def test_ll_does_not_replace_an_existing_sync_acquisition(controller):
    """A second sync acquisition is refused while one is already active.

    Purpose: Verify ``start_synchronizing`` admits the first acquisition but
    rejects a second with COMMAND_DISALLOWED, leaving the active sync in place.

    Steps:
        1. Start synchronizing with a first configuration and capture the sync.
        2. Start synchronizing again with a different SID.

    Expected:
        - The first call returns ``SUCCESS``.
        - The second returns ``COMMAND_DISALLOWED_ERROR`` and ``link.sync`` is
          still the first acquisition.
    """
    service = controller.le_procedures.ll
    first = PeriodicSyncConfig(Address.ANY, 1, 100_000)
    replacement = PeriodicSyncConfig(Address.ANY, 2, 100_000)
    assert service.start_synchronizing(first) == ErrorCode.SUCCESS
    active = service.link.sync
    assert (
        service.start_synchronizing(replacement) == ErrorCode.COMMAND_DISALLOWED_ERROR
    )
    assert service.link.sync is active


# HCI / Link Layer service boundary (moved from hci/boundary_test.py)
def test_advertising_configuration_cannot_replace_runtime_state(controller):
    """Reconfiguring an advertising set updates config but keeps runtime state.

    Purpose: Verify that applying a new ``AdvertisingConfig`` to a set preserves
    the live runtime fields (periodic counter/AA, event count, next time) and
    stored data, that the config object is a frozen detached value, and that a
    config carrying live state is rejected.

    Steps:
        1. Configure handle 0 and mutate the live advertiser's runtime fields
           and stored data.
        2. Re-apply a config with a changed tx_power_dbm.
        3. Attempt to configure with an ``Advertising`` (live) object and to
           mutate the config's frozen address.

    Expected:
        - Both configures return SUCCESS; runtime fields and payload survive;
          the snapshot reflects the new tx power.
        - Passing live state raises ``TypeError``; mutating the frozen address
          raises ``FrozenInstanceError``.
    """
    service = controller.le_procedures.ll.advertising
    config = AdvertisingConfig(controller.public_address)
    assert service.configure(0, config) == hci.HCI_ErrorCode.SUCCESS
    live = controller.backend.link.advertisers[0]
    live.periodic_counter = 17
    live.periodic_aa = 0x12345678
    live.events = 9
    live.next_us = 5000
    service.store_data(0, 'data', b'payload')
    assert (
        service.configure(0, replace(config, tx_power_dbm=-8))
        == hci.HCI_ErrorCode.SUCCESS
    )
    updated = controller.backend.link.advertisers[0]
    assert (updated.periodic_counter, updated.periodic_aa) == (17, 0x12345678)
    assert (updated.events, updated.next_us) == (9, 5000)
    assert service.snapshot(0).data == b'payload'
    assert service.snapshot(0).tx_power_dbm == -8
    with pytest.raises(TypeError, match='must not contain live state'):
        service.configure(0, Advertising(controller.public_address))
    with pytest.raises(FrozenInstanceError):
        config.address.address_type = hci.AddressType.RANDOM_DEVICE


def test_connection_queries_are_frozen_detached_values(connection):
    """Connection snapshots are frozen views that track live identity only.

    Purpose: Verify ``connection_snapshot`` returns a frozen ``ConnectionSnapshot``
    that exposes no live internals (controls, encryption, event, link), cannot
    be mutated, and reflects later live changes on a fresh query rather than
    through the stale snapshot.

    Steps:
        1. Report the connection created and take a snapshot.
        2. Check the snapshot type and the absence of internal attributes.
        3. Attempt to mutate the snapshot's ``rssi`` and nested peer type.
        4. Change the live connection's ``rssi`` and re-query.

    Expected:
        - The snapshot is a ``ConnectionSnapshot`` lacking the internal
          attributes; both mutations raise ``FrozenInstanceError``.
        - The first snapshot's rssi stays ``None``; a new snapshot reports -42.
    """
    connection.report_created()
    service = connection.link.listener.sink.ll
    before = service.connection_snapshot(connection.handle)
    assert isinstance(before, ConnectionSnapshot)
    assert not any(
        hasattr(before, name) for name in ('controls', 'encryption', 'event', 'link')
    )
    with pytest.raises(FrozenInstanceError):
        before.rssi = -20
    with pytest.raises(FrozenInstanceError):
        before.peer.address_type = hci.AddressType.RANDOM_DEVICE
    connection.last_rssi_dbm = -42
    assert before.rssi is None
    assert service.connection_snapshot(connection.handle).rssi == -42


@pytest.mark.parametrize('operation', ['scan', 'scan_reset', 'advertising_address'])
def test_ll_configuration_copies_mutable_input_addresses(controller, operation):
    """LL configuration copies caller addresses so later mutation is isolated.

    Purpose: Verify scan, scan-reset and advertising-address configuration each
    copy the supplied mutable ``Address`` into LL state, so mutating the
    caller's object afterwards does not change the configured value.

    Steps:
        1. Supply an address to the parametrized operation (scan,
           scan_reset, or advertising address).
        2. Mutate the supplied address's bytes and type.
        3. Read back the configured address (enabling scanning to publish the
           scan one) and compare.

    Expected:
        - The configured address still equals the originally supplied bytes and
          type for every operation.
    """
    service = controller.le_procedures.ll
    supplied = Address('C0:12:34:56:78:9A')
    expected = bytes(supplied), supplied.address_type
    if operation == 'scan':
        service.configure_scanning(supplied, 30_000, 20_000, False, 0)
    elif operation == 'scan_reset':
        service.reset_configuration(supplied)
    else:
        service.advertising.configure(0, AdvertisingConfig(supplied))
        assert service.advertising.set_random_address(0, supplied) == 0
    supplied.address_bytes = bytes(6)
    supplied.address_type = hci.AddressType.PUBLIC_DEVICE
    if operation.startswith('scan'):
        actual = service.scan_parameters.address
        service.enable_scanning(True)
        assert controller.backend.link.scan.address == actual
    else:
        snapshot = service.advertising.snapshot(0)
        actual = snapshot.random_address
        assert snapshot.address == actual
    assert (bytes(actual), actual.address_type) == expected


@pytest.mark.parametrize('periodic', [False, True])
def test_filter_lists_copy_address_keys_before_the_caller_changes_them(
    controller, periodic
):
    """Filter and periodic lists store copied address keys, not live references.

    Purpose: Verify adding an address to the accept list or periodic advertiser
    list copies the key so mutating the caller's address afterwards leaves the
    stored key intact and removable.

    Steps:
        1. Add a supplied address to the parametrized list (filter or periodic).
        2. Mutate the supplied address's bytes and type.
        3. Remove the entry using an equivalent fresh address and inspect the
           list.

    Expected:
        - The stored key matches the original address; removal with the fresh
          address empties the list for both the filter and periodic cases.
    """
    service = controller.le_procedures.ll
    supplied = Address('C0:12:34:56:78:9A')
    expected = Address(bytes(supplied), supplied.address_type)
    if periodic:
        assert service.edit_periodic_list((supplied, 3), True, 8) == 0
    else:
        assert service.edit_filter(supplied, True, 8) == 0
    supplied.address_bytes = bytes(6)
    supplied.address_type = hci.AddressType.PUBLIC_DEVICE
    if periodic:
        assert service.edit_periodic_list((expected, 3), False, 8) == 0
        assert not controller.backend.link.periodic_advertiser_list
    else:
        assert expected in controller.backend.link.accept_list
        assert service.edit_filter(expected, False, 8) == 0
        assert not controller.backend.link.accept_list


def test_ll_callback_passes_a_snapshot_without_sharing_connection_state(
    connection, monkeypatch
):
    """An LL callback hands the sink a snapshot, not the live connection.

    Purpose: Verify ``on_remote_features`` delivers a frozen
    ``ConnectionSnapshot`` to the sink, so a later change to the live
    connection's features is not reflected in the already-delivered value.

    Steps:
        1. Record the value passed to the sink's ``on_remote_features``.
        2. Fire ``on_remote_features`` for the connection with status 0.
        3. Set the live connection's ``peer_features`` afterwards.

    Expected:
        - Exactly one value was observed, a ``ConnectionSnapshot`` whose
          ``peer_features`` is ``None`` (unaffected by the later change).
    """
    observed = []
    sink = connection.link.listener.sink
    monkeypatch.setattr(
        sink, 'on_remote_features', lambda value, status: observed.append(value)
    )
    connection.link.listener.on_remote_features(connection, 0)
    connection.peer_features = 0x1234
    assert len(observed) == 1
    assert isinstance(observed[0], ConnectionSnapshot)
    assert observed[0].peer_features is None


def test_masked_remote_parameter_request_uses_the_ll_service(connection, monkeypatch):
    """A masked remote-parameter request is rejected through the LL service.

    Purpose: Verify that when the LE event mask hides the Remote Connection
    Parameter Request event, HCI handles the request by calling the LL service's
    ``reject_parameters`` with the unsupported-remote-feature reason rather than
    forwarding an event to the Host.

    Steps:
        1. Report the connection and clear the controller's LE event mask.
        2. Stub ``reject_parameters`` to record its arguments.
        3. Invoke ``on_remote_parameters`` with a snapshot and parameters.

    Expected:
        - ``reject_parameters`` was called once with the connection handle and
          ``UNSUPPORTED_REMOTE_FEATURE_ERROR``.
    """
    connection.report_created()
    commands = connection.link.listener.sink
    commands.controller.le_event_mask = 0
    rejected = []
    monkeypatch.setattr(
        commands.ll,
        'reject_parameters',
        lambda handle, reason: rejected.append((handle, reason)),
    )
    commands.on_remote_parameters(
        commands.ll.connection_snapshot(connection.handle), 24, 24, 0, 100
    )
    assert rejected == [
        (connection.handle, hci.HCI_ErrorCode.UNSUPPORTED_REMOTE_FEATURE_ERROR)
    ]


def test_hci_does_not_enter_live_ll_collections_or_procedure_objects():
    """HCI source never names live LL collections or procedure objects.

    Purpose: Verify, by static analysis of the ``bscontroller.hci`` package,
    that HCI code never accesses live Link-Layer collection or procedure
    attributes (advertisers, connections, syncs, scan, controls, etc.), keeping
    HCI confined to detached views.

    Steps:
        1. Parse every ``*.py`` file in the hci package.
        2. Walk each AST and inspect every attribute access.

    Expected:
        - No attribute name matches the forbidden set in any hci source file.
    """
    import bscontroller.hci as package

    forbidden = {
        'advertisers',
        'connections',
        'syncs',
        'scan',
        'initiating',
        'sync',
        'controls',
        'encryption',
        'packet_security',
        'groups',
        'streams',
    }
    for source in Path(package.__file__).parent.glob('*.py'):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Attribute) and node.value.attr == 'll':
                    assert node.attr not in {'broadcast', 'iso'}
                # Encryption is also a decoded HCI BIG command field. It
                # carries a scalar mode, not a live LL encryption procedure.
                if (
                    node.attr == 'encryption'
                    and isinstance(node.value, ast.Name)
                    and node.value.id == 'command'
                ):
                    continue
                assert node.attr not in forbidden, (source.name, node.lineno, node.attr)
