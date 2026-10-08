# -*- coding: utf-8 -*-
"""进场货款与月末基础货款口径测试。"""

import os
import json
import tempfile
from unittest import mock

import pandas as pd

import core.db as db
import core.payment_accounting as accounting


def _temp_db():
    temp = tempfile.TemporaryDirectory(prefix="workbuddy-payment-accounting-")
    original = db.DB_PATH
    db.DB_PATH = os.path.join(temp.name, "app.db")
    db.init_db()
    return temp, original


def _roster():
    return {
        "source": "dashboard",
        "available": True,
        "rows": [
            {"venue": "深圳A店", "owner": "张三", "operating": True},
            {"venue": "深圳撤店", "owner": "撤店", "operating": False},
        ],
    }


def test_entry_payment_keeps_revisions_and_returns_latest():
    temp, original = _temp_db()
    try:
        with mock.patch.object(accounting, "get_venue_roster", side_effect=_roster):
            first = accounting.save_entry_payment("深圳A店", "2026-01-10", 1200.126, "首批", "user-1")
            second = accounting.save_entry_payment("深圳A店", "2026-01-10", 1300, "修正", "user-1")
            unchanged = accounting.save_entry_payment("深圳A店", "2026-01-10", 1300, "修正", "user-1")
            result = accounting.list_entry_payments()

        assert first["revision"] == 1
        assert first["amount"] == 1200.13
        assert second["revision"] == 2
        assert unchanged["revision"] == 2
        assert unchanged["unchanged"] is True
        assert result["summary"]["filled_count"] == 1
        assert result["summary"]["total"] == 1300.0
        assert result["rows"][0]["entry"]["note"] == "修正"

        conn = db.get_connection()
        assert conn.execute("SELECT COUNT(*) FROM entry_payment_revisions").fetchone()[0] == 2
        conn.close()
    finally:
        db.DB_PATH = original
        temp.cleanup()


def test_entry_payment_rejects_unknown_venue_and_invalid_amount():
    temp, original = _temp_db()
    try:
        with mock.patch.object(accounting, "get_venue_roster", side_effect=_roster):
            for venue, amount in (("不存在门店", 10), ("深圳A店", -1), ("深圳A店", float("nan"))):
                try:
                    accounting.save_entry_payment(venue, "2026-01-10", amount)
                    assert False, "应拒绝无效进场货款"
                except ValueError:
                    pass
    finally:
        db.DB_PATH = original
        temp.cleanup()


def test_monthly_base_requires_exact_calendar_month_end():
    temp, original = _temp_db()
    try:
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO payment_imports(date,shop_name,amount) VALUES (?,?,?)",
            ("2026-07-30", "Excel深圳A", 999),
        )
        conn.commit()
        conn.close()
        with mock.patch.object(accounting, "get_venue_roster", side_effect=_roster):
            result = accounting.get_monthly_base_payment(2026, 7)
        assert result["month_end_date"] == "2026-07-31"
        assert result["status"] == "missing"
        assert result["summary"]["raw_total"] == 0
    finally:
        db.DB_PATH = original
        temp.cleanup()


def test_monthly_base_maps_rows_and_exposes_unmatched_without_treating_missing_as_zero():
    temp, original = _temp_db()
    try:
        conn = db.get_connection()
        conn.executemany(
            "INSERT INTO payment_imports(date,shop_name,amount) VALUES (?,?,?)",
            [
                ("2026-07-31", "Excel深圳A", 1500),
                ("2026-07-31", "未知店名", -25),
            ],
        )
        conn.commit()
        conn.close()
        with mock.patch.object(accounting, "get_venue_roster", side_effect=_roster), \
                mock.patch.object(accounting, "get_match_list", return_value=[("深圳A店", "Excel深圳A")]):
            result = accounting.get_monthly_base_payment(2026, 7)

        rows = {item["venue"]: item for item in result["rows"]}
        assert result["status"] == "attention"
        assert result["summary"] == {
            "raw_count": 2,
            "mapped_count": 1,
            "raw_total": 1475.0,
            "mapped_total": 1500.0,
            "unmatched_count": 1,
            "unmatched_total": -25.0,
            "missing_operating_count": 0,
        }
        assert rows["深圳A店"]["amount"] == 1500.0
        assert rows["深圳撤店"]["amount"] is None
        assert result["unmatched"] == [{"shop_name": "未知店名", "amount": -25.0}]
    finally:
        db.DB_PATH = original
        temp.cleanup()


