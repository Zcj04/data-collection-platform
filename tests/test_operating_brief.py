"""老板日报的缺基线、跨月比较、权限和区域加权比例回归。"""
import json

import pytest

from core import db, daily_operations as daily, dashboard_data as dashboard, operating_brief as brief


@pytest.fixture
def snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))
    db.init_db()
    monkeypatch.setattr(daily, "_active_venues", lambda *a, **k: {"A", "B", "private"})
    monkeypatch.setattr(daily, "_configured_platforms", lambda: {"meituan": "美团"})
    monkeypatch.setattr(daily, "load_store_regions", lambda: {"A": "深圳", "B": "深圳"})

    def insert(day, venue, amount, start=None):
        with db.get_connection() as conn:
            conn.execute("INSERT INTO daily_summary(id,date,venue,platform,metrics_json,period_start) VALUES(?,?,?,?,?,?)",
                         (day + venue, day, venue, "meituan", json.dumps({"美团收款": amount}), start or day[:7] + "-01"))
    return insert


def test_missing_new_store_baseline_does_not_become_daily_income(snapshots, monkeypatch):
    for day, venue, amount in [("2026-09-07", "A", 100), ("2026-09-08", "A", 130),
                               ("2026-09-14", "A", 200), ("2026-09-15", "A", 231),
                               ("2026-09-15", "B", 8134.2), ("2026-09-15", "private", 900000)]:
        snapshots(day, venue, amount)
    result = daily.get_daily_operations("2026-09-15", venue_scope=(v for v in ["A", "B"]))
    assert result["known_income"] == 31
    assert result["total_income"] is None
    assert result["previous_week"]["change_pct"] == 3.33
    assert not result["previous_week"]["complete"]
    assert "private" not in str(result)
    assert next(v for v in result["venues"] if v["venue"] == "B")["total_income"] is None

    # 公共月度接口必须使用同一个单日结果，包括趋势和新店。
    monkeypatch.setattr(dashboard, "_active_venue_set", lambda *a, **k: {"A", "B"})
    monkeypatch.setattr(dashboard, "load_store_targets", lambda *a: {"A": 1000, "B": 1000})
    monkeypatch.setattr(dashboard, "load_store_regions", lambda: {})
    monkeypatch.setattr(dashboard, "_snapshot_issues", lambda *a: [])
    monkeypatch.setattr(dashboard, "_load_venue_income", lambda day, *a: {"A": 231, "B": 8134.2} if day == "2026-09-15" else {"A": 200})
    monkeypatch.setattr(dashboard, "_load_daily_trend", lambda *a, **k: [{"date": "2026-09-15", "daily": 8165.2}])
    monkeypatch.setattr(brief, "_load_metrics", lambda *a: ({}, []))
    month = dashboard.get_dashboard_data("2026-09-15", {"A", "B"})
    assert month["today_total"] is None
    assert month["today_known_income"] == result["known_income"]
    assert month["daily_trend"][0]["daily"] == 31
    assert all(name != "B" for name, _ in month["top_today_gain"])
    assert "数据待核对" in month["brief_text"]
    assert "private" not in month["brief_text"]


def test_week_comparison_crosses_month_and_keeps_precision(snapshots):
    snapshots("2026-08-24", "A", 100000)
    snapshots("2026-08-25", "A", 130198.56)
    snapshots("2026-09-01", "A", 30852.6)
    result = daily.get_daily_operations("2026-09-01", venue_scope={"A"})
    assert result["known_income"] == 30852.6
    assert result["previous_week"]["income"] == 30198.56
    assert result["previous_week"]["change_pct"] == 2.17
    assert result["previous_week"]["complete"]


def test_no_baseline_and_wrong_period_are_unknown(snapshots):
    snapshots("2026-09-15", "A", 8134.2)
    result = daily.get_daily_operations("2026-09-15", venue_scope={"A"})
    assert result["known_income"] is None
    assert result["previous_week"]["change_pct"] is None
    snapshots("2026-09-14", "A", 500, "2026-08-01")
    assert daily.get_daily_operations("2026-09-15", venue_scope={"A"})["known_income"] is None


def test_region_ratio_uses_sums_retains_negative_and_marks_missing(snapshots, monkeypatch):
    for day, amount in [("2026-09-14", 100), ("2026-09-15", 150)]:
        snapshots(day, "A", amount)
    monkeypatch.setattr("core.targets.load_store_regions", lambda: {"A": "深圳", "B": "深圳"})
    metrics = {"A": {"收入汇总": 150, "总货款": -30, "投币合计": 100, "出货合计": 2},
               "B": {"收入汇总": 50, "总货款": 10}}
    rows = [{"venue": v, "platform": "payment", "period_start": "2026-09-01"} for v in ("A", "B")]
    monkeypatch.setattr(brief, "_load_metrics", lambda *a: (metrics, rows))
    def base():
        return dict(month_target=0.1, target_actual=200, days_in_month=30, days_passed=30,
                    store_completion=[dict(venue=v, target_available=True, pct=20) for v in ("A", "B")],
                    region_completion=[dict(region="深圳", actual=200, pct=20)], daily_trend=[],
                    completion_rate=20, target_date="2026-09-15", curr_month_total=200,
                    month_change_pct=None, last_month_total=None, top_cn=[])
    result = brief.enrich_dashboard(base(), "2026-09-15", {"A", "B"})
    assert result["region_completion"][0]["payment_pct"] == -10
    assert result["operating_ratios"]["coin_out_ratio"] == 50
    assert result["target_progress"]["needed_daily"] is None  # 月末不除零
    rows.pop()
    result = brief.enrich_dashboard(base(), "2026-09-15", {"A", "B"})
    assert result["region_completion"][0]["payment_pct"] is None
    assert result["region_completion"][0]["payment_missing"] == ["B"]
    assert result["operating_ratios"]["payment_pct"] is None


def test_zero_week_baseline_has_amount_but_no_percentage(snapshots):
    for day, value in [("2026-09-07", 0), ("2026-09-08", 0), ("2026-09-14", 10), ("2026-09-15", 20)]:
        snapshots(day, "A", value)
    comparison = daily.get_daily_operations("2026-09-15", venue_scope={"A"})["previous_week"]
    assert comparison["change"] == 10
    assert comparison["change_pct"] is None
