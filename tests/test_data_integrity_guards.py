# -*- coding: utf-8 -*-
"""货款导入与月累计快照的完整性保护。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core.db as db
from core.payment_store import get_date_info, replace_date_rows
from core.task_manager import TaskManager


class DataIntegrityGuardTests(unittest.TestCase):
    def test_payment_history_preserves_before_after_and_actor(self):
        from core import payment_store
        payment_store.replace_date_rows("2026-09-01", [{"shop_name": "A", "amount": 100}], "first.xlsx", "user1")
        payment_store.replace_date_rows("2026-09-01", [{"shop_name": "A", "amount": 200}], "second.xlsx", "user2")
        payment_store.delete_date("2026-09-01", "user3")
        history = payment_store.list_history("2026-09-01")
        self.assertEqual([r["actor_user_id"] for r in history], ["user3", "user2", "user1"])
        self.assertEqual(history[0]["before"][0]["amount"], 200)
        self.assertEqual(history[0]["after"], [])
        self.assertEqual(history[1]["before"][0]["amount"], 100)
        self.assertEqual(history[1]["after"][0]["source_file"], "second.xlsx")
        with self.assertRaises(ValueError):
            payment_store.replace_date_rows("2026-09-01", [{"shop_name": "A", "amount": float("inf")}])
        self.assertEqual(len(payment_store.list_history("2026-09-01")), 3)

    def test_history_and_data_rollback_together(self):
        from core import payment_store
        payment_store.replace_date_rows("2026-09-01", [{"shop_name": "A", "amount": 100}])
        conn = db.get_connection()
        conn.execute("CREATE TRIGGER reject_payment BEFORE INSERT ON payment_imports BEGIN SELECT RAISE(ABORT, 'test rollback'); END")
        conn.commit()
        conn.close()
        with self.assertRaises(Exception):
            payment_store.replace_date_rows("2026-09-01", [{"shop_name": "A", "amount": 200}])
        self.assertEqual(payment_store.get_date_info("2026-09-01")["total"], 100)
        self.assertEqual(len(payment_store.list_history("2026-09-01")), 1)

    def test_upload_limits_reject_large_and_expanding_archives(self):
        import io
        import zipfile
        from core import payment_upload
        with mock.patch.object(payment_upload, "MAX_FILE_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "10 MB"):
                payment_upload.read_upload(io.BytesIO(b"x" * 11))
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/worksheets/sheet1.xml", b"a" * 1000)
        with mock.patch.object(payment_upload, "MAX_EXPANDED_BYTES", 999):
            with self.assertRaisesRegex(ValueError, "解压"):
                payment_upload.read_upload(io.BytesIO(content.getvalue()))
        with self.assertRaisesRegex(ValueError, "有效"):
            payment_upload.read_upload(io.BytesIO(b"not-xlsx"))

    def test_ai_does_not_sum_cumulative_days(self):
        from core.analyst.tools import query_summary
        manager = TaskManager()
        for day, amount in [("2026-09-01", 100), ("2026-09-02", 200)]:
            manager.save_summary("meituan", day, "A", json.dumps({"美团收款": amount}))
        result = query_summary("2026-09-01", "2026-09-02")
        self.assertEqual(result["total"], 200)
        self.assertEqual(result["dates"], ["2026-09-02"])

    def test_forecast_cutoff_and_incomplete_month(self):
        from core.analyst import forecast
        with mock.patch.object(forecast, "date_income_totals", return_value={
            "2026-07-31": 310, "2026-08-25": 250, "2026-09-30": 9000,
        }):
            result = forecast.forecast_months(target_date="2026-08-25")
        self.assertEqual([p["month"] for p in result["points"]], ["2026-07", "2026-08"])
        self.assertEqual(result["points"][1]["kind"], "projected")
        self.assertEqual(result["forecast"][0]["month"], "2026-09")

    def test_missing_key_does_not_replace_existing_credentials_key(self):
        from core import credential_manager
        from cryptography.fernet import Fernet
        key_file = Path(self.temp.name) / "key"
        with mock.patch.object(credential_manager, "KEY_FILE", str(key_file)):
            manager = credential_manager.CredentialManager()
            manager.save("test", {"password": "synthetic"})
            key_file.unlink()
            with self.assertRaisesRegex(RuntimeError, "密钥缺失"):
                credential_manager.CredentialManager()
            self.assertFalse(key_file.exists())
            key_file.write_bytes(Fernet.generate_key())
            from core.health import readiness_status
            result = readiness_status(database_path=db.DB_PATH, key_file=str(key_file), backup_root=str(Path(self.temp.name)/"backups"))
            self.assertEqual(result["checks"]["credential_key"]["status"], "failed")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workbuddy-integrity-")
        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp.name, "app.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp.cleanup()

    def test_duplicate_shop_names_are_rejected_before_replacing_old_rows(self):
        replace_date_rows("2026-09-01", [{"shop_name": "A", "amount": 10}])
        with self.assertRaisesRegex(ValueError, "重复店名"):
            replace_date_rows(
                "2026-09-01",
                [{"shop_name": "A", "amount": 100}, {"shop_name": "a", "amount": 200}],
            )
        self.assertEqual(get_date_info("2026-09-01")["total"], 10.0)

    def test_cumulative_regression_is_rejected_and_previous_snapshot_remains(self):
        manager = TaskManager()
        manager.save_summary("meituan", "2026-09-01", "A", json.dumps({"income": 100}))
        task_id = manager.create("meituan", "2026-09-02", start_date="2026-09-01")
        with self.assertRaisesRegex(ValueError, "月累计数据较前一天"):
            manager.complete_with_summary(
                task_id,
                "meituan",
                "2026-09-02",
                [("A", json.dumps({"income": 90}))],
            )
        self.assertEqual(manager.get_summary("2026-09-01")[0]["venue"], "A")
        self.assertEqual(manager.get_summary("2026-09-02"), [])

    def test_same_day_cumulative_regression_is_rejected(self):
        manager = TaskManager()
        manager.save_summary("meituan", "2026-09-09", "A", json.dumps({"income": 100}))
        manager.save_summary("meituan", "2026-09-10", "A", json.dumps({"income": 200}))
        task_id = manager.create("meituan", "2026-09-10", start_date="2026-09-01")
        with self.assertRaisesRegex(ValueError, "月累计数据较前一天"):
            manager.complete_with_summary(
                task_id,
                "meituan",
                "2026-09-10",
                [("A", json.dumps({"income": 150}))],
            )
        self.assertEqual(json.loads(manager.get_summary("2026-09-10")[0]["metrics_json"])["income"], 200)

    def test_cumulative_regression_after_collection_gap_is_rejected(self):
        manager = TaskManager()
        manager.save_summary("meituan", "2026-09-10", "A", json.dumps({"income": 300}))
        task_id = manager.create("meituan", "2026-09-13", start_date="2026-09-01")
        with self.assertRaisesRegex(ValueError, "同月最近可信快照"):
            manager.complete_with_summary(
                task_id,
                "meituan",
                "2026-09-13",
                [("A", json.dumps({"income": 250}))],
            )
        self.assertEqual(manager.get_summary("2026-09-13"), [])

    def test_forecast_uses_target_date_month_and_no_fallback_target(self):
        from core.analyst import forecast

        original_targets = forecast.load_store_targets
        original_totals = forecast.date_income_totals
        try:
            forecast.load_store_targets = lambda month=None: (
                {"A": 2855000} if month == "2026-09" else {"A": 5612000}
            )
            forecast.date_income_totals = lambda venue_scope=None: {"2026-09-13": 100}
            result = forecast.current_month_state("2026-09-13")
            assert result["target"] == 2855000
            assert result["completion"] is not None

            forecast.load_store_targets = lambda month=None: {}
            unconfigured = forecast.current_month_state("2026-09-13")
            assert unconfigured["target"] is None
            assert unconfigured["completion"] is None
            assert unconfigured["gap"] is None
            assert forecast.get_monthly_target() is None
        finally:
            forecast.load_store_targets = original_targets
            forecast.date_income_totals = original_totals

    def test_forecast_anomaly_skips_collection_gap(self):
        from core.analyst import forecast

        manager = TaskManager()
        for day, value in [
            ("2026-09-01", 100),
            ("2026-09-02", 205),
            ("2026-09-03", 303),
            ("2026-09-04", 407),
            ("2026-09-06", 805),
        ]:
            manager.save_summary("meituan", day, "A", json.dumps({"美团收款": value}))
        original_connection = forecast.get_connection
        forecast.get_connection = db.get_connection
        try:
            result = forecast.detect_anomaly("2026-09-06", threshold=2.0)
        finally:
            forecast.get_connection = original_connection
        assert result["count"] == 0
        assert result["anomalies"] == []

    def test_month_boundary_does_not_compare_previous_month_snapshot(self):
        manager = TaskManager()
        manager.save_summary("meituan", "2026-08-31", "A", json.dumps({"income": 1000}))
        task_id = manager.create("meituan", "2026-09-01", start_date="2026-09-01")
        self.assertTrue(
            manager.complete_with_summary(
                task_id,
                "meituan",
                "2026-09-01",
                [("A", json.dumps({"income": 10}))],
            )
        )

    def test_business_decreases_are_stored_without_regression_warnings(self):
        from core.data_quality import detect_cumulative_regressions
        from core.daily_operations import _regression_warnings

        manager = TaskManager()
        cases = [
            ("payment", "amount", 80),
            ("duojinbao", "基础货款", 70),
            ("yuntai", "芸苔远程取币", 60),
            ("yuntai", "芸苔远程取币", -20),
            ("meituan", "美团收款", -10),
            ("payment", "amount", -30),
        ]
        for platform, metric, value in cases:
            with self.subTest(platform=platform, metric=metric, value=value):
                manager.save_summary(platform, "2026-09-01", "A", json.dumps({metric: 100}))
                task_id = manager.create(platform, "2026-09-02", start_date="2026-09-01")
                self.assertTrue(manager.complete_with_summary(
                    task_id, platform, "2026-09-02", [("A", json.dumps({metric: value}))],
                ))
                stored = [r for r in manager.get_summary("2026-09-02") if r["platform"] == platform]
                self.assertEqual(json.loads(stored[0]["metrics_json"])[metric], value)
                old = {"venue": "A", "platform": platform, "metrics_json": json.dumps({metric: 100})}
                new = dict(old, metrics_json=json.dumps({metric: value}))
                self.assertEqual(detect_cumulative_regressions([new], [old]), [])
                self.assertEqual(_regression_warnings(
                    {("A", platform): {"metrics": {metric: value}}},
                    {("A", platform): {"metrics": {metric: 100}}},
                ), [])



if __name__ == "__main__":
    unittest.main()
