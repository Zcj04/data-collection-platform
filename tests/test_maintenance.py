# -*- coding: utf-8 -*-
"""数据保留清理（core/maintenance.py）单元测试。"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import core.db as db


class MaintenanceCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib

        cls._temp_dir = tempfile.TemporaryDirectory(prefix="workbuddy-maintenance-")
        cls._temp_root = Path(cls._temp_dir.name)
        cls._original_db_path = db.DB_PATH
        db.DB_PATH = str(cls._temp_root / "app.db")
        sys.modules.pop("core.maintenance", None)
        cls.module = importlib.import_module("core.maintenance")
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("core.maintenance", None)
        db.DB_PATH = cls._original_db_path
        cls._temp_dir.cleanup()

    def _insert_fixtures(self, conn, old_date, recent_date):
        for date_value, platform in ((old_date, "meituan"), (recent_date, "douyin")):
            conn.execute(
                "INSERT INTO tasks (id, platform, date, status) VALUES (?,?,?,'success')",
                ("task-%s" % platform, platform, date_value),
            )
            conn.execute(
                "INSERT INTO daily_summary (id, date, venue, platform, metrics_json) "
                "VALUES (?,?,?,?,?)",
                ("sum-%s" % platform, date_value, "测试门店", platform, "{}"),
            )
            conn.execute(
                "INSERT INTO scheduled_collection_runs (id, job_key, slot_key, slot_label, "
                "target_date, scheduled_for, status) VALUES (?,?,?,?,?,?,'success')",
                ("run-%s" % platform, "job-%s" % platform, platform, platform,
                 date_value, date_value + "T10:00:00"),
            )

    def test_cleanup_deletes_only_expired_rows(self):
        from core.db import get_connection

        now = datetime(2026, 9, 26, 23, 40)
        old_date = (now - timedelta(days=200)).strftime("%Y-%m-%d")
        recent_date = (now - timedelta(days=10)).strftime("%Y-%m-%d")
        conn = get_connection()
        try:
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM daily_summary")
            conn.execute("DELETE FROM scheduled_collection_runs")
            self._insert_fixtures(conn, old_date, recent_date)
            conn.commit()
        finally:
            conn.close()

        result = self.module.cleanup_once(
            now,
            config={"task_days": 180, "run_days": 150, "log_days": 0, "daily_summary_days": 0},
            log_dir=self._temp_root,
        )

        self.assertEqual(result.get("tasks"), 1)
        self.assertEqual(result.get("scheduled_runs"), 1)
        self.assertNotIn("daily_summary", result)  # 默认永久保留

        conn = get_connection()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM daily_summary").fetchone()["c"], 2)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM scheduled_collection_runs").fetchone()["c"], 1)
        finally:
            conn.close()

    def test_cleanup_respects_daily_summary_retention(self):
        from core.db import get_connection

        now = datetime(2026, 9, 26, 23, 40)
        old_date = (now - timedelta(days=800)).strftime("%Y-%m-%d")
        recent_date = (now - timedelta(days=10)).strftime("%Y-%m-%d")
        conn = get_connection()
        try:
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM daily_summary")
            conn.execute("DELETE FROM scheduled_collection_runs")
            self._insert_fixtures(conn, old_date, recent_date)
            conn.commit()
        finally:
            conn.close()

        self.module.cleanup_once(
            now,
            config={"task_days": 0, "run_days": 0, "log_days": 0, "daily_summary_days": 730},
            log_dir=self._temp_root,
        )

        conn = get_connection()
        try:
            rows = {
                row["date"]
                for row in conn.execute("SELECT date FROM daily_summary").fetchall()
            }
        finally:
            conn.close()
        self.assertEqual(rows, {recent_date})

    def test_cleanup_log_files(self):
        now = datetime.now()
        old_log = self._temp_root / "old.log"
        recent_log = self._temp_root / "recent.log"
        rotated = self._temp_root / "old.log.1"
        old_log.write_text("x", encoding="utf-8")
        recent_log.write_text("x", encoding="utf-8")
        rotated.write_text("x", encoding="utf-8")
        old_timestamp = (now - timedelta(days=60)).timestamp()
        import os

        os.utime(old_log, (old_timestamp, old_timestamp))
        os.utime(rotated, (old_timestamp, old_timestamp))

        result = self.module.cleanup_once(
            now, config={"task_days": 0, "run_days": 0, "log_days": 30}, log_dir=self._temp_root
        )

        self.assertEqual(result.get("log_files"), 2)
        self.assertFalse(old_log.exists())
        self.assertFalse(rotated.exists())
        self.assertTrue(recent_log.exists())


if __name__ == "__main__":
    unittest.main()
