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
"""Controller identity and address contract tests."""

import pytest

from bsworld.devices import BSController


def _controller(tmp_path, address: str | None = None) -> BSController:
    return BSController(0, "sim", tmp_path, public_address=address)


def test_controller_normalizes_public_address(tmp_path) -> None:
    assert (
        _controller(tmp_path, "c0:ff:ee:00:00:03").public_address == "C0:FF:EE:00:00:03"
    )


@pytest.mark.parametrize(
    "address",
    [
        "C0:FF:EE:00:00",
        "C0:FF:EE:00:00:01:02",
        "C0:FF:EE:00:00:GG",
        "C0:FF:EE:00:00:-1",
        "C0:FF:EE:00:00:+1",
        "C0:FF:EE:00:00: 1",
        "C0:FF:EE:00:00:０１",
        "00:00:00:00:00:00",
    ],
)
def test_controller_rejects_invalid_public_address(tmp_path, address: str) -> None:
    with pytest.raises(ValueError, match="controller address"):
        _controller(tmp_path, address)
