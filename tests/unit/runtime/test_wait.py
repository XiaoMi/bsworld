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
"""Unit tests for deadline-based wait helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bsworld.runtime import wait as wait_mod
from bsworld.runtime.wait import (
    wait_for,
    wait_for_path,
    wait_for_paths,
    wait_for_paths_async,
)


def test_wait_for_polls_until_predicate_true(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        wait_mod, "time", SimpleNamespace(monotonic=lambda: 0, sleep=sleeps.append)
    )
    ready = iter([False, False, True])

    wait_for(lambda: next(ready), timeout=1.0, poll=0.01)

    assert sleeps == [0.01, 0.01]


def test_wait_for_limits_polling_to_the_remaining_deadline(monkeypatch) -> None:
    sleeps: list[float] = []
    times = iter([0.0, 0.75, 1.0])
    monkeypatch.setattr(
        wait_mod,
        "time",
        SimpleNamespace(monotonic=lambda: next(times), sleep=sleeps.append),
    )

    with pytest.raises(TimeoutError, match="waiting for my-thing"):
        wait_for(lambda: False, timeout=1.0, poll=0.5, desc="my-thing")

    assert sleeps == [0.25]


def test_sync_path_wait_accepts_symlinks_and_requires_all_paths(tmp_path) -> None:
    link = tmp_path / "ready.lock"
    link.symlink_to(tmp_path / "not-yet-created")
    wait_for_path(link, timeout=0)

    missing = tmp_path / "missing.sock"
    with pytest.raises(TimeoutError, match=r"missing\.sock"):
        wait_for_paths([link, missing], timeout=0)

    missing.touch()
    wait_for_paths([link, missing], timeout=0)


async def test_async_path_wait_allows_other_tasks_to_create_all_paths(tmp_path):
    paths = [tmp_path / "first", tmp_path / "second"]
    waiter = asyncio.create_task(wait_for_paths_async(paths, timeout=1, poll=0.001))
    try:
        paths[0].touch()
        await asyncio.sleep(0)
        assert not waiter.done()
        paths[1].touch()
        await waiter
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def test_async_path_wait_times_out_with_missing_path(tmp_path):
    missing = tmp_path / "missing.sock"
    with pytest.raises(TimeoutError, match=r"missing\.sock"):
        await wait_for_paths_async([missing], timeout=0)


async def test_async_path_wait_can_be_cancelled(tmp_path):
    missing = tmp_path / "missing.sock"
    task = asyncio.create_task(wait_for_paths_async([missing], timeout=1))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
