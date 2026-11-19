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

from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path

import pytest
from tests.unit.runtime.scheduler.support import ManualClock, run_on_clock

from bsworld.runtime.pipe import LogCapture


def open_capture(tmp_path) -> LogCapture:  # noqa: ANN001
    return LogCapture.open(tmp_path / "device.log")


def test_capture_open_failure_releases_fifo_and_descriptors(
    tmp_path, monkeypatch
) -> None:
    log_path = tmp_path / "device.log"
    log_path.mkdir()
    fifo_path = log_path.with_suffix(".log.fifo")
    descriptors = []
    open_fd = os.open

    def track_fifo(path, flags, *args, **kwargs):
        fd = open_fd(path, flags, *args, **kwargs)
        if Path(path) == fifo_path:
            descriptors.append(fd)
        return fd

    monkeypatch.setattr(os, "open", track_fifo)

    with pytest.raises(IsADirectoryError):
        LogCapture.open(log_path)

    assert len(descriptors) == 2
    assert not fifo_path.exists()
    for descriptor in descriptors:
        with pytest.raises(OSError) as exc_info:
            os.fstat(descriptor)
        assert exc_info.value.errno == errno.EBADF


async def test_wait_log_returns_matching_complete_line(tmp_path) -> None:
    capture = open_capture(tmp_path)
    try:
        capture.log_fh.write(b"first line\nmiddle target line\nlast line\n")

        assert await capture.wait_log(r"target", timeout=0.2) == "middle target line"
    finally:
        capture.stop()


async def test_wait_log_copies_fifo_output_to_artifact_file(tmp_path) -> None:
    log_path = tmp_path / "device.log"
    capture = LogCapture.open(log_path)
    try:
        capture.log_fh.write(b"first line\ntarget line\n")

        assert await capture.wait_log(r"target", timeout=0.2) == "target line"
        assert log_path.read_text() == "first line\ntarget line\n"
    finally:
        capture.stop()


async def test_wait_log_continues_after_previous_match(tmp_path) -> None:
    capture = open_capture(tmp_path)
    try:
        capture.log_fh.write(b"first target line\nsecond target line\n")

        assert await capture.wait_log(r"target", timeout=0.2) == "first target line"
        assert await capture.wait_log(r"target", timeout=0.2) == "second target line"
    finally:
        capture.stop()


async def test_wait_log_does_not_rescan_consumed_lines(tmp_path) -> None:
    capture = open_capture(tmp_path)
    try:
        capture.log_fh.write(b"target once\n")

        assert await capture.wait_log(r"target", timeout=0.2) == "target once"
        with pytest.raises(TimeoutError):
            await capture.wait_log(r"target", timeout=0.05)
    finally:
        capture.stop()


async def test_wait_log_waits_for_split_line_terminator(tmp_path) -> None:
    capture = open_capture(tmp_path)
    waiter = asyncio.create_task(capture.wait_log(r"target", timeout=1.0))

    try:
        await asyncio.sleep(0.01)
        capture.log_fh.write(b"prefix tar")
        await asyncio.sleep(0.01)
        assert not waiter.done()

        capture.log_fh.write(b"get suffix\n")

        assert await waiter == "prefix target suffix"
    finally:
        capture.stop()
        if not waiter.done():
            waiter.cancel()


async def test_wait_log_does_not_match_unterminated_line(tmp_path) -> None:
    capture = open_capture(tmp_path)
    waiter = asyncio.create_task(capture.wait_log(r"target", timeout=1.0))

    try:
        capture.log_fh.write(b"partial target")
        await asyncio.sleep(0.02)
        assert not waiter.done()

        capture.log_fh.write(b" line\n")

        assert await waiter == "partial target line"
    finally:
        capture.stop()
        if not waiter.done():
            waiter.cancel()


async def test_wait_log_timeout_reports_pattern_and_path(tmp_path) -> None:
    capture = open_capture(tmp_path)
    try:
        capture.log_fh.write(b"first line\nsecond line\n")

        with pytest.raises(TimeoutError) as error:
            await capture.wait_log(r"missing", timeout=0.05)

        assert "wait_log('missing') timed out after 0.05s" in str(error.value)
        assert str(capture.log_path) in str(error.value)
    finally:
        capture.stop()


def test_wait_log_line_notification_prevents_an_extra_simulation_step(tmp_path) -> None:
    capture = open_capture(tmp_path)
    clock = ManualClock()

    def emit_line(_now_us: int, _target_us: int) -> None:
        capture.log_fh.write(b"ready\n")

    clock.before_advance = emit_line
    try:
        assert (
            run_on_clock(capture.wait_log(r"ready", timeout=1.0), clock=clock)
            == "ready"
        )
        assert clock.targets_us == [10_000]
    finally:
        capture.stop()
