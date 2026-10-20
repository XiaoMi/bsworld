# Controller implementation boundaries

The responsibility mapping below uses Bluetooth Core 6.3. The specification defines protocols, interfaces, and behavior; Python filenames and the simulator runtime are implementation choices. Supported features are listed in the README.

References:

- [Vol 1, Part A, 2: Core system architecture](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core_v6.3/out/en/architecture,-change-history,-and-conventions/architecture.html): Host, Controller, resource manager, Link Controller, and PHY responsibilities.
- [Vol 1, Part F: Controller Error Codes](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core_v6.3/out/en/architecture,-change-history,-and-conventions/controller-error-codes.html).
- [Vol 4, Part E: HCI functional specification](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core_v6.3/out/en/host-controller-interface/host-controller-interface-functional-specification.html): Lower HCI, commands, events, and ACL transport.
- [Vol 6, Part A: Radio Physical Layer](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core_v6.3/out/en/low-energy-controller/radio-physical-layer-specification.html).
- [Vol 6, Part B: Link Layer](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core_v6.3/out/en/low-energy-controller/link-layer-specification.html).
- [Vol 6, Part E: Low Energy Link Layer Security](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core_v6.3/out/en/low-energy-controller/low-energy-link-layer-security.html).

HCI validates commands, translates wire units into Link Layer values, and encodes notifications as events. Link Layer owns advertising, scanning, initiation, synchronization, ACL state, packet procedures, and resource scheduling. Radio exposes transmission, reception, and physical time; its BabbleSim adapter implements the simulator's FIFO protocol. Runtime composes threads, clocks, sessions, and processes.

Link Layer does not import HCI. Shared addresses, Controller errors, and serialization support do not depend on HCI. HCI re-exports shared protocol types for packet construction. LL receive notifications carry properties and completion status separately; HCI alone packs report flags and unavailable-value encodings. Authenticated payload supervision uses microseconds internally; HCI converts its 10 ms field units at the boundary.

The implementation has no LocalLink fallback, BR/EDR/LMP execution, SMP pairing, L2CAP processing, or direct Controller-to-Controller object delivery. Generic HCI packet schemas can describe unsupported commands; the supported-command filter rejects those commands before decoding their parameters. Mock Host behavior and payload builders live in examples and tests.

All paths below are relative to `src/bscontroller/`.

