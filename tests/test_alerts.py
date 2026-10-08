# -*- coding: utf-8 -*-
"""故障事件先持久化，再安全投递，且不能重复刷屏。"""

from unittest import mock

from core import db
from core.alerts import dispatch_pending, pending_alert_count, record_alert


def test_alert_outbox_deduplicates_and_redacts(tmp_path):
    original = db.DB_PATH
    db.DB_PATH = str(tmp_path / "app.db")
    try:
        db.init_db()
        assert record_alert(
            "collection.failed",
            "error",
            "采集失败",
            "password=secret-value token=abc123",
            dedupe_key="collection:one",
        ) is True
        assert record_alert(
            "collection.failed",
            "error",
            "重复",
            "重复",
            dedupe_key="collection:one",
        ) is False
        assert pending_alert_count() == 1
        connection = db.get_connection()
        try:
            message = connection.execute("SELECT message FROM alert_events").fetchone()[0]
        finally:
            connection.close()
        assert "secret-value" not in message
        assert "abc123" not in message
    finally:
        db.DB_PATH = original


def test_dispatch_requires_https_and_marks_success(tmp_path):
    original = db.DB_PATH
    db.DB_PATH = str(tmp_path / "app.db")
    try:
        db.init_db()
        record_alert(
            "backup.failed",
            "critical",
            "备份失败",
            "磁盘空间不足",
            dedupe_key="backup:one",
        )
        with mock.patch("core.alerts.requests.post") as post:
            assert dispatch_pending(webhook_url="http://insecure.example/hook") == 0
            post.assert_not_called()

            post.return_value.raise_for_status.return_value = None
            assert dispatch_pending(webhook_url="https://alerts.example/hook") == 1
            post.assert_called_once()
        assert pending_alert_count() == 0
        connection = db.get_connection()
        try:
            row = connection.execute(
                "SELECT status, attempts FROM alert_events"
            ).fetchone()
        finally:
            connection.close()
        assert tuple(row) == ("sent", 1)
    finally:
        db.DB_PATH = original
