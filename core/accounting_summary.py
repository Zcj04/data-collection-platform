# -*- coding: utf-8 -*-
"""核算汇总的数据范围：所选自然月截至目标日的最后一份真实快照。"""

import json
import math
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set

from core.collection_settings import list_settings
from core.config import get as config_get
from core.db import get_connection


_STATUS_LABELS = {
    "current": "已截至目标日",
    "stale": "待核对（截至更早日期）",
    "task_failed": "待核对（最新任务失败）",
    "disabled": "已停采",
    "missing": "缺少数据",
    "no_visible_data": "当前范围无数据",
}


def _default_platform_catalog() -> List[tuple[str, str]]:
    configured = config_get("platforms", {}) or {}
    if not isinstance(configured, dict):
        return []
    return [
        (str(platform), str((item or {}).get("name") or platform))
        for platform, item in configured.items()
        if isinstance(item, dict)
    ]


def _build_summary_metadata(
    target_date: str,
    month_start: str,
    rows: Iterable[Dict[str, Any]],
    venue_scope: Optional[Iterable[str]],
    platform_catalog: Optional[Iterable[tuple[str, str]]],
    task_statuses: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """构建汇总数据的实际截止日和平台状态，不改变汇总数值。"""
    selected_rows = list(rows)
    catalog = [
        (str(platform), str(name))
        for platform, name in (platform_catalog or _default_platform_catalog())
    ]
    settings = {
        item["id"]: item
        for item in list_settings(catalog)
    }
    by_platform: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in selected_rows:
        by_platform[str(row.get("platform") or "")].append(row)

    platforms: List[Dict[str, Any]] = []
    known_platforms = set()
    scope_limited = venue_scope is not None
    task_statuses = task_statuses or {}
    failed_task_statuses = {"failed", "stopped", "timeout_requested"}
    for platform, name in catalog:
        known_platforms.add(platform)
        platform_rows = by_platform.get(platform, [])
        dates = [str(row.get("date") or "") for row in platform_rows if row.get("date")]
        latest_date = max(dates) if dates else None
        enabled = bool(settings.get(platform, {}).get("enabled", True))
        task = task_statuses.get(platform) or {}
        task_status = str(task.get("status") or "")
        if not enabled:
            status = "disabled"
        elif task_status in failed_task_statuses:
            status = "task_failed"
        elif latest_date is None:
            status = "no_visible_data" if scope_limited else "missing"
        elif latest_date == target_date:
            status = "current"
        else:
            status = "stale"
        platforms.append({
            "platform": platform,
            "name": name,
            "latest_date": latest_date,
            "venue_count": len({str(row.get("venue") or "") for row in platform_rows if row.get("venue")}),
            "status": status,
            "status_label": _STATUS_LABELS[status],
            "enabled": enabled,
            "task_status": task_status or None,
        })

    # 保留配置外但确实出现在数据中的平台，避免状态元数据掩盖历史数据。
    for platform in sorted(set(by_platform) - known_platforms):
        platform_rows = by_platform[platform]
        dates = [str(row.get("date") or "") for row in platform_rows if row.get("date")]
        latest_date = max(dates) if dates else None
        status = "current" if latest_date == target_date else "stale"
        platforms.append({
            "platform": platform,
            "name": platform,
            "latest_date": latest_date,
            "venue_count": len({str(row.get("venue") or "") for row in platform_rows if row.get("venue")}),
            "status": status,
            "status_label": _STATUS_LABELS[status],
            "enabled": True,
            "task_status": None,
        })

    actual_dates = [item["latest_date"] for item in platforms if item["latest_date"]]
    return {
        "target_date": target_date,
        "period_start": month_start,
        "scope_limited": scope_limited,
        "pair_count": len(selected_rows),
        "venue_count": len({str(row.get("venue") or "") for row in selected_rows if row.get("venue")}),
        "expected_platform_count": len(platforms),
        "current_platform_count": sum(item["status"] == "current" for item in platforms),
        "cutoff_date_min": min(actual_dates) if actual_dates else None,
        "cutoff_date_max": max(actual_dates) if actual_dates else None,
        "platforms": platforms,
    }


def load_period_summary_data(
    target_date: str,
    venue_scope: Optional[Iterable[str]] = None,
    include_metadata: bool = False,
    platform_catalog: Optional[Iterable[tuple[str, str]]] = None,
) -> Any:
    """按门店和平台读取所选月截至目标日的最后一条累计数据。"""
    parsed_date = datetime.strptime(target_date, "%Y-%m-%d")
    month_start = parsed_date.replace(day=1).strftime("%Y-%m-%d")

    scope = {str(value).strip() for value in (venue_scope or []) if str(value).strip()}
    rows: List[Dict[str, Any]] = []
    task_statuses: Dict[str, Dict[str, Any]] = {}
    if venue_scope is None or scope:
        conn = get_connection()
        try:
            scope_clause = ""
            params: List[Any] = [month_start, target_date]
            if venue_scope is not None:
                placeholders = ",".join("?" for _ in scope)
                scope_clause = f" AND venue IN ({placeholders})"
                params.extend(sorted(scope))
            rows = [dict(row) for row in conn.execute(
                "SELECT d.date, d.venue, d.platform, d.metrics_json "
                "FROM daily_summary d "
                "INNER JOIN ("
                "  SELECT venue, platform, MAX(date) AS latest_date "
                "  FROM daily_summary WHERE date>=? AND date<=?" + scope_clause + " "
                "  GROUP BY venue, platform"
                ") latest ON latest.venue=d.venue "
                "AND latest.platform=d.platform AND latest.latest_date=d.date " +
                ("WHERE d.venue IN (" + ",".join("?" for _ in scope) + ") " if venue_scope is not None else "") +
                "ORDER BY d.venue, d.platform",
                tuple(params + (sorted(scope) if venue_scope is not None else [])),
            ).fetchall()]
            for row in conn.execute(
                "SELECT platform, status, error_msg FROM tasks WHERE date=? "
                "ORDER BY platform, created_at DESC, id DESC",
                (target_date,),
            ).fetchall():
                task_statuses.setdefault(str(row["platform"]), dict(row))
        finally:
            conn.close()

    data_list: List[Dict[str, Any]] = []
    for row in rows:
        item = json.loads(row["metrics_json"])
        if isinstance(item, dict):
            item["场地"] = row["venue"]
            data_list.append(item)
    if include_metadata:
        return data_list, _build_summary_metadata(
            target_date,
            month_start,
            rows,
            venue_scope,
            platform_catalog,
            task_statuses,
        )
    return data_list


def _is_nonzero_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(float(value)) and float(value) != 0
    if not isinstance(value, str):
        return False

    text = value.strip().replace(",", "")
    if text.endswith("%"):
        text = text[:-1].strip()
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1].strip()
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", text):
        return False
    number = float(text)
    return math.isfinite(number) and number != 0


def accounting_venue_scope(data_list: Iterable[Dict[str, Any]]) -> Set[str]:
    """只返回所选期间至少有一项非零数值的门店。"""
    venues: Set[str] = set()
    for item in data_list:
        if not isinstance(item, dict):
            continue
        venue = str(item.get("场地") or "").strip()
        if venue and any(
            _is_nonzero_number(value)
            for key, value in item.items()
            if key != "场地"
        ):
            venues.add(venue)
    return venues
