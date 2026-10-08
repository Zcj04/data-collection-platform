# -*- coding: utf-8 -*-
"""任务状态管理"""

import uuid
import sqlite3
import json
from datetime import datetime, timedelta
from typing import List, Optional, Dict

from core.db import get_connection
from core.metric_rules import allows_decrease
from utils.redaction import redact_sensitive_text


class TaskManager:
    def __init__(self):
        pass

    def create(
        self,
        platform: str,
        date: str,
        venue: str = "",
        start_date: Optional[str] = None,
    ) -> str:
        """创建新任务，返回 task_id"""
        task_id = str(uuid.uuid4())
        if start_date is None:
            start_date = "%s-01" % str(date)[:7]
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO tasks (id, platform, date, start_date, venue, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (task_id, platform, date, start_date, venue, datetime.now())
            )
            conn.commit()
        finally:
            conn.close()
        return task_id

    def mark_running(self, task_id: str):
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE tasks SET status='running', started_at=? WHERE id=?",
                (datetime.now(), task_id)
            )
            conn.commit()
        finally:
            conn.close()

    def mark_success(self, task_id: str):
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE tasks SET status='success', finished_at=?, progress=100 "
                "WHERE id=? AND status IN ('running','pending')",
                (datetime.now(), task_id)
            )
            conn.commit()
        finally:
            conn.close()

    def is_active(self, task_id: str) -> bool:
        """判断任务是否仍允许后台线程写入结果。"""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            return bool(row and row["status"] in ("pending", "running"))
        finally:
            conn.close()

    def complete_with_summary(
        self,
        task_id: str,
        platform: str,
        date: str,
        rows: List[tuple],
    ) -> bool:
        """原子替换某平台某日结果，并将任务置为成功。

        事务内重新检查任务状态。若任务已超时/停止，则不写入任何结果。
        rows 为 ``[(venue, metrics_json), ...]``。
        """
        conn = get_connection()
        now = datetime.now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute(
                "SELECT status, start_date FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not task or task["status"] not in ("pending", "running"):
                conn.rollback()
                return False

            # 月累计值默认不得回退；货款、远程取币及负数按业务规则放行。
            # 在删除旧快照前校验，异常结果只能让任务失败，不能覆盖上一份可用数据。
            expected_period_start = str(task["start_date"] or "").strip()
            month_start = "%s-01" % str(date)[:7]
            # 对照同日已有版本和同月最近一份可信快照。只对同月、同一累计起始日
            # 的记录比较；这样断采后仍能拦住累计回退，也不会跨月误报。
            candidate_rows = conn.execute(
                "SELECT date, venue, metrics_json, period_start "
                "FROM daily_summary WHERE platform=? AND date>=? AND date<=? "
                "ORDER BY date DESC, updated_at DESC, id DESC",
                (platform, month_start, date),
            ).fetchall()
            previous = {}
            for row in candidate_rows:
                row_date = str(row["date"] or "")
                if row_date[:7] != str(date)[:7]:
                    continue
                previous_period_start = str(row["period_start"] or "").strip()
                if expected_period_start and expected_period_start[:7] != row_date[:7]:
                    continue
                # 月初切换时，上月快照不参与比较；旧快照没有 period_start 时仍参与，
                # 保持历史数据的保护能力。
                if (
                    previous_period_start
                    and expected_period_start
                    and previous_period_start != expected_period_start
                ):
                    continue
                try:
                    parsed_metrics = json.loads(row["metrics_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    parsed_metrics = {}
                previous.setdefault(str(row["venue"]), (
                    parsed_metrics if isinstance(parsed_metrics, dict) else {}
                ))
            regressions = []
            seen_venues = set()
            for venue, metrics_json in rows:
                venue_name = str(venue or "").strip()
                if venue_name in seen_venues:
                    raise ValueError("适配器返回重复门店：%s" % venue_name)
                seen_venues.add(venue_name)
                old_metrics = previous.get(venue_name)
                if not isinstance(old_metrics, dict):
                    continue
                try:
                    current_metrics = json.loads(metrics_json or "{}")
                except (TypeError, json.JSONDecodeError):
                    current_metrics = {}
                if not isinstance(current_metrics, dict):
                    continue
                for metric, current_value in current_metrics.items():
                    previous_value = old_metrics.get(metric)
                    if isinstance(current_value, bool) or isinstance(previous_value, bool):
                        continue
                    try:
                        current_number = float(current_value)
                        previous_number = float(previous_value)
                    except (TypeError, ValueError):
                        continue
                    if current_number < previous_number and not allows_decrease(platform, metric, current_number):
                        regressions.append(
                            "%s/%s: %.2f -> %.2f"
                            % (venue_name, metric, previous_number, current_number)
                        )
            if regressions:
                raise ValueError(
                    "月累计数据较前一天或同月最近可信快照回退，拒绝入库：%s"
                    % "；".join(regressions[:20])
                )
            conn.execute(
                "DELETE FROM daily_summary WHERE platform=? AND date=?",
                (platform, date),
            )
            if rows:
                conn.executemany(
                    "INSERT INTO daily_summary "
                    "(id, date, venue, platform, metrics_json, raw_file, period_start, source_task_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            str(uuid.uuid4()), date, venue, platform, metrics_json,
                            None, task["start_date"], task_id, now,
                        )
                        for venue, metrics_json in rows
                    ],
                )
            conn.execute(
                "UPDATE tasks SET status='success', finished_at=?, progress=100 "
                "WHERE id=? AND status IN ('running','pending')",
                (now, task_id),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_failed(self, task_id: str, error_msg: str = ""):
        error_msg = redact_sensitive_text(error_msg)
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE tasks SET status='failed', finished_at=?, error_msg=? "
                "WHERE id=? AND status IN ('running','pending','timeout_requested')",
                (datetime.now(), error_msg, task_id)
            )
            conn.commit()
        finally:
            conn.close()

    def update_step(
        self,
        task_id: str,
        step: str,
        progress: Optional[int] = None,
    ):
        """更新任务步骤；progress 为 None 时只更新 step，不覆盖进度"""
        step = redact_sensitive_text(step)
        conn = get_connection()
        try:
            if progress is None:
                conn.execute(
                    "UPDATE tasks SET step=? "
                    "WHERE id=? AND status='running'",
                    (step, task_id)
                )
            else:
                conn.execute(
                    "UPDATE tasks SET step=?, progress=? "
                    "WHERE id=? AND status='running'",
                    (step, progress, task_id)
                )
            conn.commit()
        finally:
            conn.close()

    def increment_retry(self, task_id: str) -> None:
        """记录一次调度器级重试，便于页面和日志追踪。"""
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE tasks SET retry_count=COALESCE(retry_count, 0)+1 "
                "WHERE id=? AND status='running'",
                (task_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def get(self, task_id: str) -> Optional[dict]:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_by_date(self, date: str) -> List[dict]:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE date=? ORDER BY created_at DESC",
                (date,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def list_running_and_recent(self, limit: int = 50) -> List[dict]:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def save_summary(self, platform: str, date: str, venue: str, metrics_json: str, raw_file: str = None):
        """保存采集结果到 daily_summary（UPSERT）"""
        conn = get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id FROM daily_summary WHERE date=? AND venue=? AND platform=?",
                (date, venue, platform)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE daily_summary SET metrics_json=?, raw_file=?, updated_at=? WHERE id=?",
                    (metrics_json, raw_file, datetime.now(), existing["id"])
                )
            else:
                conn.execute(
                    "INSERT INTO daily_summary (id, date, venue, platform, metrics_json, raw_file, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), date, venue, platform, metrics_json, raw_file, datetime.now())
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_summary(self, date: str) -> List[dict]:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM daily_summary WHERE date=? ORDER BY venue, platform",
                (date,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def delete_summary_by_platform_date(self, platform: str, date: str) -> int:
        """删除某平台某日期的全部汇总记录（用于采集前清理旧数据，防止残留）"""
        conn = get_connection()
        try:
            cur = conn.execute(
                "DELETE FROM daily_summary WHERE platform=? AND date=?",
                (platform, date)
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def request_stop(self, task_id: str) -> bool:
        """请求协作式停止；底层调用真正返回前不宣称已经停止。"""
        conn = get_connection()
        try:
            cur = conn.execute(
                "UPDATE tasks SET status='stop_requested', "
                "step='正在等待当前平台调用释放', error_msg='用户请求停止' "
                "WHERE id=? AND status IN ('pending','running')",
                (task_id,)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_stopped(self, task_id: str) -> bool:
        """兼容旧调用名：现在只发送停止请求。"""
        return self.request_stop(task_id)

    def request_timeout(self, task_id: str, timeout_seconds: int) -> bool:
        conn = get_connection()
        try:
            cur = conn.execute(
                "UPDATE tasks SET status='timeout_requested', "
                "step='任务超时，正在等待底层调用释放', error_msg=? "
                "WHERE id=? AND status IN ('pending','running')",
                ("超时 {}s".format(timeout_seconds), task_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def finalize_inactive(self, task_id: str) -> Optional[str]:
        """底层调用释放后，把请求中状态转换为真实终态。"""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                return None
            status = str(row["status"])
            if status == "stop_requested":
                conn.execute(
                    "UPDATE tasks SET status='stopped', finished_at=?, "
                    "step='已停止', error_msg='用户手动终止' WHERE id=?",
                    (datetime.now(), task_id),
                )
                conn.commit()
                return "stopped"
            if status == "timeout_requested":
                conn.execute(
                    "UPDATE tasks SET status='failed', finished_at=?, "
                    "step='已结束', error_msg=COALESCE(NULLIF(error_msg,''),'任务超时') "
                    "WHERE id=?",
                    (datetime.now(), task_id),
                )
                conn.commit()
                return "failed"
            return status
        finally:
            conn.close()
