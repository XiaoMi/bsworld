# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""BIG command lifetimes and ISO flow across the real H4 boundary."""

import asyncio

import pytest

from bscontroller import hci
from bscontroller.config import ControllerConfig
from bscontroller.hci.transport import H4Parser, H4Server
from bscontroller.linklayer.advertising import ExtendedPdu, SyncInfo
from bscontroller.linklayer.bis import BisCipher, group_session_key
from bscontroller.linklayer.channel_selection import isochronous_channels
from bscontroller.linklayer.discovery import PeriodicSync
from bscontroller.linklayer.isoal import IsoalParameters, IsoSdu, IsoStatus
from bscontroller.linklayer.pawr import acad_structures
from bscontroller.radio.base import Phy
from tests.unit.hci.harness import PEERS, ErrorCode, HostSink
from tests.unit.helpers import bis_info, extended_parameters
from tests.unit.linklayer.air.harness import Air


def create_big(**changes):
    values = dict(
        big_handle=0,
        advertising_handle=0,
        num_bis=1,
        sdu_interval=10000,
        iso_interval=8,
        nse=2,
        max_sdu=32,
        max_pdu=32,
        phy=1,
        packing=0,
        framing=0,
        bn=1,
        irc=2,
        pto=0,
        encryption=0,
        broadcast_code=bytes(16),
    )
    return hci.HCI_LE_Create_BIG_Test_Command(**(values | changes))


def advertising(host, handle=0):
    host.cmd(extended_parameters(advertising_handle=handle))
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(handle, 16, 16, 0))


def advance(host, target):
    host.loop.run_until_complete(host.link.advance_to_us(target))
    host._drain()


def establish(host, **changes):
    advertising(host)
    host.cmd_status(create_big(**changes))
    advance(host, 10000)
    event = host.ut_rx(hci.HCI_LE_Create_BIG_Complete_Event, status=0)
    return event.connection_handle[0]


def setup_path(host, handle, direction=0):
    return host.cmd(
        hci.HCI_LE_Setup_ISO_Data_Path_Command(
            handle, direction, 0, hci.CodingFormat(hci.CodecID.TRANSPARENT), 0, b''
        )
    )


@pytest.mark.parametrize(
    'changes,status',
    [
        ({'big_handle': 0xF0}, 0x11),
        ({'num_bis': 0}, 0x11),
        ({'iso_interval': 3}, 0x11),
        ({'nse': 0}, 0x11),
        ({'bn': 0}, 0x11),
        ({'irc': 0}, 0x11),
        ({'framing': 2}, 0x11),
        ({'phy': 3}, 0x11),
        ({'encryption': 2}, 0x11),
        ({'pto': 16}, 0x11),
        ({'max_pdu': 252}, 0x11),
    ],
)
def test_create_big_test_rejects_invalid_configuration_atomically(
    host, changes, status
):
    """Create BIG Test rejects invalid inputs before reserving any resources.

    Steps:
        1. Configure a periodic advertising train and send each invalid command.
        2. Inspect live group/stream counts and then send a valid command.

    Expected:
        - Command Status reports the required error; no partial BIG remains.
        - A subsequent valid create succeeds with the same BIG handle.
    """
    advertising(host)
    host.cmd_status(create_big(**changes), status)
    assert not host.link.roles.broadcast.groups and not host.link.roles.iso.streams
    host.cmd_status(create_big())


def test_big_create_complete_and_hci_fragment_credits(configured):
    """A fragmented Host SDU is transmitted once and returns packet credits.

    Steps:
        1. Create a BIG and set up its input path with one HCI packet credit.
        2. Send a first fragment, wait for its credit, then send the final fragment.
        3. Read ISO TX Sync and inspect both immediate repetitions on air.

    Expected:
        - Two Host packet credits are returned; repeated BIS payloads match.
        - TX Sync reports the Host sequence, event anchor and reference offset.
    """
    host = configured(ControllerConfig(iso_packet_count=1, iso_packet_length=8))
    handle = establish(host)
    setup_path(host, handle)
    host.cmd(
        hci.HCI_LE_Read_ISO_TX_Sync_Command(handle), ErrorCode.COMMAND_DISALLOWED_ERROR
    )
    host.send(
        hci.HCI_IsoDataPacket(
            handle,
            7,
            b'abc',
            0,
            packet_sequence_number=42,
            iso_sdu_length=6,
            packet_status_flag=0,
        )
    )
    host.ut_rx(
        hci.HCI_Number_Of_Completed_Packets_Event,
        connection_handles=[handle],
        num_completed_packets=[1],
    )
    host.send(hci.HCI_IsoDataPacket(handle, 3, b'def', 3))
    host.ut_rx_q_is_empty()
    advance(host, 20000)
    host.ut_rx(
        hci.HCI_Number_Of_Completed_Packets_Event,
        connection_handles=[handle],
        num_completed_packets=[1],
    )
    packets = host.radio.sent[-2:]
    assert [p.pdu for p in packets] == [b'\x00\x06abcdef'] * 2
    assert packets[1].start_us - packets[0].start_us == 486
    sync = host.cmd(hci.HCI_LE_Read_ISO_TX_Sync_Command(handle))
    assert sync.packet_sequence_number == 42
    assert sync.tx_time_stamp == packets[0].start_us
    assert sync.time_offset == 0


