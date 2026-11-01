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

"""Run BSWorld asyncio code against wall-clock or BabbleSim time.

The discrete clock protocol and the selector that advances it live in
bscontroller. Only the idle step size differs here: the pytest loop trades
idle I/O latency for fewer PHY round trips, while each Controller worker keeps
bscontroller's finer default.
"""

from bscontroller.runtime.event_loop import RadioEventLoop as BsimEventLoop

from .babblesim import BsimClock

# Longest simulated interval the idle pytest loop may skip in one PHY request.
PYTEST_MAX_STEP_US = 10_000

__all__ = [
    "PYTEST_MAX_STEP_US",
    "BsimClock",
    "BsimEventLoop",
]
