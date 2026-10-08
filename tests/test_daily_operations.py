# -*- coding: utf-8 -*-
"""每日经营数据差值与完整性契约测试。"""

import json
import os
import tempfile

from unittest import mock

import pandas as pd

import core.daily_operations as daily_operations
import core.db as db
from crawlers import report_summary


def _insert(conn, date, venue, platform, metrics, period_start=None):
    conn.execute(
        "INSERT INTO daily_summary "
        "(id,date,venue,platform,metrics_json,period_start,source_task_id,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            "%s-%s-%s" % (date, venue, platform), date, venue, platform,
            json.dumps(metrics, ensure_ascii=False), period_start,
            "task-%s-%s" % (date, platform), "%s 16:10:00" % date,
        ),
    )


def _db_with_rows(rows):
    temp = tempfile.TemporaryDirectory(prefix="workbuddy-daily-")
    original = db.DB_PATH
    db.DB_PATH = os.path.join(temp.name, "app.db")
    db.init_db()
    conn = db.get_connection()
    for row in rows:
        _insert(conn, *row)
    conn.commit()
    conn.close()
    return temp, original


def test_daily_delta_and_kpay_correction():
    rows = [
        ("2026-08-23", "香港H店", "jingjian", {"鲸舰现金": 50}, "2026-08-01"),
        ("2026-08-23", "香港H店", "kpay", {"Kpay收款": 5}, "2026-08-01"),
        ("2026-08-24", "香港H店", "jingjian", {"鲸舰现金": 110}, "2026-08-01"),
        ("2026-08-24", "香港H店", "kpay", {"Kpay收款": 10}, "2026-08-01"),
    ]
    temp, original = _db_with_rows(rows)
    try:
        with mock.patch.object(daily_operations, "_active_venues", return_value={"香港H店"}), \
                mock.patch.object(daily_operations, "_configured_platforms", return_value={
                    "jingjian": "鲸舰", "kpay": "KPay",
                }):
            result = daily_operations.get_daily_operations("2026-08-24", "香港H店")
        assert result["status"] == "complete"
        assert result["total_income"] == 60.0
        values = {item["platform"]: item["income"] for item in result["platforms"]}
        assert values == {"jingjian": 55.0, "kpay": 5.0}
    finally:
        db.DB_PATH = original
        temp.cleanup()


def test_missing_previous_snapshot_is_not_zero():
    rows = [("2026-08-24", "深圳A店", "meituan", {"美团收款": 100}, "2026-08-01")]
    temp, original = _db_with_rows(rows)
    try:
        with mock.patch.object(daily_operations, "_active_venues", return_value={"深圳A店"}), \
                mock.patch.object(daily_operations, "_configured_platforms", return_value={"meituan": "美团"}):
            result = daily_operations.get_daily_operations("2026-08-24")
        assert result["status"] == "missing"
        assert result["total_income"] is None
        assert result["missing_required_dates"] == [
            {"date": "2026-08-23", "reason": "snapshot_missing"}
        ]
    finally:
        db.DB_PATH = original
        temp.cleanup()


def test_daily_scope_filters_unassigned_store_before_totals():
    rows = [
        ("2026-08-23", "深圳A店", "meituan", {"美团收款": 0}, "2026-08-01"),
        ("2026-08-23", "广州B店", "meituan", {"美团收款": 0}, "2026-08-01"),
        ("2026-08-24", "深圳A店", "meituan", {"美团收款": 100}, "2026-08-01"),
        ("2026-08-24", "广州B店", "meituan", {"美团收款": 200}, "2026-08-01"),
    ]
    temp, original = _db_with_rows(rows)
    try:
        with mock.patch.object(daily_operations, "_active_venues", return_value={"深圳A店", "广州B店"}), \
                mock.patch.object(daily_operations, "_configured_platforms", return_value={"meituan": "美团"}):
            result = daily_operations.get_daily_operations(
                "2026-08-24", venue_scope={"深圳A店"}
            )
        assert [item["venue"] for item in result["venues"]] == ["深圳A店"]
        assert result["known_income"] == 100.0
    finally:
        db.DB_PATH = original
        temp.cleanup()


def test_legacy_snapshot_is_unverified():
    rows = [
        ("2026-08-23", "深圳A店", "meituan", {"美团收款": 80}, None),
        ("2026-08-24", "深圳A店", "meituan", {"美团收款": 100}, None),
    ]
    temp, original = _db_with_rows(rows)
    try:
        with mock.patch.object(daily_operations, "_active_venues", return_value={"深圳A店"}), \
                mock.patch.object(daily_operations, "_configured_platforms", return_value={"meituan": "美团"}):
            result = daily_operations.get_daily_operations("2026-08-24")
        assert result["status"] == "unverified"
        assert result["total_income"] is None
        assert result["known_income"] == 20.0
        assert result["warnings"][0]["reason"] == "range_unknown"
    finally:
        db.DB_PATH = original
        temp.cleanup()


