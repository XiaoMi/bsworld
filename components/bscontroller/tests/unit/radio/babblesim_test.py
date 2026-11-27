# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""FIFO framing, PHY acknowledgements and endpoint/session cleanup."""

import asyncio
import os
import select
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bscontroller.linklayer import PDU_ADV_ACCESS_ADDRESS, PDU_HEADER_SIZE
from bscontroller.radio.babblesim import (
    BSIM_CODED_CI_S8,
    BSIM_FREQUENCY_BASE_MHZ,
    BSIM_FREQUENCY_SCALE,
    BSIM_RSSI_SCALE,
    BSIM_RX_SYNC_THRESHOLD,
    DISCONNECT,
    HEADER,
    MODULATION_LE_1M,
    NEVER,
    RX,
    RX_ADDRESS,
    RX_CONTINUE,
    RX_DONE,
    RX_END,
    RX_REQUEST,
    RX_STATUS_CRC_ERROR,
    RX_STATUS_HEADER_ERROR,
    RX_STATUS_IN_PROGRESS,
    RX_STATUS_NO_SYNC,
    RX_STATUS_OK,
    RX_V2,
    RX_V2_ADDRESS,
    RX_V2_CONTINUE,
    RX_V2_DONE,
    RX_V2_END,
    RX_V2_REQUEST,
    TIME,
    TX,
    TX_END,
    TX_REQUEST,
    TX_V2,
    TX_V2_REQUEST,
    WAIT,
    WAIT_END,
    BabbleSimRadio,
    PhyConnection,
)
from bscontroller.radio.base import (
    PHY_1M_ADDRESS_TIME_US,
    PHY_1M_BITRATE_BPS,
    PHY_1M_OCTET_TIME_US,
    PHY_ADV_CHANNEL_FREQUENCIES_MHZ,
    RADIO_SWITCH_TIME_US,
    AirPacket,
    Phy,
)
from bscontroller.radio.packet import crc24
from bscontroller.runtime.event_loop import ControllerEventLoop

requires_runner = pytest.mark.skipif(
    not hasattr(asyncio, 'Runner'), reason='Controller loops require Python 3.11'
)

requires_pipe2 = pytest.mark.skipif(
    not hasattr(os, 'pipe2'), reason='FIFO tests require os.pipe2'
)


@requires_runner
@pytest.mark.parametrize('buffered', [False, True])
def test_controller_drains_callbacks_before_wait_and_blocks_until_ack(
    endpoints, buffered
):
    """A clock wait drains ready callbacks first, then blocks until the PHY ack.

    Purpose: Verify ``BabbleSimRadio.advance_to_us`` flushes all queued
    callbacks before sending the WAIT request, does not run a callback submitted
    during the wait until the PHY acknowledges, and resumes that callback after
    ``before_phy_request``, whether or not the ack was already buffered.

    Steps:
        1. Bind the radio to a controller loop; optionally pre-buffer WAIT_END.
        2. Queue a chain of 8 nested callbacks and, mid-wait, a 'send' callback.
        3. On the PHY side, read the WAIT, assert the nested callbacks ran and
           (when not buffered) that the mid-wait callback did not, then send
           WAIT_END; await ``advance_to_us`` and ``before_phy_request``.

    Expected:
        - The WAIT target is 1000; the nested callbacks ran in order before the
          wait; the final event order is the countdown then 'ack' then 'io'.
    """
    device, phy = endpoints
    radio = BabbleSimRadio()
    radio.connection = device
    events = []
    if buffered:
        os.write(phy.write_fd, HEADER.pack(WAIT_END))

    with asyncio.Runner(loop_factory=ControllerEventLoop) as runner:
        loop = runner.get_loop()
        radio.bind_loop(loop)
        send = device.send

        async def submit(packet):
            loop.call_soon(events.append, 'io')
            await send(packet)

        device.send = submit

        def respond():
            assert select.select([phy.read_fd], [], [], 5)[0]
            assert os.read(phy.read_fd, 12) == HEADER.pack(WAIT) + TIME.pack(1000)
            assert events[:9] == list(range(8, -1, -1))
            if not buffered:
                # A callback submitted during the wait cannot execute before ACK.
                assert 'io' not in events
                for byte in HEADER.pack(WAIT_END):
                    os.write(phy.write_fd, bytes([byte]))

        def nested(depth):
            events.append(depth)
            if depth:
                loop.call_soon(nested, depth - 1)

        async def exercise():
            loop.call_soon(nested, 8)
            assert await radio.advance_to_us(1000) == 1000
            events.append('ack')
            await loop.before_phy_request()
            assert events == [*range(8, -1, -1), 'ack', 'io']
            loop.unbind_clock()

        with ThreadPoolExecutor(max_workers=1) as executor:
            response = executor.submit(respond)
            runner.run(exercise())
            response.result(timeout=5)


