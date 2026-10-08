# -*- coding: utf-8 -*-
"""数据质量规则测试：累计回退与收入异常均为确定性计算。"""

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.data_quality as data_quality


detect_cumulative_regressions = data_quality.detect_cumulative_regressions
detect_income_anomalies = data_quality.detect_income_anomalies


def _row(date, venue, platform, metrics):
    return {
        "date": date,
        "venue": venue,
        "platform": platform,
        "metrics_json": json.dumps(metrics, ensure_ascii=False),
    }


def test_detect_cumulative_regression():
    previous = [_row("2026-08-10", "深圳A店", "yuntai", {"芸苔现金": 150, "芸苔投币": 20})]
    current = [_row("2026-08-11", "深圳A店", "yuntai", {"芸苔现金": 120, "芸苔投币": 25})]

    issues = detect_cumulative_regressions(current, previous)

    assert len(issues) == 1
    assert issues[0]["metric"] == "芸苔现金"
    assert issues[0]["previous"] == 150
    assert issues[0]["current"] == 120


def test_detect_income_anomaly():
    snapshots = [
        ("2026-08-01", {"深圳A店": 100}),
        ("2026-08-02", {"深圳A店": 205}),
        ("2026-08-03", {"深圳A店": 303}),
        ("2026-08-04", {"深圳A店": 407}),
        ("2026-08-05", {"深圳A店": 505}),
        ("2026-08-06", {"深圳A店": 805}),
    ]

    issues = detect_income_anomalies(snapshots, threshold=2.0)

    assert len(issues) == 1
    assert issues[0]["venue"] == "深圳A店"
    assert issues[0]["direction"] == "up"
    assert issues[0]["daily_income"] == 300


def test_detect_income_anomaly_skips_collection_gap():
    snapshots = [
        ("2026-08-01", {"深圳A店": 100}),
        ("2026-08-02", {"深圳A店": 205}),
        ("2026-08-03", {"深圳A店": 303}),
        ("2026-08-04", {"深圳A店": 407}),
        ("2026-08-06", {"深圳A店": 805}),
    ]

    assert detect_income_anomalies(snapshots, threshold=2.0) == []


