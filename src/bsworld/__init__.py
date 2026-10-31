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
"""Deterministic simulation test framework for Bluetooth LE Applications."""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

# bsworld loads bscontroller from its source in this repository.
_BSCONTROLLER_SRC = Path(__file__).parents[2] / "components" / "bscontroller" / "src"
if _BSCONTROLLER_SRC.is_dir():
    sys.path.insert(0, str(_BSCONTROLLER_SRC))

try:
    __version__ = version("bsworld")
except PackageNotFoundError:
    __version__ = "0+unknown"
