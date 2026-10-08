"""单日趋势只使用相邻自然日快照，缺失值不能伪装成零。"""

from unittest import mock

import pytest

import core.dashboard_data as dashboard


@pytest.mark.parametrize("start,end,snapshots,expected", [
    ("2026-08-01", "2026-08-04", {"2026-08-01": 100, "2026-08-03": 300, "2026-08-04": 380}, [100, None, None, 80]),
    ("2026-08-01", "2026-08-03", {"2026-08-01": 0, "2026-08-02": 100, "2026-08-03": 100}, [0, 100, 0]),
    ("2026-08-01", "2026-08-03", {"2026-08-02": 150, "2026-08-03": 200}, [None, None, 50]),
    ("2026-08-01", "2026-08-02", {}, [None, None]),
    ("2026-08-10", "2026-08-11", {"2026-08-09": 1000, "2026-08-10": 1100, "2026-08-11": 1080}, [100, -20]),
    ("2026-12-31", "2027-01-02", {"2026-12-30": 4900, "2026-12-31": 5000, "2027-01-01": 80, "2027-01-02": 180}, [100, 80, 100]),
])
def test_daily_trend_calendar_continuity(start, end, snapshots, expected):
    connection = mock.MagicMock()
    connection.execute.return_value.fetchall.return_value = [
        {"date": day} for day in sorted(snapshots) if start <= day <= end
    ]
    with mock.patch.object(dashboard, "get_connection", return_value=connection), \
            mock.patch.object(dashboard, "_load_venue_income", side_effect=lambda day: {"A": snapshots[day]} if day in snapshots else None):
        trend = dashboard._load_daily_trend(start, end)
    assert [item["daily"] for item in trend] == expected
    assert trend[0]["date"] == start
    assert trend[-1]["date"] == end
    assert [item["cumulative"] for item in trend] == [snapshots.get(item["date"]) for item in trend]


def test_daily_trend_uses_only_current_operating_venues():
    snapshots = {
        "2026-09-01": {"深圳A店": 100, "香港H店": 500},
        "2026-09-02": {"深圳A店": 140, "香港H店": 700},
    }
    with mock.patch.object(dashboard, "_load_venue_income", side_effect=snapshots.get):
        trend = dashboard._load_daily_trend(
            "2026-09-01",
            "2026-09-02",
            active_venues={"深圳A店"},
        )
    assert [item["daily"] for item in trend] == [100, 40]
    assert [item["cumulative"] for item in trend] == [100, 140]