@pytest.mark.parametrize(
    'changes',
    [
        {'max_sdu': 33, 'max_pdu': 32},
        {'framing': 1, 'max_sdu': 32, 'max_pdu': 6},
        {
            'framing': 1,
            'max_sdu': 1,
            'max_pdu': 32,
            'sdu_interval': 1000000,
            'iso_interval': 3200,
            'nse': 31,
            'irc': 1,
            'pto': 15,
        },
    ],
)
def test_big_test_rejects_incompatible_in_range_isoal_geometry(host, changes):
    """Valid individual ranges do not make an impossible ISOAL stream valid.

    Steps:
        1. Request an unframed SDU exceeding its BN capacity, or a framed
           stream whose sustained SDU rate exceeds its PDU bandwidth, or a
           transport latency exceeding the HCI event's 24-bit field.

    Expected:
        - Create BIG Test returns Invalid HCI Command Parameters, 0x12;
          the rejected configuration reserves no group or ISO handle.
    """
    advertising(host)
    host.cmd_status(create_big(**changes), 0x12)
    assert not host.link.roles.broadcast.groups and not host.link.roles.iso.streams


def test_bis_and_acl_share_the_controller_handle_namespace(host):
    """Creating an ACL after a BIG cannot reuse the BIS connection handle.

    Steps:
        1. Create one BIS and then publish a new ACL connection.

    Expected:
        - The two data-channel connection handles differ.
    """
    handle = establish(host)
    assert host.connection().handle != handle


def test_big_capacity_reset_and_advertising_set_removal(configured):
    """BIG resources have explicit capacity and survive independent PA disable.

    Steps:
        1. Create a BIG on a Controller limited to one group and one BIS.
        2. Attempt another group and removal of the associated advertising set.
        3. Reset the Controller and inspect all ISO state.

    Expected:
        - Capacity failure is 0x0D; removing an associated set is disallowed.
        - Reset drops groups, paths and pending ISO data together.
    """
    host = configured(ControllerConfig(max_bigs=1, max_bis=1))
    establish(host)
    advertising(host, 1)
    host.cmd_status(create_big(big_handle=1, advertising_handle=1), 0x0D)
    host.cmd(hci.HCI_LE_Remove_Advertising_Set_Command(0), 0x0C)
    host.cmd(hci.HCI_Reset_Command())
    assert not host.link.roles.broadcast.groups and not host.link.roles.iso.streams


def test_iso_test_mode_and_data_path_are_mutually_exclusive(host):
    """The Host cannot combine a data path with ISO-generated test SDUs.

    Steps:
        1. Enable ISO Transmit Test, try to set up an input data path and run.
        2. End the test, set up a path, then try to restart the test.
        3. Attempt a receiver test on the broadcaster's handle.

    Expected:
        - Test/path conflicts return 0x0C; the wrong test role returns 0x11.
        - Maximum-size test SDUs contain the unframed payload counter.
    """
    handle = establish(host)
    host.cmd(hci.HCI_LE_ISO_Transmit_Test_Command(handle, 2))
    host.cmd(
        hci.HCI_LE_Setup_ISO_Data_Path_Command(
            handle, 0, 0, hci.CodingFormat(hci.CodecID.TRANSPARENT), 0, b''
        ),
        0x0C,
    )
    advance(host, 20000)
    assert host.radio.sent[-1].pdu[2:6] == (1).to_bytes(4, 'little')
    assert len(host.radio.sent[-1].pdu) == 34
    host.cmd(hci.HCI_LE_ISO_Test_End_Command(handle))
    setup_path(host, handle)
    host.cmd(hci.HCI_LE_ISO_Transmit_Test_Command(handle, 0), 0x0C)
    host.cmd(hci.HCI_LE_ISO_Receive_Test_Command(handle, 0), 0x11)


@pytest.mark.parametrize('packet_length', [5, 12, 251])
def test_iso_output_fragmentation_respects_buffer_length_and_status(
    configured, packet_length
):
    """Controller-to-Host ISO fragments fit small and ordinary HCI buffers.

    Steps:
        1. Emit a 32-octet SDU with a nonzero status and synchronization time.
        2. Collect all real H4 frames and reassemble their data fragments.

    Expected:
        - Each Data_Total_Length fits the configured buffer.
        - Sequence/status appear only in the first fragment; PB flags delimit
          the SDU, including configurations too small for a timestamp.
    """
    host = configured(ControllerConfig(iso_packet_length=packet_length))
    sdu = IsoSdu(17, bytes(range(32)), 0x12345678, IsoStatus.POSSIBLY_INVALID)
    host.notify('on_iso_received', 3, sdu)
    packets = host.events
    assert all(
        isinstance(packet, hci.HCI_IsoDataPacket)
        and packet.data_total_length <= packet_length
        for packet in packets
    )
    assert b''.join(p.iso_sdu_fragment for p in packets) == sdu.data
    assert packets[0].packet_sequence_number == 17
    assert packets[0].packet_status_flag == 1
    assert packets[0].pb_flag == (2 if len(packets) == 1 else 0)
    assert packets[-1].pb_flag == (2 if len(packets) == 1 else 3)


