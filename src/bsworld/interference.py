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
"""Declarative interference-source models for BabbleSim 2.4 GHz PHY.

These dataclasses are consumed by BabbleSimBackend.start() to spawn the
appropriate bs_device_* processes and configure the PHY channel model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from numbers import Real
from pathlib import Path
from typing import Final, Literal, get_args

from .devices import BSController

InterfType = Literal[
    "CW",
    "WLAN",
    "BLE",
    "WN1",
    "WN2",
    "WN4",
    "WN8",
    "WN16",
    "WN20",
    "WN40",
    "WN80",
]

WlanConfigSet = Literal[
    "VoIP",
    "VideoConf",
    "FileDownload",
    "FileDownload_24M",
    "FileDownload_12M",
    "FileDownload_6M",
    "100",
    "100_24M",
    "100_12M",
    "100_6M",
    "50",
    "50_24M",
    "50_12M",
    "50_6M",
    "25",
    "25_24M",
    "25_12M",
    "25_6M",
    "10",
    "10_24M",
    "10_12M",
    "10_6M",
    "5",
]

# Derived from the annotations above so the accepted values are declared once.
SUPPORTED_INTERF_TYPES: Final[tuple[str, ...]] = get_args(InterfType)
SUPPORTED_WLAN_CONFIG_SETS: Final[tuple[str, ...]] = get_args(WlanConfigSet)
BSIM_CENTER_FREQ_MIN_MHZ: Final = 2273.0
BSIM_CENTER_FREQ_MAX_MHZ: Final = 2527.0
BSIM_POWER_MIN_DBM: Final = -128.0
BSIM_POWER_MAX_DBM: Final = 127.0
BSIM_INTERFERENCE_MAX_WINDOWS: Final = 256
BSIM_WLAN_CHANNEL_MIN: Final = 1
BSIM_WLAN_CHANNEL_MAX: Final = 14
BSIM_CHANNEL_ATTENUATION_MIN_DB: Final = -100.0
BSIM_CHANNEL_ATTENUATION_MAX_DB: Final = 100.0
BSIM_RANDOM_SEED_MAX: Final = 0xFFFFFFFF


def _finite_real(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _validate_center_freq_mhz(name: str, value: object) -> None:
    freq = _finite_real(name, value)
    if not (BSIM_CENTER_FREQ_MIN_MHZ < freq < BSIM_CENTER_FREQ_MAX_MHZ):
        raise ValueError(
            f"{name} must be > {BSIM_CENTER_FREQ_MIN_MHZ} and < "
            f"{BSIM_CENTER_FREQ_MAX_MHZ} MHz"
        )


def _validate_power_dbm(name: str, value: object) -> None:
    power = _finite_real(name, value)
    if not (BSIM_POWER_MIN_DBM <= power <= BSIM_POWER_MAX_DBM):
        raise ValueError(
            f"{name} must be between {BSIM_POWER_MIN_DBM} and {BSIM_POWER_MAX_DBM} dBm"
        )


def _validate_choice(name: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        choices = ", ".join(allowed)
        raise ValueError(f"{name} must be one of: {choices}")


def _validate_random_seed(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an unsigned 32-bit integer")
    if not (0 <= value <= BSIM_RANDOM_SEED_MAX):
        raise ValueError(f"{name} must be between 0 and {BSIM_RANDOM_SEED_MAX}")


def _integer_time_us(name: str, value: object) -> int:
    time_us = _finite_real(name, value)
    if time_us < 0:
        raise ValueError(f"{name} must be >= 0 us")
    if not time_us.is_integer():
        raise ValueError(f"{name} must be an integer number of microseconds")
    return int(time_us)


def _validate_windows(
    name: str, windows: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    if len(windows) > BSIM_INTERFERENCE_MAX_WINDOWS:
        raise ValueError(
            f"{name} supports at most {BSIM_INTERFERENCE_MAX_WINDOWS} windows"
        )

    normalized: list[tuple[int, int]] = []
    previous_end = 0
    for index, window in enumerate(windows):
        try:
            start_raw, end_raw = window
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{name}[{index}] must be a (start_us, end_us) pair"
            ) from exc
        start = _integer_time_us(f"{name}[{index}].start_us", start_raw)
        end = _integer_time_us(f"{name}[{index}].end_us", end_raw)
        if start <= 0:
            raise ValueError(f"{name}[{index}].start_us must be > 0 us")
        if end <= start:
            raise ValueError(f"{name}[{index}].end_us must be greater than start_us")
        if start <= previous_end:
            raise ValueError(f"{name} must be strictly ordered and non-overlapping")
        normalized.append((start, end))
        previous_end = end
    return normalized


def _validate_channel_attenuation(name: str, value: object) -> None:
    attenuation = _finite_real(name, value)
    if not (
        BSIM_CHANNEL_ATTENUATION_MIN_DB
        <= attenuation
        <= BSIM_CHANNEL_ATTENUATION_MAX_DB
    ):
        raise ValueError(
            f"{name} must be between {BSIM_CHANNEL_ATTENUATION_MIN_DB} and "
            f"{BSIM_CHANNEL_ATTENUATION_MAX_DB} dB"
        )


@dataclass(frozen=True, eq=False)
class InterferenceRef:
    """Stable reference to an interference source registered on a topology."""

    index: int


PathEndpoint = int | BSController | InterferenceRef


@dataclass
class BurstInterference:
    """Burst interferer: fixed-frequency, optionally gated by time windows.

    Maps to bs_device_2G4_burst_interf.
    windows: list of (start_us, end_us) pairs; empty = run for entire simulation.
    """

    center_freq_mhz: float
    power_dbm: float = 20.0
    interf_type: InterfType = "CW"
    windows: list[tuple[int, int]] = field(default_factory=list)
    rand_seed: int | None = None

    def __post_init__(self) -> None:
        _validate_center_freq_mhz(
            "BurstInterference.center_freq_mhz", self.center_freq_mhz
        )
        _validate_power_dbm("BurstInterference.power_dbm", self.power_dbm)
        _validate_choice(
            "BurstInterference.interf_type", self.interf_type, SUPPORTED_INTERF_TYPES
        )
        self.windows = _validate_windows("BurstInterference.windows", self.windows)
        _validate_random_seed("BurstInterference.rand_seed", self.rand_seed)


@dataclass
class WlanInterference:
    """WLAN activity model: realistic WiFi channel occupancy pattern.

    Maps to bs_device_2G4_WLAN_actmod.
    Specify either channel (1-14) or center_freq_mhz, not both.
    windows: list of (start_us, end_us) pairs; empty = run for entire simulation.
    """

    channel: int | None = None
    center_freq_mhz: float | None = None
    power_dbm: float = 20.0
    config_set: WlanConfigSet = "5"
    mod_type: InterfType = "WLAN"
    windows: list[tuple[int, int]] = field(default_factory=list)
    rand_seed: int | None = None

    def __post_init__(self) -> None:
        if self.channel is None and self.center_freq_mhz is None:
            raise ValueError(
                "WlanInterference: specify either channel or center_freq_mhz"
            )
        if self.channel is not None and self.center_freq_mhz is not None:
            raise ValueError(
                "WlanInterference: specify either channel or center_freq_mhz, not both"
            )
        if self.channel is not None:
            if isinstance(self.channel, bool) or not isinstance(self.channel, int):
                raise ValueError("WlanInterference.channel must be an integer")
            if not (BSIM_WLAN_CHANNEL_MIN <= self.channel <= BSIM_WLAN_CHANNEL_MAX):
                raise ValueError(
                    f"WlanInterference.channel must be between {BSIM_WLAN_CHANNEL_MIN} "
                    f"and {BSIM_WLAN_CHANNEL_MAX}"
                )
        if self.center_freq_mhz is not None:
            _validate_center_freq_mhz(
                "WlanInterference.center_freq_mhz", self.center_freq_mhz
            )
        _validate_power_dbm("WlanInterference.power_dbm", self.power_dbm)
        _validate_choice(
            "WlanInterference.config_set", self.config_set, SUPPORTED_WLAN_CONFIG_SETS
        )
        _validate_choice(
            "WlanInterference.mod_type", self.mod_type, SUPPORTED_INTERF_TYPES
        )
        self.windows = _validate_windows("WlanInterference.windows", self.windows)
        _validate_random_seed("WlanInterference.rand_seed", self.rand_seed)


@dataclass
class PlaybackInterference:
    """Replay recorded interference from BabbleSim CSV dumps.

    Maps to bs_device_2G4_playbackv2.
    input_file: path prefix (without extension); expects .Tx.csv / .Rx.csv /
    .RSSI.csv / .CCA.csv alongside.
    """

    input_file: Path
    tx: bool = True
    rx: bool = True
    rssi: bool = True
    cca: bool = True


@dataclass
class TimedAttenuation:
    """Per-path time-varying attenuation for lib_2G4Channel_multiatt.

    Describes how the attenuation between a specific (tx, rx) device pair
    changes over simulation time. The PHY linearly interpolates between
    consecutive (time, attenuation) points.

    tx / rx: BabbleSim dev_id, a registered Controller, or
        InterferenceRef.
    times_us: simulation timestamps in microseconds, strictly ascending.
    attenuation_db: attenuation value at the corresponding timestamp.
    """

    tx: PathEndpoint
    rx: PathEndpoint
    times_us: list[int]
    attenuation_db: list[float]

    def __post_init__(self) -> None:
        if len(self.times_us) != len(self.attenuation_db):
            raise ValueError(
                (
                    "TimedAttenuation: times_us and attenuation_db must have the same "
                    "length"
                )
            )
        if len(self.times_us) < 2:
            raise ValueError("TimedAttenuation: at least 2 time points required")
        for index, time_us in enumerate(self.times_us):
            self.times_us[index] = _integer_time_us(
                f"TimedAttenuation.times_us[{index}]", time_us
            )
        if any(b <= a for a, b in zip(self.times_us, self.times_us[1:])):
            raise ValueError("TimedAttenuation: times_us must be strictly ascending")
        for index, attenuation_db in enumerate(self.attenuation_db):
            _finite_real(f"TimedAttenuation.attenuation_db[{index}]", attenuation_db)


@dataclass
class StaticAttenuation:
    """Fixed per-path attenuation for lib_2G4Channel_multiatt.

    tx / rx: BabbleSim dev_id, a registered Controller, or
        InterferenceRef.
    attenuation_db: fixed attenuation for that directed path.
    """

    tx: PathEndpoint
    rx: PathEndpoint
    attenuation_db: float

    def __post_init__(self) -> None:
        _finite_real("StaticAttenuation.attenuation_db", self.attenuation_db)


@dataclass
class ChannelAttenuation:
    """PHY channel attenuation model (lib_2G4Channel_multiatt).

    default_attenuation_db: applied to all NxN device pairs not covered by
        timed_paths or matrix_file.
    extra_attenuation_db: additional attenuation stacked on top of every path.
    matrix_file: path to a static NxN attenuation matrix file. Mutually
        exclusive with static_paths and timed_paths (use one mode or the other).
    static_paths: fixed per-path attenuation profiles.
    timed_paths: time-varying per-path attenuation profiles.
    """

    default_attenuation_db: float = 60.0
    extra_attenuation_db: float = 0.0
    matrix_file: Path | None = None
    static_paths: list[StaticAttenuation] = field(default_factory=list)
    timed_paths: list[TimedAttenuation] = field(default_factory=list)

    def __post_init__(self) -> None:
        _validate_channel_attenuation(
            "ChannelAttenuation.default_attenuation_db", self.default_attenuation_db
        )
        _validate_channel_attenuation(
            "ChannelAttenuation.extra_attenuation_db", self.extra_attenuation_db
        )
        if self.matrix_file is not None and (self.static_paths or self.timed_paths):
            raise ValueError(
                (
                    "ChannelAttenuation: use either matrix_file or generated paths, "
                    "not both"
                )
            )


InterferenceSource = BurstInterference | WlanInterference | PlaybackInterference

__all__ = [
    "InterfType",
    "PathEndpoint",
    "WlanConfigSet",
    "SUPPORTED_INTERF_TYPES",
    "SUPPORTED_WLAN_CONFIG_SETS",
    # The remaining BSIM_* limits stay module-private; they exist to build the
    # validation messages below rather than to be imported by tests.
    "BSIM_INTERFERENCE_MAX_WINDOWS",
    "BurstInterference",
    "WlanInterference",
    "PlaybackInterference",
    "ChannelAttenuation",
    "InterferenceRef",
    "StaticAttenuation",
    "TimedAttenuation",
    "InterferenceSource",
]