@requires_runner
@pytest.mark.parametrize('blocking_wait', [False, True])
def test_controller_shutdown_interrupts_pending_phy_request(endpoints, blocking_wait):
    """Interrupting the loop cancels a pending PHY wait and clears busy state.

    Purpose: Verify that ``loop.interrupt`` cancels a wait pending on the PHY,
    for both the async ``wait`` and the blocking ``wait_sync`` paths, and leaves
    the device not busy.

    Steps:
        1. Attach the controller loop to the device.
        2. On the PHY side, read the WAIT request for 1000 us then interrupt the
           loop.
        3. Run the parametrized wait and expect cancellation.

    Expected:
        - The wait raises ``CancelledError`` and ``device._busy`` is cleared.
    """
    device, phy = endpoints
    with asyncio.Runner(loop_factory=ControllerEventLoop) as runner:
        loop = runner.get_loop()
        device.controller_loop = loop

        def interrupt():
            assert select.select([phy.read_fd], [], [], 5)[0]
            assert os.read(phy.read_fd, 12) == HEADER.pack(WAIT) + TIME.pack(1000)
            loop.interrupt()

        async def exercise():
            with pytest.raises(asyncio.CancelledError):
                if blocking_wait:
                    device.wait_sync(1000, 5)
                else:
                    await device.wait(1000)
            assert not device._busy

        with ThreadPoolExecutor(max_workers=1) as executor:
            response = executor.submit(interrupt)
            runner.run(exercise())
            response.result(timeout=5)


def test_failed_blocking_wait_cannot_reuse_a_late_ack(endpoints):
    """A timed-out blocking wait disconnects and cannot reuse a late ack.

    Purpose: Verify that when ``wait_sync`` times out, the device tears down its
    FIFOs, sends a DISCONNECT, and rejects a further wait as not connected, so a
    late PHY ack is never consumed by a stale request.

    Steps:
        1. Call ``wait_sync(1000, 0)`` and expect a timeout.
        2. Inspect the device file descriptors and busy flag.
        3. Call ``wait_sync(0, 5)`` again and read what the PHY received.

    Expected:
        - The first wait raises TimeoutError; the fds are None and not busy; the
          second wait raises RuntimeError 'not connected'; the PHY received the
          WAIT request followed by a DISCONNECT.
    """
    device, phy = endpoints
    with pytest.raises(TimeoutError, match='waiting for BabbleSim PHY'):
        device.wait_sync(1000, 0)
    assert device.read_fd is None and device.write_fd is None and not device._busy
    with pytest.raises(RuntimeError, match='not connected'):
        device.wait_sync(0, 5)
    assert os.read(phy.read_fd, 64) == (
        HEADER.pack(WAIT) + TIME.pack(1000) + HEADER.pack(DISCONNECT)
    )


@requires_runner
def test_controller_timer_shortens_wait_on_same_radio_endpoint(endpoints):
    """A controller timer shortens the WAIT target, then the full wait resumes.

    Purpose: Verify a pending timer bounds the next ``advance_to_us`` so the PHY
    is asked to wait only to the timer's time; after the timer fires a second
    advance reaches the originally requested time.

    Steps:
        1. Bind the radio and schedule a 2 ms timer recording ``now_us``.
        2. Advance toward 5000 us twice, acking each WAIT on the PHY side and
           recording the targets.

    Expected:
        - The first advance returns ~2000 (the timer) and the timer fired then;
          the second returns 5000; the recorded WAIT targets are [~2000, 5000].
    """
    device, phy = endpoints
    radio = BabbleSimRadio()
    radio.connection = device
    fired = []
    targets = []
    with asyncio.Runner(loop_factory=ControllerEventLoop) as runner:
        loop = runner.get_loop()
        radio.bind_loop(loop)

        def respond():
            for _ in range(2):
                assert select.select([phy.read_fd], [], [], 5)[0]
                request = os.read(phy.read_fd, 12)
                assert request[:4] == HEADER.pack(WAIT)
                targets.append(TIME.unpack(request[4:])[0])
                os.write(phy.write_fd, HEADER.pack(WAIT_END))

        async def exercise():
            loop.call_later(0.002, lambda: fired.append(radio.now_us))
            assert await radio.advance_to_us(5000) == pytest.approx(2000, abs=1)
            assert await radio.advance_to_us(5000) == 5000
            assert fired == [pytest.approx(2000, abs=1)]
            assert targets == [pytest.approx(2000, abs=1), 5000]
            loop.unbind_clock()

        with ThreadPoolExecutor(max_workers=1) as executor:
            response = executor.submit(respond)
            runner.run(exercise())
            response.result(timeout=5)


