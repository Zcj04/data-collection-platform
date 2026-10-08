"""总部选择、分页失败和缺失金额不能默认为零。"""
import unittest
import json
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from crawlers import duojinbao_equipment_stock_crawler as crawler


def page(index, total, items):
    return {"data": {"pageIndex": index, "pageSize": 2,
                     "pages": (total + 1) // 2, "total": total, "items": items}}


def record(second=0, day="2026-08-30"):
    return {"storeName": "测试门店", "operationTime": day + " 12:00:%02d" % second,
            "businessTypeDesc": "设备出礼", "stockCount": -1, "sumCost": -25.5,
            "operationPerson": "不应导出", "description": "不应导出"}


class EquipmentStockTests(unittest.TestCase):
    def test_headquarters_type_and_unique_match(self):
        orgs = [{"name": "示例品牌", "type": 2, "id": 1, "adOrganizationId": 10},
                {"name": "示例品牌", "type": 3, "id": 2, "adOrganizationId": 20}]
        response = {"data": [{"merchantId": 9, "tenantOrgList": orgs}]}
        with patch.object(crawler, "request_json", return_value=response) as request:
            self.assertEqual(crawler.select_headquarters(None)["storeId"], 2)
            self.assertEqual(request.call_args.kwargs["params"]["storeId"], 2)
        orgs.append(dict(orgs[-1], id=3))
        with patch.object(crawler, "request_json", return_value=response) as request:
            with self.assertRaisesRegex(ValueError, "唯一匹配"):
                crawler.select_headquarters(None)
            self.assertEqual(request.call_count, 1)

    @patch.object(crawler, "PAGE_SIZE", 2)
    def test_complete_pagination_and_sensitive_fields_removed(self):
        responses = [page(1, 3, [record(0), record(1)]), page(2, 3, [record(2)])]
        with patch.object(crawler, "request_json", side_effect=responses) as request:
            rows = crawler.fetch_records(None, "2026-08-30")
        self.assertEqual(len(rows), 3)
        self.assertNotIn("operationPerson", rows[0])
        self.assertNotIn("description", rows[0])
        self.assertEqual(request.call_args.kwargs["json_data"]["pageIndex"], 2)
        self.assertEqual(crawler.summarize_records(rows)["by_business_type"]["设备出礼"]["sumCost"], "-76.5")

    @patch.object(crawler, "PAGE_SIZE", 2)
    def test_reject_incomplete_repeated_changed_or_out_of_range_pages(self):
        first = page(1, 4, [record(0), record(1)])
        cases = [
            [page(1, 4, [record(0)])],
            [first, page(2, 4, [record(0), record(1)])],
            [first, page(2, 3, [record(2)])],
            [page(1, 1, [record(day="2026-08-29")])],
        ]
        for responses in cases:
            with self.subTest(responses=responses):
                with patch.object(crawler, "request_json", side_effect=responses):
                    with self.assertRaises(ValueError):
                        crawler.fetch_records(None, "2026-08-30")

    def test_missing_cost_is_not_zero(self):
        for value in (None, "", "NaN", "Infinity"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    crawler.summarize_records([dict(record(), sumCost=value)])

    def test_month_boundaries_and_unfinished_month_rejected(self):
        self.assertEqual(len(crawler.month_dates("2024-02", date(2024, 3, 1))), 29)
        days = crawler.month_dates("2026-07", date(2026, 8, 31))
        self.assertEqual((days[0], days[-1], len(days)), ("2026-07-01", "2026-07-31", 31))
        for month in ("2026-08", "2026-09", "2026-7"):
            with self.subTest(month=month), self.assertRaises(ValueError):
                crawler.month_dates(month, date(2026, 8, 31))

    def test_failed_day_cannot_publish_complete_month(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "trial"
            with patch.object(crawler, "month_dates", return_value=["2026-07-01", "2026-07-02"]):
                with patch.object(crawler, "fetch_records", side_effect=[[record(day="2026-07-01")], ValueError("分页失败")]):
                    with self.assertRaises(ValueError):
                        crawler.fetch_month(None, "2026-07", destination)
            self.assertTrue((destination / "2026-07-01.json").exists())
            self.assertFalse((destination / "summary.json").exists())

    def test_month_summary_keeps_types_and_all_days(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "trial"
            samples = [[record(day="2026-07-01")], [],
                       [dict(record(day="2026-07-03"), businessTypeDesc="盘亏", sumCost=-5)]]
            with patch.object(crawler, "month_dates", return_value=["2026-07-01", "2026-07-02", "2026-07-03"]):
                with patch.object(crawler, "fetch_records", side_effect=samples):
                    result = crawler.fetch_month(None, "2026-07", destination)
            self.assertEqual(result["days_succeeded"], 3)
            self.assertEqual(result["daily"][1]["records"], 0)
            self.assertEqual(result["summary"]["by_business_type"]["设备出礼"]["sumCost"], "-25.5")
            saved = json.loads((destination / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(saved, result)


if __name__ == "__main__":
    unittest.main()