def test_iso_output_sdu_crosses_socket_as_individual_h4_frames(configured, tmp_path):
    """A small HCI buffer can split one received SDU into more than 128 frames.

    Steps:
        1. Open the Controller's Unix socket and deliver a 1024-octet SDU.
        2. Parse all H4 frames received by the Host.

    Expected:
        - The Host receives all 129 frames in order and the socket remains open.
    """
    host = configured(ControllerConfig(iso_packet_length=8))
    sdu = IsoSdu(4, bytes(range(256)) * 4, 12345)

    async def exchange():
        path = tmp_path / 'iso.sock'
        async with H4Server(host.controller, path) as endpoint:
            reader, writer = await asyncio.open_unix_connection(path)
            try:
                while endpoint.writer is None:
                    await asyncio.sleep(0)
                host.link.listener.on_iso_received(3, sdu)
                sink = HostSink()
                parser = H4Parser(sink)
                while len(sink.received) < 129:
                    chunk = await asyncio.wait_for(reader.read(4096), 2)
                    assert chunk
                    parser.feed_data(chunk)
                assert not endpoint.finished.done()
                return [hci.HCI_Packet.from_bytes(frame) for frame in sink.received]
            finally:
                writer.close()
                await writer.wait_closed()

    frames = host.loop.run_until_complete(exchange())
    assert [frame.pb_flag for frame in frames] == [0] + [1] * 127 + [3]
    assert b''.join(frame.iso_sdu_fragment for frame in frames) == sdu.data


def test_iso_handle_has_independent_input_and_output_test_modes(host):
    """One ISO handle can own two independently configured directions.

    Steps:
        1. Register two directions with different burst geometry on one handle.
        2. Start input test mode and configure the output data path.
        3. Remove the output path, start output test mode and end both tests.

    Expected:
        - Output setup and teardown preserve input test mode.
        - Inactive counter queries report Unsupported Feature or Parameter Value.
    """
    handle = 5
    iso = host.link.roles.iso
    iso.add(handle, True, IsoalParameters(False, 1, 32, 32, 10000, 10000))
    iso.add(handle, False, IsoalParameters(False, 8, 32, 32, 10000, 10000))
    host.cmd(hci.HCI_LE_ISO_Transmit_Test_Command(handle, 1))
    setup_path(host, handle, 1)
    host.cmd(hci.HCI_LE_ISO_Read_Test_Counters_Command(handle), 0x11)
    host.cmd(hci.HCI_LE_Remove_ISO_Data_Path_Command(handle, 2))
    host.cmd(hci.HCI_LE_ISO_Receive_Test_Command(handle, 2))
    host.cmd(hci.HCI_LE_ISO_Read_Test_Counters_Command(handle))
    host.cmd(hci.HCI_LE_ISO_Test_End_Command(handle))
    host.cmd(hci.HCI_LE_ISO_Test_End_Command(handle), 0x11)
    setup_path(host, handle, 0)
    setup_path(host, handle, 1)
    iso.release(handle)
    assert not iso.contains(handle)


def test_framed_sdu_with_unrepresentable_offset_releases_credit(host):
    """A late framed SDU releases its credit without interrupting the next SDU.

    Steps:
        1. Send framed SDUs with a timestamp 17 s in the past and a current timestamp.
        2. Advance one BIG event and read the transmitted SDU's timing.

    Expected:
        - Both credits return, and only the second SDU reaches the air.
    """
    handle = establish(host, framing=1, max_pdu=40)
    setup_path(host, handle)
    now = host.link.now_us
    for sequence, timestamp in enumerate((now - 17000000, now)):
        host.send(
            hci.HCI_IsoDataPacket(
                handle,
                9,
                bytes([sequence]),
                2,
                time_stamp=timestamp & 0xFFFFFFFF,
                packet_sequence_number=sequence,
                iso_sdu_length=1,
                packet_status_flag=0,
            )
        )
    advance(host, now + 10000)
    host.ut_rx(hci.HCI_Number_Of_Completed_Packets_Event, num_completed_packets=[2])
    result = host.cmd(hci.HCI_LE_Read_ISO_TX_Sync_Command(handle))
    assert result.packet_sequence_number == 1
    assert host.radio.sent[-1].pdu[2:4] == b'\x02\x04'
    assert host.radio.sent[-1].pdu[7:] == b'\x01'


@pytest.mark.parametrize(
    'framed,mode,reported', [(False, 0, 0), (True, 0, 1), (True, 1, 2)]
)
def test_big_info_report_framing_mode(host, framed, mode, reported):
    """BIGInfo reports the advertised SDU framing and segmentation mode.

    Steps:
        1. Publish BIGInfo for each framed or unframed mode to the Host.

    Expected:
        - The report distinguishes unframed (0), segmentable (1) and unsegmented (2).
    """
    host.notify('on_big_info', 3, bis_info(framed=framed, framing_mode=mode))
    host.ut_rx(hci.HCI_LE_BIGInfo_Advertising_Report_Event, framing=reported)