@requires_runner
def test_controller_timer_precedes_scheduled_tx_without_changing_rf_time(endpoints):
    """A timer fires before a future scheduled TX without shifting RF time.

    Purpose: Verify that when a transmit is scheduled in the future, a timer due
    earlier runs first (via intermediate WAITs) while the actual transmit still
    occurs at its scheduled RF time.

    Steps:
        1. Bind the radio and schedule a 2 ms timer recording ``now_us``.
        2. Transmit a PDU scheduled at 3000 us; on the PHY side service WAITs
           (target <= 2001) then the TX_REQUEST, asserting the timer fired and
           the packet/CRC are correct, and ack TX_END at 3079.

    Expected:
        - The transmit completes at 3080 with ``radio.now_us == 3079``; the
          timer fired at ~2000 before the TX request arrived.
    """
    device, phy = endpoints
    radio = BabbleSimRadio()
    radio.connection = device
    fired = []
    with asyncio.Runner(loop_factory=ControllerEventLoop) as runner:
        loop = runner.get_loop()
        radio.bind_loop(loop)

        def respond():
            while True:
                assert select.select([phy.read_fd], [], [], 5)[0]
                header = os.read(phy.read_fd, HEADER.size)
                if header == HEADER.pack(WAIT):
                    target = TIME.unpack(os.read(phy.read_fd, TIME.size))[0]
                    assert target <= 2001
                    os.write(phy.write_fd, HEADER.pack(WAIT_END))
                else:
                    assert header == HEADER.pack(TX_REQUEST)
                    request = TX.unpack(os.read(phy.read_fd, TX.size))
                    assert request[:PDU_HEADER_SIZE] == (3000, 3079)
                    assert fired == [pytest.approx(2000, abs=1)]
                    assert os.read(phy.read_fd, 5) == b'\x02\x00' + crc24(b'\x02\x00')
                    os.write(phy.write_fd, HEADER.pack(TX_END) + TIME.pack(3079))
                    return

        async def exercise():
            loop.call_later(0.002, lambda: fired.append(radio.now_us))
            assert await radio.transmit(37, b'\x02\x00', start_us=3000) == 3080
            assert radio.now_us == 3079
            loop.unbind_clock()

        with ThreadPoolExecutor(max_workers=1) as executor:
            response = executor.submit(respond)
            runner.run(exercise())
            response.result(timeout=5)


@requires_runner
def test_controller_timer_cannot_interrupt_active_radio_reception(endpoints):
    """A timer cannot preempt an in-flight reception; it fires after RX ends.

    Purpose: Verify a timer due during an active ``receive`` does not interrupt
    the reception: it fires only after RX completes and the next gate runs.

    Steps:
        1. Bind the radio and schedule a 50 us timer recording ``now_us``.
        2. Start ``receive`` to 1000 us; on the PHY side service the RX_REQUEST
           (asserting the timer has not fired) and end RX at 1000.
        3. After receive returns, run ``before_phy_request``.

    Expected:
        - The timer had not fired during RX; receive returns ``None``; the timer
          fires at 1000 after the gate runs.
    """
    device, phy = endpoints
    radio = BabbleSimRadio()
    radio.connection = device
    fired = []
    with asyncio.Runner(loop_factory=ControllerEventLoop) as runner:
        loop = runner.get_loop()
        radio.bind_loop(loop)

        def respond():
            assert select.select([phy.read_fd], [], [], 5)[0]
            assert os.read(phy.read_fd, HEADER.size) == HEADER.pack(RX_REQUEST)
            assert RX.unpack(os.read(phy.read_fd, RX.size))[:2] == (1, 999)
            assert fired == []
            os.write(
                phy.write_fd, HEADER.pack(RX_END) + RX_DONE.pack(4, 0, 1000, 1000, 0)
            )

        async def exercise():
            loop.call_later(0.00005, lambda: fired.append(radio.now_us))
            assert await radio.receive(37, 1000) is None
            assert fired == []
            await loop.before_phy_request()
            assert fired == [1000]
            loop.unbind_clock()

        with ThreadPoolExecutor(max_workers=1) as executor:
            response = executor.submit(respond)
            runner.run(exercise())
            response.result(timeout=5)


@pytest.mark.asyncio
async def test_wait_packet_and_fragmented_ack(endpoints):
    """An async wait sends one WAIT and accepts a byte-fragmented ack.

    Purpose: Verify ``device.wait`` sends a single WAIT for the target and
    completes when the WAIT_END ack arrives one byte at a time.

    Steps:
        1. On the PHY side, receive the WAIT for 1234, then send WAIT_END byte
           by byte.
        2. Await ``device.wait(1234)`` concurrently.

    Expected:
        - The wait completes once the fragmented ack is fully received.
    """
    device, phy = endpoints

    async def respond():
        assert await phy.receive(12) == HEADER.pack(WAIT) + TIME.pack(1234)
        for byte in HEADER.pack(WAIT_END):
            await phy.send(bytes([byte]))
            await asyncio.sleep(0)

    await asyncio.gather(device.wait(1234), respond())


@pytest.mark.asyncio
@pytest.mark.parametrize('now_us', [0, 1234])
async def test_synchronize_waits_for_ack_without_advancing_time(endpoints, now_us):
    """synchronize sends a WAIT for the current time and does not advance it.

    Purpose: Verify ``BabbleSimRadio.synchronize`` barriers at the current
    simulation time: it sends WAIT for ``now_us`` and leaves ``now_us``
    unchanged after the ack, for both zero and non-zero start times.

    Steps:
        1. Set the radio's ``now_us`` to the parametrized value.
        2. On the PHY side, receive the WAIT for ``now_us``, assert the time is
           unchanged, then send WAIT_END fragmented.
        3. Await ``synchronize`` concurrently.

    Expected:
        - The WAIT target equals ``now_us`` and ``radio.now_us`` is unchanged
          afterward.
    """
    device, phy = endpoints
    radio = BabbleSimRadio()
    radio.connection = device
    radio.now_us = now_us

    async def respond():
        assert await phy.receive(12) == HEADER.pack(WAIT) + TIME.pack(now_us)
        assert radio.now_us == now_us
        for byte in HEADER.pack(WAIT_END):
            await phy.send(bytes([byte]))
            await asyncio.sleep(0)

    await asyncio.gather(radio.synchronize(), respond())
    assert radio.now_us == now_us


