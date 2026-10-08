# -*- coding: utf-8 -*-
"""登录限流状态必须持久化、过期并且不保存原始标识。"""

from datetime import datetime, timedelta, timezone

from core import db
from core.login_throttle import clear, record_failure, retry_after, scope_key


def test_login_throttle_blocks_then_expires_without_storing_identity(tmp_path):
    original = db.DB_PATH
    db.DB_PATH = str(tmp_path / "app.db")
    try:
        db.init_db()
        current = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
        for attempt in range(5):
            wait = record_failure("203.0.113.8", "Owner.Admin", now=current)
            assert wait == (900 if attempt == 4 else 0)
        assert retry_after("203.0.113.8", "owner.admin", now=current) == 900
        assert retry_after(
            "203.0.113.8", "owner.admin", now=current + timedelta(seconds=901)
        ) == 0

        connection = db.get_connection()
        try:
            row = connection.execute(
                "SELECT scope_key FROM auth_login_attempts"
            ).fetchone()
        finally:
            connection.close()
        assert row[0] == scope_key("203.0.113.8", "owner.admin")
        assert "203.0.113.8" not in row[0]
        assert "owner.admin" not in row[0]
    finally:
        db.DB_PATH = original


def test_success_clear_only_removes_matching_source_and_account(tmp_path):
    original = db.DB_PATH
    db.DB_PATH = str(tmp_path / "app.db")
    try:
        db.init_db()
        for _ in range(5):
            record_failure("203.0.113.8", "owner.admin")
            record_failure("203.0.113.9", "owner.admin")
        clear("203.0.113.8", "owner.admin")
        assert retry_after("203.0.113.8", "owner.admin") == 0
        assert retry_after("203.0.113.9", "owner.admin") > 0
    finally:
        db.DB_PATH = original