def test_big_termination_uses_control_instant_and_releases_paths(host):
    """Host termination repeats a BIG Control PDU until its six-event instant.

    Steps:
        1. Create a BIG, request termination and advance across the instant.
        2. Inspect control payloads, CSTF, CSSN and released stream handles.

    Expected:
        - At least six identical control payloads precede termination.
        - Data packets announce control, and the completion releases the group.
    """
    handle = establish(host)
    host.cmd_status(hci.HCI_LE_Terminate_BIG_Command(0, 0x13))
    advance(host, 85000)
    host.ut_rx(hci.HCI_LE_Terminate_BIG_Complete_Event, big_handle=0, reason=0x16)
    controls = [p.pdu for p in host.radio.sent if p.pdu[0] & 3 == 3]
    assert len(controls) == 6
    assert len(set(controls)) == 1
    assert controls[0] == bytes.fromhex('070401130700')
    assert all(p.pdu[0] & 0x20 for p in host.radio.sent[2:20] if p.pdu[0] & 3 != 3)
    assert (
        handle not in host.link.roles.iso.streams
        and not host.link.roles.broadcast.groups
    )


def receiver(host, info, code=bytes(16), *, timeout=10):
    sync = PeriodicSync(PEERS[0], 0, 1000000, 1000000, established=True, handle=3)
    host.link.syncs[3] = sync
    host.in_loop(
        host.link.roles.broadcast.observed_info,
        sync,
        bytes([len(info.encode()) + 1, 0x2C]) + info.encode(),
        10,
    )
    host.ut_rx(hci.HCI_LE_BIGInfo_Advertising_Report_Event, sync_handle=3)
    host.cmd_status(
        hci.HCI_LE_BIG_Create_Sync_Command(
            0, 3, int(info.encrypted), code, 0, timeout, [1]
        )
    )


def receive_packet(
    host,
    info,
    event,
    payload,
    *,
    subevent=0,
    valid_crc=True,
    code=bytes(16),
    llid=0,
    drift_us=0,
):
    channel = isochronous_channels(
        event, info.access_address(1), info.channel_map, info.nse
    )[subevent]
    if info.encrypted:
        payload = BisCipher(
            group_session_key(code, info.gskd), info.giv, info.access_address(1)
        ).encrypt(llid, payload, event * info.bn + subevent % info.bn)
    host.radio.script.append(
        Air(
            bytes([llid, len(payload)]) + payload,
            at=10000
            + event * info.interval_us
            + subevent * info.sub_interval_us
            + drift_us,
            channel=channel,
            phy=info.phy,
            access_address=info.access_address(1),
            valid_crc=valid_crc,
        )
    )


@pytest.mark.parametrize('phy', list(Phy))
@pytest.mark.parametrize('encrypted', [False, True])
def test_receiver_repeated_payload_crc_recovery_and_iso_delivery(host, phy, encrypted):
    """A receiver recovers from CRC loss and delivers each repeated SDU once.

    Steps:
        1. Advertise a BIGInfo with 20-ms events on the selected PHY/security.
        2. Receive one BIS packet, synchronize, and set up the output data path.
        3. Corrupt the next event's first CRC, then repeat its payload correctly.

    Expected:
        - BIG Sync Established reports a usable handle.
        - One valid HCI SDU arrives with the expected bytes and timestamp.
    """
    info = bis_info(
        offset_us=9990,
        iso_interval=16,
        sub_interval_us=4000,
        bis_spacing_us=8000,
        sdu_interval_us=20000,
        phy=phy,
        giv=bytes(range(8)) if encrypted else b'',
        gskd=bytes(range(16)) if encrypted else b'',
    )
    receiver(host, info)
    receive_packet(host, info, 0, b'first')
    advance(host, 17000)
    event = host.ut_rx(hci.HCI_LE_BIG_Sync_Established_Event, status=0)
    handle = event.connection_handle[0]
    setup_path(host, handle, 1)
    receive_packet(host, info, 1, b'second', valid_crc=False)
    receive_packet(host, info, 1, b'second', subevent=1)
    advance(host, 30000 + info.sync_delay_us + 1)
    host.ut_rx(
        hci.HCI_IsoDataPacket,
        connection_handle=handle,
        iso_sdu_fragment=b'second',
        packet_status_flag=0,
        time_stamp=30000 + info.sync_delay_us,
    )
    host.ut_rx_q_is_empty()


@pytest.mark.parametrize('delay_us', [0, 100000])
def test_iso_output_delivery_uses_synchronization_time_and_controller_delay(
    host, delay_us
):
    """The output path starts transfer at the synchronization reference plus delay.

    Steps:
        1. Synchronize to a 20 ms BIS and set up the output path with the given delay.
        2. Receive one SDU and advance to either side of its delivery time.

    Expected:
        - The Host first receives the SDU at its delivery time.
        - Its HCI timestamp still identifies the SDU synchronization reference.
    """
    info = bis_info(
        offset_us=9990,
        iso_interval=16,
        sub_interval_us=4000,
        bis_spacing_us=8000,
        sdu_interval_us=20000,
    )
    receiver(host, info, timeout=100)
    receive_packet(host, info, 0, b'first')
    advance(host, 17000)
    handle = host.ut_rx(hci.HCI_LE_BIG_Sync_Established_Event).connection_handle[0]
    host.cmd(
        hci.HCI_LE_Setup_ISO_Data_Path_Command(
            handle, 1, 0, hci.CodingFormat(hci.CodecID.TRANSPARENT), delay_us, b''
        )
    )
    receive_packet(host, info, 1, b'data')
    reference = 30000 + info.sync_delay_us
    advance(host, reference + delay_us - 1)
    host.ut_rx_q_is_empty()
    advance(host, reference + delay_us)
    host.ut_rx(hci.HCI_IsoDataPacket, time_stamp=reference, iso_sdu_fragment=b'data')