@pytest.mark.asyncio
async def test_blocking_clock_handles_fragmented_ack(endpoints):
    """The blocking clock wait also reassembles a fragmented ack.

    Purpose: Verify ``device.wait_sync`` (run in a thread) completes when
    WAIT_END arrives one byte at a time.

    Steps:
        1. On the PHY side, receive the WAIT for 1234 and send WAIT_END
           fragmented.
        2. Run ``wait_sync(1234, 5)`` in a thread concurrently.

    Expected:
        - The blocking wait completes once the fragmented ack is received.
    """
    device, phy = endpoints

    async def respond():
        assert await phy.receive(12) == HEADER.pack(WAIT) + TIME.pack(1234)
        for byte in HEADER.pack(WAIT_END):
            await phy.send(bytes([byte]))
            await asyncio.sleep(0)

    await asyncio.gather(asyncio.to_thread(device.wait_sync, 1234, 5), respond())


def test_blocking_clock_has_wall_watchdog_and_releases_request_state(endpoints):
    """The blocking clock wait times out on wall time and clears busy state.

    Purpose: Verify ``wait_sync`` enforces a wall-clock watchdog, raising
    TimeoutError when no ack arrives in time and leaving the device not busy.

    Steps:
        1. Call ``wait_sync(1234, 0.01)`` with no responder.

    Expected:
        - It raises TimeoutError mentioning 'waiting for BabbleSim' and
          ``device._busy`` is cleared.
    """
    device, _ = endpoints
    with pytest.raises(TimeoutError, match='waiting for BabbleSim'):
        device.wait_sync(1234, 0.01)
    assert not device._busy


@pytest.mark.parametrize('header', [DISCONNECT, 0x1234])
def test_blocking_clock_rejects_disconnect_or_invalid_ack(endpoints, header):
    """The blocking clock wait rejects a DISCONNECT or unknown ack.

    Purpose: Verify ``wait_sync`` raises when the PHY responds with a DISCONNECT
    or an unrecognized header instead of WAIT_END.

    Steps:
        1. Write the parametrized header (DISCONNECT or 0x1234) to the response
           FIFO.
        2. Call ``wait_sync(1234, 5)``.

    Expected:
        - ``RuntimeError`` is raised for both headers.
    """
    device, phy = endpoints
    os.write(phy.write_fd, HEADER.pack(header))
    with pytest.raises(RuntimeError):
        device.wait_sync(1234, 5)


@requires_pipe2
def test_blocking_clock_detects_eof(tmp_path):
    """The blocking clock wait detects PHY EOF as a disconnection.

    Purpose: Verify ``wait_sync`` raises a disconnection error when the PHY
    response pipe reaches EOF (its write end is closed).

    Steps:
        1. Build a PhyConnection over nonblocking pipes and close the response
           write end to force EOF.
        2. Call ``wait_sync(1234, 5)``.

    Expected:
        - ``RuntimeError`` mentioning 'disconnected' is raised.
    """
    request_read, request_write = os.pipe2(os.O_NONBLOCK)
    response_read, response_write = os.pipe2(os.O_NONBLOCK)
    device = PhyConnection(tmp_path, 0)
    device.write_fd, device.read_fd = request_write, response_read
    os.close(response_write)
    try:
        with pytest.raises(RuntimeError, match='disconnected'):
            device.wait_sync(1234, 5)
    finally:
        for fd in (request_read, request_write, response_read):
            os.close(fd)


@pytest.mark.asyncio
async def test_tx_packet_layout_crc_and_completion_time(endpoints):
    """A transmit sends the exact TX request layout, CRC and completion time.

    Purpose: Verify ``BabbleSimRadio.transmit`` builds the TX request with the
    correct timing, Access Address, modulation, frequency and length, appends
    the computed CRC, and captures the frame once TX_END confirms transmission.

    Steps:
        1. Transmit a 2-octet PDU on channel 37.
        2. On the PHY side, receive the TX_REQUEST header and fields, assert
           each field, receive the packet (PDU + CRC) and send TX_END at 80.

    Expected:
        - The TX fields match the expected tuple; the received bytes equal
          PDU+CRC; ``radio.now_us`` is 80 and the returned end time is 81.
        - The capture has the on-air bytes and the start time of 1 µs.
    """
    device, phy = endpoints
    radio = BabbleSimRadio()
    radio.connection = device
    capture = []
    radio.on_air_packet = capture.append
    pdu = b'\x02\x00'
    packet = pdu + crc24(pdu)

    async def respond():
        assert await phy.receive(HEADER.size) == HEADER.pack(TX_REQUEST)
        request = TX.unpack(await phy.receive(TX.size))
        assert request == (
            1,
            80,
            NEVER,
            NEVER,
            PDU_ADV_ACCESS_ADDRESS,
            MODULATION_LE_1M,
            (PHY_ADV_CHANNEL_FREQUENCIES_MHZ[37] - BSIM_FREQUENCY_BASE_MHZ)
            * BSIM_FREQUENCY_SCALE,
            0,
            len(packet),
        )
        assert await phy.receive(len(packet)) == packet
        assert not capture
        await phy.send(HEADER.pack(TX_END) + TIME.pack(80))

    end, _ = await asyncio.gather(radio.transmit(37, pdu), respond())
    assert radio.now_us == 80 and end == 81
    assert len(capture) == 1
    assert capture[0].start_us == 1
    assert capture[0].channel == 37
    assert capture[0].access_address == PDU_ADV_ACCESS_ADDRESS
    assert capture[0].phy == Phy.LE_1M
    assert capture[0].packet == packet and capture[0].crc_valid