def test_comparison_uses_calendar_previous_day_and_skips_month_start():
    original_get_connection = data_quality.get_connection
    original_platforms = data_quality._platforms
    original_targets = data_quality.load_store_targets
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "quality.db")
            connect = _quality_database(database_path)
            data_quality.get_connection = connect
            data_quality._platforms = lambda: {"meituan": "美团"}
            data_quality.load_store_targets = lambda month=None: {}

            conn = connect()
            try:
                conn.executemany(
                    "INSERT INTO daily_summary VALUES (?,?,?,?,?,?)",
                    [
                        ("summary-1", "2026-08-01", "深圳A店", "meituan", json.dumps({"美团收款": 100}), "2026-08-01 16:00:00"),
                        ("summary-3", "2026-08-03", "深圳A店", "meituan", json.dumps({"美团收款": 120}), "2026-08-03 16:00:00"),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            report = data_quality.inspect("2026-08-03")
            assert report["comparison"] == {
                "date": "2026-08-02",
                "available": False,
                "skipped": False,
            }
            assert any(issue["type"] == "previous_day_missing" for issue in report["issues"])

            first_day = data_quality.inspect("2026-08-01")
            assert first_day["comparison"] == {
                "date": None,
                "available": False,
                "skipped": True,
            }
            assert not any(issue["type"] == "previous_day_missing" for issue in first_day["issues"])
    finally:
        data_quality.get_connection = original_get_connection
        data_quality._platforms = original_platforms
        data_quality.load_store_targets = original_targets


def _quality_database(path):
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    conn = connect()
    try:
        conn.executescript(
            """
            CREATE TABLE daily_summary (
                id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                venue TEXT NOT NULL,
                platform TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                updated_at TIMESTAMP
            );
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                date TEXT NOT NULL,
                status TEXT NOT NULL,
                error_msg TEXT,
                created_at TIMESTAMP,
                finished_at TIMESTAMP
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
    return connect


def test_existing_data_with_latest_failed_or_stopped_task_is_retryable():
    original_get_connection = data_quality.get_connection
    original_platforms = data_quality._platforms
    original_targets = data_quality.load_store_targets
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "quality.db")
            connect = _quality_database(database_path)
            data_quality.get_connection = connect
            data_quality._platforms = lambda: {"meituan": "美团"}
            data_quality.load_store_targets = lambda month=None: {}

            conn = connect()
            try:
                conn.execute(
                    "INSERT INTO daily_summary VALUES (?,?,?,?,?,?)",
                    (
                        "summary-1", "2026-08-24", "深圳A店", "meituan",
                        json.dumps({"美团收款": 100}, ensure_ascii=False),
                        "2026-08-24 16:10:00",
                    ),
                )
                conn.execute(
                    "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                    (
                        "task-failed", "meituan", "2026-08-24", "failed",
                        "凭证失效", "2026-08-25 10:10:00", "2026-08-25 10:10:01",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            report = data_quality.inspect("2026-08-24")

            assert report["missing_platforms"] == []
            assert report["retry_platforms"] == ["meituan"]
            issue = next(item for item in report["issues"] if item["type"] == "platform_failed")
            assert issue["platform"] == "meituan"
            assert "旧数据" in issue["message"]

            conn = connect()
            try:
                conn.execute(
                    "UPDATE tasks SET status='stopped', error_msg='管理员停止' WHERE id='task-failed'"
                )
                conn.commit()
            finally:
                conn.close()

            stopped_report = data_quality.inspect("2026-08-24")
            assert stopped_report["retry_platforms"] == ["meituan"]
            assert any(
                item["type"] == "platform_failed" and item["status"] == "stopped"
                for item in stopped_report["issues"]
            )
    finally:
        data_quality.get_connection = original_get_connection
        data_quality._platforms = original_platforms
        data_quality.load_store_targets = original_targets


def test_latest_success_removes_existing_platform_from_retry_platforms():
    original_get_connection = data_quality.get_connection
    original_platforms = data_quality._platforms
    original_targets = data_quality.load_store_targets
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "quality.db")
            connect = _quality_database(database_path)
            data_quality.get_connection = connect
            data_quality._platforms = lambda: {"meituan": "美团"}
            data_quality.load_store_targets = lambda month=None: {}

            conn = connect()
            try:
                conn.execute(
                    "INSERT INTO daily_summary VALUES (?,?,?,?,?,?)",
                    (
                        "summary-1", "2026-08-24", "深圳A店", "meituan",
                        json.dumps({"美团收款": 100}, ensure_ascii=False),
                        "2026-08-24 16:10:00",
                    ),
                )
                conn.executemany(
                    "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                    [
                        (
                            "task-failed", "meituan", "2026-08-24", "failed",
                            "凭证失效", "2026-08-25 10:10:00", "2026-08-25 10:10:01",
                        ),
                        (
                            "task-success", "meituan", "2026-08-24", "success",
                            None, "2026-08-25 10:20:00", "2026-08-25 10:20:05",
                        ),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            report = data_quality.inspect("2026-08-24")

            assert report["missing_platforms"] == []
            assert report["retry_platforms"] == []
            assert not any(item["type"] == "platform_failed" for item in report["issues"])
    finally:
        data_quality.get_connection = original_get_connection
        data_quality._platforms = original_platforms
        data_quality.load_store_targets = original_targets


def main():
    test_detect_cumulative_regression()
    test_detect_income_anomaly()
    test_comparison_uses_calendar_previous_day_and_skips_month_start()
    test_existing_data_with_latest_failed_or_stopped_task_is_retryable()
    test_latest_success_removes_existing_platform_from_retry_platforms()
    print("数据质量规则测试：全部通过（5 项）")


if __name__ == "__main__":
    main()
