# -*- coding: utf-8 -*-
"""数据保留策略：后台每日自动清理任务记录、排程记录与日志文件。

daily_summary 是按 (platform, date) 先删后插的覆盖式写入，小时级实时采集不会使其膨胀；
真正持续增长的是 tasks、scheduled_collection_runs 和 logs/*.log，本模块负责定期回收。
默认不清理 daily_summary（月累计减法依赖当月+上月快照，历史数据还用于报表）。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import get as config_get
from core.db import get_connection

logger = logging.getLogger(__name__)

DEFAULT_CHECK_INTERVAL_SECONDS = 600  # 每 10 分钟检查一次是否到点
DEFAULT_RUN_AT = "23:40"              # 每天清理时刻（避开 22:00 实时采集与次日 10:00 档）


def _int_setting(section: Dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(section.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(0, value)


def cleanup_once(
    now: Optional[datetime] = None,
    config: Optional[Dict[str, Any]] = None,
    log_dir: Optional[Path] = None,
) -> Dict[str, int]:
    """按保留策略执行一次清理，返回各表/目录的删除数量。"""
    now = now or datetime.now()
    section = config if config is not None else (config_get("scheduler.retention", {}) or {})
    task_days = _int_setting(section, "task_days", 180)
    run_days = _int_setting(section, "run_days", 365)
    log_days = _int_setting(section, "log_days", 30)
    summary_days = _int_setting(section, "daily_summary_days", 0)

    result: Dict[str, int] = {}
    conn = get_connection()
    try:
        if task_days > 0:
            cutoff = (now - timedelta(days=task_days)).strftime("%Y-%m-%d")
            cursor = conn.execute("DELETE FROM tasks WHERE date<?", (cutoff,))
            result["tasks"] = int(cursor.rowcount)
        if run_days > 0:
            cutoff_text = (now - timedelta(days=run_days)).isoformat(timespec="seconds")
            cursor = conn.execute(
                "DELETE FROM scheduled_collection_runs WHERE scheduled_for<?",
                (cutoff_text,),
            )
            result["scheduled_runs"] = int(cursor.rowcount)
        if summary_days > 0:
            cutoff = (now - timedelta(days=summary_days)).strftime("%Y-%m-%d")
            cursor = conn.execute("DELETE FROM daily_summary WHERE date<?", (cutoff,))
            result["daily_summary"] = int(cursor.rowcount)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if log_days > 0:
        result["log_files"] = _cleanup_log_files(now, log_days, log_dir or Path("logs"))
    if any(count for count in result.values()):
        logger.info("数据保留清理完成：%s", result)
    return result


def _cleanup_log_files(now: datetime, log_days: int, log_dir: Path) -> int:
    """删除日志目录下超过保留天数的日志文件（含轮转副本），失败逐个跳过。"""
    removed = 0
    if not log_dir.is_dir():
        return 0
    cutoff_ts = now.timestamp() - log_days * 86400
    for path in log_dir.glob("*.log*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff_ts:
                path.unlink()
                removed += 1
        except OSError:
            logger.warning("日志文件删除失败：%s", path)
    return removed


def _parse_run_at(value: Any) -> tuple[int, int]:
    text = str(value or DEFAULT_RUN_AT).strip() or DEFAULT_RUN_AT
    parts = text.split(":")
    hour = max(0, min(23, int(parts[0])))
    minute = max(0, min(59, int(parts[1]) if len(parts) > 1 else 0))
    return hour, minute


class MaintenanceScheduler:
    """每日在固定时刻执行一次 retention 清理的后台线程。"""

    def __init__(
        self,
        run_at: Optional[str] = None,
        check_interval_seconds: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        hour, minute = _parse_run_at(
            run_at if run_at is not None else config_get("scheduler.retention.run_at", DEFAULT_RUN_AT)
        )
        self._run_at = (hour, minute)
        interval = check_interval_seconds
        if interval is None:
            interval = int(config_get("scheduler.retention.check_interval_seconds", DEFAULT_CHECK_INTERVAL_SECONDS))
        self._check_interval = max(30, int(interval))
        if enabled is None:
            enabled = bool(config_get("scheduler.retention.enabled", True))
        self._enabled = bool(enabled)
        self._last_run_date: Optional[str] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not self._enabled:
            logger.info("数据保留清理已关闭（scheduler.retention.enabled=false）")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="retention-scheduler", daemon=True)
        self._thread.start()
        logger.info(
            "数据保留清理已启动：每日 %02d:%02d 执行（tasks/runs/logs 按 scheduler.retention 保留天数回收）",
            self._run_at[0], self._run_at[1],
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                now = datetime.now()
                today_text = now.strftime("%Y-%m-%d")
                due = (now.hour, now.minute) >= self._run_at
                if due and self._last_run_date != today_text:
                    cleanup_once(now)
                    self._last_run_date = today_text
            except Exception:
                logger.exception("数据保留清理执行失败")
            if self._stop_event.wait(self._check_interval):
                break

    def trigger_now(self) -> Dict[str, int]:
        """手动触发一次清理（调试/接口用）。"""
        result = cleanup_once()
        self._last_run_date = datetime.now().strftime("%Y-%m-%d")
        return result


retention_scheduler = MaintenanceScheduler()