@pytest.mark.asyncio
async def test_coded_tx_filter_is_local_and_precedes_crc_and_capture(endpoints):
    """Only the selected radio changes its PDU, once before Coded PHY segmentation.

    Steps:
        Send the same PDU from two radios. Install a filter on the first one.
        Read the Coding Indicator and the payload of each PHY transmission.

    Expected:
        Both Coding Indicators select S=8. Each FEC2 payload and its capture
        carry the correct bytes and CRC for that radio.
    """
    endpoint, peer = endpoints
    filtered, normal = BabbleSimRadio(), BabbleSimRadio()
    original, changed = bytes((1, 1, 0)), bytes((1, 1, 1))
    calls = []

    def change(pdu):
        calls.append(pdu)
        return changed

    filtered.tx_filter = change
    for radio, expected in ((filtered, changed), (normal, original)):
        radio.connection = endpoint
        capture = []
        radio.on_air_packet = capture.append
        packet = expected + crc24(expected)

        async def acknowledge():
            for payload in (BSIM_CODED_CI_S8, packet):
                assert await peer.receive(HEADER.size) == HEADER.pack(TX_V2_REQUEST)
                fields = TX_V2.unpack(await peer.receive(TX_V2.size))
                assert await peer.receive(fields[-1]) == payload
                await peer.send(HEADER.pack(TX_END) + TIME.pack(fields[1]))

        await asyncio.gather(
            radio.transmit(37, original, phy=Phy.LE_CODED), acknowledge()
        )
        assert [record.packet for record in capture] == [packet]
    assert calls == [original]


@pytest.mark.asyncio
async def test_tx_switching_rejects_early_start_before_submitting_phy(endpoints):
    """A transmit starting before the radio is ready is rejected locally.

    Purpose: Verify ``transmit`` rejects a scheduled start earlier than the
    switch gap allows before any request reaches the PHY, and leaves the switch
    state unchanged.

    Steps:
        1. Complete a 1M transmit on channel 37 ending at 100 and set now to 99.
        2. Transmit on channel 38 with a start one microsecond before the
           earliest allowed.

    Expected:
        - ``ValueError`` 'before the radio is ready' is raised; nothing was
          written to the PHY; the switch state is still (37, LE_1M, True).
    """
    device, phy = endpoints
    radio = BabbleSimRadio()
    radio.connection = device
    radio.now_us = 99
    radio.switch.complete(37, Phy.LE_1M, transmit=True, end_us=100)
    with pytest.raises(ValueError, match='before the radio is ready'):
        await radio.transmit(38, b'\x02\x00', start_us=100 + RADIO_SWITCH_TIME_US - 1)
    assert not select.select([phy.read_fd], [], [], 0)[0]
    assert radio.switch.state == (37, Phy.LE_1M, True)


@pytest.mark.asyncio
@pytest.mark.parametrize('start_us', [None, 100])
@pytest.mark.parametrize('switch_time_us', [0, RADIO_SWITCH_TIME_US])
async def test_rx_switching_prevents_reception_before_radio_is_ready(
    endpoints, start_us, switch_time_us
):
    """A receive window starts no earlier than the switch gap allows.

    Purpose: Verify ``receive`` pushes the RX window start to the earliest time
    the radio is ready after a prior transmit, for both zero and nonzero switch
    times and whether or not an explicit start is given.

    Steps:
        1. Complete a 1M transmit on channel 37 ending at 100 and set now to 99.
        2. Receive on channel 37 until 1000 with the parametrized start.
        3. On the PHY side, assert the RX request start equals ``100 +
           switch_time_us`` and end NO_SYNC.

    Expected:
        - The RX request begins at the ready time; receive returns ``None``.
    """
    device, phy = endpoints
    radio = BabbleSimRadio(switch_time_us=switch_time_us)
    radio.connection = device
    radio.now_us = 99
    radio.switch.complete(37, Phy.LE_1M, transmit=True, end_us=100)
    ready_us = 100 + switch_time_us

    async def respond():
        assert await phy.receive(HEADER.size) == HEADER.pack(RX_REQUEST)
        request = RX.unpack(await phy.receive(RX.size))
        assert request[:2] == (ready_us, 1000 - ready_us)
        await phy.send(
            HEADER.pack(RX_END) + RX_DONE.pack(RX_STATUS_NO_SYNC, 0, 1000, 1000, 0)
        )

    result, _ = await asyncio.gather(
        radio.receive(37, 1000, start_us=start_us), respond()
    )
    assert result is None


