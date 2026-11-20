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

import errno
import os
from pathlib import Path

import pytest
from tests.unit.helpers import bind_test_clock

from bsworld.runtime.pipe import LogCapture


@pytest.fixture
async def capture(tmp_path):
    capture = LogCapture.open(tmp_path / "device.log")
    try:
        yield capture
    finally:
        capture.stop()


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


async def test_wait_log_consumes_each_matching_line_and_records_output(capture) -> None:
    output = b"first line\nfirst target line\nsecond target line\nlast line\n"
    capture.log_fh.write(output)

    assert await capture.wait_log(r"target", timeout=0) == "first target line"
    assert await capture.wait_log(r"target", timeout=0) == "second target line"
    with pytest.raises(TimeoutError):
        await capture.wait_log(r"target", timeout=0)
    assert capture.log_path.read_bytes() == output


async def test_wait_log_wakes_at_the_advance_that_completes_the_line(capture) -> None:
    capture.log_fh.write(b"prefix target")

    def complete_line() -> None:
        capture.log_fh.write(b" suffix\n")

    with bind_test_clock(complete_line) as targets:
        assert await capture.wait_log(r"target", timeout=1.0) == "prefix target suffix"

    assert len(targets) == 1


async def test_wait_log_timeout_reports_pattern_and_path(capture) -> None:
    capture.log_fh.write(b"first line\nsecond line\n")

    with bind_test_clock(lambda: None):
        with pytest.raises(TimeoutError) as error:
            await capture.wait_log(r"missing", timeout=0.05)

    assert "wait_log('missing') timed out after 0.05s" in str(error.value)
    assert str(capture.log_path) in str(error.value)


async def test_capture_stop_preserves_unread_output_and_removes_fifo(capture) -> None:
    fifo_path = capture.log_path.with_suffix(".log.fifo")
    capture.log_fh.write(b"complete line\nunterminated tail")

    capture.stop()
    capture.stop()

    assert capture.log_fh.closed
    assert not fifo_path.exists()
    assert capture.log_path.read_bytes() == b"complete line\nunterminated tail"
