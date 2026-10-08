# -*- coding: utf-8 -*-
"""核算汇总应保留所选期间内已经撤店但有数据的门店。"""

import json
import os
import tempfile
from unittest import mock

import pandas as pd

from core import accounting_summary
from core import db
from crawlers import report_summary


def _insert(conn, date, venue, platform, metrics):
    conn.execute(
        "INSERT INTO daily_summary(id,date,venue,platform,metrics_json) "
        "VALUES (?,?,?,?,?)",
        (
            "%s-%s-%s" % (date, venue, platform),
            date,
            venue,
            platform,
            json.dumps(metrics, ensure_ascii=False),
        ),
    )


def test_period_summary_uses_latest_snapshot_per_store_and_platform():
    temp = tempfile.TemporaryDirectory(prefix="workbuddy-accounting-")
    original = db.DB_PATH
    db.DB_PATH = os.path.join(temp.name, "app.db")
    db.init_db()
    conn = db.get_connection()
    try:
        _insert(conn, "2026-07-31", "香港H店", "whale", {"鲸舰现金": 40})
        _insert(conn, "2026-08-15", "香港H店", "whale", {"鲸舰现金": 50})
        _insert(conn, "2026-08-20", "香港H店", "whale", {"鲸舰现金": 80})
        _insert(conn, "2026-08-18", "香港H店", "kpay", {"Kpay收款": 10})
        _insert(conn, "2026-08-31", "深圳A店", "meituan", {"美团收款": 100})
        conn.commit()

        data = accounting_summary.load_period_summary_data("2026-08-31")
    finally:
        conn.close()
        db.DB_PATH = original
        temp.cleanup()

    values = {
        (item["场地"], key): value
        for item in data
        for key, value in item.items()
        if key != "场地"
    }
    assert values == {
        ("深圳A店", "美团收款"): 100,
        ("香港H店", "Kpay收款"): 10,
        ("香港H店", "鲸舰现金"): 80,
    }
    assert accounting_summary.accounting_venue_scope(data) == {
        "深圳A店", "香港H店",
    }


def test_period_summary_scope_filters_rows_before_aggregation():
    temp = tempfile.TemporaryDirectory(prefix="workbuddy-accounting-scope-")
    original = db.DB_PATH
    db.DB_PATH = os.path.join(temp.name, "app.db")
    db.init_db()
    conn = db.get_connection()
    try:
        _insert(conn, "2026-08-31", "深圳A店", "meituan", {"美团收款": 100})
        _insert(conn, "2026-08-31", "广州B店", "meituan", {"美团收款": 200})
        conn.commit()
        data = accounting_summary.load_period_summary_data("2026-08-31", {"深圳A店"})
    finally:
        conn.close()
        db.DB_PATH = original
        temp.cleanup()

    assert {item["场地"] for item in data} == {"深圳A店"}


def test_period_summary_metadata_reports_cutoffs_and_platform_states():
    temp = tempfile.TemporaryDirectory(prefix="workbuddy-accounting-meta-")
    original = db.DB_PATH
    db.DB_PATH = os.path.join(temp.name, "app.db")
    db.init_db()
    conn = db.get_connection()
    try:
        _insert(conn, "2026-08-31", "深圳A店", "meituan", {"美团收款": 100})
        _insert(conn, "2026-08-30", "深圳A店", "kpay", {"Kpay收款": 10})
        _insert(conn, "2026-08-31", "深圳A店", "new_system", {"新系统积分增加": 3})
        conn.execute(
            "INSERT INTO platform_collection_settings(platform, enabled) VALUES (?, ?)",
            ("kpay", 0),
        )
        conn.execute(
            "INSERT INTO tasks(id, platform, date, status, error_msg, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("task-meituan", "meituan", "2026-08-31", "failed", "上游返回错误", "2026-08-31 12:00:00"),
        )
        conn.commit()

        data, metadata = accounting_summary.load_period_summary_data(
            "2026-08-31",
            include_metadata=True,
            platform_catalog=[
                ("meituan", "美团"),
                ("kpay", "KPay"),
                ("yuntai", "芸苔"),
                ("new_system", "新系统"),
            ],
        )
    finally:
        conn.close()
        db.DB_PATH = original
        temp.cleanup()

    assert len(data) == 3
    states = {item["platform"]: item for item in metadata["platforms"]}
    assert metadata["target_date"] == "2026-08-31"
    assert metadata["cutoff_date_min"] == "2026-08-30"
    assert metadata["cutoff_date_max"] == "2026-08-31"
    assert states["meituan"]["status"] == "task_failed"
    assert states["meituan"]["latest_date"] == "2026-08-31"
    assert states["kpay"]["status"] == "disabled"
    assert states["kpay"]["latest_date"] == "2026-08-30"
    assert states["yuntai"]["status"] == "missing"
    assert states["new_system"]["status"] == "current"


def test_accounting_scope_excludes_stores_whose_numeric_data_is_all_zero():
    data = [
        {"场地": "零值店", "美团收款": 0, "鲸舰现金": "0.00", "备注": "已采集"},
        {"场地": "空白店", "美团收款": None, "状态": "完成"},
        {"场地": "正常店", "美团收款": "1,200.50"},
        {"场地": "退款店", "美团收款": -5},
    ]
    assert accounting_summary.accounting_venue_scope(data) == {"正常店", "退款店"}


def test_report_summary_appends_period_store_missing_from_current_roster():
    dataframe = pd.DataFrame([
        {"序号": 1, "负责人": "张三", "场地": "深圳A店", "美团收款": None},
    ])
    data = [{"场地": "香港H店", "美团收款": 88}]
    with mock.patch.object(
        report_summary,
        "_get_base_df",
        return_value=(dataframe, list(dataframe.columns)),
    ):
        result = report_summary.main(
            data,
            active_venues={"深圳A店", "香港H店"},
        )

    venue_index = result[0].index("场地")
    income_index = result[0].index("美团收款")
    rows = {row[venue_index]: row for row in result[1:]}
    assert rows["香港H店"][income_index] == 88
    assert set(rows) == {"深圳A店", "香港H店", "合计"}
