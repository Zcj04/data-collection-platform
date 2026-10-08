# -*- coding: utf-8 -*-
"""会员资产监控的后台同步管理器。"""

import logging
import threading
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.credential_manager import CredentialManager
from core.monitoring import (
    begin_monitor_sync,
    finish_monitor_sync,
    has_completed_monitor_sync,
    save_monitor_events,
)
from crawlers.duojinbao_store_value_crawler import collect_store_value_events


logger = logging.getLogger(__name__)


DAILY_RECONCILIATION_TIME = time(10, 0)
REALTIME_FIRST_RUN_TIME = time(10, 30)
REALTIME_LAST_RUN_TIME = time(19, 0)


def _next_monitor_schedule(now: Optional[datetime] = None) -> Tuple[datetime, str]:
    """返回下一次固定监控任务；时间使用服务所在机器的本地时间。"""
    current = now or datetime.now()
    today = current.date()
    candidates: List[Tuple[datetime, str]] = [
        (
            datetime.combine(today, DAILY_RECONCILIATION_TIME),
            "昨日全量核对",
        )
    ]
    slot = datetime.combine(today, REALTIME_FIRST_RUN_TIME)
    end = datetime.combine(today, REALTIME_LAST_RUN_TIME)
    while slot <= end:
        candidates.append((slot, "今日实时同步"))
        slot += timedelta(minutes=30)
    for scheduled_at, label in candidates:
        if scheduled_at > current:
            return scheduled_at, label
    tomorrow = today + timedelta(days=1)
    return datetime.combine(tomorrow, DAILY_RECONCILIATION_TIME), "昨日全量核对"