def test_monthly_points_require_exact_month_end_and_use_one_point_as_one_point_five_yuan():
    temp, original = _temp_db()
    try:
        conn = db.get_connection()
        conn.executemany(
            "INSERT INTO daily_summary(id,date,venue,platform,metrics_json) VALUES (?,?,?,?,?)",
            [
                ("july-30", "2026-07-30", "深圳A店", "新系统", json.dumps({"新系统积分增加": 999, "新系统积分减少": 0})),
                ("july-31-new", "2026-07-31", "深圳A店", "新系统", json.dumps({"新系统积分增加": 100, "新系统积分减少": 20})),
                ("july-31-star", "2026-07-31", "深圳A店", "StarThing", json.dumps({"StarThing积分增加": 40, "StarThing积分减少": 10})),
            ],
        )
        conn.commit()
        conn.close()
        with mock.patch.object(accounting, "get_venue_roster", side_effect=_roster):
            result = accounting.get_monthly_points(2026, 7)

        rows = {item["venue"]: item for item in result["rows"]}
        assert result["month_end_date"] == "2026-07-31"
        assert result["summary"]["net_points"] == 110.0
        assert result["summary"]["amount"] == 165.0
        assert rows["深圳A店"]["increase"] == 140.0
        assert rows["深圳A店"]["decrease"] == 30.0
        assert rows["深圳撤店"]["net_points"] is None
    finally:
        db.DB_PATH = original
        temp.cleanup()


def test_lifetime_summary_uses_closed_month_snapshots_and_keeps_outbound_pending():
    temp, original = _temp_db()
    try:
        conn = db.get_connection()
        conn.executemany(
            "INSERT INTO payment_imports(date,shop_name,amount) VALUES (?,?,?)",
            [
                ("2026-01-31", "Excel深圳A", 200),
                ("2026-02-28", "Excel深圳A", 300),
            ],
        )
        conn.executemany(
            "INSERT INTO daily_summary(id,date,venue,platform,metrics_json) VALUES (?,?,?,?,?)",
            [
                ("jan-points", "2026-01-31", "深圳A店", "新系统", json.dumps({"新系统积分增加": 60, "新系统积分减少": 10})),
                ("feb-points", "2026-02-28", "深圳A店", "鲸舰", json.dumps({"鲸舰积分增加": 25, "鲸舰积分减少": 5})),
            ],
        )
        conn.commit()
        conn.close()

        with mock.patch.object(accounting, "get_venue_roster", side_effect=_roster), \
                mock.patch.object(accounting, "get_match_list", return_value=[("深圳A店", "Excel深圳A")]):
            accounting.save_entry_payment("深圳A店", "2026-01-10", 1000, "首批", "user-1")
            result = accounting.get_lifetime_summary("2026-03-15")

        row = next(item for item in result["rows"] if item["venue"] == "深圳A店")
        assert result["through_month"] == "2026-02"
        assert result["summary"]["month_count"] == 2
        assert row["entry_amount"] == 1000.0
        assert row["base_total"] == 500.0
        assert row["points_total"] == 70.0
        assert row["points_amount"] == 105.0
        assert row["known_total"] == 1605.0
        assert row["outbound_amount"] is None
        assert row["status"] == "complete"
        assert result["outbound_status"] == "pending"
    finally:
        db.DB_PATH = original
        temp.cleanup()


def test_lifetime_summary_keeps_known_entry_amount_when_entry_date_is_unknown():
    temp, original = _temp_db()
    try:
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO entry_payment_revisions "
            "(venue,revision,entry_date,amount_cents,note,actor_user_id,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("深圳A店", 1, "", 100000, "进场日期待补", "batch-import", "2026-08-29 12:00:00"),
        )
        conn.execute(
            "INSERT INTO daily_summary(id,date,venue,platform,metrics_json) VALUES (?,?,?,?,?)",
            ("jan-points", "2026-01-31", "深圳A店", "新系统", json.dumps({"新系统积分增加": 10, "新系统积分减少": 0})),
        )
        conn.commit()
        conn.close()

        with mock.patch.object(accounting, "get_venue_roster", side_effect=_roster), \
                mock.patch.object(accounting, "get_match_list", return_value=[]):
            result = accounting.get_lifetime_summary("2026-03-15")

        row = next(item for item in result["rows"] if item["venue"] == "深圳A店")
        assert row["entry_amount"] == 1000.0
        assert row["entry_date"] == ""
        assert row["expected_month_count"] == 2
        assert "进场日期待补" in row["issues"]
        assert "缺少进场货款" not in row["issues"]
    finally:
        db.DB_PATH = original
        temp.cleanup()


def test_roster_follows_dashboard_owner_status():
    dataframe = pd.DataFrame([
        {"场地": "深圳A店", "负责人": "张三"},
        {"场地": "深圳撤店", "负责人": "撤店"},
    ])
    with mock.patch.object(accounting, "_get_base_df", return_value=(dataframe, list(dataframe.columns))):
        result = accounting.get_venue_roster()
    assert result["available"] is True
    assert [(item["venue"], item["operating"]) for item in result["rows"]] == [
        ("深圳A店", True),
        ("深圳撤店", False),
    ]
