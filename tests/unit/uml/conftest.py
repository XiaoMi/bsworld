# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def uml_vhost_library(tmp_path_factory: pytest.TempPathFactory) -> Path:
    source_dir = Path(__file__).resolve().parents[3] / "components" / "uml"
    out_dir = tmp_path_factory.mktemp("uml-vhost")
    subprocess.run(
        ["make", "-C", str(source_dir), f"OUT_DIR={out_dir}"],
        check=True,
    )
    return out_dir / "libbsworld_vhost.so"
