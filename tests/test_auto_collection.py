# -*- coding: utf-8 -*-
"""经营数据自动采集的持久化排程契约测试。

可直接用 Python 运行，不依赖 pytest。所有测试只使用临时 SQLite、固定
时钟和假 launcher，不会启动后台线程，也不会调用真实爬虫。

生产契约：
- ``CollectionAutoScheduler(launcher=..., slots=..., enabled=...)``
- 调度检查方法可命名 ``trigger_due`` 或 ``_trigger_due_run``
- 完成方法可命名 ``complete_run`` / ``finish_run``
- 过期恢复方法可命名 ``expire_stale_runs`` / ``recover_stale_runs``
- ``get_collection_freshness(target_date, expected_platforms=...)`` 返回
  ``target_date/state/data_updated_at/oldest_data_updated_at/``
  ``sources_present/sources_expected/running_count/failed_count/``
  ``last_attempt_at/schedule``，且不得返回 ``error_msg``

兼容别名只用于降低并行开发时的方法命名耦合；状态、持久防重、数据新鲜
度和隐私边界均为强约束。
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _FakeLauncher:
    """同时兼容 callable 和 ``.launch()`` 形式的无网络启动器。"""

    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [True])
        self.calls = []
        self.callbacks = []
        self._lock = threading.Lock()

    def _invoke(self, *args, **kwargs):
        with self._lock:
            self.calls.append((args, dict(kwargs)))
            callback = kwargs.get("on_complete")
            if callback is not None:
                self.callbacks.append(callback)
            if self.outcomes:
                return self.outcomes.pop(0)
            return True

    def __call__(self, *args, **kwargs):
        return self._invoke(*args, **kwargs)

    def launch(self, *args, **kwargs):
        return self._invoke(*args, **kwargs)


class AutoCollectionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp_dir = tempfile.TemporaryDirectory(prefix="workbuddy-auto-collection-")
        cls._temp_root = Path(cls._temp_dir.name)

        import core.db as db

        cls.db = db
        cls._original_db_path = db.DB_PATH
        db.DB_PATH = str(cls._temp_root / "app.db")

        sys.modules.pop("core.auto_collection", None)
        cls.module = importlib.import_module("core.auto_collection")
        cls.db.init_db()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("core.auto_collection", None)
        cls.db.DB_PATH = cls._original_db_path
        cls._temp_dir.cleanup()

    def setUp(self):
        conn = self.db.get_connection()
        try:
            conn.execute("DELETE FROM scheduled_collection_runs")
            conn.execute("DELETE FROM daily_summary")
            conn.execute("DELETE FROM tasks")
            conn.commit()
        finally:
            conn.close()

    def _new_scheduler(self, launcher=None, *, enabled=True):
        launcher = launcher or _FakeLauncher()
        scheduler = self.module.CollectionAutoScheduler(
            launcher=launcher,
            slots=(
                {
                    "key": "today-test",
                    "label": "今日测试数据",
                    "at": "10:00",
                    "target_day_offset": 0,
                },
            ),
            enabled=enabled,
        )
        return scheduler, launcher

    @staticmethod
    def _trigger(scheduler, now):
        for name in ("trigger_due", "_trigger_due_run", "check_due"):
            method = getattr(scheduler, name, None)
            if method is not None:
                return method(now)
        raise AssertionError(
            "CollectionAutoScheduler 需要 trigger_due(now) "
            "或兼容的 _trigger_due_run/check_due"
        )

    def _complete(self, scheduler, job_key, platform_results, error_msg=""):
        launcher = getattr(scheduler, "_launcher", None)
        callbacks = getattr(launcher, "callbacks", None)
        if callbacks:
            total = len(platform_results)
            completed = sum(
                str(status).lower() == "success"
                for status in platform_results.values()
            )
            return callbacks[-1](
                {
                    "total": total,
                    "completed": completed,
                    "failed": total - completed,
                    "fatal_error": error_msg,
                }
            )
        candidates = (
            getattr(scheduler, "complete_run", None),
            getattr(scheduler, "finish_run", None),
            getattr(self.module, "complete_scheduled_run", None),
            getattr(self.module, "finish_scheduled_run", None),
        )
        method = next((item for item in candidates if item is not None), None)
        if method is None:
            raise AssertionError(
                "launcher 需要接收 on_complete，或提供 complete_run/finish_run"
            )
        try:
            return method(
                job_key,
                platform_results=platform_results,
                error_msg=error_msg,
            )
        except TypeError:
            return method(job_key, platform_results, error_msg)

    def _expire(self, scheduler, now):
        candidates = (
            getattr(scheduler, "expire_stale_runs", None),
            getattr(scheduler, "recover_stale_runs", None),
            getattr(self.module, "expire_stale_collection_runs", None),
            getattr(self.module, "recover_stale_collection_runs", None),
        )
        method = next((item for item in candidates if item is not None), None)
        if method is None:
            raise AssertionError(
                "需要 expire_stale_runs/recover_stale_runs 处理服务重启遗留任务"
            )
        for args, kwargs in (
            ((), {"now": now, "stale_after_seconds": 60 * 60}),
            ((now, 60 * 60), {}),
            ((now,), {}),
            ((), {}),
        ):
            try:
                return method(*args, **kwargs)
            except TypeError:
                continue
        raise AssertionError("过期恢复方法签名无法调用")

    def _rows(self):
        conn = self.db.get_connection()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM scheduled_collection_runs ORDER BY job_key"
                ).fetchall()
            ]
        finally:
            conn.close()

    def _row(self):
        rows = self._rows()
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def _freshness(self, target_date, expected_platforms):
        method = self.module.get_collection_freshness
        scheduler, _ = self._new_scheduler(enabled=True)

        def config_value(key, default=None):
            if key == "platforms":
                return {name: {} for name in expected_platforms}
            return default

        with mock.patch.object(self.module, "config_get", side_effect=config_value):
            try:
                return method(
                    target_date,
                    expected_platforms=expected_platforms,
                    scheduler=scheduler,
                )
            except TypeError:
                try:
                    return method(target_date, scheduler=scheduler)
                except TypeError:
                    return method(target_date, expected_platforms)

    def test_schema_has_durable_unique_job_key(self):
        conn = self.db.get_connection()
        try:
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(scheduled_collection_runs)"
                ).fetchall()
            }
            self.assertTrue(
                {"job_key", "target_date", "status", "started_at"} <= columns,
                columns,
            )

            unique_columns = set()
            for index in conn.execute(
                "PRAGMA index_list(scheduled_collection_runs)"
            ).fetchall():
                if not index[2]:
                    continue
                names = tuple(
                    row[2]
                    for row in conn.execute(
                        "PRAGMA index_info(%s)" % index[1]
                    ).fetchall()
                )
                unique_columns.add(names)
            self.assertIn(("job_key",), unique_columns)
        finally:
            conn.close()

    def test_before_due_does_not_start_and_disabled_never_starts(self):
        scheduler, launcher = self._new_scheduler()
        self.assertFalse(self._trigger(scheduler, datetime(2026, 8, 25, 9, 59)))
        self.assertEqual(launcher.calls, [])
        self.assertEqual(self._rows(), [])

    def test_default_schedule_collects_yesterday_and_intraday(self):
        """默认排程：10:00 昨日正式数据先行，10:30-22:30 每小时当日实时档。"""
        scheduler = self.module.CollectionAutoScheduler(enabled=True)
        by_key = {slot.key: slot for slot in scheduler._slots}
        self.assertEqual(by_key["yesterday-final"].target_day_offset, 1)
        intraday = [
            slot for key, slot in by_key.items() if key.startswith("intraday-")
        ]
        self.assertTrue(intraday)
        self.assertTrue(all(slot.target_day_offset == 0 for slot in intraday))
        self.assertTrue(all(10 <= slot.run_at.hour <= 22 for slot in intraday))
        self.assertEqual(len(intraday), 13)  # 每小时一档
        # 顺序契约：昨日正式数据先固化，实时档全部排在其后
        self.assertLess(
            by_key["yesterday-final"].run_at,
            min(slot.run_at for slot in intraday),
        )

        disabled, disabled_launcher = self._new_scheduler(enabled=False)
        self.assertFalse(self._trigger(disabled, datetime(2026, 8, 25, 10, 5)))
        self.assertEqual(disabled_launcher.calls, [])
        self.assertEqual(self._rows(), [])

    def test_due_or_missed_slot_starts_today_once(self):
        scheduler, launcher = self._new_scheduler()

        result = self._trigger(scheduler, datetime(2026, 8, 25, 10, 7))

        self.assertTrue(result)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(
            launcher.calls[0][0][:2],
            ("2026-08-01", "2026-08-25"),
        )
        row = self._row()
        self.assertEqual(row["target_date"], "2026-08-25")
        self.assertEqual(row["status"], "running")

        self.assertFalse(self._trigger(scheduler, datetime(2026, 8, 25, 10, 8)))
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(len(self._rows()), 1)

    def test_due_slot_backfills_an_internal_snapshot_gap_first(self):
        conn = self.db.get_connection()
        try:
            for target_date in ("2026-08-23", "2026-08-25"):
                conn.execute(
                    "INSERT INTO daily_summary "
                    "(id,date,venue,platform,metrics_json,period_start) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        "snapshot-%s" % target_date,
                        target_date,
                        "测试门店",
                        "meituan",
                        "{}",
                        "2026-08-01",
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        scheduler, launcher = self._new_scheduler()
        result = self._trigger(scheduler, datetime(2026, 8, 26, 10, 1))

        self.assertEqual(result, "catchup:2026-08-24")
        self.assertEqual(launcher.calls[0][0][:2], ("2026-08-01", "2026-08-24"))
        self.assertEqual(self._row()["slot_key"], "catchup")

    def test_yesterday_slot_uses_target_month_start_across_month_boundary(self):
        launcher = _FakeLauncher()
        scheduler = self.module.CollectionAutoScheduler(
            launcher=launcher,
            slots=(
                {
                    "key": "yesterday-final",
                    "label": "昨日正式数据",
                    "at": "10:10",
                    "target_day_offset": 1,
                },
            ),
            enabled=True,
        )

        result = self._trigger(scheduler, datetime(2026, 9, 1, 10, 10))

        self.assertTrue(result)
        self.assertEqual(
            launcher.calls[0][0][:2],
            ("2026-08-01", "2026-08-31"),
        )
        self.assertEqual(self._row()["target_date"], "2026-08-31")

    def test_two_scheduler_instances_compete_for_one_database_claim(self):
        launcher = _FakeLauncher([True, True])
        first, _ = self._new_scheduler(launcher)
        second, _ = self._new_scheduler(launcher)
        barrier = threading.Barrier(3)
        results = []
        errors = []

        def trigger(scheduler):
            try:
                barrier.wait()
                results.append(
                    self._trigger(scheduler, datetime(2026, 8, 25, 10, 1))
                )
            except Exception as error:  # pragma: no cover - reported by assertion
                errors.append(error)

        threads = [
            threading.Thread(target=trigger, args=(first,)),
            threading.Thread(target=trigger, args=(second,)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(errors, errors)
        self.assertEqual(sum(bool(item) for item in results), 1, results)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(len(self._rows()), 1)

    def test_busy_launcher_releases_claim_and_same_slot_can_retry(self):
        scheduler, launcher = self._new_scheduler(_FakeLauncher([False, True]))
        now = datetime(2026, 8, 25, 10, 2)

        self.assertFalse(self._trigger(scheduler, now))
        self.assertEqual(len(launcher.calls), 1)

        retried = self._trigger(scheduler, now + timedelta(seconds=10))
        self.assertTrue(retried)
        self.assertEqual(len(launcher.calls), 2)
        row = self._row()
        self.assertEqual(row["status"], "running")

    def test_completion_records_success_partial_and_failed(self):
        cases = (
            ({"a": "success", "b": "success"}, "success", ""),
            ({"a": "success", "b": "failed"}, "partial", "b timeout"),
            ({"a": "failed", "b": "failed"}, "failed", "all failed"),
        )
        for day, (platform_results, expected_status, error_msg) in enumerate(
            cases, start=25
        ):
            with self.subTest(expected_status=expected_status):
                conn = self.db.get_connection()
                try:
                    conn.execute("DELETE FROM scheduled_collection_runs")
                    conn.commit()
                finally:
                    conn.close()

                scheduler, _ = self._new_scheduler()
                self._trigger(scheduler, datetime(2026, 8, day, 10, 0))
                job_key = self._row()["job_key"]

                self._complete(
                    scheduler,
                    job_key,
                    platform_results,
                    error_msg=error_msg,
                )

                row = self._row()
                self.assertEqual(row["status"], expected_status)
                self.assertIsNotNone(row.get("finished_at"))

    def test_launcher_completion_callback_finishes_persisted_batch(self):
        class _CompletingLauncher:
            def __call__(self, start_date, end_date, on_complete):
                on_complete({"total": 3, "completed": 2, "failed": 1})
                return True

        scheduler, _ = self._new_scheduler(_CompletingLauncher())
        job_key = self._trigger(scheduler, datetime(2026, 8, 25, 10, 0))

        self.assertIsNotNone(job_key)
        row = self._row()
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["total_tasks"], 3)
        self.assertEqual(row["success_count"], 2)
        self.assertEqual(row["failed_count"], 1)

    def test_restart_expires_stale_running_claim_and_allows_retry(self):
        scheduler, launcher = self._new_scheduler(_FakeLauncher([True, True]))
        now = datetime(2026, 8, 25, 10, 3)
        self._trigger(scheduler, now)
        original = self._row()

        conn = self.db.get_connection()
        try:
            conn.execute(
                "UPDATE scheduled_collection_runs SET started_at=? WHERE job_key=?",
                ((now - timedelta(hours=2)).isoformat(sep=" "), original["job_key"]),
            )
            conn.commit()
        finally:
            conn.close()

        restarted, _ = self._new_scheduler(launcher)
        self.assertGreaterEqual(self._expire(restarted, now), 1)
        self.assertEqual(self._row()["status"], "expired")

        retried = self._trigger(restarted, now + timedelta(seconds=5))
        self.assertTrue(retried)
        self.assertEqual(len(launcher.calls), 2)
        row = self._row()
        self.assertEqual(row["job_key"], original["job_key"])
        self.assertEqual(row["status"], "running")

    def test_freshness_uses_real_summary_time_and_hides_internal_error(self):
        target_date = "2026-08-25"
        scheduler, _ = self._new_scheduler()
        self._trigger(scheduler, datetime(2026, 8, 25, 10, 0))
        job_key = self._row()["job_key"]
        self._complete(
            scheduler,
            job_key,
            {"meituan": "success", "douyin": "failed", "payment": "success"},
            error_msg="secret upstream detail must not leave admin storage",
        )

        conn = self.db.get_connection()
        try:
            rows = (
                ("meituan", "2026-08-25 10:11:12"),
                ("payment", "2026-08-25 10:15:30"),
            )
            for platform, updated_at in rows:
                conn.execute(
                    "INSERT INTO daily_summary "
                    "(id,date,venue,platform,metrics_json,updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        target_date,
                        "测试场地",
                        platform,
                        "{}",
                        updated_at,
                    ),
                )
            for platform, status, created_at in (
                ("meituan", "success", "2026-08-25 10:10:00"),
                ("douyin", "failed", "2026-08-25 10:10:01"),
                ("payment", "success", "2026-08-25 10:10:02"),
            ):
                conn.execute(
                    "INSERT INTO tasks "
                    "(id,platform,date,status,created_at,finished_at,error_msg) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        platform,
                        target_date,
                        status,
                        created_at,
                        created_at,
                        "internal failure detail" if status == "failed" else "",
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        result = self._freshness(
            target_date,
            expected_platforms=("meituan", "douyin", "payment"),
        )

        self.assertEqual(result["target_date"], target_date)
        self.assertEqual(result["data_updated_at"], "2026-08-25 10:15:30")
        self.assertEqual(
            result["oldest_data_updated_at"], "2026-08-25 10:11:12"
        )
        self.assertEqual(result["state"], "partial")
        self.assertEqual(result["sources_present"], 2)
        self.assertEqual(result["sources_expected"], 3)
        self.assertEqual(result["running_count"], 0)
        self.assertGreaterEqual(result["failed_count"], 1)
        self.assertIsNotNone(result["last_attempt_at"])
        self.assertTrue(
            {
                "enabled",
                "next_run_at",
                "next_label",
                "next_start_date",
                "next_target_date",
                "last_run",
            } <= set(result["schedule"]),
            result["schedule"],
        )
        self.assertNotIn("error_msg", json.dumps(result, ensure_ascii=False))

    def test_freshness_without_rows_is_missing_and_has_no_fake_timestamp(self):
        result = self._freshness(
            "2026-08-25",
            expected_platforms=("meituan", "douyin"),
        )

        self.assertEqual(result["state"], "missing")
        self.assertIsNone(result["data_updated_at"])
        self.assertIsNone(result["oldest_data_updated_at"])
        self.assertEqual(result["sources_present"], 0)
        self.assertEqual(result["sources_expected"], 2)
        self.assertEqual(result["running_count"], 0)
        self.assertEqual(result["failed_count"], 0)
        self.assertNotIn("error_msg", json.dumps(result, ensure_ascii=False))

    def test_manual_success_after_partial_schedule_restores_complete_state(self):
        target_date = "2026-08-25"
        scheduler, _ = self._new_scheduler()
        job_key = self._trigger(scheduler, datetime(2026, 8, 25, 10, 0))
        self._complete(
            scheduler,
            job_key,
            {"meituan": "success", "douyin": "failed"},
        )

        conn = self.db.get_connection()
        try:
            conn.execute(
                "UPDATE scheduled_collection_runs SET started_at=? WHERE job_key=?",
                ("2026-08-25T10:00:00+08:00", job_key),
            )
            for platform in ("meituan", "douyin"):
                conn.execute(
                    "INSERT INTO daily_summary "
                    "(id,date,venue,platform,metrics_json,updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        target_date,
                        "测试场地",
                        platform,
                        "{}",
                        "2026-08-25 11:05:00",
                    ),
                )
                conn.execute(
                    "INSERT INTO tasks "
                    "(id,platform,date,status,created_at,finished_at) "
                    "VALUES (?,?,?,'success',?,?)",
                    (
                        str(uuid.uuid4()),
                        platform,
                        target_date,
                        "2026-08-25 11:00:00",
                        "2026-08-25 11:05:00",
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        result = self._freshness(
            target_date,
            expected_platforms=("meituan", "douyin"),
        )
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["failed_count"], 0)
        self.assertTrue(result["schedule"]["last_run"]["resolved"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
