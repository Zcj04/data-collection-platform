# -*- coding: utf-8 -*-
"""经营汇总数据的数据库防重自动采集。"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from adapters.factory import build_adapters
from core.config import get as config_get
from core.collection_settings import enabled_platform_ids, filter_enabled_adapters
from core.db import get_connection
from core.scheduler import Scheduler
from core.task_manager import TaskManager


logger = logging.getLogger(__name__)
BUSINESS_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

DEFAULT_SLOT_CONFIG = (
    {
        "key": "yesterday-final",
        "label": "昨日正式数据",
        "at": "10:30",
        "target_day_offset": 1,
    },
)


@dataclass(frozen=True)
class CollectionSlot:
    key: str
    label: str
    run_at: time
    target_day_offset: int


def _business_now(value: Optional[datetime] = None) -> datetime:
    current = value or datetime.now(BUSINESS_TIMEZONE)
    if current.tzinfo is None:
        return current.replace(tzinfo=BUSINESS_TIMEZONE)
    return current.astimezone(BUSINESS_TIMEZONE)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _month_start(target_date: str) -> str:
    target = datetime.strptime(str(target_date), "%Y-%m-%d").date()
    return target.replace(day=1).isoformat()


def _parse_time(value: Any) -> time:
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    return datetime.strptime(str(value or "").strip(), "%H:%M").time()


def _load_slots(raw_slots: Optional[Iterable[Any]] = None) -> List[CollectionSlot]:
    raw = raw_slots
    if raw is None:
        raw = config_get("scheduler.auto_collect.slots", DEFAULT_SLOT_CONFIG)
    slots: List[CollectionSlot] = []
    for index, item in enumerate(raw or []):
        if isinstance(item, CollectionSlot):
            slots.append(item)
            continue
        if isinstance(item, str):
            slots.append(CollectionSlot(
                "slot-%s" % index,
                "自动采集 %s" % item,
                _parse_time(item),
                0,
            ))
            continue
        if not isinstance(item, dict):
            raise ValueError("自动采集时段必须是对象")
        key = str(item.get("key") or "slot-%s" % index).strip()
        label = str(item.get("label") or key).strip()
        offset = int(item.get("target_day_offset", 0))
        if not key or not label or offset < 0:
            raise ValueError("自动采集时段配置无效")
        slots.append(CollectionSlot(key, label, _parse_time(item.get("at")), offset))
    if not slots:
        raise ValueError("至少需要一个自动采集时段")
    return sorted(slots, key=lambda item: item.run_at)


def _scheduled_at(day: date, slot: CollectionSlot) -> datetime:
    return datetime.combine(day, slot.run_at, tzinfo=BUSINESS_TIMEZONE)


def _next_slot(now: datetime, slots: List[CollectionSlot]) -> tuple[datetime, CollectionSlot]:
    current = _business_now(now)
    for day_offset in (0, 1):
        day = current.date() + timedelta(days=day_offset)
        for slot in slots:
            scheduled_for = _scheduled_at(day, slot)
            if scheduled_for > current:
                return scheduled_for, slot
    raise RuntimeError("无法计算下一次自动采集时间")


def _claim_run(
    slot: CollectionSlot,
    scheduled_for: datetime,
    target_date: str,
    job_key: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    job_key = job_key or "%s:%s" % (slot.key, scheduled_for.date().isoformat())
    run_id = str(uuid.uuid4())
    now_text = _iso(_business_now())
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            "SELECT job_key FROM scheduled_collection_runs "
            "WHERE status IN ('pending','running') LIMIT 1"
        ).fetchone()
        if active is not None:
            conn.rollback()
            return None
        row = conn.execute(
            "SELECT id, status FROM scheduled_collection_runs WHERE job_key=?",
            (job_key,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO scheduled_collection_runs "
                "(id, job_key, slot_key, slot_label, target_date, scheduled_for, "
                "status, started_at) VALUES (?,?,?,?,?,?,'pending',?)",
                (
                    run_id,
                    job_key,
                    slot.key,
                    slot.label,
                    target_date,
                    _iso(scheduled_for),
                    now_text,
                ),
            )
        elif row["status"] == "expired":
            run_id = str(row["id"])
            conn.execute(
                "UPDATE scheduled_collection_runs SET status='pending', "
                "retry_count=retry_count+1, started_at=?, finished_at=NULL, "
                "message='' WHERE id=? AND status='expired'",
                (now_text, run_id),
            )
        else:
            conn.rollback()
            return None
        conn.commit()
        return {"id": run_id, "job_key": job_key}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _release_claim(run_id: str) -> None:
    conn = get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM scheduled_collection_runs WHERE id=? AND status='pending'",
            (run_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_running(run_id: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE scheduled_collection_runs SET status='running' "
            "WHERE id=? AND status='pending'",
            (run_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _finish_run(run_id: str, result: Dict[str, Any]) -> None:
    total = max(0, int(result.get("total") or 0))
    completed = max(0, int(result.get("completed") or 0))
    failed = max(int(result.get("failed") or 0), total - completed, 0)
    if total > 0 and completed == total:
        status = "success"
        message = "全部 %s 个平台采集成功" % total
    elif completed > 0:
        status = "partial"
        message = "%s/%s 个平台采集成功" % (completed, total)
    else:
        status = "failed"
        message = "本批次没有平台成功入库"
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE scheduled_collection_runs SET status=?, total_tasks=?, "
            "success_count=?, failed_count=?, finished_at=?, message=? "
            "WHERE id=? AND status IN ('pending','running')",
            (
                status,
                total,
                completed,
                failed,
                _iso(_business_now()),
                message,
                run_id,
            ),
        )
        conn.commit()
        updated = cursor.rowcount == 1
    finally:
        conn.close()

    if updated and status != "success":
        try:
            from core.alerts import record_alert

            record_alert(
                "collection.%s" % status,
                "error" if status == "failed" else "warning",
                "经营数据自动采集%s" % ("失败" if status == "failed" else "部分失败"),
                message,
                dedupe_key="collection:%s" % run_id,
            )
        except Exception:
            logger.exception("记录自动采集告警时出错")


def expire_stale_runs(
    now: Optional[datetime] = None,
    stale_after_seconds: Optional[int] = None,
) -> int:
    """服务重启时把未结束的排程标为过期，使同一时段可受控补跑。"""
    params: List[Any] = [_iso(_business_now(now))]
    age_clause = ""
    if stale_after_seconds is not None:
        cutoff = _business_now(now) - timedelta(seconds=max(0, stale_after_seconds))
        age_clause = " AND started_at<?"
        params.append(_iso(cutoff))
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE scheduled_collection_runs SET status='expired', "
            "finished_at=?, message='服务重启，自动采集批次中断' "
            "WHERE status IN ('pending','running')" + age_clause,
            tuple(params),
        )
        conn.commit()
        return int(cursor.rowcount)
    finally:
        conn.close()


def _find_snapshot_gap(target_date: str) -> Optional[str]:
    """找出月内已开始采集但中间缺失的最早累计快照日期。

    只检查已有快照之间的日期，不把首次启用平台时月初以前的历史当成缺口；
    当前目标日由正常排程负责，避免把尚未结束的当日数据当成正式快照。
    """
    target = datetime.strptime(str(target_date), "%Y-%m-%d").date()
    month_start = target.replace(day=1)
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_summary "
            "WHERE date>=? AND date<? AND platform<>'kpay'",
            (month_start.isoformat(), target.isoformat()),
        ).fetchall()
        successful_runs = {
            str(row["target_date"])
            for row in conn.execute(
                "SELECT DISTINCT target_date FROM scheduled_collection_runs "
                "WHERE status='success' AND target_date>=? AND target_date<?",
                (month_start.isoformat(), target.isoformat()),
            ).fetchall()
        }
    finally:
        conn.close()
    observed = {
        datetime.strptime(str(row["date"]), "%Y-%m-%d").date()
        for row in rows
    }
    if len(observed) < 2:
        return None
    first_seen, last_seen = min(observed), max(observed)
    current = first_seen
    while current <= last_seen:
        current_text = current.isoformat()
        if current not in observed and current_text not in successful_runs:
            return current_text
        current += timedelta(days=1)
    return None


def _default_launcher(
    start_date: str,
    end_date: str,
    on_complete: Callable[[Dict[str, Any]], None],
) -> bool:
    configured = config_get("platforms", {}) or {}
    platform_catalog = [
        (str(platform_id), str(item.get("name") or platform_id))
        for platform_id, item in configured.items()
        if isinstance(item, dict)
    ]
    adapters = filter_enabled_adapters(build_adapters(), platform_catalog)
    if not adapters:
        logger.warning("没有开启可参与自动采集的平台")
        return False
    scheduler = Scheduler(
        task_mgr=TaskManager(),
        max_workers=int(config_get("scheduler.max_workers", 12)),
        single_task_timeout=int(config_get("scheduler.single_task_timeout", 600)),
        process_isolation=bool(config_get("scheduler.process_isolation", True)),
    )
    return scheduler.launch_background(
        start_date,
        end_date,
        adapters,
        completion_callback=on_complete,
    )


class CollectionAutoScheduler:
    """按业务时段自动采集；数据库 job_key 保证每个时段只启动一次。"""

    def __init__(
        self,
        launcher: Optional[Callable[..., bool]] = None,
        slots: Optional[Iterable[Any]] = None,
        enabled: Optional[bool] = None,
        check_interval_seconds: Optional[int] = None,
    ) -> None:
        self._launcher = launcher or _default_launcher
        self._slots = _load_slots(slots)
        self._enabled = bool(
            config_get("scheduler.auto_collect.enabled", True)
            if enabled is None else enabled
        )
        configured_interval = config_get(
            "scheduler.auto_collect.check_interval_seconds", 30
        )
        self._check_interval = max(
            5,
            int(configured_interval if check_interval_seconds is None else check_interval_seconds),
        )
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if not self._enabled:
            logger.info("经营数据自动采集已关闭")
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="collection-auto-scheduler",
                daemon=True,
            )
            self._thread.start()
        logger.info(
            "经营数据自动采集已启动：%s",
            "；".join(
                "%s %s" % (slot.run_at.strftime("%H:%M"), slot.label)
                for slot in self._slots
            ),
        )

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.trigger_due(_business_now())
            except Exception:
                logger.exception("经营数据自动采集检查失败")
            if self._stop_event.wait(self._check_interval):
                break

    def trigger_due(self, now: Optional[datetime] = None) -> Optional[str]:
        if not self._enabled:
            return None
        current = _business_now(now)
        due = [
            (_scheduled_at(current.date(), slot), slot)
            for slot in self._slots
            if _scheduled_at(current.date(), slot) <= current
        ]
        for scheduled_for, slot in due:
            target = (scheduled_for.date() - timedelta(days=slot.target_day_offset)).isoformat()
            range_start = _month_start(target)
            gap_target = _find_snapshot_gap(target)
            if gap_target:
                catchup_slot = CollectionSlot(
                    "catchup", "累计快照缺口回填", current.timetz().replace(tzinfo=None), 0
                )
                claimed = _claim_run(
                    catchup_slot,
                    current,
                    gap_target,
                    job_key="catchup:%s" % gap_target,
                )
                if claimed is None:
                    continue
                run_id = claimed["id"]
                try:
                    accepted = self._launcher(
                        _month_start(gap_target),
                        gap_target,
                        on_complete=lambda result, rid=run_id: _finish_run(rid, result),
                    )
                except Exception as error:
                    _finish_run(run_id, {
                        "total": 0,
                        "completed": 0,
                        "failed": 0,
                        "fatal_error": str(error)[:500],
                    })
                    logger.exception("累计快照缺口回填启动失败：%s", gap_target)
                    return None
                if not accepted:
                    _release_claim(run_id)
                    return None
                _mark_running(run_id)
                logger.info("累计快照缺口回填启动：%s 至 %s", _month_start(gap_target), gap_target)
                return claimed["job_key"]
            claimed = _claim_run(slot, scheduled_for, target)
            if claimed is None:
                continue
            run_id = claimed["id"]
            try:
                accepted = self._launcher(
                    range_start,
                    target,
                    on_complete=lambda result, rid=run_id: _finish_run(rid, result),
                )
            except Exception as error:
                _finish_run(run_id, {
                    "total": 0,
                    "completed": 0,
                    "failed": 0,
                    "fatal_error": str(error)[:500],
                })
                logger.exception("自动采集启动失败：%s", slot.label)
                return None
            if not accepted:
                _release_claim(run_id)
                return None
            _mark_running(run_id)
            logger.info(
                "自动采集启动：%s / %s 至 %s",
                slot.label,
                range_start,
                target,
            )
            return claimed["job_key"]
        return None

    _trigger_due_run = trigger_due

    def complete_run(
        self,
        job_key: str,
        platform_results: Dict[str, str],
        error_msg: str = "",
    ) -> None:
        """按平台结果结束批次；错误详情不写入门户可读状态。"""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM scheduled_collection_runs WHERE job_key=?",
                (job_key,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ValueError("自动采集批次不存在")
        values = [str(value or "").lower() for value in platform_results.values()]
        completed = sum(value == "success" for value in values)
        _finish_run(str(row["id"]), {
            "total": len(values),
            "completed": completed,
            "failed": len(values) - completed,
        })

    finish_run = complete_run

    @staticmethod
    def expire_stale_runs(
        now: Optional[datetime] = None,
        stale_after_seconds: Optional[int] = None,
    ) -> int:
        return expire_stale_runs(now, stale_after_seconds)

    def status(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        current = _business_now(now)
        next_at, next_collection_slot = _next_slot(current, self._slots)
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT slot_label, target_date, status, scheduled_for, started_at, "
                "finished_at, total_tasks, success_count, failed_count "
                "FROM scheduled_collection_runs "
                "ORDER BY scheduled_for DESC, started_at DESC LIMIT 1"
            ).fetchone()
            latest_task_rows = []
            if row is not None:
                latest_task_rows = conn.execute(
                    "SELECT platform, status FROM tasks t1 "
                    "WHERE date=? AND created_at=("
                    " SELECT MAX(created_at) FROM tasks t2 "
                    " WHERE t2.platform=t1.platform AND t2.date=?"
                    ")",
                    (row["target_date"], row["target_date"]),
                ).fetchall()
        finally:
            conn.close()
        last_run = dict(row) if row else None
        if last_run is not None:
            last_run["start_date"] = _month_start(last_run["target_date"])
            configured = config_get("platforms", {}) or {}
            catalog = [
                (str(platform), str((item or {}).get("name") or platform))
                for platform, item in configured.items()
                if isinstance(item, dict)
            ] if isinstance(configured, dict) else []
            configured_count = len(enabled_platform_ids(catalog))
            stored_total = last_run.get("total_tasks")
            expected_count = int(stored_total) if stored_total is not None else configured_count
            latest_success_count = len({
                str(task["platform"])
                for task in latest_task_rows
                if str(task["status"] or "") == "success"
            })
            last_run["resolved"] = bool(
                last_run.get("status") == "success"
                or (expected_count > 0 and latest_success_count >= expected_count)
            )
        next_target_date = (
            next_at.date() - timedelta(days=next_collection_slot.target_day_offset)
        ).isoformat() if self._enabled else None
        return {
            "enabled": self._enabled,
            "next_run_at": _iso(next_at) if self._enabled else None,
            "next_label": next_collection_slot.label if self._enabled else None,
            "next_start_date": _month_start(next_target_date) if next_target_date else None,
            "next_target_date": next_target_date,
            "last_run": last_run,
        }


def _timestamp_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def get_collection_freshness(
    target_date: str,
    expected_platforms: Optional[Iterable[str]] = None,
    scheduler: Optional[CollectionAutoScheduler] = None,
) -> Dict[str, Any]:
    datetime.strptime(str(target_date), "%Y-%m-%d")
    configured = config_get("platforms", {}) or {}
    if expected_platforms is None:
        catalog = [
            (str(platform), str((item or {}).get("name") or platform))
            for platform, item in configured.items()
            if isinstance(item, dict)
        ] if isinstance(configured, dict) else []
        expected = len(enabled_platform_ids(catalog))
    elif isinstance(expected_platforms, int):
        expected = max(0, expected_platforms)
    else:
        expected = len(tuple(expected_platforms))
    conn = get_connection()
    try:
        summary = conn.execute(
            "SELECT COUNT(*) AS row_count, COUNT(DISTINCT platform) AS source_count, "
            "MIN(updated_at) AS oldest_updated_at, MAX(updated_at) AS updated_at "
            "FROM daily_summary WHERE date=?",
            (target_date,),
        ).fetchone()
        latest_date_row = conn.execute(
            "SELECT MAX(date) AS latest_date FROM daily_summary"
        ).fetchone()
        tasks = conn.execute(
            "SELECT platform, status, created_at, finished_at FROM tasks t1 "
            "WHERE date=? AND created_at=("
            " SELECT MAX(created_at) FROM tasks t2 "
            " WHERE t2.platform=t1.platform AND t2.date=?"
            ")",
            (target_date, target_date),
        ).fetchall()
        scheduled_run = conn.execute(
            "SELECT status, failed_count, started_at FROM scheduled_collection_runs "
            "WHERE target_date=? ORDER BY scheduled_for DESC, started_at DESC LIMIT 1",
            (target_date,),
        ).fetchone()
    finally:
        conn.close()

    statuses = [str(row["status"] or "") for row in tasks]
    running_count = sum(status in ("pending", "running") for status in statuses)
    failed_count = sum(status in ("failed", "stopped") for status in statuses)
    attempt_values = [str(row["created_at"] or "") for row in tasks if row["created_at"]]
    scheduled_status = str(scheduled_run["status"] or "") if scheduled_run else ""
    latest_task_attempt = max(
        attempt_values,
        key=lambda value: value.replace(" ", "T"),
    ) if attempt_values else ""
    scheduled_attempt = str(scheduled_run["started_at"] or "") if scheduled_run else ""
    scheduled_is_latest = bool(
        scheduled_attempt
        and (
            not latest_task_attempt
            or scheduled_attempt.replace(" ", "T") >= latest_task_attempt.replace(" ", "T")
        )
    )
    effective_scheduled_status = scheduled_status if scheduled_is_latest else ""
    if effective_scheduled_status in ("pending", "running"):
        running_count = max(1, running_count)
    if scheduled_run:
        if scheduled_is_latest:
            failed_count = max(failed_count, int(scheduled_run["failed_count"] or 0))
        if scheduled_run["started_at"]:
            attempt_values.append(str(scheduled_run["started_at"]))
    row_count = int(summary["row_count"] or 0)
    sources_present = int(summary["source_count"] or 0)
    if running_count:
        state = "collecting"
    elif not row_count:
        state = "failed" if failed_count or effective_scheduled_status in (
            "failed", "expired"
        ) else "missing"
    elif failed_count or effective_scheduled_status in (
        "partial", "failed", "expired"
    ) or (
        expected and sources_present < expected
    ):
        state = "partial"
    else:
        state = "complete"

    active_scheduler = scheduler or collection_auto_scheduler
    return {
        "target_date": target_date,
        "latest_data_date": str(latest_date_row["latest_date"] or "") or None,
        "state": state,
        "data_updated_at": _timestamp_text(summary["updated_at"]),
        "oldest_data_updated_at": _timestamp_text(summary["oldest_updated_at"]),
        "row_count": row_count,
        "sources_present": sources_present,
        "sources_expected": expected,
        "running_count": running_count,
        "failed_count": failed_count,
        "last_attempt_at": _timestamp_text(
            max(attempt_values, key=lambda value: value.replace(" ", "T"))
            if attempt_values else None
        ),
        "schedule": active_scheduler.status(),
    }


collection_auto_scheduler = CollectionAutoScheduler()
