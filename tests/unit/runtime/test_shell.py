# Copyright 2026 Xiaomi Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Unit tests for process shell command input."""

from __future__ import annotations

import asyncio
import fcntl
import io
import os

import pytest
from tests.unit.runtime.scheduler.support import ManualClock, run_on_clock

from bsworld.runtime.shell import ProcessShell


class _Popen:
    def __init__(self, stdin: object | None) -> None:
        self.stdin = stdin


class _Proc:
    def __init__(self, stdin: object | None) -> None:
        self.popen = _Popen(stdin)


@pytest.fixture
def console_pipe():
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    shell = ProcessShell(label="dev0", enabled=True)
    with os.fdopen(read_fd, "rb", buffering=0) as output:
        with os.fdopen(write_fd, "wb", buffering=0) as stdin:
            shell.attach(_Proc(stdin))
            yield shell, stdin, output


async def test_process_shell_run_writes_line_to_stdin(console_pipe) -> None:
    shell, _, output = console_pipe
    await shell.run("ifup bnep0")
    await shell.run("blecent\n")

    assert output.read() == b"ifup bnep0\nblecent\n"


async def test_process_shell_run_many_is_noop_for_empty_commands() -> None:
    shell = ProcessShell(label="dev0", enabled=False)

    await shell.run_many(())


def test_process_shell_yields_on_full_pipe_and_preserves_order(console_pipe) -> None:
    shell, stdin, output = console_pipe
    capacity = fcntl.fcntl(stdin, fcntl.F_SETPIPE_SZ, 4096)
    # A blocking implementation fails the output check without hanging pytest.
    os.set_blocking(stdin.fileno(), False)
    commands = ("true",) * (capacity // 5 + 20)
    received = bytearray()

    def drain(_before: int, _after: int) -> None:
        received.extend(output.read() or b"")

    clock = ManualClock(before_advance=drain)

    async def send() -> None:
        await asyncio.wait_for(
            asyncio.gather(shell.run_many(commands), shell.run("echo ready")),
            timeout=0.1,
        )

    run_on_clock(send(), clock=clock)
    received.extend(output.read() or b"")

    assert received == b"true\n" * len(commands) + b"echo ready\n"
    assert clock.targets_us == [10_000]


def test_process_shell_sends_lines_synchronously_outside_an_event_loop() -> None:
    stdin = io.BytesIO()
    shell = ProcessShell(label="dev0", enabled=True)
    shell.attach(_Proc(stdin))

    shell.send_many(("ifup bnep0", "blecent"))

    assert stdin.getvalue() == b"ifup bnep0\nblecent\n"


async def test_process_shell_rejects_blocking_calls_in_event_loop(console_pipe) -> None:
    shell, _, output = console_pipe

    with pytest.raises(RuntimeError, match=r"use await shell\.run"):
        shell.send("ifup bnep0")

    assert output.read() is None


def test_process_shell_rejects_disabled_input() -> None:
    shell = ProcessShell(label="dev0", enabled=False)

    with pytest.raises(RuntimeError, match="shell input is not enabled"):
        shell.send("ifup bnep0")


def test_process_shell_rejects_unattached_input() -> None:
    shell = ProcessShell(label="dev0", enabled=True)

    with pytest.raises(RuntimeError, match="shell input is not available"):
        shell.send("ifup bnep0")
