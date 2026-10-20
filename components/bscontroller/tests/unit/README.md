# BSController unit tests

The `hci/`, `linklayer/`, `radio/`, and `runtime/` directories mirror `src/bscontroller/`. Each test file uses its implementation module's name with the `_test.py` suffix; for example, `linklayer/scheduler_test.py` exercises `src/bscontroller/linklayer/scheduler.py`. Shared fixtures and RF doubles remain in this directory's `conftest.py` and `helpers.py`. HCI and on-air procedure verdicts belong to `../qualification/hci/` and `../qualification/ll/`.

| Module tests | Invariants protected |
|---|---|
| `radio/base`, `linklayer/bitstream`, `linklayer/channel_selection`, `linklayer/parameters`, `linklayer/advertising` | Independent fixed wire/CRC/CSA vectors, strict input lengths, offset rounding, reproducible address allocation and switching boundaries. |
| `linklayer/discovery` | Scan gaps and overlapping PHY windows, backoff saturation/streaks and channel-map instants across counter wrap. |
| `linklayer/scheduler` | Half-open reservation endpoints, chained conflicts, complete nonpreemptive slots and request-relative scheduling grids. |
| `linklayer/link`, `linklayer/primary`, `linklayer/periodic` | One-radio arbitration, live identity collisions, starvation, missed anchors, acquisition/established lifetime transitions and callback-time context replacement. |
| `linklayer/extended` | Actual header-dependent capacity, PHY-aware chain spacing, identity inheritance and bounded/truncated reassembly. |
| `linklayer/connection`, `linklayer/control`, `linklayer/data_length` | Queue/ARQ ownership, empty Host fragments, exact credit return, delayed indications, retransmission snapshots, colliding negotiations and independent deadlines. |
| `linklayer/encryption` | Published cipher vectors, TX/RX nonce exhaustion, cached encrypted retries and refresh transitions that must keep user data blocked. |
| `hci/controller`, `hci/backend`, `hci/records`, `hci/le` | Decoder/backend isolation, shared handle publication, live public views and mutable advertising buffers across reconfiguration. |
| `hci/advertising`, `hci/scanning`, `hci/connection`, `hci/events` | Atomic failure/resource updates, fragment staging, per-context command routing and state completion when notifications are masked. |
| `radio/babblesim`, `runtime/session`, `runtime/event_loop`, `runtime/worker` | Fragmented FIFO I/O, wrong/late ACKs, clock admission, ready-work draining, worker-thread ownership and cleanup after cancellation or startup failure. |

`conftest.py` supplies Controller-only fixtures. `helpers.py` contains a fake RF endpoint, fixed input builders and shared context setup. These tests do not import qualification encoders, peers, registrations or verdict algorithms and need no native PHY.

Avoid constant-by-constant mirrors, encode/decode self-consistency when a fixed wire vector is available, repeated protocol scenarios and Cartesian products whose rows all exercise the same condition. Use explicit expected values for algorithm boundaries. Add no test file solely to cover a forwarding property or an otherwise fully exercised initialization wrapper.

```sh
python -m pytest tests/unit -n auto --dist worksteal -q
```

The test module is named for the implementation it exercises. HCI frame decoding belongs to `hci/controller_test.py`, CRC and Access Address vectors to `linklayer/bitstream_test.py`, CSA vectors to `linklayer/channel_selection_test.py`, and simulation sessions to `runtime/session_test.py`. Shared Host payload builders are in `../support.py`; no LocalLink fallback is exercised.