def test_receiver_inherits_periodic_sca_and_tracks_clock_drift(host):
    """The BIS receive window uses SyncInfo's SCA across long event intervals.

    Steps:
        1. Acquire a BIG with a one-second interval and 500-ppm peer SCA.
        2. Receive its next event 400 us later than the nominal anchor.

    Expected:
        - Window widening covers the advertised drift, delivers the SDU once,
          and resynchronizes the BIG anchor to the received packet, even after
          the associated periodic advertising sync times out.
    """
    info = bis_info(offset_us=9990, iso_interval=800, sdu_interval_us=1000000)
    receiver(host, info, timeout=200)
    host.link.syncs[3].info = SyncInfo(
        9990, 800, info.channel_map, 0x9A328370, 0x123456, 0, sca=0
    )
    receive_packet(host, info, 0, b'first')
    advance(host, 17000)
    event = host.ut_rx(hci.HCI_LE_BIG_Sync_Established_Event, status=0)
    setup_path(host, event.connection_handle[0], 1)
    receive_packet(host, info, 1, b'drift', drift_us=400)
    advance(host, 1017000)
    host.ut_rx(hci.HCI_LE_Periodic_Advertising_Sync_Lost_Event, sync_handle=3)
    host.ut_rx(hci.HCI_IsoDataPacket, iso_sdu_fragment=b'drift', packet_status_flag=0)
    group = host.link.roles.broadcast.groups[0]
    assert group.sca == 0 and group.last_rx_us == 1010400


def test_receiver_wrong_broadcast_code_is_a_mic_failure(host):
    """A wrong Broadcast_Code cannot establish an authenticated BIS stream.

    Steps:
        1. Synchronize with a Broadcast_Code different from the peer's code.
        2. Receive a CRC-valid, nonempty encrypted BIS packet.

    Expected:
        - Synchronization fails with MIC Failure (0x3D) and releases handles.
    """
    info = bis_info(offset_us=9990, giv=bytes(range(8)), gskd=bytes(range(16)))
    receiver(host, info, bytes([1]) * 16)
    receive_packet(host, info, 0, b'authenticated')
    advance(host, 15000)
    host.ut_rx(hci.HCI_LE_BIG_Sync_Established_Event, status=0x3D)
    assert not host.link.roles.iso.streams and not host.link.roles.broadcast.groups


def test_receiver_times_out_after_six_listened_big_events(host):
    """BIG establishment allows six events from the first listening opportunity.

    Steps:
        1. Request a 10-ms BIG with a one-second supervision timeout.
        2. Omit all BIS packets through the fifth and sixth BIG events.

    Expected:
        - Acquisition stays pending after five events. After the sixth it
          reports 0x3E with no stream handles.
    """
    receiver(host, bis_info(offset_us=9990), timeout=100)
    advance(host, 59000)
    host.ut_rx_q_is_empty()
    advance(host, 62000)
    host.ut_rx(hci.HCI_LE_BIG_Sync_Established_Event, status=0x3E, connection_handle=[])
    assert not host.link.roles.broadcast.groups


def test_big_supervision_timeout_is_at_least_six_iso_intervals(host):
    """An established BIG survives the interval to the next valid BIS packet.

    Steps:
        1. Synchronize to a 400-ms BIG with a requested 100-ms timeout.
        2. Receive the following event, then stop transmitting for 2.4 s.

    Expected:
        - The next event refreshes synchronization. Timeout is reported six
          ISO intervals after that packet.
    """
    info = bis_info(offset_us=9990, iso_interval=320, sdu_interval_us=400000)
    receiver(host, info)
    for event in (0, 1):
        receive_packet(host, info, event, b'data')
    advance(host, 420000)
    host.ut_rx(hci.HCI_LE_BIG_Sync_Established_Event, status=0)
    assert host.link.roles.broadcast.groups[0].last_rx_us == 410000
    advance(host, 2700000)
    host.ut_rx(hci.HCI_LE_Periodic_Advertising_Sync_Lost_Event, sync_handle=3)
    assert 0 in host.link.roles.broadcast.groups
    advance(host, 2811000)
    host.ut_rx(hci.HCI_LE_BIG_Sync_Lost_Event, reason=0x08)