@pytest.mark.asyncio
async def test_zero_switch_time_submits_back_to_back_tx_on_different_channels(
    endpoints,
):
    """Zero switch time lets two transmits run back to back across channels.

    Purpose: Verify that with a zero switch time two consecutive transmits on
    different channels are scheduled contiguously, the second starting right
    where the first ended.

    Steps:
        1. Transmit the same PDU on channel 37 then channel 38.
        2. On the PHY side, assert each TX starts at 1 and at ``1 + duration``
           with the right packet, and ack each TX_END.

    Expected:
        - The final completion time is ``1 + 2 * duration``.
    """
    device, phy = endpoints
    radio = BabbleSimRadio(switch_time_us=0)
    radio.connection = device
    pdu = b'\x02\x00'
    duration = Phy.LE_1M.airtime_us(len(pdu))

    async def respond():
        for start in (1, 1 + duration):
            assert await phy.receive(HEADER.size) == HEADER.pack(TX_REQUEST)
            request = TX.unpack(await phy.receive(TX.size))
            assert request[:2] == (start, start + duration - 1)
            assert await phy.receive(request[-1]) == pdu + crc24(pdu)
            await phy.send(HEADER.pack(TX_END) + TIME.pack(request[1]))

    async def transmit():
        await radio.transmit(37, pdu)
        return await radio.transmit(38, pdu)

    end, _ = await asyncio.gather(transmit(), respond())
    assert end == 1 + 2 * duration


@pytest.mark.asyncio
async def test_rx_window_inside_switching_interval_submits_only_wait(endpoints):
    """A receive window wholly inside the switch gap submits only a WAIT.

    Purpose: Verify that if a requested RX window ends before the radio is ready
    to receive, ``receive`` submits a WAIT to the window end instead of an RX
    request and leaves the switch state unchanged.

    Steps:
        1. Complete a 1M transmit on channel 37 ending at 100 and set now to 99.
        2. Receive on channel 37 until ``100 + RADIO_SWITCH_TIME_US``.
        3. On the PHY side, assert a WAIT to that time is sent and ack it.

    Expected:
        - Receive returns ``None`` and the switch state is still
          (37, LE_1M, True).
    """
    device, phy = endpoints
    radio = BabbleSimRadio()
    radio.connection = device
    radio.now_us = 99
    radio.switch.complete(37, Phy.LE_1M, transmit=True, end_us=100)
    until = 100 + RADIO_SWITCH_TIME_US

    async def respond():
        assert await phy.receive(HEADER.size) == HEADER.pack(WAIT)
        assert TIME.unpack(await phy.receive(TIME.size))[0] == until
        await phy.send(HEADER.pack(WAIT_END))

    result, _ = await asyncio.gather(radio.receive(37, until), respond())
    assert result is None
    assert radio.switch.state == (37, Phy.LE_1M, True)


@pytest.mark.asyncio
@pytest.mark.parametrize('search_until_us', [140, 1000])
@pytest.mark.parametrize(
    'status,accept_bad_crc,received',
    [
        (RX_STATUS_OK, False, True),
        (RX_STATUS_CRC_ERROR, False, False),
        (RX_STATUS_CRC_ERROR, True, True),
    ],
)
async def test_rx_address_handshake_and_crc(
    endpoints, status, accept_bad_crc, received, search_until_us
):
    """A reception runs the address handshake and honors the CRC accept policy.

    Purpose: Verify ``receive`` submits the full RX request, drives the
    RX_ADDRESS/RX_CONTINUE handshake, and returns the packet with its RSSI and
    timing only when the CRC is good or bad CRC is accepted, across search
    windows and status/accept combinations.

    Steps:
        1. Start ``receive`` with the parametrized ``search_until_us`` and
           ``accept_bad_crc``.
        2. On the PHY side, assert the RX request fields, send RX_ADDRESS, the
           in-progress acquisition and the packet fragments, then RX_END.

    Expected:
        - ``radio.now_us`` ends at 179; the result is present iff ``received``;
          when present its PDU, RSSI (-45), start (100) and end (180) match and
          ``valid_crc`` reflects whether the status was OK.
        - The capture retains all bytes and the CRC status even when the
          Controller rejects the reception.
    """
    device, phy = endpoints
    radio = BabbleSimRadio()
    radio.connection = device
    capture = []
    radio.on_air_packet = capture.append
    pdu = b'\x02\x00'
    packet = pdu + crc24(pdu)
    done = RX_DONE.pack(status, len(packet), 139, 179, -45 * BSIM_RSSI_SCALE)
    acquired = RX_DONE.pack(
        RX_STATUS_IN_PROGRESS, len(packet), 139, 139, -45 * BSIM_RSSI_SCALE
    )

    async def respond():
        assert await phy.receive(HEADER.size) == HEADER.pack(RX_REQUEST)
        request = RX.unpack(await phy.receive(RX.size))
        assert request == (
            1,
            search_until_us - 1,
            PDU_ADV_ACCESS_ADDRESS,
            MODULATION_LE_1M,
            (PHY_ADV_CHANNEL_FREQUENCIES_MHZ[37] - BSIM_FREQUENCY_BASE_MHZ)
            * BSIM_FREQUENCY_SCALE,
            0,
            BSIM_RX_SYNC_THRESHOLD,
            0,
            PHY_1M_ADDRESS_TIME_US,
            PDU_HEADER_SIZE * PHY_1M_OCTET_TIME_US,
            PHY_1M_BITRATE_BPS,
            NEVER,
            NEVER,
        )
        for chunk in (
            HEADER.pack(RX_ADDRESS),
            acquired[:7],
            acquired[7:],
            packet[:1],
            packet[1:],
        ):
            await phy.send(chunk)
            await asyncio.sleep(0)
        assert await phy.receive(HEADER.size) == HEADER.pack(RX_CONTINUE)
        assert radio.now_us == 139
        await phy.send(HEADER.pack(RX_END) + done)

    result, _ = await asyncio.gather(
        radio.receive(37, search_until_us, accept_bad_crc=accept_bad_crc), respond()
    )
    assert radio.now_us == 179
    assert capture == [
        AirPacket(
            100,
            37,
            PDU_ADV_ACCESS_ADDRESS,
            Phy.LE_1M,
            0,
            packet,
            rssi=-45,
            crc_valid=status == RX_STATUS_OK,
        )
    ]
    assert (result is not None) == received
    if result is not None:
        assert (result.pdu, result.rssi, result.start_us, result.end_us) == (
            pdu,
            -45,
            100,
            180,
        )
        assert result.valid_crc == (status == RX_STATUS_OK)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'status',
    [
        RX_STATUS_CRC_ERROR,
        RX_STATUS_HEADER_ERROR,
        RX_STATUS_NO_SYNC,
    ],
)
async def test_rx_failure_has_no_packet_payload(endpoints, status):
    """A failed reception returns no packet and advances to the window end.

    Purpose: Verify ``receive`` returns ``None`` for each failure status (CRC
    error, header error, no sync) and advances the clock to the window end.

    Steps:
        1. Start ``receive`` to 1000 us.
        2. On the PHY side, consume the RX request and send RX_END with the
           parametrized failure status.

    Expected:
        - The result is ``None`` and ``radio.now_us`` is 1000.
    """
    device, phy = endpoints
    radio = BabbleSimRadio()
    radio.connection = device

    async def respond():
        await phy.receive(HEADER.size + RX.size)
        await phy.send(HEADER.pack(RX_END) + RX_DONE.pack(status, 0, 1000, 1000, 0))

    result, _ = await asyncio.gather(radio.receive(37, 1000), respond())
    assert result is None and radio.now_us == 1000


