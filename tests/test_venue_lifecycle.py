# -*- coding: utf-8 -*-
"""门店生命周期按所选业务日期生效。"""

import json
import os
import tempfile
from types import SimpleNamespace
from unittest import mock

import core.daily_operations as daily_operations
import core.dashboard_data as dashboard_data
import core.db as db
from core import venue_lifecycle


def _temporary_db():
    temp = tempfile.TemporaryDirectory(prefix="workbuddy-lifecycle-")
    original = db.DB_PATH
    db.DB_PATH = os.path.join(temp.name, "app.db")
    db.init_db()
    return temp, original


def _insert(conn, day, amount):
    conn.execute(
        "INSERT INTO daily_summary "
        "(id,date,venue,platform,metrics_json,period_start,source_task_id,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            f"{day}-香港H店-meituan",
            day,
            "香港H店",
            "meituan",
            json.dumps({"美团收款": amount}, ensure_ascii=False),
            day[:8] + "01",
            "test-task",
            day + " 16:10:00",
        ),
    )


def test_lifecycle_boundaries_are_inclusive_and_fallback_is_preserved():
    temp, original = _temporary_db()
    try:
        venue_lifecycle.save_lifecycle("香港H店", "2026-07-01", "2026-08-31", "tester")
        assert venue_lifecycle.operating_venues_on(
            "2026-07-01", {"香港H店", "深圳A店"}, {"深圳A店"}
        ) == {"香港H店", "深圳A店"}
        assert venue_lifecycle.operating_venues_on(
            "2026-08-31", {"香港H店", "深圳A店"}, {"深圳A店"}
        ) == {"香港H店", "深圳A店"}
        assert venue_lifecycle.operating_venues_on(
            "2026-09-01", {"香港H店", "深圳A店"}, {"深圳A店"}
        ) == {"深圳A店"}
        with mock.patch.object(dashboard_data, "get_operating_venues", return_value=set()):
            assert dashboard_data._active_venue_set(
                {"香港H店"}, target_date="2026-08-31"
            ) == {"香港H店"}
            assert dashboard_data._active_venue_set(
                {"香港H店"}, target_date="2026-09-01"
            ) == set()
    finally:
        db.DB_PATH = original
        temp.cleanup()


def test_daily_operations_include_closed_store_only_during_its_business_dates():
    temp, original = _temporary_db()
    try:
        venue_lifecycle.save_lifecycle("香港H店", "2026-07-01", "2026-08-31", "tester")
        conn = db.get_connection()
        for day, amount in (
            ("2026-08-23", 500),
            ("2026-08-24", 600),
            ("2026-09-01", 700),
            ("2026-09-02", 800),
        ):
            _insert(conn, day, amount)
        conn.commit()
        conn.close()

        with mock.patch.object(daily_operations, "get_operating_venues", return_value=set()), \
                mock.patch.object(daily_operations, "_configured_platforms", return_value={"meituan": "美团"}):
            august = daily_operations.get_daily_operations("2026-08-24", "香港H店")
            september = daily_operations.get_daily_operations("2026-09-02", "香港H店")

        assert august["selected_venue"] == "香港H店"
        assert august["available_venues"] == ["香港H店"]
        assert august["known_income"] == 100.0
        assert september["selected_venue"] is None
        assert september["available_venues"] == []
        assert september["venues"] == []
    finally:
        db.DB_PATH = original
        temp.cleanup()


def test_dashboard_summary_restores_venues_missing_from_current_roster():
    temp, original = _temporary_db()
    try:
        conn = db.get_connection()
        _insert(conn, "2026-08-24", 600)
        conn.commit()
        conn.close()
        report_main = mock.Mock(return_value=[
            ["场地", "收入汇总"],
            ["香港H店", 600.0],
            ["合计", 600.0],
        ])
        with mock.patch(
            "importlib.import_module",
            return_value=SimpleNamespace(main=report_main),
        ):
            result = dashboard_data._load_venue_income("2026-08-24")
        assert result == {"香港H店": 600.0}
        assert report_main.call_args.kwargs["active_venues"] == {"香港H店"}
    finally:
        db.DB_PATH = original
        temp.cleanup()