def test_new_periodic_sync_does_not_inherit_another_peers_big_info(host):
    """Reuse of a PA sync handle cannot select the previous peer's BIGInfo.

    Steps:
        1. Receive an encrypted BIGInfo and terminate its PA synchronization.
        2. Reuse its handle for a different peer and request an unencrypted BIG.
        3. Receive the new peer's clear BIGInfo and first BIS packet.

    Expected:
        - Acquisition waits for the new BIGInfo and succeeds.
    """
    first = PeriodicSync(PEERS[0], 0, 1000000, 1000000, established=True, handle=1)
    host.link.syncs[1] = first
    encrypted = bis_info(giv=bytes(8), gskd=bytes(16)).encode()
    host.in_loop(
        host.link.roles.broadcast.observed_info,
        first,
        bytes([len(encrypted) + 1, 0x2C]) + encrypted,
        0,
    )
    host.ut_rx(hci.HCI_LE_BIGInfo_Advertising_Report_Event, sync_handle=1)
    host.cmd(hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(1))
    second = PeriodicSync(PEERS[1], 0, 1000000, 1000000, established=True, handle=1)
    host.link.syncs[1] = second
    host.cmd_status(hci.HCI_LE_BIG_Create_Sync_Command(0, 1, 0, bytes(16), 0, 10, [1]))
    advance(host, 5)
    host.ut_rx_q_is_empty()
    info = bis_info(offset_us=9990)
    clear = info.encode()
    host.in_loop(
        host.link.roles.broadcast.observed_info,
        second,
        bytes([len(clear) + 1, 0x2C]) + clear,
        10,
    )
    host.ut_rx(hci.HCI_LE_BIGInfo_Advertising_Report_Event, sync_handle=1)
    receive_packet(host, info, 0, b'new')
    advance(host, 17000)
    host.ut_rx(hci.HCI_LE_BIG_Sync_Established_Event, status=0)


def test_host_termination_during_a_big_event_starts_on_the_next_event(host):
    """A Host command arriving between subevents cannot change that event's CSSN.

    Steps:
        1. Create a BIG, then inject Terminate BIG after the next event's first
           actual transmission, while its RF executor is still running.
        2. Observe that complete event and the following termination sequence.

    Expected:
        - Both PDUs of the interrupted event retain their original CSSN/CSTF.
        - The next complete event starts six identical termination controls;
          completion occurs at their advertised Instant.
    """
    establish(host)
    original = host.radio.transmit
    injected = False

    async def transmit(*args, **kwargs):
        nonlocal injected
        end = await original(*args, **kwargs)
        if not injected:
            injected = True
            host.controller.on_packet(bytes(hci.HCI_LE_Terminate_BIG_Command(0, 0x13)))
        return end

    host.radio.transmit = transmit
    advance(host, 20000)
    host.ut_rx(
        hci.HCI_Command_Status_Event,
        status=0,
        command_opcode=hci.HCI_LE_TERMINATE_BIG_COMMAND,
    )
    assert [packet.pdu[0] & 0x3C for packet in host.radio.sent[2:4]] == [0, 0]
    advance(host, 90000)
    host.ut_rx(hci.HCI_LE_Terminate_BIG_Complete_Event, reason=0x16)
    controls = [packet for packet in host.radio.sent if packet.pdu[0] & 3 == 3]
    assert len(controls) == 6
    assert len({packet.pdu for packet in controls}) == 1
    assert int.from_bytes(controls[0].pdu[-2:], 'little') == 8


def test_big_termination_accepts_controller_error_codes_and_validates_before_cancel(
    host,
):
    """BIG termination reasons follow Vol 4, Part E, 7.8.105's error-code list.

    Steps:
        1. Submit a reserved reason while creation is still pending.
        2. Complete creation and terminate with MIC Failure (0x3D).

    Expected:
        - The invalid reason cannot cancel creation; the valid reason reaches
          the air, while local completion retains its required 0x16 reason.
    """
    advertising(host)
    host.cmd_status(create_big())
    host.cmd_status(hci.HCI_LE_Terminate_BIG_Command(0, 0xFF), 0x12)
    assert not host.link.roles.broadcast.groups[0].cancelled
    advance(host, 10000)
    host.ut_rx(hci.HCI_LE_Create_BIG_Complete_Event, status=0)
    host.cmd_status(hci.HCI_LE_Terminate_BIG_Command(0, 0x3D))
    advance(host, 90000)
    host.ut_rx(hci.HCI_LE_Terminate_BIG_Complete_Event, reason=0x16)
    controls = [p.pdu[2:] for p in host.radio.sent if p.pdu[0] & 3 == 3]
    assert controls and all(p[:2] == b'\x01\x3d' for p in controls)


def test_channel_map_state_waits_for_its_instant(host):
    """Preparing the next event must not publish its new channel map early.

    Steps:
        1. Request a BIG channel-map change and compute the upcoming Instant.
        2. Advance to just before the radio preparation boundary, then through it.

    Expected:
        - BIGInfo retains the old map until the Instant event is prepared.
        - The event at Instant uses the requested map.
    """
    establish(host)
    group = host.link.roles.broadcast.groups[0]
    old = group.info.channel_map
    new = 0x0AAAAAAAAA
    host.cmd(
        hci.HCI_LE_Set_Host_Channel_Classification_Command(new.to_bytes(5, 'little'))
    )
    instant = group.pending_control.instant
    anchor = group.next_us + (instant - group.counter) * group.info.interval_us
    advance(host, anchor - 61)
    assert group.info.channel_map == old
    advance(host, anchor + 1000)
    assert group.info.channel_map == new


