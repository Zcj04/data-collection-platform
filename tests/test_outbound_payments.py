"""真实 SQLite 上验证版本切换、缺失值、门店映射与停止/重启边界。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import db, outbound_payments as store
from core import outbound_collection as collector


def record(**changes):
    row = {"storeName": "来源门店", "operationTime": "2026-07-01 12:00:00", "businessTypeDesc": "设备出礼",
           "skuId": "gift-1", "skuName": "测试礼品", "skuPredictCost": 25.5, "originalCount": 3,
           "stockCount": -1, "changeAfterCount": 2, "sumCost": -25.5,
           "operationPerson": "不得保存", "description": "不得保存"}
    return dict(row, **changes)


class OutboundPaymentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = db.DB_PATH
        db.DB_PATH = str(Path(self.temp.name) / "app.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original
        self.temp.cleanup()

    def run_batch(self, rows=None, publish=True):
        run = store.queue_months("2026-07", "2026-07")[0]
        store.begin_run(run)
        mapping = store.bind_source(run, "hq1", "account-test", {"来源门店": ["标准门店"]}, ["来源门店", "未出现门店"])
        for day in store.month_dates("2026-07"):
            day_rows = (rows if rows is not None else [record()]) if day.endswith("01") else []
            store.save_day(run, day, day_rows, {"total": len(day_rows), "pages": int(bool(day_rows))}, mapping)
        if publish:
            store.publish(run)
        return run

    def test_complete_month_and_recollection_replace_without_double_counting(self):
        first = self.run_batch()
        second = self.run_batch([record(), record()])  # 两条完全相同的合法来源行必须保留。
        result = store.overview("2026-07")
        self.assertEqual(result["run"]["id"], second)
        self.assertEqual(result["summary"]["gift_cost"], 51)
        self.assertEqual(result["summary"]["gift_quantity"], 2)
        self.assertEqual(store.get_run(first)["is_current"], 0)
        self.assertEqual(store.details("2026-07")["total"], 2)

    def test_failed_or_cancelled_run_never_replaces_old_month(self):
        first = self.run_batch()
        next_run = store.queue_months("2026-07", "2026-07")[0]
        store.begin_run(next_run)
        with self.assertRaises(ValueError):
            store.publish(next_run)
        store.cancel_batch(next_run)
        with self.assertRaises(ValueError):
            store.save_day(next_run, "2026-07-01", [record()], {"total": 1, "pages": 1}, {})
        with self.assertRaises(ValueError):
            store.publish(next_run)
        self.assertEqual(store.overview("2026-07")["run"]["id"], first)

    def test_missing_month_or_store_day_is_not_zero(self):
        self.assertIsNone(store.overview("2026-07")["summary"])
        self.run_batch()
        overview = store.overview("2026-07", "来源门店")
        self.assertIsNone(overview["daily"][1]["gift_cost"])
        self.assertEqual(overview["daily"][1]["source_status"], "no_source_records")
        self.assertEqual(overview["stores_without_records"], ["未出现门店"])

    def test_zero_cost_and_inventory_adjustments_are_separate(self):
        self.run_batch([record(sumCost=0, skuPredictCost=0), record(businessTypeDesc="盘亏", stockCount=-3, changeAfterCount=0, sumCost=-76.5)])
        overview = store.overview("2026-07")
        self.assertEqual(overview["summary"]["gift_cost"], 0)
        self.assertEqual(overview["summary"]["zero_cost_count"], 1)
        self.assertEqual(store.details("2026-07", attention=True)["total"], 1)
        self.assertEqual(store.details("2026-07", business_type="__adjustments__")["rows"][0]["cost"], -76.5)

    def test_invalid_financial_fields_roll_back_entire_day(self):
        for bad in (None, "NaN", "-1.111"):
            run = store.queue_months("2026-07", "2026-07")[0]
            store.begin_run(run)
            with self.assertRaises(ValueError):
                store.save_day(run, "2026-07-01", [record(), record(sumCost=bad)], {"total": 2, "pages": 1}, {})
            self.assertEqual(store.completed_days(run), set())
            store.cancel_batch(run)

    def test_retry_freezes_mapping_and_rejects_account_switch(self):
        run = self.run_batch(publish=False)
        store.fail_run(run, "中断")
        store.retry_run(run)
        store.begin_run(run)
        self.assertEqual(len(store.completed_days(run)), 31)
        self.assertEqual(store.bind_source(run, "hq1", "account-test", {"来源门店": ["错误门店"]}, []), {"来源门店": ["标准门店"]})
        with self.assertRaises(ValueError):
            store.bind_source(run, "hq2", "other-account", {}, [])
        store.publish(run)

    def test_queue_serialization_and_restart_recovery(self):
        ids = store.queue_months("2026-06", "2026-07")
        with self.assertRaises(ValueError):
            store.queue_months("2026-07", "2026-07")
        store.begin_run(ids[0])
        store.interrupt_stale_runs()
        self.assertEqual([store.get_run(run)["status"] for run in ids], ["failed", "failed"])
        self.assertFalse(store.status()["busy"])

    def test_ambiguous_mapping_not_guessed_and_no_personal_fields_saved(self):
        run = store.queue_months("2026-07", "2026-07")[0]
        store.begin_run(run)
        for day in store.month_dates("2026-07"):
            rows = [record()] if day.endswith("01") else []
            store.save_day(run, day, rows, {"total": len(rows), "pages": int(bool(rows))}, {"来源门店": ["门店1", "门店2"]})
        store.publish(run)
        row = store.details("2026-07")["rows"][0]
        self.assertIsNone(row["venue"])
        self.assertEqual(row["mapping_status"], "ambiguous")
        self.assertNotIn("不得保存", str(row))

    def test_filters_grouping_and_pagination(self):
        self.run_batch([record(), record(skuId="gift-2", skuName="二号商品")])
        grouped = store.details("2026-07", group="sku")
        self.assertEqual(grouped["total"], 2)
        self.assertEqual(store.details("2026-07", sku_id="gift-1")["total"], 1)
        self.assertEqual(len(store.details("2026-07", page=2, page_size=1)["rows"]), 1)
        with self.assertRaises(ValueError):
            store.details("2026-07", day="2026-08-01")
        with self.assertRaises(ValueError):
            store.details("2026-07", group="SQL injection")

    def test_worker_login_error_redacted_and_remaining_days_retriable(self):
        run = store.queue_months("2026-07", "2026-07")[0]
        with patch.object(collector, "CredentialManager") as manager, patch.object(collector.crawler, "login", side_effect=RuntimeError("secret-test-password")):
            manager.return_value.load.return_value = {"总部账号": "test-account", "总部密码": "secret-test-password"}
            collector.collect_run(run)
        saved = store.get_run(run)
        self.assertEqual(saved["status"], "failed")
        self.assertNotIn("secret-test-password", saved["error"])

    def test_equipment_names_never_merge_across_stores_when_id_missing(self):
        self.run_batch([record(equipmentName="01号机", iotEquipmentNo=""),
                        record(storeName="另一个门店", equipmentName="01号机", iotEquipmentNo="")])
        rows = store.details("2026-07", group="equipment")["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["source_store"] for row in rows}, {"来源门店", "另一个门店"})


if __name__ == "__main__":
    unittest.main()