class MonitorAutoScheduler:
    """工作时段自动启动会员资产同步，不与人工同步并行。"""

    def __init__(self, manager: "MonitorSyncManager") -> None:
        self._manager = manager
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._started_slots: set[str] = set()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="monitor-auto-scheduler",
                daemon=True,
            )
            self._thread.start()
        logger.info("会员资产自动监控已启动：10:00 核对昨日，10:30–19:00 每 30 分钟同步今日")

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def status(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        next_at, next_label = _next_monitor_schedule(now)
        with self._lock:
            is_running = bool(self._thread and self._thread.is_alive())
        return {
            "enabled": is_running,
            "daily_reconciliation_at": DAILY_RECONCILIATION_TIME.strftime("%H:%M"),
            "realtime_window": "%s–%s，每 30 分钟" % (
                REALTIME_FIRST_RUN_TIME.strftime("%H:%M"),
                REALTIME_LAST_RUN_TIME.strftime("%H:%M"),
            ),
            "next_run_at": next_at.isoformat(timespec="minutes"),
            "next_run_label": next_label,
        }

    def _run(self) -> None:
        while not self._stop_event.wait(10):
            try:
                self._trigger_due_run(datetime.now())
            except Exception:
                logger.exception("自动监控调度检查失败")

    def _trigger_due_run(self, now: datetime) -> None:
        today = now.date()
        if DAILY_RECONCILIATION_TIME <= now.time() < REALTIME_FIRST_RUN_TIME:
            daily_slot = "reconciliation:%s" % today.isoformat()
            if daily_slot not in self._started_slots:
                yesterday = today - timedelta(days=1)
                if has_completed_monitor_sync(yesterday, today):
                    self._started_slots.add(daily_slot)
                elif self._manager.start(yesterday.isoformat(), yesterday.isoformat()):
                    self._started_slots.add(daily_slot)
                    logger.info("自动监控启动：昨日全量核对 %s", yesterday.isoformat())
                    return

        current_time = now.time()
        is_realtime_slot = (
            REALTIME_FIRST_RUN_TIME <= current_time <= REALTIME_LAST_RUN_TIME
            and now.minute in (0, 30)
        )
        if not is_realtime_slot:
            return
        realtime_slot = "realtime:%s:%02d:%02d" % (
            today.isoformat(), now.hour, now.minute,
        )
        if realtime_slot in self._started_slots:
            return
        if self._manager.start(today.isoformat(), today.isoformat()):
            self._started_slots.add(realtime_slot)
            logger.info("自动监控启动：今日实时同步 %s", today.isoformat())


class MonitorSyncManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {
            "status": "idle",
            "message": "等待同步",
            "event_count": 0,
            "stores_total": 0,
            "stores_succeeded": 0,
            "error_count": 0,
        }

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def start(self, start_date: str, end_date: str) -> bool:
        start_date = str(start_date or "").strip()
        end_date = str(end_date or "").strip()
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        if start > end:
            raise ValueError("start_date 不能晚于 end_date")
        with self._lock:
            if self._state.get("status") == "running":
                return False
            run_id = begin_monitor_sync(
                "duojinbao_store_value", "balance", start_date, end_date
            )
            self._state = {
                "status": "running",
                "message": "正在连接多金宝",
                "run_id": run_id,
                "start_date": start_date,
                "end_date": end_date,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "event_count": 0,
                "stores_total": 0,
                "stores_succeeded": 0,
                "error_count": 0,
            }
        thread = threading.Thread(
            target=self._run,
            args=(run_id, start_date, end_date),
            name="monitor-duojinbao-store-value",
            daemon=True,
        )
        thread.start()
        return True

    def _accounts(self) -> List[Dict[str, str]]:
        manager = CredentialManager()
        username = manager.get("duojinbao", "账号1")
        password = manager.get("duojinbao", "密码1")
        if not username or not password:
            return []
        return [{"username": username, "password": password}]

    def _progress(self, message: str) -> None:
        with self._lock:
            if self._state.get("status") == "running":
                self._state["message"] = str(message)[:200]

    def _run(self, run_id: str, start_date: str, end_date: str) -> None:
        try:
            result = collect_store_value_events(
                start_date,
                end_date,
                accounts=self._accounts(),
                progress_callback=self._progress,
            )
            events = result["events"]
            save_monitor_events(events)
            errors = result["errors"]
            stores_succeeded = int(result["stores_succeeded"])
            stores_total = int(result["stores_total"])
            if errors and stores_succeeded:
                final_status = "partial"
            elif errors or (stores_total and not stores_succeeded):
                final_status = "failed"
            else:
                final_status = "success"
            safe_error = "；".join(str(item) for item in errors[:5])
            if len(errors) > 5:
                safe_error += "；另有 %s 个错误" % (len(errors) - 5)
            finish_monitor_sync(
                run_id,
                final_status,
                stores_total=stores_total,
                stores_succeeded=stores_succeeded,
                event_count=len(events),
                error_count=len(errors),
                error_msg=safe_error,
            )
            if final_status != "success":
                try:
                    from core.alerts import record_alert

                    record_alert(
                        "monitor.%s" % final_status,
                        "error" if final_status == "failed" else "warning",
                        "会员资产监控%s" % (
                            "失败" if final_status == "failed" else "部分失败"
                        ),
                        safe_error or "部分门店同步未成功",
                        dedupe_key="monitor:%s" % run_id,
                    )
                except Exception:
                    logger.exception("记录会员监控告警时出错")
            message = (
                "同步完成：%s 家门店，%s 条变更" % (stores_succeeded, len(events))
                if final_status == "success"
                else "部分完成：%s/%s 家门店，%s 个错误" % (
                    stores_succeeded, stores_total, len(errors)
                )
                if final_status == "partial"
                else "同步失败：没有完整门店数据可用"
            )
            with self._lock:
                self._state.update({
                    "status": final_status,
                    "message": message,
                    "event_count": len(events),
                    "stores_total": stores_total,
                    "stores_succeeded": stores_succeeded,
                    "error_count": len(errors),
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                })
        except Exception as exc:
            logger.exception("多金宝储值同步失败")
            safe_error = str(exc)[:500]
            try:
                finish_monitor_sync(run_id, "failed", error_count=1, error_msg=safe_error)
            except Exception:
                logger.exception("写入监控同步失败状态时出错")
            try:
                from core.alerts import record_alert

                record_alert(
                    "monitor.failed",
                    "error",
                    "会员资产监控失败",
                    safe_error,
                    dedupe_key="monitor:%s" % run_id,
                )
            except Exception:
                logger.exception("记录会员监控告警时出错")
            with self._lock:
                self._state.update({
                    "status": "failed",
                    "message": "同步失败：%s" % safe_error,
                    "error_count": 1,
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                })


monitor_sync_manager = MonitorSyncManager()
monitor_auto_scheduler = MonitorAutoScheduler(monitor_sync_manager)