def test_channel_classification_during_a_pending_update_is_not_lost(host):
    """A new Host classification is retained while a BIG map update is pending.

    Steps:
        1. Request two different classifications before the first Instant.
        2. Execute both control procedures and inspect their advertised maps.

    Expected:
        - The procedures run in order, and the BIG eventually uses the latest
          classification instead of retaining the first map indefinitely.
    """
    establish(host)
    maps = (0x0AAAAAAAAA, 0x1555555555)
    for channel_map in maps:
        host.cmd(
            hci.HCI_LE_Set_Host_Channel_Classification_Command(
                channel_map.to_bytes(5, 'little')
            )
        )
    advance(host, 200000)
    assert host.link.roles.broadcast.groups[0].info.channel_map == maps[-1]
    controls = [p.pdu for p in host.radio.sent if p.pdu[0] & 3 == 3]
    assert {int.from_bytes(p[3:8], 'little') for p in controls} == set(maps)


@pytest.mark.parametrize('encrypted', [False, True])
@pytest.mark.parametrize('adi', [False, True])
@pytest.mark.parametrize('length', [0, 400])
def test_biginfo_coexists_with_periodic_channel_map_adi_and_chained_data(
    host, encrypted, adi, length
):
    """ACAD scheduling respects ExtHdrLen while PA and BIG update their maps.

    Steps:
        1. Enable a periodic train, optionally including ADI and chained data.
        2. Create a BIG and classify channels while both roles remain active.
        3. Receive past both Instants, then shorten the periodic data.

    Expected:
        - Every periodic PDU fits its six-bit extended-header length.
        - The periodic map indication takes priority when both AD structures
          cannot fit; BIGInfo resumes when header space becomes available.
        - Both trains remain active and use the requested channel map.
    """
    advertising(host)
    if length:
        operation = hci.HCI_LE_Set_Extended_Advertising_Data_Command.Operation
        for mode in (operation.FIRST_FRAGMENT, operation.LAST_FRAGMENT):
            host.cmd(
                hci.HCI_LE_Set_Periodic_Advertising_Data_Command(
                    0, mode, bytes(length // 2)
                )
            )
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(1 | int(adi) << 1, 0))
    host.cmd(hci.HCI_LE_Set_Extended_Advertising_Enable_Command(1, [0], [0], [0]))
    host.cmd_status(create_big(encryption=int(encrypted)))
    advance(host, 50000)
    host.ut_rx(hci.HCI_LE_Create_BIG_Complete_Event, status=0)
    channel_map = 0x0AAAAAAAAA
    host.cmd(
        hci.HCI_LE_Set_Host_Channel_Classification_Command(
            channel_map.to_bytes(5, 'little')
        )
    )
    advance(host, 250000)
    item = host.link.advertisers[0]
    periodic = [p for p in host.radio.sent if p.access_address == item.periodic_aa]
    assert periodic
    structures = [
        dict(acad_structures(ExtendedPdu.decode(p.pdu).acad)) for p in periodic
    ]
    assert any(0x28 in fields for fields in structures)
    if encrypted:
        assert all(0x2C not in fields for fields in structures if 0x28 in fields)
    assert item.periodic_channel_map == channel_map
    assert host.link.roles.broadcast.groups[0].info.channel_map == channel_map
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Data_Command(0, 3, b''))
    host.radio.sent.clear()
    advance(host, 300000)
    assert any(
        0x2C in dict(acad_structures(ExtendedPdu.decode(p.pdu).acad))
        for p in host.radio.sent
        if p.access_address == item.periodic_aa
    )


def test_receiver_limits_pending_and_duplicate_big_synchronizations(host):
    """The controller may establish only one BIG sync at a time.

    Steps:
        1. Request synchronization to BIG 0. While it is pending, try a
           different PA and allocate a separate ACL handle.
        2. After synchronization succeeds, request another BIG on the same PA.
        3. Start a new synchronization using the other PA.

    Expected:
        - Concurrent acquisition and duplicate synchronization return 0x0C.
          The pending BIS reserves its handle, and a different PA can be used
          once the first synchronization is established.
    """
    info = bis_info(offset_us=9990)
    receiver(host, info)
    host.link.syncs[4] = PeriodicSync(
        PEERS[1], 0, 1000000, 1000000, established=True, handle=4
    )
    other = hci.HCI_LE_BIG_Create_Sync_Command(1, 4, 0, bytes(16), 0, 10, [1])
    host.cmd_status(other, 0x0C)
    assert (
        host.connection().handle not in host.link.roles.broadcast.connection_handles()
    )
    receive_packet(host, info, 0, b'first')
    advance(host, 17000)
    host.ut_rx(hci.HCI_LE_BIG_Sync_Established_Event, status=0)
    host.cmd_status(
        hci.HCI_LE_BIG_Create_Sync_Command(1, 3, 0, bytes(16), 0, 10, [1]), 0x0C
    )
    host.cmd_status(other)


def test_receiver_rejects_more_bises_than_advertised(host):
    """Requesting two BISes from a one-BIS BIG reports Unsupported Parameter."""
    sync = PeriodicSync(PEERS[0], 0, 1000000, 1000000, established=True, handle=3)
    host.link.syncs[3] = sync
    data = bis_info().encode()
    host.in_loop(
        host.link.roles.broadcast.observed_info,
        sync,
        bytes([len(data) + 1, 0x2C]) + data,
        0,
    )
    host.ut_rx(hci.HCI_LE_BIGInfo_Advertising_Report_Event)
    host.cmd_status(
        hci.HCI_LE_BIG_Create_Sync_Command(0, 3, 0, bytes(16), 0, 10, [1, 2])
    )
    advance(host, 1)
    host.ut_rx(hci.HCI_LE_BIG_Sync_Established_Event, status=0x11, connection_handle=[])


