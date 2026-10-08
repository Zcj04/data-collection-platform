# -*- coding: utf-8 -*-
"""会员资产自动监控排程测试。"""

from datetime import datetime

from core.monitor_sync import MonitorAutoScheduler, _next_monitor_schedule


class _Manager:
    def __init__(self):
        self.calls = []

    def start(self, start_date, end_date):
        self.calls.append((start_date, end_date))
        return True


def test_next_schedule_uses_daily_reconciliation_then_half_hour_slots():
    next_at, label = _next_monitor_schedule(datetime(2026, 8, 21, 9, 50))
    assert next_at == datetime(2026, 8, 21, 10, 0)
    assert label == "昨日全量核对"

    next_at, label = _next_monitor_schedule(datetime(2026, 8, 21, 10, 0))
    assert next_at == datetime(2026, 8, 21, 10, 30)
    assert label == "今日实时同步"

    next_at, label = _next_monitor_schedule(datetime(2026, 8, 21, 19, 0))
    assert next_at == datetime(2026, 8, 22, 10, 0)
    assert label == "昨日全量核对"


def test_realtime_slot_starts_today_once(monkeypatch):
    manager = _Manager()
    scheduler = MonitorAutoScheduler(manager)
    monkeypatch.setattr("core.monitor_sync.has_completed_monitor_sync", lambda *_: True)

    scheduler._trigger_due_run(datetime(2026, 8, 21, 10, 30))
    scheduler._trigger_due_run(datetime(2026, 8, 21, 10, 30, 20))

    assert manager.calls == [("2026-08-21", "2026-08-21")]


def test_daily_reconciliation_runs_yesterday_before_realtime(monkeypatch):
    manager = _Manager()
    scheduler = MonitorAutoScheduler(manager)
    monkeypatch.setattr("core.monitor_sync.has_completed_monitor_sync", lambda *_: False)

    scheduler._trigger_due_run(datetime(2026, 8, 21, 10, 0))

    assert manager.calls == [("2026-08-20", "2026-08-20")]