@pytest.mark.asyncio
async def test_coded_rx_keeps_reservation_deadline_through_fec2(endpoints):
    """An acquired Coded packet must yield the radio at the next reservation.

    Steps:
        Accept the Coding Indicator and start receiving the payload. Return a
        CRC error at the reservation deadline with candidate bytes available.

    Expected:
        Both PHY requests and both continuations retain the same deadline.
        The truncated packet is discarded even with bad-CRC reception enabled.
    """
    device, peer = endpoints
    radio = BabbleSimRadio()
    radio.connection = device
    deadline = 600
    pdu = bytes((2, 0))

    async def respond():
        for data, start, sync, end, status in (
            (BSIM_CODED_CI_S8, 1, 376, 400, RX_STATUS_OK),
            (pdu + crc24(pdu), 401, 401, deadline, RX_STATUS_CRC_ERROR),
        ):
            assert await peer.receive(HEADER.size) == HEADER.pack(RX_V2_REQUEST)
            request = RX_V2.unpack(await peer.receive(RX_V2.size))
            assert request[:3] == (start, deadline, NEVER)
            if start == 1:
                assert await peer.receive(TIME.size) == TIME.pack(
                    PDU_ADV_ACCESS_ADDRESS
                )
            await peer.send(
                HEADER.pack(RX_V2_ADDRESS)
                + RX_V2_DONE.pack(sync, sync, 0, 0, 0, RX_STATUS_IN_PROGRESS, len(data))
                + data
            )
            assert await peer.receive(HEADER.size + 2 * TIME.size) == (
                HEADER.pack(RX_V2_CONTINUE) + TIME.pack(deadline) + TIME.pack(NEVER)
            )
            await peer.send(
                HEADER.pack(RX_V2_END)
                + RX_V2_DONE.pack(sync, end, 0, 0, 0, status, len(data))
            )

    packet, _ = await asyncio.gather(
        radio.receive(
            37, 390, phy=Phy.LE_CODED, deadline_us=deadline, accept_bad_crc=True
        ),
        respond(),
    )
    assert packet is None and radio.now_us == deadline
    assert (
        radio.earliest_start_us(0, Phy.LE_1M, transmit=True)
        == deadline + RADIO_SWITCH_TIME_US
    )


@pytest.mark.asyncio
async def test_pending_request_does_not_block_loop_and_rejects_overlap(endpoints):
    """A pending wait leaves the loop free but rejects a second request.

    Purpose: Verify that a wait pending on the PHY does not block the event loop
    and that a second concurrent wait is rejected while the first is pending.

    Steps:
        1. Start ``device.wait(100)`` as a task and read its WAIT on the PHY.
        2. Attempt a second ``device.wait(200)``.
        3. Send WAIT_END and await the first wait.

    Expected:
        - The first task is not done before the ack; the second wait raises
          RuntimeError 'one pending request'; the first completes after the ack.
    """
    device, phy = endpoints
    pending = asyncio.create_task(device.wait(100))
    assert await phy.receive(12) == HEADER.pack(WAIT) + TIME.pack(100)
    assert not pending.done()
    with pytest.raises(RuntimeError, match='one pending request'):
        await device.wait(200)
    await phy.send(HEADER.pack(WAIT_END))
    await pending


