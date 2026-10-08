# -*- coding: utf-8 -*-
"""大屏当日和昨日的差值必须尊重月累计重置。"""

from unittest import mock

import pytest

import core.dashboard_data as dashboard


@pytest.mark.parametrize("date,snapshots,today,yesterday", [
    ("2026-09-01", {"2026-09-01": 100, "2026-08-31": 10000, "2026-08-30": 9900, "2026-08-01": 80}, 100, 100),
    ("2026-09-02", {"2026-09-02": 250, "2026-09-01": 100, "2026-08-02": 180}, 150, 100),
    ("2027-01-01", {"2027-01-01": 100, "2026-12-31": 5000, "2026-12-30": 4920, "2026-12-01": 60}, 100, 80),
    ("2026-08-31", {"2026-08-31": 10000, "2026-08-30": 9900, "2026-08-29": 9700, "2026-07-31": 9000}, 100, 200),
])
def test_month_boundary_totals_and_status(date, snapshots, today, yesterday):
    with mock.patch.object(dashboard, "_load_venue_income", side_effect=lambda day: {"A": snapshots[day]} if day in snapshots else None), \
            mock.patch.object(dashboard, "_active_venue_set", return_value={"A"}), \
            mock.patch.object(dashboard, "load_store_targets", return_value={"A": 10000}), \
            mock.patch.object(dashboard, "load_store_regions", return_value={}), \
            mock.patch.object(dashboard, "_snapshot_issues", return_value=[]), \
            mock.patch.object(dashboard, "_load_daily_trend", return_value=[]), \
            mock.patch.object(dashboard, "enrich_dashboard", side_effect=lambda result, *args: result):
        result = dashboard.get_dashboard_data(date)
        status = dashboard.get_dashboard_status(date)
    assert result["ready"] is True
    assert result["today_total"] == today
    assert result["yesterday_total"] == yesterday
    assert result["day_change"] == today - yesterday
    assert result["curr_month_total"] == snapshots[date]
    assert status["ready"] is True


def test_second_day_still_requires_first_day_snapshot():
    with mock.patch.object(dashboard, "_load_venue_income", side_effect=lambda day: {"A": 250} if day == "2026-09-02" else None):
        assert dashboard.get_dashboard_data("2026-09-02")["ready"] is False
