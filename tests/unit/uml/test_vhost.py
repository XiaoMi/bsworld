# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

import mmap
import os
import socket
import struct
from contextlib import ExitStack

from bsworld.uml.vhost import VhostUserBackend

_HEADER = struct.Struct("=III")
_STATE = struct.Struct("=II")
_ACK = struct.pack("=Q", 0)


def test_split_queue_and_inband_call(tmp_path) -> None:
    backend = VhostUserBackend(tmp_path / "hci.sock", config=b"\0" * 6)
    backend.listen()
    with ExitStack() as resources:
        resources.callback(backend.close)
        frontend = resources.enter_context(socket.socket(socket.AF_UNIX))
        frontend.settimeout(1)
        frontend.connect(str(backend.path))
        backend.service(backend.read_fds)
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
                assert backend.service(backend.read_fds) == ()
                assert backend.write_fds == ()
                frontend.sendall(frame[5:])
            return backend.service(backend.read_fds)

        def response(number, value=_ACK):
            backend.service((), backend.write_fds)
            expect = _HEADER.pack(number, 5, len(value)) + value
            assert frontend.recv(len(expect)) == expect

        request(21, fd=calls.fileno())  # SET_BACKEND_REQ_FD
        response(21)
        config_request = struct.pack("=III", 0, 1, 0) + b"\0"
        request(24, config_request)  # GET_CONFIG
        response(24, config_request)

        fd = os.memfd_create("uml")
        resources.callback(os.close, fd)
        os.ftruncate(fd, 8192)
        memory = resources.enter_context(mmap.mmap(fd, 4096, offset=4096))
        memory_table = struct.pack("=IIQQQQ", 1, 0, 0x1000, 4096, 0x3000, 4096)
        request(5, memory_table, fd)  # SET_MEM_TABLE
        response(5)
        request(8, _STATE.pack(1, 2))  # SET_VRING_NUM
        response(8)
        request(9, struct.pack("=IIQQQQ", 1, 0, 0x3000, 0x3100, 0x3200, 0))
        response(9)
        request(18, _STATE.pack(1, 1))  # SET_VRING_ENABLE
        response(18)

        # One writable chain with two pieces, posted on RX queue 1.
        struct.pack_into("<QIHH", memory, 0, 0x1300, 2, 3, 1)
        struct.pack_into("<QIHH", memory, 16, 0x1400, 4, 2, 0)
        struct.pack_into("<HHH", memory, 0x200, 0, 1, 0)
        assert request(35, _STATE.pack(1, 0)) == (1,)  # VRING_KICK

        buffer = backend.pop(1)
        assert buffer is not None
        assert buffer.capacity == 6
        backend.complete(1, buffer, buffer.write(b"event"))
        assert memory[0x300:0x302] == b"ev"
        assert memory[0x400:0x403] == b"ent"
        assert struct.unpack_from("<HHII", memory, 0x100) == (0, 1, 0, 5)
        assert backend.pop(1) is None
        response(35)

        assert backend.has_notifications
        assert not backend.notification_pending
        assert backend.write_fds == ()
        backend.flush_notifications()
        assert not backend.has_notifications
        assert backend.notification_pending
        backend.service((), backend.write_fds)
        assert guest_calls.recv(20) == _HEADER.pack(4, 9, 8) + _STATE.pack(1, 0)
        guest_calls.sendall(_HEADER.pack(4, 5, 8) + _ACK)
        backend.service(backend.read_fds)
        assert not backend.notification_pending

        # Leave a completed buffer's notification unsent when replacing its ring.
        struct.pack_into("<HHHH", memory, 0x200, 0, 2, 0, 0)
        assert request(35, _STATE.pack(1, 0)) == (1,)
        buffer = backend.pop(1)
        assert buffer is not None
        backend.complete(1, buffer, buffer.write(b"again"))
        response(35)
        assert backend.has_notifications
        assert not backend.notification_pending

        request(18, _STATE.pack(1, 0))  # SET_VRING_ENABLE
        response(18)
        request(5, memory_table, fd)  # SET_MEM_TABLE
        response(5)
        assert not backend.has_notifications
        assert not backend.notification_pending
        backend.flush_notifications()
        assert backend.write_fds == ()
