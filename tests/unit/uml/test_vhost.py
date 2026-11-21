# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

import mmap
import os
import select
import socket
import struct
from contextlib import ExitStack

from bsworld.runtime.pipe import LogCapture
from bsworld.uml.virtio import GuestVirtio

_HEADER = struct.Struct("=III")
_STATE = struct.Struct("=II")
_ACK = struct.pack("=Q", 0)


def test_split_queue_and_inband_call(tmp_path, uml_vhost_library) -> None:
    with ExitStack() as resources:
        controller = resources.enter_context(socket.socket(socket.AF_UNIX))
        hci_path = tmp_path / "hci.sock"
        controller.bind(str(hci_path))
        controller.listen()
        log = LogCapture.open(tmp_path / "guest.log")
        resources.callback(log.stop)
        backend = GuestVirtio(tmp_path, "guest", hci_path, log, uml_vhost_library)
        backend.listen()
        resources.callback(backend.close, disconnect_hci=socket.socket.close)

        def service_read():
            readable, _, _ = select.select(backend.read_fds, (), (), 1)
            assert readable
            backend.service(readable, ())

        def connect(path):
            peer = resources.enter_context(socket.socket(socket.AF_UNIX))
            peer.settimeout(1)
            peer.connect(str(tmp_path / path))
            service_read()
            return peer

        def response(peer, number, value=_ACK):
            backend.service((), backend.write_fds)
            expect = _HEADER.pack(number, 5, len(value)) + value
            assert peer.recv(len(expect)) == expect

        bluetooth = connect("guest.bt")
        config = struct.pack("=III", 0, 5, 0) + bytes(5)
        bluetooth.sendall(_HEADER.pack(24, 9, len(config)) + config)
        service_read()
        assert backend.write_fds == ()
        response(bluetooth, 24, config)

        frontend = connect("guest.con")
        calls, guest_calls = socket.socketpair()
        resources.enter_context(calls)
        resources.enter_context(guest_calls)
        guest_calls.settimeout(1)

        def request(number, payload=b"", fd=None):
            frame = _HEADER.pack(number, 9, len(payload)) + payload
            if fd is None:
                frontend.sendall(frame)
            else:
                # The fd arrives before the request header is fully readable.
                socket.send_fds(frontend, [frame[:5]], [fd])
                service_read()
                assert backend.write_fds == ()
                frontend.sendall(frame[5:])
            service_read()
            if number == 35:  # VRING_KICK
                assert backend.write_fds
            response(frontend, number)

        request(21, fd=calls.fileno())  # SET_BACKEND_REQ_FD

        fd = os.memfd_create("uml")
        resources.callback(os.close, fd)
        os.ftruncate(fd, 8192)
        memory = resources.enter_context(mmap.mmap(fd, 4096, offset=4096))
        memory_table = struct.pack("=IIQQQQ", 1, 0, 0x1000, 4096, 0x3000, 4096)
        request(5, memory_table, fd)
        request(8, _STATE.pack(0, 2))  # SET_VRING_NUM
        request(9, struct.pack("=IIQQQQ", 0, 0, 0x3000, 0x3100, 0x3200, 0))
        request(18, _STATE.pack(0, 1))  # SET_VRING_ENABLE
        backend.write_console(b"event tail")
        assert not backend.has_notifications

        # One writable chain with two pieces, posted on console RX queue 0.
        struct.pack_into("<QIHH", memory, 0, 0x1300, 2, 3, 1)
        struct.pack_into("<QIHH", memory, 16, 0x1400, 4, 2, 0)
        struct.pack_into("<HHH", memory, 0x200, 0, 1, 0)
        request(35, _STATE.pack(0, 0))  # VRING_KICK

        assert memory[0x300:0x302] == b"ev"
        assert memory[0x400:0x404] == b"ent "
        assert struct.unpack_from("<HHII", memory, 0x100) == (0, 1, 0, 6)
        assert backend.has_notifications
        assert not backend.notifications_pending
        backend.flush_notifications()
        assert backend.notifications_pending
        backend.service((), backend.write_fds)
        assert guest_calls.recv(20) == _HEADER.pack(4, 9, 8) + _STATE.pack(0, 0)
        guest_calls.sendall(_HEADER.pack(4, 5, 8) + _ACK)
        service_read()
        assert not backend.notifications_pending
        assert not backend.has_notifications

        # The rest of the command waits until the guest reposts a descriptor.
        struct.pack_into("<QIHH", memory, 0, 0x1500, 4, 2, 0)
        struct.pack_into("<HHHH", memory, 0x200, 0, 2, 0, 0)
        request(35, _STATE.pack(0, 0))

        assert memory[0x500:0x504] == b"tail"
        assert struct.unpack_from("<HHIIII", memory, 0x100) == (0, 2, 0, 6, 0, 4)
        assert backend.has_notifications
        assert not backend.notifications_pending

        # Replacing the rings discards the completed queue's unsent notification.
        request(18, _STATE.pack(0, 0))  # SET_VRING_ENABLE
        request(5, memory_table, fd)  # SET_MEM_TABLE
        assert not backend.has_notifications
        assert not backend.notifications_pending