@pytest.mark.parametrize('length,without_adi,with_adi', [(93, 8, 8), (63, 6, 7)])
def test_disabled_periodic_train_can_grow_within_host_interval_range(
    host, length, without_adi, with_adi
):
    """Create BIG checks the maximum interval of a disabled PA train.

    Steps:
        1. Set a Coded PA interval of 7.5–20 ms and supply 63 or 93 octets.
        2. Create a BIG, then enable PA with ADI and BIGInfo.

    Expected:
        - The 93-octet train needs 10 ms at creation. The 63-octet train
          fits 7.5 ms until ADI requires 8.75 ms at enable.
    """
    host.cmd(extended_parameters(secondary_advertising_phy=Phy.LE_CODED))
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 6, 16, 0))
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Data_Command(0, 3, bytes(length)))
    host.cmd_status(create_big())
    assert host.link.advertisers[0].periodic_interval == without_adi
    host.cmd(hci.HCI_LE_Set_Periodic_Advertising_Enable_Command(3, 0))
    assert host.link.advertisers[0].periodic_interval == with_adi


def test_reset_and_handle_reuse_during_control_subevent(host):
    """A completed control TX cannot commit state to a replacement BIG.

    Steps:
        1. Create a two-BIS group and schedule its termination.
        2. Reset during control transmission and recreate its handle with one BIS.

    Expected:
        - The replacement publishes its own completion and continues transmitting.
    """
    establish(host, num_bis=2)
    host.cmd_status(hci.HCI_LE_Terminate_BIG_Command(0, 0x13))
    transmit = host.radio.transmit

    async def reset_during_control(channel, pdu, **parameters):
        end = await transmit(channel, pdu, **parameters)
        if pdu[0] & 3 == 3:
            host.radio.transmit = transmit
            for command in (
                hci.HCI_Reset_Command(),
                extended_parameters(),
                hci.HCI_LE_Set_Periodic_Advertising_Parameters_Command(0, 16, 16, 0),
                create_big(),
            ):
                host.controller.on_packet(bytes(command))
        return end

    host.radio.transmit = reset_during_control
    advance(host, 40000)
    assert len(host.link.roles.broadcast.groups[0].stream_handles) == 1
    assert host.link.roles.broadcast.groups[0].counter > 0


def test_framed_hci_timestamp_unwraps_at_the_controller_clock_boundary(host):
    """A 32-bit Host timestamp is resolved around the full Controller clock.

    Steps:
        1. Start a framed BIG just after the 32-bit timestamp clock wraps.
        2. Send a timestamped SDU with a reference just before the wrap.
        3. Observe its Time_Offset and Read ISO TX Sync result.

    Expected:
        - The offset is positive and small, rather than about 2**32 us.
        - The ISO payload bytes and Host sequence remain intact.
    """
    host.radio.now_us = (1 << 32) + 100
    advertising(host)
    host.cmd_status(create_big(framing=1, max_sdu=19, max_pdu=37))
    advance(host, host.radio.now_us + 1000)
    event = host.ut_rx(hci.HCI_LE_Create_BIG_Complete_Event, status=0)
    handle = event.connection_handle[0]
    setup_path(host, handle)
    host.send(
        hci.HCI_IsoDataPacket(
            handle,
            9,
            b'a',
            2,
            time_stamp=0xFFFFFFF0,
            packet_sequence_number=7,
            iso_sdu_length=1,
            packet_status_flag=0,
        )
    )
    advance(host, host.radio.now_us + 10000)
    host.ut_rx(hci.HCI_Number_Of_Completed_Packets_Event)
    packet = host.radio.sent[0]
    offset = int.from_bytes(packet.pdu[4:7], 'little')
    assert 0 < offset < 20000 and packet.pdu[7:] == b'a'
    result = host.cmd(hci.HCI_LE_Read_ISO_TX_Sync_Command(handle))
    assert result.packet_sequence_number == 7 and result.time_offset == offset


def test_reset_during_bis_transmission_cannot_publish_old_state(host):
    """Reset while an RF await is active releases the entire ISO lifetime.

    Steps:
        1. Create a BIG and inject HCI Reset while its next PDU is transmitting.
        2. Advance beyond the old group's next event.

    Expected:
        - Reset completes, all groups/streams are gone and the old executor
          produces no later packets or stale ISO events.
    """
    establish(host)
    original = host.radio.transmit
    injected = False

    async def transmit(*args, **kwargs):
        nonlocal injected
        end = await original(*args, **kwargs)
        if not injected:
            injected = True
            host.controller.on_packet(bytes(hci.HCI_Reset_Command()))
        return end

    host.radio.transmit = transmit
    before = len(host.radio.sent)
    advance(host, 40000)
    host.ut_rx(hci.HCI_Command_Complete_Event, command_opcode=hci.HCI_RESET_COMMAND)
    assert len(host.radio.sent) == before + 1
    assert not host.link.roles.broadcast.groups and not host.link.roles.iso.streams
