# -*- coding: utf-8 -*-
"""同一 SQLite 数据库不得被多个 Web 进程同时管理。"""

import os
import subprocess
import sys
from pathlib import Path

from core.process_lock import (
    acquire_single_process_lock,
    is_single_process_lock_held,
    release_single_process_lock,
)


ROOT = Path(__file__).resolve().parents[1]


def test_process_lock_is_idempotent_in_owner_process(tmp_path):
    database = str(tmp_path / "app.db")
    try:
        first = acquire_single_process_lock(database)
        second = acquire_single_process_lock(database)
        assert first == second
        assert is_single_process_lock_held(database) is True
    finally:
        release_single_process_lock(database)
    assert is_single_process_lock_held(database) is False


def test_process_lock_rejects_second_process_before_startup_writes(tmp_path):
    database = str(tmp_path / "app.db")
    try:
        acquire_single_process_lock(database)
        code = (
            "from core.process_lock import acquire_single_process_lock;"
            "acquire_single_process_lock(%r)" % database
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT)
        environment["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
        assert result.returncode != 0
        assert "single worker" in result.stderr
    finally:
        release_single_process_lock(database)