| Source file | Responsibility and reference |
| --- | --- |
| `__init__.py` | Public package exports and version; packaging support. |
| `__main__.py` | Python module entry point; runtime support. |
| `cli.py` | Launch the H4 service and manage process shutdown; runtime support. |
| `address.py` | Device address identity and byte representation; Vol 6, Part B, 1.3. HCI address-type tags accompany the shared identity. |
| `errors.py` | Controller error codes from Vol 1, Part F, plus Python codec exceptions. |
| `serialization.py` | Integer enums, field metadata, and formatting shared by wire codecs; implementation support. |
| `colors.py` | Terminal log formatting; implementation support with its retained ISC notice. |
| `capabilities.py` | This implementation's resource capacities, supported PHY masks, and retained advertising data limit. These capacities are not normative maxima. |
| `crypto/__init__.py` | AES `e` used by HCI Encrypt and LL session-key derivation; Vol 4, Part E, 7.8.22 and Vol 6, Part E. No privacy generation, pairing protocol, or key database. |
| `hci/__init__.py` | Public HCI codec and shared-type exports. |
| `hci/packets.py` | HCI packet schemas, parsing, serialization, and event definitions; Vol 4, Part E, 5–7. No link execution or L2CAP reassembly. |
| `hci/transport.py` | H4 framing from Vol 4, Part A and a Unix socket service. The Unix socket and queue limits are implementation choices. |
| `hci/snoop.py` | BTSnoop v1 H4 capture with Controller-local simulation timestamps and unbuffered file writes; diagnostic support. |
| `hci/controller.py` | Lower HCI dispatch, Controller identity, command responses, and connection-handle lookup; Vol 4, Part E, 1/4/7. |
| `hci/backend.py` | Bind radio-backed LE execution and publish its supported commands/features; Vol 4, Part E, 6/7. Backend construction is an implementation interface. |
| `hci/capabilities.py` | HCI buffer capacity, supported-state encoding, unavailable RSSI, and timeout field units; Vol 4, Part E, 7.3.93/94, 7.5.4, 7.8.2/27. |
| `hci/context.py` | HCI configuration, duplicate history, command fragments, and connection to the LL notification interface; Vol 4, Part E, 7.8. |
| `hci/le.py` | Reset and legacy/extended command-family restrictions; Vol 4, Part E, 3.1.1/7.3.2/7.8.4. |
| `hci/advertising.py` | Validate/configure legacy, extended, and periodic advertising commands; Vol 4, Part E, 7.8.5–9/52–63. |
| `hci/scanning.py` | Validate/configure scanning, periodic synchronization, and lists; Vol 4, Part E, 7.8.10/11/14–18/64–71/88. |
| `hci/connection.py` | Validate/configure ACL connection procedures, encryption, PHY, data length, and timeout fields; Vol 4, Part E, 7.8 and applicable common commands. |
| `hci/events.py` | Convert LL notifications to HCI events, apply event masks and duplicate filtering, and fragment reports; Vol 4, Part E, 7.7. |
| `hci/records.py` | HCI advertising configuration and views of live LL connection/advertising state. Records do not schedule packets or assemble Host PDUs. |
| `linklayer/__init__.py` | LL PDU constant and opcode exports. |
| `linklayer/pdu.py` | Air packet fields, LL timing bounds, LLID, advertising types, and control opcodes; Vol 6, Part B, 2/4/5. No abstract peer-message fallback. |
| `linklayer/state.py` | LE roles and internal advertising receive properties/status; Vol 6, Part B, 1.1/4.4. The notification record is an implementation interface. |
| `linklayer/features.py` | LL feature bit definitions; Vol 6, Part B, 4.6. |
| `linklayer/advertising.py` | Advertising packet codecs, AuxPtr, SyncInfo, and request addresses; Vol 6, Part B, 2.3. |
| `linklayer/discovery.py` | Advertising/scanning/initiating/synchronization contexts, scan backoff, and ACAD channel-map control data; Vol 6, Part B, 2.3.4/4.4. No GAP advertising-data interpretation. |
| `linklayer/parameters.py` | Connection parameter wire data and LL validity rules; Vol 6, Part B, 2.3.3/4.5. |
| `linklayer/link.py` | Coordinate LL states, active contexts, notification delivery, and radio work; Vol 6, Part B, 1.1/4/5. |
| `linklayer/primary.py` | Primary advertising, scanning, and initiating exchanges; Vol 6, Part B, 4.4.2–4. |
| `linklayer/extended.py` | Auxiliary advertising transactions and chain collection; Vol 6, Part B, 2.3.4/4.4. |
| `linklayer/periodic.py` | Periodic advertising and synchronization events; Vol 6, Part B, 4.4.2/4.4.5. |
| `linklayer/connection.py` | ACL connection events, LLID, SN/NESN, retransmission, fragmentation, and supervision; Vol 6, Part B, 2.4.1/4.5. |
| `linklayer/control.py` | LL control procedure state, instants, collision handling, and updates; Vol 6, Part B, 5.1. |
| `linklayer/data_length.py` | Data length negotiation and transmission limits; Vol 6, Part B, 4.5.10/5.1.9. |
| `linklayer/encryption.py` | Encryption control, LE Ping, CCM, MIC, and packet counters; Vol 6, Part B, 5.1.3/5.1.8 and Part E. |
| `linklayer/bitstream.py` | CRC and Access Address generation; Vol 6, Part B, 3.1.1/2.1.2. The BabbleSim radio adapter invokes the CRC codec when framing simulator packets. |
| `linklayer/channel_selection.py` | Channel Selection Algorithm #2; Vol 6, Part B, 4.5.8.3. |
| `linklayer/timing.py` | Window widening and LL timing defaults/guards; Vol 6, Part B, 4.2/4.5. Guard sizes and preferred intervals are explicit implementation policy. |
| `linklayer/scheduler.py` | Radio reservations, overlap checks, and available slots; resource-manager responsibility from Vol 1, Part A, 2.1.2.3. Fixed priorities are implementation policy. |
| `radio/__init__.py` | Radio service type exports. |
| `radio/base.py` | PHY modes, physical packet duration, radio switching, and transmit/receive service types; Vol 6, Part A and Part B, 2. Packet duration uses shared LL framing sizes. |
| `radio/babblesim.py` | BabbleSim registration, FIFO requests/responses, RF parameters, and radio time grants. This is a simulator adapter, not another Bluetooth protocol layer. |
| `runtime/__init__.py` | Runtime process helper exports. |
| `runtime/event_loop.py` | Simulation-time event loops and PHY I/O admission; runtime policy. |
| `runtime/worker.py` | Construct and run a Controller and its H4 socket in the owning thread; retain immutable packet handoff for in-process Hosts; runtime policy. |
| `runtime/session.py` | Worker/session lifecycle and optional caller clock endpoint; runtime policy. |
| `runtime/phy.py` | Locate, launch, and stop the BabbleSim PHY executable; runtime policy. |

Unit test paths follow the source paths: `linklayer/bitstream.py` is tested in `tests/unit/linklayer/bitstream_test.py`, and `runtime/session.py` in `tests/unit/runtime/session_test.py`. Tests use fixtures and doubles from the test tree. HCI/LL procedure verdicts live in `tests/qualification/hci/` and `tests/qualification/ll/`.