@pytest.mark.asyncio
@pytest.mark.parametrize('header', [DISCONNECT, 0x1234])
async def test_unexpected_response_raises_python_exception(endpoints, header):
    """An unexpected async wait response raises a Python exception.

    Purpose: Verify ``device.wait`` raises when the PHY answers with a
    DISCONNECT or an unknown header instead of WAIT_END.

    Steps:
        1. On the PHY side, consume the WAIT and send the parametrized header.
        2. Await ``device.wait(100)`` concurrently.

    Expected:
        - ``RuntimeError`` is raised for both headers.
    """
    device, phy = endpoints

    async def respond():
        await phy.receive(12)
        await phy.send(HEADER.pack(header))

    with pytest.raises(RuntimeError):
        await asyncio.gather(device.wait(100), respond())


@pytest.mark.skipif(sys.platform != 'linux', reason='PHY registration requires Linux')
@pytest.mark.asyncio
async def test_connect_timeout_removes_owned_registration(tmp_path):
    """A connect timeout removes only the registration this device created.

    Purpose: Verify that when ``connect`` times out, the device removes its own
    PHY lock file and leaves its file descriptors unset.

    Steps:
        1. Build a PhyConnection and call ``connect(timeout=0)``.

    Expected:
        - ``TimeoutError`` 'did not connect' is raised; the ``.lock`` file does
          not exist; both file descriptors are ``None``.
    """
    connection = PhyConnection(tmp_path, 0)
    with pytest.raises(TimeoutError, match='did not connect'):
        await connection.connect(timeout=0)
    assert not Path(f'{connection.prefix}.lock').exists()
    assert connection.read_fd is None and connection.write_fd is None


@pytest.mark.asyncio
async def test_duplicate_registration_does_not_remove_other_device_lock(tmp_path):
    """A duplicate registration fails without removing the other device's lock.

    Purpose: Verify that connecting when a lock for the same device id already
    exists raises ``FileExistsError`` and leaves the existing lock untouched.

    Steps:
        1. Write an existing lock file for device 0.
        2. Build a PhyConnection and call ``connect``.

    Expected:
        - ``FileExistsError`` is raised and the existing lock content is
          unchanged.
    """
    lock = tmp_path / '2G4.d0.lock'
    lock.write_text('existing device')
    connection = PhyConnection(tmp_path, 0)
    with pytest.raises(FileExistsError):
        await connection.connect()
    assert lock.read_text() == 'existing device'


@pytest.mark.parametrize(
    'channel,mhz',
    [(37, 2402), (38, 2426), (39, 2480), (10, 2424), (11, 2428), (36, 2478)],
)
def test_radio_channel(channel, mhz):
    """Channel numbers map to the BabbleSim modulation and frequency.

    Purpose: Verify ``BabbleSimRadio.params`` returns the LE 1M modulation and
    the scaled frequency offset for advertising and data channels.

    Steps:
        1. For each parametrized (channel, MHz) pair, call ``params``.

    Expected:
        - The result is the 1M modulation and the frequency scaled from the MHz
          value relative to the base.
    """
    assert BabbleSimRadio.params(channel) == (
        MODULATION_LE_1M,
        (mhz - BSIM_FREQUENCY_BASE_MHZ) * BSIM_FREQUENCY_SCALE,
    )


def test_negative_radio_switch_time_is_rejected():
    """A negative radio switch time is rejected.

    Purpose: Verify ``BabbleSimRadio`` refuses a negative ``switch_time_us``,
    which must be nonnegative.

    Steps:
        1. Construct ``BabbleSimRadio(switch_time_us=-1)``.

    Expected:
        - ``ValueError`` mentioning 'nonnegative' is raised.
    """
    with pytest.raises(ValueError, match='nonnegative'):
        BabbleSimRadio(switch_time_us=-1)


@pytest.mark.asyncio
@pytest.mark.parametrize('switch_time_us', [0, RADIO_SWITCH_TIME_US])
async def test_connect_resets_radio_state_but_preserves_switch_time(
    monkeypatch, switch_time_us
):
    """Connecting resets radio switch state but keeps the configured switch time.

    Purpose: Verify ``BabbleSimRadio.connect`` clears any prior switch state and
    end time while preserving the configured ``switch_time_us``, for both zero
    and nonzero switch times.

    Steps:
        1. Patch ``PhyConnection.connect`` to a no-op.
        2. Complete a transmit to set switch state, then call ``connect``.

    Expected:
        - After connect the switch time is unchanged, and the switch state is
          ``None`` with end time 0.
    """
    from bscontroller.radio.babblesim import PhyConnection

    async def connect(connection):
        pass

    monkeypatch.setattr(PhyConnection, 'connect', connect)
    radio = BabbleSimRadio(switch_time_us=switch_time_us)
    radio.switch.complete(37, Phy.LE_1M, transmit=True, end_us=100)
    await radio.connect('test_radio_switch_config', 0)
    try:
        assert radio.switch.switch_time_us == switch_time_us
        assert radio.switch.state is None and radio.switch.end_us == 0
    finally:
        radio.close()