def test_kpay_failure_is_optional_warning():
    rows = [
        ("2026-08-23", "深圳A店", "meituan", {"美团收款": 80}, "2026-08-01"),
        ("2026-08-23", "深圳A店", "kpay", {"Kpay收款": 10}, "2026-08-01"),
        ("2026-08-24", "深圳A店", "meituan", {"美团收款": 100}, "2026-08-01"),
    ]
    temp, original = _db_with_rows(rows)
    try:
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO tasks (id,platform,date,status,created_at) VALUES (?,?,?,?,?)",
            ("kpay-failed", "kpay", "2026-08-24", "failed", "2026-08-24 16:10:00"),
        )
        conn.commit()
        conn.close()
        with mock.patch.object(daily_operations, "_active_venues", return_value={"深圳A店"}), \
                mock.patch.object(daily_operations, "_configured_platforms", return_value={
                    "meituan": "美团", "kpay": "KPay",
                }):
            result = daily_operations.get_daily_operations("2026-08-24")
        assert result["status"] == "complete"
        assert result["total_income"] == 20.0
        assert result["missing_sources"] == []
        assert result["optional_source_warnings"] == [{
            "date": "2026-08-24", "platform": "kpay", "name": "KPay", "reason": "task_failed",
        }]
    finally:
        db.DB_PATH = original
        temp.cleanup()


def test_closed_venues_are_excluded_from_daily_operations():
    rows = [
        ("2026-08-23", "深圳A店", "meituan", {"美团收款": 80}, "2026-08-01"),
        ("2026-08-24", "深圳A店", "meituan", {"美团收款": 100}, "2026-08-01"),
        ("2026-08-23", "深圳撤店", "meituan", {"美团收款": 900}, "2026-08-01"),
        ("2026-08-24", "深圳撤店", "meituan", {"美团收款": 1000}, "2026-08-01"),
        ("2026-08-23", "香港H店", "meituan", {"美团收款": 500}, "2026-08-01"),
        ("2026-08-24", "香港H店", "meituan", {"美团收款": 600}, "2026-08-01"),
    ]
    temp, original = _db_with_rows(rows)
    try:
        with mock.patch.object(daily_operations, "_active_venues", return_value={"深圳A店"}), \
                mock.patch.object(daily_operations, "load_store_regions", return_value={"深圳A店": "深圳"}), \
                mock.patch.object(daily_operations, "_configured_platforms", return_value={"meituan": "美团"}):
            result = daily_operations.get_daily_operations("2026-08-24")
            retired_result = daily_operations.get_daily_operations("2026-08-24", "香港H店")
        assert result["status"] == "complete"
        assert result["available_venues"] == ["深圳A店"]
        assert [item["venue"] for item in result["venues"]] == ["深圳A店"]
        assert result["venues"][0]["region"] == "深圳"
        assert result["known_income"] == 20.0
        assert result["platforms"][0]["income"] == 20.0
        assert retired_result["selected_venue"] is None
        assert all("香港" not in item["venue"] for item in retired_result["venues"])
    finally:
        db.DB_PATH = original
        temp.cleanup()


def test_daily_active_venues_follow_dashboard_owner_status():
    with mock.patch.object(daily_operations, "get_operating_venues", return_value={"深圳A店", "深圳E店", "香港H店"}), \
            mock.patch.object(daily_operations, "load_store_targets", return_value={"已撤店": 1000}):
        assert daily_operations._active_venues() == {"深圳A店", "深圳E店"}


def test_dashboard_owner_status_excludes_closed_and_unassigned_venues():
    dataframe = pd.DataFrame([
        {"场地": "深圳A店", "负责人": "张三"},
        {"场地": "深圳撤店", "负责人": "撤店"},
        {"场地": "深圳已撤店", "负责人": "李四（撤店）"},
        {"场地": "深圳未分配", "负责人": ""},
        {"场地": "香港H店", "负责人": "王五"},
    ])
    with mock.patch.object(report_summary, "_get_base_df", return_value=(dataframe, list(dataframe.columns))):
        assert report_summary.get_operating_venues() == {"深圳A店"}


def test_daily_active_venues_fall_back_when_dashboard_source_is_unavailable():
    with mock.patch.object(daily_operations, "get_operating_venues", return_value=None), \
            mock.patch.object(daily_operations, "load_store_targets", return_value={"深圳A店": 1000, "香港H店": 1000}), \
            mock.patch.object(daily_operations, "load_daily_active_venues", return_value={"深圳E店", "香港A店"}):
        assert daily_operations._active_venues() == {"深圳A店", "深圳E店"}


def main():
    test_daily_delta_and_kpay_correction()
    test_missing_previous_snapshot_is_not_zero()
    test_legacy_snapshot_is_unverified()
    test_kpay_failure_is_optional_warning()
    test_closed_venues_are_excluded_from_daily_operations()
    test_daily_active_venues_follow_dashboard_owner_status()
    test_dashboard_owner_status_excludes_closed_and_unassigned_venues()
    test_daily_active_venues_fall_back_when_dashboard_source_is_unavailable()
    print("每日经营数据规则测试：全部通过（8 项）")


if __name__ == "__main__":
    main()
